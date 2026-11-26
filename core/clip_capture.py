"""Mac 上添加剪藏：Mac 应用抓好正文（跟手机共用 Clipper），交到这里；
这里跟手机收进剪藏一样（apps/mobile-ios/Spark/Clips.swift 的 ClipProcessor）：下图片、写摘要和标签、
全英文的逐段翻译、按手机的写法存一篇（core/clip_compose.py）、更新索引。一次只做一篇，在后台跑。"""
import os
import re
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import datetime

from core import clip_compose, clips, doctime, github_sync, jobs, remote_tasks, vault

_LOCK = threading.RLock()
_JOBS = {}
_KEEP_SECONDS = 24 * 3600    # 做完的任务留一天、最多 40 个，Mac 应用查得到结果就行
_KEEP_DONE = 40
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


def parse(raw, folder=clips.CLIPS_DIR):
    """Mac 应用交来的：{article: {url, title, source, published, publishedAt, markdown, images, titleGuessed, imagesOnly}, note}
    folder：存到哪个剪藏式文件夹（默认剪藏，论文是 clips.PAPERS_DIR），图片键按它拼。"""
    a = raw.get('article') if isinstance(raw, dict) else None
    if not isinstance(a, dict):
        raise remote_tasks.TaskError('剪藏内容格式不对')
    article = {
        'url': _text(a, 'url', 2000, required=True),
        'title': _text(a, 'title', 500).strip(),
        'source': _text(a, 'source', 200),
        'published': _text(a, 'published', 10),
        'publishedAt': _text(a, 'publishedAt', 40),   # 知道发表时刻时：ISO 8601 带时差（旧版 Mac 应用不带）
        'markdown': _text(a, 'markdown', _MAX_MARKDOWN),
        'titleGuessed': a.get('titleGuessed') is True,
        'imagesOnly': a.get('imagesOnly') is True,
    }
    if not re.match(r'^https?://\S+$', article['url']):
        raise remote_tasks.TaskError('剪藏的链接不对')
    if article['published'] and not re.fullmatch(r'\d{4}-\d{2}-\d{2}', article['published']):
        raise remote_tasks.TaskError('剪藏的发布日期不对')
    if article['publishedAt'] and not isinstance(doctime.parse(article['publishedAt']), datetime):
        raise remote_tasks.TaskError('剪藏的发布时间不对')
    if not article['markdown'].strip() and not article['imagesOnly']:
        raise remote_tasks.TaskError('没有正文')
    images = a.get('images') or {}
    if not isinstance(images, dict) or len(images) > clips.MAX_IMAGES:
        raise remote_tasks.TaskError('剪藏的图片不对')
    article['images'] = {}
    for local, url in images.items():
        key = f'{folder}/{local}'
        if not clips.image_key_ok(key, url):
            raise remote_tasks.TaskError('剪藏的图片不对')
        article['images'][key] = url
    return article, _text(raw, 'note', 2000).strip()


_BUSY = {'clip': '已有一篇剪藏正在处理，请等它做完', 'track': '已有一篇信息文章正在翻译，请等它做完',
         'paper': '已有一篇论文正在处理，请等它做完'}


def _new_job(phase, kind='clip', **extra):
    """一次只做一篇。查「有没有在做的」和登记新任务在同一把锁里，前一篇标完成的同时就能接下一篇
    （core/jobs.JobSlots 要等线程退出才还名额，紧接着的请求会被误拒，这里不用它）。
    kind：clip（剪藏）、track（信息文章补译，core/track_translate.py）或 paper（论文，core/papers.py），共用这一个任务位。"""
    with _LOCK:
        running = next((j for j in _JOBS.values() if not j['done']), None)
        if running:
            raise remote_tasks.TaskError(_BUSY.get(running.get('kind'), _BUSY['clip']))
        jobs.prune_finished(_JOBS, retention_seconds=_KEEP_SECONDS, max_completed=_KEEP_DONE)
        job_id = uuid.uuid4().hex
        _JOBS[job_id] = dict(id=job_id, done=False, phase=phase, path='', title='', error='', warning='',
                             created_at=time.time(), kind=kind, **extra)
    return job_id


