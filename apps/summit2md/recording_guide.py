"""Searchable recording directory; indexes metadata and links to existing local material."""
from __future__ import annotations

import copy
import html
import json
import os
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit, urlunsplit, parse_qsl

from core import atomic
from . import recorded_content, recording_groups

STATIC = Path(__file__).parent / 'static'
MARKER = '<!-- spark-recording-guide -->'


def source_url(source):
    p = urlsplit(source)
    if p.scheme not in ('http','https') or not p.hostname or p.username or p.password:
        raise ValueError('请提供大会官方录像目录的 HTTP/HTTPS 地址')
    query = urlencode([(k,v) for k,v in parse_qsl(p.query, keep_blank_values=True)
                       if not k.lower().startswith('utm_') and k not in ('fbclid','gclid')])
    return urlunsplit((p.scheme, p.netloc, p.path, query, ''))


def guide_directory(output_dir, source, title=None):
    from .pipeline import sanitize_filename
    import hashlib
    source = source_url(source)
    root = Path(output_dir).expanduser().resolve()
    for snapshot in root.glob('*/.recordings.json'):
        if not snapshot.parent.is_symlink() and _json(snapshot).get('source_url') == source:
            return snapshot.parent
    if not title:
        parts = urlsplit(source)
        bits = parts.path.strip('/').split('/')
        name = bits[-2] if len(bits)>1 else bits[-1] or parts.hostname
        title = name.replace('-', ' ').title().replace('Ocp', 'OCP')
    name = sanitize_filename(title)
    target = root / name
    snapshot = target / '.recordings.json'
    if snapshot.exists() and _json(snapshot).get('source_url') != source:
        target = root / (name + ' ' + hashlib.sha256(source.encode()).hexdigest()[:8])
    return target


def _json(path):
    try:
        result = json.loads(path.read_text())
        return result if isinstance(result, dict) else {}
    except (OSError, ValueError):
        return {}


def _existing(root, relative):
    if not relative:
        return None
    root = root.resolve()
    path = (root / relative).resolve()
    if path.is_relative_to(root) and path.is_file() and not any(p.startswith('.') for p in path.relative_to(root).parts):
        return str(path)
    return None


def local_materials(output_dir, materials_roots=()):
    found = {}
    base = Path(output_dir)
    if base.is_dir():
        for manifest_path in sorted(recording_groups.manifest_paths(base)):
            folder = manifest_path.parent
            if not folder.is_dir() or folder.is_symlink() or folder.name.startswith('.'):
                continue
            for row in _json(folder / '.manifest.json').get('entries', {}).values():
                entry = row.get('entry') or {}
                paths = [(label, _existing(folder, row.get(key))) for key, label in
                         [('speech_relative_path', '整理稿'), ('relative_path', '文字记录')]]
                for label, path in paths:
                    if path:
                        summary = row.get('summary') or {}
                        body = summary.get('body') or ''
                        digest = '\n\n'.join(s for s in (summary.get('tldr'), body) if s) if body.strip() else ''
                        found.setdefault(entry.get('id'), {'label': label, 'path': path, 'summary_text':digest})
                        break
    for root in materials_roots:
        base = Path(root)
        for meta_path in base.glob('*/*/metadata.json') if base.is_dir() else []:
            if any(p.startswith('.') for p in meta_path.relative_to(base).parts):
                continue
            meta = _json(meta_path)
            entry = meta.get('entry') or {}
            path = _existing(base, str(meta_path.parent.relative_to(base) / 'transcript.md'))
            if meta.get('complete') and path:
                found.setdefault(entry.get('id'), {'label': '文字记录', 'path': path})
    return found


def with_local(data, output_dir, materials_roots=(), *, offline=False):
    from . import recording_summaries as summaries
    data = recorded_content.normalize_directory(data)
    local = local_materials(output_dir, materials_roots)
    directory = guide_directory(output_dir, data['source_url'], data['summit_title'])
    unique = set()
    saved = set()
    state = _json(directory / '.recording-summaries.json')
    summary_groups = recording_groups.groups(data['recorded_tracks'])
    overall_fp = summaries.overall_fingerprint(summary_groups, local, state.get('tracks', {}))
    for group in summary_groups:
        record = state.get('tracks', {}).get(group['id'])
        rows = summaries.record_rows(group, local, record)
        fp = summaries.track_fingerprint(group, rows)
        group['local_count'] = len(rows)
        failure = state.get('failures', {}).get(group['id'])
        if failure:
            group['summary_failure'] = failure
        if summaries.valid_record(directory, record):
            path = str(directory / record['path'])
            group['summary'] = {**record, 'stale': record.get('fingerprint') != fp,
                                'url': reader_url(path, offline=offline)}
        folder = Path(summaries.filename(group)).parent
        index = _existing(directory, str(folder / (folder.name + '.md')))
        if index:
            group['index_url'] = reader_url(index, offline=offline)
    for track in data['recorded_tracks']:
        group = next(g for g in summary_groups if any(t['id'] == track['id'] for t in g['agenda_tracks']))
        track['summary_group_id'] = group['id']
        track['local_count'] = len(summaries.track_inputs(track, local))
        for entry in track['entries']:
            unique.add(entry['id'])
            hit = local.get(entry['id'])
            if hit:
                saved.add(entry['id'])
                href = reader_url(hit['path'], offline=offline)
                entry['local'] = {'label': hit['label'], 'url': href}
    data['total'] = len(unique)
    data['local_count'] = len(saved)
    overall = state.get('overall')
    if summaries.valid_record(directory, overall):
        data['overall_summary'] = {**overall, 'stale': overall.get('fingerprint') != overall_fp,
                                  'url': reader_url(directory/overall['path'], offline=offline)}
    # The browser needs metadata, not a second copy of every session.
    data['summary_groups'] = [{k:v for k,v in g.items() if k != 'entries'} for g in summary_groups if g.get('summary_role') != 'overview']
    data['overview_groups'] = [{k:v for k,v in g.items() if k != 'entries'} for g in summary_groups if g.get('summary_role') == 'overview']
    data['output_dir'] = str(Path(output_dir).expanduser().resolve())
    from .recording_grouping import fragmented
    data['grouping_needs_review'] = fragmented(data)
    return data


