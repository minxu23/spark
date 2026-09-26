"""重做整理稿：只重新生成 speech/ 里「## 演讲稿」下面的正文，别的都不动。

9 月 24 日之前生成的长节目整理稿是整篇一次交给模型整理的，超过单次输出上限就被截断，
常常只有开头三成左右。分块整理修好之后，已经生成过的文件不会自己重做，这里补上：

- 从已有的 transcripts/ 文字记录读回段落和发言人（不重新下载字幕），用现在的分块代码重新整理；
- 文件开头（标题、链接、说明、小结、笔记链接）原样保留，只换「## 演讲稿」下面的正文；
- 旧文件里用 ==…== 标的高亮，新正文里还能找到同样文字的就重新标上，找不到的留在笔记
  末尾「我的高亮」里（那里本来就有一份）；
- 改之前把旧文件备份到节目文件夹的 .cache/speech_backup/。

命令行：
    python3 -m apps.summit2md.redo_speech --scan               # 只列出看起来被截断的整理稿
    python3 -m apps.summit2md.redo_speech --file <speech.md>   # 重做一篇
    python3 -m apps.summit2md.redo_speech --all                # 重做全部被截断的
默认用本机 claude CLI 的 sonnet（--backend / --model 可改）。
"""
from __future__ import annotations

import argparse
import difflib
import glob
import json
import os
import re
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from . import pipeline
from core import llm_config
from core import vault as core_vault

SPEECH_HEADING = "## 演讲稿"
USAGE_LIMIT_RE = re.compile(r"hit your (?:session|usage|weekly) limit|rate.?limit|usage limit|quota", re.I)
_HIGHLIGHT_RE = re.compile(r"==([^=\n]+?)==")
_CJK_RE = re.compile(r"[一-鿿]")
_MODE_BY_LABEL = (("原文/中文对照", "bilingual"), ("中文翻译", "zh"), ("中文整理", "zh"), ("（原文）", "original"))

# 整理稿正文（原文部分）和文字记录的字数比，低于这个就算被截断了。正常的双语整理稿
# 在 0.8 上下；中文节目整理成中文会压缩一些；英文整理成中文，汉字数本来就只有英文字符的三成左右。
TRUNCATED_BELOW = {"bilingual": 0.6, "original": 0.6, "zh_from_zh": 0.4, "zh": 0.2}


def _split(content: str) -> tuple[str, str]:
    head, sep, body = content.partition(SPEECH_HEADING)
    if not sep:
        return content.rstrip() + "\n\n", ""
    return head + sep, body


def _speech_mode(head: str) -> str:
    for label, mode in _MODE_BY_LABEL:
        if label in head:
            return mode
    return "bilingual"


def _transcript_lang(transcript: str, paragraphs: list[tuple[float, str]]) -> str:
    m = re.search(r"字幕来源：YouTube 自动生成字幕（([^）]+)）", transcript)
    if m:
        return m.group(1)
    sample = "".join(t for _, t in paragraphs[:40])
    return "zh" if len(_CJK_RE.findall(sample)) > len(sample) * 0.2 else "en"


def coverage(body: str, paragraphs: list[tuple[float, str]], mode: str, lang: str) -> float:
    """整理稿正文占文字记录的比例（双语只算原文段落，引用块里的译文不算）。"""
    lines = [l for l in body.splitlines() if not l.startswith(">")] if mode == "bilingual" else body.splitlines()
    size = len(re.sub(r"\s", "", "".join(lines)))
    source = len(re.sub(r"\s", "", "".join(t for _, t in paragraphs)))
    return size / source if source else 1.0


def _threshold(mode: str, lang: str) -> float:
    if mode == "zh":
        return TRUNCATED_BELOW["zh_from_zh" if pipeline.is_zh_lang(lang) else "zh"]
    return TRUNCATED_BELOW.get(mode, 0.6)


