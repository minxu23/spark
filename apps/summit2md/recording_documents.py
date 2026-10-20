"""A single topic document holds both synthesis and links to source material."""
import os
import re
from pathlib import Path
from urllib.parse import quote

MARKER = '<!-- spark-recording-guide -->'
SUMMARY_MARKER = '<!-- spark-topic-summary -->'
MATERIALS_MARKER = '<!-- spark-topic-materials -->'
LEGACY_TOOL_MARKER = '> 本目录由 summit2md 工具自动生成于 '
# The conference's same-name md holds the conference summary followed by the topic directory,
# the same way each topic's same-name md holds its summary followed by the source list.
CONF_SUMMARY_MARKER = '<!-- spark-conference-summary -->'
CONF_DIRECTORY_MARKER = '<!-- spark-conference-directory -->'
LEGACY_CONF_SUMMARY = '大会总结.md'
LEGACY_CONF_DIRECTORY = '大会目录.md'
SUMMARY_ANCHOR = '#大会总结'


def demote(markdown):
    """Body headings sit under '## 大会总结', so each level moves down one (## → ###)."""
    return re.sub(r'^(#{1,5}) ', lambda m: '#' + m[1] + ' ', markdown, flags=re.M)


def conference_summary(root, document_name):
    """The conference summary section, from the same-name md or (once) from a legacy 大会总结.md."""
    document = Path(root)/document_name
    if document.is_file():
        text = document.read_text()
        if MARKER in text and CONF_SUMMARY_MARKER in text:
            section = text.split(CONF_SUMMARY_MARKER,1)[1].split(CONF_DIRECTORY_MARKER,1)[0]
            return (CONF_SUMMARY_MARKER + section).rstrip()
    legacy = Path(root)/LEGACY_CONF_SUMMARY
    if legacy.is_file() and MARKER in legacy.read_text():
        lines = legacy.read_text().replace(MARKER,'',1).strip().splitlines()
        if lines and lines[0].startswith('# '):
            lines = lines[1:]
        body = '\n'.join(l for l in lines if not l.startswith('[返回大会目录]')).strip()
        return CONF_SUMMARY_MARKER + '\n## 大会总结\n\n' + demote(body)
    return ''


def filename(group):
    from .pipeline import sanitize_filename
    folder = group.get('folder') or sanitize_filename(group['title'],100) or '未命名专题'
    return str(Path(folder)/(folder+'.md'))


def summary_part(text):
    return text.split(MATERIALS_MARKER,1)[0].rstrip()


def read_summary(folder):
    legacy = folder/'专题总结.md'
    if legacy.is_file() and MARKER in legacy.read_text():
        return summary_part(legacy.read_text())
    document = folder/(folder.name+'.md')
    if document.is_file() and document.read_text().startswith(MARKER) and SUMMARY_MARKER in document.read_text():
        return summary_part(document.read_text())
    if document.is_file() and LEGACY_TOOL_MARKER in document.read_text():
        # Older playlist processing put its summary and table in the named index.
        text = document.read_text().split('\n## 议题列表',1)[0].rstrip()
        if SUMMARY_MARKER in text:
            # The playlist renderer may have embedded the existing unified summary.
            return summary_part(text[text.index(MARKER):])
        if '\n## 大会总结' not in text:
            return ''  # Only a title and the tool stamp: nothing worth carrying into the topic document.
        return MARKER+'\n'+SUMMARY_MARKER+'\n'+text
    return ''


def render(root, folder, data, child):
    from .recording_guide import index_filename
    group, manifest = child['group'], child['manifest']
    summary = read_summary(folder)
    if summary:
        if SUMMARY_MARKER not in summary:
            summary = summary.replace(MARKER,MARKER+'\n'+SUMMARY_MARKER,1)
        lines = [summary, '']
    else:
        conference = index_filename(root)
        lines = [MARKER, '# '+data['summit_title']+' - '+group['title'], '',
                 f"共 {len(manifest['entries'])} 个已归档议题。", '',
                 '[返回大会目录](../'+quote(conference)+')', '']
        if group.get('summary_role') == 'overview' and conference_summary(root, conference):
            lines += ['[阅读大会总结中的导读](../'+quote(conference)+SUMMARY_ANCHOR+')', '']
    lines += [MATERIALS_MARKER, '## 整理稿与文字记录', '',
              '| 原序号 | 议题 | 整理稿 | 英文稿 | 文字记录 |', '| --- | --- | --- | --- | --- |']
    for row in sorted(manifest['entries'].values(),key=lambda r:r.get('rank',0)):
        links = [f"[阅读]({quote(row[key])})" if row.get(key) else '—'
                 for key in ('speech_relative_path','speech_en_relative_path','relative_path')]
        title = row['entry']['title'].replace('|','\\|')
        lines.append(f"| {row.get('rank','')} | {title} | "+' | '.join(links)+' |')
    # Videos also listed under another agenda track: one copy, kept in the owner's folder.
    for item in sorted(child.get('shared') or [], key=lambda r:r.get('rank',0)):
        links = [f"[阅读]({quote(os.path.relpath(root/item['paths'][key], folder))})" if item['paths'].get(key) else '—'
                 for key in ('speech_relative_path','speech_en_relative_path','relative_path')]
        title = item['entry']['title'].replace('|','\\|') + f"（资料存放在「{item['owner']}」）"
        lines.append(f"| {item.get('rank','')} | {title} | "+' | '.join(links)+' |')
    return '\n'.join(lines)+'\n'
