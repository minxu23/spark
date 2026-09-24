"""
summit2md 本地 GUI 服务。

平时由仓库根目录的 spark.py 统一挂在 /summit 下启动（python3 spark.py）。
只调试这一个 app 时在仓库根目录跑：
    python3 -m apps.summit2md.server
然后打开浏览器访问 http://127.0.0.1:8765
"""

from __future__ import annotations

import html
import html.entities
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from flask import Flask, jsonify, request, send_from_directory

from . import pipeline  # noqa: F401  （同时负责把仓库根目录放进 import 路径）
from . import subscriptions_store
from . import tracking
from core import sources as core_sources
from core import vault as core_vault
from core import fs_browse
from core import web_guard
from core import common_static
from core import jobs as jobs_util
from core import llm_config

APP_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(APP_DIR, "static")
# 默认写进笔记库的 Spark 目录（core/vault.py 统一定义）；笔记库不可用时退回 app 目录
DEFAULT_OUTPUT_DIR = core_vault.default_output_dir(os.path.join(APP_DIR, "output"))

app = Flask(__name__, static_folder=None)
web_guard.install(app)
common_static.register(app)

JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
ACTIVE_OUTPUT_DIRS: dict[str, str] = {}
JOB_RETENTION_SECONDS = 24 * 3600
MAX_COMPLETED_JOBS = 50


@app.errorhandler(subscriptions_store.StoreCorrupt)
def _store_corrupt(e):
    return jsonify({"error": str(e)}), 500


@app.after_request
def add_security_headers(response):
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def _prune_jobs_locked(now: float | None = None) -> None:
    """限制内存中的历史任务数量；调用方必须持有 JOBS_LOCK。"""
    jobs_util.prune_finished(JOBS, retention_seconds=JOB_RETENTION_SECONDS,
                             max_completed=MAX_COMPLETED_JOBS, now=now)


# ---------------------------------------------------------------------------
# 主题总结 / 手选议题聚焦总结用的轻量任务：跟上面 /api/run 那套 JOBS 比，不需要
# 日志文件、也不需要"同一输出目录同时只能跑一个"这种互斥——就是给一次顶多几十
# 秒的单次 LLM 调用配一个能"停止"的任务壳子，好让界面不用死等一个同步请求。
# ---------------------------------------------------------------------------
SIMPLE_JOBS: dict[str, dict] = {}
SIMPLE_JOBS_LOCK = threading.Lock()
SIMPLE_JOB_RETENTION_SECONDS = 3600


def _prune_simple_jobs_locked(now: float | None = None) -> None:
    jobs_util.prune_finished(SIMPLE_JOBS, retention_seconds=SIMPLE_JOB_RETENTION_SECONDS, now=now)


def _start_simple_job(target, /, *, reserve_dir: str | None = None, **kwargs) -> str | None:
    """启动一个轻量任务：target 是 pipeline 里那种接受 stop_flag 关键字参数的生成函数。
    只支持"停止"，不支持"暂停"——这类操作本质是一次模型调用，暂停了再恢复跟重新
    发一次没有区别，不给这个假选项。

    reserve_dir：任务会写这个目录的 .manifest.json（主题总结要记下标签），跑的期间
    跟 /api/run 一样占住它，免得两边各存一份 manifest 互相覆盖。目录已被占用时
    返回 None。
    """
    job_id = uuid.uuid4().hex
    job = {"done": False, "error": None, "stopped": False, "result": None,
          "stop_requested": False, "created_at": time.time()}
    dir_key = os.path.normcase(os.path.realpath(reserve_dir)) if reserve_dir else None
    if dir_key:
        with JOBS_LOCK:
            if ACTIVE_OUTPUT_DIRS.get(dir_key):
                return None
            ACTIVE_OUTPUT_DIRS[dir_key] = job_id
    with SIMPLE_JOBS_LOCK:
        _prune_simple_jobs_locked()
        SIMPLE_JOBS[job_id] = job

    def stop_flag() -> bool:
        with SIMPLE_JOBS_LOCK:
            return job.get("stop_requested", False)

    def run():
        try:
            result = target(stop_flag=stop_flag, **kwargs)
            with SIMPLE_JOBS_LOCK:
                job["result"] = result
                job["done"] = True
        except pipeline.Stopped:
            with SIMPLE_JOBS_LOCK:
                job["stopped"] = True
                job["done"] = True
        except pipeline.SummarizeError as e:
            with SIMPLE_JOBS_LOCK:
                job["error"] = str(e)
                job["done"] = True
        except Exception as e:  # noqa: BLE001
            with SIMPLE_JOBS_LOCK:
                job["error"] = f"意外错误：{e}"
                job["done"] = True
        finally:
            if dir_key:
                with JOBS_LOCK:
                    if ACTIVE_OUTPUT_DIRS.get(dir_key) == job_id:
                        ACTIVE_OUTPUT_DIRS.pop(dir_key, None)

    threading.Thread(target=run, daemon=True).start()
    return job_id


@app.route("/api/simple_job_status/<job_id>")
def api_simple_job_status(job_id):
    with SIMPLE_JOBS_LOCK:
        job = SIMPLE_JOBS.get(job_id)
        if not job:
            return jsonify({"error": "任务不存在"}), 404
        return jsonify({
            "done": job["done"], "error": job["error"],
            "stopped": job["stopped"], "result": job["result"],
        })


@app.route("/api/simple_job_stop/<job_id>", methods=["POST"])
def api_simple_job_stop(job_id):
    with SIMPLE_JOBS_LOCK:
        job = SIMPLE_JOBS.get(job_id)
        if not job:
            return jsonify({"error": "任务不存在"}), 404
        job["stop_requested"] = True
    return jsonify({"ok": True})


@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/static/<path:fname>")
def static_files(fname):
    return send_from_directory(STATIC_DIR, fname)


@app.route("/api/env")
def api_env():
    return jsonify(
        {
            "claude_cli_found": shutil.which("claude") is not None,
            "anthropic_api_key_in_env": bool(os.environ.get("ANTHROPIC_API_KEY")),
            "anthropic_api_key_in_file": bool(pipeline.read_key_file("anthropic")),
            "openrouter_api_key_in_file": bool(pipeline.read_key_file("openrouter")),
            "keys_dir": pipeline.KEYS_DIR,
            "default_max_transcript_chars": pipeline.DEFAULT_MAX_TRANSCRIPT_CHARS,
            "default_output_dir": DEFAULT_OUTPUT_DIR,
            "ollama_default_host": pipeline.DEFAULT_OLLAMA_HOST,
        }
    )


@app.route("/api/ollama_models", methods=["POST"])
def api_ollama_models():
    data = request.get_json(force=True) or {}
    api_base = (data.get("api_base") or "").strip()
    models = pipeline.list_ollama_models(api_base)
    return jsonify({"models": models})


@app.route("/api/browse_dir", methods=["POST"])
def api_browse_dir():
    data = request.get_json(force=True) or {}
    path = data.get("path") or ""
    try:
        entries = fs_browse.browse_dir_suggestions(path)
    except Exception:  # noqa: BLE001
        entries = []
    return jsonify({"entries": entries})


@app.route("/api/dir_plausible", methods=["POST"])
def api_dir_plausible():
    data = request.get_json(force=True) or {}
    path = data.get("path") or ""
    try:
        plausible = fs_browse.dir_plausible(path)
    except Exception:  # noqa: BLE001
        plausible = False
    return jsonify({"plausible": plausible})


