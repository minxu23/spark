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
import ssl
import subprocess
import urllib.error
import urllib.request
from typing import Optional

from core.keys import read_key_file  # noqa: F401  （给调用方当统一入口用）

OPENROUTER_API_BASE = "https://openrouter.ai/api/v1"
DEFAULT_OLLAMA_HOST = "http://localhost:11434"


class LLMError(RuntimeError):
    pass


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


def _call_claude_cli(prompt: str, model: Optional[str], timeout: int = 600) -> str:
    cmd = ["claude", "-p", "--no-session-persistence", "--output-format", "text"]
    if model:
        cmd += ["--model", model]
    try:
        result = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as e:
        raise LLMError("找不到 claude 命令行工具，请确认已安装 Claude Code CLI，或改用 Anthropic API Key") from e
    except subprocess.TimeoutExpired as e:
        raise LLMError(f"claude -p 调用超时（>{timeout}s）") from e
    if result.returncode != 0:
        # 报错有时走 stdout（登录过期就是这样），两边都要读
        err = ((result.stderr or "").strip() + " " + (result.stdout or "").strip()).strip()
        low = err.lower()
        if any(k in low for k in _AUTH_HINTS):
            raise LLMError("本地 claude CLI 未登录或登录已过期，请先在终端运行 `claude /login`，"
                           f"或改用 API Key 后端。原始信息：{err[:200]}")
        raise LLMError(f"claude -p 调用失败（退出码 {result.returncode}）：{err[:400] or '没有任何输出'}")
    return (result.stdout or "").strip()


def _call_anthropic_api(prompt: str, api_key: str, model: str, max_tokens: int = 4000) -> str:
    try:
        import anthropic
    except ImportError as e:
        raise LLMError("未安装 anthropic 库（pip install anthropic）") from e
    if not api_key:
        raise LLMError("未提供 Anthropic API Key")
    client = anthropic.Anthropic(api_key=api_key)
    args = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    try:
        resp = client.messages.create(thinking={"type": "disabled"}, **args)
    except Exception as e:
        # 个别模型/较老的 SDK 不接受 thinking 参数；只在报错确实指向它时才退回重试一次，
        # 免得把真正的调用失败（额度、鉴权、模型名写错）也吞掉重来一遍
        if "thinking" not in str(e).lower():
            raise LLMError(f"Anthropic API 调用失败：{e}") from e
        try:
            resp = client.messages.create(**args)
        except Exception as e2:
            raise LLMError(f"Anthropic API 调用失败：{e2}") from e2
    return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()


def _call_openai_compatible_api(prompt: str, api_key: str, model: str, api_base: str,
                                max_tokens: int = 4000, timeout: int = 600) -> str:
    """调用实现 OpenAI Chat Completions 协议的第三方服务。"""
    if not api_key:
        raise LLMError("未提供第三方 API Key")
    if not api_base:
        raise LLMError("未提供第三方 API Base URL")
    if not model:
        raise LLMError("未提供第三方模型名")

    base = api_base.strip().rstrip("/")
    endpoint = base if base.endswith("/chat/completions") else base + "/chat/completions"
    body = json.dumps(
        {"model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": max_tokens},
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        endpoint, data=body, method="POST",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
                 "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ssl_context()) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:800]
        raise LLMError(f"第三方 API 请求失败（HTTP {e.code}）：{detail}") from e
    except urllib.error.URLError as e:
        raise LLMError(f"无法连接第三方 API：{e.reason}") from e
    except (TimeoutError, json.JSONDecodeError) as e:
        raise LLMError(f"第三方 API 响应异常：{e}") from e

    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        detail = json.dumps(payload, ensure_ascii=False)[:800]
        raise LLMError(f"第三方 API 返回格式不兼容：{detail}") from e
    if isinstance(content, list):
        content = "".join(part.get("text", "") if isinstance(part, dict) else str(part) for part in content)
    text = str(content or "").strip()
    if not text:
        # 思考型模型（kimi-k3、glm 等）的思考过程也算进 max_tokens，budget 不够就只剩空正文。
        # 这里明说原因，免得上层只看到一句"失败"却不知道该调什么。
        reason = ""
        try:
            reason = payload["choices"][0].get("finish_reason") or ""
        except (KeyError, IndexError, TypeError):
            pass
        extra = "（finish_reason=length，多半是思考过程占满了 max_tokens，换个非思考型模型或减少单次输入）" \
            if reason == "length" else f"（finish_reason={reason or '未知'}）"
        raise LLMError(f"第三方 API 返回空内容{extra}")
    return text


def _call_ollama_api(prompt: str, model: str, api_base: str, timeout: int = 600) -> str:
    """调用本地/局域网 Ollama（原生 /api/chat 协议，无需 API Key）。"""
    if not model:
        raise LLMError("未指定 Ollama 模型名（例如 llama3.1，需先 `ollama pull` 到本地）")
    base = (api_base or DEFAULT_OLLAMA_HOST).strip().rstrip("/")
    endpoint = base if base.endswith("/api/chat") else base + "/api/chat"
    body = json.dumps(
        {"model": model, "messages": [{"role": "user", "content": prompt}], "stream": False},
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
    return str(content or "").strip()


def list_ollama_models(api_base: str = "") -> list[str]:
    """列出本地 Ollama 已拉取的模型名；连不上时返回空列表。"""
    base = (api_base or DEFAULT_OLLAMA_HOST).strip().rstrip("/")
    req = urllib.request.Request(base + "/api/tags", method="GET")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return []
    return [m.get("name", "") for m in payload.get("models", []) if m.get("name")]


# --------------------------------------------------------------------------
# 统一入口
# --------------------------------------------------------------------------

BACKENDS = ("cli", "api", "openrouter", "openai_compatible", "ollama")


def complete(prompt: str, backend: str, *, api_key: str = "", model: str = "",
             api_base: str = "", max_tokens: int = 4000, timeout: int = 600) -> str:
    if backend == "cli":
        text = _call_claude_cli(prompt, model or None, timeout=timeout)
    elif backend == "api":
        text = _call_anthropic_api(prompt, api_key, model or "claude-sonnet-5", max_tokens=max_tokens)
    elif backend == "openrouter":
        text = _call_openai_compatible_api(prompt, api_key, model, api_base or OPENROUTER_API_BASE,
                                           max_tokens=max_tokens, timeout=timeout)
    elif backend == "openai_compatible":
        text = _call_openai_compatible_api(prompt, api_key, model, api_base,
                                           max_tokens=max_tokens, timeout=timeout)
    elif backend == "ollama":
        text = _call_ollama_api(prompt, model, api_base, timeout=timeout)
    else:
        raise LLMError(f"未知的模型后端：{backend}")
    if not text.strip():
        # 调用本身没报错但没有任何可见文本——常见于内容量大的任务把 max_tokens 耗尽在
        # 思考/截断上。当作失败处理，让上层保留原有内容而不是用空结果覆盖掉。
        raise LLMError("模型返回了空内容（可能是这次要生成的内容较长、超过了这次调用的输出上限），请重试")
    return text
