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
