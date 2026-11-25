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
    monkeypatch.setattr(pipeline, "YOUTUBE_RATE_LIMIT_COOLDOWN", 0)
    monkeypatch.setattr(pipeline, "_yt_last_finish", [0.0])
    monkeypatch.setattr(pipeline, "_yt_cooldown_until", [0.0])
    monkeypatch.setattr(pipeline, "_yt_rate_limit_streak", [0])


@pytest.fixture(autouse=True)
def _isolate_pending_meetings(tmp_path, monkeypatch):
    from apps.summit2md import pending_tasks
    monkeypatch.setattr(pending_tasks, "STATE_PATH", tmp_path / "pending_meetings.json")


@pytest.fixture(autouse=True)
def _isolate_youtube_materials(tmp_path, monkeypatch):
    from apps.notes2insight import youtube_import
    from contextvars import ContextVar
    monkeypatch.setattr(youtube_import, "_ROOT", ContextVar("test_youtube_root", default=str(tmp_path / "youtube_materials")))


@pytest.fixture(autouse=True)
def _no_real_harmony_devices(monkeypatch):
    """数据线测试只用替身，不枚举或访问实际已连接的鸿蒙手机。"""
    from core import taihe_sync
    monkeypatch.setattr(taihe_sync, 'hdc_command', lambda: [])


@pytest.fixture(autouse=True)
def _fresh_subscription_list_cache(monkeypatch):
    """订阅检查会把抓到的列表缓存 10 分钟；每条用例从空缓存开始，免得上一条 mock 的列表串过来。"""
    from apps.summit2md import tracking
    monkeypatch.setattr(tracking, "_list_cache", {})


@pytest.fixture(autouse=True)
def _isolate_search_index(tmp_path, monkeypatch):
    """全文索引默认在 ~/.spark/search-index.sqlite——那是真在用的缓存。测试一律指到临时目录。"""
    monkeypatch.setenv("SPARK_SEARCH_INDEX", str(tmp_path / "_search_index" / "search-index.sqlite"))


@pytest.fixture(autouse=True)
def _no_auto_tag_fill(monkeypatch):
    """github_sync.after_task 会在后台线程里扫笔记库、补标签；没指定笔记库的测试会读到真实笔记库。
    默认关掉；测它的用例（tests/test_tags_autofill.py）自己换回真的。"""
    from core import github_sync
    monkeypatch.setattr(github_sync, "_kick_tags", lambda: None)


@pytest.fixture(autouse=True)
def _no_real_vault(tmp_path, monkeypatch):
    """保险：没有自己指定笔记库的测试，默认笔记库指到临时目录里一个不存在的位置，而不是这台机器上真实的
    Obsidian 库。漏了隔离的测试最多写进临时目录，写不到真实笔记库里（出过一次：一条测试往真实库的
    output/复盘/ 里存了文件）。要用笔记库的测试照旧自己设 SPARK_VAULT 或传 root。"""
    from core import vault
    monkeypatch.setattr(vault, "DEFAULT_VAULT", str(tmp_path / "_no_vault"))
    monkeypatch.delenv("SPARK_VAULT", raising=False)   # shell 里设了它的话，vault_root 会先读它
