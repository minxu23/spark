"""报告文件名：Step 5「报告」面板里显示的那个文件名。

2026-09 报过一次 bug："文件名不够智能，甚至有些扯"。根因是两处：

1. 文件名取自 cfg.focus（喂给模型的指令，README 明确鼓励写成一整段报告规格），
   而不是模型专门写来当标题读的 title——focus 常常是一整段话，直接截前 28 个
   字符会把一句话腰斩得不知所云，而且完全无视了模型已经给出的干净标题。
2. 解析框架失败时的默认 title 自带"技术洞察报告："前缀，文件名模板又会再拼一次
   同样的字样，不处理就是"技术洞察报告_技术洞察报告_xxx"。

这两条各写一个用例，另外补一条真实的、最常见的组合场景（有标题、也填了关注点），
按 bug 报告时的原话复现。
"""

import os

from apps.notes2insight import pipeline


def _cfg(**kw):
    base = dict(vault_root="/vault", notes=[])
    base.update(kw)
    return pipeline.RunConfig(**base)


def _assemble(title, cfg):
    """凑齐 assemble() 需要的其它参数，只关心 title/focus 怎么影响文件名。"""
    content, fname = pipeline.assemble(
        cfg=cfg, refs=[], framework="", title=title, subtitle="",
        clusters=[pipeline.Cluster(no=1, topic="测试主题")], sections=["正文"],
        elapsed=1.0,
    )
    return fname


# --------------------------------------------------------------------------
# _slug 本身
# --------------------------------------------------------------------------

def test_slug_截断后不留悬空下划线():
    # 精心构造一个会让内部下划线正好落在截断点上的例子
    text = "推理成本" + "，".join(["段落"] * 20)  # 逗号会被换成下划线
    s = pipeline._slug(text, limit=10)
    assert not s.endswith("_"), f"截断后不该留下悬空下划线：{s!r}"


def test_slug_为空时有兜底():
    assert pipeline._slug("") == "技术洞察报告"
    assert pipeline._slug("：，。") == "技术洞察报告"  # 全是会被吃掉的标点


def test_剥掉退回默认标题时的前缀():
    stripped = pipeline._RE_TITLE_LABEL_PREFIX.sub("", "技术洞察报告：AI 推理成本")
    assert stripped == "AI 推理成本"
    # 真实标题不该被误伤——这句话本身讨论"技术洞察报告"这个概念，不是那个前缀
    untouched = pipeline._RE_TITLE_LABEL_PREFIX.sub("", "我们需要更多技术洞察报告")
    assert untouched == "我们需要更多技术洞察报告"


# --------------------------------------------------------------------------
# assemble() 产出的文件名
# --------------------------------------------------------------------------

def test_最常见场景_有真实标题也填了关注点_文件名用标题不用关注点():
    """这是 2026-09 报的那次 bug 的真实触发场景：模型已经给出一个干净标题，
    用户还按 README 建议填了一整段关注点——文件名不该扯上关注点。"""
    cfg = _cfg(focus="这批材料里关于推理成本与自研 ASIC 的技术路线分歧，重点看双方证据链")
    fname = _assemble("推理单价两年掉两个数量级", cfg)
    assert fname.startswith("技术洞察报告_推理单价两年掉两个数量级_")
    assert "这批材料" not in fname, "文件名不该出现关注点里的字样"
    assert fname.count("技术洞察报告") == 1


def test_解析失败退回默认标题时不会把前缀拼两遍():
    cfg = _cfg(topic="AI 推理成本与 Token 经济学")
    fname = _assemble("", cfg)  # title 为空 → assemble() 内部退回默认标题
    assert fname.count("技术洞察报告") == 1, f"前缀被拼了不止一次：{fname}"


def test_文件名总是以_md_结尾_且不含路径分隔符():
    cfg = _cfg(focus="随便填点什么/带着奇怪的\\字符：还有，逗号。句号")
    fname = _assemble("一个标题", cfg)
    assert fname.endswith(".md")
    assert "/" not in fname and "\\" not in fname
