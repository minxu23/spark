import re
import threading
import time

import pytest

from core import clip_capture, clips, remote_tasks
from tests.test_reader import _client

IMG = 'attachments/0123456789abcdef.jpg'


@pytest.fixture
def capture(tmp_path, monkeypatch):
    monkeypatch.setenv('SPARK_VAULT', str(tmp_path))
    monkeypatch.setattr(clip_capture, '_JOBS', {})
    monkeypatch.setattr(clip_capture.github_sync, 'after_task', lambda: None)
    monkeypatch.setattr(clips, 'download_images', lambda images, root: (len(images), 0))
    monkeypatch.setattr(clip_capture, '_ask', lambda: None)
    return tmp_path


def wait(job):
    for _ in range(300):
        state = clip_capture.status(job['id'])
        if state['done']:
            return state
        time.sleep(.01)
    pytest.fail('剪藏保存未完成')


def payload(title='文章', markdown='中文正文', **article):
    a = dict(url='https://example.com/a', title=title, source='Example', published='', markdown=markdown, images={})
    a.update(article)
    return dict(article=a, note='')


def test_save_uses_phone_layout_and_keeps_existing(capture):
    first = wait(clip_capture.start(payload()))
    assert not first['error'] and first['path'].startswith('Spark/剪藏/') and first['path'].endswith('_文章.md')
    assert '没配好模型' in first['warning']
    text = (capture / first['path']).read_text()
    assert text.startswith('---\ntitle: "文章"\nsource: "Example"\nurl: "https://example.com/a"\nclipped: ')
    assert '- 来源：Example\n' in text and '## 正文\n\n中文正文\n\n[阅读原文](https://example.com/a)\n' in text
    second = wait(clip_capture.start(payload(markdown='另一篇')))
    assert second['path'].endswith('_文章_2.md')
    assert (capture / first['path']).read_text() == text, '同名的不覆盖'
    assert (capture / 'Spark/剪藏/索引.md').exists()
    assert clip_capture.active_count() == 0


def test_summary_title_and_translation(capture, monkeypatch):
    prompts = []

    def ask(prompt, max_tokens=4000, timeout=600, images=None):
        prompts.append(prompt)
        if prompt.startswith('把下面编号的英文段落'):
            return '\n'.join(f'[{n}] 中文{n}' for n in range(5))
        return 'TITLE: 模型起的标题\nTLDR: 一句话\nTAGS: AI芯片\n- 要点'

    monkeypatch.setattr(clip_capture, '_ask', lambda: ask)
    english = '\n\n'.join(['This is an English paragraph about memory stores and attention.'] * 4)
    job = wait(clip_capture.start(payload(title='example.com', markdown=english, titleGuessed=True)))
    assert not job['error'] and job['title'] == '模型起的标题' and job['path'].endswith('_模型起的标题.md')
    text = (capture / job['path']).read_text()
    assert 'tags: ["AI芯片"]\ntldr: "一句话"' in text and '**一句话**：一句话\n\n- 要点' in text
    assert '\n\n> 中文0\n\n' in text, '全英文的逐段翻译'
    assert prompts[0].startswith('这篇没有标题。')


def test_user_edited_title_is_kept(capture, monkeypatch):
    monkeypatch.setattr(clip_capture, '_ask', lambda: lambda p, **kw: 'TITLE: 不用这个\nTLDR: 一句话')
    job = wait(clip_capture.start(payload(title='我改的标题')))
    assert job['path'].endswith('_我改的标题.md')


def test_api_rejects_bad_payload(capture):
    c = _client()
    r = c.post('/read/api/clips', json=payload())
    assert r.status_code == 202
    wait(r.json)
    for bad in ([], {}, payload(url='javascript:alert(1)'), payload(markdown=''), payload(published='昨天'),
                payload(images={'../bad.jpg': 'https://example.com/x'})):
        assert c.post('/read/api/clips', json=bad).status_code == 400, bad
    assert c.get('/read/api/clips/missing').status_code == 404


def test_image_failures_keep_article(capture, monkeypatch):
    monkeypatch.setattr(clips, 'download_images', lambda images, root: (0, 1))
    job = wait(clip_capture.start(payload(images={IMG: 'https://example.com/a.jpg'})))
    assert not job['error'] and '1 张图片' in job['warning']
    assert (capture / job['path']).exists()


