"""电脑存剪藏（core/clip_compose.py）跟手机（Clips.swift、ClipTranslate.swift）写法一字不差。"""

import json
import os
import re
import shutil
import subprocess
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
    assert _literal("Clips.swift", "guidePrompt") == cc.GUIDE_PROMPT
    assert _literal("Clips.swift", "paperPrompt") == cc.PAPER_PROMPT
    assert f"static let guideMinChars = {cc.GUIDE_MIN_CHARS}" in _swift("Clips.swift")
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
        "# A: b/c?\n\n- 来源：Example\n- 发布时间：2026年11月01日\n- 收录时间：2026年11月09日\n- 备注：备注 两行\n\n"
        "## 摘要\n\n一句话\n\n- 要点\n\n"
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


GUIDE_RAW = """TLDR: 一句话结论
TAGS: 家庭堆肥，阳台园艺
## 这篇在讲什么
第一段。

第二段。
# 核心观点或方法
- 要点甲
* 要点乙
---
### 局限与争议
正文没有提到。
"""


def _article(n):
    return dict(ARTICLE, markdown="字" * n)


def test_guide_threshold_picks_prompt():
    seen = []

    def ask(prompt, **kw):
        seen.append((prompt, kw))
        return GUIDE_RAW if prompt.startswith("你是资讯编辑。下面是我收藏的一篇长文章") else "TLDR: 短\n- 要点"

    cc.summarize(_article(cc.GUIDE_MIN_CHARS), [], ask)       # 刚好到门槛：还是短文
    cc.summarize(_article(cc.GUIDE_MIN_CHARS + 1), [], ask)   # 超过一个字：导读
    assert seen[0][0].startswith("你是资讯编辑。下面是我收藏的一篇文章") and seen[0][1]["max_tokens"] == 1500
    assert seen[1][0].startswith("你是资讯编辑。下面是我收藏的一篇长文章") and seen[1][1]["max_tokens"] == 3000
    # 图片不算字数
    img = dict(ARTICLE, markdown="![](a.jpg)\n\n" * 2000 + "短")
    assert not cc.is_guide(img)
    # 标题、图片提示照样加在导读前面
    p = cc.summary_prompt(dict(_article(6000), titleGuessed=True), ["具身智能"], with_images=True)
    assert p.startswith(cc.IMAGES_ASK + cc.TITLE_ASK + "你是资讯编辑") and "已经用过的标签" in p


def test_parse_guide():
    s = cc.parse_summary(GUIDE_RAW, guide=True)
    assert s["tldr"] == "一句话结论" and s["tags"] == ["家庭堆肥", "阳台园艺"]
    assert s["body"] == ("### 这篇在讲什么\n第一段。\n\n第二段。\n### 核心观点或方法\n- 要点甲\n* 要点乙\n### 局限与争议\n正文没有提到。")
    assert "\n## " not in "\n" + s["body"] and not s["body"].startswith("## ")
    # 缺 TLDR：用第一句顶上；缺 TAGS：没有标签；都没有：报错
    s = cc.parse_summary("### 小标题\n- 第一句话很长" + "长" * 60 + "\n后面", guide=True)
    assert s["tldr"] == ("第一句话很长" + "长" * 60)[:40] and s["tags"] == []
    s = cc.parse_summary("TLDR: 只有结论\n正文一段", guide=True)
    assert s == {"tldr": "只有结论", "tags": [], "body": "正文一段", "title": ""}
    with pytest.raises(cc.SummaryError):
        cc.parse_summary("TAGS: 甲\n### 空的", guide=True)


def test_short_path_unchanged():
    raw = "TLDR: 核心结论\nTAGS: AI芯片\n\n## 不是要点\n- 第一\n* 第二\n\n其他话\n### 小标题"
    s = cc.parse_summary(raw)
    assert s == {"tldr": "核心结论", "tags": ["AI芯片"], "body": "- 第一\n- 第二", "title": ""}
    prompt = cc.summary_prompt(_article(cc.GUIDE_MIN_CHARS), [])
    assert prompt == cc.SUMMARY_PROMPT.replace("{source}", "Example").replace("{title}", "A: b/c?") \
        .replace("{existing}", "").replace("{text}", "字" * cc.GUIDE_MIN_CHARS)


