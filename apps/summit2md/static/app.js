(() => {
  const $ = (id) => document.getElementById(id);
  const qs = (root, role) => root.querySelector(`[data-role="${role}"]`);

  let entries = [];
  let sourceUrl = "";
  let agendaOrderMap = {}; // {video_id: order}，覆盖浏览器里看到的全部议题，不只是勾选的那些
  let importedShowDir = ""; // 「导入目录」导入的那个具体节目目录的绝对路径（不是输出根目录），用于读取/生成它的主题总结
  let defaultOutputDir = ""; // 记住服务端返回的默认输出根目录，供「重置」按钮恢复
  let hasExistingOverallSummary = false; // 当前导入的目录是否已经有大会/节目总结——决定要不要显示"沿用/重新生成"的选择
  const tasks = new Map(); // job_id -> {el, payload, failedEntries, progressSamples, pollTimer}

  // 入口模式：落地页上「Summit 总结」「Podcast 跟进」「信息跟进」是同一个 app 的
  // 三个入口，靠 ?mode= 区分。锁定之后，"内容类型"这个概念对用户就不存在了——选
  // 错模式的可能性也一并消失。没有 mode 参数时退回原来的行为（下拉可见、自动识别）。
  //
  // 「信息跟进」目前是「Podcast 跟进」的并行入口，不是替代——两边暂时共用同一套
  // 单期/单篇处理逻辑（后端 content_type 仍然只认 summit/series 两种，见下面的
  // CONTENT_TYPE_FOR_MODE），只是换了标题、文案和主题色，定位更宽（播客/RSS/博客都
  // 算），订阅列表这些「信息跟进」独有的功能还没接上。等那边做完，再回头看要不要把
  // 「Podcast 跟进」收掉、合并成一个入口。
  const MODE = new URLSearchParams(location.search).get("mode");
  const LOCKED = MODE === "series" || MODE === "summit" || MODE === "track" ? MODE : "";
  // 「信息跟进」在后端眼里就是 series（按发布日期命名、逐条小结+汇总），这张表把
  // "UI 模式" 和 "后端认的内容类型" 分开，别处不用记"track 其实是 series"这件事。
  const CONTENT_TYPE_FOR_MODE = { summit: "summit", series: "series", track: "series" };
  const MODE_TEXT = {
    summit: {
      title: "Summit 总结",
      subtitle: "给一个 YouTube 峰会播放列表链接，自动整理出全部议题链接、清洗后的文字记录，并生成逐议题小结与大会总结。",
      mismatch: "这个链接看起来像播客/视频栏目。仍会按「会议 / 峰会」处理——总结会去分析议程结构和策展思路，文件名用编号。想按节目处理请回落地页选「Podcast 跟进」。",
    },
    series: {
      title: "Podcast 跟进",
      subtitle: "给一个 Substack 播客、RSS/Atom 订阅源、Apple Podcast、YouTube 节目频道链接，或微信公众号单篇文章链接，自动整理出各期/各篇链接、清洗后的文字记录，并生成逐期小结与节目总结。",
      mismatch: "这个链接看起来像会议/峰会。仍会按「播客 / 视频栏目」处理——总结只按内容本身归纳话题，文件名用播出日期。想按大会处理请回落地页选「Summit 总结」。",
    },
    track: {
      title: "信息跟进",
      subtitle: "给一个播客、RSS/Atom 订阅源、博客、YouTube 频道，或微信公众号单篇文章链接，自动整理出各期/各篇链接、清洗后的文字记录，并生成逐条小结与汇总。",
      mismatch: "这个链接看起来像会议/峰会。仍会按「信息跟进」处理——总结只按内容本身归纳话题，文件名用发布日期。想按大会处理请回落地页选「Summit 总结」。",
    },
  };


  // 词表：Summit 和 Podcast 两个入口共用同一套界面，差异几乎全是这三个名词。
  // 与其把每句文案存两份（37 处，以后每改一句都得记得改两遍——Spark 这个项目
  // 当初就是为了消灭这种分叉），不如只定义这一张表，播客模式下统一替换。
  //
  // 已知失效方式：将来若有一句话里的"议题"不该被换成"单集"，这里会静默换错。
  // 所以词表要一直保持这么小，新增条目前先确认它真的是 1:1 的名词对应。
  // 复合词要排在前面：原文里有「大会/节目总结」这种把两种情况并列写的地方，
  // 不先整体换掉，逐词替换会得到「节目/节目总结」。锁定模式之后不需要再并列，
  // 各自说各自的就行——所以 Summit 那边也有一条，把并列写法收成「大会」。
  const GLOSSARIES = {
    series: [["大会/节目", "节目"], ["议题", "单集"], ["大会", "节目"], ["峰会", "节目"]],
    summit: [["大会/节目", "大会"]],
    // 「信息跟进」处理的是文章/资讯条目，不是"单集"；汇总是"汇总"，不是"节目总结"。
    track: [["大会/节目总结", "汇总"], ["大会/节目", "汇总"], ["大会总结", "汇总"], ["议题", "条目"],
            ["大会", "汇总"], ["峰会", "汇总"]],
  };

  // 文案本地化：没锁定模式时原样返回（保持合并界面时期的行为）。
  function T(s) {
    if (!LOCKED || !s) return s;
    return (GLOSSARIES[LOCKED] || []).reduce((acc, [from, to]) => acc.split(from).join(to), s);
  }

  // 把一棵已经在 DOM 里的子树按词表走一遍：只动文本节点和 placeholder/title，
  // 不碰 value、href 这些非展示内容。
  function localize(root) {
    if (!LOCKED || !root) return;
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    const texts = [];
    while (walker.nextNode()) texts.push(walker.currentNode);
    for (const n of texts) {
      const t = T(n.nodeValue);
      if (t !== n.nodeValue) n.nodeValue = t;
    }
    const scope = root.querySelectorAll ? root : document;
    for (const el of scope.querySelectorAll("[placeholder],[title]")) {
      for (const attr of ["placeholder", "title"]) {
        const v = el.getAttribute(attr);
        if (v) el.setAttribute(attr, T(v));
      }
    }
  }

  function applyMode() {
    if (!LOCKED) return;
    const t = MODE_TEXT[LOCKED];
    // 配色按模式走：CSS 里 :root[data-mode="series"] 会换掉整套强调色
    document.documentElement.setAttribute("data-mode", LOCKED);
    document.title = `${t.title} — Spark`;
    document.getElementById("titleText").textContent = t.title;
    document.querySelector(".subtitle").textContent = t.subtitle;
    $("contentType").value = CONTENT_TYPE_FOR_MODE[LOCKED];
    $("contentTypeField").style.display = "none";
    // 按议程重排只对大会有意义：播客没有议程，这一块在栏目模式下不该出现
    $("agendaSection").style.display = LOCKED === "summit" ? "" : "none";
    // 「仅选独立议题」是过滤"完整场次录像"的峰会专属概念，不是翻译问题——播客
    // 模式下直接不出现，而不是换个说法。
    $("selectTalksBtn").style.display = LOCKED === "summit" ? "" : "none";
    if (LOCKED === "track") {
      // 这两项在信息跟进里是隐藏的（.conf-only），确保不会带着上次的勾选悄悄生效
      $("doSpeakerLabel").checked = false;
      $("doSpeechScript").checked = false;
      document.querySelector("#taskPanel h2").innerHTML = `<span class="num">1</span>选择要处理的内容`;
    }
    localize(document.body);
  }

  // 自动识别出来的类型和当前模式不一致时不静默改写——模式是用户在落地页做的
  // 明确选择，但要让他知道这个链接看起来不像。
  function noteDetected(detected) {
    if (!LOCKED) return;
    const wrong = (detected === "series" ? "series" : "summit") !== CONTENT_TYPE_FOR_MODE[LOCKED];
    $("contentTypeHint").textContent = wrong ? MODE_TEXT[LOCKED].mismatch : "";
    $("contentTypeField").style.display = wrong ? "" : "none";
    if (wrong) $("contentType").value = CONTENT_TYPE_FOR_MODE[LOCKED];
  }

  // ---------- 「信息跟进」（只在 mode=track 下用到）----------
  // 三个标签页：
  // - 新内容：所有订阅的新条目汇在一起，勾选后一次生成"逐条笔记 + 本批简报"；
  // - 订阅管理：增删改订阅、分类；
  // - 临时链接：一次性的链接，走原来的 discover→选择→生成 流程。
  // "有没有新内容"不在前端记，每次检查都问服务端（服务端拿 manifest 现算）。
  const SOURCE_TYPE_LABEL = {
    rss: "RSS", substack: "Substack", wechat: "公众号", youtube: "YouTube",
    article: "网页 (sitemap)", unknown: "链接",
  };
  let subscriptions = [];
  let subsLoaded = false;
  let subsLoadError = "";
  let subsExpandedCats = new Set();
  let subsExpandedRowId = null;
  let subsEditingId = null;
  let subsAddOpen = false;
  let subsBulkOpen = false;
  let subsBulkResult = null; // 最近一次批量导入的结果 {added, failed}，展示完一次就清空
  let subsCheckResults = {}; // sub_id -> {new_count, new_entries, error, total}
  let subsLastCheckAllAt = null;
  let subsChecking = false;
  let subsCheckingOne = new Set();

  // 新内容收件箱：默认全选，这里只记"用户取消勾选了哪些"（键是 "订阅id|条目id"），
  // 这样重新检查后冒出来的新条目自动是勾上的。
  const inboxUnchecked = new Set();
  let inboxJob = null;       // {id, log, current, total, done, result, error}
  let inboxPollTimer = null;
  let inboxSummaryLength = "medium";
  try { inboxSummaryLength = localStorage.getItem("track.summaryLength") || "medium"; } catch (e) { /* ignore */ }

  const { escHtml, attachDirAutocomplete } = window.SparkCommon;

  function safeHref(url) {
    return /^https?:\/\//i.test(url || "") ? escHtml(url) : "";
  }

  function renderTrack() {
    renderSubs();
    renderInbox();
  }

  async function loadSubscriptions() {
    try {
      const r = await fetch("api/subscriptions");
      const d = await r.json();
      if (!r.ok || !Array.isArray(d)) throw new Error(d.error || "读取订阅列表失败");
      subscriptions = d;
      subsLoadError = "";
    } catch (e) {
      subsLoadError = e.message;
    }
    subsLoaded = true;
    renderTrack();
  }

  // 只做免费的列表探测 + 跟 manifest 比对，不碰模型。
  async function checkAllSubscriptions() {
    if (!subscriptions.length || subsChecking) return;
    subsChecking = true;
    renderTrack();
    try {
      const r = await fetch("api/subscriptions/check_all", { method: "POST" });
      const d = await r.json();
      (d.results || []).forEach((row) => { subsCheckResults[row.id] = row; });
      subsLastCheckAllAt = Date.now();
    } catch (e) {
      // 不挡住页面，按钮还能再点
    }
    subsChecking = false;
    renderTrack();
  }

  async function checkOneSubscription(id) {
    if (subsCheckingOne.has(id)) return;
    subsCheckingOne.add(id);
    renderTrack();
    try {
      const r = await fetch(`api/subscriptions/${id}/check`, { method: "POST" });
      const row = await r.json();
      subsCheckResults[id] = r.ok ? row : { id, error: row.error || "检查失败", new_count: 0, new_entries: [] };
    } catch (e) {
      subsCheckResults[id] = { id, error: e.message, new_count: 0, new_entries: [] };
    }
    subsCheckingOne.delete(id);
    renderTrack();
  }

  function categoriesInUse() {
    const set = new Set(subscriptions.map((s) => s.category || "未分类"));
    return [...set].sort((a, b) => a.localeCompare(b, "zh"));
  }

  async function submitAddSubscription() {
    const url = $("subsNewUrl").value.trim();
    const name = $("subsNewName").value.trim();
    const category = $("subsNewCategory").value.trim() || "未分类";
    $("subsAddErr").textContent = "";
    if (!url) { $("subsAddErr").textContent = "请输入链接"; return; }
    $("subsAddSubmit").disabled = true;
    $("subsAddSubmit").textContent = "正在识别链接…";
    try {
      const r = await fetch("api/subscriptions", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url, name, category, output_dir: $("outputDir").value }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || "添加失败");
      subsAddOpen = false;
      await loadSubscriptions();
      checkOneSubscription(d.id);
    } catch (e) {
      $("subsAddErr").textContent = e.message;
      const btn = $("subsAddSubmit");
      if (btn) { btn.disabled = false; btn.textContent = "添加"; }
    }
  }

  // 批量导入：粘贴一段"名称 : 链接"（或者干脆只有链接），一行一条，统一分到
  // 同一个类别。允许部分失败——失败/已经订阅过的会在结果里列出来。
  async function submitBulkSubscriptions() {
    const text = $("subsBulkText").value;
    const category = $("subsBulkCategory").value.trim() || "未分类";
    $("subsBulkErr").textContent = "";
    if (!text.trim()) { $("subsBulkErr").textContent = "请粘贴至少一行「名称 : 链接」"; return; }
    $("subsBulkSubmit").disabled = true;
    $("subsBulkSubmit").textContent = "正在逐条识别…";
    try {
      const r = await fetch("api/subscriptions/bulk", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text, category, output_dir: $("outputDir").value }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || "导入失败");
      subsBulkOpen = false;
      subsBulkResult = { added: d.added, failed: d.failed };
      await loadSubscriptions();
      checkAllSubscriptions();
    } catch (e) {
      $("subsBulkErr").textContent = e.message;
      const btn = $("subsBulkSubmit");
      if (btn) { btn.disabled = false; btn.textContent = "导入"; }
    }
  }

  async function saveSubscriptionEdit(id) {
    const name = $(`subEditName-${id}`).value.trim();
    const category = $(`subEditCategory-${id}`).value.trim() || "未分类";
    const folder = $(`subEditFolder-${id}`).value.trim();
    if (!name) { $(`subEditErr-${id}`).textContent = "名称不能为空"; return; }
    try {
      const r = await fetch(`api/subscriptions/${id}`, {
        method: "PATCH", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, category, folder }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || "保存失败");
      subsEditingId = null;
      await loadSubscriptions();
    } catch (e) {
      $(`subEditErr-${id}`).textContent = e.message;
    }
  }

  async function deleteSubscription(id, name) {
    if (!confirm(`停止跟进「${name}」？已经生成的笔记不会被删除，只是不再检查它有没有更新。`)) return;
    try {
      const r = await fetch(`api/subscriptions/${id}`, { method: "DELETE" });
      if (!r.ok) { const d = await r.json().catch(() => ({})); throw new Error(d.error || "删除失败"); }
      delete subsCheckResults[id];
      await loadSubscriptions();
    } catch (e) {
      alert(e.message);
    }
  }

  async function renameSubsCategory(oldName) {
    let next;
    try {
      next = prompt(`把类别「${oldName}」下的全部订阅重命名到：`, oldName);
    } catch (e) {
      return;
    }
    if (next === null || next === undefined) return;
    const trimmed = next.trim();
    if (!trimmed || trimmed === oldName) return;
    try {
      const r = await fetch("api/subscriptions/rename_category", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ old: oldName, new: trimmed }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || "重命名失败");
      subsExpandedCats.delete(oldName);
      subsExpandedCats.add(trimmed);
      await loadSubscriptions();
    } catch (e) {
      alert(e.message);
    }
  }

  // ---- 订阅管理 ----
  function renderSubRow(item) {
    const id = item.id;
    if (subsEditingId === id) {
      return `
        <div class="subs-row" style="flex-direction:column;align-items:stretch;gap:var(--space-4)">
          <div class="row">
            <div class="field" style="margin-bottom:0">
              <label for="subEditName-${id}">名称</label>
              <input type="text" id="subEditName-${id}" value="${escHtml(item.name)}" />
            </div>
            <div class="field" style="margin-bottom:0">
              <label for="subEditCategory-${id}">类别</label>
              <input type="text" id="subEditCategory-${id}" list="subsCategoryList" value="${escHtml(item.category)}" />
            </div>
          </div>
          <div class="field" style="margin-bottom:0">
            <label for="subEditFolder-${id}">笔记文件夹（改名不会自动挪；要换位置就改这里）</label>
            <input type="text" id="subEditFolder-${id}" value="${escHtml(item.folder)}" />
          </div>
          <div class="err-box" id="subEditErr-${id}" style="margin-top:0"></div>
          <div style="display:flex;gap:var(--space-3)">
            <button type="button" class="mini" data-sub-save="${id}">保存</button>
            <button type="button" class="secondary mini" data-sub-cancel-edit="${id}">取消</button>
          </div>
        </div>`;
    }

    const check = subsCheckResults[id];
    const newCount = check && !check.error ? check.new_count : 0;
    let badge = "";
    if (check && check.error) badge = `<span class="subs-badge" style="color:var(--err)">检查失败</span>`;
    else if (newCount > 0) badge = `<span class="subs-badge">${newCount} 条新</span>`;
    const dotClass = check && check.error ? "no-new" : (newCount > 0 ? "has-new" : "no-new");
    const checking = subsCheckingOne.has(id);

    return `
      <div class="subs-row">
        <button type="button" class="subs-name" data-sub-toggle="${id}" title="查看详情">
          <span class="subs-dot ${dotClass}" aria-hidden="true"></span>
          <span class="label">${escHtml(item.name)}</span>
        </button>
        <span class="tag">${SOURCE_TYPE_LABEL[item.source_type] || "链接"}</span>
        ${badge}
        <div class="subs-actions">
          <button type="button" class="secondary mini" data-sub-check="${id}" ${checking ? "disabled" : ""}>${checking ? "检查中…" : "检查"}</button>
          <button type="button" class="secondary mini" data-sub-edit="${id}">编辑</button>
          <button type="button" class="secondary mini" data-sub-delete="${id}">删除</button>
        </div>
      </div>
      ${subsExpandedRowId === id ? renderSubExpanded(item, check) : ""}`;
  }

  function renderSubExpanded(item, check) {
    let status;
    if (check && check.error) status = `<p class="subs-err">检查失败：${escHtml(check.error)}</p>`;
    else if (!check) status = `<p class="hint" style="margin:0">还没检查过。</p>`;
    else status = `<p class="hint" style="margin:0">源里共 ${check.total ?? 0} 条，其中 ${check.new_count} 条还没处理${check.new_count ? "（在「新内容」里）" : ""}。</p>`;
    const ignored = (item.ignored_ids || []).length;
    return `
      <div class="subs-new-panel">
        ${status}
        <p class="hint" style="margin:6px 0 0">链接：${escHtml(item.url)}</p>
        <p class="hint" style="margin:2px 0 0">笔记文件夹：${escHtml(item.folder)}</p>
        ${ignored ? `<p class="hint" style="margin:2px 0 0">已忽略 ${ignored} 条</p>` : ""}
      </div>`;
  }

  function renderSubs() {
    const box = $("subsBox");
    if (!subsLoaded) { box.innerHTML = `<p class="hint">正在加载订阅列表…</p>`; return; }
    if (subsLoadError) { box.innerHTML = `<p class="subs-err">${escHtml(subsLoadError)}</p>`; return; }

    const cats = categoriesInUse();
    let listHtml;
    if (!subscriptions.length) {
      listHtml = `<div class="subs-empty">还没有订阅——点下面「添加订阅」，粘一个 RSS / 博客 / 播客 / YouTube 频道链接。</div>`;
    } else {
      listHtml = `<div class="subs-list">` + cats.map((cat) => {
        const items = subscriptions.filter((s) => (s.category || "未分类") === cat);
        const open = subsExpandedCats.has(cat);
        const newTotal = items.reduce((n, it) => {
          const c = subsCheckResults[it.id];
          return n + (c && !c.error ? c.new_count : 0);
        }, 0);
        const meta = `${items.length} 个订阅`
          + (newTotal > 0 ? ` · <span class="subs-cat-new">${newTotal} 条新内容</span>` : "");
        return `
          <div class="subs-cat">
            <button type="button" class="subs-cat-toggle" data-cat-toggle="${escHtml(cat)}">
              <span class="caret">${open ? "▾" : "▸"}</span>
              <strong>${escHtml(cat)}</strong>
            </button>
            <span class="subs-cat-meta">${meta}</span>
            <button type="button" class="secondary mini" data-cat-rename="${escHtml(cat)}">重命名</button>
          </div>
          ${open ? `<div class="subs-branch">${items.map(renderSubRow).join("")}</div>` : ""}`;
      }).join("") + `</div>`;
    }

    const addButtons = `
      <div class="row" style="gap:var(--space-3)">
        <button type="button" class="subs-add-btn" id="subsAddOpenBtn">+ 添加订阅</button>
        <button type="button" class="subs-add-btn" id="subsBulkOpenBtn">+ 批量导入</button>
      </div>`;
    const addForm = `
      <div class="subs-add-form">
        <strong>添加订阅</strong>
        <div class="field" style="margin-bottom:0">
          <label for="subsNewUrl">链接</label>
          <input type="text" id="subsNewUrl" placeholder="RSS 订阅地址 / 没有 RSS 的资讯页（如 anthropic.com/news）/ 播客 / YouTube 频道" />
        </div>
        <div class="row">
          <div class="field" style="margin-bottom:0">
            <label for="subsNewName">名称（可选，留空用探测到的标题）</label>
            <input type="text" id="subsNewName" />
          </div>
          <div class="field" style="margin-bottom:0">
            <label for="subsNewCategory">类别</label>
            <input type="text" id="subsNewCategory" list="subsCategoryList" placeholder="选一个已有的，或直接输入新类别" />
          </div>
        </div>
        <div class="err-box" id="subsAddErr" style="margin-top:0"></div>
        <div style="display:flex;gap:var(--space-3)">
          <button type="button" id="subsAddSubmit">添加</button>
          <button type="button" class="secondary" id="subsAddCancel">取消</button>
        </div>
      </div>`;
    const bulkForm = `
      <div class="subs-add-form">
        <strong>批量导入</strong>
        <div class="field" style="margin-bottom:0">
          <label for="subsBulkText">粘贴一段「名称 : 链接」，一行一条（只有链接、没有名称也可以）</label>
          <textarea id="subsBulkText" rows="6" placeholder="MarkTechPost : https://www.marktechpost.com/feed/&#10;AI Insider : https://theaiinsider.tech/feed/&#10;https://the-decoder.com/feed/"></textarea>
        </div>
        <div class="field" style="margin-bottom:0">
          <label for="subsBulkCategory">类别（这一批统一归到这个类别下）</label>
          <input type="text" id="subsBulkCategory" list="subsCategoryList" placeholder="选一个已有的，或直接输入新类别" />
        </div>
        <div class="err-box" id="subsBulkErr" style="margin-top:0"></div>
        <div style="display:flex;gap:var(--space-3)">
          <button type="button" id="subsBulkSubmit">导入</button>
          <button type="button" class="secondary" id="subsBulkCancel">取消</button>
        </div>
      </div>`;

    let bulkResultHtml = "";
    if (subsBulkResult) {
      const { added, failed } = subsBulkResult;
      bulkResultHtml = `
        <div class="subs-add-form">
          <strong>批量导入完成</strong>
          <p class="hint" style="margin:0">成功 ${added.length} 条${failed.length ? `，跳过/失败 ${failed.length} 条` : ""}</p>
          ${failed.length ? `<div class="subs-new-panel">${failed.map((f) => `
            <div class="item"><span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${escHtml(f.name || f.url)}</span>
            <span class="hint" style="margin:0;color:var(--err)">${escHtml(f.error)}</span></div>`).join("")}</div>` : ""}
          <div><button type="button" class="secondary mini" id="subsBulkResultDismiss">知道了</button></div>
        </div>`;
    }

    // 添加/批量导入表单打开时不重画这一块：检查结果随时会回来，整块重画会把
    // 正在输入的内容冲掉。列表部分照常更新。
    const formOpen = subsAddOpen || subsBulkOpen;
    let addAreaHtml;
    if (subsAddOpen) addAreaHtml = addForm;
    else if (subsBulkOpen) addAreaHtml = bulkForm;
    else addAreaHtml = bulkResultHtml + addButtons;

    let listEl = box.querySelector(":scope > .subs-list-area");
    let formEl = box.querySelector(":scope > .subs-form-area");
    if (!listEl || !formEl) {
      box.innerHTML = `<div class="subs-list-area"></div><div class="subs-form-area"></div>`;
      listEl = box.querySelector(":scope > .subs-list-area");
      formEl = box.querySelector(":scope > .subs-form-area");
      formEl.dataset.mode = "";
    }
    const editFormShown = subsEditingId && listEl.querySelector(`#subEditName-${CSS.escape(subsEditingId)}`);
    if (!editFormShown) {
      listEl.innerHTML = listHtml
        + `<datalist id="subsCategoryList">${cats.map((c) => `<option value="${escHtml(c)}"></option>`).join("")}</datalist>`;
    }
    const mode = subsAddOpen ? "add" : subsBulkOpen ? "bulk" : "buttons";
    if (!formOpen || formEl.dataset.mode !== mode) {
      formEl.innerHTML = addAreaHtml;
      formEl.dataset.mode = mode;
    }
  }

  // ---- 新内容收件箱 ----
  function inboxKey(subId, entryId) { return `${subId}|${entryId}`; }

  function inboxGroups() {
    // [{cat, subs: [{sub, entries}]}]，只保留有新内容的订阅
    const byCat = new Map();
    for (const sub of subscriptions) {
      const c = subsCheckResults[sub.id];
      if (!c || c.error || !c.new_entries || !c.new_entries.length) continue;
      const cat = sub.category || "未分类";
      if (!byCat.has(cat)) byCat.set(cat, []);
      byCat.get(cat).push({ sub, entries: c.new_entries });
    }
    return [...byCat.entries()]
      .sort((a, b) => a[0].localeCompare(b[0], "zh"))
      .map(([cat, subs]) => ({ cat, subs }));
  }

  function inboxSelections() {
    const out = [];
    for (const g of inboxGroups()) {
      for (const { sub, entries } of g.subs) {
        const ids = entries.map((e) => e.id).filter((id) => !inboxUnchecked.has(inboxKey(sub.id, id)));
        if (ids.length) out.push({ sub_id: sub.id, entry_ids: ids });
      }
    }
    return out;
  }

  function fmtDate8(d) {
    return /^\d{8}$/.test(d || "") ? `${d.slice(0, 4)}-${d.slice(4, 6)}-${d.slice(6)}` : "";
  }

  function renderInbox() {
    const box = $("inboxBox");
    if (!box) return;
    if (inboxJob) { renderInboxJob(box); return; }
    if (!subsLoaded) { box.innerHTML = `<p class="hint">正在加载订阅列表…</p>`; return; }
    if (subsLoadError) { box.innerHTML = `<p class="subs-err">${escHtml(subsLoadError)}</p>`; return; }
    if (!subscriptions.length) {
      box.innerHTML = `<div class="subs-empty">还没有订阅。到「订阅管理」里添加 RSS、资讯网站、播客或 YouTube 频道，之后新内容会出现在这里。</div>`;
      return;
    }

    const groups = inboxGroups();
    const total = groups.reduce((n, g) => n + g.subs.reduce((m, s) => m + s.entries.length, 0), 0);
    const selected = inboxSelections().reduce((n, s) => n + s.entry_ids.length, 0);
    const failedChecks = subscriptions.filter((s) => subsCheckResults[s.id]?.error);
    const checkedAt = subsLastCheckAllAt ? `上次检查 ${fmtClock(subsLastCheckAllAt)}` : "";

    const head = `
      <div class="inbox-head">
        <span>${subsChecking ? `正在检查 ${subscriptions.length} 个订阅……` : `${total} 条新内容 · ${checkedAt}`}</span>
        <span class="spacer"></span>
        ${total ? `<button type="button" class="secondary mini" id="inboxAll">全选</button>
        <button type="button" class="secondary mini" id="inboxNone">全不选</button>` : ""}
        <button type="button" class="secondary mini" id="inboxRecheck" ${subsChecking ? "disabled" : ""}>${subsChecking ? "检查中…" : "重新检查"}</button>
      </div>`;

    let list;
    if (!total) {
      list = subsChecking
        ? `<div class="subs-empty">正在检查订阅有没有新内容（只列标题，不消耗模型调用）……</div>`
        : `<div class="subs-empty">所有订阅都没有新内容。</div>`;
    } else {
      list = `<div class="inbox-list">` + groups.map((g) => `
        <div class="inbox-cat">${escHtml(g.cat)}</div>
        ${g.subs.map(({ sub, entries }) => {
          const on = entries.filter((e) => !inboxUnchecked.has(inboxKey(sub.id, e.id))).length;
          return `
          <div class="inbox-sub">
            <input type="checkbox" data-inbox-sub="${sub.id}" aria-label="全选 ${escHtml(sub.name)}"
              ${on === entries.length ? "checked" : ""} ${on > 0 && on < entries.length ? 'data-indeterminate="1"' : ""} />
            <strong class="inbox-sub-name">${escHtml(sub.name)}</strong>
            <span class="hint" style="margin:0">${entries.length} 条</span>
            <span class="spacer"></span>
            <button type="button" class="secondary mini" data-inbox-ignore-sub="${sub.id}">全部忽略</button>
          </div>
          ${entries.map((e) => {
            const key = inboxKey(sub.id, e.id);
            const href = safeHref(e.url);
            const title = escHtml(e.title || "（无标题）");
            return `
            <div class="inbox-item">
              <input type="checkbox" data-inbox-item="${escHtml(key)}" id="ib-${escHtml(key)}" ${inboxUnchecked.has(key) ? "" : "checked"} />
              <div class="inbox-item-body">
                ${href ? `<a href="${href}" target="_blank" rel="noopener">${title}</a>` : `<span>${title}</span>`}
                <div class="inbox-item-meta">${fmtDate8(e.publish_date)}${e.last_error ? ` <span class="inbox-last-err">· 上次失败：${escHtml(e.last_error)}</span>` : ""}</div>
              </div>
              <button type="button" class="secondary mini" data-inbox-ignore="${escHtml(key)}">忽略</button>
            </div>`;
          }).join("")}`;
        }).join("")}`).join("") + `</div>`;
    }

    const failedHtml = failedChecks.length ? `
      <details class="more inbox-failed">
        <summary>${failedChecks.length} 个订阅这次检查失败</summary>
        ${failedChecks.map((s) => `<p class="hint" style="margin:4px 0 0"><strong>${escHtml(s.name)}</strong>：${escHtml(subsCheckResults[s.id].error)}</p>`).join("")}
      </details>` : "";

    const actions = total ? `
      <div class="inbox-actions">
        <span>已选 <b>${selected}</b> 条${selected ? ` · 预计 ${selected + 1} 次模型调用（每条一次小结 + 一次简报）` : ""}</span>
        <span class="spacer"></span>
        <label for="inboxSummaryLength" class="hint" style="margin:0">小结篇幅</label>
        <select id="inboxSummaryLength" style="width:auto">
          <option value="short" ${inboxSummaryLength === "short" ? "selected" : ""}>简洁</option>
          <option value="medium" ${inboxSummaryLength === "medium" ? "selected" : ""}>标准</option>
          <option value="long" ${inboxSummaryLength === "long" ? "selected" : ""}>详细</option>
        </select>
        <button type="button" id="inboxRun" ${selected ? "" : "disabled"}>生成简报</button>
      </div>
      <p class="hint">每条存成一篇笔记（小结 + 原文），放在「信息跟进/订阅名/」；再出一份本批简报放在「信息跟进/简报/」。用哪个模型在下面设置。</p>
      <div class="err-box" id="inboxErr"></div>` : "";

    box.innerHTML = head + list + failedHtml + actions;
    box.querySelectorAll("[data-indeterminate]").forEach((el) => { el.indeterminate = true; });
  }

  function renderInboxJob(box) {
    const j = inboxJob;
    const pct = j.total ? Math.round((j.current / j.total) * 100) : 0;
    if (!j.done) {
      box.innerHTML = `
        <div class="inbox-head"><span>正在处理 ${j.current}/${j.total} 条……</span><span class="spacer"></span>
          <button type="button" class="secondary mini" id="inboxStop" ${j.stopping ? "disabled" : ""}>${j.stopping ? "正在停止…" : "停止"}</button></div>
        <div class="progress-bar"><div style="width:${pct}%"></div></div>
        <div class="log-box" id="inboxLog"></div>`;
      $("inboxLog").textContent = (j.log || []).join("\n");
      $("inboxLog").scrollTop = $("inboxLog").scrollHeight;
      return;
    }
    const r = j.result || {};
    const failed = r.failed || [];
    let summary;
    if (j.error) summary = `<p class="subs-err">${escHtml(j.error)}</p>`;
    else if (r.stopped) summary = `<p class="hint">已停止。处理完的 ${r.processed || 0} 条已经保存，没处理的下次还会出现在新内容里。</p>`;
    else summary = `<p class="hint">处理了 ${r.processed || 0} 条${failed.length ? `，${failed.length} 条失败（下次检查还会出现，可以重试或忽略）` : ""}。</p>`;
    const path = r.brief_path ? `
      <p class="hint" style="margin:0 0 var(--space-3)">简报已保存：<code>${escHtml(r.brief_path)}</code>
        <a href="obsidian://open?path=${encodeURIComponent(r.brief_path)}">在 Obsidian 中打开</a></p>` : "";
    const failedHtml = failed.length ? `
      <details class="more"><summary>失败的 ${failed.length} 条</summary>
        ${failed.map((f) => `<p class="hint" style="margin:4px 0 0">${escHtml(f.sub_name)} · ${escHtml(f.title || f.id)}：${escHtml(f.error)}</p>`).join("")}
      </details>` : "";
    box.innerHTML = `
      <div class="inbox-head"><strong>${r.brief_path ? "本批简报" : "处理结果"}</strong><span class="spacer"></span>
        <button type="button" class="mini" id="inboxDone">完成</button></div>
      ${summary}${path}${failedHtml}
      <div class="summary-preview" id="inboxBrief"></div>`;
    if (r.brief_markdown) {
      // 页面里只看内容：去掉指向笔记文件的相对链接（浏览器里点不开），保留文字
      const md = r.brief_markdown
        .replace(/\[((?:\\\]|[^\]])*)\]\(<[^>]*>\)/g, "$1")
        .replace(/\\([[\]])/g, "$1");
      renderMarkdown(md, $("inboxBrief"));
    }
  }

  async function ignoreEntries(subId, entryIds) {
    try {
      const r = await fetch(`api/subscriptions/${subId}/ignore`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ entry_ids: entryIds }),
      });
      if (!r.ok) { const d = await r.json().catch(() => ({})); throw new Error(d.error || "忽略失败"); }
      const c = subsCheckResults[subId];
      if (c && c.new_entries) {
        const drop = new Set(entryIds);
        c.new_entries = c.new_entries.filter((e) => !drop.has(e.id));
        c.new_count = c.new_entries.length;
      }
      const sub = subscriptions.find((s) => s.id === subId);
      if (sub) sub.ignored_ids = [...(sub.ignored_ids || []), ...entryIds];
      renderTrack();
    } catch (e) {
      alert(e.message);
    }
  }

  const INBOX_CONFIRM_OVER = 30;

  async function startInboxRun() {
    const selections = inboxSelections();
    if (!selections.length) return;
    const count = selections.reduce((n, s) => n + s.entry_ids.length, 0);
    if (count > INBOX_CONFIRM_OVER
        && !confirm(`这次要处理 ${count} 条，会调用 ${count + 1} 次模型。确定继续？（可以先点「全不选」，只勾想看的）`)) {
      return;
    }
    const cfg = currentBackendConfig();
    const btn = $("inboxRun");
    btn.disabled = true;
    btn.textContent = "正在启动…";
    try {
      const r = await fetch("api/track/run", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          selections,
          output_dir: $("outputDir").value.trim(),
          summary_length: inboxSummaryLength,
          max_transcript_chars: parseMaxTranscriptChars(),
          overall_model: $("overallModel").value.trim(),
          ...cfg,
        }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || "启动失败");
      inboxJob = { id: d.job_id, log: [], current: 0, total: selections.reduce((n, s) => n + s.entry_ids.length, 0), done: false };
      renderInbox();
      pollInboxJob();
    } catch (e) {
      $("inboxErr").textContent = e.message;
      btn.disabled = false;
      btn.textContent = "生成简报";
    }
  }

  function pollInboxJob() {
    clearTimeout(inboxPollTimer);
    const job = inboxJob;
    if (!job || job.done) return;
    inboxPollTimer = setTimeout(async () => {
      try {
        const r = await fetch(`api/track/status/${job.id}`);
        const d = await r.json();
        if (r.status === 404) {
          Object.assign(job, { done: true, error: "找不到这个任务了（服务可能重启过）。已处理完的条目都已保存，重新检查即可看到剩下的。" });
        } else if (r.ok) {
          Object.assign(job, d);
          job.failures = 0;
        }
      } catch (e) {
        // 偶发网络抖动：连续失败多次才放弃
        job.failures = (job.failures || 0) + 1;
        if (job.failures >= 10) Object.assign(job, { done: true, error: `连续多次查询进度失败：${e.message}` });
      }
      if (inboxJob === job) {
        renderInbox();
        if (!job.done) pollInboxJob();
      }
    }, 1200);
  }

  $("inboxBox").addEventListener("change", (e) => {
    const t = e.target;
    if (t.dataset.inboxItem) {
      t.checked ? inboxUnchecked.delete(t.dataset.inboxItem) : inboxUnchecked.add(t.dataset.inboxItem);
      renderInbox();
    } else if (t.dataset.inboxSub) {
      const c = subsCheckResults[t.dataset.inboxSub];
      (c?.new_entries || []).forEach((en) => {
        const key = inboxKey(t.dataset.inboxSub, en.id);
        t.checked ? inboxUnchecked.delete(key) : inboxUnchecked.add(key);
      });
      renderInbox();
    } else if (t.id === "inboxSummaryLength") {
      inboxSummaryLength = t.value;
      try { localStorage.setItem("track.summaryLength", t.value); } catch (err) { /* ignore */ }
    }
  });

  $("inboxBox").addEventListener("click", (e) => {
    const t = e.target;
    let key;
    if ((key = t.closest("[data-inbox-ignore]")?.dataset.inboxIgnore)) {
      const [subId, entryId] = key.split("|");
      ignoreEntries(subId, [entryId]);
    } else if ((key = t.closest("[data-inbox-ignore-sub]")?.dataset.inboxIgnoreSub)) {
      const sub = subscriptions.find((s) => s.id === key);
      const ids = (subsCheckResults[key]?.new_entries || []).map((en) => en.id);
      if (ids.length && confirm(`忽略「${sub ? sub.name : ""}」这次的全部 ${ids.length} 条？忽略后不会再出现在新内容里。`)) {
        ignoreEntries(key, ids);
      }
    } else if (t.closest("#inboxAll") || t.closest("#inboxNone")) {
      const none = !!t.closest("#inboxNone");
      for (const g of inboxGroups()) {
        for (const { sub, entries } of g.subs) {
          entries.forEach((en) => {
            const k = inboxKey(sub.id, en.id);
            none ? inboxUnchecked.add(k) : inboxUnchecked.delete(k);
          });
        }
      }
      renderInbox();
    } else if (t.closest("#inboxRecheck")) {
      checkAllSubscriptions();
    } else if (t.closest("#inboxRun")) {
      startInboxRun();
    } else if (t.closest("#inboxStop")) {
      if (!inboxJob) return;
      inboxJob.stopping = true;
      renderInbox();
      fetch(`api/track/stop/${inboxJob.id}`, { method: "POST" }).catch(() => {});
    } else if (t.closest("#inboxDone")) {
      inboxJob = null;
      renderInbox();
      checkAllSubscriptions();
    }
  });

  function fmtClock(ts) {
    const d = new Date(ts);
    const pad = (n) => String(n).padStart(2, "0");
    return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }

  $("subsBox").addEventListener("click", (e) => {
    const t = e.target;
    let id;
    if ((id = t.closest("[data-cat-toggle]")?.dataset.catToggle) !== undefined) {
      subsExpandedCats.has(id) ? subsExpandedCats.delete(id) : subsExpandedCats.add(id);
      renderSubs();
    } else if ((id = t.closest("[data-cat-rename]")?.dataset.catRename) !== undefined) {
      renameSubsCategory(id);
    } else if ((id = t.closest("[data-sub-toggle]")?.dataset.subToggle) !== undefined) {
      subsExpandedRowId = subsExpandedRowId === id ? null : id;
      renderSubs();
    } else if ((id = t.closest("[data-sub-check]")?.dataset.subCheck) !== undefined) {
      checkOneSubscription(id);
    } else if ((id = t.closest("[data-sub-edit]")?.dataset.subEdit) !== undefined) {
      subsEditingId = id;
      renderSubs();
    } else if ((id = t.closest("[data-sub-save]")?.dataset.subSave) !== undefined) {
      saveSubscriptionEdit(id);
    } else if ((id = t.closest("[data-sub-cancel-edit]")?.dataset.subCancelEdit) !== undefined) {
      subsEditingId = null;
      renderSubs();
    } else if ((id = t.closest("[data-sub-delete]")?.dataset.subDelete) !== undefined) {
      const item = subscriptions.find((s) => s.id === id);
      deleteSubscription(id, item ? item.name : "");
    } else if (t.closest("#subsAddOpenBtn")) {
      subsAddOpen = true;
      subsBulkResult = null;
      renderSubs();
      $("subsNewUrl")?.focus();
    } else if (t.closest("#subsAddCancel")) {
      subsAddOpen = false;
      renderSubs();
    } else if (t.closest("#subsAddSubmit")) {
      submitAddSubscription();
    } else if (t.closest("#subsBulkOpenBtn")) {
      subsBulkOpen = true;
      subsBulkResult = null;
      renderSubs();
      $("subsBulkText")?.focus();
    } else if (t.closest("#subsBulkCancel")) {
      subsBulkOpen = false;
      renderSubs();
    } else if (t.closest("#subsBulkSubmit")) {
      submitBulkSubscriptions();
    } else if (t.closest("#subsBulkResultDismiss")) {
      subsBulkResult = null;
      renderSubs();
    }
  });

  // AI 后端那块面板原本长在「选择议题」之后（临时链接流程里）。新内容和订阅
  // 管理这两个标签页用不到那套选择界面，但仍需要选模型——把同一个面板挪过来，
  // 切回临时链接时再放回原位，两边用的始终是同一份设置。
  const backendPanel = $("backendField");
  const backendHome = document.createComment("backendField-home");
  backendPanel.parentNode.insertBefore(backendHome, backendPanel);
  const backendTitle = backendPanel.querySelector("h2");
  const backendTitleHome = backendTitle.innerHTML;

  function showTrackTab(which) {
    for (const [tab, box] of [["tabInbox", "inboxBox"], ["tabSubs", "subsBox"], ["tabLinkMode", "linkModeBox"]]) {
      $(tab).classList.toggle("on", tab === which);
      $(box).classList.toggle("hidden", tab !== which);
    }
    if (which === "tabInbox") {
      $("trackBackendHost").appendChild(backendPanel);
      backendTitle.innerHTML = `<span class="num">2</span>用哪个模型`;
    } else {
      backendHome.parentNode.insertBefore(backendPanel, backendHome.nextSibling);
      backendTitle.innerHTML = backendTitleHome;
    }
  }
  $("tabInbox").addEventListener("click", () => showTrackTab("tabInbox"));
  $("tabSubs").addEventListener("click", () => showTrackTab("tabSubs"));
  $("tabLinkMode").addEventListener("click", () => showTrackTab("tabLinkMode"));

  function initTrackSubscriptions() {
    $("trackTabs").classList.remove("hidden");
    showTrackTab("tabInbox");
    loadSubscriptions().then(checkAllSubscriptions);
  }

  function fmtDuration(sec) {
    sec = Math.floor(sec || 0);
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    const s = sec % 60;
    const pad = (n) => String(n).padStart(2, "0");
    return h ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
  }

  function fmtEta(ms) {
    const totalSec = Math.max(1, Math.round(ms / 1000));
    if (totalSec < 60) return `约 ${totalSec} 秒`;
    const totalMin = Math.round(totalSec / 60);
    if (totalMin < 60) return `约 ${totalMin} 分钟`;
    const h = Math.floor(totalMin / 60);
    const m = totalMin % 60;
    return m ? `约 ${h} 小时 ${m} 分钟` : `约 ${h} 小时`;
  }

  // ---- 最近使用的链接（存在浏览器本地，最多 10 条，仅本机可见）----
  const RECENT_URLS_KEY = "summit2md.recentUrls";

  function loadRecentUrls() {
    try {
      const list = JSON.parse(localStorage.getItem(RECENT_URLS_KEY));
      return Array.isArray(list) ? list : [];
    } catch (e) {
      return [];
    }
  }

  function rememberUrl(url, title, contentType) {
    let list = loadRecentUrls().filter((item) => item.url !== url);
    list.unshift({ url, title: title || url, content_type: contentType || "summit" });
    list = list.slice(0, 10);
    try { localStorage.setItem(RECENT_URLS_KEY, JSON.stringify(list)); } catch (e) { /* ignore */ }
    renderRecentUrls();
  }

  function renderRecentUrls() {
    const box = $("recentUrls");
    const list = loadRecentUrls();
    box.querySelectorAll(".recent-chip").forEach((el) => el.remove());
    box.style.display = list.length ? "flex" : "none";
    list.forEach((item) => {
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "recent-chip";
      chip.title = item.url;
      const tag = document.createElement("span");
      tag.className = "tag";
      tag.textContent = item.content_type === "series" ? "栏目" : "峰会";
      chip.appendChild(tag);
      chip.appendChild(document.createTextNode(item.title));
      chip.addEventListener("click", () => runDiscover(item.url));
      box.appendChild(chip);
    });
  }


  // ---- 极简 Markdown 渲染（仅用 DOM API，不把 LLM 输出拼进 innerHTML）----
  function appendInline(parent, text) {
    const pattern = /(\*\*(.+?)\*\*|\[(.+?)\]\((.+?)\))/g;
    let match;
    let last = 0;
    while ((match = pattern.exec(text)) !== null) {
      if (match.index > last) parent.appendChild(document.createTextNode(text.slice(last, match.index)));
      if (match[2] !== undefined) {
        const strong = document.createElement("strong");
        strong.textContent = match[2];
        parent.appendChild(strong);
      } else {
        const label = match[3];
        const href = match[4].trim();
        const lower = href.toLowerCase();
        const safe = /^https?:\/\//.test(lower) || lower.startsWith("/") || lower.startsWith("./")
          || lower.startsWith("../") || lower.startsWith("#");
        if (safe) {
          const link = document.createElement("a");
          link.href = href;
          link.target = "_blank";
          link.rel = "noopener noreferrer";
          link.textContent = label;
          parent.appendChild(link);
        } else {
          parent.appendChild(document.createTextNode(label));
        }
      }
      last = pattern.lastIndex;
    }
    if (last < text.length) parent.appendChild(document.createTextNode(text.slice(last)));
  }

  function renderMarkdown(md, container) {
    container.replaceChildren();
    const lines = md.replace(/\r\n/g, "\n").split("\n");
    let i = 0;
    let list = null;
    const closeList = () => { list = null; };
    while (i < lines.length) {
      const line = lines[i];
      if (/^\s*$/.test(line)) { closeList(); i++; continue; }
      const h = line.match(/^(#{1,4})\s+(.*)$/);
      if (h) {
        closeList();
        const heading = document.createElement(`h${h[1].length}`);
        appendInline(heading, h[2]);
        container.appendChild(heading);
        i++; continue;
      }
      if (/^\|.*\|\s*$/.test(line) && lines[i + 1] && /^\|[\s:|-]+\|\s*$/.test(lines[i + 1])) {
        closeList();
        const headCells = line.split("|").slice(1, -1).map((c) => c.trim());
        const wrapper = document.createElement("div");
        wrapper.style.overflowX = "auto";
        const table = document.createElement("table");
        const thead = document.createElement("thead");
        const headRow = document.createElement("tr");
        headCells.forEach((cell) => {
          const th = document.createElement("th");
          appendInline(th, cell);
          headRow.appendChild(th);
        });
        thead.appendChild(headRow);
        table.appendChild(thead);
        const tbody = document.createElement("tbody");
        i += 2;
        while (i < lines.length && /^\|.*\|\s*$/.test(lines[i])) {
          const cells = lines[i].split("|").slice(1, -1).map((c) => c.trim());
          const row = document.createElement("tr");
          cells.forEach((cell) => {
            const td = document.createElement("td");
            appendInline(td, cell);
            row.appendChild(td);
          });
          tbody.appendChild(row);
          i++;
        }
        table.appendChild(tbody);
        wrapper.appendChild(table);
        container.appendChild(wrapper);
        continue;
      }
      if (/^[-*]\s+/.test(line)) {
        if (!list) {
          list = document.createElement("ul");
          container.appendChild(list);
        }
        const item = document.createElement("li");
        appendInline(item, line.replace(/^[-*]\s+/, ""));
        list.appendChild(item);
        i++; continue;
      }
      if (/^>\s?/.test(line)) {
        closeList();
        const hint = document.createElement("div");
        hint.className = "hint";
        appendInline(hint, line.replace(/^>\s?/, ""));
        container.appendChild(hint);
        i++; continue;
      }
      closeList();
      const paragraph = document.createElement("p");
      appendInline(paragraph, line);
      container.appendChild(paragraph);
      i++;
    }
  }

  async function loadEnv() {
    try {
      const r = await fetch("api/env");
      const d = await r.json();
      defaultOutputDir = d.default_output_dir || "";
      $("outputDir").value = d.default_output_dir;
      $("ollamaHost").value = d.ollama_default_host || "http://localhost:11434";
      const keysDir = d.keys_dir || "~/.summit2md/keys";
      const defaultMaxChars = d.default_max_transcript_chars || 120000;
      $("maxTranscriptChars").placeholder = `默认 ${defaultMaxChars}，填 0 表示不限制`;

      const bits = [];
      bits.push(d.claude_cli_found ? "检测到本机 claude CLI" : "未检测到本机 claude CLI");
      bits.push((d.anthropic_api_key_in_env || d.anthropic_api_key_in_file) ? "已找到 Anthropic API Key（可留空输入框）" : "未找到 Anthropic API Key");
      bits.push(d.openrouter_api_key_in_file ? "已找到 OpenRouter API Key（可留空输入框）" : "未找到 OpenRouter API Key");
      $("envHint").textContent = bits.join(" · ");

      $("apiKeyHint").textContent = d.anthropic_api_key_in_env
        ? "已检测到环境变量 ANTHROPIC_API_KEY，可以留空。"
        : d.anthropic_api_key_in_file
          ? `已检测到 ${keysDir}/anthropic.key，可以留空。`
          : `留空则依次尝试：环境变量 ANTHROPIC_API_KEY → ${keysDir}/anthropic.key（把 key 存成这个文件就不用每次都填）`;
      $("openrouterKeyHint").textContent = d.openrouter_api_key_in_file
        ? `已检测到 ${keysDir}/openrouter.key，可以留空。`
        : `留空则读取 ${keysDir}/openrouter.key（把 key 存成这个文件就不用每次都填）`;
    } catch (e) { /* ignore */ }
  }

  function renderEntries() {
    const tbody = $("entriesBody");
    tbody.innerHTML = "";
    entries.forEach((e, idx) => {
      const tr = document.createElement("tr");
      if (e.is_raw_session) tr.className = "raw-session";
      tr.innerHTML = `
        <td><input type="checkbox" class="process-cb" data-idx="${idx}" ${e.is_raw_session ? "" : "checked"} /></td>
        <td>${idx + 1}</td>
        <td>${escHtml(e.title)}</td>
        <td class="conf-only">${fmtDuration(e.duration)}</td>
        <td class="conf-only"><span class="tag ${e.is_raw_session ? "raw" : "talk"}">${T(e.is_raw_session ? "完整场次录像" : "议题")}</span></td>
        <td class="conf-only"><input type="checkbox" class="summary-cb" data-idx="${idx}" checked /></td>
        <td>${safeHref(e.url) ? `<a href="${safeHref(e.url)}" target="_blank" rel="noopener">${LOCKED === "track" ? "原文" : "观看"}</a>` : ""}</td>
      `;
      tbody.appendChild(tr);
    });
    tbody.querySelectorAll("input.process-cb").forEach((cb) => cb.addEventListener("change", updateCount));
    tbody.querySelectorAll("input.summary-cb").forEach((cb) => cb.addEventListener("change", updateCallEstimate));
    updateCount();
    updateSummaryScopeUI();
  }

  function selectedCheckboxes() {
    return Array.from($("entriesBody").querySelectorAll("input.process-cb"));
  }

  function summaryCheckboxes() {
    return Array.from($("entriesBody").querySelectorAll("input.summary-cb"));
  }

  function updateSummaryScopeUI() {
    const pick = $("summaryScope").value === "pick";
    summaryCheckboxes().forEach((cb) => (cb.disabled = !pick));
    $("summaryPickAllBtn").style.display = pick ? "" : "none";
    $("summaryPickNoneBtn").style.display = pick ? "" : "none";
    updateCallEstimate();
  }

  function updateCount() {
    const boxes = selectedCheckboxes();
    const checked = boxes.filter((b) => b.checked).length;
    $("countBadge").textContent = `已选择 ${checked} / ${boxes.length} 个`;
    updateCallEstimate();
  }

  // ---- 预计模型调用次数：帮用户在点「开始生成」之前就能看出这次大概要花多少次调用 ----
  function updateCallEstimate() {
    const el = $("callEstimate");
    if (!el) return;
    const selected = selectedCheckboxes().filter((b) => b.checked);
    const processed = selected.length;
    if (!processed) { el.textContent = ""; return; }

    const doSummary = $("doSummary").checked;
    const pickSummary = $("summaryScope").value === "pick";
    // "仅勾选" 模式下，只有同时被选中处理、又在「小结」列勾选的议题才会真正生成小结——
    // 和 runBtn 点击时算 want_summary 的逻辑必须保持一致，不能直接数全表「小结」列勾了几个
    // （未被选中处理的议题即使勾了「小结」也不会有任何调用）。
    const summaryWantByIdx = new Map(summaryCheckboxes().map((cb) => [cb.dataset.idx, cb.checked]));
    const summaryCount = doSummary
      ? selected.filter((b) => (pickSummary ? summaryWantByIdx.get(b.dataset.idx) : true)).length
      : 0;
    const speakerCount = $("doSpeakerLabel").checked ? processed : 0;
    const speechPerEntry = $("doSpeechScript").checked
      ? ($("speechLangMode").value === "bilingual" ? 2 : 1)
      : 0;
    const speechCount = speechPerEntry * processed;
    // 目录已经有旧总结、且用户选了"沿用"时，这次运行不会再为大会/节目总结调用模型。
    const wantsReuseSummary = hasExistingOverallSummary && $("regenerateSummary").value !== "regenerate";
    const overallCount = doSummary && !wantsReuseSummary ? 1 : 0;
    const total = summaryCount + speakerCount + speechCount + overallCount;

    if (total === 0) {
      el.textContent = "本次设置不会调用模型（未勾选小结/发言人标注/演讲稿）。";
      return;
    }
    const parts = [];
    if (summaryCount) parts.push(T(`${summaryCount} 次逐议题小结`));
    if (speakerCount) parts.push(`最多 ${speakerCount} 次发言人标注`);
    if (speechCount) parts.push(`最多 ${speechCount} 次演讲稿整理`);
    if (overallCount) parts.push(T(`${overallCount} 次大会总结`));
    el.textContent = T(`预计最多 ${total} 次模型调用（${parts.join(" + ")}）——已经生成过的议题会自动跳过，实际调用通常更少。`);
  }

  // 每次重新获取议题列表/导入目录，都是"换了一个节目"，上一个节目残留的议程排序、
  // 已导入目录的主题总结区域这些派生状态不应该继续跟着——不管这次成不成功都先清掉，
  // 避免用户没注意到还留着上一个节目的设置，影响这次的处理结果。
  function resetPerShowState() {
    agendaOrderMap = {};
    importedShowDir = "";
    hasExistingOverallSummary = false;
    updateRegenerateSummaryVisibility();
    setupImportTopicPicker("");
  }

  // 只有当前这个目录已经有大会/节目总结、且勾了"生成小结"时，才需要问用户是沿用还是
  // 重新生成——新发现的播放列表/没生成过总结的目录不存在"沿用"这个选项，不显示。
  function updateRegenerateSummaryVisibility() {
    const show = hasExistingOverallSummary && $("doSummary").checked;
    $("regenerateSummaryField").style.display = show ? "block" : "none";
  }

  // 重新粘贴同一个链接、检查有没有新议题，是最自然的续跑操作，跟「导入目录」
  // 是两条不同的入口，但后端沿用/重新生成大会总结的判断，靠的只是"输出目录里
  // 有没有一份真实旧总结"，跟走的哪条入口无关。不在这里也探测一次的话，命中
  // 目标目录已经有旧总结时，会在用户完全没看到"沿用/重新生成"这个选择的情况下
  // 默默沿用——新发现的议题被处理了，总结却没跟着更新，看起来像是漏了。
  async function probeExistingSummary(summitTitle) {
    try {
      const r = await fetch("api/existing_summary", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ summit_title: summitTitle, output_dir: $("outputDir").value }),
      });
      const d = await r.json();
      hasExistingOverallSummary = !!d.has_overall_summary;
    } catch (e) {
      // 探测失败不该挡住正常发现流程；退回"没有旧总结"最多是少露出一次选择，
      // 比让整个「获取议题列表」失败要安全
      hasExistingOverallSummary = false;
    }
    $("regenerateSummary").value = "reuse";
    updateRegenerateSummaryVisibility();
  }

  // 三种方式（链接 / 剪贴板 / 导入目录）都会整个替换 entries 和标题、来源。先后发起两次
  // 时，晚回来的那个不能盖掉用户最后一次操作的结果——只认最新发起的那一次。
  let listSeq = 0;

  async function runDiscover(url) {
    const seq = ++listSeq;
    url = (url || "").trim();
    $("urlInput").value = url;
    $("discoverErr").textContent = "";
    if (!url) { $("discoverErr").textContent = "请输入链接"; return; }
    resetPerShowState();
    $("discoverBtn").disabled = true;
    $("discoverSpinner").style.display = "inline";
    try {
      const r = await fetch("api/discover", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ url }),
      });
      const d = await r.json();
      if (seq !== listSeq) return false;
      if (!r.ok) throw new Error(d.error || "解析失败");
      entries = d.entries;
      sourceUrl = d.source_url;
      $("summitTitle").value = d.summit_title;
      if (LOCKED) noteDetected(d.content_type);
      else $("contentType").value = d.content_type === "series" ? "series" : "summit";
      renderEntries();
      $("discoverResults").style.display = "block";
      loadSubtitleLangs();
      rememberUrl(url, d.summit_title, $("contentType").value);
      await probeExistingSummary(d.summit_title);
      return true;
    } catch (e) {
      $("discoverErr").textContent = e.message;
      return false;
    } finally {
      if (seq === listSeq) {
        $("discoverBtn").disabled = false;
        $("discoverSpinner").style.display = "none";
      }
    }
  }

  $("discoverBtn").addEventListener("click", () => runDiscover($("urlInput").value));

  // 从剪贴板粘贴的一段自由文本里批量提取链接，逐条解析成议题，拼成一份
  // "专题"——每条链接各自独立（不像 runDiscover 那样，一个链接背后是一整份
  // 共享同一个标题的播放列表/订阅源），所以没有天然的标题，交给用户自己填。
  async function runDiscoverFromText(text) {
    const seq = ++listSeq;
    text = (text || "").trim();
    $("extractLinksErr").textContent = "";
    $("extractLinksSkipped").textContent = "";
    if (!text) { $("extractLinksErr").textContent = "请粘贴包含链接的文字"; return; }
    resetPerShowState();
    $("extractLinksBtn").disabled = true;
    $("extractLinksSpinner").style.display = "inline";
    try {
      const r = await fetch("api/discover_from_text", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text }),
      });
      const d = await r.json();
      if (seq !== listSeq) return;
      if (!r.ok) throw new Error(d.error || "解析失败");
      entries = d.entries;
      sourceUrl = `剪贴板批量导入（${d.entries.length} 条链接）`;
      $("urlInput").value = "";
      const today = new Date().toISOString().slice(0, 10);
      $("summitTitle").value = `专题合集 ${today}`;
      if (LOCKED) noteDetected(d.content_type);
      else $("contentType").value = "series";
      renderEntries();
      $("discoverResults").style.display = "block";
      loadSubtitleLangs();
      await probeExistingSummary($("summitTitle").value);
      if (d.skipped && d.skipped.length) {
        const lines = d.skipped.map((s) => `⏭️ ${s.url} — ${s.reason}`);
        $("extractLinksSkipped").textContent = `跳过了 ${d.skipped.length} 条：\n${lines.join("\n")}`;
      }
    } catch (e) {
      $("extractLinksErr").textContent = e.message;
    } finally {
      $("extractLinksBtn").disabled = false;
      $("extractLinksSpinner").style.display = "none";
    }
  }

  $("extractLinksBtn").addEventListener("click", () => runDiscoverFromText($("clipboardText").value));

  // 手动清空整个「获取议题列表」/「选择议题」面板，回到刚打开页面时的状态——用于切换到
  // 完全不同的节目前先确认没有任何残留设置（链接、导入路径、议程排序、主题总结区域等）。
  function resetDiscoverState() {
    entries = [];
    sourceUrl = "";
    resetPerShowState();
    $("urlInput").value = "";
    $("discoverErr").textContent = "";
    $("importDirInput").value = "";
    $("importDirErr").textContent = "";
    $("agendaUrl").value = "";
    $("agendaOrderHint").textContent = "默认按 YouTube 播放列表原始顺序排列，通常和实际议程顺序不一致；填这个链接可以尝试按会议官网的议程顺序重排，文件名编号也会跟着改用议程顺序（未匹配到的议题排在最后）。";
    $("summitTitle").value = "";
    $("contentType").value = (LOCKED && CONTENT_TYPE_FOR_MODE[LOCKED]) || "summit";
    if (LOCKED) { $("contentTypeField").style.display = "none"; $("contentTypeHint").textContent = ""; }
    $("outputDir").value = defaultOutputDir;
    $("entriesBody").innerHTML = "";
    $("discoverResults").style.display = "none";
    $("runHint").textContent = "";
  }
  $("resetBtn").addEventListener("click", resetDiscoverState);

  // 导入一个此前已经生成过的本地输出目录：不重新解析播放列表/不重新下载，直接把已有议题
  // 带进下面「选择议题」，复用同一套勾选/处理流程——用来重新生成总结、补齐播出日期等。
  async function runImportDir(path) {
    const seq = ++listSeq;
    path = (path || "").trim();
    $("importDirErr").textContent = "";
    if (!path) { $("importDirErr").textContent = "请输入目录路径"; return; }
    resetPerShowState();
    $("importDirBtn").disabled = true;
    try {
      const r = await fetch("api/import_dir", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path }),
      });
      const d = await r.json();
      if (seq !== listSeq) return;
      if (!r.ok) throw new Error(d.error || "导入失败");
      entries = d.entries;
      sourceUrl = d.source_url || "";
      $("summitTitle").value = d.summit_title;
      if (LOCKED) noteDetected(d.content_type);
      else $("contentType").value = d.content_type === "series" ? "series" : "summit";
      if (d.output_dir) $("outputDir").value = d.output_dir;
      // 在 renderEntries()（会顺带算一次预计调用次数）之前先更新好这两个状态，
      // 不然那次估算会用旧值算出"这次还要重新生成总结"，虚高一次调用。
      importedShowDir = d.imported_dir || path;
      hasExistingOverallSummary = !!d.has_overall_summary;
      $("regenerateSummary").value = "reuse";
      updateRegenerateSummaryVisibility();
      renderEntries();
      $("discoverResults").style.display = "block";
      loadSubtitleLangs();
      setupImportTopicPicker(importedShowDir);
      importDirAutocomplete.rememberDir(path);
    } catch (e) {
      $("importDirErr").textContent = e.message;
    } finally {
      $("importDirBtn").disabled = false;
    }
  }

  $("importDirBtn").addEventListener("click", () => runImportDir($("importDirInput").value));
  const importDirAutocomplete = attachDirAutocomplete($("importDirInput"), "summit2md.recentImportDirs");
  const outputDirAutocomplete = attachDirAutocomplete($("outputDir"), "summit2md.recentOutputDirs");
  $("outputDir").addEventListener("change", () => outputDirAutocomplete.rememberDirIfPlausible($("outputDir").value));

  async function loadSubtitleLangs() {
    const probe = entries.find((e) => !e.is_raw_session) || entries[0];
    if (!probe) return;
    const sel = $("langPrefs");
    const hint = $("langPrefsHint");
    if (["substack", "rss", "wechat", "article"].includes(probe.source_type)) {
      // 这几种来源直接抓正文/官方转写，不走 YouTube 字幕下载，这个选项不生效。
      $("langPrefsField").style.display = "none";
      return;
    }
    $("langPrefsField").style.display = "block";
    hint.textContent = "正在从 YouTube 获取该视频实际可用的字幕语言……";
    try {
      const r = await fetch("api/subtitle_langs", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ url: probe.url }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || "获取字幕语言失败");
      if (!d.languages || d.languages.length === 0) {
        hint.textContent = "未探测到该视频的可用字幕语言，保留默认 en，实际以处理时的下载结果为准。";
        return;
      }
      sel.innerHTML = "";
      d.languages.forEach((lang) => {
        const opt = document.createElement("option");
        opt.value = lang.code;
        opt.textContent = lang.name === lang.code ? lang.code : `${lang.name}（${lang.code}）`;
        sel.appendChild(opt);
      });
      // 列表现在已经在后端过滤掉了 YouTube 自动翻译出来的那一大堆目标语言
      // （只剩"原始语言轨道" + 人工字幕，见 pipeline.fetch_subtitle_languages），
      // 所以这里的 code 可能是 "en"，也可能是带 "-orig" 后缀的 "en-orig"——
      // 两种都要认。YouTube 报的 original_language 时不时不准（实测遇到过明明是
      // 英语访谈却标成孟加拉语），比不上"有没有英语轨道"这个信号直接可靠：这个
      // 工具的输入内容绝大多数是英语，优先选英语，找不到英语才退回它报的原始语言，
      // 再退回列表第一项。
      const findLang = (code) => code && d.languages.find((l) => l.code === code || l.code === `${code}-orig`);
      const preferred = findLang("en") || findLang(d.original_language) || d.languages[0];
      sel.value = preferred.code;
      hint.textContent = T(`以「${probe.title}」探测到 ${d.languages.length} 种可用字幕语言（已过滤掉自动翻译产生的语言，只保留原始语言和官方字幕；同一播放列表内其他议题可能略有差异）。`);
    } catch (e) {
      hint.textContent = "获取字幕语言失败（" + e.message + "），保留默认 en。";
    }
  }

  $("agendaOrderBtn").addEventListener("click", async () => {
    const url = $("agendaUrl").value.trim();
    const hint = $("agendaOrderHint");
    if (!url) { hint.textContent = "请输入议程页面链接"; return; }
    if (entries.length === 0) { hint.textContent = T("请先获取议题列表"); return; }
    $("agendaOrderBtn").disabled = true;
    hint.textContent = "正在抓取并解析议程页面……";
    try {
      // 重新排序前先记住当前勾选状态（按视频 id），排序后原样恢复，不打乱用户已经做的选择
      const checkedIds = new Set(
        selectedCheckboxes().filter((b) => b.checked).map((b) => entries[b.dataset.idx].id)
      );
      const summaryCheckedIds = new Set(
        summaryCheckboxes().filter((b) => b.checked).map((b) => entries[b.dataset.idx].id)
      );
      const r = await fetch("api/agenda_order", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ agenda_url: url }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || "获取议程失败");
      const matched = d.matched || {};
      let matchedCount = 0;
      agendaOrderMap = {};
      entries.forEach((e) => {
        const m = matched[e.id];
        if (m) { e.agenda_order = m.order; e.agenda_meta = m; matchedCount++; agendaOrderMap[e.id] = m.order; }
        else { e.agenda_order = null; e.agenda_meta = null; }
      });
      entries.sort((a, b) => {
        if (a.agenda_order == null && b.agenda_order == null) return 0;
        if (a.agenda_order == null) return 1;
        if (b.agenda_order == null) return -1;
        return a.agenda_order - b.agenda_order;
      });
      entries.forEach((e, idx) => { e.rank = idx + 1; });
      renderEntries();
      selectedCheckboxes().forEach((b) => { b.checked = checkedIds.has(entries[b.dataset.idx].id); });
      summaryCheckboxes().forEach((b) => { b.checked = summaryCheckedIds.has(entries[b.dataset.idx].id); });
      updateCount();
      hint.textContent = `已按议程顺序重新排列：${matchedCount}/${entries.length} 个议题匹配成功，未匹配到的排在最后（按原顺序）。`;
    } catch (e) {
      hint.textContent = "按议程排序失败：" + e.message;
    } finally {
      $("agendaOrderBtn").disabled = false;
    }
  });

  $("selectAllBtn").addEventListener("click", () => { selectedCheckboxes().forEach((b) => (b.checked = true)); updateCount(); });
  $("selectNoneBtn").addEventListener("click", () => { selectedCheckboxes().forEach((b) => (b.checked = false)); updateCount(); });
  $("selectTalksBtn").addEventListener("click", () => {
    selectedCheckboxes().forEach((b) => (b.checked = !entries[b.dataset.idx].is_raw_session));
    updateCount();
  });

  $("selectFirstNBtn").addEventListener("click", () => {
    const n = parseInt($("selectFirstN").value, 10) || 0;
    let kept = 0;
    selectedCheckboxes().forEach((b) => {
      const isTalk = !entries[b.dataset.idx].is_raw_session;
      b.checked = isTalk && kept < n ? (kept++, true) : false;
    });
    updateCount();
  });

  function updateBackendVisibility() {
    const needsLLM = $("doSummary").checked || $("doSpeakerLabel").checked || $("doSpeechScript").checked;
    const backend = $("backendSelect").value;
    const isApi = backend === "api";
    const isOpenRouter = backend === "openrouter";
    const isThirdParty = backend === "openai_compatible";
    const isOllama = backend === "ollama";
    $("backendField").style.display = needsLLM ? "block" : "none";
    $("apiKeyRow").style.display = needsLLM && isApi ? "flex" : "none";
    $("openrouterRow").style.display = needsLLM && isOpenRouter ? "block" : "none";
    $("thirdPartyApiRow").style.display = needsLLM && isThirdParty ? "block" : "none";
    $("ollamaRow").style.display = needsLLM && isOllama ? "block" : "none";
    $("cliHint").style.display = needsLLM && backend === "cli" ? "block" : "none";
    $("speechLangModeField").style.display = $("doSpeechScript").checked ? "block" : "none";
    $("summaryLengthField").style.display = $("doSummary").checked ? "block" : "none";
    updateRegenerateSummaryVisibility();
    if (needsLLM && isOllama) refreshOllamaModels();
    updateOverallModelOptions();
    updateCallEstimate();
  }

  // "大会总结用的模型"是个自由文本输入（格式要跟着当前选的后端走，比如 Anthropic 和
  // OpenRouter 的模型 id 写法不一样），常用值给个 datalist 下拉建议，减少手动输入，
  // 仍然允许手打别的值——不是每个后端/每个用户常用的模型都能预先枚举全。
  const OVERALL_MODEL_PRESETS = {
    api: ["claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5-20251001"],
    openrouter: ["~anthropic/claude-opus-latest", "~openai/gpt-astra-latest", "~deepseek/deepseek-pro-latest"],
    openai_compatible: ["gpt-4.1-mini", "deepseek-reasoner"],
    ollama: [],
    cli: [],
  };
  function updateOverallModelOptions() {
    const list = $("overallModelList");
    if (!list) return;
    const backend = $("backendSelect").value;
    list.innerHTML = "";
    (OVERALL_MODEL_PRESETS[backend] || []).forEach((name) => {
      const opt = document.createElement("option");
      opt.value = name;
      list.appendChild(opt);
    });
  }

  let ollamaModelsLoaded = false;
  async function refreshOllamaModels() {
    if (ollamaModelsLoaded) return;
    ollamaModelsLoaded = true;
    try {
      const api_base = $("ollamaHost").value.trim();
      const r = await fetch("api/ollama_models", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ api_base }),
      });
      const d = await r.json();
      const list = $("ollamaModelList");
      list.innerHTML = "";
      (d.models || []).forEach((name) => {
        const opt = document.createElement("option");
        opt.value = name;
        list.appendChild(opt);
      });
      if (!d.models || !d.models.length) {
        $("ollamaHint").textContent = "没检测到本地已拉取的 Ollama 模型（或 Ollama 未启动）——可以直接在模型名里手动填写，工具会尝试直接调用。";
      }
    } catch (e) {
      ollamaModelsLoaded = false; // 允许下次重试
    }
  }

  $("backendSelect").addEventListener("change", updateBackendVisibility);
  $("ollamaHost").addEventListener("change", () => { ollamaModelsLoaded = false; refreshOllamaModels(); });
  $("doSummary").addEventListener("change", updateBackendVisibility);
  $("doSpeakerLabel").addEventListener("change", updateBackendVisibility);
  $("doSpeechScript").addEventListener("change", updateBackendVisibility);
  $("summaryScope").addEventListener("change", updateSummaryScopeUI);
  $("regenerateSummary").addEventListener("change", updateCallEstimate);
  $("summaryPickAllBtn").addEventListener("click", () => { summaryCheckboxes().forEach((cb) => (cb.checked = true)); updateCallEstimate(); });
  $("summaryPickNoneBtn").addEventListener("click", () => { summaryCheckboxes().forEach((cb) => (cb.checked = false)); updateCallEstimate(); });
  $("speechLangMode").addEventListener("change", updateCallEstimate);

  // 当前「AI 后端与成本」面板里选的后端 + 对应的 key/base/model——运行任务、导入目录后
  // 直接生成主题总结，这两处都要读同一套当前配置，抽出来避免两边各写一份、改一处忘改另一处。
  function currentBackendConfig() {
    const backend = $("backendSelect").value;
    let api_key = $("apiKey").value.trim();
    let api_base = "";
    let model = $("model").value;
    if (backend === "openrouter") {
      api_key = $("openrouterApiKey").value.trim();
      api_base = $("openrouterApiBase").value.trim();
      model = $("openrouterModel").value.trim();
    } else if (backend === "openai_compatible") {
      api_key = $("thirdPartyApiKey").value.trim();
      api_base = $("thirdPartyApiBase").value.trim();
      model = $("thirdPartyModel").value.trim();
    } else if (backend === "ollama") {
      api_key = "";
      api_base = $("ollamaHost").value.trim();
      model = $("ollamaModel").value.trim();
    }
    return { backend, api_key, api_base, model };
  }

  function buildRunPayload(selectedEntries) {
    const cfg = currentBackendConfig();
    return {
      summit_title: $("summitTitle").value.trim() || "Untitled Summit",
      source_url: sourceUrl,
      entries: selectedEntries,
      output_dir: $("outputDir").value.trim(),
      lang_prefs: $("langPrefs").value.trim() || "en",
      do_summary: $("doSummary").checked,
      // 目录没有旧总结时这个值无所谓（后端总归会生成第一份）；有旧总结时才真正生效。
      regenerate_summary: $("regenerateSummary").value === "regenerate",
      summary_length: $("summaryLength").value,
      do_speaker_label: $("doSpeakerLabel").checked,
      do_speech_script: $("doSpeechScript").checked,
      speech_lang_mode: $("speechLangMode").value,
      skip_existing: $("skipExisting").checked,
      agenda_order_map: agendaOrderMap,
      content_type: $("contentType").value,
      backend: cfg.backend,
      api_key: cfg.api_key,
      api_base: cfg.api_base,
      model: cfg.model,
      overall_model: $("overallModel").value.trim(),
      max_transcript_chars: parseMaxTranscriptChars(),
    };
  }

  // 留空 = 交给后端用默认值（120000）；显式填 0 = 不限制。区分"没填"和"填了0"很重要，
  // 不能简单用 `|| 0` 兜底，否则留空会被误当成"不限制"。
  function parseMaxTranscriptChars() {
    const raw = $("maxTranscriptChars").value.trim();
    if (raw === "") return undefined;
    const n = parseInt(raw, 10);
    return Number.isNaN(n) ? undefined : Math.max(0, n);
  }

  // ---- 任务卡片：每个任务一张卡片，各自轮询/暂停/停止，互不影响，可以同时跑多个 ----
  function createTaskCard(title) {
    const frag = $("taskCardTemplate").content.cloneNode(true);
    localize(frag);
    const el = frag.querySelector(".task-card");
    qs(el, "title").textContent = title;
    // 模板里单选组的 name 是写死的；多张任务卡片同时挂在页面上时得各自独立，
    // 不然点一张卡片的"合并/分别"单选会连带切换另一张卡片里对应的选项。
    // 随机后缀必须在循环外生成一次、两个 radio 共用——写在 forEach 回调里的话，
    // "合并"和"分别"会各自拿到不同的随机名，等于拆成了两个各自独立的单选组：
    // 互斥失效（勾一个不会取消另一个），而且单独一个 radio 一旦选中，原生行为下
    // 再点它自己是不会取消勾选的，看起来就是"一旦选中就无法取消"。
    const groupSuffix = Math.random().toString(36).slice(2);
    el.querySelectorAll('input[type="radio"][name="topicSummaryMode"]').forEach((r) => {
      r.name = `topicSummaryMode-${groupSuffix}`;
    });
    $("tasksList").prepend(el);
    $("tasksSection").style.display = "block";
    return el;
  }

  // opts.restored=true 表示这张卡片是刷新页面后从服务端任务列表恢复的，payload 只是个
  // 兜底占位（没有原始 API Key/模型/输出目录），仅用于让"重试失败项"/"生成主题总结"在默认
  // 后端下也能工作；进度/日志/暂停/停止/结果这些不依赖 payload，恢复后照常可用。
  function attachTask(jobId, title, payload, opts) {
    const restored = !!(opts && opts.restored);
    const el = createTaskCard(title);
    const task = { el, payload, failedEntries: [], progressSamples: [], pollTimer: null, restored };
    tasks.set(jobId, task);
    if (restored) qs(el, "restoredHint").style.display = "block";

    qs(el, "pauseBtn").addEventListener("click", async () => {
      await fetch(`api/pause/${jobId}`, { method: "POST" });
      qs(el, "pauseBtn").style.display = "none";
      qs(el, "resumeBtn").style.display = "";
    });
    qs(el, "resumeBtn").addEventListener("click", async () => {
      await fetch(`api/resume/${jobId}`, { method: "POST" });
      qs(el, "pauseBtn").style.display = "";
      qs(el, "resumeBtn").style.display = "none";
    });
    qs(el, "stopBtn").addEventListener("click", async () => {
      await fetch(`api/stop/${jobId}`, { method: "POST" });
    });
    // 只从界面上移除这张卡片，不碰已经生成的文件；服务端那份任务记录也一并清掉，
    // 不然刷新页面又会被 restoreTasks() 重新捞回来。按钮只在任务完成后才会出现。
    qs(el, "dismissBtn").addEventListener("click", async () => {
      try {
        await fetch(`api/jobs/${jobId}`, { method: "DELETE" });
      } catch (e) { /* 服务端删不掉也不影响界面上移除 */ }
      clearInterval(task.pollTimer);
      tasks.delete(jobId);
      el.remove();
      if (tasks.size === 0) $("tasksSection").style.display = "none";
    });
    qs(el, "openFolderBtn").addEventListener("click", async () => {
      await fetch(`api/open_folder/${jobId}`, { method: "POST" });
    });
    qs(el, "retryFailedBtn").addEventListener("click", async () => {
      if (task.failedEntries.length === 0) return;
      const btn = qs(el, "retryFailedBtn");
      btn.disabled = true;
      try {
        await launchTask({ ...task.payload, entries: task.failedEntries });
      } catch (e) {
        $("runHint").textContent = task.restored ? `${e.message}${RESTORED_ERROR_SUFFIX}` : e.message;
      } finally {
        btn.disabled = false;
      }
    });
    // 生成一次之后，这个标签/主题就"已经有总结了"——生成完得再探测一次，不然
    // "沿用/重新生成"这个选择要等用户再碰一下勾选框/标题框才会冒出来，生成完
    // 明明已经有文件了，选择却一直不出现，跟没做这个功能没区别。
    const refreshTopicReuse = () => refreshTopicReuseField({
      outputDir: task.outputDir,
      label: Array.from(qs(el, "topicCheckboxes").querySelectorAll("input:checked")).map((cb) => cb.value).join("、"),
      fieldEl: qs(el, "topicReuseField"), selectEl: qs(el, "topicReuseMode"),
    });
    const refreshEntryTopicReuse = () => refreshTopicReuseField({
      outputDir: task.outputDir, label: qs(el, "entryTopicLabel").value,
      entryIds: Array.from(qs(el, "entryTopicCheckboxes").querySelectorAll("input:checked")).map((cb) => cb.value),
      fieldEl: qs(el, "entryTopicReuseField"), selectEl: qs(el, "entryTopicReuseMode"),
    });
    qs(el, "topicSummaryBtn").addEventListener("click", async () => {
      const themes = Array.from(qs(el, "topicCheckboxes").querySelectorAll("input:checked")).map((cb) => cb.value);
      const modeInput = qs(el, "topicSection").querySelector('input[type="radio"]:checked');
      await runTopicSummary({
        outputDir: task.outputDir,
        summitTitle: task.payload.summit_title,
        contentType: task.payload.content_type,
        backend: task.payload.backend,
        apiKey: task.payload.api_key,
        apiBase: task.payload.api_base,
        // 主题总结和大会总结一样是跨议题综合，优先用「大会总结用模型」（更强），没填就退回主模型。
        model: task.payload.overall_model || task.payload.model,
        themes,
        mode: modeInput ? modeInput.value : "combined",
        reuse: qs(el, "topicReuseMode").value === "reuse",
        btn: qs(el, "topicSummaryBtn"),
        stopBtn: qs(el, "topicSummaryStopBtn"),
        hint: qs(el, "topicSummaryHint"),
        resultEl: qs(el, "topicSummaryResult"),
        errorSuffix: task.restored ? RESTORED_ERROR_SUFFIX : "",
      });
      refreshTopicReuse();
    });
    qs(el, "topicCheckboxes").addEventListener("change", refreshTopicReuse);
    qs(el, "entryTopicSummaryBtn").addEventListener("click", async () => {
      const entryIds = Array.from(qs(el, "entryTopicCheckboxes").querySelectorAll("input:checked")).map((cb) => cb.value);
      await runCustomTopicSummary({
        outputDir: task.outputDir,
        summitTitle: task.payload.summit_title,
        contentType: task.payload.content_type,
        backend: task.payload.backend,
        apiKey: task.payload.api_key,
        apiBase: task.payload.api_base,
        model: task.payload.overall_model || task.payload.model,
        entryIds,
        label: qs(el, "entryTopicLabel").value,
        reuse: qs(el, "entryTopicReuseMode").value === "reuse",
        btn: qs(el, "entryTopicSummaryBtn"),
        stopBtn: qs(el, "entryTopicSummaryStopBtn"),
        hint: qs(el, "entryTopicSummaryHint"),
        resultEl: qs(el, "entryTopicSummaryResult"),
        errorSuffix: task.restored ? RESTORED_ERROR_SUFFIX : "",
      });
      refreshEntryTopicReuse();
    });
    qs(el, "entryTopicLabel").addEventListener("input", refreshEntryTopicReuse);
    qs(el, "entryTopicCheckboxes").addEventListener("change", refreshEntryTopicReuse);
    // 纯本地改名 + 修正 manifest/链接/README，不需要 API Key/模型，刷新后恢复的卡片也能正常用。
    qs(el, "renameByDateBtn").addEventListener("click", async () => {
      const btn = qs(el, "renameByDateBtn");
      const hint = qs(el, "renameByDateHint");
      btn.disabled = true;
      hint.textContent = "正在处理……";
      try {
        const r = await fetch("api/rename_by_date", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ output_dir: task.outputDir, content_type: task.payload.content_type }),
        });
        const d = await r.json();
        if (!r.ok) throw new Error(d.error || "重命名失败");
        if (d.skipped_not_series) {
          hint.textContent = "这不是播客/访谈类节目，不需要按日期重命名。";
        } else if (d.renamed === 0 && d.dates_backfilled === 0) {
          hint.textContent = `文件名已经是播出日期格式了（共 ${d.already_dated} 个），无需改动。`;
        } else {
          hint.textContent = `已重命名 ${d.renamed} 个文件`
            + (d.dates_backfilled ? `，补了 ${d.dates_backfilled} 条缺失的播出日期` : "")
            + (d.skipped_no_date ? T(`，${d.skipped_no_date} 个议题拿不到播出日期，文件名未变`) : "")
            + "。";
        }
      } catch (e) {
        hint.textContent = e.message;
      } finally {
        btn.disabled = false;
      }
    });

    startPolling(jobId);
    return task;
  }

  const RESTORED_ERROR_SUFFIX = "（这张卡片是刷新页面后恢复的，用的是默认 AI 后端，如果原来用的是其它后端/自定义了输出目录，请重新在上方配置后手动处理）";

  async function launchTask(payload) {
    const r = await fetch("api/run", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || "启动失败");
    attachTask(d.job_id, payload.summit_title, payload);
    return d.job_id;
  }

  // 浏览器刷新后 tasks 这个内存里的 Map 会清空，但服务端的任务（JOBS）还在跑——
  // 页面加载时把服务端还记得的任务（运行中 + 最近完成的）重新接上卡片和轮询，
  // 不然用户刷新一下正在跑的任务就"消失"了（其实还在后台跑，只是界面看不到）。
  async function restoreTasks() {
    try {
      const r = await fetch("api/jobs");
      const d = await r.json();
      const jobs = (d.jobs || []).slice().reverse(); // 服务端按最新排在前；这里反过来正序 prepend，恢复后顺序不变
      jobs.forEach((j) => {
        const payload = {
          summit_title: j.summit_title,
          content_type: j.content_type || "summit",
          backend: "api", api_key: "", api_base: "", model: "", overall_model: "",
        };
        attachTask(j.job_id, j.summit_title || "（恢复的任务）", payload, { restored: true });
      });
    } catch (e) { /* 拿不到任务列表就不恢复，不影响正常使用 */ }
  }

  // 探测这个标签是不是已经生成过主题总结文件，据此显示/隐藏"沿用/重新生成"这个
  // 选择——跟大会总结那个"沿用已有的总结"选项是同一个道理，不然默认重新点一下
  // 生成按钮就会白白再调用一次模型。留空标题走自动概括那条路没法提前探测（文件名
  // 要等模型生成完才知道），直接隐藏这个选择，照常生成。
  // entryIds 是给"手选议题"那条路用的：标题常年留空（走 AI 自动概括），单靠
  // label 探测不到任何东西——带上 entry_ids，后端会去查"这批议题上次概括出的
  // 标题是什么"，查得到才探测得到文件。主题分组那条路 label 本来就是确定的
  // （主题名拼起来），不用传 entryIds 也一样能探测。
  async function refreshTopicReuseField({ outputDir, label, entryIds, fieldEl, selectEl }) {
    label = (label || "").trim();
    if (!outputDir || (!label && !(entryIds && entryIds.length))) {
      fieldEl.style.display = "none";
      return;
    }
    try {
      const r = await fetch("api/topic_summary_exists", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ output_dir: outputDir, label, entry_ids: entryIds || [] }),
      });
      const d = await r.json();
      selectEl.value = "reuse";
      fieldEl.style.display = d.exists ? "block" : "none";
    } catch (e) { fieldEl.style.display = "none"; }
  }

  function renderTopicCheckboxes(container, groups) {
    container.innerHTML = "";
    groups.forEach((g) => {
      const label = document.createElement("label");
      label.className = "check-row";
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.value = g.name;
      label.appendChild(cb);
      label.appendChild(document.createTextNode(`${g.name}（${g.count} 个）`));
      container.appendChild(label);
    });
  }

  // 生成主题总结的核心逻辑，任务卡片内和「导入目录」面板两处共用。勾选了不止一个主题、
  // 且选了"分别单独出一份"时，依次对每个主题单独调一次 /api/topic_summary——互不影响，
  // 某一个主题生成失败不会连累其它几个；否则维持原来的行为：一次调用把所有勾选主题合并
  // 生成一份综合报告（跨主题重复的议题只算一次）。
  // 主题总结/手选议题聚焦总结现在都是"提交任务、轮询状态"的异步写法（配上面
  // server.py 新加的 /api/simple_job_status、/api/simple_job_stop）——这类操作
  // 只有一次模型调用，中途没有自然的暂停点（暂停了再恢复跟重新发一次没区别），
  // 所以只给"停止"，不假装能暂停。
  class StoppedByUser extends Error {}

  async function runSimpleJob(url, body, { stopBtn } = {}) {
    const r = await fetch(url, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || "启动失败");
    const jobId = d.job_id;

    const onStopClick = () => {
      stopBtn.disabled = true;
      fetch(`api/simple_job_stop/${jobId}`, { method: "POST" });
    };
    if (stopBtn) {
      stopBtn.style.display = "inline-block";
      stopBtn.disabled = false;
      stopBtn.addEventListener("click", onStopClick);
    }
    try {
      while (true) {
        await new Promise((resolve) => setTimeout(resolve, 800));
        const sr = await fetch(`api/simple_job_status/${jobId}`);
        const status = await sr.json();
        if (!sr.ok) throw new Error(status.error || "查询任务状态失败");
        if (!status.done) continue;
        if (status.stopped) throw new StoppedByUser("已停止");
        if (status.error) throw new Error(status.error);
        return status.result;
      }
    } finally {
      if (stopBtn) {
        stopBtn.style.display = "none";
        stopBtn.removeEventListener("click", onStopClick);
      }
    }
  }

  async function runTopicSummary({
    outputDir, summitTitle, contentType, backend, apiKey, apiBase, model,
    themes, mode, reuse, btn, stopBtn, hint, resultEl, errorSuffix,
  }) {
    if (!outputDir) { hint.textContent = "缺少输出目录"; return; }
    if (themes.length === 0) { hint.textContent = "请至少勾选一个主题"; return; }
    const suffix = errorSuffix || "";
    btn.disabled = true;
    resultEl.replaceChildren();
    const callOne = (themeSubset) => runSimpleJob("api/topic_summary", {
      output_dir: outputDir, summit_title: summitTitle, content_type: contentType,
      themes: themeSubset, backend, api_key: apiKey, api_base: apiBase, model, reuse,
    }, { stopBtn });
    try {
      if (mode === "separate" && themes.length > 1) {
        const list = document.createElement("ul");
        let successCount = 0;
        let hadError = false;
        let stoppedEarly = false;
        for (let i = 0; i < themes.length; i++) {
          const theme = themes[i];
          hint.textContent = `正在生成 ${i + 1}/${themes.length}：${theme}……`;
          const li = document.createElement("li");
          try {
            const d = await callOne([theme]);
            li.textContent = `${theme} → ${d.relative_path}（${d.count} 个）`;
            successCount++;
          } catch (e) {
            if (e instanceof StoppedByUser) {
              li.textContent = `${theme}：已停止（这份还没生成）`;
              list.appendChild(li);
              stoppedEarly = true;
              break;
            }
            li.textContent = `${theme}：生成失败（${e.message}）`;
            li.style.color = "var(--err)";
            hadError = true;
          }
          list.appendChild(li);
        }
        resultEl.appendChild(list);
        hint.textContent = stoppedEarly
          ? `已停止，完成了 ${successCount}/${themes.length} 份主题报告`
          : `已完成 ${successCount}/${themes.length} 份主题报告` + (hadError ? suffix : "");
      } else {
        hint.textContent = "正在生成……";
        const d = await callOne(themes);
        hint.textContent = T(`已保存到 ${d.relative_path}（涵盖 ${d.count} 个议题）`);
        renderMarkdown(d.content, resultEl);
      }
    } catch (e) {
      hint.textContent = e instanceof StoppedByUser ? "已停止" : e.message + suffix;
    } finally {
      btn.disabled = false;
    }
  }

  function renderEntryTopicCheckboxes(container, entries) {
    container.innerHTML = "";
    entries.forEach((e) => {
      const label = document.createElement("label");
      label.className = "check-row" + (e.ok ? "" : " failed");
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.value = e.id;
      if (!e.ok) cb.disabled = true;
      label.appendChild(cb);
      label.appendChild(document.createTextNode(e.ok ? e.title : `${e.title}（处理失败，不能选）`));
      container.appendChild(label);
    });
  }

  // 手选议题生成聚焦总结的核心逻辑，跟 runTopicSummary() 是同一层级的另一个版本：
  // 那边按主题名字选，这边直接按用户勾出来的 entry_ids 选，不需要"合并/分别"的选择——
  // 手选场景天然就是"这几个凑成一份"，想要另一份重新勾一次就是了，不必为此在界面上
  // 多加一层選擇。
  async function runCustomTopicSummary({
    outputDir, summitTitle, contentType, backend, apiKey, apiBase, model,
    entryIds, label, reuse, btn, stopBtn, hint, resultEl, errorSuffix,
  }) {
    if (!outputDir) { hint.textContent = "缺少输出目录"; return; }
    if (entryIds.length === 0) { hint.textContent = "请至少勾选一个议题"; return; }
    btn.disabled = true;
    resultEl.replaceChildren();
    hint.textContent = "正在生成……";
    try {
      const d = await runSimpleJob("api/custom_topic_summary", {
        output_dir: outputDir, summit_title: summitTitle, content_type: contentType,
        entry_ids: entryIds, label, backend, api_key: apiKey, api_base: apiBase, model, reuse,
      }, { stopBtn });
      hint.textContent = T(`已保存到 ${d.relative_path}（涵盖 ${d.count} 个议题）`);
      renderMarkdown(d.content, resultEl);
    } catch (e) {
      hint.textContent = e instanceof StoppedByUser ? "已停止" : e.message + (errorSuffix || "");
    } finally {
      btn.disabled = false;
    }
  }

  // 跟 setupTopicPicker 平行的另一路：列出这个目录里的全部议题（不管有没有自动分出
  // 主题、也不管总结解析顺不顺利），只要有至少一个处理成功的议题就显示这一块。
  async function setupEntryPicker(el, task) {
    if (!task.outputDir) return;
    try {
      const r = await fetch("api/topic_entries", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ output_dir: task.outputDir }),
      });
      const d = await r.json();
      const entries = d.entries || [];
      if (!entries.some((e) => e.ok)) return;
      renderEntryTopicCheckboxes(qs(el, "entryTopicCheckboxes"), entries);
      qs(el, "entryTopicSection").style.display = "block";
    } catch (e) { /* 拿不到议题列表就不显示，不影响主流程 */ }
  }

  // 任务完成后拉一次这个输出目录已知的主题分组，渲染成勾选列表；没有分组信息
  // （比如没开小结、或者模型这次没按格式输出主题索引）就不显示这一块，不强求。
  async function setupTopicPicker(el, task) {
    if (!task.outputDir) return;
    try {
      const r = await fetch("api/topic_groups", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ output_dir: task.outputDir }),
      });
      const d = await r.json();
      const groups = d.groups || [];
      if (!groups.length) return;
      renderTopicCheckboxes(qs(el, "topicCheckboxes"), groups);
      qs(el, "topicSection").style.display = "block";
    } catch (e) { /* 拿不到主题分组就不显示，不影响主流程 */ }
  }

  // 导入一个此前已经生成过的目录之后，不需要重新跑一次任务，直接读它已有的主题分组
  // （manifest 里存过的，或者现读它的 README.md 解析出来的），让「生成主题总结」立刻可用。
  async function setupImportTopicPicker(dir) {
    const section = $("importTopicSection");
    section.style.display = "none";
    $("importTopicCheckboxes").innerHTML = "";
    $("importTopicSummaryHint").textContent = "";
    $("importTopicSummaryResult").replaceChildren();
    $("importTopicReuseField").style.display = "none";
    const entrySection = $("importEntryTopicSection");
    entrySection.style.display = "none";
    $("importEntryTopicCheckboxes").innerHTML = "";
    $("importEntryTopicLabel").value = "";
    $("importEntryTopicSummaryHint").textContent = "";
    $("importEntryTopicSummaryResult").replaceChildren();
    $("importEntryTopicReuseField").style.display = "none";
    if (!dir) return;
    try {
      const r = await fetch("api/topic_groups", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ output_dir: dir }),
      });
      const d = await r.json();
      const groups = d.groups || [];
      if (groups.length) {
        renderTopicCheckboxes($("importTopicCheckboxes"), groups);
        section.style.display = "block";
      }
    } catch (e) { /* 拿不到主题分组就不显示，不影响主流程 */ }
    // 跟上面的主题分组是两条独立的路，一条拿不到不影响另一条能不能显示
    try {
      const r2 = await fetch("api/topic_entries", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ output_dir: dir }),
      });
      const d2 = await r2.json();
      const entries = d2.entries || [];
      if (entries.some((e) => e.ok)) {
        renderEntryTopicCheckboxes($("importEntryTopicCheckboxes"), entries);
        entrySection.style.display = "block";
      }
    } catch (e) { /* 拿不到议题列表就不显示，不影响主流程 */ }
  }

  // 生成一次之后，这个标签/主题就"已经有总结了"——生成完得再探测一次，不然
  // "沿用/重新生成"这个选择要等用户再碰一下勾选框/标题框才会冒出来。
  const refreshImportTopicReuse = () => refreshTopicReuseField({
    outputDir: importedShowDir,
    label: Array.from($("importTopicCheckboxes").querySelectorAll("input:checked")).map((cb) => cb.value).join("、"),
    fieldEl: $("importTopicReuseField"), selectEl: $("importTopicReuseMode"),
  });
  const refreshImportEntryTopicReuse = () => refreshTopicReuseField({
    outputDir: importedShowDir, label: $("importEntryTopicLabel").value,
    entryIds: Array.from($("importEntryTopicCheckboxes").querySelectorAll("input:checked")).map((cb) => cb.value),
    fieldEl: $("importEntryTopicReuseField"), selectEl: $("importEntryTopicReuseMode"),
  });

  $("importEntryTopicSummaryBtn").addEventListener("click", async () => {
    const entryIds = Array.from($("importEntryTopicCheckboxes").querySelectorAll("input:checked")).map((cb) => cb.value);
    const cfg = currentBackendConfig();
    await runCustomTopicSummary({
      outputDir: importedShowDir,
      summitTitle: $("summitTitle").value.trim() || "Untitled Summit",
      contentType: $("contentType").value,
      backend: cfg.backend,
      apiKey: cfg.api_key,
      apiBase: cfg.api_base,
      model: $("overallModel").value.trim() || cfg.model,
      entryIds,
      label: $("importEntryTopicLabel").value,
      reuse: $("importEntryTopicReuseMode").value === "reuse",
      btn: $("importEntryTopicSummaryBtn"),
      stopBtn: $("importEntryTopicSummaryStopBtn"),
      hint: $("importEntryTopicSummaryHint"),
      resultEl: $("importEntryTopicSummaryResult"),
    });
    refreshImportEntryTopicReuse();
  });
  $("importEntryTopicLabel").addEventListener("input", refreshImportEntryTopicReuse);
  $("importEntryTopicCheckboxes").addEventListener("change", refreshImportEntryTopicReuse);

  $("importTopicSummaryBtn").addEventListener("click", async () => {
    const themes = Array.from($("importTopicCheckboxes").querySelectorAll("input:checked")).map((cb) => cb.value);
    const modeInput = document.querySelector('input[name="importTopicSummaryMode"]:checked');
    const cfg = currentBackendConfig();
    await runTopicSummary({
      outputDir: importedShowDir,
      summitTitle: $("summitTitle").value.trim() || "Untitled Summit",
      contentType: $("contentType").value,
      backend: cfg.backend,
      apiKey: cfg.api_key,
      apiBase: cfg.api_base,
      // 和大会总结一样是跨议题综合，优先用「大会总结用的模型」，没填就退回逐议题用的模型。
      model: $("overallModel").value.trim() || cfg.model,
      themes,
      mode: modeInput ? modeInput.value : "combined",
      reuse: $("importTopicReuseMode").value === "reuse",
      btn: $("importTopicSummaryBtn"),
      stopBtn: $("importTopicSummaryStopBtn"),
      hint: $("importTopicSummaryHint"),
      resultEl: $("importTopicSummaryResult"),
    });
    refreshImportTopicReuse();
  });
  $("importTopicCheckboxes").addEventListener("change", refreshImportTopicReuse);

  function startPolling(jobId) {
    const task = tasks.get(jobId);
    const el = task.el;
    // 任务只存在服务端内存里：服务重启后 /api/status 返回 404，这时要停止轮询并让
    // 卡片可以关掉，而不是每 1.2 秒抛一次错、卡片永远停在"运行中…"。
    const markLost = (msg) => {
      clearInterval(task.pollTimer);
      qs(el, "pauseBtn").style.display = "none";
      qs(el, "resumeBtn").style.display = "none";
      qs(el, "dismissBtn").style.display = "";
      qs(el, "statusBadge").textContent = "已断开";
      qs(el, "progressText").textContent = msg;
    };

    async function pollOnce() {
      let r, d;
      try {
        r = await fetch(`api/status/${jobId}`);
        d = await r.json();
      } catch (e) {
        task.pollFailures = (task.pollFailures || 0) + 1;
        if (task.pollFailures >= 10) {
          markLost(`连续多次查询进度失败（${e.message}）。任务可能还在后台运行，刷新页面可以重新接上。`);
        }
        return;
      }
      if (r.status === 404) {
        markLost("服务重启过，找不到这个任务的进度了。已写进输出目录的内容不受影响，重新「开始生成」会跳过已完成的部分。");
        return;
      }
      if (!r.ok) return;
      task.pollFailures = 0;
      qs(el, "logBox").textContent = (d.log || []).join("\n");
      qs(el, "logBox").scrollTop = qs(el, "logBox").scrollHeight;
      const pct = d.total ? Math.round((d.current / d.total) * 100) : 0;
      qs(el, "progressFill").style.width = pct + "%";

      let etaText = "";
      if (!d.paused && !d.done) {
        const now = Date.now();
        const samples = task.progressSamples;
        const last = samples[samples.length - 1];
        if (!last || last.current !== d.current) samples.push({ current: d.current, time: now });
        if (samples.length > 8) samples.shift();
        const first = samples[0];
        const newest = samples[samples.length - 1];
        const deltaItems = newest.current - first.current;
        const deltaMs = newest.time - first.time;
        if (deltaItems > 0 && deltaMs > 0 && d.total > d.current) {
          const remainingMs = (deltaMs / deltaItems) * (d.total - d.current);
          etaText = ` · 预计剩余 ${fmtEta(remainingMs)}`;
        }
      }
      qs(el, "progressText").textContent = `${d.current}/${d.total} · ${d.paused ? "已暂停" : (d.stage || "")}${etaText}`;
      qs(el, "statusBadge").textContent = d.done ? "" : (d.paused ? "已暂停" : "运行中…");
      qs(el, "pauseBtn").style.display = d.paused ? "none" : "";
      qs(el, "resumeBtn").style.display = d.paused ? "" : "none";
      if (d.done) {
        clearInterval(task.pollTimer);
        qs(el, "pauseBtn").style.display = "none";
        qs(el, "resumeBtn").style.display = "none";
        qs(el, "dismissBtn").style.display = "";
        if (d.error) {
          qs(el, "statusBadge").textContent = "✕ 失败";
          qs(el, "resultTitle").textContent = "✕ 任务失败";
          qs(el, "progressText").textContent = "出错：" + d.error;
          qs(el, "outDirText").textContent = "";
          qs(el, "logFileText").textContent = d.log_file || "";
          qs(el, "resultCounts").textContent = "任务失败，请查看任务日志。";
          qs(el, "resultBox").classList.add("show");
        } else if (d.result) {
          qs(el, "statusBadge").textContent = d.result.stopped ? "■ 已停止" : "✓ 完成";
          qs(el, "resultTitle").textContent = d.result.stopped ? "■ 已停止" : "✓ 完成";
          qs(el, "outDirText").textContent = d.result.output_dir;
          qs(el, "logFileText").textContent = d.result.log_file || d.log_file || "";
          qs(el, "resultBox").classList.add("show");
          const okCount = (d.result.rows || []).filter((row) => row.ok).length;
          const failedEntries = d.result.failed_entries || [];
          task.failedEntries = failedEntries;
          task.outputDir = d.result.output_dir;
          const unprocessedCount = (d.result.unprocessed_entries || []).length;
          qs(el, "resultCounts").textContent = d.result.stopped
            ? `本次已停止：已处理/跳过 ${(d.result.rows || []).length} 个，失败 ${failedEntries.length} 个，未处理 ${unprocessedCount} 个`
            : `本次运行：成功/跳过 ${okCount} 个，失败 ${failedEntries.length} 个（共选择 ${(d.result.rows || []).length} 个）`;
          qs(el, "retryFailedBtn").style.display = failedEntries.length > 0 ? "" : "none";
          qs(el, "renameByDateBtn").style.display = task.payload.content_type === "series" ? "" : "none";
          try {
            const rr = await fetch(`api/readme/${jobId}`);
            const dd = await rr.json();
            if (dd.content) renderMarkdown(dd.content, qs(el, "summaryPreview"));
          } catch (e) { /* ignore */ }
          setupTopicPicker(el, task);
          setupEntryPicker(el, task);
        }
      }
    }

    task.pollTimer = setInterval(async () => {
      if (task.polling) return;   // 上一次还没回来（服务忙/网络慢）就不叠加请求
      task.polling = true;
      try { await pollOnce(); } finally { task.polling = false; }
    }, 1200);
  }

  $("runBtn").addEventListener("click", async () => {
    const pickSummary = $("summaryScope").value === "pick";
    const summaryWantByIdx = new Map(summaryCheckboxes().map((cb) => [cb.dataset.idx, cb.checked]));
    const selected = selectedCheckboxes().filter((b) => b.checked).map((b) => ({
      ...entries[b.dataset.idx],
      want_summary: pickSummary ? (summaryWantByIdx.get(b.dataset.idx) ?? true) : true,
    }));
    if (selected.length === 0) { $("runHint").textContent = T("请至少选择一个议题"); return; }
    $("runHint").textContent = "";
    $("runBtn").disabled = true;
    try {
      await launchTask(buildRunPayload(selected));
    } catch (e) {
      $("runHint").textContent = e.message;
    } finally {
      // 任务已经作为独立卡片在后台跑了，随时可以继续配置/开始下一个任务。
      $("runBtn").disabled = false;
    }
  });

  applyMode();
  if (LOCKED === "track") initTrackSubscriptions();
  loadEnv();
  renderRecentUrls();
  updateOverallModelOptions();
  restoreTasks();
})();
