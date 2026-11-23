"""
文档头里的两行时间：「- 发表时间：…」「- 收录时间：…」，以及跟着写进文件头、处理记录的机器读的字段。
跟手机 apps/mobile-ios/Shared/DocTime.swift 一字不差（tests/test_doctime.py 对照）。

写法：
- 知道时刻：2026年11月19日，21:12（UTC+8）——换算到写文件这台机器此刻的时区，时区标在括号里；
- 只知道哪天：2026年11月19日——不带时刻、不带时区，不编 00:00；
- 不知道发表时间：不写这一行。收录时间总有时刻（收进来、处理的那一刻）。
机器读的：ISO 8601 带时差（2026-11-19T21:12:00+08:00），旧字段（published、clipped、播出、publish_date）照旧写，
知道时刻时旧字段的日期取这一刻在本机时区的那天。

旧文件里的写法（- 发布：、- 剪藏：、- 播出时间：、- 发布日期：）读的时候都认，见 PUBLISHED_LINE / CAPTURED_LINE。
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Optional, Union

When = Union[datetime, date]

PUBLISHED_LABEL = "发表时间"
CAPTURED_LABEL = "收录时间"
MODIFIED_LABEL = "修改时间"   # 随手记改过以后才有（core/jottings.py），每改一次换掉
# 文档头里的发表时间、收录时间（新旧写法都认）。第 1 组是值
PUBLISHED_LINE = re.compile(r"^- (?:发表时间|发布|播出时间|发布日期)：(.+?)\s*$", re.M)
CAPTURED_LINE = re.compile(r"^- (?:收录时间|剪藏)：(.+?)\s*$", re.M)
# 任一种时间行（换写法时整行换掉）
ANY_TIME_LINE = re.compile(r"^- (?:发表时间|发布|播出时间|发布日期|收录时间|剪藏)：.*$", re.M)

# 认的写法（手机 DocTime.swift 一模一样）：
# - 日期：YYYY-MM-DD、YYYYMMDD、2026年11月19日；
# - 时刻：YYYY-MM-DD[T 空格]HH:MM[:SS[.小数]] 后面紧跟时差 Z / ±HH / ±HHMM / ±HH:MM，可以再跟一个 [时区名]（不看）；
#   2026年11月19日，21:12（UTC+8）。秒的小数不要。
# - 有日期有时刻、但没有时差（或者时差写法不认识）：只取日期，不猜时区。
# - 时刻、日期不在 1990-01-01 到「现在 + 2 天」之间的不认（毫秒当秒、9999 年这类坏数据）。
_CN_RE = re.compile(r"^([0-9]{4})年([0-9]{1,2})月([0-9]{1,2})日(?:，([0-9]{1,2}):([0-9]{2})（UTC([+-])([0-9]{1,2})(?::([0-9]{2}))?）)?$")
_ISO_RE = re.compile(r"^([0-9]{4})-([0-9]{2})-([0-9]{2})(?:[T ]([0-9]{2}):([0-9]{2})(?::([0-9]{2})(?:[.,][0-9]+)?)?"
                     r"(Z|z|[+-][0-9]{2}(?::?[0-9]{2})?)?(?:\[[^\]\s]*\])?)?$")
_DATE_TIME_PREFIX = re.compile(r"^([0-9]{4})-([0-9]{2})-([0-9]{2})[T ][0-9]{2}:[0-9]{2}")
EARLIEST = datetime(1990, 1, 1, tzinfo=timezone.utc)
SLACK = timedelta(days=2)


def now() -> datetime:
    """此刻，带本机时区。"""
    return datetime.now().astimezone()


def local(dt: datetime) -> datetime:
    """换算到本机此刻的时区（没带时区的当成本机时间）。"""
    return dt.astimezone()


def zone_label(dt: datetime) -> str:
    """UTC+8、UTC-7、UTC+5:30、UTC+0。"""
    off = dt.utcoffset() or timedelta(0)
    mins = int(off.total_seconds() // 60)
    sign = "-" if mins < 0 else "+"
    h, m = divmod(abs(mins), 60)
    return f"UTC{sign}{h}" + (f":{m:02d}" if m else "")


def cn(value: Optional[When], keep_zone: bool = False) -> str:
    """给人看的写法；没有值返回空串。keep_zone：时刻照它自己写的时差显示，不换算到本机（迁移老文件时用）。"""
    if value is None:
        return ""
    if isinstance(value, datetime):
        d = value if keep_zone and value.tzinfo else local(value)
        return f"{d.year:04d}年{d.month:02d}月{d.day:02d}日，{d.hour:02d}:{d.minute:02d}（{zone_label(d)}）"
    return f"{value.year:04d}年{value.month:02d}月{value.day:02d}日"


def iso(dt: datetime) -> str:
    """机器读的：本机时区、到秒、带时差（+00:00 不写成 Z）。"""
    return local(dt).replace(microsecond=0).isoformat()


def day(value: When) -> date:
    """写文件时用：知道时刻的取这一刻在本机时区的那天。读的时候别用它算哪天，用文件里写着的日期（written_day）。"""
    return local(value).date() if isinstance(value, datetime) else value


def written_day(value: When) -> date:
    """时刻照它自己写的时差是哪天（跟看的人在哪个时区无关）。"""
    return value.date() if isinstance(value, datetime) else value


def _ok_instant(dt: datetime) -> bool:
    return EARLIEST <= dt <= datetime.now(timezone.utc) + SLACK


def _ok_day(d: date) -> bool:
    return date(1990, 1, 1) <= d <= (datetime.now(timezone.utc) + SLACK).date()


def from_epoch(seconds: float) -> Optional[datetime]:
    """秒数（13 位的当毫秒）→ 本机时区的时刻；不在合理范围里返回 None。"""
    try:
        v = float(seconds)
    except (TypeError, ValueError):
        return None
    if v >= 1e11:
        v /= 1000
    try:
        dt = datetime.fromtimestamp(v, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return dt.astimezone() if _ok_instant(dt) else None


def _day(y: str, m: str, d: str) -> Optional[date]:
    try:
        v = date(int(y), int(m), int(d))
    except ValueError:
        return None
    return v if _ok_day(v) else None


def _instant(d: date, hh: str, mm: str, ss: str, off_minutes: int) -> Optional[datetime]:
    h, mi, se = int(hh), int(mm), int(ss or 0)
    if h > 23 or mi > 59 or se > 59 or abs(off_minutes) > 14 * 60:
        return None
    dt = datetime(d.year, d.month, d.day, h, mi, se, tzinfo=timezone(timedelta(minutes=off_minutes)))
    return dt if _ok_instant(dt) else None


def parse(text: object) -> Optional[When]:
    """按上面的写法认，认不出返回 None。"""
    s = str(text or "").strip().strip('"').strip()
    if not s:
        return None
    m = _CN_RE.match(s)
    if m:
        d = _day(m[1], m[2], m[3])
        if d is None or not m[4]:
            return d
        if int(m[8] or 0) > 59:
            return None
        return _instant(d, m[4], m[5], "", (int(m[7]) * 60 + int(m[8] or 0)) * (-1 if m[6] == "-" else 1))
    if re.fullmatch(r"[0-9]{8}", s):
        return _day(s[:4], s[4:6], s[6:])
    m = _ISO_RE.match(s)
    if m:
        d = _day(m[1], m[2], m[3])
        if d is None or not m[4]:
            return d
        off = m[7]
        if not off:
            return d if int(m[4]) < 24 and int(m[5]) < 60 and int(m[6] or 0) < 60 else None
        if off in ("Z", "z"):
            minutes = 0
        else:
            digits = off[1:].replace(":", "")
            if int(digits[2:] or 0) > 59:
                return None
            minutes = (int(digits[:2]) * 60 + int(digits[2:] or 0)) * (-1 if off[0] == "-" else 1)
        return _instant(d, m[4], m[5], m[6], minutes)
    m = _DATE_TIME_PREFIX.match(s)
    if m:   # 有时刻但时差写法不认识：只取日期
        return _day(m[1], m[2], m[3])
    return None


def parse_local(text: object) -> Optional[datetime]:
    """本机自己记的时刻（处理记录的 updated_at：2026-11-20T08:05:31，不带时差，是这台电脑的本地时间）。
    带时差的照常认；认不出返回 None。"""
    s = str(text or "").strip()
    if not re.match(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}", s):
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.astimezone()


def best(at: object, day_text: object = "") -> Optional[When]:
    """有时刻用时刻，没有用日期（处理记录里 publish_at + publish_date、文件头 published_at + published 这样成对的）。"""
    v = parse(at)
    if isinstance(v, datetime):
        return v
    return parse(day_text) or (v if isinstance(v, date) else None)


def lines(published: Optional[When], captured: Optional[When]) -> list[str]:
    """文档头的两行；不知道发表时间就只有收录时间。"""
    out = []
    if published is not None:
        out.append(f"- {PUBLISHED_LABEL}：{cn(published)}")
    if captured is not None:
        out.append(f"- {CAPTURED_LABEL}：{cn(captured)}")
    return out


def modified_line(at: datetime) -> str:
    """「- 修改时间：2026年11月22日，21:42（UTC+8）」：随手记改过以后写在收录时间下面（同手机 DocTime.modifiedLine）。"""
    return f"- {MODIFIED_LABEL}：{cn(at)}"


def stamp_entry(entry: dict, value: Optional[When]) -> dict:
    """把发表时间记进处理记录的条目：publish_date（YYYYMMDD，本机那天）；知道时刻再加 publish_at。"""
    if value is None:
        return entry
    entry["publish_date"] = day(value).strftime("%Y%m%d")
    if isinstance(value, datetime):
        entry["publish_at"] = iso(value)
    else:
        entry.pop("publish_at", None)
    return entry


def entry_published(entry: dict) -> Optional[When]:
    """条目的发表时间：publish_at 优先，没有用 publish_date。publish_at 照它自己的时差跟 publish_date 不是同一天
    （日期被别处改过）时信日期。"""
    v = best(entry.get("publish_at"), entry.get("publish_date"))
    d = parse(entry.get("publish_date"))
    if isinstance(v, datetime) and isinstance(d, date) and not isinstance(d, datetime) and v.date() != d:
        return d
    return v


# ---------------------------------------------------------------- 文档头里的时间行（重写、接回旧值）

_NEW_PUB = re.compile(r"^- 发表时间：(.+?)\s*$")
_NEW_CAP = re.compile(r"^- 收录时间：(.+?)\s*$")
_OLD_PUB = re.compile(r"^- (?:发布|播出时间|发布日期)：(.+?)\s*$")
_OLD_CAP = re.compile(r"^- 剪藏：(.+?)\s*$")
_HEAD_ITEM = re.compile(r"^- [^：\n]{1,16}：")


def split_front(text: str) -> tuple[list[str], list[str]]:
    """(文件头的行，含首尾 ---；正文的行)。没有文件头时前一个是空的。按 \\n 分，不动别的字符。"""
    lines = text.split("\n")
    if lines and lines[0] == "---":
        for i in range(1, len(lines)):
            if lines[i] == "---":
                return lines[:i + 1], lines[i + 1:]
    return [], lines


def header_span(body: list[str]) -> Optional[tuple[int, int, int]]:
    """标题下面那串「- 键：值」：(标题在第几行, 列表开始, 列表结束后一行)。
    标题必须是正文第一个不空的行（不是的返回 None，不动）；标题后面直接没有列表时开始=结束。"""
    t = next((i for i, ln in enumerate(body) if ln.strip()), None)
    if t is None or not body[t].startswith("# "):
        return None
    i = t + 1
    while i < len(body) and body[i] == "":
        i += 1
    start = i
    while i < len(body) and _HEAD_ITEM.match(body[i]):
        i += 1
    return t, start, i


def time_line_kind(line: str) -> str:
    """pub_new / cap_new / pub_old / cap_old / 空串。"""
    for kind, rx in (("pub_new", _NEW_PUB), ("cap_new", _NEW_CAP), ("pub_old", _OLD_PUB), ("cap_old", _OLD_CAP)):
        if rx.match(line):
            return kind
    return ""


def as_new_line(line: str) -> Optional[str]:
    """一行时间换成新写法：新写法原样；旧写法（只有日期）换成只写日期的新写法；认不出返回 None。"""
    kind = time_line_kind(line)
    if kind in ("pub_new", "cap_new"):
        return line
    if not kind:
        return None
    v = parse((_OLD_PUB if kind == "pub_old" else _OLD_CAP).match(line).group(1))
    if v is None:
        return None
    label = PUBLISHED_LABEL if kind == "pub_old" else CAPTURED_LABEL
    return f"- {label}：{cn(written_day(v))}"


def header_time_lines(text: str) -> dict[str, str]:
    """文档头里的时间行：{"pub": 行, "cap": 行}，都换成新写法（旧写法只有日期）。没有的不在里面。"""
    _front, body = split_front(text)
    span = header_span(body)
    out: dict[str, str] = {}
    if span is None:
        return out
    for ln in body[span[1]:span[2]]:
        kind = time_line_kind(ln)
        key = "pub" if kind.startswith("pub") else "cap" if kind else ""
        if key and key not in out:
            new = as_new_line(ln)
            if new:
                out[key] = new
    return out


def keep_time_lines(old: str, new: str) -> str:
    """整篇重写一篇已有的文档时（重新生成整理稿、文字记录、单集笔记）：
    - 新内容文档头里没有发表时间、旧文件里有：接过来（旧写法换成只写日期的新写法），文件里已有的日期不丢；
    - 旧文件里有收录时间：用旧的（收进来的时候不变）。"""
    have = header_time_lines(old)
    if not have:
        return new
    front, body = split_front(new)
    span = header_span(body)
    if span is None or span[1] == span[2]:
        return new
    _t, start, end = span
    items = body[start:end]
    kinds = [time_line_kind(ln) for ln in items]
    if "cap_new" in kinds and "cap" in have:
        items[kinds.index("cap_new")] = have["cap"]
    if "pub_new" not in kinds and "pub_old" not in kinds and "pub" in have:
        at = kinds.index("cap_new") if "cap_new" in kinds else len(items)
        items.insert(at, have["pub"])
    return "\n".join(front + body[:start] + items + body[end:])


_CARRY_KEYS = ("published_at", "clipped", "clipped_at", "captured_at")
_CARRY_AFTER = {"published_at": ("title", "source", "url", "published", "播出"),
                "clipped": ("title", "source", "url", "published", "published_at"),
                "clipped_at": ("title", "source", "url", "published", "published_at", "clipped"),
                "captured_at": ("节目", "播出", "published_at")}


def carry_time_header(existing: str, incoming: str) -> str:
    """incoming（旧版手机交来的、迁移前那一版改出来的）文档头里没有新写法的时间行：
    把 existing（笔记库里现在那篇）的两行时间接过来，替掉 incoming 里旧写法的时间行；
    文件头的 published_at、clipped、clipped_at、captured_at，incoming 没有、existing 有的也接过来。别的一字不动。"""
    e_front, e_body = split_front(existing)
    i_front, i_body = split_front(incoming)
    # 文件头
    if e_front and i_front:
        e_keys = {ln.split(":", 1)[0]: ln for ln in e_front[1:-1] if ":" in ln}
        for key in _CARRY_KEYS:
            if key not in e_keys or any(ln.startswith(key + ":") for ln in i_front[1:-1]):
                continue
            pos = len(i_front) - 1
            for j, ln in enumerate(i_front[1:-1], 1):
                if ln.split(":", 1)[0] in _CARRY_AFTER[key]:
                    pos = j + 1
            i_front = i_front[:pos] + [e_keys[key]] + i_front[pos:]
    # 两行时间
    e_span, i_span = header_span(e_body), header_span(i_body)
    if e_span and i_span:
        e_lines = [ln for ln in e_body[e_span[1]:e_span[2]] if time_line_kind(ln) in ("pub_new", "cap_new")]
        _t, start, end = i_span
        items = i_body[start:end]
        kinds = [time_line_kind(ln) for ln in items]
        if e_lines and not any(k in ("pub_new", "cap_new") for k in kinds):
            olds = [j for j, k in enumerate(kinds) if k]
            if olds:
                at = olds[0]
                items = [ln for j, ln in enumerate(items) if j not in olds]
            else:
                src = next((j for j, ln in enumerate(items) if ln.startswith("- 来源：")), None)
                at = src + 1 if src is not None else len(items)
            items = items[:at] + e_lines + items[at:]
            i_body = i_body[:start] + items + i_body[end:]
    return "\n".join(i_front + i_body)


def sort_key(at: object, day_text: object) -> tuple:
    """排先后用（倒序时新的在前）：(写着的那天, 有没有时刻, 时刻的秒数)。
    哪天以文件、处理记录里写着的日期为准（写的时候定下来的），跟看的人在哪个时区无关；
    没写日期只有时刻的，取时刻照它自己时差的那天。同一天里有时刻的排在只有日期的前面，时刻比绝对秒数。
    跟手机 DocTime.sortKey 排出来的先后一样。"""
    inst = parse(at)
    inst = inst if isinstance(inst, datetime) else None
    d = parse(day_text)
    d = written_day(d) if d is not None else (inst.date() if inst else None)
    if d is None:
        return ("", 0, 0)
    return (d.isoformat(), 1, int(inst.timestamp() // 1)) if inst else (d.isoformat(), 0, 0)
