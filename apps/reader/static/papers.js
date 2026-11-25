// 论文专页的「添加论文」：贴链接（/read/api/papers）或选一个 PDF 文件上传（/read/api/papers/upload），
// 跟剪藏共用一个任务位，进度轮询 /read/api/clips/<id>。存好（或已经有这一篇）就打开那一篇。
(() => {
  const form = document.querySelector('.add-paper');
  if (!form) return;
  const api = form.dataset.api;
  const status = form.querySelector('.add-paper-status');
  const say = t => { status.textContent = t; };
  const busy = on => form.querySelectorAll('input, button').forEach(el => { el.disabled = on; });

  async function json(r) {
    const d = await r.json().catch(() => ({ error: `服务出错（${r.status}）` }));
    if (!r.ok) throw new Error(d.error || '没存成');
    return d;
  }

  async function follow(job) {
    while (!job.done) {
      say(job.phase || '正在处理');
      await new Promise(ok => setTimeout(ok, 1000));
      job = await json(await fetch(`${api}/clips/${job.id}`));
    }
    if (job.error) throw new Error(job.error);
    say(job.warning ? `${job.title}：${job.warning}` : `已存为论文：${job.title}`);
    // 笔记库里的路径 Spark/论文/x.md → 阅读页的 /read/f/论文/x.md
    const rel = (job.path || '').replace(/^Spark\//, '');
    if (rel) setTimeout(() => { location.href = `${api.replace(/\/api$/, '')}/f/${rel.split('/').map(encodeURIComponent).join('/')}`; }, 1200);
  }

  async function run(send) {
    busy(true);
    try {
      await follow(await json(await send()));
    } catch (e) {
      say(e.message);
    } finally {
      busy(false);
    }
  }

  form.addEventListener('submit', ev => {
    ev.preventDefault();
    const url = form.url.value.trim();
    if (!url) { say('先贴一个链接'); return; }
    say('正在提交');
    run(() => fetch(`${api}/papers`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ url }) }));
  });

  form.file.addEventListener('change', () => {
    const file = form.file.files[0];
    if (!file) return;
    if (file.size > 50 * 1024 * 1024) { say('PDF 超过 50MB，不处理'); form.file.value = ''; return; }
    const body = new FormData();
    body.append('file', file);
    say('正在上传');
    run(() => fetch(`${api}/papers/upload`, { method: 'POST', body })).finally(() => { form.file.value = ''; });
  });
})();
