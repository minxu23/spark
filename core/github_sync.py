"""
把笔记库里 Spark 的产物同步到 GitHub 仓库（设置页里填的 sync.github_repo）。

同步的范围：库里的 Spark/（会议、播客、信息跟进）和 output/（笔记洞察的报告），
跟阅读页浏览的范围一致。库里别的东西（自己写的笔记、.obsidian）不碰。
点开头的文件夹（.cache/ 和整理时留的备份）、logs/ 不同步：字幕、模型调用缓存占了一半体积，丢了能重新生成；日志只对排错有用。

git 仓库本身（.git 那一套）放在 ~/.spark/vault-sync.git，工作区直接指向笔记库，
所以库里不会多出 .git 目录，也不用另外复制一份 200 多 MB 的文件。

只做「本机 → GitHub」单向推送：GitHub 上有这台电脑没有的提交（比如在别的电脑同步过）
就不推、报出来，绝不强推覆盖。用的是本机 git 自己的登录方式（SSH key 或钥匙串），
这里不存任何 token；仓库地址里带了账号密码的直接拒收（见 core/settings.py）。
"""

from __future__ import annotations

import os
import subprocess
import threading
import time

from core import atomic
from core import settings as core_settings
from core import vault

# 同步哪些顶层目录（相对笔记库）
SYNC_DIRS = (vault.SPARK_DIRNAME, vault.REPORTS_DIRNAME)

EXCLUDE = [
    # 只要 SYNC_DIRS 里的，库里其它东西一概不看
    "/*",
    *[f"!/{d}/" for d in SYNC_DIRS],
    # 点开头的文件夹（.cache、整理时留的 .organization-backups / .summary-backups……）都不同步；
    # 只匹配文件夹，.manifest.json 这类点开头的文件照常同步
    ".*/",
    "logs/",
    "*.log",
    ".DS_Store",
    "*.tmp",
    "*.partial",
]

BRANCH = "main"
# GitHub 单个文件超过 100MB 会拒收整次推送；超过这个大小的先不同步、在结果里列出来
MAX_FILE_BYTES = 95 * 1024 * 1024
# 任务跑完后等这么久再同步：几个任务前后脚结束时合成一次提交
AFTER_TASK_DELAY = 60.0
GIT_TIMEOUT = 30 * 60

_LOCK = threading.Lock()
_STATE: dict = {"running": False, "pending": False, "timer": None}


class SyncError(RuntimeError):
    pass


def git_dir() -> str:
    """跟设置文件放一起；测试用 SPARK_SETTINGS_FILE 指到临时目录时它也跟着走。"""
    return os.path.join(os.path.dirname(core_settings.settings_path()), "vault-sync.git")


def _status_path() -> str:
    return os.path.join(git_dir(), "spark-sync-status.json")


def repo_url() -> str:
    return core_settings.get("sync.github_repo") or ""


def auto_enabled() -> bool:
    return core_settings.get("sync.auto") == "after_task" and bool(repo_url())


def _git(args: list[str], *, work_tree: str = "", check: bool = True,
         timeout: float = GIT_TIMEOUT) -> subprocess.CompletedProcess:
    cmd = ["git", f"--git-dir={git_dir()}"]
    if work_tree:
        cmd.append(f"--work-tree={work_tree}")
    env = dict(os.environ)
    # 没有 SSH key / 没登录时直接失败，不在后台线程里等一个永远没人回答的密码提示
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
    try:
        p = subprocess.run(cmd + args, capture_output=True, text=True, env=env, timeout=timeout)
    except FileNotFoundError as e:
        raise SyncError("找不到 git 命令，请先安装 Xcode 命令行工具（xcode-select --install）") from e
    except subprocess.TimeoutExpired as e:
        raise SyncError(f"git {args[0]} 超过 {int(timeout // 60)} 分钟没完成，已放弃") from e
    if check and p.returncode != 0:
        raise SyncError(f"git {args[0]} 失败：{(p.stderr or p.stdout).strip()[-600:]}")
    return p


