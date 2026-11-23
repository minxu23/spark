// 会议文件夹页：改会议时间（两个日期，结束可以不填）、恢复自动。存进处理记录后重新载入页面。
(() => {
  const box = document.querySelector('.event-dates[data-folder]');
  if (!box) return;
  const form = box.querySelector('.ed-form');
  const msg = box.querySelector('.ed-msg');

  async function send(start, end) {
    msg.textContent = '正在保存…';
    try {
      const r = await fetch(`${box.dataset.api}/event_dates`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ folder: box.dataset.folder, start, end }),
      });
      const data = await r.json().catch(() => ({}));
      if (!r.ok || !data.ok) {
        msg.textContent = data.error || `没保存上（${r.status}）`;
        return;
      }
      location.reload();
    } catch (e) {
      msg.textContent = '没保存上：连不上 Spark';
    }
  }

  form.addEventListener('submit', e => {
    e.preventDefault();
    send(form.elements.start.value, form.elements.end.value);
  });
  box.querySelector('.ed-reset')?.addEventListener('click', () => send('', ''));
})();
