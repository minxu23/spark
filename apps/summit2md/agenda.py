"""公开 FN Virtual 议程：读取打包 JSON，按现有视频标题保守匹配，不执行网页脚本。"""
from __future__ import annotations

import ast
import difflib
import html
import json
import re
import unicodedata
from datetime import datetime
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_JSON_LITERAL = re.compile(r"JSON\.parse\(('(?:\\.|[^'\\])*')\)")


def bundled_schedule(script):
    events, timezone = [], 'UTC'
    for match in _JSON_LITERAL.finditer(script):
        try:
            data = json.loads(ast.literal_eval(match.group(1)))
        except (ValueError, SyntaxError):
            continue
        if isinstance(data, dict) and data.get('time_zone_id'):
            timezone = str(data['time_zone_id'])
        if isinstance(data, list) and data and isinstance(data[0], dict):
            events.extend(e for e in data if isinstance(e, dict) and e.get('title')
                          and isinstance(e.get('start_date'), (int, float)) and isinstance(e.get('track'), dict))
    return events, timezone


def title_key(title):
    text = unicodedata.normalize('NFKC', html.unescape(str(title))).lower()
    text = re.sub(r'^\s*\d{4,6}\s+', '', text)  # 视频标题可能以官网的议题编号开头
    text = text.replace('&', ' and ')
    return ''.join(re.findall(r'[^\W_]+', text, re.UNICODE))


def title_score(left, right):
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    # YouTube 标题经常被截短；仅接受足够长、完整的前缀。
    if min(len(left), len(right)) >= 45 and (left.startswith(right) or right.startswith(left)):
        return .98
    return difflib.SequenceMatcher(None, left, right).ratio()


def match_events(events, entries, timezone):
    try:
        zone = ZoneInfo(timezone)
    except ZoneInfoNotFoundError:
        raise ValueError(f'议程时区无法识别：{timezone}')
    ordered = sorted(events, key=lambda e: (e['start_date'], e['track'].get('name', ''), str(e.get('id', ''))))
    # 同一打包数据可能重复出现，按官方议题 ID 去重。
    unique = {}
    for event in ordered:
        unique.setdefault(str(event.get('id') or (event['title'], event['start_date'])), event)
    candidates = list(unique.values())
    matched, used = {}, set()
    for entry in entries:
        key = title_key(entry.get('title', ''))
        ranked = sorted(((title_score(key, title_key(event['title'])), i)
                         for i, event in enumerate(candidates)), reverse=True)
        if not ranked or ranked[0][0] < .90:
            continue
        score, i = ranked[0]
        # 标题近似的不同场次不能靠列表先后强行选一个。
        if len(ranked) > 1 and score - ranked[1][0] < .06:
            continue
        if i in used:
            continue
        used.add(i)
        event = candidates[i]
        date = datetime.fromtimestamp(event['start_date'], zone)
        speakers = event.get('speakers') or []
        names = [' '.join(str(s.get(k) or '') for k in ('first_name', 'last_name')).strip()
                 for s in speakers if isinstance(s, dict)]
        matched[entry['id']] = dict(order=i, day=date.strftime('%Y-%m-%d'), time=date.strftime('%H:%M'),
                                    track=event['track'].get('name', ''), agenda_title=event['title'],
                                    agenda_speaker='、'.join(n for n in names if n),
                                    match_method='title', match_score=round(score, 3))
    return {'matched': matched, 'schedule_count': len(candidates), 'timezone': timezone,
            'source': 'fnvirtual', 'unmatched': [e.get('title', '') for e in entries if e['id'] not in matched]}


def fetch_fnvirtual_agenda(soup, agenda_url, entries, http_get):
    host = urlsplit(agenda_url).hostname or ''
    if not host.endswith('.fnvirtual.app'):
        return None
    script_url = None
    for script in soup.select('script[src]'):
        url = urljoin(agenda_url, script['src'])
        parts = urlsplit(url)
        if parts.netloc == urlsplit(agenda_url).netloc and re.fullmatch(r'/app-[a-f0-9]+\.js', parts.path):
            script_url = url
            break
    if not script_url:
        return None
    script = http_get(script_url).decode('utf-8')
    events, timezone = bundled_schedule(script)
    if not events:
        raise ValueError('未能从官方公开数据中读取议程，保留原顺序')
    return match_events(events, entries, timezone)
