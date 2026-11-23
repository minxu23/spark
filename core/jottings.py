"""
随手记：自己手写的笔记，一条一个 .md，放在笔记库 Spark/笔记/ 下。跟读书笔记（文章里的划线、批注、读后感）合称「笔记」。

文件的写法（跟手机 apps/mobile-ios/Spark/Jottings.swift 一字不差，tests/test_jottings.py 编手机的代码对照）：

    ---
    title: "标题"
    created_at: "2026-11-22T21:30:05+08:00"
    updated_at: "2026-11-22T21:42:10+08:00"
    tags: ["…"]          # 用户自己填了才有
    starred: true        # 加了星标才有，跟剪藏同一个键
    ---

    # 标题

    - 收录时间：2026年11月22日，21:30（UTC+8）
    - 修改时间：2026年11月22日，21:42（UTC+8）   # 改过以后才有，每改一次换掉

    正文…

- 文件名 YYYYMMDD_<标题>.md（新建那天、本机时区；起名规矩同剪藏 clip_compose.file_name），之后不再改名：
  改标题只改文件头的 title 和第一行「# 标题」。
- 标题空着：用正文第一行（最多 30 个字）。
- 正文可以有划线、批注、读后感（文件在 Spark/ 下，core/annotations.annotatable 认）。改正文只改文档头和
  「## 读后感」、批注脚注之间的那一段；读后感、脚注原样留着。正文里删掉的批注号，它的脚注一起删；
  原来就没挂在划线上的批注不动。
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from datetime import date, datetime
from typing import Optional

from core import annotations, atomic, doctime, vault
from core.clip_compose import file_name as _clip_file_name, yaml

DIRNAME = "笔记"
DIR = f"{vault.SPARK_DIRNAME}/{DIRNAME}"
TITLE_FROM_BODY = 30          # 标题空着时取正文第一行的前多少个字
MAX_TITLE = 200
MAX_BODY = 200_000
RECENT_DAYS = 7
RECENT_ITEMS = 3

_HEAD_ITEM = re.compile(r"^- [^：\n]{1,16}：")          # 同 core/doctime 文档头那串「- 键：值」
_MODIFIED_PREFIX = f"- {doctime.MODIFIED_LABEL}："
_REFLECTION = re.compile(r"^## 读后感[ \t]*\Z")
_NOTE_DEF = re.compile(r"^\[\^note-(\d+)\]:")
_FENCE = re.compile(r"^ {0,3}(?:```|~~~)")
# 两头去掉的空白：Unicode 的 Zs 类空格、Tab、各种换行、零宽空格。手机 Jottings.swift 用同一张表（逐个写出来，
# 不用两边各自的「空白」定义：Swift 的 whitespaces 认 U+200B，Python 不认）
_WS = ("\t\n\x0b\x0c\r\x85\u2028\u2029\u200b\u180e \xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007"
       "\u2008\u2009\u200a\u202f\u205f\u3000")
# 文件名里换成空格的：剪藏那套（clip_compose.file_name）加上零宽空格
_BAD_NAME = re.compile(r'[\\/:*?"<>|\n\r\t#^\[\]\x00-\x1f\x7f\u200b]+')
_NOTE_REF = re.compile(r"\[\^note-(\d+)\](?!:)")
_LEADING_MARKS = re.compile(r"^[#>*\- \t]+")
_INLINE_MARKS = re.compile(r"\[\^note-\d+\]|==|\*\*")


class JottingError(Exception):
    """存不了：内容不对（空的、太长）、文件不在随手记文件夹里。"""


class Conflict(JottingError):
    """打开编辑以后，文件在别处（手机、Obsidian）又改过了：不覆盖。"""


# ---------------------------------------------------------------- 路径

def is_path(rel: str) -> bool:
    """rel 相对笔记库：Spark/笔记/ 下直接的一篇 .md，不带 .. 和点开头的名字。"""
    if not isinstance(rel, str) or not rel.startswith(DIR + "/") or not rel.endswith(".md") or "\\" in rel:
        return False
    rest = rel[len(DIR) + 1:]
    return bool(rest) and "/" not in rest and not rest.startswith(".") and rest.strip() == rest \
        and not any(ord(ch) < 32 for ch in rest)


def folder(root: str = "") -> str:
    return os.path.join(root or vault.vault_root(), *DIR.split("/"))


def file_name(title: str, created: datetime) -> str:
    """新建那天（本机时区）+ 标题，规矩同剪藏。先按上面的表换掉、去掉两头空白，剪藏那一步就不会两边不一样。"""
    return _clip_file_name(_BAD_NAME.sub(" ", title).strip(_WS), doctime.day(created))


# ---------------------------------------------------------------- 写法

def _one_line(s: str) -> str:
    return re.sub(r"[\r\n]+", " ", s or "").strip(_WS)


def normalize_body(body: str) -> str:
    """换行统一成 \\n；去掉开头的空行和末尾的空白。第一行开头的缩进（「　　」）留着（同手机 Jottings.normalize）。"""
    lines = (body or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    while lines and not lines[0].strip(_WS):
        lines.pop(0)
    return "\n".join(lines).rstrip(_WS)


def title_of(title: str, body: str) -> str:
    """标题：填了用填的（换行换成空格）；空着用正文第一行，去掉开头的 #、>、- 和划线、批注号，最多 30 个字。"""
    t = _one_line(title)
    if t:
        return t
    first = next((ln for ln in normalize_body(body).split("\n") if ln.strip(_WS)), "")
    first = _INLINE_MARKS.sub("", _LEADING_MARKS.sub("", first)).strip(_WS)
    return first[:TITLE_FROM_BODY].strip(_WS) or "未命名"