def test_active_job_reported_and_second_rejected(capture, monkeypatch):
    entered, release = threading.Event(), threading.Event()

    def download(images, root):
        entered.set()
        assert release.wait(3)
        return (1, 0)

    monkeypatch.setattr(clips, 'download_images', download)
    job = clip_capture.start(payload(images={IMG: 'https://example.com/a.jpg'}))
    try:
        assert entered.wait(2)
        assert clip_capture.active_count() == 1
        assert _client().get('/api/activity').json['jobs'] >= 1
        with pytest.raises(remote_tasks.TaskError, match='已有一篇'):
            clip_capture.start(payload())
    finally:
        release.set()
        wait(job)
    assert clip_capture.active_count() == 0


def _saved_clip(capture, body, summary=''):
    folder = capture / 'Spark' / '剪藏'
    folder.mkdir(parents=True, exist_ok=True)
    f = folder / '20261109_旧.md'
    f.write_text('---\ntitle: "旧"\nsource: "S"\nurl: "https://example.com/a"\nclipped: 2026-11-09\n---\n\n# 旧\n\n- 来源：S\n\n'
                 + summary + f'## 正文\n\n{body}\n\n[阅读原文](https://example.com/a)\n', encoding='utf-8')
    return f


def _fake_model(prompts):
    def ask(prompt, max_tokens=4000, timeout=600, images=None):
        prompts.append(prompt)
        if prompt.startswith('把下面编号的英文段落'):
            nums = re.findall(r'^\[(\d+)\]', prompt.split('===== 原文 =====')[1], re.M)
            return '\n'.join(f'[{n}] 中文{n}' for n in nums)
        return 'TLDR: 新的一句话\nTAGS: AI芯片\n- 要点'
    return ask


def test_fill_adds_missing_summary_and_translation(capture, monkeypatch):
    prompts = []
    monkeypatch.setattr(clip_capture, '_ask', lambda: _fake_model(prompts))
    english = '\n\n'.join(f'English paragraph number {i} about memory stores and attention layers.' for i in range(4))
    f = _saved_clip(capture, english)
    assert clip_capture.needs_fill(f.read_text())
    items = _client().get('/read/api/items?place=clips').json['items']
    assert items[0]['needs'] is True
    r = _client().post('/read/api/clips/fill', json={'paths': ['剪藏/20261109_旧.md']})
    assert r.status_code == 202
    job = wait(r.json)
    assert not job['error'] and not job['warning'], job
    text = f.read_text()
    assert 'tags: ["AI芯片"]\ntldr: "新的一句话"\n---' in text
    assert '## 摘要\n\n**一句话**：新的一句话\n\n- 要点\n\n## 正文' in text
    assert 'English paragraph number 0 about memory stores and attention layers.\n\n> 中文0\n\n' in text
    assert text.endswith('> 中文3\n\n[阅读原文](https://example.com/a)\n')
    assert not clip_capture.needs_fill(text)
    assert '新的一句话' in (capture / 'Spark/剪藏/索引.md').read_text()


def test_resummarize_replaces_old_summary_only(capture, monkeypatch):
    prompts = []
    monkeypatch.setattr(clip_capture, '_ask', lambda: _fake_model(prompts))
    f = _saved_clip(capture, '中文正文。', summary='## 摘要\n\n**一句话**：旧的\n\n- 旧要点\n\n')
    assert not clip_capture.needs_fill(f.read_text())
    job = wait(clip_capture.start_fill({'paths': ['Spark/剪藏/20261109_旧.md'], 'mode': 'resummarize'}))
    text = f.read_text()
    assert not job['error'] and '旧的' not in text and text.count('## 摘要') == 1 and '中文正文。' in text
    assert len(prompts) == 1, '中文的不翻译'


def test_fill_rejects_bad_requests(capture, monkeypatch):
    _saved_clip(capture, '正文')
    with pytest.raises(remote_tasks.TaskError, match='没配好模型'):
        clip_capture.start_fill({'paths': ['Spark/剪藏/20261109_旧.md']})
    monkeypatch.setattr(clip_capture, '_ask', lambda: _fake_model([]))
    for bad in ({'paths': []}, {'paths': ['output/a.md']}, {'paths': ['Spark/剪藏/没有.md']},
                {'paths': ['Spark/剪藏/20261109_旧.md'], 'mode': 'x'}):
        with pytest.raises(remote_tasks.TaskError):
            clip_capture.start_fill(bad)
