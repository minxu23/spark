"""手机样例库（apps/mobile-ios/dev/fixture-vault）：按 App 的规则检查结构，防止以后改坏。

书架怎么认（Library.build）、剪藏和随手记的文件名和文件头、对照稿、划线和批注、读后感、公式，
都照手机和电脑的同一套规则查（能用电脑端的纯函数就用，见 core/clip_compose、core/jottings、core/annotations、
apps/reader/server._take_math）。只读仓库里的样例库，不读真实笔记库。"""

import json
import os
import re
import sys
from datetime import date

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from apps.reader import server as reader  # noqa: E402
from core import annotations, clip_compose, clips, jottings  # noqa: E402

VAULT = os.path.join(ROOT, "apps", "mobile-ios", "dev", "fixture-vault")
TRACK = "Spark/信息跟进/"
CLIPS = "Spark/剪藏"
NOTES = "Spark/笔记"


def tree():
    out = []
    for d, _, files in os.walk(VAULT):
        for f in files:
            out.append(os.path.relpath(os.path.join(d, f), VAULT).replace(os.sep, "/"))
    return sorted(out)


def read(rel):
    return open(os.path.join(VAULT, rel), encoding="utf-8").read()


# ---------------------------------------------------------------- 书架（同 Library.build / Show.load）

def build():
    paths = tree()
    folders = {}
    for p in paths:
        folders.setdefault(os.path.dirname(p), []).append(p)
    shows = []
    for d, files in folders.items():
        is_track = d.startswith(TRACK) and "/" not in d[len(TRACK):]
        is_top = d.startswith("Spark/") and "/" not in d[len("Spark/"):] and d not in ("Spark/信息跟进", CLIPS, NOTES)
        if not (is_track or is_top):
            continue
        name = os.path.basename(d)
        m = json.loads(read(f"{d}/.manifest.json")) if f"{d}/.manifest.json" in paths else {}
        kind = "track" if is_track else ("series" if m.get("content_type") == "series" else "summit")
        eps = []
        for key, row in (m.get("entries") or {}).items():
            if row.get("ok") is not True:
                continue

            def path(k, row=row, d=d):
                v = row.get(k)
                return f"{d}/{v}" if v else None
            rel = path("relative_path")
            eps.append({"id": key, "row": row, "title": row["entry"].get("title", key),
                        "article": rel if kind == "track" else None, "transcript": None if kind == "track" else rel,
                        "speech": path("speech_relative_path"), "note": path("note_relative_path")})
        used = {p for e in eps for p in (e["article"], e["transcript"], e["speech"], e["note"]) if p}
        summary = next((p for p in files if os.path.basename(p) == f"{name}.md"), None)
        docs = sorted(p for p in files if p.endswith(".md") and p not in used and p != summary)
        if eps or docs or summary:
            shows.append({"id": d, "name": name, "kind": kind, "manifest": m, "episodes": eps, "docs": docs, "summary": summary})
    reports = [p for p in paths if p.startswith("output/") and p.endswith(".md") and not p.startswith("output/复盘/")
               and (p.count("/") == 1 or (p.count("/") == 2 and os.path.basename(p).startswith("技术洞察报告_")))]
    reviews = [p for p in paths if p.startswith("output/复盘/") and p.endswith(".md") and p.count("/") == 2]
    clip_files = [p for p in paths if p.startswith(CLIPS + "/") and p.endswith(".md") and p.count("/") == 2
                  and os.path.basename(p) != "索引.md"]
    note_files = [p for p in paths if p.startswith(NOTES + "/") and p.endswith(".md") and p.count("/") == 2
                  and not os.path.basename(p).startswith(".")]
    return {"shows": shows, "reports": reports, "reviews": reviews, "clips": clip_files, "notes": note_files}


LIB = build()


def shows(kind):
    return [s for s in LIB["shows"] if s["kind"] == kind]


# ---------------------------------------------------------------- 简化版 Markdown 块（同 Markdown.parse 的段落、引用、标题、列表）

def blocks(text):
    if text.startswith("---\n"):
        text = text[text.index("\n---\n", 4) + 5:]
    out, para, quote = [], [], []

    def flush():
        nonlocal para, quote
        if para:
            out.append(("p", "\n".join(para)))
            para = []
        if quote:
            out.append(("q", "\n".join(quote)))
            quote = []
    in_code = False
    for line in text.split("\n"):
        if line.startswith("```"):
            flush()
            in_code = not in_code
            continue
        if in_code:
            continue
        if not line.strip():
            flush()
            continue
        if line.startswith(">"):
            if para:
                flush()
            quote.append(re.sub(r"^> ?", "", line))
            continue
        if quote:
            flush()
        if re.match(r"#{1,6} ", line):
            flush()
            out.append(("h", line))
        elif re.match(r"\s*(?:[-*+]|\d+\.) ", line):
            flush()
            out.append(("li", line))
        else:
            para.append(line)
    flush()
    return out


