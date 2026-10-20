import copy
import json
from pathlib import Path
from urllib.parse import unquote

import pytest

from apps.summit2md import pipeline, recording_guide as g, recording_summaries as s, server
from apps.summit2md.tests.test_recording_guide import SOURCE, directory


def setup_material(tmp_path):
    folder = tmp_path / 'Networking'
    folder.mkdir()
    (folder / 'speech.md').write_text('UNIQUE_SPEECH_EVIDENCE scale across 42 Tbps')
    (folder / '.manifest.json').write_text(json.dumps({'entries':{'video':{
        'entry':{'id':'z1xRELMKzrA'}, 'speech_relative_path':'speech.md'}}}))
    data = directory()
    data['recorded_tracks'].append({'id':'no-material', 'title':'No Material', 'count':1,
                                   'entries':[{'id':'unavailable', 'title':'Unknown', 'url':'https://youtu.be/unavailable'}]})
    g.save(data, str(tmp_path))
    return data, folder / 'speech.md'


def test_hierarchy_reuses_tracks_and_updates_only_changed_inputs(tmp_path, monkeypatch):
    data, speech = setup_material(tmp_path)
    prompts = []
    def summarize(prompt, **kwargs):
        prompts.append(prompt)
        return 'COMPOSED_TRACK_INSIGHT with [source](https://youtu.be/z1xRELMKzrA)'
    monkeypatch.setattr(pipeline, '_cached_summarize', summarize)
    args = dict(data=data, output_dir=str(tmp_path), config={'backend':'api'})
    result = s.generate(**args)
    assert result['generated'] == 2 and result['skipped'] == ['No Material']
    assert len(prompts) == 3
    assert 'UNIQUE_SPEECH_EVIDENCE' in prompts[0]
    assert 'UNIQUE_SPEECH_EVIDENCE' not in prompts[-1]  # Overall is built from track summaries.
    assert 'COMPOSED_TRACK_INSIGHT' in prompts[-1]
    path = g.guide_directory(tmp_path, SOURCE)
    overall = (path / '大会总结.md').read_text()
    assert '2/3 个专题' in overall and 'No Material' in overall
    assert 'Networking/Networking.md' in unquote(overall)
    enriched = g.with_local(data, str(tmp_path))
    assert not enriched['overall_summary']['stale']
    assert all(not t['summary']['stale'] for t in enriched['summary_groups'][:2])
    s.generate(**args)
    assert len(prompts) == 3
    # Official track changes invalidate just that track and the overall summary.
    changed = copy.deepcopy(data)
    changed['recorded_tracks'][0]['title'] = 'Updated Networking'
    result = s.generate(**{**args, 'data':changed})
    assert result['generated'] == 1 and result['reused'] == 1 and len(prompts) == 5
    speech.write_text('New evidence')
    enriched = g.with_local(changed, str(tmp_path))
    assert enriched['overall_summary']['stale']
    assert enriched['summary_groups'][0]['summary']['stale']


def test_no_metadata_only_summary_and_partial_success_survives_failure(tmp_path, monkeypatch):
    data, _ = setup_material(tmp_path)
    with pytest.raises(pipeline.SummarizeError, match='尚无本地'):
        s.generate(data=data, output_dir=str(tmp_path), config={}, track_id='no-material')
    calls = []
    def fail_second(prompt, **kwargs):
        calls.append(prompt)
        if len(calls) == 2:
            raise pipeline.SummarizeError('connection failed')
        return 'Successful summary'
    monkeypatch.setattr(pipeline, '_cached_summarize', fail_second)
    with pytest.raises(pipeline.SummarizeError):
        s.generate(data=data, output_dir=str(tmp_path), config={})
    state = g._json(g.guide_directory(tmp_path, SOURCE) / '.recording-summaries.json')
    assert len(state['tracks']) == 1
    gid = next(iter(state['tracks']))
    assert (g.guide_directory(tmp_path, SOURCE) / state['tracks'][gid]['path']).is_file()


