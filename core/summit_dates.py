"""
会议是哪几天开的（会议时间）、视频是哪几天发布的，和处理记录（.manifest.json）的读改写锁。

处理记录里两种时间，别混：
- 会议时间：顶层 event_dates = {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD", "source": …}，会议真正开的那几天。
  只存两种 source：manual（在阅读页上手动填的，自动的代码永远不改它）、agenda（官网议程里有日期）。
  老版本存过 source=sessions 的：读到就当没存，自动的代码碰到就删掉（现推就有，存一份会过时）。
- 各场的时间：entry.publish_date（+ publish_at）是视频发布那天（写的那台电脑的本地日期）；
  entry.session_date（YYYY-MM-DD）是这一场在议程上是哪天开的（会场当地的日期，议程给了日期才有）。
  两种日期不混在一起算最早到最晚。

显示的时候（resolve）：
1. 存着的 manual / agenda；
2. 现推议程日期（session_date）的最早到最晚，source=agenda；
3. 现推视频发布日期（publish_date）的最早到最晚，source=sessions——这不是会议时间，显示成「视频发布：…」；
   跨了 MAX_DERIVED_SPAN_DAYS 天以上不显示。
2、3 都要日期够多：有日期的场数至少一半，而且至少 2 场（整个会议只有 1 场的除外），不然什么都不推。
宁可空着也不显示错的。

写法（label）：2026年10月13日 / 2026年10月13日—16日 / 2026年9月30日—10月2日 / 2026年12月30日—2027年1月2日；
前面加「会议时间：」（manual、agenda）或「视频发布：」（sessions），见 display。
跟手机 apps/mobile-ios/Spark/SummitDates.swift 一字不差（tests/test_summit_dates.py 对照）。

处理记录的锁（manifest_lock）：同一进程里的线程用 RLock，不同进程（补日期的工具）用 Spark 设置目录
（core/settings.settings_path() 所在目录，一般是 ~/.spark）下 locks/ 里的 flock 文件（不放进笔记库，免得被同步到手机；
不放系统临时目录，Mac App、终端、沙盒里的终端拿到的临时目录可能不一样，系统还会清掉旧文件）。
流水线存处理记录（save_manifest）时先看一眼磁盘上的 event_dates：磁盘上是 manual 的，用磁盘上的；
内存里是 manual、磁盘上已经不是了（在阅读页上清掉了），也用磁盘上的——流水线手里那份是任务开始时读的旧副本，
不能把用户刚改的会议时间冲掉。
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import unicodedata
import threading
from typing import Iterator, Optional

from core import atomic

SOURCES = ("manual", "agenda", "sessions")
# 存进处理记录的只有这两种；sessions 只现推
STORED_SOURCES = ("manual", "agenda")
MEETING_PREFIX = "会议时间："
VIDEO_PREFIX = "视频发布："
# 从各场日期推会议时间时，最早到最晚超过这么多天就不当会议时间（视频上传常常拖几天到几周）
MAX_DERIVED_SPAN_DAYS = 10
# 手动填的会议时间最长几天（再长多半是填错了）
MAX_MANUAL_SPAN_DAYS = 60
DASH = "—"
_ISO_DAY = re.compile(r"^([0-9]{4})-([0-9]{2})-([0-9]{2})$")
_COMPACT_DAY = re.compile(r"^([0-9]{4})([0-9]{2})([0-9]{2})$")


# ---------------------------------------------------------------- 日期（纯函数，跟手机一样）

def _leap(y: int) -> bool:
    return y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)


def _valid(y: int, m: int, d: int) -> bool:
    if not (1990 <= y <= 2100 and 1 <= m <= 12):
        return False
    days = [31, 29 if _leap(y) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1]
    return 1 <= d <= days


def iso_day(text: object) -> str:
    """YYYY-MM-DD 或 YYYYMMDD → YYYY-MM-DD；不是真日期（2 月 30 日、1990 年以前、2100 年以后）返回空串。"""
    s = text.strip() if isinstance(text, str) else ""
    m = _ISO_DAY.match(s) or _COMPACT_DAY.match(s)
    if not m:
        return ""
    y, mo, d = int(m[1]), int(m[2]), int(m[3])
    return f"{y:04d}-{mo:02d}-{d:02d}" if _valid(y, mo, d) else ""


def _ordinal(day: str) -> int:
    """YYYY-MM-DD → 从 1970-01-01 算起第几天（days_from_civil，整数运算，手机上一样算）。"""
    y, m, d = int(day[:4]), int(day[5:7]), int(day[8:10])
    y -= m <= 2
    era = y // 400
    yoe = y - era * 400
    doy = (153 * (m + (-3 if m > 2 else 9)) + 2) // 5 + d - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146097 + doe - 719468


def span_days(start: str, end: str) -> int:
    """end 比 start 晚几天（同一天是 0）。"""
    return _ordinal(end) - _ordinal(start)


def label(start: str, end: str = "") -> str:
    """会议时间给人看的写法；start 不是日期返回空串，end 空着或早于 start 当成一天。"""
    a = iso_day(start)
    if not a:
        return ""
    b = iso_day(end) or a
    if b < a:
        b = a
    y1, m1, d1 = int(a[:4]), int(a[5:7]), int(a[8:10])
    y2, m2, d2 = int(b[:4]), int(b[5:7]), int(b[8:10])
    head = f"{y1}年{m1}月{d1}日"
    if a == b:
        return head
    if y1 == y2 and m1 == m2:
        return f"{head}{DASH}{d2}日"
    if y1 == y2:
        return f"{head}{DASH}{m2}月{d2}日"
    return f"{head}{DASH}{y2}年{m2}月{d2}日"


def session_day(entry: object) -> str:
    """一场在议程上是哪天开的（session_date）；没有返回空串。视频发布那天不算，不拿来当这一场的日期。"""
    return iso_day(entry.get("session_date")) if isinstance(entry, dict) else ""


def publish_day(entry: object) -> str:
    """一场的视频是哪天发布的（publish_date）；没有、或者是手机上猜的（publish_guessed）返回空串。"""
    if not isinstance(entry, dict) or entry.get("publish_guessed"):
        return ""
    return iso_day(entry.get("publish_date"))


def display(r: Optional[dict]) -> str:
    """给人看的一整句：「会议时间：…」（manual、agenda）/「视频发布：…」（sessions）；没有返回空串。"""
    if not r:
        return ""
    text = label(r["start"], r["end"])
    if not text:
        return ""
    return (VIDEO_PREFIX if r["source"] == "sessions" else MEETING_PREFIX) + text


def stored(manifest: object) -> Optional[dict]:
    """处理记录里存着的 event_dates（manual / agenda，检查过的）；没有、不合法、是老的 sessions 返回 None。"""
    ed = manifest.get("event_dates") if isinstance(manifest, dict) else None
    if not isinstance(ed, dict):
        return None
    start = iso_day(ed.get("start"))
    end = iso_day(ed.get("end")) or start
    source = ed.get("source")
    if not start or end < start or source not in STORED_SOURCES:
        return None
    return {"start": start, "end": end, "source": source}


def _entries(manifest: object) -> list:
    entries = manifest.get("entries") if isinstance(manifest, dict) else None
    if not isinstance(entries, dict):
        return []
    return [r["entry"] for r in entries.values() if isinstance(r, dict) and isinstance(r.get("entry"), dict)]


def enough(dated: int, total: int) -> bool:
    """日期够不够多：至少一半的场有日期，而且至少 2 场（整个会议只有 1 场的除外）。"""
    return total > 0 and dated * 2 >= total and (dated >= 2 or total == 1)


def coverage(manifest: object, kind: str) -> tuple[int, int]:
    """(有日期的场数, 总场数)；kind 是 "agenda"（session_date）或 "video"（publish_date）。"""
    entries = _entries(manifest)
    pick = session_day if kind == "agenda" else publish_day
    return sum(1 for e in entries if pick(e)), len(entries)


def _range(manifest: object, pick, source: str) -> Optional[dict]:
    entries = _entries(manifest)
    days = sorted(d for d in (pick(e) for e in entries) if d)
    if not days or not enough(len(days), len(entries)):
        return None
    return {"start": days[0], "end": days[-1], "source": source}


def agenda_range(manifest: object) -> Optional[dict]:
    """议程给了日期的那几场（只看 session_date）最早到最晚；日期不够多返回 None。"""
    return _range(manifest, session_day, "agenda")


def derived(manifest: object) -> Optional[dict]:
    """视频发布日期（只看 publish_date）最早到最晚，source=sessions；日期不够多、跨度超过
    MAX_DERIVED_SPAN_DAYS 天返回 None。"""
    r = _range(manifest, publish_day, "sessions")
    if r is None or span_days(r["start"], r["end"]) > MAX_DERIVED_SPAN_DAYS:
        return None
    return r


def resolve(manifest: object) -> Optional[dict]:
    """显示用的：存着的 manual / agenda，没有就现推议程日期，再没有现推视频发布日期。"""
    return stored(manifest) or agenda_range(manifest) or derived(manifest)


def publish_spread(manifest: object) -> tuple[str, str]:
    """视频发布最早、最晚的一天（报告里给手动填会议时间当提示）；没有返回 ("", "")。"""
    days = sorted(d for d in (publish_day(e) for e in _entries(manifest)) if d)
    return (days[0], days[-1]) if days else ("", "")


def sort_key(manifest: object) -> str:
    """按会议时间排用：开始那天（YYYY-MM-DD）；没有是空串。"""
    r = resolve(manifest)
    return r["start"] if r else ""


def auto_event_dates(manifest: dict) -> bool:
    """自动更新存着的会议时间（会议目录那份处理记录：流水线、整理会议目录、补日期工具）。
    manual 永远不动；议程日期够多就存 agenda；存着 agenda 的、这次议程日期不够，留着；
    存着老的 sessions（或不合法的）就删掉——视频发布日期现推就有，不存。改了返回 True。"""
    old = manifest.get("event_dates")
    current = stored(manifest)
    if current and current["source"] == "manual":
        return False
    new = agenda_range(manifest)
    if new is None:
        if current:          # 存着的 agenda
            return False
        if "event_dates" in manifest:
            manifest.pop("event_dates")
            return True
        return False
    if old == new:
        return False
    manifest["event_dates"] = new
    return True


def strip_event_dates(manifest: dict) -> bool:
    """专题文件夹的处理记录不存会议时间：自动存的（不是 manual）去掉。改了返回 True。"""
    ed = manifest.get("event_dates")
    if ed is None or (isinstance(ed, dict) and ed.get("source") == "manual"):
        return False
    manifest.pop("event_dates")
    return True


def check_manual(start: str, end: str) -> tuple[str, str]:
    """检查手动填的会议时间：返回 (start, end)；不对抛 ValueError（中文原因）。end 空着当成一天。"""
    a = iso_day(start)
    if not a or not _ISO_DAY.match(str(start).strip()):
        raise ValueError("开始日期要写成 2026-10-13 这样的真实日期")
    b = a
    if str(end or "").strip():
        b = iso_day(end)
        if not b or not _ISO_DAY.match(str(end).strip()):
            raise ValueError("结束日期要写成 2026-10-16 这样的真实日期")
    if b < a:
        raise ValueError("结束日期不能早于开始日期")
    if span_days(a, b) > MAX_MANUAL_SPAN_DAYS:
        raise ValueError(f"会议时间最长 {MAX_MANUAL_SPAN_DAYS} 天")
    return a, b


# ---------------------------------------------------------------- 处理记录的锁和读改写

class _PathLock:
    def __init__(self) -> None:
        self.rlock = threading.RLock()
        self.depth = 0
        self.fd: Optional[int] = None


_LOCKS: dict[str, _PathLock] = {}
_LOCKS_GUARD = threading.Lock()


def lock_dir() -> str:
    """锁文件放在 Spark 设置目录下的 locks/（跟 settings.json 一起；测试里设置文件指到临时目录，锁也跟着去）。"""
    from core import settings as core_settings
    return os.path.join(os.path.dirname(core_settings.settings_path()), "locks")


def lock_key(out_dir: str) -> str:
    """同一个文件夹不管怎么写路径都是同一把锁：真实路径，NFC 规范化，大小写折叠（macOS 文件系统不分大小写）。"""
    return unicodedata.normalize("NFC", os.path.realpath(out_dir)).casefold()


def _lock_file(key: str) -> str:
    folder = lock_dir()
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, hashlib.sha1(key.encode("utf-8")).hexdigest() + ".lock")


@contextlib.contextmanager
def manifest_lock(out_dir: str) -> Iterator[None]:
    """读改写一个文件夹的 .manifest.json 时拿着：同一线程可以嵌套。"""
    key = lock_key(out_dir)
    with _LOCKS_GUARD:
        lock = _LOCKS.setdefault(key, _PathLock())
    with lock.rlock:
        if lock.depth == 0:
            fd = os.open(_lock_file(key), os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
            except OSError:
                os.close(fd)
                raise
            lock.fd = fd
        lock.depth += 1
        try:
            yield
        finally:
            lock.depth -= 1
            if lock.depth == 0 and lock.fd is not None:
                try:
                    fcntl.flock(lock.fd, fcntl.LOCK_UN)
                finally:
                    os.close(lock.fd)
                    lock.fd = None


def manifest_path(out_dir: str) -> str:
    return os.path.join(out_dir, ".manifest.json")


def _disk_event_dates(path: str) -> tuple[bool, object]:
    """(读到了没有, 磁盘上的 event_dates 原样)。文件不在、读不出来返回 (False, None)。"""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return False, None
    if b"event_dates" not in raw:
        return True, None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return False, None
    return True, (data.get("event_dates") if isinstance(data, dict) else None)


def _is_manual(value: object) -> bool:
    return isinstance(value, dict) and value.get("source") == "manual"


def adopt_disk_event_dates(out_dir: str, manifest: dict) -> None:
    """存之前（拿着锁）：手动填的会议时间以磁盘上的为准，改进内存里这份（以后再存也一致）。"""
    ok, disk = _disk_event_dates(manifest_path(out_dir))
    if not ok:
        return
    mem = manifest.get("event_dates")
    if _is_manual(disk):
        if mem != disk:
            manifest["event_dates"] = disk
    elif _is_manual(mem):
        if disk is None:
            manifest.pop("event_dates", None)
        else:
            manifest["event_dates"] = disk


def save_manifest(out_dir: str, manifest: dict) -> None:
    """存处理记录（流水线、整理会议目录都走这里）：拿着锁，先接上磁盘上手动填的会议时间，再原子写。
    有意这样：内存里是 manual、磁盘上没有（或已不是 manual）的，丢掉内存里的，用磁盘上的——那是任务开始时读的旧副本，
    磁盘上的才是阅读页上最新改的。手动填的会议时间只由 set_manual 写，这里永远不会写进新的 manual。"""
    with manifest_lock(out_dir):
        adopt_disk_event_dates(out_dir, manifest)
        atomic.write_json(manifest_path(out_dir), manifest, indent=2)


def read_manifest(out_dir: str) -> dict:
    """读处理记录；文件不在、读不出来、不是 {"entries": {…}} 的抛 ValueError。"""
    try:
        with open(manifest_path(out_dir), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        raise ValueError(f"处理记录读不出来：{e}") from e
    if not isinstance(data, dict) or not isinstance(data.get("entries"), dict):
        raise ValueError("处理记录格式不对")
    return data


def set_manual(out_dir: str, start: str, end: str = "") -> Optional[dict]:
    """在阅读页上改会议时间：start 空着就是清掉手动填的（回到自动）。拿着锁读改写，别的键一个不动。
    返回改完以后显示用的会议时间（resolve）。日期不对抛 ValueError。"""
    value = None
    if str(start or "").strip():
        a, b = check_manual(start, end)
        value = {"start": a, "end": b, "source": "manual"}
    with manifest_lock(out_dir):
        data = read_manifest(out_dir)
        if value is not None:
            data["event_dates"] = value
        elif _is_manual(data.get("event_dates")):
            data.pop("event_dates")
        atomic.write_json(manifest_path(out_dir), data, indent=2)
    return resolve(data)
