"""「信息」里抓来的外文文章自动配中文译文（core/track_translate.py、apps/summit2md/tracking.py）。
模型调用全部 mock，不联网；文件都写在临时目录里。"""

import os
import re
import shutil
import time

import pytest

from apps.summit2md import pipeline, subscriptions_store, tracking
from core import clip_capture, clip_compose, review, sources, track_translate as tt
from tests.test_reader import _client, _get, _post

LLM = {"backend": "api", "api_key": "k", "model": "m", "api_base": ""}

EN = [f"Paragraph {i} explains how the new robot gripper software works in factories and labs." for i in range(4)]
ZH_TR = [f"第 {i} 段讲新的机器人夹爪软件在工厂和实验室里怎么用。" for i in range(4)]


def numbered_reply(prompt, only=None):
    """按翻译提示词里的编号回中文；only 给了的话只回这几个编号。"""
    nums = [int(n) for n in re.findall(r"^\[(\d+)\]", prompt.split("===== 原文 =====")[1], re.M)]
    return "\n".join(f"[{n}] 译文{n}" for n in nums if only is None or n in only)


def is_translate(prompt):
    return prompt.startswith("把下面编号的英文段落")


# ---------------------------------------------------------------- 要不要翻译

def test_needs_translation_rules_same_as_clips():
    assert tt.needs_translation("\n\n".join(EN))
    assert not tt.needs_translation("Short English text."), "不到 200 字的不译"
    assert not tt.needs_translation("机器人夹爪软件开源了，" * 30), "中文文章不译"
    mixed = "中文为主的文章，夹着一些 English words 和 OpenAI、GPT 之类的名字。" * 10
    assert not tt.needs_translation(mixed), "中文占比到一成的不译"


def test_is_article_path():
    assert tt.is_article_path("Spark/信息跟进/AI Insider/20261101_A.md")
    assert not tt.is_article_path("Spark/信息跟进/简报/20261101 信息简报.md")
    assert not tt.is_article_path("Spark/信息跟进/AI Insider/speech/20261101_A.md")
    assert not tt.is_article_path("Spark/剪藏/20261101_A.md")
    assert not tt.is_article_path("Spark/信息跟进/AI Insider/AI Insider.md")
    assert tt.TRACK_DIR == f"Spark/{subscriptions_store.TRACK_DIRNAME}"
    assert tt.BRIEF_DIRNAME == subscriptions_store.BRIEF_DIRNAME


# ---------------------------------------------------------------- 抓取时自动翻译

@pytest.fixture
def track(tmp_path, monkeypatch):
    monkeypatch.setattr(subscriptions_store, "STORE_PATH", str(tmp_path / "subscriptions.json"))
    monkeypatch.setattr(sources, "_fetch_generic_article_paragraphs", lambda url: None)
    sub = subscriptions_store.add(url="https://example.com/feed", name="AI Insider", category="海外",
                                  output_dir=str(tmp_path / "Spark"), source_type="rss")
    return sub


def entry(eid="e1", paragraphs=EN, title="Robot gripper", source_type="rss"):
    html = "".join(f"<p>{p}</p>" for p in paragraphs)
    return {"id": eid, "title": title, "url": f"https://x/{eid}", "source_type": source_type,
            "publish_date": "20261101", "rss_content_html": html}


def run(sub, entries, llm, ids=None, **kw):
    logs = []
    with pytestmock(tracking, "list_entries", lambda s, recent=0: {"entries": entries}), \
         pytestmock(pipeline, "_cached_summarize", llm):
        result = tracking.run_batch([(sub, ids or [e["id"] for e in entries])], output_dir=os.path.dirname(
            os.path.dirname(sub["folder"])), llm=LLM, progress_cb=logs.append, **kw)
    return result, logs


