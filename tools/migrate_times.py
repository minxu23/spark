"""
把笔记库里已有的文档换成新的时间写法，剪藏按发布那天改名（core/doctime.py）。

    python3 tools/migrate_times.py --root <笔记库>                 # 只列计划，不动文件（默认）
    python3 tools/migrate_times.py --root <笔记库> --apply         # 照计划改

推荐这样跑（先不带 --apply 看一遍计划，再加 --apply）：
    python3 tools/migrate_times.py --root ~/Documents/Obsidian/minxu \
        --readlog ~/.spark/reading-log.jsonl --taihe-home ~/.spark --cable-cache ~/.spark/cable-sha-cache.json --apply
    可选：--readlog ~/.spark/reading-log.jsonl     阅读记录里的路径跟着改名
          --taihe-home ~/.spark                   太和剪藏同步记录（taihe-sync/…/state.json）里的路径跟着改名
          --cable-cache ~/.spark/cable-sha-cache.json   数据线同步的指纹缓存里去掉改过的文件，下次同步重算
          --allow-running-server                  Spark 服务开着（8760 端口，或 SPARK_PORT 指的端口，有人听）也照改（默认不改）

迁移前：每台手机先同步一次，把发件箱清空；停掉 Spark 服务；迁移时关掉 Obsidian。
迁移后：每台手机先同步一次，再在手机上改剪藏。
同一个笔记库同时只能跑一个 --apply（剪藏文件夹里的 .migrate-times.lock，跑完删掉，超过 10 分钟算没人用了）。

做的事：
1. 文档头（正文第一行的「# 标题」下面那串「- 键：值」）里的旧时间行（- 发布：、- 剪藏：、- 播出时间：、- 发布日期：）
   换成「- 发布时间：」「- 收录时间：」两行（标签取自 core/doctime 的 PUBLISHED_LABEL、CAPTURED_LABEL）。
   上一版写的「- 发表时间：…」（core/doctime.LEGACY_PUBLISHED_LABEL）换成新标签「- 发布时间：…」，值一个字不动。
   例外：剪藏以外的文档（YouTube 视频、播客的文字记录、整理稿、单集笔记……）已经写着「- 播出时间：…」的，
   这一行原样留着、一个字不改，只在它下面紧接着加「- 收录时间：…」，不加发布时间（读的人照样认得出发布那天）。
   哪天以文件里已经写着的为准，不按这台电脑的时区重算：
   - 剪藏：发布时间取文件头的 published（published_at 跟它是同一天时连时刻，照它自己的时差写）；
     收录时间取 clipped（老文件只有日期，只写日期）；
   - 节目的文字记录、整理稿、单集笔记，信息跟进的文章，大会议题：发布时间取文件里原来写着的那行，
     没有再看处理记录的 publish_at / publish_date、文件名开头的日期；收录时间取处理记录的 captured_at，
     老记录没有就取 updated_at 那天，只写日期（updated_at 是最后一次处理的时间，不是收进来的那一刻，不写成时刻、不记进处理记录）。
   文件里已经写着的日期一定留着；已经是新写法的行原样不动。处理记录不改。
2. 剪藏的文件名前面的日期换成发布那天（文件头里写着的 published；不知道发布时间的不动）。只用 os.rename，不删、不覆盖：
   新名字被占了就加 _2、_3……；文件头没有 clipped 的先补上（收录日期原来只记在文件名里）。
3. 改名、改写之前先把整个计划记进 Spark/剪藏/.renames.json（迁移记录）：
   {"renames": {旧路径: 新路径}, "rewritten": {现在的路径: {"before": [改写前的 SHA-256], "after": 改写后的 SHA-256}}}。
   剪藏索引、笔记库里指向改了名的剪藏的链接、阅读记录、太和同步记录都按这份记录改（不按这次改了哪几个），
   中途停下再跑一遍会接着改完，跟一次跑完的结果一样。
   手机发件箱里还按旧名字交来的修改、删除，电脑照这份落到新名字那篇；手机改的是迁移前那一版的，电脑认得出，照收不算冲突。
4. 改写过的文件保留原来的修改时间、访问时间和创建时间（笔记列表按修改时间排，Obsidian 能按创建时间排）。
   重写的剪藏索引是新的修改时间。扩展属性（xattr）只在系统提供 os.listxattr 的平台（Linux）上照抄，macOS 上不抄。
5. 再跑一遍什么都不改（幂等）。划线 ==…==、批注 [^note-N]、读后感、文件头里的星标置顶等只换上面说的几行，别的一字不动。

不改、列出来的：CRLF 换行或带 BOM 的文件、正文开头不是「# 标题」的、文档头外面还有时间行的、读不出来的。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import posixpath
import re
import socket
import sys
import time
import urllib.parse
from datetime import date, datetime
from typing import Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import atomic, clips, doctime  # noqa: E402

SPARK = "Spark"
CLIPS = clips.CLIPS_DIR
TRACK = f"{SPARK}/信息跟进"
INDEX = f"{CLIPS}/{clips.INDEX_NAME}"
SERVER_PORT = int(os.environ.get("SPARK_PORT") or 8760)   # 同 spark.py：SPARK_PORT 能改端口
BEFORE_NOTE = "迁移前：每台手机先同步一次，把发件箱清空；停掉 Spark 服务；迁移时关掉 Obsidian"
AFTER_NOTE = "迁移后：每台手机先同步一次，再在手机上改剪藏"
N2I_NOTE = "提醒：notes2insight 缓存的摘要按正文的哈希记，迁移改过的文档下次会重新算一遍摘要"
LOCK_NAME = ".migrate-times.lock"
LOCK_STALE_SECONDS = 10 * 60


def _rename(src: str, dst: str) -> None:
    """只用 rename，不删、不覆盖（调用前已确认 dst 不存在）。单独拿出来方便测中途停下。"""
    os.rename(src, dst)


def server_running(port: int = SERVER_PORT) -> bool:
    """本机这个端口有人在听（Spark 服务开着）。"""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _full(root: str, rel: str) -> str:
    return os.path.join(root, *rel.split("/"))


def _read(path: str) -> Optional[str]:
    """按字节读、按 UTF-8 解，不转换换行。读不出来返回 None。"""
    try:
        with open(path, "rb") as f:
            return f.read().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _write_keep_times(path: str, text: str) -> None:
    """写进去，修改时间、访问时间、创建时间照旧（笔记列表按修改时间排，Obsidian 能按创建时间排，迁移不该改它们）。
    原子写换了一个新文件（新的 inode），创建时间会变成现在：macOS 上先把修改时间设成原来的创建时间
    （修改时间早于创建时间时，系统把创建时间一起往前拉），再设回原来的修改时间。"""
    st = os.stat(path)
    attrs = []
    if hasattr(os, "listxattr"):   # Linux；macOS 的 Python 没有这组函数，不抄
        try:
            attrs = [(n, os.getxattr(path, n)) for n in os.listxattr(path)]
        except OSError:
            attrs = []
    atomic.write_text(path, text)
    for name, value in attrs:
        try:
            os.setxattr(path, name, value)
        except OSError:
            pass
    birth = getattr(st, "st_birthtime_ns", None)
    if birth is None and getattr(st, "st_birthtime", None) is not None:
        birth = int(st.st_birthtime * 1_000_000_000)
    if birth is not None:
        os.utime(path, ns=(st.st_atime_ns, birth))
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))


class Plan:
    def __init__(self) -> None:
        self.writes: dict[str, tuple[str, str]] = {}     # 相对路径 -> (原文, 新文)
        self.aired: set[str] = set()                     # 留着「播出时间」那行的（剪藏以外）
        self.renames: list[tuple[str, str]] = []         # (旧相对路径, 新相对路径)
        self.links: dict[str, tuple[str, str, int]] = {}  # 链接要改的文件 -> (原文, 新文, 几处)
        self.index = False                               # 剪藏索引要重写
        self.readlog: Optional[tuple[str, str, str, int]] = None
        self.taihe: list[tuple[str, str, str, int]] = []
        self.skipped: list[tuple[str, str]] = []          # (相对路径, 原因)：形状不对，没动
        self.problems: list[str] = []
        self.journal: dict = {"renames": {}, "rewritten": {}}

    def write(self, rel: str, old: str, new: str) -> None:
        if old != new:
            self.writes[rel] = (old, new)

    def skip(self, rel: str, why: str) -> None:
        self.skipped.append((rel, why))

    @property
    def empty(self) -> bool:
        return not (self.writes or self.renames or self.links or self.index or self.readlog or self.taihe)


# ---------------------------------------------------------------- 文档头

def _shape_problem(text: str) -> str:
    if text.startswith("﻿"):
        return "文件开头有 BOM"
    if "\r" in text:
        return "CRLF 换行"
    return ""


def _front_get(front: list[str], key: str) -> str:
    for ln in front[1:-1]:
        if ln.startswith(key + ":"):
            v = ln[len(key) + 1:].strip()
            if v.startswith('"'):
                try:
                    v = json.loads(v)
                except ValueError:
                    pass
            return str(v)
    return ""


def _front_add(front: list[str], key: str, value: str, after: tuple[str, ...] = ()) -> list[str]:
    """文件头加一行（已经有这个键就不动）：放在 after 里最后出现的那个键后面，都没有就放在末尾。"""
    if not front or any(ln.startswith(key + ":") for ln in front[1:-1]):
        return front
    pos = len(front) - 1
    for i, ln in enumerate(front[1:-1], 1):
        if ln.split(":", 1)[0] in after:
            pos = i + 1
    return front[:pos] + [f"{key}: {value}"] + front[pos:]


def _header_lines(body: list[str]) -> dict[str, str]:
    """文档头里每种时间行的第一行原文：{"pub_new": 行, "cap_old": 行, …}。"""
    span = doctime.header_span(body)
    out: dict[str, str] = {}
    for ln in body[span[1]:span[2]] if span else []:
        kind = doctime.time_line_kind(ln)
        if kind and kind not in out:
            out[kind] = ln
    return out


def _stray_time_line(body: list[str]) -> bool:
    """文档头列表外面、第一个「## 」小节之前还有时间行（形状跟想的不一样，别动）。"""
    span = doctime.header_span(body)
    if span is None:
        return False
    for ln in body[span[2]:]:
        if ln.startswith("## "):
            break
        if doctime.time_line_kind(ln):
            return True
    return False


