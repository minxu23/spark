"""词表：Summit / Podcast / 信息跟进 三个入口共用一套界面，靠词表切换文案。

词表最大的风险是"静默换错"——换完的句子没人看就上线了。这几条用例把 index.html
里所有会显示给用户的文本都过一遍词表，检查两类问题：换出重复词，以及该换的没换。
"""

import os
import re

STATIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")


def _read(name):
    with open(os.path.join(STATIC, name), encoding="utf-8") as f:
        return f.read()


def _glossaries():
    """从 app.js 里把词表读出来，而不是在测试里再抄一份——抄一份就会各改各的。"""
    js = _read("app.js")
    block = re.search(r"const GLOSSARIES = \{(.*?)\n  \};", js, re.S)
    assert block, "没找到 GLOSSARIES 定义"
    out = {}
    for mode, body in re.findall(r"(\w+):\s*\[(.*?)\],\n", block.group(1) + "\n"):
        out[mode] = re.findall(r'\["([^"]+)",\s*"([^"]+)"\]', body)
    assert set(out) == {"series", "summit", "track"}, out.keys()
    return out


def _apply(pairs, s):
    for a, b in pairs:
        s = s.replace(a, b)
    return s


def _hidden_blocks():
    """series 模式下整块隐藏的区域，里面的文案换不换都看不见。"""
    html = _read("index.html")
    out = []
    for ident in ("agendaSection", "contentTypeField"):
        i = html.index(f'id="{ident}"')
        start = html.rfind("<", 0, i)
        out.append(html[start:start + 2000])
    return out


def _visible_texts():
    html = _read("index.html")
    hidden = _hidden_blocks()
    texts = []
    for t in re.findall(r">([^<>]+)<", html):
        s = t.strip()
        if s and not any(s in b for b in hidden):
            texts.append(s)
    return texts


def test_词表能读出来():
    g = _glossaries()
    assert ("议题", "单集") in g["series"]


def test_复合词排在前面():
    """原文里有「大会/节目总结」这种并列写法；不先整体替换就会变成「节目/节目总结」。"""
    for mode, pairs in _glossaries().items():
        keys = [a for a, _ in pairs]
        if "大会/节目" in keys and "大会" in keys:
            assert keys.index("大会/节目") < keys.index("大会"), f"{mode}: 复合词必须排在前面"


def test_换完不会出现重复词():
    g = _glossaries()
    bad = []
    for s in _visible_texts():
        for mode, pairs in g.items():
            out = _apply(pairs, s)
            if re.search(r"节目/节目|单集/单集|大会/大会|节目节目|单集单集", out):
                bad.append(f"[{mode}] {s[:50]} → {out[:50]}")
    assert not bad, "词表换出了重复词：\n" + "\n".join(bad)


def test_播客模式下不该再出现峰会词汇():
    """常驻可见的文案里如果还留着"议题""大会"，说明词表漏了一条。"""
    pairs = _glossaries()["series"]
    leftover = []
    for s in _visible_texts():
        out = _apply(pairs, s)
        if re.search(r"议题|大会|峰会", out):
            leftover.append(f"{s[:40]} → {out[:40]}")
    assert not leftover, "这些文案在播客模式下仍是峰会说法：\n" + "\n".join(leftover)
