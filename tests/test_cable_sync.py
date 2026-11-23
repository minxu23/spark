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
from core import taihe_sync as cs_taihe


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
    """默认不列模拟器；开发开关 SPARK_CABLE_SIMULATORS=1 打开后，开着的 iOS 模拟器排在真机后面。"""
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
    monkeypatch.setattr(cs_taihe, "list_devices", lambda: [])
    monkeypatch.delenv(cs.SIMULATORS_ENV, raising=False)
    devices = cs.list_devices()
    assert [(d["id"], d["transport"]) for d in devices] == [("A", "wired"), ("B", "wired")]
    monkeypatch.setenv(cs.SIMULATORS_ENV, "0")
    assert [d["id"] for d in cs.list_devices()] == ["A", "B"]
    monkeypatch.setenv(cs.SIMULATORS_ENV, "1")
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
    monkeypatch.setattr(cs, "start", lambda dev_id, device=None, auto=False, quick=False:
                        "" if quick else started.append((dev_id, auto)) or "")
    monkeypatch.setitem(cs._AUTO, "last", {})
    monkeypatch.setitem(cs._AUTO, "checked", {})
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
    monkeypatch.setattr(cs, "start", lambda dev_id, device=None, auto=False, quick=False: answers.pop(0))
    monkeypatch.setitem(cs._AUTO, "last", {})
    monkeypatch.setitem(cs._AUTO, "checked", {})
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


def _track_task(eid="0123456789abcdef", name="20261103_一篇文章.md"):
    return {"type": "track_item", "folder": "Spark/信息跟进/AI Insider", "name": name, "content": "# 一篇文章\n\n## 小结\n",
            "entry": {"id": eid, "title": "一篇文章", "url": "https://a.example/1", "publish_date": "20261103"},
            "summary": {"tldr": "一句话", "body": "- 要点"}}


def test_phone_track_item_saved_once_and_recorded(env):
    """手机写好的信息跟进：同步时存进订阅文件夹、记进 manifest，再收到一次不重复。"""
    from apps.summit2md import pipeline
    v = cs.vault.vault_root()
    t = cs.remote_tasks.parse_task(_track_task())
    assert cs.remote_tasks.save_track_item(t).startswith("存进了笔记库")
    folder = os.path.join(v, "Spark", "信息跟进", "AI Insider")
    row = pipeline._load_manifest(folder)["entries"]["0123456789abcdef"]
    assert row["ok"] and row["relative_path"] == "20261103_一篇文章.md" and row["summary"]["tldr"] == "一句话"
    assert row["entry"]["source_type"] == "rss" and row["rank"] == 1
    assert os.path.exists(os.path.join(folder, row["relative_path"]))
    assert cs.remote_tasks.save_track_item(t).startswith("已经有了")
    assert os.listdir(folder).count("20261103_一篇文章_2.md") == 0


@pytest.mark.parametrize("bad", [
    {"folder": "Spark/别处/x"}, {"folder": "Spark/信息跟进/../x"}, {"folder": "Spark/信息跟进/a/b"},
    {"folder": "Spark/信息跟进/.cache"}, {"name": "../x.md"}, {"name": "a/b.md"}, {"name": "x.txt"},
    {"content": ""}, {"entry": {"id": "../../x"}}, {"entry": {"id": "0123456789abcdef", "publish_date": "昨天"}},
])
def test_phone_track_item_rejects_bad_paths(bad):
    with pytest.raises(cs.remote_tasks.TaskError):
        cs.remote_tasks.parse_task(dict(_track_task(), **bad))


def test_subscriptions_shipped_with_relative_folders(tmp_path, monkeypatch):
    from apps.summit2md import subscriptions_store as store
    root = tmp_path / "vault"
    root.mkdir()
    monkeypatch.setattr(store, "list_all", lambda kind=None: [
        {"id": "a", "name": "内", "url": "https://a/feed", "kind": "track", "source_type": "rss", "category": "c",
         "folder": str(root / "Spark" / "信息跟进" / "内"), "ignored_ids": ["x"], "output_dir": "/私有"},
        {"id": "b", "name": "外", "url": "https://b", "kind": "track", "source_type": "rss", "folder": "/别的地方/外"}])
    got = cs.export_subscriptions(str(root))
    assert got[0] == {"id": "a", "name": "内", "url": "https://a/feed", "kind": "track", "source_type": "rss",
                      "category": "c", "skip_shorts": False, "folder": "Spark/信息跟进/内", "ignored_ids": ["x"]}
    assert got[1]["folder"] == ""


def _episode_task(**over):
    t = {"type": "episode_item", "folder": "Spark/某节目",
         "entry": {"id": "abc_DEF-123", "title": "第一期：芯片", "url": "https://www.youtube.com/watch?v=abc_DEF-123",
                   "duration": 3725, "publish_date": "20261103", "sub_kind": "auto"},
         "summary": {"tldr": "一句话", "body": "### 本期要点\n- 要点 [1:05]", "guests": ["甲（主持人）", "乙"], "topics": ["芯片"]},
         "paragraphs": [[0, "大家好"], [65, "今天聊芯片"]], "lang": "zh"}
    t.update(over)
    return t


def test_phone_episode_saved_with_computer_templates(env):
    """手机写好的一期节目：文字记录和笔记都由电脑的模板写，记进 manifest，再收到一次不重复。"""
    from apps.summit2md import pipeline
    t = cs.remote_tasks.parse_task(_episode_task())
    assert cs.remote_tasks.save_episode_item(t).startswith("存进了笔记库")
    folder = os.path.join(cs.vault.vault_root(), "Spark", "某节目")
    row = pipeline._load_manifest(folder)["entries"]["abc_DEF-123"]
    assert row["ok"] and row["relative_path"] == "transcripts/20261103_第一期：芯片.md" and row["speech_relative_path"] is None
    assert row["note_relative_path"] == "notes/20261103 第一期：芯片.md" and row["summary"]["guests"] == ["甲（主持人）", "乙"]
    with open(os.path.join(folder, row["relative_path"]), encoding="utf-8") as f:
        text = f.read()
    assert "所属节目：某节目" in text and "YouTube 自动生成字幕（zh）" in text and "t=65s" in text and "今天聊芯片" in text
    with open(os.path.join(folder, row["note_relative_path"]), encoding="utf-8") as f:
        note = f.read()
    assert '话题: ["芯片"]' in note and "> 一句话" in note and "&t=65s" in note and "完整文字记录" in note
    assert cs.remote_tasks.save_episode_item(t).startswith("已经有了")


