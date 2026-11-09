"""Mac 上添加剪藏：Mac 应用抓好正文（跟手机共用 Clipper），交到这里；
这里跟手机收进剪藏一样（apps/mobile-ios/Spark/Clips.swift 的 ClipProcessor）：下图片、写摘要和标签、
全英文的逐段翻译、按手机的写法存一篇（core/clip_compose.py）、更新索引。一次只做一篇，在后台跑。"""
import os
import re
import subprocess
import tempfile
import threading
import uuid
from datetime import date

from core import clip_compose, clips, github_sync, remote_tasks, vault

_LOCK = threading.RLock()
_JOBS = {}
_MAX_MARKDOWN = 2_000_000
_SUMMARY_IMAGES = 6           # 纯图片的文章最多给模型看几张（同手机 ClipProcessor.shots）
_IMAGE_EDGE = 1280


def status(job_id):
    with _LOCK:
        return dict(_JOBS[job_id]) if job_id in _JOBS else None


def active_count():
    with _LOCK:
        return sum(not j['done'] for j in _JOBS.values())


def _text(raw, key, limit, required=False):
    v = raw.get(key, '')
    if not isinstance(v, str) or len(v) > limit or (required and not v.strip()):
        raise remote_tasks.TaskError(f'剪藏的 {key} 不对')
    return v


def parse(raw):
    """Mac 应用交来的：{article: {url, title, source, published, markdown, images, titleGuessed, imagesOnly}, note}"""
    a = raw.get('article') if isinstance(raw, dict) else None
    if not isinstance(a, dict):
        raise remote_tasks.TaskError('剪藏内容格式不对')
    article = {
        'url': _text(a, 'url', 2000, required=True),
        'title': _text(a, 'title', 500).strip(),
        'source': _text(a, 'source', 200),
        'published': _text(a, 'published', 10),
        'markdown': _text(a, 'markdown', _MAX_MARKDOWN),
        'titleGuessed': a.get('titleGuessed') is True,
        'imagesOnly': a.get('imagesOnly') is True,
    }
    if not re.match(r'^https?://\S+$', article['url']):
        raise remote_tasks.TaskError('剪藏的链接不对')
    if article['published'] and not re.fullmatch(r'\d{4}-\d{2}-\d{2}', article['published']):
        raise remote_tasks.TaskError('剪藏的发布日期不对')
    if not article['markdown'].strip() and not article['imagesOnly']:
        raise remote_tasks.TaskError('没有正文')
    images = a.get('images') or {}
    if not isinstance(images, dict) or len(images) > clips.MAX_IMAGES:
        raise remote_tasks.TaskError('剪藏的图片不对')
    article['images'] = {}
    for local, url in images.items():
        key = f'{clips.CLIPS_DIR}/{local}'
        if not clips.image_key_ok(key, url):
            raise remote_tasks.TaskError('剪藏的图片不对')
        article['images'][key] = url
    return article, _text(raw, 'note', 2000).strip()


def _new_job(phase, **extra):
    with _LOCK:
        if active_count():
            raise remote_tasks.TaskError('已有一篇剪藏正在处理，请等它做完')
        for key in list(_JOBS):
            if len(_JOBS) < 40:
                break
            if _JOBS[key]['done']:
                del _JOBS[key]
        job_id = uuid.uuid4().hex
        _JOBS[job_id] = dict(id=job_id, done=False, phase=phase, path='', title='', error='', warning='', **extra)
    return job_id


def start(raw):
    article, note = parse(raw)
    job_id = _new_job('正在下载图片')
    _update(job_id, title=article['title'])
    root = vault.vault_root()
    threading.Thread(target=_run, args=(job_id, article, note, root), daemon=True).start()
    return status(job_id)


# ---------------------------------------------------------------- 已存的剪藏补摘要、补翻译

def needs_fill(text):
    return clip_compose.needs_summary(text) or clip_compose.wants_translation(text)


