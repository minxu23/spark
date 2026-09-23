// 两个 app（summit2md、notes2insight）共用的前端小工具。各 app 的 index.html 先加载
// 这个文件，再加载自己的 app.js；app.js 从 window.SparkCommon 里取用。
// 由各 app 的 /common/<文件> 路由提供（见 server.py），单独跑某个 app 时也能加载到。
(function () {
  "use strict";

  function escHtml(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
    ));
  }

  // ---- 目录路径输入框：自动补全子目录 + 记住最近用过的几个（存浏览器本地）----
  // 给 input 挂上这个之后返回 { rememberDir }，调用方在"这个路径确实被用了一次"
  // 的时机（导入成功、切换 change 等）自己调 rememberDir 记一笔——组件本身不猜
  // 什么时候算"用过"，避免用户还没编辑完就把半截路径记进最近列表。
  //
  // 页面上不止一个这种输入框（导入目录、输出根目录各一份），每份都各带一个悬浮
  // 建议框。如果切换输入框时上一个没有正常收起（比如没触发 blur 就转去点了别的
  // 地方），旧的悬浮框会停在原来算好的位置上不再更新，页面一滚动/一展开手风琴，
  // 看起来就是一个不明来源、一直悬在那的小框——全局只认一个"当前开着的建议框"，
  // 开新的之前先关掉别的，从根上不让这种孤儿框出现。
  let activeDirClose = null;

  function attachDirAutocomplete(input, recentKey) {
    const box = document.createElement("div");
    const boxId = `${input.id}-dirsuggest`;
    box.className = "dir-suggest";
    box.id = boxId;
    box.setAttribute("role", "listbox");
    box.style.display = "none";
    document.body.appendChild(box);
    // 屏幕阅读器看不到"输入的时候弹出了一份建议列表"这件事，除非用 combobox 这套
    // ARIA 关系显式声明出来；键盘操作本身（方向键/Enter/Esc）已经绑在 input 上了，
    // 这里补的只是语义，不改行为。
    input.setAttribute("role", "combobox");
    input.setAttribute("aria-autocomplete", "list");
    input.setAttribute("aria-expanded", "false");
    input.setAttribute("aria-controls", boxId);

    function loadRecent() {
      try {
        const list = JSON.parse(localStorage.getItem(recentKey));
        return Array.isArray(list) ? list : [];
      } catch (e) { return []; }
    }
    function rememberDir(path) {
      path = (path || "").trim();
      if (!path) return;
      let list = loadRecent().filter((p) => p !== path);
      list.unshift(path);
      list = list.slice(0, 8);
      try { localStorage.setItem(recentKey, JSON.stringify(list)); } catch (e) { /* ignore */ }
    }

    let items = [];
    let activeIndex = -1;
    let debounceTimer = null;

    function position() {
      const r = input.getBoundingClientRect();
      box.style.left = `${r.left}px`;
      box.style.top = `${r.bottom}px`;
      box.style.width = `${r.width}px`;
    }

    function render(list, label) {
      items = list;
      activeIndex = -1;
      box.innerHTML = "";
      if (!list.length) { box.style.display = "none"; return; }
      if (label) {
        const h = document.createElement("div");
        h.className = "dir-suggest-label";
        h.textContent = label;
        box.appendChild(h);
      }
      list.forEach((path, i) => {
        const row = document.createElement("div");
        row.className = "dir-suggest-item";
        row.id = `${boxId}-opt-${i}`;
        row.setAttribute("role", "option");
        row.setAttribute("aria-selected", "false");
        row.textContent = path;
        row.addEventListener("mousedown", (e) => { e.preventDefault(); pick(path); });
        box.appendChild(row);
      });
      position();
      box.style.display = "block";
      input.setAttribute("aria-expanded", "true");
      if (activeDirClose && activeDirClose !== close) activeDirClose();
      activeDirClose = close;
    }

    function highlight() {
      [...box.querySelectorAll(".dir-suggest-item")].forEach((el, i) => {
        const isActive = i === activeIndex;
        el.classList.toggle("active", isActive);
        el.setAttribute("aria-selected", String(isActive));
      });
      input.setAttribute("aria-activedescendant", activeIndex >= 0 ? `${boxId}-opt-${activeIndex}` : "");
    }

    function pick(path) {
      input.value = path;
      close();
      rememberDir(path);
      input.dispatchEvent(new Event("change"));
      input.focus();
    }

    function close() {
      box.style.display = "none"; items = []; activeIndex = -1;
      input.setAttribute("aria-expanded", "false");
      input.removeAttribute("aria-activedescendant");
      if (activeDirClose === close) activeDirClose = null;
    }

    async function showSuggestions() {
      const val = input.value.trim();
      if (!val) {
        const recent = loadRecent();
        render(recent, recent.length ? "最近使用：" : "");
        return;
      }
      let entries = [];
      try {
        const r = await fetch("api/browse_dir", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path: val }),
        });
        const d = await r.json();
        entries = Array.isArray(d.entries) ? d.entries : [];
      } catch (e) { /* 网络/接口问题不打断输入，静默跳过这次补全 */ }
      // 请求回来时用户可能已经离开输入框或改了内容：这时再弹出下拉框就是个"孤儿"
      if (document.activeElement !== input || input.value.trim() !== val) return;
      const recentMatches = loadRecent().filter(
        (p) => p !== val && p.toLowerCase().includes(val.toLowerCase()) && !entries.includes(p));
      render(recentMatches.concat(entries), "");
    }

    input.addEventListener("input", () => {
      clearTimeout(debounceTimer);
      debounceTimer = setTimeout(showSuggestions, 150);
    });
    // 只在真的开始打字之后才弹出来——原来连 focus（光标刚点进空输入框，
    // 什么都还没打）就弹"最近使用"清单，正好盖住输入框正下方那段说明文字，
    // 看着像糊在一起。多按一个字符的成本换来不遮挡说明，划算。
    input.addEventListener("blur", () => setTimeout(close, 120));
    input.addEventListener("keydown", (e) => {
      if (box.style.display === "none") return;
      if (e.key === "ArrowDown") { e.preventDefault(); activeIndex = Math.min(activeIndex + 1, items.length - 1); highlight(); }
      else if (e.key === "ArrowUp") { e.preventDefault(); activeIndex = Math.max(activeIndex - 1, -1); highlight(); }
      else if (e.key === "Enter") { if (activeIndex >= 0 && items[activeIndex]) { e.preventDefault(); pick(items[activeIndex]); } }
      else if (e.key === "Escape") { close(); }
    });
    window.addEventListener("scroll", () => { if (box.style.display !== "none") close(); }, true);

    // change 事件（用户手打后失焦）时用：打错的路径、上级目录都不存在的半截输入，
    // 不该被记进最近使用——不然列表里全是垃圾。只有目录本身已存在、或者上级目录
    // 存在（正在给一个还没建过的输出目录起名字）才记。
    async function rememberDirIfPlausible(path) {
      path = (path || "").trim();
      if (!path) return;
      try {
        const r = await fetch("api/dir_plausible", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path }),
        });
        const d = await r.json();
        if (d.plausible) rememberDir(path);
      } catch (e) { /* 网络问题就不记了，不是关键路径 */ }
    }

    return { rememberDir, rememberDirIfPlausible };
  }

  // 时间戳（毫秒）→ "HH:MM"，"上次检查 09:05"、"任务 14:30 开始"之类的提示用
  function fmtClock(ts) {
    if (!ts) return "";
    const d = new Date(ts);
    const pad = (n) => String(n).padStart(2, "0");
    return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }

  window.SparkCommon = { escHtml, attachDirAutocomplete, fmtClock };
})();
