(() => {
  const $ = (id) => document.getElementById(id);

  let allNotes = [];          // 全库笔记元数据
  let byPath = new Map();     // path -> note
  let selected = new Set();   // 勾选的 path
  let expanded = new Set();   // 展开的文件夹
  let env = {};
  let mode = "topic";          // topic | manual
  let modelsFor = null;        // 当前模型下拉是为哪个后端填充的，避免切后端时串档
  let sessionReady = false;    // 现场恢复完成前不回写，否则初始化的空渲染会冲掉上次的勾选
  let searchResult = null;     // 最近一次主题检索的结果
  let autoRunAfterSearch = false;
  let jobId = null;
  let pollTimer = null;
  const PREFS_KEY = "notes2insight.prefs";
  const SESSION_KEY = "notes2insight.session";
  const CUSTOM = "__custom__";

  // 各后端可选的模型。CLI 支持 sonnet/opus/haiku 这类别名，留空表示跟随 CLI 自身设置。
  const MODELS = {
    cli: [
      { v: "", t: "默认（跟随 claude CLI 当前设置）" },
      { v: "sonnet", t: "sonnet — 质量与速度均衡，推荐" },
      { v: "opus", t: "opus — 更强，更慢" },
      { v: "haiku", t: "haiku — 更快更省，适合只做摘取", cheap: true },
    ],
    api: [
      { v: "claude-sonnet-5", t: "claude-sonnet-5 — 质量与速度均衡，推荐" },
      { v: "claude-opus-5", t: "claude-opus-5 — 更强，更慢更贵" },
      { v: "claude-haiku-4-5-20251001", t: "claude-haiku-4-5-20251001 — 更快更省", cheap: true },
    ],
    // OpenRouter 用「厂商/模型」形式的 id，这里列常用几个，其余走「自定义」手填
    openrouter: [
      { v: "anthropic/claude-sonnet-5", t: "anthropic/claude-sonnet-5 — 质量与速度均衡，推荐" },
      { v: "anthropic/claude-opus-5", t: "anthropic/claude-opus-5 — 更强，更慢更贵" },
      { v: "anthropic/claude-haiku-4.5", t: "anthropic/claude-haiku-4.5 — 更快更省", cheap: true },
      { v: "deepseek/deepseek-chat-v3.1", t: "deepseek/deepseek-chat-v3.1 — 最省，适合只做摘取" },
      { v: "google/gemini-2.5-flash", t: "google/gemini-2.5-flash — 便宜且长上下文" },
      { v: "moonshotai/kimi-k3", t: "moonshotai/kimi-k3 — Kimi 旗舰，100 万上下文" },
      { v: "moonshotai/kimi-k2.5", t: "moonshotai/kimi-k2.5 — Kimi 便宜档，约 haiku 价位" },
      { v: "z-ai/glm-5.3", t: "z-ai/glm-5.3 — GLM 旗舰，130 万上下文" },
      { v: "z-ai/glm-5.3-flash", t: "z-ai/glm-5.3-flash — GLM 最省，长文摘取很划算" },
    ],
    openai_compatible: [],
    ollama: [],
  };

  const MODEL_HINT = {
    cli: "走本机已登录的 Claude Code CLI，不另外计费到 API Key。选「默认」就用 CLI 当前的模型设置。",
    api: "直接调 Anthropic API，按 token 计费。整份报告要几十次调用，先用小样本试跑再上深度档。",
    openrouter: "经 OpenRouter 转发，一个 Key 用所有厂商的模型；Key 留空会自动读 ~/.summit2md/keys/openrouter.key。模型 id 见 openrouter.ai/models，列表里没有就选「自定义」手填。",
    openai_compatible: "任何 OpenAI Chat Completions 兼容服务，模型名按对方文档填，例如 deepseek-chat。",
    ollama: "本机 Ollama，免费但慢。下拉里是已 pull 到本地的模型；没有就选「自定义」手填，工具会直接调。",
  };
  const MAX_RENDER = 400;     // 搜索结果一次最多渲染这么多行，避免卡顿

  // ---------- 工具 ----------
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  function fmtEta(sec) {
    if (!sec || sec < 1) return "—";
    const m = Math.round(sec / 60);
    if (m < 1) return "不到 1 分钟";
    if (m < 60) return `约 ${m} 分钟`;
    const h = Math.floor(m / 60), r = m % 60;
    return r ? `约 ${h} 小时 ${r} 分钟` : `约 ${h} 小时`;
  }

  function savePrefs() {
    try {
      localStorage.setItem(PREFS_KEY, JSON.stringify({
        root: $("root").value, outdir: $("outdir").value, depth: $("depth").value,
        backend: $("backend").value, apibase: $("apibase").value,
        models: modelSnapshot("modelSelect", "model", "models"),
        digestModels: modelSnapshot("modelDigestSelect", "modelDigest", "digestModels"),
        staged: $("staged").checked, maxChars: $("maxChars").value,
        conc: $("conc").value, focus: $("focus").value, useCache: $("useCache").checked,
        topic: $("topic").value, topicDate: $("topicDate").value, cands: $("cands").value,
        pick: $("pick").value, mode,
      }));
    } catch (e) { /* 隐私模式下 localStorage 不可用，忽略 */ }
  }

  // 只在模型下拉确实属于当前后端时才记录，否则保持原样：
  // 切换后端会先后触发两个 change 监听器，中间有一瞬间两者是错配的
  function modelSnapshot(selId, inputId, key) {
    const stored = loadPrefs()[key] || {};
    const backend = $("backend").value;
    if (modelsFor !== backend) return stored;
    const sel = $(selId);
    const value = sel.value === CUSTOM ? $(inputId).value.trim() : sel.value;
    return Object.assign({}, stored, { [backend]: value });
  }

  // 会话现场：刷新页面后要能接着看上一个任务，而不是回到空白界面
  function loadSession() {
    try { return JSON.parse(localStorage.getItem(SESSION_KEY)) || {}; } catch (e) { return {}; }
  }

  function saveSession(patch) {
    try {
      localStorage.setItem(SESSION_KEY, JSON.stringify(Object.assign(loadSession(), patch)));
    } catch (e) { /* 存不下（隐私模式或超额）就算了，不影响任务本身 */ }
  }

  function saveSelection() {
    if (!sessionReady) return;
    saveSession({ selected: [...selected], expanded: [...expanded] });
  }

  function loadPrefs() {
    try { return JSON.parse(localStorage.getItem(PREFS_KEY)) || {}; } catch (e) { return {}; }
  }

  // ---------- 笔记列表 ----------
  function currentFilter() {
    const q = $("q").value.trim().toLowerCase();
    const days = parseInt($("dateFilter").value || "0", 10);
    let cutoff = "";
    if (days) {
      const d = new Date(Date.now() - days * 86400000);
      cutoff = d.toISOString().slice(0, 10);
    }
    return { q, cutoff };
  }

  function filteredNotes() {
    const { q, cutoff } = currentFilter();
    let list = allNotes;
    if (q) {
      const terms = q.split(/\s+/).filter(Boolean);
      list = list.filter((n) => {
        const hay = (n.title + " " + n.path).toLowerCase();
        return terms.every((t) => hay.includes(t));
      });
    }
    if (cutoff) list = list.filter((n) => n.date && n.date >= cutoff);
    const sort = $("sort").value;
    list = list.slice();
    if (sort === "date") list.sort((a, b) => (b.date || "").localeCompare(a.date || ""));
    else if (sort === "size") list.sort((a, b) => b.bytes - a.bytes);
    else list.sort((a, b) => a.path.localeCompare(b.path, "zh"));
    return list;
  }

  function noteRow(n) {
    const checked = selected.has(n.path) ? " checked" : "";
    const kw = (n.chars / 1000).toFixed(1);
    return `<div class="nrow">
      <input type="checkbox" data-path="${esc(n.path)}"${checked} />
      <span class="ndate">${esc(n.date || "—")}</span>
      <span class="ntitle" data-preview="${esc(n.path)}" title="${esc(n.path)}">${esc(n.title)}</span>
      <span class="npath">${esc(n.folder)}</span>
      <span class="nsize">${kw}k</span>
    </div>`;
  }

  function render() {
    const list = filteredNotes();
    const tree = $("tree");
    const keepScroll = tree.scrollTop;
    const { q, cutoff } = currentFilter();
    const flat = !!(q || cutoff);

    if (!list.length) {
      tree.innerHTML = `<div class="nrow" style="padding-left:12px;color:var(--muted)">没有匹配的笔记</div>`;
    } else if (flat) {
      const shown = list.slice(0, MAX_RENDER);
      tree.innerHTML = shown.map(noteRow).join("") +
        (list.length > shown.length
          ? `<div class="nrow" style="color:var(--muted)">还有 ${list.length - shown.length} 篇未显示，请缩小筛选范围；「全选当前结果」会选中全部 ${list.length} 篇</div>`
          : "");
    } else {
      const groups = new Map();
      list.forEach((n) => {
        if (!groups.has(n.folder)) groups.set(n.folder, []);
        groups.get(n.folder).push(n);
      });
      const parts = [];
      [...groups.keys()].sort((a, b) => a.localeCompare(b, "zh")).forEach((folder) => {
        const items = groups.get(folder);
        const selCount = items.filter((n) => selected.has(n.path)).length;
        const state = selCount === 0 ? "" : (selCount === items.length ? " checked" : " data-indet=1");
        const open = expanded.has(folder);
        parts.push(`<div class="frow" data-folder="${esc(folder)}">
          <span class="caret">${open ? "▾" : "▸"}</span>
          <input type="checkbox" data-folder-cb="${esc(folder)}"${state} />
          <span class="fname">${esc(folder === "." ? "（根目录）" : folder)}</span>
          <span class="fmeta">${selCount ? selCount + "/" : ""}${items.length} 篇</span>
        </div>`);
        if (open) parts.push(items.map(noteRow).join(""));
      });
      tree.innerHTML = parts.join("");
    }

    tree.querySelectorAll("input[data-indet]").forEach((el) => { el.indeterminate = true; });
    tree.scrollTop = keepScroll;
    $("scanHint").textContent = `笔记库共 ${allNotes.length} 篇，当前筛选出 ${list.length} 篇`;
    renderSelection();
  }

  function updateFolderRow(folder) {
    const row = $("tree").querySelector(`.frow[data-folder="${CSS.escape(folder)}"]`);
    if (!row) return;
    const items = allNotes.filter((n) => n.folder === folder);
    const selCount = items.filter((n) => selected.has(n.path)).length;
    const box = row.querySelector("input[type=checkbox]");
    box.checked = selCount === items.length && items.length > 0;
    box.indeterminate = selCount > 0 && selCount < items.length;
    row.querySelector(".fmeta").textContent = `${selCount ? selCount + "/" : ""}${items.length} 篇`;
  }

  function renderSelection() {
    saveSelection();
    const notes = [...selected].map((p) => byPath.get(p)).filter(Boolean);
    const chars = notes.reduce((s, n) => s + n.chars, 0);
    $("selCount").textContent = notes.length;
    $("selChars").textContent = (chars / 10000).toFixed(1);

    // 粗略估算：每篇笔记按 28k 字一块，多块时多一次合卡；再加骨架 1 次与各章节
    const cap = parseInt($("maxChars").value, 10) || 0;
    let digestCalls = 0;
    notes.forEach((n) => {
      const chars = cap ? Math.min(n.chars, cap) : n.chars;
      const chunks = Math.max(1, Math.ceil(chars / 28000));
      digestCalls += chunks + (chunks > 1 ? 1 : 0);
    });
    const chapters = { brief: 4, standard: 6, deep: 9 }[$("depth").value] || 6;
    const composeCalls = chapters + 3;   // 骨架 + 摘要判断 + 各章 + 收尾
    const conc = parseInt($("conc").value || "3", 10);
    const perCall = $("backend").value === "cli" ? 50 : 35;
    const eta = (digestCalls / conc) * perCall + composeCalls * perCall * 1.6;
    $("selEta").textContent = notes.length ? fmtEta(eta) : "—";
    $("selCalls").textContent = notes.length
      ? `${digestCalls} 次摘取 + ${composeCalls} 次成文` + ($("staged").checked ? "（分阶段）" : "")
      : "—";

    const warn = $("selWarn");
    const max = env.max_notes || 400;
    if (notes.length > max) warn.innerHTML = `<span class="err">超过单次上限 ${max} 篇</span>`;
    else if (chars > 3000000) warn.textContent = "勾选内容较多，建议拆成几份报告";
    else warn.textContent = "";

    const chips = $("chips");
    chips.innerHTML = notes.slice(0, 60).map((n) =>
      `<span class="chip"><span title="${esc(n.path)}">${esc(n.title)}</span><button data-unsel="${esc(n.path)}">×</button></span>`
    ).join("") + (notes.length > 60 ? `<span class="chip"><span>…另外 ${notes.length - 60} 篇</span></span>` : "");
  }

  // ---------- 事件 ----------
  $("tree").addEventListener("click", (e) => {
    const cb = e.target.closest("input[type=checkbox]");
    if (cb) {
      if (cb.dataset.path) {
        cb.checked ? selected.add(cb.dataset.path) : selected.delete(cb.dataset.path);
        // 只刷新受影响的文件夹行，不整树重绘：既不丢滚动位置，也不会打断连续勾选
        const note = byPath.get(cb.dataset.path);
        if (note) updateFolderRow(note.folder);
        renderSelection();
      } else if (cb.dataset.folderCb) {
        const folder = cb.dataset.folderCb;
        const items = allNotes.filter((n) => n.folder === folder);
        const allSel = items.every((n) => selected.has(n.path));
        items.forEach((n) => (allSel ? selected.delete(n.path) : selected.add(n.path)));
        render();
      }
      e.stopPropagation();
      return;
    }
    const prev = e.target.closest("[data-preview]");
    if (prev) { previewNote(prev.dataset.preview); return; }
    const frow = e.target.closest(".frow");
    if (frow) {
      const f = frow.dataset.folder;
      expanded.has(f) ? expanded.delete(f) : expanded.add(f);
      saveSelection();
      render();
    }
  });

  $("chips").addEventListener("click", (e) => {
    const b = e.target.closest("[data-unsel]");
    if (!b) return;
    selected.delete(b.dataset.unsel);
    render();
  });

  $("selAll").addEventListener("click", () => {
    filteredNotes().forEach((n) => selected.add(n.path));
    render();
  });
  $("clearSel").addEventListener("click", () => { selected.clear(); render(); });
  ["q", "dateFilter", "sort"].forEach((id) => $(id).addEventListener("input", render));
  ["depth", "conc", "backend"].forEach((id) => $(id).addEventListener("change", () => { renderSelection(); savePrefs(); }));
  ["root", "outdir", "model", "apibase", "focus", "topic"].forEach((id) => $(id).addEventListener("change", savePrefs));
  ["topicDate", "cands", "pick"].forEach((id) => $(id).addEventListener("change", savePrefs));
  $("useCache").addEventListener("change", savePrefs);

  function modelList(backend) {
    let list = (MODELS[backend] || []).slice();
    if (backend === "ollama" && env.ollama_models && env.ollama_models.length) {
      list = env.ollama_models.map((m) => ({ v: m, t: m }));
    }
    list.push({ v: CUSTOM, t: "自定义…" });
    return list;
  }

  function fillSelect(selId, inputId, backend, want) {
    const sel = $(selId);
    const list = modelList(backend);
    sel.innerHTML = list.map((m) => `<option value="${esc(m.v)}">${esc(m.t)}</option>`).join("");
    // 记住的值若不在列表里（例如第三方模型名），就落到「自定义」并回填输入框
    if (list.some((m) => m.v === want)) {
      sel.value = want;
      $(inputId).value = want === CUSTOM ? "" : want;
    } else if (want) {
      sel.value = CUSTOM;
      $(inputId).value = want;
    } else {
      sel.value = list[0].v;
      $(inputId).value = list[0].v === CUSTOM ? "" : list[0].v;
    }
    syncCustom(selId, inputId);
  }

  function populateModels(backend) {
    const prefs = loadPrefs();
    fillSelect("modelSelect", "model", backend, (prefs.models || {})[backend] || "");
    const cheap = (MODELS[backend] || []).find((m) => m.cheap);
    fillSelect("modelDigestSelect", "modelDigest", backend,
               (prefs.digestModels || {})[backend] || (cheap ? cheap.v : ""));
    modelsFor = backend;
    $("modelHint").textContent = MODEL_HINT[backend] || "";
    syncStaged();
  }

  function syncCustom(selId, inputId) {
    const isCustom = $(selId).value === CUSTOM;
    $(inputId).classList.toggle("hidden", !isCustom);
    if (isCustom) $(inputId).placeholder = $("backend").value === "ollama" ? "例如 qwen2.5 / llama3.1" : "填写模型名";
  }

  function syncStaged() {
    const on = $("staged").checked;
    $("digestRow").classList.toggle("hidden", !on);
    $("modelLabel").textContent = on ? "成文模型 — 归纳骨架与撰写正文，最吃质量" : "模型";
  }

  function currentModel() {
    return $("modelSelect").value === CUSTOM ? $("model").value.trim() : $("modelSelect").value;
  }

  function digestModel() {
    if (!$("staged").checked) return "";
    return $("modelDigestSelect").value === CUSTOM ? $("modelDigest").value.trim() : $("modelDigestSelect").value;
  }

  // 检索里的相关度判定也是"量大活简单"，跟摘取用同一个模型
  function searchModel() {
    return $("staged").checked ? digestModel() : currentModel();
  }

  $("modelSelect").addEventListener("change", () => {
    syncCustom("modelSelect", "model");
    if ($("modelSelect").value !== CUSTOM) $("model").value = $("modelSelect").value;
    savePrefs();
  });
  $("modelDigestSelect").addEventListener("change", () => {
    syncCustom("modelDigestSelect", "modelDigest");
    if ($("modelDigestSelect").value !== CUSTOM) $("modelDigest").value = $("modelDigestSelect").value;
    savePrefs();
  });
  $("staged").addEventListener("change", () => { syncStaged(); renderSelection(); savePrefs(); });
  $("maxChars").addEventListener("change", () => { renderSelection(); savePrefs(); });

  async function previewNote(path) {
    const url = `api/preview?root=${encodeURIComponent($("root").value)}&path=${encodeURIComponent(path)}`;
    const r = await fetch(url);
    const d = await r.json();
    $("resultPanel").classList.remove("hidden");
    $("resultMeta").innerHTML = `预览：<code>${esc(path)}</code>（仅前 3000 字）`;
    $("resultText").textContent = d.text || d.error || "";
    $("resultPanel").scrollIntoView({ behavior: "smooth", block: "start" });
  }

  $("backend").addEventListener("change", () => {
    const b = $("backend").value;
    $("keyRow").classList.toggle("hidden", b === "cli" || b === "ollama");
    $("baseField").classList.toggle("hidden", !(b === "openai_compatible" || b === "ollama"));
    syncKeyHint(b);
    populateModels(b);
  });

  // Key 输入框：有本地 key 文件就明说可以留空，免得每次都粘贴
  function syncKeyHint(backend) {
    const dir = env.keys_dir || "~/.summit2md/keys";
    const box = $("apikey"), hint = $("keyHint");
    if (backend === "api") {
      box.placeholder = env.key_file_anthropic || env.env_api_key ? "可留空（已有可用的 Key）" : "sk-ant-…";
      hint.textContent = env.key_file_anthropic
        ? `已检测到 ${dir}/anthropic.key，可以留空。`
        : `留空则依次尝试：环境变量 ANTHROPIC_API_KEY → ${dir}/anthropic.key（把 key 存成这个文件就不用每次填）`;
    } else if (backend === "openrouter") {
      box.placeholder = env.key_file_openrouter ? "可留空（已有可用的 Key）" : "sk-or-…";
      hint.textContent = env.key_file_openrouter
        ? `已检测到 ${dir}/openrouter.key，可以留空。`
        : `留空则依次尝试：环境变量 OPENROUTER_API_KEY → ${dir}/openrouter.key（把 key 存成这个文件就不用每次填）`;
    } else {
      box.placeholder = "填写第三方服务的 API Key";
      hint.textContent = "第三方服务的 Key 不会存盘，只随本次请求发给服务端。";
    }
  }

  // ---------- 主题模式 ----------
  function setMode(next) {
    mode = next;
    $("tabTopic").classList.toggle("on", next === "topic");
    $("tabManual").classList.toggle("on", next === "manual");
    $("topicBox").classList.toggle("hidden", next !== "topic");
    $("manualBox").classList.toggle("hidden", next !== "manual");
    if (next === "manual") render();
  }
  $("tabTopic").addEventListener("click", () => setMode("topic"));
  $("tabManual").addEventListener("click", () => setMode("manual"));

  $("autoRun").addEventListener("click", () => {
    autoRunAfterSearch = !autoRunAfterSearch;
    $("autoRun").classList.toggle("on", autoRunAfterSearch);
  });

  function cutoffFrom(days) {
    if (!days) return "";
    return new Date(Date.now() - parseInt(days, 10) * 86400000).toISOString().slice(0, 10);
  }

  function setSearchHint(msg, isErr) {
    const el = $("searchHint");
    el.textContent = msg;
    el.className = "hint" + (isErr ? " err" : "");
  }

  $("searchBtn").addEventListener("click", async () => {
    const topic = $("topic").value.trim();
    if (!topic) { setSearchHint("请先填写主题", true); return; }
    savePrefs();
    setSearchHint("");
    $("searchBtn").disabled = true;
    $("searchProg").classList.remove("hidden");
    $("sBar").style.width = "3%";
    $("sText").textContent = "提交检索…";

    const payload = {
      root: $("root").value, topic,
      backend: $("backend").value, model: searchModel(),
      api_key: $("apikey").value, api_base: $("apibase").value,
      date_from: cutoffFrom($("topicDate").value),
      candidates: parseInt($("cands").value, 10),
      exclude_output: $("exOut").checked,
      output_dir: $("outdir").value,
      timeout: parseInt($("timeout").value || "300", 10),
    };
    try {
      const r = await fetch("api/search", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || "检索提交失败");
      saveSession({ searchJobId: d.job_id });
      pollSearch(d.job_id);
    } catch (e) {
      $("searchBtn").disabled = false;
      setSearchHint(e.message, true);
    }
  });

  const SEARCH_BASE = { queued: 0, expand: 5, search: 20, screen: 45, done: 100 };
  const SEARCH_SPAN = { expand: 15, search: 25, screen: 55 };

  function pollSearch(sjid) {
    setTimeout(async () => {
      try {
        const r = await fetch(`api/progress/${sjid}`);
        const d = await r.json();
        if (!r.ok) throw new Error(d.error || "查询失败");
        const base = SEARCH_BASE[d.stage] ?? 0;
        const span = SEARCH_SPAN[d.stage] ?? 0;
        const frac = d.total ? d.current / d.total : 0;
        $("sBar").style.width = Math.min(100, base + span * frac) + "%";
        $("sText").textContent = d.message || "";
        if (d.done) {
          $("searchBtn").disabled = false;
          saveSession({ searchJobId: null });
          if (!d.ok) { setSearchHint(d.error || "检索失败", true); return; }
          const rr = await fetch(`api/result/${sjid}`);
          searchResult = await rr.json();
          renderCandidates();
          saveSession({ searchResult });
          if (autoRunAfterSearch && selected.size) $("run").click();
          return;
        }
        pollSearch(sjid);
      } catch (e) {
        $("searchBtn").disabled = false;
        setSearchHint(e.message, true);
      }
    }, 1200);
  }

  function renderCandidates(autoSelect = true) {
    const d = searchResult;
    if (!d || !d.candidates) return;
    const limit = parseInt($("pick").value, 10);

    if (autoSelect) {
      // 默认勾选相关度 ≥3 的，按相关度顺序取到上限；一篇都没够格时退回前 5 篇
      selected.clear();
      let picked = d.candidates.filter((c) => c.relevance >= 3).slice(0, limit);
      if (!picked.length) picked = d.candidates.slice(0, Math.min(5, limit));
      picked.forEach((c) => selected.add(c.path));
    }

    const box = $("cands_list");
    box.classList.remove("hidden");
    box.innerHTML = d.candidates.map((c) => {
      const rel = c.relevance < 0 ? "?" : c.relevance;
      const kw = (c.chars / 1000).toFixed(1);
      return `<div class="crow">
        <input type="checkbox" data-path="${esc(c.path)}"${selected.has(c.path) ? " checked" : ""} />
        <span class="rel r${rel}">${rel}</span>
        <div class="body">
          <div class="ct" data-preview="${esc(c.path)}" title="${esc(c.path)}">${esc(c.title)}</div>
          <div class="cm">${esc(c.date || "—")}｜${esc(c.folder || c.path.split("/").slice(0, -1).join("/"))}｜${kw}k 字｜${esc(c.reason || "（未判定）")}</div>
        </div>
      </div>`;
    }).join("");

    const dup = d.duplicates ? `，已去重 ${d.duplicates} 篇` : "";
    setSearchHint(`扫描 ${d.scanned} 篇，命中 ${d.matched} 篇${dup}，取前 ${d.candidates.length} 篇判定相关度，` +
                  `${autoSelect ? "已自动勾选" : "当前勾选"} ${selected.size} 篇。可自行增减后再生成。` +
                  `　检索词：${d.terms.slice(0, 8).map((t) => t.term).join("、")}…`);
    if (sessionReady) saveSession({ searchResult: d });
    renderSelection();
  }

  $("cands_list").addEventListener("click", (e) => {
    const cb = e.target.closest("input[type=checkbox]");
    if (cb) {
      cb.checked ? selected.add(cb.dataset.path) : selected.delete(cb.dataset.path);
      renderSelection();
      return;
    }
    const prev = e.target.closest("[data-preview]");
    if (prev) previewNote(prev.dataset.preview);
  });

  function retrievalPayload() {
    if (mode !== "topic" || !searchResult) return null;
    const cands = searchResult.candidates || [];
    return {
      terms: searchResult.terms,
      scanned: searchResult.scanned,
      matched: searchResult.matched,
      duplicates: searchResult.duplicates,
      candidates_count: cands.length,
      picked: cands.filter((c) => selected.has(c.path)).length,
      dropped: cands.filter((c) => !selected.has(c.path) && c.relevance >= 0)
        .slice(0, 15).map((c) => ({ title: c.title, relevance: c.relevance, reason: c.reason })),
    };
  }

  // ---------- 运行 ----------
  $("run").addEventListener("click", async () => {
    if (!selected.size) { setRunHint("请先勾选至少一篇笔记", true); return; }
    // 大批量任务会跑很久，先把规模和预估摆到眼前再确认
    if (selected.size >= 20 &&
        !confirm(`将对 ${selected.size} 篇笔记生成「${$("depth").selectedOptions[0].text}」报告，预计耗时${$("selEta").textContent}。\n开始吗？`)) {
      return;
    }
    savePrefs();
    setRunHint("");
    setBanner("");
    $("run").disabled = true;
    $("progWrap").classList.remove("hidden");
    $("log").textContent = "";
    setProgress(0, "提交任务…");

    const payload = {
      root: $("root").value, notes: [...selected], focus: $("focus").value,
      topic: mode === "topic" ? $("topic").value.trim() : "",
      retrieval: retrievalPayload(),
      depth: $("depth").value, backend: $("backend").value, model: currentModel(),
      model_digest: digestModel(), model_compose: $("staged").checked ? currentModel() : "",
      max_note_chars: parseInt($("maxChars").value, 10) || 0,
      api_key: $("apikey").value, api_base: $("apibase").value,
      concurrency: parseInt($("conc").value, 10), timeout: parseInt($("timeout").value || "900", 10),
      output_dir: $("outdir").value, use_cache: $("useCache").checked,
    };
    try {
      const r = await fetch("api/run", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || "提交失败");
      jobId = d.job_id;
      saveSession({ jobId, jobStartedAt: Date.now() });
      poll();
    } catch (e) {
      $("run").disabled = false;
      setRunHint(e.message, true);
    }
  });

  function setRunHint(msg, isErr) {
    const el = $("runHint");
    el.textContent = msg;
    el.className = "hint" + (isErr ? " err" : "");
  }

  function setProgress(pct, text) {
    $("progBar").style.width = Math.max(0, Math.min(100, pct)) + "%";
    $("progText").textContent = text;
  }

  const STAGE_LABEL = { queued: "排队", digest: "摘取笔记", framework: "归纳骨架", compose: "撰写正文", done: "完成" };
  const STAGE_BASE = { queued: 0, digest: 2, framework: 55, compose: 68, done: 100 };
  const STAGE_SPAN = { digest: 53, framework: 13, compose: 32 };

  function poll() {
    clearTimeout(pollTimer);
    pollTimer = setTimeout(async () => {
      try {
        const r = await fetch(`api/progress/${jobId}`);
        const d = await r.json();
        if (!r.ok) throw new Error(d.error || "查询失败");

        const base = STAGE_BASE[d.stage] ?? 0;
        const span = STAGE_SPAN[d.stage] ?? 0;
        const frac = d.total ? d.current / d.total : 0;
        setProgress(base + span * frac, `${STAGE_LABEL[d.stage] || d.stage}｜${d.message || ""}`);
        $("log").textContent = (d.log || []).join("\n");
        $("log").scrollTop = $("log").scrollHeight;

        if (d.done) {
          $("run").disabled = false;
          if (d.ok) { setProgress(100, "完成"); showResult(); }
          else { setRunHint(d.error || "任务失败", true); }
          return;
        }
        poll();
      } catch (e) {
        $("run").disabled = false;
        setRunHint(e.message, true);
      }
    }, 1500);
  }

  async function showResult() {
    const r = await fetch(`api/result/${jobId}`);
    const d = await r.json();
    if (!r.ok) { setRunHint(d.error || "取结果失败", true); return; }
    $("resultPanel").classList.remove("hidden");
    const mins = Math.round(d.elapsed / 60);
    const failed = (d.failed || []).length;
    $("resultMeta").innerHTML =
      `<b>${esc(d.title || d.filename)}</b><br />` +
      `${d.ok_count} 篇笔记 · ${(d.chars / 1000).toFixed(1)}k 字 · ${d.clusters.length} 章 · 耗时 ${mins} 分钟` +
      (failed ? ` · <span class="err">${failed} 篇未成功</span>` : "") +
      `<br />已保存到 <code>${esc(d.path)}</code>`;
    $("resultText").textContent = d.content;
    const blob = new Blob([d.content], { type: "text/markdown" });
    const link = $("dlLink");
    link.href = URL.createObjectURL(blob);
    link.download = d.filename;
    $("copyBtn").onclick = () => navigator.clipboard.writeText(d.content);
    $("copyPath").onclick = () => navigator.clipboard.writeText(d.path);
    $("resultPanel").scrollIntoView({ behavior: "smooth", block: "start" });
    loadReports(d.filename);          // 演示面板里预选刚生成的这份
  }

  // ---------- 交互演示 ----------
  let deckJobId = null, deckTimer = null, reportRows = [];

  function setDeckHint(msg, cls) {
    const el = $("deckHint");
    el.textContent = msg;
    el.className = "hint" + (cls ? " " + cls : "");
  }

  function fmtSize(bytes) {
    return bytes > 1024 * 1024 ? (bytes / 1024 / 1024).toFixed(1) + "M" : Math.round(bytes / 1024) + "K";
  }

  async function loadReports(preferName) {
    const sel = $("deckReport");
    try {
      const r = await fetch(`api/reports?output_dir=${encodeURIComponent($("outdir").value)}`);
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || "读不到输出目录");
      const rows = (d.reports || []).filter((x) => !x.name.endsWith(".deck.html"));
      if (!rows.length) {
        sel.innerHTML = '<option value="">输出目录里还没有报告</option>';
        $("deckRun").disabled = true;
        return;
      }
      $("deckRun").disabled = false;
      reportRows = rows;
      sel.innerHTML = rows.map((x) => {
        const mark = (x.has_deck ? "◆" : "") + (x.has_pptx ? "▣" : "");
        return `<option value="${esc(x.path)}">${mark ? mark + " " : ""}${esc(x.name)}（${fmtSize(x.size)}）</option>`;
      }).join("");
      if (preferName) {
        const hit = rows.find((x) => x.name === preferName);
        if (hit) sel.value = hit.path;
      }
      syncDeckButtons();
    } catch (e) {
      sel.innerHTML = '<option value="">读不到输出目录</option>';
      setDeckHint(e.message, "err");
    }
  }

  function pollDeck() {
    clearTimeout(deckTimer);
    deckTimer = setTimeout(async () => {
      try {
        const r = await fetch(`api/progress/${deckJobId}`);
        const d = await r.json();
        if (!r.ok) throw new Error(d.error || "查询失败");
        if (!d.done) { setDeckHint(d.message || "生成中…"); pollDeck(); return; }
        $("deckRun").disabled = $("deckReuse").disabled = false;
        if (!d.ok) { setDeckHint(d.error || "生成失败", "err"); return; }
        const res = d.result || {};
        setDeckHint(`✅ ${res.slide_count} 页 · ${res.source_count} 条来源可跳转 · 模型 ${res.model}` +
                    (res.pptx_filename ? `　同时导出了 ${res.pptx_filename}` : "") +
                    `　已保存到 ${res.path}`, "ok");
        $("deckResultBar").classList.remove("hidden");
        $("deckOpen").href = `deck/${deckJobId}`;
        $("deckPptxLink").classList.toggle("hidden", !res.pptx_filename);
        $("deckPptxLink").href = `deck/${deckJobId}/pptx`;
        $("deckCopyPath").onclick = () => navigator.clipboard.writeText(res.pptx_path || res.path);
        loadReports();
      } catch (e) {
        $("deckRun").disabled = false;
        setDeckHint(e.message, "err");
      }
    }, 1500);
  }

  // 思考型模型的思考过程也计入 max_tokens，排版这种"一次性吐一大坨 JSON"的活儿最容易被烧空，
  // 所以在点之前就把要用的模型摆出来，别等两分钟后才报错
  const THINKING_MODEL = /kimi-k[3-9]|glm-[5-9]|deepseek-r|thinking|reason/i;

  function deckModel() { return digestModel() || currentModel(); }

  function syncDeckHint() {
    const m = deckModel();
    if (THINKING_MODEL.test(m)) {
      setDeckHint(`将用 ${m} 排版　⚠️ 这是思考型模型，思考过程会占满 max_tokens 导致返回空内容；` +
                  `建议换 claude-haiku-4.5 / gemini-2.5-flash / deepseek-chat-v3.1`, "err");
    } else {
      setDeckHint(m ? `将用 ${m} 排版，整份报告只调用一次。` : "将用后端默认模型排版，整份报告只调用一次。");
    }
  }

  function syncDeckButtons() {
    const row = reportRows.find((x) => x.path === $("deckReport").value);
    $("deckReuse").classList.toggle("hidden", !(row && row.has_deck));
  }

  $("deckReport").addEventListener("change", syncDeckButtons);
  $("deckRefresh").addEventListener("click", () => loadReports());
  ["backend", "modelSelect", "model", "modelDigestSelect", "modelDigest", "staged"].forEach((id) =>
    $(id).addEventListener("change", syncDeckHint));

  async function submitDeck({ reuse = false } = {}) {
    const path = $("deckReport").value;
    if (!path) { setDeckHint("先选一份报告", "err"); return; }
    $("deckRun").disabled = $("deckReuse").disabled = true;
    $("deckResultBar").classList.add("hidden");
    setDeckHint(reuse ? "复用已有演示脚本，不调用模型…" : "提交中…");
    try {
      const r = await fetch("api/deck", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          path, root: $("root").value, output_dir: $("outdir").value,
          reuse, pptx: reuse || $("deckPptx").checked,
          backend: $("backend").value, model: currentModel(),
          // 排版只调一次且活儿简单，默认跟着摘取模型走；思考型模型容易把预算烧光，这里偏向便宜的非思考模型
          model_deck: deckModel(),
          api_key: $("apikey").value, api_base: $("apibase").value,
          timeout: parseInt($("timeout").value || "600", 10),
        }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || "提交失败");
      deckJobId = d.job_id;
      pollDeck();
    } catch (e) {
      $("deckRun").disabled = $("deckReuse").disabled = false;
      setDeckHint(e.message, "err");
    }
  }

  $("deckRun").addEventListener("click", () => submitDeck());
  $("deckReuse").addEventListener("click", () => submitDeck({ reuse: true }));

  // ---------- 刷新后恢复现场 ----------
  function setBanner(msg) {
    const el = $("resumeBanner");
    el.textContent = msg;
    el.classList.toggle("hidden", !msg);
  }

  function fmtClock(ts) {
    if (!ts) return "";
    const d = new Date(ts);
    return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  }

  async function attachJob(id, { silent = false } = {}) {
    const r = await fetch(`api/progress/${id}`);
    if (!r.ok) {                       // 任务已过期或服务重启过
      saveSession({ jobId: null });
      return false;
    }
    const d = await r.json();
    jobId = id;
    saveSession({ jobId: id });
    $("progWrap").classList.remove("hidden");
    $("log").textContent = (d.log || []).join("\n");
    $("log").scrollTop = $("log").scrollHeight;

    if (!d.done) {
      $("run").disabled = true;
      if (!silent) setBanner(`已接上正在运行的任务（${fmtClock(d.created_at * 1000)} 开始），进度会继续更新。`);
      poll();
    } else if (d.ok) {
      setProgress(100, "完成");
      await showResult();
      if (!silent) setBanner(`已恢复上一个任务的结果（${fmtClock(d.created_at * 1000)}）。重新勾选并生成会开启新任务。`);
    } else {
      setProgress(0, "已结束");
      setRunHint(d.error || "任务失败", true);
      if (!silent) setBanner("上一个任务失败了，下面是当时的日志。");
    }
    return true;
  }

  async function restoreSession() {
    const sess = loadSession();

    if (Array.isArray(sess.expanded)) expanded = new Set(sess.expanded);
    if (Array.isArray(sess.selected)) {
      selected = new Set(sess.selected.filter((p) => byPath.has(p)));
    }
    render();
    if (sess.searchResult && sess.searchResult.candidates) {
      searchResult = sess.searchResult;
      renderCandidates(false);         // 不要重新自动勾选，保留刷新前的勾选
    }
    sessionReady = true;               // 从这里起，勾选变化才写回本地

    // 检索任务正在跑 → 接上
    if (sess.searchJobId) {
      const r = await fetch(`api/progress/${sess.searchJobId}`);
      if (r.ok) {
        const d = await r.json();
        if (!d.done) {
          $("searchBtn").disabled = true;
          $("searchProg").classList.remove("hidden");
          setBanner("已接上正在运行的检索任务。");
          pollSearch(sess.searchJobId);
        } else {
          saveSession({ searchJobId: null });
        }
      } else {
        saveSession({ searchJobId: null });
      }
    }

    // 报告任务：先用本地记的 id，找不到就问服务端要最近一个
    if (sess.jobId && await attachJob(sess.jobId)) return;
    try {
      const r = await fetch("api/jobs?limit=5");
      const d = await r.json();
      const last = (d.jobs || []).find((j) => j.kind === "report");
      if (last) await attachJob(last.job_id);
    } catch (e) { /* 服务刚重启、没有历史任务，正常 */ }
  }

  // ---------- 初始化 ----------
  async function loadNotes(refresh, skipRender) {
    $("scanHint").textContent = "正在扫描笔记库…";
    try {
      const r = await fetch(`api/notes?root=${encodeURIComponent($("root").value)}${refresh ? "&refresh=1" : ""}`);
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || "扫描失败");
      allNotes = d.notes;
      byPath = new Map(allNotes.map((n) => [n.path, n]));
      selected = new Set([...selected].filter((p) => byPath.has(p)));
      if (!skipRender) render();
    } catch (e) {
      $("scanHint").innerHTML = `<span class="err">${esc(e.message)}</span>`;
    }
  }

  $("reload").addEventListener("click", () => loadNotes(true));

  (async function init() {
    const prefs = loadPrefs();
    const r = await fetch("api/env");
    env = await r.json();

    $("depth").innerHTML = env.depths.map((d) =>
      `<option value="${d.key}">${d.label}（${d.words}）</option>`).join("");

    $("root").value = prefs.root || env.default_vault;
    $("outdir").value = prefs.outdir || env.default_output;
    if (prefs.depth) $("depth").value = prefs.depth;
    if (prefs.backend) $("backend").value = prefs.backend;
    if (prefs.apibase) $("apibase").value = prefs.apibase;
    if (prefs.conc) $("conc").value = prefs.conc;
    if (prefs.focus) $("focus").value = prefs.focus;
    if (prefs.useCache === false) $("useCache").checked = false;
    if (prefs.topic) $("topic").value = prefs.topic;
    if (prefs.topicDate !== undefined) $("topicDate").value = prefs.topicDate;
    if (prefs.cands) $("cands").value = prefs.cands;
    if (prefs.pick) $("pick").value = prefs.pick;
    if (prefs.staged) $("staged").checked = true;
    if (prefs.maxChars !== undefined) $("maxChars").value = prefs.maxChars;
    setMode(prefs.mode === "manual" ? "manual" : "topic");
    $("backend").dispatchEvent(new Event("change"));

    const bits = [];
    bits.push(env.claude_cli_found ? "✅ 已检测到 claude CLI" : "⚠️ 未检测到 claude CLI");
    bits.push(env.anthropic_installed ? "✅ anthropic 库可用" : "⚠️ 未安装 anthropic 库");
    if (env.env_api_key) bits.push("✅ 环境变量中有 ANTHROPIC_API_KEY");
    if (env.key_file_anthropic) bits.push("✅ 已找到 Anthropic Key 文件（可留空输入框）");
    if (env.key_file_openrouter) bits.push("✅ 已找到 OpenRouter Key 文件（可留空输入框）");
    if (env.ollama_models && env.ollama_models.length) bits.push(`✅ Ollama：${env.ollama_models.slice(0, 3).join(", ")}`);
    $("envHint").textContent = bits.join("　");

    $("toDeck").addEventListener("click", () => {
      $("deckPanel").scrollIntoView({ behavior: "smooth", block: "start" });
      $("deckRun").click();
    });

    await loadReports();
    syncDeckHint();
    await loadNotes(false, true);
    await restoreSession();
  })();
})();