def _value(line: str) -> object:
    return line.split("：", 1)[1].strip() if "：" in line else ""


def _with_instant(day_value: Optional[date], instant: object) -> Optional[str]:
    """文件里写着的那天 + 同一天的时刻（照它自己的时差算是同一天才用）→ 给人看的写法。哪天以写着的为准。"""
    if day_value is None:
        return None
    if isinstance(instant, datetime) and doctime.written_day(instant) == day_value:
        return doctime.cn(instant, keep_zone=True)
    return doctime.cn(day_value)


def _date_of(value: object) -> Optional[date]:
    v = doctime.parse(value)
    return doctime.written_day(v) if v is not None else None


def _rewrite_header(body: list[str], pub: Optional[str], cap: Optional[str],
                    anchors: tuple[str, ...]) -> Optional[list[str]]:
    """文档头列表里的时间行换成 pub、cap 两行（给人看的值），放在原来第一行时间行的位置；
    原来一行时间都没有：插在 anchors 开头的那行后面，没有这行就放在列表末尾。
    文档头已经全是新写法的不动。标题不在正文第一行、文档头外面还有时间行的返回 None（不动，列出来）。"""
    span = doctime.header_span(body)
    if span is None or _stray_time_line(body):
        return None
    _t, start, end = span
    items = body[start:end]
    kinds = [doctime.time_line_kind(ln) for ln in items]
    if any(kinds) and not any(k.endswith("_old") for k in kinds):
        return body
    new_lines = [ln for ln in (pub and f"- {doctime.PUBLISHED_LABEL}：{pub}", cap and f"- {doctime.CAPTURED_LABEL}：{cap}") if ln]
    if any(kinds):
        first = kinds.index(next(k for k in kinds if k))
        kept = [ln for ln, k in zip(items, kinds) if not k]
        pos = sum(1 for k in kinds[:first] if not k)
    else:
        kept = items
        anchor = next((i for i, ln in enumerate(items) if ln.startswith(anchors)), None) if anchors else None
        pos = anchor + 1 if anchor is not None else len(items)
    return body[:start] + kept[:pos] + new_lines + kept[pos:] + body[end:]


