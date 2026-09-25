"""/read：在网页里读 Spark 产物。"""

import os
import sys
import urllib.parse

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import spark
from apps.reader import server as reader


@pytest.fixture
def vault(tmp_path, monkeypatch):
    monkeypatch.setenv("SPARK_VAULT", str(tmp_path))
    show = tmp_path / "Spark" / "Show"
    for d in ("notes", "speech", "transcripts", ".cache"):
        (show / d).mkdir(parents=True)
    (show / "Show.md").write_text("# Show\n\n> 共 1 期\n\n- [第一期](<notes/20260110 第一期.md>)\n", encoding="utf-8")
    (show / "notes" / "20260110 第一期.md").write_text(
        '---\n节目: "[[Show]]"\n话题: ["A", "B"]\n链接: "https://example.com/v"\n---\n\n'
        "# 第一期\n\n> 一句话\n\n- [整理稿](<../speech/20260110_第一期.md>)\n\n<script>alert(1)</script>\n",
        encoding="utf-8")
    (show / "speech" / "20260110_第一期.md").write_text(
        "# 第一期\n\n## 演讲稿\n\nHello world.\n\n> 你好，世界。\n\nSecond.\n\n> 第二段。\n", encoding="utf-8")
    (show / ".manifest.json").write_text("{}", encoding="utf-8")
    (show / ".cache" / "x.md").write_text("secret", encoding="utf-8")
    return tmp_path


def _get(c, path):
    return c.get(urllib.parse.quote(path, safe="/?=&"))


def _client():
    from werkzeug.test import Client
    return Client(spark.application)


def test_首页列出节目_文件夹页排出主页(vault):
    c = _client()
    r = _get(c, "/read/")
    assert r.status_code == 200 and "Show" in r.get_data(as_text=True)
    assert _get(c, "/read/f/Show").status_code in (301, 302, 308), "文件夹不带斜杠要跳转，相对链接才解析得对"
    page = _get(c, "/read/f/Show/").get_data(as_text=True)
    assert "共 1 期" in page and "第一期" in page


def test_笔记_frontmatter_维基链接_外链_转义(vault):
    page = _get(_client(), "/read/f/Show/notes/20260110 第一期.md").get_data(as_text=True)
    assert '<dl class="meta">' in page
    assert 'href="/read/f/Show/"' in page, "[[Show]] 指向节目文件夹页"
    assert '<span class="chip">A</span>' in page
    assert 'target="_blank"' in page
    assert "<script>alert" not in page, "正文里的原始 HTML 要转义"


def test_整理稿排成原文译文对照(vault):
    page = _get(_client(), "/read/f/Show/speech/20260110_第一期.md").get_data(as_text=True)
    assert page.count('class="pair"') == 2
    assert '<main class="wide">' in page


def test_笔记里的引用块不当成译文(vault):
    page = _get(_client(), "/read/f/Show/notes/20260110 第一期.md").get_data(as_text=True)
    assert 'class="pair"' not in page


@pytest.mark.parametrize("path", [
    "/read/f/Show/.manifest.json", "/read/f/Show/.cache/x.md", "/read/f/../../etc/passwd",
])
def test_隐藏文件_越界路径都是_404(vault, path):
    assert _get(_client(), path).status_code == 404


def test_每页都有阅读设置面板_偏好在首屏前套上(vault):
    page = _get(_client(), "/read/f/Show/notes/20260110 第一期.md").get_data(as_text=True)
    head = page.split("</head>")[0]
    assert "/read/static/theme.js" in head, "theme.js 要在 head 里同步加载，否则会先闪一下默认配色"
    assert 'id="aa"' in page and 'id="prefs"' in page
    for theme in ("auto", "kami", "white", "sepia", "gray", "night"):
        assert f'data-theme="{theme}"' in page
    assert _get(_client(), "/read/static/theme.js").status_code == 200

