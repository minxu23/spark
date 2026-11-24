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
  或者手机上写的随手记（Spark/笔记/，见 core/jottings.py 和 _save_jotting）。
  只认 output/ 下的 .md；同名文件已经有了、内容一样就算存过，不一样就另存成 _2、_3。

状态用标签表示：没有状态标签 = 等着；「进行中」；「失败」。完成就评论结果并关掉 Issue。
手机上点「重试」= 去掉「失败」标签，下一轮会重新接手。

安全：只接你自己账号开的 Issue（和令牌是同一个账号），只认上面三类任务、http(s) 链接、
Spark/ 下真实存在的文件夹名。令牌按 core/keys 的规矩读（环境变量 SPARK_GITHUB_TOKEN 或
~/.spark/keys/github.key），只放在请求头里，不写进日志、Issue 或设置文件。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any, Callable, Optional

from core import annotations, atomic
from core import clips
from core import doctime
from core import jottings
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
# 手机交来存进笔记库的文件：最多这么多字、路径最多这么长。
# 剪藏的长文、整本书的章节能过 20 万字（2026-11-10《The Commanding Heights》就被挡过），放到 200 万字
MAX_SAVE_CHARS = 2_000_000
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
    """任务本身有问题或跑失败了：原因写进 Issue。kind 带回手机（结果、状态评论里）：
    conflict 电脑上也改过、手机这一版存进了 note-conflicts；gone 电脑上已经没有这篇了；rejected 内容不合规、电脑收不了。"""

    def __init__(self, message: str = "", kind: str = "rejected"):
        super().__init__(message)
        self.kind = kind


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
    if not isinstance(name, str) or not name or name.startswith(".") or "/" in name or "\\" in name \
            or name == jottings.DIRNAME:   # 随手记的文件夹不是节目
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
        # 新文件只能存进 output/、剪藏和随手记；Spark/ 下别的地方只收手机做了笔记、整篇发回来的已有文章（带 replace）
        if not isinstance(path, str) or not (_save_path_ok(path) or jottings.is_path(path)
                                             or (raw.get("replace") and _note_path_ok(path))):
            raise TaskError("path 应该是 output/ 下的 .md 文件，或者笔记库 Spark/ 下能做笔记的一篇")
        # 放回原处取消的删除：内容校验之前就记下（这个存本身收不了，取消照样算数，晚到的删除不再执行）
        if raw.get("restore") is True and isinstance(raw.get("cancels"), list) and _delete_path_ok(path):
            _record_cancels(path, raw["cancels"])
        if not isinstance(content, str) or not content.strip() or len(content) > MAX_SAVE_CHARS:
            raise TaskError(f"content 应该是 1 到 {MAX_SAVE_CHARS} 字的正文")
        out = {"type": "save", "path": path, "content": content}
        _parse_edit_fields(raw, out)
        if clips.is_clip_path(path):
            images = raw.get("images") or {}
            if not isinstance(images, dict) or len(images) > clips.MAX_IMAGES \
                    or not all(clips.image_key_ok(k, v) for k, v in images.items()):
                raise TaskError("images 应该是 Spark/剪藏/attachments/ 下的图片名 → 原图地址")
            out["images"] = images
            # 剪藏是手机上那一份说了算（比如后来补写了摘要）：同名的直接覆盖。
            # 手机上改的是电脑同步过来的那篇时带着改之前那一版的指纹：电脑上后来也改过（在 Mac 上划了线）就不覆盖
            out["replace"] = bool(raw.get("replace"))
            base = raw.get("base")
            if out["replace"] and isinstance(base, str) and re.fullmatch(r"[0-9a-f]{64}", base):
                out["base"] = base
            if not out["replace"]:
                out.pop("bases", None)
        elif path.startswith(REVIEW_DIR + "/"):
            # 手机上改了复盘的标题、写了追问：同名的直接覆盖
            out["replace"] = bool(raw.get("replace"))
        elif jottings.is_path(path):
            # 随手记（Spark/笔记/）：手机上新建的不带 base，写在手机定的那个名字下；改电脑上已有的一条带 bases，
            # 规矩同手机划线发回来的文章（见 save_file → _save_jotting）
            out["replace"] = True
            base = raw.get("base")
            if isinstance(base, str) and re.fullmatch(r"[0-9a-f]{64}", base):
                out["base"] = base
            elif out.get("bases"):
                out["base"] = out["bases"][0]
        elif raw.get("replace") or not _save_path_ok(path):
            # 整理稿、会议资料、报告……在手机上划线、批注后整篇发回来：带着改之前那一版的指纹，
            # 电脑上那篇还是那一版才覆盖（电脑可能刚重新生成过），见 save_file
            base = raw.get("base")
            if not isinstance(base, str) and out.get("bases"):
                base = out["bases"][0]
            if not raw.get("replace") or not isinstance(base, str) or not re.fullmatch(r"[0-9a-f]{64}", base):
                raise TaskError("改笔记库里已有的文章要带 replace 和 base（改之前那一版的 SHA-256）")
            out["replace"], out["base"] = True, base
        else:
            out.pop("bases", None)
        return out

    if kind == "purge_images":
        return _parse_purge_images(raw)
    if kind == "cleanup":   # 清理工具（core/cleanup.py）：手机发起扫描、交来人的选择
        from core import cleanup
        return cleanup.parse_task(raw)

    if kind == "track_item":
        return _parse_track_item(raw)

    if kind == "episode_item":
        return _parse_episode_item(raw)

    if kind == "delete":
        path = raw.get("path")
        if not isinstance(path, str) or not _delete_path_ok(path):
            raise TaskError("只能删 output/复盘/ 下的 .md、剪藏和随手记")
        out = {"type": "delete", "path": path}
        _parse_edit_fields(raw, out)
        out.pop("bases", None)
        out.pop("new", None)
        # shas：手机删掉的那几版内容（手机上那份、还没确认的修改、发件箱里撤下的存）。电脑记成墓碑，
        # 删除先到、存后到时，晚到的那份不再建出来
        shas = raw.get("shas")
        if isinstance(shas, list):
            out["shas"] = [h for h in shas if isinstance(h, str) and re.fullmatch(r"[0-9a-f]{64}", h)][:64]
        # keys：手机删之前交过的这篇的存（任务里的 key）：它们晚到也不建回来
        keys = raw.get("keys")
        if isinstance(keys, list):
            out["keys"] = [k for k in keys if isinstance(k, str) and clips._KEY_RE.fullmatch(k)][:64]
        out.pop("cancels", None)
        return out

    raise TaskError("type 只能是 link / check / report / review / save / delete / purge_images / cleanup / track_item / episode_item")


