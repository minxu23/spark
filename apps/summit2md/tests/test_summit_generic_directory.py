import json
from pathlib import Path
from unittest.mock import patch

import pytest

from apps.summit2md import recorded_content as rc, recording_guide as guide, recording_groups as groups, pipeline, server

URL = 'https://conference.example/2029/recordings?event=research'
HTML = '''<title>研究开发者大会 2029 | Recordings</title><h1>Recordings</h1>
<h2 id="models">Research: Models</h2><table><tr><th>Session</th><th>Recording</th></tr>
<tr><td>Training systems</td><td><a href="https://youtu.be/z1xRELMKzrA">Watch</a></td></tr></table>
<h2 id="data">Research: Data</h2><table><tr><th>Session</th><th>Recording</th></tr>
<tr><td>Data pipelines</td><td><a href="https://youtu.be/CS5Iu94q5jE">Watch</a></td></tr></table>'''.encode()


def test_other_conference_full_directory_grouping_and_recovery(tmp_path):
    data = rc.parse_directory(HTML, URL + '&utm_source=test')
    assert data['summit_title'] == '研究开发者大会 2029'
    assert data['source_url'] == URL
    grouped = groups.groups(data['recorded_tracks'])
    assert len(grouped) == 1 and grouped[0]['title'] == 'Research'
    result = guide.save(data, tmp_path)
    folder = Path(result['path']).parent
    assert folder.name == '研究开发者大会 2029'
    title = data['summit_title'] + ' - Research: Models'
    assert Path(pipeline.summit_output_path(tmp_path,title)) == folder/'Research'
    selected = rc.parse_directory(HTML,URL+'#spark-track=models')
    context = groups.collection_for(tmp_path,selected['summit_title'],selected['entries'])
    assert Path(pipeline.summit_output_path(tmp_path,selected['summit_title'],context)) == folder/'Research'
    assert context['source_url'] == URL
    assert not rc.can_discover('https://blog.example/feed')
    assert not rc.can_discover('https://blog.example/article')


def test_same_named_conferences_and_sanitized_groups_do_not_share_folders(tmp_path):
    data=rc.parse_directory(HTML,URL)
    first=guide.save(data,tmp_path)
    other=rc.parse_directory(HTML,'https://other.example/recordings')
    second=guide.save(other,tmp_path)
    assert first['path'] != second['path']
    context=other['recorded_tracks'][0]['entries'][0]['collection']
    assert Path(pipeline.summit_output_path(tmp_path,'name',context)).parent == Path(second['path']).parent
    tracks=data['recorded_tracks']
    tracks[0]['summary_group']='AI/Systems'
    tracks[1]['summary_group']='AI:Systems'
    merged=groups.groups(tracks)
    assert len({g['folder'].casefold() for g in merged}) == 2
    assert all('/' not in g['folder'] for g in merged)


def test_custom_group_mapping_survives_refresh_and_remains_conference_local(tmp_path,monkeypatch):
    data=rc.parse_directory(HTML,URL)
    guide.save(data,tmp_path)
    monkeypatch.setattr(server.core_settings,'output_dir',lambda *a:str(tmp_path/'notes'))
    client=server.app.test_client()
    payload={'source':URL,'output_dir':str(tmp_path),'assignments':{'models':'Models','data':'Data engineering'}}
    response=client.post('/api/recording_groups',json=payload)
    assert response.status_code==200,response.json
    folder=guide.guide_directory(tmp_path,URL)
    with patch.object(rc,'discover',return_value=data):
        response=client.post('/api/recording_guide',json={'source':URL,'output_dir':str(tmp_path)})
    assert response.status_code==200,response.json
    snapshot=json.loads((folder/'.recordings.json').read_text())
    assert [g['title'] for g in groups.groups(snapshot['recorded_tracks'])] == ['Models','Data engineering']
    assert [g['title'] for g in groups.groups(data['recorded_tracks'])] == ['Research']
    # A fresh discovery must honor this catalog's saved override when starting processing.
    selected = rc.parse_directory(HTML,URL+'#spark-track=models')
    context = groups.collection_for(tmp_path,selected['summit_title'],selected['entries'])
    assert context['group_title'] == 'Models'
    assert client.post('/api/recording_groups',json={**payload,'assignments':{'unknown':'x'}}).status_code==400
    assert client.post('/api/recording_groups',json={**payload,'assignments':{'models':'..'}}).status_code==400


def test_mixed_groups_do_not_accidentally_write_to_one_folder(tmp_path):
    entries=[{'collection':{'conference_title':'Summit','group_title':name}} for name in ('A','B')]
    with pytest.raises(ValueError,match='分别整理'):
        groups.collection_for(tmp_path,'Summit',entries)
    assert Path(pipeline.summit_output_path(tmp_path,'Ordinary - ungrouped')) == tmp_path/'Ordinary - ungrouped'


def test_heading_ids_do_not_depend_on_agenda_order():
    raw = HTML.decode().replace(' id="models"','').replace(' id="data"','')
    first = rc.parse_directory(raw.encode(),URL)
    start = raw.index('<h2>')
    parts = raw[start:].split('<h2>')[1:]
    reordered = raw[:start] + ''.join('<h2>' + part for part in reversed(parts))
    second = rc.parse_directory(reordered.encode(),URL)
    assert {t['title']:t['id'] for t in first['recorded_tracks']} == {t['title']:t['id'] for t in second['recorded_tracks']}