def _load(show_dir: str, row: dict) -> Optional[dict]:
    speech_rel, transcript_rel = row.get("speech_relative_path"), row.get("relative_path")
    if not speech_rel or not transcript_rel:
        return None
    speech_path = os.path.join(show_dir, speech_rel)
    transcript_path = os.path.join(show_dir, transcript_rel)
    if not (os.path.exists(speech_path) and os.path.exists(transcript_path)):
        return None
    if (row.get("entry") or {}).get("source_type") in pipeline.TEXT_SOURCE_TYPES:
        return None   # 文章只是逐段翻译，不走整理，也没有截断问题
    with open(transcript_path, encoding="utf-8") as f:
        transcript = f.read()
    paragraphs, speakers, speaker_mode = pipeline._parse_transcript_body(transcript)
    if not paragraphs:
        return None
    with open(speech_path, encoding="utf-8") as f:
        speech = f.read()
    head, body = _split(speech)
    mode = _speech_mode(head)
    lang = _transcript_lang(transcript, paragraphs)
    cov = coverage(body, paragraphs, mode, lang)
    return {
        "show_dir": show_dir, "row": row, "speech_path": speech_path, "speech": speech,
        "head": head, "body": body, "mode": mode, "lang": lang, "coverage": cov,
        "truncated": cov < _threshold(mode, lang),
        "paragraphs": paragraphs, "speakers": speakers, "speaker_mode": speaker_mode,
    }


def episode_date(item: dict) -> str:
    """播出日期 YYYYMMDD：manifest 里记的，没有就看文件名开头；大会录像按序号命名，没有日期。"""
    d = re.sub(r"\D", "", str(item["row"]["entry"].get("publish_date") or ""))
    if len(d) >= 8:
        return d[:8]
    m = re.match(r"(\d{8})_", os.path.basename(item["speech_path"]))
    return m.group(1) if m else ""


def scan(spark_dir: str) -> list[dict]:
    found = []
    for manifest_path in sorted(glob.glob(os.path.join(spark_dir, "*", ".manifest.json"))):
        show_dir = os.path.dirname(manifest_path)
        with open(manifest_path, encoding="utf-8") as f:
            manifest = json.load(f)
        for row in (manifest.get("entries") or {}).values():
            item = _load(show_dir, row)
            if item:
                found.append(item)
    return found


_CLAUSE_END_RE = re.compile(r"[。！？；，：,.!?;:]")
SIMILAR_ENOUGH = 0.6


def _closest_span(body: str, text: str, threshold: float = SIMILAR_ENOUGH,
                  within: Optional[tuple[int, int]] = None) -> Optional[tuple[int, int]]:
    """在新正文里（或正文的 within 这一段里）找和旧高亮最像的一串连续分句，够像才算。"""
    best, best_score = None, threshold
    lo, hi = within or (0, len(body))
    offset = lo
    for line in body[lo:hi].split("\n"):
        # 分句的起止（去掉引用块的 "> " 和发言人标签）
        content_start = len(line) - len(line.lstrip("> "))
        m = re.match(r"\*\*[^*]+\*\*[：:]\s*", line[content_start:])
        if m:
            content_start += m.end()
        cuts = [content_start] + [c.end() for c in _CLAUSE_END_RE.finditer(line, content_start)]
        if cuts[-1] < len(line):
            cuts.append(len(line))
        for i in range(len(cuts) - 1):
            for j in range(i + 1, min(len(cuts), i + 6)):
                piece = line[cuts[i]:cuts[j]]
                cand = piece.strip().strip("\"'“”「」")
                if not cand or "==" in cand or len(cand) > len(text) * 2:
                    continue
                score = difflib.SequenceMatcher(None, text, cand).ratio()
                if score > best_score:
                    start = offset + cuts[i] + piece.index(cand)
                    best, best_score = (start, start + len(cand)), score
        offset += len(line) + 1
    return best


def _pairs(body: str) -> list[tuple[str, int, int]]:
    """双语正文拆成 (原文段落, 译文起点, 译文终点)：原文段落后面紧跟的引用块就是它的译文。"""
    blocks = [(m.start(), m.end(), m.group(0)) for m in re.finditer(r"(?:[^\n]*\S[^\n]*(?:\n|$))+", body)]
    out = []
    for k, (_, _, blk) in enumerate(blocks[:-1]):
        a, b, nxt = blocks[k + 1]
        if not blk.startswith(">") and nxt.startswith(">"):
            out.append((blk, a, b))
    return out