# 剪藏以外的文档原来写的「- 播出时间：…」：原样留着，下面加收录时间（不换成发表时间）。只用来认，不往外写
AIRED_PREFIX = "- 播出时间："


def _keep_aired(body: list[str], cap: Optional[str]):
    """文档头里有「- 播出时间：」那行：返回 (新正文或 None, True)。那行一字不动；还没有新写法的收录时间行时，
    在它下面紧接着加一行（文档头里原来的旧收录时间行「- 剪藏：」就地换成新写法）。已经有收录时间的原样不动（再跑不重复加）。
    没有这行返回 (None, False)，照别的规矩改。标题不在正文第一行、文档头外面还有时间行的返回 (None, True)，不动。"""
    span = doctime.header_span(body)
    if span is None:
        return None, False
    _t, start, end = span
    items = body[start:end]
    at = next((i for i, ln in enumerate(items) if ln.startswith(AIRED_PREFIX)), None)
    if at is None:
        return None, False
    if _stray_time_line(body):
        return None, True
    kinds = [doctime.time_line_kind(ln) for ln in items]
    if "cap_new" in kinds or not cap:
        return body, True
    line = f"- {doctime.CAPTURED_LABEL}：{cap}"
    if "cap_old" in kinds:
        items = [line if k == "cap_old" else ln for ln, k in zip(items, kinds)]
    else:
        items = items[:at + 1] + [line] + items[at + 1:]
    return body[:start] + items + body[end:], True


def _relabel(text: str) -> str:
    """文档头里上一版的「- 发表时间：…」换成新标签（doctime.PUBLISHED_LABEL），值不动；别的一字不改。"""
    if doctime.LEGACY_PUBLISHED_LABEL == doctime.PUBLISHED_LABEL:
        return text
    front, body = doctime.split_front(text)
    span = doctime.header_span(body)
    if span is None:
        return text
    old, new = f"- {doctime.LEGACY_PUBLISHED_LABEL}：", f"- {doctime.PUBLISHED_LABEL}："
    _t, start, end = span
    changed = [new + ln[len(old):] if ln.startswith(old) else ln for ln in body[start:end]]
    if changed == body[start:end]:
        return text
    return "\n".join(front + body[:start] + changed + body[end:])


def _keep_new(lines: dict[str, str], kind: str) -> Optional[str]:
    """已经是新写法的那行：原样留着（只取值）。"""
    ln = lines.get(kind)
    return _value(ln) if ln else None


# ---------------------------------------------------------------- 剪藏

def plan_clips(root: str, plan: Plan) -> None:
    folder = _full(root, CLIPS)
    if not os.path.isdir(folder):
        return
    names = sorted(os.listdir(folder))
    taken = set(names)
    pending = plan.journal["renames"]
    for name in names:
        rel = f"{CLIPS}/{name}"
        if not clips.is_clip_path(rel):
            continue
        text = _read(os.path.join(folder, name))
        if text is None:
            plan.skip(rel, "读不出来（不是 UTF-8）")
            continue
        why = _shape_problem(text)
        if why:
            plan.skip(rel, why)
            continue
        front, body = doctime.split_front(text)
        if doctime.header_span(body) is None:
            plan.skip(rel, "正文开头不是「# 标题」")
            continue
        lines = _header_lines(body)
        f_pub = _date_of(_front_get(front, "published"))
        f_cap = _date_of(_front_get(front, "clipped"))
        name_day = _date_of(name[:8]) if re.match(r"\d{8}_", name) else None
        # 发表时间：新写法原样；文件头的 published（同一天的 published_at 连时刻）；文档头的旧行
        pub = _keep_new(lines, "pub_new")
        if pub is None:
            pub = _with_instant(f_pub, doctime.parse(_front_get(front, "published_at")))
        if pub is None and "pub_old" in lines:
            pub = doctime.cn(_date_of(_value(lines["pub_old"])) or None) or None
        cap = _keep_new(lines, "cap_new")
        if cap is None:
            cap = _with_instant(f_cap, doctime.parse(_front_get(front, "clipped_at")))
        if cap is None and "cap_old" in lines:
            cap = doctime.cn(_date_of(_value(lines["cap_old"])) or None) or None
        if cap is None and name_day is not None:
            cap = doctime.cn(name_day)     # 老文件：收录日期只记在文件名里
        new_body = _rewrite_header(body, pub, cap, ("- 来源：",))
        if new_body is None:
            plan.skip(rel, "文档头外面还有时间行")
            continue
        # 改名：用文件里写着的发表那天
        pub_day = f_pub
        if pub_day is None:
            raw = lines.get("pub_new") or lines.get("pub_old")
            pub_day = _date_of(_value(raw)) if raw else None
        target = None
        if pub_day is not None and name_day is not None and name_day != pub_day:
            if not front:
                plan.problems.append(f"{rel}：没有文件头，收录日期只记在文件名里，没改名")
            else:
                planned = pending.get(rel)
                if planned and clips.is_clip_path(planned) and not os.path.exists(_full(root, planned)) \
                        and planned.rsplit("/", 1)[-1] not in taken:
                    cand = planned.rsplit("/", 1)[-1]     # 上次没跑完：照迁移记录里的新名字
                else:
                    stem = pub_day.strftime("%Y%m%d") + name[8:-3]
                    cand, n = stem + ".md", 2
                    while cand in taken:
                        cand = f"{stem}_{n}.md"
                        n += 1
                taken.add(cand)
                target = cand
                if not _front_get(front, "clipped"):
                    cap_day = f_cap or (_date_of(_value(lines["cap_old"])) if "cap_old" in lines else None) or name_day
                    front = _front_add(front, "clipped", cap_day.isoformat(),
                                       ("title", "source", "url", "published", "published_at"))
        plan.write(rel, text, _relabel("\n".join(front + new_body)))
        if target:
            plan.renames.append((rel, f"{CLIPS}/{target}"))


