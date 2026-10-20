import json
from pathlib import Path
from urllib.parse import unquote

import pytest

from apps.summit2md import recording_organizer as o, recording_guide as g, recording_groups as groups, pipeline
from apps.summit2md.tests.test_recording_guide import directory, SOURCE


def fixture(tmp_path):
    data=directory();data['recorded_tracks']=data['recorded_tracks'][:1]
    root=g.guide_directory(tmp_path,SOURCE);(root/'speech').mkdir(parents=True);(root/'transcripts').mkdir()
    (root/'speech/001.md').write_text('# Original speech\n[Transcript](../transcripts/001.md)')
    (root/'transcripts/001.md').write_text('# Original transcript')
    (root/(root.name+'.md')).write_text('# Old index\n[Speech](speech/001.md)')
    (root/'.manifest.json').write_text(json.dumps({'entries':{'z1xRELMKzrA':{
        'entry':data['recorded_tracks'][0]['entries'][0],'rank':1,'ok':True,
        'speech_relative_path':'speech/001.md','relative_path':'transcripts/001.md',
        'summary':{'tldr':'Saved summary','body':'Saved details'}}},'content_type':'summit'}))
    g.save(data,tmp_path)
    return root,data


def test_physical_filing_preserves_sources_and_resume_metadata(tmp_path):
    root,data=fixture(tmp_path)
    plan=o.plan(data,tmp_path);result=o.apply(plan,tmp_path/'backup')
    assert result['files']==2
    assert (root/'Networking/speech/001.md').read_text()=='# Original speech\n[Transcript](../transcripts/001.md)'
    assert not (root/'speech').exists()
    assert (tmp_path/'backup/speech/001.md').is_file()
    old=(root/'旧版产物'/f'{root.name}.md').read_text()
    assert '../Networking/speech/001.md' in unquote(old)
    aggregate=g._json(root/'.manifest.json')
    assert aggregate['entries']['z1xRELMKzrA']['speech_relative_path']=='Networking/speech/001.md'
    child=g._json(root/'Networking/.manifest.json')
    row=child['entries']['z1xRELMKzrA']
    assert row['rank']==1 and row['speech_relative_path']=='speech/001.md'
    imported=pipeline.import_output_directory(str(root/'Networking'))
    assert Path(pipeline.summit_output_path(str(root),imported['summit_title'],child['collection']))==root/'Networking'
    assert g.local_materials(tmp_path)['z1xRELMKzrA']['path']==str(root/'Networking/speech/001.md')
    # Repeating filing is a no-op for assets, rather than duplicating them.
    assert o.plan(data,tmp_path)['moves']=={}
    assert g.index_filename(root)=='大会目录.md'


def test_cross_listed_video_has_one_physical_owner_and_collision_is_rejected(tmp_path):
    root,data=fixture(tmp_path)
    data['recorded_tracks'].append({**data['recorded_tracks'][0],'id':'other','title':'Other'})
    assert len(o.plan(data,tmp_path)['moves']) == 2
    assert (root/'speech/001.md').is_file()
    data['recorded_tracks'].pop()
    target=root/'Networking/speech';target.mkdir(parents=True);(target/'001.md').write_text('User file')
    with pytest.raises(ValueError,match='未覆盖'):o.plan(data,tmp_path)
    assert (target/'001.md').read_text()=='User file'


def test_merged_source_restores_only_its_official_tracks():
    from apps.summit2md import recorded_content as rc
    from apps.summit2md.tests.test_recorded_content import HTML,URL
    source=URL+'#'+o.urlencode({'spark-tracks':json.dumps(['networking']),'spark-label':'Systems'})
    data=rc.parse_directory(HTML,source)
    assert len(data['entries'])==2 and data['summit_title'].endswith(' - Systems')
    assert all(e['collection']['group_title']=='Systems' for e in data['entries'])
    with pytest.raises(RuntimeError,match='部分议程'):
        rc.parse_directory(HTML,URL+'#'+o.urlencode({'spark-tracks':json.dumps(['gone'])}))


def test_legacy_links_with_spaces_parentheses_and_titles(tmp_path):
    before = tmp_path/'Conference/index.md'
    after = tmp_path/'Conference/旧版产物/index.md'
    src = tmp_path/'Conference/speech/001_AI Systems (Part (1)).md'
    dest = tmp_path/'Conference/工程/speech'/src.name
    moves = {src.resolve():dest}
    href = 'speech/'+src.name
    text = f'[Raw]({href})\n[Angle](<{href}> "Read speech")\n[Web](https://example.com/a_(b))'
    updated = o.rewrite_links(text,before,after,moves)
    expected = '../工程/speech/'+src.name
    assert f'[Raw]({expected})' in unquote(updated)
    assert f'[Angle](<{expected}> "Read speech")' in unquote(updated)
    assert '[Web](https://example.com/a_(b))' in updated