def has_han(s):
    return re.search(r"[一-鿿]", s) is not None


def is_bilingual(text):
    """同 Markdown.isBilingual：段落（或列表项）后面紧跟一段引用（译文）算一对，至少 3 对并且占段落的三成以上"""
    b = blocks(text)
    pairs = sum(1 for i in range(len(b) - 1) if b[i + 1][0] == "q" and b[i][0] in ("p", "li")
                or (b[i + 1][0] == "q" and b[i][0] == "q" and has_han(b[i + 1][1]) and not has_han(b[i][1])))
    paras = sum(1 for k, _ in b if k == "p")
    return pairs >= 3 and pairs >= paras * 0.3


# ---------------------------------------------------------------- 每个页签都有内容

def test_every_shelf_tab_has_content():
    assert len(shows("series")) == 1
    assert len(shows("summit")) == 1
    track = shows("track")
    assert {s["name"] for s in track} == {"Example Lab Blog", "示例科技周刊", "简报"}
    assert len(LIB["clips"]) >= 4
    assert len(LIB["reports"]) == 2 and all(os.path.basename(p).startswith("技术洞察报告_") for p in LIB["reports"])
    assert len(LIB["reviews"]) == 1
    assert len(LIB["notes"]) == 3


def test_manifests_parse_and_point_at_real_files():
    paths = set(tree())
    for s in LIB["shows"]:
        for e in s["episodes"]:
            row = e["row"]
            assert row["entry"]["id"] == e["id"]
            assert re.fullmatch(r"\d{8}", row["entry"].get("publish_date", "")), e["id"]
            assert isinstance(row["summary"]["tldr"], str) and row["summary"]["tldr"]
            for p in (e["article"], e["transcript"], e["speech"], e["note"]):
                assert p is None or p in paths, p


# ---------------------------------------------------------------- 播客：3 期，三种样子

def test_podcast_three_kinds_of_episode():
    show = shows("series")[0]
    assert show["summary"] and "## 节目总结" in read(show["summary"])
    eps = {e["id"]: e for e in show["episodes"]}
    assert len(eps) == 4   # 三种样子各一期，加一期很长的对照整理稿（test_long_documents）
    full = [e for e in eps.values() if e["note"] and e["speech"] and e["transcript"]]
    assert len(full) == 1
    f = full[0]
    assert is_bilingual(read(f["speech"])), "对照整理稿要被认成中英对照"
    assert f["row"].get("speech_lang_mode") == "bilingual"
    note = read(f["note"])
    assert note.startswith("---\n") and "### 本期要点" in note
    # 只有处理记录里的小结：没有小结文件，小结正文在 summary.body
    only_summary = [e for e in eps.values() if not e["note"] and not e["speech"] and not e["row"]["entry"].get("short")]
    assert len(only_summary) == 1 and "### 本期要点" in only_summary[0]["row"]["summary"]["body"]
    # 简讯：只有整理稿
    shorts = [e for e in eps.values() if e["row"]["entry"].get("short") is True]
    assert len(shorts) == 1
    s = shorts[0]
    assert s["speech"] and not s["transcript"] and not s["note"]
    assert s["row"]["entry"]["duration"] <= 180


def test_long_documents():
    """长文：一期 300 段以上原文加译文的对照整理稿（含两行译文、重复的短句），一篇 60 项以上列表（有重复项）的剪藏。
    用来在模拟器里看长文打开、滚动和跨块划线。"""
    show = shows("series")[0]
    long = [e for e in show["episodes"] if e["speech"] and not e["note"] and not e["row"]["entry"].get("short")]
    assert len(long) == 1
    text = read(long[0]["speech"])
    assert is_bilingual(text) and long[0]["row"].get("speech_lang_mode") == "bilingual"
    b = blocks(text)
    pairs = sum(1 for i in range(len(b) - 1) if b[i][0] == "p" and b[i + 1][0] == "q")
    assert pairs >= 300
    assert any(k == "q" and "\n" in t for k, t in b), "要有两行的译文"
    quotes = [t for k, t in b if k == "q"]
    assert len(quotes) - len(set(quotes)) >= 3, "要有重复的译文"
    rel = f"{CLIPS}/20261116_工作台收纳的六十四条小建议.md"
    assert rel in LIB["clips"]
    items = [t for k, t in blocks(read(rel)) if k == "li"]
    assert len(items) >= 60 and len(items) - len(set(items)) >= 8, "列表要够长，并且有重复项"


