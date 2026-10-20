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


def _tracks(sizes):
    tracks, n = [], 0
    for i, size in enumerate(sizes):
        entries = [{'id': f'v{n + k}', 'title': f'Talk {n + k}', 'url': f'https://youtu.be/v{n + k}'} for k in range(size)]
        n += size
        tracks.append({'id': f't{i}', 'title': f'Track {i}', 'count': size, 'entries': entries})
    return {'summit_title': 'Any Conference', 'source_url': 'https://any.example/recordings', 'recorded_tracks': tracks}


def test_fragmented_compares_topic_count_with_conference_size():
    from apps.summit2md import recording_grouping, recorded_content
    check = lambda d: recording_grouping.fragmented(recorded_content.normalize_directory(d))
    # 562 个视频分在 26 个议程（OCP 的情况）：目标约 12 个专题，26 个过细
    assert check(_tracks([40, 43, 42, 28, 15, 14, 8, 1] + [20] * 18))
    # 300 个视频分 8 个议程：不算细
    assert not check(_tracks([40] * 7 + [20]))
    # 50 个视频分 10 个小议程：过细
    assert check(_tracks([5] * 10))
    # 议程数不多但有好几个只有一两场的：也要合并
    assert check(_tracks([30, 30, 1, 2, 30]))
    # 用户保存过分组（每个议程都有归属）就不再提示
    saved = _tracks([5] * 10)
    saved['group_overrides'] = {t['id']: t['title'] for t in saved['recorded_tracks']}
    assert not check(saved)
