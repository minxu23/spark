"""
复盘：三种，都由电脑上的 Spark 生成，写进笔记库 output/复盘/（阅读页、手机书架的「报告」里都能看）。

- 阅读复盘（reading）：一段时间里电脑和手机上读过什么（core/readlog.py 汇总的阅读记录）、
  读的那些单集的小结和「我的高亮」、这段时间新进来但没读的，归纳收获、反复出现的话题、值得补看的。
- 主题来龙去脉（topic）：按主题检索笔记（跟 Notes2Insight 同一套检索），按时间排开，
  写时间线、说法怎么变、分歧和转折点。
- 观点检验（claims）：从较早的单集里摘出嘉宾的判断和预测，对照之后进来的内容，
  逐条看应验了、部分应验、被推翻还是尚无定论。

材料都取自处理记录里的小结（.manifest.json），不重新读整篇文字记录，一次复盘只调一两次模型。
检索走 Notes2Insight 的接口（在本进程里调，见 configure），跟页面上「按主题检索」结果一致。
"""

from __future__ import annotations

import glob
import json
import os
import re
import time
from datetime import datetime
from typing import Any, Callable, Optional

from core import atomic
from core import llm as core_llm
from core import llm_config
from core import readlog
from core import remote_tasks
from core import settings as core_settings
from core import vault

OUT_SUBDIR = "复盘"
KINDS = ("reading", "topic", "claims")
KIND_LABELS = {"reading": "阅读复盘", "topic": "主题来龙去脉", "claims": "观点检验"}
MAX_READ_ITEMS = 60
MAX_UNREAD_ITEMS = 25
MAX_TOPIC_ITEMS = 30
MAX_OLD_ITEMS = 25
MAX_NEW_ITEMS = 40

LocalCall = Callable[[str, str, Optional[dict]], "tuple[int, Any]"]
Progress = Callable[[str], None]
_CALL: dict = {"fn": None}


class ReviewError(RuntimeError):
    pass


def configure(call: LocalCall) -> None:
    _CALL["fn"] = call


def out_dir() -> str:
    return os.path.join(vault.reports_dir(), OUT_SUBDIR)


# ---------------------------------------------------------------- 笔记库索引

def _date_of(row: dict, files: list[str]) -> str:
    d = str((row.get("entry") or {}).get("publish_date") or "")
    if re.fullmatch(r"\d{8}", d):
        return d
    for f in files:
        m = re.match(r"(\d{8})", os.path.basename(f))
        if m:
            return m.group(1)
    return ""


def build_index(root: str = "") -> dict:
    """{"items": {(节目文件夹, 条目id): 信息}, "by_path": {文件相对路径: key}}，相对路径都相对笔记库。"""
    root = root or vault.vault_root()
    items: dict[tuple, dict] = {}
    by_path: dict[str, tuple] = {}
    spark = vault.spark_dir(root)
    for mf in glob.glob(os.path.join(spark, "*", ".manifest.json")) + \
            glob.glob(os.path.join(spark, "信息跟进", "*", ".manifest.json")):
        folder = os.path.dirname(mf)
        folder_rel = os.path.relpath(folder, root).replace(os.sep, "/")
        try:
            with open(mf, encoding="utf-8") as f:
                m = json.load(f)
        except (OSError, ValueError):
            continue
        track = "/信息跟进/" in f"/{folder_rel}/"
        for key, row in (m.get("entries") or {}).items():
            if not isinstance(row, dict) or not row.get("ok"):
                continue
            entry = row.get("entry") or {}
            eid = str(entry.get("id") or key)
            files = [f"{folder_rel}/{row[k]}" for k in ("note_relative_path", "speech_relative_path", "relative_path")
                     if isinstance(row.get(k), str) and row[k]]
            summary = row.get("summary") if isinstance(row.get("summary"), dict) else {}
            k = (folder_rel, eid)
            items[k] = {
                "show": os.path.basename(folder), "folder": folder_rel, "entry": eid,
                "title": str(entry.get("title") or eid), "date": _date_of(row, files),
                "tldr": str(summary.get("tldr") or ""), "body": str(summary.get("body") or ""),
                "note": f"{folder_rel}/{row['note_relative_path']}" if row.get("note_relative_path") else "",
                "updated_at": str(row.get("updated_at") or ""), "kind": "track" if track else (m.get("content_type") or "summit"),
            }
            for f in files:
                by_path[f] = k
    return {"items": items, "by_path": by_path, "root": root}