def _later_flag(raw):
    """可选的「稍后再读」：later 是 true 才算；别的值（不带、false、字符串）都当没有，旧调用不受影响。"""
    return isinstance(raw, dict) and raw.get('later') is True


def _link_only(raw):
    """存链接（只在带 later 时认）：没给 article，或者给的 article 没有正文。返回 (url, title, note)，不是这种返回 None。"""
    a = raw.get('article')
    if a is not None and not isinstance(a, dict):
        return None
    a = a or {}
    if isinstance(a.get('markdown'), str) and a['markdown'].strip() or a.get('imagesOnly') is True:
        return None
    url = a.get('url') if a.get('url') is not None else raw.get('url')
    title = a.get('title') if a.get('title') is not None else raw.get('title', '')
    if not isinstance(url, str) or len(url) > 2000 or not re.match(r'^https?://\S+$', url.strip()):
        raise remote_tasks.TaskError('剪藏的链接不对')
    if not isinstance(title, str) or len(title) > 500:
        raise remote_tasks.TaskError('剪藏的 title 不对')
    return url.strip(), title.strip(), _text(raw, 'note', 2000).strip()


def start(raw, folder=clips.CLIPS_DIR):
    """新剪藏。可选 later: true（稍后再读）：存下的剪藏文件头带 later: <此刻>；
    这时也可以只给链接（{url, title?, note?, later: true}，或 article 里没有正文）：电脑这边抓正文，抓不到就存一篇占位剪藏。
    不带 later 时跟原来完全一样。
    folder：存到哪个剪藏式文件夹（默认剪藏；论文传 clips.PAPERS_DIR，文件、图片、索引都落在那里）。只由代码传，不从请求里读。"""
    if folder not in clips.CLIP_DIRS:
        raise remote_tasks.TaskError('只能存进剪藏或论文')
    later = doctime.iso(doctime.now()) if _later_flag(raw) else ''
    link = _link_only(raw) if later else None
    if link:
        url, title, note = link
        job_id = _new_job('正在抓取正文')
        _update(job_id, title=title or clip_compose.stub_title(url))
        root = vault.vault_root()
        threading.Thread(target=_run_link, args=(job_id, url, title, note, root, later, folder), daemon=True).start()
        return status(job_id)
    article, note = parse(raw, folder)
    job_id = _new_job('正在下载图片')
    _update(job_id, title=article['title'])
    root = vault.vault_root()
    threading.Thread(target=_run, args=(job_id, article, note, root, later, folder), daemon=True).start()
    return status(job_id)


# ---------------------------------------------------------------- 电脑这边抓正文（存链接、补抓）

def fetch_article(url):
    """按链接抓一篇的正文，返回跟 parse 一样的 article。抓不到抛 RuntimeError。
    用 core/sources 的通用文章抓取（只连公网地址）；Mac 应用自己的 Clipper 抓得更全，抓到了会直接交 article 来。"""
    from core import sources
    return article_from_entry(url, sources.fetch_generic_article_entry(url))


def article_from_entry(url, entry):
    """sources 解析好的一条（fetch_generic_article_entry / article_entry_from_raw 的结果）换成 parse 那样的 article。
    论文抓 arXiv 网页版时也用它（core/papers.py）。"""
    from bs4 import BeautifulSoup
    if entry.get('article_text'):
        markdown = entry['article_text']
    else:
        soup = BeautifulSoup(entry.get('article_content_html') or '', 'html.parser')
        blocks = []
        for el in soup.find_all(['h1', 'h2', 'h3', 'h4', 'p', 'li', 'blockquote', 'pre']):
            if el.find_parent(['li', 'blockquote', 'pre']) and el.name in ('p', 'li'):
                continue
            t = re.sub(r'\s+', ' ', el.get_text()).strip()   # 不加分隔：中文里的粗体前后不多出空格
            if not t:
                continue
            if el.name in ('h1', 'h2', 'h3', 'h4'):
                blocks.append('#' * max(2, int(el.name[1])) + ' ' + t)
            elif el.name == 'li':
                blocks.append('- ' + t)
            elif el.name == 'blockquote':
                blocks.append('> ' + t)
            else:
                blocks.append(t)
        markdown = '\n\n'.join(blocks)
    if not markdown.strip():
        raise RuntimeError('没能在页面里抠出正文')
    when = doctime.entry_published(entry)
    title = str(entry.get('title') or '').strip()
    return {'url': url, 'title': '' if title in ('', 'Untitled', url) else title[:500],
            'source': clip_compose.stub_source(url),
            'published': doctime.day(when).isoformat() if when else '',
            'publishedAt': doctime.iso(when) if isinstance(when, datetime) else '',
            'markdown': markdown[:_MAX_MARKDOWN], 'images': {}, 'titleGuessed': False, 'imagesOnly': False}