def test_phone_short_video_saved_as_flash_with_script_only(env):
    """手机写好的简讯（频道里的短视频）：只有整理稿、没有小结，记进 manifest 标着 short，不写单集笔记。"""
    from apps.summit2md import pipeline
    entry = dict(_episode_task()["entry"], duration=62, short=True)
    with pytest.raises(cs.remote_tasks.TaskError, match="整理稿"):
        cs.remote_tasks.parse_task(_episode_task(entry=entry, summary=None))
    t = cs.remote_tasks.parse_task(_episode_task(entry=entry, summary=None, speech={"text": "大家好，今天聊芯片。", "mode": "zh"}))
    assert t["summary"] is None and t["entry"]["short"] is True
    cs.remote_tasks.save_episode_item(t)
    folder = os.path.join(cs.vault.vault_root(), "Spark", "某节目")
    row = pipeline._load_manifest(folder)["entries"]["abc_DEF-123"]
    assert row["ok"] and row["entry"]["short"] and row["summary"] is None and row["speech_relative_path"]
    assert not row.get("note_relative_path") and not os.path.isdir(os.path.join(folder, "notes"))
    # 不是简讯的照样要小结
    with pytest.raises(cs.remote_tasks.TaskError):
        cs.remote_tasks.parse_task(_episode_task(summary=None, speech={"text": "x", "mode": "zh"}))


@pytest.mark.parametrize("bad", [
    {"folder": "Spark/信息跟进"}, {"folder": "Spark/剪藏"}, {"folder": "Spark/../x"}, {"folder": "Spark/a/b"}, {"folder": "output/x"},
    {"paragraphs": []}, {"paragraphs": [["x", "y"]]},
    {"entry": {"id": "../x", "publish_date": "20261103"}}, {"entry": {"id": "ok", "publish_date": ""}},
    {"entry": {"id": "ok", "publish_date": "20261103", "source_type": "sitemap"}},
])
def test_phone_episode_rejects_bad_input(bad):
    with pytest.raises(cs.remote_tasks.TaskError):
        cs.remote_tasks.parse_task(_episode_task(**bad))


SPEECH_NAME = "20261103_第一期：芯片.md"
NOTE_PATH = ("notes", "20261103 第一期：芯片.md")


def _show_folder():
    return os.path.join(cs.vault.vault_root(), "Spark", "某节目")


def _read(*parts):
    with open(os.path.join(_show_folder(), *parts), encoding="utf-8") as f:
        return f.read()


def test_手机带来的整理稿用电脑模板存下来(env):
    from apps.summit2md import pipeline
    speech = {"text": "大家好，今天聊芯片。", "mode": "zh"}
    assert cs.remote_tasks.save_episode_item(cs.remote_tasks.parse_task(_episode_task(speech=speech))).startswith("存进了笔记库")
    text = _read("speech", SPEECH_NAME)
    assert "## 演讲稿" in text and "大家好，今天聊芯片。" in text and "中文整理" in text and "自动字幕" in text
    assert f"[整理稿](<../speech/{SPEECH_NAME}>)" in _read(*NOTE_PATH)
    row = pipeline._load_manifest(_show_folder())["entries"]["abc_DEF-123"]
    assert row["speech_relative_path"] == f"speech/{SPEECH_NAME}" and row["speech_lang_mode"] == "zh"


def test_已处理但没整理稿时补上(env):
    cs.remote_tasks.save_episode_item(cs.remote_tasks.parse_task(_episode_task()))
    assert not os.path.exists(os.path.join(_show_folder(), "speech"))
    with_speech = cs.remote_tasks.parse_task(_episode_task(speech={"text": "正文", "mode": "original"}))
    assert cs.remote_tasks.save_episode_item(with_speech).startswith("补上了整理稿")
    assert "正文" in _read("speech", SPEECH_NAME) and "[整理稿]" in _read(*NOTE_PATH)
    assert cs.remote_tasks.save_episode_item(with_speech).startswith("已经有了")


@pytest.mark.parametrize("speech", [{"text": "  ", "mode": "zh"}, {"text": "x", "mode": "别的"}, "x", {"mode": "zh"}])
def test_整理稿格式不对时不收(speech):
    with pytest.raises(cs.remote_tasks.TaskError):
        cs.remote_tasks.parse_task(_episode_task(speech=speech))


def test_plugged_phone_checked_every_minute_between_full_syncs(monkeypatch):
    """一直插着：每分钟只看一眼发件箱，10 分钟整个同步一次；看一眼时不算整个同步。"""
    calls = []
    monkeypatch.setattr(cs, "start", lambda dev_id, device=None, auto=False, quick=False:
                        calls.append("quick" if quick else "full") or "")
    monkeypatch.setitem(cs._AUTO, "last", {})
    monkeypatch.setitem(cs._AUTO, "checked", {})
    phone = {"id": "p", "name": "iPhone", "transport": "wired"}
    for t in (0, 20, 40, 60, 80, 120, cs.AUTO_RESYNC_SECONDS):
        cs.auto_tick([phone], now=1000 + t)
    assert calls == ["full", "quick", "quick", "full"]


