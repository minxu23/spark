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
    assert [(d["id"], d["transport"]) for d in devices] == [("A", "wired"), ("B", "wired"), ("D", "simulator")]


def test_api_cable_rejects_unknown_device(monkeypatch):
    from werkzeug.test import Client
    import spark
    monkeypatch.setattr(cs, "list_devices", lambda: [])
    r = Client(spark.application).post("/api/cable", json={"device": "nope"})
    assert r.status_code == 400 and "没连着" in r.get_json()["error"]
    r = Client(spark.application).get("/api/cable")
    assert r.status_code == 200 and r.get_json()["devices"] == []


def test_save_tasks_stored_before_push_and_readlog_shipped(env, monkeypatch, tmp_path):
    v, tr, phone, outbox = env
    log = tmp_path / "reading-log.jsonl"
    log.write_text('{"id": "e1", "at": 1, "source": "desktop", "path": "output/报告.md"}\n', encoding="utf-8")
    monkeypatch.setattr(cs.readlog, "log_path", lambda: str(log))
    _write(str(outbox / "s1.json"), json.dumps({"id": "s1", "title": "存进笔记库：阅读复盘", "task": {
        "type": "save", "path": "output/复盘/复盘_阅读_手机.md", "content": "# 阅读复盘\n"}}, ensure_ascii=False))
    r = cs.sync_device(_device(), transport=tr)
    assert r["saved"] == 1
    assert (v / "output" / "复盘" / "复盘_阅读_手机.md").read_text(encoding="utf-8") == "# 阅读复盘\n"
    pushed = phone / cs.DEST
    # 同一趟就带回手机，清单里也有
    assert (pushed / "output" / "复盘" / "复盘_阅读_手机.md").exists()
    manifest = json.loads((pushed / cs.MANIFEST).read_text())
    assert "output/复盘/复盘_阅读_手机.md" in manifest["files"]
    assert json.loads((pushed / cs.RESULTS).read_text())["tasks"]["s1"]["state"] == "done"
    assert "e1" in (pushed / cs.READLOG_COPY).read_text(encoding="utf-8")


class GroupTransport(FakeTransport):
    """App Group 容器里的 readlog/ 也能拉"""
    def __init__(self, phone, outbox, group):
        super().__init__(phone, outbox)
        self.group = group

    def pull_group_dir(self, name, local_dir):
        src = os.path.join(self.group, name)
        if os.path.isdir(src):
            shutil.copytree(src, local_dir, dirs_exist_ok=True)


def test_reading_prefs_relayed_newest_wins(env, tmp_path):
    v, tr, phone, outbox = env
    group = tmp_path / "group"
    tr = GroupTransport(tr.phone, tr.outbox, str(group))
    prefs = group / cs.READLOG / cs.PREFS
    _write(str(prefs), json.dumps({"prefs": {"font": "songti", "size": 19}, "at": 2000}))
    cs.sync_device(_device(), transport=tr)
    pushed = phone / cs.DEST / cs.PREFS_COPY
    assert json.loads(pushed.read_text())["prefs"]["font"] == "songti"

    # 另一台设备上的是更早改的：不覆盖
    _write(str(prefs), json.dumps({"prefs": {"font": "wenkai"}, "at": 1000}))
    cs.sync_device(_device(), transport=tr)
    assert json.loads(pushed.read_text()) == {"prefs": {"font": "songti", "size": 19}, "at": 2000}

    # 格式不对的不收
    _write(str(prefs), "{not json")
    cs.sync_device(_device(), transport=tr)
    assert json.loads(pushed.read_text())["at"] == 2000

    _write(str(prefs), json.dumps({"prefs": {"font": "kaiti"}, "at": 3000}))
    cs.sync_device(_device(), transport=tr)
    assert json.loads(pushed.read_text())["prefs"] == {"font": "kaiti"}


def test_delete_task_done_during_sync(env, monkeypatch, tmp_path):
    v, tr, phone, outbox = env
    monkeypatch.setattr(cs.remote_tasks, "trash_dir", lambda: str(tmp_path / "Trash"))
    _write(str(v / "output" / "复盘" / "复盘_阅读_旧.md"), "# 旧\n")
    _write(str(outbox / "d1.json"), json.dumps({"id": "d1", "title": "删掉复盘",
                                                 "task": {"type": "delete", "path": "output/复盘/复盘_阅读_旧.md"}}))
    r = cs.sync_device(_device(), transport=tr)
    assert r["saved"] == 1
    assert not (v / "output" / "复盘" / "复盘_阅读_旧.md").exists()
    assert (tmp_path / "Trash" / "复盘_阅读_旧.md").exists()
    manifest = json.loads((phone / cs.DEST / cs.MANIFEST).read_text())
    assert "output/复盘/复盘_阅读_旧.md" not in manifest["files"]


def test_auto_sync_on_plug_in_and_every_ten_minutes(monkeypatch):
    """数据线插上马上同步；一直插着隔 10 分钟再同步；拔了再插马上同步；Wi-Fi 和模拟器不自动。"""
    started = []
    monkeypatch.setattr(cs, "start", lambda dev_id, device=None, auto=False: started.append((dev_id, auto)) or "")
    monkeypatch.setitem(cs._AUTO, "last", {})
    phone = {"id": "p", "name": "iPhone", "transport": "wired"}
    others = [{"id": "w", "name": "iPad", "transport": "localNetwork"},
              {"id": "s", "name": "模拟器", "transport": "simulator"}]

    assert cs.auto_tick([phone] + others, now=1000) == ["iPhone"]
    assert cs.auto_tick([phone] + others, now=1000 + 60) == []
    assert cs.auto_tick([phone], now=1000 + cs.AUTO_RESYNC_SECONDS) == ["iPhone"]
    assert cs.auto_tick(others, now=2000) == []               # 拔了
    assert cs.auto_tick([phone], now=2001) == ["iPhone"]      # 再插上：不等 10 分钟
    assert started == [("p", True)] * 3