def start_fill(raw):
    """{paths: [Spark/剪藏/…md], mode: fill（缺什么补什么）或 resummarize（摘要重写，缺的翻译也补）}"""
    paths = raw.get('paths') if isinstance(raw, dict) else None
    mode = raw.get('mode', 'fill') if isinstance(raw, dict) else ''
    if not isinstance(paths, list) or not paths or len(paths) > 200 or mode not in ('fill', 'resummarize'):
        raise remote_tasks.TaskError('要补的剪藏不对')
    if not all(isinstance(p, str) and clips.is_clip_path(p) for p in paths):
        raise remote_tasks.TaskError('只能补剪藏')
    root = vault.vault_root()
    if not all(os.path.isfile(os.path.join(root, *p.split('/'))) for p in paths):
        raise remote_tasks.TaskError('找不到这篇剪藏')
    if _ask() is None:
        raise remote_tasks.TaskError('没配好模型，去设置里填好再补')
    job_id = _new_job('准备中', done_count=0, total=len(paths))
    threading.Thread(target=_fill_run, args=(job_id, paths, mode, root), daemon=True).start()
    return status(job_id)


def _read(full):
    with open(full, encoding='utf-8') as f:
        return f.read()


def _fill_one(job_id, rel, mode, root, ask, label):
    """补一篇。模型跑的时候文件可能被改（比如划了重点），写之前重读，在最新的内容上改；
    正文变了的话译文不写，免得盖掉新改的。返回这一篇的提示（没有就空）。"""
    from core import atomic
    full = os.path.join(root, *rel.split('/'))
    text = _read(full)
    summary = None
    if mode == 'resummarize' or clip_compose.needs_summary(text):
        _update(job_id, phase=f'{label}正在写摘要')
        used = sorted({t for c in clips.load_clips(root) for t in c['tags']})
        summary = clip_compose.summarize(clip_compose.article_of(text), used, ask)
    body = translated = None
    if clip_compose.wants_translation(text):
        body = clip_compose.body_of(text)

        def progress(n, total):
            _update(job_id, phase=f'{label}正在翻译（{n}/{total}）')
        translated, missed = clip_compose.translate(clip_compose.without_translation(body), ask, progress)
    latest = _read(full)
    note = ''
    if translated is not None:
        if clip_compose.body_of(latest) == body:
            latest = clip_compose.replacing_body(latest, translated)
            note = f'有 {missed} 段没译成' if missed else ''
        else:
            note = '翻译时正文被改过，译文没写进去，再补一次'
    if summary:
        latest = clip_compose.with_summary(latest, summary)
    atomic.write_text(full, latest)
    return note


def _fill_run(job_id, paths, mode, root):
    ask = _ask()
    notes = []
    for n, rel in enumerate(paths, 1):
        label = f'第 {n}/{len(paths)} 篇：' if len(paths) > 1 else ''
        _update(job_id, path=rel, done_count=n - 1)
        try:
            note = _fill_one(job_id, rel, mode, root, ask, label)
            if note:
                notes.append(note if len(paths) == 1 else f'{os.path.basename(rel)[:-3]}：{note}')
        except Exception as e:  # noqa: BLE001  一篇不成不影响后面的
            notes.append(f'{os.path.basename(rel)[:-3]}：{e}')
    try:
        clips.rebuild_index(root)
        github_sync.after_task()
    except Exception as e:  # noqa: BLE001
        notes.append(f'索引没更新：{e}')
    _update(job_id, done=True, done_count=len(paths), phase='补好了', warning='；'.join(notes))


def _update(job_id, **values):
    with _LOCK:
        _JOBS[job_id].update(values)


