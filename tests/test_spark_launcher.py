import socket
import sys, os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import spark


def test_落地页与接口都能响应():
    c = spark.app.test_client()
    assert c.get("/").status_code == 200
    assert c.get("/static/app.js").status_code == 200
    apps = c.get("/api/apps").get_json()
    assert {a["key"] for a in apps} == {"summit", "notes"}
    assert all(a["url"].startswith("http://127.0.0.1:") for a in apps)


def test_端口探测():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    port = s.getsockname()[1]
    try:
        assert spark.port_busy(port) is True
    finally:
        s.close()
    assert spark.port_busy(port) is False


def test_端口已占用时不重复拉起(monkeypatch):
    """两个 app 已经在跑时再点启动器，应该直接沿用，而不是起第二份去抢同一个输出目录。"""
    spawned = []
    monkeypatch.setattr(spark, "port_busy", lambda port: True)
    monkeypatch.setattr(spark.subprocess, "Popen",
                        lambda *a, **k: spawned.append(a) or None)
    spark.start_apps()
    assert spawned == []


def test_只停自己拉起来的进程(monkeypatch):
    """沿用的外部进程不进 _children，所以退出时不会把用户自己开的服务一起杀掉。"""
    monkeypatch.setattr(spark, "port_busy", lambda port: True)
    monkeypatch.setattr(spark, "_children", [])
    spark.start_apps()
    assert spark._children == []
