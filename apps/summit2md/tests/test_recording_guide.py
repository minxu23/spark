import json
from pathlib import Path
from unittest.mock import patch

import pytest

from apps.summit2md import recording_guide as g, server

SOURCE = 'https://www.opencompute.org/events/ocp-summit/2026-ocp-global-summit/recorded-content/'


def directory():
    entry = {'id':'z1xRELMKzrA', 'title':'Scale-across Networking', 'url':'https://youtu.be/z1xRELMKzrA',
             'speakers':'Alice (OCP)', 'slides':[{'title':'G-Drive', 'url':'https://drive.google.com/file/example'}]}
    return {'source_url':SOURCE, 'summit_title':'2026 OCP Global Summit', 'entries':[], 'content_type':'summit',
            'recorded_tracks':[{'id':'networking','title':'Networking','count':1,'entries':[entry]},
                               {'id':'ai','title':'AI Clusters','count':1,'entries':[dict(entry)]}]}


def test_index_references_existing_material_without_copying_and_deduplicates(tmp_path):
    talk = tmp_path / 'Networking'
    (talk / 'speech').mkdir(parents=True)
    (talk / 'speech/talk.md').write_text('# Saved speech')
    (talk / '.manifest.json').write_text(json.dumps({'entries':{'z1xRELMKzrA':{
        'entry':{'id':'z1xRELMKzrA'}, 'speech_relative_path':'speech/talk.md'}}}))
    result = g.save(directory(), str(tmp_path))
    assert result['total'] == 1 and result['local_count'] == 1 and result['tracks'] == 2
    assert Path(result['path']).exists()
    assert len(list(tmp_path.rglob('talk.md'))) == 1
    text = Path(result['path']).read_text()
    assert 'Alice (OCP)' in text and 'drive.google.com' in text and g.reader_url(talk/'speech/talk.md',offline=True).replace('&','\\u0026') in text
    enriched = g.with_local(directory(), str(tmp_path))
    assert enriched['recorded_tracks'][0]['entries'][0]['local']['url'].startswith('/read/open?')


def test_unrelated_files_and_external_paths_do_not_become_local_materials(tmp_path):
    folder = tmp_path / 'Track';folder.mkdir()
    outside = tmp_path.parent / (tmp_path.name + '-external.md');outside.write_text('external')
    (folder / '.manifest.json').write_text(json.dumps({'entries':{'x':{
        'entry':{'id':'z1xRELMKzrA'}, 'speech_relative_path':str(outside)}}}))
    assert g.local_materials(str(tmp_path)) == {}
    target = g.guide_directory(str(tmp_path), SOURCE);target.mkdir()
    (target/'大会导航.html').write_text('User file')
    with pytest.raises(ValueError, match='未覆盖'):
        g.save(directory(), str(tmp_path))
    assert (target/'大会导航.html').read_text() == 'User file'


def test_guide_escapes_untrusted_titles_inside_json(tmp_path):
    data=directory()
    data['recorded_tracks'][0]['entries'][0]['title']='</script><script>alert(1)</script>'
    text=g.render(g.with_local(data,str(tmp_path)), '/static/recording-guide.js')
    assert '</script><script>alert(1)' not in text
    assert '\\u003c/script' in text


def test_api_saves_and_serves_searchable_guide_without_youtube(tmp_path, monkeypatch):
    monkeypatch.setattr(server.core_settings,'output_dir',lambda *args: str(tmp_path/'notes'))
    with patch.object(g.recorded_content,'discover',return_value=directory()):
        client=server.app.test_client()
        response=client.post('/api/recording_guide',json={'source':SOURCE,'output_dir':str(tmp_path)})
        assert response.status_code==200,response.json
        page=client.get(response.json['url'].replace('/summit/','/'))
    assert page.status_code==200
    assert b'recording-guide.js' in page.data
    assert b'id="search"' in page.data
    assert 'unsafe-inline' not in page.headers['Content-Security-Policy'].split('script-src ')[1].split(';')[0]


def test_online_and_saved_navigation_use_reader_for_every_local_document(tmp_path):
    talk=tmp_path/'Talks';talk.mkdir()
    source=talk/'演讲 稿.md';source.write_text('Saved text')
    (talk/'.manifest.json').write_text(json.dumps({'entries':{'z1xRELMKzrA':{
        'entry':{'id':'z1xRELMKzrA'},'speech_relative_path':source.name}}}))
    for offline in (False,True):
        data=g.with_local(directory(),tmp_path,offline=offline)
        href=data['recorded_tracks'][0]['entries'][0]['local']['url']
        assert href==g.reader_url(source,offline=offline)
        assert '/read/open?path=' in href and not href.startswith('./')


def test_offline_links_follow_spark_port(monkeypatch, tmp_path):
    monkeypatch.delenv('SPARK_PORT', raising=False)
    assert g.reader_url(tmp_path/'a.md', offline=True).startswith('http://127.0.0.1:8760/read/open?')
    monkeypatch.setenv('SPARK_PORT', '9001')
    assert g.reader_url(tmp_path/'a.md', offline=True).startswith('http://127.0.0.1:9001/read/open?')
    assert g.reader_url(tmp_path/'a.md').startswith('/read/open?')
