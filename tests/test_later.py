"""稍后再读：文件头 later: <加入的时刻>（core/annotations.with_later），各个重写文件的路径都留着它；
接口（/read/api/flag key=later、/read/api/later、place=later）；存链接（新剪藏带 later，抓不到存占位剪藏）和补抓。
全部用临时笔记库，抓网页、模型都换成假的，不联网。"""

import json
import os
import time

import pytest

from core import annotations as an
from core import clip_capture, clip_compose, clips, remote_tasks
from core import search_index as si
from tests.test_reader import _client

T1 = "2026-11-24T10:00:00+00:00"
T2 = "2026-11-24T11:30:00+00:00"


# ---------------------------------------------------------------- 写法：加上、移出后文件的精确内容

def test_without_frontmatter_adds_and_removes_block():
    text = "# 整理稿\n\n正文\n"
    on = an.with_later(text, True, T1)
    assert on == f"---\nlater: {T1}\n---\n\n# 整理稿\n\n正文\n"
    assert an.is_later(on) and an.later_value(on) == T1
    assert an.with_later(on, False) == text, "移出后文件头只剩空的，整段去掉，回到原样"
    assert an.with_later(text, False) == text


def test_with_frontmatter_appends_last_and_removes_line():
    text = '---\ntitle: "a"\nurl: "https://x"\n---\n\n# a\n'
    on = an.with_later(text, True, T1)
    assert on == f'---\ntitle: "a"\nurl: "https://x"\nlater: {T1}\n---\n\n# a\n'
    assert an.with_later(on, False) == text


@pytest.mark.parametrize("flags", [["starred: true"], ["pinned: true"], ["starred: true", "pinned: true"],
                                   ["pinned: true", "starred: true"]])
def test_order_with_starred_and_pinned(flags):
    head = '---\ntitle: "a"\n' + "".join(f"{f}\n" for f in flags) + "---\n\n# a\n"
    on = an.with_later(head, True, T1)
    assert on == '---\ntitle: "a"\n' + "".join(f"{f}\n" for f in flags) + f"later: {T1}\n---\n\n# a\n"
    # 之后再加星标：星标换到最后（同 with_star），稍后再读那行不动
    starred = an.with_star(on, True)
    assert starred.count("later:") == 1 and an.is_later(starred) and an.is_starred(starred)
    assert an.with_later(on, False) == head


def test_star_only_file_then_later_then_star_off():
    text = "---\nstarred: true\n---\n\n# x\n"
    on = an.with_later(text, True, T1)
    assert on == f"---\nstarred: true\nlater: {T1}\n---\n\n# x\n"
    assert an.with_star(on, False) == f"---\nlater: {T1}\n---\n\n# x\n"
    assert an.with_later(an.with_star(on, False), False) == "# x\n"


def test_adding_twice_keeps_one_line_and_first_time():
    on = an.with_later("# x\n", True, T1)
    again = an.with_later(on, True, T2)
    assert again == on and again.count("later:") == 1, "已经在里面的再加一次：文件不变，时刻不刷新"
    # 文件头里意外有两行（别处手改的）：移出时一起去掉
    dup = f"---\nlater: {T1}\nlater: {T2}\n---\n\n# x\n"
    assert an.with_later(dup, False) == "# x\n"


def test_recognized_values_do_not_depend_on_clock(monkeypatch):
    from core import doctime
    monkeypatch.setattr(doctime, "now", lambda: (_ for _ in ()).throw(AssertionError("认的时候不许看时钟")))
    assert an.is_later("---\nlater: true\n---\n") and an.later_at("---\nlater: true\n---\n") is None
    assert an.is_later(f'---\nlater: "{T1}"\n---\n') and an.later_value(f'---\nlater: "{T1}"\n---\n') == T1
    assert an.later_at(f"---\nlater: {T1}\n---\n").isoformat() == T1
    for no in ("---\nlater: false\n---\n", "---\nlater:\n---\n", "---\ntitle: x\n---\n", f"# x\nlater: {T1}\n"):
        assert not an.is_later(no), no


def test_stub_flag():
    assert an.is_stub('---\ntitle: "a"\nstub: true\n---\n') and not an.is_stub('---\ntitle: "a"\n---\nstub: true\n')


# ---------------------------------------------------------------- 重新生成（carry_over）留着

