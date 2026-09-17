"""
notes2insight 本地 GUI 服务。

用法：
    python3 server.py
然后打开浏览器访问 http://127.0.0.1:8766
"""

from __future__ import annotations

import os
import shutil
import threading
import time
import uuid

from flask import Flask, jsonify, request, send_from_directory

import deck
import llm
import pipeline
import search
import vault

APP_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(APP_DIR, "static")
DEFAULT_OUTPUT_DIR = os.path.join(vault.DEFAULT_VAULT, "output")
PORT = int(os.environ.get("NOTES2INSIGHT_PORT", "8766"))

app = Flask(__name__, static_folder=None)

JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
JOB_RETENTION_SECONDS = 24 * 3600
MAX_COMPLETED_JOBS = 20


@app.after_request
def add_security_headers(response):
    if request.path.startswith("/deck/"):
        # 演示页是刻意做成自包含的单文件（拷到哪都能开），它的脚本只能内联；
        # 这里单独放宽 script-src，其余限制照旧，不影响主界面。
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
            "img-src data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
        )
        return response
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def _prune_jobs_locked(now: float | None = None) -> None:
    now = now or time.time()
    expired = [
        jid for jid, job in JOBS.items()
        if job.get("done") and now - job.get("finished_at", job.get("created_at", now)) > JOB_RETENTION_SECONDS
    ]
    for jid in expired:
        JOBS.pop(jid, None)
    completed = sorted(
        ((jid, j) for jid, j in JOBS.items() if j.get("done")),
        key=lambda kv: kv[1].get("finished_at", kv[1].get("created_at", 0)),
        reverse=True,
    )
    for jid, _ in completed[MAX_COMPLETED_JOBS:]:
        JOBS.pop(jid, None)


@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/static/<path:fname>")
def static_files(fname):
    return send_from_directory(STATIC_DIR, fname)


@app.route("/api/env")
def api_env():
    return jsonify({
        "claude_cli_found": shutil.which("claude") is not None,
        "anthropic_installed": _anthropic_installed(),
        "env_api_key": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "keys_dir": llm.KEYS_DIR,
        "key_file_anthropic": bool(llm.read_key_file("anthropic")),
        "key_file_openrouter": bool(llm.read_key_file("openrouter")),
        "default_vault": vault.DEFAULT_VAULT,
        "default_output": DEFAULT_OUTPUT_DIR,
        "ollama_models": llm.list_ollama_models(),
        "max_notes": pipeline.MAX_NOTES,
        "depths": [{"key": k, "label": v["label"], "words": v["words"]}
                   for k, v in pipeline.DEPTH_PRESETS.items()],
    })


def _anthropic_installed() -> bool:
    try:
        import anthropic  # noqa: F401
        return True
    except ImportError:
        return False


@app.route("/api/notes")
def api_notes():
    root = request.args.get("root") or vault.DEFAULT_VAULT
    refresh = request.args.get("refresh") == "1"
    try:
        notes = vault.scan(root, use_cache=not refresh)
    except FileNotFoundError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({
        "root": os.path.abspath(os.path.expanduser(root)),
        "count": len(notes),
        "folders": vault.folder_tree(notes),
        "notes": notes,
    })


@app.route("/api/preview")
def api_preview():
    """预览一篇笔记的开头，方便勾选前确认内容。"""
    root = request.args.get("root") or vault.DEFAULT_VAULT
    rel = request.args.get("path", "")
    try:
        text = vault.read_note(root, rel, max_chars=3000)
    except (OSError, ValueError) as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"path": rel, "text": text})


def _run_job(job_id: str, cfg: pipeline.RunConfig) -> None:
    def progress(stage: str, cur: int, total: int, msg: str) -> None:
        with JOBS_LOCK:
            job = JOBS.get(job_id)
            if not job:
                return
            job["stage"] = stage
            job["current"] = cur
            job["total"] = total
            job["message"] = msg
            job["log"].append(f"[{time.strftime('%H:%M:%S')}] {msg}")
            del job["log"][:-200]

    try:
        result = pipeline.run(cfg, progress)
        with JOBS_LOCK:
            job = JOBS.get(job_id)
            if job is not None:
                job.update(done=True, ok=True, result=result, finished_at=time.time())
    except Exception as e:
        with JOBS_LOCK:
            job = JOBS.get(job_id)
            if job is not None:
                job.update(done=True, ok=False, error=str(e)[:800], finished_at=time.time())