class pytestmock:
    """不引 unittest.mock 的小替身：with 里换掉 obj.name，出来换回去。"""
    def __init__(self, obj, name, value):
        self.obj, self.name, self.value = obj, name, value

    def __enter__(self):
        self.old = getattr(self.obj, self.name)
        setattr(self.obj, self.name, self.value)

    def __exit__(self, *exc):
        setattr(self.obj, self.name, self.old)


def note_text(sub, title="Robot gripper"):
    return open(os.path.join(sub["folder"], f"20261101_{title}.md"), encoding="utf-8").read()


def test_english_article_gets_paired_translation_and_summary_brief_read_no_translation(track):
    prompts = []

    def llm(prompt, *a, **kw):
        prompts.append((prompt, kw))
        if is_translate(prompt):
            return numbered_reply(prompt)
        if "资讯主编" in prompt:
            return "主题：夹爪软件\n\n## 要点速览\n\n- 夹爪 [1]"
        return "TLDR: 夹爪软件开源\n- 要点一"

    result, logs = run(track, [entry()], llm)
    assert result["processed"] == 1 and result["failed"] == [] and result["warnings"] == []
    text = note_text(track)
    body = text.split("\n## 原文\n\n", 1)[1]
    assert body == "\n\n".join(f"{p}\n\n> 译文{i}" for i, p in enumerate(EN)) + "\n", "每段原文后面跟一段「> 译文」"
    assert "## 小结\n\n**夹爪软件开源**" in text
    # 翻译跟小结同一个后端、同一个模型，走同一个缓存目录
    tr = [kw for p, kw in prompts if is_translate(p)]
    assert len(tr) == 1 and tr[0]["model"] == "m" and tr[0]["cache_dir"] == pipeline.llm_cache_dir(track["folder"])
    # 小结、简报读的都不是译文
    summary_prompt = next(p for p, _ in prompts if "TLDR" in p and not is_translate(p))
    brief_prompt = next(p for p, _ in prompts if "资讯主编" in p)
    assert "译文" not in summary_prompt and EN[0] in summary_prompt
    assert "译文" not in brief_prompt and "夹爪软件开源" in brief_prompt
    # 进度里看得出在翻译
    assert any(kw.get("stage") == "translate" and kw.get("log") == "  翻译 1/1" for kw in logs)
    assert tt.untranslated(text) == 0 and not tt.wants_translation(text)


def test_chinese_or_short_or_video_article_is_not_translated(track):
    calls = []

    def llm(prompt, *a, **kw):
        calls.append(prompt)
        return "TLDR: 一句话\n- 要点"

    zh = ["机器人夹爪软件开源了，这一段讲它在工厂里怎么用，写得比较长。" * 3] * 4
    run(track, [entry("z1", zh, "中文")], llm, make_brief=False)
    assert not any(is_translate(p) for p in calls)
    assert "\n> " not in note_text(track, "中文")
    # 不到 200 字的英文（抓取那一步本身会挡掉太短的 RSS 正文，这里直接测写原文那一步）
    short = ["Short English line one.", "Short line two."]
    body, note = tracking._original_body(entry("s1", short), short, "", llm=LLM, folder=track["folder"],
                                         stop_flag=None, progress=None)
    assert body == "\n\n".join(short) and note == ""
    # 视频（没有 source_type）的条目走节目那套，不在这里翻译
    assert not tt.wants_source({"source_type": None}) and tt.wants_source({"source_type": "article"})


def test_translation_failure_still_saves_article(track):
    def llm(prompt, *a, **kw):
        if is_translate(prompt):
            raise pipeline.SummarizeError("模型超时")
        return "TLDR: 一句话\n- 要点"

    result, logs = run(track, [entry()], llm, make_brief=False)
    assert result["processed"] == 1 and result["failed"] == []
    assert result["warnings"] == ["AI Insider：Robot gripper：有 4 段没译成"]
    assert any("有 4 段没译成" in (kw.get("log") or "") for kw in logs)
    assert note_text(track).split("\n## 原文\n\n", 1)[1] == "\n\n".join(EN) + "\n", "没译的段落留原文"


