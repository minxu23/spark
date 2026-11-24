// 稍后再读：顶栏的「稍后再读 / 移出稍后再读」、稍后再读页上的「移出稍后再读」、占位剪藏的「补抓正文」。
// 都只发给本机服务（/read/api/flag、/read/api/clips/refetch），写的是文章自己的文件头。
(() => {
  async function post(url, body) {
    const r = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const d = await r.json().catch(() => ({ error: `服务出错（${r.status}）` }));
    if (!r.ok) throw new Error(d.error || '没改成');
    return d;
  }

  function setLater(btn, on) {
    return post(`${btn.dataset.api}/flag`, { path: btn.dataset.path, key: 'later', on });
  }

  const toggle = document.querySelector('.later-toggle');
  toggle?.addEventListener('click', async () => {
    const on = toggle.dataset.on !== '1';
    toggle.disabled = true;
    try {
      await setLater(toggle, on);
      toggle.dataset.on = on ? '1' : '0';
      toggle.setAttribute('aria-pressed', on ? 'true' : 'false');
      toggle.textContent = on ? '移出稍后再读' : '稍后再读';
      // 文件改了：划线、批注要带新的修改时间，免得被当成「在别处改过」
      if (document.querySelector('article.doc')) location.reload();
    } catch (e) {
      alert(e.message);
    } finally {
      toggle.disabled = false;
    }
  });

  document.querySelectorAll('.later-remove').forEach(btn => btn.addEventListener('click', async () => {
    btn.disabled = true;
    try {
      await setLater(btn, false);
      btn.closest('li')?.remove();
      const left = document.querySelectorAll('.later-list > li').length;
      const count = document.querySelector('[data-later-count]');
      if (count) count.textContent = `${left} 篇`;
    } catch (e) {
      alert(e.message);
      btn.disabled = false;
    }
  }));

  const stub = document.querySelector('.stub-note');
  const refetch = stub?.querySelector('.stub-refetch');
  refetch?.addEventListener('click', async () => {
    const msg = stub.querySelector('.stub-msg');
    refetch.disabled = true;
    msg.textContent = ' 正在抓取正文…';
    try {
      let job = await post(`${stub.dataset.api}/clips/refetch`, { path: stub.dataset.path });
      while (!job.done) {
        msg.textContent = ` ${job.phase || '正在抓取正文'}…`;
        await new Promise(ok => setTimeout(ok, 1000));
        const r = await fetch(`${stub.dataset.api}/clips/${job.id}`);
        job = await r.json();
      }
      if (job.error) throw new Error(job.error);
      location.reload();
    } catch (e) {
      msg.textContent = ` ${e.message}`;
      refetch.disabled = false;
    }
  });
})();
