"""全局设置（core/settings.py）和设置页接口 /api/settings。

conftest 已经把 SPARK_SETTINGS_FILE 指到临时目录里一个还不存在的文件，这里的测试
都在那份临时设置上读写，碰不到 ~/.spark/settings.json。
"""

import json
import os
import sys
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import spark
from core import keys, llm_config, settings, vault


def _client():
    from werkzeug.test import Client
    return Client(spark.application)


def _path():
    return settings.settings_path()


def _write_raw(obj):
    os.makedirs(os.path.dirname(_path()), exist_ok=True)
    with open(_path(), "w", encoding="utf-8") as f:
        f.write(obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False))


@pytest.fixture
def no_env(monkeypatch):
    for name in ("SPARK_VAULT", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------- 读写与版本

def test_没有设置文件时全部是空_也就是内置默认(no_env):
    assert not os.path.exists(_path())
    s, status = settings.load_with_status()
    assert s == settings.builtin()
    assert s["version"] == settings.CURRENT_VERSION == 1
    assert status["exists"] is False and status["notes"] == []
    # 行为跟没有这个功能时一样
    assert vault.vault_root() == vault.DEFAULT_VAULT
    assert settings.output_dir("notes") == vault.reports_dir()
    assert settings.backend("api") == "api" and settings.backend("cli") == "cli"


def test_保存后读回来_带版本号_原子写不留临时文件(tmp_path, no_env):
    out = str(tmp_path / "输出")
    settings.save({"storage": {"summit_output_dir": out}, "ai": {"backend": "openrouter"}})
    with open(_path(), encoding="utf-8") as f:
        on_disk = json.load(f)
    assert on_disk["version"] == 1
    assert on_disk["storage"]["summit_output_dir"] == out
    assert settings.get("ai.backend") == "openrouter"
    assert [n for n in os.listdir(os.path.dirname(_path())) if n.endswith(".tmp")] == []
    # 部分保存只改给出的项，其它保留
    settings.save({"ai": {"summary_length": "long"}})
    assert settings.get("ai.backend") == "openrouter"
    assert settings.get("storage.summit_output_dir") == out


def test_老格式_没有版本号_会迁移(no_env):
    _write_raw({"vault_root": "~/库", "backend": "cli", "summary_length": "short"})
    s, status = settings.load_with_status()
    assert s["version"] == 1
    assert s["storage"]["vault_root"] == "~/库"
    assert s["ai"]["backend"] == "cli" and s["ai"]["summary_length"] == "short"
    assert status["exists"]


def test_文件里不合规的项按没设处理_不影响其它项(no_env):
    _write_raw({"version": 1, "storage": {"vault_root": "相对路径"}, "ai": {"backend": "gpt", "summary_length": "long"}})
    s, status = settings.load_with_status()
    assert s["storage"]["vault_root"] == "" and s["ai"]["backend"] == ""
    assert s["ai"]["summary_length"] == "long"
    assert status["notes"] and "不合规" in status["notes"][0]


def test_更新版本写的文件只读认识的部分(no_env):
    _write_raw({"version": 99, "ai": {"backend": "ollama"}, "新东西": 1})
    s, status = settings.load_with_status()
    assert s["version"] == 1 and s["ai"]["backend"] == "ollama"
    assert any("v99" in n for n in status["notes"])


def test_坏文件按默认处理_保存前先另存一份(no_env):
    _write_raw("{这不是 json")
    s, status = settings.load_with_status()
    assert s == settings.builtin() and status["exists"] and status["notes"]
    settings.save({"ai": {"backend": "api"}})
    folder = os.path.dirname(_path())
    assert any(n.startswith("settings.json.broken-") for n in os.listdir(folder))
    assert settings.get("ai.backend") == "api"


def test_迁移表覆盖到当前版本():
    for v in range(settings.CURRENT_VERSION):
        assert v in settings.MIGRATIONS


# ---------------------------------------------------------------- 先后顺序

def test_任务值_优先于设置_优先于内置默认(no_env):
    assert settings.pick(None, "ai.summary_length", "medium") == "medium"
    settings.save({"ai": {"summary_length": "short", "max_transcript_chars": 0}})
    assert settings.pick(None, "ai.summary_length", "medium") == "short"
    assert settings.pick("", "ai.summary_length", "medium") == "short"
    assert settings.pick("long", "ai.summary_length", "medium") == "long"
    # 0 是"不限制"，是一个明确的值，不能被当成没设
    assert settings.pick(None, "ai.max_transcript_chars", 120000) == 0


def test_环境变量压过设置里的笔记库_设置页标出来(tmp_path, monkeypatch, no_env):
    settings.save({"storage": {"vault_root": str(tmp_path / "设置里的库")}})
    assert vault.vault_root() == str(tmp_path / "设置里的库")
    f = settings.describe()["fields"]["storage.vault_root"]
    assert f["source"] == "settings"

    monkeypatch.setenv("SPARK_VAULT", str(tmp_path / "环境变量的库"))
    assert vault.vault_root() == str(tmp_path / "环境变量的库")
    f = settings.describe()["fields"]["storage.vault_root"]
    assert (f["source"], f["env_var"]) == ("env", "SPARK_VAULT")
    assert f["saved"] == str(tmp_path / "设置里的库")          # 保存的值还在，只是没生效
    assert f["value"] == str(tmp_path / "环境变量的库")


def test_模型后端_请求里给的优先_没给才用设置(no_env):
    settings.save({"ai": {"backend": "ollama", "models": {"ollama": "qwen2.5", "cli": "opus"},
                          "api_bases": {"ollama": "http://127.0.0.1:9999"}}})
    cfg = llm_config.resolve({}, default_backend="api")
    assert (cfg["backend"], cfg["model"], cfg["api_base"]) == ("ollama", "qwen2.5", "http://127.0.0.1:9999")
    cfg = llm_config.resolve({"backend": "ollama", "model": "llama3", "api_base": "http://h:1"}, default_backend="api")
    assert (cfg["model"], cfg["api_base"]) == ("llama3", "http://h:1")
    # 表单上明确留空（CLI 的「跟随 CLI 自己的设置」）不被设置顶掉
    cfg = llm_config.resolve({"backend": "cli", "model": ""}, default_backend="cli")
    assert cfg["model"] == ""
    cfg = llm_config.resolve({"backend": "cli"}, default_backend="cli")
    assert cfg["model"] == "opus"


def test_任务上的值不会写回设置文件(tmp_path, no_env):
    from apps.summit2md import pipeline as summit_pipeline
    from apps.summit2md import server as summit_server
    settings.save({"storage": {"summit_output_dir": str(tmp_path / "设置目录")}, "ai": {"backend": "cli"}})
    before = open(_path(), "rb").read()
    mtime = os.stat(_path()).st_mtime_ns

    c = summit_server.app.test_client()
    with mock.patch.object(summit_pipeline, "probe_overall_summary", return_value=False) as probe:
        c.post("/api/existing_summary", json={"summit_title": "T", "output_dir": str(tmp_path / "任务目录")})
        assert probe.call_args[0][0] == os.path.realpath(str(tmp_path / "任务目录"))
        c.post("/api/existing_summary", json={"summit_title": "T"})
        assert probe.call_args[0][0] == os.path.realpath(str(tmp_path / "设置目录"))
    llm_config.resolve({"backend": "api", "api_key": "sk-task"}, default_backend="api")

    assert open(_path(), "rb").read() == before
    assert os.stat(_path()).st_mtime_ns == mtime


# ---------------------------------------------------------------- 接口

def test_接口_读和写(tmp_path, no_env):
    c = _client()
    d = c.get("/api/settings").get_json()
    assert d["version"] == 1 and d["exists"] is False
    assert {b["key"] for b in d["backends"]} == set(settings.BACKENDS)
    assert d["fields"]["storage.vault_root"]["source"] == "default"

    out = str(tmp_path / "报告")
    r = c.post("/api/settings", json={"settings": {"storage": {"notes_output_dir": out},
                                                   "ai": {"backend": "api", "max_transcript_chars": "50000"}}})
    assert r.status_code == 200, r.get_json()
    d = r.get_json()
    assert d["exists"] is True
    assert d["fields"]["storage.notes_output_dir"] == {
        "saved": out, "value": out, "default": vault.reports_dir(), "source": "settings", "env_var": ""}
    assert d["settings"]["ai"]["max_transcript_chars"] == 50000


@pytest.mark.parametrize("patch, field", [
    ({"storage": {"vault_root": "relative/path"}}, "storage.vault_root"),
    ({"storage": {"summit_output_dir": "/"}}, "storage.summit_output_dir"),
    ({"storage": {"track_output_dir": "~"}}, "storage.track_output_dir"),
    ({"storage": {"podcast_output_dir": "/a/../etc"}}, "storage.podcast_output_dir"),
    ({"storage": {"notes_output_dir": 3}}, "storage.notes_output_dir"),
    ({"ai": {"backend": "gpt-5"}}, "ai.backend"),
    ({"ai": {"summary_length": "huge"}}, "ai.summary_length"),
    ({"ai": {"max_transcript_chars": "-1"}}, "ai.max_transcript_chars"),
    ({"ai": {"lang_prefs": "en; rm -rf"}}, "ai.lang_prefs"),
    ({"ai": {"api_bases": {"ollama": "file:///etc/passwd"}}}, "ai.api_bases.ollama"),
    ({"ai": {"api_key": "sk-ant-xxx"}}, "ai.api_key"),
    ({"ai": {"models": {"gpt": "x"}}}, "ai.models.gpt"),
])
def test_接口_拒绝不合规的值_什么都不写(patch, field, no_env):
    c = _client()
    r = c.post("/api/settings", json={"settings": patch})
    assert r.status_code == 400
    assert field in r.get_json()["errors"]
    assert not os.path.exists(_path())


def test_接口_请求格式不对():
    c = _client()
    assert c.post("/api/settings", json={"vault_root": "/x"}).status_code == 400
    assert c.post("/api/settings", data="nope", content_type="text/plain").status_code == 400


def test_接口_拒绝跨站写入():
    c = _client()
    r = c.post("/api/settings", json={"settings": {"ai": {"backend": "cli"}}},
               headers={"Host": "127.0.0.1:8760", "Origin": "https://evil.example"})
    assert r.status_code == 403
    assert not os.path.exists(_path())


def test_key_状态只说来源_不带内容(tmp_path, monkeypatch, no_env):
    kdir = tmp_path / "keys"
    kdir.mkdir()
    (kdir / "openrouter.key").write_text("sk-or-FILE-SECRET-123", encoding="utf-8")
    monkeypatch.setattr(keys, "SEARCH_DIRS", (str(kdir),))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-ENV-SECRET-456")
    r = _client().get("/api/settings")
    body = r.get_data(as_text=True)
    assert "ENV-SECRET" not in body and "FILE-SECRET" not in body
    k = r.get_json()["keys"]
    assert k["api"]["set"] and "环境变量 ANTHROPIC_API_KEY" in k["api"]["label"]
    assert k["openrouter"]["set"] and "本机 key 文件" in k["openrouter"]["label"]
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    k = _client().get("/api/settings").get_json()["keys"]
    assert k["api"]["set"] is False and k["api"]["label"].startswith("未设置")


# ---------------------------------------------------------------- 各 app 用上保存的默认值

def test_summit_的表单默认值来自设置(tmp_path, no_env):
    from apps.summit2md import server as summit_server
    c = summit_server.app.test_client()
    before = c.get("/api/env").get_json()
    assert before["defaults"]["backend"] == "api"
    assert before["defaults"]["from_settings"]["backend"] is False

    dirs = {m: str(tmp_path / m) for m in ("summit", "podcast", "track")}
    settings.save({"storage": {f"{m}_output_dir": p for m, p in dirs.items()},
                   "ai": {"backend": "openrouter", "models": {"openrouter": "~x/y"},
                          "overall_model": "big", "summary_length": "long", "lang_prefs": "ja"}})
    d = c.get("/api/env").get_json()["defaults"]
    assert d["output_dirs"] == dirs
    assert (d["backend"], d["models"]["openrouter"], d["overall_model"]) == ("openrouter", "~x/y", "big")
    assert (d["summary_length"], d["lang_prefs"]) == ("long", "ja")
    assert d["from_settings"]["summary_length"] is True
    assert summit_server._default_output_dir("podcast") == dirs["podcast"]
    assert summit_server._default_output_dir("track") == dirs["track"]


def test_笔记洞察的默认值来自设置(tmp_path, no_env):
    from apps.notes2insight import server as notes_server
    c = notes_server.app.test_client()
    lib, out = tmp_path / "库", tmp_path / "报告"
    lib.mkdir()
    settings.save({"storage": {"vault_root": str(lib), "notes_output_dir": str(out)},
                   "ai": {"models": {"cli": "opus"}, "max_transcript_chars": 50000}})
    env = c.get("/api/env").get_json()
    assert env["default_vault"] == str(lib)
    assert env["default_output"] == str(out)
    assert env["defaults"]["backend"] == "cli"
    assert env["defaults"]["from_settings"]["vault_root"] is True
    assert env["defaults"]["from_settings"]["output_dirs"] == {"notes": True}
    assert notes_server._out_dir({"output_dir": ""}) == str(out)
    assert notes_server._out_dir({"output_dir": str(tmp_path / "这次")}) == str(tmp_path / "这次")


def test_阅读页跟着设置里的笔记库走(tmp_path, no_env):
    lib = tmp_path / "库"
    (lib / "Spark").mkdir(parents=True)
    settings.save({"storage": {"vault_root": str(lib)}})
    assert vault.spark_dir() == str(lib / "Spark")


# ---------------------------------------------------------------- 页面与入口

def test_设置页和各处入口():
    c = _client()
    page = c.get("/settings")
    assert page.status_code == 200
    text = page.get_data(as_text=True)
    for heading in ("存储与输出", "AI 默认值", "阅读体验"):
        assert heading in text
    assert c.get("/static/settings.js").status_code == 200
    assert 'href="/settings"' in c.get("/").get_data(as_text=True)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for app in ("summit2md", "notes2insight"):
        with open(os.path.join(root, "apps", app, "static", "index.html"), encoding="utf-8") as f:
            assert 'href="/settings"' in f.read(), app


def test_设置页阅读体验_跟阅读页共用浏览器本地偏好():
    c = _client()
    r = c.get("/settings")
    text = r.get_data(as_text=True)
    assert 'id="reading"' in text, "阅读页「Aa」里的「更多字体」链到 /settings#reading"
    assert "仅此浏览器" in text
    for el in ('id="read-theme"', 'id="read-font"', 'id="read-custom"', 'id="read-size"',
               'id="read-width"', 'id="read-preview"'):
        assert el in text, el
    assert "已安装字体名称" in text
    # 预览里中英文都有
    preview = text.split('id="read-preview"')[1].split("</div>")[0]
    assert 'lang="en"' in preview and "排版" in preview
    # 共用脚本要在 settings.js 之前加载；CSS 在页面自己的 <style> 之前，页面 token 优先
    assert text.index("/static/common/read-prefs.js") < text.index("/static/settings.js")
    assert text.index("/static/common/read-prefs.css") < text.index("<style>")
    assert c.get("/static/common/read-prefs.js").status_code == 200
    # 设置页的 CSP 不变：不许内联脚本
    csp = r.headers["Content-Security-Policy"]
    assert "script-src 'self';" in csp
    assert "<script>" not in text


def test_阅读页顶栏有设置链接(tmp_path, monkeypatch):
    monkeypatch.setenv("SPARK_VAULT", str(tmp_path))
    (tmp_path / "Spark").mkdir()
    r = _client().get("/read/")
    assert r.status_code == 200
    assert 'href="/settings"' in r.get_data(as_text=True)
