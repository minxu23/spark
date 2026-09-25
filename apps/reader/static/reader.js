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

// 选中文字 →「高亮」写回原文（Obsidian 的 ==文字==），「摘录」存进 Spark/摘录.md 并顺手高亮；
// 点已有的高亮 →「取消高亮」。改完用服务端重新渲染的正文换掉页面上的，滚动位置不动。
const doc = document.querySelector('article.doc[data-path]');

if (doc) {
  const api = doc.dataset.api;
  const canExcerpt = doc.dataset.excerptable === 'true';
  const bar = document.createElement('div');
  bar.className = 'selbar';
  bar.hidden = true;
  bar.setAttribute('role', 'toolbar');
  bar.setAttribute('aria-label', '标注');
  document.body.append(bar);
  const toast = document.createElement('div');
  toast.className = 'toast';
  toast.setAttribute('role', 'status');
  toast.setAttribute('aria-live', 'polite');
  document.body.append(toast);

  let pending = null;   // {text, before} 或 {mark, text, nth}
  let busy = false;
  let toastTimer = null;

  function say(msg, { error = false, link = null } = {}) {
    toast.classList.toggle('err', error);
    toast.textContent = msg;
    if (link) {
      const a = document.createElement('a');
      a.href = link.href;
      a.textContent = link.text;
      toast.append(' ', a);
    }
    toast.classList.add('on');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toast.classList.remove('on'), error ? 7000 : 4500);
  }

  function hide() {
    bar.hidden = true;
    pending = null;
  }

  function place(rect) {
    bar.hidden = false;
    const w = bar.offsetWidth, h = bar.offsetHeight;
    let top = rect.top + scrollY - h - 10;
    if (rect.top - h - 10 < 56) top = rect.bottom + scrollY + 10;   // 顶栏挡着就放到下面
    const left = Math.min(Math.max(8, rect.left + scrollX + rect.width / 2 - w / 2), scrollX + innerWidth - w - 8);
    bar.style.top = `${top}px`;
    bar.style.left = `${left}px`;
  }

  function button(label, fn) {
    const b = document.createElement('button');
    b.type = 'button';
    b.textContent = label;
    b.addEventListener('mousedown', e => e.preventDefault());   // 别让点按钮把选区清掉
    b.addEventListener('click', fn);
    return b;
  }

  function inBody(node) {
    const el = node?.nodeType === 1 ? node : node?.parentElement;
    return !!el && doc.contains(el) && !el.closest('.meta');
  }

  function showForSelection() {
    const sel = getSelection();
    if (busy || !sel.rangeCount || sel.isCollapsed) return;
    const range = sel.getRangeAt(0);
    if (!inBody(range.startContainer) || !inBody(range.endContainer)) return;
    const text = sel.toString();
    if (!text.trim()) return;
    // 选区前面同一段里的文字：同样的话出现好几次时，服务端靠它认出是哪一处
    const block = (range.startContainer.nodeType === 1 ? range.startContainer : range.startContainer.parentElement)
      .closest('p, li, h1, h2, h3, h4, h5, td, th, blockquote') || doc;
    const pre = document.createRange();
    pre.setStart(block, 0);
    pre.setEnd(range.startContainer, range.startOffset);
    // 笔记末尾「我的高亮」是汇总，同一句话在正文里也有，服务端分不清选的是哪一处，这里先说明
    const summary = [...doc.querySelectorAll('h2')].find(h => h.textContent.trim() === '我的高亮');
    const inSummary = !!summary && !!(summary.compareDocumentPosition(range.startContainer) & Node.DOCUMENT_POSITION_FOLLOWING);
    pending = { text, before: pre.toString().slice(-200), rect: range.getBoundingClientRect(), inSummary };
    bar.replaceChildren(button('高亮', doHighlight));
    if (canExcerpt) bar.append(button('摘录', openExcerpt));
    place(pending.rect);
  }

  function showForMark(mark) {
    const same = [...doc.querySelectorAll('mark')].filter(m => m.textContent === mark.textContent);
    pending = { mark, text: mark.textContent, nth: same.indexOf(mark) };
    bar.replaceChildren(button('取消高亮', doUnhighlight));
    place(mark.getBoundingClientRect());
  }

  function openExcerpt() {
    const p = pending;
    if (!p) return;
    const box = document.createElement('div');
    box.className = 'excerpt-form';
    const quote = document.createElement('blockquote');
    quote.textContent = p.text.length > 160 ? `${p.text.slice(0, 160)}…` : p.text;
    const label = document.createElement('label');
    label.textContent = '想法（可以不填）';
    const ta = document.createElement('textarea');
    ta.rows = 3;
    label.append(ta);
    const row = document.createElement('div');
    row.className = 'row';
    row.append(button('取消', hide), button('存进摘录', () => doExcerpt(ta.value)));
    ta.addEventListener('keydown', e => {
      if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) doExcerpt(ta.value);
    });
    box.append(quote, label, row);
    bar.replaceChildren(box);
    place(p.rect);   // 框变大了，按原来的选区重新摆一次
    ta.focus();
  }

  async function post(url, body) {
    busy = true;
    try {
      const r = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: doc.dataset.path, mtime: doc.dataset.mtime, ...body }),
      });
      const d = await r.json().catch(() => ({ error: `服务出错（${r.status}）` }));
      if (!r.ok) throw new Error(d.error || '保存失败');
      // 换正文不换页面：滚动位置、阅读设置都留着
      doc.innerHTML = d.html;
      doc.dataset.mtime = d.mtime;
      getSelection().removeAllRanges();
      return d;
    } finally {
      busy = false;
    }
  }

  async function doHighlight() {
    const p = pending;
    hide();
    if (!p) return;
    try {
      await post(`${api}/highlight`, { text: p.text, before: p.before, in_summary: p.inSummary });
      say('已高亮，Obsidian 里也能看到');
    } catch (e) {
      say(e.message, { error: true });
    }
  }

  async function doUnhighlight() {
    const p = pending;
    hide();
    if (!p) return;
    try {
      await post(`${api}/highlight`, { text: p.text, nth: p.nth, remove: true });
      say('已取消高亮');
    } catch (e) {
      say(e.message, { error: true });
    }
  }

  async function doExcerpt(thought) {
    const p = pending;
    hide();
    if (!p) return;
    try {
      const d = await post(`${api}/excerpt`, { text: p.text, before: p.before, thought });
      say(d.note || '已存进摘录', { link: { href: d.excerpts_url, text: '打开摘录' } });
    } catch (e) {
      say(e.message, { error: true });
    }
  }

  // 鼠标、键盘（Shift+方向键）、触屏选完都走这里；等选区稳定了再弹
  let selTimer = null;
  document.addEventListener('selectionchange', () => {
    if (bar.contains(document.activeElement)) return;   // 在摘录框里打字
    clearTimeout(selTimer);
    const sel = getSelection();
    if (sel.isCollapsed) {
      if (pending && !pending.mark) hide();
      return;
    }
    selTimer = setTimeout(showForSelection, 250);
  });
  doc.addEventListener('click', e => {
    const mark = e.target.closest('mark');
    if (mark && getSelection().isCollapsed) showForMark(mark);
  });
  document.addEventListener('mousedown', e => {
    if (!bar.hidden && !bar.contains(e.target) && !e.target.closest('mark')) hide();
  });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && !bar.hidden) hide();
  });
}
