"""
五个模型后端：本地 claude CLI、Anthropic API、OpenRouter、OpenAI 兼容第三方、本地 Ollama。

这份是 summit2md/pipeline.py 与 notes2insight/llm.py 两份实现的合并版，取各自更完整的
那一半：

- claude CLI 报错同时读 stdout 和 stderr（登录过期的提示就走 stdout，只看 stderr 会
  得到一句没用的"未知错误"）——来自 notes2insight
- OpenAI 兼容路径的空内容带 finish_reason 归因（思考型模型会把 max_tokens 烧在思考
  过程上，返回空正文）——来自 notes2insight
- Anthropic 调用关掉思考过程（这里的活儿都是"按格式改写/归纳已给定内容"，不需要它；
  内容量大时思考过程会把 max_tokens 占满，返回空文本却不报错）——来自 summit2md
- 任何后端返回空文本都当失败抛出，让上层能保留原有内容而不是用空结果覆盖——来自 summit2md
"""

from __future__ import annotations

import json
import os
import re
import ssl
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from typing import Callable, Optional

from core.keys import read_key_file  # noqa: F401  （给调用方当统一入口用）

OPENROUTER_API_BASE = "https://openrouter.ai/api/v1"
DEFAULT_OLLAMA_HOST = "http://localhost:11434"
# Ollama 默认上下文很短，超出的部分会被悄悄截掉；整理稿一块原文 + 输出约 1 万 token，留足余量
OLLAMA_NUM_CTX = 32768
# 模型自带的默认温度（Qwen3 是 1.0）整理文字时容易自由发挥、越写越短；整理/小结都要求忠实
OLLAMA_TEMPERATURE = 0.3
# Anthropic API 没指定模型时用这个（页面下拉框另有模型目录，见 core/model_catalog）
DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-5-5"


class LLMError(RuntimeError):
    pass


class Stopped(LLMError):
    """用户主动点了"停止"，不是真的出错——调用方要按"用户取消"处理，不能当失败
    展示、更不能把这半途而废的结果写进缓存或产物文件里。"""


StopFlag = Optional[Callable[[], bool]]


_SSL_CTX: "ssl.SSLContext | None" = None


def ssl_context() -> ssl.SSLContext:
    """urllib 用的 SSL 上下文。macOS 官方版 Python 若没跑过 Install Certificates.command，
    系统里就没有可用的 CA，https 请求会直接 CERTIFICATE_VERIFY_FAILED；这时退回 certifi
    自带的证书（anthropic/httpx 走的也是它），避免第三方 API 一律连不上。

    注意这是 per-context 的做法，不动进程级的 SSL_CERT_FILE 环境变量——那会影响同进程里
    其它库（见 PLAN.md 里 P1 的"已知偏差"）。"""
    global _SSL_CTX
    if _SSL_CTX is None:
        ctx = ssl.create_default_context()
        if not ctx.get_ca_certs():
            try:
                import certifi
                ctx = ssl.create_default_context(cafile=certifi.where())
            except Exception:  # 没装 certifi 就维持默认行为，报错信息本身已经足够指明原因
                pass
        _SSL_CTX = ctx
    return _SSL_CTX


# --------------------------------------------------------------------------
# 各后端
# --------------------------------------------------------------------------

_AUTH_HINTS = ("not logged in", "/login", "authenticate", "oauth", "session expired")


