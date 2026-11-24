"""界面上「信息跟进」改叫「信息」：只改给人看的名字，笔记库里的文件夹、数据键还叫「信息跟进」。"""

import os

import spark
from apps.reader import server as reader
from apps.summit2md import subscriptions_store
from core import remote_tasks

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def test_入口名字叫信息():
    track = next(a for a in spark.APPS if a["key"] == "track")
    assert track["name"] == "信息"
    assert 'data-ws="track">信息</a>' in _read("apps/summit2md/static/index.html")
    assert '<a href="/summit/?mode=track">信息</a>' in _read("apps/review/static/index.html")
    assert '<a href="/summit/?mode=track">信息</a>' in _read("apps/notes2insight/static/index.html")
    assert 'case .track: "信息"' in _read("apps/mobile-ios/Spark/Theme.swift")
    assert 'case .track: return "信息"' in _read("apps/macos/Sources/Spark/Model.swift")


def test_文件夹和数据键不动():
    assert subscriptions_store.TRACK_DIRNAME == "信息跟进"
    assert remote_tasks.TRACK_DIR.endswith("/信息跟进")
    assert reader._TRACK_NAME == "信息跟进"
    assert 'static let trackDir = "Spark/信息跟进/"' in _read("apps/mobile-ios/Spark/Library.swift")