def _highlights(root: str, note_rel: str) -> list[str]:
    if not note_rel:
        return []
    try:
        with open(os.path.join(root, note_rel), encoding="utf-8") as f:
            sec = vault.highlights_section(f.read())
    except OSError:
        return []
    return [ln[2:].strip() for ln in sec.splitlines() if ln.startswith("- ")]


def _fmt_date(d: str) -> str:
    return f"{d[:4]}-{d[4:6]}-{d[6:]}" if re.fullmatch(r"\d{8}", d or "") else "日期不详"


def _cut(text: str, n: int) -> str:
    text = re.sub(r"\n{3,}", "\n\n", text or "").strip()
    return text if len(text) <= n else text[:n].rstrip() + "……"


def _source(it: dict) -> str:
    return f"《{it['title']}》（{it['show']}，{_fmt_date(it['date'])}）"


# ---------------------------------------------------------------- 模型

_STYLE = """写法要求：
- 用平实的中文，像给自己写的读书笔记。不要开场白和收尾总结，不写「值得注意的是」「总的来说」「综上所述」「不难看出」这类套话，不用排比和感叹。
- 每个判断后面括号注明出处：节目名和标题（材料里给的那个）。材料里没有的事实不要补，不确定的就说不确定。
- 用 Markdown：二级标题分节，下面用短段落或列表。不要表格以外的花样格式，不要 emoji。"""


def _llm(prompt: str, max_tokens: int = 8000) -> str:
    try:
        cfg = llm_config.resolve({}, default_backend="api")
    except llm_config.ConfigError as e:
        raise ReviewError(str(e)) from e
    model = core_settings.get(f"ai.overall_models.{cfg['backend']}") or cfg["model"]
    try:
        return core_llm.complete(prompt, cfg["backend"], api_key=cfg["api_key"], model=model,
                                 api_base=cfg["api_base"], max_tokens=max_tokens, timeout=1200)
    except core_llm.LLMError as e:
        raise ReviewError(f"模型调用失败：{e}") from e


def _search(topic: str, progress: Progress) -> list[dict]:
    """Notes2Insight 的主题检索，返回候选（path 相对笔记库、relevance）。"""
    call = _CALL["fn"]
    if call is None:
        raise ReviewError("检索没接上（Spark 要用 spark.py 启动）")
    progress("按主题检索笔记")
    runner = remote_tasks.Runner(call, _Quiet(progress))
    result = runner._search({"topic": topic})
    if result.get("need_choice"):
        senses = result.get("senses") or []
        if senses:
            result = runner._search({"topic": topic, "sense": senses[0]})
    return result.get("candidates") or []


class _Quiet:
    """remote_tasks.Runner 要的进度对象，转成复盘自己的进度文字。"""

    def __init__(self, progress: Progress):
        self.progress = progress
        self.comment_id = 0

    def update(self, text: str, *, force: bool = False, **extra) -> None:
        self.progress(text)


def _items_for_paths(index: dict, paths: list[str]) -> list[dict]:
    seen, out = set(), []
    for p in paths:
        k = index["by_path"].get(p)
        if k and k not in seen:
            seen.add(k)
            out.append(index["items"][k])
    return out


# ---------------------------------------------------------------- 阅读复盘

def _pull_phone_reads(progress: Progress) -> str:
    """复盘前把手机走 GitHub 交上来的阅读记录拉一遍。拉不到不影响复盘，返回原因。"""
    slug, tok = remote_tasks.repo_slug(), remote_tasks.token()
    if not (slug and tok):
        return ""
    progress("拉手机上的阅读记录")
    try:
        gh = remote_tasks.GitHub(slug, tok)
        readlog.pull_github(gh, gh.me())
        return ""
    except remote_tasks.TaskError as e:
        return f"手机经 GitHub 交上来的阅读记录这次没拉到（{e}），只用了已经收到的"


