"""任务卡片里「合并成一份综合报告 / 每个主题分别单独出一份」这两个单选按钮。

没有 JS 运行时能跑 app.js（这个仓库里前端逻辑一律靠像 test_glossary.py 那样
静态检查源码文本来做回归测试），所以这里检查的是导致过一次真实 bug 的那个
具体写法：随机后缀必须在 forEach 循环外生成一次、两个 radio 共用，不能写在
回调里——写在回调里的话两个 radio 会各自拿到不同的随机 name，等于拆成了两个
独立的单选组：互斥失效，而且单独一个 radio 一旦选中，原生行为下再点它自己
不会被取消勾选，表现就是"勾选多个主题时的合并/分别选项一旦选中就无法取消"
（2026-09 报过的真实 bug）。
"""

import os
import re

APP_JS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "static", "app.js")


def _create_task_card_body():
    src = open(APP_JS, encoding="utf-8").read()
    m = re.search(r"function createTaskCard\(title\) \{", src)
    assert m, "没找到 createTaskCard"
    start = m.end()
    depth = 1
    i = start
    while depth > 0:
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
        i += 1
    return src[start:i]


def _rename_block(body):
    m = re.search(
        r'el\.querySelectorAll\(\'input\[type="radio"\]\[name="topicSummaryMode"\]\'\)'
        r'\.forEach\(\(r\) => \{(.*?)\}\);', body, re.S)
    assert m, "没找到 topicSummaryMode 的重命名逻辑"
    return m.group(1)


def test_两个_radio_共用同一个随机后缀_不是各自随机():
    body = _create_task_card_body()
    callback = _rename_block(body)
    assert "Math.random(" not in callback, (
        "随机后缀不能在 forEach 回调里生成——两个 radio 会各自拿到不同的 name，"
        "拆成两个独立的单选组，互斥失效、且单选一旦选中无法取消"
    )
    before_forEach = body[:body.index(callback)]
    assert "Math.random(" in before_forEach, (
        "随机后缀该在 forEach 之前生成一次，两个 radio 共用同一个值"
    )


def test_同一张卡片内两个_radio_用的是同一个变量():
    body = _create_task_card_body()
    callback = _rename_block(body)
    # 回调体里引用的应该是外层已经算好的变量，而不是再造一个新值
    refs = re.findall(r"topicSummaryMode-\$\{(\w+)\}", callback)
    assert refs and len(set(refs)) == 1, (
        f"两个 radio 拼 name 时用的变量应该是同一个，实际引用了: {refs}"
    )