MAX_PURGE_IMAGES = 300
_IMAGE_NAME_RE = re.compile(r"^[^/\\.][^/\\]*\.(?:jpg|jpeg|png|gif|webp)$", re.I)


def _parse_purge_images(raw: dict) -> dict:
    """手机废纸篓里彻底删除了一篇：它引用的图片（只能是这篇所在文件夹的 attachments/ 下直接的图片文件）。
    哪些真删由电脑自己查（见 purge_images），手机的判断不作数。"""
    path, images = raw.get("path"), raw.get("images")
    if not isinstance(path, str) or not _delete_path_ok(path):
        raise TaskError("path 应该是一篇剪藏、复盘或随手记")
    folder = path.rsplit("/", 1)[0] + "/attachments/"
    if not isinstance(images, list) or len(images) > MAX_PURGE_IMAGES or not all(
            isinstance(i, str) and i.startswith(folder) and _IMAGE_NAME_RE.match(i[len(folder):])
            and ".." not in i.split("/") and not any(ord(ch) < 32 for ch in i) for i in images):
        raise TaskError("images 应该是这篇所在文件夹 attachments/ 下的图片")
    out = {"type": "purge_images", "path": path, "images": list(dict.fromkeys(images))}
    _parse_edit_fields(raw, out)
    out.pop("bases", None)
    return out


def _to_trash(full: str) -> str:
    """移进 macOS 的废纸篓（同名的另起「 2」「 3」，不盖掉废纸篓里已有的）。返回移到哪了。"""
    trash = trash_dir()
    os.makedirs(trash, exist_ok=True)
    stem, ext = os.path.splitext(os.path.basename(full))
    dest = os.path.join(trash, stem + ext)
    n = 2
    while os.path.exists(dest):
        dest = os.path.join(trash, f"{stem} {n}{ext}")
        n += 1
    shutil.move(full, dest)
    atomic._note_search_index(full)   # 全文索引下一次搜索前去掉它
    return dest


class _Unreadable(Exception):
    pass


UTF16_BOMS = (b"\xff\xfe", b"\xfe\xff")
# 查图片引用时读哪些文件：笔记库里的文本类文件（Markdown、白板、Bases、导出的网页、样式表……）
TEXT_EXTS = (".md", ".canvas", ".base", ".html", ".htm", ".excalidraw", ".css", ".json", ".txt", ".svg", ".xml",
             ".yaml", ".yml", ".csv")
JSON_EXTS = (".canvas", ".json", ".excalidraw")
# 白名单之外的文件：不是这些二进制类型、不超过 EXTRA_MAX_BYTES 的也查（.log 之类）；解不开的按字节找，读不了、太大的跳过
BINARY_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic", ".heif", ".avif", ".tif", ".tiff", ".ico",
               ".pdf", ".pptx", ".ppt", ".docx", ".doc", ".xlsx", ".xls", ".key", ".pages", ".numbers", ".epub",
               ".zip", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar", ".tar", ".dmg", ".pkg", ".app", ".exe", ".bin",
               ".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus", ".mp4", ".m4v", ".mov", ".avi", ".mkv",
               ".webm", ".ttf", ".otf", ".woff", ".woff2", ".sqlite", ".db", ".pyc", ".so", ".dylib", ".o", ".a",
               ".jar", ".class", ".psd", ".ai", ".sketch", ".fig")
EXTRA_MAX_BYTES = 2 * 1024 * 1024


def name_forms(n: str) -> set[str]:
    """文件名在文档里可能的写法：NFC 归一化、不分大小写，写成 %20、+ 的也算。"""
    import unicodedata
    n = unicodedata.normalize("NFC", n).lower()
    return {n, urllib.parse.quote(n).lower(), n.replace(" ", "+"), urllib.parse.quote(n, safe="").lower()}


def _raw_forms(n: str) -> set[bytes]:
    """不是 UTF-8 的文件按字节找：文件名的 UTF-8 字节，NFC、NFD 两种写法，ASCII 不分大小写。"""
    import unicodedata
    return {unicodedata.normalize(f, n).encode("utf-8").lower() for f in ("NFC", "NFD")}


class _Suffixes:
    """一批写法在一段文字（或字节）里出现了哪些。写法都以扩展名结尾：先找扩展名，再比它前面那一截，
    不用拿每个名字把整篇扫一遍（整个笔记库两亿多字、上千张图，挨个找要好几分钟）。结果跟逐个做包含检查一样。"""

    def __init__(self):
        self.anchors: dict = {}   # 扩展名 → {写法长度: {写法: {文件名}}}
        self.loose: list = []     # 没有点的写法：逐个找

    def add(self, form, name: str) -> None:
        dot = form.rfind("." if isinstance(form, str) else b".")
        if dot < 0:
            self.loose.append((form, name))
            return
        self.anchors.setdefault(form[dot:], {}).setdefault(len(form), {}).setdefault(form, set()).add(name)

    def find(self, hay) -> set[str]:
        out: set[str] = set()
        for tail, by_len in self.anchors.items():
            i = hay.find(tail)
            while i >= 0:
                end = i + len(tail)
                for size, table in by_len.items():
                    if end >= size:
                        hit = table.get(hay[end - size:end])
                        if hit:
                            out |= hit
                i = hay.find(tail, i + 1)
        out.update(n for form, n in self.loose if form in hay)
        return out


class NameMatcher:
    """一批文件名在一篇文档里出现了哪些（写法见 name_forms、_raw_forms）。"""

    def __init__(self, names):
        self.names = set(names)
        self.text, self.raw = _Suffixes(), _Suffixes()
        for n in self.names:
            for f in name_forms(n):
                self.text.add(f, n)
            for b in _raw_forms(n):
                self.raw.add(b, n)

    def in_text(self, text: str) -> set[str]:
        """text 要先做过 NFC 归一化、转成小写。"""
        return self.text.find(text)

    def in_bytes(self, data: bytes) -> set[str]:
        return self.raw.find(data.lower())   # 只动 ASCII 字母


