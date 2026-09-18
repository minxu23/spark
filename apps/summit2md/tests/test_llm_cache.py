"""summit2md 的 LLM 结果缓存：这层之前完全不存在，重试/补生成/换模型都要整份重算。"""

from apps.summit2md import pipeline


def _patch(monkeypatch, calls):
    monkeypatch.setattr(pipeline, "summarize",
                        lambda prompt, backend, **kw: calls.append((prompt, kw.get("model"))) or "结果")


def test_不给缓存目录时行为不变(monkeypatch, tmp_path):
    calls = []
    _patch(monkeypatch, calls)
    for _ in range(2):
        pipeline._cached_summarize("提示词", "api", model="haiku")
    assert len(calls) == 2, "没有缓存目录就该保持老行为，每次都真调用"


def test_同样的提示词第二次不再调用模型(monkeypatch, tmp_path):
    calls = []
    _patch(monkeypatch, calls)
    out1 = pipeline._cached_summarize("提示词", "api", model="haiku", cache_dir=str(tmp_path))
    out2 = pipeline._cached_summarize("提示词", "api", model="haiku", cache_dir=str(tmp_path))
    assert out1 == out2 == "结果"
    assert len(calls) == 1


def test_换模型要重新算(monkeypatch, tmp_path):
    """"先用便宜模型跑一遍、再换强模型重跑总结"是文档里写明的用法，不能被缓存挡住。"""
    calls = []
    _patch(monkeypatch, calls)
    pipeline._cached_summarize("提示词", "api", model="haiku", cache_dir=str(tmp_path))
    pipeline._cached_summarize("提示词", "api", model="opus", cache_dir=str(tmp_path))
    assert len(calls) == 2


def test_改输出上限要重新算(monkeypatch, tmp_path):
    calls = []
    _patch(monkeypatch, calls)
    pipeline._cached_summarize("提示词", "api", model="m", max_tokens=2000, cache_dir=str(tmp_path))
    pipeline._cached_summarize("提示词", "api", model="m", max_tokens=8000, cache_dir=str(tmp_path))
    assert len(calls) == 2


def test_提示词变了要重新算(monkeypatch, tmp_path):
    calls = []
    _patch(monkeypatch, calls)
    pipeline._cached_summarize("提示词 A", "api", model="m", cache_dir=str(tmp_path))
    pipeline._cached_summarize("提示词 B", "api", model="m", cache_dir=str(tmp_path))
    assert len(calls) == 2


def test_失败不会被缓存(monkeypatch, tmp_path):
    """第一次报错之后重试，必须真的重试，而不是把错误状态记住。"""
    state = {"n": 0}

    def flaky(prompt, backend, **kw):
        state["n"] += 1
        if state["n"] == 1:
            raise pipeline.SummarizeError("临时失败")
        return "第二次成功"

    monkeypatch.setattr(pipeline, "summarize", flaky)
    try:
        pipeline._cached_summarize("提示词", "api", model="m", cache_dir=str(tmp_path))
    except pipeline.SummarizeError:
        pass
    assert pipeline._cached_summarize("提示词", "api", model="m", cache_dir=str(tmp_path)) == "第二次成功"


def test_force为True时跳过缓存真正重新调用(monkeypatch, tmp_path):
    """"重新生成一遍"要的就是这个：提示词跟上次一模一样，也要真的再打一次模型，
    不能命中缓存拿回旧文本——否则用户点"重新生成"会觉得毫无反应。"""
    calls = []
    _patch(monkeypatch, calls)
    pipeline._cached_summarize("提示词", "api", model="haiku", cache_dir=str(tmp_path))
    pipeline._cached_summarize("提示词", "api", model="haiku", cache_dir=str(tmp_path), force=True)
    assert len(calls) == 2


def test_force为True时结果仍然写回缓存(monkeypatch, tmp_path):
    """强制重新算这一次之后，缓存要跟着更新——不然下一次不带 force 的普通调用
    又会命中更早之前那次的旧结果，等于白强制了。"""
    calls = []
    _patch(monkeypatch, calls)
    pipeline._cached_summarize("提示词", "api", model="haiku", cache_dir=str(tmp_path), force=True)
    pipeline._cached_summarize("提示词", "api", model="haiku", cache_dir=str(tmp_path))
    assert len(calls) == 1, "第二次不带 force，应该命中第一次强制调用刚写回的缓存"


def test_缓存目录挂在输出目录下():
    assert pipeline.llm_cache_dir("/out/某会议").endswith("/out/某会议/.cache/llm")