def test_carry_over_keeps_later_value_and_star():
    old = f"---\n节目: x\nstarred: true\nlater: {T1}\n---\n\n# 小结\n\n旧的 ==重点== 内容\n"
    new = "---\n节目: x\n---\n\n# 小结\n\n新的 重点 内容\n"
    out, _ = an.carry_over(old, new)
    assert an.later_value(out) == T1 and an.is_starred(out) and "==重点==" in out
    assert out.startswith(f"---\n节目: x\nstarred: true\nlater: {T1}\n---\n")
    # 没有文件头的整理稿重跑
    out2, _ = an.carry_over(f"---\nlater: {T1}\n---\n\n# 稿\n\nA\n", "# 稿\n\nB\n")
    assert out2 == f"---\nlater: {T1}\n---\n\n# 稿\n\nB\n"
    # 旧的不在稍后再读里：新的也不加
    assert not an.is_later(an.carry_over("# a\n\n==x==\n", "# a\n\nx\n")[0])


# ---------------------------------------------------------------- 剪藏的重写路径：重写摘要、补译、改名称、划线、批注、读后感

@pytest.fixture
def capture(tmp_path, monkeypatch):
    monkeypatch.setenv("SPARK_VAULT", str(tmp_path))
    monkeypatch.setattr(clip_capture, "_JOBS", {})
    monkeypatch.setattr(clip_capture.github_sync, "after_task", lambda: None)
    monkeypatch.setattr(clips, "download_images", lambda images, root: (len(images), 0))
    monkeypatch.setattr(clip_capture, "_ask", lambda: None)
    monkeypatch.setattr(clip_capture, "fetch_article", _no_network)
    return tmp_path


def _no_network(url):
    raise AssertionError("测试里不许联网")


def wait(job):
    for _ in range(500):
        state = clip_capture.status(job["id"])
        if state["done"]:
            return state
        time.sleep(.01)
    pytest.fail("任务没做完")


def _fake_model(prompts=None):
    def ask(prompt, max_tokens=4000, timeout=600, images=None):
        if prompts is not None:
            prompts.append(prompt)
        if prompt.startswith("把下面编号的英文段落"):
            import re
            nums = re.findall(r"^\[(\d+)\]", prompt.split("===== 原文 =====")[1], re.M)
            return "\n".join(f"[{n}] 中文{n}" for n in nums)
        return "TLDR: 新的一句话\nTAGS: AI芯片\n- 要点"
    return ask


def _clip(root, body="中文正文。", extra=f"later: {T1}\n", summary=""):
    folder = root / "Spark" / "剪藏"
    folder.mkdir(parents=True, exist_ok=True)
    f = folder / "20261109_旧.md"
    f.write_text('---\ntitle: "旧"\nsource: "S"\nurl: "https://example.com/a"\nclipped: 2026-11-09\n' + extra
                 + "---\n\n# 旧\n\n- 来源：S\n\n" + summary + f"## 正文\n\n{body}\n\n[阅读原文](https://example.com/a)\n",
                 encoding="utf-8")
    return f


def test_resummarize_and_translate_keep_later(capture, monkeypatch):
    monkeypatch.setattr(clip_capture, "_ask", lambda: _fake_model())
    english = "\n\n".join(f"English paragraph number {i} about memory stores and attention layers." for i in range(4))
    f = _clip(capture, english, extra=f"starred: true\nlater: {T1}\npinned: true\n")
    job = wait(clip_capture.start_fill({"paths": ["Spark/剪藏/20261109_旧.md"], "mode": "resummarize"}))
    text = f.read_text()
    assert not job["error"], job
    assert "> 中文0" in text and "## 摘要\n\n新的一句话" in text
    assert f'starred: true\nlater: {T1}\npinned: true\ntags: ["AI芯片"]\ntldr: "新的一句话"\n---' in text