def _run_link(job_id, url, title, note, root, later, folder=clips.CLIPS_DIR):
    """存链接：先抓正文，抓到了照常存；抓不到存占位剪藏，任务不算失败，提示里说一句。"""
    try:
        article = fetch_article(url)
    except Exception as e:  # noqa: BLE001  抓不到（打不开、不是文章、超时）都存占位
        _save_stub(job_id, url, title, note, root, later, why=str(e), folder=folder)
        return
    if title:
        article['title'] = title
    elif not article['title']:
        article['title'], article['titleGuessed'] = clip_compose.stub_title(url), True
    _update(job_id, title=article['title'])
    _run(job_id, article, note, root, later, folder)


def _save_stub(job_id, url, title, note, root, later, why='', folder=clips.CLIPS_DIR):
    from core import annotations, atomic
    try:
        captured = doctime.now()
        title = title or clip_compose.stub_title(url)
        rel = _unique_path(root, clip_compose.file_name(title, doctime.day(captured)), folder)
        text = annotations.with_later(clip_compose.render_stub(url, title, note, captured), True, later)
        atomic.write_text(os.path.join(root, *rel.split('/')), text)
        clips.rebuild_index(root, folder)
        tail = f'（{why}）' if why else ''
        _update(job_id, done=True, path=rel, title=title, phase='已保存',
                warning=f'正文没抓到{tail}，先存了标题和链接，之后可以在阅读页补抓正文')
        github_sync.after_task()
    except Exception as e:  # noqa: BLE001
        _update(job_id, done=True, error=str(e), phase='保存失败')


# ---------------------------------------------------------------- 占位剪藏补抓正文

def start_refetch(raw):
    """{path: Spark/剪藏/…md（占位剪藏）, article?: 跟 start 一样的 article（Mac 应用抓好的；不给就电脑这边抓）}。
    抓到了：照新剪藏的做法写摘要、翻译，换掉占位的正文，去掉 stub；星标、置顶、稍后再读、划线、批注、读后感、
    备注、收录时间、文件名都留着。抓不到：任务报错，文件不动。"""
    rel = raw.get('path') if isinstance(raw, dict) else None
    if not isinstance(rel, str) or not clips.is_clip_path(rel):
        raise remote_tasks.TaskError('只能补抓剪藏')
    root = vault.vault_root()
    full = os.path.join(root, *rel.split('/'))
    if not os.path.isfile(full):
        raise remote_tasks.TaskError('找不到这篇剪藏')
    from core import annotations
    if not annotations.is_stub(_read(full)):
        raise remote_tasks.TaskError('这篇的正文已经有了，不用补抓')
    article = None
    if raw.get('article') is not None:
        article, _ = parse({'article': raw['article'], 'note': ''}, clips.clip_folder(rel))
    job_id = _new_job('正在抓取正文')
    _update(job_id, path=rel)
    threading.Thread(target=_refetch_run, args=(job_id, rel, article, root), daemon=True).start()
    return status(job_id)


