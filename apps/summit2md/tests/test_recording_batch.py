import json
from pathlib import Path
from unittest.mock import patch

import pytest

from apps.summit2md import recording_batch as batch, recording_guide as guide, recorded_content, pipeline, server, pending_tasks

URL='https://future.example/2031/recordings'


def catalog(tmp_path,n=3):
    tracks=[]
    for group in range(3):
        tracks.append({'id':f'track-{group}','title':f'Theme {group}','count':n,'entries':[
            {'id':f'vid-{group}-{i}','title':f'Talk {group}-{i}','duration':0,'is_raw_session':False,'url':f'https://youtu.be/vid-{group}-{i}'} for i in range(n)]})
    data=recorded_content.normalize_directory({'summit_title':'Future Conference','source_url':URL,'recorded_tracks':tracks})
    guide.save(data,tmp_path)
    selected=[dict(tracks_entry,index=i*3+g+1,want_summary=i%2==0)
              for i in range(n) for g,t in enumerate(data['recorded_tracks']) for tracks_entry in [t['entries'][i]]]
    return data,selected


def test_first_twenty_and_select_all_are_partitioned_without_losing_options(tmp_path):
    data,entries=catalog(tmp_path,12)
    for chosen in (entries[:20],entries):
        plan=batch.partition(tmp_path,'Future Conference - 全部专题',chosen)
        assert len(plan['batches'])==3
        flattened=[e for b in plan['batches'] for e in b['entries']]
        assert {e['id'] for e in flattened}=={e['id'] for e in chosen}
        assert {e['id']:(e['index'],e['want_summary']) for e in flattened}=={e['id']:(e['index'],e['want_summary']) for e in chosen}
        assert Path(plan['root'])==tmp_path/'Future Conference'
        assert all('spark-tracks=' in b['source_url'] and 'spark-track=all' not in b['source_url'] for b in plan['batches'])
    # Saved edits take precedence over entry metadata captured before regrouping.
    data['group_overrides']={t['id']:'Merged Theme' for t in data['recorded_tracks']}
    guide.save(data,tmp_path)
    plan=batch.partition(tmp_path,'name',entries)
    assert len(plan['batches'])==1 and plan['batches'][0]['collection']['group_title']=='Merged Theme'


def processor(calls,*,fail_group='',stop_state=None):
    def run(**params):
        name=params['collection']['group_title'];calls.append(name)
        assert params['do_overall_summary'] is False
        if name==fail_group:
            raise OSError('temporary group failure')
        folder=Path(pipeline.summit_output_path(params['output_base_dir'],params['summit_title'],params['collection']))
        (folder/'transcripts').mkdir(parents=True,exist_ok=True);(folder/'speech').mkdir(exist_ok=True)
        rows={}
        for i,entry in enumerate(params['entries'],1):
            stem=str(entry['index'])+'.md'
            (folder/'transcripts'/stem).write_text('Saved transcript')
            (folder/'speech'/stem).write_text('Saved speech')
            rows[entry['id']]={'entry':entry,'rank':entry['index'],'ok':True,'relative_path':'transcripts/'+stem,
                               'speech_relative_path':'speech/'+stem,'summary':{'tldr':'saved','body':'saved evidence'}}
            params['progress_cb']({'current':i,'total':len(params['entries']),'stage':'processing'})
        pipeline._save_manifest(str(folder),{'entries':rows,'content_type':'summit','collection':params['collection'],'source_url':params['source_url']})
        (folder/(folder.name+'.md')).write_text('# '+params['summit_title']+'\n\n> 本目录由 summit2md 工具自动生成于 2031\n\n## 议题列表\n')
        if stop_state is not None:stop_state['stop']=True
        return {'output_dir':str(folder),'rows':list(rows.values()),'full_rows':list(rows.values()),'failed_entries':[],
                'stopped':False,'unprocessed_entries':[]}
    return run


def run_plan(tmp_path,plan,**options):
    return batch.process_job(batches=plan['batches'],batch_root=plan['root'],summit_title=plan['title'],
        entries=[e for b in plan['batches'] for e in b['entries']],output_base_dir=str(tmp_path),
        source_url=URL,do_overall_summary=False,**options)


