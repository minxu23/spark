"""
剪藏：手机上分享进来的文章，存在笔记库 Spark/剪藏/ 下，一篇一个 .md，图片在 Spark/剪藏/attachments/。

手机（apps/mobile-ios/Spark/Clips.swift）抓全文、写摘要和标签，交一个 save 任务过来（core/remote_tasks.py）；
这里负责：按原地址把图片下到 attachments/（任务里只带地址，GitHub 的 Issue 放不下图片），再重写一遍索引。
索引按文件头里的标签归组，跟手机上的写法一致：两篇以上用到的标签成一组，剩下的放「其他」，组里新存的在前。
"""

from __future__ import annotations

import json
import os
import re

from core import atomic
from core import vault

CLIPS_DIR = f"{vault.SPARK_DIRNAME}/剪藏"
INDEX_NAME = "索引.md"
MAX_IMAGES = 80
MAX_IMAGE_BYTES = 8 * 1024 * 1024
_RE_IMAGE_KEY = re.compile(rf"^{re.escape(CLIPS_DIR)}/attachments/[0-9a-f]{{16}}\.(?:jpg|png|gif|webp)$")


def is_clip_path(path: str) -> bool:
    """Spark/剪藏/ 下直接的一篇 .md（不含索引、不在子文件夹里）。"""
    if not path.startswith(CLIPS_DIR + "/") or not path.endswith(".md"):
        return False
    rest = path[len(CLIPS_DIR) + 1:]
    return "/" not in rest and rest != INDEX_NAME and not rest.startswith(".")


def image_key_ok(key: str, url: str) -> bool:
    return bool(isinstance(key, str) and _RE_IMAGE_KEY.match(key)
                and isinstance(url, str) and re.match(r"^https?://[^\s/]+\S*$", url) and len(url) <= 2000)


def download_images(images: dict, root: str = "") -> tuple[int, int]:
    """把还没有的图片按原地址下下来。返回 (下好的, 没下成的)。"""
    from core import sources   # 抓网页那一套（带防内网地址的检查），用到时再导入
    root = root or vault.vault_root()
    ok = bad = 0
    for key, url in list(images.items())[:MAX_IMAGES]:
        full = os.path.join(root, *key.split("/"))
        if os.path.exists(full):
            continue
        try:
            data = sources.http_get(url, timeout=20, max_bytes=MAX_IMAGE_BYTES)
        except Exception:  # noqa: BLE001  一张图下不来不影响文章
            bad += 1
            continue
        if not data:
            bad += 1
            continue
        os.makedirs(os.path.dirname(full), exist_ok=True)
        tmp = full + ".partial"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, full)
        ok += 1
    return ok, bad


# ---------------------------------------------------------------- 索引

def frontmatter(text: str) -> dict:
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}
    out = {}
    for line in text[4:end].splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        v = v.strip()
        if v.startswith('"'):
            try:
                v = json.loads(v)
            except ValueError:
                pass
        out[k.strip()] = v
    return out


def _tags(raw) -> list[str]:
    if isinstance(raw, list):
        return [str(t) for t in raw]
    try:
        val = json.loads(raw or "[]")
        if isinstance(val, list):
            return [str(t) for t in val]
    except ValueError:
        pass
    return [t.strip(" \"'") for t in str(raw).strip("[] ").split(",") if t.strip(" \"'")]


def load_clips(root: str = "") -> list[dict]:
    root = root or vault.vault_root()
    folder = os.path.join(root, *CLIPS_DIR.split("/"))
    out = []
    for name in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
        if not is_clip_path(f"{CLIPS_DIR}/{name}"):
            continue
        try:
            with open(os.path.join(folder, name), encoding="utf-8") as f:
                fm = frontmatter(f.read(8192))
        except OSError:
            continue
        stem = name[:-3]
        d = stem[:8]
        clipped = fm.get("clipped") or (f"{d[:4]}-{d[4:6]}-{d[6:]}" if re.fullmatch(r"\d{8}", d) else "")
        out.append({"name": name, "title": fm.get("title") or stem, "source": fm.get("source") or "",
                    "clipped": clipped, "tags": _tags(fm.get("tags")), "tldr": fm.get("tldr") or ""})
    return out


def render_index(clips: list[dict]) -> str:
    clips = sorted(clips, key=lambda c: (c["clipped"], c["name"]), reverse=True)
    count: dict[str, int] = {}
    for c in clips:
        for t in set(c["tags"]):
            count[t] = count.get(t, 0) + 1
    groups = [t for t, n in sorted(count.items(), key=lambda kv: (-kv[1], kv[0])) if n >= 2]
    lines = ["# 剪藏索引", "", f"共 {len(clips)} 篇。按主题归组，一篇可能出现在几个主题下；每组里新存的在前。", ""]

    def row(c: dict) -> str:
        src = "，".join(x for x in (c["source"], f"{c['clipped']} 存" if c["clipped"] else "") if x)
        title = c["title"].replace("]", "］")
        return f"- [{title}](<{c['name']}>)（{src}）" + (f"：{c['tldr']}" if c["tldr"] else "")

    for g in groups:
        members = [c for c in clips if g in c["tags"]]
        lines += [f"## {g}（{len(members)}）", ""] + [row(c) for c in members] + [""]
    rest = [c for c in clips if not any(t in groups for t in c["tags"])]
    if rest:
        lines += [f"## {'其他' if groups else '全部'}（{len(rest)}）", ""] + [row(c) for c in rest] + [""]
    return "\n".join(lines)


def rebuild_index(root: str = "") -> str:
    root = root or vault.vault_root()
    path = os.path.join(root, *CLIPS_DIR.split("/"), INDEX_NAME)
    atomic.write_text(path, render_index(load_clips(root)))
    return path
