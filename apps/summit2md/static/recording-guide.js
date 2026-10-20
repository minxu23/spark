(() => {
  const data = JSON.parse(document.getElementById('guideData').textContent);
  const $ = (id) => document.getElementById(id);
  const rows = [], seen = new Map();
  data.recorded_tracks.forEach((track) => track.entries.forEach((entry) => {
    if (seen.has(entry.id)) {
      const row = seen.get(entry.id);
      row.tracks.push(track);
    } else {
      const row = { ...entry, tracks: [track] };
      seen.set(entry.id, row); rows.push(row);
    }
  }));
  const norm = (s) => String(s || '').toLowerCase().replace(/[-–—_/]+/g, ' ').replace(/\s+/g, ' ').trim();
  const aliases = {
    '光互联': ['optical', 'photonics', 'optics', 'photonic'], '网络': ['network', 'ethernet', 'routing'],
    '散热': ['cooling', 'thermal'], '供电': ['power', 'voltage'], '存储': ['storage', 'memory'],
    '芯片': ['chip', 'silicon'], '量子': ['quantum'], '安全': ['security', 'secure'],
  };
  let trackId = '', limit = 40, summaryJob = '';
  // 单个议题的专题归属（例如开场议程里的一场实质对谈），随「保存分组」一起提交
  const entryOverrides = { ...(data.entry_overrides || {}) };
  const savedOverrides = JSON.stringify(entryOverrides);
  const groupTitles = [...(data.summary_groups || []), ...(data.overview_groups || [])].map((g) => g.title);
  const jobKey = 'summit.recording-summary:' + data.source_url + ':' + data.output_dir;
  $('summary').textContent = `${rows.length} 个视频 · ${data.recorded_tracks.length} 个官网议程 · ${(data.summary_groups || []).length} 个总结组 · ${data.local_count} 个视频已有本地资料`;
  $('official').href = data.source_url;
  const node = (tag, text, cls) => { const e = document.createElement(tag); e.textContent = text; if (cls) e.className = cls; return e; };
  const link = (label, href, cls) => {
    const a = node('a', label, cls);
    // URLs from third-party metadata are never allowed to become executable links.
    if (/^https?:\/\//i.test(href) || /^\/read\/open\?/.test(href) || /^\.\.?\//.test(href)) a.href = href;
    a.target = '_blank'; a.rel = 'noopener'; return a;
  };
  const choices = [{id:'', title:'全部议程', count:rows.length}, ...data.recorded_tracks];
  choices.forEach((t) => {
    const btn = node('button', `${t.title} · ${t.count}`);
    btn.type = 'button'; btn.dataset.track = t.id; btn.setAttribute('aria-pressed', String(!t.id));
    btn.addEventListener('click', () => { trackId = t.id; limit = 40; render(); });
    $('tracks').appendChild(btn);
    $('trackSelect').appendChild(new Option(`${t.title}（${t.count}）`, t.id));
  });
  const groupById = new Map([...(data.summary_groups || []), ...(data.overview_groups || [])].map((g) => [g.id,g]));
  (data.quick_searches || (data.summary_groups || []).slice(0,6).map((g) => g.title)).forEach((text) => {
    const b = node('button', text); b.type = 'button'; b.addEventListener('click', () => { $('search').value = text; limit = 40; render(); }); $('chips').appendChild(b);
  });
  function render() {
    const words = norm($('search').value).split(' ').filter(Boolean);
    const filtered = rows.filter((r) => {
      if (trackId && !r.tracks.some((t) => t.id === trackId)) return false;
      if ($('localOnly').checked && !r.local) return false;
      const text = norm([r.title, r.agenda_title, r.speakers, r.time, ...r.tracks.map((t) => t.title), ...r.tracks.map((t) => (groupById.get(t.summary_group_id) || {}).title)].join(' '));
      return words.every((w) => [w, ...(aliases[w] || [])].some((term) => text.includes(term)));
    });
    $('resultMeta').textContent = `找到 ${filtered.length} 个视频 · 显示 ${Math.min(limit, filtered.length)} 个`;
    $('trackSelect').value = trackId;
    $('tracks').querySelectorAll('button').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.track === trackId)));
    $('results').replaceChildren();
    filtered.slice(0, limit).forEach((r) => {
      const card = node('article', '', 'session');
      const topic = (r.collection || {}).group_title;
      card.appendChild(node('div', r.tracks.map((t) => t.title).join(' · ') + (r.time ? ' · ' + r.time : '') + (topic ? ' · 专题：' + topic : ''), 'track'));
      card.appendChild(node('h2', r.title));
      if (r.speakers) card.appendChild(node('p', r.speakers, 'speaker'));
      const links = node('div', '', 'links');
      if (r.local) links.appendChild(link(`阅读本地${r.local.label}`, r.local.url, 'local'));
      links.appendChild(link('观看视频 ↗', r.url));
      (r.slides || []).forEach((s) => links.appendChild(link(s.title === 'G-Drive' ? '幻灯片 · Google Drive ↗' : `幻灯片 · ${s.title} ↗`, s.url)));
      if (location.protocol !== 'file:' && groupTitles.length > 1) {
        const assign = node('label', '归入 ', 'assign');
        const select = document.createElement('select');
        const natural = (groupById.get(r.tracks[0].summary_group_id) || {}).title || '';
        select.appendChild(new Option(`跟随议程（${natural}）`, ''));
        groupTitles.forEach((t) => select.appendChild(new Option(t, t)));
        select.value = entryOverrides[r.id] || '';
        select.setAttribute('aria-label', r.title + ' 归入的专题');
        select.addEventListener('change', () => {
          if (select.value) entryOverrides[r.id] = select.value; else delete entryOverrides[r.id];
          const changed = JSON.stringify(entryOverrides) !== savedOverrides;
          $('assignNotice').hidden = !changed;
        });
        assign.appendChild(select); links.appendChild(assign);
      }
      card.appendChild(links); $('results').appendChild(card);
    });
    if (!filtered.length) $('results').appendChild(node('p', '没有匹配的议题，试试缩短关键词或切换专题。', 'empty'));
    $('more').hidden = filtered.length <= limit;
    renderSummaries();
  }
  $('search').addEventListener('input', () => { limit = 40; render(); });
  $('localOnly').addEventListener('change', () => { limit = 40; render(); });
  $('trackSelect').addEventListener('change', () => { trackId = $('trackSelect').value; limit = 40; render(); });
  $('clear').addEventListener('click', () => { trackId = ''; $('search').value = ''; $('localOnly').checked = false; limit = 40; render(); });
  $('more').addEventListener('click', () => { limit += 40; render(); });
  $('refresh').addEventListener('click', async () => {
    if (location.protocol === 'file:') { $('status').textContent = '请从 Spark 中打开在线导航，再更新目录。'; return; }
    $('refresh').disabled = true; $('status').textContent = '正在更新官方目录…';
    try {
      const response = await fetch(data.api_url || '/summit/api/recording_guide', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({source:data.source_url, output_dir:data.output_dir})});
      const result = await response.json(); if (!response.ok) throw new Error(result.error || '更新失败');
      location.reload();
    } catch (e) { $('status').textContent = e.message; $('refresh').disabled = false; }
  });
  data.recorded_tracks.forEach((track) => {
    const group = [...(data.summary_groups || []), ...(data.overview_groups || [])].find((g) => g.id === track.summary_group_id);
    const label = node('div', '', 'group-row');
    label.appendChild(node('span', track.title));
    const input = document.createElement('input'); input.type = 'text'; input.value = group ? group.title : track.title;
    input.dataset.agendaId = track.id; input.setAttribute('aria-label', track.title + ' 的总结组');
    label.appendChild(input);
    const choice = node('label', '纳入大会导读');
    const overview = document.createElement('input'); overview.type = 'checkbox';
    overview.dataset.overviewId = track.id;
    overview.checked = track.summary_role === 'overview';
    choice.prepend(overview); label.appendChild(choice);
    $('groupAssignments').appendChild(label);
  });
  if (data.grouping_needs_review) {
    $('groupEditor').open = true;
    $('groupingNotice').textContent = '按大会规模，当前总结组偏多、偏小，建议先按内容合并再生成总结：智能建议会考虑议题数量和主题关联，保存前可调整。如果确认现有分组合适，直接点「保存分组」也会解除这个提醒。';
  }
  function openGroupEditor() {
    $('groupEditor').open = true;
    $('groupEditor').scrollIntoView({behavior:'smooth',block:'start'});
  }
  $('editGroups').addEventListener('click',openGroupEditor);
  if (location.hash === '#groupEditor') openGroupEditor();
  $('saveGroups').addEventListener('click', async () => {
    $('saveGroups').disabled = true;
    try {
      const assignments = {};
      $('groupAssignments').querySelectorAll('input[data-agenda-id]').forEach((input) => { assignments[input.dataset.agendaId] = input.value.trim(); });
      const overview_track_ids = [...$('groupAssignments').querySelectorAll('input[data-overview-id]:checked')].map((input) => input.dataset.overviewId);
      const response = await fetch(data.groups_api_url || '/summit/api/recording_groups', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({source:data.source_url,output_dir:data.output_dir,assignments,overview_track_ids,entry_overrides:entryOverrides})});
      const result = await response.json(); if (!response.ok) throw new Error(result.error || '保存失败');
      location.reload();
    } catch(e) { $('summaryStatus').textContent = e.message; $('saveGroups').disabled = false; }
  });
  function renderSummaries() {
    const agenda = data.recorded_tracks.find((t) => t.id === trackId);
    const groups = data.summary_groups || [];
    const track = agenda && groups.find((g) => g.id === agenda.summary_group_id);
    const overview = agenda && (data.overview_groups || []).find((g) => g.id === agenda.summary_group_id);
    $('summaryTitle').textContent = track ? track.title + ' · 专题总结' : overview ? '大会导读' : '大会与专题总结';
    const saved = groups.filter((t) => t.summary);
    $('summaryCoverage').textContent = track ? `总结组覆盖 ${track.local_count}/${track.count} 个议题；包含官网议程：${track.agenda_tracks.map((t) => t.title).join("、")}` : `${saved.length}/${groups.length} 个总结组已有总结 · ${data.local_count}/${rows.length} 个视频已有本地资料`;
    if (overview) $('summaryCoverage').textContent = `${overview.count} 个开场致辞等大会背景内容纳入大会总结的导读，不单独生成专题总结；其中的实质对谈可在议题卡片上改归专题。`;
    $('summaryLinks').replaceChildren();
    if (data.overall_summary) $('summaryLinks').appendChild(link('阅读大会总结' + (data.overall_summary.stale ? '（资料有更新）' : ''), data.overall_summary.url));
    (track ? [track] : groups.filter((g) => g.summary || g.index_url)).forEach((t) => {
      if (t.index_url && (!t.summary || t.index_url !== t.summary.url)) $('summaryLinks').appendChild(link((track ? '已有资料与总结' : t.title + ' · 已有资料与总结'), t.index_url));
      if (t.summary) $('summaryLinks').appendChild(link((track ? '阅读专题总结' : t.title) + (t.summary.stale ? '（资料有更新）' : ''), t.summary.url));
    });
    $('generateTrack').textContent = track && track.summary ? `更新 ${track.title} 总结` : track ? `生成 ${track.title} 总结` : '选择议程后生成专题总结';
    $('generateTrack').disabled = !!summaryJob || !track || !track.local_count || location.protocol === 'file:';
    $('generateOverall').disabled = !!summaryJob || !data.local_count || location.protocol === 'file:';
    $('refresh').disabled = !!summaryJob;
    ['saveGroups','suggestGroups','organizeFiles'].forEach((id) => { $(id).disabled = !!summaryJob || location.protocol === 'file:'; });
    $('stopSummary').hidden = !summaryJob;
  }
  async function pollSummary() {
    try {
      const response = await fetch((data.job_api_url || '/summit/api/simple_job_status/') + encodeURIComponent(summaryJob));
      const result = await response.json();
      if (!response.ok) {
        if (response.status === 404) { summaryJob = ''; sessionStorage.removeItem(jobKey); }
        throw new Error(result.error || '无法读取任务状态');
      }
      if (!result.done) { setTimeout(pollSummary, 2000); return; }
      summaryJob = ''; sessionStorage.removeItem(jobKey);
      if (result.error) throw new Error(result.error + '。已完成的专题已保留，重试会复用。');
      if (result.stopped) { $('summaryStatus').textContent = '已停止，已完成的专题会在刷新页面后显示。'; renderSummaries(); return; }
      if (result.result && result.result.assignments) {
        const proposed = result.result;
        $('groupAssignments').querySelectorAll('input[data-agenda-id]').forEach((input) => { input.value = proposed.assignments[input.dataset.agendaId] || input.value; });
        $('groupAssignments').querySelectorAll('input[data-overview-id]').forEach((input) => { input.checked = proposed.overview_track_ids.includes(input.dataset.overviewId); });
        $('groupEditor').open = true;
        $('summaryStatus').textContent = '分组建议：' + proposed.groups.map((g) => `${g.title}（${g.count}）`).join('、') + '。请查看并保存，保存时会实际归档文件。';
        renderSummaries(); return;
      }
      location.reload();
    } catch (e) {
      $('summaryStatus').textContent = e.message;
      // A transient polling failure must not allow a duplicate paid job to start.
      if (summaryJob) { setTimeout(pollSummary, 5000); return; }
      renderSummaries();
    }
  }
  async function generateSummary(currentTrack) {
    if (location.protocol === 'file:') return;
    summaryJob = 'starting'; renderSummaries();
    $('summaryStatus').textContent = '正在启动总结任务…';
    try {
      const response = await fetch(data.summary_api_url || '/summit/api/recording_summary', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body:JSON.stringify({source:data.source_url, output_dir:data.output_dir, track_id:currentTrack ? data.recorded_tracks.find((t) => t.id === trackId).summary_group_id : ''})
      });
      const result = await response.json(); if (!response.ok) throw new Error(result.error || '启动失败');
      summaryJob = result.job_id; sessionStorage.setItem(jobKey, summaryJob);
      $('summaryStatus').textContent = `正在使用 ${result.model || '总结模型'} 生成${currentTrack ? '专题总结' : '各专题总结和大会概览'}，完成后自动刷新。可以切换专题继续浏览。`;
      renderSummaries(); pollSummary();
    } catch(e) { summaryJob = ''; $('summaryStatus').textContent = e.message; renderSummaries(); }
  }
  $('generateTrack').addEventListener('click', () => generateSummary(true));
  $('generateOverall').addEventListener('click', () => generateSummary(false));
  async function layoutJob(suggest) {
    summaryJob = 'starting'; renderSummaries();
    $('summaryStatus').textContent = suggest ? '正在结合议题内容与规模拟定分组建议…' : '正在备份、归档已有文件并更新导航…';
    try {
      const endpoint = suggest ? data.grouping_api_url || '/summit/api/recording_grouping' : data.organize_api_url || '/summit/api/recording_organize';
      const response = await fetch(endpoint, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({source:data.source_url,output_dir:data.output_dir})});
      const result = await response.json(); if (!response.ok) throw new Error(result.error || '启动失败');
      summaryJob = result.job_id; sessionStorage.setItem(jobKey,summaryJob);
      renderSummaries(); pollSummary();
    } catch(e) { summaryJob = ''; $('summaryStatus').textContent = e.message; renderSummaries(); }
  }
  $('suggestGroups').addEventListener('click', () => layoutJob(true));
  $('organizeFiles').addEventListener('click', () => layoutJob(false));
  $('stopSummary').addEventListener('click', async () => {
    if (!summaryJob || summaryJob === 'starting') return;
    try {
      const response = await fetch((data.stop_api_url || '/summit/api/simple_job_stop/') + encodeURIComponent(summaryJob), {method:'POST'});
      if (!response.ok) throw new Error('停止请求失败，请重试');
      $('summaryStatus').textContent = '正在停止；已完成的专题会保留。';
    } catch(e) { $('summaryStatus').textContent = e.message; }
  });
  if (location.protocol !== 'file:') summaryJob = sessionStorage.getItem(jobKey) || '';
  render();
  if (summaryJob) { $('summaryStatus').textContent = '正在恢复总结任务状态…'; pollSummary(); }
})();