@app.route("/api/discover", methods=["POST"])
def api_discover():
    data = request.get_json(force=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "请输入 YouTube 播放列表或视频链接"}), 400
    try:
        result = pipeline.fetch_playlist(url)
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 400
    result["source_url"] = url
    return jsonify(result)


@app.route("/api/discover_from_text", methods=["POST"])
def api_discover_from_text():
    data = request.get_json(force=True) or {}
    text = data.get("text") or ""
    if not text.strip():
        return jsonify({"error": "请粘贴包含链接的文字"}), 400
    try:
        result = pipeline.fetch_entries_from_text(text)
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 400
    return jsonify(result)


# --------------------------------------------------------------------------
# 「信息跟进」订阅列表：长期跟踪的信息源，按类别分组。跟上面的 discover 是
# 两回事——discover 是"这个链接现在有什么"，这里是"我在跟哪些链接、分了什么
# 类"，持久化在 subscriptions_store 里（一个 json 文件，不是数据库）。
# --------------------------------------------------------------------------

def _guess_source_type(url: str, discover_result: dict) -> str:
    """标签用的粗分类，不影响实际抓取——真正的分流逻辑在 pipeline.fetch_playlist
    里，这里只是从它的结果里顺手读一下，凑不出来才退到按 URL 猜。"""
    entries = discover_result.get("entries") or []
    if entries and entries[0].get("source_type"):
        return entries[0]["source_type"]
    low = url.lower()
    if "youtube.com" in low or "youtu.be" in low:
        return "youtube"
    return "unknown"


def _kind(value) -> str:
    """订阅种类：信息跟进（track，默认）或 Podcast 跟进（podcast）。"""
    return subscriptions_store.normalize_kind(value)


def _podcast_folder_problem(folder: str) -> str | None:
    """Podcast 订阅的文件夹会被 process_job 整个当成节目目录写（manifest、总结、
    transcripts/）：不能是根目录、家目录，也不能正好是信息跟进那一大块的根。"""
    folder = os.path.realpath(folder)
    parent = os.path.dirname(folder)
    if parent == folder or os.path.dirname(parent) == parent \
            or folder == os.path.realpath(os.path.expanduser("~")):
        # 根目录、根目录下一层（比如填了 "/" 会变成 /untitled）、家目录
        return f"不能用 {folder} 当节目文件夹"
    if os.path.basename(folder) == subscriptions_store.TRACK_DIRNAME:
        return f"「{subscriptions_store.TRACK_DIRNAME}」是信息跟进用的文件夹，节目换个名字吧"
    return None


@app.route("/api/subscriptions", methods=["GET", "POST"])
def api_subscriptions():
    if request.method == "GET":
        return jsonify(subscriptions_store.list_all(_kind(request.args.get("kind"))))

    data = request.get_json(force=True) or {}
    kind = _kind(data.get("kind"))
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "请输入链接"}), 400
    category = (data.get("category") or "").strip() or "未分类"
    output_dir = _user_dir(data.get("output_dir") or DEFAULT_OUTPUT_DIR)
    name = (data.get("name") or "").strip()
    # 先查一遍再探测：探测可能要十几秒，重复的没必要白等（add() 里还会在锁内再查一次）
    if any(it.get("url") == url for it in subscriptions_store.list_all(kind)):
        return jsonify({"error": "这个链接已经订阅过了"}), 409

    try:
        result = pipeline.fetch_playlist(url, light=True)
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 400

    if not name:
        name = result.get("summit_title") or "Untitled"
    if kind == "podcast":
        problem = _podcast_folder_problem(subscriptions_store.default_folder(output_dir, name, kind))
        if problem:
            return jsonify({"error": problem}), 400
    try:
        item = subscriptions_store.add(
            url=url, name=name, category=category, output_dir=output_dir,
            source_type=_guess_source_type(url, result), auto_check=data.get("auto_check", True) is not False,
            kind=kind,
        )
    except subscriptions_store.DuplicateSubscription:
        return jsonify({"error": "这个链接已经订阅过了"}), 409
    item = dict(item, total_count=len(result.get("entries") or []))
    return jsonify(item)


@app.route("/api/subscriptions/<sub_id>", methods=["PATCH", "DELETE"])
def api_subscription_detail(sub_id):
    if request.method == "DELETE":
        if not subscriptions_store.delete(sub_id):
            return jsonify({"error": "没有这条订阅"}), 404
        return jsonify({"ok": True})

    data = request.get_json(force=True) or {}
    patch = {}
    if "name" in data:
        patch["name"] = (data.get("name") or "").strip()
    if "category" in data:
        patch["category"] = (data.get("category") or "").strip() or "未分类"
    if data.get("folder"):
        folder = _user_dir(data["folder"])
        current = subscriptions_store.get(sub_id)
        if current and current.get("kind") == "podcast":
            # 更新节目时按 输出目录/节目名 交给 process_job，它会把节目名再过一遍
            # sanitize_filename——文件夹名要先是"过完之后的样子"，不然两边对不上
            folder = os.path.join(os.path.dirname(folder), pipeline.sanitize_filename(os.path.basename(folder)))
            problem = _podcast_folder_problem(folder)
            if problem:
                return jsonify({"error": problem}), 400
        patch["folder"] = folder
    if "auto_check" in data:
        if not isinstance(data["auto_check"], bool):
            return jsonify({"error": "auto_check 应该是 true/false"}), 400
        patch["auto_check"] = data["auto_check"]
    item = subscriptions_store.update(sub_id, patch)
    if not item:
        return jsonify({"error": "没有这条订阅"}), 404
    return jsonify(item)


@app.route("/api/subscriptions/<sub_id>/check", methods=["POST"])
def api_subscription_check(sub_id):
    item = subscriptions_store.get(sub_id)
    if not item:
        return jsonify({"error": "没有这条订阅"}), 404
    return jsonify(tracking.check(item))


@app.route("/api/subscriptions/check_all", methods=["POST"])
def api_subscriptions_check_all():
    # 打开「信息跟进」页面时触发的那一轮——每条订阅探测是一次网络请求（RSS/
    # sitemap），逐条跑的话订阅一多（十几条）就要等上十几秒，界面上跟卡住了
    # 一样。探测之间互不依赖，并发跑更合理；个别源打不开也不影响其他源
    # （tracking.check 自己兜住了异常，不会让整批失败）。
    #
    # 只查打开了"自动检查"的订阅；其余的要用户在订阅管理里手动点「检查」。
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}   # 老页面不带请求体（当信息跟进）；带了别的形状也一样
    all_items = subscriptions_store.list_all(_kind(data.get("kind")))
    items = [it for it in all_items if it.get("auto_check", True)]
    skipped = len(all_items) - len(items)
    if not items:
        return jsonify({"results": [], "skipped": skipped})
    with ThreadPoolExecutor(max_workers=min(8, len(items))) as pool:
        results = list(pool.map(tracking.check, items))
    return jsonify({"results": results, "skipped": skipped})


@app.route("/api/subscriptions/auto_check", methods=["POST"])
def api_subscriptions_auto_check():
    """批量开关"自动检查"——订阅管理里整个类别一起勾/一起取消用。"""
    data = request.get_json(force=True) or {}
    ids = [str(i) for i in (data.get("ids") or []) if i]
    value = data.get("auto_check")
    if not ids or not isinstance(value, bool):
        return jsonify({"error": "需要 ids 和 auto_check（true/false）"}), 400
    return jsonify({"changed": subscriptions_store.set_auto_check(ids, value)})


