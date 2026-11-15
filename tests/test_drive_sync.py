"""Google Drive 文件夹同步：只用临时目录，不碰真笔记库、真 ~/.spark。"""

import os

import pytest

from core import cable_sync, drive_sync


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    home = tmp_path / "spark-home"
    home.mkdir()
    monkeypatch.setattr(cable_sync, "_spark_home", lambda: str(home))
    return home


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def test_mirror_copies_changed_and_prunes(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    _write(str(src / "a.md"), "一")
    _write(str(src / "sub" / "b.md"), "二")
    _write(str(dst / "old" / "gone.md"), "旧")
    assert drive_sync.mirror(str(src), str(dst), prune=True) == (2, 1)
    assert (dst / "sub" / "b.md").read_text(encoding="utf-8") == "二"
    assert not (dst / "old").exists()
    # 没变的不再拷
    assert drive_sync.mirror(str(src), str(dst), prune=True) == (0, 0)
    _write(str(src / "a.md"), "改过了")
    assert drive_sync.mirror(str(src), str(dst), prune=False) == (1, 0)


def test_transport_reads_outbox_and_readlog(tmp_path):
    folder = tmp_path / "drive"
    _write(str(folder / "outbox" / "t1.json"), '{"id": "t1"}')
    _write(str(folder / "outbox" / "half.json.spark-partial"), "x")
    _write(str(folder / "readlog" / "phone.jsonl"), "{}\n")
    tr = drive_sync.DriveTransport(str(folder))
    out, log = tmp_path / "out", tmp_path / "log"
    out.mkdir(), log.mkdir()
    tr.pull_outbox(str(out))
    tr.pull_group_dir(cable_sync.READLOG, str(log))
    assert sorted(os.listdir(out)) == ["t1.json"]
    assert os.listdir(log) == ["phone.jsonl"]


def test_transport_push_writes_vault(tmp_path):
    folder, staging = tmp_path / "drive", tmp_path / "staging"
    _write(str(staging / "Spark" / "x.md"), "正文")
    tr = drive_sync.DriveTransport(str(folder))
    tr.push(str(staging), cable_sync.DEST)
    assert (folder / "vault" / "Spark" / "x.md").read_text(encoding="utf-8") == "正文"
    assert tr.copied == 1


def test_detect_my_drive_and_folder(tmp_path, _home):
    base = tmp_path / "CloudStorage"
    (base / "GoogleDrive-a@example.com" / "我的云端硬盘").mkdir(parents=True)
    assert drive_sync.detect_my_drive(str(base)) == [str(base / "GoogleDrive-a@example.com" / "我的云端硬盘")]
    assert drive_sync.sync_folder({"folder": str(tmp_path / "x")}) == str(tmp_path / "x")
    drive_sync.save_config(enabled=True)
    assert drive_sync.load_config()["enabled"] is True


def test_auto_due(tmp_path):
    folder = str(tmp_path / drive_sync.FOLDER_NAME)
    assert drive_sync.auto_due(10_000, 0, True, folder)
    assert not drive_sync.auto_due(10_000, 9_900, True, folder)
    assert not drive_sync.auto_due(10_000, 0, False, folder)
    assert not drive_sync.auto_due(10_000, 0, True, "")


def test_sync_once_without_drive(monkeypatch):
    monkeypatch.setattr(drive_sync, "sync_folder", lambda cfg=None: "")
    with pytest.raises(drive_sync.DriveError):
        drive_sync.sync_once()


def test_sync_once_uses_cable_flow(tmp_path, monkeypatch):
    folder = tmp_path / "drive" / drive_sync.FOLDER_NAME
    monkeypatch.setattr(drive_sync, "sync_folder", lambda cfg=None: str(folder))
    seen = {}

    def fake_sync(device, transport=None):
        seen["device"], seen["transport"] = device, transport
        assert cable_sync._STATE["running"]
        return {"ok": True, "files": 3}

    monkeypatch.setattr(cable_sync, "sync_device", fake_sync)
    r = drive_sync.sync_once()
    assert r["ok"] and r["folder"] == str(folder)
    assert seen["device"]["transport"] == "drive"
    assert isinstance(seen["transport"], drive_sync.DriveTransport)
    assert (folder / "outbox").is_dir() and (folder / "readlog").is_dir()
    assert not cable_sync._STATE["running"]