def is_extra(name: str) -> bool:
    """白名单之外的文件（见 BINARY_EXTS 上面的说明）：读不了、太大就跳过，不算「读不出来的文档」。"""
    return not name.lower().endswith(TEXT_EXTS)


def doc_paths(root: str):
    """笔记库里要查的文档：(相对路径, 完整路径)。扩展名在 TEXT_EXTS 里的；别的不是二进制类型、不超过 2 MB 的也算
    （读不了就跳过，解不开按字节找）。跳过点开头的文件夹和文件（清理工具自己写的 output/.cleanup.json、.edits.json 都是点开头的）。"""
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if not x.startswith(".")]
        for f in files:
            if f.startswith(".") or f.lower().endswith(BINARY_EXTS):
                continue
            full = os.path.join(d, f)
            if is_extra(f):
                try:
                    if os.path.islink(full) or os.path.getsize(full) > EXTRA_MAX_BYTES:
                        continue
                except OSError:
                    continue
            yield os.path.relpath(full, root).replace(os.sep, "/"), full


def file_stamp(full: str):
    """文件变没变的记号（修改时刻、大小、inode）：只用来决定要不要重读，重读之后按内容判断。"""
    st = os.stat(full)
    return [st.st_mtime_ns, st.st_size, st.st_ino]


def read_doc(full: str):
    """(文字, 原始字节)：带 BOM 的按 UTF-16 解，不然按 UTF-8 解，解不开的文字是 None。读不了抛 OSError。"""
    with open(full, "rb") as fh:
        data = fh.read()
    try:
        text = data.decode("utf-16") if data[:2] in UTF16_BOMS else data.decode("utf-8")
    except UnicodeDecodeError:
        text = None
    return text, data


def search_text(rel: str, text: str) -> str:
    """找文件名用的文字（NFC、小写）：原文；再加上 HTML 实体解开后的（&amp; 写的 a&b.jpg）；
    JSON 文件（.canvas、.excalidraw……）再加上解析出来的字符串（\\uXXXX 转义写的文件名）。任一处出现都算。"""
    import html as html_mod
    import unicodedata
    parts = [text]
    if "&" in text:
        un = html_mod.unescape(text)
        if un != text:
            parts.append(un)
    if rel.lower().endswith(JSON_EXTS) and "\\" in text:
        try:
            strings: list[str] = []

            def walk(x):
                if isinstance(x, str):
                    strings.append(x)
                elif isinstance(x, dict):
                    for k, val in x.items():
                        strings.append(k)
                        walk(val)
                elif isinstance(x, list):
                    for val in x:
                        walk(val)
            walk(json.loads(text))
            parts.append("\n".join(strings))
        except (ValueError, RecursionError):
            pass
    return unicodedata.normalize("NFC", "\n".join(parts)).lower()


def vault_docs(root: str, stats: Optional[dict] = None):
    """笔记库里要查的文档（见 doc_paths）。一篇给一个 (相对路径, 文字, 原始字节, 读不了的原因)：
    读不了的文字和字节是 None；带 BOM 的按 UTF-16 解，不然按 UTF-8 解，解不开的文字是 None（按字节找）。
    stats 给了就在读之前记下每篇的 file_stamp（清理工具动手前看哪些文档后来又变过）。"""
    for rel, full in doc_paths(root):
        extra = is_extra(rel)
        try:
            if stats is not None:
                stats[rel] = file_stamp(full)
            text, data = read_doc(full)
        except OSError as e:
            if not extra:
                yield rel, None, None, e
            continue
        if extra and len(data) > EXTRA_MAX_BYTES:   # 解不开的照样按字节找（同白名单里的文件）
            continue
        yield rel, text, data, None


def _image_referenced(root: str, names: set[str]) -> tuple[set[str], list[str]]:
    """整个笔记库（只跳过点开头的文件夹）哪些 Markdown、.canvas 里提到了这些图片。按文件名做纯文本包含检查：
    文件名和文档内容都先做 NFC 归一化、不分大小写，写成 %20、+ 的也算；不只认 ![](attachments/x.jpg) 这类引用写法：
    ![[x.jpg]]、带相对路径的、代码块和普通文字里出现的都按在用算；别的文件夹里有同名图、文档只写了文件名的也按在用算。
    剪藏的图按原地址（公众号、小红书是图片的 image_key）的 sha1 前 16 位起名，同一来源的同一张图几篇共用一个文件，所以要查。
    不是 UTF-8 的文件按字节找（文件名的 UTF-8 字节，NFC、NFD 两种写法，ASCII 不分大小写），找到也算在用。
    彻底读不了的（没权限、读出错）：在 Spark/、output/ 下的抛 _Unreadable（这一轮不删），别处的跳过、返回它们的名字。
    清理工具（core/cleanup.py）用的是同一套：vault_docs、NameMatcher。"""
    import unicodedata
    found: set[str] = set()
    skipped: list[str] = []
    matcher = NameMatcher(names)
    for rel, text, data, err in vault_docs(root):
        if err is not None:
            if rel.split("/")[0] in (vault.SPARK_DIRNAME, vault.REPORTS_DIRNAME):
                raise _Unreadable(f"{rel}：{err}") from None
            skipped.append(rel)
            continue
        if text is not None:
            found |= matcher.in_text(search_text(rel, text))
        elif data[:2] in UTF16_BOMS:
            found |= set(names)   # 有 BOM 却按 UTF-16 解不开：拿不准，当成每张图都在用
        else:
            found |= matcher.in_bytes(data)
    return found, skipped


def purge_images(task: dict) -> str:
    """手机废纸篓里彻底删除了一篇：它引用的图片，笔记库里别的文档都没提到的，移进 macOS 废纸篓（不直接删）。
    这篇在电脑上还在笔记库里（删除没成功、或者从废纸篓拖了回来）：图片一张不动。
    别的还在废纸篓里的文章（墓碑）记着的图片也不动：那篇还可能放回。"""
    with clips.DOC_LOCK:
        return _purge_images(task)