def test_retitle_highlight_note_reflection_keep_later(capture):
    f = _clip(capture, "第一句话。第二句话。")
    c = _client()

    def mtime():
        return str(os.stat(f).st_mtime_ns)
    assert c.post("/read/api/retitle", json={"path": "剪藏/20261109_旧.md", "title": "新名字", "mtime": mtime()}).status_code == 200
    r = c.post("/read/api/highlight", json={"path": "剪藏/20261109_旧.md", "mtime": mtime(), "text": "第一句话", "before": ""})
    assert r.status_code == 200, r.json
    r = c.post("/read/api/note", json={"path": "剪藏/20261109_旧.md", "mtime": mtime(), "text": "第二句话",
                                       "before": "第一句话==。", "note": "批注"})
    r = c.post("/read/api/reflection", json={"path": "剪藏/20261109_旧.md", "mtime": mtime(), "text": "读后感"})
    assert r.status_code == 200, r.json
    text = f.read_text()
    assert 'title: "新名字"' in text and "==第一句话==" in text and "## 读后感\n\n读后感" in text
    assert an.later_value(text) == T1 and text.count("later:") == 1


def test_track_fill_keeps_later(tmp_path):
    from core import track_translate as tt
    folder = tmp_path / "Spark" / "信息跟进" / "AI Insider"
    folder.mkdir(parents=True)
    en = [f"English paragraph {i} explains how the robot gripper software works in detail." for i in range(4)]
    p = folder / "20261101_A.md"
    p.write_text(f"---\nlater: {T1}\n---\n\n# A\n\n- 来源：AI Insider\n\n## 原文\n\n" + "\n\n".join(en) + "\n", encoding="utf-8")
    assert tt.fill_one(str(p), _fake_model()) == ""
    text = p.read_text(encoding="utf-8")
    assert text.startswith(f"---\nlater: {T1}\n---\n") and "> 中文0" in text


# ---------------------------------------------------------------- 接口：加上、移出、列表

@pytest.fixture
def lib(tmp_path, monkeypatch):
    monkeypatch.setenv("SPARK_VAULT", str(tmp_path))
    show = tmp_path / "Spark" / "Show"
    for d in ("notes", "speech"):
        (show / d).mkdir(parents=True)
    (show / ".manifest.json").write_text("{}", encoding="utf-8")
    (show / "notes" / "20260110 第一期.md").write_text("---\n节目: x\n---\n\n# 第一期\n\n小结\n", encoding="utf-8")
    (show / "speech" / "20260110_第一期.md").write_text("# 第一期\n\n稿子\n", encoding="utf-8")
    (show / "speech" / "20260111_简讯.md").write_text("# 简讯\n\n只有整理稿\n", encoding="utf-8")
    _clip(tmp_path, extra="")
    track = tmp_path / "Spark" / "信息跟进" / "Feed"
    track.mkdir(parents=True)
    (track / "20261101_文章.md").write_text("# 文章\n\n## 原文\n\n内容\n", encoding="utf-8")
    rep = tmp_path / "output"
    rep.mkdir()
    (rep / "报告.md").write_text("# 报告\n\n内容\n", encoding="utf-8")
    return tmp_path


def _flag(c, path, on=True):
    return c.post("/read/api/flag", json={"path": path, "key": "later", "on": on})


def test_flag_writes_episode_note_and_lists_across_modules(lib, monkeypatch):
    from core import doctime
    c = _client()
    times = iter(["2026-11-24T09:00:00+00:00", "2026-11-24T09:05:00+00:00", "2026-11-24T09:10:00+00:00",
                  "2026-11-24T09:15:00+00:00"])
    monkeypatch.setattr(doctime, "now", lambda: doctime.parse(next(times)))
    # 交整理稿也写在这一期的小结上
    r = _flag(c, "Show/speech/20260110_第一期.md")
    assert r.status_code == 200 and r.json["path"] == "Show/notes/20260110 第一期.md" and r.json["later"] is True
    first = doctime.iso(doctime.parse("2026-11-24T09:00:00+00:00"))   # 写的是本机时区
    assert r.json["laterAt"] == first
    assert (lib / "Spark/Show/notes/20260110 第一期.md").read_text().startswith(f"---\n节目: x\nlater: {first}\n---\n")
    assert "later" not in (lib / "Spark/Show/speech/20260110_第一期.md").read_text()
    # 没有小结的简讯：记在整理稿上（补一段文件头）
    assert _flag(c, "Show/speech/20260111_简讯.md").json["path"] == "Show/speech/20260111_简讯.md"
    assert _flag(c, "剪藏/20261109_旧.md").status_code == 200
    assert _flag(c, "@r/报告.md").status_code == 200
    # 不带时刻的老写法排最后
    (lib / "Spark/信息跟进/Feed/20261101_文章.md").write_text("---\nlater: true\n---\n\n# 文章\n\n## 原文\n\n内容\n")
    got = c.get("/read/api/later").json
    assert got["count"] == 5
    assert [i["id"] for i in got["items"]] == ["@r/报告.md", "剪藏/20261109_旧.md", "Show/speech/20260111_简讯.md",
                                              "Show/notes/20260110 第一期.md", "信息跟进/Feed/20261101_文章.md"]
    assert got["items"][-1]["laterAt"] == "" and all(i["later"] for i in got["items"])
    assert [i["id"] for i in c.get("/read/api/items?place=later").json["items"]] == [i["id"] for i in got["items"]]
    clip = next(i for i in c.get("/read/api/items?place=clips").json["items"])
    assert clip["later"] is True and clip["stub"] is False
    # 重复加：文件不变
    before = (lib / "Spark/剪藏/20261109_旧.md").read_text()
    assert _flag(c, "剪藏/20261109_旧.md").status_code == 200
    assert (lib / "Spark/剪藏/20261109_旧.md").read_text() == before
    # 移出：文章留在原处，列表里没了
    assert _flag(c, "剪藏/20261109_旧.md", False).json["later"] is False
    assert "later" not in (lib / "Spark/剪藏/20261109_旧.md").read_text()
    assert c.get("/read/api/later").json["count"] == 4
    assert (lib / "Spark/剪藏/20261109_旧.md").exists()