def _refetch_run(job_id, rel, article, root):
    from core import annotations, atomic
    full = os.path.join(root, *rel.split('/'))
    try:
        old = _read(full)
        fm = clips.frontmatter(old)
        url = fm.get('url') or ''
        if article is None:
            try:
                article = fetch_article(url)
            except Exception as e:  # noqa: BLE001
                _update(job_id, done=True, error=f'没抓到正文：{e}', phase='没抓到')
                return
        # 占位时的标题是网址凑的（或者没改过）：换成抓到的；自己改过的标题留着
        old_title = fm.get('title') or ''
        if old_title and old_title != clip_compose.stub_title(url):
            article['title'], article['titleGuessed'] = old_title, False
        elif not article['title']:
            article['title'], article['titleGuessed'] = clip_compose.stub_title(url), True
        _update(job_id, title=article['title'])
        notes = []
        summary = _process(job_id, article, root, notes, clips.clip_folder(rel))
        _update(job_id, phase='正在保存')
        latest = _read(full)
        if not annotations.is_stub(latest):
            _update(job_id, done=True, error='这篇在补抓时已经有正文了，没覆盖', phase='没覆盖')
            return
        fm = clips.frontmatter(latest)
        clipped = doctime.parse(fm.get('clipped_at')) or doctime.parse(fm.get('clipped')) or doctime.now()
        new = clip_compose.render(article, fm.get('note') or '', summary, clipped)
        new, _ = annotations.carry_over(latest, new)   # 划线、批注、读后感
        new = _keep_flags(latest, new)                  # 星标、置顶、稍后再读：原样、原来的先后
        atomic.write_text(full, new)
        clips.rebuild_index(root, clips.clip_folder(rel))
        _update(job_id, done=True, path=rel, phase='已补抓', warning='；'.join(notes))
        github_sync.after_task()
    except Exception as e:  # noqa: BLE001
        _update(job_id, done=True, error=str(e), phase='补抓失败')


# ---------------------------------------------------------------- 已存的剪藏补摘要、补翻译

def needs_fill(text, paper=False):
    # 占位剪藏（正文还没抓到）不算：先补抓正文，补摘要会把那句说明当正文。paper：论文（末尾的「## 追问」不算正文）
    from core import annotations
    if annotations.is_stub(text):
        return False
    return clip_compose.needs_summary(text) or clip_compose.wants_translation(text, paper=paper)


# 复盘那边（apps/review/server.py：生成复盘、讨论、论文的追问）在跑的任务数。spark.py 启动时接上；core 不 import apps，免得循环导入
review_jobs = lambda: 0  # noqa: E731


def ollama_backend() -> bool:
    """模型后端是不是本地 Ollama（一次只能跑一件事，同时收两个请求会把内存顶满）。配置不对当不是。"""
    from core import llm_config
    try:
        return llm_config.resolve({}, default_backend='api')['backend'] == 'ollama'
    except llm_config.ConfigError:
        return False


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
    from core import annotations
    if any(annotations.is_stub(_read(os.path.join(root, *p.split('/')))) for p in paths):
        raise remote_tasks.TaskError('正文还没抓到的剪藏先补抓正文，再写摘要')
    if _ask() is None:
        raise remote_tasks.TaskError('没配好模型，去设置里填好再补')
    if ollama_backend() and review_jobs():
        raise remote_tasks.TaskError('复盘、讨论或追问还在跑，本地模型一次只跑一件事，等它结束再补')
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
    paper = clips.is_paper_path(rel)   # 论文：末尾存进来的「## 追问」不当正文，翻译写回时留着
    summary = None
    if mode == 'resummarize' or clip_compose.needs_summary(text):
        _update(job_id, phase=f'{label}正在写摘要')
        used = sorted({t for c in clips.load_clips(root, clips.clip_folder(rel)) for t in c['tags']})
        summary = clip_compose.summarize(clip_compose.article_of(text, paper), used, ask,
                                         paper=clips.clip_folder(rel) == clips.PAPERS_DIR)
    body = translated = None
    # 论文不自动翻译（点「翻译成中文」才译）：重写摘要时不顺带补翻译
    if not (paper and mode == 'resummarize') and clip_compose.wants_translation(text, paper=paper):
        body = clip_compose.body_of(text, paper)

        def progress(n, total):
            _update(job_id, phase=f'{label}正在翻译（{n}/{total}）')
        translated, missed = clip_compose.translate(body, ask, progress)   # 只译没译的段落
    with clips.DOC_LOCK:   # 重读到写回之间不让别的写（手机交来的存、存进本文、阅读页的划线）插进来
        latest = _read(full)
        note = ''
        if translated is not None:
            if clip_compose.body_of(latest, paper) == body:
                latest = clip_compose.replacing_body(latest, translated, paper)
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
        for folder in dict.fromkeys(clips.clip_folder(p) for p in paths):   # 补了哪几个文件夹的就重写哪几个的索引
            clips.rebuild_index(root, folder)
        github_sync.after_task()
    except Exception as e:  # noqa: BLE001
        notes.append(f'索引没更新：{e}')
    _update(job_id, done=True, done_count=len(paths), phase='补好了', warning='；'.join(notes))


