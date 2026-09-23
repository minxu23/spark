"""
summit2md 本地 GUI 服务。

用法：
    python3 server.py
然后打开浏览器访问 http://127.0.0.1:8765
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid

from flask import Flask, jsonify, request, send_from_directory

from . import pipeline  # noqa: F401  （同时负责把仓库根目录放进 import 路径）
from . import subscriptions_store
from core import sources as core_sources
from core import vault as core_vault
from core import fs_browse

APP_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(APP_DIR, "static")
# 默认写进笔记库的 Spark 目录（core/vault.py 统一定义）；笔记库不可用时退回 app 目录
DEFAULT_OUTPUT_DIR = core_vault.default_output_dir(os.path.join(APP_DIR, "output"))

app = Flask(__name__, static_folder=None)

JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
ACTIVE_OUTPUT_DIRS: dict[str, str] = {}
JOB_RETENTION_SECONDS = 24 * 3600
MAX_COMPLETED_JOBS = 50


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
    now = now or time.time()
    expired = [
        job_id for job_id, job in JOBS.items()
        if job.get("done") and now - job.get("finished_at", job.get("created_at", now)) > JOB_RETENTION_SECONDS
    ]
    for job_id in expired:
        JOBS.pop(job_id, None)

    completed = sorted(
        ((job_id, job) for job_id, job in JOBS.items() if job.get("done")),
        key=lambda item: item[1].get("finished_at", item[1].get("created_at", 0)),
        reverse=True,
    )
    for job_id, _job in completed[MAX_COMPLETED_JOBS:]:
        JOBS.pop(job_id, None)


# ---------------------------------------------------------------------------
# 主题总结 / 手选议题聚焦总结用的轻量任务：跟上面 /api/run 那套 JOBS 比，不需要
# 日志文件、也不需要"同一输出目录同时只能跑一个"这种互斥——就是给一次顶多几十
# 秒的单次 LLM 调用配一个能"停止"的任务壳子，好让界面不用死等一个同步请求。
# ---------------------------------------------------------------------------
SIMPLE_JOBS: dict[str, dict] = {}
SIMPLE_JOBS_LOCK = threading.Lock()
SIMPLE_JOB_RETENTION_SECONDS = 3600


def _prune_simple_jobs_locked(now: float | None = None) -> None:
    now = now or time.time()
    expired = [
        job_id for job_id, job in SIMPLE_JOBS.items()
        if job.get("done") and now - job.get("created_at", now) > SIMPLE_JOB_RETENTION_SECONDS
    ]
    for job_id in expired:
        SIMPLE_JOBS.pop(job_id, None)


def _start_simple_job(target, /, **kwargs) -> str:
    """启动一个轻量任务：target 是 pipeline 里那种接受 stop_flag 关键字参数的生成函数。
    只支持"停止"，不支持"暂停"——这类操作本质是一次模型调用，暂停了再恢复跟重新
    发一次没有区别，不给这个假选项。
    """
    job_id = uuid.uuid4().hex
    job = {"done": False, "error": None, "stopped": False, "result": None,
          "stop_requested": False, "created_at": time.time()}
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


@app.route("/api/subscriptions", methods=["GET", "POST"])
def api_subscriptions():
    if request.method == "GET":
        return jsonify(subscriptions_store.list_all())

    data = request.get_json(force=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "请输入链接"}), 400
    category = (data.get("category") or "").strip() or "未分类"
    output_dir = (data.get("output_dir") or DEFAULT_OUTPUT_DIR).strip()
    name = (data.get("name") or "").strip()

    try:
        result = pipeline.fetch_playlist(url)
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 400

    if not name:
        name = result.get("summit_title") or "Untitled"
    item = subscriptions_store.add(
        url=url, name=name, category=category, output_dir=output_dir,
        source_type=_guess_source_type(url, result),
    )
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
    if "output_dir" in data:
        patch["output_dir"] = (data.get("output_dir") or DEFAULT_OUTPUT_DIR).strip()
    item = subscriptions_store.update(sub_id, patch)
    if not item:
        return jsonify({"error": "没有这条订阅"}), 404
    return jsonify(item)


def _check_one(item: dict) -> dict:
    """探测一条订阅有没有新内容——只调免费的 discover，不碰模型。失败（链接
    暂时打不开之类）不抛出去，让调用方（单条检查/批量检查）各自决定怎么呈现。"""
    try:
        result = pipeline.fetch_playlist(item["url"])
    except Exception as e:  # noqa: BLE001
        return {"id": item["id"], "error": str(e), "new_count": 0, "new_entries": [], "total": 0}
    new_entries = pipeline.find_new_entries(item["output_dir"], item["name"], result.get("entries") or [])
    subscriptions_store.touch_checked(item["id"])
    return {
        "id": item["id"],
        "error": None,
        "new_count": len(new_entries),
        "new_entries": [
            {"id": e.get("id"), "title": e.get("title"), "publish_date": e.get("publish_date")}
            for e in new_entries
        ],
        "total": len(result.get("entries") or []),
    }


@app.route("/api/subscriptions/<sub_id>/check", methods=["POST"])
def api_subscription_check(sub_id):
    item = subscriptions_store.get(sub_id)
    if not item:
        return jsonify({"error": "没有这条订阅"}), 404
    return jsonify(_check_one(item))


@app.route("/api/subscriptions/check_all", methods=["POST"])
def api_subscriptions_check_all():
    # 打开「信息跟进」页面时触发的那一轮——逐条跑，个别源打不开不影响其他源，
    # 也不需要为了几条订阅的量专门上并发。
    return jsonify({"results": [_check_one(item) for item in subscriptions_store.list_all()]})


@app.route("/api/subscriptions/rename_category", methods=["POST"])
def api_subscriptions_rename_category():
    data = request.get_json(force=True) or {}
    old = (data.get("old") or "").strip()
    new = (data.get("new") or "").strip()
    if not old or not new:
        return jsonify({"error": "类别名不能为空"}), 400
    return jsonify({"renamed": subscriptions_store.rename_category(old, new)})


_BULK_LINE_LEADING_RE = re.compile(r"^[\s*\-•]+")
_BULK_NAME_TRAILING_RE = re.compile(r"[\s:：,，]+$")


def _parse_bulk_subscription_lines(text: str) -> list[tuple[str, str]]:
    """把粘贴进来的一段"名称 : 链接"文本拆成 (name, url) 列表，一行一条——复用
    core.sources.extract_urls 找链接（不用自己再写一遍 URL 正则），链接前面剩下
    的部分当名称，顺手去掉列表符号（*/-）和末尾的冒号/逗号。名称留空也认，这时
    交给 /api/subscriptions 的探测逻辑去补一个标题。一行有多个链接只取第一个——
    这个格式本来就是"一行一条订阅"，不是"一行一堆链接"。
    """
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
    output_dir = (data.get("output_dir") or DEFAULT_OUTPUT_DIR).strip()

    lines = _parse_bulk_subscription_lines(text)
    if not lines:
        return jsonify({"error": "没有从这段文字里找到任何链接"}), 400

    existing_urls = {it["url"] for it in subscriptions_store.list_all()}
    added = []
    failed = []
    for name, url in lines:
        if url in existing_urls:
            failed.append({"name": name, "url": url, "error": "已经订阅过了，跳过"})
            continue
        try:
            result = pipeline.fetch_playlist(url)
        except Exception as e:  # noqa: BLE001
            failed.append({"name": name, "url": url, "error": str(e)})
            continue
        final_name = name or result.get("summit_title") or "Untitled"
        item = subscriptions_store.add(
            url=url, name=final_name, category=category, output_dir=output_dir,
            source_type=_guess_source_type(url, result),
        )
        added.append(item)
        existing_urls.add(url)  # 这批文本内部也可能有重复行

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
    output_dir = (data.get("output_dir") or DEFAULT_OUTPUT_DIR).strip()
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


def _active_job_for_dir(output_dir: str) -> str | None:
    """检查某个输出目录是否正有任务在跑（按规范化路径匹配 /api/run 里维护的
    ACTIVE_OUTPUT_DIRS），避免和"导入目录""按日期重命名"这类直接读写同一批文件的
    操作并发冲突（manifest 同时写、文件正被改名时又被处理流程读写）。
    """
    with JOBS_LOCK:
        return ACTIVE_OUTPUT_DIRS.get(os.path.normcase(os.path.realpath(output_dir)))


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
            job["done"] = True
            job["finished_at"] = time.time()
    except Exception as e:  # noqa: BLE001
        message = f"❌ 任务失败：{e}"
        with JOBS_LOCK:
            job["error"] = str(e)
            job["done"] = True
            job["finished_at"] = time.time()
            job["log"].append(message)
        append_log_file(message)
    finally:
        with JOBS_LOCK:
            output_key = job.get("output_key")
            if output_key and ACTIVE_OUTPUT_DIRS.get(output_key) == job_id:
                ACTIVE_OUTPUT_DIRS.pop(output_key, None)


def _resolve_llm_config(data: dict, needs_llm: bool = True):
    """解析请求里的后端/Key/模型参数：本地 key 文件/环境变量兜底，校验必填项。
    /api/run 和 /api/topic_summary 都要走同一套解析逻辑，抽出来避免同样的校验写两遍。
    返回 (config_dict, None) 或 (None, (jsonify_payload, status_code))。
    """
    backend = data.get("backend") or "api"
    api_key = (data.get("api_key") or "").strip()
    api_base = (data.get("api_base") or "").strip()
    model = (data.get("model") or "").strip()
    if backend == "api" and not api_key:
        api_key = os.environ.get("ANTHROPIC_API_KEY", "") or pipeline.read_key_file("anthropic")
    if needs_llm and backend == "api" and not api_key:
        return None, (jsonify({
            "error": "已选择 Anthropic API 方式，但没有填写 API Key"
                     "（环境变量 ANTHROPIC_API_KEY 和 ~/.summit2md/keys/anthropic.key 里都没找到）",
        }), 400)
    if backend == "openrouter" and not api_key:
        api_key = pipeline.read_key_file("openrouter")
    if needs_llm and backend == "openrouter":
        if not api_key:
            return None, (jsonify({"error": "已选择 OpenRouter，但没有填写 API Key（~/.summit2md/keys/openrouter.key 里也没找到）"}), 400)
        if not model:
            return None, (jsonify({"error": "已选择 OpenRouter，但没有填写模型名"}), 400)
        if not api_base:
            api_base = pipeline.OPENROUTER_API_BASE
    if needs_llm and backend == "openai_compatible":
        if not api_key:
            return None, (jsonify({"error": "已选择第三方 OpenAI 兼容 API，但没有填写 API Key"}), 400)
        if not api_base:
            return None, (jsonify({"error": "已选择第三方 OpenAI 兼容 API，但没有填写 API Base URL"}), 400)
        if not model:
            return None, (jsonify({"error": "已选择第三方 OpenAI 兼容 API，但没有填写模型名"}), 400)
    if needs_llm and backend == "ollama":
        if not model:
            return None, (jsonify({"error": "已选择本地 Ollama，但没有填写/选择模型名（需先 `ollama pull <模型>`）"}), 400)
        if not api_base:
            api_base = pipeline.DEFAULT_OLLAMA_HOST
    return {"backend": backend, "api_key": api_key, "api_base": api_base, "model": model}, None


@app.route("/api/run", methods=["POST"])
def api_run():
    data = request.get_json(force=True) or {}
    entries = data.get("entries") or []
    if not entries:
        return jsonify({"error": "请至少选择一个议题"}), 400

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
        return err
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
            return jsonify({
                "error": "同一输出目录已有任务正在运行，请等待其完成或停止后再试",
                "active_job_id": active_job_id,
            }), 409
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
        return jsonify({"error": f"无法创建输出目录或任务日志：{e}"}), 400

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
    }
    t = threading.Thread(target=_run_job, args=(job_id, params), daemon=True)
    try:
        t.start()
    except RuntimeError as e:
        with JOBS_LOCK:
            JOBS.pop(job_id, None)
            if ACTIVE_OUTPUT_DIRS.get(output_key) == job_id:
                ACTIVE_OUTPUT_DIRS.pop(output_key, None)
        return jsonify({"error": f"无法启动后台任务：{e}"}), 500
    return jsonify({"job_id": job_id})


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

    llm_config, err = _resolve_llm_config(data, needs_llm=True)
    if err:
        return err
    job_id = _start_simple_job(
        pipeline.generate_custom_topic_summary,
        out_dir=output_dir, summit_title=summit_title, content_type=content_type,
        entry_ids=entry_ids, label=label, backend=llm_config["backend"],
        api_key=llm_config["api_key"], model=llm_config["model"], api_base=llm_config["api_base"],
        reuse=reuse,
    )
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

    llm_config, err = _resolve_llm_config(data, needs_llm=True)
    if err:
        return err
    job_id = _start_simple_job(
        pipeline.generate_topic_summary,
        out_dir=output_dir, summit_title=summit_title, content_type=content_type,
        theme_names=themes, backend=llm_config["backend"], api_key=llm_config["api_key"],
        model=llm_config["model"], api_base=llm_config["api_base"], reuse=reuse,
    )
    return jsonify({"job_id": job_id})


if __name__ == "__main__":
    os.makedirs(DEFAULT_OUTPUT_DIR, exist_ok=True)
    port = int(os.environ.get("PORT", "8765"))
    print(f"summit2md 服务已启动：http://127.0.0.1:{port}")
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