def test_flag_rejects_non_note_files(lib):
    c = _client()
    (lib / "Spark/Show/Show.md").write_text("# Show\n")
    assert _flag(c, "Show/Show.md").status_code == 400
    assert _flag(c, "剪藏/没有.md").status_code == 400


def test_web_pages_show_later(lib):
    c = _client()
    _flag(c, "Show/notes/20260110 第一期.md")
    page = c.get("/read/f/Show/speech/20260110_第一期.md").get_data(as_text=True)
    assert 'class="btn later-toggle"' in page and 'data-path="Show/notes/20260110 第一期.md"' in page
    assert ">移出稍后再读</button>" in page and "later.js" in page
    clip_page = c.get("/read/f/剪藏/20261109_旧.md").get_data(as_text=True)
    assert ">稍后再读</button>" in clip_page and "stub-note" not in clip_page
    later = c.get("/read/later").get_data(as_text=True)
    assert "第一期" in later and "移出稍后再读" in later and "1 篇" in later
    home = c.get("/read/").get_data(as_text=True)
    assert 'href="/read/later"' in home and "稍后再读" in home
    assert "later-tag" in c.get("/read/f/Show/").get_data(as_text=True)


# ---------------------------------------------------------------- 存链接：新剪藏带稍后再读；抓不到存占位；补抓

def _payload(**extra):
    a = dict(url="https://example.com/a", title="文章", source="Example", published="", markdown="中文正文", images={})
    return dict(article=a, note="", **extra)


def test_new_clip_without_later_is_unchanged(capture):
    job = wait(clip_capture.start(_payload()))
    text = (capture / job["path"]).read_text()
    assert "later" not in text and "stub" not in text
    assert text.startswith('---\ntitle: "文章"\nsource: "Example"\nurl: "https://example.com/a"\nclipped: ')
    # 不带 later 只给链接：照旧报没有正文
    with pytest.raises(remote_tasks.TaskError, match="剪藏内容格式不对"):
        clip_capture.start({"url": "https://example.com/a"})
    with pytest.raises(remote_tasks.TaskError, match="没有正文"):
        clip_capture.start(_payload(later="yes") | {"article": dict(url="https://example.com/a", title="t", source="",
                                                                    published="", markdown="", images={})})


def test_new_clip_with_later(capture, monkeypatch):
    from core import doctime
    monkeypatch.setattr(doctime, "now", lambda: doctime.parse(T1))
    job = wait(_client().post("/read/api/clips", json=_payload(later=True)).json)
    text = (capture / job["path"]).read_text()
    at = doctime.iso(doctime.parse(T1))
    assert f'clipped_at: "{at}"\nlater: {at}\n---\n' in text and "stub" not in text


