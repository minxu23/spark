"""Run a conference selection as one task with sequential physical theme batches."""
from __future__ import annotations

import copy
import json
import time
from pathlib import Path
from urllib.parse import urlencode

from core import atomic
from . import pipeline, recording_groups as groups, recording_guide as guide


def partition(base, title, entries, explicit=None):
    """Resolve saved group overrides once, retaining selection order and per-entry options."""
    root = Path(base)
    catalogs = {}
    for path in [root/'.recordings.json', *sorted(root.glob('*/.recordings.json'))]:
        data = guide._json(path)
        if not data.get('source_url') or not data.get('recorded_tracks'):
            continue
        data = guide.recorded_content.normalize_directory(data)
        by_track, by_video = {}, {}
        for group in groups.groups(data['recorded_tracks']):
            context = {'conference_title':data['summit_title'],'group_title':group['title'],
                       'source_url':data['source_url'],'group_folder':group['folder']}
            source = data['source_url']+'#'+urlencode({
                'spark-tracks':json.dumps([t['id'] for t in group['agenda_tracks']]),'spark-label':group['title']})
            for track in group['agenda_tracks']:
                by_track[track['id']] = (context,source)
            for entry in group['entries']:
                by_video.setdefault(entry['id'],(context,source))
        catalogs[data['source_url']] = (data,by_track,by_video)
    embedded = any(e.get('collection') for e in entries)
    fallback = groups.collection_for(base,title,entries,explicit) if not embedded else explicit
    batches, seen = {}, set()
    for i,original in enumerate(entries):
        if original['id'] in seen:
            continue
        seen.add(original['id'])
        context = groups.validate_collection(original.get('collection') or fallback) if original.get('collection') or fallback else None
        if not context:
            if embedded:
                raise ValueError('部分议题缺少所属分组，请重新载入官网专题后再试')
            return None
        source = ''
        summary_folder = context.get('group_folder')
        catalog = catalogs.get(context.get('source_url'))
        if catalog:
            # A video listed under several agenda tracks has one physical copy, owned by the first
            # group (same rule as filing). Process it there so existing files are reused; the group
            # it was selected from still gets its topic summary afterwards.
            listed = catalog[1].get(original.get('agenda_track_id'))
            resolved = catalog[2].get(original['id']) or listed
            if not resolved:
                raise ValueError('官网目录已变化，部分所选议题不在当前目录中，请重新载入后再试')
            context,source = resolved
            summary_folder = (listed or resolved)[0]['group_folder']
        path = groups.output_path(base, title, pipeline.sanitize_filename, context)
        batch = batches.setdefault(path,{'collection':context,'entries':[],'output_dir':path,'source_url':source})
        entry = copy.deepcopy(original)
        entry['collection'] = context
        entry['summary_group_folder'] = summary_folder or Path(path).name
        entry.setdefault('index',i+1)
        batch['entries'].append(entry)
    if not batches:
        return None
    contexts = [b['collection'] for b in batches.values()]
    if len({(c['conference_title'],c.get('source_url')) for c in contexts}) != 1:
        raise ValueError('所选议题来自不同大会，请分别发起任务')
    parents = {str(Path(path).parent) for path in batches}
    if len(parents) != 1:
        raise ValueError('分组目录不属于同一大会，请检查输出目录')
    return {'root':parents.pop(),'title':contexts[0]['conference_title'],'batches':list(batches.values())}


