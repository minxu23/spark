"""
拖入文件生成报告：不需要笔记已经在 Obsidian 笔记库里，也不会把上传的文件写进
笔记库——落在一个临时目录里，把这个目录当作这一次任务专属的"笔记库根"传给
pipeline.run()，其余摘取/归纳/成文/缓存全部复用现有逻辑，不需要改 pipeline.py
一行代码：RunConfig.vault_root 本来就是随便一个目录都行，read_note() 也只是
os.path.join(root, rel) 读文件。

支持的格式：.md / .txt 直接当文本读；.pdf 用 pypdf 抽取；.docx 用 python-docx
抽取段落。其它格式当场报错，不影响同批里的其它文件——单个文件解析失败不该
拖累整批。
"""

from __future__ import annotations

import io
import os
import re
import shutil
import time
from typing import Optional

APP_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOADS_ROOT = os.path.join(APP_DIR, ".uploads")

# 单个文件的大小上限：主要是防一个几百 MB 的 PDF 把内存和一次 LLM 调用都拖垮，
# 而不是这个格式本身抽取不了。跟摘取阶段的分块逻辑无关——那是笔记字数的事，
# 这里是原始文件字节数的事，先挡在最前面。
MAX_FILE_BYTES = 30 * 1024 * 1024

# 一次拖拽/选择最多接受这么多个文件；真正跑报告时的篇数上限是 pipeline.MAX_NOTES，
# 这里单独限一道是为了不让一次上传请求本身处理太久。
MAX_FILES_PER_BATCH = 60

# 上传目录是临时的，不属于任何正式产物；超过这个时间没人用就清掉，
# 和 server.py 里 JOBS 的清理是同一个"访问时顺手清理"的套路。
RETENTION_SECONDS = 7 * 24 * 3600

SUPPORTED_EXTENSIONS = {".md", ".txt", ".pdf", ".docx"}


class UploadError(RuntimeError):
    pass


def _safe_stem(original_name: str) -> str:
    """从原始文件名取一个能安全当路径分量用的词干。

    不用 werkzeug.secure_filename——它是给"必须是 ASCII 安全文件名"的场景设计的，
    会把非 ASCII 字符整段砍掉；secure_filename('笔记.md') 会变成 'md'，词干直接
    没了。这个工具面向中文用户，文件名十有八九是中文，砍掉就等于处理不了任何
    正常文件。这里只做真正需要的事：只取最后一段（防目录穿越）、把路径分隔符
    和控制字符换成下划线、掐长度——中文、空格、括号都保留。
    """
    base = os.path.basename(original_name.replace("\\", "/"))
    stem = os.path.splitext(base)[0]
    stem = re.sub(r'[\x00-\x1f<>:"/\\|?*]', "_", stem).strip(" .")
    return stem[:120] or "未命名文件"


def _extract_text(name: str, data: bytes) -> str:
    ext = os.path.splitext(name)[1].lower()
    if ext in (".md", ".txt"):
        return data.decode("utf-8", errors="replace")
    if ext == ".pdf":
        return _extract_pdf(data)
    if ext == ".docx":
        return _extract_docx(data)
    raise UploadError(f"不支持的格式：{ext or '（无扩展名）'}，目前支持 .md .txt .pdf .docx")


def _extract_pdf(data: bytes) -> str:
    try:
        import pypdf
    except ImportError as e:
        raise UploadError("未安装 pypdf（pip install pypdf），无法解析 PDF") from e
    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
        pages = [(p.extract_text() or "").strip() for p in reader.pages]
    except Exception as e:  # pypdf 对损坏/加密 PDF 的异常类型不固定，统一包装
        raise UploadError(f"PDF 解析失败：{e}") from e
    text = "\n\n".join(p for p in pages if p)
    if not text.strip():
        # 常见于扫描版 PDF（整页是图片，没有可提取的文字层）；不是代码的错，
        # 但必须明确说出来，不能悄悄生成一篇空笔记
        raise UploadError("这份 PDF 提取不出文字——大概率是扫描件/图片版，没有文字层")
    return text


