import json
import subprocess
from pathlib import Path

import pytest

from apps.summit2md import session_types as types, pipeline, recorded_content


CASES = [
    ('Demystifying MRC and UET: A Panel of Experts', 'panel'),
    ('Scale-across Networking: Panel discussion on routing and transport equipment implications', 'panel'),
    ('Panel Q&A discussion and closing remarks', 'panel'),
    ('10158 Panel: Networking', 'panel'),
    ('Fireside Chat with Industry Leaders', 'fireside'),
    ('Opening — Fireside: The Future of AI', 'fireside'),
    ('Fireside chat - Jane Smith', 'fireside'),
    ('Round Table: AI Infrastructure', 'roundtable'),
    ('Time Appliances Open Discussion & Q&A 1', 'qa'),
    ('In Conversation with Jane Smith', 'discussion'),
    ('Workshop: Hands-on Session', 'workshop'),
    ('Startup Spotlight: New Tools', 'spotlight'),
    ('专家圆桌：算力基础设施', 'roundtable'),
    ('Panel-Level Packaging for AI Chips', 'talk'),
    ('Control Panel Design', 'talk'),
    ('Jane Smith - New Infrastructure', 'talk'),
    ('', 'talk'),
]


@pytest.mark.parametrize('title,expected', CASES)
def test_title_recognition(title, expected):
    assert types.from_title(title) == expected
    if expected != 'talk':
        assert pipeline.guess_single_speaker(title) is None


def test_official_title_and_manual_override_take_precedence():
    entry = {'title':'Jane Smith - Infrastructure', 'agenda_meta':{'agenda_title':'Panel: Infrastructure'}}
    assert types.classify(entry) == 'panel'
    assert types.classify(dict(entry, session_type_override='talk')) == 'talk'
    assert types.classify(dict(entry, session_type_override='fireside')) == 'fireside'
    assert types.classify(dict(entry, session_type_override='unknown')) == 'panel'
    assert types.classify({'title':'Panel: Infrastructure','speakers':'Jane, Alex'}) == 'panel'
    assert types.classify({'title':'Infrastructure','speakers':'Jane, Alex'}) == 'talk'
    assert pipeline.guess_single_speaker('Jane Smith - Infrastructure') == 'Jane Smith'


def test_existing_cached_recording_directory_gets_session_types_without_refetch():
    data = {'summit_title':'Future Conference','source_url':'https://example.test/recordings',
            'recorded_tracks':[{'id':'track','title':'Networking','count':len(CASES),'entries':[
                {'id':str(i),'title':title} for i, (title, _) in enumerate(CASES)]}]}
    normalized = recorded_content.normalize_directory(data)
    assert [e['session_type'] for e in normalized['recorded_tracks'][0]['entries']] == [expected for _,expected in CASES]
    assert 'session_type' not in data['recorded_tracks'][0]['entries'][0]


def test_browser_and_worker_use_same_rules():
    source = (Path(__file__).parents[1]/'static/app.js').read_text()
    function = source[source.index('  function sessionType(e) {'):source.index('  function renderEntries() {')]
    entries = [{'title':title} for title, _ in CASES] + [
        {'title':'Talk','agenda_meta':{'agenda_title':'Fireside Chat: Infrastructure'}},
        {'title':'Panel: Infrastructure','session_type_override':'talk'}]
    script = ('const sessionTypes = '+json.dumps(types.catalog())+';\n'+function+
              '\nconsole.log(JSON.stringify('+json.dumps(entries)+'.map(sessionType)));')
    result = subprocess.run(['node','-e',script], text=True, capture_output=True, check=True)
    assert json.loads(result.stdout) == [types.classify(entry) for entry in entries]


def test_manual_type_is_saved_when_reusing_existing_files(tmp_path, monkeypatch):
    folder = tmp_path/'Types'
    (folder/'transcripts').mkdir(parents=True)
    (folder/'transcripts/talk.md').write_text('Existing transcript')
    entry = {'id':'video','title':'Panel: Infrastructure','url':'https://youtu.be/test',
             'session_type_override':'fireside'}
    pipeline._save_manifest(str(folder), {'entries':{'video':{
        'rank':1,'entry':{'id':'video','title':entry['title'],'duration':10,'url':entry['url']}, 'ok':True,
        'relative_path':'transcripts/talk.md'}}})
    monkeypatch.setattr(pipeline, 'download_subtitle', lambda *a, **kw: pytest.fail('must reuse existing files'))
    pipeline.process_job(summit_title='Types', source_url='https://example.test/recordings',
        entries=[entry], output_base_dir=str(tmp_path), backend='api', api_key='test', model='test',
        lang_prefs=['en'], do_summary=False, do_overall_summary=False)
    saved = pipeline.import_output_directory(str(folder))['entries'][0]
    assert saved['session_type'] == 'fireside'
    assert saved['session_type_override'] == 'fireside'
    assert (folder/'transcripts/talk.md').read_text() == 'Existing transcript'


