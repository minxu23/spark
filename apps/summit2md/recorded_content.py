"""Import OCP's public recording directory without downloading individual videos."""
from __future__ import annotations

import re
import copy
import hashlib
import json
from urllib.parse import parse_qs, quote, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from core import sources
from . import session_types


def supports(url: str) -> bool:
    p = urlsplit(url)
    return (p.scheme in ("http", "https") and p.hostname in ("opencompute.org", "www.opencompute.org")
            and p.path.rstrip('/').endswith('/recorded-content') and p.path.startswith('/events/'))


def can_discover(url):
    """Conservative routing; arbitrary article/feed URLs retain their existing import paths."""
    p = urlsplit(url)
    from . import timeline_content
    return supports(url) or timeline_content.supports(url) or (p.scheme in ('http','https') and
                             p.path.rstrip('/').split('/')[-1] in ('recordings','recorded-content','video-library'))


def normalize_directory(data):
    """Adapter defaults are separate from the reusable grouping engine and user overrides."""
    data = copy.deepcopy(data)
    overrides = data.get('group_overrides') or {}
    for track in data.get('recorded_tracks', []):
        if 'overview_track_ids' in data:
            track.pop('summary_role', None)
        if track['id'] in data.get('overview_track_ids', []):
            track['summary_role'] = 'overview'
        if track.pop('_group_override', False):
            track.pop('summary_group', None)
        for entry in track.get('entries', []):
            entry['session_type'] = session_types.classify(entry)
        name = track['title']
        if supports(data.get('source_url') or '') and not track.get('summary_group'):
            if name == 'Cooling Environments':
                track['summary_group'] = 'Cooling'
            elif name == 'AI Open DC: Power Distribution':
                track['summary_group'] = 'AI Open Data Center'
        if track['id'] in overrides:
            track['summary_group'] = overrides[track['id']]
            track['_group_override'] = True
    entry_overrides = data.get('entry_overrides') or {}
    for track in data.get('recorded_tracks', []):
        for entry in track.get('entries', []):
            entry.pop('summary_group', None)
            if entry['id'] in entry_overrides:
                entry['summary_group'] = entry_overrides[entry['id']]
    if data.get('recorded_tracks'):
        from .recording_groups import groups
        catalog = groups(data['recorded_tracks'])
        by_track = {t['id']:g for g in catalog for t in g['agenda_tracks']}
        by_title = {g['title']:g for g in catalog}
        for track in data['recorded_tracks']:
            for entry in track['entries']:
                # Each listing belongs to its agenda track's group, unless the talk itself was reassigned.
                group = by_title[entry['summary_group']] if entry.get('summary_group') else by_track[track['id']]
                entry['agenda_track_id'] = track['id']
                entry['collection'] = {'conference_title':data['summit_title'], 'group_title':group['title'],
                                       'source_url':data['source_url'], 'group_folder':group['folder']}
    return data


def video_id(url: str) -> str | None:
    p = urlsplit(url)
    host = (p.hostname or '').lower()
    if host in ('youtu.be', 'www.youtu.be'):
        candidate = p.path.strip('/').split('/')[0]
    elif host in ('youtube.com', 'www.youtube.com', 'm.youtube.com'):
        candidate = (parse_qs(p.query).get('v') or [''])[0]
        if p.path.startswith(('/embed/', '/shorts/', '/live/')):
            candidate = p.path.strip('/').split('/')[1]
    else:
        return None
    return candidate if re.fullmatch(r'[A-Za-z0-9_-]{11}', candidate) else None


def select_directory(directory, url):
    """Persist a merged group's official track identities, rather than broadening to all talks."""
    params = parse_qs(urlsplit(url).fragment)
    single = (params.get('spark-track') or [''])[0]
    multiple = (params.get('spark-tracks') or [''])[0]
    if not single and not multiple:
        return directory
    tracks = directory['recorded_tracks']
    if multiple:
        try:
            ids = json.loads(multiple)
        except ValueError as e:
            raise RuntimeError('保存的议程分组格式无效') from e
        if not isinstance(ids,list) or not ids or not all(isinstance(i,str) for i in ids):
            raise RuntimeError('保存的议程分组格式无效')
        chosen = [t for t in tracks if t['id'] in ids]
        if {t['id'] for t in chosen} != set(ids):
            raise RuntimeError('官网中已找不到之前选择的部分议程，请重新获取目录')
    else:
        chosen = tracks if single == 'all' else [t for t in tracks if t['id'] == single]
    if not chosen:
        raise RuntimeError('官网中已找不到之前选择的专题，请重新获取目录')
    entries = {e['id']:copy.deepcopy(e) for t in chosen for e in t['entries']}
    label = (params.get('spark-label') or [''])[0]
    label = label or ('全部专题' if single == 'all' else chosen[0]['entries'][0]['collection']['group_title'])
    if multiple:
        from .pipeline import sanitize_filename
        for entry in entries.values():
            entry['collection'] = {'conference_title':directory['summit_title'], 'group_title':label,
                                   'group_folder':sanitize_filename(label,100), 'source_url':directory['source_url']}
    return {'summit_title':directory['summit_title']+' - '+label,
            'source_url':directory['source_url']+'#'+urlsplit(url).fragment, 'content_type':'summit',
            'entries':[dict(e,index=i+1) for i,e in enumerate(entries.values())]}