def test_partial_translation_reports_missing(track):
    def llm(prompt, *a, **kw):
        if is_translate(prompt):
            return numbered_reply(prompt, only={0, 1, 2})
        return "TLDR: 一句话\n- 要点"

    result, _ = run(track, [entry()], llm, make_brief=False)
    assert result["warnings"] == ["AI Insider：Robot gripper：有 1 段没译成"]
    body = note_text(track).split("\n## 原文\n\n", 1)[1]
    assert "> 译文2" in body and body.rstrip().endswith(EN[3])


def test_stop_between_batches_keeps_translated_part(track):
    big = [p * 60 for p in EN[:3]]   # 每段五千多字，一段一批
    state = {"stop": False, "calls": 0}

    def llm(prompt, *a, **kw):
        if is_translate(prompt):
            state["calls"] += 1
            state["stop"] = True     # 译完第一批就点了停止
            return numbered_reply(prompt)
        return "TLDR: 一句话\n- 要点"

    result, logs = run(track, [entry("e1", big), entry("e2", EN, "Second")], llm, stop_flag=lambda: state["stop"])
    assert state["calls"] == 1
    assert result["stopped"] and result["processed"] == 1, "停在第一篇：已存，第二篇没处理"
    assert result["warnings"] == ["AI Insider：Robot gripper：有 2 段没译成（中途停止）"]
    body = note_text(track).split("\n## 原文\n\n", 1)[1]
    assert body == f"{big[0]}\n\n> 译文0\n\n{big[1]}\n\n{big[2]}\n"
    assert not os.path.exists(os.path.join(track["folder"], "20261101_Second.md"))
    assert result["brief_path"] is None


def test_refetch_keeps_translations_and_highlights(track):
    calls = []

    def llm(prompt, *a, **kw):
        calls.append(prompt)
        if is_translate(prompt):
            return numbered_reply(prompt)
        return "TLDR: 一句话\n- 要点"

    run(track, [entry()], llm, make_brief=False)
    path = os.path.join(track["folder"], "20261101_Robot gripper.md")
    text = open(path, encoding="utf-8").read()
    text = text.replace("> 译文1", "> ==译文1==[^note-1]").replace("Paragraph 2 explains", "Paragraph 2 ==explains==")
    text += "\n[^note-1]: 我的批注\n"
    open(path, "w", encoding="utf-8").write(text)
    calls.clear()
    # 第二次：同一条重新抓，最后一段原文改了
    changed = EN[:3] + ["Paragraph 3 was rewritten by the site after publication, with new numbers."]
    shutil.rmtree(os.path.join(track["folder"], ".cache", "sources"))   # 临时目录里的抓取缓存：让它真的重新抓
    with pytestmock(pipeline, "_cached_summarize", llm):
        res = tracking.process_item(track, entry(paragraphs=changed), llm=LLM, summary_length="medium", max_chars=0)
    assert res["ok"] and res["translate_note"] == ""
    tr = [p for p in calls if is_translate(p)]
    assert len(tr) == 1 and "Paragraph 3 was rewritten" in tr[0] and "Paragraph 0" not in tr[0], "只译变了的那段"
    new = open(path, encoding="utf-8").read()
    assert "> ==译文1==[^note-1]" in new and "Paragraph 2 ==explains==" in new
    assert "[^note-1]: 我的批注" in new and "以前的划线" not in new
    assert new.count("==") == 4


# ---------------------------------------------------------------- 单篇补译

ARTICLE = ("# Robot gripper\n\n- 来源：AI Insider\n- 原文链接：https://x/e1\n\n## 小结\n\n**夹爪软件开源**\n\n- 要点\n\n"
           "## 原文\n\n{body}\n")


