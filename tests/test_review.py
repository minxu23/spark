"""阅读记录（core/readlog.py）和复盘（core/review.py）。

笔记库用 SPARK_VAULT 指到临时目录，里面放两个假节目；模型调用换成假的，记下提示词、返回固定文字。
"""

import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import readlog, remote_tasks, review


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _entry(eid, title, date, tldr, body, note=None):
    row = {"ok": True, "entry": {"id": eid, "title": title, "publish_date": date},
           "relative_path": f"transcripts/{date}_{eid}.md", "speech_relative_path": f"speech/{date}_{eid}.md",
           "summary": {"tldr": tldr, "body": body}, "updated_at": f"{date[:4]}-{date[4:6]}-{date[6:]}T10:00:00"}
    if note:
        row["note_relative_path"] = note
    return row


@pytest.fixture
def lib(tmp_path, monkeypatch):
    v = tmp_path / "vault"
    monkeypatch.setenv("SPARK_VAULT", str(v))
    today = time.strftime("%Y%m%d")
    old = "20250101"
    show = v / "Spark" / "Dwarkesh"
    _write(str(show / ".manifest.json"), json.dumps({"content_type": "series", "entries": {
        "a": _entry("a", "机器人什么时候进家庭", today, "家用机器人还要五到十年。", "### 要点\n- 家用机器人规模化要五到十年", "notes/a.md"),
        "b": _entry("b", "RSI 走到哪了", today, "递归自我改进还在早期。", "### 要点\n- RSI 还在早期"),
        "c": _entry("c", "两年内会有通用机器人", old, "嘉宾预测两年内通用机器人量产。", "### 要点\n- 预测：两年内通用机器人量产"),
    }}, ensure_ascii=False))
    _write(str(show / "notes" / "a.md"), "# 机器人\n\n小结\n\n## 我的高亮\n- 数据飞轮比模型更重要\n")
    calls = []

    def fake_llm(prompt, max_tokens=8000):
        calls.append(prompt)
        if "每条输出一行 JSON" in prompt:
            return '{"claim": "两年内通用机器人量产", "who": "嘉宾", "source": 1, "horizon": "两年"}\n'
        return "## 这段时间读了什么\n- 机器人（《机器人什么时候进家庭》）"

    monkeypatch.setattr(review, "_llm", fake_llm)
    return v, calls


def test_readlog_merge_dedups_and_cleans():
    now = time.time()
    good = {"id": "e1", "at": now, "source": "phone", "show": "Spark/Dwarkesh", "entry": "a", "kind": "note"}
    assert readlog.merge([good, good, {"id": "e2", "at": now, "path": "../../etc/passwd"},
                          {"id": "e3", "at": "坏的", "path": "x.md"}]) == 1
    assert readlog.merge([good]) == 0
    rows = readlog.load()
    assert len(rows) == 1 and rows[0]["source"] == "phone"


def test_record_desktop_dedups_within_half_hour():
    readlog._RECENT.clear()
    readlog.record_desktop("Spark/Dwarkesh/notes/a.md", "note")
    readlog.record_desktop("Spark/Dwarkesh/notes/a.md", "note")
    assert len(readlog.load()) == 1


def test_parse_comment():
    body = "手机阅读记录\n\n```spark-readlog\n" + json.dumps({"id": "x", "at": 1.0}) + "\n不是 JSON\n```\n"
    assert readlog.parse_comment(body) == [{"id": "x", "at": 1.0}]


def test_reading_review_uses_both_devices_and_highlights(lib):
    v, calls = lib
    now = time.time()
    readlog.merge([
        {"id": "p1", "at": now - 3600, "source": "phone", "show": "Spark/Dwarkesh", "entry": "a", "kind": "note"},
        {"id": "d1", "at": now - 60, "source": "desktop", "path": f"Spark/Dwarkesh/speech/{time.strftime('%Y%m%d')}_a.md",
         "kind": "speech"},
    ])
    path = review.run("reading", {"days": 7})
    assert path.startswith(os.path.join(str(v), "output", "复盘"))
    text = open(path, encoding="utf-8").read()
    assert "读了 1 篇（电脑上 1，手机上 1）" in text and "没读的 1 篇" in text
    prompt = calls[-1]
    assert "数据飞轮比模型更重要" in prompt          # 我的高亮
    assert "小结、整理稿" in prompt                  # 两端读的稿子合在一起
    assert "RSI 走到哪了" in prompt.split("新进来但没读的")[1]


