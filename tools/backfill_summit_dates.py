"""
给「会议」补日期：各场视频的发布时间（entry.publish_date / publish_at）、议程上的日期（entry.session_date），
和议程给出的会议时间（处理记录顶层 event_dates，见 core/summit_dates.py）。

    python3 tools/backfill_summit_dates.py --root <笔记库> --no-network         # 只列计划，不联网，什么都不写（加 --apply 照计划改处理记录和文件，只是不联网）
    python3 tools/backfill_summit_dates.py --root <笔记库>                      # 只列计划，联网查视频日期（写联网结果缓存）
    python3 tools/backfill_summit_dates.py --root <笔记库> --apply              # 照计划改（Spark 服务开着时不改）
    可选：--only "<会议文件夹名>"（可以给几次）  --limit N（这次最多联网查几个视频）
          --sleep 秒（两次联网之间多等一会儿，默认 0.5；全局还有 YouTube 排队，每次至少隔 5 秒）
          --cache-dir 目录（联网结果缓存放这里，不放进笔记库）

每一场的日期按这个顺序找，找到就停（手机上猜的发表日期 publish_guessed 当没有，能补真的就补，补了去掉猜的标记）：
  (a) 这一场自己的文件（文字记录、整理稿）文档头里已经写着的发表时间（新旧写法都认：- 发布时间：/ - 发表时间：/ - 播出时间：…）；
  (b) 当初下载字幕时存下的 .cache/subtitles/<视频 id>.meta.json 里的上传日期；
  (c) 联网问 YouTube（yt-dlp 只取信息，不下载）：首播/上传的时刻，没有就上传那天。
      结果记在会议文件夹的 .cache/publish_dates.json（或 --cache-dir/<会议名>.json），再跑不重新查；
      一个会议连着 5 次查不到就先不查这个会议。报告最后列出写了哪些缓存文件。
议程给了日期的（agenda_meta.day 是 2026-10-13 这样的）另外记成 session_date。

会议时间（event_dates）：手动填的（manual）永远不动；议程日期够多（至少一半的场、至少 2 场）就存 source=agenda；
老版本存的 source=sessions 删掉；专题文件夹的处理记录不存会议时间。视频发布日期不是会议时间，不存，
报告里写成「视频发布：…」，会议时间要手动填。视频发布日期太少、跨度超过 10 天的，报告里也列成要手动填。
笔记库里能找到的线索（会议总结里提到的日期、官网链接）只在报告里当建议列出来，从不写进去。

--apply 只改这些：各份处理记录（会议目录和下面每个专题文件夹分别比）里条目缺的 publish_date / publish_at /
session_date（原来没有的才补，有的不动），会议目录那份的 event_dates（不动手动填的）。每次都重新比，
上次中途停下只改了一部分的，再跑会补齐。文档头没有发表时间的文字记录、整理稿补一行「- 发布时间：…」
（已经有的不动），修改时间、创建时间照旧。处理记录拿着跟流水线同一把锁改，原子写；格式跟流水线写的不一样的
不改，列出来。CRLF、BOM、正文开头不是「# 标题」的文件不改，列出来。再跑一遍什么都不改。
"""

from __future__ import annotations

import argparse
import copy
import glob
import json
import os
import re
import sys
import time
from datetime import date, datetime
from typing import Callable, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import atomic, clips, doctime, summit_dates  # noqa: E402
from tools.migrate_times import SERVER_PORT, _shape_problem, _write_keep_times, merge_rewritten, server_running  # noqa: E402

SPARK = "Spark"
SKIP_DIRS = {"信息跟进", "剪藏"}
FILE_KEYS = ("relative_path", "speech_relative_path", "speech_en_relative_path")
CACHE_NAME = "publish_dates.json"
MAX_CONSECUTIVE_FAILURES = 5

Fetch = Callable[[str], dict]


def _pipeline():
    from apps.summit2md import pipeline
    return pipeline


