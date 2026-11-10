"""
手机上添加的任务：记在同步仓库（设置里的 sync.github_repo）的 GitHub Issue 里，
电脑上的 Spark 定时（间隔可设）或在设置页手动去看，按现有的接口跑，进度和结果写回 Issue。

为什么用 Issue 不用仓库里的文件：笔记同步（core/github_sync.py）只从本机往上推，
GitHub 上多出本机没有的提交就不推了。手机往仓库里写任务文件会把同步卡住；Issue 不碰仓库内容。

一个任务 = 一个带 spark-task 标签的 Issue，正文里一段

    ```spark-task
    {"type": "link", "url": "https://www.youtube.com/watch?v=…", "dest": {"kind": "show", "folder": "Dwarkesh"}}
    ```

五类任务：
- link：处理一个链接。dest 是放进已有的文件夹（show，Spark/ 下的文件夹名）、新建节目（new_series）
  或当成一场会议（summit）。
- check：检查几个订阅（按订阅名或文件夹名）有没有新内容，新的直接处理，每个订阅最多 max_new 条。
- report：笔记洞察按主题自动选材料出报告。
- review：复盘（阅读复盘 / 主题来龙去脉 / 观点检验，见 core/review.py）。
- save：手机上自己生成好的复盘或报告（手机里填了模型 Key 时），存进笔记库 output/ 下；
  或者手机上存的剪藏（Spark/剪藏/ 下，带图片的原地址，这边下图片、重写索引，见 core/clips.py）。
  只认 output/ 下的 .md；同名文件已经有了、内容一样就算存过，不一样就另存成 _2、_3。

状态用标签表示：没有状态标签 = 等着；「进行中」；「失败」。完成就评论结果并关掉 Issue。
手机上点「重试」= 去掉「失败」标签，下一轮会重新接手。

安全：只接你自己账号开的 Issue（和令牌是同一个账号），只认上面三类任务、http(s) 链接、
Spark/ 下真实存在的文件夹名。令牌按 core/keys 的规矩读（环境变量 SPARK_GITHUB_TOKEN 或
~/.spark/keys/github.key），只放在请求头里，不写进日志、Issue 或设置文件。
"""

from __future__ import annotations

import json
import os
import shutil
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Optional

from core import atomic
from core import clips
from core import keys
from core import settings as core_settings
from core import vault

TASK_LABEL = "spark-task"
RUNNING_LABEL = "进行中"
FAILED_LABEL = "失败"
TOKEN_PROVIDER = "github"
# 定时检查的间隔（分钟）可选这几档，默认 15；设置页切换后台线程最多这么久会注意到
POLL_MINUTES_CHOICES = (5, 15, 30, 60)
DEFAULT_POLL_MINUTES = 15
SETTINGS_RECHECK_SECONDS = 60
JOB_POLL_SECONDS = 5
# 进度评论最多多久改一次，免得一个任务往 Issue 里刷几百次
PROGRESS_EDIT_SECONDS = 30
# 正在跑的任务被别的重任务占着名额（429）时，过多久再试
BUSY_RETRY_SECONDS = 120
MAX_NEW_PER_SUB = 20
DEFAULT_NEW_PER_SUB = 5
MAX_SUBS_PER_TASK = 20
# 手机交来存进笔记库的文件：最多这么多字、路径最多这么长
MAX_SAVE_CHARS = 200_000
MAX_SAVE_PATH = 300
MAX_EPISODE_CHARS = 1_500_000      # 手机上写好的一期节目：文字记录能有好几个小时
REPORT_PICK = 15
API = "https://api.github.com"

_BLOCK_RE = re.compile(r"```spark-task[ \t]*\n(.*?)\n```", re.S)
# 进度评论开头的机器可读状态，手机端解析这一行显示进度
STATUS_PREFIX = "<!-- spark-status "

# call(method, path, json) -> (status, body)：在本进程里调 Spark 自己的接口（见 spark.py）
LocalCall = Callable[[str, str, Optional[dict]], "tuple[int, Any]"]


class TaskError(Exception):
    """任务本身有问题或跑失败了：原因写进 Issue。"""


class Busy(Exception):
    """重任务名额满了：这次先不接，过一会儿再来。"""


# ---------------------------------------------------------------- 设置与仓库

def mode() -> str:
    """"" = 不接；manual = 只在设置页点「现在检查」时看；auto = 定时看。"""
    m = core_settings.get("sync.remote_tasks") or ""
    return m if m in ("manual", "auto") and repo_slug() else ""


def enabled() -> bool:
    return bool(mode())


def poll_minutes() -> int:
    try:
        n = int(core_settings.get("sync.remote_tasks_minutes") or DEFAULT_POLL_MINUTES)
    except (TypeError, ValueError):
        n = DEFAULT_POLL_MINUTES
    return n if n in POLL_MINUTES_CHOICES else DEFAULT_POLL_MINUTES


def repo_slug() -> str:
    """从同步仓库地址里取 owner/name；不是 GitHub 的地址返回空串。"""
    url = core_settings.get("sync.github_repo") or ""
    m = re.match(r"^(?:git@github\.com:|ssh://git@github\.com(?::\d+)?/|https://github\.com/)"
                 r"([\w.-]+)/([\w.-]+?)(?:\.git)?/?$", url)
    return f"{m.group(1)}/{m.group(2)}" if m else ""


def token() -> str:
    return keys.resolve(TOKEN_PROVIDER)


# ---------------------------------------------------------------- GitHub

