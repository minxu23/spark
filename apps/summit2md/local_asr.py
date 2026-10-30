"""YouTube 视频没有任何字幕时，下载音频在本机做语音识别（Qwen3-ASR 1.7B，MLX，仅 Apple Silicon）。

- 音频下载、转 WAV、识别都在临时目录里做，结束自动清掉；只把识别出的 VTT 和一份元数据留在
  缓存目录（{id}.asr.vtt / {id}.asr.json），重试时直接复用。
- 识别跑在子进程里：模型占几 GB 显存，不留在服务进程里；用户点"停止"时能直接结束子进程。
- 给模型的上下文取自标题和简介里的专有名词，名字拼得更准；但没人说话的片段（片头曲、
  转场音乐）里模型会把上下文原样念出来，所以识别完要把这种只由上下文词组成的连续片段删掉。
  实测（Mistral AI Now Summit 2026 开场主旨，42 分钟）：带上下文 Timothée 拼对 7 次、不带 3 次；
  带上下文时片头曲那两分钟被替换成了 7 遍上下文词表。
"""

from __future__ import annotations

import importlib.util
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Callable, Optional

from core import atomic
from core.llm import Stopped

ASR_MODEL = "Qwen/Qwen3-ASR-1.7B"
ASR_LABEL = "Qwen3-ASR 1.7B"
# 一次只跑一个识别：1.7B 模型 + 对齐模型大约占 5 GB 显存，并发没有好处
_ASR_LOCK = threading.Lock()

# Qwen3-ASR 报的语言名 → Spark 里通用的语言代码
LANG_CODES = {"english": "en", "chinese": "zh", "french": "fr", "german": "de", "spanish": "es",
              "japanese": "ja", "korean": "ko", "italian": "it", "portuguese": "pt", "russian": "ru",
              "arabic": "ar", "dutch": "nl", "cantonese": "yue"}

# 句首常见的大写词、简介里的平台用语：不是专有名词，不进上下文
_NOT_TERMS = {
    "the", "a", "an", "and", "or", "in", "on", "of", "for", "to", "with", "at", "by", "from", "as",
    "we", "our", "you", "your", "they", "their", "this", "that", "these", "those", "it", "its", "is",
    "are", "was", "be", "how", "what", "why", "when", "who", "where", "read", "watch", "follow",
    "subscribe", "join", "learn", "more", "full", "also", "talk", "keynote", "opening", "closing",
    "session", "panel", "about", "here", "http", "https", "www", "they", "he", "she", "his", "her",
    "i", "if", "but", "not", "all", "new", "one",
    "youtube", "linkedin", "twitter", "instagram", "facebook", "tiktok", "discord", "github", "x",
}
_TERM_RE = re.compile(r"[A-Za-zÀ-ÿ0-9][\w'’.+\-]*")


def ffmpeg_path() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg  # 随 pip 包带的 ffmpeg，本机没装 Homebrew 时用它
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001
        return ""


def unavailable_reason() -> str:
    """本机能不能做语音识别；能返回空串，不能返回原因（给日志和报错用）。"""
    if sys.platform != "darwin" or platform.machine() != "arm64":
        return "本地语音识别只支持 Apple Silicon 的 Mac"
    if importlib.util.find_spec("mlx_qwen3_asr") is None:
        return "没有安装 mlx-qwen3-asr（pip install mlx-qwen3-asr）"
    if not ffmpeg_path():
        return "没有 ffmpeg（pip install imageio-ffmpeg，或用 Homebrew 装 ffmpeg）"
    return ""