def _compose(header: list[str], body: str, tail: list[str]) -> str:
    out = "\n".join(header) + "\n"
    if body:
        out += "\n" + body + "\n"
    t = "\n".join(tail).strip("\n")
    if t:
        out += "\n" + t + "\n"
    return out


def render(title: str, body: str, created: datetime, *, tags: Optional[list[str]] = None, starred: bool = False) -> str:
    """新建的一条。"""
    b = normalize_body(body)
    t = title_of(title, b)
    stamp = yaml(doctime.iso(created))
    fm = ["---", f"title: {yaml(t)}", f"created_at: {stamp}", f"updated_at: {stamp}"]
    if tags:
        fm.append("tags: [" + ", ".join(yaml(x) for x in tags) + "]")
    if starred:
        fm.append("starred: true")
    fm.append("---")
    header = fm + ["", f"# {t}", "", f"- {doctime.CAPTURED_LABEL}：{doctime.cn(created)}"]
    return _compose(header, b, [])


def _split(text: str) -> dict:
    """按行拆开：文件头（--- 之间）、标题行、标题下面那串「- 键：值」、正文的起止行。
    正文到读后感或第一条批注脚注为止，哪个在前算哪个：
    - 读后感按 core/annotations.reflection_range 认（同手机 DocEdit.reflectionRange），而且它后面只剩脚注、空行，
      标题那一行就是「## 读后感」本身（不带 \\r）。正文中间自己写的一行「## 读后感」、后面还有别的小节的，不算；
    - 代码块（``` 或 ~~~ 围起来的）里的「[^note-3]: …」不算脚注。"""
    lines = text.split("\n")
    n = len(lines)
    front = None
    i = 0
    if lines and lines[0] == "---":
        for j in range(1, n):
            if lines[j] == "---":
                front = j
                i = j + 1
                break
    while i < n and lines[i] == "":
        i += 1
    h1, items = None, []
    if i < n and lines[i].startswith("# "):
        h1 = i
        i += 1
        while i < n and lines[i] == "":
            i += 1
        while i < n and _HEAD_ITEM.match(lines[i]):
            items.append(i)
            i += 1
    start = i
    end = n
    r = annotations.reflection_range(text)
    if r:
        head, after = text.count("\n", 0, r[0]), text.count("\n", 0, r[1])
        if head >= start and head < n and _REFLECTION.match(lines[head]) \
                and all(ln == "" or _NOTE_DEF.match(ln) for ln in lines[after:]):
            end = head
    fenced = False
    for j in range(start, n):
        if _FENCE.match(lines[j]):
            fenced = not fenced
        elif not fenced and _NOTE_DEF.match(lines[j]):
            end = min(end, j)
            break
    return {"lines": lines, "front": front, "h1": h1, "items": items, "start": start, "end": end}


def editable_body(text: str) -> str:
    """编辑框里放的正文：文档头和读后感、批注脚注之间的那一段。"""
    s = _split(text)
    return normalize_body("\n".join(s["lines"][s["start"]:s["end"]]))


def _set_key(head: list[str], key: str, value: str, after: str = "") -> list[str]:
    """改文件头里的一项（同手机 DocEdit.setting）：原来在哪就放哪；没有的放在 after 那项后面，再没有放最后。"""
    at = next((k for k, ln in enumerate(head) if ln.startswith(f"{key}:")), None)
    out = [ln for ln in head if not ln.startswith(f"{key}:")]
    if at is None and after:
        a = next((k for k, ln in enumerate(out) if ln.startswith(f"{after}:")), None)
        at = a + 1 if a is not None else None
    out.insert(len(out) if at is None else min(at, len(out)), f"{key}: {value}")
    return out


