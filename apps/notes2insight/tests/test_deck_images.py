"""演示配图（OpenRouter）：默认不调用、估算、写文件与相对引用、部分失败、Key 不外泄、
笔记洞察和阅读页两边的图片路由只给图片。所有 HTTP 调用都换成假的，不花钱。"""

import base64
import io
import json
import os
import time
import urllib.error
import urllib.parse

import pytest

from apps.notes2insight import deck, deck_images, server
from core import keys

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
KEY = "sk-or-test-SECRET-123"

REPORT = """---
title: 测试报告
sources: 3 篇笔记
---

# 测试报告
## 一句话副标题

## 执行摘要

摘要正文。

## 第 1 章 推理成本

第一章正文。

## 第 2 章 端侧模型

第二章正文。

## 第 3 章 开源生态

第三章正文。
"""


def _deck():
    return {"title": "测试报告", "subtitle": "副标题", "meta": {}, "sources": [],
            "slides": [
                {"kind": "points", "section": "", "title": "开篇", "lead": "", "note": "",
                 "bullets": [{"text": "a", "cites": []}]},
                {"kind": "section", "section": "一", "title": "第 1 章 推理成本", "lead": "成本怎么掉的", "note": ""},
                {"kind": "points", "section": "一", "title": "要点", "lead": "", "note": "",
                 "bullets": [{"text": "b", "cites": []}]},
                {"kind": "section", "section": "二", "title": "第 2 章 端侧", "lead": "", "note": ""},
            ]}


@pytest.fixture
def with_key(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)