def context_terms(title: str, description: str, limit: int = 40) -> list[str]:
    """标题和简介里像专有名词的词（大写开头、带数字或全大写），去重保序。"""
    # 链接和 #话题标签（#PublicServices 这种连写词）都不是讲话里会出现的词
    text = re.sub(r"https?://\S+|#\w+", " ", f"{title}\n{(description or '')[:2000]}")
    out: list[str] = []
    seen: set[str] = set()
    for w in _TERM_RE.findall(text):
        w = re.sub(r"['’]s$", "", w.strip(".'’-"))   # Mistral's → Mistral
        low = w.lower()
        if not w or low in _NOT_TERMS or low in seen or len(w) < 2:
            continue
        if not (w[0].isupper() or any(c.isdigit() for c in w)):
            continue
        seen.add(low)
        out.append(w)
        if len(out) >= limit:
            break
    return out


def _words(text: str) -> list[str]:
    return [w.strip(".,!?;:'’\"()").lower() for w in text.split() if w.strip(".,!?;:'’\"()")]


_CUE_RE = re.compile(r"(\d{2}:\d{2}:\d{2}\.\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}\.\d{3})")


def parse_vtt(text: str) -> list[tuple[str, str]]:
    """[(时间行, 文本), ...]"""
    cues: list[tuple[str, str]] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        if _CUE_RE.search(lines[i]):
            timing = lines[i].strip()
            i += 1
            body = []
            while i < len(lines) and lines[i].strip():
                body.append(lines[i].strip())
                i += 1
            cues.append((timing, " ".join(body)))
        i += 1
    return cues


def drop_context_echo(cues: list[tuple[str, str]], terms: list[str],
                      min_cues: int = 2, min_words: int = 6) -> tuple[list[tuple[str, str]], int]:
    """删掉"只由上下文词组成"的连续片段（至少 min_cues 条、合计 min_words 个词）。
    真实讲话里偶尔一条只说 "Mistral AI"，单条不删；连着好几条全是上下文词，就是模型在念上下文。"""
    vocab = {w for t in terms for w in _words(t)}
    if not vocab:
        return cues, 0
    echo = []
    for _, text in cues:
        ws = _words(text)
        echo.append(len(ws) >= 1 and all(w in vocab for w in ws))
    keep = [True] * len(cues)
    i = 0
    while i < len(cues):
        if not echo[i]:
            i += 1
            continue
        j = i
        while j < len(cues) and echo[j]:
            j += 1
        if j - i >= min_cues and sum(len(_words(cues[k][1])) for k in range(i, j)) >= min_words:
            for k in range(i, j):
                keep[k] = False
        i = j
    kept = [c for c, k in zip(cues, keep) if k]
    return kept, len(cues) - len(kept)


def write_vtt(path: str, cues: list[tuple[str, str]]) -> None:
    body = "WEBVTT\n\n" + "".join(f"{t}\n{x}\n\n" for t, x in cues)
    atomic.write_text(path, body)


def cached(video_id: str, cache_dir: str) -> Optional[dict]:
    vtt = os.path.join(cache_dir, f"{video_id}.asr.vtt")
    meta_path = os.path.join(cache_dir, f"{video_id}.asr.json")
    if not (os.path.isfile(vtt) and os.path.getsize(vtt) > 0):
        return None
    try:
        with open(meta_path, encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, ValueError):
        meta = {}
    return {"lang": meta.get("lang") or "en", "path": vtt, "description": meta.get("description") or "",
            "upload_date": meta.get("upload_date") or "", "kind": "asr"}


def _run(cmd: list[str], stop_flag: Optional[Callable[[], bool]], timeout: float) -> subprocess.CompletedProcess:
    """跑子进程；用户点了停止就结束它并抛 Stopped。"""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    deadline = time.monotonic() + timeout
    while True:
        try:
            out, _ = proc.communicate(timeout=1)
            return subprocess.CompletedProcess(cmd, proc.returncode, out, "")
        except subprocess.TimeoutExpired:
            if (stop_flag and stop_flag()) or time.monotonic() > deadline:
                proc.kill()
                proc.communicate()
                if stop_flag and stop_flag():
                    raise Stopped("用户停止了任务")
                raise RuntimeError(f"语音识别超时（超过 {int(timeout // 60)} 分钟）")