def test_reading_review_with_nothing_fails(lib, monkeypatch):
    monkeypatch.setattr(review, "build_index", lambda root="": {"items": {}, "by_path": {}, "root": root})
    with pytest.raises(review.ReviewError, match="没有阅读记录"):
        review.run("reading", {"days": 7})


def test_claims_review(lib):
    v, calls = lib
    path = review.run("claims", {"months": 6})
    assert "观点检验" in os.path.basename(path)
    judge = calls[-1]
    assert "[C1] 两年内通用机器人量产" in judge and "机器人什么时候进家庭" in judge


def test_topic_review_needs_search(lib, monkeypatch):
    v, calls = lib
    monkeypatch.setattr(review, "_search", lambda topic, progress: [
        {"path": f"Spark/Dwarkesh/speech/{time.strftime('%Y%m%d')}_a.md", "relevance": 3},
        {"path": f"Spark/Dwarkesh/speech/{time.strftime('%Y%m%d')}_b.md", "relevance": 3},
        {"path": "Spark/Dwarkesh/speech/20250101_c.md", "relevance": 2},
    ])
    path = review.run("topic", {"topic": "机器人"})
    prompt = calls[-1]
    # 按时间先后排：2025 年那篇在最前
    assert prompt.index("两年内会有通用机器人") < prompt.index("机器人什么时候进家庭")
    assert "主题来龙去脉：机器人" in open(path, encoding="utf-8").read()


def test_remote_task_review_parse():
    t = remote_tasks.parse_task({"type": "review", "kind": "reading", "days": 30})
    assert t == {"type": "review", "kind": "reading", "days": 30, "months": 6}
    with pytest.raises(remote_tasks.TaskError):
        remote_tasks.parse_task({"type": "review", "kind": "topic"})
    with pytest.raises(remote_tasks.TaskError):
        remote_tasks.parse_task({"type": "review", "kind": "reading", "days": 9999})


def test_review_api(lib):
    from werkzeug.test import Client
    import spark
    c = Client(spark.application)
    assert c.post("/review/api/run", json={"kind": "topic"}).status_code == 400
    r = c.post("/review/api/run", json={"kind": "claims", "months": 6})
    job = r.get_json()["job_id"]
    for _ in range(100):
        st = c.get(f"/review/api/progress/{job}").get_json()
        if st["done"]:
            break
        time.sleep(0.02)
    assert st["ok"] and st["result"]["url"].startswith("/read/r/")
    items = c.get("/review/api/list").get_json()
    assert items and items[0]["kind"] == "claims"


def _clip(v, name, title, clipped, tags, summary="", note=""):
    fm = (f"---\ntitle: {json.dumps(title, ensure_ascii=False)}\nsource: \"新智元\"\nclipped: {clipped}\n"
          f"tags: {json.dumps(tags, ensure_ascii=False)}\n" + (f"note: {json.dumps(note, ensure_ascii=False)}\n" if note else "")
          + "---\n\n# " + title + "\n\n- 来源：新智元\n\n")
    body = (f"## 摘要\n\n{summary}\n\n" if summary else "") + "## 正文\n\n![](attachments/0123456789abcdef.png)\n\n正文开头一段。\n"
    _write(str(v / "Spark" / "剪藏" / name), fm + body)


