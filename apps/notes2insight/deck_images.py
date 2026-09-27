"""
给演示配图：通过 OpenRouter 的图片生成接口，给封面和章节分隔页各画一张不带文字的插图。

- 默认关，要在「生成演示」旁边明确勾选；勾选前就把张数和大概花多少钱摆出来。
- 只挑少数几页：封面 + 章节分隔页，最多 MAX_IMAGES 张。要点页不配图——那些页的
  价值在文字和来源角标上，配图只会挤占位置。
- 图片存成演示旁边的文件（x.deck.assets/slide-3.png），演示里用相对路径引用：
  从阅读页打开、从 Finder 双击打开都能显示。
- 某张图失败不影响演示本身：那一页就不带图，结果里列出哪几张没成。
- Key 只用本机存的 OpenRouter Key（环境变量 / ~/.spark/keys），只发给官方地址；
  不写日志、不进任务记录、不出现在任何报错里。

接口形状（2026-09 查的 OpenRouter 文档 https://openrouter.ai/docs/features/multimodal/image-generation）：
  POST https://openrouter.ai/api/v1/images
  {"model": "...", "prompt": "...", "n": 1, "aspect_ratio": "16:9"}
  → {"data": [{"b64_json": "<base64>", "media_type": "image/png"}], "usage": {"cost": 0.04, ...}}
"""

from __future__ import annotations

import base64
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

from core import keys
from core.llm import OPENROUTER_API_BASE, ssl_context

IMAGES_ENDPOINT = OPENROUTER_API_BASE + "/images"
MAX_IMAGES = 6
# 默认模型：Gemini 3.1 Flash Lite Image。支持 16:9，出图约 1K，按输出 token 计费
# （OpenRouter /api/v1/images/models/google/gemini-3.1-flash-lite-image/endpoints：
# output_image $0.00003/token，一张 1K 图约 1290 token ≈ $0.039）。
DEFAULT_MODEL = os.environ.get("SPARK_DECK_IMAGE_MODEL", "").strip() or "google/gemini-3.1-flash-lite-image"
# 每张图的大概价格（美元），只用来在勾选前估算；实际花费以 OpenRouter 返回的 usage.cost 为准。
# 数字来自各模型的 endpoints 定价 × 一张约 1K 图的输出 token 数，列表外的模型显示"单价未知"。
PRICE_PER_IMAGE = {
    "google/gemini-3.1-flash-lite-image": 0.039,
    "google/gemini-2.5-flash-image": 0.039,
    "google/gemini-3.1-flash-image": 0.078,
    "openai/gpt-image-1-mini": 0.011,
}
# 只接受这几种图片格式；按文件头认，不信接口自报的 media_type
_MAGIC = ((b"\x89PNG\r\n\x1a\n", "png"), (b"\xff\xd8\xff", "jpg"))
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp")
_MODEL_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._:-]+$")


class ImageError(RuntimeError):
    pass


def assets_dirname(deck_filename: str) -> str:
    """x.deck.html → x.deck.assets（跟演示放在同一个目录里）。"""
    stem = deck_filename[:-len(".html")] if deck_filename.endswith(".html") else deck_filename
    return stem + ".assets"


def clean_model(model: str) -> str:
    """前端传来的模型名只收「厂商/模型」这种形状，别的一律回默认，免得拼出奇怪的请求。"""
    model = (model or "").strip()
    return model if _MODEL_RE.match(model) else DEFAULT_MODEL


def planned_pages(deck: dict) -> list[int]:
    """要配图的页（在最终页序里的下标：0 是封面，deck["slides"][i] 是 i+1）。
    封面 + 章节分隔页，封顶 MAX_IMAGES。"""
    pages = [0]
    for i, s in enumerate(deck.get("slides") or []):
        if s.get("kind") == "section":
            pages.append(i + 1)
    return pages[:MAX_IMAGES]


def estimate(chapter_count: int, model: str) -> dict:
    """勾选前的估算：还没排版，按报告的章数算（每章会有一页章节分隔页）。"""
    count = min(MAX_IMAGES, 1 + max(int(chapter_count or 0), 0))
    price = PRICE_PER_IMAGE.get(model)
    return {"count": count, "model": model, "per_image": price,
            "total": round(price * count, 2) if price is not None else None}


def _prompt(title: str, lead: str, deck_title: str, is_cover: bool) -> str:
    # 标题只拿来传达意思，不能用引号当成"图的标题"给出去：模型会把这几个字画进图里，
    # 中文还常画错字。幻灯片上本来就有标题，图里只要画面。
    topic = title if not lead else f"{title}；{lead}"
    role = ("the cover of a presentation" if is_cover
            else "a chapter divider slide in a presentation")
    return (
        f"A clean, minimal editorial illustration used as the background visual for {role}. "
        f"The idea it should evoke (for meaning only, never write these words in the image): {topic}. "
        "Style: abstract, diagram-like shapes and simple geometric forms, flat vector look, "
        "deep navy background (#0f1115) with soft blue accents (#6aa9ff) and a little warm orange, "
        "plenty of empty space, calm and professional. "
        "The image must contain no text of any kind: no titles, no words, no Chinese characters, "
        "no letters, no numbers, no captions, no labels, no logos, no watermarks, no UI. "
        "Wide 16:9 composition."
    )


def _decode(payload: dict) -> tuple[bytes, str]:
    try:
        item = (payload.get("data") or [])[0]
    except (AttributeError, IndexError):
        raise ImageError("接口没有返回图片") from None
    raw = ""
    if isinstance(item, dict):
        raw = item.get("b64_json") or ""
        url = item.get("url") or ""
        if not raw and url.startswith("data:image/") and ";base64," in url:
            raw = url.split(";base64,", 1)[1]
    if not raw:
        raise ImageError("接口没有返回图片数据")
    try:
        data = base64.b64decode(raw, validate=False)
    except (ValueError, TypeError) as e:
        raise ImageError(f"图片数据解不开：{e}") from e
    for magic, ext in _MAGIC:
        if data.startswith(magic):
            return data, ext
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return data, "webp"
    raise ImageError("返回的不是 PNG/JPEG/WebP 图片")


