// 复盘页的「主题」：标签词云、全部标签、一个标签下的文章、合并标签。数据来自阅读页的 /read/api/tags*（core/tags.py）。
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const API = "/read/api/tags";
  const MAX_WORDS = 63, CLOUD_MIN = 3, SIZE_MIN = 13, SIZE_MAX = 34;
  const TYPE_TEXT = { clip: "剪藏", paper: "论文", episode: "播客", talk: "演讲", track: "信息" };
  const KIND_TEXT = { tech: "技术和方法", org: "公司、产品、人物", biz: "产业、投资、社会" };

  let tags = [];          // 全部标签（min=1），篇数从多到少
  let aliases = {};
  let current = "";       // 选中的标签
  let shownWidth = 0;

  const el = (tag, props, ...kids) => { const e = Object.assign(document.createElement(tag), props); e.append(...kids); return e; };

  async function call(path, body) {
    const r = await fetch(API + path, body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : undefined);
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.error || `请求失败（${r.status}）`);
    return d;
  }

  // ---------------------------------------------------------------- 词云
  const ctx = document.createElement("canvas").getContext("2d");
  function measure(text, size, weight) {
    ctx.font = `${weight} ${size}px ${getComputedStyle($("cloud")).fontFamily}`;
    return ctx.measureText(text).width;
  }

  function drawCloud() {
    const box = $("cloud");
    const width = box.clientWidth;
    if (!width) return;   // 页签没打开时宽度是 0，打开时 ResizeObserver 会再来
    shownWidth = width;
    const top = tags.filter((t) => t.count >= CLOUD_MIN).slice(0, MAX_WORDS);
    $("cloudEmpty").hidden = top.length > 0;
    const lo = Math.sqrt(top.length ? top[top.length - 1].count : 0), hi = Math.sqrt(top.length ? top[0].count : 0);
    const words = top.map((t, i) => ({
      text: t.tag,
      size: hi > lo ? SIZE_MIN + (SIZE_MAX - SIZE_MIN) * (Math.sqrt(t.count) - lo) / (hi - lo) : (SIZE_MIN + SIZE_MAX) / 2,
      weight: i < Math.ceil(top.length / 8) ? 500 : 400,
    }));
    const { items, height } = window.SparkCloud.layout(words, width, measure);
    const byTag = new Map(top.map((t, i) => [t.tag, { ...t, rank: i }]));
    box.style.height = height + "px";
    box.replaceChildren(...items.map((it) => {
      const t = byTag.get(it.text);
      const b = el("button", { type: "button", className: "cw", textContent: it.text });
      b.dataset.kind = KIND_TEXT[t.kind] ? t.kind : "tech";
      b.dataset.tier = String(Math.floor(t.rank * 3 / top.length) + 1);
      b.dataset.tag = it.text;
      b.setAttribute("aria-label", `${t.tag}，${t.count} 篇`);
      b.setAttribute("aria-pressed", String(it.text === current));
      Object.assign(b.style, { left: it.left + "px", top: it.top + "px", width: it.w + "px", height: it.h + "px",
                               fontSize: it.size + "px", fontWeight: it.weight });
      b.addEventListener("click", () => openTag(t.tag));
      return b;
    }));
  }
  new ResizeObserver(() => { if ($("cloud").clientWidth !== shownWidth) drawCloud(); }).observe($("cloud"));

  // ---------------------------------------------------------------- 全部标签
  function drawList() {
    const q = $("tagFilter").value.trim().toLowerCase();
    const rows = tags.filter((t) => !q || t.tag.toLowerCase().includes(q));
    $("tagList").replaceChildren(...rows.map((t) => {
      const b = el("button", { type: "button", className: "tag-row" }, el("span", { textContent: t.tag }), el("small", { textContent: `${t.count} 篇` }));
      b.dataset.tag = t.tag;
      b.setAttribute("aria-pressed", String(t.tag === current));
      b.addEventListener("click", () => openTag(t.tag));
      return el("li", {}, b);
    }));
    $("tagListEmpty").hidden = rows.length > 0;
    $("tagCount").textContent = tags.length ? `共 ${tags.length} 个，按篇数从多到少` : "";
    $("tagNames").replaceChildren(...tags.map((t) => el("option", { value: t.tag })));
  }
  $("tagFilter").addEventListener("input", drawList);

  // ---------------------------------------------------------------- 一个标签下的文章
  function markCurrent() {
    document.querySelectorAll(".cw, .tag-row").forEach((b) => {
      b.setAttribute("aria-pressed", String(b.dataset.tag === current));
    });
  }

  async function openTag(tag, quiet) {
    current = tag;
    markCurrent();
    $("itemsHint").hidden = true;
    $("itemsBody").hidden = false;
    $("itemsTitle").textContent = tag;
    if (!quiet) { $("opMsg").textContent = ""; $("opMsg").className = "hint"; $("mergeForm").hidden = $("kindForm").hidden = true; }
    const info = tags.find((t) => t.tag === tag);
    $("kindSelect").value = info && KIND_TEXT[info.kind] ? info.kind : "tech";
    try {
      const d = await call(`/items?tag=${encodeURIComponent(tag)}`);
      if (current !== tag) return;
      $("itemsCount").textContent = `${d.items.length} 篇，新的在前`;
      $("items").replaceChildren(...d.items.map((it) => {
        const a = el("a", { href: "/read/f/" + it.path.split("/").map(encodeURIComponent).join("/"), textContent: it.title || it.path });
        const meta = [it.source, it.date, TYPE_TEXT[it.type] || it.type].filter(Boolean).join("　");
        return el("li", {}, a, el("small", { textContent: meta }));
      }));
      if (window.matchMedia("(max-width: 760px)").matches && !quiet) $("itemsPanel").scrollIntoView({ block: "nearest" });
    } catch (e) {
      $("itemsCount").textContent = "";
      $("items").replaceChildren(el("li", { className: "hint err", textContent: e.message }));
    }
  }

  // ---------------------------------------------------------------- 合并、改类别、撤销
  function say(text, err) { $("opMsg").textContent = text; $("opMsg").className = err ? "hint err" : "hint"; }

  async function refresh() {
    const [t, a] = await Promise.all([call("?min=1"), call("/aliases")]);
    tags = t.tags; aliases = a.aliases;
    drawList(); drawCloud(); drawAliases();
  }

  function drawAliases() {
    const rows = Object.entries(aliases).sort((x, y) => x[1].localeCompare(y[1], "zh") || x[0].localeCompare(y[0], "zh"));
    $("aliasEmpty").hidden = rows.length > 0;
    $("aliasList").replaceChildren(...rows.map(([src, dst]) => {
      const b = el("button", { type: "button", className: "link-btn", textContent: "撤销" });
      b.setAttribute("aria-label", `撤销「${src} → ${dst}」`);
      b.addEventListener("click", async () => {
        try { await call("/alias", { src, dst: "" }); await refresh(); if (current) openTag(current, true); } catch (e) { $("aliasMsg").textContent = e.message; }
      });
      return el("li", {}, el("span", { textContent: `${src} → ${dst}` }), b);
    }));
  }

  $("mergeOpen").addEventListener("click", () => { $("kindForm").hidden = true; $("mergeForm").hidden = !$("mergeForm").hidden; if (!$("mergeForm").hidden) $("mergeTo").focus(); });
  $("kindOpen").addEventListener("click", () => { $("mergeForm").hidden = true; $("kindForm").hidden = !$("kindForm").hidden; if (!$("kindForm").hidden) $("kindSelect").focus(); });
  $("mergeCancel").addEventListener("click", () => { $("mergeForm").hidden = true; });
  $("kindCancel").addEventListener("click", () => { $("kindForm").hidden = true; });

  $("mergeForm").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const dst = $("mergeTo").value.trim();
    if (!dst) { say("先填要并入的标签", true); return; }
    const src = current;
    try {
      await call("/alias", { src, dst });
      $("mergeForm").hidden = true; $("mergeTo").value = "";
      await refresh();
      await openTag(dst);
      say(`已把「${src}」并入「${dst}」。`);
    } catch (e) { say(e.message, true); }
  });

  $("kindForm").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    try {
      await call("/kind", { tag: current, kind: $("kindSelect").value });
      $("kindForm").hidden = true;
      await refresh();
      say(`类别改成「${KIND_TEXT[$("kindSelect").value]}」了。`);
      markCurrent();
    } catch (e) { say(e.message, true); }
  });

  // ---------------------------------------------------------------- 入口
  let loaded = false;
  window.SparkTopics = {
    async show() {
      if (loaded) { drawCloud(); return; }
      loaded = true;
      try { await refresh(); } catch (e) { loaded = false; $("cloudNote").textContent = `标签拿不到：${e.message}`; $("cloudNote").className = "hint err"; }
    },
  };
})();
