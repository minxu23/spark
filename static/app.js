const grid = document.getElementById('grid');

function node(tag, className, text) {
  const el = document.createElement(tag);
  if (className) el.className = className;
  if (text !== undefined) el.textContent = text;
  return el;
}

function card(a) {
  const el = node('a', 'card');
  el.dataset.key = a.key;   // 悬停配色按 app 区分
  el.href = a.path;
  const tasks = node('div', 'tasks');
  tasks.append(...a.tasks.map(t => node('span', 'tag', t)));
  const foot = node('div', 'foot');
  // 只显示基础路径：Summit、Podcast、信息跟进都显示 /summit/，正好说明它们是同一个工具
  foot.append(node('span', '', '打开'), node('span', 'path', a.path.split('?')[0]));
  el.append(node('h2', '', a.name), node('div', 'tagline', a.tagline), node('div', 'detail', a.detail), tasks, foot);
  return el;
}

fetch('api/apps')
  .then(r => r.json())
  .then(apps => grid.replaceChildren(...apps.map(card)))
  .catch(() => { grid.textContent = '读取任务列表失败，请看启动器终端里的报错。'; });