def test_processes_groups_sequentially_and_refreshes_nested_materials(tmp_path,monkeypatch):
    data,entries=catalog(tmp_path);plan=batch.partition(tmp_path,'name',entries)
    calls=[];progress=[]
    monkeypatch.setattr(pipeline,'process_job',processor(calls))
    result=run_plan(tmp_path,plan,progress_cb=progress.append)
    assert calls==['Theme 0','Theme 1','Theme 2']
    assert not result['failed_entries'] and len(result['rows'])==9
    assert Path(result['index_path']).is_file()
    assert guide.with_local(data,tmp_path)['local_count']==9
    assert [v['current'] for v in progress if 'current' in v]==sorted(v['current'] for v in progress if 'current' in v)
    for child in plan['batches']:
        folder=Path(child['output_dir'])
        assert (folder/(folder.name+'.md')).is_file()
        assert len(guide._json(folder/'.manifest.json')['entries'])==3


def test_group_failure_continues_and_stop_keeps_remaining_entries(tmp_path,monkeypatch):
    _,entries=catalog(tmp_path);plan=batch.partition(tmp_path,'name',entries)
    calls=[];monkeypatch.setattr(pipeline,'process_job',processor(calls,fail_group='Theme 1'))
    result=run_plan(tmp_path,plan)
    assert calls==['Theme 0','Theme 1','Theme 2']
    assert {e['id'] for e in result['failed_entries']}=={e['id'] for e in plan['batches'][1]['entries']}
    state={'stop':False};calls=[]
    monkeypatch.setattr(pipeline,'process_job',processor(calls,stop_state=state))
    result=run_plan(tmp_path,plan,stop_flag=lambda:state['stop'])
    assert calls==['Theme 0'] and result['stopped']
    assert len(result['unprocessed_entries'])==6


def test_server_accepts_multi_group_task_and_queues_resume_records(tmp_path,monkeypatch):
    _,entries=catalog(tmp_path)
    captured={}
    class Slots:
        def try_acquire(self):return True
        def start(self,fn,jid,params):captured.update(fn=fn,id=jid,params=params)
        def release(self):pass
    monkeypatch.setattr(server,'HEAVY_JOBS',Slots())
    monkeypatch.setattr(server,'_resolve_llm_config',lambda *a,**kw:({'backend':'api','api_key':'ephemeral-test-secret','api_base':'','model':'test'},None))
    with patch.dict(server.JOBS,{},clear=True),patch.dict(server.ACTIVE_OUTPUT_DIRS,{},clear=True):
        response=server.app.test_client().post('/api/run',json={'entries':entries,'summit_title':'Future Conference - 全部专题',
            'source_url':URL+'#spark-track=all','output_dir':str(tmp_path),'do_summary':False,'do_overall_summary':False})
        assert response.status_code==200,response.json
        job=server.JOBS[captured['id']]
        assert job['total']==9 and job['task_out_dir']==str(tmp_path/'Future Conference')
        assert len(captured['params']['batch_groups'])==3
        assert len(pending_tasks.list_pending(tmp_path))==3
        assert 'ephemeral-test-secret' not in pending_tasks.STATE_PATH.read_text()
        assert server._active_job_for_dir(str(tmp_path/'Future Conference/Theme 2'))==job['id']
        calls=[];monkeypatch.setattr(pipeline,'process_job',processor(calls))
        captured['fn'](captured['id'],captured['params'])
        assert job['done'] and job['error'] is None and len(job['result']['groups'])==3
        assert not server.ACTIVE_OUTPUT_DIRS and pending_tasks.list_pending(tmp_path)==[]


def test_real_pipeline_reuses_existing_transcripts_across_all_groups(tmp_path,monkeypatch):
    _,entries=catalog(tmp_path,1);plan=batch.partition(tmp_path,'name',entries)
    calls=[]
    fake=processor(calls)
    for b in plan['batches']:
        fake(entries=b['entries'],collection=b['collection'],source_url=b['source_url'],
             summit_title=b['collection']['conference_title']+' - '+b['collection']['group_title'],
             output_base_dir=str(tmp_path),do_overall_summary=False,progress_cb=lambda k:None)
    speeches={p:p.read_bytes() for p in tmp_path.glob('*/*/speech/*.md')}
    monkeypatch.setattr(pipeline,'_cached_summarize',lambda *a,**kw:pytest.fail('Cached-only run must not call a model'))
    monkeypatch.setattr(pipeline,'download_subtitle',lambda *a,**kw:pytest.fail('Cached-only run must not fetch subtitles'))
    result=run_plan(tmp_path,plan,backend='api',api_key='',model='',lang_prefs=['en'],do_summary=False,skip_existing=True)
    assert not result['failed_entries'] and len(result['rows'])==3, result['groups']
    assert all(p.read_bytes()==body for p,body in speeches.items())
    assert len(list(tmp_path.glob('*/*/speech/*.md')))==3