def transcribe(video_id: str, url: str, cache_dir: str, *, youtube_dl, title: str = "",
               stop_flag: Optional[Callable[[], bool]] = None) -> dict:
    """下载音频 → 转 16 kHz 单声道 WAV → Qwen3-ASR 识别 → 去掉念上下文的片段 → 存 {id}.asr.vtt。
    返回和 download_subtitle 一样的 dict（kind="asr"）；失败抛 RuntimeError（原因可直接展示）。
    youtube_dl 是 pipeline._youtube_dl：走同一个排队/限流/登录状态逻辑。"""
    hit = cached(video_id, cache_dir)
    if hit:
        return hit
    problem = unavailable_reason()
    if problem:
        raise RuntimeError(problem)
    os.makedirs(cache_dir, exist_ok=True)
    with _ASR_LOCK, tempfile.TemporaryDirectory(prefix="spark-asr-") as tmp:
        try:
            with youtube_dl({"format": "bestaudio[ext=m4a]/bestaudio", "quiet": True, "no_warnings": True,
                             "noprogress": True, "outtmpl": os.path.join(tmp, "audio.%(ext)s")}) as ydl:
                info = ydl.extract_info(url, download=True) or {}
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"下载音频失败：{str(e)[:200]}") from e
        audio = next((os.path.join(tmp, f) for f in os.listdir(tmp) if f.startswith("audio.")), "")
        if not audio:
            raise RuntimeError("下载音频失败：没有得到音频文件")
        if stop_flag and stop_flag():
            raise Stopped("用户停止了任务")
        wav = os.path.join(tmp, "audio.wav")
        conv = _run([ffmpeg_path(), "-loglevel", "error", "-y", "-i", audio, "-ac", "1", "-ar", "16000", wav],
                    stop_flag, timeout=600)
        if conv.returncode != 0 or not os.path.isfile(wav):
            raise RuntimeError(f"音频转换失败：{(conv.stdout or '').strip()[-200:]}")

        description = info.get("description") or ""
        terms = context_terms(title or info.get("title") or "", description)
        out_dir = os.path.join(tmp, "out")
        cmd = [sys.executable, "-m", "mlx_qwen3_asr", wav, "--model", ASR_MODEL, "-f", "all",
               "--timestamps", "-o", out_dir, "--no-progress"]
        if terms:
            cmd += ["--context", " ".join(terms)]
        duration = float(info.get("duration") or 3600)
        res = _run(cmd, stop_flag, timeout=1800 + duration)
        vtt_src = os.path.join(out_dir, "audio.vtt")
        if res.returncode != 0 or not os.path.isfile(vtt_src):
            raise RuntimeError(f"语音识别失败：{(res.stdout or '').strip()[-300:]}")
        language = ""
        try:
            with open(os.path.join(out_dir, "audio.json"), encoding="utf-8") as f:
                language = (json.load(f).get("language") or "").strip()
        except (OSError, ValueError):
            pass
        with open(vtt_src, encoding="utf-8") as f:
            cues = parse_vtt(f.read())
        cues, dropped = drop_context_echo(cues, terms)
        if not any(text.strip() for _, text in cues):
            raise RuntimeError("语音识别没有识别出内容")

    lang = LANG_CODES.get(language.lower(), language.lower() or "en")
    vtt = os.path.join(cache_dir, f"{video_id}.asr.vtt")
    write_vtt(vtt, cues)
    atomic.write_json(os.path.join(cache_dir, f"{video_id}.asr.json"),
                      {"lang": lang, "language": language, "model": ASR_MODEL, "context": terms,
                       "dropped_echo_cues": dropped, "description": description,
                       "upload_date": info.get("upload_date") or ""})
    return {"lang": lang, "path": vtt, "description": description,
            "upload_date": info.get("upload_date") or "", "kind": "asr", "dropped_echo_cues": dropped}
