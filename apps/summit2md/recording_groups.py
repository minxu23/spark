"""Keep official agenda tracks separate from the coarser storage/summary groups."""
import copy
import hashlib
import re
from pathlib import Path


def group_name(title):
    # Only a universal spelling alias lives here. Vendor-specific aliases belong to adapters.
    return 'Keynote' if title in ('Keynotes', 'Keynote') else title


def _parent(title):
    parts = re.split(r'[:：]', title, maxsplit=1)
    return parts[0].strip() if len(parts) == 2 else ''


def groups(tracks):
    result = {}
    parents = {}
    titles = {t['title'].strip() for t in tracks}
    for t in tracks:
        parent = _parent(t['title'])
        if parent:
            parents[parent] = parents.get(parent, 0) + 1
    for track in tracks:
        parent = _parent(track['title'])
        automatic = parent if parent and (parents.get(parent, 0) > 1 or parent in titles) else track['title']
        name = track.get('summary_group') or group_name(automatic)
        gid = 'group-' + hashlib.sha256(name.encode()).hexdigest()[:12]
        group = result.setdefault(gid, {'id':gid, 'title':name, 'entries':[], 'agenda_tracks':[]})
        group['agenda_tracks'].append({'id':track['id'], 'title':track['title'], 'count':track['count'],
                                      **({'official_anchor':track['official_anchor']} if track.get('official_anchor') else {})})
        known = {e['id'] for e in group['entries']}
        for entry in track['entries']:
            if entry['id'] not in known:
                group['entries'].append(copy.deepcopy(entry))
                known.add(entry['id'])
    for group in result.values():
        group['count'] = len(group['entries'])
        ids = {t['id'] for t in group['agenda_tracks']}
        if all(t.get('summary_role') == 'overview' for t in tracks if t['id'] in ids):
            group['summary_role'] = 'overview'
    from .pipeline import sanitize_filename
    folders = [sanitize_filename(g['title'], 100).casefold() for g in result.values()]
    for group in result.values():
        folder = sanitize_filename(group['title'], 100)
        group['folder'] = folder + ('_' + group['id'][-8:] if folders.count(folder.casefold()) > 1 else '')
    return list(result.values())


def resolve_group(tracks, selected):
    return next((g for g in groups(tracks) if g['id'] == selected or any(t['id'] == selected for t in g['agenda_tracks'])), None)


def validate_collection(value):
    if not isinstance(value, dict):
        return None
    result = {}
    for key in ('conference_title', 'group_title'):
        text = value.get(key)
        if not isinstance(text, str) or not text.strip() or len(text) > 200:
            raise ValueError('大会或分组名称无效')
        text = text.strip()
        if text in ('.', '..') or '\x00' in text:
            raise ValueError('大会或分组名称无效')
        result[key] = text
    if value.get('source_url'):
        from .recording_guide import source_url
        result['source_url'] = source_url(value['source_url'])
    if value.get('group_folder'):
        folder = value['group_folder']
        if not isinstance(folder,str) or folder in ('.','..') or any(c in folder for c in '/\\\x00'):
            raise ValueError('分组目录无效')
        result['group_folder'] = folder
    return result


def collection_for(base, title, entries=(), explicit=None):
    if explicit:
        return validate_collection(explicit)
    embedded = [validate_collection(e['collection']) for e in entries if e.get('collection')]
    import json
    root = Path(base)
    candidates = [root / '.recordings.json'] + list(root.glob('*/.recordings.json'))
    for path in candidates:
        try:
            data = json.loads(path.read_text())
            from .recorded_content import normalize_directory
            data = normalize_directory(data)
            event = data['summit_title']
            catalog_groups = groups(data['recorded_tracks'])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        selected_ids = {e.get('agenda_track_id') for e in entries}
        if embedded and selected_ids and None not in selected_ids and all(v.get('source_url') == data['source_url'] for v in embedded):
            selected = [g for g in catalog_groups if any(t['id'] in selected_ids for t in g['agenda_tracks'])]
            if len(selected) > 1:
                raise ValueError('所选议题属于不同资料分组，请按分组分别整理')
            if selected:
                group = selected[0]
                return validate_collection({'conference_title':event, 'group_title':group['title'],
                                            'source_url':data['source_url'], 'group_folder':group['folder']})
        if embedded and any(v.get('source_url') != data['source_url'] for v in embedded):
            continue
        for group in catalog_groups:
            labels = [group['title']] + [t['title'] for t in group['agenda_tracks']]
            if any(title == event + ' - ' + label for label in labels):
                return validate_collection({'conference_title':event, 'group_title':group['title'],
                                            'source_url':data['source_url'], 'group_folder':group['folder']})
    if embedded:
        if any(v != embedded[0] for v in embedded):
            raise ValueError('所选议题属于不同资料分组，请按分组分别整理')
        return embedded[0]
    return None


def canonical_title(title, collection=None):
    return (collection['conference_title'] + ' - ' + collection['group_title']) if collection else title


def output_path(base, title, sanitize, collection=None):
    collection = validate_collection(collection) if collection else collection_for(base, title)
    if not collection:
        return str(Path(base) / sanitize(title))
    event, group = collection['conference_title'], collection['group_title']
    root = Path(base)
    if (root / '.recordings.json').is_file():
        import json
        try:
            saved = json.loads((root / '.recordings.json').read_text())
        except (OSError,ValueError):
            saved = {}
        if saved.get('summit_title') == event and (not collection.get('source_url') or saved.get('source_url') == collection['source_url']):
            return str(root / (collection.get('group_folder') or sanitize(group)))
    if collection.get('source_url'):
        from .recording_guide import guide_directory
        parent = guide_directory(base, collection['source_url'], event)
    else:
        parent = root if root.name == sanitize(event) else root / sanitize(event)
    return str(parent / (collection.get('group_folder') or sanitize(group)))


def manifest_paths(root):
    root = Path(root)
    for pattern in ('*/.manifest.json', '*/*/.manifest.json'):
        for path in root.glob(pattern):
            if not any(p.startswith('.') for p in path.parent.relative_to(root).parts) and not path.parent.is_symlink():
                yield path