# ---------------------------------------------------------------- 节目、信息跟进、大会

def _show_folders(root: str) -> list[str]:
    out = []
    for parent in (SPARK, TRACK):
        full = _full(root, parent)
        if not os.path.isdir(full):
            continue
        for name in sorted(os.listdir(full)):
            rel = f"{parent}/{name}"
            if name.startswith(".") or rel in (CLIPS, TRACK) or not os.path.isdir(_full(root, rel)):
                continue
            if os.path.isfile(os.path.join(_full(root, rel), ".manifest.json")):
                out.append(rel)
    return out


def _pub_for(lines: dict[str, str], written: Optional[date], manifest_pub, name_pub: Optional[date]) -> Optional[str]:
    """节目、跟进、大会文件的发表时间（给人看的值）：
    新写法原样 > 文件里写着的旧行（处理记录的时刻跟它同一天才连时刻）> 文件头写着的（单集笔记的「播出」）
    > 处理记录 > 文件名开头的日期。文件里已经写着的日期不丢。"""
    if "pub_new" in lines:
        return _value(lines["pub_new"])
    old = _date_of(_value(lines["pub_old"])) if "pub_old" in lines else None
    day_value = old or written
    if day_value is not None:
        return _with_instant(day_value, manifest_pub)
    if isinstance(manifest_pub, datetime):
        return doctime.cn(manifest_pub, keep_zone=True)
    if manifest_pub is not None:
        return doctime.cn(manifest_pub)
    return doctime.cn(name_pub) if name_pub else None


def _rewrite_note(text: str, pub: Optional[str], cap: Optional[str], published, captured) -> Optional[str]:
    front, body = doctime.split_front(text)
    span = doctime.header_span(body)
    if span is None or _stray_time_line(body):
        return None
    t, start, end = span
    if front:
        if isinstance(published, datetime):
            front = _front_add(front, "published_at", json.dumps(published.isoformat()), ("播出",))
        if isinstance(captured, datetime):
            front = _front_add(front, "captured_at", json.dumps(captured.isoformat()), ("published_at", "播出", "节目"))
    if start == end:
        # 笔记标题下面原来没有列表：插在标题和一句话之间，前后各空一行
        new_lines = [ln for ln in (pub and f"- {doctime.PUBLISHED_LABEL}：{pub}", cap and f"- {doctime.CAPTURED_LABEL}：{cap}") if ln]
        if new_lines:
            body = body[:t + 1] + [""] + new_lines + body[t + 1:] if body[t + 1:t + 2] == [""] \
                else body[:t + 1] + [""] + new_lines + [""] + body[t + 1:]
    else:
        body = _rewrite_header(body, pub, cap, ())
        if body is None:
            return None
    return "\n".join(front + body)


