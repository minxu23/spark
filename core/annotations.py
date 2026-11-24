"""笔记：读的时候留下的划线、批注、读后感。

所有给人读的 Markdown（剪藏、整理稿、文字记录、信息跟进的原文、小结、会议资料、报告、复盘）
都用同一种写法，存在文件自己里面，Obsidian 里也看得到：

- 划线：==文字==
- 批注：划线后面跟脚注号「==文字==[^note-3]」，文末「[^note-3]: 批注」
- 读后感：文末「## 读后感」一节，放在批注脚注前面

哪些文件能做笔记只看 annotatable()：新加的资料类型只要是笔记库 Spark/ 或 output/ 下的 Markdown，
自动就能划线、批注、写读后感，不用逐类开。手机端 DocEdit.annotatable 是同一条规则。
阅读页（apps/reader）、复盘（core/review.py）、手机发回来的改动（core/remote_tasks.py）都走这里。
"""
from __future__ import annotations

import os
import re
import threading

from core import vault

NOTE_DEF_RE = re.compile(r"^\[\^note-(\d+)\]:[ \t]?(.*)$", re.M)

# ---- 划线的写法，全库只在这里定义一次（手机 MarkRule.swift 是同一条）：
# 「==」+ 内容 +「==」。内容不换行、不含连着的「==」、不以「=」开头或结尾，可以有单个「=」（「Gemini = 沟通」）；
# 两头的「==」外面不能再紧挨「=」，所以「====x====」「==x===」都不算划线。
MARK_INNER = r"[^=\n](?:=?[^=\n])*"
MARK_PATTERN = rf"(?<!=)==({MARK_INNER})==(?!=)"
MARK_TEXT_RE = re.compile(MARK_PATTERN)                                   # 第 1 组：划的字
MARK_RE = re.compile(MARK_PATTERN + r"((?:\[\^note-\d+\])*)")             # 第 2 组：后面跟着的批注号
_TRIM_CHARS = "= \t\r\n　 "   # 选区两头要去掉的：等号和紧挨的空白（同 MarkRule.trimChars）


def mark_spans(text: str) -> list[tuple[int, int, int, int]]:
    """每处划线的 (起, 止, 内容起, 内容止)。"""
    return [(m.start(), m.end(), m.start(1), m.end(1)) for m in MARK_TEXT_RE.finditer(text)]


def trim_selection(s: str) -> str:
    """选中的字两头的「=」和紧挨的空白去掉（同 MarkRule.trimmed）。"""
    return s.strip(_TRIM_CHARS)


def in_mark(text: str, a: int, b: int) -> bool:
    """text[a:b] 整个落在某一处划线的内容里。"""
    return any(ia <= a and b <= ib for _, _, ia, ib in mark_spans(text))


def grow_selection(text: str, a: int, b: int, spans=None) -> tuple[int, int] | None:
    """划线前把选区扩好（add_mark 和网页批注共用，同手机 MarkRule.wrap 里的那一段）：
    碰到（重叠或紧挨着）的已有划线并进来；外面紧挨着一个字面的「=」（「ROI=收益」只选「收益」）时，
    把这个「=」和它外面一个字一起包进来，扩完再并一次，直到外面不再紧挨「=」。
    「=」在行首行尾、连着两个以上（又不属于已有划线）的扩不了，返回 None。"""
    spans = mark_spans(text) if spans is None else spans
    grown = True
    while grown:
        grown, moved = False, True
        while moved:
            moved = False
            for s, e, _, _ in spans:
                if s <= b and a <= e and (s < a or e > b):
                    a, b, moved = min(a, s), max(b, e), True
        if a > 0 and text[a - 1] == "=":
            if a < 2 or text[a - 2] in "=\n":
                return None
            a, grown = a - 2, True
        if b < len(text) and text[b] == "=":
            if b + 1 >= len(text) or text[b + 1] in "=\n":
                return None
            b, grown = b + 2, True
    return a, b