def write_article(root, body, name="20261101_A.md"):
    folder = root / "Spark" / "信息跟进" / "AI Insider"
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / name
    p.write_text(ARTICLE.format(body=body), encoding="utf-8")
    return p


def test_fill_translates_whole_article(tmp_path):
    p = write_article(tmp_path, "\n\n".join(EN) + "\n\n## 读后感\n\n写了点想法\n\n[^note-1]: 批注")
    asked = []

    def ask(prompt, **kw):
        asked.append(prompt)
        return numbered_reply(prompt)
    assert tt.fill_one(str(p), ask) == ""
    text = p.read_text(encoding="utf-8")
    assert tt.original_section(text) == "\n\n".join(f"{e}\n\n> 译文{i}" for i, e in enumerate(EN))
    assert text.endswith("## 读后感\n\n写了点想法\n\n[^note-1]: 批注\n"), "读后感、批注脚注不动"
    assert "## 小结\n\n**夹爪软件开源**" in text and len(asked) == 1


def test_fill_only_sends_missing_and_skips_done(tmp_path):
    half = f"{EN[0]}\n\n> 已有译文0\n\n{EN[1]}\n\n{EN[2]}\n\n> 已有译文2\n\n{EN[3]}"
    p = write_article(tmp_path, half)
    asked = []

    def ask(prompt, **kw):
        asked.append(prompt)
        return numbered_reply(prompt)
    assert tt.wants_translation(p.read_text(encoding="utf-8"))
    tt.fill_one(str(p), ask)
    sent = asked[0].split("===== 原文 =====")[1]
    assert EN[1] in sent and EN[3] in sent and EN[0] not in sent and EN[2] not in sent
    text = p.read_text(encoding="utf-8")
    assert "> 已有译文0" in text and "> 已有译文2" in text and tt.untranslated(text) == 0
    asked.clear()
    assert tt.fill_one(str(p), ask) == "" and asked == [], "已经译完的不再调用模型"
    zh = write_article(tmp_path, "这是一篇中文文章。" * 40, "20261101_zh.md")
    assert tt.fill_one(str(zh), ask) == "不是外文文章（或者太短），不用翻译" and asked == []


def test_fill_endpoint_runs_job(tmp_path, monkeypatch):
    monkeypatch.setenv("SPARK_VAULT", str(tmp_path))
    monkeypatch.setattr(clip_capture, "_JOBS", {})
    monkeypatch.setattr(clip_capture.github_sync, "after_task", lambda: None)
    monkeypatch.setattr(clip_capture, "_ask", lambda: (lambda prompt, **kw: numbered_reply(prompt, only={0, 1, 2})))
    p = write_article(tmp_path, "\n\n".join(EN))
    c = _client()
    bad = _post(c, "/read/api/track/translate", paths=["剪藏/20261101_A.md"])
    assert bad.status_code == 400 and "只能翻译信息里的文章" in bad.get_json()["error"]
    r = _post(c, "/read/api/track/translate", paths=["信息跟进/AI Insider/20261101_A.md"])
    assert r.status_code == 202, r.get_json()
    job = r.get_json()
    for _ in range(300):
        st = c.get(f"/read/api/track/translate/{job['id']}").get_json()
        if st["done"]:
            break
        time.sleep(.01)
    assert st["done"] and st["warning"] == "有 1 段没译成" and st["kind"] == "track"
    assert tt.untranslated(p.read_text(encoding="utf-8")) == 1


def test_clip_and_track_jobs_share_one_slot(monkeypatch):
    monkeypatch.setattr(clip_capture, "_JOBS", {})
    clip_capture._new_job("准备中", kind="track")
    with pytest.raises(clip_capture.remote_tasks.TaskError, match="信息文章正在翻译"):
        clip_capture._new_job("正在下载图片")
    monkeypatch.setattr(clip_capture, "_JOBS", {})
    clip_capture._new_job("正在下载图片")
    with pytest.raises(clip_capture.remote_tasks.TaskError, match="已有一篇剪藏正在处理"):
        clip_capture._new_job("准备中", kind="track")


