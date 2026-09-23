import os

import pytest

from core import vault


def test_没设环境变量时用默认库位置_而且是展开过的绝对路径(monkeypatch):
    monkeypatch.delenv("SPARK_VAULT", raising=False)
    assert vault.vault_root() == vault.DEFAULT_VAULT
    assert os.path.isabs(vault.DEFAULT_VAULT)
    assert "~" not in vault.DEFAULT_VAULT


def test_环境变量可以覆盖库位置(monkeypatch, tmp_path):
    monkeypatch.setenv("SPARK_VAULT", str(tmp_path))
    assert vault.vault_root() == str(tmp_path)
    assert vault.spark_dir() == os.path.join(str(tmp_path), "Spark")


def test_库在时默认输出目录落到_Spark_目录(monkeypatch, tmp_path):
    monkeypatch.setenv("SPARK_VAULT", str(tmp_path))
    assert vault.default_output_dir("/fallback") == os.path.join(str(tmp_path), "Spark")


def test_库不可用时退回_app_目录(monkeypatch, tmp_path):
    """换了机器、外置盘没挂的时候，不要凭空造一棵 ~/Documents/... 的目录树出来。"""
    monkeypatch.setenv("SPARK_VAULT", str(tmp_path / "并不存在"))
    assert vault.default_output_dir("/fallback") == "/fallback"


def test_波浪号会被展开(monkeypatch):
    monkeypatch.setenv("SPARK_VAULT", "~/somewhere")
    assert vault.vault_root() == os.path.expanduser("~/somewhere")
