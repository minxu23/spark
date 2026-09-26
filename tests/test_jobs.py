"""后台任务的公共规则：任务表清理、重任务名额。"""

import threading
import time

import pytest

from core import jobs


def test_只删过期的已结束任务_进行中的永远保留():
    table = {
        "old-done": {"done": True, "finished_at": 0},
        "new-done": {"done": True, "finished_at": 950},
        "old-running": {"done": False, "created_at": 0},
    }
    jobs.prune_finished(table, retention_seconds=100, now=1000)
    assert set(table) == {"new-done", "old-running"}


def test_已结束的只留最新几个():
    table = {f"j{i}": {"done": True, "finished_at": i} for i in range(5)}
    table["running"] = {"done": False, "created_at": 0}
    jobs.prune_finished(table, retention_seconds=10_000, max_completed=2, now=10)
    assert set(table) == {"j4", "j3", "running"}


def test_没有finished_at时按创建时间算():
    table = {"a": {"done": True, "created_at": 0}}
    jobs.prune_finished(table, retention_seconds=5, now=10)
    assert table == {}


# ---------------------------------------------------------------------------
# JobSlots：重任务同时最多跑几个
# ---------------------------------------------------------------------------

def _wait_until(pred, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return
        time.sleep(0.005)
    raise AssertionError("超时")


def test_名额满了就拒绝_不排队():
    slots = jobs.JobSlots(2)
    assert slots.try_acquire() and slots.try_acquire()
    assert not slots.try_acquire()
    assert slots.running == 2
    assert slots.busy_message() == "已经有 2 个任务在跑，等其中一个结束再开始"
    slots.release()
    assert slots.try_acquire()


def test_多线程抢名额_只有limit个抢到():
    slots = jobs.JobSlots(3)
    got = []
    barrier = threading.Barrier(20)

    def grab():
        barrier.wait()
        got.append(slots.try_acquire())

    threads = [threading.Thread(target=grab) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert got.count(True) == 3
    assert slots.running == 3


def test_任务正常结束或抛异常都归还名额(monkeypatch):
    hooked = []
    monkeypatch.setattr(threading, "excepthook", lambda args: hooked.append(args.exc_value))
    slots = jobs.JobSlots(1)
    gate = threading.Event()
    assert slots.try_acquire()
    slots.start(gate.wait, 5)
    assert slots.running == 1 and not slots.try_acquire()
    gate.set()
    _wait_until(lambda: slots.running == 0)

    def boom():
        raise RuntimeError("出错了")

    assert slots.try_acquire()
    slots.start(boom)
    _wait_until(lambda: slots.running == 0 and hooked)   # 异常照常抛到线程外
    assert slots.try_acquire()


def test_线程起不来时归还名额并把异常抛出去(monkeypatch):
    slots = jobs.JobSlots(1)
    assert slots.try_acquire()

    def fail_start(self):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(threading.Thread, "start", fail_start)
    with pytest.raises(RuntimeError):
        slots.start(lambda: None)
    monkeypatch.undo()
    assert slots.running == 0


def test_多还不会变成负数():
    slots = jobs.JobSlots(1)
    slots.release()
    assert slots.running == 0
    assert slots.try_acquire() and not slots.try_acquire()
