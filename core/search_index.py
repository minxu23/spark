"""笔记库全文索引：一个 SQLite 文件，FTS5 trigram 分词，按段落块建。

给谁用：
- 阅读页搜索（apps/reader/server.py 的 /read/api/search）调 search_docs：按文件归并的结果；
- 以后「基于知识库的问答」调 search_chunks：取出最相关的若干段落块交给模型。
  notes2insight/search.py 的主题检索（模型扩词 + BM25 + 模型筛选）也可以拿扩好的词调
  search_chunks(" ".join(词), mode="any") 当召回，这次没有接。

范围：跟原来扫文件的搜索一样，笔记库 Spark/ 和 output/ 下的 .md，跳过点开头的目录和文件
（apps/reader/server.py 的 _search_files）。

存储（SCHEMA_VERSION 变了就整库重建）：
- docs：一个文件一行。路径（相对笔记库根目录）、文件名、种类、标题、日期、修改时间（纳秒）、
  大小、一句话摘要、标签、文件头原文、稍后再读（文件头 later: 的值，即加入的时刻；不在稍后再读里是空串，
  认法同 core/annotations.later_value。单独一列是为了按它筛，不用每次去解析文件头原文）。
- chunks：正文切成的段落块。rowid = 文件 id << 16 | 块序号，所以从块直接得到文件，不用联表。
  带所在小节的标题路径、块在文件里的字符位置、块文字。
- chunks_fts / docs_fts：FTS5 trigram，无内容表（contentless_delete=1，文字只存在 chunks / docs
  里一份）。docs_fts 的列是 文件名、标题、摘要、标签、文件头，标题和摘要命中排得更前。
  进索引的文字末尾都补一个换行：两个字的词在结尾时也能凑成一个 trigram（见下）。

查询：空格分开的每个词都要出现（同一篇里，不必同一块）。
- 三个字及以上：trigram 短语匹配（"词"），子串语义，大小写不敏感。
- 两个字（「算力」「AI」）：trigram 查不了。到 fts5vocab 里取所有以这两个字开头的 trigram
  （「算力和」「算力的」「算力\\n」……），拼成 OR 查询，还是走索引、不读原文件。这种查询不算
  bm25（几百上千个 OR 分支逐行算分太慢），改按命中块数排。
- 一个字：以它开头的 trigram 少（生僻字）就同样展开；多（常用字）就对 chunks 表做 LIKE，
  找够一批就停。常用字很快凑够；只有「展开太多又命中很少」的字会扫完整张表。

更新：refresh() 遍历目录 stat 一遍，按（修改时间, 大小）找出新增、改过、删掉的文件，只重读这些。
改名按删加处理。同一时间只有一个刷新（锁）；读用 WAL，刷新时照样能搜。
文件坏了或版本不对：删掉重建，重建完成之前 search_* 抛 IndexUnavailable，调用方退回扫文件。

刷新时机（由调用方触发，没有常驻轮询）：
- spark.py 启动后 start_background_refresh() 在后台建或刷新一次；
- 电脑端保存 .md（core/atomic.write_text）调 note_changed(path)：下一次搜索前同步补进这几篇；
- 同步收进文件、删文件这类说不清改了哪些的，调 mark_dirty()：下一次搜索先用现有索引回答，同时在后台刷新；
- 距上次刷新超过 STALE_SECONDS（Obsidian 里改的、别的进程写的），下一次搜索同样触发后台刷新。
"""

from __future__ import annotations

import math
import os
import re
import sqlite3
import threading
import time
from typing import Iterable, Optional

SCHEMA_VERSION = 2                   # 2：docs 加 later 列（稍后再读）
ENV_PATH = "SPARK_SEARCH_INDEX"      # 索引文件放哪：环境变量 > ~/.spark/search-index.sqlite
DEFAULT_NAME = "search-index.sqlite"
SUBDIRS = ("Spark", "output")        # core.vault.SPARK_DIRNAME / REPORTS_DIRNAME

CHUNK_MIN = 300      # 块不到这么长时，遇到小标题也先不切
CHUNK_MAX = 800      # 块最长；一段超过这个按句子切开
CHUNK_BITS = 16      # 一篇最多 65536 块（800 字一块，约 5000 万字）
CHUNK_MASK = (1 << CHUNK_BITS) - 1

STALE_SECONDS = 60.0        # 距上次刷新超过这么久，下一次搜索触发后台刷新
BATCH_FILES = 200           # 刷新时每处理这么多文件提交一次，读的一方看得到进度、WAL 不会太大
EXPAND_MAX = 4000           # 一两个字的词最多展开成这么多个 trigram，再多当「太常见」
FREQ_MAX = 60000            # 估计命中超过这么多块的词当「太常见」：不全枚举，只核对候选（见 _plan）
LIKE_BUDGET = 0.5           # 全是太常见的词时，按新旧逐篇核对最多花这么久（秒）
FIELD_BONUS = 6.0           # 摘要、标签、文件头里都有这些词时加的分（bm25 越小越相关，这里是减）
PENDING_MAX = 2000          # 记下的「刚保存的文件」超过这么多就改成整库刷新

# 列权重：name, title, summary, tags, meta
_FIELD_WEIGHTS = (8.0, 10.0, 5.0, 4.0, 1.0)


class IndexUnavailable(Exception):
    """索引还没建好（首次建、重建中、文件坏了）。调用方退回原来的扫文件办法。"""


# ---------------------------------------------------------------- 路径和种类

def default_path() -> str:
    env = os.environ.get(ENV_PATH)
    if env:
        return os.path.expanduser(env)
    from core import settings   # ~/.spark：跟设置、cable-sha-cache.json 一处（core/cable_sync._spark_home）
    return os.path.join(os.path.dirname(settings.settings_path()), DEFAULT_NAME)


def default_vault() -> str:
    from core import vault
    return vault.vault_root()


# 种类按路径推，顺序和手机 ReaderPlace.doc 一样（apps/mobile-ios/Spark/ReaderPlace.swift）：
# 剪藏、论文（core/clips.clip_folder：剪藏式文件夹）、随手记（Spark/笔记/ 下直接的一篇，core/jottings.DIR）、
# 复盘（output/复盘/）、总览页（core/annotations.annotatable 为假：节目 / 会议文件夹的总览页、索引、摘录）。
# 再按单集子目录（apps/reader/server.py 的 _KIND_DIRS / _DRAFT_KINDS）：notes 单集小结、speech 整理稿、
# transcripts 文字记录、topics 专题。最后：output/ 报告；信息跟进/简报/ 简报（core/track_translate.BRIEF_DIRNAME）；
# 信息跟进/<订阅>/x.md 信息文章（core/track_translate.is_article_path）；剩下的是会议资料。
KINDS = ("剪藏", "论文", "随手记", "复盘", "总览页", "单集小结", "整理稿", "文字记录", "专题",
         "报告", "简报", "信息文章", "会议资料")
_EPISODE_DIRS = {"notes": "单集小结", "speech": "整理稿", "transcripts": "文字记录", "topics": "专题"}


