"""手机上添加的任务（core/remote_tasks.py）。

GitHub 和 Spark 自己的接口都换成假的：不联网、不真跑任务。笔记库用 SPARK_VAULT 指到临时目录。
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import remote_tasks as rt
from core import settings


def _body(task) -> str:
    return "从手机添加\n\n```spark-task\n" + json.dumps(task, ensure_ascii=False) + "\n```\n"


class FakeGitHub:
    def __init__(self, issues, me="minxu23"):
        self.issues = {i["number"]: dict(i, labels=list(i.get("labels") or [rt.TASK_LABEL]), state="open")
                       for i in issues}
        self.login = me
        self.comments: dict[int, dict] = {}
        self._next = 1

    def me(self):
        return self.login

    def open_tasks(self):
        return [dict(i, labels=[{"name": n} for n in i["labels"]]) for i in self.issues.values()
                if i["state"] == "open" and rt.TASK_LABEL in i["labels"]]

    def comment(self, number, body):
        cid = self._next
        self._next += 1
        self.comments[cid] = {"issue": number, "body": body}
        return cid

    def edit_comment(self, cid, body):
        self.comments[cid]["body"] = body

    def add_label(self, number, label):
        if label not in self.issues[number]["labels"]:
            self.issues[number]["labels"].append(label)

    def remove_label(self, number, label):
        if label in self.issues[number]["labels"]:
            self.issues[number]["labels"].remove(label)

    def close(self, number):
        self.issues[number]["state"] = "closed"

    def last_status(self, number):
        bodies = [c["body"] for c in self.comments.values() if c["issue"] == number]
        line = bodies[-1].split("\n", 1)[0]
        return json.loads(line[len(rt.STATUS_PREFIX):-len(" -->")]), bodies[-1]


class FakeSpark:
    """按路径返回预设的回应，记下每次调用。"""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, method, path, body):
        self.calls.append((method, path, body))
        for key, resp in self.routes.items():
            if path.startswith(key):
                return resp(body) if callable(resp) else resp
        return 404, {"error": "没有这个接口"}


@pytest.fixture
def show(tmp_path, monkeypatch):
    v = tmp_path / "vault"
    folder = v / "Spark" / "Dwarkesh"
    folder.mkdir(parents=True)
    (folder / ".manifest.json").write_text(json.dumps({"content_type": "series", "source_url": "https://feed"}))
    monkeypatch.setenv("SPARK_VAULT", str(v))
    monkeypatch.setenv("SPARK_GITHUB_TOKEN", "test-token")
    return str(folder)


def _queue(gh, spark):
    return rt.Queue(spark, gh_factory=lambda: gh, sleep=lambda s: None)


# ---------------------------------------------------------------- 解析

def test_parse_link_to_existing_show(show):
    t = rt.parse(_body({"type": "link", "url": "https://youtu.be/abc", "dest": {"kind": "show", "folder": "Dwarkesh"}}))
    assert t["dest"] == {"kind": "show", "folder": show}


@pytest.mark.parametrize("task, msg", [
    ({"type": "link", "url": "file:///etc/passwd"}, "url"),
    ({"type": "link", "url": "https://x.com/a", "dest": {"kind": "show", "folder": "../.."}}, "没有文件夹"),
    ({"type": "link", "url": "https://x.com/a", "dest": {"kind": "show", "folder": "不存在"}}, "没有文件夹"),
    ({"type": "check", "subscriptions": []}, "subscriptions"),
    ({"type": "check", "subscriptions": ["量子位"], "max_new": 500}, "max_new"),
    ({"type": "report", "topic": ""}, "topic"),
    ({"type": "rm", "path": "/"}, "type"),
])
def test_parse_rejects(show, task, msg):
    with pytest.raises(rt.TaskError, match=msg):
        rt.parse(_body(task))


def test_parse_needs_block():
    with pytest.raises(rt.TaskError, match="spark-task"):
        rt.parse("随便写的 issue")


def test_repo_slug():
    for url in ("git@github.com:minxu23/spark-vault.git", "https://github.com/minxu23/spark-vault"):
        settings.save({"sync": {"github_repo": url}})
        assert rt.repo_slug() == "minxu23/spark-vault"


# ---------------------------------------------------------------- 跑任务

def test_link_task_runs_and_closes_issue(show):
    gh = FakeGitHub([{"number": 7, "user": {"login": "minxu23"},
                      "body": _body({"type": "link", "url": "https://youtu.be/abc",
                                     "dest": {"kind": "show", "folder": "Dwarkesh"}})}])
    spark = FakeSpark({
        "/summit/api/discover": (200, {"entries": [{"id": "abc"}], "summit_title": "x"}),
        "/summit/api/run": (200, {"job_id": "j1"}),
        "/summit/api/status/j1": (200, {"done": True, "error": None,
                                        "result": {"output_dir": show, "failed_entries": []}}),
    })
    assert _queue(gh, spark).poll_once() == 1
    run = next(b for m, p, b in spark.calls if p == "/summit/api/run")
    # 放进已有节目：用文件夹自己的名字、类型和来源地址，不拿单集链接覆盖来源
    assert run["summit_title"] == "Dwarkesh" and run["content_type"] == "series"
    assert run["source_url"] == "https://feed" and run["output_dir"] == os.path.dirname(show)
    assert gh.issues[7]["state"] == "closed" and rt.RUNNING_LABEL not in gh.issues[7]["labels"]
    meta, text = gh.last_status(7)
    assert meta["state"] == "done" and "Spark/Dwarkesh" in text


def test_job_error_marks_failed(show):
    gh = FakeGitHub([{"number": 3, "user": {"login": "minxu23"},
                      "body": _body({"type": "link", "url": "https://youtu.be/abc"})}])
    spark = FakeSpark({
        "/summit/api/discover": (200, {"entries": [{"id": "abc"}], "summit_title": "新节目"}),
        "/summit/api/run": (200, {"job_id": "j1"}),
        "/summit/api/status/j1": (200, {"done": True, "error": "YouTube 要求验证不是机器人"}),
    })
    _queue(gh, spark).poll_once()
    assert gh.issues[3]["state"] == "open"
    assert rt.FAILED_LABEL in gh.issues[3]["labels"] and rt.RUNNING_LABEL not in gh.issues[3]["labels"]
    meta, text = gh.last_status(3)
    assert meta["state"] == "failed" and "机器人" in text


def test_bad_task_fails_without_running(show):
    gh = FakeGitHub([{"number": 4, "user": {"login": "minxu23"}, "body": "没有代码块"}])
    spark = FakeSpark({})
    _queue(gh, spark).poll_once()
    assert rt.FAILED_LABEL in gh.issues[4]["labels"] and not spark.calls


def test_ignores_other_users_and_failed(show):
    task = _body({"type": "link", "url": "https://youtu.be/abc"})
    gh = FakeGitHub([
        {"number": 1, "user": {"login": "someone-else"}, "body": task},
        {"number": 2, "user": {"login": "minxu23"}, "body": task, "labels": [rt.TASK_LABEL, rt.FAILED_LABEL]},
    ])
    spark = FakeSpark({})
    assert _queue(gh, spark).poll_once() == 0
    assert not spark.calls and not gh.comments


def test_busy_waits_and_says_so_once(show):
    gh = FakeGitHub([{"number": 5, "user": {"login": "minxu23"},
                      "body": _body({"type": "link", "url": "https://youtu.be/abc"})}])
    spark = FakeSpark({
        "/summit/api/discover": (200, {"entries": [{"id": "abc"}], "summit_title": "x"}),
        "/summit/api/run": (429, {"error": "已经有 1 个任务在跑"}),
    })
    q = _queue(gh, spark)
    assert q.poll_once() == 0
    q._busy_until.clear()
    q.poll_once()
    assert gh.issues[5]["state"] == "open" and gh.issues[5]["labels"] == [rt.TASK_LABEL]
    waiting = [c for c in gh.comments.values() if '"waiting"' in c["body"]]
    assert len(waiting) == 1


def test_interrupted_task_marked_failed_on_start(show):
    gh = FakeGitHub([{"number": 9, "user": {"login": "minxu23"}, "labels": [rt.TASK_LABEL, rt.RUNNING_LABEL],
                      "body": _body({"type": "link", "url": "https://youtu.be/abc"})}])
    _queue(gh, FakeSpark({})).poll_once()
    assert rt.FAILED_LABEL in gh.issues[9]["labels"]
    assert "重启" in gh.last_status(9)[1]


def test_check_task_limits_new_entries(show):
    gh = FakeGitHub([{"number": 11, "user": {"login": "minxu23"},
                      "body": _body({"type": "check", "subscriptions": ["量子位", "不存在的"], "max_new": 2})}])
    new = [{"id": f"e{i}", "publish_date": f"2026100{i}"} for i in range(1, 5)]
    spark = FakeSpark({
        "/summit/api/subscriptions?kind=podcast": (200, []),
        "/summit/api/subscriptions?kind=track": (200, [{"id": "s1", "name": "量子位", "kind": "track",
                                                        "folder": "/x/信息跟进/量子位"}]),
        "/summit/api/subscriptions/s1/check": (200, {"error": None, "new_entries": new}),
        "/summit/api/track/run": (200, {"job_id": "t1"}),
        "/summit/api/track/status/t1": (200, {"done": True, "error": None}),
    })
    _queue(gh, spark).poll_once()
    run = next(b for m, p, b in spark.calls if p == "/summit/api/track/run")
    assert run["selections"] == [{"sub_id": "s1", "entry_ids": ["e4", "e3"]}]   # 最新的两条
    _meta, text = gh.last_status(11)
    assert "处理了 2 条" in text and "还有 2 条" in text and "不存在的" in text
    assert gh.issues[11]["state"] == "closed"


def test_report_task_picks_relevant_notes(show):
    gh = FakeGitHub([{"number": 12, "user": {"login": "minxu23"},
                      "body": _body({"type": "report", "topic": "机器人触觉"})}])
    cands = [{"path": "a.md", "relevance": 3}, {"path": "b.md", "relevance": 1}, {"path": "c.md", "relevance": 4}]
    spark = FakeSpark({
        "/notes/api/search": (200, {"job_id": "s1"}),
        "/notes/api/progress/s1": (200, {"done": True, "ok": True}),
        "/notes/api/result/s1": (200, {"candidates": cands}),
        "/notes/api/run": (200, {"job_id": "r1"}),
        "/notes/api/progress/r1": (200, {"done": True, "ok": True,
                                         "result": {"report_path": os.path.join(os.environ["SPARK_VAULT"], "output", "报告.md")}}),
    })
    _queue(gh, spark).poll_once()
    run = next(b for m, p, b in spark.calls if p == "/notes/api/run")
    assert run["notes"] == ["a.md", "c.md"] and run["source"] == "topic"
    assert "output/报告.md" in gh.last_status(12)[1]


def test_modes_and_interval():
    assert rt.mode() == "" and rt.enabled() is False
    settings.save({"sync": {"github_repo": "git@github.com:minxu23/spark-vault.git", "remote_tasks": "manual"}})
    assert rt.mode() == "manual" and rt.poll_minutes() == rt.DEFAULT_POLL_MINUTES
    settings.save({"sync": {"remote_tasks": "auto", "remote_tasks_minutes": "30"}})
    assert rt.mode() == "auto" and rt.poll_minutes() == 30
    for bad in ({"remote_tasks": "on"}, {"remote_tasks_minutes": "1"}):
        with pytest.raises(settings.SettingsError):
            settings.save({"sync": bad})


def test_mode_off_without_github_repo():
    settings.save({"sync": {"github_repo": "https://gitlab.com/a/b", "remote_tasks": "auto"}})
    assert rt.mode() == ""


def test_trigger_explains_why_not(monkeypatch):
    monkeypatch.delenv("SPARK_GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(rt.keys, "read_key_file", lambda provider: "")
    assert "打开" in rt.trigger()
    settings.save({"sync": {"github_repo": "git@github.com:minxu23/spark-vault.git", "remote_tasks": "manual"}})
    assert "令牌" in rt.trigger()


def test_api_sync_tasks_rejects_when_off():
    from werkzeug.test import Client
    import spark
    r = Client(spark.application).post("/api/sync/tasks")
    assert r.status_code == 400 and "打开" in r.get_json()["error"]


def test_github_errors_do_not_leak_token(monkeypatch):
    import urllib.error

    def opener(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

    gh = rt.GitHub("minxu23/spark-vault", "secret-token-123", opener=opener)
    with pytest.raises(rt.TaskError) as e:
        gh.me()
    assert "secret-token-123" not in str(e.value) and "401" in str(e.value)
