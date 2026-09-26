"""把请求里的模型后端参数（backend / api_key / api_base / model）解析成一份能直接用的
配置：没填的 key 按 环境变量 → key 文件 找（见 core/keys），缺了必填项就报一句能看懂的错。

summit2md 和 notes2insight 原来各写了一份，已经走样（一边读 OPENROUTER_API_KEY 环境
变量、一边不读；默认后端也不同）。默认后端仍按 app 区分，由调用方传入。

请求里没带的后端 / 模型 / API Base，先看设置页里保存的默认值（core/settings），
再落到内置默认：请求里明确给的值永远优先，也不会写回设置。
"""

from __future__ import annotations

from core import keys
from core import settings
from core.llm import DEFAULT_OLLAMA_HOST, OPENROUTER_API_BASE


class ConfigError(ValueError):
    pass


def resolve(data: dict, *, default_backend: str, needs_llm: bool = True) -> dict:
    """返回 {"backend", "api_key", "api_base", "model"}；缺必填项时抛 ConfigError。
    needs_llm=False 时（这次运行根本不调模型）只做默认值补全，不校验必填项。"""
    backend = data.get("backend") or settings.backend(default_backend)
    api_key = (data.get("api_key") or "").strip()
    api_base = (data.get("api_base") or "").strip()
    model = (data.get("model") or "").strip()
    # 模型和 API Base 只在请求里压根没带这一项时才取设置：表单上留空也是一种明确的
    # 选择（比如 CLI 选「默认，跟随 CLI 自己的设置」发的就是空串）
    if "model" not in data:
        model = settings.model_for(backend)
    if "api_base" not in data and backend in settings.API_BASE_BACKENDS:
        api_base = settings.get(f"ai.api_bases.{backend}") or ""

    if backend == "api":
        api_key = keys.resolve("anthropic", api_key)
        if needs_llm and not api_key:
            raise ConfigError(
                "已选择 Anthropic API，但没有填写 API Key（环境变量 ANTHROPIC_API_KEY 和 "
                f"{keys.display_dir('anthropic')}/anthropic.key 里都没找到）")
    elif backend == "openrouter":
        key_from_local = not api_key
        api_key = keys.resolve("openrouter", api_key)
        key_from_local = key_from_local and bool(api_key)
        api_base = api_base or OPENROUTER_API_BASE
        if needs_llm:
            if not api_key:
                raise ConfigError(
                    "已选择 OpenRouter，但没有填写 API Key（环境变量 OPENROUTER_API_KEY 和 "
                    f"{keys.display_dir('openrouter')}/openrouter.key 里都没找到）")
            if not model:
                raise ConfigError("已选择 OpenRouter，但没有填写模型名")
            # 本机保存的 key 只发给官方地址：请求里的 api_base 可以是任意服务器，
            # 不能让一次请求就把本机的 key 带出去
            if key_from_local and api_base.rstrip("/") != OPENROUTER_API_BASE:
                raise ConfigError("自定义了 API Base URL 时请手动填写 OpenRouter API Key"
                                  "（不会把本地保存的 key 发给非官方地址）")
    elif backend == "openai_compatible":
        if needs_llm:
            if not api_key:
                raise ConfigError("已选择第三方 OpenAI 兼容 API，但没有填写 API Key")
            if not api_base:
                raise ConfigError("已选择第三方 OpenAI 兼容 API，但没有填写 API Base URL")
            if not model:
                raise ConfigError("已选择第三方 OpenAI 兼容 API，但没有填写模型名")
    elif backend == "ollama":
        if needs_llm and not model:
            raise ConfigError("已选择本地 Ollama，但没有填写/选择模型名（需先 `ollama pull <模型>`）")
        api_base = api_base or DEFAULT_OLLAMA_HOST

    return {"backend": backend, "api_key": api_key, "api_base": api_base, "model": model}
