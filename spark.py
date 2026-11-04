"""
Spark：一个进程、一个端口、一个入口。

    python3 spark.py          （或双击「启动 Spark.command」）

落地页在 /，两个 app 挂在 /summit/ 和 /notes/ 下，产物阅读页挂在 /read/，
全局设置页在 /settings（读写 core/settings.py 管的那份设置文件）。用 WSGI 层的
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

from flask import Flask, jsonify, request, send_from_directory
from werkzeug.middleware.dispatcher import DispatcherMiddleware
from werkzeug.serving import run_simple

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from apps.notes2insight import server as notes_server  # noqa: E402
from apps.reader import server as reader_server  # noqa: E402
from apps.review import server as review_server  # noqa: E402
from apps.summit2md import server as summit_server  # noqa: E402
from core import cable_sync  # noqa: E402
from core import github_sync  # noqa: E402
from core import model_catalog  # noqa: E402
from core import remote_tasks  # noqa: E402
from core import review  # noqa: E402
from core import settings as core_settings  # noqa: E402
from core import web_guard  # noqa: E402

PORT = int(os.environ.get("SPARK_PORT") or 8760)

# 首页两列：第一行是两个一次性出报告的（Summit、Notes2Insight），第二行是两个
# 持续跟进的（Podcast、信息跟进），最后是回头看的复盘。
APPS = [
    {
        "key": "summit",
        "name": "Summit 总结",
        "path": "/summit/?mode=summit",
        "tagline": "会议 / 峰会 → 逐议题小结 + 大会总结",
        "detail": "给一个 YouTube 峰会播放列表，拉字幕、整理演讲稿、逐个议题出小结，"
                  "最后合成一份大会总结。可以按会议官网的议程重新排序，产物直接写进笔记库。",
        "tasks": ["大会总结", "按议程排序", "按主题出聚焦总结", "导入已有目录重跑"],
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
    {
        "key": "podcast",
        "name": "Podcast 跟进",
        "path": "/summit/?mode=series",
        "tagline": "订阅播客节目，一档节目一个文件夹",
        "detail": "订阅 Substack 播客或 YouTube 节目频道，有新单集就列出来；选中的整理成转写稿和逐期小结，"
                  "再合成整档节目的总结。适合长期跟几档固定的节目。",
        "tasks": ["节目总结", "逐期小结", "按播出日期命名", "导入已有目录重跑"],
    },
    {
        # 跟「Podcast 跟进」并列的新入口，暂时不是替代关系：两边现在共用同一套
        # 处理逻辑（RSS/播客/YouTube频道/微信公众号），只是定位更宽、主题色换成
        # 橙红。等订阅列表、分类、打开自动检查这些「信息跟进」独有的功能做完，
        # 再回头看要不要把「Podcast 跟进」收掉。
        "key": "track",
        "name": "信息跟进",
        "path": "/summit/?mode=track",
        "tagline": "订阅各种资讯源，逐条出笔记",
        "detail": "订阅 RSS/Atom、博客、播客、YouTube 频道，或者贴微信公众号文章，新内容逐条整理成笔记和小结，"
                  "再合成简报。适合来源杂、按条读的信息流。",
        "tasks": ["逐条小结", "汇总", "按发布日期命名", "导入已有目录重跑"],
    },
    {
        "key": "review",
        "name": "复盘",
        "path": "/review/",
        "tagline": "回头看读过的、一个主题的演变、当初的判断",
        "detail": "按电脑和手机上的阅读记录做阅读复盘；按主题理出来龙去脉；把较早节目里嘉宾的判断和预测，"
                  "对照后来的内容看应验了没有。写进笔记库，手机上也能看。",
        "tasks": ["阅读复盘", "主题来龙去脉", "观点检验"],
    },
]

hub = Flask(__name__, static_folder=None)
web_guard.install(hub)


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


@hub.route("/settings")
def settings_page():
    return send_from_directory(os.path.join(ROOT, "static"), "settings.html")


@hub.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    """GET：保存的设置 + 实际生效的值和来源 + 各后端 key 的状态（只有状态，没有内容）。
    POST：{"settings": {...}}，完整或部分都行，校验通过才写；跨站请求由 web_guard 挡掉。"""
    if request.method == "POST":
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or not isinstance(data.get("settings"), dict):
            return jsonify({"error": "请求格式不对，应该是 {\"settings\": {...}}"}), 400
        try:
            core_settings.save(data["settings"])
        except core_settings.SettingsError as e:
            return jsonify({"error": "有几项没通过校验，没有保存", "errors": e.errors}), 400
        except OSError as e:
            return jsonify({"error": f"写设置文件失败：{e}"}), 500
    return jsonify(core_settings.describe())


@hub.route("/api/sync", methods=["GET", "POST"])
def api_sync():
    """GitHub 同步。GET：仓库地址、是否在同步、上一次的结果；POST：现在同步一次（后台跑）。"""
    if request.method == "POST":
        if not github_sync.repo_url():
            return jsonify({"error": "先在上面填好 GitHub 仓库地址并保存"}), 400
        github_sync.start()
    return jsonify(dict(github_sync.status(), remote_tasks=remote_tasks.status()))


@hub.route("/api/activity")
def api_activity():
    """Mac App 用：有任务在跑时不让电脑休眠，退出前也要提醒。"""
    tables = (summit_server.JOBS, summit_server.TRACK_JOBS, summit_server.SIMPLE_JOBS,
              notes_server.JOBS, review_server.JOBS)
    jobs = sum(not job.get("done") for table in tables for job in list(table.values()))
    syncing = bool(github_sync.status().get("running") or cable_sync.status().get("running"))
    return jsonify({"jobs": jobs, "syncing": syncing})


@hub.route("/api/sync/tasks", methods=["POST"])
def api_sync_tasks():
    """设置页「现在检查」：马上看一轮手机上添加的任务（后台跑，结果在 /api/sync 的 remote_tasks 里）。"""
    problem = remote_tasks.trigger()
    if problem:
        return jsonify({"error": problem}), 400
    return jsonify(dict(github_sync.status(), remote_tasks=dict(remote_tasks.status(), checking=True)))


@hub.route("/api/cable", methods=["GET", "POST"])
def api_cable():
    """数据线同步到手机。GET：能同步的设备和上一次的结果；POST {"device": id}：现在同步（后台跑）。"""
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        problem = cable_sync.start(str(data.get("device") or ""))
        if problem:
            return jsonify({"error": problem}), 400
    try:
        devices, device_error = cable_sync.list_devices(), ""
    except cable_sync.CableError as e:
        devices, device_error = [], str(e)
    return jsonify(dict(cable_sync.status(), devices=devices, device_error=device_error))


@hub.route("/api/model_catalog", methods=["GET", "POST"])
def api_model_catalog():
    """模型下拉框的 Anthropic 模型目录。GET 用缓存（后台每天刷新）；POST 立即联网刷新一次。"""
    if request.method == "POST":
        model_catalog.refresh()
    return jsonify(model_catalog.catalog(allow_fetch=not os.path.exists(model_catalog.cache_path())))


application = DispatcherMiddleware(hub, {
    "/summit": summit_server.app,
    "/notes": notes_server.app,
    "/read": reader_server.app,
    "/review": review_server.app,
})


def local_call(method: str, path: str, body=None):
    """在本进程里调 Spark 自己的接口（手机任务队列用），跟页面发的请求走同一套校验和任务表。"""
    from werkzeug.test import Client
    resp = Client(application).open(path, method=method, json=body)
    return resp.status_code, resp.get_json(silent=True)


def main() -> None:
    url = f"http://127.0.0.1:{PORT}"
    print(f"Spark：{url}\n  /summit/  Summit2MD\n  /notes/   Notes2Insight\n  /read/    在网页里读 Spark 产物\n  按 Ctrl+C 停止")

    if not os.environ.get("SPARK_NO_BROWSER"):
        threading.Thread(target=lambda: (time.sleep(1.2), webbrowser.open(url)),
                         daemon=True).start()
    # 模型下拉框里的 Anthropic 模型列表每天联网刷新一次
    model_catalog.start_background_refresh()
    # 手机上添加的任务：设置里开了才会去 GitHub 看，没开就空转
    remote_tasks.start(local_call)
    # 数据线同步时从手机发件箱收来的任务，也用同一个函数调本进程的接口去跑
    cable_sync.configure(local_call)
    # 用数据线插上手机就自动同步：带回手机上添加的任务、生成好的复盘和报告、阅读记录
    cable_sync.start_auto()
    # 复盘的主题检索走 Notes2Insight 的接口
    review.configure(local_call)
    # threaded=True 是必须的：两个 app 都在后台线程里跑任务，前端同时在轮询进度，
    # 单线程服务器会把轮询和任务卡在一起。
    run_simple("127.0.0.1", PORT, application, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
