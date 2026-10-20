from unittest.mock import patch

import pytest

from apps.summit2md import pipeline, recorded_content as rc, server

URL = 'https://www.opencompute.org/events/ocp-summit/2026-ocp-global-summit/recorded-content/'
HTML = b'''<h1>Recorded Content</h1>
<a href="https://www.youtube.com/watch?v=trailer0000">Trailer</a>
<h2>Index</h2><table><tr><td>Tracks</td></tr><tr><td><a href="https://www.youtube.com/playlist?list=PLnav">Playlist</a></td></tr></table>
<h2><a id="networking"></a>Networking (Alphabetical order)</h2>
<table><tr><th>Title</th><th>Speaker(s)</th><th>Video</th><th>Slides</th></tr>
<tr><td>Talk B</td><td>Speaker</td><td><a href="https://www.youtube.com/watch?v=CS5Iu94q5jE">Video</a></td><td><a href="https://drive.google.com/file/id">Slides</a></td></tr>
<tr><td>Talk A</td><td></td><td><a href="https://youtu.be/z1xRELMKzrA">Video</a></td></tr>
<tr><td>Duplicate</td><td></td><td><a href="https://youtu.be/z1xRELMKzrA">Video</a></td></tr>
<tr><td>No video yet</td><td></td><td></td></tr></table>
<h2 id="fts">FTS (Date/time order)</h2>
<table><tr><th>Title</th><th>Video</th></tr><tr><td>Shared talk</td><td><a href="https://youtu.be/z1xRELMKzrA">Video</a></td></tr>
<tr><td>FTS only</td><td><a href="https://www.youtube.com/embed/LdjbbZV7zCg">Video</a></td></tr></table>'''


def test_directory_excludes_navigation_and_preserves_order():
    result = rc.parse_directory(HTML, URL + '?utm_source=chatgpt.com')
    assert result['source_url'] == URL
    assert result['entries'] == []
    assert result['summit_title'] == '2026 OCP Global Summit'
    tracks = result['recorded_tracks']
    assert [(t['id'], t['count']) for t in tracks] == [('networking', 2), ('fts', 2)]
    assert [e['title'] for e in tracks[0]['entries']] == ['Talk B', 'Talk A']


@pytest.mark.parametrize('selection,ids', [('networking', ['CS5Iu94q5jE', 'z1xRELMKzrA']),
                                           ('all', ['CS5Iu94q5jE', 'z1xRELMKzrA', 'LdjbbZV7zCg'])])
def test_saved_source_restores_only_selected_track(selection, ids):
    result = rc.parse_directory(HTML, URL + '#spark-track=' + selection)
    assert [e['id'] for e in result['entries']] == ids
    assert [e['index'] for e in result['entries']] == list(range(1, len(ids) + 1))
    assert result['source_url'].endswith('#spark-track=' + selection)
    assert 'recorded_tracks' not in result


def test_missing_track_and_changed_page_do_not_expand_other_content():
    with pytest.raises(RuntimeError, match='找不到之前选择'):
        rc.parse_directory(HTML, URL + '#spark-track=removed')
    with pytest.raises(RuntimeError, match='没有找到'):
        rc.parse_directory(b'<h2>News</h2><a href="https://youtu.be/z1xRELMKzrA">Trailer</a>', URL)
    assert not rc.supports('https://www.opencompute.org.evil.test/events/recorded-content/')
    assert not rc.supports('https://example.com/events/recorded-content/')
    assert rc.supports(URL + '?utm_source=chatgpt.com')


def test_discovery_and_refresh_use_directory_without_youtube_requests():
    with patch.object(rc.sources, '_http_get', return_value=HTML), \
         patch.object(pipeline, '_fetch_youtube_playlist', side_effect=AssertionError('must not fetch YouTube')):
        result = pipeline.fetch_playlist(URL)
        assert len(result['recorded_tracks']) == 2
        result = pipeline.fetch_playlist(URL + '#spark-track=fts', light=True)
        assert len(result['entries']) == 2
        resp = server.app.test_client().post('/api/discover', json={'url': URL + '?utm_source=chatgpt.com#spark-track=fts'})
        assert resp.status_code == 200
        assert resp.json['source_url'] == URL + '#spark-track=fts'
        assert resp.json['summit_title'].endswith(' - FTS')


def test_directory_includes_speaker_and_slide_links_for_search():
    data = rc.parse_directory(HTML, URL)
    talk = data['recorded_tracks'][0]['entries'][0]
    assert talk['speakers'] == 'Speaker'
    assert talk['slides'] == [{'title':'Slides', 'url':'https://drive.google.com/file/id'}]