def plan_shows(root: str, plan: Plan) -> None:
    for folder in _show_folders(root):
        mpath = os.path.join(_full(root, folder), ".manifest.json")
        try:
            with open(mpath, encoding="utf-8") as f:
                manifest = json.load(f)
            rows = manifest.get("entries") or {}
            assert isinstance(rows, dict)
        except (OSError, ValueError, AssertionError, AttributeError):
            plan.problems.append(f"{folder}/.manifest.json：读不出来，这个文件夹没改")
            continue
        track = folder.startswith(TRACK + "/")
        for row in rows.values():
            if not isinstance(row, dict) or not row.get("ok"):
                continue
            entry = row.get("entry") if isinstance(row.get("entry"), dict) else None
            if entry is None:
                continue
            # 收录时间：处理记录里新写法记的时刻；老记录只有 updated_at（最后一次处理），只取那天
            captured = doctime.parse(entry.get("captured_at"))
            if isinstance(captured, datetime):
                cap = doctime.cn(captured, keep_zone=True)
            else:
                captured = None
                upd = _date_of(str(row.get("updated_at") or "")[:10])
                cap = doctime.cn(upd) if upd else None
            manifest_pub = None if entry.get("publish_guessed") else doctime.entry_published(entry)
            m = re.match(r"(\d{8})[_ ]", os.path.basename(str(row.get("relative_path") or row.get("note_relative_path") or "")))
            name_pub = None if entry.get("publish_guessed") else (_date_of(m.group(1)) if m else None)
            for key in ("relative_path", "speech_relative_path", "speech_en_relative_path", "note_relative_path"):
                rp = row.get(key)
                if not isinstance(rp, str) or not rp or ".." in rp.split("/"):
                    continue
                rel = f"{folder}/{rp}"
                full = _full(root, rel)
                if rel in plan.writes or not os.path.isfile(full):
                    continue
                text = _read(full)
                if text is None:
                    plan.skip(rel, "读不出来（不是 UTF-8）")
                    continue
                why = _shape_problem(text)
                if why:
                    plan.skip(rel, why)
                    continue
                front, body = doctime.split_front(text)
                lines = _header_lines(body)
                written = _date_of(_front_get(front, "播出")) if key == "note_relative_path" else None
                pub = _pub_for(lines, written, manifest_pub, name_pub)
                c = _keep_new(lines, "cap_new") or \
                    (doctime.cn(_date_of(_value(lines["cap_old"]))) if "cap_old" in lines and _date_of(_value(lines["cap_old"])) else None) \
                    or cap
                if c is None:
                    plan.problems.append(f"{rel}：处理记录没有处理时间，没写收录时间")
                kept, aired = _keep_aired(body, c)
                if aired:
                    plan.aired.add(rel)
                    if kept is None:
                        plan.skip(rel, "正文开头不是「# 标题」或文档头外面还有时间行")
                        continue
                    if key == "note_relative_path" and front and isinstance(captured, datetime):
                        front = _front_add(front, "captured_at", json.dumps(captured.isoformat()), ("published_at", "播出", "节目"))
                    new = "\n".join(front + kept)
                elif key == "note_relative_path":
                    published = manifest_pub if isinstance(manifest_pub, datetime) and pub == doctime.cn(manifest_pub, keep_zone=True) else None
                    new = _rewrite_note(text, pub, c, published, captured)
                else:
                    anchor = ("- 来源：",) if track and key == "relative_path" else ("- 时长：",)
                    nb = _rewrite_header(body, pub, c, anchor)
                    new = None if nb is None else "\n".join(front + nb)
                if new is None:
                    plan.skip(rel, "正文开头不是「# 标题」或文档头外面还有时间行")
                    continue
                plan.write(rel, text, _relabel(new))


# ---------------------------------------------------------------- 改名之后：链接、索引、阅读记录、太和同步记录

_WIKI_RE = re.compile(r"(!?)\[\[([^\]|#\n]+)((?:[#|][^\]\n]*)?)\]\]")
_MDLINK_RE = re.compile(r"\]\((<)?([^)>\n]+)(>?)\)")
_FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
_INLINE_CODE_RE = re.compile(r"(`+)(?:.+?)\1")


def _outside_code(text: str, fn) -> str:
    """只对代码块（``` / ~~~ 围起来的）和行内代码（`…`）以外的部分做 fn。"""
    out, fence = [], None
    for line in text.split("\n"):
        m = _FENCE_RE.match(line)
        if fence:
            out.append(line)
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence):
                fence = None
            continue
        if m:
            fence = m.group(1)
            out.append(line)
            continue
        parts, pos = [], 0
        for cm in _INLINE_CODE_RE.finditer(line):
            parts.append(fn(line[pos:cm.start()]))
            parts.append(cm.group(0))
            pos = cm.end()
        parts.append(fn(line[pos:]))
        out.append("".join(parts))
    return "\n".join(out)


def _relink(text: str, rel: str, mapping: dict[str, str], elsewhere: set[str]) -> tuple[str, int]:
    """指向改了名的剪藏的链接换成新名字，别的不碰：
    - [[名字]]（别处没有同名笔记时）、[[Spark/剪藏/名字|…]]、![[名字]]；[[attachments/…]] 不碰；
    - [标题](<路径.md>)、[标题](路径%20.md)：按这篇所在的文件夹解开相对路径，正好是改了名的那篇才换。
    代码块、行内代码里的不碰。"""
    by_stem = {o.rsplit("/", 1)[-1][:-3]: (o, n) for o, n in mapping.items()}
    count = 0

    def wiki(m):
        nonlocal count
        target = m.group(2)
        if "attachments/" in target:
            return m.group(0)
        bare = target[:-3] if target.endswith(".md") else target
        stem = bare.rsplit("/", 1)[-1]
        hit = by_stem.get(stem)
        if hit is None:
            return m.group(0)
        if "/" in bare:
            if not (bare == hit[0][:-3] or bare.endswith("剪藏/" + stem)):
                return m.group(0)
        elif stem in elsewhere:
            return m.group(0)     # 别处有同名的笔记：说不准指的是哪篇
        count += 1
        new_stem = hit[1].rsplit("/", 1)[-1][:-3]
        new_target = target[:len(target) - len(target.rsplit("/", 1)[-1])] + new_stem + (".md" if target.endswith(".md") else "")
        return m.group(1) + "[[" + new_target + m.group(3) + "]]"

    def md(m):
        nonlocal count
        target = m.group(2)
        if re.match(r"[A-Za-z][A-Za-z0-9+.-]*:", target):
            return m.group(0)     # http:、obsidian: 这类
        path, sep, frag = target.partition("#")
        decoded = urllib.parse.unquote(path)
        if not decoded.endswith(".md"):
            return m.group(0)
        resolved = posixpath.normpath(decoded.lstrip("/") if decoded.startswith("/")
                                      else posixpath.join(posixpath.dirname(rel), decoded))
        if resolved not in mapping:
            return m.group(0)
        count += 1
        raw_base = path.rsplit("/", 1)[-1]
        new_base = mapping[resolved].rsplit("/", 1)[-1]
        if urllib.parse.unquote(raw_base) != raw_base:
            new_base = urllib.parse.quote(new_base)
        return "](" + (m.group(1) or "") + path[:len(path) - len(raw_base)] + new_base + sep + frag + m.group(3) + ")"

    def both(segment: str) -> str:
        return _MDLINK_RE.sub(md, _WIKI_RE.sub(wiki, segment))

    return _outside_code(text, both), count


