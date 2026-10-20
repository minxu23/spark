"""Plan and apply reversible physical filing using the saved conference grouping."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import uuid
from pathlib import Path
from urllib.parse import quote, unquote, urlencode, urlsplit, urlunsplit, parse_qsl

from core import atomic
from . import recording_guide as guide, recording_groups, recording_documents as documents

PATH_FIELDS = ('relative_path', 'speech_relative_path', 'speech_en_relative_path', 'note_relative_path')


def _manifest(path):
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
        if not isinstance(data,dict) or not isinstance(data.get('entries'),dict):
            raise ValueError()
        return data
    except (ValueError,UnicodeError) as e:
        raise ValueError(f'处理记录损坏，未归档或覆盖：{path}') from e


def plan(data, output_dir):
    data = guide.recorded_content.normalize_directory(data)
    root = guide.guide_directory(output_dir, data['source_url'], data['summit_title'])
    if root.is_symlink():
        raise ValueError('大会目录是符号链接，未归档')
    original = _manifest(root / '.manifest.json')
    aggregate = copy.deepcopy(original)
    aggregate.setdefault('entries', {})
    old_children = {}
    legacy_children = set()
    catalog_ids = {e['id'] for t in data['recorded_tracks'] for e in t['entries']}
    for path in sorted(root.glob('*/.manifest.json')):
        if path.parent.name.startswith('.') or path.parent.name == '旧版产物':
            continue
        saved = _manifest(path)
        context = saved.get('collection') or {}
        index = path.parent/(path.parent.name+'.md')
        pipeline_index = (bool(saved.get('entries')) and set(saved['entries']).issubset(catalog_ids)
                          and index.is_file() and documents.LEGACY_TOOL_MARKER in index.read_text()
                          and index.read_text().startswith('# '+data['summit_title']+' - '))
        if context.get('conference_title') != data['summit_title']:
            if context or not pipeline_index:
                continue
        if pipeline_index:
            legacy_children.add(path.parent.name)
        old_children[path.parent.name] = saved
        for eid, source_row in saved.get('entries', {}).items():
            row = copy.deepcopy(source_row)
            for key in PATH_FIELDS:
                if row.get(key):
                    resolved = (path.parent / row[key]).resolve()
                    if not resolved.is_relative_to(root.resolve()):
                        raise ValueError('议题文件指向大会目录之外，未移动')
                    row[key] = str(resolved.relative_to(root.resolve()))
            aggregate['entries'][eid] = row
    assigned, children, moves, redirects = {}, {}, {}, {}
    destinations = {}
    for group in recording_groups.groups(data['recorded_tracks']):
        folder = root / group['folder']
        if not folder.resolve().is_relative_to(root.resolve()):
            raise ValueError('专题目录指向大会目录之外，未整理')
        context = {'conference_title':data['summit_title'], 'group_title':group['title'],
                   'source_url':data['source_url'], 'group_folder':group['folder']}
        source = data['source_url'] + '#' + urlencode({
            'spark-tracks':json.dumps([t['id'] for t in group['agenda_tracks']]),
            'spark-label':group['title']})
        child = copy.deepcopy(_manifest(folder / '.manifest.json'))
        existing_context = child.get('collection') or {}
        if child and folder.name not in legacy_children and (existing_context.get('conference_title') != data['summit_title']
                      or existing_context.get('source_url') not in (None,data['source_url'])):
            raise ValueError(f'分组目录已有其他来源的处理记录，未覆盖：{folder}')
        rows = {}
        child['entries'] = rows
        for entry in group['entries']:
            eid = entry['id']
            if eid in assigned:
                continue  # Official cross-listings share one physical copy, owned by the first group.
            assigned[eid] = group['id']
            row = aggregate['entries'].get(eid)
            if row is None:
                continue
            child_row = copy.deepcopy(row)
            for key in PATH_FIELDS:
                relative = row.get(key)
                if not relative:
                    continue
                src = (root / relative).resolve()
                if not src.is_relative_to(root.resolve()) or not src.is_file():
                    raise ValueError(f'议题文件不存在或越出大会目录：{relative}')
                kind = {'relative_path':'transcripts', 'speech_relative_path':'speech',
                        'speech_en_relative_path':'speech', 'note_relative_path':'notes'}[key]
                dest = folder / kind / src.name
                claimed = destinations.get(str(dest).casefold())
                if claimed and claimed != src:
                    raise ValueError(f'归档后文件重名，未移动：{dest.name}')
                destinations[str(dest).casefold()] = src
                if dest.exists() and dest.resolve() != src:
                    raise ValueError(f'目标已有同名文件，未覆盖：{dest}')
                if src != dest.resolve():
                    if src in moves and moves[src] != dest:
                        raise ValueError(f'同一原稿被多个议题指向不同目录，未移动：{src.name}')
                    moves[src] = dest
                child_row[key] = str(dest.relative_to(folder))
                row[key] = str(dest.relative_to(root))
            child_row['entry'] = {**entry, 'collection':context}
            row['entry'] = copy.deepcopy(child_row['entry'])
            rows[eid] = child_row
        index = folder/(folder.name+'.md')
        if index.exists() and guide.MARKER not in index.read_text() and folder.name not in legacy_children:
            raise ValueError(f'已有自建索引，未覆盖：{index}')
        child.update(content_type='summit', source_url=source, collection=context)
        legacy = folder/'专题总结.md'
        if legacy.is_file() and legacy != index:
            if guide.MARKER not in legacy.read_text():
                raise ValueError(f'已有自建专题文件，未合并：{legacy}')
            redirects[legacy.resolve()] = index
        summary = documents.read_summary(folder)
        child['overall_summary'] = (summary if summary else
                                    '[阅读大会导读](../大会总结.md)' if group.get('summary_role') == 'overview'
                                    and (root/'大会总结.md').is_file() else '')
        child['topic_groups'] = {group['title']:list(rows)} if group.get('summary_role') != 'overview' else {}
        children[group['folder']] = {'manifest':child, 'group':group}
    unknown = set(aggregate['entries']) - set(assigned)
    if unknown:
        raise ValueError(f'还有 {len(unknown)} 个已处理议题未归类，未移动任何文件')
    retired = {name:saved for name,saved in old_children.items() if name not in children}
    aggregate['content_type'] = 'summit'
    aggregate['source_url'] = data['source_url'] + '#spark-track=all'
    aggregate['topic_groups'] = {c['group']['title']:list(c['manifest']['entries'])
                                for c in children.values() if c['group'].get('summary_role') != 'overview'}
    if (root/'大会总结.md').is_file():
        aggregate['overall_summary'] = (root/'大会总结.md').read_text()
    return {'root':root, 'data':data, 'aggregate':aggregate, 'children':children, 'moves':moves,
            'retired':retired, 'redirects':redirects, 'legacy_indexes':legacy_children}


def rewrite_links(text, before, after, moves, vault_root=None):
    """Rewrite only link targets; keep source prose and original numbering intact."""
    before, after = Path(before), Path(after)
    def target(href):
        parsed = urlsplit(href)
        if (parsed.scheme == 'obsidian' and parsed.netloc == 'open') or (parsed.hostname in ('127.0.0.1','localhost') and parsed.path == '/read/open'):
            params=[];changed=False
            for key,value in parse_qsl(parsed.query,keep_blank_values=True):
                source = Path(value).resolve() if key=='path' else (Path(vault_root)/value).resolve() if key=='file' and vault_root else None
                suffix = bool(source and not source.suffix)
                if source not in moves and suffix:
                    source=Path(str(source)+'.md')
                if source in moves:
                    dest=moves[source]
                    value=str(dest.relative_to(vault_root)) if key=='file' else str(dest)
                    if suffix:
                        value=value.removesuffix('.md')
                    changed=True
                params.append((key,value))
            if changed:
                return urlunsplit((parsed.scheme,parsed.netloc,parsed.path,urlencode(params),parsed.fragment))
        if parsed.scheme or not parsed.path:
            return href
        src = (before.parent / unquote(parsed.path)).resolve()
        dest = moves.get(src, src)
        if dest == src and before.parent == after.parent:
            return href
        relative = os.path.relpath(dest, after.parent)
        if relative == unquote(parsed.path):
            return href
        result = quote(relative)
        return result + ('#' + parsed.fragment if parsed.fragment else '')
    # Older generated indexes used literal spaces and parentheses in filenames.
    # Walk balanced parentheses so those destinations are repaired as a whole.
    chunks, cursor = [], 0
    for match in re.finditer(r'\[[^\]\n]*\]\(', text):
        if match.start() < cursor:
            continue
        start = end = match.end()
        depth, angled = 1, text[start:start+1] == '<'
        while end < len(text) and text[end] != '\n':
            char = text[end]
            if char == '\\':
                end += 2
                continue
            if angled:
                angled = char != '>'
            elif char == '(':
                depth += 1
            elif char == ')':
                depth -= 1
                if depth == 0:
                    break
            end += 1
        if depth:
            continue
        href = text[start:end]
        title = re.search(r'\s+([\"\']).*\1$', href)
        suffix = href[title.start():] if title else ''
        href = href[:title.start()] if title else href
        rewritten = '<'+target(href[1:-1])+'>' if href.startswith('<') and href.endswith('>') else target(href)
        chunks += [text[cursor:start], rewritten, suffix, ')']
        cursor = end + 1
    text = ''.join(chunks) + text[cursor:]
    def wiki(match):
        address, sep, alias = match[2].partition('|')
        address, hashmark, anchor = address.partition('#')
        candidates = [(before.parent / address).resolve()]
        if vault_root:
            candidates.append((Path(vault_root)/address).resolve())
        for src in candidates:
            if src not in moves and src.suffix != '.md':
                src = Path(str(src)+'.md')
            if src not in moves:
                continue
            dest = moves[src]
            new = str(dest.relative_to(vault_root)) if vault_root and dest.is_relative_to(vault_root) else os.path.relpath(dest,after.parent)
            if not address.endswith('.md'):
                new = new.removesuffix('.md')
            return match[1]+new+(hashmark+anchor if hashmark else '')+(sep+alias if sep else '')+match[3]
        return match[0]
    return re.sub(r'(!?\[\[)([^\]]+)(\]\])',wiki,text)


def group_index(root, folder, data, child):
    return documents.render(root,folder,data,child)


def _all_moves(plan):
    root = plan['root']
    moves = dict(plan['moves'])
    history = root/'旧版产物'
    named = root/(root.name+'.md')
    if named.exists() and guide.MARKER not in named.read_text():
        moves[named.resolve()] = history/named.name
    old_topics = root/'topics'
    if old_topics.exists():
        for path in old_topics.rglob('*'):
            if path.is_file():
                moves[path.resolve()] = history/'topics'/path.relative_to(old_topics)
    for name in plan.get('retired', {}):
        folder = root/name
        for path in (folder/'.manifest.json',folder/(name+'.md'),folder/'专题总结.md'):
            if path.is_file():
                if (path.suffix == '.md' and guide.MARKER not in path.read_text()
                        and not (name in plan.get('legacy_indexes',set()) and documents.LEGACY_TOOL_MARKER in path.read_text())):
                    continue
                moves[path.resolve()] = history/name/path.name
    for source,dest in moves.items():
        if not source.resolve().is_relative_to(root.resolve()) or not dest.resolve().is_relative_to(root.resolve()):
            raise ValueError('归档路径越出大会目录，未移动')
        if dest.exists():
            raise ValueError(f'目标已有文件，未覆盖：{dest}')
    return moves


def _write_metadata(plan):
    root = plan['root']
    for folder_name,child in plan['children'].items():
        folder = root/folder_name;folder.mkdir(exist_ok=True)
        index = folder/(folder.name+'.md')
        atomic.write_text(str(index),group_index(root,folder,plan['data'],child))
        if documents.SUMMARY_MARKER in index.read_text():
            child['manifest']['overall_summary'] = documents.summary_part(index.read_text())
        atomic.write_json(str(folder/'.manifest.json'),child['manifest'],indent=2)
    if (root/'大会总结.md').is_file():
        plan['aggregate']['overall_summary'] = (root/'大会总结.md').read_text()
    atomic.write_json(str(root/'.manifest.json'),plan['aggregate'],indent=2)
    state = guide._json(root/'.recording-summaries.json')
    changed = False
    for record in state.get('tracks',{}).values():
        before = (root/record.get('path','')).resolve()
        if before in plan.get('redirects',{}):
            record['path'] = str(plan['redirects'][before].relative_to(root))
            changed = True
    if changed:
        atomic.write_json(str(root/'.recording-summaries.json'),state,indent=2)


def apply(plan, backup_dir, *, move_batch=None, vault_root=None):
    """Restore the original tree on failure; keep the failed attempt for recovery."""
    backup = Path(backup_dir)
    if backup.resolve().is_relative_to(plan['root'].resolve()):
        raise ValueError('备份目录必须位于大会目录之外')
    if backup.exists():
        raise ValueError('备份目录已存在，请使用新的备份目录')
    _all_moves(plan)  # All collisions must fail before creating a backup or moving anything.
    try:
        return _apply(plan,backup,move_batch=move_batch,vault_root=vault_root)
    except Exception:
        if backup.is_dir() and (backup/'归档移动清单.json').is_file():
            failed = backup/'归档失败现场'
            shutil.move(str(plan['root']),str(failed))
            shutil.copytree(backup,plan['root'],ignore=shutil.ignore_patterns(
                '归档失败现场','归档移动清单.json','归档后剩余目录-*','已合并专题原件'))
        raise


def _apply(plan, backup_dir, *, move_batch=None, vault_root=None):
    """No deletion; a caller may supply Obsidian's link-aware batch mover."""
    root = plan['root']; backup = Path(backup_dir)
    if backup.exists():
        raise ValueError('备份目录已存在，请使用新的备份目录')
    moves = _all_moves(plan)
    link_moves = {**moves,**plan.get('redirects',{})}
    shutil.copytree(root, backup)
    atomic.write_json(str(backup/'归档移动清单.json'),[[str(a),str(b)] for a,b in moves.items()],indent=2)
    for dest in moves.values():
        dest.parent.mkdir(parents=True,exist_ok=True)
    originals = {p: p.read_text() for p in root.rglob('*.md') if p.is_file()}
    hashes = {src:hashlib.sha256(src.read_bytes()).hexdigest() for src in plan['moves']}
    if move_batch:
        move_batch(list(moves.items()))
    else:
        for src,dest in moves.items():
            shutil.move(str(src),str(dest))
    for before,text in originals.items():
        after = moves.get(before.resolve(),before)
        if not after.is_file():
            raise ValueError(f'移动未完成：{after}')
        updated = rewrite_links(text,before,after,link_moves,vault_root)
        if after.read_text()!=updated:
            atomic.write_text(str(after),updated)
    _write_metadata(plan)
    for legacy in plan.get('redirects',{}):
        saved = backup/'已合并专题原件'/legacy.relative_to(root)
        saved.parent.mkdir(parents=True,exist_ok=True)
        shutil.move(str(legacy),str(saved))
    # Keep even empty directories and Finder metadata recoverable in the backup.
    for name in ('speech','transcripts','topics'):
        old = root/name
        if old.exists() and not any(p.is_file() and not p.name.startswith('.') for p in old.rglob('*')):
            shutil.move(str(old),str(backup/('归档后剩余目录-'+name)))
    for src,original_hash in hashes.items():
        dest = moves[src]
        original = backup/src.relative_to(root)
        expected = rewrite_links(original.read_text(),src,dest,link_moves,vault_root) if src.suffix=='.md' else None
        if expected is not None:
            assert dest.read_text()==expected,f'原稿正文意外变化：{dest}'
        else:
            assert hashlib.sha256(dest.read_bytes()).hexdigest()==original_hash
    return {'files':len(plan['moves']), 'groups':len(plan['children']), 'moves':moves, 'backup':str(backup),
            'merged_documents':len(plan.get('redirects',{}))}


def organize(*, data, output_dir, materials_roots=(), stop_flag=None):
    """Normal application workflow, including partial runs and already nested output."""
    prepared = plan(data, output_dir)
    if stop_flag and stop_flag():
        from .pipeline import Stopped
        raise Stopped()
    if not prepared['aggregate']['entries'] and not prepared.get('redirects'):
        guide.save(data, output_dir, materials_roots)
        return {'files':0, 'groups':0}
    from . import pending_tasks
    pending_tasks.regroup(prepared['root'],prepared['children'],dry_run=True)
    moves = _all_moves(prepared)
    if moves or prepared.get('redirects') or prepared.get('legacy_indexes'):
        backup = prepared['root'].parent/'.organization-backups'/('分组归档-'+uuid.uuid4().hex)
        result = apply(prepared, backup)
    else:
        _write_metadata(prepared)
        result = {'files':0, 'groups':len(prepared['children'])}
    pending_tasks.regroup(prepared['root'],prepared['children'])
    guide.save(data, output_dir, materials_roots)
    return {k:v for k,v in result.items() if k != 'moves'}
