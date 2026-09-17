import os

import pytest

from core import keys


@pytest.fixture
def two_dirs(tmp_path, monkeypatch):
    """模拟"新位置 + 老位置"两个 key 目录。"""
    spark = tmp_path / "spark"
    legacy = tmp_path / "legacy"
    spark.mkdir()
    legacy.mkdir()
    monkeypatch.setattr(keys, "SPARK_KEYS_DIR", str(spark))
    monkeypatch.setattr(keys, "LEGACY_KEYS_DIR", str(legacy))
    monkeypatch.setattr(keys, "SEARCH_DIRS", (str(spark), str(legacy)))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return spark, legacy


def test_老位置的_key_仍然能读到(two_dirs):
    _spark, legacy = two_dirs
    (legacy / "anthropic.key").write_text("  sk-legacy\n", encoding="utf-8")
    assert keys.read_key_file("anthropic") == "sk-legacy"


def test_新位置优先于老位置(two_dirs):
    spark, legacy = two_dirs
    (legacy / "anthropic.key").write_text("sk-legacy", encoding="utf-8")
    (spark / "anthropic.key").write_text("sk-spark", encoding="utf-8")
    assert keys.read_key_file("anthropic") == "sk-spark"


def test_查找顺序_显式传入_环境变量_文件(two_dirs, monkeypatch):
    _spark, legacy = two_dirs
    (legacy / "anthropic.key").write_text("sk-file", encoding="utf-8")

    assert keys.resolve("anthropic") == "sk-file"

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-env")
    assert keys.resolve("anthropic") == "sk-env"

    assert keys.resolve("anthropic", "sk-supplied") == "sk-supplied"


def test_没有任何配置时返回空串而不是报错(two_dirs):
    assert keys.read_key_file("anthropic") == ""
    assert keys.resolve("anthropic") == ""
    assert keys.source_of("anthropic") == ""


def test_display_dir_指向实际生效的那个目录(two_dirs):
    spark, legacy = two_dirs
    # 一个 key 都没有时，提示新位置
    assert keys.display_dir() == str(spark)
    # 老位置有 key 时，提示必须跟着走，否则界面写的路径和真正生效的不是同一个
    (legacy / "openrouter.key").write_text("sk-legacy", encoding="utf-8")
    assert keys.display_dir() == str(legacy)


def test_source_of_能区分来源(two_dirs, monkeypatch):
    _spark, legacy = two_dirs
    (legacy / "anthropic.key").write_text("sk-file", encoding="utf-8")
    assert keys.source_of("anthropic") == os.path.join(str(legacy), "anthropic.key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-env")
    assert keys.source_of("anthropic") == "env"
    assert keys.source_of("anthropic", "sk-supplied") == "request"
