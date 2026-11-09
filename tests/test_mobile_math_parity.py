"""手机带公式的文章用网页排（apps/mobile-ios/Spark/MathArticle.swift），认公式的规则和 KaTeX 文件要跟电脑阅读页一样。"""

import filecmp
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from apps.reader import server as reader

SWIFT = os.path.join(ROOT, "apps", "mobile-ios", "Spark", "MathArticle.swift")
IOS_KATEX = os.path.join(ROOT, "apps", "mobile-ios", "Spark", "KaTeX")
WEB = os.path.join(ROOT, "apps", "reader", "static")


def _swift_const(name):
    src = open(SWIFT, encoding="utf-8").read()
    return re.search(rf'static let {name} = #"(.*?)"#\n', src).group(1)


def test_math_pattern_same_as_reader():
    # 命名分组的写法 Python 是 (?P<名字>，Swift 用的 ICU 是 (?<名字>，其余一字不差
    assert _swift_const("pattern") == reader._CODE_OR_MATH_RE.pattern.replace("(?P<", "(?<")
    assert _swift_const("mathy") == reader._MATHY_RE.pattern
    assert _swift_const("shortVar") == "^" + reader._SHORT_VAR_RE.pattern + "$"


def test_katex_files_same_as_reader():
    pairs = [("katex.min.js", "katex/katex.min.js"), ("katex.min.css", "katex/katex.min.css"), ("math.js", "math.js")]
    pairs += [(f, "katex/fonts/" + f) for f in os.listdir(os.path.join(WEB, "katex", "fonts"))]
    for ios, web in pairs:
        assert filecmp.cmp(os.path.join(IOS_KATEX, ios), os.path.join(WEB, web), shallow=False), ios