def _ensure_repo(root: str, url: str) -> None:
    gd = git_dir()
    if not os.path.isfile(os.path.join(gd, "HEAD")):
        os.makedirs(gd, exist_ok=True)
        _git(["init", "--quiet", f"--initial-branch={BRANCH}"])
    # 每次都写一遍：笔记库位置、仓库地址、排除规则改了都跟着变
    _git(["config", "core.worktree", root])
    _git(["config", "core.quotepath", "false"])
    _git(["config", "core.precomposeunicode", "true"])
    if not _git(["config", "user.name"], check=False).stdout.strip():
        _git(["config", "user.name", "Spark"])
        _git(["config", "user.email", "spark@localhost"])
    remotes = _git(["remote"]).stdout.split()
    if "origin" in remotes:
        _git(["remote", "set-url", "origin", url])
    else:
        _git(["remote", "add", "origin", url])
    os.makedirs(os.path.join(gd, "info"), exist_ok=True)
    with open(os.path.join(gd, "info", "exclude"), "w", encoding="utf-8") as f:
        f.write("# 由 Spark 生成（core/github_sync.py），每次同步都会重写\n" + "\n".join(EXCLUDE) + "\n")


def _skip_oversized(root: str) -> list[str]:
    """已暂存的文件里超过 MAX_FILE_BYTES 的撤出暂存，返回它们的相对路径。"""
    out = _git(["diff", "--cached", "--name-only", "--diff-filter=AM", "-z"], work_tree=root).stdout
    big = [p for p in out.split("\0") if p and os.path.getsize(os.path.join(root, p)) > MAX_FILE_BYTES]
    if big:
        _git(["rm", "--cached", "--quiet", "--"] + big, work_tree=root)
    return big


def _counts(root: str) -> dict:
    out = _git(["diff", "--cached", "--name-status", "-z"], work_tree=root).stdout.split("\0")
    c = {"added": 0, "modified": 0, "deleted": 0}
    i = 0
    while i < len(out) - 1:
        status = out[i][:1]
        i += 3 if status in ("R", "C") else 2
        c[{"A": "added", "D": "deleted"}.get(status, "modified")] += 1
    return c


def _remote_ahead(root: str) -> bool:
    """GitHub 上的 main 有没有本机没有的提交。远端还是空仓库时为 False。"""
    _git(["fetch", "--quiet", "origin"], work_tree=root)
    if _git(["rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{BRANCH}"], check=False).returncode:
        return False
    if _git(["rev-parse", "--verify", "--quiet", "HEAD"], check=False).returncode:
        return True    # 本机还没提交过、远端已经有内容：不是这台电脑建的，别动它
    return _git(["merge-base", "--is-ancestor", f"origin/{BRANCH}", "HEAD"], check=False).returncode != 0


