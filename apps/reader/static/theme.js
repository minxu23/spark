// 在 <head> 里同步执行：把上次选的阅读偏好套到 <html> 上，免得页面先按默认样式闪一下。
// reader.js 负责「Aa」面板，两边共用 window.SparkRead。
(() => {
  const KEY = 'spark-read-prefs';
  const SIZES = [14, 15, 16, 17, 18, 20, 22, 24];
  const DEFAULTS = { theme: 'auto', font: 'serif', size: null, width: 'normal' };

  function load() {
    try { return { ...DEFAULTS, ...JSON.parse(localStorage.getItem(KEY) || '{}') }; }
    catch { return { ...DEFAULTS }; }
  }

  function apply(p) {
    const root = document.documentElement;
    root.dataset.theme = p.theme;
    root.dataset.font = p.font;
    root.dataset.width = p.width;
    // size 为空表示没调过：用 CSS 里的默认（桌面 17px、手机 16px）
    if (Number.isInteger(p.size)) root.style.setProperty('--fs', SIZES[p.size] + 'px');
    else root.style.removeProperty('--fs');
  }

  function save(p) {
    try { localStorage.setItem(KEY, JSON.stringify(p)); } catch {}
  }

  window.SparkRead = { SIZES, DEFAULTS, load, apply, save };
  apply(load());
})();
