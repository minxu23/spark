// 「用 HTML Anything 美化」：选版式、发起任务、轮询进度、完成后给出查看链接。
// 每种版式各存一份，下拉框里给已经生成过的标上「✓」。
const btn = document.getElementById('beautify');
const select = document.getElementById('style');

if (btn && select) {
  const api = btn.dataset.api;
  const path = btn.dataset.path;
  const labels = Object.fromEntries([...select.options].map(o => [o.value, o.textContent]));
  const status = document.createElement('span');
  status.className = 'status';
  status.setAttribute('aria-live', 'polite');
  select.before(status);
  const view = document.createElement('a');
  view.className = 'btn';
  view.target = '_blank';
  view.rel = 'noopener';
  view.textContent = '查看美化版';
  view.hidden = true;
  btn.after(view);

  // 记住上次选的版式（只是个人偏好，存不下也没关系）
  try {
    const saved = localStorage.getItem('spark-read-style');
    if (saved && labels[saved]) select.value = saved;
  } catch {}

  let timer = null;
  const style = () => select.value;
  const viewUrl = s => `${btn.dataset.view}?style=${encodeURIComponent(s)}`;
  const fmt = s => `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;

  function markGenerated(generated) {
    for (const o of select.options) {
      o.textContent = labels[o.value] + (o.value in (generated || {}) ? ' ✓' : '');
    }
  }

  function show(st) {
    if (st.generated) markGenerated(st.generated);
    status.classList.toggle('err', st.state === 'error' || !!st.error && st.state !== 'running');
    view.hidden = !st.has_html;
    view.href = viewUrl(st.style || style());
    if (st.state === 'running') {
      btn.disabled = true;
      select.disabled = true;
      btn.textContent = '美化中…';
      const got = st.chars ? `，已生成 ${Math.round(st.chars / 1000)}k 字符` : '，等模型开始输出';
      status.textContent = `${fmt(st.elapsed || 0)}${got}`;
      return;
    }
    btn.disabled = false;
    select.disabled = false;
    btn.textContent = st.has_html ? '重新生成' : '用 HTML Anything 美化';
    if (st.state === 'error' || st.error) status.textContent = st.error;
    else if (st.has_html && st.stale) status.textContent = '原文改过了，这一版已过期';
    else status.textContent = '';
    if (timer) { clearInterval(timer); timer = null; }
  }

  async function poll() {
    const s = style();
    try {
      const st = await (await fetch(`${api}?path=${encodeURIComponent(path)}&style=${encodeURIComponent(s)}`)).json();
      if (s !== style()) return;  // 等回复的时候用户换了版式
      const wasRunning = !!timer;
      show(st);
      if (st.state === 'running' && !timer) timer = setInterval(poll, 2000);
      if (wasRunning && st.state === 'done') window.open(view.href, '_blank', 'noopener');
    } catch {
      status.textContent = '查询进度失败，请看 Spark 终端里的报错。';
    }
  }

  select.addEventListener('change', () => {
    try { localStorage.setItem('spark-read-style', style()); } catch {}
    status.textContent = '';
    poll();
  });

  btn.addEventListener('click', async () => {
    btn.disabled = true;
    status.classList.remove('err');
    status.textContent = '正在连接 HTML Anything…';
    try {
      const r = await fetch(api, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path, style: style() }),
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
