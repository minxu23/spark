import copy
import importlib.util
import json
from pathlib import Path
import shutil
import pytest
from core import clips,clip_compose
from datetime import date

from core import taihe_sync as ts

class Phone:
    def __init__(self):self.rows=[];self.sent=[];self.photos={};self.fail=False
    def device_info(self):return {'version':1,'enabled':True,'id':'test-phone'}
    def records(self):return copy.deepcopy(self.rows)
    def image(self,cid,name):return self.photos[(cid,name)]
    def push(self,folder):
        if self.fail:raise ts.SyncError('connection dropped')
        p=json.loads((folder/'packet.json').read_text());self.sent.append(p)
        for f in p['files']:
            b=(folder/f['name']).read_bytes();assert len(b)==f['size']
            import hashlib
            assert hashlib.sha256(b).hexdigest()==f['sha256']
    def apply(self):
        rows={r['clip']['id']:r for r in self.rows}
        for p in self.sent:rows[p['record']['clip']['id']]=p['record']
        self.rows=list(rows.values());self.sent=[]

@pytest.fixture
def env(tmp_path):
    root=tmp_path/'vault';(root/'Spark/剪藏').mkdir(parents=True)
    return root,tmp_path/'state',Phone()

def note(cid='phone-a',body='完整正文',url='https://example.com/a',remark='手机备注'):
    text=clip_compose.render({'url':url,'title':'文章','source':'作者','published':'2026-11-01','markdown':body},remark,{'tldr':'摘要','tags':['AI'],'body':'- 要点'},date(2026,11,10))
    r=ts.from_document(text,cid);return r

def run(env):
    root,home,tr=env
    return ts.sync_device({'name':'太和测试'},transport=tr,root=root,home=home)

def files(root):return [p for p in (root/'Spark/剪藏').glob('*.md') if p.name!='索引.md']

def test_phone_upload_roundtrip_and_repeated_sync_no_duplicate(env):
    root,_,tr=env;tr.rows=[note()]
    run(env);tr.apply();run(env);tr.apply();result=run(env)
    assert len(files(root))==1 and len(tr.rows)==1
    assert result['received']==0 and result['saved']==0
    text=files(root)[0].read_text();assert '完整正文' in text and '手机备注' in text and '- 要点' in text

def test_desktop_download_bilingual_and_custom_fields(env):
    root,_,tr=env
    text=note(body='English paragraph.\n\n> 中文译文')['document'].replace('clipped:', 'custom: "keep"\nclipped:')
    (root/'Spark/剪藏/desktop.md').write_text(text)
    run(env);tr.apply();assert tr.rows[0]['document']==text
    assert '中文译文' in tr.rows[0]['clip']['bilingual']
    assert run(env)['saved']==0

def test_first_pair_same_url_different_content_preserves_both(env):
    root,_,tr=env;tr.rows=[note()]
    (root/'Spark/剪藏/existing.md').write_text(note(body='电脑正文')['document'])
    result=run(env);tr.apply()
    assert result['conflicts']==1 and len(files(root))==2 and len(tr.rows)==2
    assert (root/'Spark/剪藏/existing.md').read_text().find('电脑正文')>0
    run(env);tr.apply();assert len(files(root))==2

def test_both_sides_edit_preserves_two_versions(env):
    root,_,tr=env;tr.rows=[note()];run(env);tr.apply();run(env);tr.apply()
    path=files(root)[0];path.write_text(path.read_text().replace('完整正文','电脑新版'))
    c=tr.rows[0]['clip'];tr.rows[0]=note(c['id'],body='手机新版')
    result=run(env);tr.apply()
    assert result['conflicts']==1 and len(files(root))==2
    assert any('手机新版' in p.read_text() for p in files(root))
    assert any('电脑新版' in p.read_text() for p in files(root))