# ---------------------------------------------------------------- 阅读页

@pytest.fixture
def vault(tmp_path, monkeypatch):
    monkeypatch.setenv("SPARK_VAULT", str(tmp_path))
    return tmp_path


def _pairs(page):
    return re.findall(r'<div class="pair"><div class="orig">(.*?)</div><div class="tr">(.*?)</div></div>', page, re.S)


def test_reader_treats_translated_track_article_as_bilingual(vault):
    write_article(vault, "\n\n".join(f"{e}\n\n> {z}" for e, z in zip(EN, ZH_TR)))
    write_article(vault, "\n\n".join(EN), "20261101_B.md")
    write_article(vault, "\n\n".join(f"中文第 {i} 段。\n\n> 引用一句。" for i in range(4))
                  + "\n\n" + "\n\n".join(f"普通段落 {i}。" for i in range(10)), "20261101_C.md")
    c = _client()
    page = _get(c, "/read/f/信息跟进/AI Insider/20261101_A.md").get_data(as_text=True)
    pairs = _pairs(page)
    assert len(pairs) == 4 and '<main class="wide">' in page
    # 三种显示（Mac App 用 data-speech 藏掉一边，同剪藏）：英文 = 每对的 orig，中文 = 每对的 tr，对照 = 两边都在
    assert all(EN[i] in o and ZH_TR[i] not in o for i, (o, _) in enumerate(pairs)), "英文：只有原文"
    assert all(ZH_TR[i] in t and EN[i] not in t for i, (_, t) in enumerate(pairs)), "中文：只有译文"
    outside = re.sub(r'<div class="pair">.*?</div></div>', "", page, flags=re.S)
    assert "夹爪软件开源" in outside and not any(e in outside for e in EN), "小结在对照之外，三种显示都在"
    plain = _get(c, "/read/f/信息跟进/AI Insider/20261101_B.md").get_data(as_text=True)
    assert 'class="pair"' not in plain and EN[0] in plain, "没有译文的显示不变"
    zh = _get(c, "/read/f/信息跟进/AI Insider/20261101_C.md").get_data(as_text=True)
    assert 'class="pair"' not in zh, "中文文章里的引用不当成译文"


def test_reader_search_finds_both_languages_once(vault):
    write_article(vault, "\n\n".join(f"{e}\n\n> {z}" for e, z in zip(EN, ZH_TR)))
    c = _client()
    for q in ("gripper software", "夹爪软件在工厂"):
        items = c.get("/read/api/search", query_string={"q": q}).get_json()["items"]
        assert len(items) == 1, q


def test_highlight_and_unhighlight_on_original_and_translation(vault):
    p = write_article(vault, "\n\n".join(f"{e}\n\n> {z}" for e, z in zip(EN, ZH_TR)))
    rel = "信息跟进/AI Insider/20261101_A.md"
    c = _client()
    mtime = str(os.stat(p).st_mtime_ns)
    r = _post(c, "/read/api/highlight", path=rel, mtime=mtime, text="robot gripper software", before="Paragraph 1 explains how the new ")
    assert r.status_code == 200, r.get_json()
    r = _post(c, "/read/api/highlight", path=rel, mtime=r.get_json()["mtime"], text="夹爪软件", before="第 2 段讲新的机器人")
    assert r.status_code == 200, r.get_json()
    text = p.read_text(encoding="utf-8")
    assert f"Paragraph 1 explains how the new ==robot gripper software== works" in text
    assert "> 第 2 段讲新的机器人==夹爪软件==在工厂" in text
    assert "<mark>夹爪软件</mark>" in r.get_json()["html"] and "<mark>robot gripper software</mark>" in r.get_json()["html"]
    # 加批注（在译文上），能显示
    r = _post(c, "/read/api/note", path=rel, mtime=r.get_json()["mtime"], text="实验室里", before="第 3 段讲新的机器人夹爪软件在工厂和", note="想看实验数据")
    assert r.status_code == 200, r.get_json()
    text = p.read_text(encoding="utf-8")
    assert re.search(r"==实验室里==\[\^note-\d+\]", text) and "]: 想看实验数据" in text
    # 取消两处划线
    m = r.get_json()["mtime"]
    for t in ("robot gripper software", "夹爪软件"):
        r = _post(c, "/read/api/highlight", path=rel, mtime=m, text=t, remove=True, nth=0)
        assert r.status_code == 200, r.get_json()
        m = r.get_json()["mtime"]
    text = p.read_text(encoding="utf-8")
    assert "==robot gripper software==" not in text and "==夹爪软件==" not in text
    assert tt.original_section(text).count("\n\n> ") == 4, "对照结构没被划线改乱"
    page = _get(c, "/read/f/" + rel).get_data(as_text=True)
    assert len(_pairs(page)) == 4