def effective_renames(root: str, plan: Plan) -> dict[str, str]:
    """要跟着改的：迁移记录（含这次计划的）里，旧名字已经没有了（或这次要改走）、最后的新名字在（或这次要改成）的。旧→最后的名字。"""
    table = dict(plan.journal["renames"])
    table.update(dict(plan.renames))
    moving = {o for o, _n in plan.renames}
    arriving = {n for _o, n in plan.renames}
    out = {}
    for old in table:
        final = clips.resolve(table, old)
        if final == old:
            continue
        gone = old in moving or not os.path.exists(_full(root, old))
        there = final in arriving or os.path.exists(_full(root, final))
        if gone and there:
            out[old] = final
    return out


def plan_links(root: str, plan: Plan, mapping: dict[str, str]) -> None:
    plan.links = {}
    if not mapping:
        return
    stems = {o.rsplit("/", 1)[-1][:-3] for o in mapping}
    elsewhere: set[str] = set()
    files = []
    for folder, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in names:
            if not name.endswith(".md") or name.startswith("."):
                continue
            rel = os.path.relpath(os.path.join(folder, name), root).replace(os.sep, "/")
            if not rel.startswith(CLIPS + "/") and name[:-3] in stems:
                elsewhere.add(name[:-3])
            files.append(rel)
    for rel in files:
        if rel == INDEX:
            continue   # 索引整篇重写
        base = plan.writes[rel][1] if rel in plan.writes else _read(_full(root, rel))
        if base is None:
            continue
        if not any(s in base or urllib.parse.quote(s) in base for s in stems):
            continue
        new, n = _relink(base, rel, mapping, elsewhere)
        if n:
            plan.links[rel] = (base, new, n)


def plan_index(root: str, plan: Plan, mapping: dict[str, str]) -> None:
    """有改了名的剪藏：索引要按现在的名字重写（跟现在的不一样才写）。"""
    plan.index = False
    if not mapping:
        return
    if plan.renames:
        plan.index = True
        return
    current = _read(_full(root, INDEX))
    plan.index = current != clips.render_index(clips.load_clips(root))


def plan_readlog(path: str, plan: Plan, mapping: dict[str, str]) -> None:
    plan.readlog = None
    if not mapping:
        return
    text = _read(path)
    if text is None:
        plan.problems.append(f"阅读记录 {path} 读不出来，里面的路径没改")
        return
    out, n = [], 0
    for line in text.split("\n"):
        try:
            row = json.loads(line) if line.strip() else None
        except ValueError:
            row = None
        if isinstance(row, dict) and row.get("path") in mapping:
            row["path"] = mapping[row["path"]]
            line = json.dumps(row, ensure_ascii=False)
            n += 1
        out.append(line)
    if n:
        plan.readlog = (path, text, "\n".join(out), n)


def plan_taihe(home: str, root: str, plan: Plan, mapping: dict[str, str]) -> None:
    plan.taihe = []
    if not mapping:
        return
    from core import taihe_sync
    key = taihe_sync.sha(str(os.path.realpath(root)))[:16]
    base = os.path.join(home, "taihe-sync")
    for device in sorted(os.listdir(base)) if os.path.isdir(base) else []:
        path = os.path.join(base, device, key, "state.json")
        if not os.path.isfile(path):
            continue
        text = _read(path) or ""
        try:
            state = json.loads(text)
        except ValueError:
            plan.problems.append(f"太和同步记录 {path} 读不出来，里面的路径没改")
            continue
        n = 0
        for e in (state.get("entries") or {}).values():
            if isinstance(e, dict) and e.get("path") in mapping:
                e["path"] = mapping[e["path"]]
                n += 1
        if n:
            plan.taihe.append((path, text, json.dumps(state, ensure_ascii=False), n))


def plan_after(root: str, plan: Plan, readlog: str = "", taihe_home: str = "") -> None:
    """链接、索引、阅读记录、太和同步记录：都按迁移记录（加这次的计划）来。"""
    mapping = effective_renames(root, plan)
    plan_links(root, plan, mapping)
    plan_index(root, plan, mapping)
    if readlog:
        plan_readlog(readlog, plan, mapping)
    if taihe_home:
        plan_taihe(taihe_home, root, plan, mapping)


# ---------------------------------------------------------------- 计划、执行

def build(root: str, readlog: str = "", taihe_home: str = "") -> Plan:
    plan = Plan()
    plan.journal = clips.load_journal(root)
    plan_clips(root, plan)
    plan_shows(root, plan)
    plan_after(root, plan, readlog, taihe_home)
    return plan


def _diff(old: str, new: str) -> list[str]:
    import difflib
    return [ln for ln in difflib.unified_diff(old.split("\n"), new.split("\n"), lineterm="", n=0)
            if ln and ln[0] in "+-" and not ln.startswith(("+++", "---"))]


