import io
import json
import subprocess
import types

import pytest

from core import llm


# --------------------------------------------------------------------------
# claude CLI：报错走 stdout 的那个 bug（两个 app 里各有一份，合并时一起修掉）
# --------------------------------------------------------------------------

def _fake_run(returncode=0, stdout="", stderr=""):
    """假的 subprocess 模块：_call_claude_cli 现在用 Popen（能配合 stop_flag 轮询/
    真正杀掉进程），不再用一把梭的 subprocess.run，这里的假子进程要配合着换成
    Popen 形状——真实实现会把输出写进传进来的临时文件对象，这里也一样写进去。
    """
    out_text, err_text = stdout, stderr

    class _FakeProc:
        def __init__(self, cmd, stdin=None, stdout=None, stderr=None, text=None):
            self.returncode = returncode
            self.stdin = io.StringIO()
            if stdout is not None:
                stdout.write(out_text)
            if stderr is not None:
                stderr.write(err_text)

        def wait(self, timeout=None):
            return self.returncode

    return types.SimpleNamespace(Popen=_FakeProc, TimeoutExpired=subprocess.TimeoutExpired,
                                 PIPE=subprocess.PIPE)


def test_cli_stop_flag变True时真正终止进程(monkeypatch):
    """跟别的后端不一样，CLI 是本机子进程，点"停止"要做到真正把它杀掉，不是
    等它自己跑完再假装没看见结果。"""
    terminated = []

    class _SlowProc:
        def __init__(self, cmd, stdin=None, stdout=None, stderr=None, text=None):
            self.returncode = None
            self.stdin = io.StringIO()
            self._waits = 0

        def wait(self, timeout=None):
            self._waits += 1
            if self._waits <= 2:
                raise subprocess.TimeoutExpired(cmd="claude", timeout=timeout)
            self.returncode = -15
            return self.returncode

        def terminate(self):
            terminated.append("terminate")

        def kill(self):
            terminated.append("kill")

    monkeypatch.setattr(llm, "subprocess", types.SimpleNamespace(
        Popen=_SlowProc, TimeoutExpired=subprocess.TimeoutExpired, PIPE=subprocess.PIPE))

    calls = {"n": 0}

    def stop_flag():
        calls["n"] += 1
        return calls["n"] >= 2  # 第一次查询还没到停止的时候，第二次才点

    with pytest.raises(llm.Stopped):
        llm._call_claude_cli("hi", None, stop_flag=stop_flag)
    assert terminated == ["terminate"], "该调用 terminate() 优雅结束，不该一上来就 kill()"


def test_cli_stop_flag为None时不受影响(monkeypatch):
    monkeypatch.setattr(llm, "subprocess", _fake_run(0, stdout="正常结果"))
    assert llm._call_claude_cli("hi", None, stop_flag=None) == "正常结果"


def test_complete_stop_flag已经为True时直接抛Stopped_不发起调用(monkeypatch):
    """在别的后端（走网络请求，中途没法真正打断）上，唯一能做到的就是发起
    调用之前先看一眼——已经点了停止就压根不用再打一次请求。"""
    called = {"n": 0}

    def fake_openai_compatible(*a, **k):
        called["n"] += 1
        return "不该走到这里"

    monkeypatch.setattr(llm, "_call_openai_compatible_api", fake_openai_compatible)
    with pytest.raises(llm.Stopped):
        llm.complete("提示词", "openai_compatible", api_key="k", api_base="https://x", model="m",
                     stop_flag=lambda: True)
    assert called["n"] == 0


def test_cli_登录过期信息只在_stdout_时也能认出来(monkeypatch):
    monkeypatch.setattr(llm, "subprocess",
                        _fake_run(1, stdout="Invalid API key · Please run /login", stderr=""))
    with pytest.raises(llm.LLMError) as e:
        llm._call_claude_cli("hi", None)
    assert "未登录或登录已过期" in str(e.value)


def test_cli_其它报错带上退出码而不是一句未知错误(monkeypatch):
    monkeypatch.setattr(llm, "subprocess", _fake_run(2, stderr="something broke"))
    with pytest.raises(llm.LLMError) as e:
        llm._call_claude_cli("hi", None)
    msg = str(e.value)
    assert "退出码 2" in msg and "something broke" in msg


def test_cli_完全没有输出时不说未知错误(monkeypatch):
    monkeypatch.setattr(llm, "subprocess", _fake_run(1))
    with pytest.raises(llm.LLMError) as e:
        llm._call_claude_cli("hi", None)
    assert "没有任何输出" in str(e.value)


