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
    overall = (path / (path.name + '.md')).read_text()   # 大会总结写在会议同名 md 里，下面接专题目录
    assert not (path / '大会总结.md').exists()
    assert overall.index(s.documents.CONF_SUMMARY_MARKER) < overall.index(s.documents.CONF_DIRECTORY_MARKER)
    assert overall.count('## 大会总结') == 1 and '## 总结与资料分组' in overall
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
    # 大会导航里临时改过的模型优先（重试时用这个，不悄悄换回设置里的）
    assert client.post('/api/recording_summary', json={**args, 'model':'retry-model'}).status_code == 200
    assert captured['model'] == 'retry-model'
    assert client.post('/api/recording_summary', json={**args, 'model':'bad\nmodel'}).status_code == 400
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
    overall=root/(root.name+'.md')
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
def test_detailed_topic_budget_and_overall_uses_classic_sections(tmp_path,monkeypatch,count,target):
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
    # 大会总结沿用 Summit 一直以来的小节和篇幅（跟播放列表生成的大会总结一致）
    assert '不少于 3000 字' in prompt and options['max_tokens']==20000
    for heading in ('## 大会概览', '## 主要趋势与共识', '## 值得关注的演讲', '## 分主题看点'):
        assert heading in prompt
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



def _group_ids(data):
    return [grp['id'] for grp in s.recording_groups.groups(g.recorded_content.normalize_directory(data)['recorded_tracks'])]


def test_track_ids_only_generate_those_topics_and_reuse_others_for_conference(tmp_path, monkeypatch):
    data, _ = setup_material(tmp_path)
    prompts = []
    monkeypatch.setattr(pipeline, '_cached_summarize', lambda prompt, **kw: prompts.append(prompt) or f'TOPIC_{len(prompts)}')
    first, second = _group_ids(data)[:2]
    s.generate(data=data, output_dir=str(tmp_path), track_id=first, config={})
    assert len(prompts) == 1
    result = s.generate(data=data, output_dir=str(tmp_path), track_ids=[second], config={})
    # 只生成本次的专题 + 大会总结；第一个专题沿用已有总结，没有资料的专题不补
    assert result['generated'] == 1 and len(prompts) == 3
    assert 'TOPIC_1' in prompts[-1] and 'TOPIC_2' in prompts[-1]
    state = g._json(g.guide_directory(tmp_path, SOURCE) / '.recording-summaries.json')
    assert set(state['tracks']) == {first, second} and state['overall']['covered'] == 2


def test_topic_summary_failure_is_recorded_for_navigation_and_cleared_on_retry(tmp_path, monkeypatch):
    data, _ = setup_material(tmp_path)
    first = _group_ids(data)[0]
    def broken(prompt, **kw):
        raise pipeline.SummarizeError('模型超时')
    monkeypatch.setattr(pipeline, '_cached_summarize', broken)
    with pytest.raises(pipeline.SummarizeError):
        s.generate(data=data, output_dir=str(tmp_path), track_id=first, config={})
    enriched = g.with_local(data, str(tmp_path))
    failed = [grp for grp in enriched['summary_groups'] if grp.get('summary_failure')]
    assert [grp['id'] for grp in failed] == [first] and '模型超时' in failed[0]['summary_failure']['error']
    monkeypatch.setattr(pipeline, '_cached_summarize', lambda prompt, **kw: 'Recovered summary')
    s.generate(data=data, output_dir=str(tmp_path), track_id=first, config={})
    assert not any(grp.get('summary_failure') for grp in g.with_local(data, str(tmp_path))['summary_groups'])


def test_existing_conference_summary_and_directory_merge_into_same_name_document(tmp_path, monkeypatch):
    """已有的「大会总结.md」「大会目录.md」（OCP、Agentic 的现状）并进会议同名 md，不重新调用模型。"""
    from apps.summit2md import recording_organizer as organizer, recording_documents as docs
    data, _ = setup_material(tmp_path)
    prompts = []
    monkeypatch.setattr(pipeline, '_cached_summarize', lambda prompt, **kw: prompts.append(prompt) or '## 核心判断\n\nKEEP_THIS_CONFERENCE_INSIGHT')
    s.generate(data=data, output_dir=str(tmp_path), config={})
    root = g.guide_directory(tmp_path, SOURCE)
    named = root / (root.name + '.md')
    # 还原成旧布局：大会总结单独一个文件，目录叫「大会目录.md」，专题文档链到这两个文件
    state = g._json(root / '.recording-summaries.json')
    legacy_summary = (g.MARKER + '\n# ' + data['summit_title'] + ' · 大会总结\n\n[返回大会目录](%E5%A4%A7%E4%BC%9A%E7%9B%AE%E5%BD%95.md)\n\n'
                      '## 核心判断\n\nKEEP_THIS_CONFERENCE_INSIGHT\n\n## 专题总结索引\n\n- [Networking](Networking/Networking.md)\n')
    (root / '大会总结.md').write_text(legacy_summary)
    (root / '大会目录.md').write_text(g.MARKER + '\n# old directory\n[阅读大会总结](大会总结.md)\n')
    named.unlink()
    (root / '旧版产物').mkdir(exist_ok=True)
    (root / '旧版产物' / named.name).write_text('# playlist-era index')
    topic = root / 'Networking' / 'Networking.md'
    topic.write_text(topic.read_text().replace('\n## 包含的官网议程', '\n[旧目录](../%E5%A4%A7%E4%BC%9A%E7%9B%AE%E5%BD%95.md) '
                     '[旧总结](../%E5%A4%A7%E4%BC%9A%E6%80%BB%E7%BB%93.md)\n\n## 包含的官网议程', 1))
    state['overall']['path'] = '大会总结.md'
    (root / '.recording-summaries.json').write_text(json.dumps(state))
    result = organizer.organize(data=data, output_dir=str(tmp_path))
    assert result.get('backup')
    assert not (root / '大会总结.md').exists() and not (root / '大会目录.md').exists()
    text = named.read_text()
    assert text.count('## 大会总结') == 1 and 'KEEP_THIS_CONFERENCE_INSIGHT' in text
    assert '### 核心判断' in text                       # 并到「## 大会总结」下面降一级
    assert text.index(docs.CONF_SUMMARY_MARKER) < text.index(docs.CONF_DIRECTORY_MARKER)
    linked = unquote(topic.read_text())
    assert '大会目录.md' not in linked and '大会总结.md' not in linked and '[旧总结](../' + named.name in linked
    assert g._json(root / '.recording-summaries.json')['overall']['path'] == named.name
    assert not g.with_local(data, str(tmp_path))['overall_summary']['stale']
    backup = Path(result['backup'])
    assert 'KEEP_THIS_CONFERENCE_INSIGHT' in (backup / '大会总结.md').read_text()
    calls = len(prompts)
    s.generate(data=data, output_dir=str(tmp_path), config={})
    assert len(prompts) == calls                      # 内容和标准都没变：复用，不重新生成
    assert named.read_text().count('## 大会总结') == 1