def request_image(prompt: str, *, model: str, api_key: str, timeout: int = 120) -> tuple[bytes, str, float]:
    """调一次 OpenRouter 出一张图，返回 (图片字节, 扩展名, 实际花费美元)。
    只发到官方地址；报错信息里只带状态码和对方返回的说明，不带请求头。"""
    body = {"model": model, "prompt": prompt, "n": 1, "aspect_ratio": "16:9"}
    req = urllib.request.Request(
        IMAGES_ENDPOINT, data=json.dumps(body, ensure_ascii=False).encode("utf-8"), method="POST",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
                 "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ssl_context()) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:300]
        raise ImageError(f"OpenRouter 返回 HTTP {e.code}：{detail}") from None
    except urllib.error.URLError as e:
        raise ImageError(f"连不上 OpenRouter：{e.reason}") from None
    except (TimeoutError, json.JSONDecodeError, UnicodeDecodeError) as e:
        raise ImageError(f"OpenRouter 响应异常：{e}") from None
    if isinstance(payload, dict) and payload.get("error"):
        err = payload["error"]
        msg = err.get("message") if isinstance(err, dict) else str(err)
        raise ImageError(f"OpenRouter 报错：{str(msg)[:300]}")
    data, ext = _decode(payload if isinstance(payload, dict) else {})
    cost = 0.0
    try:
        cost = float(((payload.get("usage") or {}).get("cost")) or 0)
    except (TypeError, ValueError):
        pass
    return data, ext, cost


def _clear_old(assets_dir: str) -> None:
    """重新配图前清掉上一次的 slide-N.*，免得新旧混在一起、引用到旧图。只删我们自己起名的文件。"""
    if not os.path.isdir(assets_dir):
        return
    for name in os.listdir(assets_dir):
        if re.fullmatch(r"slide-\d+\.(png|jpg|webp)", name):
            try:
                os.remove(os.path.join(assets_dir, name))
            except OSError:
                pass


def add_images(deck: dict, deck_path: str, *, model: str = "", api_key: str = "",
               progress=None, stop_flag=None, timeout: int = 120) -> dict:
    """给 deck 里挑出来的几页配图：图片写进 deck_path 旁边的 .deck.assets/，
    在对应页（封面记在 deck["cover_image"]）写上相对路径。单张失败只记下来，不抛。

    返回 {"model", "planned", "generated": [{"slide", "file"}], "failed": [{"slide", "title", "error"}],
          "cost", "assets_dir"}。"""
    model = clean_model(model)
    api_key = api_key or keys.resolve("openrouter", "")
    pages = planned_pages(deck)
    folder = assets_dirname(os.path.basename(deck_path))
    assets_dir = os.path.join(os.path.dirname(deck_path), folder)
    out = {"model": model, "planned": len(pages), "generated": [], "failed": [], "cost": 0.0,
           "assets_dir": assets_dir}
    deck.pop("cover_image", None)
    for s in deck.get("slides") or []:
        s.pop("image", None)
    if not pages:
        return out
    if not api_key:
        for p in pages:
            out["failed"].append({"slide": p + 1, "title": _page_title(deck, p),
                                  "error": "没有找到 OpenRouter Key（环境变量 OPENROUTER_API_KEY 或 ~/.spark/keys/openrouter.key）"})
        return out

    os.makedirs(assets_dir, exist_ok=True)
    _clear_old(assets_dir)
    title = deck.get("title") or ""
    for n, p in enumerate(pages, 1):
        slide_title = _page_title(deck, p)
        if stop_flag and stop_flag():
            out["failed"].append({"slide": p + 1, "title": slide_title, "error": "点了停止，这张没有生成"})
            continue
        if progress:
            progress(n, len(pages), slide_title)
        if p == 0:
            prompt = _prompt(title, deck.get("subtitle", ""), title, True)
        else:
            s = deck["slides"][p - 1]
            prompt = _prompt(s.get("title", ""), s.get("lead", ""), title, False)
        try:
            data, ext, cost = request_image(prompt, model=model, api_key=api_key, timeout=timeout)
        except ImageError as e:
            out["failed"].append({"slide": p + 1, "title": slide_title, "error": str(e)})
            continue
        except Exception as e:  # noqa: BLE001 —— 配图是锦上添花，任何意外都不能拖垮演示
            out["failed"].append({"slide": p + 1, "title": slide_title, "error": f"意外错误：{type(e).__name__}"})
            continue
        name = f"slide-{p + 1}.{ext}"
        try:
            with open(os.path.join(assets_dir, name), "wb") as f:
                f.write(data)
        except OSError as e:
            out["failed"].append({"slide": p + 1, "title": slide_title, "error": f"写文件失败：{e}"})
            continue
        # 相对演示文件的路径；文件夹名可能有中文、空格，按 URL 编码写进 src
        rel = urllib.parse.quote(f"{folder}/{name}")
        if p == 0:
            deck["cover_image"] = rel
        else:
            deck["slides"][p - 1]["image"] = rel
        out["generated"].append({"slide": p + 1, "file": name})
        out["cost"] += cost
    out["cost"] = round(out["cost"], 4)
    return out


def _page_title(deck: dict, p: int) -> str:
    if p == 0:
        return "封面"
    s = (deck.get("slides") or [])[p - 1]
    return s.get("title", "") or f"第 {p + 1} 页"
