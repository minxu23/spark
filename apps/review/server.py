"""
复盘（/review/）：阅读复盘、主题来龙去脉、观点检验。生成逻辑在 core/review.py，这里只有页面和任务表。
一次只跑一份复盘（每份要调一两次长文模型）。
"""

from __future__ import annotations

import os
import threading
import time
import uuid

from flask import Flask, jsonify, request, send_from_directory

from core import clips
from core import common_static
from core import discuss
from core import readlog
from core import review
from core import vault as core_vault
from core import web_guard

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
JOB_RETENTION_SECONDS = 6 * 3600

app = Flask(__name__, static_folder=None)
web_guard.install(app)
common_static.register(app)

JOBS: dict[str, dict] = {}
LOCK = threading.Lock()


@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/static/<path:fname>")
def static_files(fname):
    return send_from_directory(STATIC_DIR, fname)


def _reader_url(path: str) -> str:
    """阅读页里打开这份复盘的地址（output/ 下的文件走 /read/r/…）。"""
    rel = os.path.relpath(path, core_vault.reports_dir()).replace(os.sep, "/")
    from urllib.parse import quote
    return f"/read/r/{quote(rel)}"


@app.route("/api/list")
def api_list():
    """已经生成过的复盘，新的在前。"""
    d = review.out_dir()
    out = []
    for name in os.listdir(d) if os.path.isdir(d) else []:
        if not name.endswith(".md") or name.startswith("."):
            continue
        p = os.path.join(d, name)
        try:
            with open(p, encoding="utf-8") as f:
                first = f.readline().lstrip("# ").strip()
        except OSError:
            continue
        kind = "discuss" if name.startswith("复盘_讨论_") else next((k for k, label in (("reading", "_阅读_"), ("clips", "_剪藏_"), ("picked", "_综合_"), ("tag", "_主题综述_"), ("topic", "_主题_"), ("claims", "_观点检验_")) if label in name), "")
        out.append({"title": first or name[:-3], "kind": kind, "mtime": os.path.getmtime(p), "url": _reader_url(p)})
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return jsonify(out[:50])


@app.route("/api/clips")
def api_clips():
    """综合小结要挑的剪藏，新存的在前。"""
    items = clips.load_clips()
    items.sort(key=lambda c: (c["clipped"], c["name"]), reverse=True)
    return jsonify([{"path": f"{clips.CLIPS_DIR}/{c['name']}", "title": c["title"], "source": c["source"],
                     "clipped": c["clipped"], "tags": c["tags"]} for c in items])


@app.route("/api/stats")
def api_stats():
    """表单旁边的提示：近 N 天电脑和手机上各读了多少篇。"""
    try:
        days = max(1, min(366, int(request.args.get("days") or 7)))
    except ValueError:
        days = 7
    events = readlog.load(time.time() - days * 86400)
    keys = {}
    for ev in events:
        k = (ev.get("show"), ev.get("entry")) if ev.get("show") else ev.get("path")
        keys.setdefault(k, set()).add(ev["source"])
    return jsonify({"days": days, "total": len(keys),
                    "desktop": sum(1 for s in keys.values() if "desktop" in s),
                    "phone": sum(1 for s in keys.values() if "phone" in s)})


def _one_at_a_time() -> bool:
    """模型后端是本地 Ollama 时，复盘和讨论不能同时跑（Ollama 同时收两个请求会把内存顶满）。"""
    try:
        return review.llm_config.resolve({}, default_backend="api")["backend"] == "ollama"
    except review.llm_config.ConfigError:
        return False