def test_summit_two_sessions_with_summary():
    summit = shows("summit")[0]
    assert summit["manifest"].get("content_type") == "summit"
    assert summit["summary"] and "## 大会总结" in read(summit["summary"])
    assert len(summit["episodes"]) == 2
    for e in summit["episodes"]:
        assert e["speech"] and e["transcript"]
        assert is_bilingual(read(e["speech"]))
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", e["row"]["entry"]["session_date"])
    ed = summit["manifest"]["event_dates"]
    assert ed["source"] in ("manual", "agenda") and ed["start"] <= ed["end"]


# ---------------------------------------------------------------- 信息

def test_track_two_subscriptions_two_articles_each_and_a_brief():
    subs = [s for s in shows("track") if s["episodes"]]
    assert len(subs) == 2
    for s in subs:
        assert len(s["episodes"]) == 2 and not s["docs"]
        langs = set()
        for e in s["episodes"]:
            text = read(e["article"])
            assert "\n## 小结\n" in text and "\n## 原文\n" in text, e["article"]
            assert re.search(r"^- 来源：", text, re.M) and re.search(r"^- 原文链接：", text, re.M)
            original = text.split("\n## 原文\n", 1)[1]
            letters = len(re.findall(r"[A-Za-z]", original))
            langs.add("en" if letters > len(re.findall(r"[一-鿿]", original)) * 2 else "zh")
            name = os.path.basename(e["article"])
            assert name.startswith(e["row"]["entry"]["publish_date"] + "_")
        assert langs == {"en", "zh"}, s["name"]
    brief = [s for s in shows("track") if s["name"] == "简报"][0]
    assert not brief["episodes"] and len(brief["docs"]) == 1
    text = read(brief["docs"][0])
    assert re.search(r"^生成时间：\d{4}-\d{2}-\d{2} \d{2}:\d{2}", text, re.M)   # Doc.generatedTime
    assert re.match(r"\d{8} 信息简报：", os.path.basename(brief["docs"][0]))


# ---------------------------------------------------------------- 剪藏

def clip(rel):
    text = read(rel)
    return text, clips.frontmatter(text)


def test_clips_front_matter_and_file_names():
    for rel in LIB["clips"]:
        text, f = clip(rel)
        for k in ("title", "source", "url", "clipped", "tldr"):
            assert f.get(k), (rel, k)
        assert "\n## 摘要\n" in text and "\n## 正文\n" in text, rel
        assert f"\n[阅读原文]({f['url']})\n" in text, rel
        day = f.get("published") or f["clipped"]
        assert os.path.basename(rel) == clip_compose.file_name(f["title"], date.fromisoformat(day)), rel


def test_long_chinese_clip_has_every_block_kind():
    rel = f"{CLIPS}/20261118_城市夜跑的十年变化.md"
    text, _ = clip(rel)
    assert not is_bilingual(text)
    body = text.split("\n## 正文\n", 1)[1]
    assert re.search(r"^### ", body, re.M) and re.search(r"^#### ", body, re.M)
    assert re.search(r"^- ", body, re.M) and re.search(r"^> ", body, re.M)
    assert re.search(r"^\|---", body, re.M) and "```python" in body
    imgs = re.findall(r"^!\[[^\]]*\]\((attachments/[^)]+)\)\n\n(.+)$", body, re.M)
    assert len(imgs) == 2
    for src, caption in imgs:
        data = open(os.path.join(VAULT, CLIPS, src), "rb").read()
        assert data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) < 50_000
        assert len(caption) <= 50   # 图片下面的短句排成图注（Markdown.captions）


def test_bilingual_clip_is_recognised():
    text, f = clip(f"{CLIPS}/20261105_Notes on Building a Small Weather Station.md")
    assert is_bilingual(text)


