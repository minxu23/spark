"""主题综述（core/review.py 的 kind "tag"）：材料取自标签索引，最多 40 篇、新的在前，超过时提示词里有说明，
别名、错误、存出的文档、接口，以及手机提示词（apps/mobile-ios/Spark/TagReviewPrompt.swift）和电脑逐字一致。
笔记库用临时目录，模型是假的，不碰真实笔记库。"""

import json
import os
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import review, tags, remote_tasks

ROOT = os.path.join(os.path.dirname(__file__), "..")
SWIFT_SRC = os.path.join(ROOT, "apps", "mobile-ios", "Spark", "TagReviewPrompt.swift")


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _clip(v, n, tag="线", day=None):
    day = day or (f"2026-03-{n:02d}" if n <= 28 else f"2026-04-{n - 28:02d}")
    fm = (f"---\ntitle: \"文章{n:02d}\"\nsource: \"新智元\"\nclipped: {day}\ntags: {json.dumps([tag], ensure_ascii=False)}\n"
          f"tldr: \"第{n}篇的一句话\"\n---\n\n# 文章{n:02d}\n\n## 正文\n\n正文。\n")
    _write(str(v / "Spark" / "剪藏" / f"{day.replace('-', '')}_文章{n:02d}.md"), fm)


@pytest.fixture
def lib(tmp_path, monkeypatch):
    v = tmp_path / "vault"
    monkeypatch.setenv("SPARK_VAULT", str(v))
    calls = []

    def fake_llm(prompt, max_tokens=8000):
        calls.append(prompt)
        return "TITLE: 线 的几条主线\n## 这些内容合起来在讲什么\n……"

    monkeypatch.setattr(review, "_llm", fake_llm)
    return v, calls


def test_材料只含这个标签_最多40篇_新的在前(lib):
    v, calls = lib
    for n in range(1, 46):
        _clip(v, n)
    _clip(v, 50, tag="别的", day="2026-05-01")
    path = review.run("tag", {"tag": "线"})
    prompt = calls[-1]
    assert "文章50" not in prompt
    assert "这个主题共 45 篇，这里只读了最近 40 篇" in prompt
    assert prompt.count("第二行单独写一句：「这个主题共 45 篇，这里只读了最近 40 篇。」") == 1
    assert prompt.count("\n[") == 40 and "[41]" not in prompt
    # 日期最新的（4 月 17 日那篇，n=45）在最前，第 6 篇以前的被丢掉
    assert prompt.index("文章45") < prompt.index("文章30") < prompt.index("文章06")
    assert "文章05" not in prompt
    assert "《文章45》（新智元，2026-04-17）" in prompt and "[1] 剪藏　" in prompt
    text = open(path, encoding="utf-8").read()
    assert text.startswith("# 主题综述：线 的几条主线\n")
    assert "- 主题：线" in text and "读了 40 篇（这个主题共 45 篇，这里只读了最近 40 篇）" in text
    assert os.path.basename(path).startswith("复盘_主题综述_线_")


def test_不超过40篇时没有截断说明(lib):
    v, calls = lib
    for n in range(1, 5):
        _clip(v, n)
    path = review.run("tag", {"tag": "线"})
    assert "只读了最近" not in calls[-1] and "材料（共 4 篇）" in calls[-1]
    assert "从第二行开始写这几节：" in calls[-1]
    assert "- 读了 4 篇\n" in open(path, encoding="utf-8").read()


def test_别名指向的标签能找到(lib):
    v, calls = lib
    for n in range(1, 4):
        _clip(v, n, tag="正式名")
    _write(str(v / "Spark" / ".tags.json"), json.dumps({"version": 1, "aliases": {"旧名": "正式名"}}))
    review.run("tag", {"tag": "旧名"})
    assert "文章01" in calls[-1] and "「旧名」" in calls[-1]


def test_节目条目带上处理记录里的小结(lib):
    v, calls = lib
    show = v / "Spark" / "某播客"
    _write(str(show / ".manifest.json"), json.dumps({"content_type": "series", "entries": {"a": {
        "ok": True, "entry": {"id": "a", "title": "一期节目", "publish_date": "20260102"},
        "note_relative_path": "notes/20260102_a.md",
        "summary": {"tldr": "节目一句话", "body": "节目小结正文很长"}}}}, ensure_ascii=False))
    _write(str(show / "notes" / "20260102_a.md"), "# 一期节目\n\n小结\n")
    _write(str(v / "Spark" / ".tags.json"), json.dumps({"version": 1, "docs": {"某播客/notes/20260102_a.md": ["线"]}}))
    _clip(v, 1)
    review.run("tag", {"tag": "线"})
    prompt = calls[-1]
    assert "播客　《一期节目》（某播客，2026-01-02）\n一句话：节目一句话\n节目小结正文很长" in prompt
    assert "剪藏　《文章01》（新智元，2026-03-01）\n一句话：第1篇的一句话\n" in prompt


def _doc(v, folder, name, title, abstract, tag="线", clipped="2026-03-01"):
    fm = (f"---\ntitle: \"{title}\"\nsource: \"来源\"\nclipped: {clipped}\ntags: [\"{tag}\"]\ntldr: \"{title}的一句话\"\n---\n\n# {title}\n\n"
          + (f"## 摘要\n\n{abstract}\n\n" if abstract else "") + "## 正文\n\n正文不该进材料。\n")
    _write(str(v / "Spark" / folder / name), fm)


