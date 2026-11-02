"""补翻中英对照整理稿里没有译文的段落，别的都不动。

翻译是一批一批交给模型的，模型有时把一整批当成「复制受版权保护的全文」拒绝翻译，
之前补译也是同一批原样重试、照样被拒，这批段落就只剩英文（2026-11 查出 53 期）。
pipeline 里补译已经改成拆小批重试；这里给已经生成好的整理稿补上：

- 只看 speech/ 里的对照稿（正文里有 > 译文的），找出后面没跟着译文的外文段落；
- 只把这些段落交给模型翻译，译文插在原段落后面，原文、已有译文、高亮、「我的高亮」都不动；
- 改之前把旧文件备份到节目文件夹的 .cache/speech_backup/。

命令行：
    python3 -m apps.summit2md.fill_translations --scan            # 只列出缺译文的整理稿和段数
    python3 -m apps.summit2md.fill_translations --file <speech.md>
    python3 -m apps.summit2md.fill_translations --all
默认用设置页里选的后端和模型（--backend / --model 可改）。
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from typing import Optional

from . import pipeline
from core import llm_config
from core import vault as core_vault

SPEECH_HEADING = "## 演讲稿"
_HIGHLIGHT_MARK_RE = re.compile(r"==([^=\n]+?)==")


def _paragraph_spans(text: str, start: int, end: int) -> list[tuple[int, int]]:
    """text[start:end] 里各段（空行隔开）的起止位置。"""
    spans, pos = [], start
    for m in re.finditer(r"\n[ \t]*\n+", text[start:end]):
        spans.append((pos, start + m.start()))
        pos = start + m.end()
    spans.append((pos, end))
    return [(a, b) for a, b in spans if text[a:b].strip()]


def missing_spans(text: str) -> list[tuple[int, int]]:
    """「## 演讲稿」下面后面没跟着译文的外文段落。不是对照稿（正文里一条译文都没有）返回空。"""
    i = text.find(SPEECH_HEADING)
    if i < 0:
        return []
    start = text.find("\n", i) + 1
    nxt = re.search(r"^## ", text[start:], re.M)
    end = start + nxt.start() if nxt else len(text)
    spans = _paragraph_spans(text, start, end)
    paras = [text[a:b].strip() for a, b in spans]
    if not any(p.startswith(">") for p in paras):
        return []
    out = []
    for k, p in enumerate(paras):
        if p.startswith((">", "#")) or not re.search(r"[A-Za-z]{3,}", p) or re.search(r"[一-鿿]", p):
            continue
        if k + 1 >= len(paras) or not paras[k + 1].startswith(">"):
            out.append(spans[k])
    return out


def scan(spark_dir: str) -> list[tuple[str, int]]:
    found = []
    for dp, dirs, files in os.walk(spark_dir):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d != "logs")
        if os.path.basename(dp) != "speech":
            continue
        for f in sorted(files):
            if not f.endswith(".md"):
                continue
            path = os.path.join(dp, f)
            with open(path, encoding="utf-8") as fh:
                n = len(missing_spans(fh.read()))
            if n:
                found.append((path, n))
    return found


def fill(path: str, backend: str, model: str, api_key: str = "", api_base: str = "", log=print) -> int:
    """补翻一篇，返回补上的段数。"""
    with open(path, encoding="utf-8") as f:
        text = f.read()
    spans = missing_spans(text)
    if not spans:
        return 0
    show_dir = os.path.dirname(os.path.dirname(path))
    originals = [text[a:b].strip() for a, b in spans]
    translations = pipeline._translate_speech_paragraphs(
        [_HIGHLIGHT_MARK_RE.sub(r"\1", p) for p in originals], pipeline._lang_display_name("en"),
        backend, api_key, model, api_base, cache_dir=pipeline.llm_cache_dir(show_dir))
    done = 0
    for k in range(len(spans) - 1, -1, -1):   # 从后往前插，前面的位置不受影响
        block = pipeline.translation_block(originals[k], translations.get(k + 1) or "")
        if block:
            text = text[:spans[k][1]] + "\n\n" + block + text[spans[k][1]:]
            done += 1
    if not done:
        log(f"⚠️ {os.path.basename(path)}：{len(spans)} 段一段也没译出来，没有改文件")
        return 0
    backup_dir = os.path.join(show_dir, ".cache", "speech_backup")
    os.makedirs(backup_dir, exist_ok=True)
    shutil.copy2(path, os.path.join(backup_dir, os.path.basename(path)))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)
    left = len(spans) - done
    log(f"✅ {os.path.basename(path)}：补上 {done} 段" + (f"，还有 {left} 段没译出来" if left else ""))
    return done


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="补翻对照整理稿里没有译文的段落")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--scan", action="store_true", help="只列出缺译文的")
    g.add_argument("--all", action="store_true", help="补翻全部缺译文的")
    g.add_argument("--file", help="补翻这一篇（speech/ 里的文件路径）")
    p.add_argument("--backend", default="", help="默认跟设置页一样")
    p.add_argument("--model", default=None, help="默认跟设置页一样")
    args = p.parse_args(argv)

    if args.file:
        items = [(args.file, len(missing_spans(open(args.file, encoding="utf-8").read())))]
    else:
        items = scan(core_vault.spark_dir())
    if args.scan:
        for path, n in items:
            print(f"{n}\t{os.path.relpath(path, core_vault.spark_dir())}")
        print(f"共 {len(items)} 篇，{sum(n for _, n in items)} 段")
        return 0

    data = {"backend": args.backend} if args.backend else {}
    if args.model is not None:
        data["model"] = args.model
    try:
        cfg = llm_config.resolve(data, default_backend="api")
    except llm_config.ConfigError as e:
        print(f"⛔ {e}", file=sys.stderr)
        return 1
    print(f"要补翻 {len(items)} 篇、{sum(n for _, n in items)} 段（{cfg['backend']} / {cfg['model']}）", flush=True)
    failed = []
    for path, _ in items:
        try:
            fill(path, cfg["backend"], cfg["model"], api_key=cfg["api_key"], api_base=cfg["api_base"])
        except Exception as e:  # noqa: BLE001  一篇失败不影响别的
            failed.append(path)
            print(f"⚠️ {os.path.basename(path)}：{e}", flush=True)
    print(f"完成，失败 {len(failed)} 篇", flush=True)
    return 0 if not failed else 2


if __name__ == "__main__":
    sys.exit(main())