def process_job(*, batches, batch_root, **params):
    total = sum(len(b['entries']) for b in batches)
    progress = params.get('progress_cb') or (lambda kw:None)
    stop = params.get('stop_flag') or (lambda:False)
    pause = params.get('pause_flag') or (lambda:False)
    rows,full_rows,failed,unprocessed,outcomes = [],[],[],[],[]
    summarize_topics = params.pop('do_topic_summary', params.get('do_overall_summary', False))
    offset,stopped = 0,False
    # A topic is summarised once every selected entry of it has been processed, even when a
    # cross-listed entry was processed in another group's folder.
    remaining = {}
    for b in batches:
        for e in b['entries']:
            key = e.get('summary_group_folder') or Path(b['output_dir']).name
            remaining[key] = remaining.get(key,0) + 1
    summarized = set()
    progress({'stage':'processing','current':0,'total':total,
              'log':f'已将 {total} 个议题分为 {len(batches)} 个资料分组，按顺序串行处理。'})
    for number,batch in enumerate(batches,1):
        while pause() and not stop():
            time.sleep(.2)
        if stop():
            stopped = True
            unprocessed.extend(e for b in batches[number-1:] for e in b['entries'])
            break
        name = batch['collection']['group_title']
        progress({'stage':'processing','current':offset,'total':total,
                  'log':f'分组 {number}/{len(batches)}：{name}，本组 {len(batch["entries"])} 个议题。'})
        def report(kw):
            value = dict(kw)
            value['total'] = total
            if 'current' in value:
                value['current'] = offset + min(len(batch['entries']),value['current'])
            if value.get('stage') == 'done':
                value['stage'] = 'processing'
            progress(value)
        child = {**params,'entries':batch['entries'],'collection':batch['collection'],
                 'summit_title':groups.canonical_title('',batch['collection']),
                 'source_url':batch['source_url'] or params.get('source_url',''),
                 'do_overall_summary':False,'index_title':None,'progress_cb':report}
        child_completed = False
        try:
            result = pipeline.process_job(**child)
            child_completed = True
            rows.extend(result.get('rows',[]));full_rows.extend(result.get('full_rows',[]))
            failed.extend(result.get('failed_entries',[]))
            outcomes.append({'title':name,'output_dir':result['output_dir'],'failed':len(result.get('failed_entries',[]))})
            offset += len(batch['entries']) - len(result.get('unprocessed_entries',[]))
            if result.get('stopped') or stop():
                stopped = True
                unprocessed.extend(result.get('unprocessed_entries',[]))
                unprocessed.extend(e for b in batches[number:] for e in b['entries'])
                break
            while pause() and not stop():
                time.sleep(.2)
            for e in batch['entries']:
                key = e.get('summary_group_folder') or Path(batch['output_dir']).name
                remaining[key] -= 1
            ready = [k for k,v in remaining.items() if v <= 0 and k not in summarized]
            if summarize_topics and ready and not stop():
                from . import recording_summaries
                snapshot = guide._json(Path(batch_root)/'.recordings.json')
                catalog_groups = groups.groups(guide.recorded_content.normalize_directory(snapshot).get('recorded_tracks', [])) if snapshot.get('recorded_tracks') else []
                for folder in ready:
                    summarized.add(folder)
                    topic = next((g for g in catalog_groups if g['folder'] == folder), None)
                    label = topic['title'] if topic else folder
                    if topic and topic.get('summary_role') != 'overview':
                        progress({'stage':'summarizing','current':offset,'total':total,
                                  'log':f'正在生成大专题总结：{label}…'})
                        try:
                            config = {k:params.get(k,'') for k in ('backend','api_key','api_base')}
                            config['model'] = params.get('overall_model') or params.get('model','')
                            recording_summaries.generate(data=snapshot,output_dir=Path(batch_root).parent,
                                track_id=topic['id'],config=config,stop_flag=stop)
                            outcomes[-1].setdefault('summaries',[]).append(label)
                            outcomes[-1]['summary'] = 'saved'
                            progress({'log':f'大专题总结已保存：{label}。'})
                        except pipeline.Stopped:
                            raise
                        except Exception as error:
                            outcomes[-1]['summary_error'] = str(error)
                            outcomes[-1].setdefault('summary_errors',{})[label] = str(error)
                            progress({'log':f'⚠️ 大专题 {label} 总结生成失败：{error}；资料已保留，可从大会导航重试总结。'})
                    elif not topic:
                        progress({'log':f'⚠️ 未找到大专题 {label} 的分类记录，未生成总结，请重新载入官网专题后重试。'})
        except pipeline.Stopped:
            stopped = True
            unprocessed.extend(e for b in batches[number if child_completed else number-1:] for e in b['entries'])
            break
        except Exception as error:
            # Keep successful groups and continue independent groups; retry only failed entries.
            failed.extend(batch['entries']);offset += len(batch['entries'])
            outcomes.append({'title':name,'output_dir':batch['output_dir'],'error':str(error)})
            progress({'log':f'⚠️ 分组 {name} 处理失败：{error}；继续后续分组。'})
    directory = Path(batch_root)
    snapshot = guide._json(directory/'.recordings.json')
    index = directory/guide.index_filename(directory)
    if snapshot.get('recorded_tracks'):
        try:
            from . import recording_organizer, recording_summaries
            progress({'stage':'processing','current':offset,'total':total,'log':'正在更新大会导航与各组续跑记录…'})
            recording_organizer.organize(data=snapshot,output_dir=directory.parent)
            index = directory/guide.index_filename(directory)
            while not stopped and not stop() and pause():
                time.sleep(.2)
            if params.get('do_overall_summary') and not stopped and not stop():
                saved = guide._json(directory/'.recording-summaries.json').get('overall',{})
                legacy = guide._json(directory/'.manifest.json').get('overall_summary')
                have_summary = (recording_summaries.valid_record(directory,saved) or
                                isinstance(legacy,str) and bool(legacy.strip()) and not pipeline._is_failed_overall_summary(legacy))
                reuse = not params.get('regenerate_summary',True) and have_summary
                if reuse:
                    progress({'log':'按设置沿用已有大会与专题总结；新增议题已归档，可在导航中单独更新总结。'})
                else:
                    progress({'log':'正在基于已有资料生成专题总结与大会总结…'})
                    config = {k:params.get(k,'') for k in ('backend','api_key','api_base')}
                    config['model'] = params.get('overall_model') or params.get('model','')
                    recording_summaries.generate(data=snapshot,output_dir=directory.parent,config=config,stop_flag=stop)
        except pipeline.Stopped:
            stopped = True
        except Exception as error:
            progress({'log':f'⚠️ 导航或大会总结更新失败：{error}；已完成的议题和各组文件保留。'})
    else:
        if not index.exists() or guide.MARKER in index.read_text():
            from urllib.parse import quote
            content = [guide.MARKER,'# '+params['summit_title'],'','## 本次处理分组','']
            content += [f"- [{b['collection']['group_title']}]({quote(str(Path(b['output_dir']).relative_to(directory)/(Path(b['output_dir']).name+'.md')))})" for b in batches]
            atomic.write_text(str(index),'\n'.join(content)+'\n')
    stopped = stopped or stop()
    progress({'stage':'stopped' if stopped else 'done','current':offset,'total':total,
              'log':f'分组处理已{"停止" if stopped else "完成"}，失败 {len(failed)} 个，未处理 {len(unprocessed)} 个。'})
    return {'output_dir':str(directory),'index_path':str(index),'rows':rows,'full_rows':full_rows,
            'failed_entries':failed,'unprocessed_entries':unprocessed,'stopped':stopped,'groups':outcomes}