def _call_claude_cli(prompt: str, model: Optional[str], timeout: int = 600,
                     stop_flag: StopFlag = None) -> str:
    """五个后端里只有这个是本机子进程，也只有这个能做到真正中断——点"停止"
    直接把进程杀掉，不用等它自己跑完。别的后端都是走网络请求的阻塞调用，
    Python 标准库没有干净的跨线程取消手段，做不到这一步（complete() 里
    只能在真正发起请求之前查一下 stop_flag，发出去之后就等它跑完/超时）。

    stdout/stderr 不能用 PIPE：中途轮询 stop_flag 时如果不去读 PIPE，输出
    一旦超过系统管道缓冲区（通常几十 KB，生成较长的总结很容易超过），子进程
    会卡在往管道写、我们卡在等它退出，两边互相等，死锁。改用临时文件收输出，
    没有这个缓冲区上限，也就不用额外开一个线程专门读 PIPE。
    """
    cmd = ["claude", "-p", "--no-session-persistence", "--output-format", "text"]
    if model:
        cmd += ["--model", model]
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as out_f, \
         tempfile.TemporaryFile(mode="w+", encoding="utf-8") as err_f:
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=out_f, stderr=err_f, text=True)
        except FileNotFoundError as e:
            raise LLMError("找不到 claude 命令行工具，请确认已安装 Claude Code CLI，或改用 Anthropic API Key") from e

        # 提示词通常远大于管道缓冲区，write 会一直阻塞到子进程把 stdin 读完；claude 要是
        # 停在某个交互提示上不读 stdin，主线程就永远卡在这里，timeout 和 stop_flag 都管不到。
        # 放到单独线程里写：下面的循环照常计时、查停止；进程被杀后管道断开，这个线程自己结束。
        def _feed() -> None:
            try:
                proc.stdin.write(prompt)
                proc.stdin.close()
            except (BrokenPipeError, OSError, ValueError):
                pass  # 进程可能已经因为别的原因提前退出，退出码/stderr 里会看到具体原因

        feeder = threading.Thread(target=_feed, daemon=True)
        feeder.start()

        start = time.monotonic()
        while True:
            try:
                proc.wait(timeout=0.3)
                break
            except subprocess.TimeoutExpired:
                pass
            if stop_flag and stop_flag():
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                raise Stopped("已停止")
            if time.monotonic() - start > timeout:
                proc.kill()
                raise LLMError(f"claude -p 调用超时（>{timeout}s）")

        out_f.seek(0)
        stdout = out_f.read()
        err_f.seek(0)
        stderr = err_f.read()

    if proc.returncode != 0:
        # 报错有时走 stdout（登录过期就是这样），两边都要读
        err = (stderr.strip() + " " + stdout.strip()).strip()
        low = err.lower()
        if any(k in low for k in _AUTH_HINTS):
            raise LLMError("本地 claude CLI 未登录或登录已过期，请先在终端运行 `claude /login`，"
                           f"或改用 API Key 后端。原始信息：{err[:200]}")
        raise LLMError(f"claude -p 调用失败（退出码 {proc.returncode}）：{err[:400] or '没有任何输出'}")
    return stdout.strip()


def _call_anthropic_api(prompt: str, api_key: str, model: str, max_tokens: int = 4000,
                        timeout: int = 600, images: Optional[list] = None) -> str:
    try:
        import anthropic
    except ImportError as e:
        raise LLMError("未安装 anthropic 库（pip install anthropic）") from e
    if not api_key:
        raise LLMError("未提供 Anthropic API Key")
    # SDK 默认 10 分钟超时、失败重试 2 次，一次卡住的请求最坏要等半小时；调用方传的
    # timeout 得真的生效。留 1 次重试给偶发的 529/overloaded。
    client = anthropic.Anthropic(api_key=api_key, timeout=timeout, max_retries=1)
    args = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": _anthropic_content(prompt, images)}],
    }

    def create(tokens: int):
        """关掉思考发请求。各代模型认的写法不一样：老模型认 {"type": "disabled"}；5.5 这一代
        不认，报错里会说要用 {"type": "between_tools"}；更老的 SDK/模型干脆不认 thinking 参数。
        哪种写法管用按模型记下来，后面的调用直接用，不用每次先挨一个 400。"""
        kw = dict(args, max_tokens=tokens)
        tried = _THINKING_OFF.get(model, "disabled")
        while True:
            try:
                if tried is not None:
                    kw["thinking"] = {"type": tried}
                else:
                    kw.pop("thinking", None)
                # 长总结的非流式请求可能数分钟没有数据，代理容易在约 60 秒时断开。
                # SDK 收齐事件后仍返回完整 Message；中途断流会报错，不保存半篇正文。
                with client.messages.stream(**kw) as stream:
                    resp = stream.get_final_message()
                _THINKING_OFF[model] = tried
                return resp
            except Exception as e:
                msg = str(e).lower()
                # 只在报错确实指向 thinking 参数时才换写法重试，免得把真正的调用失败
                # （额度、鉴权、模型名写错）也吞掉重来一遍
                if "thinking" not in msg:
                    raise LLMError(f"Anthropic API 调用失败：{e}") from e
                if tried == "disabled" and "between_tools" in msg:
                    tried = "between_tools"
                elif tried is not None:
                    tried = None
                else:
                    raise LLMError(f"Anthropic API 调用失败：{e}") from e

    def text_of(resp) -> str:
        return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()

    resp = create(max_tokens)
    text = text_of(resp)
    if getattr(resp, "stop_reason", "") == "max_tokens" and max_tokens < _ANTHROPIC_RETRY_MAX_TOKENS:
        # Nonempty text can still be a truncated document. Retry once with more room.
        resp = create(_ANTHROPIC_RETRY_MAX_TOKENS)
        text = text_of(resp)
    if getattr(resp, "stop_reason", "") == "max_tokens":
        raise LLMError('模型输出达到长度上限，正文尚未完成；请缩小单次生成范围后重试（未保存截断正文）')
    return text


