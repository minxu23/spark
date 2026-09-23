"""后台任务表的清理规则。"""

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

