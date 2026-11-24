"""全文索引（core/search_index.py）的性能测量：在临时目录里生成合成笔记库，量建库、刷新、查询的耗时。

不进默认的 pytest（5 万篇要跑十几分钟）。用法：

    python3 tools/search_bench.py gen   --n 5000  --out /tmp/x/vault5k
    python3 tools/search_bench.py bench --vault /tmp/x/vault5k --index /tmp/x/i5k.sqlite [--old]

合成库：内容是程序生成的假文字（中文为主、夹英文），不拷真实笔记。种类比例和文件大小按 2026-11
真实笔记库的统计（约 4000 篇、162 MB）：文字记录 36%（中位 22 KB）、整理稿 36%（30 KB）、单集小结 13%
（8 KB）、信息文章 8%（10 KB）、剪藏 3.5%（17 KB）、其余是总览页、专题、报告、简报、复盘、随手记。
大小按对数正态分布抽（中位数和 90 分位对上真实的）。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 常用汉字（按大致字频排的前几百个，再随机补一些区段里的字当生僻字）
COMMON = ("的一是在不了有和人这中大为上个国我以要他时来用们生到作地于出就分对成会可主发年动同工也能下过子说产种面而方后多定"
          "行学法所民得经十三之进着等部度家电力里如水化高自二理起小物现实加量都两体制机当使点从业本去把性好应开它合还因由其些然前外"
          "天政四日那社义事平形相全表间样与关各重新线内数正心反你明看原又么利比或但质气第向道命此变条只没结解问意建月公无系军很情者最"
          "立代想已通并提直题党程展五果料象员革位入常文总次品式活设及管特件长求老头基资边流路级少图山统接知较将组见计别她手角期根论运"
          "农指几九区强放决西被干做必战先回则任取据处府研队南给色光门即保治北造百规热领七海口东导器压志世金增争济阶油思术极交受联什认"
          "六共权收证改清美再采转更单风切打白教速花带安场身车例真务具万每目至达走积示议声报斗完类八离华名确才科张信马节话米整空元况今"
          "集温传土许步群广石记需段研界拉林律叫且究观越织装影算低持音众书布复容儿须际商非验连断深难近矿千周委素技备半办青省列习响约支"
          "般史感劳便团往酸历市克何除消构府称太准精值号率族维划选标写存候毛亲快效斯院查江型眼王按格养易置派层片始却专状育厂京识适属圆"
          "包火住调满县局照参红细引听该铁价严龙飞芯片模型推理训练数据中心供电网络智能体代理机器人开源协议芯")
ZH_WORDS_FIXED = ["算力", "复盘", "芯片", "模型", "推理", "训练", "数据中心", "供电", "光通信", "智能体", "具身智能",
                  "大模型", "强化学习", "扩展法则", "长时程任务", "触觉传感器", "液冷散热", "先进封装", "带宽瓶颈"]
EN_WORDS_FIXED = ["AI", "GPU", "model", "inference", "training", "transformer", "scaling", "agent", "robotics",
                  "datacenter", "power", "bandwidth", "memory", "chip", "software", "gripper", "startup", "token",
                  "economics", "Nvidia", "OpenAI", "Anthropic", "latency", "throughput", "cluster", "optical"]

KINDS = [  # (种类, 占比, 中位字节, 90 分位字节)
    ("transcripts", 0.360, 22375, 108703),
    ("speech", 0.360, 29729, 138988),
    ("notes", 0.127, 8214, 11341),
    ("track", 0.078, 10332, 27824),
    ("clip", 0.036, 16808, 50872),
    ("overview", 0.019, 30475, 61727),
    ("topics", 0.007, 16948, 25230),
    ("report", 0.008, 55000, 90135),
    ("brief", 0.0025, 4688, 11077),
    ("review", 0.00125, 8165, 11451),
    ("jotting", 0.0005, 7566, 28002),
]


class Words:
    def __init__(self, rng: random.Random):
        self.rng = rng
        chars = list(dict.fromkeys(COMMON))
        rare = [chr(c) for c in rng.sample(range(0x4E00, 0x9FA5), 1500)]
        pool = chars * 3 + rare
        self.zh = list(dict.fromkeys(
            "".join(rng.choice(pool) for _ in range(rng.choice((2, 2, 2, 3, 4)))) for _ in range(30000)))
        letters = "etaoinshrdlcumwfgypbvkjxqz"
        self.en = list(dict.fromkeys(
            "".join(rng.choice(letters[:18] if i % 2 else letters) for i in range(rng.randint(2, 10)))
            for _ in range(20000)))
        # 要查的词放在中等词频（排第 100～3000 位），不让它们成为最常见的词，免得查询偏慢、不像真实库
        for words, fixed in ((self.zh, ZH_WORDS_FIXED), (self.en, EN_WORDS_FIXED)):
            for i, word in enumerate(fixed):
                words.insert(100 + i * 150, word)
        # Zipf 权重：前面的词常见
        self.zh_cum = _cum([1 / (i + 10) for i in range(len(self.zh))])
        self.en_cum = _cum([1 / (i + 10) for i in range(len(self.en))])

    def zh_sentence(self, n: int) -> str:
        ws = self.rng.choices(self.zh, cum_weights=self.zh_cum, k=n)
        if self.rng.random() < 0.3:
            ws.insert(self.rng.randrange(len(ws)), self.rng.choice(EN_WORDS_FIXED))
        return "".join(ws) + self.rng.choice("，。。。；？")

    def en_sentence(self, n: int) -> str:
        ws = self.rng.choices(self.en, cum_weights=self.en_cum, k=n)
        s = " ".join(ws)
        return s[:1].upper() + s[1:] + self.rng.choice("...?,")

    def zh_para(self, size: int) -> str:
        out = []
        while sum(map(len, out)) * 3 < size:
            out.append(self.zh_sentence(self.rng.randint(5, 14)))
        return "".join(out)

    def en_para(self, size: int) -> str:
        out = []
        while sum(map(len, out)) < size:
            out.append(self.en_sentence(self.rng.randint(6, 22)))
        return " ".join(out)


def _cum(ws):
    out, s = [], 0.0
    for w in ws:
        s += w
        out.append(s)
    return out


def _size(rng, med, p90) -> int:
    sigma = math.log(p90 / med) / 1.2816
    return int(min(600_000, max(800, rng.lognormvariate(math.log(med), sigma))))


def _doc(kind: str, w: Words, rng: random.Random, title: str, date: str, target: int) -> str:
    out: list[str] = []
    size = lambda: sum(len(s.encode()) for s in out)   # noqa: E731
    if kind == "transcripts":
        out.append(f"# {title}\n\n- 所属节目：合成节目\n- 链接：https://www.youtube.com/watch?v=abc\n- 播出时间：{date}\n\n## 文字记录\n")
        t = 0
        while size() < target:
            out.append(f"\n### 🗣️ Speaker {rng.randint(1, 4)}\n")
            for _ in range(rng.randint(3, 12)):
                out.append(f"\n**[{t // 60}:{t % 60:02d}](https://www.youtube.com/watch?v=abc&t={t}s)**  \n{w.en_para(rng.randint(40, 260))}\n")
                t += rng.randint(2, 30)
    elif kind == "speech":
        out.append(f"# {title}\n\n- 所属节目：合成节目\n- 播出时间：{date}\n\n## 演讲稿\n")
        while size() < target:
            if rng.random() < 0.15:
                out.append(f"\n**{w.en_sentence(4)}**\n\n> **{w.zh_sentence(4)}**\n")
            para = w.en_para(rng.randint(200, 900))
            out.append(f"\n{para}\n\n> {w.zh_para(len(para))}\n")
    elif kind in ("notes", "topics", "review", "jotting", "brief", "report", "overview"):
        fm = (f'---\n节目: "[[合成节目]]"\n播出: {date}\n话题: {json.dumps([w.zh_sentence(1)[:-1] for _ in range(4)], ensure_ascii=False)}\n---\n\n'
              if kind == "notes" else "")
        out.append(f"{fm}# {title}\n\n> {w.zh_sentence(14)}\n\n")
        while size() < target:
            out.append(f"\n## {w.zh_sentence(3)[:-1]}\n\n")
            for _ in range(rng.randint(2, 8)):
                out.append(f"- {w.zh_para(rng.randint(150, 600))}\n")
            out.append(f"\n{w.zh_para(rng.randint(300, 1500))}\n")
    else:   # clip / track：摘要 + 原文（一半英文配译文）
        tags = json.dumps([w.zh_sentence(1)[:-1] for _ in range(3)], ensure_ascii=False)
        fm = (f'---\ntitle: "{title}"\nurl: "https://example.com/{rng.randint(1, 10**9)}"\npublished: {date}\n'
              f'tags: {tags}\ntldr: "{w.zh_sentence(12)}"\n---\n\n' if kind == "clip" else "")
        out.append(f"{fm}# {title}\n\n- 来源：合成来源\n- 发布时间：{date}\n\n## 小结\n\n**{w.zh_sentence(10)}**\n\n")
        for _ in range(4):
            out.append(f"- {w.zh_para(200)}\n")
        out.append("\n## 原文\n\n")
        english = rng.random() < 0.5
        while size() < target:
            if rng.random() < 0.1:
                out.append(f"![图](attachments/{rng.randint(1, 10**12):x}.png)\n\n")
            if english:
                para = w.en_para(rng.randint(200, 800))
                out.append(f"{para}\n\n> {w.zh_para(len(para))}\n\n")
            else:
                out.append(f"{w.zh_para(rng.randint(400, 1500))}\n\n")
    return "".join(out)


def generate(n: int, out_dir: str, seed: int = 1) -> dict:
    rng = random.Random(seed)
    w = Words(rng)
    spark = os.path.join(out_dir, "Spark")
    n_shows = max(10, n // 120)
    counts = {}
    total = 0
    for kind, share, med, p90 in KINDS:
        k = max(1, round(n * share))
        counts[kind] = k
        for i in range(k):
            date = f"2026{rng.randint(1, 11):02d}{rng.randint(1, 28):02d}"
            title = (w.zh_sentence(3)[:-1] + " " + w.en_sentence(3)[:-1]).replace("/", " ")[:60]
            show = f"合成节目{rng.randrange(n_shows):03d}"
            if kind in ("transcripts", "speech", "notes", "topics"):
                folder = os.path.join(spark, show, kind)
                name = f"{date}{' ' if kind == 'notes' else '_'}{title}_{i}.md"
            elif kind == "overview":
                folder = os.path.join(spark, f"合成会议{i:04d}")
                name = f"合成会议{i:04d}.md"
            elif kind == "track":
                folder = os.path.join(spark, "信息跟进", f"订阅{rng.randrange(max(3, n // 600)):02d}")
                name = f"{date}_{title}_{i}.md"
            elif kind == "brief":
                folder = os.path.join(spark, "信息跟进", "简报")
                name = f"简报_{date}_{i}.md"
            elif kind == "clip":
                folder = os.path.join(spark, "剪藏")
                name = f"{date}_{title}_{i}.md"
            elif kind == "jotting":
                folder = os.path.join(spark, "笔记")
                name = f"{date}_{i}.md"
            elif kind == "review":
                folder = os.path.join(out_dir, "output", "复盘")
                name = f"复盘_{date}_{i}.md"
            else:
                folder = os.path.join(out_dir, "output")
                name = f"技术洞察报告_{title}_{date}_{i}.md"
            os.makedirs(folder, exist_ok=True)
            text = _doc(kind, w, rng, title, f"{date[:4]}-{date[4:6]}-{date[6:]}", _size(rng, med, p90))
            with open(os.path.join(folder, name), "w", encoding="utf-8") as f:
                f.write(text)
            total += len(text.encode())
    # 点开头的目录：不该进索引
    os.makedirs(os.path.join(spark, "合成节目000", ".cache"), exist_ok=True)
    with open(os.path.join(spark, "合成节目000", ".cache", "x.md"), "w", encoding="utf-8") as f:
        f.write("secret 算力")
    return {"files": sum(counts.values()), "bytes": total, "counts": counts}


# ---------------------------------------------------------------- 测量

QUERIES = {
    "3字以上中文": ["数据中心", "强化学习", "光通信", "扩展法则", "触觉传感器", "先进封装"],
    "2字中文": ["算力", "芯片", "复盘", "模型", "推理", "供电"],
    "英文单词": ["inference", "transformer", "bandwidth", "gripper", "Nvidia", "latency"],
    "2字英文": ["AI", "ai", "Go"],
    "多词": ["算力 芯片", "数据中心 供电", "AI 推理", "model inference", "强化学习 agent", "光通信 带宽瓶颈"],
}


def _timed(fn, *a, **k):
    t = time.perf_counter()
    r = fn(*a, **k)
    return time.perf_counter() - t, r


def _db_size(path: str) -> int:
    return sum(os.path.getsize(path + s) for s in ("", "-wal") if os.path.exists(path + s))


def bench(vault: str, index_path: str, *, old: bool, repeat: int = 5, out=print) -> dict:
    from core import search_index as si
    res: dict = {}
    idx = si.SearchIndex(index_path, vault)
    fresh = not os.path.exists(index_path)
    dt, stats = _timed(idx.refresh)
    res["build" if fresh else "first_refresh"] = {"seconds": round(dt, 2), **stats, "db_bytes": _db_size(index_path)}
    out(f"{'首次建库' if fresh else '第一次刷新'}：{dt:.2f} s，{stats}，索引 {_db_size(index_path) / 1e6:.1f} MB")
    times = [_timed(idx.refresh)[0] for _ in range(3)]
    res["noop_refresh"] = {"median": round(statistics.median(times), 3), "max": round(max(times), 3)}
    out(f"没有变化的刷新：中位 {statistics.median(times):.3f} s，最慢 {max(times):.3f} s")
    # 改 10 个文件
    rng = random.Random(7)
    files = [full for _rel, full, _st in si.walk_markdown(vault)]
    picked = rng.sample(files, 10)
    for p in picked:
        with open(p, "a", encoding="utf-8") as f:
            f.write(f"\n\n追加一段用来测增量刷新的文字 bench{rng.randint(1, 10**9)}。\n")
    dt, stats = _timed(idx.refresh)
    res["refresh_10_changed"] = {"seconds": round(dt, 3), **stats}
    out(f"改 10 个文件后的刷新：{dt:.3f} s，{stats}")
    si.STALE_SECONDS = 1e9   # 测查询时不触发后台刷新
    res["queries"] = {}
    for group, qs in QUERIES.items():
        ts, counts = [], []
        for q in qs:
            idx.search_docs(q, 60)   # 预热一次（文件缓存）
            for _ in range(repeat):
                dt, r = _timed(idx.search_docs, q, 60)
                ts.append(dt)
            counts.append(len(r))
        res["queries"][group] = {"median_ms": round(statistics.median(ts) * 1000, 1),
                                 "max_ms": round(max(ts) * 1000, 1), "n": len(ts), "hits": counts}
        out(f"查询 {group}：中位 {statistics.median(ts) * 1000:.1f} ms，最慢 {max(ts) * 1000:.1f} ms（{len(ts)} 次），条数 {counts}")
    ts = []
    for q in QUERIES["3字以上中文"] + QUERIES["2字中文"]:
        for _ in range(repeat):
            ts.append(_timed(idx.search_chunks, q, 20)[0])
    res["search_chunks"] = {"median_ms": round(statistics.median(ts) * 1000, 1), "max_ms": round(max(ts) * 1000, 1)}
    out(f"search_chunks（20 块）：中位 {statistics.median(ts) * 1000:.1f} ms，最慢 {max(ts) * 1000:.1f} ms")
    if old:
        res["old"] = bench_old(vault, out=out)
    idx.close()
    return res


def bench_old(vault: str, out=print) -> dict:
    """旧实现（逐个文件整篇读进来找子串）：直接调阅读页里保留的那份 _scan_search。"""
    os.environ["SPARK_VAULT"] = vault
    from apps.reader import server as reader
    res = {}
    with reader.app.test_request_context("/read/api/search"):
        for group, qs in QUERIES.items():
            ts = []
            for q in qs[:3]:
                reader._scan_search(q)
                for _ in range(2):
                    ts.append(_timed(reader._scan_search, q)[0])
            res[group] = {"median_ms": round(statistics.median(ts) * 1000, 1), "max_ms": round(max(ts) * 1000, 1)}
            out(f"旧实现 {group}：中位 {statistics.median(ts) * 1000:.1f} ms，最慢 {max(ts) * 1000:.1f} ms（{len(ts)} 次）")
    return res


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("gen")
    g.add_argument("--n", type=int, required=True)
    g.add_argument("--out", required=True)
    g.add_argument("--seed", type=int, default=1)
    b = sub.add_parser("bench")
    b.add_argument("--vault", required=True)
    b.add_argument("--index", required=True)
    b.add_argument("--old", action="store_true")
    b.add_argument("--json", default="")
    a = ap.parse_args()
    if a.cmd == "gen":
        if os.path.exists(a.out) and os.listdir(a.out):
            sys.exit(f"{a.out} 不是空目录")
        dt, r = _timed(generate, a.n, a.out, a.seed)
        print(f"生成 {r['files']} 篇，{r['bytes'] / 1e6:.1f} MB，{dt:.1f} s：{r['counts']}")
    else:
        # 设置文件、索引都不碰 ~/.spark
        os.environ.setdefault("SPARK_SETTINGS_FILE", os.path.join(os.path.dirname(os.path.abspath(a.index)), "settings.json"))
        r = bench(a.vault, a.index, old=a.old)
        if a.json:
            with open(a.json, "w", encoding="utf-8") as f:
                json.dump(r, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
