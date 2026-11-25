// 顶栏「Aa」面板：配色、字体、字号、栏宽、行间距、段间距。点了立刻生效，存在浏览器本地。
const aa = document.getElementById('aa');
const panel = document.getElementById('prefs');

if (aa && panel && window.SparkRead) {
  const { SIZES, CORE, load, apply, save, fontLabel } = window.SparkRead;
  const sizeNow = document.getElementById('size-now');
  const fontRow = panel.querySelector('.row.fonts');
  let prefs = load();
  // 设置页里选的字体不在这三个常用选项里时，多出一个按钮显示它，别被悄悄换回宋体；
  // 在这页切到常用字体后按钮还留着，方便切回去
  let more = null;

  function syncMore() {
    if (CORE.includes(prefs.font)) return;
    if (!more) {
      more = document.createElement('button');
      more.type = 'button';
      more.className = 'opt more read-scope';
      fontRow.append(more);
    }
    more.dataset.font = prefs.font;
    more.textContent = `当前：${fontLabel(prefs)}`;
    more.title = '在设置页里选的字体';
  }

  // 没调过字号时，从页面实际字号推出当前档位
  const currentSize = () => Number.isInteger(prefs.size) ? prefs.size
    : Math.max(0, SIZES.indexOf(Math.round(parseFloat(getComputedStyle(document.body).fontSize))));

  // 设置页导入的主题：用着的时候在配色下面显示名字和「换回内置配色」
  const ctRow = document.getElementById('custom-theme');
  const ctName = document.getElementById('ct-name');

  function sync() {
    syncMore();
    for (const key of ['theme', 'font', 'width', 'lineGap', 'paraGap']) {
      const attr = key.replace(/[A-Z]/g, c => '-' + c.toLowerCase());   // lineGap → data-line-gap
      for (const b of panel.querySelectorAll(`[data-${attr}]`)) {
        // 用着导入的主题时，内置配色一个都不算选中
        const on = b.dataset[key] === prefs[key] && !(key === 'theme' && prefs.customTheme);
        b.setAttribute('aria-pressed', String(on));
      }
    }
    if (ctRow) {
      ctRow.hidden = !prefs.customTheme;
      ctName.textContent = prefs.customThemeName || '导入的主题';
    }
    const i = currentSize();
    sizeNow.textContent = `${SIZES[i]}px`;
    panel.querySelector('[data-size="-1"]').disabled = i <= 0;
    panel.querySelector('[data-size="+1"]').disabled = i >= SIZES.length - 1;
  }

  function set(change) {
    prefs = { ...load(), ...change };   // 先重读，别把另一个标签页刚改的别的项覆盖掉
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
  // theme.js 收到别的标签页（比如设置页）的改动后发这个事件
  document.addEventListener('spark-read-prefs', e => { prefs = e.detail; sync(); });
  // 导入的主题文件找不到了（在设置页删掉了）：read-prefs.js 已经退回内置配色，这里刷新按钮
  document.addEventListener('spark-theme-missing', () => { prefs = load(); sync(); });
  sync();

  panel.addEventListener('click', e => {
    const b = e.target.closest('button');
    if (!b) return;
    if (b.dataset.size) {
      const i = Math.min(SIZES.length - 1, Math.max(0, currentSize() + Number(b.dataset.size)));
      set({ size: i });
    } else if (b.id === 'ct-off') {
      set({ customTheme: '', customThemeName: '' });
    } else if (b.dataset.theme) {
      // 点内置配色 = 不用导入的主题了
      set({ theme: b.dataset.theme, customTheme: '', customThemeName: '' });
    } else {
      for (const key of ['font', 'width', 'lineGap', 'paraGap']) if (b.dataset[key]) set({ [key]: b.dataset[key] });
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
  // 停留 20 秒才算读过：只点开看一眼不进阅读记录（core/readlog.py），复盘才准。
  // 只算页面在前台的时间，切走再回来接着累计
  const DWELL_MS = 20000;
  let seen = 0;
  let since = document.visibilityState === 'visible' ? Date.now() : null;
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') since = since ?? Date.now();
    else if (since !== null) { seen += Date.now() - since; since = null; }
  });
  const dwell = setInterval(() => {
    if (seen + (since === null ? 0 : Date.now() - since) < DWELL_MS) return;
    clearInterval(dwell);
    fetch(`${api}/read_ping`, { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                body: JSON.stringify({ path: doc.dataset.path }) }).catch(() => {});
  }, 1000);
  const canExcerpt = doc.dataset.excerptable === 'true';
  // 能做笔记的（剪藏、整理稿、文字记录、会议资料、报告……）：批注、撤销都开，规则在服务端 core/annotations.py
  const canNote = doc.dataset.notes === 'true';
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
  // 别的脚本（论文的「存进本文」，static/paper-read.js）写文件时也走这个 busy，和划线、批注、读后感排开；
  // dirty：页面上有没存的读后感输入或正在起草，这时换正文会把它冲掉
  window.SparkDocWrite = {
    busy: () => busy,
    setBusy: (v) => { busy = v; },
    dirty: () => !!(doc.querySelector('.refl-input')?.value.trim() || doc.querySelector('.refl-draft[aria-busy="true"]')),
  };

  function say(msg, { error = false, link = null, undo = false } = {}) {
    toast.classList.toggle('err', error);
    toast.textContent = msg;
    if (link) {
      const a = document.createElement('a');
      a.href = link.href;
      a.textContent = link.text;
      toast.append(' ', a);
    }
    // 划线、批注改完底下留 5 秒「撤销」（同手机）
    if (undo && canNote) {
      const u = document.createElement('button');
      u.type = 'button';
      u.className = 'undo';
      u.textContent = '撤销';
      u.addEventListener('click', async () => {
        toast.classList.remove('on');
        try { await post(`${api}/undo`, {}); say('已撤销'); } catch (e) { say(e.message, { error: true }); }
      });
      toast.append(' ', u);
    }
    toast.classList.add('on');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toast.classList.remove('on'), error ? 7000 : undo && canNote ? 5000 : 4500);
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
    // frontmatter 和元信息折叠条上的那行字不在正文源码里，选了也对不上
    return !!el && doc.contains(el) && !el.closest('.meta, .docmeta > summary, .reflection');
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
    if (canNote) bar.append(button('批注', openNote));
    if (canExcerpt) bar.append(button('摘录', openExcerpt));
    place(pending.rect);
  }

  function showForMark(mark) {
    const same = [...doc.querySelectorAll('mark')].filter(m => m.textContent === mark.textContent);
    pending = { mark, text: mark.textContent, nth: same.indexOf(mark), rect: mark.getBoundingClientRect() };
    // 点已划的字：批注、取消高亮、拷贝（同手机）
    bar.replaceChildren();
    if (canNote) bar.append(button('批注', openNote));
    bar.append(button('取消高亮', doUnhighlight), button('拷贝', () => {
      navigator.clipboard?.writeText(mark.textContent).then(() => say('已拷贝'), () => say('没拷贝上', { error: true }));
      hide();
    }));
    place(pending.rect);
  }

  // 批注框：新写（选中的字或点中的划线）和看、改、删已有的（点小气泡）用同一个框
  function noteBox({ quote, value = '', save, remove = null }) {
    const box = document.createElement('div');
    box.className = 'excerpt-form note-form';
    if (quote) {
      const q = document.createElement('blockquote');
      q.textContent = quote.length > 160 ? `${quote.slice(0, 160)}…` : quote;
      box.append(q);
    }
    const label = document.createElement('label');
    label.textContent = '批注';
    const ta = document.createElement('textarea');
    ta.rows = 3;
    ta.maxLength = 2000;
    ta.value = value;
    label.append(ta);
    const row = document.createElement('div');
    row.className = 'row';
    if (remove) {
      const del = button('删除批注', remove);
      del.className = 'danger';
      row.append(del);
    }
    row.append(button(remove ? '关闭' : '取消', hide), button(remove ? '保存' : '存批注', () => save(ta.value)));
    ta.addEventListener('keydown', e => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) save(ta.value); });
    box.append(label, row);
    bar.replaceChildren(box);
    return ta;   // 先摆好位置（框显示出来）再聚焦，隐藏着聚焦不上，选区一变框就被收起
  }

  function openNote() {
    const p = pending;
    if (!p) return;
    const ta = noteBox({ quote: p.text, save: v => doNote(p, v) });
    place(p.rect);
    ta.focus();
  }

  function showNote(ref) {
    const id = Number(ref.dataset.note);
    pending = { ref, rect: ref.getBoundingClientRect() };
    const mark = ref.previousElementSibling?.tagName === 'MARK' ? ref.previousElementSibling.textContent : '';
    const ta = noteBox({
      quote: mark, value: ref.dataset.text,
      save: v => noteChange({ id, note: v }, '批注改好了'),
      remove: () => noteChange({ id, remove: true }, '已删掉这条批注，划线还在'),
    });
    place(pending.rect);
    ta.focus();
  }

  async function doNote(p, note) {
    if (!note.trim()) { say('批注不能是空的', { error: true }); return; }
    hide();
    const body = p.mark ? { text: p.text, nth: p.nth, note } : { text: p.text, before: p.before, note };
    try {
      await post(`${api}/note`, body);
      say('批注存好了，Obsidian 里是脚注', { undo: true });
    } catch (e) {
      say(e.message, { error: true });
    }
  }

  async function noteChange(body, done) {
    if (!body.remove && !body.note.trim()) { say('批注不能是空的', { error: true }); return; }
    hide();
    try {
      await post(`${api}/note`, body);
      say(done, { undo: true });
    } catch (e) {
      say(e.message, { error: true });
    }
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
        body: JSON.stringify({ path: doc.dataset.path, mtime: doc.dataset.mtime, view: doc.dataset.view || '', ...body }),
      });
      const d = await r.json().catch(() => ({ error: `服务出错（${r.status}）` }));
      if (!r.ok) throw new Error(d.error || '保存失败');
      // 换正文不换页面：滚动位置、阅读设置、元信息展没展开都留着
      const metaOpen = doc.querySelector('.docmeta')?.open;
      doc.innerHTML = d.html;
      if (metaOpen) doc.querySelector('.docmeta')?.setAttribute('open', '');
      doc.dataset.mtime = d.mtime;
      getSelection().removeAllRanges();
      setupImages();
      return d;
    } finally {
      busy = false;
    }
  }

  // 剪藏里的图：没下下来的显示「重新下载 / 删除这张图」，下好的鼠标移上去能删（同手机）
  const isClip = doc.dataset.path.startsWith('剪藏/');
  const localSrc = img => {
    const src = img.getAttribute('src') || '';
    return /^attachments\/[0-9a-f]{16}\.(jpg|png|gif|webp)$/.test(src) ? src : '';
  };

  async function imageAction(src, action, button) {
    if (busy) return;
    if (action === 'remove' && !confirm('从这篇剪藏里删掉这张图？')) return;
    if (button) { button.disabled = true; button.textContent = action === 'retry' ? '正在下载…' : '正在删除…'; }
    try {
      await post(`${api}/clip_image`, { src, action });
      say(action === 'retry' ? '图片下好了' : '已删掉这张图');
    } catch (e) {
      say(e.message, { error: true });
      setupImages();
    }
  }

  function failed(img) {
    const src = localSrc(img);
    const box = document.createElement('div');
    box.className = 'imgfail';
    const label = document.createElement('span');
    label.textContent = img.alt || '图片没有下载下来';
    const retry = document.createElement('button');
    retry.type = 'button';
    retry.textContent = '重新下载';
    retry.addEventListener('click', () => imageAction(src, 'retry', retry));
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'danger';
    remove.textContent = '删除这张图';
    remove.addEventListener('click', () => imageAction(src, 'remove', remove));
    box.append(label, retry, remove);
    (img.closest('.imgbox') || img).replaceWith(box);
  }

  function setupImages() {
    if (!isClip) return;
    for (const img of doc.querySelectorAll('img')) {
      const src = localSrc(img);
      if (!src || img.closest('.imgbox')) continue;
      if (img.complete && img.naturalWidth === 0) { failed(img); continue; }
      img.addEventListener('error', () => failed(img), { once: true });
      const box = document.createElement('span');
      box.className = 'imgbox';
      img.replaceWith(box);
      const del = document.createElement('button');
      del.type = 'button';
      del.className = 'imgdel';
      del.textContent = '删除';
      del.title = '删除这张图';
      del.setAttribute('aria-label', '删除这张图');
      del.addEventListener('click', () => imageAction(src, 'remove', null));
      box.append(img, del);
    }
  }
  setupImages();

  // 剪藏文末的读后感卡：点「写读后感 / 改读后感」就地换成输入框（同手机）
  doc.addEventListener('click', e => {
    const edit = e.target.closest('.refl-edit');
    if (!edit || busy) return;
    const card = edit.closest('.reflection');
    const ta = document.createElement('textarea');
    ta.className = 'refl-input';
    ta.rows = 6;
    ta.maxLength = 20000;
    ta.value = card.dataset.raw || '';
    ta.setAttribute('aria-label', '读后感');
    ta.placeholder = '读完有什么想法';
    const row = document.createElement('div');
    row.className = 'refl-row';
    const save = document.createElement('button');
    save.type = 'button';
    save.textContent = '保存';
    const cancel = document.createElement('button');
    cancel.type = 'button';
    cancel.textContent = '取消';
    row.append(save, cancel);
    // 配好了模型、这篇有划线或批注：先让模型按划线和批注起个草稿再改（同手机），草稿不自动存
    let drafting = false;
    if (card.dataset.draft) {
      const draft = document.createElement('button');
      draft.type = 'button';
      draft.className = 'refl-draft';
      const label = () => { draft.textContent = drafting ? '正在起草…' : (ta.value.trim() ? '重新起草（会替换上面的字）' : '让模型按划线和批注起个草稿'); };
      label();
      ta.addEventListener('input', label);
      draft.addEventListener('click', async () => {
        if (drafting) return;
        drafting = true;
        draft.disabled = save.disabled = true;
        draft.setAttribute('aria-busy', 'true');
        label();
        try {
          const r = await fetch(`${api}/reflection/draft`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ path: doc.dataset.path }),
          });
          const d = await r.json().catch(() => ({ error: `服务出错（${r.status}）` }));
          if (!r.ok) throw new Error(d.error || '没起草成');
          if (ta.isConnected) { ta.value = d.text || ''; ta.focus(); }
        } catch (err) {
          say(err.message, { error: true });
        } finally {
          drafting = false;
          draft.disabled = save.disabled = false;
          draft.removeAttribute('aria-busy');
          label();
        }
      });
      row.prepend(draft);
    }
    const keep = [...card.children].filter(c => c.tagName !== 'H2');
    keep.forEach(c => { c.hidden = true; });
    card.append(ta, row);
    ta.focus();
    const close = () => { ta.remove(); row.remove(); keep.forEach(c => { c.hidden = false; }); };
    cancel.addEventListener('click', close);
    const submit = async () => {
      save.disabled = true;
      try {
        await post(`${api}/reflection`, { text: ta.value });
        say(ta.value.trim() ? '读后感存好了' : '读后感清空了', { undo: true });
      } catch (err) {
        save.disabled = false;
        say(err.message, { error: true });
      }
    };
    save.addEventListener('click', submit);
    ta.addEventListener('keydown', ev => {
      if (ev.key === 'Enter' && (ev.metaKey || ev.ctrlKey)) submit();
      if (ev.key === 'Escape') close();
    });
  });

  // 复盘末尾：继续追问（同手机），答案接在复盘后面，以后打开还在
  if (doc.dataset.path.startsWith('@r/复盘/')) {
    const ask = document.createElement('form');
    ask.className = 'followup';
    ask.innerHTML = '<label for="followup-q">继续追问</label>'
      + '<textarea id="followup-q" rows="2" maxlength="500" placeholder="根据这份复盘和它用到的原文回答，答案接在复盘末尾"></textarea>'
      + '<div><button type="submit">提问</button><span class="followup-status" role="status"></span></div>';
    doc.after(ask);
    const q = ask.querySelector('textarea');
    const btn = ask.querySelector('button');
    const status = ask.querySelector('.followup-status');
    ask.addEventListener('submit', async e => {
      e.preventDefault();
      const question = q.value.trim();
      if (!question || busy) return;
      btn.disabled = true;
      status.textContent = '正在回答，大约要半分钟…';
      try {
        await post(`${api}/followup`, { question });
        q.value = '';
        status.textContent = '';
        doc.querySelectorAll('h2[id]').forEach((h, i, all) => { if (i === all.length - 1) h.scrollIntoView({ block: 'start' }); });
        say('答案接在复盘末尾了');
      } catch (err) {
        status.textContent = '';
        say(err.message, { error: true });
      } finally {
        btn.disabled = false;
      }
    });
    q.addEventListener('keydown', e => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) ask.requestSubmit(); });
  }

  async function doHighlight() {
    const p = pending;
    hide();
    if (!p) return;
    try {
      await post(`${api}/highlight`, { text: p.text, before: p.before, in_summary: p.inSummary });
      say('已高亮，Obsidian 里也能看到', { undo: true });
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
      say('已取消高亮', { undo: true });
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
      if (pending && !pending.mark && !pending.ref) hide();
      return;
    }
    selTimer = setTimeout(showForSelection, 250);
  });
  doc.addEventListener('click', e => {
    const ref = e.target.closest('.note-ref');
    if (ref) { showNote(ref); return; }
    const mark = e.target.closest('mark');
    if (mark && getSelection().isCollapsed && !mark.closest('.reflection')) showForMark(mark);
  });
  document.addEventListener('mousedown', e => {
    if (!bar.hidden && !bar.contains(e.target) && !e.target.closest('mark, .note-ref')) hide();
  });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && !bar.hidden) hide();
  });
}