def _purge_images(task: dict) -> str:
    root = os.path.realpath(vault.vault_root())
    rel = task["path"]
    full = os.path.realpath(os.path.join(root, *rel.split("/")))
    if os.path.commonpath([full, root]) != root:
        raise TaskError("路径不在笔记库里")
    if os.path.exists(full):
        return f"这篇在电脑上还在笔记库里，图片不动：`{rel}`"
    # 修改记录（墓碑在里面）靠不住：别的废纸篓里文章的图分不清，这次一张都不动，任务失败、能重试。
    # 要先查：下面 release_images 一写，坏文件就被挪开、换成空的了
    from core import cleanup
    paused = cleanup.moves_paused(root)
    if paused:
        raise TaskError(f"电脑上的修改记录有问题（{paused}），这次图片一张都没动。"
                        "在电脑的清理工具页确认之后点重试")
    # 手机彻底删除了：这篇墓碑里记着的图片不再算在用（墓碑本身留着，挡晚到的旧任务）
    for r in {rel, clips.renamed(root, rel)}:
        clips.release_images(root, r)
    held = clips.held_images(root)   # 别的还在废纸篓里、可能放回的文章提到的图片：不动
    folder = os.path.realpath(os.path.join(root, *rel.split("/")[:-1], "attachments"))
    targets = {}
    for img in task["images"]:
        p = os.path.realpath(os.path.join(root, *img.split("/")))
        if os.path.dirname(p) != folder or os.path.commonpath([p, root]) != root:
            raise TaskError(f"图片不在这篇的附件文件夹里：`{img}`")
        if os.path.isfile(p):
            targets[os.path.basename(p)] = p
    try:
        used, skipped = _image_referenced(root, set(targets))
    except _Unreadable as e:   # 拿不准就不删：这一轮一张都不动，点重试再查
        raise TaskError(f"笔记库里有文档读不出来（{e}），这次图片一张都没动") from None
    rels = {n: os.path.relpath(p, root).replace(os.sep, "/") for n, p in targets.items()}
    moved = [n for n in sorted(targets) if n not in used and rels[n] not in held]
    for n in moved:
        _to_trash(targets[n])
    kept = len(targets) - len(moved)
    return f"图片移进了废纸篓 {len(moved)} 张" + (f"，别的文档还在用的 {kept} 张没动" if kept else "") \
        + (f"；这几篇读不了，没查：{'、'.join(skipped[:10])}" if skipped else "")


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
    clean.update(_time_fields(entry))
    return {"type": "track_item", "folder": folder, "name": name, "content": content, "entry": clean,
            "summary": {"tldr": str(summary.get("tldr") or ""), "body": str(summary.get("body") or "")}}


MAX_BASES = 32   # 同手机 PendingEdits.maxBases：最早的 8 个加最近的


def _parse_edit_fields(raw: dict, out: dict) -> None:
    """手机新版交来的：bases（手机这几次改之前、电脑上可能是的那几版的 SHA-256，第一个跟 base 一样）、
    edited_at（手机上改的时刻，毫秒）。认不出的不要。"""
    bases = raw.get("bases")
    if isinstance(bases, list):
        good = [b for b in bases if isinstance(b, str) and re.fullmatch(r"[0-9a-f]{64}", b)][:MAX_BASES]
        if good:
            out["bases"] = good
    at = raw.get("edited_at")
    if isinstance(at, int) and not isinstance(at, bool) and 631152000000 <= at <= (time.time() + 2 * 86400) * 1000:
        out["edited_at"] = at
    # new：手机上新建的一篇，手机还没看到电脑收下过它（第一份可能根本没到，比如在发件箱里被后来的修改替掉了）。
    # 电脑上没有这个文件时照写（不算「没有了」）；有的话照常比内容（见 _create_new）
    if raw.get("new") is True:
        out["new"] = True
    # restore：用户在手机废纸篓里点了「放回原处」。电脑清掉这篇的墓碑、照常存（墓碑只挡删之前交的旧任务）
    if raw.get("restore") is True:
        out["restore"] = True
    # key：手机给每个存、删任务起的标识。cancels：放回原处时取消的删除任务的 key（迟到的那个删除不再执行）
    if isinstance(raw.get("key"), str) and clips._KEY_RE.fullmatch(raw["key"]):
        out["key"] = raw["key"]
    cancels = raw.get("cancels")
    if isinstance(cancels, list):
        good = [k for k in cancels if isinstance(k, str) and clips._KEY_RE.fullmatch(k)][:16]
        if good:
            out["cancels"] = good


def _bases(task: dict) -> list[str]:
    return ([task["base"]] if task.get("base") else []) + [b for b in task.get("bases") or [] if b != task.get("base")]


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _same_version(root: str, rel: str, task: dict, current: bytes) -> bool:
    """电脑上这篇还是手机改之前的某一版：bases 里有它的指纹；或者是迁移前那一版（clips.migrated_from）；
    或者 base 是手机之前交来的那份、电脑收下时只接回了时间行（clips.same_content）。不看时间。"""
    h = _sha(current)
    return any(b == h or clips.migrated_from(root, rel, b, current) or clips.same_content(root, rel, b, h) for b in _bases(task))


def _contained(root: str, rel: str, task: dict, current: bytes) -> bool:
    """手机这份的内容已经在电脑现在这版里：一模一样；或者它收下时写成的就是现在这版；
    或者它是现在这版的祖先（后来一次从它改来的修改已经收下，clips.ancestors）。不看时间。"""
    h, c = _sha(current), _sha(task["content"].encode("utf-8"))
    return c == h or clips.same_content(root, rel, c, h) or c in clips.ancestors(root, rel, h)


def blob_sha(data: bytes) -> str:
    """跟 git / GitHub 一样的 blob sha：手机拿它跟同步过来的文件树对，看自己的修改到了没有。"""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _applied(task: dict, full: str) -> None:
    """记下写进去的那一版的 blob sha（结果里带回手机：applied_sha）。"""
    try:
        with open(full, "rb") as f:
            task["_applied_sha"] = blob_sha(f.read())
    except OSError:
        pass