@app.route("/api/subscriptions/<sub_id>/ignore", methods=["POST"])
def api_subscription_ignore(sub_id):
    data = request.get_json(force=True) or {}
    ids = [str(i) for i in (data.get("entry_ids") or []) if i]
    if not ids:
        return jsonify({"error": "没有要忽略的条目"}), 400
    if not subscriptions_store.ignore(sub_id, ids):
        return jsonify({"error": "没有这条订阅"}), 404
    return jsonify({"ok": True, "ignored": len(ids)})


@app.route("/api/subscriptions/podcast_candidates", methods=["POST"])
def api_podcast_candidates():
    """输出目录下以前用「临时链接」处理过、还没订阅的节目：找每个子文件夹里的
    .manifest.json，content_type 是 series、记着来源链接的就算。按文件夹名当订阅名，
    订阅后的文件夹正好就是这个文件夹，已经处理过的单集照样算已处理。"""
    data = request.get_json(force=True) or {}
    output_dir = _user_dir(data.get("output_dir") or DEFAULT_OUTPUT_DIR)
    subs = subscriptions_store.list_all("podcast")
    taken_folders = {os.path.normcase(os.path.realpath(s["folder"])) for s in subs}
    taken_urls = {s["url"] for s in subs}
    out, manual = [], []
    try:
        names = sorted(os.listdir(output_dir))
    except OSError as e:
        return jsonify({"error": f"读不了输出目录：{e}"}), 400
    for name in names:
        folder = os.path.join(output_dir, name)
        if not os.path.isfile(os.path.join(folder, ".manifest.json")):
            continue
        try:
            manifest = pipeline._load_manifest(folder)
        except Exception:  # noqa: BLE001 —— 读不出来的跳过，不影响别的
            continue
        url = manifest.get("source_url") or ""
        if manifest.get("content_type") != "series" or not url.startswith(("http://", "https://")):
            continue
        # 文件夹名要跟"按名字算出来的文件夹"一致，订阅后才会落回同一个文件夹
        if pipeline.sanitize_filename(name) != name:
            continue
        if os.path.normcase(os.path.realpath(folder)) in taken_folders or url in taken_urls:
            continue
        if name == subscriptions_store.TRACK_DIRNAME:
            continue
        item = {"name": name, "url": url, "episodes": len(manifest.get("entries") or {})}
        # 批量导入按行解析时会去掉名称开头的列表符号、末尾的冒号逗号——这种文件夹名
        # 填进去就会变成另一个名字、订到一个新文件夹，只能用「添加订阅」单独加
        if _parse_bulk_subscription_lines(f"{name} : {url}") != [(name, url)]:
            manual.append(item)
        else:
            out.append(item)
    return jsonify({"candidates": out, "manual": manual})


# 更新时照搬给 process_job 的那些选项；其它（节目名、输出目录、来源、内容类型）
# 都由订阅本身决定，不从请求里拿。
_PODCAST_UPDATE_PASSTHROUGH = (
    "backend", "api_key", "api_base", "model", "overall_model", "summary_length",
    "max_transcript_chars", "lang_prefs", "regenerate_summary",
)


@app.route("/api/subscriptions/<sub_id>/update", methods=["POST"])
def api_subscription_update(sub_id):
    """Podcast 跟进：把这个节目的新单集（或者 entry_ids 指定的那几期）交给跟「临时
    链接」同一套逐期处理，写进节目文件夹、刷新节目总结。返回任务 id，进度照旧
    走 /api/status。"""
    data = request.get_json(force=True) or {}
    sub = subscriptions_store.get(sub_id)
    if not sub:
        return jsonify({"error": "没有这条订阅"}), 404
    if sub.get("kind") != "podcast":
        return jsonify({"error": "只有 Podcast 跟进的订阅能这样更新"}), 400
    wanted = data.get("entry_ids")
    if wanted is not None and not isinstance(wanted, list):
        return jsonify({"error": "entry_ids 应该是一个列表"}), 400
    folder = sub["folder"]
    problem = _podcast_folder_problem(folder)
    if problem:
        return jsonify({"error": problem + "（在订阅管理里改一下文件夹）"}), 400
    try:
        entries = tracking.list_entries(sub).get("entries") or []
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"读取节目单集列表失败：{e}"}), 400
    try:
        new = tracking.find_new(sub, entries)
    except pipeline.ManifestCorrupt as e:
        return jsonify({"error": str(e)}), 400
    if wanted is not None:
        wanted = {str(i) for i in wanted}
        new = [e for e in new if e.get("id") in wanted]
    if not new:
        return jsonify({"error": "没有需要处理的新单集（可能刚被处理过，重新检查一下）"}), 400
    # find_new 按日期从新到旧排（给收件箱看的）；交给 process_job 时按源里原来的
    # 顺序，跟在「临时链接」里勾选处理时编号的先后一致
    order = {e.get("id"): i for i, e in enumerate(entries)}
    new.sort(key=lambda e: order.get(e.get("id"), 0))
    for e in new:
        e.pop("last_error", None)   # find_new 加的展示字段，不是条目本身的

    # 总结文件的一级标题沿用原来的节目名（文件夹名是清洗过的，可能少了冒号之类）
    index_title = (pipeline._read_summit_title_from_readme(folder)
                   if pipeline._existing_summary_path(folder) else sub.get("name")) or None
    payload = {k: data[k] for k in _PODCAST_UPDATE_PASSTHROUGH if k in data}
    payload.update({
        "summit_title": os.path.basename(folder),
        "index_title": index_title,
        "output_dir": os.path.dirname(folder),
        "source_url": sub["url"],
        "entries": new,
        "content_type": "series",
        "do_summary": True,
        "skip_existing": True,
    })
    body, status = _launch_run(payload)
    if status == 200:
        body = dict(body, count=len(new), sub_id=sub_id, summit_title=payload["summit_title"],
                    output_dir=payload["output_dir"])
    return jsonify(body), status


# 「信息跟进」的一次批量处理：逐条小结 + 本批简报。跟 /api/run 共用
# ACTIVE_OUTPUT_DIRS——每个涉及的订阅文件夹都要占住，免得和别的任务同时写
# 同一份 manifest。
_ENTRY_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

TRACK_JOBS: dict[str, dict] = {}
TRACK_LOG_MAX_LINES = 1000