def sync_now() -> dict:
    """同步一次（阻塞）。返回结果，同时写进状态文件。"""
    url = repo_url()
    if not url:
        raise SyncError("还没在设置里填 GitHub 仓库地址")
    root = vault.vault_root()
    if not os.path.isdir(root):
        raise SyncError(f"笔记库不在：{root}")
    if not any(os.path.isdir(os.path.join(root, d)) for d in SYNC_DIRS):
        raise SyncError(f"笔记库里没有 {' / '.join(SYNC_DIRS)}，没有可同步的内容")

    _ensure_repo(root, url)
    if _remote_ahead(root):
        raise SyncError("GitHub 上有这台电脑没有的提交（可能在别的电脑同步过，或在网页上改过），"
                        "这次没有推送，以免覆盖。")
    _git(["add", "--all", "--"] + [d for d in SYNC_DIRS if os.path.isdir(os.path.join(root, d))],
         work_tree=root)
    # 整个 Spark/ 或 output/ 没了的话上面的路径给不进去，删除靠这一步记下
    _git(["add", "--update"], work_tree=root)
    # 以前同步过、现在按规则不该同步的（比如后来才排除的隐藏文件夹）：从仓库里拿掉，本地文件不动
    ignored = _git(["ls-files", "--cached", "--ignored", "--exclude-standard", "-z"], work_tree=root).stdout
    stale = [p for p in ignored.split("\0") if p]
    for i in range(0, len(stale), 500):
        _git(["rm", "--cached", "--quiet", "--"] + stale[i:i + 500], work_tree=root)
    skipped = _skip_oversized(root)
    counts = _counts(root)
    committed = any(counts.values())
    if committed:
        parts = [f"新增 {counts['added']}" if counts["added"] else "",
                 f"修改 {counts['modified']}" if counts["modified"] else "",
                 f"删除 {counts['deleted']}" if counts["deleted"] else ""]
        msg = "同步笔记库：" + "，".join(p for p in parts if p) + " 个文件"
        _git(["commit", "--quiet", "-m", msg], work_tree=root)

    head = _git(["rev-parse", "--verify", "--quiet", "HEAD"], check=False).stdout.strip()
    pushed = False
    if head:
        remote = _git(["rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{BRANCH}"],
                      check=False).stdout.strip()
        if remote != head:
            _git(["push", "--quiet", "origin", f"HEAD:refs/heads/{BRANCH}"], work_tree=root)
            _git(["fetch", "--quiet", "origin"], work_tree=root, check=False)
            pushed = True
    return {"ok": True, "committed": committed, "pushed": pushed, "counts": counts,
            "skipped_large": skipped, "commit": head[:7]}


def _record(result: dict) -> None:
    result = dict(result, finished_at=time.time(), repo=repo_url())
    try:
        os.makedirs(git_dir(), exist_ok=True)
        atomic.write_json(_status_path(), result, indent=2)
    except OSError:
        pass


def last_result() -> dict:
    import json
    try:
        with open(_status_path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _run_loop() -> None:
    while True:
        try:
            result = sync_now()
        except SyncError as e:
            result = {"ok": False, "error": str(e)}
        except Exception as e:  # noqa: BLE001
            result = {"ok": False, "error": f"意外错误：{e}"}
        _record(result)
        with _LOCK:
            if not _STATE["pending"]:
                _STATE["running"] = False
                return
            _STATE["pending"] = False   # 同步途中又有任务跑完，再来一轮


def start() -> bool:
    """在后台开始一次同步。已经在同步就记一笔，等这轮完了再来一轮。返回是否新开了线程。"""
    with _LOCK:
        if _STATE["timer"] is not None:
            _STATE["timer"].cancel()
            _STATE["timer"] = None
        if _STATE["running"]:
            _STATE["pending"] = True
            return False
        _STATE["running"] = True
    threading.Thread(target=_run_loop, daemon=True).start()
    return True


def after_task() -> None:
    """任务结束时调用：开了「任务跑完自动同步」就在 AFTER_TASK_DELAY 秒后同步一次；
    这段时间里又有任务结束，就从头再等。没开或没填仓库地址什么都不做。"""
    try:
        if not auto_enabled():
            return
    except Exception:  # noqa: BLE001  设置读不出来不能影响任务收尾
        return
    with _LOCK:
        if _STATE["timer"] is not None:
            _STATE["timer"].cancel()
        t = threading.Timer(AFTER_TASK_DELAY, start)
        t.daemon = True
        _STATE["timer"] = t
        t.start()


def status() -> dict:
    with _LOCK:
        running = _STATE["running"]
        scheduled = _STATE["timer"] is not None
    return {"repo": repo_url(), "auto": auto_enabled(), "running": running,
            "scheduled": scheduled, "last": last_result(), "dirs": list(SYNC_DIRS),
            "git_dir": git_dir()}