def test_large_input_preserves_tail_and_stop(tmp_path, monkeypatch):
    prompts = []
    monkeypatch.setattr(pipeline, '_cached_summarize', lambda prompt, **kw: prompts.append(prompt) or 'short digest')
    s.compose('a' * s.CHUNK + 'TAIL_EVIDENCE', 'Track', {}, tmp_path, None)
    assert len(prompts) == 3 and 'TAIL_EVIDENCE' in prompts[1]
    with pytest.raises(pipeline.Stopped):
        s.compose('content', 'Track', {}, tmp_path, lambda:True)


def test_api_validates_official_track_and_uses_summary_model(tmp_path, monkeypatch):
    setup_material(tmp_path)
    monkeypatch.setattr(server.core_settings, 'output_dir', lambda *args:str(tmp_path/'notes'))
    monkeypatch.setattr(server.core_settings, 'backend', lambda *args:'api')
    monkeypatch.setattr(server.core_settings, 'get', lambda key:'summary-model')
    captured = {}
    def resolve(data, **kwargs):
        captured.update(data)
        return {**data, 'api_key':'fake-test-key', 'api_base':''}, None
    monkeypatch.setattr(server, '_resolve_llm_config', resolve)
    monkeypatch.setattr(server, '_start_simple_job', lambda *args, **kw:'job')
    client = server.app.test_client()
    args = {'source':SOURCE, 'output_dir':str(tmp_path), 'track_id':'networking'}
    result = client.post('/api/recording_summary', json=args)
    assert result.status_code == 200 and captured['model'] == 'summary-model'
    assert 'fake-test-key' not in result.text
    assert client.post('/api/recording_summary',json={**args,'track_id':'no-material'}).status_code == 400
    assert client.post('/api/recording_summary',json={**args,'track_id':'../../bad'}).status_code == 400


def test_summary_workflow_physically_files_sources_and_reuses_after_retry(tmp_path,monkeypatch):
    from apps.summit2md.tests.test_recording_organizer import fixture
    root,data=fixture(tmp_path)
    prompts=[]
    monkeypatch.setattr(pipeline,'_cached_summarize',lambda prompt,**kw:prompts.append(prompt) or 'Complete evidence based summary')
    result=s.generate(data=data,output_dir=tmp_path,config={})
    assert result['organized_files']==2 and not (root/'speech').exists()
    assert (root/'Networking/speech/001.md').exists()
    child=g._json(root/'Networking/.manifest.json')
    assert child['overall_summary']==s.documents.summary_part((root/'Networking/Networking.md').read_text())
    assert (root/'Networking/Networking.md').exists()
    again=s.generate(data=data,output_dir=tmp_path,config={})
    assert again['organized_files']==0 and again['reused']==1
    assert len(prompts)==2
    assert not g.with_local(data,tmp_path)['overall_summary']['stale']


def test_fragmented_agenda_requires_review_before_any_paid_summary(tmp_path,monkeypatch):
    from apps.summit2md.tests.test_recording_grouping import catalog
    monkeypatch.setattr(pipeline,'_cached_summarize',lambda *a,**k:pytest.fail('No paid call before grouping review'))
    with pytest.raises(ValueError,match='分组过细'):
        s.generate(data=catalog(),output_dir=tmp_path,config={})