def test_disconnect_retries_same_transaction(env):
    root,_,tr=env;(root/'Spark/剪藏/desktop.md').write_text(note()['document']);tr.fail=True
    with pytest.raises(ts.SyncError):run(env)
    state=next(env[1].rglob('state.json'));tx=next(iter(json.loads(state.read_text())['entries'].values()))['pending']
    tr.fail=False;run(env);assert tr.sent[0]['transaction']==tx
    tr.apply();assert len(files(root))==1

def test_pending_pc_update_and_new_phone_edit_is_conflict(env):
    root,_,tr=env;tr.rows=[note()];run(env);tr.apply();run(env);tr.apply()
    path=files(root)[0];path.write_text(path.read_text().replace('完整正文','电脑先更新'))
    run(env) # phone has not applied the update
    tr.rows=[note(body='手机离线更新')]
    assert run(env)['conflicts']==1
    assert len(files(root))==2

def test_attachments_transferred_without_network_and_old_names_converted(env):
    root,_,tr=env;r=note(body='正文\n\n![](attachments/image-1.jpg)')
    r['clip']['images']=[{'name':'attachments/image-1.jpg','remote':'https://example.com/photo.jpg','saved':True}];r=ts.record(r['clip'],r['document'])
    tr.rows=[r];tr.photos[('phone-a','attachments/image-1.jpg')]=b'synthetic-image'
    run(env);assert list((root/'Spark/剪藏/attachments').glob('*.jpg'))
    assert 'image-1' not in files(root)[0].read_text()
    assert tr.sent[0]['files'][0]['size']==len(b'synthetic-image')

def test_deletion_and_phone_trash_do_not_delete_other_side(env):
    root,_,tr=env;tr.rows=[note()];run(env);tr.apply();run(env);tr.apply()
    files(root)[0].unlink();run(env);assert not files(root) and len(tr.rows)==1
    root2=root/'other';(root2/'Spark/剪藏').mkdir(parents=True)
    tr.rows[0]['clip']['archived']=True
    ts.sync_device({},transport=tr,root=root2,home=env[1]);assert not files(root2)

def test_path_traversal_symlinks_and_tampering_rejected(env,tmp_path):
    root,_,tr=env
    with pytest.raises(ts.SyncError):ts.safe_file(root,'Spark/剪藏/../../secret.md')
    outside=tmp_path/'secret';outside.mkdir();(root/'Spark/剪藏/attachments').symlink_to(outside,target_is_directory=True)
    with pytest.raises(ts.SyncError):ts.safe_file(root,'Spark/剪藏/attachments/abcdef0123456789.jpg')
    r=note();r['document']+='tampered';tr.rows=[r]
    with pytest.raises(ts.SyncError):run(env)

def test_stable_revision_matches_javascript_contract():
    r=note();ts.validate(r)
    assert ts.identity('https://example.com/a?utm_source=x&mid=2#x')=='https://example.com/a?mid=2'
    assert ts.identity('https://example.com/a?mid=3')!='https://example.com/a?mid=2'


def test_delete_before_phone_confirmation_is_not_resurrected(env):
    root,_,tr=env;tr.rows=[note(body='Native English.\n\n> 中文译文')]
    run(env);tr.apply()
    files(root)[0].unlink()
    run(env)
    assert not files(root) and len(tr.rows)==1


def test_missing_primary_never_remaps_to_conflict_sibling(env):
    root,_,tr=env;tr.rows=[note()];run(env);tr.apply();run(env);tr.apply()
    primary=files(root)[0];primary.write_text(primary.read_text().replace('完整正文','电脑新版'))
    tr.rows=[note(body='手机新版')];run(env);tr.apply();run(env);tr.apply()
    primary.unlink();before={p.name:p.read_text() for p in files(root)}
    run(env)
    assert {p.name:p.read_text() for p in files(root)}==before


