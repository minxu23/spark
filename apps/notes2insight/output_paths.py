"""Shared topic folders for imported materials and generated reports."""
import os
import re


def topic_directory(output_dir: str, topic: str) -> str:
    topic = topic.strip()
    if not topic:
        raise ValueError('请填写分析主题，用于创建资料和报告文件夹')
    name = re.sub(r'[\x00-\x1f<>:"/\\|?*]', '_', topic).strip(' .')
    if not name:
        raise ValueError('分析主题中需要包含有效的文字')
    while len(name.encode('utf-8')) > 150:
        name = name[:-1]
    return os.path.join(os.path.abspath(os.path.expanduser(output_dir)), name)