def test_clip_with_highlights_notes_and_reflection():
    text, _ = clip(f"{CLIPS}/20261020_慢读一本植物图鉴的方法.md")
    marks = annotations.MARK_TEXT_RE.findall(text)
    assert len(marks) >= 3
    assert any("=" in m for m in marks), "要有一处划线内容里带等号"
    assert "辨认能力 = 观察次数 + 对照次数" in marks
    note = annotations.reading_note(text)
    assert sum(len(i["notes"]) for i in note["items"]) == 2 and not note["loose"]
    assert note["reflection"].startswith("我以前总想")
    # 读后感在「阅读原文」后面、批注脚注前面
    assert text.index("[阅读原文](") < text.index("## 读后感") < text.index("[^note-1]:")
    refs = set(re.findall(r"\[\^note-(\d+)\](?!:)", text))
    assert refs == set(str(k) for k in annotations.note_defs(text))


def test_clip_with_long_title():
    long = [rel for rel in LIB["clips"] if len(clip(rel)[1]["title"]) > 60]
    assert len(long) == 1
    title = clip(long[0])[1]["title"]
    assert os.path.basename(long[0]) == f"20261002_{title[:60]}.md"   # 文件名按码位截到 60


def test_math_article():
    found = []
    for rel in LIB["clips"]:
        _, maths = reader._take_math(read(rel))
        if maths:
            found.append((rel, maths))
    assert len(found) == 1
    _, maths = found[0]
    assert any(display for display, _ in maths) and any(not display for display, _ in maths)


def test_clip_index_lists_every_clip():
    index = read(f"{CLIPS}/索引.md")
    assert index.startswith("# 剪藏索引\n")
    for rel in LIB["clips"]:
        assert f"](<{os.path.basename(rel)}>)" in index, rel
    assert f"共 {len(LIB['clips'])} 篇" in index


# ---------------------------------------------------------------- 随手记

def test_jottings_two_months():
    months = set()
    for rel in LIB["notes"]:
        text = read(rel)
        info = jottings.parse(text, os.path.basename(rel))
        assert clips.frontmatter(text).get("title") == info["title"]
        assert re.search(r"^- 收录时间：\d{4}年\d{2}月\d{2}日，\d{2}:\d{2}（UTC[+-]\d+）$", text, re.M), rel
        # 新建那天按文件头里写着的时刻自己的时差算（jottings.file_name 按本机时区，换了时区的机器上会差一天）
        assert os.path.basename(rel) == clip_compose.file_name(info["title"], info["created"].date()), rel
        months.add(info["created"].strftime("%Y-%m"))
    assert len(months) == 2


# ---------------------------------------------------------------- 报告、复盘

def test_reports_and_review():
    for rel in LIB["reports"]:
        assert re.fullmatch(r"output/技术洞察报告_.+_\d{8}\.md", rel)
        assert read(rel).startswith("# ")
    review = LIB["reviews"][0]
    assert re.fullmatch(r"output/复盘/复盘_阅读_.+_\d{8}-\d{8}\.md", review)
    assert re.match(r"# .+\n", read(review))   # 复盘的标题写在第一行


# ---------------------------------------------------------------- 全部内容

def all_dates():
    out = []
    for s in LIB["shows"]:
        out += [e["row"]["entry"]["publish_date"] for e in s["episodes"]]
    for rel in LIB["clips"]:
        f = clip(rel)[1]
        out += [f["clipped"].replace("-", "")] + ([f["published"].replace("-", "")] if f.get("published") else [])
    for rel in LIB["notes"] + LIB["reports"] + LIB["reviews"] + [d for s in LIB["shows"] for d in s["docs"]]:
        out += re.findall(r"(?<!\d)(20\d{6})(?!\d)", os.path.basename(rel))
    return sorted(date(int(d[:4]), int(d[4:6]), int(d[6:])) for d in out)


def test_dates_span_about_two_months():
    ds = all_dates()
    assert (ds[-1] - ds[0]).days <= 62
    assert len({(d.year, d.month) for d in ds}) >= 2


@pytest.mark.parametrize("rel", [p for p in tree() if p.endswith((".md", ".json"))])
def test_no_real_links_and_wording(rel):
    text = read(rel)
    for host in re.findall(r"https?://([^/\s)\"'>]+)", text):
        assert host in ("example.com", "example.org"), (rel, host)
    assert "标注" not in text, rel   # 「笔记」里分划线、批注、读后感，不用「标注」


def test_not_in_app_bundle():
    """样例库不在 Xcode 同步进 App 的文件夹里（Spark/、Shared/、SparkShare/）"""
    pbx = open(os.path.join(ROOT, "apps", "mobile-ios", "Spark.xcodeproj", "project.pbxproj"), encoding="utf-8").read()
    synced = set(re.findall(r"isa = PBXFileSystemSynchronizedRootGroup;\s*path = (\w+);", pbx))
    assert synced == {"Spark", "Shared", "SparkShare"}