def test_legacy_summary_and_index_merge_without_regenerating_or_losing_sources(tmp_path,monkeypatch):
    from apps.summit2md import recording_organizer as organizer, recording_documents as docs
    from apps.summit2md.tests.test_recording_organizer import fixture
    root,data=fixture(tmp_path)
    prompts=[]
    monkeypatch.setattr(pipeline,'_cached_summarize',lambda prompt,**kw:prompts.append(prompt) or 'Original synthesis with precise evidence')
    s.generate(data=data,output_dir=tmp_path,config={})
    canonical=root/'Networking/Networking.md';legacy=canonical.with_name('专题总结.md')
    original=docs.summary_part(canonical.read_text()).replace(docs.SUMMARY_MARKER+'\n','')
    legacy.write_text(original)
    canonical.write_text(g.MARKER+'\n# Legacy separate index\n[Summary](专题总结.md)')
    state=g._json(root/'.recording-summaries.json')
    gid=next(iter(state['tracks']))
    state['tracks'][gid]['path']='Networking/专题总结.md'
    (root/'.recording-summaries.json').write_text(json.dumps(state))
    overall=root/'大会总结.md'
    overall.write_text(overall.read_text().replace('Networking/Networking.md','Networking/专题总结.md'))
    source=(root/'Networking/speech/001.md').read_bytes()
    result=organizer.organize(data=data,output_dir=tmp_path)
    assert result['merged_documents']==1 and not legacy.exists()
    assert (Path(result['backup'])/'Networking/专题总结.md').read_text()==original
    merged=canonical.read_text()
    assert 'Original synthesis with precise evidence' in merged
    assert 'speech/001.md' in merged and 'transcripts/001.md' in merged
    assert merged.count(docs.MATERIALS_MARKER)==1
    assert not (root/'Networking/专题总结.md').exists()
    assert 'Networking/专题总结.md' not in overall.read_text()
    assert g._json(root/'.recording-summaries.json')['tracks'][gid]['path']=='Networking/Networking.md'
    assert (root/'Networking/speech/001.md').read_bytes()==source
    enriched=g.with_local(data,tmp_path)
    assert enriched['summary_groups'][0]['summary']['url']==enriched['summary_groups'][0]['index_url']
    assert not enriched['overall_summary']['stale']
    s.generate(data=data,output_dir=tmp_path,config={})
    assert len(prompts)==2 and canonical.read_text().count(docs.MATERIALS_MARKER)==1


@pytest.mark.parametrize(('count','target'),[(8,'2400–4500'),(9,'5400–9000'),(25,'5400–9000'),(26,'9000–15000')])
def test_detailed_topic_budget_and_overall_remains_concise(tmp_path,monkeypatch,count,target):
    calls=[]
    def summarize(prompt,**kw):
        calls.append((prompt,kw))
        return 'Evidence-based analysis'
    monkeypatch.setattr(pipeline,'_cached_summarize',summarize)
    s.compose('Local evidence','Topic',{},tmp_path,None,unit_count=count)
    prompt,options=calls[-1]
    assert target in prompt and options['max_tokens']==40000
    assert '正文篇幅不含' in prompt and '方案的比较' in prompt and '不为达到目标篇幅编造' in prompt
    s.compose('Topic summaries','Conference',{},tmp_path,None,overall=True,unit_count=count)
    prompt,options=calls[-1]
    assert '1500–2500' in prompt and options['max_tokens']==20000
    assert target not in prompt and '正文篇幅不含' not in prompt


def test_detailed_topic_reduction_keeps_more_evidence(tmp_path,monkeypatch):
    calls=[]
    def summarize(prompt,**kw):
        calls.append((prompt,kw))
        return 'Preserved facts and contrasting engineering approaches'
    monkeypatch.setattr(pipeline,'_cached_summarize',summarize)
    s.compose('A'*s.CHUNK+'TAIL_SOURCE_EVIDENCE','Topic',{},tmp_path,None,unit_count=30)
    assert len(calls)==3
    assert all('正文最多 5400 字' in prompt for prompt,_ in calls[:2])
    assert 'TAIL_SOURCE_EVIDENCE' in calls[1][0]
    assert '9000–15000' in calls[-1][0] and calls[-1][1]['max_tokens']==40000


def test_previous_length_policy_does_not_reuse_short_summaries(tmp_path,monkeypatch):
    data,_=setup_material(tmp_path)
    prompts=[]
    monkeypatch.setattr(pipeline,'_cached_summarize',lambda prompt,**kw:prompts.append(prompt) or 'Saved synthesis')
    current=s.VERSION
    monkeypatch.setattr(s,'VERSION',current-1)
    s.generate(data=data,output_dir=tmp_path,config={})
    prompts.clear()
    monkeypatch.setattr(s,'VERSION',current)
    result=s.generate(data=data,output_dir=tmp_path,config={})
    assert result['generated']==2 and result['reused']==0
    assert len(prompts)==3
