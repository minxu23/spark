"""
notes2insight 本地 GUI 服务。

平时由仓库根目录的 spark.py 统一挂在 /notes 下启动（python3 spark.py）。
只调试这一个 app 时在仓库根目录跑：
    python3 -m apps.notes2insight.server
然后打开浏览器访问 http://127.0.0.1:8766
"""

from __future__ import annotations

import os
import re
import shutil
import threading
import time
import uuid

from flask import Flask, jsonify, request, send_from_directory

from . import deck
from . import link_import
from . import llm
from . import pipeline
from . import search
from . import uploads
from . import vault
from core import fs_browse
from core import web_guard
from core import common_static
from core import jobs as jobs_util
from core import llm_config

APP_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(APP_DIR, "static")
DEFAULT_OUTPUT_DIR = os.path.join(vault.DEFAULT_VAULT, "output")
PORT = int(os.environ.get("NOTES2INSIGHT_PORT", "8766"))

app = Flask(__name__, static_folder=None)
web_guard.install(app)
common_static.register(app)
# 每个文件已经在 uploads.py 里限了 30MB，但那道检查是读完整个文件之后才做的；
# 这里在请求层再挡一道，防止有人一次拖几百 MB 进来把内存吃满才发现超限。
# 300MB 留了够用的余量（正常一批不超过十来个 PDF）。
app.config["MAX_CONTENT_LENGTH"] = 300 * 1024 * 1024


@app.errorhandler(413)
def _too_large(_e):
    # 默认会返回一个 HTML 错误页；这个 app 的前端一律 await r.json()，不处理会在
    # 解析 JSON 那一步报一个无关的错误，看不出真正原因是文件太大了。
    return jsonify({"error": "这批文件加起来太大了（超过 300MB），分批拖入"}), 413

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
    jobs_util.prune_finished(JOBS, retention_seconds=JOB_RETENTION_SECONDS,
                             max_completed=MAX_COMPLETED_JOBS, now=now)


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


_SESSION_ID_RE = re.compile(r"^[0-9a-f]{32}$")


@app.route("/api/upload", methods=["POST"])
def api_upload():
    """拖入文件生成报告的入口：files 落到一个临时目录，返回的 notes 跟
    /api/notes 扫描出来的笔记长得一样，前端可以直接勾选、直接喂给 /api/run——
    只是 root 换成了这个临时目录，不是真正的笔记库，所以这批文件不会进库。

    session 由前端在同一个页面会话里复用，好让分几次拖拽的文件落进同一批；
    只在它确实是我们之前发过的那种 32 位十六进制字符串时才信任，否则一律
    现开一个新的——这个值最终会拼进磁盘路径，不能让客户端随便指定。
    """
    session_id = request.form.get("session", "")
    if not _SESSION_ID_RE.match(session_id):
        session_id = uuid.uuid4().hex

    files = request.files.getlist("files")
    if not files:
        return jsonify({"error": "没有收到文件"}), 400

    batch = [(f.filename or "未命名文件", f.read()) for f in files]
    uploads.prune_old_batches()
    dest_dir = os.path.join(uploads.UPLOADS_ROOT, session_id)
    try:
        notes, errors = uploads.save_batch(dest_dir, batch)
    except uploads.UploadError as e:
        return jsonify({"error": str(e)}), 400

    return jsonify({
        "session": session_id,
        "root": dest_dir,
        "notes": notes,
        "errors": errors,
    })


@app.route("/api/import_links", methods=["POST"])
def api_import_links():
    """从一段自由文本（或者就是一个链接）里批量提取链接，抓取成笔记——跟
    /api/upload 是同一个 session/临时目录，返回形状也一样，前端可以合并到
    同一份列表里，不用区分"这篇笔记是拖进来的还是导进来的"。
    """
    data = request.get_json(force=True) or {}
    session_id = data.get("session", "")
    if not _SESSION_ID_RE.match(session_id):
        session_id = uuid.uuid4().hex

    text = data.get("text") or ""
    if not text.strip():
        return jsonify({"error": "请粘贴包含链接的文字"}), 400
    try:
        urls = link_import.extract_import_urls(text)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 400

    uploads.prune_old_batches()
    dest_dir = os.path.join(uploads.UPLOADS_ROOT, session_id)
    # 最多 40 条链接、每条订阅源最多 20 篇，逐篇抓正文可能要好几分钟——放到后台跑，
    # 前端轮询 /api/progress 看进度，完成后从 /api/result 取回 notes/errors
    job_id = _new_job("import_links", len(urls), f"准备抓取 {len(urls)} 条链接")

    def work():
        progress = _progress_fn(job_id)
        try:
            notes, errors = link_import.import_urls(dest_dir, urls, progress)
            progress("done", len(urls), len(urls), f"导入完成：{len(notes)} 篇")
            _finish(job_id, ok=True, result={
                "session": session_id, "root": dest_dir, "notes": notes, "errors": errors,
            })
        except Exception as e:  # noqa: BLE001
            _finish(job_id, ok=False, error=str(e)[:800])

    threading.Thread(target=work, daemon=True).start()
    return jsonify({"job_id": job_id, "session": session_id, "root": dest_dir})


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
    def stop_flag() -> bool:
        with JOBS_LOCK:
            return bool((JOBS.get(job_id) or {}).get("stop_requested"))

    cfg.stop_flag = stop_flag
    try:
        result = pipeline.run(cfg, _progress_fn(job_id))
        _finish(job_id, ok=True, result=result)
    except llm.Stopped:
        _finish(job_id, ok=False, stopped=True)
    except Exception as e:
        _finish(job_id, ok=False, error=str(e)[:800])


