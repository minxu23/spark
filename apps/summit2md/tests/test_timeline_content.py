from apps.summit2md import recorded_content as rc


def test_nested_session_workshop_and_public_title_are_preserved():
    raw = b'''<title>Example Summit - Site</title><meta property="og:title" content="Example Summit">
    <h1>Watch recordings</h1><div class="agenda-tab">Atlas - Saturday</div>
    <div id="agenda-track-0"><div class="timeline">
    <div class="session-block-wrap"><div class="session-header"><div class="session-title">Session 1: Research</div></div>
    <div class="event"><div class="event-title">Talk</div><a href="https://youtu.be/z1xRELMKzrA">[Video]</a></div></div>
    <div class="event workshop"><div class="event-title">Workshop: Tools</div><a href="https://youtu.be/CS5Iu94q5jE">[Video]</a></div>
    </div></div>'''
    d=rc.parse_directory(raw,'https://conference.example/summit')
    assert d['summit_title']=='Example Summit'
    assert [(t['agenda_session'],t['count']) for t in d['recorded_tracks']] == [('Research',1),('Workshop: Tools',1)]
    assert all(t['official_anchor']=='agenda-track-0' for t in d['recorded_tracks'])


def test_existing_conference_index_is_preserved_and_overview_has_no_separate_summary(tmp_path,monkeypatch):
    import json
    from apps.summit2md import recording_guide as g, recording_summaries as s, pipeline
    from apps.summit2md.tests.test_recording_guide import directory, SOURCE
    data=directory()
    data['overview_track_ids']=['ai']
    data['group_overrides']={'ai':'大会导读'}
    folder=g.guide_directory(tmp_path,SOURCE);folder.mkdir()
    original=folder/(folder.name+'.md');original.write_text('# Existing index and summary')
    speech=folder/'speech.md';speech.write_text('Full transcript')
    (folder/'.manifest.json').write_text(json.dumps({'entries':{'z1xRELMKzrA':{
        'entry':{'id':'z1xRELMKzrA'},'speech_relative_path':'speech.md',
        'summary':{'tldr':'Existing summary','body':'REUSE_EXISTING_SUMMARY'}}}}))
    prompts=[]
    monkeypatch.setattr(pipeline,'_cached_summarize',lambda prompt,**kw:prompts.append(prompt) or 'Generated insight')
    result=s.generate(data=data,output_dir=tmp_path,config={})
    assert result['tracks']==1 and len(prompts)==2
    assert 'REUSE_EXISTING_SUMMARY' in prompts[0] and 'Full transcript' not in prompts[0]
    assert '大会导读素材' in prompts[1]
    assert not (folder/'大会导读/专题总结.md').exists()
    # 自建的同名文件挪进旧版产物，同名 md 改由工具维护：大会总结 + 专题目录
    assert g.MARKER in original.read_text() and '## 大会总结' in original.read_text()
    assert not (folder/'大会目录.md').exists() and not (folder/'大会总结.md').exists()
    assert (folder/'旧版产物'/original.name).read_text()=='# Existing index and summary'
    enriched=g.with_local(data,tmp_path)
    assert len(enriched['summary_groups'])==1 and len(enriched['overview_groups'])==1
    assert not enriched['overall_summary']['stale']
    offline=g.with_local(data,tmp_path,offline=True)
    assert offline['recorded_tracks'][0]['entries'][0]['local']['url']==g.reader_url(folder/'Networking/speech/speech.md',offline=True)
