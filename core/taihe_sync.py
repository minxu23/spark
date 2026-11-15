"""太和剪藏的数据线双向同步。只访问用户开启的剪藏交换目录。

电脑保留三方版本基线；双端变更另存冲突副本。传输是不可变事务，最后发送
ready 标志；手机导入前校验正文和附件，未确认的事务可重复发送。
"""
from __future__ import annotations
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
from datetime import datetime
from core import atomic, clips, clip_compose, vault

BUNDLE = 'com.minxu.taihe'
APP = 'data/storage/el2/base/haps/entry/files'
EXCHANGE = APP + '/spark-sync'
MAX_DOCUMENT = 16 * 1024 * 1024
MAX_IMAGE = 8 * 1024 * 1024
ID = re.compile(r'[a-z0-9-]{1,120}\Z')
IMAGE = re.compile(r'attachments/(?:[a-f0-9]{16}|image-\d+)\.(?:jpg|png|gif|webp)\Z')
HASH = re.compile(r'[a-f0-9]{40}\Z')

class SyncError(RuntimeError):
    pass

def packed(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)

def sha(text):
    return hashlib.sha1(text.encode('utf-8')).hexdigest()

def revision(clip, document):
    return sha(packed([clip['id'], clip['created'], document, clip['title'], clip['source'], clip['published'], clip['url'], clip['note'], clip['tldr'], clip['summary'], clip['markdown'], clip.get('bilingual') or '', clip.get('translation') or '', clip['tags'], clip['kind'], bool(clip.get('titleGuessed')), bool(clip.get('imagesOnly')), clip.get('captureVersion') or 3, [[i['name'], i['remote']] for i in clip['images']]]))

def record(clip, document):
    return {'version': 1, 'revision': revision(clip, document), 'clip': clip, 'document': document}

def validate(value):
    try:
        c, doc = value['clip'], value['document']
        assert value['version'] == 1 and ID.fullmatch(c['id'])
        assert isinstance(c['created'], int) and 0 <= c['created'] <= 9007199254740991
        assert isinstance(doc, str) and len(doc.encode('utf-8')) <= MAX_DOCUMENT
        assert c['markdown'] and all(isinstance(c[k], str) for k in ('url','title','source','published','note','tldr','summary','markdown','translation'))
        assert isinstance(c['tags'], list) and all(isinstance(t, str) for t in c['tags'])
        assert isinstance(c['images'], list) and len(c['images']) <= 80
        assert all(IMAGE.fullmatch(i['name']) and isinstance(i['remote'], str) for i in c['images'])
        assert len({i['name'] for i in c['images']}) == len(c['images'])
        assert revision(c, doc) == value['revision']
    except (KeyError, TypeError, AssertionError, ValueError):
        raise SyncError('手机剪藏记录不完整或版本校验不一致，请回到太和重新同步') from None
    return value

def identity(url):
    try:
        p = urlsplit(url.strip())
        if p.scheme not in ('http','https') or not p.netloc:
            return ''
        query = [(k,v) for k,v in parse_qsl(p.query, keep_blank_values=True) if not k.lower().startswith('utm_') and k.lower() not in ('fbclid','gclid')]
        return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path, urlencode(query), ''))
    except ValueError:
        return ''

def hdc_command():
    explicit = os.environ.get('SPARK_HDC', '')
    if explicit:
        return [explicit] if os.path.isfile(explicit) else []
    candidate = shutil.which('hdc')
    if not candidate:
        sibling = Path(__file__).resolve().parents[2] / 'Taihe_HMOS/.tools'
        for path in (sibling/'clt26/command-line-tools/sdk/default/openharmony/toolchains/hdc', sibling/'command-line-tools/sdk/default/openharmony/toolchains/hdc', Path('/Applications/DevEco-Studio.app/Contents/sdk/default/openharmony/toolchains/hdc')):
            if path.is_file():
                candidate = str(path); break
    return [candidate] if candidate else []