def youtube_fetch(url: str) -> dict:
    """问 YouTube 这个视频的发布时间（不下载）。返回 {upload_date, timestamp, release_timestamp} 里有的；查不到抛异常。"""
    p = _pipeline()
    opts = {"skip_download": True, "quiet": True, "no_warnings": True, "noplaylist": True}
    with p._youtube_dl(opts) as ydl:
        info = ydl.extract_info(url, download=False) or {}
    out = {k: info.get(k) for k in ("upload_date", "timestamp", "release_timestamp") if info.get(k)}
    if not out:
        raise ValueError("YouTube 没给发布日期")
    return out


def info_when(info: dict) -> Optional[object]:
    """yt-dlp 的信息 → 发布时间：首播/上传的时刻优先（本机时区），其次 upload_date 那天。"""
    ts = _pipeline().video_timestamp(info or {})
    if ts:
        at = doctime.from_epoch(ts)
        if at is not None:
            return at
    d = doctime.parse((info or {}).get("upload_date"))
    return d if isinstance(d, date) else None


def _dump(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def _tail(raw: Optional[str], data: dict) -> Optional[str]:
    """处理记录是不是流水线那样写的（json.dumps 缩进 2，结尾最多一个换行）：是的话返回结尾（"" 或 "\n"），
    写回时照样带上，别的字节不变；不是返回 None（写回会重排，不改）。"""
    if raw is None:
        return None
    body = _dump(data)
    if raw == body:
        return ""
    return "\n" if raw == body + "\n" else None


def _read(path: str) -> Optional[str]:
    try:
        with open(path, "rb") as f:
            return f.read().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


class Summit:
    def __init__(self, folder: str, rel: str) -> None:
        self.folder, self.rel = folder, rel
        self.root = ""                 # 笔记库根目录（记迁移记录用）
        self.name = os.path.basename(folder)
        self.manifest: dict = {}
        self.problem = ""              # 会议目录的处理记录读不出来，整个会议不看
        self.total = self.dated = 0
        self.from_files = self.from_meta = self.from_cache = self.from_network = 0
        self.need_network: list[str] = []
        self.failed: list[tuple[str, str]] = []
        self.unresolved: list[str] = []
        self.updates: dict[str, dict] = {}            # 会议目录处理记录里条目 id -> 要补的字段（报告用）
        self.truth: dict[str, dict] = {}              # 条目 id -> 补完以后的条目（各份处理记录都照它补）
        self.manifest_changes: dict[str, int] = {}    # 处理记录所在文件夹 -> 要改几处（条目数，会议时间也算一处）
        self.skipped_manifests: list[tuple[str, str]] = []   # (相对路径, 原因)：格式跟流水线写的不一样，不改
        self.file_adds: dict[str, tuple[str, str]] = {}   # 文件绝对路径 -> (原文, 新文)
        self.skipped: list[tuple[str, str]] = []      # (文件相对路径, 原因)
        self.event_before: Optional[dict] = None
        self.event_after: Optional[dict] = None
        self.event_change = False
        self.video_coverage: tuple[int, int] = (0, 0)
        self.manual_hint: tuple[str, str] = ("", "")
        self.suggestions: list[str] = []
        self.children: list[str] = []                 # 同一大会下各专题文件夹（有自己的处理记录）
        self.cache: dict = {}
        self.cache_path = ""
        self.cache_written = False
        self.network_stopped = False

    @property
    def changes(self) -> bool:
        return bool(self.manifest_changes or self.file_adds)


# ---------------------------------------------------------------- 找日期

def summit_folders(root: str, only: Optional[list[str]] = None) -> list[str]:
    spark = os.path.join(root, SPARK)
    out = []
    for name in sorted(os.listdir(spark)):
        full = os.path.join(spark, name)
        if name.startswith(".") or name in SKIP_DIRS or not os.path.isdir(full):
            continue
        if only and name not in only:
            continue
        mf = os.path.join(full, ".manifest.json")
        if not os.path.isfile(mf):
            continue
        try:
            with open(mf, encoding="utf-8") as f:
                kind = (json.load(f) or {}).get("content_type")
        except (OSError, ValueError, AttributeError):
            kind = None
        # 没写 content_type 的老会议也算会议（手机、阅读页都这么认）
        if kind != "series":
            out.append(full)
    return out


def _file_published(text: str) -> Optional[object]:
    line = doctime.header_time_lines(text).get("pub")
    if not line:
        return None
    m = doctime.PUBLISHED_LINE.match(line)
    return doctime.parse(m.group(1)) if m else None


def _meta_info(s: Summit, vid: str) -> Optional[dict]:
    paths = [os.path.join(s.folder, ".cache", "subtitles", f"{vid}.meta.json")]
    paths += [os.path.join(c, ".cache", "subtitles", f"{vid}.meta.json") for c in s.children]
    for p in paths:
        try:
            with open(p, encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(meta, dict) and (meta.get("upload_date") or meta.get("timestamp") or meta.get("release_timestamp")):
            return meta
    return None


def _stamp(fields: dict, when: object) -> None:
    """发布时间记成条目字段：哪天以它自己写着的为准；知道时刻再记 publish_at（照它自己的时差）。"""
    if isinstance(when, datetime):
        fields["publish_date"] = when.date().strftime("%Y%m%d")
        fields["publish_at"] = when.replace(microsecond=0).isoformat()
    else:
        fields["publish_date"] = when.strftime("%Y%m%d")


def _load_cache(s: Summit) -> dict:
    try:
        with open(s.cache_path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_cache(s: Summit) -> None:
    os.makedirs(os.path.dirname(s.cache_path), exist_ok=True)
    atomic.write_json(s.cache_path, s.cache, indent=2)
    s.cache_written = True


def _header_insert(text: str, line: str) -> tuple[Optional[str], str]:
    """在文档头那串「- 键：值」里补一行发表时间：「时长」后面，没有就「收录时间」「字幕来源」前面，再没有放最后。
    返回 (新文, 不改的原因)。"""
    why = _shape_problem(text)
    if why:
        return None, why
    if doctime.PUBLISHED_LINE.search(text):
        return None, ""
    front, body = doctime.split_front(text)
    span = doctime.header_span(body)
    if span is None:
        return None, "正文开头不是「# 标题」"
    _t, start, end = span
    if start == end:
        return None, "标题下面没有「- 键：值」"
    items = body[start:end]
    at = next((i + 1 for i, ln in enumerate(items) if ln.startswith("- 时长：")), None)
    if at is None:
        at = next((i for i, ln in enumerate(items) if ln.startswith(("- 收录时间：", "- 剪藏：", "- 字幕来源："))), len(items))
    items = items[:at] + [line] + items[at:]
    return "\n".join(front + body[:start] + items + body[end:]), ""


_CN_DATE = re.compile(r"(20\d{2})年(\d{1,2})月(\d{1,2})日(?:\s*[—–\-至到~]\s*(?:(\d{1,2})月)?(\d{1,2})日)?")
_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_EN_DATE = re.compile(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s+(\d{1,2})(?:\s*[-–—]\s*(\d{1,2}))?,?\s+(20\d{2})\b")


def suggestions(s: Summit) -> list[str]:
    """笔记库里现成的线索（只在报告里给人看，从不写进处理记录）：会议总结里提到的日期、官网链接。"""
    texts = [str(s.manifest.get("overall_summary") or "")]
    home = _read(os.path.join(s.folder, s.name + ".md"))
    if home:
        texts.append(home[:20000])
    found: list[str] = []
    for text in texts:
        for m in _CN_DATE.finditer(text):
            y, mo, d = int(m[1]), int(m[2]), int(m[3])
            start = f"{y:04d}-{mo:02d}-{d:02d}"
            end = f"{y:04d}-{int(m[4] or mo):02d}-{int(m[5]):02d}" if m[5] else start
            lab = summit_dates.label(start, end)
            if lab and lab not in found:
                found.append(lab)
        for m in _EN_DATE.finditer(text):
            mo = _MONTHS[m[1][:3].lower()]
            start = f"{int(m[4]):04d}-{mo:02d}-{int(m[2]):02d}"
            end = f"{int(m[4]):04d}-{mo:02d}-{int(m[3]):02d}" if m[3] else start
            lab = summit_dates.label(start, end)
            if lab and lab not in found:
                found.append(lab)
    out = [f"会议总结里提到：{'、'.join(found[:4])}"] if found else []
    rec = os.path.join(s.folder, ".recordings.json")
    try:
        with open(rec, encoding="utf-8") as f:
            src = (json.load(f) or {}).get("source_url") or ""
    except (OSError, ValueError, AttributeError):
        src = ""
    src = src or str(s.manifest.get("source_url") or "")
    if src and "youtube.com" not in src:
        out.append(f"官网：{src}")
    year = re.search(r"(20\d{2})", s.name)
    if year:
        out.append(f"文件夹名里的年份：{year.group(1)}")
    return out


def _missing(entry: dict, truth: dict) -> dict:
    """这个条目跟补完的条目比还缺哪些日期字段。手机上猜的发表日期（publish_guessed）当没有。"""
    out = {}
    if not summit_dates.publish_day(entry) and summit_dates.publish_day(truth):
        out["publish_date"] = truth["publish_date"]
        if truth.get("publish_at"):
            out["publish_at"] = truth["publish_at"]
    if not summit_dates.session_day(entry) and summit_dates.session_day(truth):
        out["session_date"] = truth["session_date"]
    return out


def _fill(entry: dict, fields: dict) -> None:
    """补字段；补了真的发表日期，猜的标记和不配套的旧发表时刻去掉。"""
    if "publish_date" in fields:
        entry.pop("publish_guessed", None)
        if "publish_at" not in fields:
            entry.pop("publish_at", None)
    entry.update(fields)


def _read_manifest(folder: str) -> tuple[Optional[dict], Optional[str], str]:
    """(内容, 写回时的结尾, 不改的原因)。"""
    raw = _read(summit_dates.manifest_path(folder))
    try:
        data = json.loads(raw) if raw is not None else None
    except ValueError:
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("entries"), dict):
        return None, None, "处理记录读不出来或格式不对"
    tail = _tail(raw, data)
    if tail is None:
        return data, None, "处理记录的格式跟流水线写出来的不一样（写回会重排），不改"
    return data, tail, ""


def _patch(data: dict, truth: dict[str, dict], *, summit_level: bool) -> int:
    """照补完的条目补一份处理记录（改的是 data 本身）：返回改了几处。会议目录那份更新会议时间（只存议程的、
    老的 sessions 删掉），专题文件夹那份去掉自动存的会议时间。"""
    n = 0
    for vid, row in data["entries"].items():
        entry = row.get("entry") if isinstance(row, dict) else None
        t = truth.get(str(vid))
        if not isinstance(entry, dict) or t is None:
            continue
        fields = _missing(entry, t)
        if fields:
            _fill(entry, fields)
            n += 1
    moved = summit_dates.auto_event_dates(data) if summit_level else summit_dates.strip_event_dates(data)
    return n + (1 if moved else 0)


def plan_summit(folder: str, root: str, *, fetch: Optional[Fetch], limit: list, sleep: float = 0.0,
                cache_dir: str = "", log=print) -> Summit:
    s = Summit(folder, os.path.relpath(folder, root).replace(os.sep, "/"))
    s.root = root
    data, _tail_root, why = _read_manifest(folder)
    if data is None:
        s.problem = why
        return s
    s.manifest = data
    s.children = sorted(os.path.dirname(p) for p in glob.glob(os.path.join(glob.escape(folder), "*", ".manifest.json"))
                        if not os.path.basename(os.path.dirname(p)).startswith("."))
    s.cache_path = (os.path.join(cache_dir, f"{s.name}.json") if cache_dir
                    else os.path.join(folder, ".cache", CACHE_NAME))
    s.cache = _load_cache(s)
    p = _pipeline()
    consecutive = 0
    after = copy.deepcopy(data)
    for vid, row in data["entries"].items():
        if not isinstance(row, dict) or not isinstance(row.get("entry"), dict):
            continue
        entry = row["entry"]
        s.total += 1
        fields: dict = {}
        if not summit_dates.session_day(entry):
            sd = p.agenda_session_date(entry)
            if sd:
                fields["session_date"] = sd
        # 手机上猜的发表日期（publish_guessed）当没有日期，能补真的就补
        when = doctime.entry_published(entry) if summit_dates.publish_day(entry) else None
        if when is not None:
            s.dated += 1
        else:
            for key in FILE_KEYS:
                rel = row.get(key)
                text = _read(os.path.join(folder, rel)) if isinstance(rel, str) and rel else None
                when = _file_published(text) if text else None
                if when is not None:
                    s.from_files += 1
                    break
            if when is None:
                meta = _meta_info(s, str(vid))
                when = info_when(meta) if meta else None
                if when is not None:
                    s.from_meta += 1
            if when is None:
                hit = s.cache.get(str(vid))
                if isinstance(hit, dict) and not hit.get("error"):
                    when = info_when(hit)
                    if when is not None:
                        s.from_cache += 1
            url = str(entry.get("url") or "")
            if when is None and url.startswith("https://www.youtube.com/"):
                if fetch is None or s.network_stopped or (limit[0] is not None and limit[0] <= 0):
                    s.need_network.append(str(vid))
                else:
                    if limit[0] is not None:
                        limit[0] -= 1
                    try:
                        info = fetch(url)
                        when = info_when(info)
                        if when is None:
                            raise ValueError("YouTube 没给发布日期")
                        s.cache[str(vid)] = {k: info[k] for k in ("upload_date", "timestamp", "release_timestamp") if info.get(k)}
                        s.from_network += 1
                        consecutive = 0
                    except Exception as e:  # noqa: BLE001
                        msg = str(e).splitlines()[0][:200] if str(e) else type(e).__name__
                        s.cache[str(vid)] = {"error": msg, "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
                        s.failed.append((str(vid), msg))
                        consecutive += 1
                        if consecutive >= MAX_CONSECUTIVE_FAILURES:
                            s.network_stopped = True
                            log(f"  {s.name}：连着 {consecutive} 次查不到，这个会议先不查了")
                    _save_cache(s)
                    if sleep:
                        time.sleep(sleep)
            elif when is None:
                s.unresolved.append(str(vid))
            if when is not None:
                _stamp(fields, when)
        if fields:
            s.updates[str(vid)] = fields
            _fill(after["entries"][vid]["entry"], fields)
        t = after["entries"][vid]["entry"]
        s.truth[str(vid)] = t
        # 文件里补发表时间
        final = doctime.entry_published(t) if summit_dates.publish_day(t) else None
        if final is None:
            continue
        line = doctime.lines(final, None)[0]
        for key in FILE_KEYS:
            rel = row.get(key)
            if not isinstance(rel, str) or not rel:
                continue
            path = os.path.join(folder, rel)
            text = _read(path)
            if text is None:
                if os.path.exists(path):
                    s.skipped.append((f"{s.rel}/{rel}", "读不出来"))
                continue
            new, why_file = _header_insert(text, line)
            if why_file:
                s.skipped.append((f"{s.rel}/{rel}", why_file))
            elif new is not None and new != text:
                s.file_adds[path] = (text, new)
    # 各份处理记录（会议目录 + 各专题文件夹）分别照补完的条目比，缺什么补什么
    for f in [folder] + s.children:
        d, tail, why_m = _read_manifest(f)
        rel = os.path.relpath(summit_dates.manifest_path(f), root).replace(os.sep, "/")
        if d is None or tail is None:
            s.skipped_manifests.append((rel, why_m))
            continue
        n = _patch(copy.deepcopy(d), s.truth, summit_level=(f == folder))
        if n:
            s.manifest_changes[f] = n
    s.event_before = summit_dates.stored(data)
    probe = copy.deepcopy(after)
    s.event_change = summit_dates.auto_event_dates(probe)
    s.event_after = summit_dates.resolve(probe)
    s.video_coverage = summit_dates.coverage(probe, "video")
    if s.event_after is None or s.event_after["source"] == "sessions":
        s.manual_hint = summit_dates.publish_spread(probe)
        s.suggestions = suggestions(s)
    return s


def build(root: str, *, fetch: Optional[Fetch], only: Optional[list[str]] = None, limit: Optional[int] = None,
          sleep: float = 0.0, cache_dir: str = "", log=print) -> list[Summit]:
    budget = [limit]
    return [plan_summit(f, root, fetch=fetch, limit=budget, sleep=sleep, cache_dir=cache_dir, log=log)
            for f in summit_folders(root, only)]


# ---------------------------------------------------------------- 报告

def _event_text(s: Summit) -> str:
    ev = s.event_after
    lo, hi = s.manual_hint
    if ev is None:
        dated, total = s.video_coverage
        if dated and not summit_dates.enough(dated, total):
            return f"日期太少（{dated}/{total} 场有视频发布日期），需要手动填会议时间"
        if lo:
            return f"需要手动填会议时间（视频发布 {summit_dates.label(lo)} 到 {summit_dates.label(hi)}，跨度超过 {summit_dates.MAX_DERIVED_SPAN_DAYS} 天）"
        return "需要手动填会议时间（一场日期都没有）"
    text = summit_dates.display(ev)
    if ev["source"] == "sessions":
        return f"{text}（只是视频发布日期，不存；会议时间要手动填）"
    if ev["source"] == "manual":
        return f"{text}（手动填的，不动）"
    return f"{text}（按议程，{'要写进处理记录' if s.event_change else '已存着'}）"


def report(summits: list[Summit], out=None) -> None:
    out = out or sys.stdout
    w = lambda t="": print(t, file=out)  # noqa: E731
    w("会议 | 场数 | 已有日期 | 从文件补 | 从字幕缓存补 | 从联网缓存补 | 这次联网补 | 还要联网 | 查不到 | 补不了 | 会议时间")
    for s in summits:
        if s.problem:
            w(f"{s.name} | {s.problem}")
            continue
        w(f"{s.name} | {s.total} | {s.dated} | {s.from_files} | {s.from_meta} | {s.from_cache} | {s.from_network} | "
          f"{len(s.need_network)} | {len(s.failed)} | {len(s.unresolved)} | {_event_text(s)}")
    w()
    for s in summits:
        for rel, why in s.skipped_manifests:
            w(f"不改处理记录：{rel}（{why}）")
        if s.manifest_changes or s.file_adds:
            w(f"{s.name}：改 {len(s.manifest_changes)} 份处理记录（共 {sum(s.manifest_changes.values())} 处）、"
              f"补 {len(s.file_adds)} 个文件的发布时间")
        for hint in s.suggestions:
            w(f"  {s.name} 的线索（只供参考，没写进去）：{hint}")
        for rel, why in s.skipped[:20]:
            w(f"  不改文件：{rel}（{why}）")
        if len(s.skipped) > 20:
            w(f"  ……还有 {len(s.skipped) - 20} 个文件不改")
        for vid, msg in s.failed[:5]:
            w(f"  查不到：{vid}（{msg}）")


def cache_note(summits: list[Summit]) -> list[str]:
    return [s.cache_path for s in summits if s.cache_written]


# ---------------------------------------------------------------- 照计划改

def _apply_manifest(folder: str, truth: dict[str, dict], *, summit_level: bool) -> int:
    """拿着锁重读一份处理记录、照补完的条目补（只补原来没有的），改了才写。格式跟流水线写的不一样的不改。"""
    with summit_dates.manifest_lock(folder):
        data, tail, _why = _read_manifest(folder)
        if data is None or tail is None:
            return 0
        n = _patch(data, truth, summit_level=summit_level)
        if n:
            atomic.write_text(summit_dates.manifest_path(folder), _dump(data) + tail)
    return n


def apply(summits: list[Summit], log=print) -> list[str]:
    """照计划改。返回因为迁移记录写不上、文件这次没改的会议名。"""
    skipped: list[str] = []
    for s in summits:
        if s.problem:
            continue
        # 各份处理记录每次都照补完的条目重新比：上次中途停下、只改了一部分的，再跑会补齐
        for f in [s.folder] + s.children:
            _apply_manifest(f, s.truth, summit_level=(f == s.folder))
        todo = {}
        for path, (old, new) in s.file_adds.items():
            if _read(path) != old:
                log(f"  不改文件（计划之后又改过）：{os.path.relpath(path, s.folder)}")
                continue
            todo[path] = (old, new)
        if not todo:
            continue
        # 先记进迁移记录（Spark/剪藏/.renames.json 的 rewritten，跟 tools/migrate_times.py 同一份、同一把锁）再写：
        # 手机上还在回填前那一版上改的，电脑认得出（clips.migrated_from），不判冲突；回填和迁移前后两次改写也接得起来
        root = os.path.realpath(s.root or os.path.dirname(os.path.dirname(s.folder)))

        def change(journal, todo=todo, root=root):
            for path, (old, new) in todo.items():
                rel = os.path.relpath(os.path.realpath(path), root).replace(os.sep, "/")
                merge_rewritten(journal["rewritten"], rel, old, new)
        try:
            clips.update_journal(root, change)
        except clips.JournalError as e:
            log(f"  迁移记录写不上（{e}），{s.name} 的文件这次没改")
            skipped.append(s.name)
            continue
        for path, (old, new) in todo.items():
            _write_keep_times(path, new)
    return skipped


def main(argv: Optional[list[str]] = None, fetch: Optional[Fetch] = None) -> int:
    ap = argparse.ArgumentParser(description="给会议补日期：各场视频的发布时间和会议时间")
    ap.add_argument("--root", required=True, help="笔记库的根目录（下面有 Spark/）")
    ap.add_argument("--apply", action="store_true", help="照计划改（默认只列计划）")
    ap.add_argument("--only", action="append", default=[], help="只看这个会议文件夹（可以给几次）")
    ap.add_argument("--no-network", action="store_true", help="不联网，只用笔记库里现成的日期。不加 --apply 什么都不写；加了 --apply 照计划改处理记录和文件，只是不联网")
    ap.add_argument("--limit", type=int, default=None, help="这次最多联网查几个视频")
    ap.add_argument("--sleep", type=float, default=0.5, help="两次联网之间多等几秒")
    ap.add_argument("--cache-dir", default="", help="联网结果缓存放这个目录（默认放各会议文件夹的 .cache/ 里）")
    args = ap.parse_args(argv)
    root = os.path.abspath(os.path.expanduser(args.root))
    if not os.path.isdir(os.path.join(root, SPARK)):
        print(f"{root} 下面没有 {SPARK}/，不是笔记库", file=sys.stderr)
        return 2
    # 服务开着时不改：正在跑的任务手里拿着旧的处理记录，它下次存盘会把补的日期冲掉
    if args.apply and server_running():
        print(f"{SERVER_PORT} 端口有人在听（Spark 服务开着），没改。先停掉服务再加 --apply。", file=sys.stderr)
        return 3
    fetcher = None if args.no_network else (fetch or youtube_fetch)
    cache_dir = os.path.abspath(os.path.expanduser(args.cache_dir)) if args.cache_dir else ""
    summits = build(root, fetch=fetcher, only=args.only or None, limit=args.limit, sleep=args.sleep, cache_dir=cache_dir)
    report(summits)
    written = cache_note(summits)
    for path in written:
        print(f"写了联网结果缓存（再跑不重查）：{path}")
    if args.apply:
        skipped = apply(summits)
        if skipped:
            print(f"有 {len(skipped)} 个会议因为迁移记录写不上，文件没改：{'、'.join(skipped)}（处理记录已改，修好迁移记录后再跑一遍）")
            return 5
        print("已照计划改完。")
    else:
        print("这是计划，处理记录和文档都没动" + ("（只写了上面列的联网结果缓存）" if written else "") + "；照着改加 --apply。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