def _update(job_id, **values):
    if values.get('done'):
        values.setdefault('finished_at', time.time())
    with _LOCK:
        _JOBS[job_id].update(values)


def _ask():
    """跟设置里单集小结用的模型一样；没配好模型时返回 None，只存正文（同手机没填模型 Key 时）。"""
    from core import llm as core_llm, llm_config
    try:
        cfg = llm_config.resolve({}, default_backend='api')
    except llm_config.ConfigError:
        return None

    from core import settings as core_settings
    strong = (core_settings.get(f"ai.overall_models.{cfg['backend']}") or '').strip()

    def call(model, prompt, max_tokens, timeout, images):
        return core_llm.complete(prompt, cfg['backend'], api_key=cfg['api_key'], model=model,
                                 api_base=cfg['api_base'], max_tokens=max_tokens, timeout=timeout, images=images)

    def ask(prompt, max_tokens=4000, timeout=600, images=None):
        try:
            return call(cfg['model'], prompt, max_tokens, timeout, images)
        except core_llm.LLMError as e:
            # 日常用的小模型偶尔整篇拒答（返回空内容，比如讲芯片出口的文章），换设置里的总结模型再试一次
            if '空内容' in str(e) and strong and strong != cfg['model']:
                return call(strong, prompt, max_tokens, timeout, images)
            raise
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


def _unique_path(root, name, rel_dir=clips.CLIPS_DIR):
    """同名的另存 _2、_3（同手机 ClipProcessor）。rel_dir：剪藏或论文的文件夹。"""
    folder = os.path.join(root, *rel_dir.split('/'))
    os.makedirs(folder, exist_ok=True)
    for n in range(2, 100):
        if not os.path.exists(os.path.join(folder, name)):
            break
        name = re.sub(r'_\d+$', '', os.path.splitext(name)[0]) + f'_{n}.md'
    else:
        raise remote_tasks.TaskError('同名文章过多，请修改标题')
    return f'{rel_dir}/{name}'


_FLAG_KEYS = ('starred:', 'pinned:', 'later:')
# 论文的作者、arXiv 编号、PDF 原件：旧文件里有这一行才用旧的盖掉新渲染的；旧文件没有时留着新渲染的
_PAPER_KEYS = ('authors:', 'arxiv:', 'pdf:')


def _keep_flags(old, new):
    """old 文件头里的星标、置顶、稍后再读（还有论文的 authors、arxiv、pdf）几行原样搬到 new 的文件头最后（按 old 里的先后），
    new 里原有的星标、置顶、稍后再读去掉；论文那三行只在 old 里有同一项时才去掉 new 的（同手机 LaterRule.keepingFlags）。"""
    def head(text):
        if not text.startswith('---\n'):
            return None
        end = text.find('\n---\n', 4)
        return end if end >= 0 else None
    oe, ne = head(old), head(new)
    if ne is None:
        return new
    olds = old[4:oe].split('\n') if oe is not None else []
    flags = [ln for ln in olds if ln.startswith(_FLAG_KEYS + _PAPER_KEYS)]
    drop = _FLAG_KEYS + tuple(k for k in _PAPER_KEYS if any(ln.startswith(k) for ln in olds))
    kept = [ln for ln in new[4:ne].split('\n') if not ln.startswith(drop)]
    return '---\n' + '\n'.join(kept + flags) + new[ne:]


