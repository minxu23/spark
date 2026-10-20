import json
from unittest import mock

from apps.summit2md import pending_tasks, pipeline, server


def meeting(tmp_path, *, ok=False, overall='', content_type='summit'):
    folder = tmp_path / 'Conference'
    folder.mkdir(exist_ok=True)
    entry = {'id': 'video1', 'title': 'A talk', 'url': 'https://www.youtube.com/watch?v=video1'}
    (folder / 'talk.md').write_text('Transcript', encoding='utf-8')
    manifest = {'content_type': content_type, 'entries': {
        entry['id']: {'entry': entry, 'ok': ok, 'relative_path': 'talk.md',
                      'summary': {'tldr': 'done', 'body': 'done'}}}, 'overall_summary': overall}
    pipeline._save_manifest(str(folder), manifest)
    return folder, entry


def config(tmp_path, **kwargs):
    return dict(summit_title='Conference', output_dir=str(tmp_path), content_type='summit', backend='api',
                model='test', api_base='', overall_model='test', do_summary=True, do_overall_summary=False,
                **kwargs)


def test_saved_plan_survives_memory_loss_and_completion_clears_list(tmp_path):
    folder, entry = meeting(tmp_path)
    pending_tasks.remember(str(folder), config(tmp_path), [entry])
    # 清空服务内存不影响磁盘里的待完成清单。
    with mock.patch.dict(server.JOBS, {}, clear=True):
        assert pending_tasks.list_pending(tmp_path)[0]['pending_count'] == 1
    manifest = pipeline._load_manifest(str(folder))
    manifest['entries'][entry['id']]['ok'] = True
    pipeline._save_manifest(str(folder), manifest)
    pending_tasks.finish(str(folder), {'result': {}, 'log': []})
    assert pending_tasks.list_pending(tmp_path) == []


def test_legacy_summary_failure_is_found_even_if_all_topics_succeeded(tmp_path):
    folder, entry = meeting(tmp_path, ok=True, overall='_（大会总结生成失败：Connection error.）_')
    items = pending_tasks.list_pending(tmp_path)
    assert len(items) == 1
    assert items[0]['pending_count'] == 0
    assert items[0]['overall_needed']
    assert items[0]['config'] is None


def test_interrupted_entries_not_yet_in_manifest_are_recoverable(tmp_path):
    folder, entry = meeting(tmp_path, ok=True)
    second = dict(entry, id='video2')
    pending_tasks.remember(str(folder), config(tmp_path), [entry, second])
    assert [e['id'] for e in pending_tasks.list_pending(tmp_path)[0]['pending_entries']] == ['video2']


def test_missing_summary_and_speech_detected_only_when_requested(tmp_path):
    folder, entry = meeting(tmp_path, ok=True)
    manifest = pipeline._load_manifest(str(folder))
    manifest['entries'][entry['id']]['summary'] = {'body': '_（摘要生成失败：network）_'}
    pipeline._save_manifest(str(folder), manifest)
    pending_tasks.remember(str(folder), config(tmp_path, do_speech_script=True), [entry])
    assert pending_tasks.list_pending(tmp_path)[0]['pending_count'] == 1
    cfg = config(tmp_path)
    cfg['do_summary'] = False
    pending_tasks.remember(str(folder), cfg, [entry])
    assert pending_tasks.list_pending(tmp_path) == []


def test_series_and_successful_legacy_meetings_are_excluded(tmp_path):
    meeting(tmp_path, ok=False, content_type='series')
    assert pending_tasks.list_pending(tmp_path) == []
    meeting(tmp_path, ok=True, overall='Done')
    assert pending_tasks.list_pending(tmp_path) == []


def test_retry_overall_only_disables_per_topic_work_and_keeps_original_model(tmp_path):
    folder, entry = meeting(tmp_path, ok=True, overall='_（大会总结生成失败：network）_')
    pending_tasks.remember(str(folder), config(tmp_path), [entry])
    client = server.app.test_client()
    with mock.patch.object(server, '_launch_run', return_value=({'job_id': 'new'}, 200)) as launch:
        result = client.post(f'/api/pending_meetings/{pending_tasks.key(str(folder))}/retry', json={
            'output_dir': str(tmp_path), 'api_key': 'temporary-secret', 'model': 'wrong-model'})
    assert result.status_code == 200
    payload = launch.call_args.args[0]
    assert payload['model'] == 'test'
    assert payload['do_overall_summary'] is True
    assert payload['do_summary'] is False
    assert payload['do_speech_script'] is False
    assert payload['skip_existing'] is True
    assert 'temporary-secret' not in pending_tasks.STATE_PATH.read_text()


def test_retry_active_meeting_rejected_and_no_entries_exposed_in_list(tmp_path):
    folder, entry = meeting(tmp_path)
    client = server.app.test_client()
    with mock.patch.dict(server.ACTIVE_OUTPUT_DIRS, {str(folder): 'active'}, clear=True):
        items = client.get('/api/pending_meetings', query_string={'output_dir': str(tmp_path)}).get_json()['meetings']
        assert items[0]['running']
        assert 'entries' not in items[0]
        response = client.post(f'/api/pending_meetings/{items[0]["id"]}/retry', json={'output_dir': str(tmp_path)})
        assert response.status_code == 409