def _resolve_llm(data: dict):
    """解析后端 / Key / Base（规则见 core/llm_config，与 summit2md 共用）。
    返回 (params, None) 或 (None, (payload, status))。"""
    try:
        return llm_config.resolve(data, default_backend="cli"), None
    except llm_config.ConfigError as e:
        return None, (jsonify({"error": str(e)}), 400)


_FOCUS_FROM_TOPIC_PROMPT = """你在帮一个人把一句话主题，展开成一段"报告关注点"说明，
用来指导后续从笔记里摘取内容、写技术洞察报告。

主题：{topic}

写一段 2-4 句的中文关注点说明，具体到：报告应该覆盖哪几个角度/维度，
重点辨析哪些分歧或争议，应该回答读者的什么疑问。不要复述主题本身，
不要加标题、编号或 Markdown 格式，只输出这段说明文字。"""


@app.route("/api/focus_from_topic", methods=["POST"])
def api_focus_from_topic():
    """根据一句话主题自动生成一段更具体的关注点说明。这是单次模型调用、几秒钟就
    出结果，做成同步接口、不给停止/暂停按钮——前端在用户填完主题后自动触发，
    用户等一下就好，跟点"生成"那种要跑几分钟的任务不是一回事。"""
    data = request.get_json(silent=True) or {}
    topic = (data.get("topic") or "").strip()
    if not topic:
        return jsonify({"error": "主题为空"}), 400

    llm_params, err = _resolve_llm(data)
    if err:
        return err

    try:
        text = llm.complete(
            _FOCUS_FROM_TOPIC_PROMPT.format(topic=topic),
            llm_params["backend"],
            api_key=llm_params["api_key"],
            model=llm_params["model"],
            api_base=llm_params["api_base"],
            max_tokens=400,
            timeout=60,
        )
    except llm.LLMError as e:
        return jsonify({"error": str(e)}), 502

    return jsonify({"focus": text.strip()})


class BadParam(ValueError):
    pass


@app.errorhandler(BadParam)
def _bad_param(e):
    return jsonify({"error": str(e)}), 400


def _int_param(data: dict, key: str, default: int, lo: int, hi: int) -> int:
    """前端传来的数字参数：空着用默认值，填得不是数字就给一句能看懂的 400，
    不要让 int() 直接抛成 500；超出范围的收到边界上。"""
    raw = data.get(key)
    if raw in (None, ""):
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise BadParam(f"参数 {key} 应该是整数，收到的是 {raw!r}") from None
    return min(max(value, lo), hi)


def _out_dir(data: dict) -> str:
    # 展开 ~：不然 "~/报告" 会在服务的当前目录下建出一个字面叫 "~" 的文件夹
    return os.path.abspath(os.path.expanduser((data.get("output_dir") or DEFAULT_OUTPUT_DIR).strip()))


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
        concurrency=_int_param(data, "concurrency", 3, 1, 16),
        timeout=_int_param(data, "timeout", 900, 30, 7200),
        output_dir=_out_dir(data),
        use_cache=bool(data.get("use_cache", True)),
        topic=(data.get("topic") or "").strip(),
        retrieval=data.get("retrieval") if isinstance(data.get("retrieval"), dict) else None,
        model_digest=(data.get("model_digest") or "").strip(),
        model_compose=(data.get("model_compose") or "").strip(),
        max_note_chars=_int_param(data, "max_note_chars", 0, 0, 10_000_000),
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


def _finish(job_id: str, *, ok: bool, result=None, error: str = "", stopped: bool = False) -> None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is not None:
            job.update(done=True, ok=ok, result=result, error=error, stopped=stopped,
                      finished_at=time.time())


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
        "candidates": _int_param(data, "candidates", search.DEFAULT_CANDIDATES, 1, 500),
        "do_screen": bool(data.get("screen", True)),
        "timeout": _int_param(data, "timeout", 300, 30, 7200),
        # 工具自己生成的报告默认不作为检索素材，免得越滚越自我引用
        "exclude_output": bool(data.get("exclude_output", True)),
        "output_dir": _out_dir(data),
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
    output_dir = _out_dir(data)
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
    timeout = _int_param(data, "timeout", 600, 30, 7200)
    vault_name = os.path.basename(os.path.realpath(os.path.expanduser(root)).rstrip(os.sep))
    want_pptx = bool(data.get("pptx"))
    reuse = bool(data.get("reuse"))
    if reuse and not os.path.exists(md_path[:-3] + ".deck.html"):
        return jsonify({"error": "这份报告还没有生成过演示，没法复用，请先生成一次"}), 400

    job_id = _new_job("deck", 3, "演示生成已排队")
    with JOBS_LOCK:
        JOBS[job_id]["stop_requested"] = False

    def stop_flag() -> bool:
        with JOBS_LOCK:
            job = JOBS.get(job_id)
            return bool(job and job.get("stop_requested"))

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
                    stop_flag=stop_flag,
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
        except llm.Stopped:
            _finish(job_id, ok=False, stopped=True)
        except Exception as e:
            _finish(job_id, ok=False, error=str(e)[:800])

    threading.Thread(target=work, daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/api/stop/<job_id>", methods=["POST"])
def api_stop_job(job_id):
    """只支持"停止"，不支持"暂停"——生成演示这类任务里能停的步骤本质是一次
    模型调用，暂停了再恢复跟重新发一次没区别，不给这个假选项。生成报告和生成
    演示的任务都会在每次模型调用前检查 stop_requested。"""
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"error": "任务不存在"}), 404
        job["stop_requested"] = True
    return jsonify({"ok": True})


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
            print("否则换个端口：NOTES2INSIGHT_PORT=9000 python3 -m apps.notes2insight.server")
            raise SystemExit(1) from e
        raise


if __name__ == "__main__":
    main()