// 目录：宽屏是左边的侧栏，窄屏是正文上面折起来的一块。点条目就是普通的 #锚点 跳转，
// 地址栏的 # 跟着变、浏览器后退能回去；这里只管两件事：窄屏点完把目录收起，侧栏标出正在读的那一节。
const tocSide = document.querySelector('.toc-side');
const tocTop = document.querySelector('.toc-top');

if (tocTop) {
  tocTop.addEventListener('click', e => {
    if (e.target.closest('a[href^="#"]')) tocTop.open = false;
  });
}

if (tocSide) {
  const links = [...tocSide.querySelectorAll('a[href^="#"]')];
  const ids = links.map(a => decodeURIComponent(a.hash.slice(1)));
  let current = null;
  let ticking = false;

  function mark() {
    ticking = false;
    if (!tocSide.offsetParent) return;   // 窄屏上侧栏不显示
    // 标题每次现找：高亮后正文整个换过，旧节点已经不在页面上了
    let hit = -1;
    for (let i = 0; i < ids.length; i++) {
      const h = document.getElementById(ids[i]);
      if (!h) continue;
      if (h.getBoundingClientRect().top <= 90) hit = i; else break;
    }
    const a = links[hit] || null;
    if (a === current) return;
    current?.removeAttribute('aria-current');
    current = a;
    if (!a) return;
    a.setAttribute('aria-current', 'location');
    // 侧栏自己能滚：当前条目跑出侧栏可见范围时挪回来（只滚侧栏，不动页面）
    const top = a.offsetTop, bottom = top + a.offsetHeight;
    if (top < tocSide.scrollTop + 40) tocSide.scrollTop = Math.max(0, top - 40);
    else if (bottom > tocSide.scrollTop + tocSide.clientHeight - 40) tocSide.scrollTop = bottom - tocSide.clientHeight + 40;
  }

  addEventListener('scroll', () => {
    if (!ticking) { ticking = true; requestAnimationFrame(mark); }
  }, { passive: true });
  addEventListener('resize', mark);
  addEventListener('hashchange', mark);
  mark();
}

// 读书笔记页：复制成 Markdown
document.addEventListener('click', e => {
  const b = e.target.closest('.rn-copy');
  if (!b) return;
  navigator.clipboard?.writeText(b.dataset.md).then(() => {
    const was = b.textContent;
    b.textContent = '已复制';
    setTimeout(() => { b.textContent = was; }, 1600);
  }, () => { b.textContent = '没复制上'; });
});