def _process(job_id, article, root, notes, folder=clips.CLIPS_DIR):
    """下图片、写摘要、翻译（新剪藏和补抓共用）。改 article 的正文、图片、标题，返回摘要（没写成是 None），提示加进 notes。"""
    # 同一张图只存一份：地址不同的几个版本先合并，下载以后字节一样的再合并（纯图片的文章重复的那行也去掉）
    article['markdown'], article['images'] = clips.merge_variants(article['markdown'], article['images'], article['imagesOnly'])
    images = list(article['images'].items())
    failed = 0
    for n, (key, url) in enumerate(images, 1):
        _update(job_id, phase=f'正在下载图片 {n}/{len(images)}')
        failed += clips.download_images({key: url}, root)[1]
    if failed:
        notes.append(f'{failed} 张图片没下载，可以看原文')
    article['markdown'], article['images'] = clips.merge_identical(article['markdown'], article['images'], root, article['imagesOnly'])

    summary = None
    ask = _ask()
    if ask is None:
        notes.append('没配好模型，没写摘要')
    else:
        _update(job_id, phase='正在写摘要')
        used = sorted({t for c in clips.load_clips(root, folder) for t in c['tags']})
        shots = _shots(article, root) if article['imagesOnly'] else []
        why = ''
        for _ in range(2):   # 模型偶尔不按格式写或者超时，再试一次
            try:
                summary = clip_compose.summarize(article, used, ask, images=shots, paper=folder == clips.PAPERS_DIR,
                                                      ask_meta=folder == clips.PAPERS_DIR)
                break
            except Exception as e:  # noqa: BLE001
                why = str(e)
        if summary is None:
            notes.append(f'摘要没写成（{why}），正文已经存了')
        elif summary['title'] and article['titleGuessed']:
            article['title'] = summary['title']
            _update(job_id, title=article['title'])
        if summary is not None and folder == clips.PAPERS_DIR:
            # 论文缺发表时间、作者时模型顺带取到的（只补缺的，已有的不动）
            if summary.get('published') and clip_compose.published_of(article) is None:
                article['published'] = summary['published']
            if summary.get('authors') and not article.get('authors'):
                article['authors'] = summary['authors']
        # 全英文的文章逐段译成中文，阅读时能切换中文、英文、对照
        # 论文不自动译（一篇全文译下来调用太多）：要看中文在阅读页点「翻译成中文」
        if folder != clips.PAPERS_DIR and clip_compose.needs_translation(article['markdown']):
            def progress(n, total):
                _update(job_id, phase=f'正在翻译（{n}/{total}）')
            try:
                article['markdown'], missed = clip_compose.translate(article['markdown'], ask, progress)
                if missed:
                    notes.append(f'有 {missed} 段没译成')
            except Exception as e:  # noqa: BLE001
                notes.append(f'没翻译：{e}')
    return summary


def _run(job_id, article, note, root, later='', folder=clips.CLIPS_DIR):
    """later：稍后再读加入的时刻（ISO），空着是不带。folder：存进哪个剪藏式文件夹。"""
    from core import annotations, atomic
    notes = []
    try:
        summary = _process(job_id, article, root, notes, folder)
        _update(job_id, phase='正在保存')
        # 同一篇以前存过（小红书比笔记编号，每次分享的短链不一样；别的比去掉跟踪参数的 https 地址）：照旧另存一份，提醒一句
        if clips.find_by_link(article['url'], root, folder):
            notes.append('这篇以前存过，这次另存了一份')
        captured = doctime.now()
        # 文件名用发表那天（不知道发表时间用今天），文件头记收录的这一刻
        rel = _unique_path(root, clip_compose.file_name(article['title'], clip_compose.name_day(article, captured)), folder)
        text = clip_compose.render(article, note, summary, captured)
        if later:
            text = annotations.with_later(text, True, later)
        atomic.write_text(os.path.join(root, *rel.split('/')), text)
        clips.rebuild_index(root, folder)
        _update(job_id, done=True, path=rel, phase='已保存', warning='；'.join(notes))
        github_sync.after_task()
    except Exception as e:  # noqa: BLE001
        _update(job_id, done=True, error=str(e), phase='保存失败')