@pytest.fixture
def no_key(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(keys, "SEARCH_DIRS", ())   # 不去读本机真的 key 文件


# ---------------------------------------------------------------- 估算与挑页

def test_估算按章数算_封面加每章一张_封顶():
    est = deck_images.estimate(3, "google/gemini-3.1-flash-lite-image")
    assert est["count"] == 4
    assert est["per_image"] == pytest.approx(0.039)
    assert est["total"] == pytest.approx(0.16)
    assert deck_images.estimate(20, deck_images.DEFAULT_MODEL)["count"] == deck_images.MAX_IMAGES
    unknown = deck_images.estimate(2, "someone/new-image-model")
    assert unknown["per_image"] is None and unknown["total"] is None and unknown["count"] == 3


def test_只给封面和章节页配图():
    assert deck_images.planned_pages(_deck()) == [0, 2, 4]


def test_模型名形状不对就回默认():
    assert deck_images.clean_model("openai/gpt-image-1-mini") == "openai/gpt-image-1-mini"
    for bad in ("", "no-slash", "a/b c", "https://evil.example/x", "../x/y"):
        assert deck_images.clean_model(bad) == deck_images.DEFAULT_MODEL


# ---------------------------------------------------------------- 请求形状

class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_只发到官方地址_解出图片和实际花费(monkeypatch):
    seen = {}

    def fake_urlopen(req, timeout=None, context=None):
        seen["url"] = req.full_url
        seen["auth"] = req.get_header("Authorization")
        seen["body"] = json.loads(req.data)
        payload = {"data": [{"b64_json": base64.b64encode(PNG).decode(), "media_type": "image/png"}],
                   "usage": {"cost": 0.0387}}
        return _Resp(json.dumps(payload).encode())

    monkeypatch.setattr(deck_images.urllib.request, "urlopen", fake_urlopen)
    data, ext, cost = deck_images.request_image("画一张图", model="google/gemini-3.1-flash-lite-image", api_key=KEY)
    assert seen["url"] == "https://openrouter.ai/api/v1/images"
    assert seen["auth"] == f"Bearer {KEY}"
    assert seen["body"]["model"] == "google/gemini-3.1-flash-lite-image"
    assert seen["body"]["aspect_ratio"] == "16:9" and seen["body"]["n"] == 1
    assert (data, ext, cost) == (PNG, "png", pytest.approx(0.0387))


def test_接口报错时报错信息里没有Key(monkeypatch):
    def fake_urlopen(req, timeout=None, context=None):
        raise urllib.error.HTTPError(req.full_url, 402, "Payment Required", {},
                                     io.BytesIO(b'{"error":{"message":"Insufficient credits"}}'))

    monkeypatch.setattr(deck_images.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(deck_images.ImageError) as ei:
        deck_images.request_image("x", model=deck_images.DEFAULT_MODEL, api_key=KEY)
    assert "402" in str(ei.value) and KEY not in str(ei.value)


def test_返回的不是图片就算失败(monkeypatch):
    payload = {"data": [{"b64_json": base64.b64encode(b"<html>not an image</html>").decode()}]}
    monkeypatch.setattr(deck_images.urllib.request, "urlopen",
                        lambda req, timeout=None, context=None: _Resp(json.dumps(payload).encode()))
    with pytest.raises(deck_images.ImageError):
        deck_images.request_image("x", model=deck_images.DEFAULT_MODEL, api_key=KEY)


# ---------------------------------------------------------------- 写文件与引用

def test_配图写在演示旁边_相对路径引用_渲染和复用都带着(tmp_path, monkeypatch):
    calls = []

    def fake_request(prompt, *, model, api_key, timeout=120):
        calls.append(prompt)
        return PNG, "png", 0.04

    monkeypatch.setattr(deck_images, "request_image", fake_request)
    deck_path = str(tmp_path / "我的 报告.deck.html")
    d = _deck()
    out = deck_images.add_images(d, deck_path, api_key=KEY)
    assert len(calls) == 3 and out["failed"] == []
    assert out["cost"] == pytest.approx(0.12)
    assets = tmp_path / "我的 报告.deck.assets"
    assert sorted(os.listdir(assets)) == ["slide-1.png", "slide-3.png", "slide-5.png"]
    assert (assets / "slide-3.png").read_bytes() == PNG
    # 相对路径 + URL 编码（文件夹名有中文和空格）
    rel = urllib.parse.quote("我的 报告.deck.assets/slide-3.png")
    assert d["slides"][1]["image"] == rel
    assert d["cover_image"] == urllib.parse.quote("我的 报告.deck.assets/slide-1.png")
    assert all("no text" in p.lower() for p in calls)

    html = deck.render_html(d, report_filename="我的 报告.md")
    assert rel in html and "file://" not in html and str(tmp_path) not in html
    with open(deck_path, "w", encoding="utf-8") as f:
        f.write(html)
    again = deck.load_deck_from_html(deck_path)
    assert again["cover_image"] == d["cover_image"]
    assert again["slides"][1]["image"] == rel


def test_部分失败_演示照出_列出哪几张没成(tmp_path, monkeypatch):
    def fake_request(prompt, *, model, api_key, timeout=120):
        if "端侧" in prompt:
            raise deck_images.ImageError("OpenRouter 返回 HTTP 502：upstream")
        return PNG, "png", 0.04

    monkeypatch.setattr(deck_images, "request_image", fake_request)
    d = _deck()
    out = deck_images.add_images(d, str(tmp_path / "r.deck.html"), api_key=KEY)
    assert [g["slide"] for g in out["generated"]] == [1, 3]
    assert out["failed"] == [{"slide": 5, "title": "第 2 章 端侧", "error": "OpenRouter 返回 HTTP 502：upstream"}]
    assert "image" not in d["slides"][3]
    html = deck.render_html(d)
    assert "slide-5" not in html and "slide-3.png" in html


def test_重新配图时清掉上一次的图(tmp_path, monkeypatch):
    assets = tmp_path / "r.deck.assets"
    assets.mkdir()
    (assets / "slide-9.png").write_bytes(PNG)
    (assets / "别人的.txt").write_text("keep")
    monkeypatch.setattr(deck_images, "request_image", lambda *a, **k: (PNG, "png", 0))
    deck_images.add_images(_deck(), str(tmp_path / "r.deck.html"), api_key=KEY)
    names = set(os.listdir(assets))
    assert "slide-9.png" not in names and "别人的.txt" in names


# ---------------------------------------------------------------- /api/deck 接上配图

def _write_report(out_dir):
    path = os.path.join(out_dir, "报告.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(REPORT)
    return path


def _run_deck(client, payload, timeout=5):
    r = client.post("/api/deck", json=payload)
    assert r.status_code == 200, r.get_json()
    job_id = r.get_json()["job_id"]
    deadline = time.time() + timeout
    while time.time() < deadline:
        d = client.get(f"/api/progress/{job_id}").get_json()
        if d["done"]:
            return job_id, d
        time.sleep(0.02)
    raise AssertionError("超时")


def _fake_generate(md, **kw):
    d = _deck()
    return deck.render_html(d, report_filename=kw.get("report_filename", "")), d


def test_不勾配图时一次都不调OpenRouter(tmp_path, monkeypatch, with_key):
    def boom(*a, **k):
        raise AssertionError("不该调用配图接口")

    monkeypatch.setattr(deck_images, "request_image", boom)
    monkeypatch.setattr(deck_images.urllib.request, "urlopen", boom)
    monkeypatch.setattr(server.deck, "generate", _fake_generate)
    report = _write_report(str(tmp_path))
    _job, d = _run_deck(server.app.test_client(), {
        "path": report, "output_dir": str(tmp_path), "root": str(tmp_path), "backend": "cli"})
    assert d["ok"] and "images" not in d["result"]
    assert not (tmp_path / "报告.deck.assets").exists()


def test_勾了配图_文件写好_结果里有张数和失败_任务记录里没有Key(tmp_path, monkeypatch, with_key):
    seen_keys = []

    def fake_request(prompt, *, model, api_key, timeout=120):
        seen_keys.append(api_key)
        if "端侧" in prompt:
            raise deck_images.ImageError("OpenRouter 返回 HTTP 500：boom")
        return PNG, "png", 0.04

    monkeypatch.setattr(deck_images, "request_image", fake_request)
    monkeypatch.setattr(server.deck, "generate", _fake_generate)
    report = _write_report(str(tmp_path))
    client = server.app.test_client()
    job, d = _run_deck(client, {
        "path": report, "output_dir": str(tmp_path), "root": str(tmp_path), "backend": "cli",
        "images": True, "image_model": "openai/gpt-image-1-mini"})
    assert d["ok"], d
    img = d["result"]["images"]
    assert (img["planned"], img["generated"], len(img["failed"])) == (3, 2, 1)
    assert img["model"] == "openai/gpt-image-1-mini"
    assert seen_keys == [KEY] * 3
    html = (tmp_path / "报告.deck.html").read_text(encoding="utf-8")
    assert urllib.parse.quote("报告.deck.assets/slide-1.png") in html
    assert (tmp_path / "报告.deck.assets" / "slide-3.png").read_bytes() == PNG
    # Key 不进任务记录、不出现在任何接口返回里
    for url in (f"/api/progress/{job}", f"/api/result/{job}", "/api/jobs"):
        assert KEY not in client.get(url).get_data(as_text=True)
    assert KEY not in json.dumps(server.JOBS[job], ensure_ascii=False, default=str)

    # 笔记洞察直接打开演示（输出目录不在阅读页范围时用）：配图的相对路径解析得到
    r = client.get(f"/deck/{job}/")
    assert r.status_code in (301, 302, 308)
    loc = r.headers["Location"]
    assert loc.endswith("/deck/" + job + "/" + urllib.parse.quote("报告.deck.html"))
    page = client.get(loc)
    assert page.status_code == 200 and "img-src 'self' data:" in page.headers["Content-Security-Policy"]
    asset_url = f"/deck/{job}/" + urllib.parse.quote("报告.deck.assets/slide-1.png")
    a = client.get(asset_url)
    assert a.status_code == 200 and a.data == PNG
    (tmp_path / "报告.deck.assets" / "notes.txt").write_text("x")
    assert client.get(f"/deck/{job}/" + urllib.parse.quote("报告.deck.assets/notes.txt")).status_code == 404
    assert client.get(f"/deck/{job}/" + urllib.parse.quote("报告.deck.assets/../报告.md")).status_code == 404
    assert client.get(f"/deck/{job}/" + urllib.parse.quote("报告.md")).status_code == 404


def test_勾了配图但本机没有Key_直接说清楚(tmp_path, no_key):
    report = _write_report(str(tmp_path))
    r = server.app.test_client().post("/api/deck", json={
        "path": report, "output_dir": str(tmp_path), "root": str(tmp_path), "backend": "cli", "images": True})
    assert r.status_code == 400 and "OpenRouter Key" in r.get_json()["error"]


def test_估算接口按报告章数给张数和价格(tmp_path, with_key):
    report = _write_report(str(tmp_path))
    d = server.app.test_client().get("/api/deck_image_estimate", query_string={
        "path": report, "output_dir": str(tmp_path), "root": str(tmp_path)}).get_json()
    assert d["key_ok"] is True
    assert d["model"] == deck_images.DEFAULT_MODEL
    assert len(deck.outline(REPORT)["chapters"]) == 3
    assert d["count"] == 4
    assert d["total"] == pytest.approx(round(d["per_image"] * d["count"], 2))
    assert KEY not in json.dumps(d)


def test_估算接口不读输出目录之外的文件(tmp_path, with_key):
    r = server.app.test_client().get("/api/deck_image_estimate", query_string={
        "path": "/etc/hosts", "output_dir": str(tmp_path), "root": str(tmp_path)})
    assert r.status_code == 400


def test_prompt_drops_brand_names_so_no_logos_get_drawn():
    p = deck_images._prompt("从算力囤积到RL扩展法则：OpenAI与Anthropic通向2028年算力垄断", "副标题里的概念",
                            "", True)
    assert "OpenAI" not in p and "Anthropic" not in p and "2028" not in p
    assert "算力垄断" in p
    assert "副标题" not in p            # 封面只用标题
    assert "no logos" in p
    en = deck_images._abstract_topic("Why OpenAI and Anthropic will own most compute")
    assert "OpenAI" not in en and en.startswith("Why") and "compute" in en


def test_估算接口的_root_和_output_dir_参数不能当成白名单(tmp_path, with_key, monkeypatch):
    monkeypatch.setenv("SPARK_VAULT", str(tmp_path / "vault"))
    other = tmp_path / "other"
    other.mkdir()
    secret = other / "私密.md"
    secret.write_text("# 私密\n\n## 第一章\n", encoding="utf-8")
    c = server.app.test_client()
    # 请求里把 root 传成祖先目录：以前能过，现在不行
    r = c.get("/api/deck_image_estimate", query_string={
        "path": str(secret), "output_dir": str(tmp_path / "out"), "root": str(tmp_path)})
    assert r.status_code == 400
    # output_dir 传成祖先目录也不行：只放行直接放在输出目录下面的报告
    r = c.get("/api/deck_image_estimate", query_string={"path": str(secret), "output_dir": str(tmp_path)})
    assert r.status_code == 400
    r = c.get("/api/deck_image_estimate", query_string={"path": str(secret), "output_dir": str(other)})
    assert r.status_code == 200


def test_一张都没出就停止_旧图和旧配图字段都保留(tmp_path, monkeypatch):
    assets = tmp_path / "r.deck.assets"
    assets.mkdir()
    (assets / "slide-1.png").write_bytes(PNG)
    d = _deck()
    d["cover_image"] = "r.deck.assets/slide-1.png"
    monkeypatch.setattr(deck_images, "request_image", lambda *a, **k: (PNG, "png", 0))
    out = deck_images.add_images(d, str(tmp_path / "r.deck.html"), api_key=KEY, stop_flag=lambda: True)
    assert out["stopped"] and not out["generated"]
    assert (assets / "slide-1.png").exists()
    assert d["cover_image"] == "r.deck.assets/slide-1.png"


def test_配图中途停止_任务按已停止算_不标成功(tmp_path, monkeypatch, with_key):
    import apps.notes2insight.server as srv
    report = _write_report(str(tmp_path))
    html_path = report[:-3] + ".deck.html"
    with open(html_path, "w", encoding="utf-8") as f:
        f.write("<html>旧演示</html>")
    monkeypatch.setattr(srv.deck, "generate", _fake_generate)
    stopped = {"flag": False}

    def fake_add(d, path, **kw):
        kw["stop_flag"]()   # 模拟停止：一张没出
        return {"model": "m", "planned": 2, "generated": [], "failed": [{"slide": 1, "title": "封面", "error": "停止"}],
                "cost": 0, "assets_dir": "", "stopped": True}
    monkeypatch.setattr(srv.deck_images, "add_images", fake_add)
    _job, status = _run_deck(srv.app.test_client(), {
        "path": report, "output_dir": str(tmp_path), "backend": "cli", "images": True})
    assert status.get("stopped") and not status["ok"]
    with open(html_path, encoding="utf-8") as f:
        assert f.read() == "<html>旧演示</html>"
