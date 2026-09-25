// 顶栏「Aa」面板：配色、字体、字号、栏宽。点了立刻生效，存在浏览器本地。
const aa = document.getElementById('aa');
const panel = document.getElementById('prefs');

if (aa && panel && window.SparkRead) {
  const { SIZES, load, apply, save } = window.SparkRead;
  const sizeNow = document.getElementById('size-now');
  let prefs = load();

  // 没调过字号时，从页面实际字号推出当前档位
  const currentSize = () => Number.isInteger(prefs.size) ? prefs.size
    : Math.max(0, SIZES.indexOf(Math.round(parseFloat(getComputedStyle(document.body).fontSize))));

  function sync() {
    for (const key of ['theme', 'font', 'width']) {
      for (const b of panel.querySelectorAll(`[data-${key}]`)) {
        b.setAttribute('aria-pressed', String(b.dataset[key] === prefs[key]));
      }
    }
    const i = currentSize();
    sizeNow.textContent = `${SIZES[i]}px`;
    panel.querySelector('[data-size="-1"]').disabled = i <= 0;
    panel.querySelector('[data-size="+1"]').disabled = i >= SIZES.length - 1;
  }

  function set(change) {
    prefs = { ...prefs, ...change };
    apply(prefs);
    save(prefs);
    sync();
  }

  function toggle(open) {
    panel.hidden = !open;
    aa.setAttribute('aria-expanded', String(open));
    if (open) { sync(); panel.querySelector('[aria-pressed="true"]')?.focus(); }
  }

  aa.addEventListener('click', () => toggle(panel.hidden));

  panel.addEventListener('click', e => {
    const b = e.target.closest('button');
    if (!b) return;
    if (b.dataset.size) {
      const i = Math.min(SIZES.length - 1, Math.max(0, currentSize() + Number(b.dataset.size)));
      set({ size: i });
    } else {
      for (const key of ['theme', 'font', 'width']) if (b.dataset[key]) set({ [key]: b.dataset[key] });
    }
  });

  document.addEventListener('click', e => {
    if (!panel.hidden && !panel.contains(e.target) && e.target !== aa) toggle(false);
  });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && !panel.hidden) { toggle(false); aa.focus(); }
  });
}
