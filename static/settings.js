// 设置页：读 /api/settings 渲染表单，保存时把整份表单 POST 回去。
// 服务端（core/settings.py）负责校验；这里只管显示每一项的来源和当前生效的值。
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);

  const SOURCE_TEXT = { settings: "来自：设置", env: "来自：环境变量", default: "默认" };
  const FOLDER_NOTE = "改了不会移动已有的订阅、笔记或文件夹。";

  // 存储与输出
  const STORAGE = [
    { key: "storage.vault_root", label: "笔记库位置（Obsidian vault）",
      hint: "Spark 的产物默认写进这个库里的 Spark/ 文件夹，笔记洞察从这里选笔记。" },
    { key: "storage.summit_output_dir", label: "Summit 总结的输出根目录",
      hint: "每场会议在下面建一个以会议名命名的文件夹。" + FOLDER_NOTE },
    { key: "storage.podcast_output_dir", label: "Podcast 跟进：节目文件夹的上级目录",
      hint: "新订阅的节目在下面建一个以节目名命名的文件夹。" + FOLDER_NOTE },
    { key: "storage.track_output_dir", label: "信息跟进：上级目录",
      hint: "新订阅放在它下面的「信息跟进/订阅名/」里，简报在「信息跟进/简报/」。" + FOLDER_NOTE },
    { key: "storage.notes_output_dir", label: "笔记洞察：报告目录",
      hint: "报告和演示写到这里。阅读页只浏览笔记库里的 Spark/ 和 output/，放到库外的报告不会出现在阅读页里。" + FOLDER_NOTE },
  ];

  const LENGTH_TEXT = { short: "简洁", medium: "标准", long: "详细" };
  const SPEECH_TEXT = { bilingual: "中英文对照", zh: "中文", original: "保留原文" };

  function aiFields(d) {
    return [
      { key: "ai.backend", label: "默认后端", type: "select",
        options: [["", "按模块默认（Summit 系用 Anthropic API，笔记洞察用本机 claude CLI）"]]
          .concat(d.backends.map((b) => [b.key, b.label])) },
      { key: "ai.overall_model", label: "总结用的模型（大会总结 / 节目总结 / 信息跟进简报）",
        hint: "留空则和逐条用的模型一致。格式要符合所选后端，比如 Anthropic 填 claude-opus-5。" },
      { key: "ai.summary_length", label: "单条小结篇幅（Summit / Podcast / 信息跟进）", type: "select",
        options: [["", "默认（标准）"]].concat(d.summary_lengths.map((v) => [v, LENGTH_TEXT[v] || v])) },
      { key: "ai.speech_lang_mode", label: "演讲稿语言", type: "select",
        options: [["", "默认（中英文对照）"]].concat(d.speech_lang_modes.map((v) => [v, SPEECH_TEXT[v] || v])) },
      { key: "ai.lang_prefs", label: "优先选的字幕语言",
        hint: "YouTube 字幕语言代码，例如 en、zh-Hans、ja。视频没有这种语言时仍按原来的规则挑。", placeholder: "en" },
      { key: "ai.max_transcript_chars", label: "单篇最多读取的字符数", type: "number",
        hint: "超长文字记录只读前面这么多字符再交给模型，0 表示不限制。留空则按模块默认（Summit 系 120000，笔记洞察 10 万字）。" },
      { key: "ai.api_bases.ollama", label: "Ollama 地址", placeholder: "http://localhost:11434" },
      { key: "ai.api_bases.openai_compatible", label: "第三方 OpenAI 兼容 API 的 Base URL",
        placeholder: "https://api.example.com/v1", hint: "Key 仍然每次在任务里填写。" },
    ];
  }

  function getPath(obj, dotted) {
    return dotted.split(".").reduce((o, k) => (o == null ? undefined : o[k]), obj);
  }

  function setPath(obj, dotted, value) {
    const parts = dotted.split(".");
    let o = obj;
    parts.slice(0, -1).forEach((k) => { o = o[k] = o[k] || {}; });
    o[parts[parts.length - 1]] = value;
  }

  function idOf(key) { return "f-" + key.replace(/\./g, "-"); }

  function node(tag, attrs, text) {
    const el = document.createElement(tag);
    Object.entries(attrs || {}).forEach(([k, v]) => {
      if (v === undefined || v === null || v === false) return;
      if (k === "className") el.className = v; else el.setAttribute(k, v === true ? "" : v);
    });
    if (text !== undefined) el.textContent = text;
    return el;
  }

  function renderField(spec, d) {
    const info = d.fields[spec.key] || { saved: getPath(d.settings, spec.key), source: "default" };
    const id = idOf(spec.key);
    const wrap = node("div", { className: "field" });
    const row = node("div", { className: "label-row" });
    row.append(node("label", { for: id }, spec.label));
    const badgeText = info.source === "env" ? `来自：环境变量 ${info.env_var}` : SOURCE_TEXT[info.source] || "默认";
    row.append(node("span", { className: `badge ${info.source}`, id: id + "-src" }, badgeText));
    wrap.append(row);

    let input;
    const describedBy = [id + "-src"];
    if (spec.type === "select") {
      input = node("select", { id, name: spec.key });
      spec.options.forEach(([v, t]) => input.append(node("option", { value: v }, t)));
      input.value = info.saved || "";
    } else {
      input = node("input", {
        id, name: spec.key, type: spec.type === "number" ? "number" : "text",
        min: spec.type === "number" ? "0" : null, step: spec.type === "number" ? "1" : null,
        inputmode: spec.type === "number" ? "numeric" : null,
        autocomplete: "off", spellcheck: "false",
        placeholder: spec.placeholder || (info.default ? `默认：${info.default}` : "留空用默认"),
      });
      input.value = info.saved === null || info.saved === undefined ? "" : String(info.saved);
    }
    wrap.append(input);

    if (spec.key.startsWith("storage.")) {
      const eff = node("div", { className: "hint effective", id: id + "-eff" });
      eff.append("当前生效：", node("code", {}, info.value || ""));
      wrap.append(eff);
      describedBy.push(id + "-eff");
    }
    if (info.source === "env") {
      const w = node("div", { className: "hint warn", id: id + "-env" },
        `环境变量 ${info.env_var} 正在覆盖这一项。在这里改了会保存，但要先去掉这个环境变量（重启 Spark）才会生效。`);
      wrap.append(w);
      describedBy.push(id + "-env");
    }
    if (spec.hint) {
      wrap.append(node("div", { className: "hint", id: id + "-hint" }, spec.hint));
      describedBy.push(id + "-hint");
    }
    const err = node("div", { className: "hint err hidden", id: id + "-err" });
    wrap.append(err);
    describedBy.push(id + "-err");
    input.setAttribute("aria-describedby", describedBy.join(" "));
    input.addEventListener("input", () => { $("status").textContent = ""; $("status").className = ""; });
    return wrap;
  }

  let fieldKeys = [];

  function render(d) {
    $("fileInfo").replaceChildren("设置文件：", node("code", {}, d.path),
      d.exists ? "" : "（还没有保存过，现在全部是默认值）");
    const notes = $("notes");
    notes.classList.toggle("hidden", !(d.notes && d.notes.length));
    notes.textContent = (d.notes || []).join("\n");

    fieldKeys = [];
    const storage = $("storageFields");
    storage.replaceChildren(...STORAGE.map((s) => { fieldKeys.push(s); return renderField(s, d); }));
    const ai = $("aiFields");
    ai.replaceChildren(...aiFields(d).map((s) => { fieldKeys.push(s); return renderField(s, d); }));
    const models = $("modelFields");
    models.replaceChildren(...d.backends.map((b) => {
      const s = { key: `ai.models.${b.key}`, label: b.label,
        placeholder: b.key === "cli" ? "例如 sonnet / opus（留空跟随 CLI 自己的设置）" : "留空用模块默认" };
      fieldKeys.push(s);
      return renderField(s, d);
    }));

    $("keys").replaceChildren(...d.backends.map((b) => {
      const k = d.keys[b.key] || {};
      const li = node("li");
      li.append(node("b", {}, b.label), node("span", { className: k.set ? "on" : "off" }, k.label || ""));
      return li;
    }));
  }

  function collect() {
    const out = {};
    fieldKeys.forEach((s) => {
      const el = $(idOf(s.key));
      let v = el.value.trim();
      if (s.type === "number") v = v === "" ? null : v;
      setPath(out, s.key, v);
    });
    return out;
  }

  function showErrors(errors) {
    let first = null;
    fieldKeys.forEach((s) => {
      const id = idOf(s.key);
      const msg = errors[s.key];
      const box = $(id + "-err");
      $(id).setAttribute("aria-invalid", msg ? "true" : "false");
      box.textContent = msg || "";
      box.classList.toggle("hidden", !msg);
      if (msg && !first) first = $(id);
    });
    if (first) {
      first.scrollIntoView({ block: "center" });
      first.focus({ preventScroll: true });
    }
  }

  async function load() {
    try {
      const r = await fetch("/api/settings");
      render(await r.json());
      // 从阅读页「更多字体和选项…」跳过来的：上面的表单刚渲染完把这一节挤下去了，再滚一次
      if (location.hash === "#reading") $("reading").scrollIntoView();
    } catch (e) {
      $("status").textContent = "读取设置失败：" + e.message;
      $("status").className = "err";
    }
  }

  $("form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const btn = $("save");
    btn.disabled = true;
    $("status").className = "";
    $("status").textContent = "正在保存……";
    try {
      const r = await fetch("/api/settings", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ settings: collect() }),
      });
      const d = await r.json();
      if (!r.ok) {
        showErrors(d.errors || {});
        $("status").textContent = d.error || "保存失败";
        $("status").className = "err";
        return;
      }
      render(d);
      showErrors({});
      $("status").textContent = `已保存（${new Date().toLocaleTimeString()}）。之后新打开的表单会用这些默认值，已打开的页面刷新后生效。`;
      $("status").className = "ok";
    } catch (e) {
      $("status").textContent = "保存失败：" + e.message;
      $("status").className = "err";
    } finally {
      btn.disabled = false;
    }
  });

  load();
  initReading();

  // ---------------------------------------------------------------- 阅读体验
  // 跟上面的服务端设置无关：直接读写浏览器本地的阅读偏好（window.SparkRead，和阅读页「Aa」同一份），
  // 改一项立刻存、立刻套到预览框上；开着的阅读页通过 storage 事件跟着变。
  function initReading() {
    const R = window.SparkRead;
    if (!R || !$("reading")) return;
    const preview = $("read-preview");
    const fontSel = $("read-font");
    const custom = $("read-custom");
    const warn = $("read-custom-warn");
    let prefs = R.load();
    // 自定义名字清空时退回到哪个字体
    let lastBuiltin = prefs.font === "custom" ? R.DEFAULTS.font : prefs.font;
    // 导入的主题（服务端 /read/api/themes 的列表）；previewing = {id, name} 是只在预览框里试看、还没用上的
    let themes = [];
    let themesLoaded = false;
    let previewing = null;

    // 配色：按钮本身挂 .read-scope + data-theme，颜色直接取阅读页那套
    $("read-theme").replaceChildren(...R.THEMES.map(([k, t]) =>
      node("button", { type: "button", className: "choice read-scope", "data-theme": k, "aria-pressed": "false",
        title: k === "auto" ? "白天羊皮纸，系统切到深色时变暗色" : null }, t)));

    // 字体：常用三个 + 按分组的更多字体 + 自定义。检测本机有没有装，没装的标出来。
    const groups = new Map();
    R.FONTS.forEach((f) => {
      if (!groups.has(f.group)) groups.set(f.group, node("optgroup", { label: f.group }));
      const missing = f.probe && f.probe.length && f.probe.every((n) => R.installed(n) === false);
      groups.get(f.group).append(node("option", { value: f.key }, f.label + (missing ? "（本机未检测到）" : "")));
    });
    const other = node("optgroup", { label: "其他" });
    other.append(node("option", { value: "custom" }, "已安装的其他字体（在下面填名字）"));
    fontSel.replaceChildren(...groups.values(), other);

    $("read-size").replaceChildren(node("option", { value: "" }, "默认（电脑 17px，手机 16px）"),
      ...R.SIZES.map((px, i) => node("option", { value: String(i) }, `${px}px`)));
    $("read-width").replaceChildren(...R.WIDTHS.map(([k, t, px]) =>
      node("option", { value: k }, `${t}（最宽 ${px}px）`)));

    function checkCustom() {
      const names = prefs.customFont.split(/[,，]/).map((s) => s.trim().replace(/["']/g, "")).filter(Boolean);
      const missing = names.filter((n) => R.installed(n) === false);
      const show = prefs.font === "custom" && names.length > 0 && missing.length === names.length;
      warn.classList.toggle("hidden", !show);
      warn.textContent = show
        ? `本机没检测到「${missing.join("、")}」，现在会用后备字体（Charter / 宋体）显示。` +
          "检查名字是不是跟「字体册」里的系列名一致，或者这个浏览器不让网页用它。"
        : "";
      custom.setAttribute("aria-invalid", show ? "true" : "false");
    }

    function render() {
      // 用着导入的主题时，内置配色一个都不算选中
      $("read-theme").querySelectorAll("[data-theme]").forEach((b) =>
        b.setAttribute("aria-pressed", String(b.dataset.theme === prefs.theme && !prefs.customTheme)));
      fontSel.value = prefs.font;
      if (document.activeElement !== custom) custom.value = prefs.customFont;
      $("read-size").value = Number.isInteger(prefs.size) ? String(prefs.size) : "";
      $("read-width").value = prefs.width;
      // 预览框：正在试看某个导入的主题就显示它，否则跟实际用的一样
      const shown = previewing ? { ...prefs, customTheme: previewing.id, customThemeName: previewing.name } : prefs;
      R.apply(shown, preview);
      checkCustom();
      renderThemes();
    }

    function set(change, msg) {
      prefs = Object.assign(R.load(), change);   // 先重读，别覆盖阅读页那边刚改的其他项
      if (prefs.font !== "custom") lastBuiltin = prefs.font;
      R.save(prefs);
      render();
      $("read-status").textContent = msg || "已存到这个浏览器，阅读页立刻生效";
    }

    $("read-theme").addEventListener("click", (e) => {
      const b = e.target.closest("[data-theme]");
      // 点内置配色 = 不用导入的主题了
      if (b) { previewing = null; set({ theme: b.dataset.theme, customTheme: "", customThemeName: "" }); }
    });
    fontSel.addEventListener("change", () => {
      if (fontSel.value === "custom" && !R.cleanFamilies(custom.value)) {
        // 还没填名字：先别切，等填了再用
        fontSel.value = prefs.font;
        custom.focus();
        $("read-status").textContent = "先在「已安装字体名称」里填字体名";
        return;
      }
      set({ font: fontSel.value });
    });
    custom.addEventListener("input", () => {
      const name = custom.value;
      if (R.cleanFamilies(name)) set({ customFont: name, font: "custom" });
      else set({ customFont: "", font: prefs.font === "custom" ? lastBuiltin : prefs.font });
    });
    // 这个框在设置表单里，回车别把上面的服务端设置提交了
    custom.addEventListener("keydown", (e) => { if (e.key === "Enter") e.preventDefault(); });
    custom.addEventListener("blur", () => { custom.value = prefs.customFont; });
    $("read-size").addEventListener("change", (e) =>
      set({ size: e.target.value === "" ? null : Number(e.target.value) }));
    $("read-width").addEventListener("change", (e) => set({ width: e.target.value }));
    $("read-reset").addEventListener("click", () => {
      lastBuiltin = R.DEFAULTS.font;
      previewing = null;
      set({ ...R.DEFAULTS }, "已恢复默认");
    });

    // ------------------------------------------------ 导入 Typora / Obsidian 主题
    // 主题文件存在服务端，用哪个是这个浏览器的阅读偏好（customTheme），跟「Aa」共用。
    const ctResult = $("ct-result");
    const MAX_BYTES = 1000000;
    const REASON_TEXT = {
      external: "外链资源（url()、@import、@font-face）", ui: "影响页面控件（顶栏、面板、整页）",
      editor: "编辑器界面（侧栏、标题栏、编辑区等）", selector: "不支持的选择器", layout: "定位、尺寸或动画",
      pseudo: "伪元素装饰（::before / ::after）", property: "不支持的属性", unsafe: "不安全的写法",
      atrule: "不支持的 @ 规则（打印、按宽度、动画等）", syntax: "语法错误",
    };
    const KIND_TEXT = { rule: "整条规则", decl: "属性", selector: "选择器" };

    function renderThemes() {
      const badge = $("ct-active-badge");
      badge.textContent = prefs.customTheme ? `正在用：${prefs.customThemeName || "导入的主题"}` : "";
      badge.classList.toggle("hidden", !prefs.customTheme);
      $("ct-off").classList.toggle("hidden", !prefs.customTheme);
      $("ct-list").replaceChildren(...themes.map((t) => {
        const li = node("li");
        const info = node("span", { className: "ct-info" });
        const src = { typora: "Typora", obsidian: "Obsidian" }[t.source] || "CSS";
        info.append(node("b", {}, t.name), node("span", { className: "ct-meta" },
          `${src} · ${t.imported_at} 导入 · 保留 ${t.kept} 条、去掉 ${t.dropped} 条` + (t.has_dark ? " · 有深色版本" : "")));
        const using = t.id === prefs.customTheme;
        const viewing = !!previewing && previewing.id === t.id;
        const btns = node("span", { className: "ct-btns" });
        btns.append(
          node("button", { type: "button", className: "plain", "data-act": "preview", "data-id": t.id,
            "aria-pressed": String(viewing), disabled: using }, viewing ? "停止预览" : "预览"),
          node("button", { type: "button", className: "plain", "data-act": "use", "data-id": t.id,
            "aria-pressed": String(using) }, using ? "正在使用" : "使用"),
          node("button", { type: "button", className: "plain", "data-act": "delete", "data-id": t.id,
            "aria-label": `删除主题「${t.name}」` }, "删除"));
        li.append(info, btns);
        return li;
      }));
    }

    function showResult(kind, lines, details) {
      ctResult.className = `ct-result ${kind}`;
      ctResult.replaceChildren(...lines.map((l) => node("p", {}, l)));
      if (details && details.length) {
        const d = node("details");
        d.append(node("summary", {}, `看去掉了哪些（${details.length} 项${details.length >= 200 ? "，只列前 200 项" : ""}）`));
        const ul = node("ul");
        details.forEach((x) => {
          const li = node("li");
          li.append(node("code", {}, x.what), ` — ${KIND_TEXT[x.kind] || ""}：${REASON_TEXT[x.reason] || x.reason}`);
          ul.append(li);
        });
        d.append(ul);
        ctResult.append(d);
      }
    }

    async function loadThemes() {
      try {
        const r = await fetch("/read/api/themes");
        const d = await r.json();
        themes = Array.isArray(d.themes) ? d.themes : [];
        themesLoaded = true;
      } catch (e) {
        showResult("err", ["读取导入的主题失败：" + e.message]);
      }
      // 记着的主题在服务端已经没有了（别处删了）：清掉，回到内置配色
      if (themesLoaded && prefs.customTheme && !themes.some((t) => t.id === prefs.customTheme)) {
        set({ customTheme: "", customThemeName: "" }, "之前用的主题已经不在了，已回到内置配色");
      } else {
        render();
      }
    }

    async function importCss(css, name, filename) {
      if (!css.trim()) { showResult("err", ["CSS 是空的"]); return; }
      const size = new Blob([css]).size;
      if (size > MAX_BYTES) { showResult("err", [`文件太大（${(size / 1e6).toFixed(1)} MB），最多 1 MB`]); return; }
      showResult("", ["正在导入……"]);
      try {
        const r = await fetch("/read/api/themes", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ css, name, filename }),
        });
        let d;
        try { d = await r.json(); } catch { d = { error: `导入失败（HTTP ${r.status}）` }; }
        if (!r.ok) {
          showResult("err", [d.error || "导入失败"], d.report && d.report.details);
          return;
        }
        themes = d.themes || themes;
        const t = d.theme;
        previewing = { id: t.id, name: t.name };
        showResult("ok", [
          `已导入「${t.name}」。${t.summary}`,
          t.has_dark ? "这个主题有深色版本，系统切到深色时跟着换。"
            : "这个主题没有单独的深色版本：系统是深色时也按它本来的样子显示。",
          "侧栏、标题栏、编辑区这类编辑器界面没法照搬，所以跟在原编辑器里看会有出入。",
          "下面的预览框已经换成这个主题；满意的话在列表里点「使用」。",
        ], t.details);
        render();
      } catch (e) {
        showResult("err", ["导入失败：" + e.message]);
      }
    }

    $("ct-pick").addEventListener("click", () => $("ct-file").click());
    $("ct-file").addEventListener("change", () => {
      const f = $("ct-file").files[0];
      $("ct-file").value = "";
      if (!f) return;
      if (f.size > MAX_BYTES) { showResult("err", [`文件太大（${(f.size / 1e6).toFixed(1)} MB），最多 1 MB`]); return; }
      f.text().then((css) => importCss(css, "", f.name),
        (e) => showResult("err", ["读不了这个文件：" + e.message]));
    });
    $("ct-paste-toggle").addEventListener("click", () => {
      const box = $("ct-paste");
      box.hidden = !box.hidden;
      $("ct-paste-toggle").setAttribute("aria-expanded", String(!box.hidden));
      if (!box.hidden) $("ct-text").focus();
    });
    // 这个框在设置表单里，回车别把上面的服务端设置提交了
    $("ct-name").addEventListener("keydown", (e) => { if (e.key === "Enter") e.preventDefault(); });
    $("ct-import").addEventListener("click", () =>
      importCss($("ct-text").value, $("ct-name").value.trim() || "粘贴的主题", ""));
    $("ct-off").addEventListener("click", () => {
      previewing = null;
      set({ customTheme: "", customThemeName: "" }, "已停用导入的主题，回到内置配色");
    });
    $("ct-list").addEventListener("click", async (e) => {
      const b = e.target.closest("button[data-act]");
      const t = b && themes.find((x) => x.id === b.dataset.id);
      if (!t) return;
      if (b.dataset.act === "preview") {
        previewing = previewing && previewing.id === t.id ? null : { id: t.id, name: t.name };
        render();
        $("read-status").textContent = previewing ? `预览框里是「${t.name}」，还没用上` : "";
      } else if (b.dataset.act === "use") {
        previewing = null;
        set({ customTheme: t.id, customThemeName: t.name }, `已用上「${t.name}」，阅读页立刻生效`);
      } else if (b.dataset.act === "delete") {
        if (!confirm(`删除主题「${t.name}」？`)) return;
        try {
          const r = await fetch(`/read/api/themes/${encodeURIComponent(t.id)}`, { method: "DELETE" });
          const d = await r.json().catch(() => ({}));
          if (!r.ok && r.status !== 404) { showResult("err", [d.error || "删除失败"]); return; }
          themes = d.themes || themes.filter((x) => x.id !== t.id);
          if (previewing && previewing.id === t.id) previewing = null;
          if (prefs.customTheme === t.id) set({ customTheme: "", customThemeName: "" }, `已删除「${t.name}」，回到内置配色`);
          else { render(); $("read-status").textContent = `已删除「${t.name}」`; }
        } catch (err) {
          showResult("err", ["删除失败：" + err.message]);
        }
      }
    });
    // 主题文件加载失败：read-prefs.js 已经退回内置配色，这边刷新一下
    document.addEventListener("spark-theme-missing", (e) => {
      if (previewing && previewing.id === e.detail) previewing = null;
      prefs = R.load();
      render();
      $("read-status").textContent = "主题文件加载失败，已回到内置配色";
    });

    // 阅读页（另一个标签页）里用「Aa」改了：这边跟着刷新
    R.watch((p) => { prefs = p; render(); });
    render();
    loadThemes();
  }
})();