def test_guide_file_has_no_extra_h2_section():
    d = date(2026, 11, 9)
    s = cc.parse_summary(GUIDE_RAW, guide=True)
    text = cc.render(ARTICLE, "", s, d)
    assert re.findall(r"(?m)^## .*", text) == ["## 摘要", "## 正文"]
    assert "tldr: \"一句话结论\"" in text
    # 重写摘要（with_summary）后也只有这两节，正文不动
    again = cc.with_summary(text, dict(s, body="### 新\n内容"))
    assert re.findall(r"(?m)^## .*", again) == ["## 摘要", "## 正文"]
    assert cc.body_of(again) == cc.body_of(text)


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


def test_translate_keeps_existing_translations_and_sends_only_missing():
    half = ("First English paragraph.\n\n> 第一段（旧译文，不能动）\n\n"
            "- item one\n\n> - 条目一\n\n"
            "Second English paragraph without translation.\n\n"
            "> A quoted English line.\n\n> 引用的旧译文\n\n"
            "- item two")
    sent = []

    def ask(prompt, **kw):
        body = prompt.split("===== 原文 =====")[1]
        sent.append(body)
        return "\n".join(f"[{n}] 新译{n}" for n in re.findall(r"^\[(\d+)\]", body, re.M))

    out, missed = cc.translate(half, ask)
    assert missed == 0 and len(sent) == 1
    assert "Second English paragraph" in sent[0] and "item two" in sent[0]
    assert "First English" not in sent[0] and "item one" not in sent[0] and "A quoted" not in sent[0]
    assert out == ("First English paragraph.\n\n> 第一段（旧译文，不能动）\n\n- item one\n\n> - 条目一\n\n"
                   "Second English paragraph without translation.\n\n> 新译2\n\n"
                   "> A quoted English line.\n\n> 引用的旧译文\n\n- item two\n\n> - 新译4")


def test_guide_heading_named_body_becomes_bold():
    """导读里的小标题叫「正文」「摘要」时改成加粗行，不留下能被「## 正文」匹配到的行。"""
    out = cc.parse_summary("TLDR: 一句话\n### 正文\n一段\n## 摘要\n又一段\n### 核心观点或方法\n第三段", guide=True)
    assert "## 正文" not in out["body"] and "## 摘要" not in out["body"]
    assert "**正文**" in out["body"] and "**摘要**" in out["body"] and "### 核心观点或方法" in out["body"]


# ---------------------------------------------------------------- 译文合理性：漏编号并段的译文不收

MERGE_MD = ("## 3. The FDE role at OpenAI\n\n"
            "Colin Jarvis is the head of the forward deployed engineering team at OpenAI.\n\n"
            "He joined in 2023 after working on consulting projects for large enterprise customers.")
GOOD = ["OpenAI的FDE角色", "Colin Jarvis是OpenAI前线部署工程团队的负责人。", "他2023年加入，此前为大型企业客户做咨询项目。"]


def _nums(prompt):
    return [int(n) for n in re.findall(r"^\[(\d+)\]", prompt.split("===== 原文 =====")[1], re.M)]


def test_translation_dropping_a_number_is_not_merged_into_previous():
    calls = []

    def ask(prompt, **kw):
        calls.append(_nums(prompt))
        if len(calls) == 1:   # 第 1 段漏写「[1]」：它的译文被接到第 0 段后面
            return f"[0] {GOOD[0]}\n{GOOD[1]}\n[2] {GOOD[2]}"
        return "\n".join(f"[{n}] {GOOD[n]}" for n in _nums(prompt))

    out, missed = cc.translate(MERGE_MD, ask)
    assert calls == [[0, 1, 2], [0, 1, 2]], "并段的第 0 段被拒收，第 0、1 段都进入重试（重试沿用整批提示词）"
    assert missed == 0
    assert out == ("## 3. The FDE role at OpenAI\n\n> ## OpenAI的FDE角色\n\n"
                   "Colin Jarvis is the head of the forward deployed engineering team at OpenAI.\n\n> " + GOOD[1] + "\n\n"
                   "He joined in 2023 after working on consulting projects for large enterprise customers.\n\n> " + GOOD[2])


