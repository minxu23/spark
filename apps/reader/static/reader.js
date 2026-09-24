// 「用 HTML Anything 美化」按钮：发起任务、轮询进度、完成后给出查看链接。
const btn = document.getElementById('beautify');

if (btn) {
  const api = btn.dataset.api;
  const path = btn.dataset.path;
  const status = document.createElement('span');
  status.className = 'status';
  status.setAttribute('aria-live', 'polite');
  btn.before(status);
  const view = document.createElement('a');
  view.className = 'btn';
  view.href = btn.dataset.view;
  view.target = '_blank';
  view.rel = 'noopener';
  view.textContent = '查看美化版';
  view.hidden = true;
  btn.before(view);

  let timer = null;

  const fmt = s => `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;

  function show(st) {
    status.classList.toggle('err', st.state === 'error' || !!st.error && st.state !== 'running');
    view.hidden = !st.has_html;
    if (st.state === 'running') {
      btn.disabled = true;
      btn.textContent = '美化中…';
      const got = st.chars ? `，已生成 ${Math.round(st.chars / 1000)}k 字符` : '，等模型开始输出';
      status.textContent = `${fmt(st.elapsed || 0)}${got}`;
      return;
    }
    btn.disabled = false;
    btn.textContent = st.has_html ? '重新美化' : '用 HTML Anything 美化';
    if (st.state === 'error' || st.error) status.textContent = st.error;
    else if (st.has_html && st.stale) status.textContent = '原文改过了，美化版已过期';
    else status.textContent = '';
    if (timer) { clearInterval(timer); timer = null; }
  }

  async function poll() {
    try {
      const st = await (await fetch(`${api}?path=${encodeURIComponent(path)}`)).json();
      const wasRunning = !!timer;
      show(st);
      if (st.state === 'running' && !timer) timer = setInterval(poll, 2000);
      if (wasRunning && st.state === 'done') window.open(view.href, '_blank', 'noopener');
    } catch {
      status.textContent = '查询进度失败，请看 Spark 终端里的报错。';
    }
  }

  btn.addEventListener('click', async () => {
    btn.disabled = true;
    status.classList.remove('err');
    status.textContent = '正在连接 HTML Anything…';
    try {
      const r = await fetch(api, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path }),
      });
      const st = await r.json();
      if (!r.ok) { show({ state: 'error', error: st.error, has_html: !view.hidden }); return; }
      show(st);
      if (!timer) timer = setInterval(poll, 2000);
    } catch {
      show({ state: 'error', error: '请求失败，请看 Spark 终端里的报错。', has_html: !view.hidden });
    }
  });

  poll();
}
