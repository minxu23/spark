"""新内容处理完自动补标签（core/tags.fill_new，由 github_sync.after_task 触发）。假 ask，不调模型，不碰真实笔记库。"""

import os
import shutil
import sys
import threading
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import github_sync, llm_config, tags  # noqa: E402

FIXTURE = os.path.join(ROOT, "apps", "mobile-ios", "dev", "fixture-vault")
_REAL_KICK = github_sync._kick_tags


@pytest.fixture(autouse=True)
def _real_kick(monkeypatch):
    monkeypatch.setattr(github_sync, "_kick_tags", _REAL_KICK)
    monkeypatch.setattr(github_sync, "_TAG_THREAD", None)
    monkeypatch.setattr(tags, "_TRIED", set())
    monkeypatch.setenv("SPARK_VAULT", "/nonexistent-spark-test-vault")   # 没用 lib 的用例也不读真实库


@pytest.fixture
def lib(tmp_path, monkeypatch):
    v = tmp_path / "vault"
    shutil.copytree(FIXTURE, v)
    monkeypatch.setenv("SPARK_VAULT", str(v))
    monkeypatch.setattr(tags, "_JOBS", {})
    os.remove(os.path.join(v, "Spark", ".tags.json"))   # 从没有标签开始
    return str(v)


def _ask_factory(calls):
    def ask(prompt, max_tokens=4000, timeout=600):
        calls.append(prompt)
        n = prompt.split("内容列表")[1].count("\n") - 1
        return "\n".join(f"T|{i}|新标签|动手做" for i in range(1, n + 1))
    return ask


def _wait_idle():
    for _ in range(500):
        if tags.active_count() == 0:
            return
        time.sleep(0.01)
    raise AssertionError("任务没做完")


def _cfg(backend, model="m-set"):
    return {"backend": backend, "api_key": "k", "api_base": "", "model": model}


def _join():
    github_sync._TAG_THREAD.join(5)


def test_nothing_to_fill_starts_no_job(lib, monkeypatch):
    calls = []
    assert tags.fill_new(ask=_ask_factory(calls))     # 有待补的：开了任务
    _wait_idle()
    assert calls
    calls.clear()
    monkeypatch.setattr(tags, "start_fill", lambda **k: pytest.fail("不该开任务"))
    assert tags.fill_new(ask=_ask_factory(calls)) == ""
    assert not calls


def test_pending_starts_one_job_and_after_task_does_not_loop(lib, monkeypatch):
    calls = []
    monkeypatch.setattr(llm_config, "resolve", lambda data, default_backend: _cfg("api"))
    monkeypatch.setattr(tags, "_ask", lambda model="": _ask_factory(calls))
    started = []
    real = tags.start_fill
    monkeypatch.setattr(tags, "start_fill", lambda **k: started.append(k) or real(**k))
    github_sync.after_task()                  # 新内容处理完
    _join()
    _wait_idle()
    assert len(started) == 1 and started[0]["model"] == tags.TAG_MODEL
    n_calls = len(calls)
    assert n_calls > 0 and len(tags._JOBS) == 1
    # 补标签自己收尾（schedule_sync）不触发补标签：没有第二个线程、第二个任务
    t = github_sync._TAG_THREAD
    time.sleep(0.2)
    assert github_sync._TAG_THREAD is t and len(started) == 1 and len(calls) == n_calls
    # 再来一次 after_task：没有待补的，直接返回，不开任务
    github_sync.after_task()
    _join()
    assert len(started) == 1 and len(calls) == n_calls


def test_skipped_items_do_not_loop(lib, monkeypatch):
    """模型一条标签都没给：任务结束后也不会因为还有待补的而自己再开。"""
    monkeypatch.setattr(llm_config, "resolve", lambda data, default_backend: _cfg("api"))
    monkeypatch.setattr(tags, "_ask", lambda model="": (lambda p, max_tokens=0, timeout=0: ""))
    github_sync.after_task()
    _join()
    _wait_idle()
    time.sleep(0.2)
    assert len(tags._JOBS) == 1
    # 别的任务再收尾：同一批补不上的条目不再开任务、不再问模型
    github_sync.after_task()
    _join()
    _wait_idle()
    assert len(tags._JOBS) == 1


def test_quiet_when_busy(lib):
    gate = threading.Event()

    def slow(prompt, max_tokens=4000, timeout=600):
        gate.wait(5)
        return ""
    tags.start_fill(ask=slow, root=lib)
    assert tags.fill_new(ask=slow) == ""      # 已有任务在跑：不抛错
    gate.set()
    _wait_idle()
    # 刚才被拒的那次不算试过：前一个任务没补上的条目，现在再触发要能开任务
    assert tags.fill_new(ask=lambda p, max_tokens=0, timeout=0: "") != ""
    _wait_idle()


def test_quiet_when_no_model(lib, monkeypatch):
    def bad(data, default_backend):
        raise llm_config.ConfigError("没填 Key")
    monkeypatch.setattr(llm_config, "resolve", bad)
    assert tags.fill_new() == ""
    assert tags._JOBS == {}


def test_quiet_when_ollama(lib, monkeypatch):
    monkeypatch.setattr(llm_config, "resolve", lambda data, default_backend: _cfg("ollama"))
    monkeypatch.setattr(tags, "start_fill", lambda **k: pytest.fail("不该开任务"))
    assert tags.fill_new() == ""


def test_fill_model_by_backend():
    assert tags.fill_model(_cfg("api", "claude-opus-x")) == tags.TAG_MODEL
    for b in ("openrouter", "openai_compatible", "ollama", "cli"):
        assert tags.fill_model(_cfg(b, "m-set")) == "m-set"


def test_after_task_calls_fill_new_and_does_not_wait(monkeypatch):
    gate = threading.Event()
    monkeypatch.setattr(tags, "fill_new", lambda: gate.wait(5))
    t0 = time.time()
    github_sync.after_task()
    assert time.time() - t0 < 1               # 调用方不等
    gate.set()
    _join()


# ---------------------------------------------------------------- 各入口都走 after_task

@pytest.fixture
def seen(monkeypatch):
    calls = []
    monkeypatch.setattr(tags, "fill_new", lambda: calls.append(1))
    return calls


def test_summit2md_simple_job_triggers(seen):
    from apps.summit2md import server
    job_id = server._start_simple_job(lambda stop_flag: "ok")
    for _ in range(500):
        if server.SIMPLE_JOBS[job_id]["done"] and github_sync._TAG_THREAD is not None:
            break
        time.sleep(0.01)
    time.sleep(0.1)
    _join()
    assert seen


def test_summit2md_generate_job_triggers(seen):
    from apps.summit2md import server
    with server.JOBS_LOCK:
        server._mark_done_locked("nope", {})
    _join()
    assert seen


def test_notes2insight_job_triggers(seen):
    from apps.notes2insight import server
    server._finish("nope", ok=True)
    _join()
    assert seen


@pytest.mark.parametrize("typ", ["track_item", "episode_item"])
def test_remote_task_item_triggers(seen, monkeypatch, typ):
    from core import remote_tasks as rt
    monkeypatch.setattr(rt, "save_track_item", lambda t: "存进了笔记库")
    monkeypatch.setattr(rt, "save_episode_item", lambda t: "存进了笔记库")
    assert rt.Runner(lambda *a: (500, None), None)._track_item({"type": typ}) == "存进了笔记库"
    _join()
    assert seen
