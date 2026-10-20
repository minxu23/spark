"""Finish and persist the active entry before pausing, including the last entry."""
import json
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from apps.summit2md import pipeline, server


@pytest.mark.parametrize('count,backfill', [(2, False), (1, False), (2, True)])
def test_pause_after_summary_saves_current_and_resume_continues(tmp_path, monkeypatch, count, backfill):
    captured = {}
    class Slots:
        def try_acquire(self): return True
        def start(self, fn, jid, params): captured.update(fn=fn, jid=jid, params=params)
        def release(self): pass
    monkeypatch.setattr(server, 'HEAVY_JOBS', Slots())
    monkeypatch.setattr(server, '_resolve_llm_config', lambda *a, **kw: (
        {'backend':'api','api_key':'test','model':'test','api_base':''}, None))
    entries = [{'id':f'video-{i}', 'title':f'Talk {i}', 'url':f'https://youtu.be/video-{i}',
                'index':i+1, 'duration':10} for i in range(count)]
    folder = tmp_path/'Pause Test'
    if backfill:
        (folder/'transcripts').mkdir(parents=True)
        rows = {}
        for i, entry in enumerate(entries, 1):
            rel = f'transcripts/{i}.md'
            (folder/rel).write_text(pipeline.render_transcript_md(
                entry, 'Pause Test', [(0, 'Existing evidence')], None, 'en'))
            rows[entry['id']] = {'entry':entry, 'rank':i, 'ok':True, 'relative_path':rel}
        pipeline._save_manifest(str(folder), {'entries':rows})
    vtt = tmp_path/'sample.vtt'
    vtt.write_text('WEBVTT\n\n00:00:00.000 --> 00:00:02.000\nEvidence for summary.\n')
    monkeypatch.setattr(pipeline, 'download_subtitle', lambda *a, **kw: {
        'lang':'en','path':str(vtt),'kind':'auto','description':'','upload_date':''})
    started, release = threading.Event(), threading.Event()
    calls = []
    def summary(job, entry, *args):
        calls.append(entry['id'])
        if len(calls) == 1:
            started.set()
            assert release.wait(5), 'test did not release current summary'
        return {'tldr':'Saved summary', 'body':'Saved evidence'}
    monkeypatch.setattr(pipeline, '_summarize_entry', summary)
    with patch.dict(server.JOBS, {}, clear=True), patch.dict(server.ACTIVE_OUTPUT_DIRS, {}, clear=True):
        client = server.app.test_client()
        response = client.post('/api/run', json={
            'summit_title':'Pause Test','source_url':'https://example.org/playlist',
            'entries':entries,'output_dir':str(tmp_path), 'backend':'api',
            'do_summary':True,'do_overall_summary':False})
        assert response.status_code == 200, response.json
        jid = captured['jid']
        worker = threading.Thread(target=captured['fn'], args=(jid, captured['params']))
        worker.start()
        try:
            assert started.wait(5)
            assert client.post(f'/api/pause_after_current/{jid}').status_code == 200
            status = client.get(f'/api/status/{jid}').json
            assert status['pause_after_current'] and not status['paused']
            release.set()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                status = client.get(f'/api/status/{jid}').json
                if status['paused'] or status['done']: break
                time.sleep(.01)
            assert status['paused'] and not status['done'], status
            assert not status['pause_after_current']
            assert calls == ['video-0']
            saved = json.loads((folder/'.manifest.json').read_text())['entries']['video-0']
            assert saved['summary']['tldr'] == 'Saved summary'
            assert 'Saved summary' in (folder/saved['relative_path']).read_text()
            assert client.post(f'/api/resume/{jid}').status_code == 200
            worker.join(5)
            assert not worker.is_alive()
            assert calls == [e['id'] for e in entries]
            assert server.JOBS[jid]['done'] and not server.JOBS[jid]['error']
        finally:
            release.set()
            client.post(f'/api/stop/{jid}')
            worker.join(5)


def test_cancel_pending_stop_and_invalid_requests():
    client = server.app.test_client()
    with patch.dict(server.JOBS, {'test':{'done':False, 'paused':False}}, clear=True):
        assert client.post('/api/pause_after_current/missing').status_code == 404
        assert client.post('/api/pause_after_current/test').status_code == 200
        assert server.JOBS['test']['pause_after_current']
        client.post('/api/resume/test')
        assert not server.JOBS['test']['pause_after_current']
        client.post('/api/pause_after_current/test')
        client.post('/api/stop/test')
        assert not server.JOBS['test']['pause_after_current']
        assert not server.JOBS['test']['paused']
        assert client.post('/api/pause_after_current/test').status_code == 400
        server.JOBS['test']['done'] = True
        assert client.post('/api/pause_after_current/test').status_code == 400


def test_batch_waits_before_next_group(tmp_path, monkeypatch):
    from apps.summit2md import recording_batch
    from apps.summit2md.tests.test_recording_batch import catalog, run_plan, processor
    _, entries = catalog(tmp_path, 1)
    plan = recording_batch.partition(tmp_path, 'name', entries)
    paused, resume = threading.Event(), threading.Event()
    state = {'pending':False}
    calls = []
    process = processor(calls)
    def child(**kwargs):
        result = process(**kwargs)
        if len(calls) == 1: state['pending'] = True
        return result
    def pause():
        if not state['pending']: return False
        paused.set()
        return not resume.is_set()
    monkeypatch.setattr(pipeline, 'process_job', child)
    result = []
    def run():
        result.append(run_plan(tmp_path, plan, pause_flag=pause))
    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert paused.wait(5)
        assert calls == ['Theme 0']
        assert (Path(plan['batches'][0]['output_dir'])/'.manifest.json').is_file()
        resume.set()
        worker.join(5)
        assert not worker.is_alive()
        assert calls == ['Theme 0', 'Theme 1', 'Theme 2']
        assert result and not result[0]['stopped']
    finally:
        resume.set()
        worker.join(5)