def test_quick_check_saves_phone_content_without_copying_vault(env, monkeypatch):
    v, tr, phone, outbox = env
    _write(str(outbox / "s1.json"), json.dumps({"id": "s1", "title": "存进笔记库：剪藏", "task": {
        "type": "save", "path": "output/复盘/手机复盘.md", "content": "# 手机上写的\n"}}, ensure_ascii=False))
    r = cs.check_device(_device(), transport=tr)
    assert r["quick"] and r["tasks_received"] == 1 and r["saved"] == 1 and r["device_id"] == "dev-1"
    assert (v / "output" / "复盘" / "手机复盘.md").read_text(encoding="utf-8") == "# 手机上写的\n"
    dest = phone / cs.DEST
    assert sorted(os.listdir(dest)) == [cs.RESULTS], "看一眼手机不往手机拷笔记库，只推任务结果（手机发件箱那件能马上清掉）"
    results = json.loads((dest / cs.RESULTS).read_text(encoding="utf-8"))
    assert results["tasks"]["s1"]["state"] == "done"
    assert results["tasks"]["s1"]["applied_sha"] == cs.blob_sha(str(v / "output" / "复盘" / "手机复盘.md"))


def test_quick_check_status_does_not_replace_last_full_sync(env, monkeypatch):
    monkeypatch.setattr(cs, "check_device", lambda device: {"ok": True, "device": "测试 iPhone", "quick": True})
    monkeypatch.setitem(cs._STATE, "last", {"ok": True, "device": "测试 iPhone", "files": 3, "finished_at": 1})
    import threading
    done = threading.Event()
    assert cs.start("dev-1", device=_device(), quick=True) == ""
    for _ in range(200):
        if not cs.status()["running"]:
            break
        done.wait(0.01)
    st = cs.status()
    assert st["last"]["files"] == 3 and st["checked"]["quick"] and not st["quick"]


def test_任务做完马上整个同步_写出来的文件送到手机(env, monkeypatch):
    """手机交给电脑的复盘、报告：只推任务结果的话，手机上「完成」了却看不到文件。做完要整个同步一次；没做成的不用。"""
    import time
    _v, tr, _phone, outbox = env
    _write(str(outbox / "r1.json"), json.dumps({"id": "r1", "task": {"type": "review", "kind": "topic", "topic": "睡眠"}}))
    _write(str(outbox / "r2.json"), json.dumps({"id": "r2", "task": {"type": "review", "kind": "topic", "topic": "失败的"}}))

    class FakeRunner:
        def __init__(self, call, progress):
            pass

        def run(self, task):
            if task["topic"] == "失败的":
                raise cs.remote_tasks.TaskError("材料不够")
            return "复盘写好了"

    synced = []
    monkeypatch.setitem(cs._AUTO, "last", {})
    monkeypatch.setattr(cs.remote_tasks, "Runner", FakeRunner)
    monkeypatch.setattr(cs, "Transport", lambda device: tr)
    monkeypatch.setitem(cs._CALL, "fn", lambda *a: (200, {}))
    monkeypatch.setattr(cs, "start", lambda dev_id, **kw: synced.append(dev_id) or "")
    cs.sync_device(_device(), transport=tr)
    for _ in range(200):
        if synced:
            break
        time.sleep(0.02)
    assert synced == ["dev-1"]          # 两个任务做完只同步一次
    assert cs._AUTO["last"]["dev-1"] > 0


def test_任务做完的同步_正在同步时等一会儿再试_手机断开就不试(env, monkeypatch):
    calls = []
    answers = iter(["正在同步，等这一次结束", ""])
    monkeypatch.setattr(cs, "SYNC_AFTER_TASKS_WAIT", 0)
    monkeypatch.setattr(cs, "start", lambda dev_id, **kw: calls.append(dev_id) or next(answers))
    cs._sync_after_tasks(_device())
    assert calls == ["dev-1", "dev-1"]
    calls.clear()
    monkeypatch.setattr(cs, "start", lambda dev_id, **kw: calls.append(dev_id) or "这台设备现在没连着")
    cs._sync_after_tasks(_device())
    assert calls == ["dev-1"]           # 手机断开了：不再试


def test_manifest_pushed_last_alone(env):
    """清单最后单独推一次：手机开着时会隔一会儿看清单，看到新清单时这一批文件已经全拷进去了。"""
    v, tr, phone, outbox = env
    seen = []
    real = tr.push

    def push(local_dir, dest):
        names = {os.path.relpath(os.path.join(d, f), local_dir) for d, _s, fs in os.walk(local_dir) for f in fs}
        seen.append(names)
        real(local_dir, dest)
    tr.push = push
    cs.sync_device(_device(), transport=tr)
    cs.sync_device(_device(), transport=tr)   # 第二次：staging 里不留上一次的清单
    for first, last in (seen[0:2], seen[2:4]):
        assert cs.MANIFEST not in first and "output/报告.md" in first
        assert last == {cs.MANIFEST}
    assert json.loads((phone / cs.DEST / cs.MANIFEST).read_text())["files"]


def test_manifest_push_failure_fails_sync_and_next_sync_repushes(env):
    """清单那次单独推送失败：这次同步算失败（CableError）；下一次同步重新打包、重推清单。"""
    v, tr, phone, outbox = env
    real = tr.push
    calls = []

    def push(local_dir, dest):
        only_manifest = os.listdir(local_dir) == [cs.MANIFEST]
        calls.append(only_manifest)
        if only_manifest and calls.count(True) == 1:
            raise cs.CableError("拷到手机失败：断开了")
        real(local_dir, dest)
    tr.push = push
    with pytest.raises(cs.CableError):
        cs.sync_device(_device(), transport=tr)
    assert not (phone / cs.DEST / cs.MANIFEST).exists()   # 手机上看不到新清单，不会照它删文件
    _write(str(v / "output" / "新报告.md"), "新")
    cs.sync_device(_device(), transport=tr)
    manifest = json.loads((phone / cs.DEST / cs.MANIFEST).read_text())
    assert "output/新报告.md" in manifest["files"] and calls.count(True) == 2


def _swift_func(src: str, start: str) -> str:
    i = src.index(start)
    j = src.index("{", i)   # 签名里没有花括号：第一个 { 就是函数体
    depth = 0
    for k in range(j, len(src)):
        depth += {"{": 1, "}": -1}.get(src[k], 0)
        if depth == 0:
            return src[i:k + 1]
    raise ValueError(start)


