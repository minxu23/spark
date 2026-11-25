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


def test_push_timeout_grows_with_upload_size():
    """几百 MB 图片在慢上行下要传半个多小时：超时按大小算，不再固定 30 分钟被掐断后从头再传。"""
    from core import github_sync as gs
    assert gs.push_timeout(0) == gs.GIT_TIMEOUT
    assert gs.push_timeout(304 * 1024 * 1024) > 90 * 60
    assert gs.push_timeout(50 * 1024 ** 3) == gs.PUSH_TIMEOUT_MAX


def _remote_log(remote):
    return subprocess.run(["git", f"--git-dir={remote}", "log", "--format=%s", "main"],
                          capture_output=True, text=True, check=True).stdout.splitlines()


def test_big_sync_pushed_in_batches(env, monkeypatch):
    """要推的多就分批提交、分批推：推上去的留在 GitHub 上，断了下次只推剩下的。"""
    v, remote = env
    github_sync.sync_now()
    monkeypatch.setattr(github_sync, "BATCH_BYTES", 1000)
    for i in range(3):
        _write(os.path.join(v, "Spark", "剪藏", "attachments", f"{i}.png"), "x" * 800)
    os.remove(os.path.join(v, "output", "报告.md"))
    r = github_sync.sync_now()
    assert r["pushed"] and r["batches"] == 3 and r["counts"] == {"added": 3, "modified": 0, "deleted": 1}
    log = _remote_log(remote)
    assert [m.endswith(f"（第 {n}/3 批）") for n, m in zip((3, 2, 1), log)] == [True] * 3
    files = _remote_files(remote)
    assert {f"Spark/剪藏/attachments/{i}.png" for i in range(3)} <= files and "output/报告.md" not in files


def test_interrupted_batches_resume_with_the_rest(env, monkeypatch):
    """第二批推送失败：第一批已经在 GitHub 上；下次同步只推剩下的。"""
    v, remote = env
    github_sync.sync_now()
    monkeypatch.setattr(github_sync, "BATCH_BYTES", 1000)
    for i in range(3):
        _write(os.path.join(v, "Spark", "剪藏", "attachments", f"{i}.png"), "x" * 800)
    real, calls = github_sync._git, []

    def flaky(args, **kw):
        if args[0] == "push":
            calls.append(1)
            if len(calls) == 2:
                raise github_sync.SyncError("git push 超过 30 分钟没完成，已放弃")
        return real(args, **kw)
    monkeypatch.setattr(github_sync, "_git", flaky)
    with pytest.raises(github_sync.SyncError):
        github_sync.sync_now()
    assert len({p for p in _remote_files(remote) if "attachments" in p}) == 1
    r = github_sync.sync_now()
    assert r["ok"] and r["counts"]["added"] == 2
    assert {p for p in _remote_files(remote) if "attachments" in p} == {f"Spark/剪藏/attachments/{i}.png" for i in range(3)}


def test_stop_kills_git_and_its_ssh_child():
    """暂停：整个进程组一起停（git 推送带起的 ssh 不能留在后台占带宽）。"""
    import time
    p = subprocess.Popen(["sh", "-c", "sleep 30 & echo $!; wait"], stdout=subprocess.PIPE, text=True,
                         start_new_session=True)
    child = int(p.stdout.readline())
    github_sync._kill_group(p)
    p.wait(timeout=5)
    for _ in range(50):
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        raise AssertionError("子进程还在")


def test_pause_mid_sync_keeps_pushed_batches(env, monkeypatch):
    """第一批推完后暂停：第一批留在 GitHub 上，记成「暂停」不是出错；再同步接着推剩下的。"""
    v, remote = env
    github_sync.sync_now()
    monkeypatch.setattr(github_sync, "BATCH_BYTES", 1000)
    for i in range(3):
        _write(os.path.join(v, "Spark", "剪藏", "attachments", f"{i}.png"), "x" * 800)
    real, pushes = github_sync._git, []

    def pausing(args, **kw):
        out = real(args, **kw)
        if args[0] == "push":
            pushes.append(1)
            github_sync._STATE["stop"] = True     # 跟点了「暂停」一样：下一条 git 命令前停下
        return out
    monkeypatch.setattr(github_sync, "_git", pausing)
    monkeypatch.setitem(github_sync._STATE, "running", True)
    github_sync._run_loop()
    last = github_sync.last_result()
    assert last["stopped"] and not last["ok"] and not github_sync._STATE["running"]
    assert not github_sync._STATE["stop"]
    assert len({p for p in _remote_files(remote) if "attachments" in p}) == 1
    monkeypatch.setattr(github_sync, "_git", real)
    assert github_sync.sync_now()["ok"]
    assert len({p for p in _remote_files(remote) if "attachments" in p}) == 3


def test_tags_index_is_synced_but_bad_copies_are_not(env):
    """标签索引 Spark/.tags.json（core/tags.py）推上去，手机拉得到；挪开的坏文件不推。"""
    v, remote = env
    _write(os.path.join(v, "Spark", ".tags.json"), '{"version": 1}')
    _write(os.path.join(v, "Spark", ".tags.bad-20261124-101010.json"), "{坏")
    github_sync.sync_now()
    files = _remote_files(remote)
    assert "Spark/.tags.json" in files and "Spark/.tags.bad-20261124-101010.json" not in files


def test_paper_pdfs_stay_off_github(env):
    """论文的 PDF 原件只走数据线：不进 GitHub；论文 Markdown、论文图片、别的文件夹的 PDF 照常。"""
    from core import clips
    v, remote = env
    assert f"/{clips.PAPERS_DIR}/attachments/*.pdf" in github_sync.EXCLUDE
    pdf = "Spark/论文/attachments/0123456789abcdef.pdf"
    _write(os.path.join(v, *pdf.split("/")), "%PDF-1.7")
    _write(os.path.join(v, "Spark", "论文", "20261125_一篇论文.md"), "论文")
    _write(os.path.join(v, "Spark", "论文", "attachments", "0123456789abcdef.jpg"), "图")
    _write(os.path.join(v, "Spark", "剪藏", "attachments", "0123456789abcdef.pdf"), "%PDF-1.7")
    github_sync.sync_now()
    files = _remote_files(remote)
    assert pdf not in files
    assert {"Spark/论文/20261125_一篇论文.md", "Spark/论文/attachments/0123456789abcdef.jpg",
            "Spark/剪藏/attachments/0123456789abcdef.pdf"} <= files
    assert os.path.exists(os.path.join(v, *pdf.split("/")))


def test_paper_pdf_pushed_before_rule_is_removed_from_repo_only(env, monkeypatch):
    """规则加上之前推上去过的论文 PDF：从仓库拿掉，本地文件不动。"""
    v, remote = env
    pdf = "Spark/论文/attachments/0123456789abcdef.pdf"
    _write(os.path.join(v, *pdf.split("/")), "%PDF-1.7")
    current = list(github_sync.EXCLUDE)
    monkeypatch.setattr(github_sync, "EXCLUDE", [e for e in current if not e.endswith("attachments/*.pdf")])
    github_sync.sync_now()
    assert pdf in _remote_files(remote)
    monkeypatch.setattr(github_sync, "EXCLUDE", current)
    github_sync.sync_now()
    assert pdf not in _remote_files(remote)
    assert os.path.exists(os.path.join(v, *pdf.split("/")))