def test_merged_translation_that_repeats_stays_untranslated():
    out, missed = cc.translate(MERGE_MD, lambda p, **kw: f"[0] {GOOD[0]}\n{GOOD[1]}\n[2] {GOOD[2]}")
    assert missed == 2 and "> ## " not in out and GOOD[1] not in out, "重试还是并段：两段都保持原文，计入没译成"
    assert out.count("> " + GOOD[2]) == 1


def test_merged_heading_with_period_is_rejected_even_if_short():
    assert not cc.translation_plausible("Title", "标题。第二句", True)
    assert cc.translation_plausible("Title", "标题", True)


def test_long_but_normal_translation_is_accepted():
    src = "x" * 100
    assert cc.translation_plausible(src, "译" * 140, False) and not cc.translation_plausible(src, "译" * 141, False)
    assert cc.translation_plausible("x" * 10, "译" * 36, False) and not cc.translation_plausible("x" * 10, "译" * 37, False)
    assert cc.translation_plausible("x" * 10, "译" * 40, True) and not cc.translation_plausible("x" * 10, "译" * 41, True)
    out, missed = cc.translate("A short English line about caches.\n\nAnother English paragraph about queues.",
                               lambda p, **kw: "[0] " + "译" * 60 + "\n[1] 好")
    assert missed == 0 and "> " + "译" * 60 in out, "原文 34 字，译文 60 字：在 1.15 倍加 25 以内"


def test_translation_folded_into_two_lines_is_still_accepted():
    out, missed = cc.translate("A fairly long English sentence that the model may fold in two.",
                               lambda p, **kw: "[0] 模型有时把一段译文\n折成了两行")
    assert missed == 0 and out.endswith("> 模型有时把一段译文 折成了两行")


# 共用用例表：(原文, 译文, 是标题)；手机端 TranslationRule.plausible 用 swiftc 单独编出来跑同一张表
PLAUSIBLE_CASES = [
    ("Title", "标题", True), ("Title", "标题。第二句", True), ("", "x" * 20, True), ("", "x" * 21, True),
    ("x" * 10, "译" * 40, True), ("x" * 10, "译" * 41, True), ("x" * 10, "a.b。", True),
    ("x" * 100, "译" * 140, False), ("x" * 100, "译" * 141, False), ("x" * 10, "译" * 36, False), ("x" * 10, "译" * 37, False),
    ("", "译" * 25, False), ("", "译" * 26, False), ("x" * 20, "。" * 48, False), ("x" * 20, "。" * 49, False),
    ("a😀b", "译" * 28, False), ("a😀b", "译" * 29, False), ("éé", "译" * 28, False), ("éé", "译" * 29, False),
    ("🇯🇵🇯🇵", "译" * 30, True), ("🇯🇵🇯🇵", "译" * 29 + "。", True),
    ("The FDE role at OpenAI", "OpenAI的FDE角色 Colin Jarvis是OpenAI前线部署工程团队的负责人。", True),
]

TRANSLATION_RULE = os.path.join(IOS, "TranslationRule.swift")


def test_translation_rule_constants_same_as_phone():
    src = _swift("TranslationRule.swift")
    assert f"static let headingFactor = {cc.HEADING_FACTOR}, headingSlack = {cc.HEADING_SLACK}" in src
    assert f"static let bodyPercent = {cc.BODY_PERCENT}, bodySlack = {cc.BODY_SLACK}" in src
    assert 'return !translated.contains("。") && t <= s * headingFactor + headingSlack' in src
    assert "return t * 100 <= s * bodyPercent + bodySlack * 100" in src
    assert "TranslationRule.plausible(source: items[i].source, translated: t, heading: items[i].marker.hasPrefix(\"#\"))" in _swift("ClipTranslate.swift")