def updated(text: str, title: str, body: str, now: datetime) -> str:
    """改过的一条：标题、正文换掉，updated_at 和「修改时间」换成 now；文件头别的项、读后感、脚注照旧。
    正文里删掉的批注号，脚注一起删。"""
    s = _split(text)
    lines = s["lines"]
    old_body = normalize_body("\n".join(lines[s["start"]:s["end"]]))
    b = normalize_body(body)
    t = title_of(title, b)
    head = lines[1:s["front"]] if s["front"] is not None else []
    head = _set_key(head, "title", yaml(t))
    head = _set_key(head, "updated_at", yaml(doctime.iso(now)), after="created_at")
    items = [lines[k] for k in s["items"] if not lines[k].startswith(_MODIFIED_PREFIX)]
    items.append(doctime.modified_line(now))
    header = ["---"] + head + ["---", "", f"# {t}", ""] + items
    # 正文里删掉的批注号；读后感里还引着的不算（脚注留着，免得读后感里的引用落空）
    kept = "\n".join(ln for ln in lines[s["end"]:] if not _NOTE_DEF.match(ln))
    gone = {int(x) for x in _NOTE_REF.findall(old_body)} - {int(x) for x in _NOTE_REF.findall(b + "\n" + kept)}
    tail = [ln for ln in lines[s["end"]:] if not ((m := _NOTE_DEF.match(ln)) and int(m.group(1)) in gone)]
    return _compose(header, b, tail)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 读

def _when(v: object) -> Optional[datetime]:
    w = doctime.parse(v)
    if isinstance(w, datetime):
        return w
    if isinstance(w, date):
        return datetime(w.year, w.month, w.day).astimezone()
    return None


def excerpt(body: str, limit: int = 80) -> str:
    """列表上的一句：正文第一行（去掉开头的 #、>、- 和划线、批注号）。"""
    for ln in body.split("\n"):
        t = _INLINE_MARKS.sub("", _LEADING_MARKS.sub("", ln)).strip(_WS)
        if t:
            return t if len(t) <= limit else t[:limit] + "…"
    return ""


def parse(text: str, name: str = "", mtime: float = 0.0) -> dict:
    """一条的信息：标题、新建和修改的时刻、正文第一行、星标、划线和批注数。"""
    from core import clips
    fm = clips.frontmatter(text)
    s = _split(text)
    lines = s["lines"]
    h1 = lines[s["h1"]][2:].strip(_WS) if s["h1"] is not None else ""
    title = str(fm.get("title") or h1 or (name[:-3] if name.endswith(".md") else name) or "未命名")
    created = _when(fm.get("created_at"))
    if created is None:
        m = doctime.CAPTURED_LINE.search(text)
        created = _when(m.group(1)) if m else None
    if created is None and re.match(r"\d{8}_", name):
        created = _when(name[:8])
    if created is None:
        created = datetime.fromtimestamp(mtime or time.time()).astimezone()
    changed = _when(fm.get("updated_at")) or created
    body = normalize_body("\n".join(lines[s["start"]:s["end"]]))
    note = annotations.reading_note(text)
    return {"title": title, "created": created, "updated": changed, "excerpt": excerpt(body),
            "starred": annotations.is_starred(text), "marks": len(note["items"]),
            "notes": sum(len(i["notes"]) for i in note["items"]) + len(note["loose"]),
            "reflection": bool(note["reflection"])}


def list_all(root: str = "") -> list[dict]:
    """全部随手记，最近改过的在前。每条多带 rel（相对笔记库）、name。"""
    d = folder(root)
    out = []
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return []
    for name in names:
        rel = f"{DIR}/{name}"
        full = os.path.join(d, name)
        if not is_path(rel) or not os.path.isfile(full):
            continue
        try:
            with open(full, encoding="utf-8", errors="replace") as f:
                text = f.read()
            mtime = os.path.getmtime(full)
        except OSError:
            continue
        out.append(dict(parse(text, name, mtime), rel=rel, name=name))
    return sorted(out, key=lambda j: (-j["updated"].timestamp(), j["rel"]))


def sort(items: list[dict], by: str) -> list[dict]:
    """by：updated 最近修改 / created 创建时间 / name 名称。"""
    if by == "created":
        return sorted(items, key=lambda j: (-j["created"].timestamp(), j["rel"]))
    if by == "name":
        return sorted(items, key=lambda j: (j["title"].lower(), j["rel"]))
    return sorted(items, key=lambda j: (-j["updated"].timestamp(), j["rel"]))


