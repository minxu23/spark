// 论文阅读页：导读 / 原文 / PDF 三个页签，和正文后面的「追问」（core/paper_read.py）。
// 两个文字页签是正文里的两块（.paper-pane），切换只改 article 上的 data-pane；划线重排正文时 article 本身不换，页签留着。
// PDF 页签是正文外面的 iframe，第一次切过去才填地址。追问的对话框是复盘那边的 SparkChat（/review/static/discuss.js）。
(() => {
  const doc = document.querySelector('article.doc[data-path]');
  const bar = document.querySelector('.paper-tabs');
  const pdf = document.querySelector('.paper-pdf');
  if (!doc) return;

  function show(pane) {
    if (!bar) return;
    doc.dataset.pane = pane;
    doc.hidden = pane === 'pdf';
    for (const b of bar.querySelectorAll('button[data-pane]')) b.setAttribute('aria-selected', String(b.dataset.pane === pane));
    if (pdf) {
      pdf.hidden = pane !== 'pdf';
      const frame = pdf.querySelector('iframe');
      if (pane === 'pdf' && frame && !frame.getAttribute('src') && frame.dataset.src) frame.src = frame.dataset.src;
    }
  }

  // 参考文献一节默认折起：目录、页内锚点跳到它里面的标题时先展开（标题本身是折叠条，点它也是切换）
  const openRefs = () => {
    const id = decodeURIComponent(location.hash.slice(1));
    const refs = id ? document.getElementById(id)?.closest('details.refs') : null;
    if (refs && !refs.open) { refs.open = true; document.getElementById(id).scrollIntoView(); }
  };
  addEventListener('hashchange', openRefs);
  document.addEventListener('click', e => {
    const a = e.target.closest('a[href^="#"]');
    const refs = a ? document.getElementById(decodeURIComponent(a.hash.slice(1)))?.closest('details.refs') : null;
    if (refs) refs.open = true;   // 地址栏的 # 没变时不会触发 hashchange
  });
  openRefs();

  if (bar) {
    show(doc.dataset.pane || 'guide');
    bar.addEventListener('click', e => {
      const b = e.target.closest('button[data-pane]');
      if (b) show(b.dataset.pane);
    });
    // 目录、页内锚点跳到另一个文字页签里的标题：先切过去
    const follow = () => {
      const id = decodeURIComponent(location.hash.slice(1));
      const target = id ? document.getElementById(id) : null;
      const pane = target?.closest('.paper-pane');
      if (pane && pane.dataset.pane !== doc.dataset.pane) { show(pane.dataset.pane); target.scrollIntoView(); }
      else if (target && doc.hidden && doc.contains(target)) { show('guide'); target.scrollIntoView(); }
    };
    addEventListener('hashchange', follow);
    follow();
  }

  // ---------------------------------------------------------------- 追问
  const box = document.querySelector('.paper-ask');
  if (!box || !window.SparkChat) return;
  const sleep = ms => new Promise(ok => setTimeout(ok, ms));

  async function post(url, body) {
    const r = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.error || `请求失败（${r.status}）`);
    return d;
  }

  async function ask(question, history, stage) {
    const { job_id: id } = await post(box.dataset.ask, { path: box.dataset.paper, question, history });
    for (;;) {
      const d = await (await fetch(box.dataset.progress + id)).json();
      if (d.error && !d.done) throw new Error(d.error);
      if (d.done) {
        if (!d.ok) throw new Error(d.error || '没回答出来');
        return d.result;
      }
      stage(d.stage ? `正在回答：${d.stage}` : '正在回答…');
      await sleep(1000);
    }
  }

  // 存进本文：服务端重新读盘再接到文末「## 追问」一节，回来的是重排好的正文；换上它，修改时间跟着换（不然下一次划线会说文件改过了）
  // 和划线、批注、读后感共用 reader.js 的 busy：别的写操作在跑时不存，存的时候别的也等着。
  // 页面上有没存的读后感输入、或者正在起草：不换正文（会冲掉），只换修改时间，提示刷新后再看
  async function save(turns) {
    const w = window.SparkDocWrite;
    if (w?.busy()) throw new Error('上一个修改还在保存，等一下再存');
    w?.setBusy(true);
    try {
      const d = await post(`${doc.dataset.api}/paper_ask/save`, { path: doc.dataset.path, view: doc.dataset.view || '',
                                                                  turns: turns.map(t => ({ q: t.q, a: t.a })) });
      doc.dataset.mtime = d.mtime;
      if (w?.dirty()) return { message: '已存进本文，刷新后可见（读后感还没存，先没换正文）。' };
      const metaOpen = doc.querySelector('.docmeta')?.open;
      const refsOpen = doc.querySelector('.refs')?.open;
      doc.innerHTML = d.html;
      if (metaOpen) doc.querySelector('.docmeta')?.setAttribute('open', '');
      if (refsOpen) doc.querySelector('.refs')?.setAttribute('open', '');
      return { message: '已存进本文：在「导读」页签末尾的「追问」一节。' };
    } finally {
      w?.setBusy(false);
    }
  }

  window.SparkChat.mount(box.querySelector('.chat'), {
    ask, save, saveLabel: '存进本文', incremental: true,
    placeholder: '问这篇论文，Ctrl+Enter 发送',
  });
})();