# 思考过程把 max_tokens 耗光、正文为空时，放大到这么多再试一次
_THINKING_RETRY_MAX_TOKENS = 32000
# 空正文的重试仍限制在 20000，避免一次重试产生过高费用。
_ANTHROPIC_RETRY_MAX_TOKENS = 20000
# 各模型"关掉思考"认哪种写法：{模型: "disabled" / "between_tools" / None（不带 thinking 参数）}
_THINKING_OFF: dict = {}


def _call_openai_compatible_api(prompt: str, api_key: str, model: str, api_base: str,
                                max_tokens: int = 4000, timeout: int = 600,
                                openrouter: bool = False, images: Optional[list] = None) -> str:
    """调用实现 OpenAI Chat Completions 协议的第三方服务。

    思考型模型（Kimi、GLM 的 flash 等）的思考过程也算在 max_tokens 里，内容一长
    就会只剩思考、正文为空（finish_reason=length）。两层应对：
    - OpenRouter：请求里带 reasoning.effort=none 关掉思考；强制思考的模型会拒绝
      这个参数，那就不带参数重发一次；
    - 仍然因为 length 返回空正文时，把 max_tokens 放大到足够容纳思考过程（OpenRouter
      上同时把思考强度压到 low）再试一次。
    """
    if not api_key:
        raise LLMError("未提供第三方 API Key")
    if not api_base:
        raise LLMError("未提供第三方 API Base URL")
    if not model:
        raise LLMError("未提供第三方模型名")

    base = api_base.strip().rstrip("/")
    endpoint = base if base.endswith("/chat/completions") else base + "/chat/completions"

    def post(tokens: int, reasoning: Optional[dict]) -> dict:
        body = {"model": model, "messages": [{"role": "user", "content": _openai_content(prompt, images)}],
                "max_tokens": tokens}
        if reasoning is not None:
            body["reasoning"] = reasoning
        req = urllib.request.Request(
            endpoint, data=json.dumps(body, ensure_ascii=False).encode("utf-8"), method="POST",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
                     "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ssl_context()) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:800]
            raise _HTTPStatusError(e.code, f"第三方 API 请求失败（HTTP {e.code}）：{detail}") from e
        except urllib.error.URLError as e:
            raise LLMError(f"无法连接第三方 API：{e.reason}") from e
        except (TimeoutError, json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
            raise LLMError(f"第三方 API 响应异常：{type(e).__name__}") from e
        except ValueError:
            # 请求头/地址不合法（比如 key 里有换行）：异常原文带着头的值，不能透出
            raise LLMError("第三方 API 请求参数不合法（请检查 API Key 和接口地址）") from None

    def extract(payload: dict) -> tuple[str, str]:
        try:
            choice = payload["choices"][0]
            content = choice["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            detail = json.dumps(payload, ensure_ascii=False)[:800]
            raise LLMError(f"第三方 API 返回格式不兼容：{detail}") from e
        if isinstance(content, list):
            content = "".join(part.get("text", "") if isinstance(part, dict) else str(part) for part in content)
        return str(content or "").strip(), (choice.get("finish_reason") or "") if isinstance(choice, dict) else ""

    reasoning = {"effort": "none"} if openrouter else None
    try:
        payload = post(max_tokens, reasoning)
    except _HTTPStatusError as e:
        if reasoning is None or e.code not in (400, 422):
            raise LLMError(str(e)) from e
        reasoning = None  # 这个模型不允许关掉思考
        try:
            payload = post(max_tokens, None)
        except _HTTPStatusError as e2:
            raise LLMError(str(e2)) from e2
    text, reason = extract(payload)

    if not text and reason == "length" and max_tokens < _THINKING_RETRY_MAX_TOKENS:
        # 强制思考的模型至少把思考强度压到最低，别让思考再吃掉大头
        retry_reasoning = reasoning or ({"effort": "low"} if openrouter else None)
        try:
            text, reason = extract(post(_THINKING_RETRY_MAX_TOKENS, retry_reasoning))
        except _HTTPStatusError:
            pass  # 模型不接受这么大的上限/这个强度：按原来的空内容报错

    if not text:
        # 放大上限之后还是空：明说原因，免得上层只看到一句"失败"却不知道该调什么
        extra = "（finish_reason=length，思考过程占满了输出上限，换个非思考型模型或减少单次输入）" \
            if reason == "length" else f"（finish_reason={reason or '未知'}）"
        raise LLMError(f"第三方 API 返回空内容{extra}")
    return text


class _HTTPStatusError(LLMError):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def _call_ollama_api(prompt: str, model: str, api_base: str, timeout: int = 600) -> str:
    """调用本地/局域网 Ollama（原生 /api/chat 协议，无需 API Key）。"""
    if not model:
        raise LLMError("未指定 Ollama 模型名（例如 llama3.1，需先 `ollama pull` 到本地）")
    base = (api_base or DEFAULT_OLLAMA_HOST).strip().rstrip("/")
    endpoint = base if base.endswith("/api/chat") else base + "/api/chat"
    body = json.dumps(
        # think=False：Qwen3 这类会先"思考"的模型关掉思考，省一半时间，也免得思考过程混进正文
        {"model": model, "messages": [{"role": "user", "content": prompt}], "stream": False,
         "think": False, "options": {"num_ctx": OLLAMA_NUM_CTX, "temperature": OLLAMA_TEMPERATURE}},
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        endpoint, data=body, method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:800]
        raise LLMError(f"Ollama 请求失败（HTTP {e.code}）：{detail}") from e
    except urllib.error.URLError as e:
        raise LLMError(f"无法连接本地 Ollama（{base}）：{e.reason}；请确认已运行 `ollama serve`") from e
    except (TimeoutError, json.JSONDecodeError) as e:
        raise LLMError(f"Ollama 响应异常：{e}") from e

    try:
        content = payload["message"]["content"]
    except (KeyError, TypeError) as e:
        detail = json.dumps(payload, ensure_ascii=False)[:800]
        raise LLMError(f"Ollama 返回格式不兼容：{detail}") from e
    # 不支持 think 参数的旧版 Ollama 会把思考过程放进正文
    return strip_think(str(content or ""))


def list_ollama_models(api_base: str = "") -> list[str]:
    """列出本地 Ollama 已拉取的模型名；连不上时返回空列表。"""
    base = (api_base or DEFAULT_OLLAMA_HOST).strip().rstrip("/")
    req = urllib.request.Request(base + "/api/tags", method="GET")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            payload = json.loads(resp.read(1 << 20).decode("utf-8"))   # 地址是用户填的，别无限读
    except (urllib.error.URLError, OSError, ValueError):
        return []
    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        return []
    return [m.get("name", "") for m in models if isinstance(m, dict) and m.get("name")]


# --------------------------------------------------------------------------
# 统一入口
# --------------------------------------------------------------------------

BACKENDS = ("cli", "api", "openrouter", "openai_compatible", "ollama")


_CJK = r"一-鿿㐀-䶿"
_RE_CJK_LATIN_SPACE = re.compile(
    rf"(?<=[{_CJK}])[ \t]+(?=[A-Za-z0-9])|(?<=[A-Za-z0-9])[ \t]+(?=[{_CJK}])"
)
_RE_CODE_SPAN = re.compile(r"```.*?```|`[^`\n]*`", re.S)


_RE_THINK_PAIR = re.compile(r"<think>.*?</think>", re.S | re.I)
_RE_THINK_CLOSE = re.compile(r"</think>", re.I)
_RE_THINK_OPEN = re.compile(r"<think>", re.I)


def strip_think(text: str) -> str:
    """去掉混进正文的思考过程。思考型模型（Qwen3 一类）关了思考也不一定听：
    - 成对的 <think>…</think>：整块去掉；
    - 只剩一个 </think>（开头的 <think> 在对话模板里，没出现在输出里）：它前面全是思考，
      只留最后一个 </think> 后面的。2026-10 张小珺 148、152 两期就是这样：思考里先
      打了一遍草稿、写到半句被截断，然后 </think>，再完整写一遍，整理稿里内容重复了一遍；
    - 只剩一个 <think>、没有收尾（思考写到上限被截断）：它后面全是思考，只留前面的。
    结果可能是空字符串，交给调用方按「空内容」处理。"""
    text = _RE_THINK_PAIR.sub("", text)
    closes = list(_RE_THINK_CLOSE.finditer(text))
    if closes:
        text = text[closes[-1].end():]
    m = _RE_THINK_OPEN.search(text)
    if m:
        text = text[:m.start()]
    return text.strip()


def _strip_cjk_latin_spacing(text: str) -> str:
    """模型习惯在中文和紧邻的英文/数字之间加一个空格（常见的中文排版惯例），但这不是
    这几个 app 想要的输出风格，这里统一去掉。跳过代码块（```围栏和行内`code`），
    避免动到代码里本来就需要的空格。
    """
    parts = _RE_CODE_SPAN.split(text)
    codes = _RE_CODE_SPAN.findall(text)
    out = [_RE_CJK_LATIN_SPACE.sub("", p) for p in parts]
    result = []
    for i, p in enumerate(out):
        result.append(p)
        if i < len(codes):
            result.append(codes[i])
    return "".join(result)


def _anthropic_content(prompt: str, images: Optional[list]):
    """images：JPEG 的 bytes，放在文字前面（跟手机 LLM.swift 的写法一样）。"""
    if not images:
        return prompt
    import base64
    return [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                         "data": base64.b64encode(img).decode("ascii")}} for img in images] \
        + [{"type": "text", "text": prompt}]


def _openai_content(prompt: str, images: Optional[list]):
    """OpenAI 协议的图片写法：data URL，跟在文字后面（同手机 LLM.swift）。"""
    if not images:
        return prompt
    import base64
    return [{"type": "text", "text": prompt}] + [
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(img).decode("ascii")}}
        for img in images]