def _time_fields(entry: dict) -> dict:
    """手机带来的发表时刻 publish_at、收录时刻 captured_at（ISO 8601 带时差，core/doctime.py）：认得出才留。"""
    out = {}
    for key in ("publish_at", "captured_at"):
        v = entry.get(key)
        if isinstance(v, str) and len(v) <= 40 and isinstance(doctime.parse(v), datetime):
            out[key] = v
    return out


def _parse_episode_item(raw: dict) -> dict:
    """手机上写好的一期节目（见 apps/mobile-ios/Spark/FeedRunner.swift）：结构化的内容，文件由电脑自己的模板生成。"""
    folder, entry, summary = raw.get("folder"), raw.get("entry"), raw.get("summary")
    parts = folder.split("/") if isinstance(folder, str) else []
    if len(parts) != 2 or parts[0] != vault.SPARK_DIRNAME or not _save_part_ok(parts[1]) \
            or parts[1] in ("信息跟进", "剪藏", jottings.DIRNAME):
        raise TaskError("folder 应该是 Spark/ 下的一个节目文件夹")
    # 简讯（频道里的短视频）只有整理稿、没有小结：summary 可以不带，但要带 speech
    short = isinstance(entry, dict) and entry.get("short") is True
    if not isinstance(entry, dict) or not (isinstance(summary, dict) or (short and summary is None)):
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
    if short and summary is None and speech is None:
        raise TaskError("简讯没有小结，要带整理稿")
    return {"type": "episode_item", "folder": folder,
            "entry": {"id": eid, "title": str(entry.get("title") or "")[:500], "url": str(entry.get("url") or "")[:2000],
                      "duration": int(entry.get("duration") or 0), "is_raw_session": False, "source_type": source,
                      "publish_date": date, "sub_kind": entry.get("sub_kind")} | ({"short": True} if short else {})
                     | _time_fields(entry) | ({"publish_guessed": True} if entry.get("publish_guessed") is True else {}),
            "summary": None if summary is None else {
                "tldr": str(summary.get("tldr") or ""), "body": str(summary.get("body") or ""),
                "guests": words(summary.get("guests")), "topics": words(summary.get("topics"))},
            "paragraphs": [(float(t), x) for t, x in paragraphs], "lang": str(raw.get("lang") or "")[:20],
            "speech": {"text": speech["text"], "mode": speech["mode"]} if speech else None}


REVIEW_DIR = f"{vault.REPORTS_DIRNAME}/复盘"


def _delete_path_ok(path: str) -> bool:
    """手机上能删的：output/复盘/ 下直接的一篇 .md，一篇剪藏（Spark/剪藏/ 下直接的 .md，不含索引），一条随手记。"""
    if clips.is_clip_path(path):
        return ".." not in path.split("/")
    if jottings.is_path(path):
        return True
    return path.startswith(REVIEW_DIR + "/") and "/" not in path[len(REVIEW_DIR) + 1:] and _save_path_ok(path)


def trash_dir() -> str:
    return os.path.expanduser("~/.Trash")


def delete_file(task: dict) -> str:
    """手机上删掉的复盘或剪藏：移进废纸篓（不直接删，想要回来还能从废纸篓拖出来）。已经没有了也算完成。
    剪藏删了要重写索引；文章用过的图片留在 attachments/ 里不动，记进墓碑（清理工具不动它们，见 clips.held_images）。
    task["source"] == "mac"：Mac 应用、网页删的（apps/reader 的 /api/delete 传进来）；手机交来的任务经过 parse_task，
    不会带这个字段，一律按手机算。"""
    with clips.DOC_LOCK:
        return _delete_file(task)


def _delete_file(task: dict) -> str:
    root = os.path.realpath(vault.vault_root())
    rel = task["path"]
    if _delete_path_ok(rel):
        rel = clips.renamed(root, rel)   # 剪藏改过名：删的是新名字那篇
    full = os.path.realpath(os.path.join(root, *rel.split("/")))
    if os.path.commonpath([full, root]) != root or not _delete_path_ok(rel):
        raise TaskError("路径不在笔记库的复盘、剪藏或随手记文件夹里")
    if task.get("key") and task["key"] in clips.cancelled_keys(root, rel):
        return f"这次删除在手机上撤回了（放回原处），没删：`{rel}`"
    tomb = set(task.get("shas") or []) | clips.known_versions(root, rel)
    keys = set(task.get("keys") or []) | clips.seen_keys(root, rel)   # 电脑见过的这篇的存都算进去，不只手机交来的
    if not os.path.isfile(full):
        _record_deleted(root, rel, tomb, keys)
        return f"笔记库里已经没有了：`{rel}`"
    if _bases(task):
        with open(full, "rb") as f:
            current = f.read()
        if not _same_version(root, rel, task, current):
            raise TaskError(f"电脑上这篇在手机删之前又改过，没删：`{rel}`", kind="conflict")
    with open(full, "rb") as f:
        data = f.read()
    tomb.add(_sha(data))
    try:
        from core import cleanup
        images = cleanup.doc_images(root, rel, data)
    except OSError:
        images = []
    _to_trash(full)
    _record_deleted(root, rel, tomb, keys, images, "mac" if task.get("source") == "mac" else "phone")
    if clips.is_clip_path(rel):
        clips.rebuild_index(root)
    return f"移进了废纸篓：`{rel}`"


def _record_deleted(root: str, rel: str, shas: set[str], keys: set[str] = frozenset(),
                    images: list[str] = (), source: str = "phone") -> None:
    """记墓碑。记不上（.edits.json 坏了）不算删除失败：文件已经移进了废纸篓，只是晚到的旧任务可能把它建回来。"""
    try:
        clips.record_deleted(root, rel, shas, keys, images, source)
    except clips.JournalError as e:
        print(f"警告：{e}", file=sys.stderr)


def _record_cancels(rel: str, cancels: list) -> None:
    try:
        clips.record_cancelled(os.path.realpath(vault.vault_root()), rel, cancels[:16])
    except clips.JournalError as e:
        print(f"警告：{e}", file=sys.stderr)