@app.route("/api/track/run", methods=["POST"])
def api_track_run():
    data = request.get_json(force=True) or {}
    llm, err = _resolve_llm_config(data)
    if err:
        return err
    # 只认信息跟进的订阅：podcast 订阅的文件夹是节目目录，不能往里写逐条笔记
    subs = {s["id"]: s for s in subscriptions_store.list_all("track")}
    selections = []
    for sel in data.get("selections") or []:
        sub = subs.get(sel.get("sub_id"))
        ids = list(dict.fromkeys(str(i) for i in (sel.get("entry_ids") or []) if i))
        if sub and ids:
            selections.append((sub, ids))
    if not selections:
        return jsonify({"error": "没有勾选任何内容"}), 400

    output_dir = _user_dir(data.get("output_dir") or DEFAULT_OUTPUT_DIR)
    summary_length = data.get("summary_length") or "medium"
    max_chars = data.get("max_transcript_chars")
    if not isinstance(max_chars, int) or max_chars < 0:
        max_chars = pipeline.DEFAULT_MAX_TRANSCRIPT_CHARS

    keys = [os.path.normcase(os.path.realpath(sub["folder"])) for sub, _ in selections]
    job_id = uuid.uuid4().hex
    job = {
        "log": [], "stage": "queued", "current": 0,
        "total": sum(len(ids) for _, ids in selections),
        "done": False, "error": None, "result": None, "stop_requested": False,
        "created_at": time.time(),
    }
    with JOBS_LOCK:
        busy = next((ACTIVE_OUTPUT_DIRS[k] for k in keys if ACTIVE_OUTPUT_DIRS.get(k)), None)
        if busy:
            # 占着的是另一批信息跟进的话把它的 id 带回去，页面可以直接接上看进度
            return jsonify({"error": "有订阅的文件夹正被另一个任务使用，等它结束后再试",
                            "active_track_job_id": busy if busy in TRACK_JOBS else None}), 409
        jobs_util.prune_finished(TRACK_JOBS, retention_seconds=JOB_RETENTION_SECONDS,
                                 max_completed=MAX_COMPLETED_JOBS)
        TRACK_JOBS[job_id] = job
        for k in keys:
            ACTIVE_OUTPUT_DIRS[k] = job_id

    def progress(kw: dict) -> None:
        with JOBS_LOCK:
            if kw.get("log"):
                job["log"].append(kw["log"])
                del job["log"][:-TRACK_LOG_MAX_LINES]
            for k in ("stage", "current", "total"):
                if k in kw:
                    job[k] = kw[k]

    def stop_flag() -> bool:
        with JOBS_LOCK:
            return job["stop_requested"]

    def run() -> None:
        try:
            result = tracking.run_batch(
                selections, output_dir=output_dir, llm=llm, summary_length=summary_length,
                max_chars=max_chars, brief_model=(data.get("overall_model") or "").strip(),
                stop_flag=stop_flag, progress_cb=progress,
            )
            with JOBS_LOCK:
                job["result"] = result
        except Exception as e:  # noqa: BLE001
            with JOBS_LOCK:
                job["error"] = f"意外错误：{e}"
                job["log"].append(f"❌ 意外错误：{e}")
        finally:
            with JOBS_LOCK:
                job["done"] = True
                job["finished_at"] = time.time()
                for k in keys:
                    if ACTIVE_OUTPUT_DIRS.get(k) == job_id:
                        ACTIVE_OUTPUT_DIRS.pop(k, None)

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/api/track/status/<job_id>")
def api_track_status(job_id):
    with JOBS_LOCK:
        job = TRACK_JOBS.get(job_id)
        if not job:
            return jsonify({"error": "任务不存在（服务可能重启过）"}), 404
        return jsonify({k: job[k] for k in ("log", "stage", "current", "total", "done", "error", "result",
                                            "stop_requested")})


@app.route("/api/track/jobs")
def api_track_jobs():
    """还在跑的信息跟进任务——页面刷新后靠这个重新接上进度和停止按钮。"""
    with JOBS_LOCK:
        running = [{"job_id": jid, "created_at": job["created_at"], "current": job["current"],
                    "total": job["total"]}
                   for jid, job in TRACK_JOBS.items() if not job["done"]]
    running.sort(key=lambda j: j["created_at"], reverse=True)
    return jsonify({"jobs": running})


@app.route("/api/track/stop/<job_id>", methods=["POST"])
def api_track_stop(job_id):
    with JOBS_LOCK:
        job = TRACK_JOBS.get(job_id)
        if not job:
            return jsonify({"error": "任务不存在"}), 404
        job["stop_requested"] = True
    return jsonify({"ok": True})


@app.route("/api/subscriptions/rename_category", methods=["POST"])
def api_subscriptions_rename_category():
    data = request.get_json(force=True) or {}
    old = (data.get("old") or "").strip()
    new = (data.get("new") or "").strip()
    if not old or not new:
        return jsonify({"error": "类别名不能为空"}), 400
    return jsonify({"renamed": subscriptions_store.rename_category(old, new, _kind(data.get("kind")))})


_BULK_LINE_LEADING_RE = re.compile(r"^[\s*\-•]+")
_BULK_NAME_TRAILING_RE = re.compile(r"[\s:：,，]+$")


class BadOpml(ValueError):
    pass


_OPML_HEAD_RE = re.compile(
    r"(<\?xml[^>]*\?>\s*)?(<!--.*?-->\s*|<!DOCTYPE[^>\[]*>\s*)*<opml\b", re.IGNORECASE | re.DOTALL)
_XML_BUILTIN_ENTITIES = {"amp;", "lt;", "gt;", "quot;", "apos;"}


def _xml_char_ok(cp: int) -> bool:
    return cp in (0x9, 0xA, 0xD) or 0x20 <= cp <= 0xD7FF or 0xE000 <= cp <= 0xFFFD or 0x10000 <= cp <= 0x10FFFF


def _fix_xml_ampersand(m: "re.Match") -> str:
    ref = m.group(1)
    if not ref:
        return "&amp;"
    if ref.startswith("#"):
        # &#0;、&#xD800; 这种 XML 不允许的字符，一个就会让整份解析失败——换成替换符
        digits = ref[2:-1] if ref[1] in "xX" else ref[1:-1]
        if len(digits) > 8:  # 再长就不可能是合法码点了（也免得 int() 碰上超长数字串报错）
            return "\ufffd"
        cp = int(digits, 16 if ref[1] in "xX" else 10)
        return m.group(0) if _xml_char_ok(cp) else "\ufffd"
    if ref in _XML_BUILTIN_ENTITIES:
        return m.group(0)
    # HTML5 的实体表（&AMP;、&NewLine; 也在里面）；认不出来的当成普通文字
    chars = html.entities.html5.get(ref)
    return "".join(f"&#{ord(c)};" for c in chars) if chars else "&amp;" + ref


def _parse_opml(text: str) -> list[tuple[str, str]] | None:
    """RSS 阅读器导出的 OPML：每个带 xmlUrl 的 <outline> 是一个订阅源。不是 OPML
    返回 None（交给按行解析）。"""
    # 去掉开头的 BOM 和空白；<opml 前面只允许 XML 声明、注释（多长都行）和 DOCTYPE。
    # 不能放宽成"开头一段里出现 <opml"：以 <https://…> 开头、正文里提到 <opml> 的
    # 普通链接清单会被当成 OPML、解析失败、整批 400。
    text = (text or "").lstrip("\ufeff").strip()
    if not _OPML_HEAD_RE.match(text):
        return None
    if re.search(r"<!ENTITY|<!DOCTYPE[^>]*\[", text, re.IGNORECASE):
        # OPML 用不着 DTD；带内部子集/实体定义的一律不认，免得被拿来做实体展开。
        # 光秃秃的 <!DOCTYPE opml> 无害（ElementTree 不会去取外部 DTD），照常解析。
        raise BadOpml("OPML 里不能带实体定义（<!ENTITY> / <!DOCTYPE ... [ ]>）")
    import xml.etree.ElementTree as ET
    # 粘进来的已经是解码好的文字，声明里的 encoding（ISO-8859-1、UTF-16…）不再
    # 算数；留着的话按 UTF-8 重新编码后再让解析器按声明解码，中文会变乱码。
    text = re.sub(r"^<\?xml[^>]*>", "", text)
    # 手工编辑过的 OPML 常有没转义的 &（A&B），还有从网页上抄来的 &nbsp; 之类 HTML
    # 实体——XML 只认五个内置实体和数字引用：HTML 实体换成数字引用，其余的 & 补成 &amp;
    text = re.sub(r"&(#[0-9]+;|#x[0-9A-Fa-f]+;|[A-Za-z][A-Za-z0-9]*;)?", _fix_xml_ampersand, text)
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        # 看着是 OPML 却解析不了，别退回按行拆——那样会把 <outline ...> 整行当成订阅名
        raise BadOpml(f"OPML 解析失败：{e}") from e
    out = []
    for node in root.iter("outline"):
        url = (node.get("xmlUrl") or "").strip()
        if url.startswith(("http://", "https://")):
            out.append(((node.get("title") or node.get("text") or "").strip(), url))
    return out


