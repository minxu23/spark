"""后台任务的公共规则：两个 app 各有几张任务表（生成、主题总结、信息跟进……），
字段各不相同，但"已结束的任务留多久、最多留几个"和"重任务同时最多跑几个"
是同一套规则，只写一份。"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Optional


def prune_finished(jobs: dict[str, dict], *, retention_seconds: float,
                   max_completed: Optional[int] = None, now: Optional[float] = None) -> None:
    """就地删掉结束超过 retention_seconds 的任务；max_completed 给了的话，已结束的
    只保留最新的这么多个。进行中的任务永远不删。调用方负责加锁。"""
    now = now or time.time()

    def finished_at(job: dict) -> float:
        return job.get("finished_at", job.get("created_at", now))

    for job_id in [jid for jid, job in jobs.items()
                   if job.get("done") and now - finished_at(job) > retention_seconds]:
        jobs.pop(job_id, None)
    if max_completed is not None:
        done = sorted((jid for jid, job in jobs.items() if job.get("done")),
                      key=lambda jid: finished_at(jobs[jid]), reverse=True)
        for job_id in done[max_completed:]:
            jobs.pop(job_id, None)


class JobSlots:
    """同一个 app 里"重"任务（生成报告、会议/节目批量处理、信息跟进批量处理……）
    同时最多跑几个。每个重任务从启动到后台线程退出一直占一个名额——暂停中的
    也算，它的线程还活着、随时会接着跑。

    满了就直接拒绝、让用户知道，不偷偷排队：排队的任务看不到进度也停不掉，
    用户会以为点了没反应又去点一次。"""

    def __init__(self, limit: int):
        self.limit = limit
        self._running = 0
        self._lock = threading.Lock()

    @property
    def running(self) -> int:
        with self._lock:
            return self._running

    def busy_message(self) -> str:
        return f"已经有 {self.limit} 个任务在跑，等其中一个结束再开始"

    def try_acquire(self) -> bool:
        with self._lock:
            if self._running >= self.limit:
                return False
            self._running += 1
            return True

    def release(self) -> None:
        with self._lock:
            self._running = max(0, self._running - 1)

    def start(self, target: Callable[..., Any], *args: Any) -> None:
        """在后台线程里跑 target，跑完（不管成功、出错还是被停止）归还名额。
        调用前必须已经 try_acquire 成功；线程起不来时也归还，再把异常抛给调用方。"""
        def run() -> None:
            try:
                target(*args)
            finally:
                self.release()

        try:
            threading.Thread(target=run, daemon=True).start()
        except BaseException:
            self.release()
            raise