@pytest.mark.skipif(not shutil.which("swiftc"), reason="没有 swiftc")
def test_phone_ignores_same_manifest_swift(tmp_path):
    """手机：电脑推完文件、清单还没到（手机上还是上一份清单）时，importCable 什么都不改（Vault.newCableManifest 给 nil）。"""
    import subprocess
    vault_swift = os.path.join(os.path.dirname(__file__), "..", "apps", "mobile-ios", "Spark", "Vault.swift")
    with open(vault_swift, encoding="utf-8") as f:
        fn = _swift_func(f.read(), "nonisolated static func newCableManifest(")
    main = tmp_path / "main.swift"
    main.write_text("import Foundation\nenum V {\n" + fn + '''
}
func d(_ s: String) -> Data { s.data(using: .utf8)! }
let cases: [(Data?, String?)] = [
    (d(#"{"generated_at": "A", "files": {"x.md": ["sha", 1]}}"#), "A"),
    (d(#"{"generated_at": "B", "files": {"x.md": ["sha", 1]}}"#), "A"),
    (d(#"{"generated_at": "B", "files": {}}"#), nil),
    (nil, "A"), (d("{坏"), "A"), (d(#"{"generated_at": "B"}"#), "A")]
for (data, last) in cases {
    if let r = V.newCableManifest(data, lastCable: last) { print("\\(r.generated) \\(r.files.count) \\(r.deleted.count)") } else { print("nil") }
}
// 带墓碑的清单
let withTomb = V.newCableManifest(d(#"{"generated_at": "C", "files": {}, "deleted": {"Spark/笔记/a.md": ["h1", "h2"], "bad": 3}}"#), lastCable: "B")!
print(withTomb.deleted["Spark/笔记/a.md"]!.joined(separator: ","), withTomb.deleted.count)
''', encoding="utf-8")
    exe = tmp_path / "probe"
    r = subprocess.run(["swiftc", "-o", str(exe), str(main)], capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr
    out = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30).stdout.split("\n")[:7]
    assert out == ["nil", "B 1 0", "B 0 0", "nil", "nil", "nil", "h1,h2 1"]   # 旧电脑没有 deleted：空的


def test_manifest_carries_tombstones(env):
    """清单里的 deleted 来自墓碑（SHA-256）；放回原处清掉墓碑后不再出现；文件又在清单里的不放。"""
    import hashlib
    from core import clips
    v, tr, phone, outbox = env
    root = str(v)
    gone = "Spark/笔记/20261123_删掉的.md"
    clips.record_deleted(root, gone, {hashlib.sha256("内容".encode()).hexdigest()}, {"del-key-0001"})
    clips.record_deleted(root, "output/报告.md", {hashlib.sha256("报告".encode()).hexdigest()})   # 文件还在：不放
    cs.sync_device(_device(), transport=tr)
    m = json.loads((phone / cs.DEST / cs.MANIFEST).read_text())
    assert m["deleted"] == {gone: [hashlib.sha256("内容".encode()).hexdigest()]}
    clips.clear_deleted(root, gone)
    cs.sync_device(_device(), transport=tr)
    assert json.loads((phone / cs.DEST / cs.MANIFEST).read_text())["deleted"] == {}



@pytest.mark.skipif(not shutil.which("swiftc"), reason="没有 swiftc")
def test_elsewhere_deleted_swift(tmp_path):
    """手机：在别的设备上删掉的新建文档（ElsewhereDeleted.swift 单独编译）。"""
    src = os.path.join(os.path.dirname(__file__), "..", "apps", "mobile-ios", "Spark", "ElsewhereDeleted.swift")
    main = tmp_path / "main.swift"
    main.write_text(r"""
func show(_ d: ElsewhereDeleted.Decision) { print("trash=" + d.trash.joined(separator: ",") + " stranded=" + d.stranded.joined(separator: ",")) }
let blob: (String) -> String = { "b:" + $0 }
// 任务 done 以后：手机上这份就是交上去的、或者是电脑写进去的那版，才放手；之后又改过的不放
print(ElsewhereDeleted.released(disk: "x", submitted: "x", appliedBlob: nil, blobOf: blob))
print(ElsewhereDeleted.released(disk: "y", submitted: "x", appliedBlob: "b:y", blobOf: blob))
print(ElsewhereDeleted.released(disk: "改过", submitted: "x", appliedBlob: "b:x", blobOf: blob))
print(ElsewhereDeleted.released(disk: nil, submitted: "x", appliedBlob: nil, blobOf: blob))
let tomb = ["Spark/笔记/a.md": ["h1"], "Spark/笔记/b.md": ["h9"], "Spark/笔记/c.md": ["h3"], "Spark/笔记/e.md": ["h5"]]
// a 指纹相同 → 废纸篓；b 不同 → 留着、标出来；c 有待确认记录或发件箱任务 → 不动；d 电脑没墓碑 → 不动；e 读不出来 → 留着、标出来
show(ElsewhereDeleted.decide(onlyPhone: ["Spark/笔记/a.md": "h1", "Spark/笔记/b.md": "h2", "Spark/笔记/c.md": "h3",
                                         "Spark/笔记/d.md": "h4", "Spark/笔记/e.md": nil],
                             deleted: tomb, busy: ["Spark/笔记/c.md"]))
// 旧电脑：清单里没有 deleted
show(ElsewhereDeleted.decide(onlyPhone: ["Spark/笔记/a.md": "h1"], deleted: [:], busy: []))
// 文件树里没有这篇：数据线清单 + 数据线任务结果才放手；GitHub 文件树或 GitHub 任务都不放手；文件树里有就照内容判断
print(ElsewhereDeleted.released(disk: "x", submitted: "x", appliedBlob: nil, blobOf: blob, treeHas: false, cableTree: false, cableTask: true))
print(ElsewhereDeleted.released(disk: "x", submitted: "x", appliedBlob: nil, blobOf: blob, treeHas: false, cableTree: true, cableTask: false))
print(ElsewhereDeleted.released(disk: "x", submitted: "x", appliedBlob: nil, blobOf: blob, treeHas: false, cableTree: true, cableTask: true))
print(ElsewhereDeleted.released(disk: "x", submitted: "x", appliedBlob: nil, blobOf: blob, treeHas: true, cableTree: false, cableTask: false))
print(ElsewhereDeleted.released(disk: "改过", submitted: "x", appliedBlob: nil, blobOf: blob, treeHas: false, cableTree: true, cableTask: true))
// 彻底删除时交不交 purge_images：在别的设备上删掉的不交，电脑没删成的不交
print(ElsewhereDeleted.sendsPurge(deleteFailed: false, elsewhere: false), ElsewhereDeleted.sendsPurge(deleteFailed: false, elsewhere: true),
      ElsewhereDeleted.sendsPurge(deleteFailed: true, elsewhere: false))
// 废纸篓里已经有同一份（路径、内容指纹相同、在别的设备上删掉的）：不再加
let items: [(path: String, base: String?, elsewhere: Bool)] = [("Spark/笔记/a.md", "h1", true), ("Spark/笔记/b.md", "h2", false)]
print(ElsewhereDeleted.alreadyTrashed(items, path: "Spark/笔记/a.md", base: "h1"),
      ElsewhereDeleted.alreadyTrashed(items, path: "Spark/笔记/a.md", base: "h9"),
      ElsewhereDeleted.alreadyTrashed(items, path: "Spark/笔记/b.md", base: "h2"))
""", encoding="utf-8")
    exe = tmp_path / "probe"
    r = subprocess.run(["swiftc", "-o", str(exe), src, str(main)], capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr
    out = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30).stdout.split("\n")
    assert out[:13] == ["true", "true", "false", "false",
                        "trash=Spark/笔记/a.md stranded=Spark/笔记/b.md,Spark/笔记/e.md", "trash= stranded=",
                        "false", "false", "true", "true", "false",
                        "true false false", "true false false"]