def _parse_bulk_subscription_lines(text: str) -> list[tuple[str, str]]:
    """把粘贴进来的一段"名称 : 链接"文本拆成 (name, url) 列表，一行一条——复用
    core.sources.extract_urls 找链接（不用自己再写一遍 URL 正则），链接前面剩下
    的部分当名称，顺手去掉列表符号（*/-）和末尾的冒号/逗号。名称留空也认，这时
    交给 /api/subscriptions 的探测逻辑去补一个标题。一行有多个链接只取第一个——
    这个格式本来就是"一行一条订阅"，不是"一行一堆链接"。
    """
    opml = _parse_opml(text)
    if opml is not None:
        return opml
    results = []
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        urls = core_sources.extract_urls(line)
        if not urls:
            continue
        url = urls[0]
        idx = line.find(url)
        name = line[:idx] if idx > 0 else ""
        name = _BULK_LINE_LEADING_RE.sub("", name)
        name = _BULK_NAME_TRAILING_RE.sub("", name).strip()
        results.append((name, url))
    return results


MAX_BULK_SUBSCRIPTIONS = 300


@app.route("/api/subscriptions/bulk", methods=["POST"])
def api_subscriptions_bulk():
    """批量导入订阅：粘贴一段"名称 : 链接"（或者干脆只有链接）的文本，一行一条，
    统一分到同一个类别。每条各自探测（跟单条添加走的是同一段 discover 逻辑），
    互不影响——链接打不开的那几条会在 failed 里给出原因，不会因为几条失败就
    拖累其它成功的。已经订阅过的链接直接跳过，不重复添加。
    """
    data = request.get_json(force=True) or {}
    text = data.get("text") or ""
    category = (data.get("category") or "").strip() or "未分类"
    output_dir = _user_dir(data.get("output_dir") or DEFAULT_OUTPUT_DIR)

    try:
        lines = _parse_bulk_subscription_lines(text)
    except BadOpml as e:
        return jsonify({"error": str(e)}), 400
    if not lines:
        return jsonify({"error": "没有从这段文字里找到任何链接"}), 400

    auto_check = data.get("auto_check", True) is not False
    kind = _kind(data.get("kind"))
    if len(lines) > MAX_BULK_SUBSCRIPTIONS:
        return jsonify({"error": f"一次最多导入 {MAX_BULK_SUBSCRIPTIONS} 条，这次有 {len(lines)} 条"}), 400

    existing_urls = {it["url"] for it in subscriptions_store.list_all(kind)}
    failed = []
    todo = []
    for name, url in lines:
        if url in existing_urls:
            failed.append({"name": name, "url": url, "error": "已经订阅过了，跳过"})
            continue
        existing_urls.add(url)  # 这批文本内部也可能有重复行
        todo.append((name, url))

    def probe(line):
        name, url = line
        try:
            return name, url, pipeline.fetch_playlist(url, light=True), None
        except Exception as e:  # noqa: BLE001
            return name, url, None, str(e)

    # 探测互不依赖，并发跑：一份 OPML 动辄几十上百个源，逐条等要好几分钟
    added = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        for name, url, result, error in pool.map(probe, todo):
            if error:
                failed.append({"name": name, "url": url, "error": error})
                continue
            final_name = name or result.get("summit_title") or "Untitled"
            if kind == "podcast":
                problem = _podcast_folder_problem(subscriptions_store.default_folder(output_dir, final_name, kind))
                if problem:
                    failed.append({"name": final_name, "url": url, "error": problem})
                    continue
            try:
                item = subscriptions_store.add(
                    url=url, name=final_name, category=category,
                    output_dir=output_dir, source_type=_guess_source_type(url, result), auto_check=auto_check,
                    kind=kind,
                )
            except subscriptions_store.DuplicateSubscription:
                # 探测这段时间里别的请求先加上了
                failed.append({"name": name, "url": url, "error": "已经订阅过了，跳过"})
                continue
            added.append(item)

    return jsonify({"added": added, "failed": failed})


@app.route("/api/subtitle_langs", methods=["POST"])
def api_subtitle_langs():
    data = request.get_json(force=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "缺少视频链接"}), 400
    try:
        result = pipeline.fetch_subtitle_languages(url)
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 400
    return jsonify(result)


@app.route("/api/existing_summary", methods=["POST"])
def api_existing_summary():
    """给「重新发现」流程用：这个标题对应的输出目录下有没有一份真实的旧总结。
    前端用它来决定要不要露出"沿用/重新生成"这个选择——原来这个选择只在走
    「导入目录」时才会出现，但后端沿用与否的判断跟走没走导入无关，靠的是
    manifest 里有没有真实总结；不露出这个探测，重新粘贴同一个链接、发现新
    议题、开始生成，会在用户完全不知情的情况下沿用旧总结，不把新议题纳入。
    """
    data = request.get_json(force=True) or {}
    summit_title = (data.get("summit_title") or "").strip()
    output_dir = _user_dir(data.get("output_dir") or DEFAULT_OUTPUT_DIR)
    if not summit_title:
        return jsonify({"has_overall_summary": False})
    try:
        has = pipeline.probe_overall_summary(output_dir, summit_title)
    except Exception:  # noqa: BLE001
        # 探测失败（权限、路径异常等）不该挡住正常发现流程，退回"没有旧总结"，
        # 大不了这次多问一句/多生成一次，比直接报错中断更安全。
        has = False
    return jsonify({"has_overall_summary": has})


@app.route("/api/agenda_order", methods=["POST"])
def api_agenda_order():
    data = request.get_json(force=True) or {}
    agenda_url = (data.get("agenda_url") or "").strip()
    if not agenda_url:
        return jsonify({"error": "请输入会议议程页面链接"}), 400
    try:
        result = pipeline.fetch_agenda_order(agenda_url)
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"抓取/解析议程页面失败：{e}"}), 400
    if not result.get("matched"):
        return jsonify({"error": "没有在这个页面里找到任何 YouTube 视频链接，暂不支持这种议程页面结构"}), 400
    return jsonify(result)


def _user_dir(path: str) -> str:
    """前端填的目录：展开 ~、转成绝对路径。不展开的话 "~/笔记" 会在服务的当前目录下
    建出一个字面叫 "~" 的文件夹，同目录互斥的检查也认不出它跟展开后的路径是同一处。"""
    return os.path.realpath(os.path.abspath(os.path.expanduser(path.strip())))


def _active_job_for_dir(output_dir: str) -> str | None:
    """检查某个输出目录是否正有任务在跑（按规范化路径匹配 /api/run 里维护的
    ACTIVE_OUTPUT_DIRS），避免和"导入目录""按日期重命名"这类直接读写同一批文件的
    操作并发冲突（manifest 同时写、文件正被改名时又被处理流程读写）。
    """
    with JOBS_LOCK:
        return ACTIVE_OUTPUT_DIRS.get(os.path.normcase(_user_dir(output_dir)))


def _dir_busy_response(output_dir: str):
    """这个目录正有生成任务在写 .manifest.json 时，其它也要写它的操作先别动——
    两边各拿一份 manifest 各自保存，后保存的会把先保存的改动整份冲掉。"""
    if _active_job_for_dir(output_dir):
        return jsonify({"error": "这个目录正在生成中，等任务结束后再试"}), 409
    return None