def _ask():
    """跟设置里单集小结用的模型一样；没配好模型时返回 None，只存正文（同手机没填模型 Key 时）。"""
    from core import llm as core_llm, llm_config
    try:
        cfg = llm_config.resolve({}, default_backend='api')
    except llm_config.ConfigError:
        return None

    def ask(prompt, max_tokens=4000, timeout=600, images=None):
        return core_llm.complete(prompt, cfg['backend'], api_key=cfg['api_key'], model=cfg['model'],
                                 api_base=cfg['api_base'], max_tokens=max_tokens, timeout=timeout, images=images)
    return ask


def _shots(article, root):
    """纯图片的文章给模型看的图：最多 6 张，用系统的 sips 缩到长边 1280、转成 JPEG。"""
    out = []
    for key in list(article['images'])[:_SUMMARY_IMAGES]:
        src = os.path.join(root, *key.split('/'))
        if not os.path.exists(src):
            continue
        with tempfile.TemporaryDirectory() as tmp:
            dst = os.path.join(tmp, 'shot.jpg')
            try:
                subprocess.run(['sips', '-s', 'format', 'jpeg', '-Z', str(_IMAGE_EDGE), src, '--out', dst],
                               check=True, capture_output=True, timeout=30)
                with open(dst, 'rb') as f:
                    out.append(f.read())
            except (OSError, subprocess.SubprocessError):
                continue
    return out


def _unique_path(root, name):
    """同名的另存 _2、_3（同手机 ClipProcessor）。"""
    folder = os.path.join(root, *clips.CLIPS_DIR.split('/'))
    os.makedirs(folder, exist_ok=True)
    for n in range(2, 100):
        if not os.path.exists(os.path.join(folder, name)):
            break
        name = re.sub(r'_\d+$', '', os.path.splitext(name)[0]) + f'_{n}.md'
    else:
        raise remote_tasks.TaskError('同名文章过多，请修改标题')
    return f'{clips.CLIPS_DIR}/{name}'


def _run(job_id, article, note, root):
    from core import atomic
    notes = []
    try:
        images = list(article['images'].items())
        failed = 0
        for n, (key, url) in enumerate(images, 1):
            _update(job_id, phase=f'正在下载图片 {n}/{len(images)}')
            failed += clips.download_images({key: url}, root)[1]
        if failed:
            notes.append(f'{failed} 张图片没下载，可以看原文')

        summary = None
        ask = _ask()
        if ask is None:
            notes.append('没配好模型，没写摘要')
        else:
            _update(job_id, phase='正在写摘要')
            used = sorted({t for c in clips.load_clips(root) for t in c['tags']})
            shots = _shots(article, root) if article['imagesOnly'] else []
            why = ''
            for _ in range(2):   # 模型偶尔不按格式写或者超时，再试一次
                try:
                    summary = clip_compose.summarize(article, used, ask, images=shots)
                    break
                except Exception as e:  # noqa: BLE001
                    why = str(e)
            if summary is None:
                notes.append(f'摘要没写成（{why}），正文已经存了')
            elif summary['title'] and article['titleGuessed']:
                article['title'] = summary['title']
                _update(job_id, title=article['title'])
            # 全英文的文章逐段译成中文，阅读时能切换中文、英文、对照
            if clip_compose.needs_translation(article['markdown']):
                def progress(n, total):
                    _update(job_id, phase=f'正在翻译（{n}/{total}）')
                try:
                    article['markdown'], missed = clip_compose.translate(article['markdown'], ask, progress)
                    if missed:
                        notes.append(f'有 {missed} 段没译成')
                except Exception as e:  # noqa: BLE001
                    notes.append(f'没翻译：{e}')

        _update(job_id, phase='正在保存')
        today = date.today()
        rel = _unique_path(root, clip_compose.file_name(article['title'], today))
        atomic.write_text(os.path.join(root, *rel.split('/')), clip_compose.render(article, note, summary, today))
        clips.rebuild_index(root)
        _update(job_id, done=True, path=rel, phase='已保存', warning='；'.join(notes))
        github_sync.after_task()
    except Exception as e:  # noqa: BLE001
        _update(job_id, done=True, error=str(e), phase='保存失败')
