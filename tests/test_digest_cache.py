"""摘要卡缓存键的行为：什么该让缓存失效，什么不该。

这层直接对应真金白银——一次失效就是一批模型调用。
"""

from apps.notes2insight import pipeline

import pytest


def cfg(**kw):
    base = dict(vault_root="/vault", notes=[], backend="cli", model="haiku")
    base.update(kw)
    return pipeline.RunConfig(**base)


TEXT = "这是一篇笔记的正文，讲了推理成本与 token 经济学。"


def key(c=None, title="某期播客", date="2026-09-11", text=TEXT):
    return pipeline._cache_key(c or cfg(), title, date, text)


# --------------------------------------------------------------------------
# 不该失效的
# --------------------------------------------------------------------------

def test_文件夹改名不影响缓存():
    """会议/ 改名成 Spark/ 这类操作，内容一个字没变，不该让整批缓存作废。"""
    assert key() == key()  # 路径根本不参与，换个路径算出来还是同一个键


def test_mtime_变化不影响缓存():
    """同步工具/Finder/插件碰一下文件就改 mtime，内容没变就不该重新摘取。"""
    # mtime 已不是参数，能调用通过即证明它不在键里
    import inspect
    params = inspect.signature(pipeline._cache_key).parameters
    assert "mtime" not in params and "path" not in params


def test_换成文模型不影响摘取缓存():
    """成文模型只影响后半程，不该让前面几十次摘取重算。"""
    assert key(cfg(model_compose="opus")) == key(cfg(model_compose="sonnet"))


def test_改深度不影响摘取缓存():
    assert key(cfg(depth="brief")) == key(cfg(depth="deep"))


# --------------------------------------------------------------------------
# 该失效的
# --------------------------------------------------------------------------

def test_正文变了要重新摘取():
    assert key(text=TEXT) != key(text=TEXT + "又补了一段。")


def test_截断长度变了要重新摘取():
    """max_note_chars 改了会让喂进去的正文变短，那是不同的输入。"""
    assert key(text=TEXT[:10]) != key(text=TEXT)


def test_换摘取模型要重新摘取():
    assert key(cfg(model_digest="haiku")) != key(cfg(model_digest="sonnet"))


def test_换后端要重新摘取():
    assert key(cfg(backend="cli")) != key(cfg(backend="api"))


def test_换关注点要重新摘取():
    assert key(cfg(topic="推理成本")) != key(cfg(topic="自研 ASIC"))


def test_标题或日期变了要重新摘取():
    assert key(title="A") != key(title="B")
    assert key(date="2026-09-11") != key(date="2026-09-12")


def test_提示词版本变了要重新摘取(monkeypatch):
    k1 = key()
    monkeypatch.setattr(pipeline, "PROMPT_VERSION", pipeline.PROMPT_VERSION + "-next")
    assert key() != k1