def _resolve_llm(data: dict):
    """解析后端 / Key / Base：请求里没填就依次找环境变量和 ~/.summit2md/keys 下的 key 文件
    （与 summit2md 共用），并把缺参数的情况在入队前就报清楚。
    返回 (params, None) 或 (None, (payload, status))。"""
    backend = data.get("backend") or "cli"
    api_key = (data.get("api_key") or "").strip()
    api_base = (data.get("api_base") or "").strip()
    model = (data.get("model") or "").strip()

    if backend == "api":
        if not api_key:
            api_key = os.environ.get("ANTHROPIC_API_KEY", "") or llm.read_key_file("anthropic")
        if not api_key:
            return None, (jsonify({"error": "已选择 Anthropic API，但没有填写 API Key"
                                            f"（环境变量 ANTHROPIC_API_KEY 和 {llm.KEYS_DIR}/anthropic.key 里都没找到）"}), 400)
    elif backend == "openrouter":
        if not api_key:
            api_key = os.environ.get("OPENROUTER_API_KEY", "") or llm.read_key_file("openrouter")
        if not api_key:
            return None, (jsonify({"error": "已选择 OpenRouter，但没有填写 API Key"
                                            f"（{llm.KEYS_DIR}/openrouter.key 里也没找到）"}), 400)
        if not model:
            return None, (jsonify({"error": "已选择 OpenRouter，但没有填写模型名"}), 400)
        api_base = api_base or llm.OPENROUTER_API_BASE
    elif backend == "openai_compatible":
        if not api_key:
            return None, (jsonify({"error": "已选择第三方 OpenAI 兼容 API，但没有填写 API Key"}), 400)
        if not api_base:
            return None, (jsonify({"error": "已选择第三方 OpenAI 兼容 API，但没有填写 API Base URL"}), 400)
        if not model:
            return None, (jsonify({"error": "已选择第三方 OpenAI 兼容 API，但没有填写模型名"}), 400)
    elif backend == "ollama":
        if not model:
            return None, (jsonify({"error": "已选择本地 Ollama，但没有填写/选择模型名（需先 `ollama pull <模型>`）"}), 400)
        api_base = api_base or llm.DEFAULT_OLLAMA_HOST

    return {"backend": backend, "api_key": api_key, "api_base": api_base, "model": model}, None


@app.route("/api/run", methods=["POST"])
def api_run():
    data = request.get_json(silent=True) or {}
    notes = data.get("notes") or []
    if not isinstance(notes, list) or not notes:
        return jsonify({"error": "没有勾选任何笔记"}), 400
    if len(notes) > pipeline.MAX_NOTES:
        return jsonify({"error": f"一次最多处理 {pipeline.MAX_NOTES} 篇，当前 {len(notes)} 篇"}), 400

    llm_params, err = _resolve_llm(data)
    if err:
        return err

    focus = (data.get("focus") or "").strip() or (data.get("topic") or "").strip()
    cfg = pipeline.RunConfig(
        vault_root=data.get("root") or vault.DEFAULT_VAULT,
        notes=[str(p) for p in notes],
        focus=focus,
        depth=data.get("depth") or "standard",
        backend=llm_params["backend"],
        model=llm_params["model"],
        api_key=llm_params["api_key"],
        api_base=llm_params["api_base"],
        concurrency=int(data.get("concurrency") or 3),
        timeout=int(data.get("timeout") or 900),
        output_dir=(data.get("output_dir") or DEFAULT_OUTPUT_DIR).strip(),
        use_cache=bool(data.get("use_cache", True)),
        topic=(data.get("topic") or "").strip(),
        retrieval=data.get("retrieval") if isinstance(data.get("retrieval"), dict) else None,
        model_digest=(data.get("model_digest") or "").strip(),
        model_compose=(data.get("model_compose") or "").strip(),
        max_note_chars=max(0, int(data.get("max_note_chars") or 0)),
    )

    job_id = uuid.uuid4().hex
    with JOBS_LOCK:
        _prune_jobs_locked()
        JOBS[job_id] = {
            "created_at": time.time(), "done": False, "ok": False, "kind": "report",
            "stage": "queued", "current": 0, "total": len(notes),
            "message": "任务已排队", "log": [], "note_count": len(notes),
            "topic": cfg.topic, "focus": cfg.focus, "depth": cfg.depth,
        }
    threading.Thread(target=_run_job, args=(job_id, cfg), daemon=True).start()
    return jsonify({"job_id": job_id})