def add_mark(text: str, a: int, b: int) -> str | None:
    """把 text[a:b] 划上，返回新文字；不用改（已经整个在一处划线里、去掉两头的等号后是空的、
    外面紧挨的「=」扩不了）就返回 None，文字不变。同手机 MarkRule.wrap，两边用同一张用例表核对：
    - 两头的「=」和紧挨的空白先去掉；
    - 碰到（重叠或紧挨着）的已有划线并成一处，中间原来的「==」去掉；
    - 外面紧挨着一个字面的「=」时往外扩，把「=」和它外面一个字包进来（「=」在行首行尾、连着两个以上的不划）；
    - 跨了几行的，每行各划一处（划线不能跨行）。
    任何顺序划几次都不会出现「===」。"""
    while a < b and text[a] in _TRIM_CHARS:
        a += 1
    while b > a and text[b - 1] in _TRIM_CHARS:
        b -= 1
    if a >= b:
        return None
    if "\n" in text[a:b]:
        out, changed, starts = text, False, [a] + [i + 1 for i in range(a, b) if text[i] == "\n"]
        for s in reversed(starts):
            e = text.find("\n", s, b)
            got = add_mark(out, s, b if e < 0 else e)
            if got is not None:
                out, changed = got, True
        return out if changed else None
    spans = mark_spans(text)
    if any(ia <= a and b <= ib for _, _, ia, ib in spans):
        return None
    got = grow_selection(text, a, b, spans)
    if got is None:
        return None
    a, b = got
    inner = text[a:b].replace("==", "")
    if not inner or inner[0] == "=" or inner[-1] == "=" or "\n" in inner:
        return None
    if (a > 0 and text[a - 1] == "=") or (b < len(text) and text[b] == "="):
        return None
    out = f"{text[:a]}=={inner}=={text[b:]}"
    return None if out == text else out


REFLECTION_HEAD_RE = re.compile(r"^## 读后感[ \t]*$", re.M)
READ_ORIGINAL = "\n[阅读原文]("
_NEXT_SECTION_RE = re.compile(r"^## ", re.M)

# 程序生成的列表页，不是拿来读的文章
_LIST_PAGES = {"索引.md", "摘录.md"}


def annotatable(rel: str) -> bool:
    """rel 是相对笔记库根目录的路径（「Spark/剪藏/x.md」「output/复盘/y.md」）。
    Spark/ 和 output/ 下的 Markdown 都能做笔记；剪藏索引、摘录、节目 / 会议文件夹的总览页
    （「Spark/All-In Podcast/All-In Podcast.md」）这类生成的列表页除外。"""
    rel = str(rel or "").replace("\\", "/").strip("/")
    parts = rel.split("/")
    if not rel.endswith(".md") or len(parts) < 2 or parts[0] not in (vault.SPARK_DIRNAME, vault.REPORTS_DIRNAME):
        return False
    if any(not p or p.startswith(".") for p in parts):
        return False
    name = parts[-1]
    if name in _LIST_PAGES:
        return False
    return not (len(parts) >= 3 and name == parts[-2] + ".md")


def note_defs(text: str) -> dict[int, str]:
    return {int(m.group(1)): m.group(2).strip() for m in NOTE_DEF_RE.finditer(text)}


def reflection_range(text: str) -> tuple[int, int] | None:
    """「## 读后感」一节的范围：到下一个「## 」或第一条批注脚注为止。文章自己也可能有叫「读后感」的小标题：
    剪藏只认「阅读原文」后面的，都取最后一个（同手机 DocEdit.reflectionRange）。"""
    frm = text.rfind(READ_ORIGINAL)
    frm = frm + len(READ_ORIGINAL) if frm >= 0 else 0
    heads = list(REFLECTION_HEAD_RE.finditer(text, frm))
    if not heads:
        return None
    start = heads[-1]
    ends = [m.start() for m in (_NEXT_SECTION_RE.search(text, start.end()), NOTE_DEF_RE.search(text, start.end())) if m]
    return start.start(), min(ends) if ends else len(text)


def reflection(text: str) -> str:
    r = reflection_range(text)
    if not r:
        return ""
    lines = text[r[0]:r[1]].split("\n")[1:]
    return "\n".join(ln[1:] if ln.startswith(("\\#", "\\[^")) else ln for ln in lines).strip()


def without_reflection(text: str) -> str:
    """正文里不重复画读后感：阅读页在文末单画一张能改的卡。"""
    r = reflection_range(text)
    return text[:r[0]] + text[r[1]:] if r else text


def with_reflection(text: str, body: str) -> str:
    """写上（或改掉、清空）读后感。放在批注脚注前面，没有脚注就放在最后。"""
    out = text
    r = reflection_range(out)
    if r:
        head, tail = out[:r[0]].rstrip("\n"), out[r[1]:].lstrip("\n")
        out = head + "\n\n" + tail if tail else head + "\n"
    # 自己写的一行要是以 # 或 [^ 开头，会被当成下一节或批注脚注，前面加个反斜杠（Markdown 里照样显示原字）
    b = "\n".join("\\" + ln if ln.startswith(("#", "[^")) else ln for ln in body.strip().split("\n"))
    if not b:
        return out.strip("\n") + "\n"
    section = f"## 读后感\n\n{b}\n\n"
    d = NOTE_DEF_RE.search(out)
    if d:
        return out[:d.start()] + section + out[d.start():]
    return out.strip("\n") + "\n\n" + section.strip("\n") + "\n"


