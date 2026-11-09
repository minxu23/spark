"""电脑存剪藏（core/clip_compose.py）跟手机（Clips.swift、ClipTranslate.swift）写法一字不差。"""

import os
import re
from datetime import date

import pytest

from core import clip_compose as cc

IOS = os.path.join(os.path.dirname(__file__), "..", "apps", "mobile-ios", "Spark")


def _swift(file):
    with open(os.path.join(IOS, file), encoding="utf-8") as f:
        return f.read()


def _literal(file, name):
    """Swift 多行字符串：去掉跟结尾 \"\"\" 一样的缩进，最后一个换行不算（Swift 的规则）。"""
    m = re.search(rf'static let {name} = """\n(.*?)\n( *)"""', _swift(file), re.S)
    assert m, name
    ind = m.group(2)
    lines = [ln[len(ind):] if ln.startswith(ind) else ln for ln in m.group(1).split("\n")]
    return "\n".join(lines).replace('\\"', '"')


def test_prompts_same_as_phone():
    assert _literal("Clips.swift", "summaryPrompt") == cc.SUMMARY_PROMPT
    assert _literal("ClipTranslate.swift", "prompt") == cc.TRANSLATE_PROMPT
    src = _swift("Clips.swift")
    assert f'prompt = "{cc.TITLE_ASK[:-2]}\\n\\n" + prompt' in src
    assert f'prompt = "{cc.IMAGES_ASK[:-2]}\\n\\n" + prompt' in src
    assert "已经用过的标签（合适就沿用同一个写法，不合适再起新的）：" in src and cc.EXISTING_TAGS.startswith("\n已经用过的标签")
    assert f"static let batchChars = {cc.TRANSLATE_BATCH_CHARS}" in _swift("ClipTranslate.swift")


ARTICLE = {"url": "https://example.com/a/b", "title": "A: b/c?", "source": "Example", "published": "2026-11-01",
           "markdown": "Hello **world**.\n\n![](attachments/0123456789abcdef.jpg)\n", "images": {}}


def test_file_name_and_render_match_phone_layout():
    d = date(2026, 11, 9)
    assert cc.file_name("A: b/c?", d) == "20261109_A  b c.md", "跟手机一样：只换掉特殊字符，原有空格不合并"
    assert cc.file_name("", d) == "20261109_未命名.md"
    assert cc.file_name("长" * 70, d) == "20261109_" + "长" * 60 + ".md"
    text = cc.render(ARTICLE, "备注\n两行", {"tldr": "一句话", "tags": ["AI芯片", "OpenAI"], "body": "- 要点"}, d)
    assert text == (
        '---\ntitle: "A: b/c?"\nsource: "Example"\nurl: "https://example.com/a/b"\npublished: 2026-11-01\n'
        'clipped: 2026-11-09\ntags: ["AI芯片", "OpenAI"]\ntldr: "一句话"\nnote: "备注\\n两行"\n---\n\n'
        "# A: b/c?\n\n- 来源：Example\n- 发布：2026-11-01\n- 剪藏：2026-11-09\n- 备注：备注 两行\n\n"
        "## 摘要\n\n**一句话**：一句话\n\n- 要点\n\n"
        "## 正文\n\nHello **world**.\n\n![](attachments/0123456789abcdef.jpg)\n\n[阅读原文](https://example.com/a/b)\n")


def test_summary_prompt_and_parse():
    seen = {}

    def ask(prompt, **kw):
        seen.update(kw, prompt=prompt)
        return "**TITLE**：《起的标题》\nTLDR: 核心结论\nTAGS: AI芯片，#OpenAI, 太长太长太长太长太长太长的标签\n- 第一\n* 第二\n其他话"

    s = cc.summarize(dict(ARTICLE, titleGuessed=True), ["具身智能"], ask)
    assert s == {"tldr": "核心结论", "tags": ["AI芯片", "OpenAI"], "body": "- 第一\n- 第二", "title": "起的标题"}
    assert seen["prompt"].startswith(cc.TITLE_ASK + "你是资讯编辑")
    assert "已经用过的标签（合适就沿用同一个写法，不合适再起新的）：具身智能\n" in seen["prompt"]
    assert "![](" not in seen["prompt"] and seen["max_tokens"] == 1500 and seen["timeout"] == 180
    assert cc.parse_summary("- 只有要点")["tldr"] == "只有要点"
    with pytest.raises(cc.SummaryError):
        cc.parse_summary("随便说说")


def test_translate_pairs_and_retries():
    md = "# Title here\n\nFirst paragraph.\n\n- item one\n- item two\n\n```\ncode\n```\n\n> A quoted line."
    calls = []

    def ask(prompt, **kw):
        calls.append(prompt)
        nums = re.findall(r"^\[(\d+)\]", prompt.split("===== 原文 =====")[1], re.M)
        if len(calls) == 1:   # 第一次漏了最后一段，第二次补上
            nums = nums[:-1]
        return "\n".join(f"[{n}] 译文{n}" for n in nums)

    out, missed = cc.translate(md, ask)
    assert missed == 0 and len(calls) == 2
    assert out == ("# Title here\n\n> # 译文0\n\nFirst paragraph.\n\n> 译文1\n\n- item one\n\n> - 译文2\n\n"
                   "- item two\n\n> - 译文3\n\n```\ncode\n```\n\n> A quoted line.\n\n> 译文5"), "原文的引用保留「> 」"
    assert cc.needs_translation("English " * 40) and not cc.needs_translation("中文" * 200)


def test_display_math_is_not_translated_or_counted_as_missing():
    md = "An English paragraph about memory stores and attention.\n\n$$W = \\sum_{i=1}^{M} v_i k_i$$\n\nAnother English paragraph here."
    out, missed = cc.translate(md, lambda p, **kw: "\n".join(f"[{n}] 译文{n}" for n in re.findall(r"^\[(\d+)\]", p, re.M)))
    assert "$$W = \\sum_{i=1}^{M} v_i k_i$$\n\nAnother" in out and "> $$" not in out and missed == 0
    # 公式块、表格行（译出来跟原文一样）都不算没译
    body = out + "\n\nReLU MLP\u3000$V\\transpose \\operatorname{ReLU}(Kx)$\u3000$2d_k$\u3000128 more\n\n> ReLU MLP\u3000$V$\u3000128 more"
    assert cc.untranslated(body) == 0
    assert cc.untranslated("A long English paragraph that has no translation after it at all.") == 1
