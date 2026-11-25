// 对话界面：给一个「发问题的函数」和一个「保存的函数」就能用（复盘页的「讨论」、论文阅读页的「追问」共用）。
//   SparkChat.mount(box, { ask, save, placeholder, saveLabel, incremental })
//   ask(question, history, stage) → Promise<{ answer, sources }>   history：[{q, a}]；stage(文字)：显示等待中的进度
//   save(turns) → Promise<{ url, rel } | { message }>             turns：[{q, a, sources}]
//   saveLabel：保存按钮的字（默认「存成复盘」）；incremental：只交上次存过以后新问的几组（追问往同一篇里接，不重复写）
// 页面上的文字一律 textContent 写入；回答没有现成的 Markdown 渲染可用，按纯文本加换行显示，出处单独列在下面。
(function () {
  "use strict";
  const el = (tag, props, ...kids) => { const e = Object.assign(document.createElement(tag), props); e.append(...kids); return e; };

  function mount(box, opts) {
    let turns = [];
    let busy = false;
    const log = el("div", { className: "chat-log" });
    const input = el("textarea", { className: "chat-input", rows: 3, placeholder: opts.placeholder || "输入问题，Ctrl+Enter 发送" });
    input.setAttribute("aria-label", "问题");
    const send = el("button", { type: "button", className: "primary", textContent: "发送" });
    const save = el("button", { type: "button", className: "plain", textContent: opts.saveLabel || "存成复盘", disabled: true });
    let saved = 0;   // incremental：前几组已经存过
    const reset = el("button", { type: "button", className: "plain", textContent: "重新开始", disabled: true });
    const status = el("p", { className: "hint", role: "status" });
    status.setAttribute("aria-live", "polite");
    box.replaceChildren(log, input, el("div", { className: "actions" }, send, save, reset), status);

    const say = (text, err) => { status.textContent = text; status.className = err ? "hint err" : "hint"; };
    const sync = () => {
      input.disabled = send.disabled = busy;
      save.disabled = busy || turns.length <= (opts.incremental ? saved : 0);
      reset.disabled = busy || (!turns.length && !input.value);
    };

    function sourceList(sources) {
      if (!sources.length) return null;
      return el("ul", { className: "chat-sources", "aria-label": "出处" }, ...sources.map((s) => {
        const meta = [s.source, s.date, s.kind].filter(Boolean).join("　");
        return el("li", {}, el("span", { className: "chat-n", textContent: `[${s.n}]` }),
                  s.url ? el("a", { href: s.url, textContent: s.title }) : el("span", { textContent: s.title }),
                  el("small", { textContent: meta }));
      }));
    }

    function draw() {
      log.replaceChildren(...turns.map((t) => el("div", { className: "chat-turn" },
        el("p", { className: "chat-q", textContent: t.q }),
        el("p", { className: "chat-a", textContent: t.a }),
        sourceList(t.sources) || "")));
      log.hidden = !turns.length;
      sync();
      if (turns.length) log.lastElementChild.scrollIntoView({ block: "nearest" });
    }

    async function submit() {
      const q = input.value.trim();
      if (!q || busy) return;
      busy = true; sync(); say("正在回答…");
      try {
        const r = await opts.ask(q, turns.map((t) => ({ q: t.q, a: t.a })), (text) => say(text));
        turns.push({ q, a: r.answer, sources: r.sources || [] });
        input.value = "";
        say("");
      } catch (e) {
        say(e.message, true);   // 问题留在输入框里，可以改了再发
      }
      busy = false; draw(); input.focus();
    }

    send.addEventListener("click", submit);
    input.addEventListener("keydown", (ev) => { if (ev.key === "Enter" && (ev.ctrlKey || ev.metaKey)) { ev.preventDefault(); submit(); } });
    input.addEventListener("input", sync);
    const restart = () => { turns = []; saved = 0; input.value = ""; say(""); draw(); input.focus(); };
    reset.addEventListener("click", restart);
    save.addEventListener("click", async () => {
      busy = true; sync(); say("正在存…");
      try {
        const r = await opts.save(opts.incremental ? turns.slice(saved) : turns);
        if (opts.incremental) saved = turns.length;
        status.className = "hint";
        if (r && r.url) status.replaceChildren("存好了：", el("a", { href: r.url, textContent: r.rel }));
        else status.textContent = (r && r.message) || "存好了";
      } catch (e) {
        say(`没存上：${e.message}`, true);
      }
      busy = false; sync();
    });
    draw();
    return { hasTurns: () => turns.length > 0, busy: () => busy, reset: restart, focus: () => input.focus() };
  }

  // ---------------------------------------------------------------- 复盘页的「讨论」：问笔记库，可以限定在一个标签下
  async function post(path, body) {
    const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.error || `请求失败（${r.status}）`);
    return d;
  }

  const sleep = (ms) => new Promise((ok) => setTimeout(ok, ms));
  let scope = "";   // 空：整个笔记库
  let chat = null;

  async function askLibrary(question, history, stage) {
    const { job_id: id } = await post("api/discuss", { question, history, tag: scope });
    for (;;) {
      const d = await (await fetch(`api/progress/${id}`)).json();
      if (d.error && !d.done) throw new Error(d.error);
      if (d.done) {
        if (!d.ok) throw new Error(d.error || "没回答出来");
        return d.result;
      }
      stage(d.stage ? `正在回答：${d.stage}` : "正在回答…");
      await sleep(1000);
    }
  }

  function showScope() {
    const $ = (id) => document.getElementById(id);
    $("discussTitle").textContent = scope ? `讨论：${scope}` : "讨论";
    $("discussWide").hidden = !scope;
    $("discussHint").textContent = scope ? `只在「${scope}」这个主题下的文章里找。` : "在整个笔记库里找相关内容，再回答。回答里的 [编号] 对应下面列出的出处。";
  }

  function setScope(tag) {
    if (tag === scope) return true;
    if (chat && chat.busy()) { window.alert("上一个问题还在回答，等它结束再换范围。"); return false; }
    if (chat && chat.hasTurns() && !window.confirm("换了范围，现在的讨论会清掉（没存的就没了）。继续吗？")) return false;
    scope = tag;
    if (chat) chat.reset();
    showScope();
    return true;
  }

  window.SparkDiscuss = {
    init() {
      chat = mount(document.getElementById("chat"), {
        ask: askLibrary,
        save: (turns) => post("api/discuss/save", { tag: scope, turns }),
      });
      document.getElementById("discussWide").addEventListener("click", () => setScope(""));
      showScope();
    },
    // 主题页某个标签的文章列表上的「讨论」按钮：切到讨论页，范围限定在这个标签
    open(tag) {
      if (!setScope(tag || "")) return;
      window.SparkSetTab("discuss");
      chat.focus();
    },
    show() { if (chat) chat.focus(); },
  };
  window.SparkChat = { mount };
})();