def test_regroup_already_filed_partial_outputs_preserves_resume_and_links(tmp_path):
    root,data=fixture(tmp_path)
    first=o.organize(data=data,output_dir=tmp_path)
    assert first['files']==2
    backups=list((tmp_path/'.organization-backups').iterdir())
    data['group_overrides']={'networking':'Systems'}
    result=o.organize(data=data,output_dir=tmp_path)
    assert result['files']==2
    assert not (root/'Networking/.manifest.json').exists()
    assert (root/'旧版产物/Networking/.manifest.json').exists()
    child=g._json(root/'Systems/.manifest.json')
    assert list(child['entries'])==['z1xRELMKzrA']
    assert (root/'Systems/speech/001.md').read_text().endswith('(../transcripts/001.md)')
    old=(root/'旧版产物'/f'{root.name}.md').read_text()
    assert '../Systems/speech/001.md' in unquote(old)
    assert g.local_materials(tmp_path)['z1xRELMKzrA']['path']==str(root/'Systems/speech/001.md')
    assert o.organize(data=data,output_dir=tmp_path)['files']==0
    assert len(list((tmp_path/'.organization-backups').iterdir()))==len(backups)+1
    # New partial output in a child folder must supersede the root aggregate's older copy.
    child['entries']['z1xRELMKzrA']['summary']['body']='Updated summary evidence'
    (root/'Systems/.manifest.json').write_text(json.dumps(child))
    o.organize(data=data,output_dir=tmp_path)
    assert g.local_materials(tmp_path)['z1xRELMKzrA']['summary_text'].endswith('Updated summary evidence')


def test_disjoint_partial_children_are_consolidated_without_root_manifest(tmp_path):
    root,data=fixture(tmp_path)
    o.organize(data=data,output_dir=tmp_path)
    # Move the root manifest aside to emulate a conference processed group by group.
    (root/'.manifest.json').rename(tmp_path/'saved-parent.json')
    data['recorded_tracks'].append({'id':'unprocessed','title':'Later Research','count':1,
                                   'entries':[{'id':'future-video','title':'Future talk','url':'https://youtu.be/future-video'}]})
    result=o.organize(data=data,output_dir=tmp_path)
    assert result['files']==0
    assert len(g._json(root/'.manifest.json')['entries'])==1
    assert (root/'Networking/speech/001.md').is_file()


def test_failed_move_restores_original_tree_and_preserves_failure_snapshot(tmp_path):
    root,data=fixture(tmp_path)
    plan=o.plan(data,tmp_path)
    def fail_after_one(pairs):
        source,target=pairs[0];source.rename(target)
        raise OSError('Interrupted disk operation')
    with pytest.raises(OSError,match='Interrupted'):
        o.apply(plan,tmp_path/'backup',move_batch=fail_after_one)
    assert (root/'speech/001.md').read_text().startswith('# Original speech')
    assert (root/'transcripts/001.md').is_file()
    assert (tmp_path/'backup/归档失败现场').is_dir()
    assert o.plan(data,tmp_path)['moves']==plan['moves']


def test_requested_missing_speech_follows_new_group_in_pending_list(tmp_path):
    from apps.summit2md import pending_tasks
    root,data=fixture(tmp_path)
    entry=data['recorded_tracks'][0]['entries'][0]
    pending_tasks.remember(str(root),{'content_type':'summit','summit_title':root.name,
                                     'do_summary':True,'do_speech_script':True,'do_overall_summary':True},[entry])
    o.organize(data=data,output_dir=tmp_path)
    records=json.loads(pending_tasks.STATE_PATH.read_text())
    assert len(records)==1
    record=next(iter(records.values()))
    assert record['path']==str(root/'Networking')
    assert record['config']['do_speech_script'] and not record['config']['do_overall_summary']
    assert record['config']['source_url'].split('#')[1].startswith('spark-tracks=')
    assert record['entries'][0]['id']==entry['id']


def test_corrupt_or_foreign_manifest_is_not_overwritten(tmp_path):
    root,data=fixture(tmp_path)
    saved=(root/'.manifest.json').read_text()
    (root/'.manifest.json').write_text('{damaged')
    with pytest.raises(ValueError,match='记录损坏'):
        o.organize(data=data,output_dir=tmp_path)
    assert (root/'.manifest.json').read_text()=='{damaged'
    (root/'.manifest.json').write_text(saved)
    foreign=root/'Networking';foreign.mkdir()
    (foreign/'.manifest.json').write_text(json.dumps({'entries':{},'collection':{'conference_title':'Another event'}}))
    with pytest.raises(ValueError,match='其他来源'):
        o.organize(data=data,output_dir=tmp_path)
    assert (root/'speech/001.md').exists()


def test_legacy_playlist_topic_folders_can_enter_grouping_without_losing_summary(tmp_path):
    from apps.summit2md import recording_documents as docs
    root,data=fixture(tmp_path)
    o.organize(data=data,output_dir=tmp_path)
    folder=root/'Networking'
    manifest=g._json(folder/'.manifest.json');manifest.pop('collection')
    (folder/'.manifest.json').write_text(json.dumps(manifest))
    index=folder/'Networking.md'
    index.write_text('# '+data['summit_title']+' - Networking\n\n'+docs.LEGACY_TOOL_MARKER+
                     '2029-01-01\n\n## 大会总结\n\nLegacy valuable synthesis\n\n## 议题列表（共 1 个）\n\nOld generated table')
    result=o.organize(data=data,output_dir=tmp_path)
    assert result.get('backup')
    # 没有文件要移动时只备份被改写的索引，不再整份复制大会目录
    from pathlib import Path as _P
    saved = sorted(p.relative_to(result['backup']) for p in _P(result['backup']).rglob('*') if p.is_file())
    assert [str(p) for p in saved] == ['Networking/Networking.md']
    assert 'Legacy valuable synthesis' in index.read_text()
    assert 'Old generated table' not in index.read_text()
    assert 'speech/001.md' in index.read_text()
    assert g._json(folder/'.manifest.json')['collection']['group_title']=='Networking'
    assert not o.organize(data=data,output_dir=tmp_path).get('backup')
