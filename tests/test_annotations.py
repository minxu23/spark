from core import annotations as an


OLD = """---
title: x
---

# 第一期

## 要点

算力会继续涨价，==这是第一句重点==[^note-1]。另外==第二句会被改掉的话==[^note-2]。

还有==原样不变的一句==。

## 读后感

值得再读。

[^note-1]: 同意
[^note-2]: 存疑
"""

NEW = """---
title: x
---

# 第一期

## 要点

算力会继续涨价，这是第一句重点。另外第二句换了说法。

还有原样不变的一句。
"""


def test_carry_over_keeps_marks_notes_and_reflection():
    out, moved = an.carry_over(OLD, NEW)
    assert "==这是第一句重点==[^note-1]" in out
    assert "==原样不变的一句==" in out
    # 找不到的划线进「以前的划线」，批注还挂着
    assert an.CARRIED_HEADING in out and "- ==第二句会被改掉的话==[^note-2]" in out
    assert an.reflection(out) == "值得再读。"
    assert an.note_defs(out) == {1: "同意", 2: "存疑"}
    n = an.reading_note(out)
    assert [it["notes"] for it in n["items"]] == [["同意"], [], ["存疑"]]
    assert moved == {}
    # 再生成一次也不会重复
    again, _ = an.carry_over(out, NEW)
    assert again.count("第二句会被改掉的话") == 1
    assert again.count("[^note-1]: 同意") == 1


def test_carry_over_uses_locate_for_reworded_marks():
    def locate(body, text):
        i = body.find("第二句换了说法")
        return (i, i + len("第二句换了说法")) if text.startswith("第二句") and i >= 0 else None
    out, moved = an.carry_over(OLD, NEW, locate=locate)
    assert "==第二句换了说法==[^note-2]" in out
    assert an.CARRIED_HEADING not in out
    assert moved == {"第二句会被改掉的话": "第二句换了说法"}


def test_carry_over_adds_note_refs_to_marks_the_caller_already_placed():
    placed = NEW.replace("第二句换了说法", "==第二句换了说法==")
    out, _ = an.carry_over(OLD, placed, renamed={"第二句会被改掉的话": "第二句换了说法"})
    assert "==第二句换了说法==[^note-2]" in out
    assert out.count("第二句换了说法") == 1


def test_carry_over_without_notes_returns_new_unchanged():
    assert an.carry_over(NEW, NEW + "x") == (NEW + "x", {})
    assert an.carry_over("", NEW) == (NEW, {})
