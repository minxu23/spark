const grid = document.getElementById('grid');
const note = document.getElementById('note');

function card(a) {
  const el = document.createElement(a.up ? 'a' : 'div');
  el.className = 'card';
  if (a.up) el.href = a.url;
  const tags = a.tasks.map(t => `<span class="tag">${t}</span>`).join('');
  el.innerHTML = `
    <h2>${a.name}</h2>
    <div class="tagline">${a.tagline}</div>
    <div class="detail">${a.detail}</div>
    <div class="tasks">${tags}</div>
    <div class="foot">
      <span class="${a.up ? 'up' : 'down'}"><i class="dot"></i>${a.up ? '运行中' : '未启动'}</span>
      <span>:${a.port}</span>
    </div>`;
  return el;
}

async function load() {
  const apps = await (await fetch('/api/apps')).json();
  grid.replaceChildren(...apps.map(card));
  const down = apps.filter(a => !a.up).map(a => a.name);
  note.innerHTML = down.length
    ? `${down.join(' 和 ')} 还没起来——刚启动时要等几秒装依赖，页面会自动重试。`
    : `两个服务都在跑。关掉启动器的终端窗口，或在里面按 <code>Ctrl+C</code>，会把它们一起停掉。`;
}

load();
setInterval(load, 3000);
