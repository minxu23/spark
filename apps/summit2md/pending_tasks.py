"""持久保存会议续跑计划；实际完成情况仍以产物 manifest 为准，不保存密钥。"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path

from core import atomic
from . import pipeline, recording_groups

STATE_PATH = Path(__file__).parent / '.state' / 'pending_meetings.json'
_LOCK = threading.RLock()


def _read(state_path=None):
    try:
        return json.loads(Path(state_path or STATE_PATH).read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {}


def _write(records, state_path):
    Path(state_path).parent.mkdir(parents=True, exist_ok=True)
    atomic.write_json(str(state_path), records, indent=2)


def key(path):
    return hashlib.sha256(os.path.realpath(path).encode()).hexdigest()[:24]


def remember(path, config, entries):
    if config.get('content_type') != 'summit':
        return
    state_path = STATE_PATH
    with _LOCK:
        records = _read(state_path)
        previous = records.get(key(path), {})
        merged = {e['id']: e for e in previous.get('entries', [])}
        merged.update({e['id']: e for e in entries})
        records[key(path)] = dict(path=os.path.realpath(path), config=config,
                                  entries=list(merged.values()), updated_at=time.time(), interrupted=True)
        _write(records, state_path)


def finish(path, job, state_path=None):
    state_path = state_path or STATE_PATH
    with _LOCK:
        records = _read(state_path)
        record = records.get(key(path))
        if record is None:
            return
        record['interrupted'] = bool(job.get('error') or (job.get('result') or {}).get('stopped'))
        record['overall_failed'] = any('大会总结生成失败' in line for line in job.get('log', []))
        record['updated_at'] = time.time()
        _write(records, state_path)


def describe(record):
    path = record['path']
    manifest = pipeline._load_manifest(path)
    saved = manifest.get('entries') or {}
    config = record.get('config')
    if manifest.get('content_type') == 'series':
        return None
    requested = {e['id']: e for e in record.get('entries', [])}
    # 之前分批处理过的失败项也一起列出；未曾要求生成的小结/整理稿不算漏项。
    entries = {k: v.get('entry') for k, v in saved.items() if v.get('entry')}
    entries.update(requested)
    pending, reasons = [], set()
    for eid, entry in entries.items():
        old = saved.get(eid)
        opts = config or {}
        kind, _, summary, speech = pipeline._entry_plan(
            entry, old, path, do_summary=bool(opts.get('do_summary') and eid in requested),
            do_speech_script=bool(opts.get('do_speech_script') and eid in requested), skip_existing=True,
            speech_lang_mode=opts.get("speech_lang_mode", "bilingual"))
        if not old or not old.get('ok') or kind != 'skip':
            pending.append(entry)
            reasons.add('议题小结' if summary else '整理稿' if speech else '议题内容')
    overall = manifest.get('overall_summary') or ''
    overall_needed = bool(pipeline._is_failed_overall_summary(overall) or record.get('overall_failed'))
    if config and config.get('do_overall_summary', config.get('do_summary')):
        if (record.get('interrupted') or not overall) and len(entries) > 1:
            overall_needed = True
    if overall_needed:
        reasons.add('大会总结')
    if not pending and not overall_needed:
        return None
    return dict(id=key(path), title=(config or {}).get('summit_title') or os.path.basename(path),
                path=path, config=config, entries=list(entries.values()), pending_entries=pending,
                overall_needed=overall_needed, pending_count=len(pending), reasons=sorted(reasons),
                updated_at=record.get('updated_at', 0))


def list_pending(root):
    with _LOCK:
        records = _read()
    # 兼容此前没有续跑记录的会议；只读扫描，不修改笔记库。
    root = Path(root)
    if root.is_dir():
        for manifest in recording_groups.manifest_paths(root):
            path = str(manifest.parent.resolve())
            if key(path) not in records:
                records[key(path)] = dict(path=path, updated_at=manifest.stat().st_mtime)
    items = []
    for record in records.values():
        if not os.path.isdir(record['path']):
            continue
        try:
            item = describe(record)
        except (OSError, ValueError, KeyError, TypeError, pipeline.ManifestCorrupt):
            continue
        if item:
            items.append(item)
    return sorted(items, key=lambda x: x['updated_at'], reverse=True)


def regroup(root, children, *, dry_run=False):
    """Carry requested work into its new physical folder, including unprocessed talks."""
    root = Path(root).resolve()
    with _LOCK:
        records = _read()
        affected = {k:r for k,r in records.items() if Path(r['path']).resolve().is_relative_to(root)}
        if not affected:
            return
        owner = {}
        for name,child in children.items():
            for entry in child['group']['entries']:
                owner.setdefault(entry['id'], (name,child))
        rebuilt = {}
        for record in sorted(affected.values(),key=lambda r:r.get('updated_at',0)):
            for entry in record.get('entries',[]):
                if entry['id'] not in owner:
                    raise ValueError('待完成清单仍有未归类议题，原清单已保留')
                name,child = owner[entry['id']]
                path = str(root/name)
                current = rebuilt.setdefault(key(path), {**record,'path':path,'entries':[]})
                if entry['id'] not in {e['id'] for e in current['entries']}:
                    current['entries'].append({**entry,'collection':child['manifest']['collection']})
                context = child['manifest']['collection']
                current['config'] = {**(record.get('config') or {}),
                    'summit_title':recording_groups.canonical_title('',context), 'content_type':'summit',
                    'source_url':child['manifest']['source_url'], 'collection':context,
                    'output_dir':str(root.parent), 'do_overall_summary':False}
                current['overall_failed'] = False  # Hierarchical summaries are retried in the guide.
        if dry_run:
            return
        for k in affected:
            records.pop(k)
        records.update(rebuilt)
        _write(records,STATE_PATH)