class GitHub:
    def __init__(self, slug: str, tok: str, opener=None):
        self.slug = slug
        self._tok = tok
        self._open = opener or urllib.request.urlopen

    def _req(self, method: str, path: str, body: Optional[dict] = None):
        url = API + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self._tok}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "Spark",
            **({"Content-Type": "application/json"} if data is not None else {}),
        })
        try:
            with self._open(req, timeout=30) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            # 只带状态码和 GitHub 给的说明，不带请求头（里面有令牌）
            try:
                msg = json.loads(e.read() or b"{}").get("message") or ""
            except ValueError:
                msg = ""
            raise TaskError(f"GitHub 返回 {e.code}{'：' + msg if msg else ''}") from None
        except (urllib.error.URLError, OSError) as e:
            raise TaskError(f"连不上 GitHub：{getattr(e, 'reason', e)}") from None
        return json.loads(raw) if raw else None

    def me(self) -> str:
        return (self._req("GET", "/user") or {}).get("login") or ""

    def open_tasks(self) -> list[dict]:
        q = urllib.parse.urlencode({"labels": TASK_LABEL, "state": "open", "per_page": 50,
                                    "sort": "created", "direction": "asc"})
        return [i for i in self._req("GET", f"/repos/{self.slug}/issues?{q}") or []
                if "pull_request" not in i]

    def issues_with_label(self, label: str) -> list[dict]:
        q = urllib.parse.urlencode({"labels": label, "state": "all", "per_page": 20})
        return [i for i in self._req("GET", f"/repos/{self.slug}/issues?{q}") or [] if "pull_request" not in i]

    def comments(self, number: int) -> list[dict]:
        out, page = [], 1
        while True:
            batch = self._req("GET", f"/repos/{self.slug}/issues/{number}/comments?per_page=100&page={page}") or []
            out += batch
            if len(batch) < 100 or page >= 50:
                return out
            page += 1

    def comment(self, number: int, body: str) -> int:
        return (self._req("POST", f"/repos/{self.slug}/issues/{number}/comments", {"body": body}) or {}).get("id", 0)

    def edit_comment(self, comment_id: int, body: str) -> None:
        self._req("PATCH", f"/repos/{self.slug}/issues/comments/{comment_id}", {"body": body})

    def add_label(self, number: int, label: str) -> None:
        self._req("POST", f"/repos/{self.slug}/issues/{number}/labels", {"labels": [label]})

    def remove_label(self, number: int, label: str) -> None:
        try:
            self._req("DELETE", f"/repos/{self.slug}/issues/{number}/labels/{urllib.parse.quote(label)}")
        except TaskError:
            pass   # 本来就没有这个标签

    def close(self, number: int) -> None:
        self._req("PATCH", f"/repos/{self.slug}/issues/{number}", {"state": "closed", "state_reason": "completed"})


# ---------------------------------------------------------------- 解析任务

def _labels(issue: dict) -> set[str]:
    return {(lb.get("name") if isinstance(lb, dict) else str(lb)) for lb in issue.get("labels") or []}


def _show_folder(name: str) -> str:
    """Spark/ 下真实存在的一个文件夹（只认一层，不认 .. 和路径分隔符）。返回绝对路径或空串。"""
    if not isinstance(name, str) or not name or name.startswith(".") or "/" in name or "\\" in name:
        return ""
    path = os.path.join(vault.spark_dir(), name)
    return path if os.path.isdir(path) else ""


def parse(body: str) -> dict:
    """Issue 正文 → 整理过的任务。不合规就抛 TaskError（原因会写回 Issue）。"""
    m = _BLOCK_RE.search(body or "")
    if not m:
        raise TaskError("正文里没有 ```spark-task 代码块")
    try:
        raw = json.loads(m.group(1))
    except ValueError:
        raise TaskError("spark-task 代码块不是合法的 JSON") from None
    if not isinstance(raw, dict):
        raise TaskError("spark-task 代码块应该是一个对象")
    return parse_task(raw)