def test_topic_summary_precedes_next_group_and_overall_uses_summary_model(tmp_path,monkeypatch):
    from apps.summit2md import recording_summaries
    _,entries=catalog(tmp_path,1);plan=batch.partition(tmp_path,'name',entries)
    calls=[];summaries=[]
    monkeypatch.setattr(pipeline,'process_job',processor(calls))
    def summarize(**kwargs):
        if kwargs.get('track_id'):
            assert calls == ['Theme 0','Theme 1','Theme 2'][:len(summaries)+1]
        else:
            assert calls == ['Theme 0','Theme 1','Theme 2']
        summaries.append(kwargs)
    monkeypatch.setattr(recording_summaries,'generate',summarize)
    result=batch.process_job(batches=plan['batches'],batch_root=plan['root'],summit_title=plan['title'],
        entries=entries,output_base_dir=str(tmp_path),source_url=URL,do_overall_summary=True,
        backend='api',model='extract-model',overall_model='summary-model',api_key='temporary')
    assert len(summaries)==4
    assert all(s['config']['model']=='summary-model' for s in summaries)
    assert all(s.get('track_id') for s in summaries[:3]) and not summaries[-1].get('track_id')
    assert result['output_dir']==plan['root']


def test_repeated_cached_batches_do_not_nest_existing_topic_documents(tmp_path,monkeypatch):
    from apps.summit2md import recording_summaries,recording_documents
    data,entries=catalog(tmp_path,1);plan=batch.partition(tmp_path,'name',entries)
    real_process = pipeline.process_job
    monkeypatch.setattr(pipeline,'process_job',processor([]))
    run_plan(tmp_path,plan)
    monkeypatch.setattr(pipeline,'process_job',real_process)
    root=Path(plan['root'])
    for b in plan['batches']:
        folder=Path(b['output_dir']);p=folder/(folder.name+'.md')
        p.write_text(guide.MARKER+'\n'+recording_documents.SUMMARY_MARKER+'\n# '+folder.name+'\n\nImportant original synthesis\n')
        m=guide._json(folder/'.manifest.json');m['overall_summary']=p.read_text();(folder/'.manifest.json').write_text(json.dumps(m))
    monkeypatch.setattr(pipeline,'_cached_summarize',lambda *a,**kw:pytest.fail('No model call'))
    params=dict(backend='api',api_key='',model='',lang_prefs=['en'],do_summary=False,skip_existing=True)
    run_plan(tmp_path,plan,**params)
    docs={p:p.read_text() for p in root.glob('*/*.md')}
    run_plan(tmp_path,plan,**params)
    assert docs and all(p.read_text()==text for p,text in docs.items())
    assert all(text.count(recording_documents.SUMMARY_MARKER)==1 for text in docs.values())


def test_batch_picker_uses_saved_large_topics_and_only_loads_selected_groups(tmp_path,monkeypatch):
    data,entries=catalog(tmp_path,2)
    data['group_overrides']={data['recorded_tracks'][0]['id']:'Merged Large Topic',
                             data['recorded_tracks'][1]['id']:'Merged Large Topic'}
    guide.save(data,tmp_path)
    monkeypatch.setattr(recorded_content,'discover',lambda *a,**kw:pytest.fail('saved catalog must not be fetched again'))
    client=server.app.test_client()
    payload={'source':URL,'output_dir':str(tmp_path)}
    response=client.post('/api/recording_batches',json=payload)
    assert response.status_code==200,response.json
    options=response.json['groups']
    assert [(g['title'],g['count']) for g in options]==[('Merged Large Topic',4),('Theme 2',2)]
    assert len(options[0]['tracks'])==2
    response=client.post('/api/recording_batches',json={**payload,'group_ids':[options[0]['id']]})
    assert response.status_code==200,response.json
    selected=response.json['entries']
    assert len(selected)==4 and response.json['batch_group_count']==1
    assert {e['collection']['group_title'] for e in selected}=={'Merged Large Topic'}
    assert [e['index'] for e in selected]==[1,2,3,4]
    plan=batch.partition(tmp_path,response.json['summit_title'],selected)
    assert len(plan['batches'])==1
    assert plan['batches'][0]['collection']['group_title']=='Merged Large Topic'
    for ids in ([],['stale-group-id'],True):
        assert client.post('/api/recording_batches',json={**payload,'group_ids':ids}).status_code==400
    assert client.post('/api/recording_batches',json={**payload,'source':URL+'/unknown'}).status_code==404