def test_group_edit_moves_existing_files_and_clears_overview_role(tmp_path,monkeypatch):
    from apps.summit2md.tests.test_recording_organizer import fixture
    root,data=fixture(tmp_path)
    data['overview_track_ids']=['networking']
    guide.save(data,tmp_path)
    monkeypatch.setattr(server.core_settings,'output_dir',lambda *a:str(tmp_path/'notes'))
    result=server.app.test_client().post('/api/recording_groups',json={
        'source':data['source_url'],'output_dir':str(tmp_path),
        'assignments':{'networking':'工程'},'overview_track_ids':[]})
    assert result.status_code==200 and result.json['organized_files']==2
    assert (root/'工程/speech/001.md').is_file()
    snapshot=guide._json(root/'.recordings.json')
    assert snapshot['overview_track_ids']==[]
    assert 'summary_role' not in snapshot['recorded_tracks'][0]


def test_parent_and_child_jobs_exclude_each_other(tmp_path,monkeypatch):
    from apps.summit2md.tests.test_recording_organizer import fixture
    root,data=fixture(tmp_path)
    with patch.dict(server.ACTIVE_OUTPUT_DIRS,{str(root/'Networking'):'child-job'},clear=True):
        assert server._start_simple_job(lambda **k:None,reserve_dir=str(root)) is None
        with pytest.raises(server._DirBusy):
            with server._hold_dir(str(root)):
                pytest.fail('Should be blocked')
        result=server.app.test_client().post('/api/recording_organize',json={
            'source':data['source_url'],'output_dir':str(tmp_path)})
        assert result.status_code==409
    with patch.dict(server.ACTIVE_OUTPUT_DIRS,{str(root):'parent-job'},clear=True):
        assert server._start_simple_job(lambda **k:None,reserve_dir=str(root/'Networking')) is None
        assert server._active_job_for_dir(str(root/'Networking'))=='parent-job'
    with patch.dict(server.ACTIVE_OUTPUT_DIRS,{str(root/'Networking'):'child-job'},clear=True):
        assert server._active_job_for_dir(str(root/'Different')) is None


def test_loading_conference_or_theme_exposes_group_merge_entry(tmp_path):
    from apps.summit2md.tests.test_recording_organizer import fixture
    from apps.summit2md import recording_organizer
    root,data=fixture(tmp_path)
    recording_organizer.organize(data=data,output_dir=tmp_path)
    client=server.app.test_client()
    for path in (root,root/'Networking'):
        result=client.post('/api/import_dir',json={'path':str(path)})
        assert result.status_code==200,result.json
        context=result.json['recording_context']
        assert context['source_url']==data['source_url']
        assert context['output_dir']==str(tmp_path)
        assert context['agenda_count']==1 and context['group_count']==1
        page=client.get(context['url'])
        assert page.status_code==200
        assert 'id="editGroups"' in page.text
    # Unrelated shows must not inherit the previous conference's tools.
    other=tmp_path/'Other';other.mkdir()
    (other/'.manifest.json').write_text(json.dumps({'entries':{}}))
    with server.app.test_request_context():
        assert server._recording_context(other) is None


def test_fresh_conference_can_bind_user_supplied_official_page_without_saved_context(tmp_path,monkeypatch):
    data=rc.parse_directory(HTML,URL)
    monkeypatch.setattr(server.core_settings,'output_dir',lambda *a:str(tmp_path/'notes'))
    monkeypatch.setattr(rc,'discover',lambda source:data if source==URL else pytest.fail('Unexpected inferred URL'))
    monkeypatch.setattr(pipeline,'_cached_summarize',lambda *a,**k:pytest.fail('No model call to link an agenda'))
    assert not list(tmp_path.glob('*/.recordings.json'))
    result=server.app.test_client().post('/api/recording_guide',json={
        'source':URL,'output_dir':str(tmp_path),'include_directory':True})
    assert result.status_code==200,result.json
    assert result.json['directory']['source_url']==URL
    assert [t['id'] for t in result.json['directory']['recorded_tracks']]==['models','data']
    assert result.json['groups']==1
    assert guide._json(guide.guide_directory(tmp_path,URL)/'.recordings.json')['source_url']==URL
    assert server.app.test_client().get(result.json['url'].replace('/summit/','/')).status_code==200


def test_official_agenda_controls_are_collapsed_advanced_tools_in_step_two():
    from bs4 import BeautifulSoup
    page=server.app.test_client().get('/')
    soup=BeautifulSoup(page.data,'html.parser')
    section=soup.select_one('#agendaSection')
    assert section.name=='details' and not section.has_attr('open') and section.find_parent(id='discoverResults')
    assert section.find_parent('details') is None
    assert soup.select_one('#taskPanel #agendaUrl') is None
    for control in ('agendaUrl','loadOfficialAgendaBtn','agendaOrderBtn','recordingTools','recordedTrackField'):
        assert section.select_one('#'+control)
    assert 'display:none' in section.select_one('#recordingTools').get('style','')
    assert len(soup.select('#agendaUrl'))==1


def test_failed_first_agenda_binding_does_not_create_cached_conference(tmp_path,monkeypatch):
    monkeypatch.setattr(rc,'discover',lambda *a:(_ for _ in ()).throw(RuntimeError('No supported video catalog')))
    result=server.app.test_client().post('/api/recording_guide',json={
        'source':URL,'output_dir':str(tmp_path),'include_directory':True})
    assert result.status_code==400
    assert 'directory' not in result.json and not list(tmp_path.glob('*/.recordings.json'))
