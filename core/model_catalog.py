"""
模型目录：各个页面「模型」下拉框里列哪些 Anthropic 模型。

Anthropic 隔几个月就出新模型，写死在前端的列表很快就旧了。这里：
  - 有 Anthropic Key 时从 /v1/models 拉官方列表；OpenRouter 的模型列表是公开的，不用 Key
  - 结果缓存在设置文件旁边的 model_catalog.json，一天刷新一次（spark.py 起一个后台线程定时刷）
  - 拉不到（没网、没 Key）就用缓存；缓存也没有就用下面内置的列表

每个系列（Fable / Opus / Sonnet / Haiku）最新的一版排在前面，旧版本跟在后面，
用户之前选过的旧模型 id 仍在列表里，不会因为列表更新被顶掉。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from typing import Callable, Optional

from core import atomic
from core import keys as core_keys
from core import settings as core_settings

REFRESH_SECONDS = 24 * 3600
FETCH_TIMEOUT = 10
# 旧版本最多列几个；再老的（4.5 以前）不列
MAX_OLDER = 12
MIN_VERSION = (4, 5)

ANTHROPIC_MODELS_URL = "https://api.anthropic.com/v1/models?limit=100"
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"

# 系列：排序、说明、是否算"省钱档"
FAMILIES = {
    "sonnet": {"order": 0, "desc": "质量与速度均衡，推荐", "cheap": False},
    "opus":   {"order": 1, "desc": "更强，更慢更贵", "cheap": False},
    "fable":  {"order": 2, "desc": "最强，最慢最贵", "cheap": False},
    "haiku":  {"order": 3, "desc": "更快更省", "cheap": True},
}

# 内置快照（2026-10）：没网也没缓存时用。(id, 发布日期)
BUILTIN_API = [
    ("claude-haiku-5-5", "2026-10-07"), ("claude-sonnet-5-5", "2026-09-28"),
    ("claude-opus-5-5", "2026-09-21"), ("claude-fable-5-1", "2026-08-28"),
    ("claude-opus-5", "2026-07-24"), ("claude-sonnet-5", "2026-06-29"),
    ("claude-fable-5", "2026-06-07"), ("claude-haiku-4-5-20251001", "2025-10-15"),
]
BUILTIN_OPENROUTER = [
    ("anthropic/claude-haiku-5.5", "2026-10-07"), ("anthropic/claude-sonnet-5.5", "2026-09-28"),
    ("anthropic/claude-opus-5.5", "2026-09-21"), ("anthropic/claude-fable-5.1", "2026-08-28"),
    ("anthropic/claude-opus-5", "2026-07-24"), ("anthropic/claude-sonnet-5", "2026-06-29"),
    ("anthropic/claude-haiku-4.5", "2025-10-15"),
]
# OpenRouter 的「~厂商/系列-latest」别名会自动跟随该系列最新版，列在 OpenRouter 选项最前面
OPENROUTER_LATEST = {
    "sonnet": "~anthropic/claude-sonnet-latest", "opus": "~anthropic/claude-opus-latest",
    "fable": "~anthropic/claude-fable-latest", "haiku": "~anthropic/claude-haiku-latest",
}

_RE_ID = re.compile(r"claude-(fable|opus|sonnet|haiku)-(\d+)(?:[-.](\d{1,2}))?(?:-(\d{8}))?$")

_LOCK = threading.Lock()
_REFRESH_LOCK = threading.Lock()


def cache_path() -> str:
    # 跟设置文件放一起：测试把 SPARK_SETTINGS_FILE 指到临时目录，缓存也跟着走
    return os.path.join(os.path.dirname(core_settings.settings_path()), "model_catalog.json")


def parse_id(model_id: str) -> Optional[tuple[str, tuple[int, int]]]:
    """claude-opus-5-5 / anthropic/claude-haiku-4.5 / claude-haiku-4-5-20251001 → (系列, 版本)。"""
    m = _RE_ID.search(model_id or "")
    if not m:
        return None
    return m.group(1), (int(m.group(2)), int(m.group(3) or 0))


def _version_text(v: tuple[int, int]) -> str:
    return f"{v[0]}.{v[1]}" if v[1] else str(v[0])


def build_options(models: list[tuple[str, str]], *, latest_aliases: bool = False) -> list[dict]:
    """(id, 发布日期) 列表 → 下拉框选项 [{v, t, cheap}]：各系列最新版在前，旧版在后。"""
    rows = []
    for mid, created in models:
        p = parse_id(mid)
        if not p or p[1] < MIN_VERSION or mid.endswith(":batch"):
            continue
        rows.append({"id": mid, "family": p[0], "version": p[1], "created": created or ""})
    newest: dict[str, dict] = {}
    for r in rows:
        cur = newest.get(r["family"])
        if cur is None or (r["version"], r["created"]) > (cur["version"], cur["created"]):
            newest[r["family"]] = r

    out = []
    for fam in sorted(newest, key=lambda f: FAMILIES[f]["order"]):
        info = FAMILIES[fam]
        if latest_aliases:
            out.append({"v": OPENROUTER_LATEST[fam],
                        "t": f"{OPENROUTER_LATEST[fam]} — 自动跟随 {fam.capitalize()} 最新版，{info['desc']}",
                        "cheap": info["cheap"]})
        r = newest[fam]
        out.append({"v": r["id"], "t": f"{r['id']} — {fam.capitalize()} {_version_text(r['version'])}，{info['desc']}",
                    "cheap": info["cheap"]})
    older = sorted((r for r in rows if newest[r["family"]] is not r),
                   key=lambda r: (r["created"], r["version"]), reverse=True)[:MAX_OLDER]
    for r in older:
        out.append({"v": r["id"], "t": f"{r['id']} — {r['family'].capitalize()} {_version_text(r['version'])}（旧版）",
                    "cheap": FAMILIES[r["family"]]["cheap"]})
    return out


def _get_json(url: str, headers: dict) -> dict:
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
        return json.loads(resp.read(4 << 20).decode("utf-8"))


def fetch_anthropic(api_key: str, get_json: Optional[Callable] = None) -> list[tuple[str, str]]:
    data = (get_json or _get_json)(ANTHROPIC_MODELS_URL, {"x-api-key": api_key, "anthropic-version": "2023-06-01"})
    out = []
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    for m in data.get("data") or []:
        if not isinstance(m, dict) or not m.get("id"):
            continue
        if m.get("retires_at") and str(m["retires_at"]) <= now:
            continue   # 已经下线的不列
        out.append((str(m["id"]), str(m.get("created_at") or "")[:10]))
    return out


def fetch_openrouter(get_json: Optional[Callable] = None) -> list[tuple[str, str]]:
    data = (get_json or _get_json)(OPENROUTER_MODELS_URL, {})
    out = []
    for m in data.get("data") or []:
        mid = str((m or {}).get("id") or "")
        if mid.startswith("anthropic/") and not mid.endswith(":batch"):
            created = m.get("created")
            day = time.strftime("%Y-%m-%d", time.gmtime(created)) if isinstance(created, (int, float)) else ""
            out.append((mid, day))
    return out


def _read_cache() -> dict:
    try:
        with open(cache_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_cache(data: dict) -> None:
    try:
        os.makedirs(os.path.dirname(cache_path()), exist_ok=True)
        atomic.write_json(cache_path(), data)
    except OSError:
        pass


def refresh(*, api_key: str = "", get_json: Optional[Callable] = None) -> dict:
    """联网拉一次，成功的部分写进缓存；失败的部分保留缓存里的旧数据。返回新的缓存内容。"""
    with _REFRESH_LOCK:
        cache = _read_cache()
        errors = {}
        key = api_key or core_keys.resolve("anthropic")
        if key:
            try:
                cache["api"] = fetch_anthropic(key, get_json)
                cache["api_at"] = time.time()
            except (urllib.error.URLError, OSError, ValueError, AttributeError) as e:
                errors["api"] = str(e)[:200]
        else:
            errors["api"] = "没有 Anthropic Key，用内置列表"
        try:
            cache["openrouter"] = fetch_openrouter(get_json)
            cache["openrouter_at"] = time.time()
        except (urllib.error.URLError, OSError, ValueError, AttributeError) as e:
            errors["openrouter"] = str(e)[:200]
        cache["checked_at"] = time.time()
        cache["errors"] = errors
        with _LOCK:
            _write_cache(cache)
        return cache


def catalog(*, max_age: float = REFRESH_SECONDS, allow_fetch: bool = True,
            get_json: Optional[Callable] = None) -> dict:
    """给前端的目录。缓存超过 max_age 没检查过就先刷新一次（allow_fetch=False 时不联网）。"""
    cache = _read_cache()
    if allow_fetch and time.time() - float(cache.get("checked_at") or 0) > max_age:
        cache = refresh(get_json=get_json)
    api = [tuple(x) for x in cache.get("api") or []] or BUILTIN_API
    orr = [tuple(x) for x in cache.get("openrouter") or []] or BUILTIN_OPENROUTER
    return {
        "api": build_options(api),
        "openrouter": build_options(orr, latest_aliases=True),
        "source": {"api": "live" if cache.get("api") else "builtin",
                   "openrouter": "live" if cache.get("openrouter") else "builtin"},
        "updated_at": {"api": cache.get("api_at"), "openrouter": cache.get("openrouter_at")},
        "checked_at": cache.get("checked_at"),
        "errors": cache.get("errors") or {},
    }


def start_background_refresh(interval: float = REFRESH_SECONDS) -> threading.Thread:
    """后台定时刷新（spark.py 启动时调用）：先等一会儿再刷，不跟启动抢网络。"""
    def loop():
        time.sleep(5)
        while True:
            try:
                cache = _read_cache()
                if time.time() - float(cache.get("checked_at") or 0) > interval - 60:
                    refresh()
            except Exception:
                pass
            time.sleep(min(interval, 3600))
    t = threading.Thread(target=loop, name="model-catalog-refresh", daemon=True)
    t.start()
    return t
