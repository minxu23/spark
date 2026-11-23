// 清理工具页（/cleanup）：接口见 spark.py 的 /api/cleanup*，判断和执行在 core/cleanup.py。
// 所有文字都用 textContent 放进去，文件名、文档原文不当 HTML。
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  let state = null;
  let choices = {};      // 拿不准的一张：keep / clean / app
  let merges = {};       // 重复图片组：sha256 → true
  let scanId = "";
  let polling = null;

  function el(tag, attrs, ...kids) {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (k === "class") e.className = v;
      else if (k === "text") e.textContent = v;
      else e.setAttribute(k, v);
    }
    for (const k of kids) if (k) e.append(k);
    return e;
  }

  function size(n) {
    if (!n) return "0 B";
    const u = ["B", "KB", "MB", "GB"];
    let i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return (i ? n.toFixed(1) : n) + " " + u[i];
  }

  function thumb(path) {
    const img = el("img", { class: "thumb", alt: "", loading: "lazy" });
    if (!/\.svg$/i.test(path)) img.src = "/api/cleanup/image?path=" + encodeURIComponent(path);
    return img;
  }

  async function api(url, body) {
    const opt = body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {};
    const r = await fetch(url, opt);
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || ("出错了（" + r.status + "）"));
    return data;
  }

  function renderJob() {
    const job = state.job || {};
    const busy = !!job.running;
    for (const id of ["scan", "apply", "runRegular"]) $(id).disabled = busy;
    $("job").className = "hint" + (job.error ? " err" : "");
    $("job").textContent = busy ? ("正在" + ({ scan: "扫描", apply: "清理", regular: "做定期清理" }[job.kind] || "跑") + "：" + (job.phase || "开始"))
      : (job.error || job.result || "");
    if (busy && !polling) polling = setInterval(load, 1500);
    if (!busy && polling) { clearInterval(polling); polling = null; }
  }

  function renderState() {
    const bad = state.kept_lost || state.keys_lost;
    $("stateBox").classList.toggle("hidden", !bad);
    if (!bad) return;
    const parts = [state.state_problem || "记录读不出来，换了一份空的重新记。"];
    if (state.kept_lost) parts.push("你选了留的图片名单丢了：定期清理先停着，点下面的按钮确认后再做；之前选了留的图片会重新列进拿不准。");
    if (state.keys_lost) parts.push("手机交来的清理分不清做没做过：先不做，在这里点「开始扫描」重新扫描一次之后，手机上再交。");
    $("stateText").textContent = parts.join(" ");
    $("confirmState").classList.toggle("hidden", !state.kept_lost);
  }

  function renderEdits() {
    const p = state.edits_problem;
    $("editsBox").classList.toggle("hidden", !p);
    if (p) $("editsText").textContent = p;
  }

  function renderRegular() {
    $("schedule").value = state.config.schedule;
    const parts = [];
    if (state.last_success) parts.push("上次定期清理：" + new Date(state.last_success * 1000).toLocaleString());
    else parts.push("还没做过定期清理（第一次在开着 Spark 一个周期后做）");
    if (state.pending) parts.push("待确认 " + state.pending + " 张");
    if (state.kept) parts.push("你选了留的 " + state.kept + " 张以后不再清");
    if (state.last_error) parts.push("上次出错：" + state.last_error);
    $("regularInfo").textContent = parts.join("；");
  }

  function evidenceBlock(item) {
    const box = el("details");
    box.append(el("summary", { text: "文件名出现在哪" }));
    for (const e of item.evidence || []) {
      if (e.kind === "unreadable") {
        box.append(el("div", { class: "meta", text: "读不出来的文档：" + (e.docs || []).join("、") }));
        continue;
      }
      box.append(el("div", { class: "meta", text: e.doc }));
      for (const s of e.snippets || []) box.append(el("pre", { class: "snip", text: s }));
    }
    return box;
  }

  function renderScan() {
    const sc = state.scan;
    $("scanBox").classList.toggle("hidden", !sc);
    if (!sc) return;
    if (sc.id !== scanId) { scanId = sc.id; choices = {}; merges = {}; }
    $("scanSummary").textContent = sc.at + " 扫描（" + (sc.mode === "regular" ? "定期清理" : "深度清理") + "，" + sc.source + "）：attachments/ 里共 " + sc.total + " 张，确定在用 " + sc.used + " 张"
      + (sc.kept ? "，你选了留的 " + sc.kept + " 张" : "") + "。";
    $("certainLabel").textContent = "清掉这 " + sc.certain.length + " 张（" + size(sc.certain_bytes) + "）";
    $("certain").disabled = !sc.certain.length;
    $("certainList").replaceChildren(...sc.certain.map((c) => el("li", { text: c.path + "（" + size(c.size) + "）" })));

    $("unsureCount").textContent = sc.unsure.length + " 张";
    $("unsureList").replaceChildren(...sc.unsure.map((u) => {
      const name = "u-" + u.path;
      const row = el("div", { class: "choice-row", role: "radiogroup", "aria-label": "怎么处理 " + u.path });
      for (const [v, label] of [["", "不选"], ["keep", "留"], ["clean", "清"], ["app", "交给应用"]]) {
        const input = el("input", { type: "radio", name, value: v });
        input.checked = (choices[u.path] || "") === v;
        input.addEventListener("change", () => { if (v) choices[u.path] = v; else delete choices[u.path]; updateHint(); });
        row.append(el("label", {}, input, document.createTextNode(label)));
      }
      const info = el("div", { class: "info" },
        el("b", { text: u.path.split("/").pop() }),
        el("div", { class: "meta", text: u.path + "（" + size(u.size) + "）" }),
        el("div", { class: "meta", text: (u.reasons || []).slice(0, 3).join("；") }),
        evidenceBlock(u), row);
      return el("li", {}, thumb(u.path), info);
    }));

    $("dupCount").textContent = sc.dups.length + " 组";
    $("dupList").replaceChildren(...sc.dups.map((g) => {
      const box = el("input", { type: "checkbox" });
      box.checked = !!merges[g.sha256];
      box.addEventListener("change", () => { if (box.checked) merges[g.sha256] = true; else delete merges[g.sha256]; updateHint(); });
      const thumbs = el("div", { class: "group-thumbs" }, ...g.members.slice(0, 4).map((m) => thumb(m.path)));
      const info = el("div", { class: "info" },
        el("div", { class: "meta", text: "每张 " + size(g.size) + "，" + g.members.length + " 张一模一样" }),
        ...g.members.map((m) => el("div", { text: (m.path === g.keeper ? "留：" : "合并掉：") + m.path + "（" + m.refs + " 篇在用）" })),
        g.note ? el("div", { class: "note", text: g.note }) : null,
        el("label", {}, box, document.createTextNode(" 合并这一组")));
      return el("li", {}, thumbs, info);
    }));

    const held = sc.held || [];
    $("heldCount").textContent = held.length ? held.length + " 篇" : "";
    $("heldList").replaceChildren(...(held.length ? held.map((g) => {
      const btn = el("button", { type: "button", text: "手机上已经没有这篇了，释放这些图" });
      btn.addEventListener("click", async () => {
        if (!confirm("释放《" + g.title + "》用过的 " + g.count + " 张图？\n之后这些图按正常规则判断，没有别的文档在用就可能被清掉；以后在手机上放回这篇时，图片可能已经不在了。")) return;
        try { state = await api("/api/cleanup/release", { doc: g.doc }); g.count = 0; btn.disabled = true; btn.textContent = "已释放，重新扫描后生效"; }
        catch (e) { btn.textContent = e.message; }
      });
      return el("li", {}, el("div", { class: "info" },
        el("b", { text: g.title }),
        el("div", { class: "meta", text: g.doc + "（" + g.from + "上删的，" + g.count + " 张图）" }),
        g.from === "手机" ? btn : el("div", { class: "meta", text: "电脑上删的：这些图在上面「拿不准」里，逐张确认才清。" })));
    }) : [el("li", { class: "hint", text: "没有。" })]));

    const c = sc.conflicts || { count: 0, names: [] };
    $("conflictInfo").textContent = c.count ? ("note-conflicts/ 里有 " + c.count + " 份冲突副本（" + c.folder + "）。只列出来，清理工具不动它们。")
      : "没有冲突副本。";
    $("conflictList").replaceChildren(...(c.names || []).map((n) => el("li", { text: n })));
    const un = sc.unreadable || [];
    $("unreadableBox").classList.toggle("hidden", !un.length);
    $("unreadableList").replaceChildren(...un.map((u) => el("li", { text: u.doc + "：" + u.error })));
    updateHint();
  }

  function picked() {
    const out = { scan_id: scanId, certain: $("certain").checked, clean: [], keep: [], delegate: [], merge: Object.keys(merges) };
    for (const [p, v] of Object.entries(choices)) {
      if (v === "clean") out.clean.push(p);
      else if (v === "keep") out.keep.push(p);
      else if (v === "app") out.delegate.push(p);
    }
    return out;
  }

  function updateHint() {
    const p = picked();
    const parts = [];
    if (p.certain) parts.push("清确定没人用的");
    if (p.clean.length) parts.push("清 " + p.clean.length + " 张");
    if (p.keep.length) parts.push("留 " + p.keep.length + " 张");
    if (p.delegate.length) parts.push(p.delegate.length + " 张交给应用");
    if (p.merge.length) parts.push("合并 " + p.merge.length + " 组");
    $("applyHint").textContent = parts.length ? "将会：" + parts.join("，") : "还没选";
    $("apply").disabled = !parts.length || !!(state.job || {}).running;
  }

  function renderReport() {
    const r = state.report;
    const box = $("reportBox");
    if (!r) { box.replaceChildren(el("p", { class: "hint", text: "还没有清理过。" })); return; }
    const kids = [el("p", { class: "stat", text: r.at + "，" + r.mode + "（" + r.source + "发起）：移进废纸篓 " + r.cleaned.length + " 张，留着 " + r.kept.length + " 张"
      + (r.merged.length ? "，合并 " + r.merged.length + " 组" : "") + (r.pending ? "，待确认 " + r.pending + " 张" : "")
      + (r.conflicts ? "，冲突副本 " + r.conflicts + " 份" : "") })];
    if (r.cleaned.length) {
      kids.push(el("h3", { text: "移进了废纸篓" }));
      kids.push(el("ul", { class: "simple" }, ...r.cleaned.map((c) => el("li", { text: c.path + "（" + size(c.size) + "）—" + c.reason + "（" + c.by + "）" }))));
    }
    if (r.merged.length) {
      kids.push(el("h3", { text: "合并的重复图片" }));
      kids.push(el("ul", { class: "simple" }, ...r.merged.map((m) => el("li", { text: "留 " + m.keeper + "，改了 " + m.docs.length + " 篇文档的引用" }))));
    }
    if (r.kept.length) {
      kids.push(el("h3", { text: "没清的" }));
      kids.push(el("ul", { class: "simple" }, ...r.kept.map((c) => el("li", { text: c.path + "—" + c.reason + "（" + c.by + "）" }))));
    }
    if (r.problems.length) {
      kids.push(el("h3", { text: "问题" }));
      kids.push(el("ul", { class: "simple err" }, ...r.problems.map((p) => el("li", { text: p }))));
    }
    box.replaceChildren(...kids);
  }

  async function load() {
    try {
      state = await api("/api/cleanup");
    } catch (e) {
      $("job").textContent = e.message;
      return;
    }
    renderState();
    renderEdits();
    renderRegular();
    renderJob();
    renderScan();
    renderReport();
  }

  async function run(body) {
    try {
      state = await api("/api/cleanup/run", body);
      renderJob();
    } catch (e) {
      $("job").className = "hint err";
      $("job").textContent = e.message;
    }
  }

  $("scan").addEventListener("click", () => run({ kind: "scan" }));
  $("runRegular").addEventListener("click", () => run({ kind: "regular" }));
  $("apply").addEventListener("click", () => run({ kind: "apply", choices: picked() }));
  $("schedule").addEventListener("change", async () => {
    try { state = await api("/api/cleanup/config", { schedule: $("schedule").value }); renderRegular(); }
    catch (e) { $("regularInfo").textContent = e.message; }
  });
  $("certain").addEventListener("change", updateHint);
  $("confirmEdits").addEventListener("click", async () => {
    try { state = await api("/api/cleanup/confirm_edits", {}); renderEdits(); renderRegular(); }
    catch (e) { $("editsText").textContent = e.message; }
  });
  $("confirmState").addEventListener("click", async () => {
    try { state = await api("/api/cleanup/confirm", {}); renderState(); renderRegular(); }
    catch (e) { $("stateText").textContent = e.message; }
  });
  function setAll(v) {
    for (const u of (state.scan || {}).unsure || []) { if (v) choices[u.path] = v; else delete choices[u.path]; }
    renderScan();
  }
  $("allKeep").addEventListener("click", () => setAll("keep"));
  $("allApp").addEventListener("click", () => setAll("app"));
  $("allNone").addEventListener("click", () => setAll(""));
  load();
})();