def test_panel_uses_multiple_speaker_path_and_persists_type(tmp_path, monkeypatch):
    vtt = tmp_path/'sample.vtt'
    vtt.write_text('WEBVTT\n\n00:00:00.000 --> 00:00:02.000\nEvidence for discussion.\n')
    monkeypatch.setattr(pipeline, 'download_subtitle', lambda *a, **kw: {
        'lang':'en','path':str(vtt),'kind':'auto','description':'','upload_date':''})
    calls = []
    def speakers(*args, **kwargs):
        calls.append(args[1])
        return {1:'Moderator'}
    monkeypatch.setattr(pipeline, 'infer_speakers', speakers)
    title = 'Demystifying MRC and UET: A Panel of Experts'
    result = pipeline.process_job(summit_title='Panel Test', source_url='https://example.test/recordings',
        entries=[{'id':'video','title':title,'url':'https://youtu.be/test','duration':10}],
        output_base_dir=str(tmp_path), backend='api', api_key='test', model='test',
        lang_prefs=['en'], do_summary=False, do_overall_summary=False, do_speaker_label=True)
    assert calls == [title]
    saved = pipeline.import_output_directory(result['output_dir'])['entries'][0]
    assert saved['session_type'] == 'panel'


@pytest.mark.parametrize('enabled', [True, False])
def test_speaker_option_only_calls_model_for_dialogue_types(tmp_path, monkeypatch, enabled):
    vtt = tmp_path/'sample.vtt'
    vtt.write_text('WEBVTT\n\n00:00:00.000 --> 00:00:02.000\nEvidence for discussion.\n')
    monkeypatch.setattr(pipeline, 'download_subtitle', lambda *a, **kw: {
        'lang':'en','path':str(vtt),'kind':'auto','description':'','upload_date':''})
    entries = [
        {'title':'Technical infrastructure without a speaker name'},
        {'title':'Jane Smith - Infrastructure'},
        {'title':'Panel: Infrastructure'},
        {'title':'Fireside Chat: Infrastructure'},
        {'title':'Roundtable: Infrastructure'},
        {'title':'In Conversation with Jane Smith'},
        {'title':'Q&A: Infrastructure'},
        {'title':'Workshop: Infrastructure'},
        {'title':'Startup Spotlight: Infrastructure'},
        {'title':'Panel: Manually marked as solo','session_type_override':'talk'},
        {'title':'Manually marked as a discussion','session_type_override':'discussion'},
    ]
    entries = [dict(entry, id=str(i), duration=10, url=f'https://example.test/video/{i}')
               for i,entry in enumerate(entries)]
    calls = []
    def speakers(paragraphs, title, *args, **kwargs):
        calls.append(title)
        return {1:'Moderator'}
    monkeypatch.setattr(pipeline, 'infer_speakers', speakers)
    result = pipeline.process_job(summit_title='Speaker Scope', source_url='https://example.test/recordings',
        entries=entries, output_base_dir=str(tmp_path), backend='api', api_key='test', model='test',
        lang_prefs=['en'], do_summary=False, do_overall_summary=False, do_speaker_label=enabled)
    assert not result['failed_entries']
    expected = [entries[i]['title'] for i in (2,3,4,5,6,10)] if enabled else []
    assert calls == expected
    # An unnamed solo talk is saved normally, rather than going through speaker inference.
    assert all((Path(result['output_dir'])/row['relative_path']).is_file() for row in result['rows'])


def test_browser_and_worker_agree_on_multispeaker_scope():
    source = (Path(__file__).parents[1]/'static/app.js').read_text()
    function = source[source.index('  function sessionType(e) {'):source.index('  function renderEntries() {')]
    entries = [{'title':'No named speaker','session_type_override':kind} for kind in types.LABELS]
    entries += [{'title':'Panel: Manually marked solo','session_type_override':'talk'},
                {'title':'Full session Panel: Infrastructure','is_raw_session':True}]
    script = ('const sessionTypes = '+json.dumps(types.catalog())+';\n'+function+
              '\nconsole.log(JSON.stringify('+json.dumps(entries)+'.map(isMultiSpeaker)));')
    result = subprocess.run(['node','-e',script], text=True, capture_output=True, check=True)
    assert json.loads(result.stdout) == [types.is_multi_speaker(entry) for entry in entries]