def _new_job(kind: str, total: int, msg: str) -> str:
    job_id = uuid.uuid4().hex
    with JOBS_LOCK:
        _prune_jobs_locked()
        JOBS[job_id] = {
            "created_at": time.time(), "done": False, "ok": False, "kind": kind,
            "stage": "queued", "current": 0, "total": total, "message": msg, "log": [],
        }
    return job_id


def _progress_fn(job_id: str):
    def progress(stage: str, cur: int, total: int, msg: str) -> None:
        with JOBS_LOCK:
            job = JOBS.get(job_id)
            if not job:
                return
            job["stage"] = stage
            job["current"] = cur
            job["total"] = total
            job["message"] = msg
            job["log"].append(f"[{time.strftime('%H:%M:%S')}] {msg}")
            del job["log"][:-200]
    return progress


def _finish(job_id: str, *, ok: bool, result=None, error: str = "") -> None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is not None:
            job.update(done=True, ok=ok, result=result, error=error, finished_at=time.time())


@app.route("/api/search", methods=["POST"])
def api_search():
    data = request.get_json(silent=True) or {}
    topic = (data.get("topic") or "").strip()
    if not topic:
        return jsonify({"error": "请先填写主题"}), 400

    llm_params, err = _resolve_llm(data)
    if err:
        return err

    params = {
        "root": data.get("root") or vault.DEFAULT_VAULT,
        "topic": topic,
        "backend": llm_params["backend"],
        "api_key": llm_params["api_key"],
        "model": llm_params["model"],
        "api_base": llm_params["api_base"],
        "date_from": (data.get("date_from") or "").strip(),
        "folder": (data.get("folder") or "").strip(),
        "candidates": int(data.get("candidates") or search.DEFAULT_CANDIDATES),
        "do_screen": bool(data.get("screen", True)),
        "timeout": int(data.get("timeout") or 300),
        # 工具自己生成的报告默认不作为检索素材，免得越滚越自我引用
        "exclude_output": bool(data.get("exclude_output", True)),
        "output_dir": (data.get("output_dir") or DEFAULT_OUTPUT_DIR).strip(),
    }

    job_id = _new_job("search", 1, "检索任务已排队")

    def work():
        try:
            progress = _progress_fn(job_id)
            root = os.path.abspath(os.path.expanduser(params["root"]))
            exclude = ""
            if params["exclude_output"]:
                out = os.path.abspath(os.path.expanduser(params["output_dir"]))
                if out.startswith(root):
                    exclude = os.path.relpath(out, root)
            result = search.find(
                root, params["topic"], backend=params["backend"], api_key=params["api_key"],
                model=params["model"], api_base=params["api_base"], date_from=params["date_from"],
                folder=params["folder"], candidates=params["candidates"],
                do_screen=params["do_screen"], timeout=params["timeout"],
                exclude_folder=exclude, progress=progress,
            )
            progress("done", 1, 1, f"检索完成：{len(result['candidates'])} 篇候选")
            _finish(job_id, ok=True, result=result)
        except Exception as e:
            _finish(job_id, ok=False, error=str(e)[:800])

    threading.Thread(target=work, daemon=True).start()
    return jsonify({"job_id": job_id})


def _safe_report_path(raw: str, root: str, output_dir: str) -> str:
    """只允许读笔记库或输出目录里的 .md：这个服务虽然只监听 127.0.0.1，
    也不该让一个请求参数就能翻出任意文件。"""
    p = os.path.realpath(os.path.expanduser((raw or "").strip()))
    if not p.endswith(".md") or not os.path.isfile(p):
        raise ValueError("请选择一份 .md 报告")
    allowed = [os.path.realpath(os.path.expanduser(x)) for x in (root, output_dir) if x]
    if not any(p == a or p.startswith(a + os.sep) for a in allowed):
        raise ValueError("只能读取笔记库或输出目录里的报告")
    return p