def group_options(data):
    """Use the same saved grouping and order as physical batch processing."""
    data = guide.recorded_content.normalize_directory(data)
    return [{'id':g['folder'],'title':g['title'],'count':g['count'],
             'tracks':[t['title'] for t in g['agenda_tracks']]}
            for g in groups.groups(data['recorded_tracks'])]


def select_groups(data, selected):
    """Resolve a batch selection against the latest saved conference catalog."""
    if not isinstance(selected,list) or not selected or not all(isinstance(i,str) for i in selected):
        raise ValueError('请至少选择一个大专题')
    data = guide.recorded_content.normalize_directory(data)
    available = groups.groups(data['recorded_tracks'])
    if set(selected) - {g['folder'] for g in available}:
        raise ValueError('大专题分类已变化，请重新载入后选择')
    chosen = [g for g in available if g['folder'] in selected]
    seen, entries = set(), []
    for group in chosen:
        for original in group['entries']:
            if original['id'] in seen:
                continue
            seen.add(original['id'])
            entry = copy.deepcopy(original)
            entry['index'] = len(entries)+1
            entries.append(entry)
    tracks = [t['id'] for g in chosen for t in g['agenda_tracks']]
    label = chosen[0]['title'] if len(chosen)==1 else '所选大专题'
    return {'entries':entries,'summit_title':data['summit_title']+' - '+label,
            'content_type':'summit','batch_group_count':len(chosen),
            'source_url':data['source_url']+'#'+urlencode({'spark-tracks':json.dumps(tracks,separators=(',',':')),'spark-label':label})}