def _clear_deleted(root: str, rel: str) -> None:
    try:
        clips.clear_deleted(root, rel)
    except clips.JournalError as e:
        print(f"警告：{e}", file=sys.stderr)


def _deleted_before(task: dict, root: str, rel: str) -> str:
    """手机上删掉过这篇（墓碑），电脑上现在也没有。返回空串表示照常存。
    - restore（手机废纸篓里放回原处）：墓碑清掉，照常存（文件不存在就建出来）；它取消的删除任务记下（cancels），晚到也不执行；
    - 任务的 key 在墓碑里（手机删之前交的那几个存）→ 算做过、不建；
    - 内容正好是删掉的某一版：
      · 不带 new 的 → 删之前交的旧任务，算做过、不建；
      · 带 new、有 key 但 key 不在墓碑里 → 用户删了以后又新做了一篇一字不差的（老格式只记日期的剪藏会这样）：照常建，墓碑清掉；
      · 带 new、没有 key（老版手机）→ 分不清是旧任务还是新做的：宁可不建，任务失败（gone）、手机上任务页标着没成功，不悄悄等；
    - 内容是新的、但它是从删掉的那几版改来的（bases 都在墓碑里）→ gone，不建，手机上那一版留着；
    - 别的（重新剪藏、重新写一条，收录时刻不同，内容跟删掉的不一样）照常存。"""
    if not _delete_path_ok(rel):
        return ""
    full = os.path.realpath(os.path.join(root, *rel.split("/")))
    if os.path.commonpath([full, root]) != root or os.path.exists(full):
        return ""
    tomb = clips.deleted_shas(root, rel)
    keys = clips.deleted_keys(root, rel)
    if not tomb and not keys:
        return ""
    if task.get("restore"):
        _clear_deleted(root, rel)
        return ""
    if task.get("key") and (task["key"] in keys or task["key"] in clips.seen_keys(root, rel)):
        return f"这篇在电脑上已经删了（移进了废纸篓），这一份是删之前交的，没再建：`{rel}`"
    if _sha(task["content"].encode("utf-8")) in tomb:
        if not task.get("new"):
            return f"这篇在电脑上已经删了（移进了废纸篓），这一份是删之前交的，没再建：`{rel}`"
        if task.get("key"):
            _clear_deleted(root, rel)
            return ""
        raise TaskError(f"这篇刚在手机上删过，这一份跟删掉的一字不差，分不清是不是删之前交的，没再建：`{rel}`。"
                        "要放回来，请用手机「设置 → 废纸篓」里的放回原处", kind="gone")
    if _bases(task) and set(_bases(task)) <= tomb:
        raise TaskError(f"电脑上已经没有这篇了（删了、移进了废纸篓），这次修改没存：`{rel}`", kind="gone")
    return ""


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


def _note_path_ok(path: str) -> bool:
    """手机上做了笔记、整篇发回来的文章：能做笔记的路径（core/annotations.annotatable），字符跟 _save_path_ok 一样严。"""
    return len(path) <= MAX_SAVE_PATH and "\\" not in path and annotations.annotatable(path) \
        and not any(ord(ch) < 32 for ch in path)


def note_conflicts_dir() -> str:
    from core import settings
    return os.path.join(os.path.dirname(settings.settings_path()), "note-conflicts")


def _save_noted(task: dict, root: str) -> str:
    """手机上划线、批注过的一篇：电脑上那篇还是手机改之前那一版（或者已经一样）才覆盖。
    电脑上后来又改过（重新生成、在 Mac 上也划了线）就不覆盖，手机这一版存进 note-conflicts/，不在笔记库里另生一份。"""
    rel = task["path"]
    full = os.path.realpath(os.path.join(root, *rel.split("/")))
    if os.path.commonpath([full, root]) != root:
        raise TaskError("路径不在笔记库里")
    if not os.path.isfile(full):
        if task.get("new") or task.get("restore"):   # 手机上新建的、放回原处的：照建
            return _create_new(task, root, rel, full)
        raise TaskError(f"电脑上已经没有这篇了：`{rel}`", kind="gone")
    with open(full, "rb") as f:
        current = f.read()
    if current == task["content"].encode("utf-8"):
        _applied(task, full)
        return f"笔记库里已经是这一版：`{rel}`"
    # 先看电脑上是不是手机改之前的某一版（bases、迁移前那版、接回时间行前那份）：是就照写，不管写成什么
    # （手机上撤了划线、改回原样也要写进去）
    if _same_version(root, rel, task, current):
        # 手机那一版还是旧的时间写法：文档头的两行时间、文件头的时间字段用电脑上现在的（core/doctime.carry_time_header）
        written = doctime.carry_time_header(current.decode("utf-8", "replace"), task["content"])
        written = _carry_merged(root, rel, written)
        atomic.write_text(full, written)
        # 记下收下的这次：手机下一次改带它本地那份的指纹也认；晚到的祖先认得出已经在里面
        _record_edit(task, root, rel, written, _bases(task) + [_sha(current)])
        _applied(task, full)
        return f"手机上的笔记存进了：`{rel}`" + _journal_note(task)
    # 不是：晚到、重复的旧任务，内容已经在现在这版里（后来一次从它改来的修改已经收下）→ done，applied_sha 是现在这版
    if _contained(root, rel, task, current):
        _applied(task, full)
        return f"笔记库里已经有这一版的改动：`{rel}`"
    _keep_phone_version(rel, task["content"])


def _carry_merged(root: str, rel: str, text: str) -> str:
    """清理工具合并过重复图片：手机交来的（可能是合并前那一版）里指向合并掉的那张的引用换成留下的那张。"""
    from core import cleanup
    return cleanup.carry_merged_images(root, rel, text)


def _record_edit(task: dict, root: str, rel: str, written: str, bases: list[str]) -> None:
    """记下收下的这次修改。文件已经写好了：记录写不进去（.edits.json 坏了）不算任务失败，记在任务上、大声说出来。"""
    try:
        clips.record_edit(root, rel, task["content"], written, bases)
    except clips.JournalError as e:
        task["_journal_problem"] = str(e)
        print(f"警告：{e}", file=sys.stderr)