# ---------------------------------------------------------------- 下游：复盘、笔记洞察只读原文

def test_review_and_notes2insight_read_original_only(vault):
    from apps.notes2insight import search, vault as n2i_vault
    write_article(vault, "\n\n".join(f"{e}\n\n> {z}" for e, z in zip(EN, ZH_TR)))
    rel = "Spark/信息跟进/AI Insider/20261101_A.md"
    items = review.picked_items(str(vault), [rel])
    assert len(items) == 1
    body = items[0]["body"]
    assert all(e in body for e in EN) and not any(z in body for z in ZH_TR), "复盘：原文 + 中文小结，不带译文"
    assert "夹爪软件开源" in body
    text = n2i_vault.read_note(str(vault), rel)
    assert all(text.count(e) == 1 for e in EN) and not any(z in text for z in ZH_TR)
    assert "## 小结" in text
    notes = [{"path": rel, "title": "Robot gripper", "date": "", "folder": "", "name": "20261101_A.md", "chars": 0}]
    hits = search.score_notes(str(vault), notes, [("gripper", 1)])
    assert len(hits) == 1 and "第 0 段" not in hits[0].snippet
    # 跟没译以前一样：只在译文里出现的中文词检索不到这篇
    assert search.score_notes(str(vault), notes, [("实验室", 1)]) == []
    # 不是信息文章的文件（剪藏）照旧原样读
    clip = vault / "Spark" / "剪藏"
    clip.mkdir(parents=True)
    (clip / "20261101_c.md").write_text("# c\n\n## 原文\n\nEnglish.\n\n> 中文。\n", encoding="utf-8")
    assert "> 中文。" in n2i_vault.read_note(str(vault), "Spark/剪藏/20261101_c.md")


def test_original_only_leaves_original_quotes():
    text = ARTICLE.format(body=f"> {EN[0]}\n\n> {ZH_TR[0]}\n\n{EN[1]}\n\n> {ZH_TR[1]}")
    out = tt.original_only(text)
    assert f"> {EN[0]}" in out and ZH_TR[0] not in out and ZH_TR[1] not in out
    assert tt.original_only(ARTICLE.format(body="\n\n".join(EN))) == ARTICLE.format(body="\n\n".join(EN))


# ---------------------------------------------------------------- 剪藏不变

def test_clip_helpers_unchanged():
    clip = ('---\ntitle: "A"\n---\n\n# A\n\n## 正文\n\n' + "\n\n".join(EN)
            + "\n\n[阅读原文](https://x)\n\n## 读后感\n\n想法\n")
    assert clip_compose.section_body(clip) == clip_compose.body_of(clip) == "\n\n".join(EN)
    assert clip_compose.wants_translation(clip)
    new = clip_compose.replacing_section(clip, "X")
    assert new == clip_compose.replacing_body(clip, "X")
    # 信息文章的「原文」到下一个 ## 为止，剪藏的「正文」到「[阅读原文]」
    art = ARTICLE.format(body="\n\n".join(EN) + "\n\n## 读后感\n\n想法")
    assert clip_compose.section_body(art, "原文") == "\n\n".join(EN)
    assert clip_compose.replacing_section(art, "B", "原文").endswith("## 原文\n\nB\n\n## 读后感\n\n想法\n")


