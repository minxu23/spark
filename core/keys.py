"""
API Key 的来源与查找顺序。

一个 provider 一个文件，文件内容就是 key 本身（前后空白会去掉）。这样两个 app
配一次就够，也不用把 key 写进浏览器本地存储或每次粘贴。

查找顺序：调用方显式传入 → 环境变量 → ~/.spark/keys → ~/.summit2md/keys（老位置）。
老位置保留是因为现有的 key 文件就在那儿，迁移期间不动它们；新装机器会落到 ~/.spark/keys。
"""

from __future__ import annotations

import os

SPARK_KEYS_DIR = os.path.expanduser("~/.spark/keys")
LEGACY_KEYS_DIR = os.path.expanduser("~/.summit2md/keys")
SEARCH_DIRS: tuple[str, ...] = (SPARK_KEYS_DIR, LEGACY_KEYS_DIR)

ENV_VARS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}


def key_file_path(provider: str) -> str:
    """返回这个 provider 实际存在的 key 文件路径；没有就返回空串。"""
    for d in SEARCH_DIRS:
        path = os.path.join(d, f"{provider}.key")
        if os.path.isfile(path):
            return path
    return ""


def read_key_file(provider: str) -> str:
    """读 key 文件内容。没配置就返回空串——这是可选功能，没有文件的人应该完全无感知。"""
    path = key_file_path(provider)
    if not path:
        return ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def resolve(provider: str, supplied: str = "") -> str:
    """按 调用方 → 环境变量 → key 文件 的顺序拿 key，全都没有就返回空串。"""
    if supplied and supplied.strip():
        return supplied.strip()
    env_name = ENV_VARS.get(provider)
    if env_name:
        from_env = (os.environ.get(env_name) or "").strip()
        if from_env:
            return from_env
    return read_key_file(provider)


def source_of(provider: str, supplied: str = "") -> str:
    """这个 key 是从哪儿来的，用于界面提示：'request' / 'env' / key 文件路径 / ''。"""
    if supplied and supplied.strip():
        return "request"
    env_name = ENV_VARS.get(provider)
    if env_name and (os.environ.get(env_name) or "").strip():
        return "env"
    return key_file_path(provider)


def display_dir(provider: str = "") -> str:
    """界面上写"把 key 放到这里"时用的目录。已经有 key 文件就指向它所在的目录，
    否则指向新位置——免得提示的路径和实际生效的路径不是同一个。"""
    if provider:
        path = key_file_path(provider)
        if path:
            return os.path.dirname(path)
    for p in ENV_VARS:
        path = key_file_path(p)
        if path:
            return os.path.dirname(path)
    return SPARK_KEYS_DIR