def report(plan: Plan, out=None) -> None:
    out = out or sys.stdout
    renamed = dict(plan.renames)
    for rel, (old, new) in sorted(plan.writes.items()):
        dest = renamed.get(rel)
        print(f"改写 {rel}" + (f" → {dest}" if dest else ""), file=out)
        for ln in _diff(old, new):
            print(f"    {ln}", file=out)
    for old, new in plan.renames:
        if old not in plan.writes:
            print(f"改名 {old} → {new}", file=out)
    if plan.renames:
        print(f"改名、改写前先记进 {CLIPS}/{clips.RENAMES_NAME}", file=out)
    if plan.index:
        print(f"重写 {INDEX}", file=out)
    for rel, (_o, _n, n) in sorted(plan.links.items()):
        print(f"改链接 {rel}：{n} 处", file=out)
    if plan.readlog:
        print(f"阅读记录 {plan.readlog[0]}：{plan.readlog[3]} 条路径", file=out)
    for path, _o, _n, n in plan.taihe:
        print(f"太和同步记录 {path}：{n} 条路径", file=out)
    reasons: dict[str, int] = {}
    for _rel, why in plan.skipped:
        reasons[why] = reasons.get(why, 0) + 1
    print(f"共：改写 {len(plan.writes)} 篇，改名 {len(plan.renames)} 篇，改链接 {len(plan.links)} 篇，"
          f"没动 {len(plan.skipped)} 篇" + "".join(f"（{w} {n}）" for w, n in sorted(reasons.items())), file=out)
    added = sum(1 for rel in plan.aired if rel in plan.writes)
    print(f"保留「播出时间」{len(plan.aired)} 篇（其中这次加了{doctime.CAPTURED_LABEL} {added} 篇）", file=out)
    for rel, why in plan.skipped:
        print(f"没动：{rel}：{why}", file=out)
    for p in plan.problems:
        print(f"没改：{p}", file=out)


def _record_journal(root: str, plan: Plan) -> None:
    """改之前先把整个计划记进迁移记录（跟已有的合在一起，加锁读改写，见 clips.update_journal）：
    中途停下，下次照它接着改完；手机迁移前那一版的修改照它认。迁移记录坏了就不改（clips.JournalError）。"""
    final = dict(plan.renames)

    def change(journal):
        renames, rewritten = journal["renames"], journal["rewritten"]
        for old, new in plan.renames:
            renames[old] = new
            if old in rewritten and new not in rewritten:
                rewritten[new] = rewritten.pop(old)
        for rel, (old, new) in plan.writes.items():
            merge_rewritten(rewritten, final.get(rel, rel), old, new)

    plan.journal = clips.update_journal(root, change)


def merge_rewritten(rewritten: dict, key: str, old: str, new: str) -> None:
    """迁移记录里记下一次整篇改写（tools/backfill_summit_dates.py 回填时也用这一个）：
    上一次改写（迁移或回填）的结果又被改写：手机上可能是最早那版，也可能是中间那版，都认，两步接得起来；
    中间被别的改过（不是上一次改写的结果）：只认这次改之前的那版。"""
    e = rewritten.get(key) or {}
    h = _sha(old)
    have = [b for b in (e.get("before") or []) if isinstance(b, str)]
    if e.get("after") == _sha(new) and h in have:
        return   # 中断后重跑、同一步又记一次：原来的 before 不动（不然前面几版就认不出了）
    before = have if e.get("after") == h else []
    if h not in before:
        before.append(h)
    rewritten[key] = {"before": before, "after": _sha(new)}


def apply(root: str, plan: Plan, readlog: str = "", taihe_home: str = "", cable_cache: str = "") -> None:
    """先记迁移记录，再在原位置改内容，再改名；最后按迁移记录改链接、索引、阅读记录、太和同步记录。"""
    touched: set[str] = set()
    if plan.writes or plan.renames:
        _record_journal(root, plan)
    changed: set[str] = set()
    for rel, (old, new) in plan.writes.items():
        full = _full(root, rel)
        if _read(full) != old:
            plan.problems.append(f"{rel}：列计划之后被改过，这篇没改，也没改名")
            changed.add(rel)
            continue
        _write_keep_times(full, new)
        touched.add(rel)
    dropped = []
    for old, new in plan.renames:
        if old in changed:
            dropped.append(old)
            continue
        if os.path.exists(_full(root, new)):
            plan.problems.append(f"{old}：{new} 已经有了，没改名")
            dropped.append(old)
            continue
        _rename(_full(root, old), _full(root, new))
        touched.update((old, new))
    if dropped:
        # 没改的名字从迁移记录里拿掉：不然手机会把发件箱里的修改换到一个电脑上没有的名字下
        clips.update_journal(root, lambda journal: [journal["renames"].pop(old, None) for old in dropped])
    # 改了名的剪藏：收下的手机修改记录（.edits.json）跟着挪到新名字下
    clips.move_edits(root, [(o, n) for o, n in plan.renames if o not in dropped and o in touched])
    # 改名之后按现在的文件重新算一遍（按迁移记录，不按这次改了哪几个）
    after = Plan()
    after.journal = clips.load_journal(root)
    plan_after(root, after, readlog, taihe_home)
    for rel, (base, new, _n) in after.links.items():
        full = _full(root, rel)
        if _read(full) != base:
            plan.problems.append(f"{rel}：列计划之后被改过，链接没改")
            continue
        _write_keep_times(full, new)
        touched.add(rel)
    plan.links = after.links
    if after.index:
        clips.rebuild_index(root)
        touched.add(INDEX)
    plan.index = after.index
    if after.readlog:
        path, old, new, _n = after.readlog
        if _read(path) == old:
            atomic.write_text(path, new)
        else:
            plan.problems.append(f"阅读记录 {path} 列计划之后又有新记录，路径没改，再跑一遍")
    plan.readlog = after.readlog
    for path, old, new, _n in after.taihe:
        if _read(path) == old:
            atomic.write_text(path, new)
        else:
            plan.problems.append(f"太和同步记录 {path} 列计划之后变了，路径没改，再跑一遍")
    plan.taihe = after.taihe
    plan.problems += after.problems
    if cable_cache and touched:
        _forget_cable_shas(cable_cache, touched | {f"{CLIPS}/{clips.RENAMES_NAME}", f"{CLIPS}/{clips.EDITS_NAME}"})


