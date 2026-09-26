"""
Spark 的全局设置：存储位置和 AI 默认值，一份文件、所有 app 共用。

以前每个 app 各记各的：笔记洞察把上次填的目录、后端、模型存在浏览器 localStorage，
Summit 的表单每次打开都从写死的默认值开始，信息跟进的小结篇幅又单独存了一份。
这里统一成服务端一个文件，设置页（/settings）读写它，各 app 的表单从这里取默认值。

文件位置：~/.spark/settings.json（跟 ~/.spark/keys 放在一起）。环境变量
SPARK_SETTINGS_FILE 可以指到别处——测试、临时起的第二个实例都靠它，免得碰到真设置。

取值的先后：任务/表单上明确填的值 > 这里保存的默认值 > 代码里的内置默认值。
环境变量（比如 SPARK_VAULT）在运行时仍然压过这里保存的值，设置页会标出来。
任务上临时改的值只作用于那一次任务，不会写回这个文件。

这里保存的值一律用"空"（空串 / None）表示"没设，用内置默认"，所以没有设置文件、
或者文件里某一项留空时，行为跟加这个功能之前完全一样。

文件里不存任何 API Key：设置页只显示 key 有没有、从哪来。
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import threading
import time

from core import atomic

CURRENT_VERSION = 1
SETTINGS_ENV = "SPARK_SETTINGS_FILE"
DEFAULT_PATH = os.path.expanduser("~/.spark/settings.json")

BACKENDS = ("api", "openrouter", "openai_compatible", "cli", "ollama")
BACKEND_LABELS = {
    "api": "Anthropic API",
    "openrouter": "OpenRouter",
    "openai_compatible": "第三方 OpenAI 兼容 API",
    "cli": "本机 claude CLI",
    "ollama": "本地 Ollama",
}
SUMMARY_LENGTHS = ("short", "medium", "long")
SPEECH_LANG_MODES = ("bilingual", "zh", "original")
# 这几个后端的 API Base 可以有默认值；OpenRouter 用官方地址就好，不单独存
API_BASE_BACKENDS = ("openai_compatible", "ollama")
# 各模块的输出目录
OUTPUT_MODULES = ("summit", "podcast", "track", "notes")

MAX_PATH_LEN = 1024
MAX_MODEL_LEN = 200
MAX_TRANSCRIPT_CHARS_LIMIT = 10_000_000
_LANG_RE = re.compile(r"^[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]{1,8})*$")


class SettingsError(ValueError):
    """保存的内容不合规。errors 是 {字段路径: 原因}。"""

    def __init__(self, errors: dict[str, str]):
        self.errors = errors
        super().__init__("；".join(f"{k}：{v}" for k, v in errors.items()))


def builtin() -> dict:
    """全空的一份设置：每一项都"没设，用内置默认"。"""
    return {
        "version": CURRENT_VERSION,
        "storage": {
            "vault_root": "",
            "summit_output_dir": "",
            "podcast_output_dir": "",
            "track_output_dir": "",
            "notes_output_dir": "",
        },
        "ai": {
            "backend": "",
            "models": {b: "" for b in BACKENDS},
            "overall_model": "",
            "summary_length": "",
            "speech_lang_mode": "",
            "lang_prefs": "",
            "max_transcript_chars": None,
            "api_bases": {b: "" for b in API_BASE_BACKENDS},
        },
    }


def settings_path() -> str:
    return os.path.expanduser(os.environ.get(SETTINGS_ENV) or DEFAULT_PATH)


# ---------------------------------------------------------------- 校验

def _clean_path(value, field: str, errors: dict) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        errors[field] = "应该是文本"
        return ""
    v = value.strip()
    if not v:
        return ""
    if len(v) > MAX_PATH_LEN or any(c in v for c in "\0\n\r"):
        errors[field] = "路径不合规"
        return ""
    if not (v.startswith("/") or v == "~" or v.startswith("~/")):
        errors[field] = "请填绝对路径（以 / 开头）或以 ~/ 开头的路径"
        return ""
    expanded = os.path.normpath(os.path.expanduser(v))
    if expanded == os.sep or expanded == os.path.normpath(os.path.expanduser("~")):
        errors[field] = "不能直接用根目录或家目录"
        return ""
    if ".." in v.replace("\\", "/").split("/"):
        errors[field] = "路径里不要带 ..，请写成完整路径"
        return ""
    return v.rstrip("/") or v


def _clean_choice(value, choices, field: str, errors: dict) -> str:
    if value in (None, ""):
        return ""
    if value not in choices:
        errors[field] = f"只能是 {' / '.join(choices)} 之一"
        return ""
    return value


def _clean_text(value, field: str, errors: dict, max_len: int = MAX_MODEL_LEN) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        errors[field] = "应该是文本"
        return ""
    v = value.strip()
    if len(v) > max_len or any(c in v for c in "\0\n\r"):
        errors[field] = "内容不合规"
        return ""
    return v


def _clean_url(value, field: str, errors: dict) -> str:
    v = _clean_text(value, field, errors, MAX_PATH_LEN)
    if v and not re.match(r"^https?://[^\s/]+", v):
        errors[field] = "请填 http:// 或 https:// 开头的地址"
        return ""
    return v


def _clean_lang(value, field: str, errors: dict) -> str:
    v = _clean_text(value, field, errors, 40)
    if v and not _LANG_RE.match(v):
        errors[field] = "请填语言代码，例如 en、zh-Hans、ja"
        return ""
    return v


def _clean_int(value, field: str, errors: dict):
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        errors[field] = "应该是整数"
        return None
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        errors[field] = "应该是整数（0 表示不限制）"
        return None
    if n < 0 or n > MAX_TRANSCRIPT_CHARS_LIMIT:
        errors[field] = f"应该在 0 到 {MAX_TRANSCRIPT_CHARS_LIMIT} 之间"
        return None
    return n


def _check_keys(section: dict, allowed, prefix: str, errors: dict) -> None:
    for k in section:
        if k not in allowed:
            errors[f"{prefix}{k}"] = "不认识的设置项"


def _normalize(data: dict, errors: dict) -> dict:
    """把一份（可能不完整的）设置整理成完整结构；不合规的项记进 errors 并按"没设"处理。"""
    out = builtin()
    if not isinstance(data, dict):
        errors["settings"] = "应该是一个对象"
        return out
    _check_keys(data, ("version", "storage", "ai"), "", errors)

    storage = data.get("storage") or {}
    if not isinstance(storage, dict):
        errors["storage"] = "应该是一个对象"
        storage = {}
    _check_keys(storage, out["storage"].keys(), "storage.", errors)
    for k in out["storage"]:
        out["storage"][k] = _clean_path(storage.get(k), f"storage.{k}", errors)

    ai = data.get("ai") or {}
    if not isinstance(ai, dict):
        errors["ai"] = "应该是一个对象"
        ai = {}
    _check_keys(ai, out["ai"].keys(), "ai.", errors)
    o = out["ai"]
    o["backend"] = _clean_choice(ai.get("backend"), BACKENDS, "ai.backend", errors)
    models = ai.get("models") or {}
    if not isinstance(models, dict):
        errors["ai.models"] = "应该是一个对象"
        models = {}
    _check_keys(models, BACKENDS, "ai.models.", errors)
    for b in BACKENDS:
        o["models"][b] = _clean_text(models.get(b), f"ai.models.{b}", errors)
    o["overall_model"] = _clean_text(ai.get("overall_model"), "ai.overall_model", errors)
    o["summary_length"] = _clean_choice(ai.get("summary_length"), SUMMARY_LENGTHS, "ai.summary_length", errors)
    o["speech_lang_mode"] = _clean_choice(ai.get("speech_lang_mode"), SPEECH_LANG_MODES,
                                          "ai.speech_lang_mode", errors)
    o["lang_prefs"] = _clean_lang(ai.get("lang_prefs"), "ai.lang_prefs", errors)
    o["max_transcript_chars"] = _clean_int(ai.get("max_transcript_chars"), "ai.max_transcript_chars", errors)
    bases = ai.get("api_bases") or {}
    if not isinstance(bases, dict):
        errors["ai.api_bases"] = "应该是一个对象"
        bases = {}
    _check_keys(bases, API_BASE_BACKENDS, "ai.api_bases.", errors)
    for b in API_BASE_BACKENDS:
        o["api_bases"][b] = _clean_url(bases.get(b), f"ai.api_bases.{b}", errors)
    return out


def validate(data: dict) -> dict:
    """校验一份完整或部分的设置，返回整理好的完整结构；有问题就抛 SettingsError。"""
    errors: dict[str, str] = {}
    out = _normalize(data, errors)
    if errors:
        raise SettingsError(errors)
    return out


# ---------------------------------------------------------------- 版本迁移

def _migrate_0_to_1(data: dict) -> dict:
    """没有 version 字段的老格式（平铺的 key，从没正式发布过，留着这条路）：
    vault_root / *_output_dir 归到 storage，其余归到 ai。"""
    base = builtin()
    storage, ai = {}, {}
    for k, v in data.items():
        if k in base["storage"]:
            storage[k] = v
        elif k in base["ai"]:
            ai[k] = v
        elif k in ("storage", "ai") and isinstance(v, dict):
            (storage if k == "storage" else ai).update(v)
    return {"version": 1, "storage": storage, "ai": ai}


# 从第 n 版升到第 n+1 版。以后改结构就在这里加一条，读的时候按顺序一路升上来。
MIGRATIONS = {0: _migrate_0_to_1}


def migrate(data) -> tuple[dict, list[str]]:
    """把读到的任意版本升到当前版本。返回 (整理好的设置, 提示)。"""
    notes: list[str] = []
    if not isinstance(data, dict):
        return builtin(), ["设置文件内容不是对象，已按默认值处理"]
    version = data.get("version", 0)
    if not isinstance(version, int) or version < 0:
        notes.append("设置文件的版本号不对，按老格式读")
        version = 0
    if version > CURRENT_VERSION:
        notes.append(f"设置文件是更新版本（v{version}）写的，只读出了认识的部分；在这里保存会按 v{CURRENT_VERSION} 写回")
        data = {k: data[k] for k in ("storage", "ai") if k in data}
        version = CURRENT_VERSION
    while version < CURRENT_VERSION:
        data = MIGRATIONS[version](data)
        version += 1
    errors: dict[str, str] = {}
    # 文件里多出来的/不合规的项：按"没设"处理，不因为一项写坏了整个设置都不认
    out = _normalize(data, errors)
    if errors:
        notes.append("设置文件里有几项不合规，已按默认值处理：" + "；".join(f"{k}（{v}）" for k, v in errors.items()))
    return out, notes


# ---------------------------------------------------------------- 读写

_LOCK = threading.Lock()
_CACHE: dict = {"key": None, "value": None, "notes": []}


def _file_key(path: str):
    try:
        st = os.stat(path)
    except OSError:
        return (path, None)
    return (path, st.st_mtime_ns, st.st_size)


def _read(path: str) -> tuple[dict, list[str], bool, bool]:
    """返回 (设置, 提示, 文件是否存在, 文件是否坏了)。读不出来也不抛错——设置坏了
    不能让所有 app 起不来。"""
    if not os.path.exists(path):
        return builtin(), [], False, False
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError) as e:
        return builtin(), [f"设置文件读不出来（{e.__class__.__name__}），已按默认值处理；保存时会先把坏文件另存一份"], True, True
    value, notes = migrate(raw)
    return value, notes, True, False


def load_with_status() -> tuple[dict, dict]:
    """返回 (设置, {"path", "exists", "notes"})。按文件的修改时间缓存，频繁调用也不重复读盘。"""
    path = settings_path()
    key = _file_key(path)
    with _LOCK:
        if _CACHE["key"] != key:
            value, notes, exists, _broken = _read(path)
            _CACHE.update(key=key, value=value, notes=notes, exists=exists)
        return copy.deepcopy(_CACHE["value"]), {"path": path, "exists": _CACHE["exists"],
                                               "notes": list(_CACHE["notes"])}


def load() -> dict:
    return load_with_status()[0]


def _merge(base: dict, patch: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def save(patch: dict) -> dict:
    """把 patch（完整或部分的设置）合并进已保存的设置，校验后原子写回。返回写入后的设置。"""
    if not isinstance(patch, dict):
        raise SettingsError({"settings": "应该是一个对象"})
    patch = {k: v for k, v in patch.items() if k != "version"}
    # 先单独校验 patch：不认识的字段（比如想顺手塞个 api_key）直接拒绝
    errors: dict[str, str] = {}
    _normalize(patch, errors)
    if errors:
        raise SettingsError(errors)
    path = settings_path()
    with _LOCK:
        current, _notes, _exists, broken = _read(path)
        merged = validate(_merge(current, patch))
        merged["version"] = CURRENT_VERSION
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        if broken:
            try:
                shutil.copy2(path, f"{path}.broken-{time.strftime('%Y%m%d-%H%M%S')}")
            except OSError:
                pass
        atomic.write_json(path, merged, indent=2)
        _CACHE["key"] = None
    return merged


# ---------------------------------------------------------------- 取值

def get(dotted: str):
    """保存的值；没设就是空串或 None。"""
    node = load()
    for part in dotted.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def pick(explicit, dotted: str, fallback):
    """任务上明确给的值 > 设置里保存的 > 内置默认。explicit 为 None 或空串算没给。"""
    if explicit not in (None, ""):
        return explicit
    saved = get(dotted)
    if saved not in (None, ""):
        return saved
    return fallback


def backend(app_default: str) -> str:
    return get("ai.backend") or app_default


def model_for(backend_name: str) -> str:
    return (get(f"ai.models.{backend_name}") or "") if backend_name in BACKENDS else ""


def output_dir(module: str, fallback: str = "") -> str:
    """某个模块的默认输出目录（已展开 ~）。设置里没填就按原来的规则：
    笔记洞察 → 笔记库/output；其它 → 笔记库/Spark（库不在时退回 fallback）。"""
    from core import vault
    saved = get(f"storage.{module}_output_dir") if module in OUTPUT_MODULES else ""
    if saved:
        return os.path.expanduser(saved)
    if module == "notes":
        return vault.reports_dir()
    return vault.default_output_dir(fallback) if fallback else vault.spark_dir()


# ---------------------------------------------------------------- 给设置页的说明

def _key_status() -> dict:
    """每个后端的 key 状态和来源。只说有没有、从哪来，绝不带 key 的内容。"""
    from core import keys
    out = {}
    for b in BACKENDS:
        provider = {"api": "anthropic", "openrouter": "openrouter"}.get(b)
        if provider:
            src = keys.source_of(provider)
            env_name = keys.ENV_VARS.get(provider, "")
            if src == "env":
                out[b] = {"set": True, "source": "env", "label": f"已设置（环境变量 {env_name}）"}
            elif src:
                out[b] = {"set": True, "source": "file", "label": f"已设置（本机 key 文件 {src}）"}
            else:
                out[b] = {"set": False, "source": "", "label":
                          f"未设置（可设环境变量 {env_name}，或把 key 存成 "
                          f"{keys.display_dir(provider)}/{provider}.key）"}
        elif b == "openai_compatible":
            out[b] = {"set": False, "source": "", "label": "每次在任务里填写，不保存"}
        elif b == "cli":
            found = shutil.which("claude") is not None
            out[b] = {"set": found, "source": "", "label":
                      "不需要 key（已检测到本机 claude CLI）" if found else "不需要 key（但没检测到本机 claude CLI）"}
        else:
            out[b] = {"set": True, "source": "", "label": "不需要 key"}
    return out


def describe() -> dict:
    """设置页用：保存的值、实际生效的值、来源（settings / env / default），以及 key 状态。"""
    from core import vault
    saved, status = load_with_status()
    fields: dict[str, dict] = {}

    env_vault = (os.environ.get("SPARK_VAULT") or "").strip()
    sv = saved["storage"]["vault_root"]
    fields["storage.vault_root"] = {
        "saved": sv,
        "value": vault.vault_root(),
        "default": vault.DEFAULT_VAULT,
        "source": "env" if env_vault else ("settings" if sv else "default"),
        "env_var": "SPARK_VAULT" if env_vault else "",
    }
    summit_fallback = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                   "apps", "summit2md", "output")
    for m in OUTPUT_MODULES:
        k = f"storage.{m}_output_dir"
        s = saved["storage"][f"{m}_output_dir"]
        builtin_value = vault.reports_dir() if m == "notes" else vault.default_output_dir(summit_fallback)
        fields[k] = {"saved": s, "value": os.path.expanduser(s) if s else builtin_value,
                     "default": builtin_value, "source": "settings" if s else "default", "env_var": ""}

    ai = saved["ai"]
    builtin_ai = {
        "ai.backend": "（按 app：Summit / Podcast / 信息跟进用 Anthropic API，笔记洞察用本机 claude CLI）",
        "ai.overall_model": "（和逐条用的模型一致）",
        "ai.summary_length": "medium",
        "ai.speech_lang_mode": "bilingual",
        "ai.lang_prefs": "en",
        "ai.max_transcript_chars": "（按 app：Summit 系 120000，笔记洞察 100000）",
    }
    for k, d in builtin_ai.items():
        s = ai[k.split(".", 1)[1]]
        fields[k] = {"saved": s, "value": s if s not in ("", None) else d, "default": d,
                     "source": "settings" if s not in ("", None) else "default", "env_var": ""}
    for b in BACKENDS:
        s = ai["models"][b]
        fields[f"ai.models.{b}"] = {"saved": s, "value": s or "（按 app 自己的默认）", "default": "",
                                    "source": "settings" if s else "default", "env_var": ""}
    for b in API_BASE_BACKENDS:
        s = ai["api_bases"][b]
        d = "http://localhost:11434" if b == "ollama" else ""
        fields[f"ai.api_bases.{b}"] = {"saved": s, "value": s or d, "default": d,
                                       "source": "settings" if s else "default", "env_var": ""}

    return {
        "version": CURRENT_VERSION,
        "path": status["path"],
        "exists": status["exists"],
        "notes": status["notes"],
        "settings": saved,
        "fields": fields,
        "keys": _key_status(),
        "backends": [{"key": b, "label": BACKEND_LABELS[b]} for b in BACKENDS],
        "summary_lengths": list(SUMMARY_LENGTHS),
        "speech_lang_modes": list(SPEECH_LANG_MODES),
    }


def app_defaults(app_backend: str, output_modules: tuple[str, ...], output_fallback: str = "") -> dict:
    """给各 app 的 /api/env 用：表单要预填的默认值，以及哪些项是"设置里明确存过的"
    （前端据此决定要不要让浏览器里记住的上次值盖过它）。"""
    s = load()
    ai = s["ai"]
    outputs = {m: output_dir(m, output_fallback) for m in output_modules}
    return {
        "backend": ai["backend"] or app_backend,
        "models": dict(ai["models"]),
        "overall_model": ai["overall_model"],
        "summary_length": ai["summary_length"] or "medium",
        "speech_lang_mode": ai["speech_lang_mode"] or "bilingual",
        "lang_prefs": ai["lang_prefs"] or "en",
        "max_transcript_chars": ai["max_transcript_chars"],
        "api_bases": dict(ai["api_bases"]),
        "output_dirs": outputs,
        "from_settings": {
            "vault_root": bool(s["storage"]["vault_root"]) and not os.environ.get("SPARK_VAULT"),
            "backend": bool(ai["backend"]),
            "models": {b: bool(v) for b, v in ai["models"].items()},
            "overall_model": bool(ai["overall_model"]),
            "summary_length": bool(ai["summary_length"]),
            "speech_lang_mode": bool(ai["speech_lang_mode"]),
            "lang_prefs": bool(ai["lang_prefs"]),
            "max_transcript_chars": ai["max_transcript_chars"] is not None,
            "api_bases": {b: bool(v) for b, v in ai["api_bases"].items()},
            "output_dirs": {m: bool(s["storage"][f"{m}_output_dir"]) for m in output_modules},
        },
    }
