"""报告跑完后的结果概览要用的字段：进度接口里现查的报告/演示文件情况、阅读页能不能打开、
下载链接指向对的文件、部分失败的篇目。pipeline.run 换成假的，不调模型。"""

import time

from apps.notes2insight import pipeline, server


def _run(client, monkeypatch, result, **payload):
    monkeypatch.setattr(pipeline, "run", lambda cfg, progress: result)
    body = {"notes": ["a.md", "b.md"], "root": "/tmp", "backend": "api", "api_key": "sk-ant-SECRET",
            "model": "claude-sonnet-5"}
    body.update(payload)
    r = client.post("/api/run", json=body)
    assert r.status_code == 200, r.get_json()
    job = r.get_json()["job_id"]
    deadline = time.time() + 5
    while time.time() < deadline:
        d = client.get(f"/api/progress/{job}").get_json()
        if d["done"]:
            return job, d
        time.sleep(0.02)
    raise AssertionError("超时")


def _result(path, **kw):
    r = {"path": str(path), "filename": path.name, "title": "报告标题", "content": "# 正文",
         "chars": 1234, "elapsed": 75.0, "ok_count": 1, "clusters": [{"no": 1, "topic": "第一章", "notes": [1]}],
         "failed": [{"path": "b.md", "title": "B 笔记", "error": "模型超时"}], "truncated": [],
         "max_note_chars": 0}
    r.update(kw)
    return r


def test_库里output下的报告_阅读页能开_演示后来生成了也能查到(tmp_path, monkeypatch):
    monkeypatch.setenv("SPARK_VAULT", str(tmp_path))
    out = tmp_path / "output"
    out.mkdir()
    report = out / "报告.md"
    report.write_text("# 报告", encoding="utf-8")
    client = server.app.test_client()
    job, d = _run(client, monkeypatch, _result(report))

    res = d["result"]
    assert "content" not in res
    assert res["failed"][0]["title"] == "B 笔记"
    art = res["artifacts"]
    assert art["report"] == {"path": str(report), "exists": True, "in_reader": True}
    assert art["deck"]["exists"] is False
    # 结果页要显示的模型、材料数都在任务记录里，Key 不在
    assert d["settings"]["model"] == "claude-sonnet-5" and d["materials"]["count"] == 2
    assert "sk-ant-SECRET" not in client.get(f"/api/progress/{job}").get_data(as_text=True)

    # 演示是后来才生成的：再查一次就有了，下载也指到演示
    (out / "报告.deck.html").write_text("<html>deck</html>", encoding="utf-8")
    assets = out / "报告.deck.assets"
    assets.mkdir()
    (assets / "slide-1.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    art = client.get(f"/api/progress/{job}").get_json()["result"]["artifacts"]
    assert art["deck"]["exists"] and art["deck"]["in_reader"] and art["deck"]["images"] == 1

    md = client.get(f"/api/job/{job}/download/md")
    assert md.status_code == 200 and md.data.decode() == "# 报告"
    assert "attachment" in md.headers["Content-Disposition"]
    dk = client.get(f"/api/job/{job}/download/deck")
    assert dk.status_code == 200 and b"deck" in dk.data
    assert client.get(f"/api/job/{job}/download/etc").status_code == 400
    # 报告任务也能直接打开旁边的演示（输出目录在阅读页范围外时用）
    assert client.get(f"/deck/{job}/").status_code in (301, 302, 308)


def test_输出目录在库外_标出阅读页打不开(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    (vault / "output").mkdir(parents=True)
    monkeypatch.setenv("SPARK_VAULT", str(vault))
    elsewhere = tmp_path / "别处"
    elsewhere.mkdir()
    report = elsewhere / "报告.md"
    report.write_text("# 报告", encoding="utf-8")
    (elsewhere / "报告.deck.html").write_text("<html>deck</html>", encoding="utf-8")
    client = server.app.test_client()
    job, d = _run(client, monkeypatch, _result(report, failed=[]), output_dir=str(elsewhere))
    art = d["result"]["artifacts"]
    assert art["report"]["exists"] and art["report"]["in_reader"] is False
    assert art["deck"]["exists"] and art["deck"]["in_reader"] is False
    assert client.get(f"/api/job/{job}/download/md").status_code == 200


def test_报告文件被挪走了_标出不存在_下载给404(tmp_path, monkeypatch):
    monkeypatch.setenv("SPARK_VAULT", str(tmp_path))
    (tmp_path / "output").mkdir()
    report = tmp_path / "output" / "报告.md"
    client = server.app.test_client()
    job, d = _run(client, monkeypatch, _result(report))
    assert d["result"]["artifacts"]["report"]["exists"] is False
    assert client.get(f"/api/job/{job}/download/md").status_code == 404


def test_点开头目录里的报告阅读页不认(tmp_path, monkeypatch):
    monkeypatch.setenv("SPARK_VAULT", str(tmp_path))
    hidden = tmp_path / "output" / ".cache"
    hidden.mkdir(parents=True)
    assert server._reader_reachable(str(hidden / "x.md")) is False
    assert server._reader_reachable(str(tmp_path / "output" / "x.md")) is True
    assert server._reader_reachable(str(tmp_path / "Spark" / "x.md")) is True
    assert server._reader_reachable(str(tmp_path / "其他" / "x.md")) is False