def test_translate_without_stop_is_same_as_before():
    md = "\n\n".join(EN)
    body, missed = clip_compose.translate(md, lambda p, **kw: numbered_reply(p))
    assert missed == 0 and body == "\n\n".join(f"{e}\n\n> 译文{i}" for i, e in enumerate(EN))


# ---------------------------------------------------------------- 本机 Ollama 一次只跑一件事

def _fill_setup(tmp_path, monkeypatch, backend, busy):
    from apps.reader import server as reader
    monkeypatch.setenv("SPARK_VAULT", str(tmp_path))
    monkeypatch.setattr(clip_capture, "_JOBS", {})
    monkeypatch.setattr(clip_capture.github_sync, "after_task", lambda: None)
    asked = []

    def ask(prompt, **kw):
        asked.append(prompt)
        return numbered_reply(prompt)
    monkeypatch.setattr(clip_capture, "_ask", lambda: ask)
    monkeypatch.setattr(tt, "_backend", lambda: backend)
    monkeypatch.setattr(reader, "busy_jobs", lambda: busy)
    p = write_article(tmp_path, "\n\n".join(EN))
    return p, asked


def _start(c):
    return _post(c, "/read/api/track/translate", paths=["信息跟进/AI Insider/20261101_A.md"])


def _wait_track(c, job_id):
    for _ in range(300):
        st = c.get(f"/read/api/track/translate/{job_id}").get_json()
        if st["done"]:
            return st
        time.sleep(.01)
    pytest.fail("补译没做完")


def test_fill_refused_on_ollama_while_other_job_runs(tmp_path, monkeypatch):
    p, asked = _fill_setup(tmp_path, monkeypatch, "ollama", 1)
    before = p.read_text(encoding="utf-8")
    r = _start(_client())
    assert r.status_code == 400
    assert r.get_json()["error"] == "电脑上正在跑别的任务，本机模型一次只能跑一件事，等它跑完再译"
    assert p.read_text(encoding="utf-8") == before and asked == [] and clip_capture._JOBS == {}


def test_fill_starts_on_ollama_when_idle(tmp_path, monkeypatch):
    p, asked = _fill_setup(tmp_path, monkeypatch, "ollama", 0)
    c = _client()
    r = _start(c)
    assert r.status_code == 202, r.get_json()
    assert _wait_track(c, r.get_json()["id"])["warning"] == ""
    assert len(asked) == 1 and tt.untranslated(p.read_text(encoding="utf-8")) == 0


def test_fill_not_blocked_on_cloud_backend(tmp_path, monkeypatch):
    p, asked = _fill_setup(tmp_path, monkeypatch, "api", 3)
    c = _client()
    r = _start(c)
    assert r.status_code == 202, r.get_json()
    _wait_track(c, r.get_json()["id"])
    assert len(asked) == 1 and tt.untranslated(p.read_text(encoding="utf-8")) == 0


def test_reader_busy_jobs_counts_running_track_batch(monkeypatch):
    """阅读页用的 busy_jobs 就是 /api/activity 那一套：summit2md 的 track 批次在跑也算。"""
    import spark  # noqa: F401  接上 reader_server.busy_jobs
    from apps.reader import server as reader
    from apps.summit2md import server as summit_server
    monkeypatch.setattr(clip_capture, "_JOBS", {})
    idle = reader.busy_jobs()
    monkeypatch.setitem(summit_server.TRACK_JOBS, "fake", {"done": False})
    assert reader.busy_jobs() == idle + 1