@app.route("/api/import_dir", methods=["POST"])
def api_import_dir():
    """导入一个此前已经生成过的输出目录（本机之前跑过，或者从别处拷贝过来的），
    不重新解析播放列表/不重新请求网络，直接从目录里的记录还原出议题列表，交给
    前端沿用「选择议题」开始的整套流程——用来重试失败项、补生成小结/演讲稿、
    或者单纯重新生成一遍大会/节目总结。
    """
    data = request.get_json(force=True) or {}
    path = (data.get("path") or "").strip()
    if not path:
        return jsonify({"error": "请输入要导入的输出目录路径"}), 400
    path = _user_dir(path)
    active_job_id = _active_job_for_dir(path)
    if active_job_id:
        return jsonify({
            "error": "这个目录正有任务在运行，请等它完成或停止后再导入",
            "active_job_id": active_job_id,
        }), 409
    try:
        result = pipeline.import_output_directory(path)
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 400
    return jsonify(result)


@app.route("/api/rename_by_date", methods=["POST"])
def api_rename_by_date():
    """把某个播客/访谈类节目输出目录里已经生成好的文档，从编号命名批量改成
    播出日期前缀命名；纯本地改名 + 修正 manifest/链接/README，不需要 API Key。
    """
    data = request.get_json(force=True) or {}
    output_dir = (data.get("output_dir") or "").strip()
    content_type = data.get("content_type") or None
    if content_type not in ("summit", "series", None):
        content_type = None
    if not output_dir:
        return jsonify({"error": "缺少输出目录"}), 400
    output_dir = os.path.realpath(os.path.abspath(os.path.expanduser(output_dir)))
    if not os.path.isdir(output_dir):
        return jsonify({"error": "输出目录不存在"}), 400
    active_job_id = _active_job_for_dir(output_dir)
    if active_job_id:
        return jsonify({
            "error": "这个目录正有任务在运行，请等它完成或停止后再重命名",
            "active_job_id": active_job_id,
        }), 409
    try:
        result = pipeline.rename_series_by_date(output_dir, content_type=content_type)
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 400
    return jsonify(result)


def _run_job(job_id: str, params: dict):
    job = JOBS[job_id]

    def append_log_file(message: str) -> None:
        path = job.get("log_file")
        if not path:
            return
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}\n")
        except OSError:
            pass  # 日志落盘失败不能阻断正文生成任务

    def progress_cb(kw: dict):
        message = kw.get("log")
        with JOBS_LOCK:
            if message is not None:
                job["log"].append(message)
                job["log"] = job["log"][-500:]
            if "stage" in kw:
                job["stage"] = kw["stage"]
            if "current" in kw:
                job["current"] = kw["current"]
            if "total" in kw:
                job["total"] = kw["total"]
        if message is not None:
            append_log_file(message)

    def stop_flag():
        with JOBS_LOCK:
            return job.get("stop_requested", False)

    def pause_flag():
        with JOBS_LOCK:
            return job.get("paused", False)

    try:
        result = pipeline.process_job(
            summit_title=params["summit_title"],
            source_url=params["source_url"],
            entries=params["entries"],
            output_base_dir=params["output_base_dir"],
            backend=params["backend"],
            api_key=params.get("api_key", ""),
            model=params.get("model", ""),
            api_base=params.get("api_base", ""),
            overall_model=params.get("overall_model", ""),
            max_transcript_chars=params.get("max_transcript_chars", pipeline.DEFAULT_MAX_TRANSCRIPT_CHARS),
            lang_prefs=params["lang_prefs"],
            do_summary=params["do_summary"],
            regenerate_summary=params.get("regenerate_summary", True),
            do_speaker_label=params.get("do_speaker_label", False),
            do_speech_script=params.get("do_speech_script", False),
            speech_lang_mode=params.get("speech_lang_mode", "bilingual"),
            skip_existing=params.get("skip_existing", True),
            index_title=params.get("index_title"),
            agenda_order_map=params.get("agenda_order_map") or None,
            content_type=params.get("content_type", "summit"),
            summary_length=params.get("summary_length", "medium"),
            stop_flag=stop_flag,
            pause_flag=pause_flag,
            progress_cb=progress_cb,
        )
        result["log_file"] = job.get("log_file")
        with JOBS_LOCK:
            job["result"] = result
            _mark_done_locked(job_id, job)
    except Exception as e:  # noqa: BLE001
        message = f"❌ 任务失败：{e}"
        with JOBS_LOCK:
            job["error"] = str(e)
            job["log"].append(message)
            _mark_done_locked(job_id, job)
        append_log_file(message)
    finally:
        with JOBS_LOCK:
            if not job.get("done"):   # 上面两处都没走到（比如 BaseException），兜底收尾
                _mark_done_locked(job_id, job)


def _mark_done_locked(job_id: str, job: dict) -> None:
    """标记任务结束并同时释放它占用的输出目录——两件事必须在同一次加锁里做完：
    前端一看到 done 就可能马上对同一目录发起下一个任务（比如"重试失败项"），
    这时目录还被占着就会莫名其妙地收到 409。调用方必须持有 JOBS_LOCK。"""
    job["done"] = True
    job["finished_at"] = time.time()
    output_key = job.get("output_key")
    if output_key and ACTIVE_OUTPUT_DIRS.get(output_key) == job_id:
        ACTIVE_OUTPUT_DIRS.pop(output_key, None)


def _resolve_llm_config(data: dict, needs_llm: bool = True):
    """解析请求里的后端/Key/模型参数（规则见 core/llm_config）。
    返回 (config_dict, None) 或 (None, (jsonify_payload, status_code))。"""
    try:
        return llm_config.resolve(data, default_backend="api", needs_llm=needs_llm), None
    except llm_config.ConfigError as e:
        return None, (jsonify({"error": str(e)}), 400)


@app.route("/api/run", methods=["POST"])
def api_run():
    body, status = _launch_run(request.get_json(force=True) or {})
    return jsonify(body), status


