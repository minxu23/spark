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
# 推送的超时按要传的大小算：家里上行慢时几百 MB 图片要传半个多小时，固定 30 分钟会被掐断、下次又从头传，一直「同步中」。
# 按每秒 50 KB 估，最少 30 分钟，最多 12 小时
PUSH_BYTES_PER_SECOND = 50 * 1024
PUSH_TIMEOUT_MAX = 12 * 3600
# 一次要推的超过这么大就分批：每批单独提交、单独推。git 推送不能续传，一次推几百 MB 中途断了下次要从头传；
# 分批后推上去的就留在 GitHub 上，断了下次只推剩下的
BATCH_BYTES = 50 * 1024 * 1024

_LOCK = threading.Lock()
_STATE: dict = {"running": False, "pending": False, "timer": None, "phase": ""}


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
    remote = _rev(f"refs/remotes/origin/{BRANCH}")
    # 上次提交了没推上去（推到一半断了）：退回暂存，跟这次的改动一起按批重新提交
    if remote and _rev("HEAD") not in ("", remote):
        _git(["reset", "--soft", "--quiet", remote])
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
    pushed = False
    batches = _batches(root) if committed else []
    if len(batches) > 1:
        # 先把暂存清回上一次提交的样子，再一批批加回来
        _git(["read-tree", _rev("HEAD") or "--empty"], work_tree=root)
    for n, batch in enumerate(batches, 1):
        if len(batches) > 1:
            for i in range(0, len(batch), 500):
                part = batch[i:i + 500]
                gone = [p for st, p, _ in part if st == "D"]
                here = [p for st, p, _ in part if st != "D"]
                if gone:
                    _git(["rm", "--cached", "--quiet", "--ignore-unmatch", "--"] + gone, work_tree=root)
                if here:
                    _git(["add", "--"] + here, work_tree=root)
        msg = _message(counts) + (f"（第 {n}/{len(batches)} 批）" if len(batches) > 1 else "")
        _git(["commit", "--quiet", "-m", msg], work_tree=root)
        size = sum(b for _, _, b in batch)
        label = f"第 {n}/{len(batches)} 批，" if len(batches) > 1 else ""
        pushed = _push(root, f"推到 GitHub（{label}{_mb(size)}）" if size >= 1024 * 1024 or label else "推到 GitHub") or pushed
    if not batches:
        # 没有新改动，但以前有没推上去的提交（上面退回暂存时不会走到这里）
        pushed = _push(root, "推到 GitHub")
    head = _rev("HEAD")
    return {"ok": True, "committed": committed, "pushed": pushed, "counts": counts,
            "skipped_large": skipped, "commit": head[:7], "batches": len(batches)}


def _rev(ref: str) -> str:
    return _git(["rev-parse", "--verify", "--quiet", ref], check=False).stdout.strip()


def _message(counts: dict) -> str:
    parts = [f"新增 {counts['added']}" if counts["added"] else "",
             f"修改 {counts['modified']}" if counts["modified"] else "",
             f"删除 {counts['deleted']}" if counts["deleted"] else ""]
    return "同步笔记库：" + "，".join(p for p in parts if p) + " 个文件"


def _batches(root: str) -> list[list[tuple[str, str, int]]]:
    """暂存的改动按大小分批：[[(状态 A/M/D, 路径, 字节数)]]，每批不超过 BATCH_BYTES（单个大文件自成一批）。"""
    out = _git(["diff", "--cached", "--name-status", "--no-renames", "-z"], work_tree=root).stdout.split("\0")
    rows = []
    for i in range(0, len(out) - 1, 2):
        st, rel = out[i][:1], out[i + 1]
        try:
            size = 0 if st == "D" else os.path.getsize(os.path.join(root, rel))
        except OSError:
            size = 0
        rows.append((st, rel, size))
    batches: list[list] = []
    cur, total = [], 0
    for row in rows:
        if cur and total + row[2] > BATCH_BYTES:
            batches.append(cur)
            cur, total = [], 0
        cur.append(row)
        total += row[2]
    if cur:
        batches.append(cur)
    return batches


def _push(root: str, phase: str) -> bool:
    """HEAD 跟 GitHub 上不一样就推。返回推没推。"""
    head, remote = _rev("HEAD"), _rev(f"refs/remotes/origin/{BRANCH}")
    if not head or head == remote:
        return False
    _STATE["phase"] = phase
    _git(["push", "--quiet", "origin", f"HEAD:refs/heads/{BRANCH}"], work_tree=root,
         timeout=push_timeout(_push_bytes(root, remote)))
    _git(["fetch", "--quiet", "origin"], work_tree=root, check=False)
    return True


def _mb(n: int) -> str:
    return f"{n / 1024 / 1024:.0f} MB"


def push_timeout(size: int) -> float:
    return min(PUSH_TIMEOUT_MAX, max(GIT_TIMEOUT, size / PUSH_BYTES_PER_SECOND))


def _push_bytes(root: str, remote: str) -> int:
    """这次要推的文件一共多大（GitHub 上还没有的那些提交里改动的文件）。估超时用，读不出来当 0。"""
    if remote:
        p = _git(["diff", "--name-only", "-z", "--no-renames", remote, "HEAD"], check=False)
    else:
        p = _git(["ls-tree", "-r", "--name-only", "-z", "HEAD"], check=False)
    total = 0
    for rel in (p.stdout or "").split("\0"):
        try:
            total += os.path.getsize(os.path.join(root, rel)) if rel else 0
        except OSError:
            pass
    return total


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
            _STATE["phase"] = ""
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
        phase = _STATE["phase"] if running else ""
    return {"repo": repo_url(), "auto": auto_enabled(), "running": running, "phase": phase,
            "scheduled": scheduled, "last": last_result(), "dirs": list(SYNC_DIRS),
            "git_dir": git_dir()}