def _translation_span(old_body: str, new_body: str, text: str) -> Optional[tuple[int, int]]:
    """旧高亮在哪段原文的译文里 → 新正文里对应的原文段落（原文重新整理后变化很小）→ 在它的译文里找。"""
    old_pairs = _pairs(old_body)
    src = next((orig for orig, a, b in old_pairs if text in old_body[a:b].replace("==", "")), None)
    if not src:
        return None
    best, best_score = None, 0.6
    for orig, a, b in _pairs(new_body):
        sm = difflib.SequenceMatcher(None, src[:400], orig[:400])
        if sm.quick_ratio() > best_score and sm.ratio() > best_score:
            best, best_score = (a, b), sm.ratio()
    return _closest_span(new_body, text, threshold=0.35, within=best) if best else None


def reapply_highlights(body: str, highlights: list[str],
                       old_body: str = "") -> tuple[str, dict[str, str], list[str]]:
    """把旧高亮标回新正文。同样的文字还在就标原处；措辞变了就标最像的那一句
    （双语稿先按原文段落对上位置，再在那段译文里找）。
    返回 (新正文, {旧文字: 新文字}（措辞变了的）, 没标上的)。"""
    moved: dict[str, str] = {}
    missed = []
    for text in highlights:
        i = body.find(text)
        while i >= 0 and body[max(0, i - 2):i] == "==" and body[i + len(text):i + len(text) + 2] == "==":
            i = body.find(text, i + len(text))   # 已经标过（同一句高亮了两次）
        if i >= 0:
            body = f"{body[:i]}=={text}=={body[i + len(text):]}"
            continue
        span = (_translation_span(old_body, body, text) if old_body else None) or _closest_span(body, text)
        if not span:
            missed.append(text)
            continue
        a, b = span
        moved[text] = body[a:b]
        body = f"{body[:a]}=={body[a:b]}=={body[b:]}"
    return body, moved, missed


def _update_note_highlights(note_path: str, moved: dict[str, str]) -> None:
    """笔记末尾「我的高亮」里记的是高亮原文，措辞变了的换成新的，阅读页取消高亮时才对得上。"""
    if not moved or not os.path.exists(note_path):
        return
    with open(note_path, encoding="utf-8") as f:
        text = f.read()
    m = core_vault.HIGHLIGHTS_SECTION_RE.search(text)
    if not m:
        return
    section = m.group(0)
    for old, new in moved.items():
        section = section.replace(f"- {old}（[整理稿]", f"- {new}（[整理稿]", 1)
    text = text[:m.start()] + section + text[m.end():]
    tmp = note_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, note_path)