def _launch_run(data: dict) -> tuple[dict, int]:
    """启动一个会议/节目处理任务（process_job），返回 (响应内容, 状态码)。
    /api/run 和 podcast 订阅的「更新」共用这一段。"""
    entries = data.get("entries") or []
    if not entries:
        return {"error": "请至少选择一个议题"}, 400
    # 条目 id 会直接拼进缓存/产物的文件名，只接受本应用各来源会生成的那种 id
    # （YouTube 视频 id、Substack slug、哈希 id），挡住 "../x" 这类路径穿越。
    bad = [e.get("id") for e in entries if not isinstance(e, dict) or not _ENTRY_ID_RE.match(str(e.get("id") or ""))]
    if bad:
        return {"error": f"条目 id 不合法：{bad[0]!r}"}, 400

    do_summary = bool(data.get("do_summary", True))
    regenerate_summary = bool(data.get("regenerate_summary", True))
    do_speaker_label = bool(data.get("do_speaker_label", False))
    do_speech_script = bool(data.get("do_speech_script", False))
    speech_lang_mode = data.get("speech_lang_mode") or "bilingual"
    if speech_lang_mode not in ("bilingual", "zh", "original"):
        speech_lang_mode = "bilingual"
    summary_length = data.get("summary_length") or "medium"
    if summary_length not in ("short", "medium", "long"):
        summary_length = "medium"
    skip_existing = bool(data.get("skip_existing", True))
    content_type = data.get("content_type") or "summit"
    if content_type not in ("summit", "series"):
        content_type = "summit"
    agenda_order_map_raw = data.get("agenda_order_map") or {}
    agenda_order_map = {
        str(k): float(v) for k, v in agenda_order_map_raw.items() if isinstance(v, (int, float))
    }
    needs_llm = do_summary or do_speaker_label or do_speech_script
    llm_config, err = _resolve_llm_config(data, needs_llm)
    if err:
        resp, code = err
        return resp.get_json(), code
    backend, api_key, api_base, model = (
        llm_config["backend"], llm_config["api_key"], llm_config["api_base"], llm_config["model"]
    )
    overall_model = (data.get("overall_model") or "").strip()
    try:
        max_transcript_chars = max(0, int(data.get("max_transcript_chars", pipeline.DEFAULT_MAX_TRANSCRIPT_CHARS)))
    except (TypeError, ValueError):
        max_transcript_chars = pipeline.DEFAULT_MAX_TRANSCRIPT_CHARS

    lang_prefs_raw = data.get("lang_prefs") or "en"
    lang_prefs = [s.strip() for s in lang_prefs_raw.split(",") if s.strip()]

    output_base_dir = (data.get("output_dir") or DEFAULT_OUTPUT_DIR).strip() or DEFAULT_OUTPUT_DIR
    output_base_dir = os.path.realpath(os.path.abspath(os.path.expanduser(output_base_dir)))

    job_id = uuid.uuid4().hex[:12]
    summit_title = data.get("summit_title") or "Untitled Summit"
    task_out_dir = os.path.join(output_base_dir, pipeline.sanitize_filename(summit_title))
    output_key = os.path.normcase(os.path.realpath(task_out_dir))
    log_dir = os.path.join(task_out_dir, "logs")
    log_file = os.path.join(log_dir, f"task_{time.strftime('%Y%m%d-%H%M%S')}_{job_id}.log")
    job = {
        "id": job_id,
        "summit_title": summit_title,
        "content_type": content_type,
        "log": [],
        "stage": "queued",
        "current": 0,
        "total": len(entries),
        "done": False,
        "error": None,
        "result": None,
        "stop_requested": False,
        "paused": False,
        "created_at": time.time(),
        "log_file": log_file,
        "output_key": output_key,
    }
    with JOBS_LOCK:
        _prune_jobs_locked()
        active_job_id = ACTIVE_OUTPUT_DIRS.get(output_key)
        if active_job_id:
            return {
                "error": "同一输出目录已有任务正在运行，请等待其完成或停止后再试",
                "active_job_id": active_job_id,
            }, 409
        JOBS[job_id] = job
        ACTIVE_OUTPUT_DIRS[output_key] = job_id

    try:
        os.makedirs(log_dir, exist_ok=True)
        with open(log_file, "w", encoding="utf-8") as f:
            f.write("Summit2MD 任务日志\n")
            f.write(f"任务 ID：{job_id}\n")
            f.write(f"开始时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"会议：{summit_title}\n")
            f.write(f"议题数：{len(entries)}\n")
            f.write(f"后端：{backend}\n")
            f.write("-" * 60 + "\n")
    except OSError as e:
        with JOBS_LOCK:
            JOBS.pop(job_id, None)
            if ACTIVE_OUTPUT_DIRS.get(output_key) == job_id:
                ACTIVE_OUTPUT_DIRS.pop(output_key, None)
        return {"error": f"无法创建输出目录或任务日志：{e}"}, 400

    params = {
        "summit_title": summit_title,
        "source_url": data.get("source_url") or "",
        "entries": entries,
        "output_base_dir": output_base_dir,
        "backend": backend,
        "api_key": api_key,
        "model": model,
        "api_base": api_base,
        "overall_model": overall_model,
        "max_transcript_chars": max_transcript_chars,
        "lang_prefs": lang_prefs,
        "do_summary": do_summary,
        "regenerate_summary": regenerate_summary,
        "do_speaker_label": do_speaker_label,
        "do_speech_script": do_speech_script,
        "speech_lang_mode": speech_lang_mode,
        "skip_existing": skip_existing,
        "agenda_order_map": agenda_order_map,
        "content_type": content_type,
        "summary_length": summary_length,
        "index_title": (data.get("index_title") or "").strip() or None,
    }
    t = threading.Thread(target=_run_job, args=(job_id, params), daemon=True)
    try:
        t.start()
    except RuntimeError as e:
        with JOBS_LOCK:
            JOBS.pop(job_id, None)
            if ACTIVE_OUTPUT_DIRS.get(output_key) == job_id:
                ACTIVE_OUTPUT_DIRS.pop(output_key, None)
        return {"error": f"无法启动后台任务：{e}"}, 500
    return {"job_id": job_id}, 200


@app.route("/api/jobs")
def api_jobs():
    """列出内存里还记得的任务（运行中 + 最近完成的），供浏览器刷新后重新接上进度条/日志。
    不含 API Key/模型等敏感或任务专属配置——那些只存在于发起请求的那次 /api/run 里，
    刷新后前端会用默认后端兜底，行为见 /api/topic_summary 和前端 restoreTasks()。
    """
    with JOBS_LOCK:
        _prune_jobs_locked()
        jobs = sorted(JOBS.values(), key=lambda j: j.get("created_at", 0), reverse=True)
        return jsonify({
            "jobs": [
                {
                    "job_id": j["id"],
                    "summit_title": j.get("summit_title") or "",
                    "content_type": j.get("content_type") or "summit",
                    "done": j["done"],
                }
                for j in jobs
            ]
        })


@app.route("/api/status/<job_id>")
def api_status(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"error": "任务不存在"}), 404
        return jsonify(
            {
                "log": job["log"],
                "stage": job["stage"],
                "current": job["current"],
                "total": job["total"],
                "done": job["done"],
                "error": job["error"],
                "result": job["result"],
                "paused": job.get("paused", False),
                "log_file": job.get("log_file"),
            }
        )


@app.route("/api/jobs/<job_id>", methods=["DELETE"])
def api_dismiss_job(job_id):
    """从任务列表里移除一个已经结束的任务——只是不再显示在界面上，不影响已经写盘的
    文件。只允许移除已完成/已失败的任务，避免误移除一个还在跑的任务导致前端断开轮询、
    找不到地方暂停/停止它。不存在（已经被移除过、或服务重启后不记得了）时直接当成功处理，
    这个操作本来就是幂等的"确保它不在列表里"。
    """
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job and not job.get("done"):
            return jsonify({"error": "任务还在运行中，无法移除；请先停止或等它完成"}), 400
        JOBS.pop(job_id, None)
    return jsonify({"ok": True})


@app.route("/api/stop/<job_id>", methods=["POST"])
def api_stop(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"error": "任务不存在"}), 404
        job["stop_requested"] = True
        job["paused"] = False  # 停止应该能立刻打断暂停中的等待循环
    return jsonify({"ok": True})


@app.route("/api/pause/<job_id>", methods=["POST"])
def api_pause(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"error": "任务不存在"}), 404
        if job["done"]:
            return jsonify({"error": "任务已结束，无法暂停"}), 400
        job["paused"] = True
    return jsonify({"ok": True})


@app.route("/api/resume/<job_id>", methods=["POST"])
def api_resume(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"error": "任务不存在"}), 404
        job["paused"] = False
    return jsonify({"ok": True})


