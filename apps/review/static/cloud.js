// 标签词云的排布：纯函数，不碰 DOM，也不用随机数——同样的词、同样的宽度，位置每次都一样。
// words 已按篇数从大到小排好：[{text, size, weight}]；measure(text, size, weight) 返回文字宽度（px）。
// 从中心向外走椭圆螺旋，第一个不和已放的词重叠、又没超出左右边界的位置就是它的位置。
(function (root) {
  "use strict";
  const GAP = 4;            // 两个词之间至少空多少 px
  const MAX_STEPS = 20000;  // 螺旋最多走多少步（走完还放不下就不放这个词）
  const STEP = 0.12;        // 螺旋每步转多少弧度
  const SPREAD = 2.4;       // 横向比纵向拉开多少倍，词云是扁的

  function layout(words, width, measure) {
    const placed = [];
    for (const w of words) {
      let size = w.size;
      let tw = measure(w.text, size, w.weight);
      if (tw > width - GAP) {   // 比整个容器还宽：缩小到放得下
        size = Math.max(9, Math.floor(size * (width - GAP) / tw));
        tw = measure(w.text, size, w.weight);
      }
      const bw = Math.ceil(tw), bh = Math.ceil(size * 1.25);
      for (let i = 0; i < MAX_STEPS; i++) {
        const t = i * STEP, r = 1.1 * t;
        const x = SPREAD * r * Math.cos(t), y = r * Math.sin(t);
        if (x - bw / 2 < -width / 2 || x + bw / 2 > width / 2) continue;
        if (placed.some((p) => Math.abs(p.x - x) * 2 < p.w + bw + GAP && Math.abs(p.y - y) * 2 < p.h + bh + GAP)) continue;
        placed.push({ text: w.text, size, weight: w.weight, x, y, w: bw, h: bh });
        break;
      }
    }
    if (!placed.length) return { items: [], height: 0 };
    const top = Math.min(...placed.map((p) => p.y - p.h / 2));
    const bottom = Math.max(...placed.map((p) => p.y + p.h / 2));
    return {
      height: Math.ceil(bottom - top),
      items: placed.map((p) => ({ text: p.text, size: p.size, weight: p.weight, w: p.w, h: p.h,
                                  left: Math.round(p.x - p.w / 2 + width / 2), top: Math.round(p.y - p.h / 2 - top) })),
    };
  }

  const api = { layout };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.SparkCloud = api;
})(typeof window !== "undefined" ? window : globalThis);