# ---------------------------------------------------------------- 模拟器默认不认（2026-11-23 模拟器测试数据混进笔记库）

class RecordingTransport(GroupTransport):
    """记下每一次推、拉：被拒绝的同步不能碰手机，也不能碰电脑这边。"""
    def __init__(self, phone, outbox, group):
        super().__init__(phone, outbox, group)
        self.calls = []

    def push(self, local_dir, dest):
        self.calls.append(("push", dest))
        super().push(local_dir, dest)

    def pull_outbox(self, local_dir):
        self.calls.append(("pull_outbox",))
        super().pull_outbox(local_dir)

    def pull_group_dir(self, name, local_dir):
        self.calls.append(("pull_group_dir", name))
        super().pull_group_dir(name, local_dir)


def _sim():
    return {"id": "SIM-1", "udid": "SIM-1", "name": "iPhone 17 模拟器", "model": "iPhone 17", "transport": "simulator"}


def _harmony_sim():
    return {"id": "taihe:127.0.0.1:5555", "udid": "127.0.0.1:5555", "platform": "HarmonyOS", "name": "太和剪藏",
            "model": "鸿蒙手机", "transport": "simulator"}


@pytest.fixture
def sim_env(env, tmp_path, monkeypatch):
    """模拟器发件箱里有一条剪藏、阅读记录里有一条：被拒绝时一条都不能进电脑。"""
    v, tr, phone, outbox = env
    group = tmp_path / "group"
    tr = RecordingTransport(tr.phone, tr.outbox, str(group))
    _write(str(outbox / "s1.json"), json.dumps({"id": "s1", "title": "测试剪藏", "task": {
        "type": "save", "path": "output/模拟器测试.md", "content": "# 测试\n"}}, ensure_ascii=False))
    _write(str(group / cs.READLOG / "r.jsonl"), '{"id": "sim-e1", "at": 1795000000, "source": "phone", "path": "output/报告.md"}\n')
    merged = []
    real_merge = cs.readlog.merge
    monkeypatch.setattr(cs.readlog, "merge", lambda events: merged.extend(events) or real_merge(events))
    monkeypatch.setattr(cs.readlog, "log_path", lambda: str(tmp_path / "reading-log.jsonl"))
    return v, tr, phone, merged


def _assert_untouched(v, tr, phone, merged):
    assert tr.calls == []                                         # 没拉、没推
    assert not phone.exists()                                     # 手机那边什么都没写
    assert not (v / "output" / "模拟器测试.md").exists()           # 剪藏没进笔记库
    assert merged == []                                           # 阅读记录没并
    home = cs._spark_home()
    for name in ("cable-staging", "cable-sha-cache.json", "cable-tasks.json", "device-prefs.json"):
        assert not os.path.exists(os.path.join(home, name)), name


def test_simulator_refused_by_default(sim_env, monkeypatch):
    """设备信息由调用方直接传进来（不经过 list_devices）：sync_device、check_device 都拒绝，什么都不碰。"""
    monkeypatch.delenv(cs.SIMULATORS_ENV, raising=False)
    v, tr, phone, merged = sim_env
    for fn in (cs.sync_device, cs.check_device):
        with pytest.raises(cs.CableError) as e:
            fn(_sim(), transport=tr)
        assert str(e.value) == cs.SIMULATOR_REFUSED and cs.SIMULATORS_ENV in str(e.value)
    _assert_untouched(v, tr, phone, merged)


def test_harmony_simulator_refused_by_default(env, monkeypatch):
    monkeypatch.delenv(cs.SIMULATORS_ENV, raising=False)
    called = []
    monkeypatch.setattr(cs_taihe, "sync_device", lambda *a, **k: called.append(a) or {})
    monkeypatch.setattr(cs_taihe, "Transport", lambda *a, **k: called.append(("transport", a)))
    for fn in (cs.sync_device, cs.check_device):
        with pytest.raises(cs.CableError, match=cs.SIMULATORS_ENV):
            fn(_harmony_sim())
    assert called == []


