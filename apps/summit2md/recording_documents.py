"""A single topic document holds both synthesis and links to source material."""
from pathlib import Path
from urllib.parse import quote

MARKER = '<!-- spark-recording-guide -->'
SUMMARY_MARKER = '<!-- spark-topic-summary -->'
MATERIALS_MARKER = '<!-- spark-topic-materials -->'
LEGACY_TOOL_MARKER = '> 本目录由 summit2md 工具自动生成于 '


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
        lines = [MARKER, '# '+data['summit_title']+' - '+group['title'], '',
                 f"共 {len(manifest['entries'])} 个已归档议题。", '',
                 '[返回大会目录](../'+quote(index_filename(root))+')', '']
        if group.get('summary_role') == 'overview' and (root/'大会总结.md').is_file():
            lines += ['[阅读大会导读](../大会总结.md)', '']
    lines += [MATERIALS_MARKER, '## 整理稿与文字记录', '',
              '| 原序号 | 议题 | 整理稿 | 英文稿 | 文字记录 |', '| --- | --- | --- | --- | --- |']
    for row in sorted(manifest['entries'].values(),key=lambda r:r.get('rank',0)):
        links = [f"[阅读]({quote(row[key])})" if row.get(key) else '—'
                 for key in ('speech_relative_path','speech_en_relative_path','relative_path')]
        title = row['entry']['title'].replace('|','\\|')
        lines.append(f"| {row.get('rank','')} | {title} | "+' | '.join(links)+' |')
    return '\n'.join(lines)+'\n'