@app.route("/api/readme/<job_id>")
def api_readme(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        result = job.get("result") if job else None
    if not result or not result.get("index_path") or not os.path.exists(result["index_path"]):
        return jsonify({"error": "总结文件还不存在"}), 404
    with open(result["index_path"], encoding="utf-8") as f:
        content = f.read()
    return jsonify({"content": content})


@app.route("/api/open_folder/<job_id>", methods=["POST"])
def api_open_folder(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        result = job.get("result") if job else None
    if not result or not result.get("output_dir"):
        return jsonify({"error": "该任务还没有可打开的输出目录"}), 400
    path = result["output_dir"]
    try:
        if sys.platform == "darwin":
            subprocess.run(["open", path], check=False)
        elif sys.platform.startswith("win"):
            os.startfile(path)  # type: ignore[attr-defined]
        else:
            subprocess.run(["xdg-open", path], check=False)
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"打开失败：{e}"}), 500
    return jsonify({"ok": True})


@app.route("/api/topic_groups", methods=["POST"])
def api_topic_groups():
    data = request.get_json(force=True) or {}
    output_dir = (data.get("output_dir") or "").strip()
    if not output_dir:
        return jsonify({"error": "缺少输出目录"}), 400
    output_dir = os.path.realpath(os.path.abspath(os.path.expanduser(output_dir)))
    if not os.path.isdir(output_dir):
        return jsonify({"error": "输出目录不存在"}), 400
    busy = _dir_busy_response(output_dir)
    if busy:
        return busy
    return jsonify({"groups": pipeline.list_topic_groups(output_dir)})


@app.route("/api/topic_entries", methods=["POST"])
def api_topic_entries():
    """给"手选议题生成聚焦总结"用：列出这个输出目录 manifest 里的全部议题，不依赖、
    也不需要大会/节目总结解析出"主题索引"——跟 /api/topic_groups 是同一层级的另一个
    数据源，一个列自动分组，一个列全部议题原始列表。
    """
    data = request.get_json(force=True) or {}
    output_dir = (data.get("output_dir") or "").strip()
    if not output_dir:
        return jsonify({"error": "缺少输出目录"}), 400
    output_dir = os.path.realpath(os.path.abspath(os.path.expanduser(output_dir)))
    if not os.path.isdir(output_dir):
        return jsonify({"error": "输出目录不存在"}), 400
    return jsonify({"entries": pipeline.list_manifest_entries(output_dir)})


@app.route("/api/custom_topic_summary", methods=["POST"])
def api_custom_topic_summary():
    """跟 /api/topic_summary 是同一件事的另一个入口：那边按自动分出的主题名字选议题，
    这边直接按用户手工勾出来的 entry_ids 选——不依赖主题分组是否存在或解析成不成功。
    """
    data = request.get_json(force=True) or {}
    output_dir = (data.get("output_dir") or "").strip()
    summit_title = (data.get("summit_title") or "").strip() or "Untitled Summit"
    content_type = data.get("content_type") or "summit"
    if content_type not in ("summit", "series"):
        content_type = "summit"
    entry_ids = [e.strip() for e in (data.get("entry_ids") or []) if isinstance(e, str) and e.strip()]
    label = (data.get("label") or "").strip()
    reuse = bool(data.get("reuse"))
    if not output_dir:
        return jsonify({"error": "缺少输出目录"}), 400
    if not entry_ids:
        return jsonify({"error": "请至少勾选一个议题"}), 400
    output_dir = os.path.realpath(os.path.abspath(os.path.expanduser(output_dir)))
    if not os.path.isdir(output_dir):
        return jsonify({"error": "输出目录不存在"}), 400
    busy = _dir_busy_response(output_dir)
    if busy:
        return busy

    llm_config, err = _resolve_llm_config(data, needs_llm=True)
    if err:
        return err
    job_id = _start_simple_job(
        pipeline.generate_custom_topic_summary,
        out_dir=output_dir, summit_title=summit_title, content_type=content_type,
        entry_ids=entry_ids, label=label, backend=llm_config["backend"],
        api_key=llm_config["api_key"], model=llm_config["model"], api_base=llm_config["api_base"],
        reuse=reuse, reserve_dir=output_dir,
    )
    if job_id is None:
        return jsonify({"error": "这个目录正在生成中，等任务结束后再试"}), 409
    return jsonify({"job_id": job_id})


@app.route("/api/topic_summary_exists", methods=["POST"])
def api_topic_summary_exists():
    """给"按主题生成聚焦总结""手选议题生成聚焦总结"两处前端用：这个标签对应的
    主题总结文件是不是已经生成过，前端据此决定要不要露出"沿用/重新生成"的选择，
    跟 /api/existing_summary 是同一个道理。

    手选议题那边标题常年留空（走 AI 自动概括），这时候没有 label 可探测——带上
    entry_ids 就能查"这批议题上次概括出的标题是什么"，查得到再探测那份文件在
    不在；查不到就是真的没生成过，不是"探测这件事做不了"。返回里的 label 是
    实际探测用的标题，前端拿这个去发起"重新生成"请求，不能再假设是空的。
    """
    data = request.get_json(force=True) or {}
    output_dir = (data.get("output_dir") or "").strip()
    label = (data.get("label") or "").strip()
    entry_ids = [e.strip() for e in (data.get("entry_ids") or []) if isinstance(e, str) and e.strip()]
    if not output_dir or (not label and not entry_ids):
        return jsonify({"exists": False, "label": label})
    output_dir = os.path.realpath(os.path.abspath(os.path.expanduser(output_dir)))
    try:
        if entry_ids:
            result = pipeline.probe_custom_topic_summary(output_dir, entry_ids, label)
        else:
            result = {"exists": pipeline.probe_topic_summary(output_dir, label), "label": label}
    except Exception:  # noqa: BLE001
        result = {"exists": False, "label": label}
    return jsonify(result)


@app.route("/api/topic_summary", methods=["POST"])
def api_topic_summary():
    data = request.get_json(force=True) or {}
    output_dir = (data.get("output_dir") or "").strip()
    summit_title = (data.get("summit_title") or "").strip() or "Untitled Summit"
    content_type = data.get("content_type") or "summit"
    if content_type not in ("summit", "series"):
        content_type = "summit"
    themes = [t.strip() for t in (data.get("themes") or []) if isinstance(t, str) and t.strip()]
    reuse = bool(data.get("reuse"))
    if not output_dir:
        return jsonify({"error": "缺少输出目录"}), 400
    if not themes:
        return jsonify({"error": "请至少选择一个主题"}), 400
    output_dir = os.path.realpath(os.path.abspath(os.path.expanduser(output_dir)))
    if not os.path.isdir(output_dir):
        return jsonify({"error": "输出目录不存在"}), 400
    busy = _dir_busy_response(output_dir)
    if busy:
        return busy

    llm_config, err = _resolve_llm_config(data, needs_llm=True)
    if err:
        return err
    job_id = _start_simple_job(
        pipeline.generate_topic_summary,
        out_dir=output_dir, summit_title=summit_title, content_type=content_type,
        theme_names=themes, backend=llm_config["backend"], api_key=llm_config["api_key"],
        model=llm_config["model"], api_base=llm_config["api_base"], reuse=reuse,
        reserve_dir=output_dir,
    )
    if job_id is None:
        return jsonify({"error": "这个目录正在生成中，等任务结束后再试"}), 409
    return jsonify({"job_id": job_id})


if __name__ == "__main__":
    os.makedirs(DEFAULT_OUTPUT_DIR, exist_ok=True)
    port = int(os.environ.get("PORT", "8765"))
    print(f"summit2md 服务已启动：http://127.0.0.1:{port}")
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
