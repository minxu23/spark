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
  // 能做的事写成一行普通文字：做成胶囊标签的话看着像能点的按钮，其实点了只是打开整张卡片
  const tasks = node('div', 'tasks', '可以：' + a.tasks.join('、'));
  el.append(node('h2', '', a.name), node('div', 'tagline', a.tagline), node('div', 'detail', a.detail), tasks);
  return el;
}

fetch('api/apps')
  .then(r => r.json())
  .then(apps => grid.replaceChildren(...apps.map(card)))
  .catch(() => { grid.textContent = '读取任务列表失败，请看启动器终端里的报错。'; });