# --------------------------------------------------------------------------
# OpenAI 兼容：思考型模型把 max_tokens 烧光，返回空正文
# --------------------------------------------------------------------------

def _fake_urlopen(payload):
    class Resp:
        def read(self):
            return json.dumps(payload).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    return lambda req, timeout=None, context=None: Resp()


def test_第三方返回空内容时点名_finish_reason(monkeypatch):
    payload = {"choices": [{"message": {"content": ""}, "finish_reason": "length"}]}
    monkeypatch.setattr(llm.urllib.request, "urlopen", _fake_urlopen(payload))
    with pytest.raises(llm.LLMError) as e:
        llm._call_openai_compatible_api("hi", "k", "m", "https://x/v1")
    msg = str(e.value)
    assert "finish_reason=length" in msg and "思考过程" in msg


def test_第三方返回分块_content_能拼起来(monkeypatch):
    payload = {"choices": [{"message": {"content": [{"text": "前"}, {"text": "后"}]}}]}
    monkeypatch.setattr(llm.urllib.request, "urlopen", _fake_urlopen(payload))
    assert llm._call_openai_compatible_api("hi", "k", "m", "https://x/v1") == "前后"


# --------------------------------------------------------------------------
# Anthropic：默认关掉思考过程，但只在报错确实指向它时才退回重试
# --------------------------------------------------------------------------

class _FakeAnthropic:
    def __init__(self, fail_on_thinking=False, error=None):
        self.calls = []
        self._fail_on_thinking = fail_on_thinking
        self._error = error
        self.messages = types.SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self._error and len(self.calls) == 1:
            raise RuntimeError(self._error)
        if self._fail_on_thinking and "thinking" in kwargs:
            raise RuntimeError("unexpected keyword `thinking`")
        return types.SimpleNamespace(
            content=[types.SimpleNamespace(type="text", text="ok")])


def _install_anthropic(monkeypatch, client):
    monkeypatch.setitem(
        __import__("sys").modules, "anthropic",
        types.SimpleNamespace(Anthropic=lambda api_key=None: client))


def test_anthropic_默认关掉思考过程(monkeypatch):
    client = _FakeAnthropic()
    _install_anthropic(monkeypatch, client)
    assert llm._call_anthropic_api("hi", "k", "m") == "ok"
    assert client.calls[0]["thinking"] == {"type": "disabled"}


def test_模型不认_thinking_参数时退回重试一次(monkeypatch):
    client = _FakeAnthropic(fail_on_thinking=True)
    _install_anthropic(monkeypatch, client)
    assert llm._call_anthropic_api("hi", "k", "m") == "ok"
    assert len(client.calls) == 2 and "thinking" not in client.calls[1]


def test_与_thinking_无关的失败不重试(monkeypatch):
    client = _FakeAnthropic(error="credit balance is too low")
    _install_anthropic(monkeypatch, client)
    with pytest.raises(llm.LLMError) as e:
        llm._call_anthropic_api("hi", "k", "m")
    assert "credit balance" in str(e.value)
    assert len(client.calls) == 1, "只重试与 thinking 相关的失败，别把真失败也重来一遍"


# --------------------------------------------------------------------------
# 统一入口：五个后端的分派，以及空内容一律当失败
# --------------------------------------------------------------------------

@pytest.mark.parametrize("backend,fn", [
    ("cli", "_call_claude_cli"),
    ("api", "_call_anthropic_api"),
    ("openrouter", "_call_openai_compatible_api"),
    ("openai_compatible", "_call_openai_compatible_api"),
    ("ollama", "_call_ollama_api"),
])
def test_五个后端都分派到对应实现(monkeypatch, backend, fn):
    seen = []
    monkeypatch.setattr(llm, fn, lambda *a, **k: seen.append(fn) or "内容")
    assert llm.complete("hi", backend, api_key="k", model="m", api_base="https://x") == "内容"
    assert seen == [fn]


def test_openrouter_没给_api_base_时用默认地址(monkeypatch):
    seen = {}
    monkeypatch.setattr(llm, "_call_openai_compatible_api",
                        lambda p, k, m, base, **kw: seen.setdefault("base", base) or "内容")
    llm.complete("hi", "openrouter", api_key="k", model="m")
    assert seen["base"] == llm.OPENROUTER_API_BASE


def test_未知后端直接报错():
    with pytest.raises(llm.LLMError) as e:
        llm.complete("hi", "gpt5-turbo-max")
    assert "未知的模型后端" in str(e.value)


# --------------------------------------------------------------------------
# complete() 去掉模型加在中文和英文/数字之间的空格
# --------------------------------------------------------------------------

