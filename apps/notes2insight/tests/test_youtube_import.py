from pathlib import Path
from unittest.mock import patch

import pytest

from apps.notes2insight import link_import, youtube_import as yt, vault

VID = 'z1xRELMKzrA'
URL = f'https://www.youtube.com/watch?v={VID}'
ENTRY = {'id': VID, 'title': 'Scale Across', 'url': URL, 'duration': 60}


def subtitle(tmp_path):
    path = tmp_path / 'sub.vtt'
    path.write_text('WEBVTT\n\n00:00:00.000 --> 00:00:02.000\nA multi region AI cluster.\n')
    return {'path': str(path), 'lang': 'en', 'kind': 'manual', 'upload_date': '20261018'}


def test_import_persists_and_reuses_across_sessions_without_network(tmp_path):
    with patch.object(yt.summit, 'fetch_playlist', return_value={'entries': [ENTRY]}) as discover, \
         patch.object(yt.summit, 'download_subtitle', return_value=subtitle(tmp_path)) as download:
        notes, errors = link_import.import_urls(str(tmp_path / 'session1'), [URL])
        assert not errors and len(notes) == 1
        content = vault.read_note(str(tmp_path / 'session1'), notes[0]['path'])
        assert 'A multi region AI cluster.' in content
        assert URL in content and '文字记录' in content
        assert list(Path(yt.current_root()).rglob('transcript.md'))
        # A new session and shortened URL both reuse the durable material.
        notes2, errors2 = link_import.import_urls(str(tmp_path / 'session2'), [f'https://youtu.be/{VID}'])
        assert not errors2 and len(notes2) == 1
        assert discover.call_count == download.call_count == 1
        assert vault.read_note(str(tmp_path / 'session2'), notes2[0]['path']) == content


def test_playlist_deduplicates_and_preserves_order(tmp_path):
    second = dict(ENTRY, id='CS5Iu94q5jE', title='Another talk', url='https://youtu.be/CS5Iu94q5jE')
    with patch.object(yt.summit, 'fetch_playlist', return_value={'entries': [ENTRY, ENTRY, second]}), \
         patch.object(yt.summit, 'download_subtitle', return_value=subtitle(tmp_path)):
        notes, errors = link_import.import_urls(str(tmp_path / 'session'), ['https://www.youtube.com/playlist?list=PLtest'])
    assert not errors
    assert [n['title'] for n in notes] == ['Scale Across', 'Another talk']


def test_missing_subtitles_are_not_cached_and_can_retry(tmp_path):
    with patch.object(yt.summit, 'fetch_playlist', return_value={'entries': [ENTRY]}), \
         patch.object(yt.summit, 'download_subtitle', return_value=None):
        notes, errors = link_import.import_urls(str(tmp_path / 'session'), [URL])
    assert not notes and errors
    assert yt.cached(VID) is None
    with patch.object(yt.summit, 'download_subtitle', return_value=subtitle(tmp_path)):
        yt.prepare(ENTRY)
    assert yt.cached(VID)


def test_block_message_and_stop_do_not_save_complete_material(tmp_path):
    def blocked(vid, directory, langs, status):
        status['reason'] = 'bot_check'
        return None
    with patch.object(yt.summit, 'download_subtitle', side_effect=blocked):
        with pytest.raises(RuntimeError, match='机器人|拦截|验证'):
            yt.prepare(ENTRY)
    checks = []
    def stop_after_download():
        checks.append(True)
        if len(checks) == 2:
            raise link_import.Stopped()
    with patch.object(yt.summit, 'download_subtitle', return_value=subtitle(tmp_path)):
        with pytest.raises(link_import.Stopped):
            yt.prepare(ENTRY, stop_after_download)
    assert yt.cached(VID) is None


def test_video_with_playlist_parameter_does_not_expand_playlist(tmp_path):
    with patch.object(yt.summit, 'fetch_playlist', return_value={'entries': [ENTRY]}) as fetch:
        assert len(yt.entries_for_url(URL + '&list=PLtest')) == 1
    assert fetch.call_args.args[0] == URL