def test_batch_picker_selects_all_groups_for_one_serial_job(tmp_path,monkeypatch):
    data,entries=catalog(tmp_path,2)
    client=server.app.test_client();body={'source':URL,'output_dir':str(tmp_path)}
    options=client.post('/api/recording_batches',json=body).json['groups']
    response=client.post('/api/recording_batches',json={**body,'group_ids':[g['id'] for g in options]})
    assert response.status_code==200,response.json
    selection=response.json
    captured={}
    class Slots:
        def try_acquire(self):return True
        def start(self,fn,jid,params):captured.update(fn=fn,id=jid,params=params)
        def release(self):pass
    monkeypatch.setattr(server,'HEAVY_JOBS',Slots())
    monkeypatch.setattr(server,'_resolve_llm_config',lambda *a,**kw:({'backend':'api','api_key':'test','api_base':'','model':'test'},None))
    with patch.dict(server.JOBS,{},clear=True),patch.dict(server.ACTIVE_OUTPUT_DIRS,{},clear=True):
        started=client.post('/api/run',json={**selection,'output_dir':str(tmp_path),
            'do_summary':False,'do_overall_summary':False})
        assert started.status_code==200,started.json
        assert len(captured['params']['batch_groups'])==3
        calls=[];monkeypatch.setattr(pipeline,'process_job',processor(calls))
        captured['fn'](captured['id'],captured['params'])
        assert calls==['Theme 0','Theme 1','Theme 2']
        job=server.JOBS[captured['id']]
        assert job['done'] and not job['error']
        assert len(job['result']['rows'])==6


def test_group_selection_deduplicates_shared_recordings_without_mutating_catalog(tmp_path):
    data,_=catalog(tmp_path,1)
    data['recorded_tracks'][1]['entries'].append(dict(data['recorded_tracks'][0]['entries'][0]))
    data['recorded_tracks'][1]['count']=2
    before=json.dumps(data,sort_keys=True)
    selected=batch.select_groups(data,[g['id'] for g in batch.group_options(data)])
    assert len(selected['entries'])==3
    assert json.dumps(data,sort_keys=True)==before


def test_running_server_fallback_has_same_group_selection_as_backend(tmp_path):
    import subprocess
    data,_=catalog(tmp_path,2)
    data['group_overrides']={t['id']:('Merged' if i<2 else 'Other') for i,t in enumerate(data['recorded_tracks'])}
    data=recorded_content.normalize_directory(data)
    source=(Path(__file__).parents[1]/'static/app.js').read_text()
    function=source[source.index('  function batchCatalogFromDirectory('):source.index('  let legacyBatchCatalog =')]
    selected=[g['id'] for g in batch.group_options(data)]
    script=function+'\nconsole.log(JSON.stringify(batchCatalogFromDirectory('+json.dumps(data)+','+json.dumps(selected)+')));'
    result=subprocess.run(['node','-e',script],text=True,capture_output=True,check=True)
    browser=json.loads(result.stdout)
    assert browser['groups']==batch.group_options(data)
    expected=batch.select_groups(data,selected)
    assert browser['entries']==expected['entries']
    assert browser['batch_group_count']==expected['batch_group_count']
    assert browser['source_url']==expected['source_url']