def test_link_fetch_fails_saves_valid_stub(capture, monkeypatch):
    def boom(url):
        raise RuntimeError("打不开这个链接")
    monkeypatch.setattr(clip_capture, "fetch_article", boom)
    r = _client().post("/read/api/clips", json={"url": "https://www.example.com/blog/post-1/?utm=x", "later": True, "note": "回头看"})
    assert r.status_code == 202
    job = wait(r.json)
    assert not job["error"] and "正文没抓到" in job["warning"] and "打不开这个链接" in job["warning"]
    assert job["path"].endswith("_example.com blog post-1.md")
    text = (capture / job["path"]).read_text()
    fm = clips.frontmatter(text)
    assert fm["title"] == "example.com/blog/post-1" and fm["url"] == "https://www.example.com/blog/post-1/?utm=x"
    assert fm["source"] == "example.com" and fm["note"] == "回头看" and fm["stub"] == "true"
    head = text.split("\n---\n", 1)[0]
    assert head.endswith("\nstub: true\nlater: " + an.later_value(text)) and an.later_at(text) is not None
    assert f"## 正文\n\n{clip_compose.STUB_NOTE}\n\n[阅读原文](https://www.example.com/blog/post-1/?utm=x)\n" in text
    # 书架、索引、搜索、补全都认得：是合法的剪藏，但不算「缺摘要」
    loaded = clips.load_clips(str(capture))
    assert loaded[0]["title"] == "example.com/blog/post-1" and loaded[0]["later"] and loaded[0]["stub"]
    assert "example.com/blog/post-1" in (capture / "Spark/剪藏/索引.md").read_text()
    assert not clip_capture.needs_fill(text)
    doc = si.parse_doc(job["path"], text)
    assert doc["title"] == "example.com/blog/post-1" and doc["later"] == an.later_value(text)
    item = _client().get("/read/api/items?place=clips").json["items"][0]
    assert item["stub"] is True and item["later"] is True and item["needs"] is False
    page = _client().get("/read/f/剪藏/" + job["path"].split("/")[-1]).get_data(as_text=True)
    assert "stub-note" in page and "补抓正文" in page
    with pytest.raises(remote_tasks.TaskError, match="先补抓正文"):
        clip_capture.start_fill({"paths": [job["path"]], "mode": "resummarize"})


def test_link_fetch_ok_saves_normal_clip(capture, monkeypatch):
    monkeypatch.setattr(clip_capture, "fetch_article", lambda url: dict(
        url=url, title="抓到的标题", source="example.com", published="2026-11-20", publishedAt="",
        markdown="抓到的正文。", images={}, titleGuessed=False, imagesOnly=False))
    job = wait(clip_capture.start({"url": "https://example.com/p", "later": True}))
    text = (capture / job["path"]).read_text()
    assert job["path"].endswith("20261120_抓到的标题.md") and "抓到的正文。" in text
    assert an.is_later(text) and not an.is_stub(text) and not job["warning"].startswith("正文没抓到")


def test_fetch_article_converts_html(monkeypatch):
    from core import sources
    monkeypatch.setattr(sources, "fetch_generic_article_entry", lambda url: {
        "title": "标题", "url": url,
        "article_content_html": "<div><h2>小节</h2><p>第一段 <b>粗</b></p><ul><li>一</li><li>二</li></ul></div>"})
    a = clip_capture.fetch_article("https://www.example.com/x")
    assert a["markdown"] == "## 小节\n\n第一段 粗\n\n- 一\n\n- 二" and a["title"] == "标题" and a["source"] == "example.com"


def _stub(root, extra=""):
    folder = root / "Spark" / "剪藏"
    folder.mkdir(parents=True, exist_ok=True)
    f = folder / "20261124_example.com p.md"
    f.write_text('---\ntitle: "example.com/p"\nsource: "example.com"\nurl: "https://example.com/p"\nclipped: 2026-11-24\n'
                 f'clipped_at: "{T1}"\nnote: "备注"\nstub: true\n' + extra + "---\n\n# example.com/p\n\n- 来源：example.com\n"
                 f"- 收录时间：2026年11月24日，10:00（UTC+0）\n- 备注：备注\n\n## 正文\n\n{clip_compose.STUB_NOTE}\n\n"
                 "[阅读原文](https://example.com/p)\n\n## 读后感\n\n先记一句\n", encoding="utf-8")
    return f