def reading_material(index: dict, since: float, until: float) -> dict:
    events = sorted(readlog.load(since, until), key=lambda e: e["at"])
    reads: dict[Any, dict] = {}
    for ev in events:
        if ev.get("show") and ev.get("entry"):
            k = (ev["show"], ev["entry"])
        else:
            k = index["by_path"].get(ev.get("path") or "") or ("doc", ev.get("path") or "")
        it = index["items"].get(k)
        if it is None:
            path = ev.get("path") or ""
            if not path.endswith(".md"):
                continue
            it = {"show": os.path.dirname(path).split("/")[-1] or "笔记库", "title": os.path.basename(path)[:-3],
                  "date": "", "tldr": "", "body": "", "note": "", "folder": "", "entry": path}
        r = reads.setdefault(k, {"item": it, "first": ev["at"], "sources": set(), "kinds": set()})
        r["sources"].add(ev["source"])
        if ev.get("kind"):
            r["kinds"].add(ev["kind"])
    lo, hi = time.strftime("%Y%m%d", time.localtime(since)), time.strftime("%Y%m%d", time.localtime(until))
    unread = [it for k, it in index["items"].items()
              if k not in reads and lo <= (it["date"] or it["updated_at"][:10].replace("-", "")) <= hi]
    unread.sort(key=lambda it: it["date"], reverse=True)
    read_list = sorted(reads.values(), key=lambda r: r["first"])
    return {"reads": read_list[:MAX_READ_ITEMS], "read_total": len(read_list),
            "phone": sum(1 for r in read_list if "phone" in r["sources"]),
            "desktop": sum(1 for r in read_list if "desktop" in r["sources"]),
            "unread": unread[:MAX_UNREAD_ITEMS], "unread_total": len(unread)}


def reading_prompt(mat: dict, root: str, period: str) -> str:
    blocks = []
    for i, r in enumerate(mat["reads"], 1):
        it = r["item"]
        hl = _highlights(root, it.get("note", ""))
        how = "、".join(sorted({"note": "小结", "speech": "整理稿", "transcript": "文字记录", "article": "原文",
                                 "doc": "全文"}.get(k, k) for k in r["kinds"])) or "打开过"
        part = [f"[{i}] {_source(it)}　读了：{how}"]
        if it["tldr"]:
            part.append(f"一句话：{it['tldr']}")
        if hl:
            part.append("我的高亮：\n" + "\n".join(f"- {h}" for h in hl[:12]))
        if it["body"]:
            part.append("小结摘录：\n" + _cut(it["body"], 900))
        blocks.append("\n".join(part))
    unread = "\n".join(f"- {_source(it)}：{it['tldr'] or '（没有一句话总结）'}" for it in mat["unread"])
    return f"""你在帮我做一次阅读复盘，时间范围是{period}。下面是这段时间我读过的内容（按第一次打开的先后排），以及这段时间新进来但我还没读的。

请写这几节：
## 这段时间读了什么
按话题归成几组（不按节目），每组一两句话说清楚读的是什么，列出对应的篇目。
## 主要收获
挑出最有分量的 5 到 8 条认识或事实，每条一两句，注明出处。我划过高亮的地方优先。
## 反复出现的话题和分歧
哪些话题在不同节目里反复被提到，各方说法一致还是有分歧。
## 值得补看的
从「没读的」里挑 3 到 6 篇跟我这段时间关注的话题最相关的，说明为什么值得看。没读的为空就写「这段时间新进来的都读过了」。

{_STYLE}

===== 读过的（共 {mat['read_total']} 篇，下面列了 {len(mat['reads'])} 篇）=====
{chr(10).join(blocks) or '（这段时间没有阅读记录）'}

===== 新进来但没读的（共 {mat['unread_total']} 篇，下面列了 {len(mat['unread'])} 篇）=====
{unread or '（无）'}
"""


# ---------------------------------------------------------------- 主题来龙去脉

def topic_prompt(topic: str, items: list[dict]) -> str:
    items = sorted(items, key=lambda it: it["date"] or "99999999")
    blocks = []
    for i, it in enumerate(items, 1):
        part = [f"[{i}] {_source(it)}"]
        if it["tldr"]:
            part.append(f"一句话：{it['tldr']}")
        if it["body"]:
            part.append(_cut(it["body"], 1000))
        blocks.append("\n".join(part))
    return f"""下面是我笔记库里跟「{topic}」相关的内容，按时间先后排列。请复盘这个主题的来龙去脉。

请写这几节：
## 时间线
按时间列出关键节点：谁在什么时候说了什么、发生了什么，一条一行，注明出处。
## 说法是怎么变的
早期的主流看法是什么，后来哪里变了、因为什么变的。
## 分歧和转折点
各方在哪些问题上意见不同；哪些事件或数据改变了讨论的方向。
## 现在的共识和悬而未决的问题
到最近这批材料为止，哪些已经比较确定，哪些还在争。

{_STYLE}

===== 材料（共 {len(items)} 篇）=====
{chr(10).join(chr(10) + b for b in blocks)}
"""


