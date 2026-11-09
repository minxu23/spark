"""Mac 剪藏保存：复用手机保存校验，后台下载图片，不覆盖已有文章。"""
import os
import threading
import uuid

from core import clips, github_sync, remote_tasks, vault

_LOCK = threading.RLock()
_JOBS = {}


def status(job_id):
    with _LOCK:
        return dict(_JOBS[job_id]) if job_id in _JOBS else None


def active_count():
    with _LOCK:
        return sum(not j['done'] for j in _JOBS.values())


def start(raw):
    task = remote_tasks.parse_task(dict(raw, type='save', replace=False))
    if not clips.is_clip_path(task['path']):
        raise remote_tasks.TaskError('只能保存到剪藏文件夹')
    with _LOCK:
        if active_count():
            raise remote_tasks.TaskError('已有一篇剪藏正在保存，请稍后再添加')
        for key in list(_JOBS):
            if len(_JOBS) < 40:
                break
            if _JOBS[key]['done']:
                del _JOBS[key]
        job_id = uuid.uuid4().hex
        _JOBS[job_id] = dict(id=job_id, done=False, phase='正在保存正文', path='', error='', warning='')
    root = vault.vault_root()
    threading.Thread(target=_run, args=(job_id, task, root), daemon=True).start()
    return status(job_id)


def _update(job_id, **values):
    with _LOCK:
        _JOBS[job_id].update(values)


def _run(job_id, task, root):
    try:
        # save_file 的重名规则：内容相同不重写，内容不同另存一篇。
        # 图片下载留在后台；正文保存成功后即使图片失败也保留文章。
        # save_file 从全局配置读库根目录；此处直接按同一规则保存，避免线程间改环境。
        rel = task['path']
        stem, ext = os.path.splitext(rel)
        root = os.path.realpath(root)
        for i in range(1, 100):
            cand = rel if i == 1 else f'{stem}_{i}{ext}'
            full = os.path.realpath(os.path.join(root, *cand.split('/')))
            if os.path.commonpath([full, root]) != root:
                raise remote_tasks.TaskError('路径不在笔记库里')
            os.makedirs(os.path.dirname(full), exist_ok=True)
            if os.path.exists(full):
                with open(full, encoding='utf-8') as f:
                    if f.read() == task['content']:
                        break
                continue
            from core import atomic
            atomic.write_text(full, task['content'])
            break
        else:
            raise remote_tasks.TaskError('同名文章过多，请修改标题')
        _update(job_id, path=cand, phase='正在下载图片')
        failed = 0
        images = list(task.get('images', {}).items())
        for n, (key, url) in enumerate(images, 1):
            _update(job_id, phase=f'正在下载图片 {n}/{len(images)}')
            _, bad = clips.download_images({key: url}, root)
            failed += bad
        clips.rebuild_index(root)
        _update(job_id, done=True, phase='已保存', warning=f'{failed} 张图片未下载，可通过原文查看' if failed else '')
        github_sync.after_task()
    except Exception as e:
        _update(job_id, done=True, error=str(e), phase='保存失败')
