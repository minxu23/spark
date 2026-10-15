"""放在仓库根目录，pytest 会把这个目录加进 sys.path，测试里就能直接 import core。"""

import pytest


@pytest.fixture(autouse=True)
def _isolate_notes2insight_cache(tmp_path, monkeypatch):
    """notes2insight 的笔记库索引和摘要卡缓存默认写在 app 目录下的 .cache 里——
    那是真实在用的缓存。测试一律改写到临时目录，不往里面塞测试用的假数据。"""
    from apps.notes2insight import pipeline, vault

    cache = tmp_path / "_n2i_cache"
    monkeypatch.setattr(vault, "CACHE_DIR", str(cache))
    monkeypatch.setattr(vault, "INDEX_PATH", str(cache / "index.json"))
    monkeypatch.setattr(pipeline, "DIGEST_CACHE_DIR", str(cache / "digests"))


@pytest.fixture(autouse=True)
def _isolate_spark_settings(tmp_path, monkeypatch):
    """全局设置默认在 ~/.spark/settings.json——那是真在用的设置。测试一律指到临时
    目录里一个还不存在的文件，既不读到真设置，也不会写坏它。"""
    monkeypatch.setenv("SPARK_SETTINGS_FILE", str(tmp_path / "_spark_settings" / "settings.json"))


@pytest.fixture(autouse=True)
def _no_network_model_catalog(monkeypatch):
    """模型目录没有缓存时会联网拉 Anthropic / OpenRouter 的模型列表；测试里一律不联网，
    要测拉取逻辑的测试自己传 get_json。"""
    from core import model_catalog

    def offline(url, headers):
        raise OSError("测试里不联网")

    monkeypatch.setattr(model_catalog, "_get_json", offline)


@pytest.fixture(autouse=True)
def _no_youtube_pacing(monkeypatch):
    """YouTube 请求之间的全局间隔（防被判成机器人）在测试里关掉，不然每条用例白等几秒。"""
    from apps.summit2md import pipeline
    monkeypatch.setattr(pipeline, "YOUTUBE_MIN_INTERVAL", 0)