# ---------------------------------------------------------------- 观点检验

CLAIMS_EXTRACT_PROMPT = """下面是我笔记库里较早的一批节目和文章的小结。请从中摘出嘉宾或作者对未来的判断和预测：
有明确指向、过一段时间能检验的（比如「X 会在两年内发生」「Y 路线会胜出」「Z 公司会如何」），不要泛泛的观点和事实陈述。

每条输出一行 JSON，字段：
{{"claim": "判断内容（一句话，保留原意）", "who": "谁说的", "source": 材料编号（数字）, "horizon": "说的时间范围，没说就写空串"}}
只输出 JSON 行，不要别的文字。最多 {limit} 条，挑最具体、最有分量的。
{topic_line}
===== 材料 =====
{material}
"""


def _extract_claims(old: list[dict], topic: str, limit: int = 30) -> list[dict]:
    blocks = "\n\n".join(f"[{i}] {_source(it)}\n{_cut(it['body'] or it['tldr'], 1500)}" for i, it in enumerate(old, 1))
    raw = _llm(CLAIMS_EXTRACT_PROMPT.format(
        limit=limit, material=blocks,
        topic_line=f"只摘跟「{topic}」有关的。\n" if topic else ""), max_tokens=6000)
    claims = []
    for line in raw.splitlines():
        line = line.strip().strip(",")
        if not line.startswith("{"):
            continue
        try:
            c = json.loads(line)
        except ValueError:
            continue
        try:
            src = old[int(c.get("source")) - 1]
        except (TypeError, ValueError, IndexError):
            continue
        if c.get("claim"):
            claims.append({"claim": str(c["claim"]), "who": str(c.get("who") or ""), "horizon": str(c.get("horizon") or ""),
                           "source": src})
    return claims[:limit]


def claims_prompt(claims: list[dict], new: list[dict], topic: str) -> str:
    cl = "\n".join(f"[C{i}] {c['claim']}（{c['who'] or '说话人不详'}，{_source(c['source'])}"
                   f"{'，时间范围：' + c['horizon'] if c['horizon'] else ''}）" for i, c in enumerate(claims, 1))
    ev = "\n\n".join(f"[E{i}] {_source(it)}\n{it['tldr']}\n{_cut(it['body'], 700)}" for i, it in enumerate(new, 1))
    return f"""下面左边是较早时候嘉宾和作者做出的判断与预测，右边是之后进来的节目和文章的小结。{f'主题是「{topic}」。' if topic else ''}
请逐条检验这些判断。

请写这几节：
## 检验结果
一个 Markdown 表格，列：判断（含谁说的、出处）｜结论｜依据。结论只能是：应验、部分应验、被推翻、尚无定论。
依据写清楚是后来哪篇材料（出处）说了什么；后来的材料里没提到的就写「尚无定论」，不要凭常识判断。
## 几点观察
谁的判断更靠谱、哪类预测容易落空、有没有大家都没料到的变化。三到五条。

{_STYLE}

===== 较早的判断（共 {len(claims)} 条）=====
{cl}

===== 之后进来的材料（共 {len(new)} 篇）=====
{ev or '（这段时间之后没有相关的新材料）'}
"""


# ---------------------------------------------------------------- 生成

def _write(kind: str, title: str, meta: list[str], body: str, name: str) -> str:
    os.makedirs(out_dir(), exist_ok=True)
    path = os.path.join(out_dir(), name)
    text = f"# {title}\n\n" + "\n".join(f"- {m}" for m in meta) + "\n\n" + body.strip() + "\n"
    atomic.write_text(path, text)
    return path


def _safe(name: str) -> str:
    return re.sub(r"[\\/:*?\"<>|\s]+", "_", name).strip("_")[:40] or "未命名"


