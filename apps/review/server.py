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
        kind = next((k for k, label in (("reading", "_阅读_"), ("clips", "_剪藏_"), ("picked", "_综合_"), ("topic", "_主题_"), ("claims", "_观点检验_")) if label in name), "")
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


@app.route("/api/run", methods=["POST"])
def api_run():
    data = request.get_json(silent=True) or {}
    kind = data.get("kind")
    if kind not in review.KINDS:
        return jsonify({"error": "复盘类型只能是 " + " / ".join(review.KINDS)}), 400
    params = {k: data.get(k) for k in ("days", "topic", "months", "paths") if data.get(k) not in (None, "")}
    if kind == "picked":
        paths = params.get("paths")
        if not isinstance(paths, list) or not 2 <= len(paths) <= review.MAX_PICKED:
            return jsonify({"error": f"综合小结要选 2 到 {review.MAX_PICKED} 篇剪藏"}), 400
    if kind in ("topic",) and not str(params.get("topic") or "").strip():
        return jsonify({"error": "主题来龙去脉要填一个主题"}), 400
    with LOCK:
        for jid in [j for j, job in JOBS.items() if job["done"] and time.time() - job["finished_at"] > JOB_RETENTION_SECONDS]:
            JOBS.pop(jid, None)
        if any(not job["done"] for job in JOBS.values()):
            return jsonify({"error": "已经有一份复盘在生成，等它结束再开始"}), 429
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
