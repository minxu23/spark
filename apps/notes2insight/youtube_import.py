"""Durable YouTube transcripts, fetched through Summit's shared subtitle queue."""
from __future__ import annotations

import contextlib
import contextvars
import json
import os
import re
import threading

from apps.summit2md import pipeline as summit
from core import atomic
from .output_paths import topic_directory as materials_directory

_ROOT = contextvars.ContextVar('youtube_materials_root', default=None)
_LOCK = threading.Lock()


def current_root(required=True):
    root = _ROOT.get()
    if required and not root:
        raise ValueError('请先指定分析主题和输出目录')
    return root


@contextlib.contextmanager
def storage(root):
    token = _ROOT.set(root)
    try:
        yield
    finally:
        _ROOT.reset(token)


def _validate_id(vid):
    if not re.fullmatch(r'[A-Za-z0-9_-]{11}', vid or ''):
        raise ValueError('无效的 YouTube 视频 ID')


def _metadata(directory):
    try:
        with open(os.path.join(directory, 'metadata.json'), encoding='utf-8') as f:
            meta = json.load(f)
        return meta if isinstance(meta, dict) else {}
    except (OSError, ValueError):
        return {}


def _locate(vid):
    _validate_id(vid)
    root = current_root()
    if not os.path.isdir(root):
        return None
    for item in os.scandir(root):
        if item.name.startswith('.') or item.is_symlink() or not item.is_dir():
            continue
        entry = _metadata(item.path).get('entry')
        if isinstance(entry, dict) and entry.get('id') == vid:
            return item.path
    # Earlier imports could leave only downloaded subtitles in an ID directory.
    legacy = os.path.join(root, vid)
    return legacy if os.path.isdir(legacy) and not os.path.islink(legacy) and not _metadata(legacy) else None


def _directory(vid: str, title: str) -> str:
    existing = _locate(vid)
    if existing and os.path.basename(existing) != vid:
        return existing
    name = re.sub(r'[\x00-\x1f<>:"/\\|?*]', '_', title or '').strip(' .') or '未命名视频'
    while len(name.encode('utf-8')) > 180:
        name = name[:-1]
    candidate = os.path.join(current_root(), name)
    n = 2
    while os.path.lexists(candidate) and candidate != existing:
        candidate = os.path.join(current_root(), f'{name} ({n})')
        n += 1
    if existing and candidate != existing:
        os.rename(existing, candidate)
    return candidate


def cached(vid: str) -> tuple[dict, str] | None:
    directory = _locate(vid)
    if not directory:
        return None
    meta = _metadata(directory)
    try:
        with open(os.path.join(directory, 'transcript.md'), encoding='utf-8') as f:
            body = f.read()
        if meta.get('complete') and body.strip():
            return meta['entry'], body
    except (OSError, ValueError, KeyError):
        pass
    return None


def entries_for_url(url: str) -> list[dict]:
    link = summit.parse_youtube_url(url)
    if not link:
        raise ValueError('不是有效的 YouTube 链接')
    if link.video_id:
        hit = cached(link.video_id)
        if hit:
            return [dict(hit[0], source_type='youtube')]
        # A video carrying a playlist parameter still means this one video here.
        result = summit.fetch_playlist(link.video_url)
    elif link.playlist_id:
        result = summit.fetch_playlist(link.url)
    else:
        raise ValueError('请提供单条 YouTube 视频或播放列表链接，暂不展开整个频道')
    entries = [dict(e, source_type='youtube') for e in result.get('entries', [])]
    if not entries:
        raise RuntimeError('未能获取到视频条目，请检查视频或播放列表是否公开可访问')
    return entries


def prepare(entry: dict, check_stop=lambda: None) -> tuple[dict, str]:
    """Return complete transcript; incomplete/blocked downloads never become reusable materials."""
    vid = entry['id']
    with _LOCK:
        check_stop()
        directory = _directory(vid, entry.get('title', ''))
        hit = cached(vid)
        if hit:
            return hit
        os.makedirs(directory, exist_ok=True)
        atomic.write_json(os.path.join(directory, 'metadata.json'), {
            'entry': {k: entry[k] for k in ('id', 'title', 'url', 'duration') if k in entry}, 'complete': False})
        status = {}
        sub = summit.download_subtitle(vid, os.path.join(directory, '.cache'),
                                       ['en', 'zh-Hans', 'zh', 'zh-Hant'], status=status)
        check_stop()
        if not sub:
            raise RuntimeError(summit.subtitle_error_message(status.get('reason', ''), status.get('upload_date', '')))
        paragraphs = summit.vtt_to_paragraphs(sub['path'])
        if not any(text.strip() for _, text in paragraphs):
            raise RuntimeError('字幕正文为空，未保存为资料，请稍后重试')
        saved_entry = {k: entry[k] for k in ('id', 'title', 'url', 'duration') if k in entry}
        saved_entry.setdefault('duration', 0)
        saved_entry['sub_kind'] = sub.get('kind', 'auto')
        if sub.get('upload_date'):
            saved_entry['publish_date'] = sub['upload_date']
        body = summit.render_transcript_md(saved_entry, 'Notes2Insight YouTube 资料', paragraphs,
                                          None, sub['lang'], include_summary=False)
        check_stop()
        os.makedirs(directory, exist_ok=True)
        atomic.write_text(os.path.join(directory, 'transcript.md'), body)
        atomic.write_json(os.path.join(directory, 'metadata.json'), {'entry': saved_entry, 'complete': True})
        return saved_entry, body