def run(kind: str, params: dict, progress: Progress = lambda s: None) -> str:
    """生成一份复盘，返回写出的文件路径。"""
    if kind not in KINDS:
        raise ReviewError(f"不认识的复盘类型：{kind}")
    root = vault.vault_root()
    progress("读笔记库的处理记录")
    index = build_index(root)
    now = time.time()
    today = time.strftime("%Y%m%d")
    stamp = time.strftime("%Y-%m-%d %H:%M")

    if kind == "reading":
        days = int(params.get("days") or 7)
        if not 1 <= days <= 366:
            raise ReviewError("天数应该在 1 到 366 之间")
        note = _pull_phone_reads(progress)
        since = now - days * 86400
        mat = reading_material(index, since, now)
        if not mat["read_total"] and not mat["unread_total"]:
            raise ReviewError(f"近 {days} 天既没有阅读记录，也没有新进来的内容")
        lo = datetime.fromtimestamp(since)
        period = f"{lo.month}月{lo.day}日到{datetime.now().month}月{datetime.now().day}日（近 {days} 天）"
        progress("写阅读复盘")
        body = _llm(reading_prompt(mat, root, period))
        meta = [f"生成时间：{stamp}", f"读了 {mat['read_total']} 篇（电脑上 {mat['desktop']}，手机上 {mat['phone']}）；"
                f"这段时间新进来但没读的 {mat['unread_total']} 篇"]
        if note:
            meta.append(note)
        return _write(kind, f"阅读复盘：{period}", meta, body,
                      f"复盘_阅读_{time.strftime('%Y%m%d', time.localtime(since))}-{today}.md")

    topic = str(params.get("topic") or "").strip()
    if kind == "topic":
        if not topic:
            raise ReviewError("主题来龙去脉要填一个主题")
        cands = _search(topic, progress)
        items = _items_for_paths(index, [c["path"] for c in cands if (c.get("relevance") or 0) >= 2])[:MAX_TOPIC_ITEMS]
        if len(items) < 3:
            raise ReviewError(f"笔记库里跟「{topic}」相关、有处理记录的内容只有 {len(items)} 篇，不够复盘")
        progress(f"用 {len(items)} 篇写来龙去脉")
        body = _llm(topic_prompt(topic, items))
        dates = sorted(it["date"] for it in items if it["date"])
        span = f"{_fmt_date(dates[0])} 到 {_fmt_date(dates[-1])}" if dates else "日期不详"
        return _write(kind, f"主题来龙去脉：{topic}", [f"生成时间：{stamp}", f"材料 {len(items)} 篇，时间跨度 {span}"],
                      body, f"复盘_主题_{_safe(topic)}_{today}.md")

    # claims
    months = int(params.get("months") or 6)
    if not 1 <= months <= 60:
        raise ReviewError("月数应该在 1 到 60 之间")
    cutoff = time.strftime("%Y%m%d", time.localtime(now - months * 30 * 86400))
    if topic:
        pool = _items_for_paths(index, [c["path"] for c in _search(topic, progress) if (c.get("relevance") or 0) >= 2])
    else:
        pool = list(index["items"].values())
    dated = [it for it in pool if it["date"] and it["body"]]
    old = sorted((it for it in dated if it["date"] < cutoff), key=lambda it: it["date"], reverse=True)[:MAX_OLD_ITEMS]
    new = sorted((it for it in dated if it["date"] >= cutoff), key=lambda it: it["date"], reverse=True)[:MAX_NEW_ITEMS]
    if not old:
        raise ReviewError(f"{months} 个月以前{'跟「' + topic + '」相关' if topic else ''}的内容一篇都没有，没有可检验的判断")
    progress(f"从 {len(old)} 篇较早的内容里摘判断")
    claims = _extract_claims(old, topic)
    if not claims:
        raise ReviewError("较早的内容里没有摘出能检验的判断或预测")
    progress(f"对照之后的 {len(new)} 篇检验 {len(claims)} 条判断")
    body = _llm(claims_prompt(claims, new, topic))
    title = f"观点检验：{topic}" if topic else f"观点检验：{months} 个月前的判断"
    return _write(kind, title, [f"生成时间：{stamp}",
                                f"判断取自 {_fmt_date(cutoff)} 以前的 {len(old)} 篇，对照之后的 {len(new)} 篇，共 {len(claims)} 条"],
                  body, f"复盘_观点检验_{_safe(topic) if topic else f'{months}个月'}_{today}.md")
