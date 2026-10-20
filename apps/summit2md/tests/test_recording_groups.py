from pathlib import Path

from apps.summit2md import pipeline, recording_groups as g, recording_guide, recording_summaries as summaries
from apps.summit2md.tests.test_recording_guide import directory, SOURCE


def track(tid, title, ids):
    return {'id':tid, 'title':title, 'count':len(ids), 'entries':[
        {'id':eid, 'title':eid, 'url':'https://youtu.be/' + eid} for eid in ids]}


def test_merges_related_tracks_preserving_agenda_and_deduplicating():
    tracks = [track('fts-ai','FTS: AI / HPC',['a','b']), track('fts-q','FTS: Quantum (Data Center Design)',['b','c']),
              track('fti-a','FTI Workshop: Data Centric Computing',['d']),
              track('fti-b','FTI Workshop: Short Reach Optical Interconnects',['e']),
              track('server','Server',['f']), track('server-c','Server: Open Chiplet Economy',['g'])]
    groups = g.groups(tracks)
    assert [(t['title'],t['count']) for t in groups] == [('FTS',3),('FTI Workshop',2),('Server',2)]
    assert [t['title'] for t in groups[0]['agenda_tracks']] == [t['title'] for t in tracks[:2]]
    assert g.resolve_group(tracks,'fts-q')['id'] == groups[0]['id']
    assert tracks[0]['entries'] == track('fts-ai','FTS: AI / HPC',['a','b'])['entries']


def test_future_runs_and_resumes_share_nested_folder(tmp_path):
    for event in ('Cloud Summit 2028', '开发者大会 2029', 'Energy - Research Summit'):
        for name in ('Keynote', 'Research', 'Systems'):
            ctx = {'conference_title':event, 'group_title':name}
            title = event + ' - ' + name
            expected = str(tmp_path / event / name)
            assert pipeline.summit_output_path(str(tmp_path), title, ctx) == expected
            assert pipeline.summit_output_path(str(tmp_path / event), title, ctx) == expected
    assert pipeline.summit_output_path(str(tmp_path),'Other Conference') == str(tmp_path/'Other Conference')


def test_selecting_one_agenda_generates_entire_merged_group(tmp_path, monkeypatch):
    data = directory()
    data['recorded_tracks'] = [track('ai','FTS: AI / HPC',['videoone']),track('quantum','FTS: Quantum',['videotwo'])]
    local = {}
    for eid in ('videoone','videotwo'):
        path = tmp_path / (eid+'.md'); path.write_text(eid + ' evidence')
        local[eid] = {'path':str(path),'label':'整理稿'}
    monkeypatch.setattr(recording_guide, 'local_materials', lambda *args:local)
    prompts = []
    monkeypatch.setattr(pipeline, '_cached_summarize', lambda prompt, **kwargs:prompts.append(prompt) or 'summary')
    summaries.generate(data=data,output_dir=str(tmp_path),track_id='ai',config={})
    assert len(prompts) == 1 and 'videoone evidence' in prompts[0] and 'videotwo evidence' in prompts[0]
    path = recording_guide.guide_directory(tmp_path,SOURCE)/'FTS/FTS.md'
    assert path.is_file() and 'FTS: Quantum' in path.read_text()
    enriched = recording_guide.with_local(data,str(tmp_path))
    assert len(enriched['recorded_tracks']) == 2 and len(enriched['summary_groups']) == 1
    assert enriched['recorded_tracks'][0]['summary_group_id'] == enriched['recorded_tracks'][1]['summary_group_id']
    assert not enriched['summary_groups'][0]['summary']['stale']


def test_nested_materials_remain_searchable(tmp_path):
    import json
    folder=tmp_path/'2026 OCP Global Summit/Keynote'
    folder.mkdir(parents=True)
    (folder/'speech.md').write_text('evidence')
    (folder/'.manifest.json').write_text(json.dumps({'entries':{'x':{'entry':{'id':'x'},'speech_relative_path':'speech.md'}}}))
    assert recording_guide.local_materials(tmp_path)['x']['path'] == str(folder/'speech.md')
