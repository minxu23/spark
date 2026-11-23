// 随手记的编辑页（server.py jotting_editor）：标题 + 正文，「保存」或 ⌘S 存。
// 打的字随时存进浏览器本地（localStorage），页面关了、浏览器崩了，再打开这一页会换上没保存的草稿。
// 保存时带上打开时那一版的指纹（data-base）：文件在别处（手机、Obsidian）改过，服务端不覆盖、说清楚，字留在框里。
const form = document.getElementById('jt-edit');

if (form) {
  const api = form.dataset.api;
  const title = form.querySelector('#jt-title');
  const body = form.querySelector('#jt-body');
  const status = form.querySelector('.jt-status');
  const save = form.querySelector('.jt-save');
  const cancel = form.querySelector('.jt-cancel');
  const key = `spark.jotting.draft:${form.dataset.path || 'new'}`;
  const original = { title: title.value, body: body.value };
  // 保存时带的指纹：换上的草稿是在哪一版上写的就带哪一版，文件后来又变过服务端会拒绝，不会悄悄盖掉
  let base = form.dataset.base || '';
  let saving = false;
  let done = false;

  const readDraft = () => { try { return JSON.parse(localStorage.getItem(key) || 'null'); } catch { return null; } };
  const writeDraft = () => {
    try { localStorage.setItem(key, JSON.stringify({ title: title.value, body: body.value, base, at: Date.now() })); } catch { /* 存不了就算了 */ }
  };
  const clearDraft = () => { try { localStorage.removeItem(key); } catch { /* 同上 */ } };
  const dirty = () => title.value !== original.title || body.value !== original.body;
  const empty = () => !title.value.trim() && !body.value.trim();

  function say(text, { error = false, action = null } = {}) {
    status.textContent = text;
    status.classList.toggle('err', error);
    if (action) {
      const b = document.createElement('button');
      b.type = 'button';
      b.textContent = action.label;
      b.addEventListener('click', action.run);
      status.append(b);
    }
  }

  const draft = readDraft();
  if (draft && (draft.title !== original.title || draft.body !== original.body)) {
    title.value = draft.title || '';
    body.value = draft.body || '';
    if (typeof draft.base === 'string') base = draft.base;
    say('换上了上次没保存的草稿。', {
      action: {
        label: '丢掉草稿',
        run: () => {
          title.value = original.title;
          body.value = original.body;
          base = form.dataset.base || '';
          clearDraft();
          say('');
          sync();
        },
      },
    });
  } else if (draft) {
    clearDraft();   // 草稿跟现在的一样：没用了
  }

  function sync() { save.disabled = saving || empty(); }
  sync();

  for (const el of [title, body]) {
    el.addEventListener('input', () => {
      if (dirty()) writeDraft(); else clearDraft();
      sync();
    });
  }

  async function submit() {
    if (saving || empty()) return;
    saving = true;
    sync();
    say('正在保存…');
    try {
      const r = await fetch(`${api}/jottings/save`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: form.dataset.path || '', base, title: title.value, body: body.value }),
      });
      const d = await r.json().catch(() => ({ error: `服务出错（${r.status}）` }));
      if (!r.ok) throw new Error(d.error || '没保存上');
      clearDraft();
      done = true;
      say('已保存');
      setTimeout(() => { location.href = d.url; }, 500);
    } catch (e) {
      writeDraft();   // 没保存上：草稿留着
      say(e.message, { error: true });
      saving = false;
      sync();
    }
  }

  form.addEventListener('submit', e => { e.preventDefault(); submit(); });
  document.addEventListener('keydown', e => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 's') { e.preventDefault(); submit(); }
  });
  cancel.addEventListener('click', e => {
    if (!dirty()) { clearDraft(); return; }
    if (!confirm('放弃这次修改？没保存的字会删掉。')) { e.preventDefault(); return; }
    clearDraft();
    done = true;
  });
  // 关标签页、点了别的链接：有没保存的字先问一句（草稿已经在本地，回来还在）
  addEventListener('beforeunload', e => {
    if (!done && dirty()) e.preventDefault();
  });
  (form.dataset.path ? body : title).focus();
}
