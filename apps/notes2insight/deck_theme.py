"""
演示的风格：一套参数（颜色、字体、字重、圆角），HTML 和 PPTX 两个渲染器都按它画。

默认是「瑞士网格」：白底宽屏、灰色为主、蓝色点缀、无衬线、直角、靠左对齐的网格。
用户也可以用一句话描述想要的风格（"深色背景、橙色点缀、标题用衬线"），由模型转成这套参数；
转出来的每一项都要过校验——颜色必须是 #RRGGBB，字体只能从几种字体族里选，正文和背景的
对比度不够就退回默认，不让一句描述把演示弄得没法看。
"""

from __future__ import annotations

import json
import re
from typing import Optional

from . import llm

DEFAULT_THEME = {
    "name": "瑞士网格",
    "bg": "#ffffff",          # 页面底色
    "surface": "#f2f2f2",     # 次级底色：对比栏、数字卡、页面外围
    "ink": "#1c1c1c",         # 正文、标题
    "dim": "#6b6b6b",         # 辅助文字、标签
    "line": "#d4d4d4",        # 分隔线、未强调的条形
    "accent": "#1f5fd6",      # 唯一的强调色：要点标记、强调的数字、来源角标
    "font": "sans",           # 正文字体族：sans / serif / mono
    "display_font": "sans",   # 标题字体族
    "title_weight": 700,
    "radius": 0,              # 圆角，瑞士风格是直角
}

# 字体族 → CSS 字体栈 / PPTX 字体名。只给这几种，别的写法一律退回 sans
FONT_STACKS = {
    "sans": '"Helvetica Neue", Helvetica, Arial, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif',
    "serif": '"Iowan Old Style", Georgia, "Songti SC", "Source Han Serif SC", "SimSun", serif',
    "mono": '"SF Mono", Menlo, Consolas, "PingFang SC", "Microsoft YaHei", monospace',
}
PPT_FONTS = {   # (西文, 中文)
    "sans": ("Helvetica Neue", "微软雅黑"),
    "serif": ("Georgia", "宋体"),
    "mono": ("Menlo", "微软雅黑"),
}

_COLOR_KEYS = ("bg", "surface", "ink", "dim", "line", "accent")
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


def _rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _luminance(hex_color: str) -> float:
    def ch(c):
        c = c / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = _rgb(hex_color)
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def contrast(a: str, b: str) -> float:
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def clean_theme(raw) -> dict:
    """把一份（可能不完整、可能有错的）风格参数收敛成能用的：不认识的键丢掉，
    不合规的值退回默认，正文/辅助文字/强调色跟底色对比度不够也退回默认。"""
    t = dict(DEFAULT_THEME)
    if not isinstance(raw, dict):
        return t
    name = str(raw.get("name") or "").strip()
    if name:
        t["name"] = name[:20]
    for k in _COLOR_KEYS:
        v = str(raw.get(k) or "").strip()
        if _HEX.match(v):
            t[k] = v.lower()
    for k in ("font", "display_font"):
        if raw.get(k) in FONT_STACKS:
            t[k] = raw[k]
    try:
        w = int(raw.get("title_weight", t["title_weight"]))
        if 300 <= w <= 900:
            t["title_weight"] = w // 100 * 100
    except (TypeError, ValueError):
        pass
    try:
        r = int(raw.get("radius", t["radius"]))
        if 0 <= r <= 24:
            t["radius"] = r
    except (TypeError, ValueError):
        pass
    # 看得清是底线：对比度不够就整组颜色退回默认（只改一两个颜色容易配出别的问题）
    if (contrast(t["ink"], t["bg"]) < 7 or contrast(t["dim"], t["bg"]) < 4.5
            or contrast(t["accent"], t["bg"]) < 3):
        for k in _COLOR_KEYS:
            t[k] = DEFAULT_THEME[k]
    return t


def css_vars(theme: dict) -> str:
    """风格 → :root 上的 CSS 变量。"""
    t = clean_theme(theme)
    r, g, b = _rgb(t["accent"])
    return (f"--bg:{t['bg']};--surface:{t['surface']};--ink:{t['ink']};--dim:{t['dim']};"
            f"--line:{t['line']};--accent:{t['accent']};--accent-soft:rgba({r},{g},{b},.12);"
            f"--font:{FONT_STACKS[t['font']]};--display:{FONT_STACKS[t['display_font']]};"
            f"--title-weight:{t['title_weight']};--radius:{t['radius']}px;")


STYLE_PROMPT = """把下面这段演示风格描述，转成一组排版参数。

描述：{desc}

只输出一个 JSON 对象，不要解释、不要代码块，键和取值范围如下：
- name：风格名，不超过 8 个字
- bg：页面底色；surface：次级底色（卡片、对比栏用，跟 bg 稍有区别）
- ink：正文和标题颜色；dim：辅助文字颜色；line：分隔线颜色
- accent：唯一的强调色
- 以上颜色都写成 #RRGGBB。正文和底色要有足够对比度（深底配浅字、浅底配深字）
- font：正文字体族，只能是 sans、serif、mono 之一；display_font：标题字体族，同上
- title_weight：标题字重，300 到 900 之间的整百数
- radius：圆角像素，0 到 24 的整数

描述里没提到的项，参考这组默认值：
{default}
"""


def theme_from_description(desc: str, *, backend: str, api_key: str = "", model: str = "",
                           api_base: str = "", timeout: int = 120, stop_flag=None) -> dict:
    """一句风格描述 → 校验过的风格参数（一次模型调用）。描述为空直接给默认。"""
    desc = (desc or "").strip()
    if not desc:
        return dict(DEFAULT_THEME)
    raw = llm.complete(STYLE_PROMPT.format(desc=desc[:500], default=json.dumps(DEFAULT_THEME, ensure_ascii=False)),
                       backend, api_key=api_key, model=model, api_base=api_base,
                       max_tokens=800, timeout=timeout, stop_flag=stop_flag)
    m = re.search(r"\{.*\}", raw or "", re.S)
    try:
        parsed = json.loads(m.group(0)) if m else {}
    except json.JSONDecodeError:
        parsed = {}
    t = clean_theme(parsed)
    t["description"] = desc[:500]
    return t


def describe(theme: Optional[dict]) -> str:
    """给页面和日志看的一行说明。"""
    t = clean_theme(theme or {})
    return (f"{t['name']}：底色 {t['bg']}、正文 {t['ink']}、强调 {t['accent']}，"
            f"正文 {t['font']}、标题 {t['display_font']} {t['title_weight']}")