def _journal_note(task: dict) -> str:
    return f"（修改记录没记上：{task['_journal_problem']}，要处理一下）" if task.get("_journal_problem") else ""


def _create_new(task: dict, root: str, rel: str, full: str) -> str:
    """手机上新建的一篇（new），电脑上还没有：写在手机定的那个名字下，记进修改记录（之后带 bases 的修改认得出）。"""
    os.makedirs(os.path.dirname(full), exist_ok=True)
    written = _carry_merged(root, rel, task["content"])
    atomic.write_text(full, written)
    _record_edit(task, root, rel, written, _bases(task))
    _applied(task, full)
    return f"手机上新建的存进了笔记库：`{rel}`" + _journal_note(task)


def _known(root: str, rel: str, task: dict) -> bool:
    """这次交来的这份、或者它是从哪几版改来的（bases），电脑的修改记录里见过：说明电脑上这个文件跟它是同一篇。
    都没见过：电脑上同名的是另一篇（另一台设备同一天、同一个开头写的随手记）。"""
    mine = set(_bases(task)) | {_sha(task["content"].encode("utf-8"))}
    for e in clips.load_edits(root).get(rel, []):
        if mine & ({e.get("after"), e.get("content")} | set(e.get("ancestors") or [])):
            return True
    return False


def _keep_phone_version(rel: str, content: str, kind: str = "conflict") -> None:
    """两边都改过：电脑上的不动，手机这一版存进 note-conflicts/，报错让手机上看得到。
    kind=taken：手机上新建的一篇，电脑上这个名字已经是另一篇（手机换个名字重交，见手机 Vault.moveNewJotting）。"""
    folder = note_conflicts_dir()
    os.makedirs(folder, exist_ok=True)
    stem = os.path.splitext(os.path.basename(rel))[0]
    keep = os.path.join(folder, f"{stem}-{time.strftime('%Y%m%d-%H%M%S')}.md")
    n = 2
    while os.path.exists(keep):   # 同一秒里第二份：另起名字，不盖掉上一份
        keep = os.path.join(folder, f"{stem}-{time.strftime('%Y%m%d-%H%M%S')}-{n}.md")
        n += 1
    atomic.write_text(keep, content)
    if kind == "taken":
        raise TaskError(f"电脑上这个名字已经是另一篇了，没覆盖：`{rel}`。手机上的版本存在 {keep}", kind="taken")
    raise TaskError(f"电脑上这篇在手机改之后又变过，没覆盖：`{rel}`。手机上的版本存在 {keep}", kind="conflict")


def save_file(task: dict) -> str:
    """把手机生成好的文件写进笔记库。已经有同名文件：内容一样就不动，不一样另存成 _2、_3……
    写的时候拿着 clips.DOC_LOCK：清理工具移走图片前的最后一查跟它排开。"""
    with clips.DOC_LOCK:
        return _save_file(task)


def _save_file(task: dict) -> str:
    root = os.path.realpath(vault.vault_root())
    rel = task["path"]
    done = _deleted_before(task, root, clips.renamed(root, rel))
    if task.get("key") and _delete_path_ok(rel):   # 收到过的存记下标识（删这篇时进墓碑）
        try:
            clips.record_seen(root, clips.renamed(root, rel), task["key"])
        except clips.JournalError as e:
            print(f"警告：{e}", file=sys.stderr)
    if done:
        return done
    if jottings.is_path(rel):
        return _save_jotting(task, root)
    if task.get("replace") and clips.is_clip_path(rel):
        return _save_clip(task, root)
    if task.get("base"):
        return _save_noted(task, root)
    if task.get("replace") and rel.startswith(REVIEW_DIR + "/"):
        full = os.path.realpath(os.path.join(root, *rel.split("/")))
        if os.path.commonpath([full, root]) != root:
            raise TaskError("路径不在笔记库里")
        os.makedirs(os.path.dirname(full), exist_ok=True)
        atomic.write_text(full, task["content"])
        _applied(task, full)
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
                        _applied(task, full)
                        return f"笔记库里已经有了：`{cand}`"
            except OSError:
                pass
            continue
        os.makedirs(os.path.dirname(full), exist_ok=True)
        atomic.write_text(full, task["content"])
        _applied(task, full)
        return f"存进了笔记库：`{cand}`"
    raise TaskError("同名文件太多了，没存")


def _save_clip(task: dict, root: str) -> str:
    rel = clips.renamed(root, task["path"])   # 剪藏按发表日期改过名：手机上旧名字的修改写进新名字那篇
    full = os.path.realpath(os.path.join(root, *rel.split("/")))
    if os.path.commonpath([full, root]) != root:
        raise TaskError("路径不在笔记库里")
    content = task["content"]
    before: list[str] = []
    if os.path.isfile(full):
        with open(full, "rb") as f:
            current = f.read()
        # 旧版手机整篇交来的还是旧的时间写法（- 发布：、- 剪藏：），文件头也没有 clipped_at 这些：
        # 电脑上这篇已经是新写法的，时间行和这几个字段接过来，不被旧版盖回去
        content = doctime.carry_time_header(current.decode("utf-8", "replace"), content)
        # 放回原处、或者手机上新存的（new）、不带 bases，电脑上已经有这个文件：不覆盖。一样、或者已经在里面才算做过；
        # 别的：放回的当冲突，新存的是同名的另一篇（另一台设备同一天剪了同一篇，kind=taken，手机换个名字重交）。
        # 不带 new 的老版手机照旧直接覆盖
        if (task.get("restore") or task.get("new")) and not _bases(task) and current != content.encode("utf-8"):
            if _contained(root, rel, task, current):
                _applied(task, full)
                return f"笔记库里已经有这一版的改动：`{rel}`"
            _keep_phone_version(rel, task["content"], "conflict" if task.get("restore") else "taken")
        # 先看是不是手机改之前的某一版（是就照写，撤了划线也写）；不是再看是不是晚到的、已经包含在里面的旧任务
        if _bases(task) and current != content.encode("utf-8") and not _same_version(root, rel, task, current):
            if _contained(root, rel, task, current):
                _applied(task, full)
                return f"笔记库里已经有这一版的改动：`{rel}`"
            _keep_phone_version(rel, task["content"])
        before = [_sha(current)]
    elif _bases(task) and not task.get("new") and not task.get("restore"):
        # 带着 base 改的一篇（电脑上原来有），电脑上已经没有了（删了、移进了废纸篓）：不把它变回来，手机上那一版留着。
        # 手机上新建的（new）照写：第一份可能根本没到电脑
        raise TaskError(f"电脑上已经没有这篇了（删了或移进了废纸篓），手机上这次修改没存：`{rel}`", kind="gone")
    os.makedirs(os.path.dirname(full), exist_ok=True)
    content = _carry_merged(root, rel, content)
    atomic.write_text(full, content)
    _record_edit(task, root, rel, content, _bases(task) + before)
    _applied(task, full)
    got, failed = clips.download_images(task.get("images") or {}, root)
    if failed:   # 小红书的签名地址过期了：重新打开笔记拿新地址补下
        more = clips.refresh_signed_images(task.get("images") or {}, clips.frontmatter(task["content"]).get("url") or "", root)
        got, failed = got + more, failed - more
    clips.rebuild_index(root)
    tail = f"，图片下了 {got} 张" if got else ""
    tail += f"（{failed} 张没下成）" if failed else ""
    return f"剪藏存进了笔记库：`{rel}`{tail}" + _journal_note(task)