def test_auto_sync_retries_when_busy(monkeypatch):
    """正在同步别的就不算数，下一轮再试。"""
    answers = ["正在同步，等这一次结束", ""]
    monkeypatch.setattr(cs, "start", lambda dev_id, device=None, auto=False: answers.pop(0))
    monkeypatch.setitem(cs._AUTO, "last", {})
    phone = {"id": "p", "name": "iPhone", "transport": "wired"}
    assert cs.auto_tick([phone], now=1000) == []
    assert cs.auto_tick([phone], now=1020) == ["iPhone"]


def test_auto_sync_survives_device_listing_error(monkeypatch):
    def boom():
        raise cs.CableError("找不到 xcrun")
    monkeypatch.setattr(cs, "list_devices", boom)
    assert cs.auto_tick() == []


def test_progress_pushed_to_phone_at_most_every_half_minute(env, monkeypatch):
    """跑任务时进度写进记录，隔半分钟推一次给手机；进度一句话里有第几条和在做哪一条。"""
    book = cs._book()
    book.tasks["t1"] = {"title": "处理链接", "state": "running", "text": "", "updated_at": 0}
    pushes = []
    clock = [1000.0]
    monkeypatch.setattr(cs.time, "time", lambda: clock[0])
    monkeypatch.setattr(cs.threading, "Thread", lambda target, **kw: type("T", (), {"start": lambda self: target()})())
    pr = cs._LocalProgress(book, "t1", lambda: pushes.append(dict(book.tasks["t1"])))
    st = {"stage": "subtitle", "current": 2, "total": 18, "log": ["[2/18] 下载字幕：第二集"]}
    pr.update(cs.remote_tasks._progress_text("处理", st), current=2, total=18)
    clock[0] += 10
    pr.update("处理第 3/18 条：正在写小结：第三集", current=3, total=18)
    clock[0] += 30
    pr.update("处理第 4/18 条：下载字幕：第四集", current=4, total=18)
    assert [p["text"] for p in pushes] == ["处理第 2/18 条：下载字幕：第二集", "处理第 4/18 条：下载字幕：第四集"]
    assert pushes[-1]["current"] == 4 and pushes[-1]["total"] == 18
    monkeypatch.setitem(cs._STATE, "worker", True)
    s = cs.status()
    assert s["tasks_now"][0]["text"].startswith("处理第 4/18 条") and s["tasks_left"] == 0


def test_progress_parts_for_the_card():
    """卡片上分开显示这一步和这一集；列清单那行不当成某一集。"""
    parts = cs.remote_tasks._progress_parts
    assert parts({"stage": "subtitle", "log": ["[2/18] 下载字幕：第二集"]}) == ("下载字幕", "第二集")
    assert parts({"stage": "speech", "log": ["  正在整理演讲稿：第三集", " "]}) == ("整理演讲稿", "第三集")
    assert parts({"stage": "skip", "log": ["[4/18] ⏭️ 已生成过，跳过：第四集"]}) == ("已生成过，跳过", "第四集")
    assert parts({"stage": "overview", "log": ["本次共选中 18 个议题：甲，乙"]}) == ("列出要处理的", "")
    assert parts({"stage": "summary"}) == ("写小结", "")


def test_runner_reports_subject_and_step(env):
    """Runner 把节目名、这一步、这一集一起交给进度，带回手机的结果里都有。"""
    seen = []

    class P:
        def update(self, text, *, force=False, **extra):
            seen.append(extra)

    calls = iter([(200, {"done": False, "stage": "subtitle", "current": 2, "total": 18, "log": ["[2/18] 下载字幕：第二集"]}),
                  (200, {"done": True, "ok": True})])
    r = cs.remote_tasks.Runner(lambda m, p, b: next(calls), P(), sleep=lambda s: None)
    r.subject = "某节目"
    r._wait("/x", "处理")
    assert seen == [{"stage": "subtitle", "current": 2, "total": 18, "step": "下载字幕", "item": "第二集", "subject": "某节目"}]
    book = cs._book()
    book.tasks["t1"] = {"title": "处理链接", "state": "running", "text": "", "updated_at": 0}
    cs._LocalProgress(book, "t1").update("x", **seen[0])
    r = book.results()["tasks"]["t1"]
    assert (r["step"], r["item"], r["subject"], r["current"]) == ("下载字幕", "第二集", "某节目", 2)
    assert "stage" not in r


def test_task_left_running_by_a_restart_is_requeued(env):
    """Spark 重启后不留「一直在跑」的任务：重新排队。"""
    book = cs._book()
    book.tasks["t1"] = {"title": "处理链接", "state": "running", "text": "处理第 3/18 条", "updated_at": 0}
    book.save()
    again = cs.TaskBook()
    assert again.tasks["t1"]["state"] == "waiting" and again.waiting() == ["t1"]
