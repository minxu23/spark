const grid = document.getElementById('grid');

function card(a) {
  const el = document.createElement('a');
  el.className = 'card';
  el.dataset.key = a.key;   // 悬停配色按 app 区分：summit 蓝、notes 绿
  el.href = a.path;
  el.innerHTML = `
    <h2>${a.name}</h2>
    <div class="tagline">${a.tagline}</div>
    <div class="detail">${a.detail}</div>
    <div class="tasks">${a.tasks.map(t => `<span class="tag">${t}</span>`).join('')}</div>
    <div class="foot"><span>打开</span><span class="path">${a.path}</span></div>`;
  return el;
}

fetch('api/apps')
  .then(r => r.json())
  .then(apps => grid.replaceChildren(...apps.map(card)))
  .catch(() => { grid.textContent = '读取任务列表失败，请看启动器终端里的报错。'; });
