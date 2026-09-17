"""
Spark：一个进程、一个端口、一个入口。

    python3 spark.py          （或双击「启动 Spark.command」）

落地页在 /，两个 app 挂在 /summit/ 和 /notes/ 下。用 WSGI 层的
DispatcherMiddleware 按前缀分发，所以两个 app 的 34 条路由一条都不用改写成
blueprint——每个 app 收到的仍然是自己原来的 /api/env 这种路径，只是前端改用了
相对 URL，好让它们在各自的前缀下解析正确。
"""

from __future__ import annotations

import os
import sys
import threading
import time
import webbrowser

from flask import Flask, jsonify, send_from_directory
from werkzeug.middleware.dispatcher import DispatcherMiddleware
from werkzeug.serving import run_simple

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from apps.notes2insight import server as notes_server  # noqa: E402
from apps.summit2md import server as summit_server  # noqa: E402

PORT = int(os.environ.get("SPARK_PORT") or 8760)

APPS = [
    {
        "key": "summit",
        "name": "Summit2MD",
        "path": "/summit/",
        "tagline": "会议 / 播客 → 文字记录、演讲稿、总结",
        "detail": "给一个 YouTube 播放列表或 Substack 播客链接，拉字幕、整理演讲稿、"
                  "逐个议题出小结，最后合成大会或节目总结。产物直接写进笔记库的 Spark 目录。",
        "tasks": ["Summit 总结", "播客跟进", "导入已有目录重新生成", "按主题出聚焦总结"],
    },
    {
        "key": "notes",
        "name": "Notes2Insight",
        "path": "/notes/",
        "tagline": "笔记库 → 技术洞察报告",
        "detail": "按主题检索或手工勾选笔记，逐篇压成摘要卡，跨卡归纳出带证据编号、"
                  "分歧梳理与展望的长篇报告，还能压成交互 HTML / PPTX 演示。",
        "tasks": ["主题检索出报告", "手工选材出报告", "跨会议 / 播客专题", "报告转演示"],
    },
]

hub = Flask(__name__, static_folder=None)


@hub.after_request
def security_headers(resp):
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'")
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


@hub.route("/")
def index():
    return send_from_directory(os.path.join(ROOT, "static"), "index.html")


@hub.route("/static/<path:fname>")
def static_files(fname):
    return send_from_directory(os.path.join(ROOT, "static"), fname)


@hub.route("/api/apps")
def api_apps():
    return jsonify(APPS)


application = DispatcherMiddleware(hub, {
    "/summit": summit_server.app,
    "/notes": notes_server.app,
})


def main() -> None:
    url = f"http://127.0.0.1:{PORT}"
    print(f"Spark：{url}\n  /summit/  Summit2MD\n  /notes/   Notes2Insight\n  按 Ctrl+C 停止")

    if not os.environ.get("SPARK_NO_BROWSER"):
        threading.Thread(target=lambda: (time.sleep(1.2), webbrowser.open(url)),
                         daemon=True).start()
    # threaded=True 是必须的：两个 app 都在后台线程里跑任务，前端同时在轮询进度，
    # 单线程服务器会把轮询和任务卡在一起。
    run_simple("127.0.0.1", PORT, application, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
