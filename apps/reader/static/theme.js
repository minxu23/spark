// 在 <head> 里同步执行：把上次选的阅读偏好套到 <html> 上，免得页面先按默认样式闪一下。
// 读写逻辑在 /static/common/read-prefs.js（window.SparkRead，设置页也用它），要先于本文件加载。
// 用了设置页导入的 Typora / Obsidian 主题时，apply 同时给 <html> 加 theme-custom，并在 <head> 里挂上
// /read/themes/<id>.css（同源样式表，CSP 不用放松）；文件没了就退回内置配色。
// reader.js 负责「Aa」面板。
(() => {
  const R = window.SparkRead;
  if (!R) return;
  R.apply(R.load());
  // 设置页或另一个阅读标签页改了偏好：这页立刻跟着变，再通知「Aa」面板刷新按钮状态
  R.watch(p => {
    R.apply(p);
    document.dispatchEvent(new CustomEvent('spark-read-prefs', { detail: p }));
  });
})();