def parse_directory(raw: bytes, url: str) -> dict:
    soup = BeautifulSoup(raw, 'lxml')
    parts = urlsplit(url)
    from .recording_guide import source_url
    base = source_url(url)
    from . import timeline_content
    timeline = timeline_content.parse(soup, base, video_id)
    if timeline:
        return select_directory(normalize_directory(timeline), url)
    if supports(url):
        event = parts.path.rstrip('/').split('/')[-2]
        title = event.replace('-', ' ').title().replace('Ocp', 'OCP')
    else:
        heading = soup.find('h1')
        if heading is None or heading.get_text(' ', strip=True).lower() in ('recordings','recorded content','videos'):
            heading = soup.find('title') or heading
        title = heading.get_text(' ', strip=True) if heading else (parts.hostname or 'Summit')
        title = re.sub(r'\s*[|–—]\s*.*$', '', title).strip()
    tracks = []
    # Only session tables under section headings count. The directory's navigation,
    # trailers, playlist links and slide downloads are not individual sessions.
    for heading in soup.select('h2, h3'):
        table = None
        for node in heading.next_elements:
            if node is heading or not getattr(node, 'name', None):
                continue
            if node.name in ('h2', 'h3'):
                break
            if node.name == 'table':
                table = node
                break
        if table is None:
            continue
        header = [c.get_text(' ', strip=True).lower() for c in table.select('tr:first-child th, tr:first-child td')]
        aliases = {'session':'title', 'talk':'title', 'presentation':'title', '议题':'title', '标题':'title',
                   'recording':'video', 'watch':'video', '视频':'video', '录像':'video', '演讲者':'speakers', '资料':'slides'}
        header = [aliases.get(h, h) for h in header]
        if not header or header[0] != 'title' or 'video' not in header:
            continue
        name = re.sub(r'\s*\((?:date/time|alphabetical) order\)\s*$', '', heading.get_text(' ', strip=True), flags=re.I)
        anchor = heading.get('id') or next((a.get('id') for a in heading.select('[id]')), None)
        tid = anchor or 'track-' + hashlib.sha256(name.encode()).hexdigest()[:12]
        entries, seen = [], set()
        for row in table.select('tr'):
            cells = row.find_all('td', recursive=False)
            if len(cells) <= header.index('video'):
                continue
            session = cells[0].get_text(' ', strip=True)
            if not session:
                continue
            for link in cells[header.index('video')].select('a[href]'):
                vid = video_id(link['href'])
                if not vid or vid in seen:
                    continue
                seen.add(vid)
                speaker_column = next((i for i, label in enumerate(header) if label.startswith('speaker')), None)
                speakers = cells[speaker_column].get_text(' ', strip=True) if speaker_column is not None and speaker_column < len(cells) else ''
                slides = []
                if 'slides' in header and header.index('slides') < len(cells):
                    for slide in cells[header.index('slides')].select('a[href]'):
                        href = urljoin(base, slide['href'])
                        if urlsplit(href).scheme in ('http', 'https'):
                            slides.append({'title': slide.get_text(' ', strip=True) or 'Slides', 'url': href})
                entries.append({'speakers': speakers, 'slides': slides, 'id': vid, 'title': session, 'url': f'https://www.youtube.com/watch?v={vid}',
                                'duration': 0, 'index': len(entries) + 1, 'is_raw_session': False})
        if entries:
            if any(t['id'] == tid for t in tracks):
                raise RuntimeError('官网中存在无法区分的同名议程，请为该来源配置独立的议程标识')
            tracks.append({'id': tid, 'title': name, 'entries': entries, 'count': len(entries)})
    if not tracks:
        raise RuntimeError('官网录像目录中没有找到可导入的视频表格，页面结构可能已变化')
    directory = normalize_directory({'summit_title':title, 'source_url':base, 'recorded_tracks':tracks,
                                     'entries':[], 'content_type':'summit'})
    return select_directory(directory, url)


def discover(url: str) -> dict:
    return parse_directory(sources._http_get(url), url)