def test_chinese_article_quotes_are_not_translations():
    body='这是一篇中文技术文章，介绍 RSI 与递归自我改进的边界。\n\n> 这是正文引用，必须保留在原文。\n\n还有中文段落讨论证据与验证。'
    value=note(body=body)['clip']
    assert value['bilingual']==''
    assert value['markdown']==body


def test_distant_english_and_chinese_quote_do_not_form_translation():
    body='English product name and technical reference.\n\n这里接着是中文正文。\n\n> 后面的引用属于文章原文。'
    value=note(body=body)['clip']
    assert value['bilingual']=='' and value['markdown']==body


def test_single_chinese_quote_in_long_english_article_is_preserved():
    body='\n\n'.join(['English source paragraph with important details.']*20+['> 中文引语来自另一位作者。'])
    value=note(body=body)['clip']
    assert value['bilingual']=='' and value['markdown']==body


def test_pending_packet_rebuilds_when_interpreted_fields_change(env):
    root,home,tr=env
    engine=ts.Engine(root,home,tr,tr.device_info());r=note()
    engine.send(r['clip']['id'],'Spark/剪藏/metadata.md',r,'same-phone-revision')
    before=tr.sent[-1]['transaction'];c=copy.deepcopy(r['clip']);c['titleGuessed']=True
    changed=ts.record(c,r['document'])
    engine.send(c['id'],'Spark/剪藏/metadata.md',changed,'same-phone-revision')
    assert tr.sent[-1]['transaction']!=before
    assert tr.sent[-1]['record']['revision']==changed['revision']


def reformatted(r,title=None,cid=None):
    """模拟手机端重排：标题下加来源行、文末换行不同，正文不变。"""
    c=copy.deepcopy(r['clip']);doc=r['document'].rstrip('\n')+'\n'
    doc=doc.replace('\n# '+c['title']+'\n','\n# '+c['title']+'\n\n- 来源：作者\n',1)
    if title:c['title']=title;doc=ts._document_with_title(doc,title)
    if cid:c['id']=cid
    return ts.record(c,doc)


def test_phone_reformat_of_same_article_is_not_a_conflict(env):
    root,_,tr=env;(root/'Spark/剪藏/desktop.md').write_text(note()['document'])
    tr.rows=[reformatted(note())]
    result=run(env)
    assert result['conflicts']==0 and len(files(root))==1
    tr.sent=[];again=run(env)
    assert again['conflicts']==0 and again['saved']==0 and not tr.sent


def test_phone_copy_with_same_content_is_not_imported(env):
    root,_,tr=env;(root/'Spark/剪藏/desktop.md').write_text(note()['document'])
    run(env);tr.apply()
    tr.rows.append(reformatted(tr.rows[0],title='文章（同步冲突副本）（手机目录副本）',cid='spark-x-conflic-folder-abc'))
    result=run(env)
    assert result['conflicts']==0 and len(files(root))==1


def test_conflict_copy_never_spawns_another_copy(env):
    root,_,tr=env;tr.rows=[note()];(root/'Spark/剪藏/existing.md').write_text(note(body='电脑正文')['document'])
    run(env);tr.apply()
    fork=next(r for r in tr.rows if '-conflict-' in r['clip']['id'])
    assert fork['clip']['title'].count('同步冲突副本')==1
    # 副本在两端都被改过
    path=next(p for p in files(root) if '同步冲突副本' in p.name)
    path.write_text(path.read_text().replace('完整正文','电脑改副本'))
    i=tr.rows.index(fork);tr.rows[i]=note(fork['clip']['id'],body='手机改副本');tr.rows[i]=ts.record(dict(tr.rows[i]['clip'],title=fork['clip']['title']),ts._document_with_title(tr.rows[i]['document'],fork['clip']['title']))
    for _ in range(3):run(env);tr.apply()
    names=[p.name for p in files(root)]
    assert len(names)==2 and not any(n.count('同步冲突副本')>1 for n in names)
    assert not any(r['clip']['id'].count('-conflict-')>1 for r in tr.rows)
