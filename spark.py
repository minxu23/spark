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
from core import cleanup  # noqa: E402
from core import clip_capture  # noqa: E402
from core import tags as core_tags  # noqa: E402
from core import drive_sync  # noqa: E402
from core import github_sync  # noqa: E402
from core import model_catalog  # noqa: E402
from core import remote_tasks  # noqa: E402
from core import review  # noqa: E402
from core import search_index  # noqa: E402
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
        "name": "信息",
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


def _other_jobs() -> int:
    """各模块在跑的任务，不算清理工具（清理工具问本机模型前看它，自己不能挡自己）。"""
    tables = (summit_server.JOBS, summit_server.TRACK_JOBS, summit_server.SIMPLE_JOBS,
              notes_server.JOBS, review_server.JOBS)
    jobs = sum(not job.get("done") for table in tables for job in list(table.values()))
    return jobs + clip_capture.active_count() + core_tags.active_count()


def _job_count() -> int:
    return _other_jobs() + cleanup.active()


def _syncing() -> bool:
    """在不在同步（GitHub、数据线、Google Drive）：定期清理这时候不做。"""
    cable = cable_sync.status()
    return bool(github_sync.status().get("running") or cable.get("running") or cable.get("tasks_running")
                or drive_sync.is_running())


# 阅读页起草读后感时看一眼：本机 Ollama 有任务在跑就先不起草
reader_server.busy_jobs = _job_count
# 清理工具：有任务在跑时不清理、不问本机模型；正在同步时不做定期清理
cleanup.configure(busy_jobs=_other_jobs, syncing=_syncing)


@hub.route("/api/activity")
def api_activity():
    """Mac App 用：有任务在跑时不让电脑休眠，退出前也要提醒。"""
    jobs = _job_count()
    cable = cable_sync.status()
    # 在同步什么，说清楚：以前只有一个「正在同步」，GitHub 推大提交时看着像手机一直没同步完
    syncs = []
    gh = github_sync.status()
    if gh.get("running"):
        syncs.append(gh.get("phase") or "推到 GitHub")
    if cable.get("running") and not cable.get("quick"):
        syncs.append(f"跟手机同步：{cable.get('phase') or '开始'}")
    if drive_sync.is_running():
        syncs.append("Google Drive 同步")
    # 手机：上次看过手机是什么时候、带回了什么（每分钟看一眼发件箱那种不算「正在同步」，一两秒就完）
    checked = cable.get("checked") or {}
    phone = {}
    if checked.get("finished_at"):
        phone = {"device": checked.get("device") or "", "device_id": checked.get("device_id") or "",
                 "at": checked["finished_at"], "ok": bool(checked.get("ok")), "error": checked.get("error") or "",
                 "saved": checked.get("saved") or 0, "tasks": checked.get("tasks_received") or 0,
                 "checking": bool(cable.get("running"))}
    return jsonify({"jobs": jobs, "syncing": bool(syncs), "syncs": syncs, "phone": phone,
                    "phone_tasks": bool(cable.get("tasks_running"))})


@hub.route("/api/sync/stop", methods=["POST"])
def api_sync_stop():
    """暂停 GitHub 同步：停掉正在推的 git，推上去的批次留着，下次「现在同步」接着推剩下的。"""
    github_sync.stop()
    return jsonify(dict(github_sync.status(), remote_tasks=remote_tasks.status()))


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
        problem = cable_sync.start(str(data.get("device") or ""), quick=bool(data.get("quick")))
        if problem:
            return jsonify({"error": problem}), 400
    try:
        devices, device_error = cable_sync.list_devices(), ""
    except cable_sync.CableError as e:
        devices, device_error = [], str(e)
    # simulators：开发开关（SPARK_CABLE_SIMULATORS=1）开没开，没开时设置页不显示模拟器相关的入口
    return jsonify(dict(cable_sync.status(), devices=devices, device_error=device_error,
                        simulators=cable_sync.simulators_allowed()))


@hub.route("/api/drive", methods=["GET", "POST"])
def api_drive():
    """用 Google Drive 文件夹跟手机同步。GET：开没开、用哪个文件夹、上一次的结果；
    POST {"enabled": true/false} 打开或关掉（打开时马上同步一次），POST {} 现在同步一次（后台跑）。"""
    if request.method == "POST":
        data = request.get_json(silent=True)
        data = data if isinstance(data, dict) else {}
        if "enabled" in data:
            if not isinstance(data["enabled"], bool):
                return jsonify({"error": "enabled 要填 true 或 false"}), 400
            drive_sync.save_config(enabled=data["enabled"])
        # 没打开时不同步：空的 POST 不能把整个笔记库拷进 Google Drive
        if drive_sync.load_config().get("enabled"):
            if not drive_sync.sync_folder():
                return jsonify({"error": "这台电脑没装 Google Drive 桌面版，装好并登录后再打开"}), 400
            problem = drive_sync.start()
            if problem:
                return jsonify({"error": problem}), 400
    return jsonify(drive_sync.status())


