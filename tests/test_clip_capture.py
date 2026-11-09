import time

import pytest

from core import clip_capture, clips, remote_tasks
from tests.test_reader import _client


@pytest.fixture
def capture(tmp_path, monkeypatch):
    monkeypatch.setenv('SPARK_VAULT', str(tmp_path))
    monkeypatch.setattr(clip_capture, '_JOBS', {})
    monkeypatch.setattr(clip_capture.github_sync, 'after_task', lambda: None)
    monkeypatch.setattr(clips, 'download_images', lambda images, root: (len(images), 0))
    return tmp_path


def wait(job):
    for _ in range(200):
        state = clip_capture.status(job['id'])
        if state['done']:
            return state
        time.sleep(.01)
    pytest.fail('剪藏保存未完成')


def payload(content='# 标题\n\n正文'):
    return dict(path='Spark/剪藏/20261108_文章.md', content=content, images={})


def test_save_preserves_existing_and_indexes(capture):
    first = wait(clip_capture.start(payload()))
    assert not first['error']
    second = wait(clip_capture.start(payload('# 另一个标题\n\n另一篇正文')))
    assert first['path'] != second['path']
    assert (capture / first['path']).read_text() == payload()['content']
    assert (capture / second['path']).read_text() == '# 另一个标题\n\n另一篇正文'
    assert (capture / 'Spark/剪藏/索引.md').exists()
    assert clip_capture.active_count() == 0


def test_api_rejects_outside_clips(capture):
    c = _client()
    assert c.post('/read/api/clips', json=payload()).status_code == 202
    for path in ('output/report.md', 'Spark/剪藏/../../坏.md', 'Spark/剪藏/索引.md'):
        assert c.post('/read/api/clips', json=dict(payload(), path=path)).status_code == 400
    assert c.post('/read/api/clips', json=[]).status_code == 400
    assert c.get('/read/api/clips/missing').status_code == 404
    for job_id in list(clip_capture._JOBS):
        wait(dict(id=job_id))


def test_image_failures_keep_article(capture, monkeypatch):
    monkeypatch.setattr(clips, 'download_images', lambda images, root: (0, 1))
    data = payload()
    data['images'] = {'Spark/剪藏/attachments/0123456789abcdef.jpg': 'https://example.com/a.jpg'}
    job = wait(clip_capture.start(data))
    assert not job['error']
    assert '1 张图片' in job['warning']
    assert (capture / job['path']).exists()


def test_bad_image_path_rejected(capture):
    with pytest.raises(remote_tasks.TaskError):
        clip_capture.start(dict(payload(), images={'../bad.jpg': 'https://example.com/x'}))


def test_active_job_reported_and_second_rejected(capture, monkeypatch):
    import threading
    entered, release = threading.Event(), threading.Event()

    def download(images, root):
        entered.set()
        assert release.wait(3)
        return (1, 0)

    monkeypatch.setattr(clips, 'download_images', download)
    data = dict(payload(), images={'Spark/剪藏/attachments/0123456789abcdef.jpg': 'https://example.com/a.jpg'})
    job = clip_capture.start(data)
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