def test_translation_rule_same_on_phone(tmp_path):
    import json
    import shutil
    import subprocess
    if not shutil.which("swiftc"):
        pytest.skip("没有 swiftc")
    main = tmp_path / "main.swift"
    main.write_text('import Foundation\n'
                    'let rows = try! JSONSerialization.jsonObject(with: FileHandle.standardInput.readDataToEndOfFile()) as! [[Any]]\n'
                    'print(rows.map { TranslationRule.plausible(source: $0[0] as! String, translated: $0[1] as! String, heading: $0[2] as! Bool) ? "1" : "0" }.joined())\n',
                    encoding="utf-8")
    exe = tmp_path / "rule"
    r = subprocess.run(["swiftc", "-o", str(exe), TRANSLATION_RULE, str(main)], capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr
    run = subprocess.run([str(exe)], input=json.dumps(PLAUSIBLE_CASES), capture_output=True, text=True, timeout=60)
    want = "".join("1" if cc.translation_plausible(*c) else "0" for c in PLAUSIBLE_CASES)
    assert run.stdout.strip() == want
    assert "0" in want and "1" in want


# 整理稿逐段翻译的两条规则，电脑和手机用同一张表：舞台提示段不送去翻译；回复的编号不是 1..n 整批不收
CUE_CASES = [
    "[applause]", "[Applause]", " [laughter] ", "[Music] [applause]", "**Host**：[Laughter]", "**Host**: [applause]",
    "[applause] Thanks", "Thanks [applause]", "[see note](http://x.com/a)", "![img](a.png)", "[1]", "[]", "[" + "a" * 40 + "]",
    "[" + "a" * 41 + "]", "[a(b)]", "[a\nb]", "[applause]\n[laughter]", "**Host**：", "**Host**：[applause] ok", "", "(applause)", "[掌声]",
]
COMPLETE_CASES = [([1, 2, 3], 3), ([3, 1, 2], 3), ([1, 2], 3), ([1, 2, 3, 4], 3), ([2, 3], 3), ([1, 3], 3), ([], 1), ([1], 1), ([0, 1], 1)]


def test_speech_translation_rules_same_on_phone(tmp_path):
    if not shutil.which("swiftc"):
        pytest.skip("没有 swiftc")
    main = tmp_path / "main.swift"
    main.write_text('import Foundation\n'
                    'let d = try! JSONSerialization.jsonObject(with: FileHandle.standardInput.readDataToEndOfFile()) as! [String: [Any]]\n'
                    'print((d["cue"] as! [String]).map { TranslationRule.isStageCue($0) ? "1" : "0" }.joined())\n'
                    'print((d["complete"] as! [[Any]]).map { TranslationRule.numbersComplete($0[0] as! [Int], count: $0[1] as! Int) ? "1" : "0" }.joined())\n',
                    encoding="utf-8")
    exe = tmp_path / "cue"
    r = subprocess.run(["swiftc", "-o", str(exe), TRANSLATION_RULE, str(main)], capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr
    run = subprocess.run([str(exe)], input=json.dumps({"cue": CUE_CASES, "complete": COMPLETE_CASES}), capture_output=True, text=True, timeout=60)
    want_cue = "".join("1" if cc.is_stage_cue(c) else "0" for c in CUE_CASES)
    want_complete = "".join("1" if cc.numbers_complete(g, n) else "0" for g, n in COMPLETE_CASES)
    assert run.stdout.split() == [want_cue, want_complete]
    assert "0" in want_cue and "1" in want_cue and "0" in want_complete and "1" in want_complete


def test_speech_translation_rules_python_values():
    assert [c for c in CUE_CASES if cc.is_stage_cue(c)] == [
        "[applause]", "[Applause]", " [laughter] ", "[Music] [applause]", "**Host**：[Laughter]", "**Host**: [applause]",
        "[1]", "[" + "a" * 40 + "]", "[a\nb]", "[掌声]"]
    assert [cc.numbers_complete(g, n) for g, n in COMPLETE_CASES] == [True, True, False, False, False, False, False, True, False]


def test_speech_script_uses_rules_on_phone():
    src = _swift("SpeechScript.swift")
    assert "TranslationRule.numbersComplete(Array(got.keys), count: batch.count)" in src
    assert "TranslationRule.isStageCue($0.1)" in src
    assert "TranslationRule.plausible(source: batch[j - 1].1, translated: t, heading: batch[j - 1].1.hasPrefix(\"#\"))" in src


GUIDE_LINES = [
    "### 正文", "## 正文", "# 正文", "#### 摘要", "##正文", "# 摘要", "###  摘要", "###\u3000正文", "###\u00a0摘要",
    "### 正文：", "### 正文与摘要", "### 摘要的摘要", "正文", "摘要", "**正文**", "### **正文**", "#正文摘要",
    "### 核心观点或方法", "### 在讲什么", "# 大标题", "## 二级标题", "#三级标题没有空格", "#### 四级标题", "普通一段话。",
    "- 一条要点", "* 另一条", "---", "-----", "***", "_____", "- - -", "--", "==", "", "## ", "# ", "###",
]


def test_guide_lines_same_as_phone(tmp_path):
    """手机 GuideLine.keep（Foundation 单独编出来）和电脑 parse_summary(guide=True) 对同一张行表留下的行一样，
    小标题「正文」「摘要」改成加粗行这一条也在里面。"""
    if not shutil.which("swiftc"):
        pytest.skip("没有 swiftc")
    main = tmp_path / "main.swift"
    main.write_text(
        "import Foundation\n"
        "let lines = try! JSONSerialization.jsonObject(with: FileHandle.standardInput.readDataToEndOfFile()) as! [String]\n"
        "let out: [Any] = lines.map { GuideLine.keep($0) ?? NSNull() }\n"
        "print(String(decoding: try! JSONSerialization.data(withJSONObject: out), as: UTF8.self))\n", encoding="utf-8")
    src = os.path.join(IOS, "GuideLine.swift")
    exe = tmp_path / "guide"
    r = subprocess.run(["swiftc", "-o", str(exe), src, str(main)], capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr
    sent = [ln.strip() for ln in GUIDE_LINES]     # 电脑端先去掉首尾空白再判断
    run = subprocess.run([str(exe)], input=json.dumps(sent), capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    got = [x if x is not None else "" for x in json.loads(run.stdout)]
    want = [cc.parse_summary("TLDR: t\n" + ln, guide=True)["body"] for ln in GUIDE_LINES]
    assert got == want
    assert got[0] == got[1] == got[2] == "**正文**" and got[3] == "**摘要**"
    assert got[GUIDE_LINES.index("---")] == "" and got[GUIDE_LINES.index("### 正文：")] == "### 正文："


# ---------------------------------------------------------------- 论文专用摘要（PAPER_PROMPT）

PAPER_RAW = ("TLDR: 提出新的稀疏注意力\nTAGS: 稀疏注意力，长上下文\n"
             "这篇论文要解决长序列注意力太贵的问题，用分块稀疏的办法，在三个基准上持平全注意力，只在超过 64k 时有退化。\n\n"
             "# 1 Introduction\n介绍背景。\n## 2 方法\n分块选取。\n---\n### 摘要\n### 实验\n结果持平。")


def test_paper_always_uses_paper_prompt_and_clip_unchanged():
    seen = []

    def ask(prompt, **kw):
        seen.append((prompt, kw))
        return PAPER_RAW

    short = dict(ARTICLE, markdown="很短的一段。")
    cc.summarize(short, [], ask, paper=True)                           # 很短也走论文提示词
    cc.summarize(_article(cc.GUIDE_MIN_CHARS + 1), [], ask, paper=True)
    assert len(seen) == 2
    assert all(p.startswith("你是资讯编辑。下面是我收藏的一篇论文") and kw["max_tokens"] == 4000 for p, kw in seen)
    # 不传 paper（剪藏）：一个字都没变
    assert cc.summary_prompt(short, []) == cc.SUMMARY_PROMPT.replace("{source}", "Example").replace("{title}", "A: b/c?") \
        .replace("{existing}", "").replace("{text}", "很短的一段。")
    assert cc.summary_prompt(_article(cc.GUIDE_MIN_CHARS + 1), []).startswith("你是资讯编辑。下面是我收藏的一篇长文章")
    p = cc.summary_prompt(dict(short, titleGuessed=True), ["具身智能"], with_images=True, paper=True)
    assert p.startswith(cc.IMAGES_ASK + cc.TITLE_ASK + "你是资讯编辑。下面是我收藏的一篇论文") and "已经用过的标签" in p


def test_paper_summary_has_no_h2_and_drops_references():
    s = cc.parse_summary(PAPER_RAW, guide=True)
    assert s["tldr"] == "提出新的稀疏注意力" and s["tags"] == ["稀疏注意力", "长上下文"]
    assert s["body"].startswith("这篇论文要解决") and "### 1 Introduction\n介绍背景。\n### 2 方法\n分块选取。" in s["body"]
    assert "\n## " not in "\n" + s["body"] and "**摘要**" in s["body"]
    md = "引言。" * 400 + "\n\n## References\n\n[1] A. Smith. 2020.\n\n## Appendix\n\n附录。"
    p = cc.summary_prompt(dict(ARTICLE, markdown=md), [], paper=True)
    assert "引言。" in p and "[1] A. Smith" not in p and "附录" not in p
    assert "[1] A. Smith" in cc.summary_prompt(dict(ARTICLE, markdown=md), [])   # 剪藏不截


REFERENCE_TEXTS = [
    "正文" * 100 + "\n\n## References\n\n[1] x", "正文" * 100 + "\n\n## 7. REFERENCES\n\n[1] x", "正文" * 100 + "\n\n## VII Bibliography",
    "正文" * 100 + "\n\n## 参考文献\n\n[1] x", "## References\n\n[1] x",                 # 前面没有字：不截
    "目录\n\n## References\n\n" + "正文" * 100 + "\n\n## References\n\n[1] x",            # 前面不到 1/5：找下一个
    "正文\n\n### References\n\n[1] x", "正文\n\n## References and notes\n\n[1] x", "正文\n\nReferences\n\n[1] x",
    "正文" * 100 + "\n\n## 1234 References", "没有参考文献", "", "正文" * 10 + "\n\n## references\n\n" + "x" * 50,
    "正文" * 100 + "\n\n##  References",   # 两个空格：不认
]


def test_before_references_rule():
    got = [cc.before_references(t) for t in REFERENCE_TEXTS]
    assert got[0] == got[1] == got[2] == got[3] == "正文" * 100
    assert got[4] == REFERENCE_TEXTS[4] and got[5] == "目录\n\n## References\n\n" + "正文" * 100
    assert got[6:11] == REFERENCE_TEXTS[6:11] and got[11] == "" and got[12] == "正文" * 10 and got[13] == REFERENCE_TEXTS[13]


def test_references_rule_same_on_phone(tmp_path):
    """手机 Paper.beforeReferences 对同一组文字截出来的一样。"""
    if not shutil.which("swiftc"):
        pytest.skip("没有 swiftc")
    main = tmp_path / "main.swift"
    main.write_text(
        "import Foundation\n"
        "let texts = try! JSONSerialization.jsonObject(with: FileHandle.standardInput.readDataToEndOfFile()) as! [String]\n"
        "print(String(decoding: try! JSONSerialization.data(withJSONObject: texts.map { Paper.beforeReferences($0) }), as: UTF8.self))\n",
        encoding="utf-8")
    shared = os.path.join(IOS, "..", "Shared")
    exe = tmp_path / "refs"
    r = subprocess.run(["swiftc", "-o", str(exe), os.path.join(shared, "Paper.swift"), os.path.join(shared, "SparkError.swift"), str(main)],
                       capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, r.stderr
    run = subprocess.run([str(exe)], input=json.dumps(REFERENCE_TEXTS), capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout) == [cc.before_references(t) for t in REFERENCE_TEXTS]
    src = _swift("Clips.swift")
    assert "paper ? 4000 : guide ? 3000 : 1500" in src and "if paper { text = Paper.beforeReferences(text) }" in src