def kind_of(rel: str) -> str:
    from core import annotations, clips, jottings, track_translate
    parts = rel.split("/")
    if clips.is_clip_path(rel):
        return clips.kind_name(clips.clip_folder(rel))   # 剪藏、论文
    if rel.startswith(jottings.DIR + "/") and len(parts) == 3:
        return "随手记"
    if rel.startswith("output/复盘/"):
        return "复盘"
    if not annotations.annotatable(rel):
        return "总览页"
    if len(parts) >= 3 and parts[-2] in _EPISODE_DIRS:
        return _EPISODE_DIRS[parts[-2]]
    if parts[0] == "output":
        return "报告"
    if rel.startswith(track_translate.TRACK_DIR + "/"):
        if len(parts) >= 3 and parts[2] == track_translate.BRIEF_DIRNAME:
            return "简报"
        if track_translate.is_article_path(rel):
            return "信息文章"
        return "简报"
    return "会议资料"


# ---------------------------------------------------------------- 解析一篇

_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.S)          # 同 apps/reader/server.py
_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|([^\]]+))?\]\]")
_DATE_PREFIX_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})[ _](.+)$")
_H1_RE = re.compile(r"^# (.+)$", re.M)
_INLINE_MD_RE = re.compile(r"\*\*|==|`")
_IMG_MD_RE = re.compile(r"!\[([^\]]*)\]\((?:<[^>]*>|[^)]*)\)")
_IMG_EMBED_RE = re.compile(r"!\[\[[^\]]*\]\]")
_IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.I)
_LINK_RE = re.compile(r"\[([^\]]*)\]\((?:<[^>]*>|[^)\s]*)(?:\s+\"[^\"]*\")?\)")
_FOOTREF_RE = re.compile(r"\[\^[^\]\s]+\](?!:)")
_FOOTDEF_RE = re.compile(r"^\[\^[^\]\s]+\]:\s*")
_MARKUP_RE = re.compile(r"\*\*|==|~~|`")
_QUOTE_RE = re.compile(r"^(?:[ \t]*>[ \t]?)+")
_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t#]*$")
_FENCE_RE = re.compile(r"^[ \t]*(```|~~~)")
_SENTENCE_RE = re.compile(r"(?<=[。！？；])|(?<=[.!?;])\s+")
_WS_RE = re.compile(r"\s+")
_SUMMARY_KEYS = ("tldr", "summary", "摘要", "一句话", "description")
_TAG_KEYS = ("tags", "话题", "标签", "keywords")
_DATE_KEYS = ("播出", "published", "date")                          # 同 server._file_info


def _frontmatter(text: str) -> tuple[str, dict[str, str], str]:
    """(文件头原文, {键: 纯文字值}, 正文)。解析规则同 server._split_frontmatter / _plain。"""
    import json
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return "", {}, text
    fields: dict[str, str] = {}
    for line in m.group(1).splitlines():
        key, sep, val = line.partition(":")
        if not sep or not key.strip():
            continue
        val = val.strip()
        v: object = val
        if val.startswith(("[", '"')):
            try:
                v = json.loads(val)
            except ValueError:
                pass
        fields[key.strip()] = _plain(v)
    return m.group(1), fields, text[m.end():]


def _plain(value: object) -> str:
    if isinstance(value, list):
        return "、".join(_plain(v) for v in value)
    return _WIKILINK_RE.sub(lambda m: (m.group(2) or m.group(1)).strip(), str(value)).strip()


def _clean_line(line: str) -> str:
    """正文一行去掉图片、链接地址和格式符号，留下读得到的字。"""
    line = _IMG_EMBED_RE.sub("", line)
    line = _IMG_TAG_RE.sub("", line)
    line = _IMG_MD_RE.sub(lambda m: m.group(1), line)
    line = _LINK_RE.sub(lambda m: m.group(1), line)
    line = _WIKILINK_RE.sub(lambda m: (m.group(2) or m.group(1)).strip(), line)
    line = _FOOTREF_RE.sub("", line)
    line = _FOOTDEF_RE.sub("", line)
    line = _QUOTE_RE.sub("", line)
    return _MARKUP_RE.sub("", line).strip()