def has_notes(text: str) -> bool:
    return bool(MARK_RE.search(text) or NOTE_DEF_RE.search(text) or reflection_range(text))


# ---- 星标：文件头里的 starred: true（同手机 DocEdit.starring）。剪藏、小结、整理稿、会议资料、报告……
# 能做笔记的文件都能加。一期节目的星标加在它的小结上，没有小结的（简讯）加在整理稿上。
_FRONT_RE = re.compile(r"\A---\n(.*?)\n?---[ \t]*(?:\n|\Z)", re.S)
_STAR_LINE_RE = re.compile(r"^starred:[ \t]*true[ \t]*$", re.M)


def is_starred(text: str) -> bool:
    m = _FRONT_RE.match(text or "")
    return bool(m and _STAR_LINE_RE.search(m.group(1)))


def with_star(text: str, on: bool) -> str:
    """加上或去掉星标。没有文件头的（整理稿、文字记录、报告）加星标时补一段文件头；
    取消时文件头里只剩空的就整段去掉，文件回到原样。"""
    m = _FRONT_RE.match(text)
    if not m:
        return f"---\nstarred: true\n---\n\n{text.lstrip(chr(10))}" if on else text
    head = [ln for ln in m.group(1).split("\n") if ln.strip() and not ln.startswith("starred:")]
    if on:
        head.append("starred: true")
    rest = text[m.end():]
    if not head:
        return rest.lstrip("\n")
    return "---\n" + "\n".join(head) + "\n---\n" + rest


# ---- 稍后再读：文件头里的 later: <加入的时刻>（同星标，所有能加星标的文件都能加）。
# 写法：「later: 2026-11-24T10:00:00+00:00」（本机时区、到秒、带时差，doctime.iso）。认的时候只看这一行在不在、
# 值是不是空或 false，不看时钟：「later: true」（不带时刻的写法）也算，只是排在带时刻的后面。
# 位置、没有文件头时补一段、文件头只剩空时整段去掉，都同 with_star。已经在稍后再读里的再加一次，文件不变（时刻不刷新）。
_LATER_LINE_RE = re.compile(r"^later:[ \t]*(.*?)[ \t]*$", re.M)


def later_value(text: str) -> str:
    """文件头里 later: 后面的值（去掉两头的引号）；不在稍后再读里返回空串。"""
    m = _FRONT_RE.match(text or "")
    if not m:
        return ""
    for v in _LATER_LINE_RE.findall(m.group(1)):
        v = v.strip().strip('"').strip("'").strip()
        if v and v.lower() != "false":
            return v
    return ""


def is_later(text: str) -> bool:
    return bool(later_value(text))


def later_at(text: str):
    """加入稍后再读的时刻（带时区的 datetime）；值是 true 或认不出的返回 None。"""
    from core import doctime
    from datetime import datetime
    w = doctime.parse(later_value(text))
    return w if isinstance(w, datetime) else None


def with_later(text: str, on: bool, value: str = "") -> str:
    """加进或移出稍后再读。value：要写的时刻（ISO 带时差），空着用现在。"""
    if on and is_later(text):
        return text
    m = _FRONT_RE.match(text)
    if on and not value:
        from core import doctime
        value = doctime.iso(doctime.now())
    if not m:
        return f"---\nlater: {value}\n---\n\n{text.lstrip(chr(10))}" if on else text
    head = [ln for ln in m.group(1).split("\n") if ln.strip() and not ln.startswith("later:")]
    if on:
        head.append(f"later: {value}")
    rest = text[m.end():]
    if not head:
        return rest.lstrip("\n")
    return "---\n" + "\n".join(head) + "\n---\n" + rest


# ---- 占位剪藏：存链接时正文没抓到，文件头记 stub: true，「## 正文」里一句说明。补抓成功后去掉这一行。
_STUB_LINE_RE = re.compile(r"^stub:[ \t]*true[ \t]*$", re.M)


def is_stub(text: str) -> bool:
    m = _FRONT_RE.match(text or "")
    return bool(m and _STUB_LINE_RE.search(m.group(1)))