def summarize(jottings: list[dict], reading: list[dict], now: datetime) -> dict:
    """笔记页最上面那张卡的数字（同手机 Jottings.summarize）。不调模型。
    jottings：[{rel, title, created, updated}]；reading：[{rel, title, changed}]（有笔记的文件）。
    - 读书笔记只算 Spark/笔记/ 以外的：随手记里划了线的不重复算；
    - 近 7 天（含正好 7 天前那一刻）新增：这几天新建的随手记；修改：之前就有、这几天改过的随手记，加上这几天改过的读书笔记；
    - 最近的三条：随手记按修改时刻、读书笔记按文件改动时刻，新的在前。"""
    reading = [r for r in reading if not r["rel"].startswith(DIR + "/")]
    cutoff = now.timestamp() - RECENT_DAYS * 86400
    added = sum(1 for j in jottings if j["created"].timestamp() >= cutoff)
    edited = sum(1 for j in jottings if j["updated"].timestamp() >= cutoff and j["created"].timestamp() < cutoff
                 and j["updated"].timestamp() > j["created"].timestamp())
    edited += sum(1 for r in reading if r["changed"].timestamp() >= cutoff)
    rows = [{"rel": j["rel"], "title": j["title"], "kind": "随手记", "at": j["updated"]} for j in jottings] \
        + [{"rel": r["rel"], "title": r["title"], "kind": "读书笔记", "at": r["changed"]} for r in reading]
    rows.sort(key=lambda r: (-r["at"].timestamp(), r["rel"]))
    return {"jottings": len(jottings), "reading": len(reading), "latest": rows[0]["at"] if rows else None,
            "added": added, "edited": edited, "recent": rows[:RECENT_ITEMS]}


def summary(root: str = "", now: Optional[datetime] = None) -> dict:
    """笔记库里的随手记和读书笔记，算成卡片上的数字。读书笔记的标题由调用方按需要补（这里给文件头的 title 或第一行标题）。"""
    from core import clips
    root = root or vault.vault_root()
    js = list_all(root)
    reading = []
    for rel, mtime, _n in annotations.annotated_docs(root):
        if rel.startswith(DIR + "/"):
            continue
        reading.append({"rel": rel, "title": "", "changed": datetime.fromtimestamp(mtime).astimezone()})
    out = summarize(js, reading, now or doctime.now())
    for r in out["recent"]:
        if r["kind"] == "读书笔记":
            try:
                with open(os.path.join(root, *r["rel"].split("/")), encoding="utf-8", errors="replace") as f:
                    head = f.read(16384)
            except OSError:
                head = ""
            h1 = re.search(r"^# +(.+)$", head, re.M)
            r["title"] = str(clips.frontmatter(head).get("title") or (h1.group(1).strip() if h1 else "")
                             or os.path.basename(r["rel"])[:-3])
    return out


# ---------------------------------------------------------------- 写

def _check(title: str, body: str) -> str:
    b = normalize_body(body)
    if not b and not _one_line(title):
        raise JottingError("标题和正文都是空的，没存")
    if len(b) > MAX_BODY or len(_one_line(title)) > MAX_TITLE:
        raise JottingError("太长了，没存")
    return b


def create(title: str, body: str, *, root: str = "", now: Optional[datetime] = None) -> str:
    """新建一条，返回相对笔记库的路径。同名的另起 _2、_3。"""
    b = _check(title, body)
    now = now or doctime.now()
    root = root or vault.vault_root()
    d = folder(root)
    os.makedirs(d, exist_ok=True)
    name = file_name(title_of(title, b), now)
    stem = name[:-3]
    for n in range(2, 100):
        if not os.path.exists(os.path.join(d, name)):
            break
        name = f"{stem}_{n}.md"
    else:
        raise JottingError("同名的太多了，没存")
    atomic.write_text(os.path.join(d, name), render(title, b, now))
    return f"{DIR}/{name}"


def update(rel: str, title: str, body: str, base: str, *, root: str = "", now: Optional[datetime] = None) -> str:
    """改一条。base 是打开编辑时那一版的 SHA-256：文件后来又变过就不写，抛 Conflict。返回新内容的 SHA-256。"""
    if not is_path(rel):
        raise JottingError("这不是一条随手记")
    b = _check(title, body)
    root = os.path.realpath(root or vault.vault_root())
    full = os.path.realpath(os.path.join(root, *rel.split("/")))
    if os.path.commonpath([full, root]) != root:
        raise JottingError("路径不在笔记库里")
    try:
        with open(full, encoding="utf-8") as f:
            current = f.read()
    except FileNotFoundError:
        raise Conflict("这条随手记已经不在了（删了或者移进了废纸篓）。你写的字还在框里，先复制下来。") from None
    if sha256(current) != base:
        raise Conflict("这条随手记在别处改过了（手机上或者 Obsidian 里），没有覆盖。你写的字还在框里：先复制下来，再刷新页面看新的版本。")
    new = updated(current, title, b, now or doctime.now())
    atomic.write_text(full, new)
    return sha256(new)
