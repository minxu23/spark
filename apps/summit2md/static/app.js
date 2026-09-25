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
  // 「信息跟进」有自己的订阅 → 新内容 → 逐条笔记 + 本批简报流程（见下面的收件箱和
  // tracking.py）；它的「临时链接」标签页仍走 discover→选择→生成 这套单期处理逻辑，
  // 在后端眼里是 series（见 CONTENT_TYPE_FOR_MODE）。
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
    if (LOCKED === "series") {
      // 这一块现在是「新单集 / 订阅管理 / 临时链接」三个标签页，不只是"获取单集列表"
      document.querySelector("#taskPanel h2").innerHTML = `<span class="num">1</span>跟进的节目`;
    }
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

  // ---------- 订阅（信息跟进 mode=track、Podcast 跟进 mode=series 共用）----------
  // 三个标签页：
  // - 新内容：所有订阅的新条目汇在一起，勾选后一次生成"逐条笔记 + 本批简报"；
  // - 订阅管理：增删改订阅、分类；
  // - 临时链接：一次性的链接，走原来的 discover→选择→生成 流程。
  // "有没有新内容"不在前端记，每次检查都问服务端（服务端拿 manifest 现算）。
  const SOURCE_TYPE_LABEL = {
    rss: "RSS", substack: "Substack", wechat: "公众号", youtube: "YouTube",
    article: "网页 (sitemap)", unknown: "链接",
  };
  // 「Podcast 跟进」也有一套一样的订阅管理，订阅存在同一个文件里，用 kind 区分。
  // 差别只在「新单集」那一页：选中的单集交给跟「临时链接」同一套逐期处理（写进节目
  // 文件夹、刷新节目总结，进度是页面下方的任务卡片），而不是出一份跨订阅的简报。
  const HAS_SUBS = LOCKED === "track" || LOCKED === "series";
  const SUBS_KIND = LOCKED === "series" ? "podcast" : "track";
  const IS_PODCAST_SUBS = SUBS_KIND === "podcast";
  const SUBS_TEXT = IS_PODCAST_SUBS ? {
    tabInbox: "新单集",
    unit: "期",
    newThings: "期新单集",
    emptyList: "还没有订阅——点下面「添加订阅」，粘一个播客链接（Substack、RSS、Apple Podcast、YouTube 节目频道）。",
    urlPlaceholder: "Substack 播客 / 播客 RSS / podcasts.apple.com/... / YouTube 节目频道",
    inboxNoSubs: "还没有订阅。到「订阅管理」里添加想跟的播客，之后新单集会出现在这里。",
    noneNew: "自动检查的节目都没有新单集。",
    checkingHint: "正在检查节目有没有新单集（只列标题，不消耗模型调用）……",
  } : {
    tabInbox: "新内容",
    unit: "条",
    newThings: "条新内容",
    emptyList: "还没有订阅——点下面「添加订阅」，粘一个 RSS / 博客 / 播客 / YouTube 频道链接。",
    urlPlaceholder: "RSS 订阅地址 / 没有 RSS 的资讯页（如 anthropic.com/news）/ 播客 / YouTube 频道",
    inboxNoSubs: "还没有订阅。到「订阅管理」里添加 RSS、资讯网站、播客或 YouTube 频道，之后新内容会出现在这里。",
    noneNew: "自动检查的订阅都没有新内容。",
    checkingHint: "正在检查订阅有没有新内容（只列标题，不消耗模型调用）……",
  };
  // 正在更新的节目：订阅 id -> {jobId, count}（count 为 null 表示刷新页面后接上的、
  // 不知道这次选了几期）。更新期间不再列它的新单集，免得重复启动。
  const podcastUpdating = new Map();
  // 检查结果按"这次检查是什么时候发出的"取舍：更新跑完后要以跑完之后发出的检查
  // 为准，之前发出、之后才回来的（比如更新期间点的「重新检查」）一律丢掉。
  const subsCheckFloor = new Map();   // sub_id -> 这个时间之前发出的检查结果不要
  const subsRecheck = new Set();      // 检查还在路上时又要求重查的订阅
  function acceptCheckResult(id, row, startedAt) {
    if (startedAt < (subsCheckFloor.get(id) || 0)) return;
    const cur = subsCheckResults[id];
    if (cur && (cur._at || 0) > startedAt) return;
    subsCheckResults[id] = { ...row, _at: startedAt };
  }

  // 节目文件夹名：更新任务的 summit_title 就是它，刷新后按它把任务卡片认回订阅
  const folderBase = (folder) => (folder || "").replace(/[\\/]+$/, "").split(/[\\/]/).pop();

  // 刷新页面后，任务卡片由 restoreTasks() 接回来，但哪个节目正在更新这件事只在
  // 内存里——按节目文件夹名把还在跑的卡片跟订阅对上，重新标成"正在更新"。
  function linkPodcastTasks() {
    if (!IS_PODCAST_SUBS || !subsLoaded) return;
    for (const [jobId, task] of tasks) {
      if (task.done || task.onDone || task.payload.content_type !== "series") continue;
      const sub = subscriptions.find((x) => folderBase(x.folder) === task.payload.summit_title);
      if (!sub || podcastUpdating.has(sub.id)) continue;
      podcastUpdating.set(sub.id, { jobId, count: null });
      task.onDone = () => finishPodcastUpdate(sub.id);
    }
    renderInbox();
  }

  function finishPodcastUpdate(subId) {
    podcastUpdating.delete(subId);
    // 更新前的检查结果里全是刚处理掉的单集：先清掉，不然重查回来之前会闪回来、还能再点
    delete subsCheckResults[subId];
    subsCheckFloor.set(subId, Date.now());
    if (subsCheckingOne.has(subId)) subsRecheck.add(subId);
    else checkOneSubscription(subId);
  }

  let subscriptions = [];
  let subsLoaded = false;
  let subsLoadError = "";
  let subsExpandedCats = new Set();
  let subsExpandedRowId = null;
  let subsEditingId = null;
  // 下一次重画订阅列表后要把焦点放到哪（CSS 选择器）：点了「编辑」去名称输入框，
  // 「取消」「保存」回到那一行的「编辑」，「删除」回到所在类别——被点的按钮本身
  // 在重画后已经不在了，不指定的话焦点会掉回页面开头。
  let subsFocusNext = null;
  // 正在用输入法打字（拼音还没上屏）时不重画：整块 innerHTML 重建会把没上屏的拼音吞掉。
  // 等 compositionend 再补画一次。
  let subsComposing = false, subsRenderPending = false;
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
  let inboxStarting = false; // 「生成简报」请求发出去还没回来：防止连点启动两批
  let inboxStartError = "";
  let inboxSummaryLength = "medium";
  try { inboxSummaryLength = localStorage.getItem("track.summaryLength") || "medium"; } catch (e) { /* ignore */ }

  const { escHtml, attachDirAutocomplete, fmtClock } = window.SparkCommon;

  function safeHref(url) {
    return /^https?:\/\//i.test(url || "") ? escHtml(url) : "";
  }

  function renderTrack() {
    renderSubs();
    renderInbox();
  }

  async function loadSubscriptions() {
    const doneSeqAtStart = autoCheckDoneSeq;
    try {
      const r = await fetch(`api/subscriptions?kind=${SUBS_KIND}`);
      const d = await r.json();
      if (!r.ok || !Array.isArray(d)) throw new Error(d.error || "读取订阅列表失败");
      subscriptions = d;
      rememberSavedAutoCheck(doneSeqAtStart);
      subsLoaded = true;
      linkPodcastTasks();
      subsLoadError = "";
    } catch (e) {
      subsLoadError = e.message;
    }
    subsLoaded = true;
    renderTrack();
  }

  // 打开了"自动检查"的订阅（老订阅没有这个字段，按打开算）
  const isAutoCheck = (sub) => sub.auto_check !== false;
  const autoSubscriptions = () => subscriptions.filter(isAutoCheck);

  // 只做免费的列表探测 + 跟 manifest 比对，不碰模型。只查打开了自动检查的订阅。
  async function checkAllSubscriptions() {
    if (!autoSubscriptions().length || subsChecking) return;
    subsChecking = true;
    renderTrack();
    const startedAt = Date.now();
    try {
      const r = await fetch("api/subscriptions/check_all", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ kind: SUBS_KIND }),
      });
      const d = await r.json();
      (d.results || []).forEach((row) => acceptCheckResult(row.id, row, startedAt));
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
    const startedAt = Date.now();
    try {
      const r = await fetch(`api/subscriptions/${id}/check`, { method: "POST" });
      const row = await r.json();
      acceptCheckResult(id, r.ok ? row : { id, error: row.error || "检查失败", new_count: 0, new_entries: [] }, startedAt);
    } catch (e) {
      acceptCheckResult(id, { id, error: e.message, new_count: 0, new_entries: [] }, startedAt);
    }
    subsCheckingOne.delete(id);
    renderTrack();
    // 这次检查在路上时更新跑完了：结果是跑完之前的，已经被丢掉，再查一次
    if (subsRecheck.delete(id)) checkOneSubscription(id);
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
        body: JSON.stringify({ url, name, category, output_dir: $("outputDir").value,
                               auto_check: $("subsNewAuto").checked, kind: SUBS_KIND }),
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

  async function fillProcessedPodcasts() {
    const hint = $("subsFillProcessedHint");
    const btn = $("subsFillProcessed");
    if (btn.disabled) return;
    btn.disabled = true;
    hint.textContent = "正在查找…";
    try {
      const r = await fetch("api/subscriptions/podcast_candidates", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ output_dir: $("outputDir").value }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || "查找失败");
      const found = d.candidates || [];
      const manual = d.manual || [];
      // 这几个文件夹名在按行导入时会被改掉（开头的 - * •、末尾的冒号逗号），
      // 填进来就会订到一个新文件夹——只提示，让用户用「添加订阅」单独加
      const manualNote = manual.length
        ? ` 另外 ${manual.map((c) => `「${c.name}」`).join("、")}的文件夹名批量导入认不准，请用「添加订阅」单独加，名称填文件夹名。`
        : "";
      if (!found.length) { hint.textContent = "输出目录里没有还没订阅的、处理过的节目。" + manualNote; return; }
      // 名称就用文件夹名：订阅的文件夹按名称算，这样才会落回原来那个文件夹
      const box = $("subsBulkText");
      const lines = found.map((c) => `${c.name} : ${c.url}`).join("\n");
      box.value = box.value.trim() ? `${box.value.trim()}\n${lines}` : lines;
      hint.textContent = `填入了 ${found.length} 个节目（${found.map((c) => `${c.name} ${c.episodes} 期`).join("、")}）。不想订阅的删掉那一行，再点「导入」。` + manualNote;
    } catch (e) {
      hint.textContent = e.message;
    } finally {
      if ($("subsFillProcessed")) $("subsFillProcessed").disabled = false;
    }
  }

  // 批量导入：粘贴一段"名称 : 链接"（或者干脆只有链接），一行一条，统一分到
  // 同一个类别。允许部分失败——失败/已经订阅过的会在结果里列出来。
  async function submitBulkSubscriptions() {
    const text = $("subsBulkText").value;
    const category = $("subsBulkCategory").value.trim() || "未分类";
    $("subsBulkErr").textContent = "";
    if (!text.trim()) { $("subsBulkErr").textContent = "请粘贴至少一行「名称 : 链接」，或一份 OPML"; return; }
    $("subsBulkSubmit").disabled = true;
    $("subsBulkSubmit").textContent = "正在识别（源多的话要等一会儿）…";
    try {
      const r = await fetch("api/subscriptions/bulk", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text, category, output_dir: $("outputDir").value,
                               auto_check: $("subsBulkAuto").checked, kind: SUBS_KIND }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || "导入失败");
      subsBulkOpen = false;
      subsBulkResult = { added: d.added, failed: d.failed };
      await loadSubscriptions();
      // 只查这次新加进来的：页面打开时那一轮「检查全部」可能还在跑，
      // 它拿的是导入之前的清单，不会顺带查到它们
      checkSubscriptionsSoon((d.added || []).filter(isAutoCheck).map((it) => it.id));
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
      subsFocusNext = `button[data-sub-edit="${CSS.escape(id)}"]`;
      await loadSubscriptions();
    } catch (e) {
      $(`subEditErr-${id}`).textContent = e.message;
    }
  }

  // 刚打开自动检查 / 刚批量导入的订阅顺手查一次，新内容马上出现在收件箱里。
  // 整个类别一起打开时可能有几十个，同时最多查 6 个。
  function checkSubscriptionsSoon(ids) {
    // 查过而且没出错的就不再查；以前查失败的（比如当时断网）要重查，不然「检查失败」一直挂着
    const queue = ids.filter((id) => !subsCheckResults[id] || subsCheckResults[id].error);
    const worker = async () => { while (queue.length) await checkOneSubscription(queue.shift()); };
    for (let i = 0; i < Math.min(6, queue.length); i++) worker();
  }

  // 「自动检查」的保存请求排队一个个发：连着勾、取消很快时，两个请求并发到了服务端
  // 谁先落盘说不准，界面显示的可能跟存下来的相反。
  //   autoCheckOp：每个订阅最后一次改动是第几次——只有最后一次改动能决定显示什么；
  //   autoCheckSaved：服务端确认过的值——失败回滚回滚到它，而不是回滚到上一次
  //     乐观改动的值（连着两次都失败时，后者会让界面显示成跟服务端相反）；
  //   autoCheckWanted：还没存完的改动——这期间重新拉一次订阅列表，要把它们盖回去。
  //   autoCheckDone：每个订阅最近一次存成功的值和序号——拉列表的 GET 如果是在这次
  //     保存完成之前发出的，它带回来的是旧值，要用这里的新值盖掉。
  let autoCheckQueue = Promise.resolve();
  let autoCheckSeq = 0, autoCheckPending = 0, autoCheckDoneSeq = 0;
  const autoCheckOp = new Map();
  const autoCheckSaved = new Map();
  const autoCheckWanted = new Map();
  const autoCheckDone = new Map();
  const autoCheckFailed = new Map();  // id -> 失败原因；后来又存成功了就删掉

  function rememberSavedAutoCheck(doneSeqAtStart) {
    subscriptions.forEach((s) => {
      const done = autoCheckDone.get(s.id);
      if (done && done.seq > doneSeqAtStart) s.auto_check = done.value;
    });
    autoCheckSaved.clear();
    subscriptions.forEach((s) => autoCheckSaved.set(s.id, isAutoCheck(s)));
    subscriptions.forEach((s) => { if (autoCheckWanted.has(s.id)) s.auto_check = autoCheckWanted.get(s.id); });
  }

  function setAutoCheck(ids, value) {
    // 先改本地再发请求：开关要立刻有反应；失败了改回去并提示
    const op = ++autoCheckSeq;
    const idSet = new Set(ids);
    subscriptions.forEach((s) => { if (idSet.has(s.id)) s.auto_check = value; });
    ids.forEach((id) => { autoCheckOp.set(id, op); autoCheckWanted.set(id, value); });
    autoCheckPending += 1;
    renderTrack();
    autoCheckQueue = autoCheckQueue.then(async () => {
      const latest = (id) => autoCheckOp.get(id) === op;
      try {
        // 卡住的请求不能一直占着队列，后面的改动全发不出去
        const signal = AbortSignal.timeout(20000);
        const r = ids.length === 1
          ? await fetch(`api/subscriptions/${ids[0]}`, {
            method: "PATCH", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ auto_check: value }), signal,
          })
          : await fetch("api/subscriptions/auto_check", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ ids, auto_check: value }), signal,
          });
        if (!r.ok) { const d = await r.json().catch(() => ({})); throw new Error(d.error || "保存失败"); }
        autoCheckDoneSeq += 1;
        ids.forEach((id) => {
          autoCheckSaved.set(id, value);
          autoCheckDone.set(id, { seq: autoCheckDoneSeq, value });
          autoCheckFailed.delete(id);  // 之前失败过、这次存上了，就不用再提示
        });
        if (value) checkSubscriptionsSoon(ids.filter(latest));
      } catch (e) {
        ids.forEach((id) => autoCheckFailed.set(id, e.name === "TimeoutError" ? "保存超时" : e.message));
        if (e.name === "TimeoutError") {
          // 超时只是浏览器这边不等了，服务端可能已经存上——不猜，重新拉一次看服务端现在是什么
          ids.forEach((id) => { if (latest(id)) autoCheckWanted.delete(id); });
          await loadSubscriptions();
        } else {
          subscriptions.forEach((s) => {
            if (idSet.has(s.id) && latest(s.id) && autoCheckSaved.has(s.id)) s.auto_check = autoCheckSaved.get(s.id);
          });
          renderTrack();
        }
      } finally {
        ids.forEach((id) => { if (latest(id)) autoCheckWanted.delete(id); });
        autoCheckPending -= 1;
        // 连着几次都失败时只弹一次，别一个个排队弹窗；后来又存成功的不算
        if (!autoCheckPending && autoCheckFailed.size) {
          const msg = [...new Set(autoCheckFailed.values())].join("；");
          autoCheckFailed.clear();
          alert(`没能保存"自动检查"设置：${msg}`);
        }
      }
    });
  }

  async function deleteSubscription(id, name) {
    if (!confirm(`停止跟进「${name}」？已经生成的笔记不会被删除，只是不再检查它有没有更新。`)) return;
    try {
      const r = await fetch(`api/subscriptions/${id}`, { method: "DELETE" });
      if (!r.ok) { const d = await r.json().catch(() => ({})); throw new Error(d.error || "删除失败"); }
      delete subsCheckResults[id];
      // 删掉的那一行没了，焦点回到它所在类别的标题（类别也空了就落到「添加订阅」）
      const cat = subscriptions.find((s) => s.id === id)?.category || "未分类";
      subsFocusNext = `button[data-cat-toggle="${CSS.escape(cat)}"]`;
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
        body: JSON.stringify({ old: oldName, new: trimmed, kind: SUBS_KIND }),
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
    const newCount = visibleNewEntries(item).length;
    let badge = "";
    if (check && check.error) badge = `<span class="subs-badge" style="color:var(--err)">检查失败</span>`;
    else if (newCount > 0) badge = `<span class="subs-badge">${newCount} ${SUBS_TEXT.unit}新</span>`;
    const dotClass = check && check.error ? "no-new" : (newCount > 0 ? "has-new" : "no-new");
    const checking = subsCheckingOne.has(id);

    const auto = isAutoCheck(item);
    return `
      <div class="subs-row${auto ? "" : " manual-only"}">
        <button type="button" class="subs-name" data-sub-toggle="${id}" title="查看详情">
          <span class="subs-dot ${dotClass}" aria-hidden="true"></span>
          <span class="label">${escHtml(item.name)}</span>
        </button>
        <span class="tag">${SOURCE_TYPE_LABEL[item.source_type] || "链接"}</span>
        ${badge}
        <label class="subs-auto" title="勾上：打开页面、点「重新检查」时自动检查它；不勾：只在点「检查」时才查">
          <input type="checkbox" data-sub-auto="${id}" ${auto ? "checked" : ""} aria-label="自动检查「${escHtml(item.name)}」" />自动检查
        </label>
        <div class="subs-actions">
          <button type="button" class="secondary mini" data-sub-check="${id}" ${checking ? 'aria-disabled="true"' : ""}>${checking ? "检查中…" : "检查"}</button>
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
    else {
      const n = visibleNewEntries(item).length;
      const u = SUBS_TEXT.unit;
      status = `<p class="hint" style="margin:0">源里共 ${check.total ?? 0} ${u}，其中 ${n} ${u}还没处理${n ? `（在「${SUBS_TEXT.tabInbox}」里）` : ""}。</p>`
        + (check.older_count ? `<p class="hint" style="margin:4px 0 0">另有 ${check.older_count} ${u}比已处理的最新一${u}更早、从没处理过，不算新单集；要补的话在「临时链接」里粘这个节目的链接，勾选处理（已处理的会自动跳过）。</p>` : "");
    }
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
    if (subsComposing) { subsRenderPending = true; return; }
    // 只用一次：这次重画没用上（比如列表读取失败走了提前返回）也就作废，
    // 免得很久以后某次不相干的重画突然把焦点拽走
    const focusNext = subsFocusNext;
    subsFocusNext = null;
    const box = $("subsBox");
    if (!subsLoaded) { box.innerHTML = `<p class="hint">正在加载订阅列表…</p>`; return; }
    if (subsLoadError) { box.innerHTML = `<p class="subs-err">${escHtml(subsLoadError)}</p>`; return; }

    const cats = categoriesInUse();
    let listHtml;
    if (!subscriptions.length) {
      listHtml = `<div class="subs-empty">${SUBS_TEXT.emptyList}</div>`;
    } else {
      listHtml = `<div class="subs-list">` + cats.map((cat) => {
        const items = subscriptions.filter((s) => (s.category || "未分类") === cat);
        const open = subsExpandedCats.has(cat);
        const newTotal = items.reduce((n, it) => n + visibleNewEntries(it).length, 0);
        const autoN = items.filter(isAutoCheck).length;
        const meta = `${items.length} 个订阅` + (autoN < items.length ? `（${autoN} 个自动检查）` : "")
          + (newTotal > 0 ? ` · <span class="subs-cat-new">${newTotal} ${SUBS_TEXT.newThings}</span>` : "");
        return `
          <div class="subs-cat">
            <button type="button" class="subs-cat-toggle" data-cat-toggle="${escHtml(cat)}">
              <span class="caret">${open ? "▾" : "▸"}</span>
              <strong>${escHtml(cat)}</strong>
            </button>
            <span class="subs-cat-meta">${meta}</span>
            <label class="subs-auto" title="这个类别下的订阅一起开/关自动检查">
              <input type="checkbox" data-cat-auto="${escHtml(cat)}" ${autoN === items.length ? "checked" : ""}
                ${autoN > 0 && autoN < items.length ? 'data-indeterminate="1"' : ""} aria-label="「${escHtml(cat)}」全部自动检查" />全部自动检查
            </label>
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
          <input type="text" id="subsNewUrl" placeholder="${SUBS_TEXT.urlPlaceholder}" />
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
        <label class="check-row" style="margin:0"><input type="checkbox" id="subsNewAuto" checked />每次打开页面时自动检查更新（不勾就只在手动点「检查」时查）</label>
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
          <label for="subsBulkText">粘贴一段「名称 : 链接」，一行一条（只有链接、没有名称也可以）；也可以直接粘贴 RSS 阅读器导出的 OPML</label>
          <textarea id="subsBulkText" rows="6" placeholder="${IS_PODCAST_SUBS
            ? "Dwarkesh Podcast : https://www.dwarkesh.com/&#10;Latent Space : https://www.latent.space/feed&#10;https://www.youtube.com/@a16z/videos"
            : "MarkTechPost : https://www.marktechpost.com/feed/&#10;AI Insider : https://theaiinsider.tech/feed/&#10;https://the-decoder.com/feed/"}"></textarea>
        </div>
        ${IS_PODCAST_SUBS ? `
        <div>
          <button type="button" class="secondary mini" id="subsFillProcessed">填入以前处理过的节目</button>
          <span class="hint" id="subsFillProcessedHint" style="margin:0 0 0 8px">在输出目录里找用「临时链接」处理过、还没订阅的节目；订阅后沿用原来的文件夹，处理过的单集不会重做</span>
        </div>` : ""}
        <div class="field" style="margin-bottom:0">
          <label for="subsBulkCategory">类别（这一批统一归到这个类别下）</label>
          <input type="text" id="subsBulkCategory" list="subsCategoryList" placeholder="选一个已有的，或直接输入新类别" />
        </div>
        <label class="check-row" style="margin:0"><input type="checkbox" id="subsBulkAuto" checked />导入后每次打开页面时自动检查更新（源很多时建议不勾，之后在列表里挑着开）</label>
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
      box.innerHTML = subsFilterHtml() + `<div class="subs-list-area"></div><div class="subs-form-area"></div>`;
      renderSubsFilterResults();
      listEl = box.querySelector(":scope > .subs-list-area");
      formEl = box.querySelector(":scope > .subs-form-area");
      formEl.dataset.mode = "";
    }
    // 编辑表单开着时列表照样更新（检查结果、新条数），只是先记下表单里正在输入的
    // 内容和光标位置，重画后原样放回去。
    const saved = [];
    if (subsEditingId) {
      for (const field of ["subEditName", "subEditCategory", "subEditFolder", "subEditErr"]) {
        const el = listEl.querySelector(`#${field}-${CSS.escape(subsEditingId)}`);
        if (el) saved.push([el.id, el.tagName === "INPUT" ? el.value : el.textContent]);
      }
    }
    // 焦点也要跟着搬过去：勾一下「自动检查」、检查跑完都会重画整个列表，
    // 不然键盘用户的焦点每次都掉回页面开头。没有 id 的控件按它的 data-* 找回来。
    let focusSel = null, selStart = null, selEnd = null;
    const active = document.activeElement;
    if (active && listEl.contains(active)) {
      if (active.id) focusSel = `#${CSS.escape(active.id)}`;
      else {
        const attr = [...active.attributes].find((a) => a.name.startsWith("data-") && a.name !== "data-indeterminate");
        if (attr) focusSel = `${active.tagName.toLowerCase()}[${attr.name}="${CSS.escape(attr.value)}"]`;
      }
      if (active.tagName === "INPUT" && active.selectionStart != null) {
        selStart = active.selectionStart; selEnd = active.selectionEnd;
      }
    }
    listEl.innerHTML = listHtml
      + `<datalist id="subsCategoryList">${cats.map((c) => `<option value="${escHtml(c)}"></option>`).join("")}</datalist>`;
    for (const [id, value] of saved) {
      const el = $(id);
      if (!el) continue;
      if (el.tagName === "INPUT") el.value = value; else el.textContent = value;
    }
    const focusEl = focusSel && listEl.querySelector(focusSel);
    if (focusEl) {
      focusEl.focus();
      if (selStart !== null) focusEl.setSelectionRange(selStart, selEnd);
    }
    listEl.querySelectorAll("[data-indeterminate]").forEach((el) => { el.indeterminate = true; });
    const mode = subsAddOpen ? "add" : subsBulkOpen ? "bulk" : "buttons";
    if (!formOpen || formEl.dataset.mode !== mode) {
      formEl.innerHTML = addAreaHtml;
      formEl.dataset.mode = mode;
    }
    // 只在焦点确实丢了的时候放过去：等网络回来这段时间用户可能已经点到别处了
    const activeNow = document.activeElement;
    if (focusNext && (!activeNow || activeNow === document.body || !activeNow.isConnected)) {
      (box.querySelector(focusNext) || $("subsAddOpenBtn"))?.focus();
    }
  }

  // ---- 订阅管理里的「找内容」：按时间范围 + 话题找单集/文章，找到后能直接读、写成报告 ----
  // 处理过的从各订阅文件夹的记录里读（带小结，可以按意思筛），没处理过的用这一页已经
  // 检查出来的新条目（只有标题）。话题交给模型按意思挑：搜"RSI"也能找到讲递归自我改进的。
  // 话题里写"最近半年""近三个月"这类时间，后端会认出来当时间范围用。
  let subsFilterState = null;   // {busy, error, items, considered, note, query, topic, days, timeSaid, updateError, report}
  const subsFilterChecked = new Set();   // 勾上的条目："订阅id|条目id"（处理过的、没处理的都能勾）
  // 表单里填的东西单独记着：列表读取失败时整块订阅管理会被换成错误提示，下次重建时照原样填回去
  const subsFilterForm = { open: false, days: "30", query: "", model: true, focus: "" };
  const FILTER_DAYS = [["7", "最近一周"], ["30", "最近一个月"], ["90", "最近三个月"],
    ["182", "最近半年"], ["365", "最近一年"], ["0", "全部"]];
  const REPORT_MAX_NOTES = 400;   // 跟笔记洞察一次任务的上限一致

  function subsFilterHtml() {
    const what = IS_PODCAST_SUBS ? "单集" : "内容";
    const days = FILTER_DAYS.some(([v]) => v === subsFilterForm.days) ? subsFilterForm.days : "30";
    return `
      <details class="more subs-filter-area" id="subsFilter" ${subsFilterForm.open ? "open" : ""}>
        <summary>找${what}：按时间和话题找，找到后可以直接读，或者合起来写一份专题报告</summary>
        <div class="subs-filter-row">
          <label for="subsFilterDays" class="hint" style="margin:0">时间</label>
          <select id="subsFilterDays" style="width:auto">
            ${FILTER_DAYS.map(([v, t]) => `<option value="${v}" ${days === v ? "selected" : ""}>${t}</option>`).join("")}
          </select>
          <input type="text" id="subsFilterQuery" aria-label="话题" value="${escHtml(subsFilterForm.query)}"
            placeholder="比如：最近半年 RSI 相关的；写了时间就不看左边的下拉框" />
          <button type="button" id="subsFilterRun">查找</button>
        </div>
        <label class="check-row" style="margin:0"><input type="checkbox" id="subsFilterModel" ${subsFilterForm.model ? "checked" : ""} />
          按意思匹配话题（调用一次模型，用下面「用哪个模型」里的设置；不勾就按关键词找）</label>
        <div id="subsFilterResults" aria-live="polite" tabindex="-1"></div>
      </details>`;
  }

  function subsFilterNewEntries() {
    const out = [];
    for (const sub of subscriptions) {
      if (podcastUpdating.has(sub.id)) continue;
      for (const e of visibleNewEntries(sub)) {
        out.push({ sub_id: sub.id, id: e.id, title: e.title, publish_date: e.publish_date, url: e.url });
      }
    }
    return out;
  }

  async function runSubsFilter() {
    if (subsFilterState?.busy) return;
    const query = $("subsFilterQuery").value.trim();
    const useModel = $("subsFilterModel").checked;
    subsFilterState = { busy: true };
    subsFilterChecked.clear();
    renderSubsFilterResults();
    try {
      const r = await fetch("api/subscriptions/search_updates", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          kind: SUBS_KIND, days: parseInt($("subsFilterDays").value, 10) || 0, query,
          use_model: useModel, new_entries: subsFilterNewEntries(), ...currentBackendConfig(),
        }),
      });
      const d = await r.json().catch(() => ({ error: `服务出错（${r.status}）` }));
      if (!r.ok) throw new Error(d.error || "查找失败");
      subsFilterState = {
        items: d.items || [], considered: d.considered, note: d.note, query,
        topic: d.topic || "", days: d.days || 0, timeSaid: d.time_said || "",
      };
      // 找出来的就是想看的：能写进报告的（处理过、在笔记库里）默认全勾上
      for (const it of subsFilterState.items) {
        if (it.status === "processed" && it.vault_path) subsFilterChecked.add(`${it.sub_id}|${it.id}`);
      }
      // 报告关注点默认就是话题；用户改过的不覆盖
      if (!subsFilterForm.focusEdited) subsFilterForm.focus = subsFilterState.topic;
      // 话题里说了时间：下拉框跟着对上（有对应选项的话），免得两边说的不一样
      if (subsFilterState.timeSaid) {
        const opt = FILTER_DAYS.find(([v]) => +v === subsFilterState.days);
        if (opt) { subsFilterForm.days = opt[0]; $("subsFilterDays").value = opt[0]; }
      }
    } catch (e) {
      subsFilterState = { error: e.message };
    }
    renderSubsFilterResults();
  }

  function filterItemByKey() {
    return new Map((subsFilterState?.items || []).map((it) => [`${it.sub_id}|${it.id}`, it]));
  }

  // 勾选里按用途分开：处理过、在库里的能写报告；没处理的能交给「处理」
  function subsFilterPicked() {
    const byKey = filterItemByKey();
    const out = { report: [], outside: 0, fresh: 0 };
    for (const key of subsFilterChecked) {
      const it = byKey.get(key);
      if (!it) continue;
      if (it.status === "processed") {
        if (it.vault_path) out.report.push(it); else out.outside += 1;
      } else if (!podcastUpdating.has(it.sub_id)) {
        out.fresh += 1;
      }
    }
    return out;
  }

  function readHref(it) {
    if (it.read_path) return `../read/f/${it.read_path.split("/").map(encodeURIComponent).join("/")}`;
    return it.path ? `obsidian://open?path=${encodeURIComponent(it.path)}` : "";
  }

  function filterActionsHtml() {
    const st = subsFilterState;
    const p = subsFilterPicked();
    const hasProcessed = st.items.some((it) => it.status === "processed");
    const hasFresh = IS_PODCAST_SUBS && st.items.some((it) => it.status === "new" && !podcastUpdating.has(it.sub_id));
    if (!hasProcessed && !hasFresh) return "";
    const rep = st.report || {};
    let report = "";
    if (hasProcessed) {
      const warn = [
        p.outside ? `${p.outside} 篇不在笔记库里，写不进报告` : "",
        p.fresh ? `${p.fresh} 期还没处理，不会写进报告` : "",
        p.report.length > REPORT_MAX_NOTES ? `一次最多 ${REPORT_MAX_NOTES} 篇，先少勾一些` : "",
      ].filter(Boolean).join("；");
      const started = rep.jobId
        ? `<p class="hint" style="margin:0">报告已经开始写了，进度和结果在 <a href="${escHtml(rep.url)}" target="_blank" rel="noopener">笔记洞察</a> 里（已在新标签页打开）。</p>`
        : "";
      report = `
        <div class="filter-report">
          <label for="subsFilterFocus" class="hint" style="margin:0">报告关注点</label>
          <input type="text" id="subsFilterFocus" value="${escHtml(subsFilterForm.focus)}"
            placeholder="想让报告回答什么，比如：各家对 RSI 何时到来的判断和分歧" />
          <button type="button" id="subsFilterReport"
            ${p.report.length && p.report.length <= REPORT_MAX_NOTES && !rep.starting ? "" : "disabled"}>
            ${rep.starting ? "正在启动…" : `用勾选的 ${p.report.length} 篇写专题报告`}</button>
          ${warn ? `<p class="hint" style="margin:0;flex-basis:100%">${escHtml(warn)}</p>` : ""}
          ${rep.error ? `<div class="err-box" style="flex-basis:100%">${escHtml(rep.error)}</div>` : ""}
          ${started}
        </div>`;
    }
    const update = hasFresh ? `
        <div class="filter-update">
          <span class="hint" style="margin:0">没处理过的只有标题，先处理才能读、才能写进报告：</span>
          <button type="button" class="secondary" id="subsFilterUpdate" ${p.fresh && !inboxStarting ? "" : "disabled"}>
            ${inboxStarting ? "正在启动…" : `处理勾选的 ${p.fresh} 期`}</button>
        </div>` : "";
    return `<div class="inbox-actions filter-actions">${report}${update}</div>`;
  }

  function renderSubsFilterResults() {
    const el = $("subsFilterResults");
    if (!el) return;
    const st = subsFilterState;
    if (!st) { el.innerHTML = ""; return; }
    if (st.busy) { el.innerHTML = `<p class="hint">正在查找…</p>`; return; }
    if (st.error) { el.innerHTML = `<p class="subs-err">${escHtml(st.error)}</p>`; return; }
    // 查完之后：别处刚开始更新的节目、刚被忽略的条目，不能再勾
    const subById = new Map(subscriptions.map((x) => [x.id, x]));
    const ignored = (it) => (subById.get(it.sub_id)?.ignored_ids || []).includes(it.id);
    st.items = st.items.filter((it) => !(it.status === "new" && ignored(it)));
    const tickable = (it) => it.status === "processed" || (IS_PODCAST_SUBS && !podcastUpdating.has(it.sub_id));
    const since = st.days ? fmtDate8(ymd(new Date(Date.now() - st.days * 86400000))) : "";
    const range = st.timeSaid ? `按「${escHtml(st.timeSaid)}」算，${since} 以来` : since ? `${since} 以来` : "全部时间";
    const topic = st.topic ? `，和「${escHtml(st.topic)}」相关的` : "";
    const head = `<div class="filter-head">
        <p class="hint" style="margin:0">${range}${topic}，找到 <b>${st.items.length}</b> 条`
      + `${st.query && st.topic ? `（范围内共 ${st.considered} 条）` : ""}${st.note ? `；${escHtml(st.note)}` : ""}。`
      + `没处理过的只包括这一页已经检查过的订阅。</p>
        ${st.items.some(tickable) ? `<span class="spacer"></span>
          <button type="button" class="secondary mini" id="subsFilterAll">全选</button>
          <button type="button" class="secondary mini" id="subsFilterNone">全不选</button>` : ""}
      </div>`;
    const rows = st.items.map((it) => {
      const key = `${it.sub_id}|${it.id}`;
      const title = escHtml(it.title || "（无标题）");
      const read = it.status === "processed" ? readHref(it) : "";
      const src = safeHref(it.url);
      const link = read ? `<a href="${escHtml(read)}" target="_blank" rel="noopener" title="在阅读页打开">${title}</a>`
        : src ? `<a href="${src}" target="_blank" rel="noopener" title="打开原链接">${title}</a>` : `<span>${title}</span>`;
      const box = tickable(it)
        ? `<input type="checkbox" data-filter-item="${escHtml(key)}" aria-label="勾选「${title}」" ${subsFilterChecked.has(key) ? "checked" : ""} />`
        : `<span style="width:16px;flex-shrink:0"></span>`;
      const status = it.status !== "new" ? "已处理"
        : podcastUpdating.has(it.sub_id) ? "正在处理" : `<span class="subs-badge">未处理</span>`;
      const meta = [fmtDate8(it.date) || "日期未知", escHtml(it.show), status,
        read && src ? `<a href="${src}" target="_blank" rel="noopener">原链接</a>` : ""].filter(Boolean).join(" · ");
      const detail = it.reason || it.tldr;
      return `
        <div class="inbox-item">
          ${box}
          <div class="inbox-item-body">
            ${link}
            <div class="inbox-item-meta">${meta}${detail ? ` —— ${escHtml(detail)}` : ""}</div>
          </div>
        </div>`;
    }).join("");
    const updateErr = st.updateError ? `<div class="err-box">${escHtml(st.updateError)}</div>` : "";
    el.innerHTML = head + (rows ? `<div class="inbox-list">${rows}</div>` + filterActionsHtml() : "") + updateErr;
  }

  function ymd(d) {
    return `${d.getFullYear()}${String(d.getMonth() + 1).padStart(2, "0")}${String(d.getDate()).padStart(2, "0")}`;
  }

  // 勾选的已处理条目交给「笔记洞察」写专题报告：走它自己的 /notes/api/run，
  // 进度和结果（还能转演示）都在它的页面里看——那边打开时会自动接上最近的报告任务
  async function startFilterReport() {
    const st = subsFilterState;
    if (!st || st.report?.starting) return;
    const notes = subsFilterPicked().report.map((it) => it.vault_path);
    if (!notes.length) return;
    // 先开好标签页再发请求：等请求回来再 window.open 会被浏览器当成弹窗拦掉
    const win = window.open("about:blank", "_blank");
    st.report = { starting: true };
    renderSubsFilterResults();
    try {
      const base = new URL("../notes/", location.href);
      const r = await fetch(new URL("api/run", base), {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ notes, focus: subsFilterForm.focus.trim(), ...currentBackendConfig() }),
      });
      const d = await r.json().catch(() => ({ error: r.status === 404 ? "找不到笔记洞察（要从 Spark 首页启动）" : `服务出错（${r.status}）` }));
      if (!r.ok) throw new Error(d.error || "启动失败");
      const url = new URL(`?job=${encodeURIComponent(d.job_id)}`, base).href;
      st.report = { jobId: d.job_id, url };
      if (win) win.location.href = url;
    } catch (e) {
      if (win) win.close();
      st.report = { error: `写报告没能启动：${e.message}` };
    }
    renderSubsFilterResults();
  }

  function subsFilterSelections() {
    // 只取没处理过的：交给「处理」用
    const byKey = filterItemByKey();
    const bySub = new Map();
    for (const key of subsFilterChecked) {
      const it = byKey.get(key);
      if (!it || it.status !== "new" || podcastUpdating.has(it.sub_id)) continue;
      if (!bySub.has(it.sub_id)) bySub.set(it.sub_id, []);
      bySub.get(it.sub_id).push(it.id);
    }
    return [...bySub.entries()].map(([sub_id, entry_ids]) => ({ sub_id, entry_ids }));
  }

  // 勾选变了只换底下的操作栏；报告关注点输入框正在输入时保住焦点和光标
  function refreshFilterActions() {
    const bar = document.querySelector("#subsFilterResults .filter-actions");
    if (!bar || !subsFilterState?.items) return;
    const focusInput = document.activeElement?.id === "subsFilterFocus";
    const tmp = document.createElement("div");
    tmp.innerHTML = filterActionsHtml();
    bar.replaceWith(tmp.firstElementChild || document.createTextNode(""));
    if (focusInput) $("subsFilterFocus")?.focus();
  }

  // ---- 新内容收件箱 ----
  function inboxKey(subId, entryId) { return `${subId}|${entryId}`; }

  // 这个订阅这次检查出来、而且没被忽略的新条目。在"忽略"之前就发出去的检查，
  // 结果回来时还带着刚忽略的条目——按本地记的忽略列表再筛一遍，不让它们冒回来。
  function visibleNewEntries(sub) {
    const c = subsCheckResults[sub.id];
    if (!c || c.error || !c.new_entries) return [];
    const ignored = new Set(sub.ignored_ids || []);
    return c.new_entries.filter((e) => !ignored.has(e.id));
  }

  function inboxGroups() {
    // [{cat, subs: [{sub, entries}]}]，只保留有新内容的订阅
    const byCat = new Map();
    for (const sub of subscriptions) {
      const entries = visibleNewEntries(sub);
      if (!entries.length) continue;
      const cat = sub.category || "未分类";
      if (!byCat.has(cat)) byCat.set(cat, []);
      byCat.get(cat).push({ sub, entries });
    }
    return [...byCat.entries()]
      .sort((a, b) => a[0].localeCompare(b[0], "zh"))
      .map(([cat, subs]) => ({ cat, subs }));
  }

  function inboxSelections() {
    const out = [];
    for (const g of inboxGroups()) {
      for (const { sub, entries } of g.subs) {
        if (podcastUpdating.has(sub.id)) continue;
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
    delete box.dataset.jobView;
    if (!subsLoaded) { box.innerHTML = `<p class="hint">正在加载订阅列表…</p>`; return; }
    if (subsLoadError) { box.innerHTML = `<p class="subs-err">${escHtml(subsLoadError)}</p>`; return; }
    if (!subscriptions.length) {
      box.innerHTML = `<div class="subs-empty">${SUBS_TEXT.inboxNoSubs}</div>`;
      return;
    }

    const groups = inboxGroups();
    // 正在更新的节目不算进"还有多少新内容"——它们正在处理，也没法再选
    const total = groups.reduce((n, g) => n + g.subs.reduce(
      (m, s) => m + (podcastUpdating.has(s.sub.id) ? 0 : s.entries.length), 0), 0);
    const selected = inboxSelections().reduce((n, s) => n + s.entry_ids.length, 0);
    const failedChecks = subscriptions.filter((s) => subsCheckResults[s.id]?.error);
    const checkedAt = subsLastCheckAllAt ? `上次检查 ${fmtClock(subsLastCheckAllAt)}` : "";
    const autoN = autoSubscriptions().length;
    const manualN = subscriptions.length - autoN;
    const manualNote = manualN ? ` · 另有 ${manualN} 个订阅不自动检查（在订阅管理里手动点「检查」）` : "";

    const head = `
      <div class="inbox-head">
        <span>${subsChecking ? `正在检查 ${autoN} 个订阅……`
          : !autoN && !total ? `${subscriptions.length} 个订阅都没开自动检查`
          : `${total} ${SUBS_TEXT.newThings}${checkedAt ? ` · ${checkedAt}` : ""}${manualNote}`}</span>
        <span class="spacer"></span>
        ${total ? `<button type="button" class="secondary mini" id="inboxAll">全选</button>
        <button type="button" class="secondary mini" id="inboxNone">全不选</button>` : ""}
        <button type="button" class="secondary mini" id="inboxRecheck" ${subsChecking || !autoN ? "disabled" : ""}>${subsChecking ? "检查中…" : "重新检查"}</button>
      </div>`;

    let list;
    const anyUpdating = groups.some((g) => g.subs.some((x) => podcastUpdating.has(x.sub.id)));
    if (!total && !anyUpdating) {
      list = subsChecking
        ? `<div class="subs-empty">${SUBS_TEXT.checkingHint}</div>`
        : autoN
          ? `<div class="subs-empty">${SUBS_TEXT.noneNew}</div>`
          : `<div class="subs-empty">还没有设为自动检查的订阅。到「订阅管理」里勾上「自动检查」，或者逐个点「检查」。</div>`;
    } else {
      list = `<div class="inbox-list">` + groups.map((g) => `
        <div class="inbox-cat">${escHtml(g.cat)}</div>
        ${g.subs.map(({ sub, entries }) => {
          const updating = podcastUpdating.get(sub.id);
          if (updating) {
            const rest = updating.count == null ? 0 : entries.length - updating.count;
            return `
          <div class="inbox-sub">
            <strong class="inbox-sub-name">${escHtml(sub.name)}</strong>
            <span class="hint" style="margin:0">${updating.count == null ? "正在更新" : `正在更新 ${updating.count} 期`}——进度在页面下方的任务列表里，跑完会自动重新检查${rest > 0 ? `；另外 ${rest} 期这次没选，等跑完再处理` : ""}</span>
          </div>`;
          }
          const on = entries.filter((e) => !inboxUnchecked.has(inboxKey(sub.id, e.id))).length;
          return `
          <div class="inbox-sub">
            <input type="checkbox" data-inbox-sub="${escHtml(sub.id)}" aria-label="全选「${escHtml(sub.name)}」的 ${entries.length} 条"
              ${on === entries.length ? "checked" : ""} ${on > 0 && on < entries.length ? 'data-indeterminate="1"' : ""} />
            <strong class="inbox-sub-name">${escHtml(sub.name)}</strong>
            <span class="hint" style="margin:0">${entries.length} ${SUBS_TEXT.unit}</span>
            <span class="spacer"></span>
            <button type="button" class="secondary mini" data-inbox-ignore-sub="${sub.id}">全部忽略</button>
          </div>
          ${entries.map((e) => {
            const key = inboxKey(sub.id, e.id);
            const href = safeHref(e.url);
            const title = escHtml(e.title || "（无标题）");
            return `
            <div class="inbox-item">
              <input type="checkbox" data-inbox-item="${escHtml(key)}" aria-label="${title}" ${inboxUnchecked.has(key) ? "" : "checked"} />
              <div class="inbox-item-body">
                ${href ? `<a href="${href}" target="_blank" rel="noopener">${title}</a>` : `<span>${title}</span>`}
                <div class="inbox-item-meta">${fmtDate8(e.publish_date)}${e.last_error ? ` <span class="inbox-last-err">· 上次失败：${escHtml(e.last_error)}</span>` : ""}</div>
              </div>
              <button type="button" class="secondary mini" data-inbox-ignore="${escHtml(key)}" aria-label="忽略「${title}」">忽略</button>
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
        <span id="inboxSelInfo">${inboxSelInfo(selected)}</span>
        <span class="spacer"></span>
        <label for="inboxSummaryLength" class="hint" style="margin:0">小结篇幅</label>
        <select id="inboxSummaryLength" style="width:auto">
          <option value="short" ${inboxSummaryLength === "short" ? "selected" : ""}>简洁</option>
          <option value="medium" ${inboxSummaryLength === "medium" ? "selected" : ""}>标准</option>
          <option value="long" ${inboxSummaryLength === "long" ? "selected" : ""}>详细</option>
        </select>
        <button type="button" id="inboxRun" ${selected && !inboxStarting ? "" : "disabled"}>${inboxStarting ? "正在启动…" : IS_PODCAST_SUBS ? "更新选中的单集" : "生成简报"}</button>
      </div>
      <p class="hint">${IS_PODCAST_SUBS
        ? "每期出逐期小结和文字记录，放进节目自己的文件夹（跟在「临时链接」里处理同一个节目是同一个文件夹，以前处理过的单集不会重做），然后刷新节目总结。每个节目一个任务，进度在页面下方。用哪个模型在下面设置。"
        : "每条存成一篇笔记（小结 + 原文），放在「信息跟进/订阅名/」；再出一份本批简报放在「信息跟进/简报/」。用哪个模型在下面设置。"}</p>
      <div class="err-box" id="inboxErr">${escHtml(inboxStartError)}</div>` : "";

    box.innerHTML = head + list + failedHtml + actions;
    box.querySelectorAll("[data-indeterminate]").forEach((el) => { el.indeterminate = true; });
  }

  function inboxSelInfo(selected) {
    if (IS_PODCAST_SUBS) {
      const shows = inboxSelections().length;
      return `已选 <b>${selected}</b> 期${selected ? ` · 预计 ${selected + shows} 次模型调用（每期一次小结 + 每个节目刷新一次节目总结）` : ""}`;
    }
    return `已选 <b>${selected}</b> 条${selected ? ` · 预计 ${selected + 1} 次模型调用（每条一次小结 + 一次简报）` : ""}`;
  }

  // 勾选变化只改动受影响的那几个控件，不整块重画——整块重画会丢掉键盘焦点和滚动位置。
  function syncInboxChecks() {
    const box = $("inboxBox");
    for (const g of inboxGroups()) {
      for (const { sub, entries } of g.subs) {
        const on = entries.filter((e) => !inboxUnchecked.has(inboxKey(sub.id, e.id))).length;
        const subBox = box.querySelector(`[data-inbox-sub="${CSS.escape(sub.id)}"]`);
        if (subBox) {
          subBox.checked = on === entries.length;
          subBox.indeterminate = on > 0 && on < entries.length;
        }
        entries.forEach((e) => {
          const key = inboxKey(sub.id, e.id);
          const el = box.querySelector(`[data-inbox-item="${CSS.escape(key)}"]`);
          if (el) el.checked = !inboxUnchecked.has(key);
        });
      }
    }
    const selected = inboxSelections().reduce((n, s) => n + s.entry_ids.length, 0);
    if ($("inboxSelInfo")) $("inboxSelInfo").innerHTML = inboxSelInfo(selected);
    if ($("inboxRun")) $("inboxRun").disabled = !selected || inboxStarting;
  }

  function renderInboxJob(box) {
    const j = inboxJob;
    const pct = j.total ? Math.round((j.current / j.total) * 100) : 0;
    if (!j.done) {
      // 运行中每 1.2 秒刷新一次：只更新文字、进度条和日志，不重建按钮——重建会吞掉
      // 恰好落在刷新瞬间的"停止"点击，日志也会被拉回底部，没法往上翻。
      if (box.dataset.jobView !== j.id) {
        box.innerHTML = `
          <div class="inbox-head"><span id="inboxJobText"></span><span class="spacer"></span>
            <button type="button" class="secondary mini" id="inboxStop"></button></div>
          <p class="hint" id="inboxJobNotice" role="status" hidden></p>
          <div class="progress-bar"><div id="inboxJobBar"></div></div>
          <div class="log-box" id="inboxLog" aria-live="off"></div>`;
        box.dataset.jobView = j.id;
      }
      $("inboxJobText").textContent = `正在处理 ${j.current}/${j.total} 条……`;
      $("inboxJobNotice").hidden = !j.notice;
      $("inboxJobNotice").textContent = j.notice || "";
      $("inboxJobBar").style.width = `${pct}%`;
      $("inboxStop").disabled = !!j.stopping;
      $("inboxStop").textContent = j.stopping ? "正在停止…" : "停止";
      const log = $("inboxLog");
      const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 24;
      const text = (j.log || []).join("\n");
      if (log.textContent !== text) {
        log.textContent = text;
        if (atBottom) log.scrollTop = log.scrollHeight;
      }
      return;
    }
    delete box.dataset.jobView;
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
      <div class="inbox-head"><strong>${j.notice ? (r.brief_path ? "另一批的简报" : "另一批的处理结果") : (r.brief_path ? "本批简报" : "处理结果")}</strong><span class="spacer"></span>
        <button type="button" class="mini" id="inboxDone">完成</button></div>
      ${j.notice ? `<p class="hint">这是另一批的结果，不是你刚才选的那些；那一批没处理到的仍在新内容里，点「完成」后可以重新勾选、生成简报。</p>` : ""}${summary}${path}${failedHtml}
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
      if (sub) sub.ignored_ids = [...new Set([...(sub.ignored_ids || []), ...entryIds])];
      renderTrack();
    } catch (e) {
      alert(e.message);
    }
  }

  const INBOX_CONFIRM_OVER = 30;

  // Podcast 跟进：每个选中的节目各启动一个跟「临时链接」一样的处理任务（后端按订阅
  // 的节目文件夹和来源拼好参数，只处理选中的那几期），卡片出现在页面下方的任务列表，
  // 跑完后自动重新检查这个节目。
  async function startPodcastUpdate(fromFilter = false) {
    // fromFilter：从「筛选更新」的结果里勾的；否则是新单集页里勾的
    const selections = fromFilter ? subsFilterSelections() : inboxSelections();
    if (!selections.length || inboxStarting) return;
    const count = selections.reduce((n, s) => n + s.entry_ids.length, 0);
    if (count > INBOX_CONFIRM_OVER
        && !confirm(`这次要处理 ${count} 期，会调用大约 ${count + selections.length} 次模型。确定继续？（可以先点「全不选」，只勾想看的）`)) {
      return;
    }
    const cfg = currentBackendConfig();
    const options = {
      ...cfg,
      summary_length: inboxSummaryLength,
      max_transcript_chars: parseMaxTranscriptChars(),
      overall_model: $("overallModel").value.trim(),
      lang_prefs: $("langPrefs").value.trim() || "en",
      regenerate_summary: true,
    };
    inboxStarting = true;
    if (fromFilter) { if (subsFilterState) subsFilterState.updateError = ""; renderSubsFilterResults(); }
    else inboxStartError = "";
    renderInbox();
    const errors = [];
    for (const sel of selections) {
      const sub = subscriptions.find((x) => x.id === sel.sub_id);
      if (!sub) continue;
      try {
        const r = await fetch(`api/subscriptions/${sel.sub_id}/update`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ...options, entry_ids: sel.entry_ids }),
        });
        const d = await r.json();
        if (!r.ok) {
          throw new Error(r.status === 409 ? "这个节目已经有任务在跑，等它结束后再更新" : (d.error || "启动失败"));
        }
        // 卡片上「重试失败项」「生成主题总结」这些按钮要用的参数：跟临时链接处理
        // 同一个节目时一样，节目名 + 上一级目录定位到同一个节目文件夹
        const payload = {
          ...buildRunPayload([]),
          ...options,
          summit_title: d.summit_title,
          output_dir: d.output_dir,
          source_url: sub.url,
          content_type: "series",
          do_summary: true,
          skip_existing: true,
          do_speaker_label: false,
          do_speech_script: false,
          agenda_order_map: {},
        };
        const task = attachTask(d.job_id, sub.name, payload);
        podcastUpdating.set(sub.id, { jobId: d.job_id, count: d.count });
        task.onDone = () => finishPodcastUpdate(sub.id);
      } catch (e) {
        errors.push(`${sub.name}：${e.message}`);
      }
    }
    inboxStarting = false;
    if (fromFilter) {
      // 启动成功的节目：勾选清掉（结果里它们会显示成「正在更新」）；启动失败的保留勾选，
      // 错误写在筛选面板里——用户就在这儿点的，不能只写到另一个标签页
      const byKey = filterItemByKey();
      for (const key of [...subsFilterChecked]) {
        const it = byKey.get(key);
        if (it?.status === "new" && podcastUpdating.has(it.sub_id)) subsFilterChecked.delete(key);
      }
      if (subsFilterState) subsFilterState.updateError = errors.join("；");
      renderSubsFilterResults();
      $("subsFilterResults")?.focus();   // 被点的按钮已经重画没了，焦点放回结果区
    } else {
      inboxStartError = errors.join("；");
      renderSubsFilterResults();   // 筛选面板里的按钮状态也跟着刷新
    }
    renderInbox();
  }

  async function startInboxRun() {
    const selections = inboxSelections();
    if (!selections.length) return;
    const count = selections.reduce((n, s) => n + s.entry_ids.length, 0);
    if (count > INBOX_CONFIRM_OVER
        && !confirm(`这次要处理 ${count} 条，会调用 ${count + 1} 次模型。确定继续？（可以先点「全不选」，只勾想看的）`)) {
      return;
    }
    if (inboxStarting || inboxJob) return;
    const cfg = currentBackendConfig();
    inboxStarting = true;
    inboxStartError = "";
    renderInbox();
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
      if (r.status === 409 && d.active_track_job_id) {
        // 另一批信息跟进还在跑（比如刷新前启动的那批）：接上它的进度，但要说清楚
        // 这回选的没开始——那一批不一定包含它们，等它结束后还得再点一次
        inboxJob = { id: d.active_track_job_id, log: [], current: 0, total: 0, done: false,
                     notice: `已经有一批在处理，下面显示的是那一批的进度；你这次选的 ${count} 条还没开始，等它结束后再点「生成简报」。` };
      } else if (!r.ok) {
        throw new Error(d.error || "启动失败");
      } else {
        inboxJob = { id: d.job_id, log: [], current: 0, total: count, done: false };
      }
      pollInboxJob();
    } catch (e) {
      inboxStartError = e.message;
    } finally {
      inboxStarting = false;
      renderInbox();
    }
  }

  // 页面刷新后接上还在跑的那批：不然看不到进度、也没有停止按钮，再点生成只会得到"文件夹被占用"
  async function attachRunningInboxJob() {
    try {
      const r = await fetch("api/track/jobs");
      const d = await r.json();
      const running = r.ok && (d.jobs || [])[0];
      if (!running || inboxJob) return;
      inboxJob = { id: running.job_id, log: [], current: running.current, total: running.total, done: false };
      renderInbox();
      pollInboxJob();
    } catch (e) { /* 连不上就算了，不影响检查新内容 */ }
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
          if (d.stop_requested) job.stopping = true;
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
      syncInboxChecks();
    } else if (t.dataset.inboxSub) {
      const sub = subscriptions.find((s) => s.id === t.dataset.inboxSub);
      (sub ? visibleNewEntries(sub) : []).forEach((en) => {
        const key = inboxKey(t.dataset.inboxSub, en.id);
        t.checked ? inboxUnchecked.delete(key) : inboxUnchecked.add(key);
      });
      syncInboxChecks();
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
      const ids = (sub ? visibleNewEntries(sub) : []).map((en) => en.id);
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
      syncInboxChecks();
    } else if (t.closest("#inboxRecheck")) {
      checkAllSubscriptions();
    } else if (t.closest("#inboxRun")) {
      if (IS_PODCAST_SUBS) startPodcastUpdate(); else startInboxRun();
    } else if (t.closest("#inboxStop")) {
      if (!inboxJob || inboxJob.stopping) return;
      const job = inboxJob;
      job.stopping = true;
      renderInbox();
      fetch(`api/track/stop/${job.id}`, { method: "POST" })
        .then((r) => { if (!r.ok) throw new Error(); })
        .catch(() => {
          // 停止请求没送到：把按钮还回去，让人能再点一次，而不是一直显示"正在停止…"
          job.stopping = false;
          if (inboxJob === job) renderInbox();
        });
    } else if (t.closest("#inboxDone")) {
      inboxJob = null;
      renderInbox();
      checkAllSubscriptions();
    }
  });

  $("subsBox").addEventListener("compositionstart", () => { subsComposing = true; });
  $("subsBox").addEventListener("compositionend", () => {
    subsComposing = false;
    // 晚一拍再画：有的浏览器（WebKit）compositionend 在最后那次 input 之前触发，
    // 这时就把输入框换掉，刚上屏的字可能丢或重复
    if (subsRenderPending) {
      setTimeout(() => {
        if (subsComposing || !subsRenderPending) return;
        subsRenderPending = false;
        renderSubs();
      }, 0);
    }
  });
  $("subsBox").addEventListener("keydown", (e) => {
    // Safari 里确认输入法候选的那次回车，keydown 在 compositionend 之后、isComposing 已经是 false，
    // 只能靠 keyCode 229 认出来
    if (e.key === "Enter" && e.target.id === "subsFilterQuery" && !e.isComposing && e.keyCode !== 229) {
      e.preventDefault();
      runSubsFilter();
    }
  });
  $("subsBox").addEventListener("input", (e) => {
    if (e.target.id === "subsFilterQuery") subsFilterForm.query = e.target.value;
    if (e.target.id === "subsFilterFocus") { subsFilterForm.focus = e.target.value; subsFilterForm.focusEdited = true; }
  });
  $("subsBox").addEventListener("toggle", (e) => {
    if (e.target.id === "subsFilter") subsFilterForm.open = e.target.open;
  }, true);   // toggle 不冒泡，要在捕获阶段接
  $("subsBox").addEventListener("change", (e) => {
    const t = e.target;
    if (t.id === "subsFilterDays") subsFilterForm.days = t.value;
    else if (t.id === "subsFilterModel") subsFilterForm.model = t.checked;
    if (t.dataset.filterItem) {
      // 只重画底下的操作栏，不整块重画（重画会丢焦点）
      if (t.checked) subsFilterChecked.add(t.dataset.filterItem); else subsFilterChecked.delete(t.dataset.filterItem);
      refreshFilterActions();
    } else if (t.dataset.subAuto) {
      setAutoCheck([t.dataset.subAuto], t.checked);
    } else if (t.dataset.catAuto !== undefined) {
      const ids = subscriptions.filter((s) => (s.category || "未分类") === t.dataset.catAuto).map((s) => s.id);
      if (ids.length) setAutoCheck(ids, t.checked);
    }
  });

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
      subsFocusNext = `#subEditName-${CSS.escape(id)}`;
      renderSubs();
    } else if ((id = t.closest("[data-sub-save]")?.dataset.subSave) !== undefined) {
      saveSubscriptionEdit(id);
    } else if ((id = t.closest("[data-sub-cancel-edit]")?.dataset.subCancelEdit) !== undefined) {
      subsEditingId = null;
      subsFocusNext = `button[data-sub-edit="${CSS.escape(id)}"]`;
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
    } else if (t.closest("#subsFillProcessed")) {
      fillProcessedPodcasts();
    } else if (t.closest("#subsFilterRun")) {
      runSubsFilter();
    } else if (t.closest("#subsFilterUpdate")) {
      startPodcastUpdate(true);
    } else if (t.closest("#subsFilterReport")) {
      startFilterReport();
    } else if (t.closest("#subsFilterAll") || t.closest("#subsFilterNone")) {
      const all = !!t.closest("#subsFilterAll");
      for (const cb of document.querySelectorAll("#subsFilterResults [data-filter-item]")) {
        cb.checked = all;
        if (all) subsFilterChecked.add(cb.dataset.filterItem); else subsFilterChecked.delete(cb.dataset.filterItem);
      }
      refreshFilterActions();
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
      $(tab).setAttribute("aria-selected", String(tab === which));
      $(tab).tabIndex = tab === which ? 0 : -1;  // 一组 tab 只占一个 Tab 键位，组内用方向键切
      $(box).classList.toggle("hidden", tab !== which);
    }
    if (which === "tabInbox") {
      $("trackBackendHost").appendChild(backendPanel);
      backendTitle.innerHTML = `<span class="num">2</span>用哪个模型`;
    } else {
      backendHome.parentNode.insertBefore(backendPanel, backendHome.nextSibling);
      backendTitle.innerHTML = backendTitleHome;
    }
    updateBackendVisibility();
  }
  $("tabInbox").addEventListener("click", () => showTrackTab("tabInbox"));
  $("tabSubs").addEventListener("click", () => showTrackTab("tabSubs"));
  $("tabLinkMode").addEventListener("click", () => showTrackTab("tabLinkMode"));
  $("trackTabs").addEventListener("keydown", (e) => {
    const tabs = ["tabInbox", "tabSubs", "tabLinkMode"];
    const i = tabs.indexOf(document.activeElement?.id);
    if (i < 0) return;
    const next = { ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: tabs.length - 1 }[e.key];
    if (next === undefined) return;
    e.preventDefault();
    const id = tabs[(next + tabs.length) % tabs.length];
    showTrackTab(id);
    $(id).focus();
  });

  function initTrackSubscriptions() {
    $("trackTabs").classList.remove("hidden");
    // 只有信息跟进模式有这排 tab，linkModeBox 这时才算其中一个 tab 面板；
    // 其它模式下它就是页面正文，不能让读屏读成一个隐藏 tab 的面板
    $("linkModeBox").setAttribute("role", "tabpanel");
    $("linkModeBox").setAttribute("aria-labelledby", "tabLinkMode");
    showTrackTab("tabInbox");
    $("tabInbox").textContent = SUBS_TEXT.tabInbox;
    $("trackTabs").setAttribute("aria-label", MODE_TEXT[LOCKED].title);
    loadSubscriptions().then(() => {
      // 信息跟进的「生成简报」是一个页面级的批次，刷新后要接上；Podcast 的更新是
      // 普通任务卡片，restoreTasks() 已经接上了
      if (!IS_PODCAST_SUBS) attachRunningInboxJob();
      checkAllSubscriptions();
    });
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
      const keysDir = d.keys_dir || "~/.spark/keys";
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
    // 挪到「新内容」标签页时一定要调模型（每条都要小结），不看临时链接那边的勾选
    const inInbox = $("trackBackendHost").contains($("backendField"));
    const needsLLM = inInbox || $("doSummary").checked || $("doSpeakerLabel").checked || $("doSpeechScript").checked;
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
        const task = attachTask(j.job_id, j.summit_title || "（恢复的任务）", payload, { restored: true });
        if (j.done) task.done = true;
      });
      linkPodcastTasks();
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
      fetch(`api/simple_job_stop/${jobId}`, { method: "POST" })
        .then((sr) => { if (!sr.ok) throw new Error(); })
        .catch(() => { stopBtn.disabled = false; });   // 没送到就让人再点一次
    };
    if (stopBtn) {
      stopBtn.style.display = "inline-block";
      stopBtn.disabled = false;
      stopBtn.addEventListener("click", onStopClick);
    }
    try {
      let failures = 0;
      while (true) {
        await new Promise((resolve) => setTimeout(resolve, 800));
        let status;
        try {
          const sr = await fetch(`api/simple_job_status/${jobId}`);
          status = await sr.json();
          if (sr.status === 404) throw Object.assign(new Error("找不到这个任务了（服务可能重启过）"), { fatal: true });
          if (!sr.ok) throw new Error(status.error || "查询任务状态失败");
        } catch (e) {
          // 偶发的网络抖动不算失败，任务还在后台跑；连续多次才放弃
          failures += 1;
          if (e.fatal || failures >= 10) throw e;
          continue;
        }
        failures = 0;
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
    // gone=true：服务端确实没有这个任务了（服务重启过）——节目可以放开重新更新；
    // 只是连续查不到进度的话任务可能还在跑，先不放开，免得又启动一个撞上 409
    const markLost = (msg, gone) => {
      clearInterval(task.pollTimer);
      task.done = true;
      if (gone && task.onDone) { const f = task.onDone; task.onDone = null; f(); }
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
        markLost("服务重启过，找不到这个任务的进度了。已写进输出目录的内容不受影响，重新「开始生成」会跳过已完成的部分。", true);
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
        task.done = true;
        if (task.onDone) { const f = task.onDone; task.onDone = null; f(); }
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
  if (HAS_SUBS) initTrackSubscriptions();
  loadEnv();
  renderRecentUrls();
  updateOverallModelOptions();
  restoreTasks();
})();