def _save_jotting(task: dict, root: str) -> str:
    """手机上写的随手记（Spark/笔记/，core/jottings.py）。规矩同别的手机修改：收不收只看内容，不看时间
    （edited_at 只决定先做哪件）。
    - 电脑上没有这个文件：手机上新建的（new，或者不带 bases 的老写法）写在手机定的那个名字下，记进修改记录；
      改已有的一条（带 bases、不是 new）算 gone，不把它变回来；
    - 电脑上有：一模一样 → 做过；电脑上是手机改之前的某一版（bases）→ 写（_save_noted）；手机这份是现在这版的祖先 → 做过；
      别的都不覆盖，手机这一版存进 note-conflicts/：手机上新建的、交来的这份和它的来历电脑都没见过，是同名的另一篇
      （kind=taken，手机换个名字重交）；别的是两边都改过（kind=conflict）。"""
    rel = task["path"]
    full = os.path.realpath(os.path.join(root, *rel.split("/")))
    if os.path.commonpath([full, root]) != root:
        raise TaskError("路径不在笔记库里")
    if not os.path.isfile(full):
        if _bases(task) and not task.get("new") and not task.get("restore"):   # 放回原处的照建
            raise TaskError(f"电脑上已经没有这篇了：`{rel}`", kind="gone")
        return _create_new(task, root, rel, full)
    with open(full, "rb") as f:
        current = f.read()
    if current == task["content"].encode("utf-8"):
        _applied(task, full)
        return f"笔记库里已经是这一版：`{rel}`"
    if _bases(task) and _same_version(root, rel, task, current):
        return _save_noted(task, root)
    if _contained(root, rel, task, current):
        _applied(task, full)
        return f"笔记库里已经有这一版的改动：`{rel}`"
    # 手机上新建的这一篇（不带 bases 的是老写法的新建）；放回原处的不算新建，电脑上已有的是同一篇
    new = (task.get("new") or not _bases(task)) and not task.get("restore")
    _keep_phone_version(rel, task["content"], "taken" if new and not _known(root, rel, task) else "conflict")


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
               "paused": "暂停了", "queued": "排队", "rename": "整理文件名", "repair": "检查已有的文件",
               "translate": "翻译"}


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
                "purge_images": self._purge_images, "cleanup": self._cleanup,
                "track_item": self._track_item, "episode_item": self._track_item}[task["type"]](task)

    # -- 手机废纸篓里彻底删除了一篇：只有它用的图片移进废纸篓
    def _purge_images(self, task: dict) -> str:
        return purge_images(task)

    # -- 手机上的清理工具：扫描、按人的选择清理（电脑自己再查一遍，见 core/cleanup.py）
    def _cleanup(self, task: dict) -> str:
        from core import cleanup
        return cleanup.run_task(task)

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
            if title in (jottings.DIRNAME, os.path.basename(clips.CLIPS_DIR), "信息跟进"):
                title += "（节目）"   # 这几个名字是随手记、剪藏、信息跟进自己的文件夹
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
            warned = 0
            if sub.get("kind") == "podcast":
                job = self._post(f"/summit/api/subscriptions/{sub['id']}/update", {"entry_ids": ids, **extra})
                self.subject = name
                self._wait(f"/summit/api/status/{job['job_id']}", f"「{name}」")
            else:
                job = self._post("/summit/api/track/run",
                                 {"selections": [{"sub_id": sub["id"], "entry_ids": ids}], **extra})
                self.subject = name
                st = self._wait(f"/summit/api/track/status/{job['job_id']}", f"「{name}」")
                # 外文文章抓取时自动配上译文（tracking.process_item）；有段落没译成的不算失败，提一句
                warned = len(((st or {}).get("result") or {}).get("warnings") or [])
            more = len(new) - len(ids)
            lines.append(f"「{name}」处理了 {len(ids)} 条新内容" + (f"，还有 {more} 条没处理" if more > 0 else "")
                         + (f"，{warned} 篇的译文没译全" if warned else ""))
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
            self._fail(gh, number, str(e), progress, kind=e.kind)
            return 1
        except Exception as e:  # noqa: BLE001
            gh.remove_label(number, RUNNING_LABEL)
            self._fail(gh, number, f"意外错误：{e}", progress)
            return 1
        self._busy_until.pop(number, None)
        self._busy_noted.discard(number)
        extra = {"applied_sha": task["_applied_sha"]} if task.get("_applied_sha") else {}
        body = status_comment("done", f"完成：{text}", **extra)
        if progress.comment_id:
            gh.edit_comment(progress.comment_id, body)
        else:
            gh.comment(number, body)
        gh.remove_label(number, RUNNING_LABEL)
        gh.close(number)
        return 1

    def _fail(self, gh: GitHub, number: int, reason: str, progress: Optional[Progress] = None, kind: str = "rejected") -> None:
        body = status_comment("failed", f"没成功：{reason}", kind=kind)
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