@app.route("/api/reports")
def api_reports():
    """输出目录里已有的报告，供"为已有报告生成演示"用。"""
    out_dir = os.path.expanduser((request.args.get("output_dir") or DEFAULT_OUTPUT_DIR).strip())
    rows = []
    try:
        for name in os.listdir(out_dir):
            if not name.endswith(".md"):
                continue
            full = os.path.join(out_dir, name)
            st = os.stat(full)
            rows.append({"name": name, "path": full, "size": st.st_size, "mtime": st.st_mtime,
                         "has_deck": os.path.exists(full[:-3] + ".deck.html"),
                         "has_pptx": os.path.exists(full[:-3] + ".pptx")})
    except OSError as e:
        return jsonify({"error": f"读不到输出目录：{e}"}), 400
    rows.sort(key=lambda r: r["mtime"], reverse=True)
    return jsonify({"output_dir": out_dir, "reports": rows})


@app.route("/api/deck", methods=["POST"])
def api_deck():
    data = request.get_json(silent=True) or {}
    root = data.get("root") or vault.DEFAULT_VAULT
    output_dir = (data.get("output_dir") or DEFAULT_OUTPUT_DIR).strip()
    try:
        md_path = _safe_report_path(data.get("path", ""), root, output_dir)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    # 排版这一步活儿简单、只调一次，默认跟着"摘取模型"走，便宜档就够用。
    # 先定下用哪个模型再交给 _resolve_llm 校验，否则"没填模型名"会指着一个这里根本不用的字段报错。
    model = ((data.get("model_deck") or "").strip() or (data.get("model_digest") or "").strip()
             or (data.get("model") or "").strip())
    llm_params, err = _resolve_llm({**data, "model": model})
    if err:
        return err
    model = llm_params["model"]
    timeout = int(data.get("timeout") or 600)
    vault_name = os.path.basename(os.path.realpath(os.path.expanduser(root)).rstrip(os.sep))
    want_pptx = bool(data.get("pptx"))
    reuse = bool(data.get("reuse"))
    if reuse and not os.path.exists(md_path[:-3] + ".deck.html"):
        return jsonify({"error": "这份报告还没有生成过演示，没法复用，请先生成一次"}), 400

    job_id = _new_job("deck", 3, "演示生成已排队")

    def work():
        progress = _progress_fn(job_id)
        try:
            html_path = md_path[:-3] + ".deck.html"
            if reuse:
                # 已经有演示了就把内嵌的幻灯片脚本读回来，补一个格式不该再花一次模型调用
                progress("slides", 1, 3, f"复用已有演示：{os.path.basename(html_path)}")
                d = deck.load_deck_from_html(html_path)
                html = deck.render_html(d, vault_name=vault_name,
                                        report_filename=os.path.basename(md_path))
            else:
                with open(md_path, "r", encoding="utf-8") as f:
                    md = f.read()
                progress("slides", 1, 3, f"抽取报告骨架：{os.path.basename(md_path)}")
                html, d = deck.generate(
                    md, backend=llm_params["backend"], api_key=llm_params["api_key"], model=model,
                    api_base=llm_params["api_base"], timeout=timeout,
                    vault_name=vault_name, report_filename=os.path.basename(md_path),
                )
            progress("slides", 2, 3, f"{len(d['slides'])} 页，正在渲染")
            with open(html_path, "w", encoding="utf-8") as f:
                f.write(html)

            result = {
                "path": html_path, "filename": os.path.basename(html_path),
                "report": os.path.basename(md_path), "title": d.get("title", ""),
                "slide_count": len(d["slides"]) + 1 + (1 if d.get("sources") else 0),
                "source_count": len(d.get("sources") or []),
                "outline_chars": d.get("outline_chars", 0),
                "model": "（复用已有脚本，未调用模型）" if reuse else (model or "（后端默认）"),
                "reused": reuse,
            }
            if want_pptx:
                progress("slides", 2, 3, "渲染 PPTX")
                pptx_path = md_path[:-3] + ".pptx"
                deck.render_pptx(d, pptx_path, vault_name=vault_name,
                                 report_filename=os.path.basename(md_path))
                result["pptx_path"] = pptx_path
                result["pptx_filename"] = os.path.basename(pptx_path)
            progress("done", 3, 3, "演示已生成：" + "、".join(
                x for x in (result["filename"], result.get("pptx_filename")) if x))
            _finish(job_id, ok=True, result=result)
        except Exception as e:
            _finish(job_id, ok=False, error=str(e)[:800])

    threading.Thread(target=work, daemon=True).start()
    return jsonify({"job_id": job_id})