def parse_task(raw: dict) -> dict:
    """任务对象 → 整理过的任务（Issue 正文和数据线同步的发件箱共用这一套校验）。"""
    if not isinstance(raw, dict):
        raise TaskError("任务应该是一个对象")
    kind = raw.get("type")
    length = raw.get("summary_length") or ""
    if length not in ("", "short", "medium", "long"):
        raise TaskError("summary_length 只能是 short / medium / long")

    if kind == "link":
        url = raw.get("url")
        if not isinstance(url, str) or not re.match(r"^https?://[^\s/]+\S*$", url.strip()) or len(url) > 2000:
            raise TaskError("url 应该是 http:// 或 https:// 开头的链接")
        dest = raw.get("dest") or {"kind": "new_series"}
        if not isinstance(dest, dict) or dest.get("kind") not in ("show", "new_series", "summit"):
            raise TaskError("dest.kind 只能是 show / new_series / summit")
        out = {"type": "link", "url": url.strip(), "dest": {"kind": dest["kind"]}, "summary_length": length}
        if dest["kind"] == "show":
            folder = _show_folder(dest.get("folder"))
            if not folder:
                raise TaskError(f"Spark/ 下没有文件夹「{dest.get('folder')}」")
            out["dest"]["folder"] = folder
        return out

    if kind == "check":
        subs = raw.get("subscriptions")
        if not isinstance(subs, list) or not subs or len(subs) > MAX_SUBS_PER_TASK \
                or not all(isinstance(s, str) and 0 < len(s) <= 200 for s in subs):
            raise TaskError(f"subscriptions 应该是 1 到 {MAX_SUBS_PER_TASK} 个订阅名")
        n = raw.get("max_new", DEFAULT_NEW_PER_SUB)
        if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= MAX_NEW_PER_SUB:
            raise TaskError(f"max_new 应该是 1 到 {MAX_NEW_PER_SUB} 的整数")
        return {"type": "check", "subscriptions": list(dict.fromkeys(subs)), "max_new": n, "summary_length": length}

    if kind == "report":
        topic = raw.get("topic")
        if not isinstance(topic, str) or not topic.strip() or len(topic) > 200:
            raise TaskError("topic 应该是 1 到 200 字的主题")
        scope = raw.get("scope") or ""
        if scope and not _show_folder(scope):
            raise TaskError(f"Spark/ 下没有文件夹「{scope}」")
        return {"type": "report", "topic": topic.strip(), "scope": scope}

    if kind == "review":
        rk = raw.get("kind")
        if rk not in ("reading", "topic", "claims", "clips", "picked"):
            raise TaskError("复盘的 kind 只能是 reading / topic / claims / clips / picked")
        out = {"type": "review", "kind": rk}
        if rk == "picked":
            paths = raw.get("paths")
            if not isinstance(paths, list) or not 2 <= len(paths) <= clips.MAX_PICKED \
                    or not all(isinstance(p, str) and clips.is_note_path(p) for p in paths):
                raise TaskError(f"综合小结的 paths 应该是 2 到 {clips.MAX_PICKED} 篇 Spark/ 或 output/ 下的文章")
            out["paths"] = list(dict.fromkeys(paths))
        topic = raw.get("topic") or ""
        if not isinstance(topic, str) or len(topic) > 200:
            raise TaskError("topic 应该是 200 字以内的主题")
        if rk == "topic" and not topic.strip():
            raise TaskError("主题来龙去脉要填一个主题")
        if topic.strip():
            out["topic"] = topic.strip()
        for key, lo, hi, default in (("days", 1, 366, 7), ("months", 1, 60, 6)):
            v = raw.get(key, default)
            if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
                raise TaskError(f"{key} 应该是 {lo} 到 {hi} 的整数")
            out[key] = v
        return out

    if kind == "save":
        path, content = raw.get("path"), raw.get("content")
        if not isinstance(path, str) or not _save_path_ok(path):
            raise TaskError("path 应该是 output/ 下的 .md 文件，或者 Spark/剪藏/ 下的一篇")
        if not isinstance(content, str) or not content.strip() or len(content) > MAX_SAVE_CHARS:
            raise TaskError(f"content 应该是 1 到 {MAX_SAVE_CHARS} 字的正文")
        out = {"type": "save", "path": path, "content": content}
        if clips.is_clip_path(path):
            images = raw.get("images") or {}
            if not isinstance(images, dict) or len(images) > clips.MAX_IMAGES \
                    or not all(clips.image_key_ok(k, v) for k, v in images.items()):
                raise TaskError("images 应该是 Spark/剪藏/attachments/ 下的图片名 → 原图地址")
            out["images"] = images
            # 剪藏是手机上那一份说了算（比如后来补写了摘要）：同名的直接覆盖
            out["replace"] = bool(raw.get("replace"))
        elif path.startswith(REVIEW_DIR + "/"):
            # 手机上改了复盘的标题、写了追问：同名的直接覆盖
            out["replace"] = bool(raw.get("replace"))
        return out

    if kind == "track_item":
        return _parse_track_item(raw)

    if kind == "episode_item":
        return _parse_episode_item(raw)

    if kind == "delete":
        path = raw.get("path")
        if not isinstance(path, str) or not _delete_path_ok(path):
            raise TaskError("只能删 output/复盘/ 下的 .md 和剪藏")
        return {"type": "delete", "path": path}

    raise TaskError("type 只能是 link / check / report / review / save / delete / track_item / episode_item")


TRACK_DIR = f"{vault.SPARK_DIRNAME}/信息跟进"


def _parse_track_item(raw: dict) -> dict:
    """手机上检查订阅写好的一条（见 apps/mobile-ios/Spark/FeedRunner.swift）。"""
    folder, name, content = raw.get("folder"), raw.get("name"), raw.get("content")
    entry, summary = raw.get("entry"), raw.get("summary")
    parts = folder.split("/") if isinstance(folder, str) else []
    if not isinstance(folder, str) or not folder.startswith(TRACK_DIR + "/") or len(parts) != 3 \
            or not _save_part_ok(parts[2]):
        raise TaskError("folder 应该是 Spark/信息跟进/ 下的一个订阅文件夹")
    if not isinstance(name, str) or not name.endswith(".md") or not _save_part_ok(name):
        raise TaskError("name 应该是订阅文件夹里的一个 .md 文件")
    if not isinstance(content, str) or not content.strip() or len(content) > MAX_SAVE_CHARS:
        raise TaskError(f"content 应该是 1 到 {MAX_SAVE_CHARS} 字的正文")
    if not isinstance(entry, dict) or not isinstance(summary, dict):
        raise TaskError("entry 和 summary 应该是对象")
    eid, date = str(entry.get("id") or ""), str(entry.get("publish_date") or "")
    if not re.fullmatch(r"[0-9a-f]{16}", eid) or (date and not re.fullmatch(r"\d{8}", date)):
        raise TaskError("entry 的 id 或发布日期不对")
    clean = {"id": eid, "title": str(entry.get("title") or "")[:500], "url": str(entry.get("url") or "")[:2000],
             "source_type": "rss"}
    if date:
        clean["publish_date"] = date
    return {"type": "track_item", "folder": folder, "name": name, "content": content, "entry": clean,
            "summary": {"tldr": str(summary.get("tldr") or ""), "body": str(summary.get("body") or "")}}