class Transport:
    def __init__(self, device):
        command = hdc_command()
        if not command:
            raise SyncError('找不到鸿蒙设备连接工具，请安装 HDC 或设置 SPARK_HDC')
        server = os.environ.get('SPARK_HDC_SERVER', '')
        self.command = command + (['-s', server] if server else []) + ['-t', device['udid']]
    def run(self, args, timeout=60):
        try:
            p = subprocess.run(self.command + args, capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise SyncError('鸿蒙设备连接中断或超时，请检查数据线并重试') from error
        if p.returncode or '[Fail]' in (p.stdout+p.stderr) or '[E00' in (p.stdout+p.stderr):
            raise SyncError('无法读写太和同步目录，请解锁手机并检查调试连接')
        return p.stdout
    def receive(self, remote, local):
        self.run(['file','recv','-b',BUNDLE,remote,str(local)])
        if not Path(local).is_file():
            raise SyncError('手机文件未传完整，请重试')
    def device_info(self):
        with tempfile.TemporaryDirectory() as folder:
            file = Path(folder)/'device.json'; self.receive(EXCHANGE+'/device.json', file)
            return json.loads(file.read_text())
    def records(self):
        names = self.run(['shell','-b',BUNDLE,'ls',EXCHANGE+'/outbox']).split()
        with tempfile.TemporaryDirectory() as folder:
            result = []
            for name in sorted(names):
                if not name.endswith('.json') or not ID.fullmatch(name[:-5]):
                    continue
                file=Path(folder)/name; self.receive(EXCHANGE+'/outbox/'+name,file)
                if file.stat().st_size > MAX_DOCUMENT * 4:
                    raise SyncError('同步文章超过处理上限')
                result.append(validate(json.loads(file.read_text())))
                if len(result) > 10000:
                    raise SyncError('单次剪藏数量超过处理上限')
            return result
    def image(self, clip_id, name):
        if not ID.fullmatch(clip_id) or not IMAGE.fullmatch(name):
            raise SyncError('图片路径无效')
        with tempfile.TemporaryDirectory() as folder:
            file=Path(folder)/'image'; self.receive(f'{APP}/clips/{clip_id}/{name}',file)
            if not 0 < file.stat().st_size <= MAX_IMAGE:
                raise SyncError('同步图片超过 8 MB 或为空')
            return file.read_bytes()
    def push(self, folder):
        # Packet and all images first; ready is the commit marker.
        transaction=folder.name
        self.run(['shell','-b',BUNDLE,'mkdir','-p',f'{EXCHANGE}/inbox/{transaction}/attachments'])
        for file in sorted(folder.rglob('*')):
            if not file.is_file() or file.name == 'ready': continue
            rel=file.relative_to(folder).as_posix()
            self.run(['file','send','-b',BUNDLE,str(file),f'{EXCHANGE}/inbox/{transaction}/{rel}'])
        self.run(['file','send','-b',BUNDLE,str(folder/'ready'),f'{EXCHANGE}/inbox/{transaction}/ready'])

def list_devices():
    command=hdc_command()
    if not command: return []
    server=os.environ.get('SPARK_HDC_SERVER','')
    try:
        p=subprocess.run(command+(['-s',server] if server else [])+['list','targets'],capture_output=True,text=True,timeout=8)
        if p.returncode: return []
    except (OSError,subprocess.TimeoutExpired): return []
    devices=[]
    for serial in p.stdout.split():
        if not re.fullmatch(r'[\w.:-]+',serial) or serial in ('[Empty]','Empty'): continue
        device={'id':'taihe:'+serial,'udid':serial,'platform':'HarmonyOS','name':'太和剪藏','model':'鸿蒙手机','transport':'simulator' if serial.startswith('127.0.0.1:') else 'wired'}
        try:
            info=Transport(device).device_info()
            if info.get('version')==1 and info.get('enabled') is True and ID.fullmatch(info.get('id','')):
                device['name']=info.get('name') or '太和剪藏'; devices.append(device)
        except (OSError,ValueError,SyncError,subprocess.TimeoutExpired): pass
    return devices

def safe_file(root, rel):
    if not isinstance(rel,str) or not (clips.is_clip_path(rel) or re.fullmatch(r'Spark/剪藏/'+IMAGE.pattern,rel)) or any(p.startswith('.') for p in rel.split('/')):
        raise SyncError('剪藏路径无效')
    root=root.resolve(); file=root.joinpath(*rel.split('/'))
    if not file.resolve().is_relative_to(root) or any(p.is_symlink() for p in [file,*file.parents] if p != root and p.is_relative_to(root)):
        raise SyncError('同步路径指向笔记库之外或经过符号链接')
    return file

def read_document(file):
    if file.stat().st_size > MAX_DOCUMENT: raise SyncError('电脑剪藏超过 16 MB，未覆盖任何内容')
    return file.read_text(encoding='utf-8')

def split_bilingual_body(body):
    """Recognize adjacent English/Chinese pairs, preserving normal quotations.

    A distant English product name does not turn every Chinese quotation into
    a translation. Long articles need at least three pairs and 30% coverage;
    short manual translations may consist of one or two complete pairs.
    """
    blocks=body.split('\n\n'); original=[]; pairs=0; english=0; in_code=False
    previous_english=False
    for block in blocks:
        text=block.strip()
        if text.startswith('```'):
            original.append(block)
            if text.count('```') % 2:in_code=not in_code
            previous_english=False
            continue
        if in_code:
            original.append(block);previous_english=False;continue
        han=len(re.findall(r'[\u3400-\u9fff]',text));latin=len(re.findall(r'[A-Za-z]',text))
        if text.startswith('> ') and han and previous_english:
            pairs+=1;previous_english=False;continue
        original.append(block)
        source=not text.startswith(('![','|','$$')) and latin>=2 and han/(han+latin)<0.1
        previous_english=source
        if source:english+=1
    source='\n\n'.join(original)
    han=len(re.findall(r'[\u3400-\u9fff]',source));latin=len(re.findall(r'[A-Za-z]',source))
    coverage=pairs>=min(3,english) and pairs>=english*0.3
    if pairs and coverage and latin>=2 and han/(han+latin)<0.1:
        return source,body
    return body,''

def from_document(text, clip_id, prior=None):
    fm=clips.frontmatter(text)
    m=re.search(r'\n## 正文\n([\s\S]*)',text)
    if not m: raise SyncError('电脑剪藏缺少正文分节，保留原文件，请修复后再同步')
    body=m[1].strip(); link=body.rfind('\n\n[阅读原文](')
    if link >= 0: body=body[:link]
    else: body=re.split(r'(?m)^(?:## 读后感[ \t]*$|\[\^note-\d+\]:)',body,maxsplit=1)[0].strip()
    translation=''
    if '\n## 中文翻译\n' in body: body,translation=body.split('\n## 中文翻译\n',1); translation=translation.strip()
    original,bilingual=split_bilingual_body(body)
    day=str(fm.get('clipped') or '')
    try: created=int(datetime.strptime(day,'%Y-%m-%d').timestamp()*1000)
    except ValueError: created=0
    if prior: created=prior['created']
    summary=re.search(r'\n## 摘要\n([\s\S]*?)\n## 正文\n',text)
    summary=re.sub(r'^\s*\*\*一句话\*\*[:：][^\n]*\n?', '',summary[1]).strip() if summary else ''
    tldr=str(fm.get('tldr') or '')
    if tldr and summary.startswith(tldr): summary=summary[len(tldr):].strip()
    old_images={i['name']:i for i in (prior or {}).get('images',[])}
    names=list(dict.fromkeys(re.findall(r'!\[[^\]]*\]\((attachments/[a-f0-9]{16}\.(?:jpg|png|gif|webp))\)',body)))
    clip={'id':clip_id,'url':str(fm.get('url') or ''),'created':created,'title':str(fm.get('title') or clip_id),'source':str(fm.get('source') or ''),'published':str(fm.get('published') or ''),'markdown':original.strip(),'images':[{'name':n,'remote':old_images.get(n,{}).get('remote',''),'saved':False} for n in names], 'note':str(fm.get('note') or ''),'tags':clips._tags(fm.get('tags')),'tldr':str(fm.get('tldr') or ''),'summary':summary,'translation':translation,'bilingual':bilingual,'error':'','archived':False,'kind':(prior or {}).get('kind','article'),'captureVersion':3,'titleGuessed':(prior or {}).get('titleGuessed',False),'imagesOnly':(prior or {}).get('imagesOnly',False)}
    if not clip['markdown']: raise SyncError('电脑剪藏正文为空，未替换手机内容')
    return record(clip,text)

COPY_SUFFIX=re.compile(r'(?:（(?:同步冲突副本|手机目录副本)）)+\Z')
COPY_ID=re.compile(r'-conflic|-folder-')

def base_title(title):
    """去掉标题末尾一层或多层「（同步冲突副本）」「（手机目录副本）」。"""
    return COPY_SUFFIX.sub('',str(title or '').strip()).strip()

def is_copy(cid,title=''):
    """同步冲突或手机端复制出来的副本：副本不再派生副本，也不按网址认领别人的文件。"""
    return bool(COPY_ID.search(cid)) or base_title(title)!=str(title or '').strip()

def _flat(value):
    return re.sub(r'\s+','',str(value or ''))

def same_content(clip,text):
    """手机记录和电脑文件是不是同一篇内容，只是排版、转义或副本后缀不同。"""
    try:desk=from_document(text,clip['id'],clip)['clip']
    except SyncError:return False
    if base_title(clip['title'])!=base_title(desk['title']) or identity(clip['url'])!=identity(desk['url']):return False
    if sorted(clip['tags'])!=sorted(desk['tags']):return False
    if sorted(i['name'] for i in clip['images'])!=sorted(i['name'] for i in desk['images']):return False
    return all(_flat(clip.get(k))==_flat(desk.get(k)) for k in ('source','published','note','tldr','summary','markdown','translation'))

def _document_with_title(text,title):
    text=re.sub(r'(?m)^title:.*$', 'title: '+json.dumps(title,ensure_ascii=False),text,count=1)
    return re.sub(r'(?m)^# [^\n]*',lambda _: '# '+title,text,count=1)

class Engine:
    def __init__(self, root, home, transport, info):
        if info.get('enabled') is not True or info.get('version') != 1 or not ID.fullmatch(info.get('id','')): raise SyncError('请先在太和剪藏中开启 Spark 同步')
        self.root=Path(root).resolve(); self.transport=transport
        self.home=Path(home)/'taihe-sync'/info['id']/sha(str(self.root))[:16]; self.home.mkdir(parents=True,exist_ok=True)
        self.path=self.home/'state.json'
        if self.path.exists():
            try:self.state=json.loads(self.path.read_text())
            except (ValueError,OSError):raise SyncError('同步基线损坏，已停止以保护两端文章') from None
        else:self.state={'version':1,'source':sha(str(self.home)+str(time.time_ns())),'sequence':0,'entries':{}}
        self.stats={'ok':True,'platform':'HarmonyOS','files':0,'bytes':0,'saved':0,'received':0,'conflicts':0,'missing_images':0}
    def checkpoint(self): atomic.write_json(str(self.path),self.state)
    def documents(self):
        folder=safe_file(self.root,'Spark/剪藏/probe.md').parent
        return {f'Spark/剪藏/{p.name}':read_document(safe_file(self.root,f'Spark/剪藏/{p.name}')) for p in sorted(folder.glob('*.md')) if clips.is_clip_path(f'Spark/剪藏/{p.name}')}
    def write(self,rel,text,old=None):
        file=safe_file(self.root,rel); file.parent.mkdir(parents=True,exist_ok=True)
        if file.exists():
            actual=read_document(file)
            if actual!=old: raise SyncError('同步时电脑文章又被修改，已保留两端内容，请重试')
            backups=self.home/'backups';backups.mkdir(exist_ok=True)
            backup=backups/(sha(rel+'\n'+actual)+'.md')
            if not backup.exists():atomic.write_text(str(backup),actual)
        elif old is not None:raise SyncError('同步时电脑文章被移动，未重新创建，请重试')
        atomic.write_text(str(file),text);self.stats['saved']+=1
    def unique_path(self,c,suffix=''):
        day=datetime.fromtimestamp(c['created']/1000).date()
        name=clip_compose.file_name(c['title'],day)
        stem=name[:-3]+suffix; rel=f'Spark/剪藏/{stem}.md'
        for n in range(2,10000):
            if not safe_file(self.root,rel).exists():return rel
            rel=f'Spark/剪藏/{stem}_{n}.md'
        raise SyncError('同名文章过多，未覆盖旧文件')
    def import_phone(self,r):
        # Old phone versions used per-article sequential names; convert to Spark's global names.
        r=copy.deepcopy(r); c=r['clip']; text=r['document']
        for image in c['images']:
            old=image['name']; name=clips.local_name(image['remote']) if image['remote'] else old
            if name.startswith('attachments/image-'):raise SyncError('旧附件缺少原地址，请先在太和重新抓取正文')
            if image.get('saved'):
                data=self.transport.image(c['id'],old)
                if not 0<len(data)<=MAX_IMAGE:raise SyncError('图片大小无效')
                file=safe_file(self.root,'Spark/剪藏/'+name); file.parent.mkdir(parents=True,exist_ok=True)
                if file.exists() and file.read_bytes()!=data:
                    name='attachments/'+hashlib.sha256(data).hexdigest()[:16]+'.'+name.rsplit('.',1)[1];file=safe_file(self.root,'Spark/剪藏/'+name)
                if not file.exists():
                    temporary=file.with_suffix(file.suffix+'.sync');temporary.write_bytes(data);os.replace(temporary,file)
            text=text.replace(']('+old+')',']('+name+')')
            c['markdown']=c['markdown'].replace(']('+old+')',']('+name+')')
            c['bilingual']=(c.get('bilingual') or '').replace(']('+old+')',']('+name+')');image['name']=name
        return record(c,text)
    def packet(self,r,base,rel):
        self.state['sequence']+=1
        packet={'version':1,'base':base,'path':rel,'record':r,'files':[],'source':self.state['source'],'sequence':self.state['sequence']}
        files={}
        for image in r['clip']['images']:
            file=safe_file(self.root,'Spark/剪藏/'+image['name'])
            if not file.exists():self.stats['missing_images']+=1;continue
            if not 0<file.stat().st_size<=MAX_IMAGE:raise SyncError('电脑图片超过 8 MB 或为空，未推送不完整文章')
            data=file.read_bytes();packet['files'].append({'name':image['name'],'sha256':hashlib.sha256(data).hexdigest(),'size':len(data)});files[image['name']]=data
        tx=sha(packed(packet));packet['transaction']=tx
        folder=self.home/'packets'/tx;folder.mkdir(parents=True,exist_ok=True)
        for name,data in files.items():
            file=folder/name;file.parent.mkdir(exist_ok=True);file.write_bytes(data)
        atomic.write_json(str(folder/'packet.json'),packet);atomic.write_text(str(folder/'ready'),tx)
        return tx
    def send(self,cid,rel,r,phone_revision):
        previous=self.state['entries'].get(cid,{})
        current_sha=sha(r['document'])
        assets=[]
        for image in r['clip']['images']:
            file=safe_file(self.root,'Spark/剪藏/'+image['name'])
            assets.append([image['name'],file.stat().st_size,file.stat().st_mtime_ns] if file.exists() else [image['name'],0,0])
        if not previous.get('pending') and phone_revision==r['revision'] and previous.get('desktop')==current_sha and previous.get('assets')==assets:
            return
        if previous.get('pending') and previous.get('expected')==r['revision'] and previous.get('phone')==phone_revision and previous.get('desktop')==current_sha and previous.get('assets')==assets:
            tx=previous['pending']
        else:
            tx=self.packet(r,phone_revision,rel)
            self.state['entries'][cid]={'path':rel,'phone':phone_revision,'desktop':current_sha,'expected':r['revision'],'pending':tx,'assets':assets}
            self.checkpoint()
        self.transport.push(self.home/'packets'/tx);self.stats['received']+=1
    def keep_phone_version(self,rel,r):
        """副本两端都改了：电脑文件不动，手机这一版存进同步目录的备份，不再派生新副本。"""
        backups=self.home/'backups';backups.mkdir(exist_ok=True)
        backup=backups/('phone-'+sha(rel+'\n'+r['document'])+'.md')
        if not backup.exists():atomic.write_text(str(backup),r['document'])
        self.stats['kept_phone']=self.stats.get('kept_phone',0)+1
    def run(self):
        snapshots=self.transport.records(); seen=set(); docs=self.documents(); all_docs=dict(docs)
        by_url={}
        for rel,text in docs.items():
            key=identity(str(clips.frontmatter(text).get('url') or ''))
            if key:by_url.setdefault(key,[]).append(rel)
        entries=self.state['entries']
        for original in snapshots:
            validate(original);c=original['clip'];cid=c['id']
            if cid in seen:raise SyncError('手机发件箱出现重复条目，未继续同步')
            seen.add(cid);previous=entries.get(cid)
            copy_record=is_copy(cid,c['title'])
            rel=previous.get('path') if previous else None
            # 旧版本让副本按网址认领了原文件；原文件归非副本条目所有。
            if copy_record and rel and any(k!=cid and e.get('path')==rel and not is_copy(k) for k,e in entries.items()):rel=None
            # 同一网址的文件已被别的条目占用时不能再认领；副本永远不按网址认领。
            owners={e.get('path'):k for k,e in entries.items()}
            candidates=[] if copy_record else [p for p in by_url.get(identity(c['url']),[]) if owners.get(p,cid)==cid]
            if rel not in docs and len(candidates)==1:rel=candidates[0]
            if c.get('archived'):
                if rel in docs: docs.pop(rel)
                continue
            if rel not in docs and copy_record and any(same_content(c,all_docs[p]) for p in by_url.get(identity(c['url']),[])):
                continue # 内容和电脑上已有的同一篇一样的手机副本，不再导入成新文件。
            if previous and rel in docs and previous.get('same')==sha(docs[rel]) and original['revision']==previous.get('phone'):
                docs.pop(rel);continue # 上次已确认两端是同一篇内容，两边都没再改。
            if previous and original['revision']==previous.get('expected'):
                previous['phone']=original['revision']
                confirmed=previous.pop('pending',None)
                if confirmed and HASH.fullmatch(confirmed):
                    shutil.rmtree(self.home/'packets'/confirmed, ignore_errors=True)
            if previous and rel not in docs and original['revision']==previous['phone']:
                continue # Local deletion is not propagated or silently resurrected.
            text=docs.get(rel)
            phone_changed=not previous or original['revision']!=previous['phone']
            desktop_changed=bool(previous and text is not None and sha(text)!=previous['desktop'])
            awaiting_edit=bool(previous and previous.get('pending') and phone_changed)
            r=None
            if not phone_changed and any(i.get('saved') and not safe_file(self.root,'Spark/剪藏/'+(clips.local_name(i['remote']) if i['remote'] else i['name'])).exists() for i in c['images']):
                self.import_phone(original)
            if text is None or (previous and phone_changed and not desktop_changed and not awaiting_edit):
                r=self.import_phone(original)
                if text is None:rel=self.unique_path(c)
                self.write(rel,r['document'],text);text=r['document'];docs[rel]=text
            elif phone_changed and original['document'].strip()!=text.strip() and same_content(c,text):
                # 手机端重排了文件（转义、来源行、副本后缀），正文没变：以电脑版为准，记下两端一致。
                entries[cid]={'path':rel,'phone':original['revision'],'desktop':sha(text),'expected':original['revision'],'same':sha(text),'assets':(previous or {}).get('assets',[])}
                self.checkpoint();docs.pop(rel);continue
            elif phone_changed and original['document'].strip()!=text.strip() and copy_record:
                self.keep_phone_version(rel,original)
            elif phone_changed and original['document'].strip()!=text.strip():
                # First pairing with different versions or a true two-sided edit.
                r=self.import_phone(original);fork=copy.deepcopy(r['clip'])
                fork['id']=cid[:60]+'-conflict-'+original['revision'][:12]
                fork['title']=base_title(fork['title'])+'（同步冲突副本）'
                fork_text=_document_with_title(r['document'],fork['title']);fork_record=record(fork,fork_text)
                fork_entry=self.state['entries'].get(fork['id']);fork_rel=fork_entry['path'] if fork_entry else self.unique_path(fork)
                if not fork_entry:self.write(fork_rel,fork_text)
                self.send(fork['id'],fork_rel,fork_record,'');self.stats['conflicts']+=1
            if rel in docs:docs.pop(rel)
            desired=from_document(text,cid,(r or original)['clip'])
            self.send(cid,rel,desired,original['revision'])
        # Remaining desktop clips are imported into Taihe. Stable path ids avoid duplicates.
        for rel,text in docs.items():
            old=next(((cid,e) for cid,e in self.state['entries'].items() if e['path']==rel),None)
            if old and old[0] in seen:continue
            cid=old[0] if old else 'spark-'+sha(rel)[:24]
            desired=from_document(text,cid)
            self.send(cid,rel,desired,old[1]['phone'] if old else '')
        self.checkpoint()
        clips.rebuild_index(str(self.root))
        self.stats['files']=len(self.state['entries']);self.stats['bytes']=sum(len(x['document'].encode('utf-8')) for x in snapshots)
        return self.stats

def sync_device(device, *, transport=None, root=None, home=None):
    from core import settings
    tr=transport or Transport(device)
    result=Engine(root or vault.vault_root(),home or os.path.dirname(settings.settings_path()),tr,tr.device_info()).run()
    return dict(result,device=device.get('name','太和剪藏'))