def _job_file(job_id: str, key: str) -> str:
    with JOBS_LOCK:
        job = JOBS.get(job_id) or {}
        result = job.get("result")
    return (result or {}).get(key, "") if isinstance(result, dict) else ""


@app.route("/deck/<job_id>")
def serve_deck(job_id: str):
    """在浏览器里直接打开刚生成的演示。文件本身在输出目录里，双击也能开，
    这条路由只是省掉一次找文件。"""
    path = _job_file(job_id, "path")
    if not path or not os.path.isfile(path):
        return "演示不存在或已过期，请到输出目录里直接打开 .deck.html", 404
    return send_from_directory(os.path.dirname(path), os.path.basename(path))


@app.route("/deck/<job_id>/pptx")
def serve_deck_pptx(job_id: str):
    path = _job_file(job_id, "pptx_path")
    if not path or not os.path.isfile(path):
        return "这个任务没有生成 PPTX", 404
    return send_from_directory(os.path.dirname(path), os.path.basename(path), as_attachment=True)


@app.route("/api/jobs")
def api_jobs():
    """最近的任务，供页面刷新后恢复现场。"""
    try:
        limit = min(max(int(request.args.get("limit", 5)), 1), 20)
    except ValueError:
        limit = 5
    with JOBS_LOCK:
        rows = sorted(JOBS.items(), key=lambda kv: kv[1].get("created_at", 0), reverse=True)[:limit]
        out = []
        for jid, job in rows:
            item = {
                "job_id": jid,
                "kind": job.get("kind", "report"),
                "stage": job.get("stage"),
                "done": job.get("done"),
                "ok": job.get("ok"),
                "message": job.get("message", ""),
                "current": job.get("current", 0),
                "total": job.get("total", 0),
                "created_at": job.get("created_at", 0),
                "note_count": job.get("note_count", 0),
                "topic": job.get("topic", ""),
                "error": (job.get("error") or "")[:200],
            }
            r = job.get("result")
            if job.get("done") and job.get("ok") and isinstance(r, dict):
                item["title"] = r.get("title") or r.get("topic") or ""
                item["filename"] = r.get("filename", "")
            out.append(item)
    return jsonify({"jobs": out})


@app.route("/api/progress/<job_id>")
def api_progress(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"error": "任务不存在或已过期"}), 404
        payload = {k: v for k, v in job.items() if k != "result"}
        if job.get("done") and job.get("ok") and isinstance(job.get("result"), dict):
            r = job["result"]
            payload["result"] = {k: v for k, v in r.items() if k not in ("content", "candidates")}
    return jsonify(payload)


@app.route("/api/result/<job_id>")
def api_result(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job or not job.get("done") or not job.get("ok"):
            return jsonify({"error": "结果尚未就绪"}), 404
        return jsonify(job["result"])


def main() -> None:
    os.makedirs(STATIC_DIR, exist_ok=True)
    print(f"notes2insight 已启动：http://127.0.0.1:{PORT}")
    print(f"笔记库：{vault.DEFAULT_VAULT}")
    try:
        app.run(host="127.0.0.1", port=PORT, threaded=True)
    except OSError as e:
        if getattr(e, "errno", None) in (48, 98):  # EADDRINUSE
            print(f"\n端口 {PORT} 已被占用。")
            print("如果是 Notes2Insight 自己在跑，直接打开 http://127.0.0.1:%d 即可；" % PORT)
            print("否则换个端口：NOTES2INSIGHT_PORT=9000 python3 launch.py")
            raise SystemExit(1) from e
        raise


if __name__ == "__main__":
    main()