def _split_long(text: str, limit: int, first: int = 0) -> list[str]:
    """一段太长：按句子切，再长的硬切（尽量在空白处）。first：第一片最多多长（接在上一块的零头后面时用）。"""
    out, cur = [], ""
    room = lambda: first if not out and first else limit   # noqa: E731
    for sent in (s for s in _SENTENCE_RE.split(text) if s):
        while len(cur) + len(sent) + 1 > room() and len(sent) > room() - len(cur) - 1:
            if cur and len(cur) >= room() // 2:
                out.append(cur)
                cur = ""
                continue
            n = room() - len(cur) - (1 if cur else 0)
            cut = sent.rfind(" ", n // 2, n)
            cut = cut if cut > 0 else n
            out.append(f"{cur} {sent[:cut].strip()}" if cur else sent[:cut].strip())
            cur, sent = "", sent[cut:].strip()
        if not sent:
            continue
        if cur and len(cur) + len(sent) + 1 > room():
            out.append(cur)
            cur = ""
        cur = f"{cur} {sent}" if cur else sent
    if cur:
        out.append(cur)
    return [p for p in out if p]


def chunk_body(body: str, offset: int = 0) -> list[dict]:
    """正文 → [{heading, pos, text}]。heading 是所在小节的标题路径（「一级 > 二级」），
    pos 是块第一行在文件里的字符位置（offset 是正文在文件里的起点，即文件头的长度）。

    规则：按空行分段；段落依次装进块，装不下（超过 CHUNK_MAX）就开新块；遇到小标题时块已经有
    CHUNK_MIN 字就从标题处开新块，不到就接着装（短小节合进下一块，块的标题路径记开头那节）；
    一段超过 CHUNK_MAX 按句子切。小标题自己的文字也进块。结尾不到 100 字的零头并进上一块。"""
    paras: list[tuple[str, int, str, bool]] = []   # (文字, 位置, 标题路径, 是不是标题)
    stack: list[tuple[int, str]] = []
    buf: list[str] = []
    buf_pos = 0
    in_fence = False
    pos = offset

    def path() -> str:
        return " > ".join(t for _, t in stack)

    def end_para() -> None:
        if buf:
            paras.append(("\n".join(buf), buf_pos, path(), False))
            buf.clear()

    for raw in body.split("\n"):
        line_pos = pos
        pos += len(raw) + 1
        if _FENCE_RE.match(raw):
            in_fence = not in_fence
            continue
        h = None if in_fence else _HEADING_RE.match(raw)
        if h:
            end_para()
            level, title = len(h.group(1)), _clean_line(h.group(2))
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            if title:
                paras.append((title, line_pos, path(), True))
            continue
        text = raw.strip() if in_fence else _clean_line(raw)
        if not text:
            end_para()
            continue
        if not buf:
            buf_pos = line_pos
        buf.append(text)
    end_para()

    chunks: list[dict] = []
    cur: list[str] = []
    cur_len = 0
    cur_head, cur_pos = "", 0

    def flush() -> None:
        nonlocal cur, cur_len
        if cur:
            chunks.append({"heading": cur_head, "pos": cur_pos, "text": "\n".join(cur)})
        cur, cur_len = [], 0

    for text, at, head, is_heading in paras:
        if is_heading and cur_len >= CHUNK_MIN:
            flush()
        if cur_len + len(text) + 1 > CHUNK_MAX and cur_len >= CHUNK_MIN:
            flush()
        if len(text) > CHUNK_MAX - cur_len - 1:
            # 装不下：块里已有的不到 CHUNK_MIN 时，先切一片补满这块，剩下的按 CHUNK_MAX 切
            pieces = _split_long(text, CHUNK_MAX, CHUNK_MAX - cur_len - 1 if cur else 0)
        else:
            pieces = [text]
        for piece in pieces:
            if cur and cur_len + len(piece) + 1 > CHUNK_MAX:
                flush()
            if not cur:
                found = text.find(piece[:20])
                cur_head, cur_pos = head, at + max(found, 0)   # 一段切开的：大致位置（段落开头加片内偏移）
            cur.append(piece)
            cur_len += len(piece) + 1
    flush()
    if len(chunks) >= 2 and len(chunks[-1]["text"]) < 100:
        last = chunks.pop()
        chunks[-1]["text"] += "\n" + last["text"]
    if len(chunks) > CHUNK_MASK + 1:   # 超长文件：多出来的并进最后一块
        tail = "\n".join(c["text"] for c in chunks[CHUNK_MASK:])
        chunks = chunks[:CHUNK_MASK] + [dict(chunks[CHUNK_MASK], text=tail)]
    return chunks


def _summary(fields: dict[str, str], body: str) -> str:
    for k in _SUMMARY_KEYS:
        if fields.get(k):
            return fields[k][:300]
    first_h2 = re.search(r"^## (.+)$", body, re.M)
    head = body[:first_h2.start()] if first_h2 else body[:4000]
    for line in head.split("\n"):
        if line.lstrip().startswith(">"):
            s = _clean_line(line)
            if s:
                return s[:300]
    if first_h2 and first_h2.group(1).strip() in ("小结", "摘要"):
        for line in body[first_h2.end():first_h2.end() + 4000].split("\n"):
            if line.startswith("#"):
                break
            s = _clean_line(line)
            if s and not s.startswith(("- ", "* ")):
                return s[:300]
    return ""


def parse_doc(rel: str, text: str) -> dict:
    """一篇 Markdown → 索引要的字段和段落块。标题、日期的取法同 server._file_info。"""
    name = rel.rsplit("/", 1)[-1]
    stem = name[:-3] if name.endswith(".md") else name
    m = _DATE_PREFIX_RE.match(stem)
    date, label = (f"{m.group(1)}-{m.group(2)}-{m.group(3)}", m.group(4)) if m else ("", stem)
    meta, fields, body = _frontmatter(text)
    h1 = _H1_RE.search(body[:16384])
    title = h1.group(1) if h1 else fields.get("title", "")
    title = _INLINE_MD_RE.sub("", _plain(title)).strip() or label.replace("_", " ")
    date = date or next((fields[k][:10] for k in _DATE_KEYS if fields.get(k)), "")
    tags = " ".join(fields[k] for k in _TAG_KEYS if fields.get(k))
    from core.annotations import later_value
    return {"name": name, "kind": kind_of(rel), "title": title, "date": date,
            "summary": _summary(fields, body), "tags": tags, "meta": meta, "later": later_value(text),
            "chunks": chunk_body(body, len(text) - len(body))}


# ---------------------------------------------------------------- 「刚保存的文件」登记（全进程一份）

_PENDING: set[str] = set()
_PENDING_LOCK = threading.Lock()
_DIRTY = [0]   # 每 mark_dirty 一次加一；各索引记着自己刷新时看到的值


def note_changed(path: str) -> None:
    """电脑端刚写了这个文件（core/atomic.write_text 调）。只记一笔，下一次搜索前补进索引。"""
    if not str(path).endswith(".md"):
        return
    with _PENDING_LOCK:
        if len(_PENDING) >= PENDING_MAX:
            _PENDING.clear()
            _DIRTY[0] += 1
            return
        _PENDING.add(os.path.abspath(path))


def mark_dirty() -> None:
    """笔记库有说不清范围的改动（同步收进文件、删文件）：下一次搜索时在后台整库刷新。"""
    with _PENDING_LOCK:
        _DIRTY[0] += 1


def _take_pending(prefixes: tuple[str, ...]) -> list[str]:
    with _PENDING_LOCK:
        mine = [p for p in _PENDING if p.startswith(prefixes)]
        _PENDING.difference_update(mine)
    return mine


# ---------------------------------------------------------------- 索引

_SCHEMA = f"""
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE docs(
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    name TEXT, kind TEXT, title TEXT, date TEXT,
    mtime_ns INTEGER, size INTEGER,
    summary TEXT, tags TEXT, meta TEXT, n_chunks INTEGER, later TEXT NOT NULL DEFAULT '');
CREATE TABLE chunks(id INTEGER PRIMARY KEY, heading TEXT, pos INTEGER, text TEXT);
CREATE VIRTUAL TABLE chunks_fts USING fts5(text, content='', contentless_delete=1, tokenize='trigram',
    detail=none);
CREATE VIRTUAL TABLE docs_fts USING fts5(name, title, summary, tags, meta,
    content='', contentless_delete=1, tokenize='trigram');
CREATE VIRTUAL TABLE chunks_vocab USING fts5vocab(chunks_fts, 'row');
CREATE VIRTUAL TABLE docs_vocab USING fts5vocab(docs_fts, 'row');
"""

_TABLES = ("meta", "docs", "chunks", "chunks_fts", "docs_fts", "chunks_vocab", "docs_vocab")
_CORRUPT_HINTS = ("malformed", "not a database", "corrupt", "file is encrypted", "disk image")


def _is_corrupt(err: Exception) -> bool:
    return isinstance(err, sqlite3.DatabaseError) and any(h in str(err).lower() for h in _CORRUPT_HINTS)


def _quote(term: str) -> str:
    return '"' + term.replace('"', '""') + '"'


def _like_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _needs_py_case(term: str) -> bool:
    """词里有区分大小写的非 ASCII 字母（É、Ü……）：SQLite 的 LIKE 只对 ASCII 不分大小写，改用 Python 核对。"""
    return any(ord(ch) > 127 and ch.lower() != ch.upper() for ch in term)


def _verify_sql(plans: list[dict], joiner: str, *, alias: str = "c",
                columns: Iterable[str] = ("text",)) -> tuple[str, list]:
    """在文字里核对每个词（不分大小写，同原来的 needle in text.lower()）。"""
    parts, args = [], []
    cols = list(columns)
    for i, p in enumerate(plans):
        col = f"{alias}.{cols[i % len(cols)]}"
        if _needs_py_case(p["term"]):
            parts.append(f"ci_has({col}, ?)")
            args.append(p["term"].lower())
        else:
            parts.append(f"{col} LIKE ? ESCAPE '\\'")
            args.append("%" + _like_escape(p["term"]) + "%")
    return joiner.join(parts) or "1", args


def _ci_has(text: Optional[str], needle: str) -> bool:
    return needle in (text or "").lower()


def walk_markdown(vault: str, subdirs: Iterable[str] = SUBDIRS) -> Iterable[tuple[str, str, os.stat_result]]:
    """(相对路径, 绝对路径, stat)。跳过规则同 server._search_files：点开头的目录和文件不要，只要 .md。
    不跟符号链接进目录（os.walk 默认）。"""
    for sub in subdirs:
        top = os.path.join(vault, sub)
        stack = [(top, sub)]
        while stack:
            folder, rel = stack.pop()
            try:
                it = os.scandir(folder)
            except OSError:
                continue
            with it:
                for e in it:
                    if e.name.startswith("."):
                        continue
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append((e.path, f"{rel}/{e.name}"))
                        elif e.name.endswith(".md"):
                            yield f"{rel}/{e.name}", e.path, e.stat()
                    except OSError:
                        continue


class SearchIndex:
    """一个笔记库的一份索引。db_path 和 vault 在创建时定下，后台线程只用这两个，不再读环境变量。"""

    def __init__(self, db_path: str, vault: str, subdirs: Iterable[str] = SUBDIRS):
        self.db_path = os.path.abspath(db_path)
        self.vault = os.path.abspath(vault)
        self.subdirs = tuple(subdirs)
        self._prefixes = tuple(os.path.join(self.vault, s) + os.sep for s in self.subdirs)
        self._roots_key = "\n".join([self.vault, *self.subdirs])
        self._lock = threading.Lock()          # 同一时间只有一个写的（刷新或补几篇）
        self._thread: Optional[threading.Thread] = None
        self._writer: Optional[sqlite3.Connection] = None
        self._seen_dirty = _DIRTY[0]
        self.last_refresh = 0.0                # time.monotonic()，0 表示这个进程里还没刷新过
        self.last_stats: dict = {}
        self._docs_cache: tuple[str, dict] = ("", {})
        self._by_date: tuple[Optional[dict], list[int]] = (None, [])
        self._cache_lock = threading.Lock()
        self.before_file = None                # 测试用：处理每个文件前调一下（可以在这里卡住）

    # ---- 连接

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10, check_same_thread=False)
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _reader(self) -> sqlite3.Connection:
        if not os.path.exists(self.db_path):
            raise IndexUnavailable("索引还没建")
        try:
            conn = self._connect()
            conn.execute("PRAGMA query_only=1")
            conn.create_function("ci_has", 2, _ci_has, deterministic=True)
            rows = dict(conn.execute("SELECT key, value FROM meta WHERE key IN ('version', 'roots', 'state')"))
        except sqlite3.Error as e:
            if _is_corrupt(e):
                self.mark_broken()
            raise IndexUnavailable(f"索引读不了：{e}") from e
        if rows.get("version") != str(SCHEMA_VERSION) or rows.get("roots") != self._roots_key or rows.get("state") != "ready":
            conn.close()
            raise IndexUnavailable("索引还没建好")
        return conn

    def _open_writer(self) -> sqlite3.Connection:
        """拿写连接；文件坏了、版本或库位置不对就删掉重建。调用时已持有 self._lock。"""
        if self._writer is not None:
            return self._writer
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        for attempt in range(2):
            try:
                conn = self._connect()
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table')")}
                meta = dict(conn.execute("SELECT key, value FROM meta")) if "meta" in have else {}
                if meta.get("version") != str(SCHEMA_VERSION) or meta.get("roots") != self._roots_key \
                        or not set(_TABLES) <= have:
                    self._reset(conn, have)
                self._writer = conn
                return conn
            except sqlite3.DatabaseError as e:
                try:
                    conn.close()
                except Exception:
                    pass
                if attempt or not _is_corrupt(e):
                    raise
                self._remove_files()
        raise AssertionError("unreachable")

    def _reset(self, conn: sqlite3.Connection, have: set[str]) -> None:
        """整库清空、按当前结构重建（版本变了、库位置变了）。state=building：建完之前不给搜。"""
        for name in ("chunks_vocab", "docs_vocab", "chunks_fts", "docs_fts", "chunks", "docs", "meta"):
            if name in have or name in _TABLES:
                conn.execute(f"DROP TABLE IF EXISTS {name}")
        conn.executescript(_SCHEMA)
        conn.executemany("INSERT INTO meta VALUES (?, ?)", [
            ("version", str(SCHEMA_VERSION)), ("roots", self._roots_key), ("state", "building"), ("generation", "0")])
        conn.commit()

    def _remove_files(self) -> None:
        """索引文件坏了：删掉（程序自己的缓存，不是用户文件），连同 WAL 和共享内存文件。"""
        for suffix in ("", "-wal", "-shm", "-journal"):
            try:
                os.remove(self.db_path + suffix)
            except FileNotFoundError:
                pass

    def mark_broken(self) -> None:
        """读的时候发现文件坏了：后台关掉写连接、删文件、重建。几个搜索同时发现也只重建一次；
        轮到它时再看一眼，已经被别人重建好了就不删。"""
        def rebuild() -> None:
            with self._lock:
                if self._writer is not None:
                    try:
                        self._writer.close()
                    except Exception:
                        pass
                    self._writer = None
                try:
                    probe = self._connect()
                    try:
                        probe.execute("SELECT count(*) FROM meta").fetchone()
                        probe.execute("SELECT count(*) FROM docs").fetchone()
                        probe.execute("SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH '\"abc\"' LIMIT 1").fetchall()
                        healthy = True
                    finally:
                        probe.close()
                except sqlite3.Error:
                    healthy = False
                if not healthy:
                    self._remove_files()
            self.refresh()
        with self._cache_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._quiet(rebuild), daemon=True, name="search-index-rebuild")
            self._thread.start()

    def _quiet(self, fn):
        def run() -> None:
            try:
                fn()
            except Exception as e:   # 后台线程：失败了下次再来，搜索退回扫文件
                self.last_stats = {"error": str(e)}
        return run

    def close(self) -> None:
        with self._lock:
            if self._writer is not None:
                self._writer.close()
                self._writer = None

    # ---- 写

    def _bump(self, conn: sqlite3.Connection) -> None:
        conn.execute("UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key = 'generation'")

    def _drop_doc(self, conn: sqlite3.Connection, doc_id: int, keep_row: bool = False) -> None:
        lo, hi = doc_id << CHUNK_BITS, (doc_id << CHUNK_BITS) | CHUNK_MASK
        conn.execute("DELETE FROM chunks_fts WHERE rowid BETWEEN ? AND ?", (lo, hi))
        conn.execute("DELETE FROM chunks WHERE id BETWEEN ? AND ?", (lo, hi))
        conn.execute("DELETE FROM docs_fts WHERE rowid = ?", (doc_id,))
        if not keep_row:
            conn.execute("DELETE FROM docs WHERE id = ?", (doc_id,))

    def _put_doc(self, conn: sqlite3.Connection, rel: str, full: str, st: os.stat_result,
                 doc_id: Optional[int]) -> None:
        if self.before_file:
            self.before_file(rel)
        try:
            with open(full, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            if doc_id is not None:
                self._drop_doc(conn, doc_id)
            return
        d = parse_doc(rel, text)
        row = (rel, d["name"], d["kind"], d["title"], d["date"], st.st_mtime_ns, st.st_size,
               d["summary"], d["tags"], d["meta"], len(d["chunks"]), d["later"])
        if doc_id is None:
            doc_id = conn.execute(
                "INSERT INTO docs(path, name, kind, title, date, mtime_ns, size, summary, tags, meta, n_chunks, later)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", row).lastrowid
        else:
            self._drop_doc(conn, doc_id, keep_row=True)
            conn.execute("UPDATE docs SET path=?, name=?, kind=?, title=?, date=?, mtime_ns=?, size=?, summary=?,"
                         " tags=?, meta=?, n_chunks=?, later=? WHERE id=?", (*row, doc_id))
        base = doc_id << CHUNK_BITS
        rows = [(base | i, c["heading"], c["pos"], c["text"]) for i, c in enumerate(d["chunks"])]
        conn.executemany("INSERT INTO chunks(id, heading, pos, text) VALUES (?,?,?,?)", rows)
        # 末尾补换行：两个字的词在块尾也能凑出一个以它开头的 trigram
        conn.executemany("INSERT INTO chunks_fts(rowid, text) VALUES (?, ?)", [(r[0], r[3] + "\n") for r in rows])
        conn.execute("INSERT INTO docs_fts(rowid, name, title, summary, tags, meta) VALUES (?,?,?,?,?,?)",
                     (doc_id, d["name"] + "\n", d["title"] + "\n", d["summary"] + "\n", d["tags"] + "\n",
                      d["meta"] + "\n"))

    def refresh(self) -> dict:
        """整库对一遍（阻塞）。已经有一个在跑就等它跑完再跑（保证调用之后的状态是新的）。
        返回 {scanned, added, changed, removed, seconds, rebuilt}。"""
        with self._lock:
            t0 = time.monotonic()
            dirty_at = _DIRTY[0]
            try:
                stats = self._refresh_locked()
            except sqlite3.DatabaseError as e:
                if not _is_corrupt(e):
                    raise
                if self._writer is not None:
                    self._writer.close()
                    self._writer = None
                self._remove_files()
                stats = self._refresh_locked()
            # 刷新期间又有人存了文件：顺手补上
            paths = _take_pending(self._prefixes)
            if paths:
                self._index_paths_locked(paths)
            stats["seconds"] = round(time.monotonic() - t0, 3)
            self._seen_dirty = dirty_at
            self.last_refresh = time.monotonic()
            self.last_stats = stats
            return stats

    def _refresh_locked(self) -> dict:
        conn = self._open_writer()
        state = conn.execute("SELECT value FROM meta WHERE key='state'").fetchone()[0]
        known = {p: (i, m, s) for i, p, m, s in conn.execute("SELECT id, path, mtime_ns, size FROM docs")}
        seen = set()
        todo: list[tuple[str, str, os.stat_result, Optional[int]]] = []
        for rel, full, st in walk_markdown(self.vault, self.subdirs):
            seen.add(rel)
            k = known.get(rel)
            if k is None:
                todo.append((rel, full, st, None))
            elif (k[1], k[2]) != (st.st_mtime_ns, st.st_size):
                todo.append((rel, full, st, k[0]))
        gone = [k[0] for p, k in known.items() if p not in seen]
        for doc_id in gone:
            self._drop_doc(conn, doc_id)
        for n, (rel, full, st, doc_id) in enumerate(todo, 1):
            self._put_doc(conn, rel, full, st, doc_id)
            if n % BATCH_FILES == 0:
                self._bump(conn)
                conn.commit()
        if todo or gone or state != "ready":
            self._bump(conn)
            conn.execute("UPDATE meta SET value='ready' WHERE key='state'")
        conn.commit()
        if state != "ready":
            # 首次建完：合并 FTS 段，之后的查询读的段少
            conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('optimize')")
            conn.execute("INSERT INTO docs_fts(docs_fts) VALUES('optimize')")
            conn.commit()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        added = sum(1 for t in todo if t[3] is None)
        return {"scanned": len(seen), "added": added, "changed": len(todo) - added, "removed": len(gone),
                "rebuilt": state != "ready"}

    def index_paths(self, paths: Iterable[str]) -> int:
        with self._lock:
            return self._index_paths_locked(list(paths))

    def _index_paths_locked(self, paths: list[str]) -> int:
        """只补这几篇（刚保存的）：在就重读，不在了就去掉。索引还没建好时不做（整库刷新会带上）。"""
        conn = self._open_writer()
        if conn.execute("SELECT value FROM meta WHERE key='state'").fetchone()[0] != "ready":
            return 0
        n = 0
        for full in paths:
            full = os.path.abspath(full)
            rel = os.path.relpath(full, self.vault).replace(os.sep, "/")
            parts = rel.split("/")
            if parts[0] not in self.subdirs or any(p.startswith(".") for p in parts) or not rel.endswith(".md"):
                continue
            row = conn.execute("SELECT id, mtime_ns, size FROM docs WHERE path=?", (rel,)).fetchone()
            try:
                st = os.stat(full)
            except OSError:
                st = None
            if st is None:
                if row:
                    self._drop_doc(conn, row[0])
                    n += 1
                continue
            if row and (row[1], row[2]) == (st.st_mtime_ns, st.st_size):
                continue
            self._put_doc(conn, rel, full, st, row[0] if row else None)
            n += 1
        if n:
            self._bump(conn)
        conn.commit()
        return n

    def refresh_async(self) -> bool:
        """后台刷新一次；已经有一个在跑就不再起。返回是否起了新的。"""
        with self._cache_lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._thread = threading.Thread(target=self._quiet(self.refresh), daemon=True, name="search-index-refresh")
            self._thread.start()
            return True

    def wait(self, timeout: float = 60.0) -> None:
        t = self._thread
        if t is not None:
            t.join(timeout)

    def maybe_refresh(self) -> None:
        """搜索前调：刚保存的几篇同步补进（很快）；有说不清的改动或者太久没刷新，起后台刷新。"""
        paths = _take_pending(self._prefixes)
        if paths:
            if self._lock.acquire(blocking=False):
                try:
                    self._index_paths_locked(paths)
                except sqlite3.Error:
                    pass
                finally:
                    self._lock.release()
            else:   # 正在整库刷新：放回去，刷新结束时会补
                with _PENDING_LOCK:
                    _PENDING.update(paths)
        if self._seen_dirty != _DIRTY[0] or not self.last_refresh \
                or time.monotonic() - self.last_refresh > STALE_SECONDS:
            self.refresh_async()

    # ---- 读

    def ready(self) -> bool:
        try:
            self._reader().close()
            return True
        except IndexUnavailable:
            return False

    def _docs_meta(self, conn: sqlite3.Connection) -> dict[int, tuple]:
        """{文件 id: (path, kind, title, 排序用日期, mtime_ns)}，按 generation 缓存在内存里（5 万篇约十几 MB）。"""
        gen = conn.execute("SELECT value FROM meta WHERE key='generation'").fetchone()[0]
        with self._cache_lock:
            if self._docs_cache[0] == gen:
                return self._docs_cache[1]
        docs = {}
        for i, path, kind, title, date, mtime in conn.execute("SELECT id, path, kind, title, date, mtime_ns FROM docs"):
            day = date or time.strftime("%Y-%m-%d", time.localtime((mtime or 0) / 1e9))
            docs[i] = (path, kind, title, day, mtime or 0)
        with self._cache_lock:
            self._docs_cache = (gen, docs)
        return docs

    def _expand(self, conn: sqlite3.Connection, vocab: str, term: str) -> Optional[list[tuple[str, int]]]:
        """一两个字的词 → 以它开头的所有 trigram 和各自出现在几行；太多（常用单字）返回 None。"""
        t = term.lower()
        out = list(conn.execute(f"SELECT term, doc FROM {vocab} WHERE term >= ? AND term < ? LIMIT ?",
                                (t, t + "\U0010ffff", EXPAND_MAX + 1)))
        return None if len(out) > EXPAND_MAX else out

    def _gram_rows(self, conn: sqlite3.Connection, gram: str) -> int:
        r = conn.execute("SELECT doc FROM chunks_vocab WHERE term = ?", (gram,)).fetchone()
        return r[0] if r else 0

    def _plan(self, conn: sqlite3.Connection, terms: list[str]) -> list[dict]:
        """每个词怎么查：
        body   "phrase"（三字以上：它的 trigram 都要在块里）、"grams"（一两个字：以它开头的 trigram 任一个在块里）、
               "like"（太常见：展开超过 EXPAND_MAX 个 trigram，或者估计命中超过 FREQ_MAX 块。不枚举全部命中，
               只在候选块里、或按新旧逐篇用 LIKE 核对）、"none"（正文里肯定没有）。
        grams  phrase 时是去重的 trigram，grams 时是展开出来的 trigram。
        verify 命中块还要在块文字里核对整个词：正文索引只存了 trigram 在哪块（detail=none），没存位置，
               四个字以上可能各段都在、却不连着。正好三个字就是一个 trigram，不用核对。
        fields 文件名 / 标题 / 摘要 / 标签 / 文件头里的查法：("phrase", 词) / ("grams", 展开) / ("like", 词) / ("none",)。
        ranked 有没有 bm25（两个字展开出来的几百上千个 OR 分支逐行算分太慢，不算）。"""
        plans = []
        for t in terms:
            low = t.lower()
            if len(t) >= 3:
                grams = list(dict.fromkeys(low[i:i + 3] for i in range(len(low) - 2)))
                est = min(self._gram_rows(conn, g) for g in grams)   # 命中块数的上限
                body = "none" if not est else ("like" if est > FREQ_MAX else "phrase")
                plans.append({"term": t, "body": body, "grams": grams, "verify": len(t) > 3,
                              "fields": ("phrase", t), "ranked": body == "phrase", "est": est})
                continue
            exp = self._expand(conn, "chunks_vocab", t)
            fields = self._expand(conn, "docs_vocab", t)
            est = sum(n for _, n in exp) if exp is not None else FREQ_MAX + 1
            body = "like" if exp is None or est > FREQ_MAX else ("grams" if exp else "none")
            plans.append({"term": t, "body": body, "grams": [g for g, _ in exp or ()], "verify": False,
                          "ranked": False, "est": est,
                          "fields": ("like", t) if fields is None else
                          (("grams", [g for g, _ in fields]) if fields else ("none",))})
        return plans

    def _or_rowids(self, conn: sqlite3.Connection, table: str, grams: list[str], cols: str = "") -> set[int]:
        """任一个 trigram 出现的行。分组查再在 Python 里并起来：FTS5 的 OR 每前进一行要看遍所有分支，
        上千个分支放进一个查询比每 32 个一组慢十几倍（5000 篇合成库上「ai」800 ms 对 63 ms）。"""
        out: set[int] = set()
        for i in range(0, len(grams), 32):
            expr = " OR ".join(_quote(g) for g in grams[i:i + 32])
            if cols:
                expr = f"{{{cols}}}: ({expr})"
            out.update(r[0] for r in conn.execute(f"SELECT rowid FROM {table} WHERE {table} MATCH ?", (expr,)))
        return out

    def _field_docs(self, conn: sqlite3.Connection, plan: dict, cols: str = "") -> set[int]:
        how = plan["fields"]
        if how[0] == "none":
            return set()
        if how[0] == "grams":
            return self._or_rowids(conn, "docs_fts", how[1], cols)
        if how[0] == "like":
            names = ("name", "title") if cols else ("name", "title", "summary", "tags", "meta")
            cond, args = _verify_sql([plan] * len(names), " OR ", columns=names)
            return {r[0] for r in conn.execute(f"SELECT id FROM docs c WHERE {cond}", args)}
        expr = f"{{{cols}}}: {_quote(how[1])}" if cols else _quote(how[1])
        return {r[0] for r in conn.execute("SELECT rowid FROM docs_fts WHERE docs_fts MATCH ?", (expr,))}

    def _term_hits(self, conn: sqlite3.Connection, plan: dict) -> Optional[dict[int, float]]:
        """这个词命中的块：{块 rowid: bm25（没有算分的是 0）}。走不了索引（常用单字）返回 None。"""
        if plan["body"] == "none":
            return {}
        if plan["body"] == "like":
            return None
        if plan["body"] == "grams":
            return dict.fromkeys(self._or_rowids(conn, "chunks_fts", plan["grams"]), 0.0)
        expr = " AND ".join(_quote(g) for g in plan["grams"])
        if not plan["verify"]:
            return dict(conn.execute("SELECT rowid, bm25(chunks_fts) FROM chunks_fts WHERE chunks_fts MATCH ?", (expr,)))
        cond, args = _verify_sql([plan], " AND ")
        return dict(conn.execute(
            "WITH h AS MATERIALIZED (SELECT rowid AS r, bm25(chunks_fts) AS s FROM chunks_fts WHERE chunks_fts MATCH ?) "
            f"SELECT h.r, h.s FROM h JOIN chunks c ON c.id = h.r WHERE {cond}", [expr] + args))

    def _verify_ids(self, conn: sqlite3.Connection, ids: Iterable[int], plans: list[dict]) -> set[int]:
        """这些块里，文字含有 plans 里每个词的。"""
        ids = list(ids)
        cond, args = _verify_sql(plans, " AND ", alias="chunks")
        out: set[int] = set()
        for i in range(0, len(ids), 500):
            part = ids[i:i + 500]
            out.update(r[0] for r in conn.execute(
                f"SELECT id FROM chunks WHERE id IN ({','.join('?' * len(part))}) AND {cond}", part + args))
        return out

    def _like_scan(self, conn: sqlite3.Connection, plans: list[dict], want: int, joiner: str = " AND ") -> list[int]:
        """全是太常见的词：按新旧逐篇在块文字里 LIKE，找够 want 篇就停，返回每篇第一处命中的块 rowid。
        词常见，所以通常看几十篇就够；最多花 LIKE_BUDGET 秒（几个常见词很少同块出现时可能不全）。"""
        cond, args = _verify_sql(plans, joiner, alias="chunks")
        sql = f"SELECT id FROM chunks WHERE id BETWEEN ? AND ? AND {cond} ORDER BY id LIMIT 1"
        out = []
        deadline = time.monotonic() + LIKE_BUDGET
        for d in self._docs_by_date(conn):
            r = conn.execute(sql, [d << CHUNK_BITS, (d << CHUNK_BITS) | CHUNK_MASK] + args).fetchone()
            if r:
                out.append(r[0])
                if len(out) >= want:
                    break
            if time.monotonic() > deadline:
                break
        return out

    def _docs_by_date(self, conn: sqlite3.Connection) -> list[int]:
        docs = self._docs_meta(conn)
        with self._cache_lock:
            if self._by_date[0] is docs:
                return self._by_date[1]
        order = sorted(docs, key=lambda d: (_neg(docs[d][3]), -docs[d][4]))
        with self._cache_lock:
            self._by_date = (docs, order)
        return order

    def _chunk_and(self, conn: sqlite3.Connection, plans: list[dict], hits: list[Optional[dict]],
                   want: int) -> dict[int, float]:
        """每个词都在里面的块：{块 rowid: 各词 bm25 之和}。want：至少要凑出几篇（太常见的词不全枚举时用）。"""
        idx = sorted((h for h in hits if h is not None), key=len)
        likes = [p for p, h in zip(plans, hits) if h is None]
        if not idx:
            return dict.fromkeys(self._like_scan(conn, likes, want), 0.0)
        base = set(idx[0])
        for h in idx[1:]:
            base &= h.keys()
            if not base:
                return {}
        scores = {r: sum(h[r] for h in idx) for r in base}
        if likes:
            # 太常见的词只在候选块里核对：按分数从好到差（没有分数的按新旧）分批核对，凑够 want 篇就停
            docs = self._docs_meta(conn)
            order = sorted(scores, key=lambda r: (scores[r], _neg(docs.get(r >> CHUNK_BITS, ("", "", "", "", 0))[3])))
            kept: dict[int, float] = {}
            seen: set[int] = set()
            for i in range(0, len(order), 2000):
                for r in self._verify_ids(conn, order[i:i + 2000], likes):
                    kept[r] = scores[r]
                    seen.add(r >> CHUNK_BITS)
                if len(seen) >= want:
                    break
            scores = kept
        return scores

    def later_docs(self) -> list[dict]:
        """在稍后再读里的文件：[{path, abs, title, kind, date, later}]，按 later 的值（加入的时刻）从新到旧；
        值不是时刻的（later: true）排最后，按修改时间从新到旧。"""
        self.maybe_refresh()
        conn = self._reader()
        try:
            rows = conn.execute("SELECT path, kind, title, date, later, mtime_ns FROM docs WHERE later != ''").fetchall()
        except sqlite3.Error as e:
            if _is_corrupt(e):
                self.mark_broken()
            raise IndexUnavailable(f"索引查询出错：{e}") from e
        finally:
            conn.close()
        from core import doctime
        from datetime import datetime

        def key(r):
            w = doctime.parse(r[4])
            return (1, w.timestamp(), r[5]) if isinstance(w, datetime) else (0, 0.0, r[5])
        return [{"path": p, "abs": os.path.join(self.vault, *p.split("/")), "title": t, "kind": k, "date": d, "later": v}
                for p, k, t, d, v, _m in sorted(rows, key=key, reverse=True)]

    def search_docs(self, query: str, limit: int = 60, kinds: Optional[Iterable[str]] = None) -> list[dict]:
        """按文件归并：[{path, abs, title, kind, date, snippet, hits, score, tier}]。
        排序：文件名或标题里有全部词的在前（tier 0）；再按相关度（最好那块的 bm25，命中块多的再加一点，
        摘要 / 标签 / 文件头也有这些词再加分）；同分新的在前。词分散在不同块、或一部分只在标题里的排最后
        （tier 2）。hits 是 snippet 里每处命中的 [起, 止)。"""
        terms = parse_query(query)
        if not terms or limit <= 0:
            return []
        self.maybe_refresh()
        conn = self._reader()
        try:
            return self._search_docs(conn, terms, limit, set(kinds) if kinds else None)
        except sqlite3.Error as e:
            if _is_corrupt(e):
                self.mark_broken()
            raise IndexUnavailable(f"索引查询出错：{e}") from e
        finally:
            conn.close()

    def _search_docs(self, conn, terms, limit, kinds) -> list[dict]:
        docs = self._docs_meta(conn)
        plans = self._plan(conn, terms)
        title_sets = [self._field_docs(conn, p, "name title") for p in plans]
        field_sets = [self._field_docs(conn, p) for p in plans]
        titled = set.intersection(*title_sets)
        fielded = set.intersection(*field_sets)
        hits = [self._term_hits(conn, p) for p in plans]
        ranked = any(p["ranked"] for p in plans)
        body: dict[int, list] = {}   # 文件 id → [最好那块的分数, 那块 rowid, 命中块数]
        for r, s in self._chunk_and(conn, plans, hits, limit).items():
            b = body.get(r >> CHUNK_BITS)
            if b is None:
                body[r >> CHUNK_BITS] = [s, r, 1]
            else:
                if (s, r) < (b[0], b[1]):
                    b[0], b[1] = s, r
                b[2] += 1
        ok = (lambda d: d in docs and docs[d][1] in kinds) if kinds else (lambda d: d in docs)
        cands: dict[int, list] = {}
        for d in titled | fielded | body.keys():
            if not ok(d):
                continue
            s, cid = 0.0, None
            if d in body:
                best, cid, n = body[d]
                s = best - 0.5 * math.log(n) if ranked else -1.0 - math.log(n)
            if d in fielded:
                s -= FIELD_BONUS
            cands[d] = [0 if d in titled else 1, s, cid]
        if len(cands) < limit and len(terms) > 1:
            # 词分散在不同块、或者一部分只在标题 / 文件头里：每个词各自出现在这篇里就算，排最后
            spread: Optional[set[int]] = None
            for p, h, fs in sorted(zip(plans, hits, field_sets), key=lambda x: x[1] is None):
                if h is None:   # 常用单字：在剩下的候选里逐篇核对
                    s = fs | {d for d in (spread or ()) if self._doc_has(conn, d, p)} if spread is not None else fs
                else:
                    s = fs | {r >> CHUNK_BITS for r in h}
                spread = s if spread is None else spread & s
                if not spread:
                    break
            for d in spread or ():
                if d not in cands and ok(d):
                    cands[d] = [2, 0.0, None]
        order = sorted(cands, key=lambda d: (cands[d][0], round(cands[d][1], 6), _neg(docs[d][3]), -docs[d][4]))
        out = []
        for d in order[:limit]:
            tier, score, cid = cands[d]
            if cid is None:
                cid = self._first_chunk(conn, d, plans, hits)
            text = ""
            if cid is not None:
                r = conn.execute("SELECT text FROM chunks WHERE id = ?", (cid,)).fetchone()
                text = r[0] if r else ""
            snippet, marks = make_snippet(text, terms)
            path, kind, title, day, _m = docs[d]
            out.append({"path": path, "abs": os.path.join(self.vault, *path.split("/")), "title": title,
                        "kind": kind, "date": day, "snippet": snippet, "hits": marks,
                        "score": round(score, 4), "tier": tier})
        return out

    def _doc_has(self, conn: sqlite3.Connection, doc_id: int, plan: dict) -> bool:
        lo, hi = doc_id << CHUNK_BITS, (doc_id << CHUNK_BITS) | CHUNK_MASK
        cond, args = _verify_sql([plan], " AND ", alias="chunks")
        return conn.execute(f"SELECT 1 FROM chunks WHERE id BETWEEN ? AND ? AND {cond} LIMIT 1",
                            [lo, hi] + args).fetchone() is not None

    def _first_chunk(self, conn: sqlite3.Connection, doc_id: int, plans: list[dict],
                     hits: list[Optional[dict]]) -> Optional[int]:
        """片段从哪块取：第一个词在这篇里第一次出现的块；都没有（只在标题、文件头里）就取第一块。"""
        lo, hi = doc_id << CHUNK_BITS, (doc_id << CHUNK_BITS) | CHUNK_MASK
        for p, h in zip(plans, hits):
            if h is None:
                cond, args = _verify_sql([p], " AND ", alias="chunks")
                r = conn.execute(f"SELECT id FROM chunks WHERE id BETWEEN ? AND ? AND {cond} ORDER BY id LIMIT 1",
                                 [lo, hi] + args).fetchone()
                if r:
                    return r[0]
                continue
            mine = [r for r in h if lo <= r <= hi]
            if mine:
                return min(mine)
        r = conn.execute("SELECT id FROM chunks WHERE id BETWEEN ? AND ? ORDER BY id LIMIT 1", (lo, hi)).fetchone()
        return r[0] if r else None

    def search_chunks(self, query: str, limit: int = 20, kinds: Optional[Iterable[str]] = None,
                      mode: str = "all") -> list[dict]:
        """段落块：[{path, abs, title, kind, date, heading, ord, pos, text, score}]，按相关度。
        mode="all"：块里要有全部词；mode="any"：有任何一个词就算（问答召回用，命中词多的排前）。
        文件名或标题里有这些词的文件，它的块加分。"""
        terms = parse_query(query)
        if not terms or limit <= 0:
            return []
        self.maybe_refresh()
        conn = self._reader()
        try:
            return self._search_chunks(conn, terms, limit, set(kinds) if kinds else None, mode)
        except sqlite3.Error as e:
            if _is_corrupt(e):
                self.mark_broken()
            raise IndexUnavailable(f"索引查询出错：{e}") from e
        finally:
            conn.close()

    def _search_chunks(self, conn, terms, limit, kinds, mode) -> list[dict]:
        docs = self._docs_meta(conn)
        plans = self._plan(conn, terms)
        hits = [self._term_hits(conn, p) for p in plans]
        title_sets = [self._field_docs(conn, p, "name title") for p in plans]
        if mode == "any":
            titled = set.union(*title_sets)
            found: dict[int, float] = {}
            for h in hits:
                for r, s in (h or {}).items():
                    found[r] = found.get(r, 0.0) + s
            if not found and all(h is None for h in hits):
                found = dict.fromkeys(self._like_scan(conn, plans, limit, " OR "), 0.0)
        else:
            titled = set.intersection(*title_sets)
            found = self._chunk_and(conn, plans, hits, limit)
        scored = []
        for cid, s in found.items():
            d = cid >> CHUNK_BITS
            if d not in docs or (kinds and docs[d][1] not in kinds):
                continue
            scored.append([cid, s - (FIELD_BONUS if d in titled else 0.0)])
        # 取前一批，按命中几个词、出现几次补分（正文索引不记次数，bm25 只知道块里有没有）
        scored.sort(key=lambda x: (x[1], _neg(docs[x[0] >> CHUNK_BITS][3])))
        scored = scored[:max(limit * 20, 200)]
        texts = _texts(conn, [c for c, _ in scored])
        for x in scored:
            low = texts.get(x[0], "").lower()
            present = [low.count(t.lower()) for t in terms]
            x[1] += -2.0 * sum(1 for n in present if n) - math.log1p(sum(present))
        scored.sort(key=lambda x: (round(x[1], 6), _neg(docs[x[0] >> CHUNK_BITS][3])))
        return self._chunk_rows(conn, docs, scored[:limit])

    def _chunk_rows(self, conn, docs, top) -> list[dict]:
        rows = {r[0]: r for r in conn.execute(
            f"SELECT id, heading, pos, text FROM chunks WHERE id IN ({','.join('?' * len(top))})",
            [c for c, _ in top])} if top else {}
        out = []
        for cid, s in top:
            d = cid >> CHUNK_BITS
            path, kind, title, day, _m = docs[d]
            _, heading, pos, text = rows[cid]
            out.append({"path": path, "abs": os.path.join(self.vault, *path.split("/")), "title": title,
                        "kind": kind, "date": day, "heading": heading, "ord": cid & CHUNK_MASK, "pos": pos,
                        "text": text, "score": round(s, 4)})
        return out


def _texts(conn: sqlite3.Connection, ids: list[int]) -> dict[int, str]:
    out = {}
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        out.update(conn.execute(f"SELECT id, text FROM chunks WHERE id IN ({','.join('?' * len(part))})", part))
    return out


def _neg(day: str) -> tuple:
    """日期倒序用的排序键（字符串不能取负）。"""
    return tuple(-ord(ch) for ch in day)


def parse_query(query: str) -> list[str]:
    """空格分开的词，去重，保留原样（匹配时不分大小写）。"""
    seen, out = set(), []
    for t in (query or "").split():
        k = t.lower()
        if k not in seen:
            seen.add(k)
            out.append(t)
    return out


def make_snippet(text: str, terms: list[str], before: int = 40, after: int = 100) -> tuple[str, list[list[int]]]:
    """块文字里第一处命中前后一段（空白压成一个空格，同原来的搜索），和片段里每处命中的 [起, 止)。"""
    if not text:
        return "", []
    low = text.lower()
    found = [i for i in (low.find(t.lower()) for t in terms) if i >= 0]
    at = min(found) if found else 0
    start = max(0, at - before)
    raw = text[start:at + after]
    snippet = _WS_RE.sub(" ", raw).strip()
    s_low = snippet.lower()
    hits = []
    for t in terms:
        tl = t.lower()
        i = s_low.find(tl)
        while i >= 0:
            hits.append([i, i + len(tl)])
            i = s_low.find(tl, i + len(tl))
    hits.sort()
    return snippet, hits


# ---------------------------------------------------------------- 默认索引（当前笔记库）

_INDEXES: dict[tuple[str, str], SearchIndex] = {}
_INDEXES_LOCK = threading.Lock()


def get_index(db_path: str = "", vault: str = "") -> SearchIndex:
    key = (os.path.abspath(db_path or default_path()), os.path.abspath(vault or default_vault()))
    with _INDEXES_LOCK:
        idx = _INDEXES.get(key)
        if idx is None:
            idx = _INDEXES[key] = SearchIndex(*key)
        return idx


def search_docs(query: str, limit: int = 60, kinds: Optional[Iterable[str]] = None) -> list[dict]:
    return get_index().search_docs(query, limit, kinds)


def later_docs() -> list[dict]:
    return get_index().later_docs()


def search_chunks(query: str, limit: int = 20, kinds: Optional[Iterable[str]] = None, mode: str = "all") -> list[dict]:
    return get_index().search_chunks(query, limit, kinds, mode)


def start_background_refresh() -> bool:
    """服务启动时调：后台建或刷新一次当前笔记库的索引。"""
    return get_index().refresh_async()