def test_剪藏和论文带上摘要一节_没有就只用一句话(lib):
    v, calls = lib
    _doc(v, "剪藏", "20260303_甲.md", "甲", "导读开头。\n\n### 小标题\n\n![](a.png)小标题下的话。", clipped="2026-03-03")
    _doc(v, "剪藏", "20260302_乙.md", "乙", "", clipped="2026-03-02")
    _doc(v, "论文", "20260301_丙.md", "丙", "论文摘要一段。" + "长" * 1100)
    review.run("tag", {"tag": "线"})
    prompt = calls[-1]
    assert "一句话：甲的一句话\n导读开头。\n\n### 小标题\n\n小标题下的话。\n" in prompt
    assert "一句话：乙的一句话\n\n[3]" in prompt
    assert "正文不该进材料" not in prompt
    assert "论文　《丙》" in prompt and "论文摘要一段。" in prompt and "长……" in prompt   # 截到 1000 字


def test_没有这个标签或没有条目时报错(lib):
    v, calls = lib
    _clip(v, 1)
    with pytest.raises(review.ReviewError, match="标签「不存在」下没有内容"):
        review.run("tag", {"tag": "不存在"})
    with pytest.raises(review.ReviewError, match="要指定一个标签"):
        review.run("tag", {})
    assert not calls


def test_种类名_和别的复盘同一种出处写法(lib):
    v, calls = lib
    assert review.KIND_LABELS["tag"] == "主题综述" and "tag" in review.KINDS
    for n in range(1, 4):
        _clip(v, n)
    review.run("tag", {"tag": "线"})
    # 出处沿用 _STYLE 和 _source：《标题》（来源，日期）
    assert review._STYLE in calls[-1]
    assert review._source({"title": "文章01", "show": "新智元", "date": "20260301"}) in calls[-1]


def test_接口(lib):
    v, calls = lib
    from werkzeug.test import Client
    import spark
    import time
    c = Client(spark.application)
    assert c.post("/review/api/run", json={"kind": "tag"}).status_code == 400
    assert c.post("/review/api/run", json={"kind": "tag", "tag": "  "}).status_code == 400
    for n in range(1, 4):
        _clip(v, n)
    r = c.post("/review/api/run", json={"kind": "tag", "tag": "线"})
    assert r.status_code == 200
    job = r.get_json()["job_id"]
    for _ in range(100):
        st = c.get(f"/review/api/progress/{job}").get_json()
        if st["done"]:
            break
        time.sleep(0.02)
    assert st["ok"] and st["result"]["url"].startswith("/read/r/")
    items = c.get("/review/api/list").get_json()
    assert items[0]["kind"] == "tag"


def test_手机交给电脑的任务认tag():
    t = remote_tasks.parse_task({"type": "review", "kind": "tag", "tag": " 线 "})
    assert t["tag"] == "线" and t["kind"] == "tag"
    with pytest.raises(remote_tasks.TaskError):
        remote_tasks.parse_task({"type": "review", "kind": "tag"})


# ---------------------------------------------------------------- 手机提示词逐字一致

MAIN = """import Foundation
let cases = try! JSONSerialization.jsonObject(with: FileHandle.standardInput.readDataToEndOfFile()) as! [[String: Any]]
var out: [String] = []
for c in cases {
    let rows = (c["rows"] as! [[String: String]]).map {
        TagReviewPrompt.Row(type: $0["type"]!, source: $0["source"]!, tldr: $0["tldr"]!, body: $0["body"]!)
    }
    out.append(TagReviewPrompt.prompt(tag: c["tag"] as! String, rows: rows, total: c["total"] as! Int, style: c["style"] as! String))
}
print(String(decoding: try! JSONSerialization.data(withJSONObject: out), as: UTF8.self))
"""


def test_两端提示词逐字一致(tmp_path):
    if not shutil.which("swiftc"):
        pytest.skip("没有 swiftc")
    main = tmp_path / "main.swift"
    main.write_text(MAIN, encoding="utf-8")
    exe = tmp_path / "tag_prompt"
    r = subprocess.run(["swiftc", "-o", str(exe), SWIFT_SRC, str(main)], capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr

    def item(i, tldr="一句话", body="小结正文"):
        return {"type": "播客", "show": f"节目{i}", "title": f"标题{i}", "date": "20260102", "tldr": tldr, "body": body}

    cases = [
        {"tag": "具身智能", "total": 3, "items": [item(1), item(2, tldr=""), item(3, body="")]},
        {"tag": "AI \"安全\"", "total": 40, "items": [item(i) for i in range(1, 41)]},
        {"tag": "芯片", "total": 57, "items": [item(i) for i in range(1, 41)]},
        {"tag": "剪藏", "total": 2, "items": [item(1, body="导读。\n\n### 小标题\n\n下面的话。"), item(2, body="长" * 1500)]},
        {"tag": "空", "total": 1, "items": [item(1, tldr="", body="")]},
    ]
    sent, want = [], []
    for c in cases:
        want.append(review.tag_prompt(c["tag"], c["items"], c["total"]))
        sent.append({"tag": c["tag"], "total": c["total"], "style": review._STYLE,
                     "rows": [{"type": it["type"], "source": review._source(it), "tldr": it["tldr"],
                               "body": review._cut(it["body"], 1000) if it["body"] else ""} for it in c["items"]]})
    run = subprocess.run([str(exe)], input=json.dumps(sent), capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    got = json.loads(run.stdout)
    for g, w in zip(got, want):
        assert g == w
