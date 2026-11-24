"""标签索引：剪藏、论文、播客、会议演讲、信息文章放在一起按标签看。

数据在笔记库的 Spark/.tags.json（点开头，Obsidian 里看不到；数据线和 GitHub 同步都带到手机，手机只读）：

    {"version": 1,
     "aliases": {"NVIDIA": "英伟达"},      写法 → 正式标签
     "kinds":   {"Agent": "tech"},         正式标签 → tech|org|biz；没有的按 tech 显示
     "docs":    {"<相对 Spark/ 的路径>": ["标签1", "标签2"]}}   只记没有文件头 tags 的：播客、演讲、信息文章

剪藏和论文的标签以它们自己文件头的 tags 为准，读的时候过一遍 aliases。播客、演讲、信息文章的标签由
tag_untagged 让模型补，只记在这里，不改原文件。

读到坏 JSON 或顶层不是对象：当空索引，不抛错、不动原文件；写的时候先把它改名成
.tags.bad-<时间>.json 再写新的（两种同步都跳过名字里带 .bad- 的）。单项类型不对、值不合法：只丢那一项。
版本号更高：读当空索引，写直接拒绝（新版程序写的，旧程序不动它）。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from typing import Callable, Optional

from core import atomic, clips, jobs, vault
from core import search_index, track_translate

NAME = ".tags.json"
VERSION = 1
KINDS = ("tech", "org", "biz")
DEFAULT_KIND = "tech"
PAPERS_DIR = clips.PAPERS_DIR   # 格式同剪藏（剪藏式文件夹，core/clips.CLIP_DIRS）
UNTAGGED_TYPES = ("episode", "talk", "track")
BATCH = 40           # 补标签一次问多少条
VOCAB_MAX = 300      # 附给模型的已有标签最多多少个
KIND_BATCH = 200     # 补类别一次问多少个标签
TAG_MAX_CHARS = 20
TAG_MODEL = "claude-sonnet-5-5"   # 自动补标签固定用它，不跟全局设置走；只在后端是 Anthropic API 时才用得上

Ask = Callable[..., str]   # ask(prompt, max_tokens=, timeout=) -> 模型的回复


class TagsError(ValueError):
    pass


def _empty() -> dict:
    return {"version": VERSION, "aliases": {}, "kinds": {}, "docs": {}, "sources": {}}


def path(root: str = "") -> str:
    return os.path.join(vault.spark_dir(root or vault.vault_root()), NAME)


def _read(p: str) -> tuple[dict, str]:
    """(索引, 状态)。状态："" 正常或没有文件；"bad" 不是合法 JSON 或顶层不是对象；"newer" 版本号比本程序高。
    合法的对象里逐项宽容：aliases、kinds、docs 里类型不对、值不合法的那一项丢掉，其余照用。
    读不出来（权限之类）抛 OSError，免得写的一方当空表盖掉。"""
    try:
        with open(p, encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        return _empty(), ""
    except UnicodeDecodeError:   # 不是 UTF-8：跟不是合法 JSON 一样算坏文件（写的时候先改名挪开）
        return _empty(), "bad"
    try:
        data = json.loads(text)
    except ValueError:
        return _empty(), "bad"
    if not isinstance(data, dict):
        return _empty(), "bad"
    # 版本号不是 1（也不是没写）：当成别的版本的程序写的，不读也不写
    if "version" in data and (type(data["version"]) is not int or data["version"] != VERSION):
        return _empty(), "newer"
    out = _empty()
    a, k, d = data.get("aliases"), data.get("kinds"), data.get("docs")
    if isinstance(a, dict):
        out["aliases"] = {key: v for key, v in a.items() if isinstance(v, str)}
    if isinstance(k, dict):
        out["kinds"] = {key: v for key, v in k.items() if v in KINDS}
    if isinstance(d, dict):
        out["docs"] = {key: list(v) for key, v in d.items()
                       if isinstance(v, list) and all(isinstance(t, str) for t in v)}
    src = data.get("sources")
    if isinstance(src, dict):
        out["sources"] = {key: v for key, v in src.items() if isinstance(v, str)}
    return out, ""


def load(root: str = "") -> dict:
    """读索引；坏了、版本更高、没有都当空的（不合法的单项丢掉）。不改文件。"""
    try:
        return _read(path(root))[0]
    except OSError:
        return _empty()


_WRITE_LOCK = threading.Lock()


def _set_aside(p: str) -> None:
    stem = p[:-len(".json")]
    stamp = time.strftime("%Y%m%d-%H%M%S")   # 时间只用来起名
    bad, n = f"{stem}.bad-{stamp}.json", 2
    while os.path.exists(bad):
        bad, n = f"{stem}.bad-{stamp}-{n}.json", n + 1
    os.rename(p, bad)


def update(root: str, change: Callable[[dict], None]) -> dict:
    """读 → change(索引) 就地改 → 原子写回。整段在一把锁里，几个写的一方不会互相盖掉。
    原文件不是合法 JSON 对象时先改名挪开再写；版本号更高（新版程序写的）或读不出来（OSError）就不写，抛 TagsError。"""
    p = path(root)
    with _WRITE_LOCK:
        try:
            data, state = _read(p)
        except OSError as e:
            raise TagsError(f"{NAME} 读不出来，没有写：{e}") from e
        if state == "newer":
            raise TagsError(f"{NAME} 是更新版本的 Spark 写的，这个版本不改它")
        change(data)
        if state == "bad":
            _set_aside(p)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        atomic.write_json(p, data, indent=1)
        return data


# ---------------------------------------------------------------- 别名和类别

def _norm(tag) -> str:
    return tag.strip() if isinstance(tag, str) else ""


def resolve(tag: str, aliases: Optional[dict] = None) -> str:
    """过别名，顺着 a→b→c 找到最后的正式标签。表里有环（手改出来的）时停在回到见过的那一个之前。"""
    aliases = load()["aliases"] if aliases is None else aliases
    cur, seen = _norm(tag), set()
    while cur in aliases and cur not in seen:
        seen.add(cur)
        nxt = _norm(aliases[cur])
        if not nxt or nxt in seen:
            break
        cur = nxt
    return cur


def _name_ok(tag: str) -> bool:
    """手工填的标签名：跟模型给的一个标准（_ok_tag），再不许带换行这类控制字符。"""
    return bool(_ok_tag(tag)) and not any(ord(c) < 32 or ord(c) == 127 for c in tag)


def set_alias(src: str, dst: str, root: str = "") -> dict:
    """src 这种写法以后算 dst。dst 自己有别名时指到最终的那个；会成环、指向自己的拒绝。
    原来指向 src 的别名一起改指到最终的那个。"""
    src, dst = _norm(src), _norm(dst)
    if not src or not dst:
        raise TagsError("别名的两边都不能是空的")
    if not _name_ok(src) or not _name_ok(dst):
        raise TagsError("标签名太长，或带了换行、竖线")
    if src == dst:
        raise TagsError("不能把一个标签设成它自己的别名")

    def change(data):
        final = resolve(dst, data["aliases"])
        if final == src:
            raise TagsError(f"「{dst}」已经指向「{src}」，再设会成环")
        data["aliases"][src] = final
        for k, v in data["aliases"].items():
            if _norm(v) == src:
                data["aliases"][k] = final
    return update(root, change)


def remove_alias(src: str, root: str = "") -> dict:
    src = _norm(src)
    return update(root, lambda data: data["aliases"].pop(src, None))


def set_kind(tag: str, kind: str, root: str = "") -> dict:
    """类别记在正式标签上（tag 是别的写法时记到它指向的那个）。"""
    if kind not in KINDS:
        raise TagsError(f"类别只能是 {'、'.join(KINDS)}")
    if not _norm(tag):
        raise TagsError("标签不能是空的")
    if not _name_ok(tag):
        raise TagsError("标签名太长，或带了换行、竖线")

    def change(data):
        data["kinds"][resolve(tag, data["aliases"])] = kind
    return update(root, change)


# ---------------------------------------------------------------- 汇总条目

def main_file(paths: list[str]) -> str:
    """一期节目 / 演讲的「主文件」：notes/ 下的小结；没有就 speech/；再没有 transcripts/。
    信息文章就是文章文件本身（不在这三个文件夹里的，取排序后的第一个）。"""
    for sub in ("notes", "speech", "transcripts"):
        hit = sorted(p for p in paths if p.rsplit("/", 2)[-2:-1] == [sub])
        if hit:
            return hit[0]
    return sorted(paths)[0]


_EP_KEY_RE = re.compile(r"^(\d{3,8})[ _](.+)$")   # 同 apps/reader/server._ep_key


def _ep_key(name: str) -> str:
    stem = name[:-3] if name.endswith(".md") else name
    m = _EP_KEY_RE.match(stem)
    prefix, rest = (m.group(1), m.group(2)) if m else ("", stem)
    return prefix + "|" + re.sub(r"[^\w]+", "", rest.lower())


def folder_of(paths: list[str]) -> str:
    """一期节目、一场演讲、一篇信息文章所在的来源文件夹（路径都相对 Spark/）：主文件去掉文件名，
    再去掉 notes / speech / transcripts 这一层。「a16z - Videos/notes/x.md」→「a16z - Videos」，
    「信息跟进/a16z/x.md」→「信息跟进/a16z」，「某大会/某分会/speech/1.md」→「某大会/某分会」。"""
    parts = main_file(paths).split("/")[:-1]
    if parts and parts[-1] in ("notes", "speech", "transcripts"):
        parts = parts[:-1]
    return "/".join(parts)


def source_tags(folder: str, sources: dict) -> list[str]:
    """来源文件夹自带的标签（索引里的 sources：文件夹 → 标签）。记在上层文件夹的规则对它下面的分会也算；
    几条规则都对得上时按文件夹名的字符顺序排。"""
    return [sources[k] for k in sorted(sources) if k and (folder == k or folder.startswith(k + "/"))]


def item_tags(index: dict, paths: list[str]) -> tuple[list[str], bool]:
    """播客、演讲、信息文章的一条：（正式标签, 模型补过没有）。paths 相对 Spark/。
    标签 = docs 里记的（按主文件找；主文件换了时同一期别的文件下记的也认）+ 来源文件夹自带的，过一遍别名。"""
    main = main_file(paths)
    keys = [main] + sorted(p for p in paths if p != main)
    hit = next((k for k in keys if k in index["docs"]), None)
    raw = (index["docs"][hit] if hit else []) + source_tags(folder_of(paths), index.get("sources") or {})
    return _clean_tags(raw, index["aliases"]), hit is not None


def set_source(folder: str, tag: str, root: str = "") -> dict:
    """来自这个文件夹的内容都算在 tag 下（tag 为空表示去掉这条规则）。folder 相对 Spark/，如「信息跟进/a16z」。"""
    folder = "/".join(x for x in _norm(folder).split("/") if x)
    if not folder or any(x in (".", "..") for x in folder.split("/")) or len(folder) > 300 or any(ord(c) < 32 for c in folder):
        raise TagsError("来源文件夹写法不对")
    tag = _norm(tag)
    if tag and not _name_ok(tag):
        raise TagsError("标签名太长、太泛，或带了换行、竖线")

    def change(data):
        if tag:
            data["sources"][folder] = tag
        else:
            data["sources"].pop(folder, None)
    return update(root, change)


def _clean_tags(tags, aliases: dict) -> list[str]:
    out: list[str] = []
    for t in tags or []:
        r = resolve(t, aliases)
        if r and r not in out:
            out.append(r)
    return out


def _frontmatter_items(root: str, rel_dir: str, typ: str, aliases: dict) -> list[dict]:
    """剪藏和论文（剪藏式文件夹，同一个读法 core/clips.load_clips）：标签在文件头里。"""
    spark = vault.SPARK_DIRNAME + "/"
    rows = clips.load_clips(root, rel_dir)
    return [{"path": f"{rel_dir}/{c['name']}"[len(spark):], "title": c["title"],
             "date": c["clipped"] or c["published"], "type": typ, "summary": c["tldr"],
             "source": c["source"], "tags": _clean_tags(c["tags"], aliases)} for c in rows]


def _manifest(full_dir: str) -> dict:
    try:
        with open(os.path.join(full_dir, ".manifest.json"), encoding="utf-8") as f:
            m = json.load(f)
        return m if isinstance(m, dict) else {}
    except (OSError, ValueError):
        return {}


def _episode_groups(root: str) -> list[dict]:
    """播客、会议演讲、信息文章：每期一组 {folder, files, row, type}。遍历用 search_index.walk_markdown，
    种类用 search_index.kind_of（单集小结 / 整理稿 / 文字记录 / 信息文章）。同一期的几个文件先按
    .manifest.json 的行认，manifest 里没有的再按文件名（日期或序号前缀 + 标题）归到一起，同 server._episodes。"""
    track_prefix = track_translate.TRACK_DIR + "/"
    folders: dict[str, list[str]] = {}
    for rel, _full, _st in search_index.walk_markdown(root, (vault.SPARK_DIRNAME,)):
        kind = search_index.kind_of(rel)
        parts = rel.split("/")
        if kind == "信息文章":
            folder = "/".join(parts[:3])
        elif kind in ("单集小结", "整理稿", "文字记录"):
            folder = "/".join(parts[:-2])
            if folder == vault.SPARK_DIRNAME or folder.startswith(PAPERS_DIR + "/"):
                continue
        else:
            continue
        folders.setdefault(folder, []).append(rel)
    groups: list[dict] = []
    for folder, rels in folders.items():
        m = _manifest(os.path.join(root, *folder.split("/")))
        typ = ("track" if folder.startswith(track_prefix)
               else "episode" if m.get("content_type") == "series" else "talk")
        rows = m.get("entries") if isinstance(m.get("entries"), dict) else {}
        owner: dict[str, str] = {}      # 文件（相对笔记库）→ manifest 行
        for rid, row in rows.items():
            if not isinstance(row, dict):
                continue
            for key in ("note_relative_path", "speech_relative_path", "relative_path"):
                v = row.get(key)
                if isinstance(v, str) and v:
                    owner.setdefault(f"{folder}/{v}", rid)
        key_owner = {_ep_key(r.rsplit("/", 1)[-1]): owner[r] for r in rels if r in owner}
        by: dict[str, dict] = {}
        for r in sorted(rels):
            rid = owner.get(r) or key_owner.get(_ep_key(r.rsplit("/", 1)[-1]))
            gk = f"row:{rid}" if rid else f"key:{_ep_key(r.rsplit('/', 1)[-1])}"
            g = by.setdefault(gk, {"folder": folder, "files": [], "type": typ,
                                   "row": rows.get(rid) if rid and isinstance(rows.get(rid), dict) else {}})
            g["files"].append(r)
        groups += by.values()
    return groups


def _group_item(root: str, g: dict, index: dict) -> dict:
    spark = vault.SPARK_DIRNAME + "/"
    main = main_file(g["files"])
    row = g["row"]
    entry = row.get("entry") if isinstance(row.get("entry"), dict) else {}
    summary = row.get("summary") if isinstance(row.get("summary"), dict) else {}
    pd = str(entry.get("publish_date") or "")
    title = str(entry.get("title") or "")
    date = f"{pd[:4]}-{pd[4:6]}-{pd[6:8]}" if re.fullmatch(r"\d{8}", pd) else ""
    tldr = summary.get("tldr") if isinstance(summary.get("tldr"), str) else ""
    if not (title and date and tldr):
        try:
            with open(os.path.join(root, *main.split("/")), encoding="utf-8", errors="replace") as f:
                doc = search_index.parse_doc(main, f.read(16384))
        except OSError:
            doc = {}
        title, date, tldr = title or doc.get("title", ""), date or doc.get("date", ""), tldr or doc.get("summary", "")
    topics = [t for t in summary.get("topics") or [] if isinstance(t, str)] if isinstance(summary.get("topics"), list) else []
    got, tagged = item_tags(index, [f[len(spark):] for f in g["files"]])
    return {"path": main[len(spark):], "title": title, "date": date, "type": g["type"], "summary": tldr,
            "source": g["folder"].rsplit("/", 1)[-1],      # 节目、会议、信息来源的文件夹名
            "tags": got, "_tagged": tagged, "_topics": topics}


def _collect(root: str, index: dict) -> list[dict]:
    a = index["aliases"]
    items = (_frontmatter_items(root, clips.CLIPS_DIR, "clip", a) + _frontmatter_items(root, PAPERS_DIR, "paper", a))
    for it in items:
        it["_tagged"], it["_topics"] = True, []
    return items + [_group_item(root, g, index) for g in _episode_groups(root)]


def _public(it: dict) -> dict:
    return {k: v for k, v in it.items() if not k.startswith("_")}


def all_items(root: str = "", index: Optional[dict] = None) -> list[dict]:
    """全部类型的条目：{path（相对 Spark/）, title, date, type（clip/paper/episode/talk/track）, summary, source, tags}。"""
    root = root or vault.vault_root()
    return [_public(it) for it in _collect(root, index or load(root))]


def doc_tags(rel: str, root: str = "", index: Optional[dict] = None) -> list[str]:
    """一篇的正式标签。rel 相对 Spark/。剪藏、论文读文件头，别的查 docs。"""
    root = root or vault.vault_root()
    index = index or load(root)
    full_rel = f"{vault.SPARK_DIRNAME}/{rel}"
    if clips.is_clip_path(full_rel):   # 剪藏和论文
        try:
            with open(os.path.join(root, *full_rel.split("/")), encoding="utf-8") as f:
                return _clean_tags(clips._tags(clips.frontmatter(f.read(8192)).get("tags")), index["aliases"])
        except OSError:
            return []
    return _clean_tags(index["docs"].get(rel), index["aliases"])


def cloud(min_count: int = 3, root: str = "", items: Optional[list[dict]] = None,
          index: Optional[dict] = None) -> list[dict]:
    """[{tag, count, kind}]：count 是有这个标签的条目数（所有类型合计，一篇里两种写法指向同一个只算一次），
    只要 count ≥ min_count 的；按篇数降序，同数按标签字符串升序。"""
    root = root or vault.vault_root()
    index = index or load(root)
    items = all_items(root, index) if items is None else items
    count: dict[str, int] = {}
    for it in items:
        for t in set(it["tags"]):
            count[t] = count.get(t, 0) + 1
    return [{"tag": t, "count": n, "kind": index["kinds"].get(t, DEFAULT_KIND)}
            for t, n in sorted(count.items(), key=lambda kv: (-kv[1], kv[0])) if n >= min_count]


def items_for(tag: str, root: str = "", items: Optional[list[dict]] = None,
              index: Optional[dict] = None) -> list[dict]:
    """这个标签（先过别名）下的条目，按日期新到旧，同一天按路径。"""
    root = root or vault.vault_root()
    index = index or load(root)
    want = resolve(tag, index["aliases"])
    items = all_items(root, index) if items is None else items
    hits = sorted((it for it in items if want in it["tags"]), key=lambda it: it["path"])
    return sorted(hits, key=lambda it: it["date"], reverse=True)


def reconcile(root: str = "") -> int:
    """docs 里指向已经不存在的文件的键删掉，返回删了几个。只给显式调用，不挂在任务和接口上
    （这种键本来就不会出现在结果里，留着无害；笔记库暂时读不到时自动删会误删）。"""
    root = root or vault.vault_root()
    spark = vault.spark_dir(root)
    gone = [k for k in load(root)["docs"] if not os.path.isfile(os.path.join(spark, *k.split("/")))]
    if not gone:
        return 0
    removed = [0]

    def change(data):
        for k in list(data["docs"]):
            if not os.path.isfile(os.path.join(spark, *k.split("/"))):
                del data["docs"][k]
                removed[0] += 1
    update(root, change)
    return removed[0]


# ---------------------------------------------------------------- 补标签（模型）

TYPE_LABELS = {"episode": "播客", "talk": "演讲", "track": "信息文章"}

TAG_PROMPT = """给下面每一条内容打 1–4 个标签。这些标签用来把同一主题的剪藏、播客、演讲、信息文章放在一起看。