def test_import_endpoint_returns_materials_ready_for_report(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from apps.notes2insight import server, uploads
    monkeypatch.setattr(server, 'JOBS', {})
    monkeypatch.setattr(server, 'HEAVY_JOBS', SimpleNamespace(try_acquire=lambda: True, start=lambda work: work()))
    monkeypatch.setattr(uploads, 'UPLOADS_ROOT', str(tmp_path / 'uploads'))
    monkeypatch.setattr(uploads, 'prune_old_batches', lambda: None)
    with patch.object(yt.summit, 'fetch_playlist', return_value={'entries': [ENTRY]}), \
         patch.object(yt.summit, 'download_subtitle', return_value=subtitle(tmp_path)):
        client = server.app.test_client()
        started = client.post('/api/import_links', json={'text': URL, 'topic': 'Scale Across', 'output_dir': str(tmp_path / 'output')})
        assert started.status_code == 200
        result = client.get('/api/result/' + started.json['job_id']).json
    assert not result['errors']
    assert result['youtube_materials_dir'] == str(tmp_path / 'output' / 'Scale Across')
    assert len(result['notes']) == 1
    # This is the same root/path contract used by report generation.
    assert 'A multi region AI cluster.' in vault.read_note(result['root'], result['notes'][0]['path'])


def test_topic_folder_is_inside_output_and_has_safe_name(tmp_path):
    root = tmp_path / 'output'
    result = Path(yt.materials_directory(str(root), '../网络/AI:算力'))
    assert result.is_relative_to(root)
    assert result.parent == root
    assert result.name == '_网络_AI_算力'
    with pytest.raises(ValueError, match='主题'):
        yt.materials_directory(str(root), '  ')


def test_different_topics_do_not_share_material_directories(tmp_path):
    first = yt.materials_directory(str(tmp_path / 'output'), '主题甲')
    second = yt.materials_directory(str(tmp_path / 'output'), '主题乙')
    with patch.object(yt.summit, 'download_subtitle', return_value=subtitle(tmp_path)) as download:
        with yt.storage(first):
            yt.prepare(ENTRY)
        with yt.storage(second):
            assert yt.cached(VID) is None
            yt.prepare(ENTRY)
        assert download.call_count == 2
    assert (Path(first) / ENTRY['title'] / 'transcript.md').exists()
    assert (Path(second) / ENTRY['title'] / 'transcript.md').exists()


def test_same_title_videos_use_distinct_named_folders(tmp_path):
    second = dict(ENTRY, id='CS5Iu94q5jE')
    with patch.object(yt.summit, 'download_subtitle', return_value=subtitle(tmp_path)):
        yt.prepare(ENTRY)
        yt.prepare(second)
    root = Path(yt.current_root())
    assert (root / ENTRY['title'] / 'transcript.md').exists()
    assert (root / (ENTRY['title'] + ' (2)') / 'transcript.md').exists()
    assert yt.cached(VID)[0]['id'] == VID
    assert yt.cached(second['id'])[0]['id'] == second['id']


def test_legacy_id_folder_is_renamed_without_redownloading(tmp_path):
    import json
    root = Path(yt.current_root())
    legacy = root / VID
    legacy.mkdir(parents=True)
    (legacy / 'transcript.md').write_text('Saved transcript')
    (legacy / 'metadata.json').write_text(json.dumps({'entry': ENTRY, 'complete': True}))
    with patch.object(yt.summit, 'download_subtitle', side_effect=AssertionError('must reuse')):
        assert yt.prepare(ENTRY)[1] == 'Saved transcript'
    assert not legacy.exists()
    assert (root / ENTRY['title'] / 'transcript.md').read_text() == 'Saved transcript'


def test_title_path_separators_stay_within_topic_directory(tmp_path):
    entry = dict(ENTRY, title='../AI/算力:现状')
    with patch.object(yt.summit, 'download_subtitle', return_value=subtitle(tmp_path)):
        yt.prepare(entry)
    assert (Path(yt.current_root()) / '_AI_算力_现状' / 'transcript.md').exists()