def _body(text: str) -> str:
    """找划线的范围：文件头不算；剪藏只看「## 正文」后面；读后感、脚注定义去掉。"""
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end >= 0:
            text = text[end + 4:]
    m = re.search(r"\n## 正文\n", text)
    if m:
        text = text[m.end():]
    return NOTE_DEF_RE.sub("", without_reflection(text))


def _body_start(text: str) -> int:
    """正文从哪儿开始（同 _body）：文件头后面；剪藏是「## 正文」后面。"""
    start = 0
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end >= 0:
            start = end + 4
    m = re.compile(r"\n## 正文\n").search(text, start)
    return m.end() if m else start


def _inside_mark(text: str, i: int) -> bool:
    """text[i] 是不是落在某处 ==划线== 里面（按 MARK_PATTERN 认，只看这一行）。"""
    lo = text.rfind("\n", 0, i) + 1
    hi = text.find("\n", i)
    line = text[lo:len(text) if hi < 0 else hi]
    return any(s <= i - lo < e for s, e, _, _ in mark_spans(line))


CARRIED_HEADING = "## 以前的划线"


def carry_over(old: str, new: str, locate=None, renamed: dict[str, str] | None = None) -> tuple[str, dict[str, str]]:
    """整篇重新生成时把旧文件里的笔记接到新内容上（见 _carry_notes），星标、稍后再读（连同加入的时刻）也留着。"""
    out, moved = _carry_notes(old, new, locate, renamed)
    if old and is_starred(old) and not is_starred(out):
        out = with_star(out, True)
    if old and is_later(old) and not is_later(out):
        out = with_later(out, True, later_value(old))
    return out, moved


def _carry_notes(old: str, new: str, locate=None, renamed: dict[str, str] | None = None) -> tuple[str, dict[str, str]]:
    """整篇重新生成（小结、专题、整理稿重跑）时，把旧文件里的笔记接到新内容上：
    - 划线：同样的字还在就原处划上，批注跟着挂上；locate(text, 字) 给了的话，措辞变了的划到最像的那句
      （返回 (起, 止) 或 None）；还找不到的放进文末「以前的划线」，照样是划线，批注不丢；
    - 读后感整节接回去；批注脚注都留着。
    renamed：调用方已经换成新措辞的划线 {旧字: 新字}。返回 (新内容, {旧字: 新字}（措辞变了的）)。"""
    if not old or not has_notes(old):
        return new, {}
    renamed = renamed or {}
    defs = note_defs(old)
    out, moved, missed = new, {}, []
    for mk in MARK_RE.finditer(_body(old)):
        text, refs = mk.group(1), mk.group(2)
        want = renamed.get(text, text)
        start = _body_start(out)
        # 新内容里已经划着（调用方先标过了）：只补上批注号
        j = out.find(f"=={want}==", start)
        if j >= 0:
            k = j + len(want) + 4
            if refs and not out.startswith("[^note-", k):
                out = out[:k] + refs + out[k:]
            continue
        i = out.find(want, start)
        while i >= 0 and _inside_mark(out, i):
            i = out.find(want, i + 1)
        if i >= 0:
            out = f"{out[:i]}=={want}=={refs}{out[i + len(want):]}"
            continue
        span = locate(out, text) if locate else None
        if span and span[0] >= start and "==" not in out[span[0]:span[1]] and not _inside_mark(out, span[0]):
            a, b = span
            moved[text] = out[a:b]
            out = f"{out[:a]}=={out[a:b]}=={refs}{out[b:]}"
            continue
        missed.append(f"- =={text}=={refs}")
    if missed:
        out = out.rstrip("\n") + f"\n\n{CARRIED_HEADING}\n\n重新生成后原文里找不到这几处了，批注还在。\n\n" + "\n".join(missed) + "\n"
    old_reflection = reflection(old)
    if old_reflection and not reflection(out):
        out = with_reflection(out, old_reflection)
    have = note_defs(out)
    extra = [f"[^note-{i}]: {t}" for i, t in sorted(defs.items()) if i not in have]
    if extra:
        out = out.rstrip("\n") + "\n\n" + "\n".join(extra) + "\n"
    return out, moved