def test_complete去掉正文里中文和英文数字之间的空格(monkeypatch):
    monkeypatch.setattr(llm, "_call_claude_cli",
                        lambda *a, **k: "这是 Python 代码，运行在 3.11 版本上。")
    assert llm.complete("hi", "cli") == "这是Python代码，运行在3.11版本上。"


def test_complete不动代码块里的空格(monkeypatch):
    text = "说明文字 continue\n```\nvar x = 1 中文 a\n```\n后面 continue 还有字"
    monkeypatch.setattr(llm, "_call_claude_cli", lambda *a, **k: text)
    out = llm.complete("hi", "cli")
    assert "var x = 1 中文 a" in out
    assert out == "说明文字continue\n```\nvar x = 1 中文 a\n```\n后面continue还有字"


@pytest.mark.parametrize("backend,fn", [
    ("cli", "_call_claude_cli"),
    ("api", "_call_anthropic_api"),
    ("ollama", "_call_ollama_api"),
])
def test_任何后端返回空内容都当失败(monkeypatch, backend, fn):
    """空结果必须抛出，上层才能保留原有内容，而不是用空字符串覆盖掉已经生成好的东西。"""
    monkeypatch.setattr(llm, fn, lambda *a, **k: "   \n ")
    with pytest.raises(llm.LLMError) as e:
        llm.complete("hi", backend, api_key="k", model="m")
    assert "空内容" in str(e.value)


# --------------------------------------------------------------------------
# 思考型模型：OpenRouter 上先关思考；正文为空再放大上限重试
# --------------------------------------------------------------------------

class _Recorder:
    """按顺序返回预设响应（dict 当 JSON 正文，int 当 HTTP 错误码），记下每次请求体。"""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.bodies = []

    def __call__(self, req, timeout=None, context=None):
        self.bodies.append(json.loads(req.data.decode("utf-8")))
        r = self.responses.pop(0)
        if isinstance(r, int):
            raise llm.urllib.error.HTTPError(req.full_url, r, "bad", {}, io.BytesIO(b"reasoning not allowed"))
        return _fake_urlopen(r)(req)


def _ok(text):
    return {"choices": [{"message": {"content": text}, "finish_reason": "stop"}]}


_EMPTY_LENGTH = {"choices": [{"message": {"content": ""}, "finish_reason": "length"}]}


def test_openrouter_默认关掉思考(monkeypatch):
    rec = _Recorder(_ok("结果"))
    monkeypatch.setattr(llm.urllib.request, "urlopen", rec)
    assert llm._call_openai_compatible_api("hi", "k", "m", "https://x/v1", openrouter=True) == "结果"
    assert rec.bodies[0]["reasoning"] == {"effort": "none"}


def test_openrouter_模型不许关思考时不带参数重发(monkeypatch):
    rec = _Recorder(400, _ok("结果"))
    monkeypatch.setattr(llm.urllib.request, "urlopen", rec)
    assert llm._call_openai_compatible_api("hi", "k", "m", "https://x/v1", openrouter=True) == "结果"
    assert "reasoning" not in rec.bodies[1]


def test_思考占满上限时放大上限再试一次(monkeypatch):
    rec = _Recorder(400, _EMPTY_LENGTH, _ok("终于有正文"))
    monkeypatch.setattr(llm.urllib.request, "urlopen", rec)
    assert llm._call_openai_compatible_api("hi", "k", "m", "https://x/v1", openrouter=True) == "终于有正文"
    assert rec.bodies[2]["max_tokens"] == llm._THINKING_RETRY_MAX_TOKENS
    assert rec.bodies[2]["reasoning"] == {"effort": "low"}


def test_第三方兼容接口不带_reasoning_参数_重试后仍为空才报错(monkeypatch):
    rec = _Recorder(_EMPTY_LENGTH, _EMPTY_LENGTH)
    monkeypatch.setattr(llm.urllib.request, "urlopen", rec)
    with pytest.raises(llm.LLMError, match="finish_reason=length"):
        llm._call_openai_compatible_api("hi", "k", "m", "https://x/v1")
    assert all("reasoning" not in b for b in rec.bodies) and len(rec.bodies) == 2


def test_放大上限被拒时按原来的空内容报错(monkeypatch):
    rec = _Recorder(_EMPTY_LENGTH, 400)
    monkeypatch.setattr(llm.urllib.request, "urlopen", rec)
    with pytest.raises(llm.LLMError, match="返回空内容"):
        llm._call_openai_compatible_api("hi", "k", "m", "https://x/v1")