def _extract_docx(data: bytes) -> str:
    try:
        import docx
    except ImportError as e:
        raise UploadError("未安装 python-docx（pip install python-docx），无法解析 docx") from e
    try:
        doc = docx.Document(io.BytesIO(data))
        paras = [p.text for p in doc.paragraphs if p.text.strip()]
    except Exception as e:
        raise UploadError(f"docx 解析失败：{e}") from e
    text = "\n\n".join(paras)
    if not text.strip():
        raise UploadError("这份 docx 是空的（也可能是 .doc 老格式，需要先另存为 .docx）")
    return text


def _unique_path(dest_dir: str, stem: str) -> tuple[str, str]:
    """避免同批次里重名文件互相覆盖：foo.md 已存在就依次试 foo (2).md、foo (3).md……"""
    name = f"{stem}.md"
    n = 2
    while os.path.exists(os.path.join(dest_dir, name)):
        name = f"{stem} ({n}).md"
        n += 1
    return name, os.path.splitext(name)[0]


def prune_old_batches(root: str = UPLOADS_ROOT, *, now: Optional[float] = None) -> None:
    now = now if now is not None else time.time()
    try:
        entries = os.listdir(root)
    except OSError:
        return
    for name in entries:
        path = os.path.join(root, name)
        try:
            if now - os.path.getmtime(path) > RETENTION_SECONDS:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            continue


def save_batch(dest_dir: str, files: list[tuple[str, bytes]]) -> tuple[list[dict], list[dict]]:
    """把一批上传文件抽取成文字，各自存成一篇 .md，落在 dest_dir 下（拍平，不建子目录）。

    返回 (notes, errors)：notes 和 vault.scan() 产出的字典形状一致（path/folder/name/
    title/date/bytes/chars/mtime），这样前端和 pipeline 都能像对待库里笔记一样对待它们；
    errors 是 [{"name": 原始文件名, "error": 原因}]，单个文件失败不影响同批其它文件。
    """
    if len(files) > MAX_FILES_PER_BATCH:
        raise UploadError(f"一次最多拖 {MAX_FILES_PER_BATCH} 个文件，这次有 {len(files)} 个")

    os.makedirs(dest_dir, exist_ok=True)
    notes: list[dict] = []
    errors: list[dict] = []

    for original_name, data in files:
        # 扩展名直接从原始文件名取（basename 去掉路径部分即可，不需要整段消毒）——
        # 判断"这是不是我们认得的格式"跟"文件名能不能安全落盘"是两件事，不能用
        # 同一次消毒处理两者，否则中文文件名会在还没判断格式之前就先被砍没了。
        base_name = os.path.basename(original_name.replace("\\", "/")) or "未命名文件"
        ext = os.path.splitext(base_name)[1].lower()
        if ext not in SUPPORTED_EXTENSIONS:
            errors.append({"name": original_name,
                          "error": f"不支持的格式：{ext or '（无扩展名）'}，目前支持 .md .txt .pdf .docx"})
            continue
        if len(data) > MAX_FILE_BYTES:
            mb = MAX_FILE_BYTES // (1024 * 1024)
            errors.append({"name": original_name, "error": f"超过单文件 {mb}MB 上限"})
            continue
        try:
            text = _extract_text(base_name, data)
        except UploadError as e:
            errors.append({"name": original_name, "error": str(e)})
            continue

        stem = _safe_stem(original_name)
        fname, title = _unique_path(dest_dir, stem)
        header = f"# {title}\n\n- 来源：拖入的文件「{original_name}」（不在笔记库里，只用于这次生成）\n\n"
        body = header + text.strip() + "\n"
        full = os.path.join(dest_dir, fname)
        with open(full, "w", encoding="utf-8") as f:
            f.write(body)

        st = os.stat(full)
        notes.append({
            "path": fname, "folder": ".", "name": fname, "title": title, "date": "",
            "bytes": st.st_size, "chars": len(body), "mtime": int(st.st_mtime),
        })

    return notes, errors