def reader_url(path, *, offline=False):
    # A file opened from Finder still needs Spark's reader, not a raw .md URL.
    return ('http://127.0.0.1:8760' if offline else '') + '/read/open?' + urlencode({'path':str(Path(path).resolve())})


def render(data, script_url=None):
    template = (STATIC / 'recording-guide.html').read_text()
    payload = json.dumps(data, ensure_ascii=False).replace('<', '\\u003c').replace('&', '\\u0026')
    script = ('<script src="' + html.escape(script_url, quote=True) + '"></script>' if script_url else
              '<script>' + (STATIC / 'recording-guide.js').read_text() + '</script>')
    return template.replace('__SCRIPT__', script).replace('__TITLE__', html.escape(data['summit_title'])).replace('__DATA__', payload)


def index_filename(directory):
    """The conference document is the same-name md (summary + topic directory). Only while a file
    we did not write still occupies that name (filing moves it to 旧版产物) use 大会目录.md."""
    named = directory / (directory.name + '.md')
    if named.exists() and MARKER not in named.read_text():
        return '大会目录.md'
    return directory.name + '.md'


def write_conference_summary(directory, title, section):
    """Put the conference summary into the conference document, keeping the topic directory."""
    from . import recording_documents as documents
    path = directory / index_filename(directory)
    if path.exists() and MARKER not in path.read_text():
        raise ValueError(f'已有同名文件，未覆盖：{path.name}')
    text = path.read_text() if path.exists() else ''
    if documents.CONF_DIRECTORY_MARKER in text:
        head = text.split(documents.CONF_SUMMARY_MARKER,1)[0] if documents.CONF_SUMMARY_MARKER in text \
            else text.split(documents.CONF_DIRECTORY_MARKER,1)[0]
        tail = documents.CONF_DIRECTORY_MARKER + text.split(documents.CONF_DIRECTORY_MARKER,1)[1]
    else:
        head, tail = '\n'.join([MARKER, '# ' + title, '', '']), documents.CONF_DIRECTORY_MARKER + '\n'
    atomic.write_text(str(path), head.rstrip() + '\n\n' + section.rstrip() + '\n\n' + tail)
    return path


def save(data, output_dir, materials_roots=()):
    data = recorded_content.normalize_directory(data)
    data['source_url'] = source_url(data['source_url'])
    directory = guide_directory(output_dir, data['source_url'], data['summit_title'])
    html_path = directory / '大会导航.html'
    md_name = index_filename(directory)
    md_path = directory / md_name
    for path in [html_path, md_path]:
        if path.exists() and MARKER not in path.read_text():
            raise ValueError(f'已有同名文件，未覆盖：{path.name}')
    snapshot = directory / '.recordings.json'
    if snapshot.exists() and _json(snapshot).get('source_url') != data['source_url']:
        raise ValueError('已有其他来源的目录索引，未覆盖')
    directory.mkdir(parents=True, exist_ok=True)
    atomic.write_json(str(directory / '.recordings.json'), data)
    enriched = with_local(data, output_dir, materials_roots, offline=True)
    atomic.write_text(str(html_path), render(enriched))
    url = '/summit/recordings?' + urlencode({'source': data['source_url'], 'output_dir': str(Path(output_dir).expanduser().resolve())})
    from . import recording_documents as documents
    summary = documents.conference_summary(directory, md_name)
    import time
    lines = [MARKER, '# ' + data['summit_title'], '',
             f"> 由 summit2md 生成于 {time.strftime('%Y-%m-%d %H:%M')}  ",
             f"> 官网录像目录：{data['source_url']}", '',
             f"共 {enriched['total']} 个视频 · {len(data['recorded_tracks'])} 个官网议程 · {len(enriched['summary_groups'])} 个总结组 · 本地已有 {enriched['local_count']} 个视频的资料", '',
             '[打开可搜索的大会导航](http://127.0.0.1:8760' + url + ') · [离线导航文件](大会导航.html)', '']
    if summary:
        lines += [summary, '']
    lines += [documents.CONF_DIRECTORY_MARKER, '## 总结与资料分组', '']
    for group in enriched['summary_groups']:
        summary = group.get('summary')
        extra = f" · [专题总结]({quote(summary['path'])})（覆盖 {summary['covered']}/{summary['total']}）" if summary else ' · 专题总结待生成'
        if group.get('index_url') and (not summary or group['index_url'] != summary['url']):
            extra += f" · [已有资料与总结]({group['index_url']})"
        lines.append(f"- **{group['title']}**：{group['count']} 个议题{extra}")
        for track in group['agenda_tracks']:
            lines.append(f"  - [{track['title']}]({data['source_url']}#{quote(track.get('official_anchor') or track['id'])})：{track['count']} 个议题")
    for group in enriched.get('overview_groups', []):
        lines += ['', f"## {group['title']}", '', f"{group['count']} 个开场致辞等大会背景议题纳入大会总结的导读，不单独生成专题总结。", '']
        for track in group['agenda_tracks']:
            lines.append(f"- {track['title']}：{track['count']} 个议题")
    atomic.write_text(str(md_path), '\n'.join(lines) + '\n')
    return {'url': url, 'path': str(html_path), 'total': enriched['total'], 'local_count': enriched['local_count'], 'tracks': len(data['recorded_tracks'])}