def test_start_refuses_simulator_by_default(monkeypatch):
    """start：传进来的、和 list_devices 里冒出来的模拟器都拒绝，不起后台同步。真机照常。"""
    monkeypatch.delenv(cs.SIMULATORS_ENV, raising=False)
    ran = []
    monkeypatch.setattr(cs, "sync_device", lambda d, **k: ran.append(("sync", d["id"])) or {"ok": True})
    monkeypatch.setattr(cs, "check_device", lambda d, **k: ran.append(("check", d["id"])) or {"ok": True})
    monkeypatch.setattr(cs.threading, "Thread", lambda target, **kw: type("T", (), {"start": lambda self: target()})())
    monkeypatch.setattr(cs, "_STATE", dict(cs._STATE, running=False, last={}, checked={}))
    for dev in (_sim(), _harmony_sim()):
        assert cs.start(dev["id"], device=dev) == cs.SIMULATOR_REFUSED
        assert cs.start(dev["id"], device=dev, quick=True) == cs.SIMULATOR_REFUSED
    monkeypatch.setattr(cs, "list_devices", lambda: [_sim()])
    assert cs.start("SIM-1") == cs.SIMULATOR_REFUSED
    assert ran == [] and cs._STATE["running"] is False
    assert cs.start("dev-1", device=_device()) == ""
    assert cs.start("dev-1", device=_device(), quick=True) == ""
    assert ran == [("sync", "dev-1"), ("check", "dev-1")]


def test_simulator_transport_refused_by_default(monkeypatch, tmp_path):
    """第三道：Transport 读写模拟器容器前也拦，连 simctl 都不调。"""
    monkeypatch.delenv(cs.SIMULATORS_ENV, raising=False)
    runs = []
    monkeypatch.setattr(cs, "_run", lambda cmd, timeout=0: runs.append(cmd))
    tr = cs.Transport(_sim())
    with pytest.raises(cs.CableError, match=cs.SIMULATORS_ENV):
        tr.push(str(tmp_path), cs.DEST)
    with pytest.raises(cs.CableError, match=cs.SIMULATORS_ENV):
        tr.pull_outbox(str(tmp_path / "out"))
    assert runs == []


def test_simulator_allowed_with_dev_switch(sim_env, monkeypatch, tmp_path):
    """开关打开：模拟器跟现在一样能同步（拉发件箱、并阅读记录、推笔记库）。"""
    monkeypatch.setenv(cs.SIMULATORS_ENV, "1")
    v, tr, phone, merged = sim_env
    r = cs.sync_device(_sim(), transport=tr)
    assert r["ok"] and r["saved"] == 1 and r["reads_received"] == 1
    assert (v / "output" / "模拟器测试.md").exists()
    assert [m["id"] for m in merged] == ["sim-e1"]
    assert (phone / cs.DEST / cs.MANIFEST).exists()
    assert cs.check_device(_sim(), transport=tr)["ok"]

    # Transport 走模拟器容器目录
    container = tmp_path / "container"
    monkeypatch.setattr(cs, "_run", lambda cmd, timeout=0: subprocess.CompletedProcess(cmd, 0, str(container) + "\n", ""))
    src = tmp_path / "src"
    _write(str(src / "a.md"), "内容")
    cs.Transport(_sim()).push(str(src), cs.DEST)
    assert (container / cs.DEST / "a.md").read_text(encoding="utf-8") == "内容"

    ran = []
    monkeypatch.setattr(cs, "sync_device", lambda d, **k: ran.append(d["id"]) or {"ok": True})
    monkeypatch.setattr(cs.threading, "Thread", lambda target, **kw: type("T", (), {"start": lambda self: target()})())
    monkeypatch.setattr(cs, "_STATE", dict(cs._STATE, running=False, last={}, checked={}))
    assert cs.start("SIM-1", device=_sim()) == ""
    assert ran == ["SIM-1"]


def test_harmony_simulator_not_listed_by_default(monkeypatch):
    """鸿蒙模拟器（127.0.0.1: 开头）默认不列，也不去读它的 device.json；开关打开后照旧列成 simulator。"""
    monkeypatch.setattr(cs_taihe, "hdc_command", lambda: ["hdc"])
    monkeypatch.setattr(cs_taihe.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "127.0.0.1:5555\nABC123\n", ""))
    probed = []

    class FakeHdc:
        def __init__(self, device):
            self.device = device

        def device_info(self):
            probed.append(self.device["udid"])
            return {"version": 1, "enabled": True, "id": "phone-1", "name": "太和剪藏"}

    monkeypatch.setattr(cs_taihe, "Transport", FakeHdc)
    monkeypatch.delenv(cs.SIMULATORS_ENV, raising=False)
    assert [(d["udid"], d["transport"]) for d in cs_taihe.list_devices()] == [("ABC123", "wired")]
    assert probed == ["ABC123"]
    monkeypatch.setenv(cs.SIMULATORS_ENV, "1")
    assert [(d["udid"], d["transport"]) for d in cs_taihe.list_devices()] == [
        ("127.0.0.1:5555", "simulator"), ("ABC123", "wired")]


def test_api_cable_reports_switch_and_refuses_simulator(monkeypatch):
    from werkzeug.test import Client
    import spark
    monkeypatch.delenv(cs.SIMULATORS_ENV, raising=False)
    monkeypatch.setattr(cs, "list_devices", lambda: [_sim()])   # 就算设备列表里混进了模拟器
    ran = []
    monkeypatch.setattr(cs, "sync_device", lambda d, **k: ran.append(d["id"]) or {"ok": True})
    monkeypatch.setattr(cs, "_STATE", dict(cs._STATE, running=False, last={}, checked={}))
    client = Client(spark.application)
    assert client.get("/api/cable").get_json()["simulators"] is False
    r = client.post("/api/cable", json={"device": "SIM-1"})
    assert r.status_code == 400 and r.get_json()["error"] == cs.SIMULATOR_REFUSED
    assert ran == []
    monkeypatch.setenv(cs.SIMULATORS_ENV, "1")
    assert client.get("/api/cable").get_json()["simulators"] is True


