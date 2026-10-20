import copy
import json

import pytest

from apps.summit2md import recording_grouping as grouping, recording_groups, recorded_content, pipeline


def catalog():
    tracks = []
    for i in range(16):
        tracks.append({'id':str(i),'title':f'Workshop {i}','count':2,'entries':[
            {'id':f'video-{i}-{j}','title':f'Research result {i}-{j}','url':'https://example.com/video'} for j in range(2)]})
    return {'source_url':'https://conference.example/recordings','summit_title':'Future Summit', 'recorded_tracks':tracks}


def test_semantic_suggestions_merge_small_tracks_and_do_not_apply(tmp_path,monkeypatch):
    data=catalog();original=copy.deepcopy(data)
    result={'groups':[{'title':name,'track_ids':[str(i) for i in range(start,start+4)]}
                       for name,start in [('Research',0),('Engineering',4),('Applications',8),('Governance',12)]]}
    prompts=[]
    monkeypatch.setattr(pipeline,'_cached_summarize',lambda prompt,**kw:prompts.append(prompt) or json.dumps(result))
    assert grouping.fragmented(data)
    proposed=grouping.suggest(data=data,output_dir=tmp_path,config={})
    assert len(proposed['groups'])==4 and len(proposed['assignments'])==16
    assert 'Fireside' in prompts[0] and 'Research result' in prompts[0]
    assert data==original and not list(tmp_path.rglob('.recordings.json'))
    assert not grouping.fragmented({**data,'group_overrides':proposed['assignments']})


@pytest.mark.parametrize('bad',[
    {'groups':[{'title':'Research','track_ids':['0']}]},
    {'groups':[{'title':'Research','track_ids':['0','0']}]},
    {'groups':[{'title':'../Oops','track_ids':[str(i) for i in range(16)]}]},
    {'groups':[{'title':f'Track {i}','track_ids':[str(i)]} for i in range(16)]},
])
def test_invalid_or_still_fragmented_suggestion_does_not_write(tmp_path,monkeypatch,bad):
    monkeypatch.setattr(pipeline,'_cached_summarize',lambda *a,**kw:json.dumps(bad))
    with pytest.raises(pipeline.SummarizeError):
        grouping.suggest(data=catalog(),output_dir=tmp_path,config={})
    assert not list(tmp_path.rglob('.recordings.json'))


def test_overview_is_editable_and_cannot_swallow_substantive_track():
    data=catalog();data['overview_track_ids']=['0'];data['group_overrides']={'0':'Intro','1':'Intro'}
    normalized=recorded_content.normalize_directory(data)
    group=recording_groups.groups(normalized['recorded_tracks'])[0]
    assert group.get('summary_role')!='overview'
    normalized['overview_track_ids']=[]
    normalized=recorded_content.normalize_directory(normalized)
    assert all(t.get('summary_role')!='overview' for t in normalized['recorded_tracks'])
