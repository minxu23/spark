"""后台任务表的清理规则：两个 app 各有几张任务表（生成、主题总结、信息跟进……），
字段各不相同，但"已结束的任务留多久、最多留几个"是同一套规则，只写一份。"""

from __future__ import annotations

import time
from typing import Optional


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
