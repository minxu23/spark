from core import taihe_sync as sync


def test_latest_manual_summary_notes_and_reflection():
    doc = '---\ntitle: "手写"\nsource: "手写"\nurl: ""\nclipped: 2026-11-15\ntldr: "一句摘要"\nstarred: true\npinned: true\n---\n\n# 手写\n\n## 摘要\n\n一句摘要\n\n- 摘要要点\n\n## 正文\n\n==完整正文==[^note-1]\n\n## 读后感\n\n自己的判断\n\n[^note-1]: 批注判断\n'
    r = sync.from_document(doc, "manual-latest")
    sync.validate(r)
    assert r["clip"]["summary"] == "- 摘要要点"
    assert r["clip"]["markdown"] == "==完整正文==[^note-1]"
    assert "自己的判断" in r["document"]
    assert "[^note-1]: 批注判断" in r["document"]
    assert "starred: true" in r["document"] and "pinned: true" in r["document"]


def test_two_million_chinese_characters_are_not_truncated():
    body = "长" * 2_000_000
    doc = '---\ntitle: "长文"\nclipped: 2026-11-15\n---\n\n# 长文\n\n## 正文\n\n' + body + "\n"
    r = sync.from_document(doc, "long-latest")
    sync.validate(r)
    assert r["clip"]["markdown"] == body
    assert len(doc.encode("utf-8")) > 4 * 1024 * 1024