@hub.route("/cleanup")
def cleanup_page():
    return send_from_directory(os.path.join(ROOT, "static"), "cleanup.html")


@hub.route("/api/cleanup")
def api_cleanup():
    """清理工具：设置、上次结果、最近一次扫描、在跑的后台任务。"""
    try:
        return jsonify(cleanup.status())
    except cleanup.CleanupError as e:
        return jsonify({"error": str(e)}), 500


@hub.route("/api/cleanup/config", methods=["POST"])
def api_cleanup_config():
    data = request.get_json(silent=True) or {}
    try:
        cleanup.set_config(str(data.get("schedule") or ""))
    except cleanup.CleanupError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(cleanup.status())


@hub.route("/api/cleanup/confirm", methods=["POST"])
def api_cleanup_confirm():
    """清理工具的记录坏过：你在页面上确认之后，定期清理接着做。"""
    return jsonify(cleanup.confirm_state())


@hub.route("/api/cleanup/confirm_edits", methods=["POST"])
def api_cleanup_confirm_edits():
    """修改记录坏过：你确认「手机废纸篓里的文章可能失去图片保护，仍要继续」之后，清理才接着移图片。"""
    return jsonify(cleanup.confirm_edits())


@hub.route("/api/cleanup/release", methods=["POST"])
def api_cleanup_release():
    """POST {"doc": 相对路径}：手机上已经没有这篇了，去掉墓碑里它记着的图片（不移走图片）。"""
    data = request.get_json(silent=True) or {}
    try:
        return jsonify(cleanup.release_held(str(data.get("doc") or "")))
    except cleanup.CleanupError as e:
        return jsonify({"error": str(e)}), 400


@hub.route("/api/cleanup/run", methods=["POST"])
def api_cleanup_run():
    """POST {"kind": "scan"} 深度扫描；{"kind": "regular"} 现在做一次定期清理；
    {"kind": "apply", "choices": {...}} 按选择清理（见 core/cleanup.apply）。都在后台跑。"""
    data = request.get_json(silent=True) or {}
    kind = data.get("kind")
    if kind not in ("scan", "regular", "apply"):
        return jsonify({"error": "kind 只能是 scan / regular / apply"}), 400
    choices = data.get("choices") if isinstance(data.get("choices"), dict) else {}
    try:
        cleanup.start_job(kind, choices)
    except cleanup.CleanupError as e:
        return jsonify({"error": str(e)}), 409
    return jsonify(cleanup.status())


@hub.route("/api/cleanup/image")
def api_cleanup_image():
    """清理页的缩略图：只给 attachments/ 里的图片。"""
    from flask import send_file
    from core import vault
    root = os.path.realpath(vault.vault_root())
    full = cleanup._safe_target(root, str(request.args.get("path") or ""))
    if not full or full.lower().endswith(".svg"):
        return jsonify({"error": "没有这张图"}), 404
    return send_file(full, max_age=0)


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
    # 打开了 Google Drive 同步就隔一阵同步一次（没打开就空转）
    drive_sync.start_auto()
    # 定期清理：按设置每周或每月一次，只清确定没人用的图片（关了就空转）
    cleanup.start_auto()
    # 复盘的主题检索走 Notes2Insight 的接口
    review.configure(local_call)
    # 阅读页搜索用的全文索引：后台建或刷新一次（没建好之前搜索照旧逐个文件扫）
    try:
        search_index.start_background_refresh()
    except Exception as e:   # noqa: BLE001  索引起不来不能让服务起不来；搜索照旧逐个文件扫
        print(f"全文索引没启动：{e}", file=sys.stderr)
    # 服务被停掉（Mac App 退出、按端口 kill）时：正在推 GitHub 的 git、ssh 自成进程组，不跟着走，要顺手停掉
    import atexit
    import signal
    atexit.register(github_sync.stop_on_exit)

    def on_term(_sig, _frame):
        github_sync.stop_on_exit()
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, on_term)
    # threaded=True 是必须的：两个 app 都在后台线程里跑任务，前端同时在轮询进度，
    # 单线程服务器会把轮询和任务卡在一起。
    run_simple("127.0.0.1", PORT, application, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
