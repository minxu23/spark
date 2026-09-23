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
    // 「信息跟进」暂时借用「Podcast 跟进」这套词表——两边现在走的是同一段处理逻辑，
    // 用词分叉之前先别急着造一份新词表，见上面 track 入口的注释。
    track: [["大会/节目", "节目"], ["议题", "单集"], ["大会", "节目"], ["峰会", "节目"]],
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

  // ---------- 「信息跟进」订阅列表（只在 mode=track 下用到）----------
  // 设计上尽量少加状态：新内容不在这里维护一份"已知 id"，每次「检查」都问
  // 服务端现算（服务端拿 manifest 现场比对）；生成动作也不重新实现一遍选择
  // 界面，而是把订阅的链接/名称/输出目录灌进已有的 discover→选择议题→生成
  // 这条路径——一条订阅本质上就是"记住了名字和输出目录的一个链接"。
  const SOURCE_TYPE_LABEL = {
    rss: "RSS", substack: "Substack", wechat: "公众号", youtube: "YouTube", unknown: "链接",
  };
  let subscriptions = [];
  let subsLoaded = false;
  let subsExpandedCats = new Set();
  let subsExpandedRowId = null;
  let subsEditingId = null;
  let subsAddOpen = false;
  let subsCheckResults = {}; // sub_id -> {new_count, new_entries, error, total}
  let subsLastCheckAllAt = null;

  function escHtml(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
    ));
  }

  async function loadSubscriptions() {
    try {
      const r = await fetch("api/subscriptions");
      subscriptions = await r.json();
    } catch (e) {
      subscriptions = [];
    }
    subsLoaded = true;
    renderSubs();
  }

  // 打开页面时跑一轮——只做免费的 discover 探测 + 跟 manifest 比对，不碰模型。
  async function checkAllSubscriptions() {
    if (!subscriptions.length) return;
    try {
      const r = await fetch("api/subscriptions/check_all", { method: "POST" });
      const d = await r.json();
      (d.results || []).forEach((row) => { subsCheckResults[row.id] = row; });
      subsLastCheckAllAt = Date.now();
    } catch (e) {
      // 静默失败——不该因为自动检查失败挡住整个页面，手动点「检查」/「重新检查全部」还能再试
    }
    renderSubs();
  }

  async function checkOneSubscription(id) {
    try {
      const r = await fetch(`api/subscriptions/${id}/check`, { method: "POST" });
      const row = await r.json();
      subsCheckResults[id] = row;
    } catch (e) {
      subsCheckResults[id] = { id, error: e.message, new_count: 0, new_entries: [] };
    }
    renderSubs();
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
      // 失败时表单还开着，按钮还在；成功时 loadSubscriptions() 已经把整个
      // #subsBox 重画过一遍（表单收起、按钮也没了），这里就不用再管它。
      const btn = $("subsAddSubmit");
      if (btn) btn.disabled = false;
    }
  }

  async function saveSubscriptionEdit(id) {
    const name = $(`subEditName-${id}`).value.trim();
    const category = $(`subEditCategory-${id}`).value.trim() || "未分类";
    const outputDir = $(`subEditOutdir-${id}`).value.trim();
    if (!name) { $(`subEditErr-${id}`).textContent = "名称不能为空"; return; }
    try {
      const r = await fetch(`api/subscriptions/${id}`, {
        method: "PATCH", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, category, output_dir: outputDir }),
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
    if (!confirm(`停止跟进「${name}」？已经生成的内容不会被删除，只是这个页面不再帮你盯着它有没有更新。`)) return;
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
      // 极少数嵌入式/受限浏览器环境不支持 prompt()——降级成不做任何事，
      // 比抛出一个用户看不到解释的未捕获错误安全。
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

  // 点一条订阅的名字，就相当于把这个链接粘进「临时链接」那套 discover→选择
  // 议题→生成的流程——复用它全部的选择/勾选/模型配置逻辑，不重新做一遍。
  async function openSubscriptionForRun(sub) {
    await runDiscover(sub.url);
    $("outputDir").value = sub.output_dir;
    $("summitTitle").value = sub.name;
    await probeExistingSummary(sub.name);
    $("discoverResults").scrollIntoView({ behavior: "smooth", block: "start" });
  }

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
            <label for="subEditOutdir-${id}">输出目录</label>
            <input type="text" id="subEditOutdir-${id}" value="${escHtml(item.output_dir)}" />
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
    else if (newCount > 0) badge = `<span class="subs-badge">${newCount} 条新内容</span>`;
    const dotClass = check && check.error ? "no-new" : (newCount > 0 ? "has-new" : "no-new");

    return `
      <div class="subs-row">
        <button type="button" class="subs-name" data-sub-toggle="${id}" title="查看新内容 / 去选择生成">
          <span class="subs-dot ${dotClass}" aria-hidden="true"></span>
          <span class="label">${escHtml(item.name)}</span>
        </button>
        <span class="tag">${SOURCE_TYPE_LABEL[item.source_type] || "链接"}</span>
        ${badge}
        <div class="subs-actions">
          <button type="button" class="secondary mini" data-sub-check="${id}">检查</button>
          <button type="button" class="secondary mini" data-sub-edit="${id}">编辑</button>
          <button type="button" class="secondary mini" data-sub-delete="${id}">删除</button>
        </div>
      </div>
      ${subsExpandedRowId === id ? renderSubExpanded(item, check) : ""}`;
  }

  function renderSubExpanded(item, check) {
    let body;
    if (check && check.error) {
      body = `<p class="subs-err">检查失败：${escHtml(check.error)}</p>`;
    } else if (!check) {
      body = `<p class="hint" style="margin:0">还没检查过，点「检查」看看有没有新内容。</p>`;
    } else if (check.new_count > 0) {
      body = `
        <p class="hint" style="margin:0 0 6px">发现 ${check.new_count} 条新内容：</p>
        ${check.new_entries.slice(0, 8).map((e) => `
          <div class="item">
            <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${escHtml(e.title || "(未命名)")}</span>
            <span class="hint" style="margin:0">${escHtml(e.publish_date || "")}</span>
          </div>`).join("")}
        ${check.new_entries.length > 8 ? `<p class="hint" style="margin:4px 0 0">还有 ${check.new_entries.length - 8} 条，去选择议题里能看全部</p>` : ""}`;
    } else {
      body = `<p class="hint" style="margin:0">共 ${check.total ?? 0} 条，暂时没有新内容。</p>`;
    }
    return `
      <div class="subs-new-panel">
        ${body}
        <div style="display:flex;gap:var(--space-3);margin-top:10px">
          <button type="button" class="mini" data-sub-generate="${item.id}">去选择 / 生成 →</button>
          <button type="button" class="secondary mini" data-sub-toggle="${item.id}">收起</button>
        </div>
      </div>`;
  }

  function renderSubs() {
    const box = $("subsBox");
    if (!subsLoaded) { box.innerHTML = `<p class="hint">正在加载订阅列表…</p>`; return; }

    const cats = categoriesInUse();
    let listHtml;
    if (!subscriptions.length) {
      listHtml = `<div class="subs-empty">还没有订阅——点下面「添加订阅」，粘一个播客/RSS/博客/YouTube 频道链接。</div>`;
    } else {
      listHtml = `<div class="subs-list">` + cats.map((cat) => {
        const items = subscriptions.filter((s) => (s.category || "未分类") === cat);
        const open = subsExpandedCats.has(cat);
        const newTotal = items.reduce((n, it) => {
          const c = subsCheckResults[it.id];
          return n + (c && !c.error ? c.new_count : 0);
        }, 0);
        const meta = `${items.length} 个订阅` + (newTotal > 0 ? ` · 发现 ${newTotal} 条新内容` : "");
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

    const addBtn = `<button type="button" class="subs-add-btn" id="subsAddOpenBtn">+ 添加订阅</button>`;
    const addForm = `
      <div class="subs-add-form">
        <strong>添加订阅</strong>
        <div class="field" style="margin-bottom:0">
          <label for="subsNewUrl">链接</label>
          <input type="text" id="subsNewUrl" placeholder="播客 / RSS 订阅地址 / YouTube 频道 / podcasts.apple.com/.../id... / mp.weixin.qq.com/s/..." />
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

    const checkedHint = subsLastCheckAllAt
      ? `已自动检查全部订阅 · ${fmtClock(subsLastCheckAllAt)}`
      : "打开页面时会自动检查一遍订阅（只做免费探测，不消耗模型调用）";
    const footer = `
      <p class="hint" style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
        ${checkedHint}
        <button type="button" class="secondary mini" id="subsCheckAllBtn">重新检查全部</button>
      </p>`;

    box.innerHTML = listHtml
      + `<datalist id="subsCategoryList">${cats.map((c) => `<option value="${escHtml(c)}"></option>`).join("")}</datalist>`
      + (subsAddOpen ? addForm : addBtn)
      + footer;
  }

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
    } else if ((id = t.closest("[data-sub-generate]")?.dataset.subGenerate) !== undefined) {
      const item = subscriptions.find((s) => s.id === id);
      if (item) openSubscriptionForRun(item);
    } else if (t.closest("#subsAddOpenBtn")) {
      subsAddOpen = true;
      renderSubs();
      $("subsNewUrl")?.focus();
    } else if (t.closest("#subsAddCancel")) {
      subsAddOpen = false;
      renderSubs();
    } else if (t.closest("#subsAddSubmit")) {
      submitAddSubscription();
    } else if (t.closest("#subsCheckAllBtn")) {
      checkAllSubscriptions();
    }
  });

  $("tabSubs").addEventListener("click", () => {
    $("tabSubs").classList.add("on");
    $("tabLinkMode").classList.remove("on");
    $("subsBox").classList.remove("hidden");
    $("linkModeBox").classList.add("hidden");
  });
  $("tabLinkMode").addEventListener("click", () => {
    $("tabLinkMode").classList.add("on");
    $("tabSubs").classList.remove("on");
    $("linkModeBox").classList.remove("hidden");
    $("subsBox").classList.add("hidden");
  });

  function initTrackSubscriptions() {
    $("trackTabs").classList.remove("hidden");
    $("subsBox").classList.remove("hidden");
    $("linkModeBox").classList.add("hidden");
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
        <td>${e.title.replace(/</g, "&lt;")}</td>
        <td>${fmtDuration(e.duration)}</td>
        <td><span class="tag ${e.is_raw_session ? "raw" : "talk"}">${T(e.is_raw_session ? "完整场次录像" : "议题")}</span></td>
        <td><input type="checkbox" class="summary-cb" data-idx="${idx}" checked /></td>
        <td><a href="${e.url}" target="_blank" rel="noopener">观看</a></td>
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

  async function runDiscover(url) {
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
    } catch (e) {
      $("discoverErr").textContent = e.message;
    } finally {
      $("discoverBtn").disabled = false;
      $("discoverSpinner").style.display = "none";
    }
  }

  $("discoverBtn").addEventListener("click", () => runDiscover($("urlInput").value));

  // 从剪贴板粘贴的一段自由文本里批量提取链接，逐条解析成议题，拼成一份
  // "专题"——每条链接各自独立（不像 runDiscover 那样，一个链接背后是一整份
  // 共享同一个标题的播放列表/订阅源），所以没有天然的标题，交给用户自己填。
  async function runDiscoverFromText(text) {
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
      const preferred = d.original_language && d.languages.some((l) => l.code === d.original_language)
        ? d.original_language
        : (d.languages.some((l) => l.code === "en") ? "en" : d.languages[0].code);
      sel.value = preferred;
      hint.textContent = T(`以「${probe.title}」探测到 ${d.languages.length} 种可用字幕语言（同一播放列表内其他议题可能略有差异）。`);
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
    task.pollTimer = setInterval(async () => {
      const r = await fetch(`api/status/${jobId}`);
      const d = await r.json();
      qs(el, "logBox").textContent = d.log.join("\n");
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
