// 阅读偏好（配色、字体、字号、栏宽）的唯一一份读写逻辑，阅读页和设置页共用。
// 存在浏览器本地（localStorage，只管这一个浏览器）；/read 和 /settings 同源，所以是同一份。
// 这里只定义 window.SparkRead，不动页面；阅读页由 /read/static/theme.js 在首屏前套到 <html> 上，
// 设置页只套到预览框上。字体栈写在 read-prefs.css 里（按 data-font），这里只管名字和分组。
(() => {
  const KEY = 'spark-read-prefs';
  const SIZES = [14, 15, 16, 17, 18, 20, 22, 24];
  const DEFAULTS = { theme: 'auto', font: 'serif', size: null, width: 'normal', customFont: '' };

  const THEMES = [
    ['auto', '自动'], ['kami', '羊皮纸'], ['white', '白'], ['sepia', '米黄'], ['gray', '灰'], ['night', '夜间'],
  ];
  const WIDTHS = [['narrow', '窄', 620], ['normal', '中', 760], ['wide', '宽', 920]];

  // 「Aa」面板里只放前三个；其余在设置页选。probe 是用来检测本机有没有装的主字体名（没有就不检测）。
  const FONTS = [
    { key: 'serif', label: '宋体', group: '常用', probe: ['Charter', 'Songti SC'] },
    { key: 'sans', label: '黑体', group: '常用', probe: ['PingFang SC'] },
    { key: 'kai', label: '楷体', group: '常用', probe: ['Kaiti SC'] },

    { key: 'song', label: '宋体（中西文都用宋体）', group: '衬线 / 宋体', probe: ['Songti SC'] },
    { key: 'iowan', label: 'Iowan Old Style + 宋体', group: '衬线 / 宋体', probe: ['Iowan Old Style'] },
    { key: 'georgia', label: 'Georgia + 宋体', group: '衬线 / 宋体', probe: ['Georgia'] },
    { key: 'palatino', label: 'Palatino + 宋体', group: '衬线 / 宋体', probe: ['Palatino'] },
    { key: 'baskerville', label: 'Baskerville + 宋体', group: '衬线 / 宋体', probe: ['Baskerville'] },
    { key: 'times', label: 'Times New Roman + 宋体', group: '衬线 / 宋体', probe: ['Times New Roman'] },
    { key: 'source-serif', label: '思源宋体 / Noto Serif', group: '衬线 / 宋体', probe: ['Source Han Serif SC', 'Noto Serif CJK SC', 'Noto Serif SC'] },

    { key: 'helvetica', label: 'Helvetica Neue + 苹方', group: '无衬线 / 黑体', probe: ['Helvetica Neue'] },
    { key: 'avenir', label: 'Avenir Next + 苹方', group: '无衬线 / 黑体', probe: ['Avenir Next'] },
    { key: 'hiragino', label: '冬青黑体', group: '无衬线 / 黑体', probe: ['Hiragino Sans GB'] },
    { key: 'source-sans', label: '思源黑体 / Noto Sans', group: '无衬线 / 黑体', probe: ['Source Han Sans SC', 'Noto Sans CJK SC', 'Noto Sans SC'] },
    { key: 'yahei', label: '微软雅黑', group: '无衬线 / 黑体', probe: ['Microsoft YaHei'] },

    { key: 'wenkai', label: '霞鹜文楷', group: '楷体 / 仿宋', probe: ['LXGW WenKai', 'LXGW WenKai Screen'] },
    { key: 'fangsong', label: '仿宋', group: '楷体 / 仿宋', probe: ['STFangsong', 'FangSong'] },

    { key: 'mono', label: '等宽（SF Mono / Menlo）', group: '等宽', probe: ['SF Mono', 'Menlo'] },
  ];
  const CORE = ['serif', 'sans', 'kai'];

  function load() {
    let p;
    try { p = { ...DEFAULTS, ...JSON.parse(localStorage.getItem(KEY) || '{}') }; }
    catch { p = { ...DEFAULTS }; }
    // 旧版本或手改坏的值别把页面弄乱：认不出的一律退回默认
    if (!THEMES.some(([k]) => k === p.theme)) p.theme = DEFAULTS.theme;
    if (!WIDTHS.some(([k]) => k === p.width)) p.width = DEFAULTS.width;
    if (typeof p.customFont !== 'string') p.customFont = '';
    if (p.font === 'custom' ? !cleanFamilies(p.customFont) : !FONTS.some(f => f.key === p.font)) p.font = DEFAULTS.font;
    if (!(Number.isInteger(p.size) && p.size >= 0 && p.size < SIZES.length)) p.size = null;
    return p;
  }

  function save(p) {
    try { localStorage.setItem(KEY, JSON.stringify(p)); } catch {}
  }

  // 用户填的字体名 → CSS 的 font-family 片段。逗号分开可以填好几个；每个都加引号，
  // 去掉引号、反斜杠和控制字符，免得写出坏 CSS。空字符串表示没填。
  function cleanFamilies(text) {
    return String(text || '').split(/[,，]/)
      .map(s => s.replace(/[\u0000-\u001f"'\\;{}<>]/g, '').trim().slice(0, 80))
      .filter(Boolean).slice(0, 4)
      .map(s => `"${s}"`).join(', ');
  }

  function fontLabel(p) {
    if (p.font === 'custom') return p.customFont.trim() || '自定义字体';
    return (FONTS.find(f => f.key === p.font) || FONTS[0]).label;
  }

  // 套到某个元素上：阅读页是 <html>，设置页是预览框。字体栈、配色都在 CSS 里按 data-* 取。
  function apply(p, el = document.documentElement) {
    el.dataset.theme = p.theme;
    el.dataset.font = p.font;
    el.dataset.width = p.width;
    // size 为空表示没调过：用 CSS 里的默认（桌面 17px、手机 16px）
    if (Number.isInteger(p.size)) el.style.setProperty('--fs', SIZES[p.size] + 'px');
    else el.style.removeProperty('--fs');
    const fam = cleanFamilies(p.customFont);
    if (fam) el.style.setProperty('--custom-font', fam);
    else el.style.removeProperty('--custom-font');
  }

  // 另一个标签页改了偏好（比如开着阅读页又去设置页调）：这边跟着变
  function watch(cb) {
    addEventListener('storage', e => {
      if (e.key === KEY || e.key === null) cb(load());
    });
  }

  // 本机装没装某个字体：拿同一串字分别用「这个字体, 通用字体」和「通用字体」量宽度，
  // 三种通用字体下都一样宽，说明浏览器根本没找到它，用的是后备字体。
  // Safari 出于防指纹不让网页用自己装的字体，那里会如实报「没有」——反正也显示不出来。
  let ctx = null;
  function installed(family) {
    try {
      ctx = ctx || document.createElement('canvas').getContext('2d');
      if (!ctx) return null;
      const text = '永和九年，岁在癸丑 Hamburgefonstiv mmmwwwlli 0123';
      const name = family.replace(/["\\]/g, '');
      return ['monospace', 'serif', 'sans-serif'].some(base => {
        ctx.font = `72px ${base}`;
        const w0 = ctx.measureText(text).width;
        ctx.font = `72px "${name}", ${base}`;
        return Math.abs(ctx.measureText(text).width - w0) > 0.5;
      });
    } catch { return null; }
  }

  window.SparkRead = {
    KEY, SIZES, DEFAULTS, THEMES, WIDTHS, FONTS, CORE,
    load, save, apply, watch, cleanFamilies, fontLabel, installed,
  };
})();
