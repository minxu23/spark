"""数据线同步（core/cable_sync.py）。

传输换成本机临时目录：「推到手机」就是拷进一个假的 App 容器，「手机发件箱」是另一个目录。
笔记库用 SPARK_VAULT 指到临时目录，设置文件（和 ~/.spark 下的缓存）conftest 已经指到临时目录。
"""

import json
import os
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import cable_sync as cs


def _write(path, text="x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


class FakeTransport:
    def __init__(self, phone, outbox):
        self.phone, self.outbox = phone, outbox
        self.pushes = 0

    def push(self, local_dir, dest):
        self.pushes += 1
        shutil.copytree(local_dir, os.path.join(self.phone, dest), dirs_exist_ok=True)

    def pull_outbox(self, local_dir):
        if os.path.isdir(self.outbox):
            shutil.copytree(self.outbox, local_dir, dirs_exist_ok=True)


@pytest.fixture
def env(tmp_path, monkeypatch):
    v = tmp_path / "vault"
    monkeypatch.setenv("SPARK_VAULT", str(v))
    monkeypatch.setitem(cs._CALL, "fn", None)      # 不在后台真跑任务
    monkeypatch.setitem(cs._BOOK, "book", None)
    _write(str(v / "Spark" / "节目" / "notes" / "第1期.md"), "小结")
    _write(str(v / "Spark" / "节目" / ".manifest.json"), "{}")
    _write(str(v / "Spark" / "节目" / ".cache" / "a.json"), "缓存")
    _write(str(v / "Spark" / "节目" / "logs" / "t.log"), "日志")
    _write(str(v / "Spark" / ".DS_Store"), "")
    _write(str(v / "Spark" / ".organization-backups" / "旧.md"), "备份")
    _write(str(v / "output" / "报告.md"), "报告")
    _write(str(v / "我的日记.md"), "私人")
    phone, outbox = tmp_path / "phone", tmp_path / "outbox"
    return v, FakeTransport(str(phone), str(outbox)), phone, outbox


def _device():
    return {"id": "dev-1", "udid": "dev-1", "name": "测试 iPhone", "transport": "wired"}


def test_blob_sha_matches_git(tmp_path):
    p = tmp_path / "a.md"
    p.write_text("你好，Spark\n", encoding="utf-8")
    git = subprocess.run(["git", "hash-object", str(p)], capture_output=True, text=True, check=True).stdout.strip()
    assert cs.blob_sha(str(p)) == git


def test_sync_pushes_vault_and_manifest(env):
    v, tr, phone, _ = env
    result = cs.sync_device(_device(), transport=tr)
    assert result["ok"] and result["files"] == 3
    dest = phone / cs.DEST
    manifest = json.loads((dest / cs.MANIFEST).read_text())
    assert set(manifest["files"]) == {"Spark/节目/notes/第1期.md", "Spark/节目/.manifest.json", "output/报告.md"}
    assert manifest["files"]["output/报告.md"][0] == cs.blob_sha(str(v / "output" / "报告.md"))
    assert (dest / "Spark" / "节目" / "notes" / "第1期.md").read_text(encoding="utf-8") == "小结"
    assert not (dest / "我的日记.md").exists() and not (dest / "Spark" / "节目" / ".cache").exists()
    assert (dest / cs.RESULTS).exists()


def test_staging_drops_deleted_files_and_uses_hardlinks(env, tmp_path):
    v, tr, _, _ = env
    cs.sync_device(_device(), transport=tr)
    staging = tmp_path / "_spark_settings" / "cable-staging" / "vault"
    src = v / "output" / "报告.md"
    assert os.stat(staging / "output" / "报告.md").st_ino == os.stat(src).st_ino
    os.remove(src)
    result = cs.sync_device(_device(), transport=tr)
    assert result["files"] == 2 and not (staging / "output" / "报告.md").exists()


def test_outbox_tasks_received_once_and_validated(env):
    _v, tr, phone, outbox = env
    _write(str(outbox / "a1.json"), json.dumps({"id": "a1", "title": "检查订阅：量子位",
                                                 "task": {"type": "check", "subscriptions": ["量子位"]}}))
    _write(str(outbox / "b2.json"), json.dumps({"id": "b2", "task": {"type": "rm", "path": "/"}}))
    _write(str(outbox / "bad.json"), "不是 JSON")
    r = cs.sync_device(_device(), transport=tr)
    assert r["tasks_received"] == 2 and r["tasks_waiting"] == 1
    results = json.loads((phone / cs.DEST / cs.RESULTS).read_text())["tasks"]
    assert results["a1"]["state"] == "waiting" and results["b2"]["state"] == "failed"
    # 再同步一次：发件箱里还是那两个文件，不重复收
    assert cs.sync_device(_device(), transport=tr)["tasks_received"] == 0


def test_worker_runs_received_task(env, monkeypatch):
    _v, tr, phone, outbox = env
    _write(str(outbox / "c3.json"), json.dumps({"id": "c3", "task": {"type": "link", "url": "https://youtu.be/x"}}))
    ran = []

    class FakeRunner:
        def __init__(self, call, progress):
            self.progress = progress

        def run(self, task):
            ran.append(task["url"])
            self.progress.update("处理：写小结 1/2", current=1, total=2)
            return "处理完 1 条"

    monkeypatch.setattr(cs.remote_tasks, "Runner", FakeRunner)
    monkeypatch.setattr(cs, "Transport", lambda device: tr)
    monkeypatch.setitem(cs._CALL, "fn", lambda *a: (200, {}))
    cs.sync_device(_device(), transport=tr)
    for _ in range(100):
        if not cs._STATE["worker"]:
            break
        import time
        time.sleep(0.02)
    assert ran == ["https://youtu.be/x"]
    results = json.loads((phone / cs.DEST / cs.RESULTS).read_text())["tasks"]
    assert results["c3"]["state"] == "done" and "处理完" in results["c3"]["text"]


def test_list_devices_filters(monkeypatch, tmp_path):
    raw = {"result": {"devices": [
        {"identifier": "A", "hardwareProperties": {"platform": "iOS", "reality": "physical", "deviceType": "iPhone",
                                                   "udid": "U1", "marketingName": "iPhone 16 Pro Max"},
         "deviceProperties": {"name": "我的手机"}, "connectionProperties": {"pairingState": "paired", "transportType": "wired"}},
        {"identifier": "B", "hardwareProperties": {"platform": "iOS", "reality": "physical", "deviceType": "iPad"},
         "deviceProperties": {"name": "iPad"}, "connectionProperties": {"pairingState": "paired", "transportType": "wired"}},
        {"identifier": "C", "hardwareProperties": {"platform": "iOS", "reality": "physical", "deviceType": "iPhone"},
         "deviceProperties": {"name": "没连着"}, "connectionProperties": {"pairingState": "paired"}},
        {"identifier": "D", "hardwareProperties": {"platform": "iOS", "reality": "simulated", "deviceType": "iPhone", "udid": "D"},
         "deviceProperties": {"name": "模拟器", "bootState": "booted"}, "connectionProperties": {}},
        {"identifier": "E", "hardwareProperties": {"platform": "watchOS", "reality": "simulated"},
         "deviceProperties": {"name": "手表", "bootState": "booted"}, "connectionProperties": {}},
    ]}}

    def fake_run(cmd, timeout=0):
        out = cmd[cmd.index("--json-output") + 1]
        with open(out, "w") as f:
            json.dump(raw, f)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(cs, "_run", fake_run)
    devices = cs.list_devices()
    assert [(d["id"], d["transport"]) for d in devices] == [("A", "wired"), ("D", "simulator")]


def test_api_cable_rejects_unknown_device(monkeypatch):
    from werkzeug.test import Client
    import spark
    monkeypatch.setattr(cs, "list_devices", lambda: [])
    r = Client(spark.application).post("/api/cable", json={"device": "nope"})
    assert r.status_code == 400 and "没连着" in r.get_json()["error"]
    r = Client(spark.application).get("/api/cable")
    assert r.status_code == 200 and r.get_json()["devices"] == []