def _parse_episode_item(raw: dict) -> dict:
    """手机上写好的一期节目（见 apps/mobile-ios/Spark/FeedRunner.swift）：结构化的内容，文件由电脑自己的模板生成。"""
    folder, entry, summary = raw.get("folder"), raw.get("entry"), raw.get("summary")
    parts = folder.split("/") if isinstance(folder, str) else []
    if len(parts) != 2 or parts[0] != vault.SPARK_DIRNAME or not _save_part_ok(parts[1]) or parts[1] in ("信息跟进", "剪藏"):
        raise TaskError("folder 应该是 Spark/ 下的一个节目文件夹")
    if not isinstance(entry, dict) or not isinstance(summary, dict):
        raise TaskError("entry 和 summary 应该是对象")
    eid, date = str(entry.get("id") or ""), str(entry.get("publish_date") or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", eid) or not re.fullmatch(r"\d{8}", date):
        raise TaskError("entry 的 id 或播出日期不对")
    source = entry.get("source_type")
    if source not in (None, "substack", "rss") or entry.get("sub_kind") not in (None, "manual", "auto"):
        raise TaskError("entry 的来源不对")
    paragraphs = raw.get("paragraphs")
    if not isinstance(paragraphs, list) or not paragraphs \
            or not all(isinstance(p, list) and len(p) == 2 and isinstance(p[0], (int, float)) and isinstance(p[1], str)
                       for p in paragraphs) or sum(len(p[1]) for p in paragraphs) > MAX_EPISODE_CHARS:
        raise TaskError("paragraphs 应该是 [开始秒数, 文字] 的列表")
    def words(v) -> list:
        return [str(x)[:200] for x in v][:30] if isinstance(v, list) else []
    speech = raw.get("speech")   # 旧版 App 不带这一项
    if speech is not None and not (isinstance(speech, dict) and isinstance(speech.get("text"), str)
                                   and speech["text"].strip() and speech.get("mode") in ("bilingual", "zh", "original")
                                   and len(speech["text"]) <= MAX_EPISODE_CHARS):
        raise TaskError("speech 应该是 {text, mode}")
    return {"type": "episode_item", "folder": folder,
            "entry": {"id": eid, "title": str(entry.get("title") or "")[:500], "url": str(entry.get("url") or "")[:2000],
                      "duration": int(entry.get("duration") or 0), "is_raw_session": False, "source_type": source,
                      "publish_date": date, "sub_kind": entry.get("sub_kind")},
            "summary": {"tldr": str(summary.get("tldr") or ""), "body": str(summary.get("body") or ""),
                        "guests": words(summary.get("guests")), "topics": words(summary.get("topics"))},
            "paragraphs": [(float(t), x) for t, x in paragraphs], "lang": str(raw.get("lang") or "")[:20],
            "speech": {"text": speech["text"], "mode": speech["mode"]} if speech else None}


REVIEW_DIR = f"{vault.REPORTS_DIRNAME}/复盘"


def _delete_path_ok(path: str) -> bool:
    """手机上能删的：output/复盘/ 下直接的一篇 .md，和一篇剪藏（Spark/剪藏/ 下直接的 .md，不含索引）。"""
    if clips.is_clip_path(path):
        return ".." not in path.split("/")
    return path.startswith(REVIEW_DIR + "/") and "/" not in path[len(REVIEW_DIR) + 1:] and _save_path_ok(path)


def trash_dir() -> str:
    return os.path.expanduser("~/.Trash")


def delete_file(task: dict) -> str:
    """手机上删掉的复盘或剪藏：移进废纸篓（不直接删，想要回来还能从废纸篓拖出来）。已经没有了也算完成。
    剪藏删了要重写索引；文章用过的图片留在 attachments/ 里不动。"""
    root = os.path.realpath(vault.vault_root())
    rel = task["path"]
    full = os.path.realpath(os.path.join(root, *rel.split("/")))
    if os.path.commonpath([full, root]) != root or not _delete_path_ok(rel):
        raise TaskError("路径不在笔记库的复盘或剪藏文件夹里")
    if not os.path.isfile(full):
        return f"笔记库里已经没有了：`{rel}`"
    trash = trash_dir()
    os.makedirs(trash, exist_ok=True)
    stem, ext = os.path.splitext(os.path.basename(full))
    dest = os.path.join(trash, stem + ext)
    n = 2
    while os.path.exists(dest):
        dest = os.path.join(trash, f"{stem} {n}{ext}")
        n += 1
    shutil.move(full, dest)
    if clips.is_clip_path(rel):
        clips.rebuild_index(root)
    return f"移进了废纸篓：`{rel}`"


def _save_part_ok(part: str) -> bool:
    """路径里的一段：不空、不点开头、没有斜杠和控制字符。"""
    return bool(part) and not part.startswith(".") and part.strip() == part and len(part) <= MAX_SAVE_PATH \
        and "/" not in part and "\\" not in part and not any(ord(ch) < 32 for ch in part)


def save_episode_item(task: dict) -> str:
    """手机上写好的一期节目存进笔记库，记进那个节目的 manifest。"""
    from apps.summit2md import tracking
    root = os.path.realpath(vault.vault_root())
    folder = os.path.realpath(os.path.join(root, *task["folder"].split("/")))
    if os.path.commonpath([folder, root]) != root:
        raise TaskError("路径不在笔记库里")
    return tracking.save_episode(folder, task["entry"], task["summary"], task["paragraphs"], task["lang"],
                                 speech=task.get("speech"))


def save_track_item(task: dict) -> str:
    """手机上写好的一条信息跟进存进笔记库，记进那个订阅的 manifest。"""
    from apps.summit2md import tracking   # 这里才用：不让任务队列一启动就把整个处理流程加载进来
    root = os.path.realpath(vault.vault_root())
    folder = os.path.realpath(os.path.join(root, *task["folder"].split("/")))
    if os.path.commonpath([folder, root]) != root:
        raise TaskError("路径不在笔记库里")
    return tracking.save_item(folder, task["entry"], task["summary"], task["name"], task["content"])


def _save_path_ok(path: str) -> bool:
    """output/ 下的 .md 或 Spark/剪藏/ 下的一篇，相对路径、不带 .. 和点开头的文件夹、不带反斜杠和控制字符。"""
    if len(path) > MAX_SAVE_PATH or "\\" in path:
        return False
    if not path.startswith(vault.REPORTS_DIRNAME + "/") and not clips.is_clip_path(path):
        return False
    parts = path.split("/")
    if not path.endswith(".md") or any(not p or p.startswith(".") or p.strip() != p for p in parts):
        return False
    return not any(ord(ch) < 32 for ch in path)


def save_file(task: dict) -> str:
    """把手机生成好的文件写进笔记库。已经有同名文件：内容一样就不动，不一样另存成 _2、_3……"""
    root = os.path.realpath(vault.vault_root())
    rel = task["path"]
    if task.get("replace") and clips.is_clip_path(rel):
        return _save_clip(task, root)
    if task.get("replace") and rel.startswith(REVIEW_DIR + "/"):
        full = os.path.realpath(os.path.join(root, *rel.split("/")))
        if os.path.commonpath([full, root]) != root:
            raise TaskError("路径不在笔记库里")
        os.makedirs(os.path.dirname(full), exist_ok=True)
        atomic.write_text(full, task["content"])
        return f"更新了笔记库里的：`{rel}`"
    stem, ext = os.path.splitext(rel)
    for i in range(1, 100):
        cand = rel if i == 1 else f"{stem}_{i}{ext}"
        full = os.path.realpath(os.path.join(root, *cand.split("/")))
        if os.path.commonpath([full, root]) != root:
            raise TaskError("路径不在笔记库里")
        if os.path.exists(full):
            try:
                with open(full, encoding="utf-8") as f:
                    if f.read() == task["content"]:
                        return f"笔记库里已经有了：`{cand}`"
            except OSError:
                pass
            continue
        os.makedirs(os.path.dirname(full), exist_ok=True)
        atomic.write_text(full, task["content"])
        return f"存进了笔记库：`{cand}`"
    raise TaskError("同名文件太多了，没存")


def _save_clip(task: dict, root: str) -> str:
    rel = task["path"]
    full = os.path.realpath(os.path.join(root, *rel.split("/")))
    if os.path.commonpath([full, root]) != root:
        raise TaskError("路径不在笔记库里")
    os.makedirs(os.path.dirname(full), exist_ok=True)
    atomic.write_text(full, task["content"])
    got, failed = clips.download_images(task.get("images") or {}, root)
    clips.rebuild_index(root)
    tail = f"，图片下了 {got} 张" if got else ""
    tail += f"（{failed} 张没下成）" if failed else ""
    return f"剪藏存进了笔记库：`{rel}`{tail}"


# ---------------------------------------------------------------- 状态评论

def status_comment(state: str, text: str, **extra) -> str:
    meta = json.dumps({"state": state, **extra}, ensure_ascii=False)
    return f"{STATUS_PREFIX}{meta} -->\n{text}"


class Progress:
    """一个任务一条进度评论，跑的过程中隔一会儿改一次。"""

    def __init__(self, gh: GitHub, number: int):
        self.gh, self.number = gh, number
        self.comment_id = 0
        self._last = 0.0

    def update(self, text: str, *, force: bool = False, **extra) -> None:
        now = time.time()
        if not force and now - self._last < PROGRESS_EDIT_SECONDS:
            return
        self._last = now
        body = status_comment("running", text, **extra)
        try:
            if self.comment_id:
                self.gh.edit_comment(self.comment_id, body)
            else:
                self.comment_id = self.gh.comment(self.number, body)
        except TaskError:
            pass   # 进度写不上去不影响任务本身


# ---------------------------------------------------------------- 跑任务

_STAGE_TEXT = {"subtitles": "下载字幕", "subtitle": "下载字幕", "transcript": "整理文字记录", "speech": "整理稿",
               "summary": "写小结", "overall": "写总结", "overall_summary": "写总结", "speakers": "识别发言人",
               "digest": "读材料", "compose": "写报告", "skip": "跳过已处理的", "overview": "列出要处理的",
               "paused": "暂停了", "queued": "排队", "rename": "整理文件名", "repair": "检查已有的文件"}


def _progress_parts(st: dict) -> tuple[str, str]:
    """这一步在做什么、在做哪一条。取任务日志的最后一行（「[2/18] 下载字幕：标题」「  正在整理演讲稿：标题」），
    没有日志或者那行不是在说某一条（列清单），就用阶段名。"""
    stage = st.get("stage") or ""
    fallback = _STAGE_TEXT.get(stage, "") or "进行中"
    log = st.get("log") if isinstance(st.get("log"), list) else []
    last = next((str(x) for x in reversed(log) if str(x).strip()), "")
    detail = re.sub(r"^\s*\[\d+/\d+\]\s*", "", last).strip()
    if not detail or stage == "overview":
        return fallback, ""
    step, colon, item = detail.partition("：")
    step = re.sub(r"^[^\w（《]+", "", step).strip().removeprefix("正在") or fallback
    return (step[:40], item.strip()[:120]) if colon else (step[:40], "")


def _progress_text(label: str, st: dict) -> str:
    """进度一句话：第几条、在做什么、哪一条。"""
    cur, total = st.get("current") or 0, st.get("total") or 0
    step, item = _progress_parts(st)
    head = f"{label}第 {cur}/{total} 条" if total else label
    return f"{head}：{step}" + (f"：{item if len(item) <= 80 else item[:80] + '…'}" if item else "")


class Runner:
    def __init__(self, call: LocalCall, progress: Progress, sleep=time.sleep):
        self.call, self.progress, self.sleep = call, progress, sleep
        self.subject = ""      # 进度卡片上的名字：节目或者会议名，比链接好认

    def _post(self, path: str, body: dict) -> Any:
        status, data = self.call("POST", path, body)
        if status in (409, 429):   # 重任务名额满了，或者要写的文件夹正被别的任务占着
            raise Busy()
        if status >= 400:
            raise TaskError((data or {}).get("error") if isinstance(data, dict) else f"接口返回 {status}")
        return data

    def _wait(self, status_path: str, label: str) -> dict:
        """轮询一个后台任务直到结束，顺手更新进度评论。返回最后一次的状态。"""
        while True:
            status, st = self.call("GET", status_path, None)
            if status >= 400 or not isinstance(st, dict):
                raise TaskError(f"{label}：查不到任务进度（Spark 可能重启过）")
            if st.get("done"):
                if st.get("error"):
                    raise TaskError(f"{label}：{st['error']}")
                if st.get("ok") is False and not st.get("stopped"):
                    raise TaskError(f"{label}：没有成功")
                if st.get("stopped"):
                    raise TaskError(f"{label}：在电脑上被停止了")
                return st
            cur, total = st.get("current") or 0, st.get("total") or 0
            step, item = _progress_parts(st)
            self.progress.update(_progress_text(label, st), stage=st.get("stage") or "", current=cur, total=total,
                                 step=step, item=item, subject=self.subject)
            self.sleep(JOB_POLL_SECONDS)

    def run(self, task: dict) -> str:
        return {"link": self._link, "check": self._check, "report": self._report,
                "review": self._review, "save": self._save, "delete": self._delete,
                "track_item": self._track_item, "episode_item": self._track_item}[task["type"]](task)

    # -- 手机生成好的复盘、报告，存进笔记库
    def _save(self, task: dict) -> str:
        text = save_file(task)
        try:
            from core import github_sync
            github_sync.after_task()   # 开了「任务跑完自动同步」才会做事
        except Exception:  # noqa: BLE001  同步没排上不影响存文件
            pass
        return text

    # -- 手机上写好的一条信息跟进、一期节目
    def _track_item(self, task: dict) -> str:
        text = (save_episode_item if task["type"] == "episode_item" else save_track_item)(task)
        try:
            from core import github_sync
            github_sync.after_task()
        except Exception:  # noqa: BLE001
            pass
        return text

    # -- 手机上删掉的复盘
    def _delete(self, task: dict) -> str:
        text = delete_file(task)
        try:
            from core import github_sync
            github_sync.after_task()
        except Exception:  # noqa: BLE001
            pass
        return text

    # -- 处理一个链接
    def _link(self, task: dict) -> str:
        self.progress.update("读取链接", force=True)
        found = self._post("/summit/api/discover", {"url": task["url"]})
        entries = found.get("entries") or []
        if not entries:
            raise TaskError("这个链接里没有找到能处理的内容")
        dest = task["dest"]
        payload: dict = {"entries": entries, "do_summary": True, "do_speech_script": True, "skip_existing": True}
        if task.get("summary_length"):
            payload["summary_length"] = task["summary_length"]
        if dest["kind"] == "show":
            folder = dest["folder"]
            try:
                with open(os.path.join(folder, ".manifest.json"), encoding="utf-8") as f:
                    manifest = json.load(f)
            except (OSError, ValueError):
                manifest = {}
            # 已有文件夹的来源地址原样留着：process_job 会把 source_url 写进处理记录
            payload.update(summit_title=os.path.basename(folder), output_dir=os.path.dirname(folder),
                           content_type=manifest.get("content_type") or "series",
                           source_url=manifest.get("source_url") or task["url"])
            where = os.path.basename(folder)
        else:
            title = found.get("summit_title") or "Untitled"
            payload.update(summit_title=title, source_url=found.get("source_url") or task["url"],
                           content_type="summit" if dest["kind"] == "summit" else "series")
            where = title
        self.subject = where
        job = self._post("/summit/api/run", payload)
        st = self._wait(f"/summit/api/status/{job['job_id']}", "处理")
        res = st.get("result") or {}
        failed = len(res.get("failed_entries") or [])
        ok = len(entries) - failed
        out = res.get("output_dir") or ""
        rel = os.path.relpath(out, vault.vault_root()) if out else where
        text = f"处理完 {ok} 条，写进 `{rel}`" + (f"；{failed} 条没成功，电脑上看任务日志" if failed else "")
        if failed and not ok:
            raise TaskError(text)
        return text

    # -- 检查订阅
    def _check(self, task: dict) -> str:
        subs = []
        for kind in ("podcast", "track"):
            status, items = self.call("GET", f"/summit/api/subscriptions?kind={kind}", None)
            if status < 400 and isinstance(items, list):
                subs += items
        lines, problems = [], []
        for name in task["subscriptions"]:
            sub = next((s for s in subs if name in (s.get("name"), os.path.basename(s.get("folder") or ""))), None)
            if not sub:
                problems.append(f"没有叫「{name}」的订阅")
                continue
            self.progress.update(f"检查「{name}」", force=True)
            found = self._post(f"/summit/api/subscriptions/{sub['id']}/check", {})
            if found.get("error"):
                problems.append(f"「{name}」：{found['error']}")
                continue
            new = sorted(found.get("new_entries") or [], key=lambda e: e.get("publish_date") or "", reverse=True)
            ids = [e["id"] for e in new[:task["max_new"]]]
            if not ids:
                lines.append(f"「{name}」没有新内容")
                continue
            extra = {"summary_length": task["summary_length"]} if task.get("summary_length") else {}
            if sub.get("kind") == "podcast":
                job = self._post(f"/summit/api/subscriptions/{sub['id']}/update", {"entry_ids": ids, **extra})
                self.subject = name
                self._wait(f"/summit/api/status/{job['job_id']}", f"「{name}」")
            else:
                job = self._post("/summit/api/track/run",
                                 {"selections": [{"sub_id": sub["id"], "entry_ids": ids}], **extra})
                self.subject = name
                self._wait(f"/summit/api/track/status/{job['job_id']}", f"「{name}」")
            more = len(new) - len(ids)
            lines.append(f"「{name}」处理了 {len(ids)} 条新内容" + (f"，还有 {more} 条没处理" if more > 0 else ""))
        if problems and not lines:
            raise TaskError("；".join(problems))
        return "；".join(lines + problems)

    # -- 出报告
    def _report(self, task: dict) -> str:
        self.progress.update("检索笔记", force=True)
        body = {"topic": task["topic"]}
        if task.get("scope"):
            body["folder"] = os.path.relpath(_show_folder(task["scope"]), vault.vault_root())
        result = self._search(body)
        if result.get("need_choice"):
            # 主题有几种含义：手机上没法当场选，按第一种接着检索
            senses = result.get("senses") or []
            if not senses:
                raise TaskError("主题有几种含义，没法自动选")
            result = self._search(dict(body, sense=senses[0]))
        if result.get("insufficient"):
            raise TaskError(result["insufficient"])
        cands = result.get("candidates") or []
        picked = [c["path"] for c in cands if (c.get("relevance") or 0) >= 3][:REPORT_PICK] \
            or [c["path"] for c in cands[:5]]
        if not picked:
            raise TaskError("没有找到相关的笔记")
        job = self._post("/notes/api/run", {"notes": picked, "topic": task["topic"], "focus": task["topic"],
                                            "source": "topic"})
        st = self._wait(f"/notes/api/progress/{job['job_id']}", "写报告")
        res = st.get("result") or {}
        path = res.get("report_path") or res.get("path") or ""
        rel = os.path.relpath(path, vault.vault_root()) if path else "笔记洞察的输出目录"
        return f"用 {len(picked)} 篇笔记写好了报告：`{rel}`"

    # -- 复盘（core/review.py）
    def _review(self, task: dict) -> str:
        self.progress.update("开始复盘", force=True)
        body = {"kind": task["kind"], "days": task["days"], "months": task["months"]}
        if task.get("topic"):
            body["topic"] = task["topic"]
        if task.get("paths"):
            body["paths"] = task["paths"]
        job = self._post("/review/api/run", body)
        st = self._wait(f"/review/api/progress/{job['job_id']}", "复盘")
        rel = (st.get("result") or {}).get("rel") or "output/复盘/"
        return f"复盘写好了：`{rel}`"

    def _search(self, body: dict) -> dict:
        job = self._post("/notes/api/search", body)
        self._wait(f"/notes/api/progress/{job['job_id']}", "检索笔记")
        status, result = self.call("GET", f"/notes/api/result/{job['job_id']}", None)
        if status >= 400 or not isinstance(result, dict):
            raise TaskError("检索结果拿不到")
        return result


# ---------------------------------------------------------------- 一轮轮询

class Queue:
    def __init__(self, call: LocalCall, gh_factory=None, sleep=time.sleep):
        self.call = call
        self.sleep = sleep
        self._gh_factory = gh_factory or (lambda: GitHub(repo_slug(), token()))
        self._me = ""
        self._busy_until: dict[int, float] = {}
        self._busy_noted: set[int] = set()
        self._started = False
        self.last_poll = 0.0
        self.last_error = ""

    def _gh(self) -> GitHub:
        if not token():
            raise TaskError("没有 GitHub 令牌：设环境变量 SPARK_GITHUB_TOKEN，或存成 ~/.spark/keys/github.key")
        return self._gh_factory()

    def poll_once(self) -> int:
        """看一轮 Issue，按创建顺序一次跑一个。返回这一轮跑完（成功或失败）的任务数。"""
        gh = self._gh()
        if not self._me:
            self._me = gh.me()
        issues = gh.open_tasks()
        self.last_poll = time.time()
        if not self._started:
            # 刚启动：上次退出时还标着「进行中」的，任务已经跟着进程没了
            for issue in issues:
                if RUNNING_LABEL in _labels(issue):
                    self._fail(gh, issue["number"], "电脑上的 Spark 重启了，这个任务中断了。点重试会从头再跑。")
            self._started = True
            issues = gh.open_tasks()
        done = 0
        for issue in issues:
            labels = _labels(issue)
            number = issue["number"]
            if labels & {RUNNING_LABEL, FAILED_LABEL}:
                continue
            if (issue.get("user") or {}).get("login") != self._me:
                continue   # 别人开的（比如协作者）一律不接
            if self._busy_until.get(number, 0) > time.time():
                continue
            done += self._handle(gh, issue)
        return done

    def _handle(self, gh: GitHub, issue: dict) -> int:
        number = issue["number"]
        try:
            task = parse(issue.get("body") or "")
        except TaskError as e:
            self._fail(gh, number, f"任务内容有问题：{e}")
            return 1
        gh.add_label(number, RUNNING_LABEL)
        progress = Progress(gh, number)
        try:
            text = Runner(self.call, progress, sleep=self.sleep).run(task)
        except Busy:
            # 电脑上正跑着别的大任务：退回等待，过一会儿再来
            gh.remove_label(number, RUNNING_LABEL)
            self._busy_until[number] = time.time() + BUSY_RETRY_SECONDS
            if number not in self._busy_noted:   # 只说一次，不每两分钟刷一条
                self._busy_noted.add(number)
                gh.comment(number, status_comment("waiting", "电脑上正在跑别的任务，等它结束再开始"))
            return 0
        except TaskError as e:
            gh.remove_label(number, RUNNING_LABEL)
            self._fail(gh, number, str(e), progress)
            return 1
        except Exception as e:  # noqa: BLE001
            gh.remove_label(number, RUNNING_LABEL)
            self._fail(gh, number, f"意外错误：{e}", progress)
            return 1
        self._busy_until.pop(number, None)
        self._busy_noted.discard(number)
        body = status_comment("done", f"完成：{text}")
        if progress.comment_id:
            gh.edit_comment(progress.comment_id, body)
        else:
            gh.comment(number, body)
        gh.remove_label(number, RUNNING_LABEL)
        gh.close(number)
        return 1

    def _fail(self, gh: GitHub, number: int, reason: str, progress: Optional[Progress] = None) -> None:
        body = status_comment("failed", f"没成功：{reason}")
        try:
            if progress and progress.comment_id:
                gh.edit_comment(progress.comment_id, body)
            else:
                gh.comment(number, body)
            gh.add_label(number, FAILED_LABEL)
        except TaskError:
            pass


# ---------------------------------------------------------------- 后台线程

_THREAD: dict = {"queue": None}
_WAKE = threading.Event()
_STATE = {"checking": False, "next_poll": 0.0, "last_check": 0.0}


def _check(q: "Queue") -> None:
    _STATE["checking"] = True
    try:
        q.poll_once()
        q.last_error = ""
    except TaskError as e:
        q.last_error = str(e)
    except Exception as e:  # noqa: BLE001
        q.last_error = f"意外错误：{e}"
    finally:
        _STATE["checking"] = False


def start(call: LocalCall) -> None:
    """spark.py 启动时调一次。所有检查都在这一个线程里做，不会两轮同时跑。
    定时：按设置的间隔看；手动：只在 trigger() 时看；改了设置不用重启。"""
    if _THREAD["queue"] is not None:
        return
    q = Queue(call)
    _THREAD["queue"] = q

    def loop() -> None:
        while True:
            # 下一次定时检查 = 上一次检查 + 间隔；间隔改了马上按新的算。刚开启（还没查过）就先查一次
            m = mode()
            _STATE["next_poll"] = (_STATE["last_check"] + poll_minutes() * 60) if m == "auto" else 0.0
            wait = _STATE["next_poll"] - time.time() if m == "auto" else SETTINGS_RECHECK_SECONDS
            woken = _WAKE.wait(timeout=max(0.0, min(wait, SETTINGS_RECHECK_SECONDS)))
            _WAKE.clear()
            m = mode()
            due = m == "auto" and time.time() >= _STATE["last_check"] + poll_minutes() * 60
            if (woken and m) or due:
                _check(q)
                _STATE["last_check"] = time.time()

    threading.Thread(target=loop, daemon=True, name="spark-remote-tasks").start()


def trigger() -> str:
    """设置页「现在检查」：叫醒后台线程马上看一轮。返回不能检查的原因，能检查返回空串。"""
    if not mode():
        return "先在设置里打开「手机上添加的任务」并保存"
    if not token():
        return "还没有 GitHub 令牌：设环境变量 SPARK_GITHUB_TOKEN，或存成 ~/.spark/keys/github.key"
    if _THREAD["queue"] is None:
        return "后台检查没有启动（Spark 需要用 spark.py 启动）"
    if _STATE["checking"]:
        return "正在检查，等这一轮结束"
    _WAKE.set()
    return ""


def status() -> dict:
    q = _THREAD["queue"]
    return {"enabled": enabled(), "mode": mode(), "minutes": poll_minutes(), "repo": repo_slug(),
            "has_token": bool(token()), "token_source": keys.source_of(TOKEN_PROVIDER),
            "checking": _STATE["checking"], "next_poll": _STATE["next_poll"] if mode() == "auto" else 0,
            "last_poll": q.last_poll if q else 0, "last_error": q.last_error if q else ""}