def complete(prompt: str, backend: str, *, api_key: str = "", model: str = "",
             api_base: str = "", max_tokens: int = 4000, timeout: int = 600,
             stop_flag: StopFlag = None, images: Optional[list] = None) -> str:
    """images：api、openrouter、openai_compatible 后端会把图片发给模型（模型得支持看图）；cli、ollama 只发文字。

    stop_flag 只有 cli 后端能在调用过程中真正生效（见 _call_claude_cli）；其它
    后端在这里只做一次"发起网络请求之前查一眼"，发出去之后没有取消手段——多个
    连续调用之间点"停止"能立刻生效，但单次调用中途点不会打断那一次请求本身。
    """
    if stop_flag and stop_flag():
        raise Stopped("已停止")
    if api_key and any(ch.isspace() or not ch.isprintable() for ch in api_key):
        # 不能让它进 HTTP 头：那条异常会把 key 原文带进日志
        raise LLMError("API Key 里夹着换行、空格或不可见字符，请检查 key 文件或输入框")
    if backend == "cli":
        text = _call_claude_cli(prompt, model or None, timeout=timeout, stop_flag=stop_flag)
    elif backend == "api":
        text = _call_anthropic_api(prompt, api_key, model or DEFAULT_ANTHROPIC_MODEL, max_tokens=max_tokens,
                                   timeout=timeout, images=images)
    elif backend == "openrouter":
        text = _call_openai_compatible_api(prompt, api_key, model, api_base or OPENROUTER_API_BASE,
                                           max_tokens=max_tokens, timeout=timeout, openrouter=True, images=images)
    elif backend == "openai_compatible":
        text = _call_openai_compatible_api(prompt, api_key, model, api_base,
                                           max_tokens=max_tokens, timeout=timeout, images=images)
    elif backend == "ollama":
        text = _call_ollama_api(prompt, model, api_base, timeout=timeout)
    else:
        raise LLMError(f"未知的模型后端：{backend}")
    text = strip_think(text)
    if not text.strip():
        # 调用本身没报错但没有任何可见文本——常见于内容量大的任务把 max_tokens 耗尽在
        # 思考/截断上。当作失败处理，让上层保留原有内容而不是用空结果覆盖掉。
        raise LLMError("模型返回了空内容（可能是这次要生成的内容较长、超过了这次调用的输出上限），请重试")
    return _strip_cjk_latin_spacing(text)
