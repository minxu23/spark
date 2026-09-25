// 注入到笔记洞察演示里的标注脚本：选中幻灯片上的文字可以「高亮」，点已有高亮可以「取消高亮」。
// 高亮不写进演示文件，记在同名报告末尾的「我的高亮」（带「演示第 N 页」），打开时读回来标上。
(() => {
  const me = document.currentScript;
  const api = me.dataset.api;
  const deck = me.dataset.deck;
  const reportUrl = me.dataset.report;
  // 导出 PDF 时演示会把 #stage 整个重画，#slide 每次现取
  const slideOf = () => document.getElementById('slide');
  const stage = document.getElementById('stage');
  if (!stage || !slideOf()) return;

  let items = [];          // [{slide, text}]
  let pending = null;      // {text, slide, rect} 或 {text, slide, rect, remove: true}
  let toastTimer = null;

  const bar = document.createElement('div');
  bar.className = 'dk-bar';
  bar.hidden = true;
  bar.setAttribute('role', 'toolbar');
  bar.setAttribute('aria-label', '标注');
  const toast = document.createElement('div');
  toast.className = 'dk-toast';
  toast.setAttribute('role', 'status');
  toast.setAttribute('aria-live', 'polite');
  document.body.append(bar, toast);

  const curSlide = () => parseInt(document.getElementById('pageNo')?.textContent || '1', 10) || 1;
  const squash = (t) => t.replace(/\s+/g, ' ').trim();

  function say(msg, error = false) {
    toast.textContent = msg;
    toast.classList.toggle('err', error);
    if (!error && reportUrl) {
      const a = document.createElement('a');
      a.href = reportUrl;
      a.textContent = '看报告里的汇总';
      toast.append(' ', a);
    }
    toast.classList.add('on');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toast.classList.remove('on'), error ? 7000 : 4000);
  }

  // 在当前这页里找到这句话，包上 <mark>。一句话可能跨好几个文字节点（加粗、角标），逐段包
  function markText(text) {
    const slideEl = slideOf();
    if (!slideEl) return false;
    const nodes = [];
    const walker = document.createTreeWalker(slideEl, NodeFilter.SHOW_TEXT, {
      acceptNode: (n) => n.parentElement.closest('.cite, .printnote, mark') ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT,
    });
    let all = '';
    const map = [];   // all 里每个字符来自哪个节点的哪一位（空白压成一个）
    for (let n; (n = walker.nextNode());) {
      nodes.push(n);
      for (let i = 0; i < n.data.length; i++) {
        const ch = /\s/.test(n.data[i]) ? ' ' : n.data[i];
        if (ch === ' ' && all.endsWith(' ')) continue;
        all += ch;
        map.push([n, i]);
      }
    }
    const at = all.indexOf(squash(text));
    if (at < 0) return false;
    const end = at + squash(text).length;   // 不含
    const spans = new Map();
    for (let k = at; k < end; k++) {
      const [n, i] = map[k];
      const s = spans.get(n) || [i, i];
      s[1] = i;
      spans.set(n, s);
    }
    for (const [n, [a, b]] of spans) {
      const r = document.createRange();
      r.setStart(n, a);
      r.setEnd(n, b + 1);
      const m = document.createElement('mark');
      m.className = 'dk-mark';
      m.dataset.text = text;
      r.surroundContents(m);
    }
    return true;
  }

  function apply() {
    const n = curSlide();
    for (const it of items) if (it.slide === n) markText(it.text);
    observer.takeRecords();   // 自己包 <mark> 引起的变化不算翻页
  }

  // 翻页时演示整页重写 #slide 的内容：跟着再标一遍
  const observer = new MutationObserver(apply);
  observer.observe(stage, { childList: true, subtree: true });

  function hide() { bar.hidden = true; pending = null; }

  function place(rect) {
    bar.hidden = false;
    const w = bar.offsetWidth, h = bar.offsetHeight;
    let top = rect.top - h - 10;
    if (top < 44) top = rect.bottom + 10;
    bar.style.top = `${top}px`;
    bar.style.left = `${Math.min(Math.max(8, rect.left + rect.width / 2 - w / 2), innerWidth - w - 8)}px`;
  }

  function button(label, fn) {
    const b = document.createElement('button');
    b.type = 'button';
    b.textContent = label;
    b.addEventListener('mousedown', (e) => e.preventDefault());
    b.addEventListener('click', (e) => { e.stopPropagation(); fn(); });
    return b;
  }

  async function send(body) {
    const r = await fetch(`${api}/deck_highlight`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ deck, ...body }),
    });
    const d = await r.json().catch(() => ({ error: `服务出错（${r.status}）` }));
    if (!r.ok) throw new Error(d.error || '保存失败');
    items = d.items || [];
    getSelection().removeAllRanges();
    // 重画这一页：先去掉旧的 mark，再按最新列表标
    const slideEl = slideOf();
    for (const m of slideEl.querySelectorAll('mark.dk-mark')) m.replaceWith(...m.childNodes);
    slideEl.normalize();
    apply();
  }

  async function act() {
    const p = pending;
    hide();
    if (!p) return;
    try {
      await send({ text: p.text, slide: p.slide, remove: !!p.remove });
      say(p.remove ? '已取消高亮' : '已高亮，记在报告末尾「我的高亮」');
    } catch (e) {
      say(e.message, true);
    }
  }

  let selTimer = null;
  document.addEventListener('selectionchange', () => {
    clearTimeout(selTimer);
    const sel = getSelection();
    if (sel.isCollapsed) { if (pending && !pending.remove) hide(); return; }
    selTimer = setTimeout(() => {
      const s = getSelection();
      if (!s.rangeCount || s.isCollapsed) return;
      const range = s.getRangeAt(0);
      if (!slideOf()?.contains(range.commonAncestorContainer)) return;
      const text = squash(s.toString());
      if (!text) return;
      pending = { text, slide: curSlide() };
      bar.replaceChildren(button('高亮', act));
      place(range.getBoundingClientRect());
    }, 250);
  });

  stage.addEventListener('click', (e) => {
    const m = e.target.closest('mark.dk-mark');
    if (!m || !getSelection().isCollapsed) return;
    pending = { text: m.dataset.text, slide: curSlide(), remove: true };
    bar.replaceChildren(button('取消高亮', act));
    place(m.getBoundingClientRect());
  });
  document.addEventListener('mousedown', (e) => {
    if (!bar.hidden && !bar.contains(e.target) && !e.target.closest('mark.dk-mark')) hide();
  });
  // 翻页时收起工具条（演示自己的方向键 / 空格翻页照常）
  document.addEventListener('keydown', () => { if (!bar.hidden) hide(); });

  fetch(`${api}/deck_highlights?deck=${encodeURIComponent(deck)}`)
    .then((r) => r.json())
    .then((d) => { items = d.items || []; apply(); })
    .catch(() => {});
})();