def test_refetch_success_keeps_flags_and_notes(capture, monkeypatch):
    monkeypatch.setattr(clip_capture, "_ask", lambda: _fake_model())
    monkeypatch.setattr(clip_capture, "fetch_article", lambda url: dict(
        url=url, title="真标题", source="example.com", published="", publishedAt="",
        markdown="补抓到的正文。", images={}, titleGuessed=False, imagesOnly=False))
    f = _stub(capture, f"starred: true\nlater: {T1}\npinned: true\n")
    r = _client().post("/read/api/clips/refetch", json={"path": "剪藏/" + f.name})
    assert r.status_code == 202
    job = wait(r.json)
    assert not job["error"], job
    text = f.read_text()
    fm = clips.frontmatter(text)
    from core import doctime
    assert fm["title"] == "真标题" and fm["note"] == "备注" and "stub" not in fm
    assert doctime.parse(fm["clipped_at"]) == doctime.parse(T1), "收录时刻不变"
    assert text.split("\n---\n", 1)[0].endswith(f'tldr: "新的一句话"\nnote: "备注"\nstarred: true\nlater: {T1}\npinned: true')
    assert "补抓到的正文。" in text and clip_compose.STUB_NOTE not in text and "## 摘要\n\n新的一句话" in text
    assert "## 读后感\n\n先记一句" in text
    assert len(list(f.parent.glob("*.md"))) == 2, "原地换掉，不另存（另一份是索引）"


def test_refetch_keeps_user_title_and_accepts_article(capture):
    f = _stub(capture)
    f.write_text(f.read_text().replace('title: "example.com/p"', 'title: "我起的名字"'))
    art = dict(url="https://example.com/p", title="网页标题", source="example.com", published="", markdown="Mac 抓好的", images={})
    job = wait(clip_capture.start_refetch({"path": "Spark/剪藏/" + f.name, "article": art}))
    text = f.read_text()
    assert not job["error"] and clips.frontmatter(text)["title"] == "我起的名字" and "Mac 抓好的" in text
    assert "没配好模型" in job["warning"]


def test_refetch_failure_leaves_file(capture, monkeypatch):
    def boom(url):
        raise RuntimeError("超时")
    monkeypatch.setattr(clip_capture, "fetch_article", boom)
    f = _stub(capture, f"later: {T1}\n")
    before = f.read_text()
    job = wait(clip_capture.start_refetch({"path": "Spark/剪藏/" + f.name}))
    assert "没抓到正文" in job["error"] and "超时" in job["error"]
    assert f.read_text() == before


def test_refetch_rejects_non_stub(capture):
    _clip(capture)
    r = _client().post("/read/api/clips/refetch", json={"path": "剪藏/20261109_旧.md"})
    assert r.status_code == 400 and "不用补抓" in r.json["error"]
    assert _client().post("/read/api/clips/refetch", json={"path": "Show/a.md"}).status_code == 400


# ---------------------------------------------------------------- 手机交来的新剪藏带 later

def _phone_clip():
    return ('---\ntitle: "手机存的"\nsource: "S"\nurl: "https://example.com/m"\nclipped: 2026-11-24\n---\n\n'
            "# 手机存的\n\n## 正文\n\n正文\n\n[阅读原文](https://example.com/m)\n")


def test_phone_clip_save_with_later(capture, monkeypatch):
    from core import doctime
    monkeypatch.setattr(doctime, "now", lambda: doctime.parse(T1))
    t = remote_tasks.parse_task({"type": "save", "path": "Spark/剪藏/20261124_手机存的.md", "content": _phone_clip(),
                                 "replace": True, "new": True, "later": True})
    assert t["later"] is True
    remote_tasks.save_file(t)
    text = (capture / "Spark/剪藏/20261124_手机存的.md").read_text()
    assert f'clipped: 2026-11-24\nlater: {doctime.iso(doctime.parse(T1))}\n---\n' in text
    # 不带 later：照原样写
    t2 = remote_tasks.parse_task({"type": "save", "path": "Spark/剪藏/20261124_另一篇.md", "content": _phone_clip(),
                                  "replace": True, "new": True})
    assert "later" not in t2
    remote_tasks.save_file(t2)
    assert (capture / "Spark/剪藏/20261124_另一篇.md").read_text() == _phone_clip()


# ---------------------------------------------------------------- 全文索引