def redo(item: dict, backend: str, model: str, api_key: str = "", api_base: str = "",
         log=print) -> dict:
    title = item["row"]["entry"]["title"]
    entry = item["row"]["entry"]
    old_highlights = _HIGHLIGHT_RE.findall(item["body"])
    started = time.monotonic()
    text, mode_used = pipeline.generate_speech_script(
        entry, item["paragraphs"], item["speakers"], item["speaker_mode"], item["lang"], item["mode"],
        backend, api_key, model, api_base, cache_dir=pipeline.llm_cache_dir(item["show_dir"]),
    )
    if mode_used != item["mode"]:
        raise pipeline.SummarizeError(f"翻译没做成（只拿到了{mode_used}），没有改文件")
    new_body, moved, missed = reapply_highlights(text.strip(), old_highlights, item["body"])
    cov = coverage(new_body, item["paragraphs"], item["mode"], item["lang"])
    if cov < _threshold(item["mode"], item["lang"]) or cov <= item["coverage"]:
        raise pipeline.SummarizeError(f"重做后仍不完整（覆盖 {cov:.0%}），没有改文件")

    backup_dir = os.path.join(item["show_dir"], ".cache", "speech_backup")
    os.makedirs(backup_dir, exist_ok=True)
    shutil.copy2(item["speech_path"], os.path.join(backup_dir, os.path.basename(item["speech_path"])))
    content = item["head"] + "\n\n" + new_body + "\n"
    content = core_vault.keep_highlights_section(item["speech"], content)
    tmp = item["speech_path"] + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, item["speech_path"])
    note_rel = item["row"].get("note_relative_path")
    if note_rel:
        _update_note_highlights(os.path.join(item["show_dir"], note_rel), moved)
    result = {"title": title, "before": item["coverage"], "after": cov, "moved_highlights": moved, "missed_highlights": missed,
              "seconds": round(time.monotonic() - started)}
    log(f"✅ {title}：{item['coverage']:.0%} → {cov:.0%}（{result['seconds']} 秒）"
        + (f"，{len(moved)} 处高亮措辞变了、标在最像的句子上" if moved else "")
        + (f"，{len(missed)} 处高亮没能标回去" if missed else ""))
    return result


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="重做被截断的整理稿")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--scan", action="store_true", help="只列出被截断的")
    g.add_argument("--all", action="store_true", help="重做全部被截断的")
    g.add_argument("--file", help="重做这一篇（speech/ 里的文件路径）")
    p.add_argument("--backend", default="cli")
    p.add_argument("--model", default="sonnet")
    p.add_argument("--jobs", type=int, default=2, help="同时重做几篇")
    p.add_argument("--since", default="", help="只做这天及以后播出的，如 20260101（没有日期的大会录像照做）")
    p.add_argument("--fallback", default="", help="额度用完时换用的后端，如 api（Anthropic API Key）")
    p.add_argument("--fallback-model", default="claude-sonnet-5")
    p.add_argument("--skip-show", action="append", default=[], help="跳过这个节目/大会文件夹（可以写多次）")
    args = p.parse_args(argv)

    items = scan(core_vault.spark_dir())
    if args.file:
        target = os.path.realpath(args.file)
        items = [i for i in items if os.path.realpath(i["speech_path"]) == target]
        if not items:
            print("找不到这篇整理稿（要是 speech/ 里、manifest 里记着的文件）", file=sys.stderr)
            return 1
    else:
        items = [i for i in items if i["truncated"]]
        if args.skip_show:
            items = [i for i in items if os.path.basename(i["show_dir"]) not in args.skip_show]
        if args.since:
            items = [i for i in items if (episode_date(i) or "99999999") >= args.since.replace("-", "")]
        items.sort(key=lambda i: -os.path.getmtime(i["speech_path"]))
    if args.scan:
        for i in items:
            print(f"{i['coverage']:.0%}\t{os.path.relpath(i['speech_path'], core_vault.spark_dir())}")
        print(f"共 {len(items)} 篇")
        return 0

    failed = []
    limit_hit = threading.Event()
    lock = threading.Lock()
    use = {"backend": args.backend, "model": args.model, "api_key": ""}

    def switch_to_fallback(from_backend: str) -> bool:
        """额度用完时换到 --fallback 指定的后端。别的线程已经换过了也算成功。"""
        with lock:
            if use["backend"] != from_backend:
                return True
            if not args.fallback or args.fallback == from_backend:
                return False
            try:
                cfg = llm_config.resolve({"backend": args.fallback, "model": args.fallback_model},
                                         default_backend=args.fallback)
            except llm_config.ConfigError as e:
                print(f"⛔ 换不了 {args.fallback}：{e}", flush=True)
                return False
            use.update(backend=cfg["backend"], model=cfg["model"] or args.fallback_model,
                       api_key=cfg["api_key"])
            print(f"🔁 {from_backend} 额度用完了，后面改用 {use['backend']} / {use['model']} 继续", flush=True)
            return True

    def run(item):
        while not limit_hit.is_set():
            backend, model, api_key = use["backend"], use["model"], use["api_key"]
            try:
                return redo(item, backend, model, api_key=api_key)
            except Exception as e:  # noqa: BLE001  一篇失败不影响别的
                if USAGE_LIMIT_RE.search(str(e)):
                    if switch_to_fallback(backend):
                        continue   # 换了后端，这一篇重来
                    if not limit_hit.is_set():
                        # 额度用完了，剩下的每一篇都会一样失败：停下来，额度恢复后再跑一次 --all 接着做
                        limit_hit.set()
                        print("⛔ 模型额度用完了，先停在这里；恢复后再运行 --all 会接着做剩下的", flush=True)
                    return None
                failed.append(item["speech_path"])
                print(f"⚠️ {item['row']['entry']['title']}：{e}", flush=True)
                return None
        return None

    print(f"要重做 {len(items)} 篇（{args.backend} / {args.model}，同时 {args.jobs} 篇）", flush=True)
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as ex:
        done = [r for r in ex.map(run, items) if r]
    left = len(items) - len(done) - len(failed)
    print(f"完成 {len(done)} 篇，失败 {len(failed)} 篇" + (f"，没做 {left} 篇" if left else ""), flush=True)
    for path in failed:
        print(f"  失败：{path}")
    return 0 if not failed else 2


if __name__ == "__main__":
    sys.exit(main())