规则：
1. 优先用「已有标签」里的写法。意思相同就照抄原样，不换同义词，不改大小写，不在中英文之间换。
2. 已有标签里没有合适的，才起新标签。新标签 2–8 个字，是具体的领域、技术、方法、公司、产品或人名，比如「强化学习」「英伟达」「储能」「黄仁勋」。
3. 不要泛词，比如「AI」「人工智能」「科技」「思考」「访谈」「行业」「趋势」「观点」「分析」。不要用节目名、栏目名、来源名当标签。
4. 每条至少 1 个，最多 4 个。只打标题和小结里明确讲到的主题，不要为了凑数加不相干的标签。内容和 AI 无关（比如健康、创业、政治、财经）也要打，用它自己的具体主题。
5. 起新标签之前，先在已有标签里找意思相同的，有就用它；意思不同就起新的，不要硬套一个不贴切的已有标签。
6. 新标签要粗一点：写一个领域、技术、公司或人名，是别的内容也会用到的，比如「运动科学」「加密货币」「自动驾驶」「财政政策」。不要把这一篇的论点或细节当标签，比如「酒精致癌风险」「窄切入与快速迭代」「机架级扩展」。
7. 能用已有标签说清楚的，就不再另起新标签；一条里新标签最多 1 个。

输出只有这一种行，别的什么都不写（不写解释、不加代码块、不加空格对齐）：
T|序号|标签1|标签2|标签3

