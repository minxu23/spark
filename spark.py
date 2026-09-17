"""
Spark 统一启动器。

双击「启动 Spark.command」或 `python3 spark.py`：把两个 app 各自拉起来，再开一个
落地页让你选这次要做什么。关掉终端窗口（或 Ctrl+C）就把它们一起停掉。

目前两个 app 仍然是各自的进程、各自的端口——这一步统一的是"入口"，不是后端。
端口已经被占用时不会重复拉起，直接当成"已经在跑"接管显示。
"""

from __future__ import annotations

import atexit
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import webbrowser

from flask import Flask, jsonify, send_from_directory

ROOT = os.path.dirname(os.path.abspath(__file__))
HUB_PORT = int(os.environ.get("SPARK_PORT") or 8760)

APPS = [
    {
        "key": "summit",
        "name": "Summit2MD",
        "port": 8765,
        "cwd": os.path.join(ROOT, "apps", "summit2md"),
        "cmd": [sys.executable, "server.py"],
        "tagline": "会议 / 播客 → 文字记录、演讲稿、总结",
        "detail": "给一个 YouTube 播放列表或 Substack 播客链接，拉字幕、整理演讲稿、"
                  "逐个议题出小结，最后合成大会或节目总结。产物直接写进笔记库的 Spark 目录。",
        "tasks": ["Summit 总结", "播客跟进", "导入已有目录重新生成", "按主题出聚焦总结"],
    },
    {
        "key": "notes",
        "name": "Notes2Insight",
        "port": 8766,
        "cwd": os.path.join(ROOT, "apps", "notes2insight"),
        "cmd": [sys.executable, "launch.py"],
        "tagline": "笔记库 → 技术洞察报告",
        "detail": "按主题检索或手工勾选笔记，逐篇压成摘要卡，跨卡归纳出带证据编号、"
                  "分歧梳理与展望的长篇报告，还能压成交互 HTML / PPTX 演示。",
        "tasks": ["主题检索出报告", "手工选材出报告", "跨会议 / 播客专题", "报告转演示"],
    },
]

app = Flask(__name__, static_folder=None)
_children: list[subprocess.Popen] = []


def port_busy(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.25)
        return s.connect_ex(("127.0.0.1", port)) == 0


def start_apps() -> None:
    for a in APPS:
        if port_busy(a["port"]):
            print(f"  {a['name']}：{a['port']} 端口已经在跑，直接沿用")
            continue
        print(f"  {a['name']}：启动中……")
        _children.append(subprocess.Popen(a["cmd"], cwd=a["cwd"],
                                          stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT))


def stop_apps() -> None:
    for p in _children:
        if p.poll() is None:
            p.terminate()
    deadline = time.time() + 5
    for p in _children:
        while p.poll() is None and time.time() < deadline:
            time.sleep(0.1)
        if p.poll() is None:
            p.kill()


atexit.register(stop_apps)


@app.after_request
def security_headers(resp):
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'")
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


@app.route("/")
def index():
    return send_from_directory(os.path.join(ROOT, "static"), "index.html")


@app.route("/static/<path:fname>")
def static_files(fname):
    return send_from_directory(os.path.join(ROOT, "static"), fname)


@app.route("/api/apps")
def api_apps():
    """落地页要显示每个 app 起没起来。在服务端探端口，免得页面去跨源请求。"""
    return jsonify([
        {**{k: a[k] for k in ("key", "name", "port", "tagline", "detail", "tasks")},
         "url": f"http://127.0.0.1:{a['port']}",
         "up": port_busy(a["port"])}
        for a in APPS
    ])


def main() -> None:
    print("Spark 启动中……")
    start_apps()

    def open_when_ready() -> None:
        if os.environ.get("SPARK_NO_BROWSER"):   # 自动化/测试时别弹浏览器
            return
        for _ in range(40):
            if port_busy(HUB_PORT):
                webbrowser.open(f"http://127.0.0.1:{HUB_PORT}")
                return
            time.sleep(0.25)

    threading.Thread(target=open_when_ready, daemon=True).start()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: sys.exit(0))
    print(f"  落地页：http://127.0.0.1:{HUB_PORT}\n  按 Ctrl+C 停止全部服务")
    app.run(host="127.0.0.1", port=HUB_PORT, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
