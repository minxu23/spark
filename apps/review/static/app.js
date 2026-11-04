// 复盘页：选类型、填参数、生成（后台跑，轮询进度），下面列出生成过的复盘。
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const KIND_TEXT = { reading: "阅读复盘", topic: "主题来龙去脉", claims: "观点检验", clips: "剪藏复盘", picked: "剪藏综合小结" };
  const MAX_PICKED = 20;
  const picked = new Set();
  let clipsLoaded = false;
  let kind = "reading";
  let pollTimer = null;

  function setKind(k) {
    kind = k;
    document.querySelectorAll(".kind").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.kind === k)));
    document.querySelectorAll("[data-for]").forEach((el) => {
      el.hidden = !el.dataset.for.split(" ").includes(k);
    });
    $("topicLabel").textContent = k === "claims" ? "主题（可以不填）" : "主题";
    // 剪藏攒得慢，默认看一个月
    if (k === "clips" && $("days").value === "7") $("days").value = "30";
    if (k === "picked" && !clipsLoaded) loadClips();
    $("run").textContent = k === "picked" ? "写综合小结" : "生成复盘";
  }

  // ---------------------------------------------------------------- 剪藏综合小结：挑几篇
  function showPickCount() {
    $("pickCount").textContent = picked.size ? `已选 ${picked.size} 篇${picked.size < 2 ? "，再挑至少 1 篇" : ""}` : `挑 2 到 ${MAX_PICKED} 篇`;
    $("pickCount").className = "hint";
  }
  async function loadClips() {
    try {
      const items = await (await fetch("api/clips")).json();
      clipsLoaded = true;
      $("picks").replaceChildren(...items.map((c) => {
        const li = document.createElement("li");
        li.dataset.search = [c.title, c.source, ...(c.tags || [])].join(" ").toLowerCase();
        const label = document.createElement("label");
        const box = Object.assign(document.createElement("input"), { type: "checkbox", value: c.path, checked: picked.has(c.path) });
        box.addEventListener("change", () => {
          if (box.checked && picked.size >= MAX_PICKED) {
            box.checked = false;
            $("pickCount").textContent = `一次最多 ${MAX_PICKED} 篇`;
            $("pickCount").className = "hint err";
            return;
          }
          if (box.checked) picked.add(c.path); else picked.delete(c.path);
          showPickCount();
        });
        const title = Object.assign(document.createElement("span"), { textContent: c.title });
        const meta = Object.assign(document.createElement("small"), { textContent: [c.source, c.clipped].filter(Boolean).join("，") });
        label.append(box, title, meta);
        li.append(label);
        return li;
      }));
      $("picksEmpty").hidden = items.length > 0;
    } catch (e) {
      $("pickCount").textContent = `剪藏列表拿不到：${e.message}`;
      $("pickCount").className = "hint err";
    }
  }
  $("pickFilter").addEventListener("input", () => {
    const q = $("pickFilter").value.trim().toLowerCase();
    document.querySelectorAll("#picks li").forEach((li) => { li.hidden = !!q && !li.dataset.search.includes(q); });
  });
  document.querySelectorAll(".kind").forEach((b) => b.addEventListener("click", () => setKind(b.dataset.kind)));

  async function loadStats() {
    try {
      const d = await (await fetch(`api/stats?days=${$("days").value}`)).json();
      $("readStats").textContent = d.total
        ? `这段时间有阅读记录的 ${d.total} 篇（电脑上 ${d.desktop}，手机上 ${d.phone}）。生成前会再拉一次手机走 GitHub 交上来的记录。`
        : "这段时间还没有阅读记录：阅读页里打开的、手机上读的都会记下来。没有阅读记录时只列新进来没读的。";
    } catch (e) { $("readStats").textContent = ""; }
  }
  $("days").addEventListener("change", loadStats);

  async function loadList() {
    try {
      const items = await (await fetch("api/list")).json();
      $("list").replaceChildren(...items.map((it) => {
        const li = document.createElement("li");
        const a = document.createElement("a");
        a.href = it.url;
        a.textContent = it.title;
        const small = document.createElement("small");
        small.textContent = `${KIND_TEXT[it.kind] || ""}　${new Date(it.mtime * 1000).toLocaleString("zh-CN", { hour12: false })}`;
        li.append(a, small);
        return li;
      }));
      $("listEmpty").hidden = items.length > 0;
    } catch (e) { /* 列表拿不到就算了 */ }
  }

  function setStatus(text, cls = "") {
    $("status").className = `status ${cls}`;
    $("status").textContent = text;
  }

  async function poll(jobId) {
    try {
      const d = await (await fetch(`api/progress/${jobId}`)).json();
      if (d.error && !d.done) throw new Error(d.error);
      if (!d.done) {
        setStatus(`正在生成：${d.stage || "…"}`);
        pollTimer = setTimeout(() => poll(jobId), 1500);
        return;
      }
      $("run").disabled = false;
      if (d.ok) {
        $("status").className = "status";
        $("status").replaceChildren("生成好了：", Object.assign(document.createElement("a"),
          { href: d.result.url, textContent: d.result.rel }));
        loadList();
      } else {
        setStatus(`没生成出来：${d.error}`, "hint err");
      }
    } catch (e) {
      $("run").disabled = false;
      setStatus(`进度拿不到：${e.message}`, "hint err");
    }
  }

  $("run").addEventListener("click", async () => {
    const body = { kind };
    if (kind === "reading" || kind === "clips") body.days = Number($("days").value);
    else if (kind === "picked") {
      if (picked.size < 2) { setStatus("先挑至少 2 篇剪藏", "hint err"); return; }
      body.paths = [...picked];
    } else {
      body.topic = $("topic").value.trim();
      if (kind === "topic" && !body.topic) { setStatus("先填一个主题", "hint err"); $("topic").focus(); return; }
      if (kind === "claims") body.months = Number($("months").value);
    }
    $("run").disabled = true;
    setStatus("开始生成…");
    try {
      const r = await fetch("api/run", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || "没能开始");
      clearTimeout(pollTimer);
      poll(d.job_id);
    } catch (e) {
      $("run").disabled = false;
      setStatus(e.message, "hint err");
    }
  });

  setKind("reading");
  loadStats();
  loadList();
})();