def _forget_cable_shas(path: str, rels: set[str]) -> None:
    """数据线同步的指纹缓存（core/cable_sync.stage）里去掉改过的文件：修改时间照旧、大小碰巧一样时也会重算指纹。"""
    try:
        with open(path, encoding="utf-8") as f:
            cache = json.load(f)
    except (OSError, ValueError):
        return
    if not isinstance(cache, dict):
        return
    gone = [k for k in rels if k in cache]
    for k in gone:
        del cache[k]
    if gone:
        atomic.write_json(path, cache)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=f"文档换成新的时间写法（{doctime.PUBLISHED_LABEL}、{doctime.CAPTURED_LABEL}），剪藏按发表那天改名")
    ap.add_argument("--root", required=True, help="笔记库的根目录（下面有 Spark/）")
    ap.add_argument("--readlog", default="", help="阅读记录 reading-log.jsonl 的路径（改名后路径跟着改；不给就不改）")
    ap.add_argument("--taihe-home", default="", help="太和剪藏同步记录所在的目录（下面有 taihe-sync/；不给就不改）")
    ap.add_argument("--cable-cache", default="", help="数据线同步的指纹缓存 cable-sha-cache.json（改过的文件从里面去掉；不给就不动）")
    ap.add_argument("--allow-running-server", action="store_true", help=f"{SERVER_PORT} 端口有人在听也照改")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", default=True, help="只列计划（默认）")
    mode.add_argument("--apply", action="store_true", help="照计划改")
    args = ap.parse_args(argv)
    root = os.path.abspath(os.path.expanduser(args.root))
    if not os.path.isdir(os.path.join(root, SPARK)):
        print(f"{root} 下面没有 {SPARK}/，不是笔记库", file=sys.stderr)
        return 2
    print(BEFORE_NOTE)
    print(AFTER_NOTE)
    print(N2I_NOTE)
    if args.apply and not args.allow_running_server and server_running():
        print(f"{SERVER_PORT} 端口有人在听（Spark 服务开着），没改。先停掉服务；确实要改加 --allow-running-server。", file=sys.stderr)
        return 3
    if args.apply:
        lock = _take_lock(root)
        if lock is None:
            path = os.path.join(_full(root, CLIPS), LOCK_NAME)
            print(f"另一个 --apply 正在改这个笔记库，没改。锁文件：{path}。它最后一次改动 10 分钟后自动算过期；"
                  f"确认没有另一个迁移在跑（比如上次被强行中断），可以删掉这个文件再跑。", file=sys.stderr)
            return 4
        try:
            n = clean_tmp(root)
            if n:
                print(f"清掉了上次中断留下的临时文件 {n} 个（.md.<随机串>.tmp）")
            return _run(args, root)
        finally:
            try:
                os.remove(lock)
            except OSError:
                pass
    return _run(args, root)


_TMP_RE = re.compile(r"^.+\.md\.[0-9a-f]{32}\.tmp$")   # core/atomic.write_text 的临时文件：<文件名>.<uuid>.tmp


def clean_tmp(root: str) -> int:
    """清掉上次中断留下的原子写临时文件：只在脚本会改写的 Spark/、output/ 下，只认 <名字>.md.<32 位十六进制>.tmp，
    只清 10 分钟没动过的（正在写的不碰）。拿着锁时才调。返回清了几个。"""
    n = 0
    now = time.time()
    for top in (SPARK, "output"):
        for d, dirs, files in os.walk(_full(root, top)):
            dirs[:] = [x for x in dirs if not x.startswith(".")]
            for f in files:
                if not _TMP_RE.match(f):
                    continue
                p = os.path.join(d, f)
                try:
                    if now - os.stat(p).st_mtime >= LOCK_STALE_SECONDS:
                        os.remove(p)
                        n += 1
                except OSError:
                    pass
    return n


def _take_lock(root: str) -> Optional[str]:
    """同一个笔记库同时只跑一个 --apply：剪藏文件夹里放一个锁文件（新建不成就是有人在用；超过 10 分钟没动算没人用了）。"""
    folder = _full(root, CLIPS)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, LOCK_NAME)
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            try:
                if time.time() - os.stat(path).st_mtime < LOCK_STALE_SECONDS:
                    return None
                os.remove(path)
            except FileNotFoundError:
                pass
            continue
        with os.fdopen(fd, "w") as f:
            f.write(f"{os.getpid()} {time.strftime('%Y-%m-%dT%H:%M:%S')}\n")
        return path
    return None


def _run(args, root: str) -> int:
    readlog, taihe_home = os.path.expanduser(args.readlog), os.path.expanduser(args.taihe_home)
    plan = build(root, readlog, taihe_home)
    report(plan)
    if (plan.renames or plan.journal["renames"]) and not args.taihe_home:
        print("提醒：没给 --taihe-home，太和剪藏同步记录里的路径不会跟着改（太和按原文链接还能认回来，没有链接的手写剪藏认不回来）")
    if (plan.renames or plan.journal["renames"]) and not args.readlog:
        print("提醒：没给 --readlog，阅读记录里的旧路径不会跟着改（改了名的剪藏在复盘里、手机上会显示成没读过）")
    if args.apply and not plan.empty:
        before = len(plan.problems)
        apply(root, plan, readlog, taihe_home, os.path.expanduser(args.cable_cache))
        print("已照计划改完。")
        for p in plan.problems[before:]:
            print(f"没改：{p}")
    elif not args.apply:
        print("这是计划，没动文件；照着改加 --apply。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