def reading_note(text: str) -> dict:
    """一篇的笔记：{reflection, items: [{section, mark, notes}], loose: [没挂在划线上的批注]}。"""
    defs = note_defs(text)
    section, items, used = "", [], set()
    for line in _body(text).split("\n"):
        h = re.match(r"#{2,4} +(.+)$", line)
        if h:
            section = h.group(1).strip()
            continue
        if "==" not in line:
            continue
        for mk in MARK_RE.finditer(line):
            ids = [int(x) for x in re.findall(r"\[\^note-(\d+)\]", mk.group(2)) if int(x) in defs]
            used.update(ids)
            items.append({"section": section, "mark": mk.group(1), "notes": [defs[i] for i in ids]})
    loose = [defs[i] for i in sorted(defs) if i not in used and defs[i]]
    return {"reflection": reflection(text), "items": items, "loose": loose}


def is_empty(note: dict) -> bool:
    return not (note["items"] or note["loose"] or note["reflection"])


def plain_inline(s: str) -> str:
    return re.sub(r"\*\*|__|`|\[\^note-\d+\]", "", re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s))


def prompt_lines(text: str, max_marks: int = 12) -> str:
    """写复盘、起草时附在这篇后面的几行：读后感、划线、批注（同手机 ReadingNote.promptLines）。没有就是空串。"""
    n = reading_note(text)
    out = []
    if n["reflection"]:
        out.append(f"我的读后感：{n['reflection']}")
    if n["items"]:
        out.append("我的划线和批注：")
    for it in n["items"][:max_marks]:
        mark = plain_inline(it["mark"]).strip()
        out.append("- " + (mark[:220] + "…" if len(mark) > 220 else mark))
        out += [f"  批注：{t}" for t in it["notes"]]
    # 划线多、没列出来的那几处，挂着的批注照样给
    out += [f"批注：{t}" for it in n["items"][max_marks:] for t in it["notes"]]
    out += [f"批注：{t}" for t in n["loose"]]
    return "\n".join(out)


def note_markdown(title: str, source: str, url: str, n: dict) -> str:
    """整篇笔记的 Markdown：贴到别处用（同手机 ReadingNote.markdown）。"""
    out = [f"# {title}", ""]
    src = "，".join(x for x in (source, f"[原文]({url})" if url else "") if x)
    if src:
        out += [src, ""]
    if n["reflection"]:
        out += ["## 读后感", "", n["reflection"], ""]
    if n["items"]:
        out += ["## 划线和批注", ""]
        last = None
        for it in n["items"]:
            if it["section"] != last and it["section"]:
                out += [f"### {it['section']}", ""]
            last = it["section"]
            out += [f"> {plain_inline(it['mark'])}", ""]
            out += sum(([f"批注：{t}", ""] for t in it["notes"]), [])
    if n["loose"]:
        out += ["## 其他批注", ""] + [f"- {t}" for t in n["loose"]] + [""]
    return "\n".join(out).strip("\n") + "\n"


# ---- 全库有笔记的文件：按修改时间和大小记着，没变的不再读

_CACHE: dict[str, tuple[int, int, dict | None]] = {}
_CACHE_LOCK = threading.Lock()
_MARKERS = (b"==", b"[^note-", "## 读后感".encode())


def _note_of(full: str, st: os.stat_result) -> dict | None:
    with open(full, "rb") as f:
        data = f.read()
    if not any(m in data for m in _MARKERS):
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    n = reading_note(text)
    return None if is_empty(n) else n


def annotated_docs(root: str = "") -> list[tuple[str, float, dict]]:
    """笔记库里有划线、批注或读后感的文件：[(相对库根的路径, 修改时间, 笔记)]，最近改过的在前。"""
    root = os.path.realpath(root or vault.vault_root())
    out, seen = [], set()
    for top in (vault.SPARK_DIRNAME, vault.REPORTS_DIRNAME):
        base = os.path.join(root, top)
        for d, dirs, files in os.walk(base):
            dirs[:] = [x for x in dirs if not x.startswith(".")]
            for name in files:
                if not name.endswith(".md"):
                    continue
                full = os.path.join(d, name)
                rel = os.path.relpath(full, root).replace(os.sep, "/")
                if not annotatable(rel):
                    continue
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                seen.add(rel)
                with _CACHE_LOCK:
                    hit = _CACHE.get(rel)
                if hit and hit[0] == st.st_mtime_ns and hit[1] == st.st_size:
                    n = hit[2]
                else:
                    try:
                        n = _note_of(full, st)
                    except OSError:
                        continue
                    with _CACHE_LOCK:
                        _CACHE[rel] = (st.st_mtime_ns, st.st_size, n)
                if n:
                    out.append((rel, st.st_mtime, n))
    with _CACHE_LOCK:
        for gone in set(_CACHE) - seen:
            _CACHE.pop(gone, None)
    return sorted(out, key=lambda r: r[1], reverse=True)
