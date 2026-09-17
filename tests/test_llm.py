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
    result = types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)
    return types.SimpleNamespace(run=lambda *a, **k: result,
                                 TimeoutExpired=subprocess.TimeoutExpired)


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
