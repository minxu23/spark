"""GitHub 同步（core/github_sync.py）。

「GitHub」用临时目录里的一个裸仓库代替，笔记库用 SPARK_VAULT 指到临时目录；
conftest 已经把设置文件指到临时目录，所以 git 仓库（设置文件旁的 vault-sync.git）也在临时目录里。
"""

import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import spark
from core import github_sync, settings


def _write(path, text="x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _remote_files(remote):
    out = subprocess.run(["git", f"--git-dir={remote}", "ls-tree", "-r", "-z", "--name-only", "main"],
                         capture_output=True, text=True, check=True).stdout
    return set(p for p in out.split("\0") if p)


@pytest.fixture
def env(tmp_path, monkeypatch):
    vault_dir = tmp_path / "vault"
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "--quiet", str(remote)], check=True)
    monkeypatch.setenv("SPARK_VAULT", str(vault_dir))
    monkeypatch.setattr(github_sync, "repo_url", lambda: str(remote))
    v = str(vault_dir)
    _write(os.path.join(v, "Spark", "节目", "节目.md"), "总结")
    _write(os.path.join(v, "Spark", "节目", "notes", "第1期.md"), "小结")
    _write(os.path.join(v, "Spark", "节目", ".manifest.json"), "{}")
    _write(os.path.join(v, "Spark", "节目", ".cache", "llm", "a.json"), "缓存")
    _write(os.path.join(v, "Spark", "节目", "logs", "task_1.log"), "日志")
    _write(os.path.join(v, "Spark", ".DS_Store"), "")
    _write(os.path.join(v, "output", "报告.md"), "报告")
    _write(os.path.join(v, "我的日记.md"), "私人")
    _write(os.path.join(v, ".obsidian", "app.json"), "{}")
    return v, str(remote)


def test_first_sync_pushes_only_spark_and_output(env):
    v, remote = env
    r = github_sync.sync_now()
    assert r["ok"] and r["committed"] and r["pushed"]
    assert r["counts"]["added"] == 4
    assert _remote_files(remote) == {"Spark/节目/节目.md", "Spark/节目/notes/第1期.md",
                                     "Spark/节目/.manifest.json", "output/报告.md"}
    # 库里不会多出 .git
    assert not os.path.exists(os.path.join(v, ".git"))


def test_second_sync_records_changes_and_deletions(env):
    v, remote = env
    github_sync.sync_now()
    assert github_sync.sync_now()["committed"] is False   # 没变化不提交

    _write(os.path.join(v, "Spark", "节目", "notes", "第2期.md"), "新的")
    _write(os.path.join(v, "Spark", "节目", "节目.md"), "改过的总结")
    os.remove(os.path.join(v, "output", "报告.md"))
    r = github_sync.sync_now()
    assert r["counts"] == {"added": 1, "modified": 1, "deleted": 1}
    files = _remote_files(remote)
    assert "Spark/节目/notes/第2期.md" in files and "output/报告.md" not in files


def test_refuses_when_remote_has_other_commits(env, tmp_path):
    v, remote = env
    github_sync.sync_now()
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "--quiet", remote, str(other)], check=True)
    _write(str(other / "别处.md"), "别的电脑")
    subprocess.run(["git", "-C", str(other), "add", "."], check=True)
    subprocess.run(["git", "-C", str(other), "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "--quiet", "-m", "x"], check=True)
    subprocess.run(["git", "-C", str(other), "push", "--quiet", "origin", "HEAD:main"], check=True)

    _write(os.path.join(v, "Spark", "节目", "notes", "第3期.md"), "新的")
    with pytest.raises(github_sync.SyncError, match="没有推送"):
        github_sync.sync_now()
    assert "别处.md" in _remote_files(remote)


def test_hidden_folders_skipped_and_removed_if_synced_before(env, monkeypatch):
    v, remote = env
    _write(os.path.join(v, "Spark", ".organization-backups", "旧.md"), "备份")
    # 模拟以前的规则：隐藏文件夹也同步过
    current = list(github_sync.EXCLUDE)
    monkeypatch.setattr(github_sync, "EXCLUDE", [e for e in current if e != ".*/"] + [".cache/"])
    github_sync.sync_now()
    assert "Spark/.organization-backups/旧.md" in _remote_files(remote)
    monkeypatch.setattr(github_sync, "EXCLUDE", current)
    r = github_sync.sync_now()
    files = _remote_files(remote)
    assert "Spark/.organization-backups/旧.md" not in files and "Spark/节目/.manifest.json" in files
    assert r["counts"]["deleted"] == 1
    assert os.path.exists(os.path.join(v, "Spark", ".organization-backups", "旧.md"))   # 本地不动


def test_oversized_files_are_skipped(env, monkeypatch):
    v, remote = env
    monkeypatch.setattr(github_sync, "MAX_FILE_BYTES", 10)
    _write(os.path.join(v, "Spark", "节目", "大文件.md"), "x" * 50)
    r = github_sync.sync_now()
    assert r["skipped_large"] == ["Spark/节目/大文件.md"]
    assert "Spark/节目/大文件.md" not in _remote_files(remote)


def test_no_repo_configured(monkeypatch):
    monkeypatch.setattr(github_sync, "repo_url", lambda: "")
    with pytest.raises(github_sync.SyncError, match="仓库地址"):
        github_sync.sync_now()


def test_after_task_does_nothing_unless_enabled(monkeypatch):
    started = []
    monkeypatch.setattr(github_sync, "start", lambda: started.append(1))
    github_sync.after_task()
    assert github_sync.status()["scheduled"] is False and not started


def test_settings_validate_repo_url():
    settings.save({"sync": {"github_repo": "git@github.com:minxu23/spark-vault.git", "auto": "after_task"}})
    assert settings.get("sync.github_repo") == "git@github.com:minxu23/spark-vault.git"
    assert github_sync.auto_enabled()
    for bad in ("https://user:token@github.com/a/b", "not a url", "file:///tmp/x"):
        with pytest.raises(settings.SettingsError):
            settings.save({"sync": {"github_repo": bad}})
    with pytest.raises(settings.SettingsError):
        settings.save({"sync": {"auto": "hourly"}})


def test_api_sync_requires_repo():
    from werkzeug.test import Client
    c = Client(spark.application)
    r = c.post("/api/sync")
    assert r.status_code == 400
    r = c.get("/api/sync")
    assert r.status_code == 200 and r.get_json()["repo"] == ""