def test_topics_saved_before_next_batch_without_conference_summary(tmp_path,monkeypatch):
    from apps.summit2md import recording_summaries, recording_documents
    _,entries=catalog(tmp_path,1);plan=batch.partition(tmp_path,'name',entries)
    events=[]
    fake=processor([])
    def process(**kw):
        name=kw['collection']['group_title']
        if events:
            previous=Path(plan['batches'][len(events)//2-1]['output_dir'])
            assert recording_documents.SUMMARY_MARKER in (previous/(previous.name+'.md')).read_text()
        events.append('download '+name)
        return fake(**kw)
    def compose(text,label,*args,**kw):
        events.append('summary '+label)
        return 'Evidence-based synthesis for '+label
    monkeypatch.setattr(pipeline,'process_job',process)
    monkeypatch.setattr(recording_summaries,'compose',compose)
    result=run_plan(tmp_path,plan,do_topic_summary=True,overall_model='summary-model')
    assert events==[event for i in range(3) for event in (f'download Theme {i}',f'summary Theme {i}')]
    assert all(g['summary']=='saved' for g in result['groups'])
    assert not (Path(plan['root'])/'大会总结.md').exists()
    # Unchanged local evidence reuses persisted topic summaries.
    monkeypatch.setattr(pipeline,'process_job',fake)
    monkeypatch.setattr(recording_summaries,'compose',lambda *a,**k:pytest.fail('unchanged summary must be reused'))
    result=run_plan(tmp_path,plan,do_topic_summary=True)
    assert all(g['summary']=='saved' for g in result['groups'])


def test_topic_summary_failure_keeps_material_and_continues(tmp_path,monkeypatch):
    from apps.summit2md import recording_summaries
    _,entries=catalog(tmp_path,1);plan=batch.partition(tmp_path,'name',entries)
    calls=[];summaries=[]
    monkeypatch.setattr(pipeline,'process_job',processor(calls))
    def summarize(**kw):
        summaries.append(kw['track_id'])
        if len(summaries)==1:raise OSError('temporary model failure')
    monkeypatch.setattr(recording_summaries,'generate',summarize)
    result=run_plan(tmp_path,plan,do_topic_summary=True)
    assert len(calls)==len(summaries)==3
    assert result['groups'][0]['summary_error']=='temporary model failure'
    assert not result['failed_entries'] and len(result['rows'])==3
    assert result['groups'][1]['summary']=='saved'


def test_single_topic_batch_routes_through_topic_summary_and_persists_resume_option(tmp_path,monkeypatch):
    from apps.summit2md import recording_summaries
    _,entries=catalog(tmp_path,1)
    captured={}
    class Slots:
        def try_acquire(self):return True
        def start(self,fn,jid,params):captured.update(fn=fn,id=jid,params=params)
        def release(self):pass
    monkeypatch.setattr(server,'HEAVY_JOBS',Slots())
    def resolve(data,needs):
        assert needs is True
        return {'backend':'api','api_key':'test','api_base':'','model':'test'},None
    monkeypatch.setattr(server,'_resolve_llm_config',resolve)
    with patch.dict(server.JOBS,{},clear=True),patch.dict(server.ACTIVE_OUTPUT_DIRS,{},clear=True):
        response=server.app.test_client().post('/api/run',json={'entries':entries[:1],'summit_title':'Future Conference',
            'source_url':URL,'output_dir':str(tmp_path),'do_summary':False,'do_overall_summary':False,'do_topic_summary':True})
        assert response.status_code==200,response.json
        assert len(captured['params']['batch_groups'])==1
        assert server.JOBS[captured['id']]['config']['do_topic_summary'] is True
        pending=pending_tasks.list_pending(tmp_path)
        assert len(pending)==1
        calls=[];summaries=[]
        monkeypatch.setattr(pipeline,'process_job',processor(calls))
        monkeypatch.setattr(recording_summaries,'generate',lambda **kw:summaries.append(kw))
        captured['fn'](captured['id'],captured['params'])
        assert len(summaries)==1 and summaries[0]['track_id']
        assert server.JOBS[captured['id']]['error'] is None


def test_repeated_real_batches_do_not_copy_the_whole_conference(tmp_path,monkeypatch):
    """每跑一批都把专题索引写回旧格式，归档时就当成旧版索引、把整个大会目录备份一份。"""
    from apps.summit2md import recording_documents
    _,entries=catalog(tmp_path,1);plan=batch.partition(tmp_path,'name',entries)
    fake=processor([])
    for b in plan['batches']:
        fake(entries=b['entries'],collection=b['collection'],source_url=b['source_url'],
             summit_title=b['collection']['conference_title']+' - '+b['collection']['group_title'],
             output_base_dir=str(tmp_path),do_overall_summary=False,progress_cb=lambda k:None)
    run_plan(tmp_path,plan,backend='api',api_key='',model='',lang_prefs=['en'],do_summary=False,skip_existing=True)
    backups=tmp_path/'.organization-backups'
    before={p.name for p in backups.iterdir()} if backups.exists() else set()
    monkeypatch.setattr(pipeline,'_cached_summarize',lambda *a,**kw:pytest.fail('no model call'))
    monkeypatch.setattr(pipeline,'download_subtitle',lambda *a,**kw:pytest.fail('no subtitle fetch'))
    for _ in range(2):
        run_plan(tmp_path,plan,backend='api',api_key='',model='',lang_prefs=['en'],do_summary=False,skip_existing=True)
    after={p.name for p in backups.iterdir()} if backups.exists() else set()
    assert not [n for n in after-before if n.startswith('分组归档-')]
    root=Path(plan['root'])
    for index in root.glob('*/*.md'):
        if index.stem == index.parent.name:
            text=index.read_text()
            assert recording_documents.LEGACY_TOOL_MARKER not in text
            assert text.count(recording_documents.MATERIALS_MARKER)==1
