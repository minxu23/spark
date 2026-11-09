// 公式：服务端把 $…$ 和 $$…$$ 包成 .math，这里用 KaTeX 排版。写错的公式原样留着，不报错。
(() => {
  if (!window.katex) return;
  // 网页抓下来的文章常用作者自定义的宏，常见的几个补上
  const macros = {
    "\\reals": "\\mathbb{R}", "\\R": "\\mathbb{R}", "\\N": "\\mathbb{N}", "\\Z": "\\mathbb{Z}",
    "\\transpose": "^{\\top}", "\\T": "^{\\top}", "\\grad": "\\nabla",
    "\\norm": "\\left\\lVert #1 \\right\\rVert", "\\abs": "\\left\\lvert #1 \\right\\rvert",
    "\\argmax": "\\operatorname*{arg\\,max}", "\\argmin": "\\operatorname*{arg\\,min}",
  };
  for (const el of document.querySelectorAll(".math")) {
    const display = el.tagName === "DIV";
    const tex = el.textContent.trim().replace(display ? /^\$\$|\$\$$/g : /^\\\(|\\\)$/g, "").trim();
    try {
      katex.render(tex, el, { displayMode: display, throwOnError: false, macros: { ...macros } });
    } catch (e) {
      el.textContent = display ? `$$${tex}$$` : `$${tex}$`;
    }
  }
})();