def test_settings_page_hides_simulator_entry_without_switch():
    """设置页：只有 /api/cable 说开关打开了（simulators），才列模拟器、才显示「显示模拟器」。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "static", "settings.js"), encoding="utf-8") as f:
        js = f.read()
    assert "const simsOn = d.simulators === true;" in js
    assert 'dev.transport !== "simulator" || (simsOn && showSims)' in js


def _harmony_wired_sim():
    """transport 写成 wired，udid 却是鸿蒙模拟器的 127.0.0.1:：照样按模拟器处理。"""
    return dict(_harmony_sim(), transport="wired")


def test_is_simulator():
    assert cs.is_simulator(_sim()) and cs.is_simulator(_harmony_sim()) and cs.is_simulator(_harmony_wired_sim())
    assert not cs.is_simulator(_device())
    assert not cs.is_simulator({"id": "taihe:ABC123", "udid": "ABC123", "platform": "HarmonyOS", "transport": "wired"})
    assert not cs.is_simulator({"id": "x", "udid": "127.0.0.1:5555", "transport": "wired"})   # 不是鸿蒙不按 udid 算
    assert not cs.is_simulator(None)


def test_harmony_wired_transport_but_simulator_udid_refused(sim_env, monkeypatch, tmp_path):
    monkeypatch.delenv(cs.SIMULATORS_ENV, raising=False)
    v, tr, phone, merged = sim_env
    called = []
    monkeypatch.setattr(cs_taihe, "Transport", lambda *a, **k: called.append(("transport", a)))
    real_engine = cs_taihe.Engine
    monkeypatch.setattr(cs_taihe, "Engine", lambda *a, **k: called.append(("engine", a)) or real_engine(*a, **k))
    for fn in (cs.sync_device, cs.check_device):
        with pytest.raises(cs.CableError) as e:
            fn(_harmony_wired_sim(), transport=tr)
        assert str(e.value) == cs.SIMULATOR_REFUSED
    with pytest.raises(cs_taihe.SyncError, match=cs.SIMULATORS_ENV):
        cs_taihe.sync_device(_harmony_wired_sim(), transport=tr, root=str(v), home=str(tmp_path / "home"))
    monkeypatch.setattr(cs, "sync_device", lambda d, **k: called.append(("sync", d["id"])) or {"ok": True})
    monkeypatch.setattr(cs, "_STATE", dict(cs._STATE, running=False, last={}, checked={}))
    assert cs.start("taihe:127.0.0.1:5555", device=_harmony_wired_sim()) == cs.SIMULATOR_REFUSED
    assert called == []
    assert not (tmp_path / "home").exists()
    _assert_untouched(v, tr, phone, merged)


class TaiheRecorder:
    """鸿蒙 Transport 的替身：记下每一次读写。"""
    def __init__(self):
        self.calls = []

    def device_info(self):
        self.calls.append("device_info")
        return {"version": 1, "enabled": True, "id": "phone-1"}

    def records(self):
        self.calls.append("records")
        return []

    def image(self, cid, name):
        self.calls.append("image")
        return b""

    def push(self, folder):
        self.calls.append("push")


def test_taihe_sync_device_refuses_simulator_directly(env, monkeypatch, tmp_path):
    """直接调 taihe_sync.sync_device（不经过 cable_sync）：开关没开也拒绝模拟器，什么都不读不写。"""
    v, _tr, _phone, _outbox = env
    home = tmp_path / "home"
    before = sorted(str(p.relative_to(v)) for p in v.rglob("*"))
    monkeypatch.delenv(cs.SIMULATORS_ENV, raising=False)
    for dev in (_harmony_sim(), _harmony_wired_sim()):
        rec = TaiheRecorder()
        with pytest.raises(cs_taihe.SyncError) as e:
            cs_taihe.sync_device(dev, transport=rec, root=str(v), home=str(home))
        assert str(e.value) == cs.SIMULATOR_REFUSED and rec.calls == []
    assert not home.exists()
    assert sorted(str(p.relative_to(v)) for p in v.rglob("*")) == before
    # 开关打开：照常同步（读设备信息、读发件箱）
    monkeypatch.setenv(cs.SIMULATORS_ENV, "1")
    (v / "Spark" / "剪藏").mkdir(parents=True, exist_ok=True)
    rec = TaiheRecorder()
    cs_taihe.sync_device(_harmony_sim(), transport=rec, root=str(v), home=str(home))
    assert rec.calls[:2] == ["device_info", "records"]


# ---------------------------------------------------------------- 审查后补的四处（回环写法、taihe 按 udid、开关取值、自动同步和推结果）

@pytest.mark.parametrize("udid", ["127.0.0.1:5555", "localhost:5555", "LOCALHOST:5555", "127.0.0.2:5555", "127.1:5555"])
def test_harmony_loopback_udid_is_simulator(udid):
    assert cs.is_loopback_udid(udid)
    assert cs.is_simulator({"id": "taihe:" + udid, "udid": udid, "platform": "HarmonyOS", "transport": "wired"})


@pytest.mark.parametrize("udid", ["192.168.1.20:5555", "ABC123", "FMR0223C13000649", "", None, "127.0.0.1", "10.0.0.127:5555"])
def test_harmony_real_device_udid_not_simulator(udid):
    assert not cs.is_loopback_udid(udid)
    assert not cs.is_simulator({"id": "taihe:x", "udid": udid, "platform": "HarmonyOS", "transport": "wired"})


def test_ios_device_not_judged_by_udid():
    """iOS 设备只看 transport：udid 恰好像回环地址也不算模拟器。"""
    for udid in ("127.0.0.1:5555", "localhost:5555", "127.1:5555"):
        assert not cs.is_simulator({"id": "x", "udid": udid, "transport": "wired"})
        assert not cs.is_simulator({"id": "x", "udid": udid, "transport": "localNetwork"})


def test_taihe_list_devices_hides_all_loopback_forms(monkeypatch):
    serials = "localhost:5555\nLOCALHOST:5556\n127.0.0.2:5555\n127.1:5555\n192.168.1.20:5555\nABC123\n"
    monkeypatch.setattr(cs_taihe, "hdc_command", lambda: ["hdc"])
    monkeypatch.setattr(cs_taihe.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, serials, ""))
    probed = []

    class FakeHdc:
        def __init__(self, device):
            self.device = device

        def device_info(self):
            probed.append(self.device["udid"])
            return {"version": 1, "enabled": True, "id": "phone-1", "name": "太和剪藏"}

    monkeypatch.setattr(cs_taihe, "Transport", FakeHdc)
    monkeypatch.delenv(cs.SIMULATORS_ENV, raising=False)
    assert [(d["udid"], d["transport"]) for d in cs_taihe.list_devices()] == [
        ("192.168.1.20:5555", "wired"), ("ABC123", "wired")]
    assert probed == ["192.168.1.20:5555", "ABC123"]
    monkeypatch.setenv(cs.SIMULATORS_ENV, "1")
    assert [(d["udid"], d["transport"]) for d in cs_taihe.list_devices()] == [
        ("localhost:5555", "simulator"), ("LOCALHOST:5556", "simulator"), ("127.0.0.2:5555", "simulator"),
        ("127.1:5555", "simulator"), ("192.168.1.20:5555", "wired"), ("ABC123", "wired")]


def test_taihe_sync_device_judges_by_udid_without_platform(env, monkeypatch, tmp_path):
    """直接调 taihe_sync.sync_device，设备信息里没有 platform：照样按 udid 认出模拟器，Transport 一个方法都不调。"""
    v, _tr, _phone, _outbox = env
    home = tmp_path / "home"
    monkeypatch.delenv(cs.SIMULATORS_ENV, raising=False)
    made = []
    monkeypatch.setattr(cs_taihe, "Transport", lambda *a, **k: made.append(a))
    for udid in ("127.0.0.1:5555", "localhost:5555", "127.1:5555"):
        rec = TaiheRecorder()
        with pytest.raises(cs_taihe.SyncError) as e:
            cs_taihe.sync_device({"udid": udid}, transport=rec, root=str(v), home=str(home))
        assert str(e.value) == cs.SIMULATOR_REFUSED and rec.calls == []
        with pytest.raises(cs_taihe.SyncError):
            cs_taihe.sync_device({"udid": udid}, root=str(v), home=str(home))   # 不传 transport：也不新建
    assert made == [] and not home.exists()
    # 真机（无线调试的 192.168.x.x、普通序列号）照常同步
    (v / "Spark" / "剪藏").mkdir(parents=True, exist_ok=True)
    for udid in ("192.168.1.20:5555", "ABC123"):
        rec = TaiheRecorder()
        cs_taihe.sync_device({"udid": udid}, transport=rec, root=str(v), home=str(home))
        assert rec.calls[:2] == ["device_info", "records"]


@pytest.mark.parametrize("value,on", [
    ("1", True), (" 1", True), ("1\n", True), (" 1 \t", True),
    ("true", False), ("TRUE", False), ("01", False), ("0", False), ("", False), ("yes", False)])
def test_simulators_switch_values(monkeypatch, value, on):
    """开关：值去掉首尾空白后等于 1 才算开。"""
    monkeypatch.setenv(cs.SIMULATORS_ENV, value)
    assert cs.simulators_allowed() is on


def test_simulators_switch_unset(monkeypatch):
    monkeypatch.delenv(cs.SIMULATORS_ENV, raising=False)
    assert cs.simulators_allowed() is False


def test_auto_tick_skips_simulators(monkeypatch):
    """自动同步：iOS 模拟器、transport 写成 wired 的鸿蒙回环设备都不开始同步，不起线程、不调 transport。
    同一轮里的真机照常开始。"""
    monkeypatch.delenv(cs.SIMULATORS_ENV, raising=False)
    threads, touched = [], []
    monkeypatch.setattr(cs.threading, "Thread", lambda target, **kw: threads.append(kw.get("name")) or
                        type("T", (), {"start": lambda self: None})())
    monkeypatch.setattr(cs, "Transport", lambda *a, **k: touched.append(("cable", a)))
    monkeypatch.setattr(cs_taihe, "Transport", lambda *a, **k: touched.append(("taihe", a)))
    monkeypatch.setattr(cs, "_run", lambda *a, **k: touched.append(("run", a)))
    monkeypatch.setattr(cs, "_STATE", dict(cs._STATE, running=False, last={}, checked={}))
    monkeypatch.setattr(cs, "_AUTO", {"on": False, "last": {}, "checked": {}})
    sims = [_sim(), _harmony_wired_sim()]
    assert cs.auto_tick(sims, now=1000.0) == []
    assert threads == [] and touched == [] and cs._STATE["running"] is False
    assert cs._AUTO["last"] == {}
    # 下一轮（过了看一眼的间隔）也不开始
    assert cs.auto_tick(sims, now=1000.0 + cs.AUTO_CHECK_SECONDS + 1) == []
    assert threads == [] and touched == []
    # 真机照常：起一个同步线程
    assert cs.auto_tick(sims + [_device()], now=2000.0) == [_device()["name"]]
    assert threads == ["spark-cable-sync"] and touched == []


def test_push_results_skips_simulator(env, monkeypatch, tmp_path):
    """后台推任务结果：开关没开时遇到模拟器不调 simctl；开关打开后照旧推进模拟器容器。"""
    monkeypatch.delenv(cs.SIMULATORS_ENV, raising=False)
    runs = []
    container = tmp_path / "container"
    monkeypatch.setattr(cs, "_run", lambda cmd, timeout=0: runs.append(cmd) or
                        subprocess.CompletedProcess(cmd, 0, str(container) + "\n", ""))
    book = cs._book()
    cs._push_results(_sim(), book)
    cs._push_results(_harmony_wired_sim(), book)
    assert runs == [] and not container.exists()
    monkeypatch.setenv(cs.SIMULATORS_ENV, "1")
    cs._push_results(_sim(), book)
    assert runs and runs[0][:3] == ["xcrun", "simctl", "get_app_container"]
    assert (container / cs.DEST / cs.RESULTS).exists()