def test_clips_review_uses_period_earlier_tags_and_related(lib):
    v, calls = lib
    today = time.strftime("%Y-%m-%d")
    _clip(v, "20260101_旧文章.md", "旧文章", "2026-01-01", ["AI芯片"])
    _clip(v, "a_新文章.md", "家用机器人的数据难题", today, ["机器人", "具身智能"], "**一句话**：数据是瓶颈", note="跟进数据飞轮")
    _clip(v, "b_没摘要.md", "另一篇", today, [])
    path = review.run("clips", {"days": 30})
    assert os.path.basename(path).startswith("复盘_剪藏_")
    prompt = calls[-1]
    assert "共 2 篇" in prompt and "数据是瓶颈" in prompt and "我存它时的备注：跟进数据飞轮" in prompt
    assert "正文开头一段" in prompt and "attachments" not in prompt          # 没摘要的取正文开头，图片去掉
    assert "AI芯片（1 篇）" in prompt                                      # 以前的标签
    assert "机器人什么时候进家庭" in prompt and "两年内会有通用机器人" not in prompt   # 相关的新内容按标签匹配、只要这段时间的
    with open(path, encoding="utf-8") as f:
        assert "这段时间存了 2 篇，更早的 1 篇" in f.read()


def test_clips_review_without_clips_fails(lib):
    with pytest.raises(review.ReviewError, match="没有剪藏"):
        review.run("clips", {"days": 7})
    assert remote_tasks.parse_task({"type": "review", "kind": "clips", "days": 30})["kind"] == "clips"


def test_picked_clips_summary(lib):
    """挑出来的几篇放在一起写：摘要、正文、备注都进提示词，按存的先后排，重复和不是剪藏的路径不算。"""
    v, calls = lib
    _clip(v, "20260301_b.md", "后存的一篇", "2026-03-01", ["机器人"], "**一句话**：数据是瓶颈")
    _clip(v, "20260201_a.md", "先存的一篇", "2026-02-01", [], note="对照着看")
    _clip(v, "20260401_c.md", "没挑的一篇", "2026-04-01", [])
    paths = ["Spark/剪藏/20260301_b.md", "Spark/剪藏/20260201_a.md", "Spark/剪藏/20260301_b.md", "Spark/别处/x.md"]
    path = review.run("picked", {"paths": paths})
    assert os.path.basename(path).startswith("复盘_综合_先存的一篇等2篇_")
    prompt = calls[-1]
    assert "挑出来的 2 篇" in prompt and prompt.index("先存的一篇") < prompt.index("后存的一篇")
    assert "数据是瓶颈" in prompt and "我存它时的备注：对照着看" in prompt and "正文开头一段" in prompt
    assert "没挑的一篇" not in prompt and "attachments" not in prompt
    with open(path, encoding="utf-8") as f:
        text = f.read()
    assert text.startswith("# 剪藏综合小结：《先存的一篇》等 2 篇") and "选了 2 篇：《先存的一篇》、《后存的一篇》" in text and "<!-- sources: " in text
    with pytest.raises(review.ReviewError, match="至少要选 2 篇"):
        review.run("picked", {"paths": ["Spark/剪藏/20260201_a.md", "Spark/剪藏/没有这篇.md"]})


def test_picked_task_and_api(lib):
    v, calls = lib
    ok = ["Spark/剪藏/a.md", "Spark/剪藏/b.md"]
    t = remote_tasks.parse_task({"type": "review", "kind": "picked", "paths": ok + ["Spark/剪藏/a.md"]})
    assert t["paths"] == ok
    for bad in (["Spark/剪藏/a.md"], ["Spark/剪藏/a.md", "output/../x.md"], [f"Spark/剪藏/{i}.md" for i in range(21)], "Spark/剪藏/a.md"):
        with pytest.raises(remote_tasks.TaskError):
            remote_tasks.parse_task({"type": "review", "kind": "picked", "paths": bad})

    from werkzeug.test import Client
    import spark
    c = Client(spark.application)
    _clip(v, "20260201_a.md", "先存的一篇", "2026-02-01", ["芯片"])
    _clip(v, "20260301_b.md", "后存的一篇", "2026-03-01", [])
    clips_list = c.get("/review/api/clips").get_json()
    assert [x["title"] for x in clips_list] == ["后存的一篇", "先存的一篇"] and clips_list[1]["tags"] == ["芯片"]
    assert c.post("/review/api/run", json={"kind": "picked", "paths": ["Spark/剪藏/20260201_a.md"]}).status_code == 400
    job = c.post("/review/api/run", json={"kind": "picked", "paths": [x["path"] for x in clips_list]}).get_json()["job_id"]
    for _ in range(100):
        st = c.get(f"/review/api/progress/{job}").get_json()
        if st["done"]:
            break
        time.sleep(0.02)
    assert st["ok"], st
    assert c.get("/review/api/list").get_json()[0]["kind"] == "picked"