每条内容写一行 T，序号照抄下面列表里的序号。标签里不能有「|」。

已有标签（按篇数从多到少）：
{vocab}

内容列表（每行：序号｜类型｜标题｜一句话小结｜原有话题）：
{items}
"""

KIND_PROMPT = """给下面每个标签分一个类别，三选一：
- tech：技术和方法（如 强化学习、数据工程、阅读方法）
- org：公司、产品、人物（如 英伟达、ChatGPT、黄仁勋）
- biz：产业、投资、社会（如 储能、风险投资、城市生活）

每个标签输出一行，别的什么都不写（不写解释、不加代码块）：
K|标签|类别

标签照抄下面列表里的写法。

标签列表：
{tags}
"""


def _one_line(s: str, limit: int = 200) -> str:
    return re.sub(r"\s+", " ", str(s or "")).replace("｜", "/").strip()[:limit]


GENERIC = {"ai", "人工智能", "科技", "技术"}   # 太泛的词不当标签：模型给了也不收，手工也不能设（比较时不分大小写）


def _ok_tag(t: str) -> str:
    t = t.strip()
    if t.lower() in GENERIC:
        return ""
    return t if t and len(t) <= TAG_MAX_CHARS and "|" not in t and "｜" not in t else ""


def build_prompt(batch: list[dict], vocab: list[str]) -> str:
    lines = [f"{i}｜{TYPE_LABELS.get(it['type'], it['type'])}｜{_one_line(it['title'], 120)}｜"
             f"{_one_line(it['summary'])}｜{'、'.join(_one_line(t, 30) for t in it.get('_topics') or [])}"
             for i, it in enumerate(batch, 1)]
    return TAG_PROMPT.replace("{vocab}", "、".join(vocab) or "（还没有）").replace("{items}", "\n".join(lines))


_FENCE_RE = re.compile(r"```[\w-]*")
_BULLET_RE = re.compile(r"^[-*•]\s+")


def parse_reply(text: str, n: int) -> tuple[dict[int, list[str]], dict[str, str]]:
    """严格解析：({序号: [标签…]}, {新标签: 类别})。每行先去掉首尾空白、行首的「- 」「* 」「• 」和包裹的反引号，
    全角「｜」当「|」，代码块围栏行跳过。认不出的行、序号不在 1..n 的、重复的序号丢掉；
    一条一个合格标签都没有就不算（留到下次）；超过 4 个只取前 4 个。"""
    tags: dict[int, list[str]] = {}
    kinds: dict[str, str] = {}
    for line in str(text or "").splitlines():
        line = line.strip()
        if _FENCE_RE.fullmatch(line):
            continue
        line = _BULLET_RE.sub("", line).strip("`").strip().replace("｜", "|")
        parts = line.split("|")
        if parts[0] == "T" and len(parts) >= 3 and parts[1].strip().isdigit():
            i = int(parts[1].strip())
            got: list[str] = []
            for t in parts[2:]:
                t = _ok_tag(t)
                if t and t not in got:
                    got.append(t)
            if 1 <= i <= n and i not in tags and got:
                tags[i] = got[:4]
        elif parts[0] == "K" and len(parts) == 3 and _ok_tag(parts[1]) and parts[2].strip() in KINDS:
            kinds.setdefault(parts[1].strip(), parts[2].strip())
    return tags, kinds


def _vocab(items: list[dict], index: dict) -> list[str]:
    return [c["tag"] for c in cloud(1, items=items, index=index)[:VOCAB_MAX]]


def _untagged(items: list[dict]) -> list[dict]:
    """还没有标签的播客、演讲、信息文章，新的在前。"""
    todo = sorted((it for it in items if it["type"] in UNTAGGED_TYPES and not it["_tagged"]),
                  key=lambda it: it["path"])
    todo.sort(key=lambda it: it["date"], reverse=True)
    return todo


def tag_untagged(limit: Optional[int] = None, dry_run: bool = False, ask: Optional[Ask] = None, *,
                 root: str = "", preview_path: str = "", batch: int = BATCH,
                 progress: Callable[[int, int], None] = lambda done, total: None) -> dict:
    """给还没有标签的播客、演讲、信息文章补标签（docs 里没有键的），新的在前，每批 batch 条问一次模型。
    正式跑：每批成功后原子写一次（中途失败已完成的批次留着）。dry_run：不写 .tags.json，
    结果写到 preview_path（每批写一次）。返回 {total, tagged, skipped, batches, new_tags}。"""
    if ask is None:
        raise TagsError("没有模型")
    if dry_run and not preview_path:
        raise TagsError("试跑要给预览文件的路径")
    root = root or vault.vault_root()
    index = load(root)
    items = _collect(root, index)
    vocab = _vocab(items, index)
    known = set(vocab)
    todo = _untagged(items)
    if limit is not None:
        todo = todo[:max(0, int(limit))]
    preview: dict = {"items": [], "new_tags": {}}
    tagged = batches = 0
    for start in range(0, len(todo), batch):
        part = todo[start:start + batch]
        got, kinds = parse_reply(ask(build_prompt(part, vocab), max_tokens=3000, timeout=300), len(part))
        batches += 1
        docs = {part[i - 1]["path"]: t for i, t in got.items()}
        new = {t: "" for ts in docs.values() for t in ts if resolve(t, index["aliases"]) not in known}
        for t in new:
            new[t] = kinds.get(t, "")
        if dry_run:
            preview["items"] += [{"path": part[i - 1]["path"], "title": part[i - 1]["title"],
                                  "type": part[i - 1]["type"], "tags": t} for i, t in sorted(got.items())]
            preview["new_tags"].update(new)
            atomic.write_json(preview_path, preview, indent=1)
        elif docs:
            def change(data, docs=docs, new=new):
                data["docs"].update(docs)
                for t, k in new.items():
                    if k:
                        data["kinds"].setdefault(resolve(t, data["aliases"]), k)
            update(root, change)
        for t in new:   # 这一批新起的标签，后面的批次也当已有的用，免得同一个主题各批各起一个写法
            if t not in known:
                known.add(t)
                vocab.append(t)
        tagged += len(docs)
        progress(min(start + batch, len(todo)), len(todo))
    if dry_run and not todo:
        atomic.write_json(preview_path, preview, indent=1)
    return {"total": len(todo), "tagged": tagged, "skipped": len(todo) - tagged, "batches": batches,
            "new_tags": preview["new_tags"] if dry_run else None}


def classify_kinds(ask: Ask, root: str = "", batch: int = KIND_BATCH,
                   progress: Callable[[int, int], None] = lambda done, total: None) -> dict:
    """已经在用、还没有类别的正式标签（多半是剪藏的）补类别，每批 batch 个问一次，每批成功后写一次。
    只认问到的标签，已有的类别不改。返回 {total, classified, batches}。"""
    root = root or vault.vault_root()
    index = load(root)
    todo = [c["tag"] for c in cloud(1, root, index=index) if c["tag"] not in index["kinds"]]
    done = batches = 0
    for start in range(0, len(todo), batch):
        part = todo[start:start + batch]
        _tags, kinds = parse_reply(ask(KIND_PROMPT.replace("{tags}", "\n".join(part)), max_tokens=4000, timeout=300), 0)
        batches += 1
        got = {t: k for t, k in kinds.items() if t in part}
        if got:
            update(root, lambda data, got=got: [data["kinds"].setdefault(t, k) for t, k in got.items()])
        done += len(got)
        progress(min(start + batch, len(todo)), len(todo))
    return {"total": len(todo), "classified": done, "batches": batches}


# ---------------------------------------------------------------- 后台任务（同一时间只跑一个）

_LOCK = threading.Lock()
_JOBS: dict[str, dict] = {}
_KEEP_SECONDS = 24 * 3600
_KEEP_DONE = 20


def _ask(model: str = "") -> Optional[Ask]:
    """设置里的云端模型（core/llm_config），同 core/clip_capture._ask；没配好或选的是本地 Ollama 返回 None。
    model：只这一步换一个模型（后端、Key、接口地址照设置里的），不改设置。"""
    from core import llm as core_llm, llm_config
    try:
        cfg = llm_config.resolve({}, default_backend="api")
    except llm_config.ConfigError:
        return None
    if cfg["backend"] == "ollama":
        return None

    def ask(prompt, max_tokens=4000, timeout=600):
        return core_llm.complete(prompt, cfg["backend"], api_key=cfg["api_key"], model=model or cfg["model"],
                                 api_base=cfg["api_base"], max_tokens=max_tokens, timeout=timeout)
    return ask


def status(job_id: str) -> Optional[dict]:
    with _LOCK:
        return dict(_JOBS[job_id]) if job_id in _JOBS else None


def active_count() -> int:
    with _LOCK:
        return sum(not j["done"] for j in _JOBS.values())


def _update(job_id: str, **values) -> None:
    if values.get("done"):
        values.setdefault("finished_at", time.time())
    with _LOCK:
        _JOBS[job_id].update(values)


def start_fill(*, limit: Optional[int] = None, dry_run: bool = False, kinds: bool = False,
               preview_path: str = "", root: str = "", ask: Optional[Ask] = None, model: str = "") -> str:
    """开一个补标签任务（kinds=True 时是补类别），返回任务号。已经有一个在跑就拒绝。
    model：这一次用哪个模型，空的用设置里的。"""
    if kinds and dry_run:
        raise TagsError("补类别没有试跑")
    ask = ask or (_ask(model) if model else _ask())
    if ask is None:
        raise TagsError("没配好云端模型（本地 Ollama 不用来补标签），去设置里填好再补")
    with _LOCK:
        if any(not j["done"] for j in _JOBS.values()):
            raise TagsError("已经有一个补标签任务在跑，等它做完")
        jobs.prune_finished(_JOBS, retention_seconds=_KEEP_SECONDS, max_completed=_KEEP_DONE)
        job_id = uuid.uuid4().hex
        _JOBS[job_id] = {"id": job_id, "done": False, "kind": "kinds" if kinds else "tags", "dry_run": dry_run,
                         "phase": "开始", "done_count": 0, "total": 0, "error": "", "result": None,
                         "preview": preview_path if dry_run else "", "created_at": time.time()}

    def progress(done, total):
        _update(job_id, done_count=done, total=total, phase=f"{done}/{total}")

    def run():
        try:
            if kinds:
                result = classify_kinds(ask, root, progress=progress)
            else:
                result = tag_untagged(limit, dry_run, ask, root=root, preview_path=preview_path, progress=progress)
            _update(job_id, done=True, phase="做完了", result=result)
            if not dry_run:
                from core import github_sync
                github_sync.schedule_sync()   # 不走 after_task：它会再触发补标签
        except Exception as e:  # noqa: BLE001  错误带回给查进度的一方；做完的批次已经写好了
            _update(job_id, done=True, phase="出错了", error=str(e))

    try:
        threading.Thread(target=run, daemon=True).start()
    except BaseException:
        _update(job_id, done=True, phase="出错了", error="后台线程起不来")
        raise
    return job_id


def fill_model(cfg: dict) -> str:
    """自动补标签用哪个模型：后端是 Anthropic API（"api"）用 TAG_MODEL；别的后端用设置里的，
    TAG_MODEL 这个名字在它们那里不存在。"""
    return TAG_MODEL if cfg["backend"] == "api" else cfg["model"]


_TRIED: set[str] = set()   # fill_new 已经为它们开过补标签任务的条目（只记在内存里，重启后会再试一次）


def fill_new(*, root: str = "", ask: Optional[Ask] = None) -> str:
    """新内容处理完后调：有还没补标签的就开一个补标签任务，返回任务号；没有、已有任务在跑、没配好云端模型、
    设置选的是本地 Ollama，都安静地返回 ""。任务在后台线程跑；扫笔记库这一步在调用线程里，调用方要自己放到线程里。
    只用云端模型，不碰 Ollama。"""
    from core import llm_config
    try:
        root = root or vault.vault_root()
        todo = {it["path"] for it in _untagged(_collect(root, load(root)))}
        # 只为「这次服务启动以来还没试过的」条目开任务：模型一直不给标签的那几条不会让每个任务收尾都再花一次调用
        if not todo - _TRIED:
            return ""
        model = ""
        if ask is None:
            cfg = llm_config.resolve({}, default_backend="api")
            if cfg["backend"] == "ollama":
                return ""
            model = fill_model(cfg)
        job_id = start_fill(root=root, ask=ask, model=model)
        _TRIED.update(todo)   # 任务真的开起来了才算试过：被拒（已有任务在跑）、没配好模型、选了 Ollama 的都不记，下次还会再试
        return job_id
    except (TagsError, llm_config.ConfigError, OSError, ValueError):
        return ""