@app.route("/api/run", methods=["POST"])
def api_run():
    data = request.get_json(silent=True) or {}
    kind = data.get("kind")
    if kind not in review.KINDS or kind == "discuss":
        return jsonify({"error": "复盘类型只能是 " + " / ".join(k for k in review.KINDS if k != "discuss")}), 400
    params = {k: data.get(k) for k in ("days", "topic", "months", "paths", "tag") if data.get(k) not in (None, "")}
    if kind == "picked":
        paths = params.get("paths")
        if not isinstance(paths, list) or not 2 <= len(paths) <= review.MAX_PICKED:
            return jsonify({"error": f"综合小结要选 2 到 {review.MAX_PICKED} 篇"}), 400
    if kind in ("topic",) and not str(params.get("topic") or "").strip():
        return jsonify({"error": "主题来龙去脉要填一个主题"}), 400
    if kind == "tag" and not str(params.get("tag") or "").strip():
        return jsonify({"error": "主题综述要指定一个标签"}), 400
    with LOCK:
        for jid in [j for j, job in JOBS.items() if job["done"] and time.time() - job["finished_at"] > JOB_RETENTION_SECONDS]:
            JOBS.pop(jid, None)
        if any(not job["done"] and job["kind"] != "discuss" for job in JOBS.values()):
            return jsonify({"error": "已经有一份复盘在生成，等它结束再开始"}), 429
        if _one_at_a_time() and any(not job["done"] for job in JOBS.values()):
            return jsonify({"error": "讨论还在回答，本地模型一次只跑一件事，等它结束再开始"}), 429
        job_id = uuid.uuid4().hex[:12]
        job = {"id": job_id, "kind": kind, "params": params, "done": False, "ok": False, "stage": "排队",
               "error": "", "result": None, "created_at": time.time(), "finished_at": 0}
        JOBS[job_id] = job

    def progress(text: str) -> None:
        with LOCK:
            job["stage"] = text

    def work() -> None:
        try:
            path = review.run(kind, params, progress)
            result = {"path": path, "url": _reader_url(path),
                      "rel": os.path.relpath(path, core_vault.vault_root()).replace(os.sep, "/")}
            with LOCK:
                job.update(ok=True, result=result)
        except review.ReviewError as e:
            with LOCK:
                job["error"] = str(e)
        except Exception as e:  # noqa: BLE001
            with LOCK:
                job["error"] = f"意外错误：{e}"
        finally:
            with LOCK:
                job.update(done=True, finished_at=time.time(), stage="")

    threading.Thread(target=work, daemon=True, name="spark-review").start()
    return jsonify({"job_id": job_id})


@app.route("/api/progress/<job_id>")
def api_progress(job_id: str):
    with LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"error": "任务不存在（服务可能重启过）"}), 404
        return jsonify({k: job[k] for k in ("done", "ok", "stage", "error", "result", "kind")})


# ---------------------------------------------------------------- 讨论（core/discuss.py）：任务表和轮询同 /api/run，
# 但和生成复盘分开计：一问一答几十秒，不该被一份在跑的复盘挡住；讨论自己一次只跑一个。

@app.route("/api/discuss", methods=["POST"])
def api_discuss():
    data = request.get_json(silent=True) or {}
    try:
        question = discuss.check_question(data.get("question"))
        history = discuss.clean_history(data.get("history"))
    except review.ReviewError as e:
        return jsonify({"error": str(e)}), 400
    tag = data.get("tag")
    if tag not in (None, "") and (not isinstance(tag, str) or len(tag.strip()) > 100):
        return jsonify({"error": "tag 应该是一个标签名"}), 400
    tag = (tag or "").strip()
    with LOCK:
        for jid in [j for j, job in JOBS.items() if job["done"] and time.time() - job["finished_at"] > JOB_RETENTION_SECONDS]:
            JOBS.pop(jid, None)
        if any(not job["done"] and job["kind"] == "discuss" for job in JOBS.values()):
            return jsonify({"error": "上一个问题还在回答，等它结束再问"}), 429
        if _one_at_a_time() and any(not job["done"] for job in JOBS.values()):
            return jsonify({"error": "有一份复盘在生成，本地模型一次只跑一件事，等它结束再问"}), 429
        job_id = uuid.uuid4().hex[:12]
        job = {"id": job_id, "kind": "discuss", "params": {}, "done": False, "ok": False, "stage": "排队",
               "error": "", "result": None, "created_at": time.time(), "finished_at": 0}
        JOBS[job_id] = job

    def progress(text: str) -> None:
        with LOCK:
            job["stage"] = text

    def work() -> None:
        try:
            result = discuss.ask(question, history, tag, progress)
            with LOCK:
                job.update(ok=True, result=result)
        except review.ReviewError as e:
            with LOCK:
                job["error"] = str(e)
        except Exception as e:  # noqa: BLE001
            with LOCK:
                job["error"] = f"意外错误：{e}"
        finally:
            with LOCK:
                job.update(done=True, finished_at=time.time(), stage="")

    threading.Thread(target=work, daemon=True, name="spark-discuss").start()
    return jsonify({"job_id": job_id})


@app.route("/api/discuss/save", methods=["POST"])
def api_discuss_save():
    """整段讨论存成一篇复盘：写到哪由服务端定（output/复盘/复盘_讨论_….md），请求里的只是内容。"""
    data = request.get_json(silent=True) or {}
    try:
        path = discuss.save(data.get("tag") or "", data.get("turns"))
    except review.ReviewError as e:
        return jsonify({"error": str(e)}), 400
    except OSError as e:
        return jsonify({"error": f"写文件失败：{e}"}), 500
    return jsonify({"path": path, "url": _reader_url(path),
                    "rel": os.path.relpath(path, core_vault.vault_root()).replace(os.sep, "/")})