def test_picked_mixes_clips_with_other_notes_and_uses_model_title(lib, monkeypatch):
    """综合小结能混着挑剪藏和别的笔记，标题用模型起的，别的笔记取全文。"""
    v, _calls = lib
    _clip(v, "20260201_a.md", "一篇剪藏", "2026-02-01", ["芯片"])
    _write(str(v / "Spark" / "节目" / "notes" / "20260301 第一期.md"), "---\n节目: x\n---\n\n# 第一期\n\n节目里的要点。\n")
    seen = []
    monkeypatch.setattr(review, "_llm", lambda p, max_tokens=8000: seen.append(p) or "TITLE: 《芯片的下一步》\n## 这几篇在讲什么\n芯片。")
    path = review.run("picked", {"paths": ["Spark/剪藏/20260201_a.md", "Spark/节目/notes/20260301 第一期.md"]})
    assert "节目里的要点" in seen[0] and "《第一期》（节目，2026-03-01）" in seen[0]
    with open(path, encoding="utf-8") as f:
        text = f.read()
    assert text.startswith("# 综合小结：芯片的下一步\n") and "TITLE" not in text
    assert '<!-- sources: ["Spark/剪藏/20260201_a.md", "Spark/节目/notes/20260301 第一期.md"] -->' in text


def test_review_can_be_replaced_from_phone(lib):
    """手机上改了复盘的标题、写了追问：覆盖原文件，不另存一份。"""
    v, _ = lib
    t = remote_tasks.parse_task({"type": "save", "path": "output/复盘/复盘_综合_x.md", "content": "# 旧\n"})
    remote_tasks.save_file(t)
    t = remote_tasks.parse_task({"type": "save", "path": "output/复盘/复盘_综合_x.md", "content": "# 新\n", "replace": True})
    assert remote_tasks.save_file(t).startswith("更新了")
    with open(v / "output" / "复盘" / "复盘_综合_x.md", encoding="utf-8") as f:
        assert f.read() == "# 新\n"
    assert not (v / "output" / "复盘" / "复盘_综合_x_2.md").exists()
    t = remote_tasks.parse_task({"type": "save", "path": "output/报告.md", "content": "x", "replace": True})
    assert not t.get("replace")


def test_reading_prompt_marks_depth():
    it = {"title": "t", "show": "s", "date": "20261101", "tldr": "", "body": "", "note": "", "folder": ""}
    mat = {"reads": [{"item": it, "kinds": {"note"}}, {"item": it, "kinds": {"speech"}}], "read_total": 2, "unread": [], "unread_total": 0}
    p = review.reading_prompt(mat, "", "近 7 天")
    assert "（只看了小结）" in p and "（读得深）" in p and "不是这段时间新出了什么" in p


def test_read_ping_records_after_dwell(lib, monkeypatch):
    """打开页面不记阅读记录，页面停留够久发 read_ping 才记。"""
    from werkzeug.test import Client
    import spark
    from core import readlog
    seen = []
    monkeypatch.setattr(readlog, "record_desktop", lambda rel, kind="": seen.append((rel, kind)))
    c = Client(spark.application)
    assert c.get("/read/s/Dwarkesh/notes/a.md").status_code in (200, 302, 404)
    assert seen == []
    assert c.post("/read/api/read_ping", json={"path": "Dwarkesh/notes/a.md"}).status_code == 200
    assert seen == [("Spark/Dwarkesh/notes/a.md", "note")]
    assert c.post("/read/api/read_ping", json={"path": "../../etc/passwd"}).status_code == 400