def test_search_index_later_column(tmp_path):
    v = tmp_path / "vault"
    for rel, text in [("Spark/剪藏/20261101_a.md", f'---\ntitle: "甲"\nlater: {T1}\n---\n\n# 甲\n\n正文\n'),
                      ("Spark/剪藏/20261102_b.md", f'---\ntitle: "乙"\nlater: "{T2}"\n---\n\n# 乙\n\n正文\n'),
                      ("Spark/Show/speech/20260101_c.md", "---\nlater: true\n---\n\n# 丙\n"),
                      ("output/报告.md", "# 丁\n\n不在稍后再读里\n")]:
        p = v / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    assert si.SCHEMA_VERSION == 2
    idx = si.SearchIndex(str(tmp_path / "idx.sqlite"), str(v))
    idx.refresh()
    try:
        rows = idx.later_docs()
        assert [r["path"] for r in rows] == ["Spark/剪藏/20261102_b.md", "Spark/剪藏/20261101_a.md",
                                             "Spark/Show/speech/20260101_c.md"]
        assert [r["later"] for r in rows] == [T2, T1, "true"]
        # 移出后刷新：不在了
        p = v / "Spark/剪藏/20261101_a.md"
        p.write_text(an.with_later(p.read_text(encoding="utf-8"), False), encoding="utf-8")
        os.utime(p, (time.time() + 5, time.time() + 5))
        idx.refresh()
        assert [r["path"] for r in idx.later_docs()] == ["Spark/剪藏/20261102_b.md", "Spark/Show/speech/20260101_c.md"]
    finally:
        idx.close()


def test_clip_index_unaffected_by_later(capture):
    _clip(capture, extra=f"later: {T1}\n")
    with_flag = clips.rebuild_index(str(capture))
    f = capture / "Spark/剪藏/20261109_旧.md"
    f.write_text(an.with_later(f.read_text(), False))
    assert clips.rebuild_index(str(capture)) == with_flag


def test_review_skips_stub_clips(capture):
    from core import review
    _stub(capture, f"later: {T1}\n")
    _clip(capture)
    assert [c["name"] for c in review.clip_items(str(capture))] == ["20261109_旧.md"]


# ---------------------------------------------------------------- 「信息」重新抓取同一篇（tracking → carry_over）

def test_track_refetch_keeps_later(tmp_path, monkeypatch):
    import shutil
    from apps.summit2md import pipeline, tracking
    from core import sources
    from tests import test_track_translate as ttt
    from apps.summit2md import subscriptions_store
    monkeypatch.setattr(subscriptions_store, "STORE_PATH", str(tmp_path / "subscriptions.json"))
    monkeypatch.setattr(sources, "_fetch_generic_article_paragraphs", lambda url: None)
    sub = subscriptions_store.add(url="https://example.com/feed", name="AI Insider", category="海外",
                                  output_dir=str(tmp_path / "Spark"), source_type="rss")

    def llm(prompt, *a, **kw):
        return ttt.numbered_reply(prompt) if ttt.is_translate(prompt) else "TLDR: 一句话\n- 要点"
    ttt.run(sub, [ttt.entry()], llm, make_brief=False)
    path = os.path.join(sub["folder"], "20261101_Robot gripper.md")
    with open(path, encoding="utf-8") as f:
        text = f.read()
    with open(path, "w", encoding="utf-8") as f:
        f.write(an.with_later(text, True, T1))
    shutil.rmtree(os.path.join(sub["folder"], ".cache", "sources"))
    changed = ttt.EN[:3] + ["Paragraph 3 was rewritten by the site after publication, with new numbers."]
    monkeypatch.setattr(pipeline, "_cached_summarize", llm)
    res = tracking.process_item(sub, ttt.entry(paragraphs=changed), llm=ttt.LLM, summary_length="medium", max_chars=0)
    assert res["ok"]
    with open(path, encoding="utf-8") as f:
        new = f.read()
    assert "rewritten" in new and an.later_value(new) == T1 and new.count("later:") == 1


def test_jotting_edit_keeps_later():
    from datetime import datetime, timezone
    from core import jottings as jt
    t0 = datetime(2026, 11, 22, 21, 30, 5, tzinfo=timezone.utc)
    text = an.with_later(jt.render("标题", "正文", t0), True, T1)
    out = jt.updated(text, "新标题", "新正文", datetime(2026, 11, 23, 8, 0, tzinfo=timezone.utc))
    assert an.later_value(out) == T1 and 'title: "新标题"' in out and out.count("later:") == 1
