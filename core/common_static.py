"""两个 app 共用的前端文件（static/common/）。每个 app 各挂一条 /common/<文件> 路由，
页面用相对路径 common/xxx.js 加载——挂在总入口 /summit/、/notes/ 下，或者单独跑某个
app 时都能加载到同一份文件。"""

from __future__ import annotations

import os

from flask import Flask, send_from_directory

COMMON_STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static", "common")


def register(app: Flask) -> None:
    @app.route("/common/<path:fname>")
    def common_static(fname):
        return send_from_directory(COMMON_STATIC_DIR, fname)
