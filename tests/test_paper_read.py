"""论文的阅读侧（core/paper_read.py）：三个页签的出现条件和默认页签、PDF 接口的路径校验、追问的提示词、
「存进本文」的追加写法和并发、重写摘要 / 翻译 / 改标题以后「## 追问」还在，以及手机 PaperRead.swift 和电脑逐字一致。
笔记库是临时目录，模型是假的，不联网。"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import clip_compose, paper_read as pr, review  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
SWIFT = os.path.join(ROOT, "apps", "mobile-ios", "Spark")
HEX = "0123456789abcdef"
PDF_KEY = f"Spark/论文/attachments/{HEX}.pdf"


def paper(source="PDF", pdf=f"attachments/{HEX}.pdf", body="First paragraph of the paper.\n\nSecond paragraph.",
          url="", summary=True, extra=""):
    head = ["---", f'title: "论文标题"', f'source: "{source}"', f'url: "{url}"', "clipped: 2026-11-20"]
    if summary:
        head += ['tags: ["机器学习"]', 'tldr: "一句话"']
    if pdf:
        head.append(f'pdf: "{pdf}"')
    head.append("---")
    lines = head + ["", "# 论文标题", "", f"- 来源：{source}", "- 收录：2026-11-20", ""]
    if summary:
        lines += ["## 摘要", "", "一句话", "", "### 这篇在讲什么", "", "讲了一件事。", ""]
    lines += ["## 正文", "", body, ""]
    if url:
        lines += [f"[阅读原文]({url})", ""]
    return "\n".join(lines) + extra


@pytest.fixture
def lib(tmp_path, monkeypatch):
    v = tmp_path / "vault"
    (v / "Spark" / "论文" / "attachments").mkdir(parents=True)
    monkeypatch.setenv("SPARK_VAULT", str(v))
    return v


def _write(v, name, text):
    p = v / "Spark" / "论文" / name
    p.write_text(text, encoding="utf-8")
    return p


# ---------------------------------------------------------------- 页签

def test_从PDF取字的判断():
    assert pr.from_pdf(paper(source="PDF"))                                  # 上传的
    assert pr.from_pdf(paper(source="example.com", url="https://example.com/a.pdf"))   # PDF 链接
    assert pr.from_pdf(paper(source="arXiv", url="https://arxiv.org/abs/2601.00001"))  # arXiv 网页版没有：只有段落
    html_body = "## 1 Introduction\n\nWe study.\n\n## 2 Method\n\nWe do."
    assert not pr.from_pdf(paper(source="arXiv", url="https://arxiv.org/abs/2601.00001", body=html_body))
    # 收录时 PDF 取字把参考文献那一行写成「## References」：不算小标题，还是 PDF 取的字
    pdf_body = "We study things.\n\n## References\n\n[1] A paper."
    assert pr.from_pdf(paper(source="arXiv", url="https://arxiv.org/abs/2601.00001", body=pdf_body))
    assert pr.from_pdf(paper(source="arXiv", url="https://arxiv.org/abs/2601.00001", body="x\n\n## 7. Bibliography"))
    assert not pr.from_pdf(paper(source="arXiv", url="https://arxiv.org/abs/2601.00001", body=pdf_body + "\n\n## Appendix"))
    assert not pr.from_pdf(paper(pdf=""))                                    # 没有 pdf: 一行
    assert not pr.from_pdf(paper(pdf="attachments/notahash.pdf"))            # 不合规
    assert pr.pdf_key(paper(pdf=PDF_KEY)) == PDF_KEY                         # 写成从库根算起的也认
    assert pr.pdf_key(paper(pdf=f"../attachments/{HEX}.pdf")) == ""
    assert pr.pdf_key(paper(pdf=f"Spark/剪藏/attachments/{HEX}.pdf")) == ""


def test_页签和默认页签():
    assert pr.tabs(paper(), True) == (["guide", "text", "pdf"], "pdf")
    assert pr.tabs(paper(), False) == (["guide", "text"], "guide")           # 本机没有原件：没有 PDF 页签
    arxiv_html = paper(source="arXiv", url="https://arxiv.org/abs/2601.00001", body="## 1 Intro\n\nx")
    assert pr.tabs(arxiv_html, True) == (["guide", "text", "pdf"], "guide")  # 网页版全文的：有 PDF 页签，默认导读
    assert pr.tabs(paper(pdf=""), True) == (["guide", "text"], "guide")


# ---------------------------------------------------------------- PDF 接口

@pytest.fixture
def client(lib):
    from werkzeug.test import Client
    import spark
    return Client(spark.application)


def test_PDF接口的路径校验(lib, client, tmp_path):
    (lib / "Spark" / "论文" / "attachments" / f"{HEX}.pdf").write_bytes(b"%PDF-1.4 test")
    (lib / "Spark" / "剪藏" / "attachments").mkdir(parents=True)
    (lib / "Spark" / "剪藏" / "attachments" / f"{HEX}.pdf").write_bytes(b"%PDF-1.4 clip")
    outside = tmp_path / f"{HEX}.pdf"
    outside.write_bytes(b"%PDF-1.4 outside")
    get = lambda key: client.get("/read/api/paper_pdf", query_string={"key": key})
    r = get(PDF_KEY)
    assert r.status_code == 200 and r.data == b"%PDF-1.4 test" and r.headers["Content-Type"] == "application/pdf"
    assert get(f"Spark/论文/attachments/../attachments/{HEX}.pdf").status_code == 404
    assert get(f"Spark/论文/../../{HEX}.pdf").status_code == 404
    assert get(f"Spark/剪藏/attachments/{HEX}.pdf").status_code == 404     # 别的文件夹
    assert get(f"Spark/论文/attachments/{'f' * 16}.pdf").status_code == 404  # 不存在
    assert get(f"Spark/论文/attachments/{HEX}.PDF").status_code == 404
    assert get(str(outside)).status_code == 404
    assert client.get("/read/api/paper_pdf").status_code == 404
    # attachments 里是个指到库外的软链接：realpath 出了文件夹，不给
    link = lib / "Spark" / "论文" / "attachments" / f"{'a' * 16}.pdf"
    os.symlink(outside, link)
    assert get(f"Spark/论文/attachments/{'a' * 16}.pdf").status_code == 404


def test_阅读页的页签和分块(lib, client):
    (lib / "Spark" / "论文" / "attachments" / f"{HEX}.pdf").write_bytes(b"%PDF-1.4 test")
    _write(lib, "a.md", pr.appending(paper(), [{"q": "问一", "a": "答一"}]))
    page = client.get("/read/f/论文/a.md").get_data(as_text=True)
    assert 'class="paper-tabs"' in page and 'data-pane="pdf"' in page and '<article data-pane="pdf" ' in page
    assert "/read/api/paper_pdf?key=" in page and 'class="paper-ask"' in page
    guide = page.split('<div class="paper-pane" data-pane="guide">', 1)[1].split('<div class="paper-pane" data-pane="text">', 1)
    assert "讲了一件事" in guide[0] and "问一" in guide[0] and "First paragraph" not in guide[0]
    assert "First paragraph" in guide[1] and "问一" not in guide[1]
    # 没有原件：没有 PDF 页签，默认导读
    _write(lib, "b.md", paper(pdf=""))
    page = client.get("/read/f/论文/b.md").get_data(as_text=True)
    assert '<article data-pane="guide" ' in page and 'data-pane="pdf"' not in page
    # 剪藏的阅读页不变
    (lib / "Spark" / "剪藏").mkdir(exist_ok=True)
    (lib / "Spark" / "剪藏" / "c.md").write_text(paper(), encoding="utf-8")
    page = client.get("/read/f/剪藏/c.md").get_data(as_text=True)
    assert "paper-tabs" not in page and "paper-pane" not in page and "paper-ask" not in page


def test_论文阅读页的参考文献默认折起(lib, client):
    body = ("Intro text, long enough to be the bulk of the paper. " * 6
            + "\n\n## References\n\n[1] A. B. Paper.\n\n[2] C. D. Other.\n\n## Appendix\n\nExtra proof.")
    _write(lib, "a.md", paper(body=body, extra="\n## 追问\n\n### 问\n\n答。\n"))
    page = client.get("/read/f/论文/a.md").get_data(as_text=True)
    fold = page.split('<details class="refs">', 1)[1].split("</details>", 1)[0]
    assert '<details class="refs">' in page and '<details class="refs" open' not in page
    assert fold.startswith("<summary><h2 ") and "References</h2></summary>" in fold
    assert "[1] A. B. Paper." in fold and "[2] C. D. Other." in fold
    assert "Appendix" not in fold and "Extra proof" not in fold and "追问" not in fold
    assert page.count('<details class="refs">') == 1
    assert "Extra proof" in page.split("</details>", 1)[1]          # 折叠之后的附录在外面
    # 不折叠的对照：改成别的标题名
    other = body.replace("## References", "## Notes")
    _write(lib, "b.md", paper(body=other))
    plain = client.get("/read/f/论文/b.md").get_data(as_text=True)
    assert 'class="refs"' not in plain
    # 标题太靠前（不到全文的 1/5）和摘要截断一样不折
    early = "## References\n\n[1] x\n\n" + "Long body text here. " * 40
    _write(lib, "c.md", paper(body=early))
    assert 'class="refs"' not in client.get("/read/f/论文/c.md").get_data(as_text=True)


def test_参考文献折叠只管论文(lib, client):
    body = "Intro text, long enough to be the bulk of the paper. " * 6 + "\n\n## References\n\n[1] A. B. Paper.\n"
    (lib / "Spark" / "剪藏").mkdir(exist_ok=True)
    (lib / "Spark" / "剪藏" / "c.md").write_text(paper(body=body), encoding="utf-8")
    assert 'class="refs"' not in client.get("/read/f/剪藏/c.md").get_data(as_text=True)
    _write(lib, "none.md", paper(body="Just text with no references at all."))
    assert 'class="refs"' not in client.get("/read/f/论文/none.md").get_data(as_text=True)


def test_折叠不改划线的位置(lib, client):
    body = ("Intro text, long enough to be the bulk of the paper. " * 6
            + "\n\n## References\n\n[1] A. B. Paper.\n\n[2] A. B. Paper.\n")
    p = _write(lib, "m.md", paper(body=body))
    # 划线按「文字 + 前文」回源码里找，不看页面结构：折叠里的第二处（前文 [2] ）划在源码里的第二处
    r = client.post("/read/api/highlight", json={"path": "论文/m.md", "mtime": str(p.stat().st_mtime_ns),
                                                 "text": "A. B. Paper.", "before": "[2] "})
    if r.status_code == 409:
        r = client.post("/read/api/highlight", json={"path": "论文/m.md", "mtime": r.get_json().get("mtime", ""),
                                                     "text": "A. B. Paper.", "before": "[2] "})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert "[1] A. B. Paper.\n\n[2] ==A. B. Paper.==" in p.read_text(encoding="utf-8")
    html_back = r.get_json()["html"]                       # 划线后换上的正文也带折叠
    fold = html_back.split('<details class="refs">', 1)[1].split("</details>", 1)[0]
    assert "[2] <mark>A. B. Paper.</mark>" in fold and "<mark>" not in html_back.split("</details>", 1)[0].split("<details")[0]


# ---------------------------------------------------------------- 提示词

def test_正文去掉参考文献_截断():
    long = "Intro text, long enough to be the bulk of the paper. " * 4
    body = long + "\n\n![图](attachments/x.png)\n\n\n\nMore.\n\n## References\n\n[1] A. B. Paper."
    t = pr.paper_text(paper(body=body))
    assert t == long + "\n\nMore."
    # 参考文献的规则同写摘要（clip_compose.before_references）：「## 」标题行，前面的字不到 1/5 的不截
    assert pr.paper_text(paper(body="a\n\n## 7. REFERENCES\n\n" + "b" * 100)) == "a\n\n## 7. REFERENCES\n\n" + "b" * 100
    assert pr.paper_text(paper(body="a" * 100 + "\n\n## 参考文献\n\nc")) == "a" * 100
    assert pr.paper_text(paper(body="x" * (pr.MAX_PAPER_CHARS + 50))) == "x" * pr.MAX_PAPER_CHARS
    # 追问一节不进正文
    t = pr.paper_text(pr.appending(paper(), [{"q": "问", "a": "答"}]))
    assert "问" not in t and "Second paragraph." in t


def test_回答提示词带历史_只根据这篇():
    hist = [{"q": f"问{i}", "a": "答" * 900} for i in range(8)]
    p = pr.answer_prompt("标题", "正文", "最后一问", hist)
    assert "请只根据这篇论文" in p and pr.NOT_IN_PAPER in p and "问7" in p and "问1\n" not in p and "问2" in p
    assert "答" * 800 + "……" in p and p.endswith("===== 我最后的问题 =====\n最后一问\n")
    assert "之前的问答" not in pr.answer_prompt("标题", "正文", "q", [])


# ---------------------------------------------------------------- 存进本文

def test_追加_没有这一节():
    text = paper()
    new = pr.appending(text, [{"q": "问一\n换行", "a": "答一"}])
    assert new.startswith(text.rstrip("\n"))
    assert new.endswith("\n\n## 追问\n\n### 问一 换行\n\n答一\n")


def test_追加_已有这一节_不另开():
    one = pr.appending(paper(url="https://example.com/a"), [{"q": "问一", "a": "答一"}])
    two = pr.appending(one, [{"q": "问二", "a": "答二"}, {"q": "问三", "a": "答三"}])
    assert two.startswith(one.rstrip("\n")) and two.count("## 追问") == 1
    assert two.endswith("### 问一\n\n答一\n\n### 问二\n\n答二\n\n### 问三\n\n答三\n")


def test_追加_后面还有读后感和批注脚注():
    from core import annotations
    text = pr.appending(paper(), [{"q": "问一", "a": "答一"}])
    text = annotations.with_reflection(text, "我的读后感") + "\n[^note-1]: 批注\n"
    new = pr.appending(text, [{"q": "问二", "a": "答二"}])
    # 接在追问一节最后，读后感、脚注原样在后面
    i = new.index("### 问二")
    assert new.index("### 问一") < i < new.index("## 读后感") < new.index("[^note-1]: 批注")
    assert annotations.reflection(new) == "我的读后感" and new.count("## 追问") == 1
    assert new.replace("### 问二\n\n答二\n\n", "") == text


def test_追加_文件末尾无换行():
    text = paper().rstrip("\n")
    assert not text.endswith("\n")
    new = pr.appending(text, [{"q": "问", "a": "答"}])
    assert new == text + "\n\n## 追问\n\n### 问\n\n答\n"
    again = pr.appending(new.rstrip("\n"), [{"q": "问2", "a": "答2"}])
    assert again == new + "\n### 问2\n\n答2\n"


def test_回答里的划线记号_没关上的代码栅栏():
    a = "a ==假划线== b\n===\n```python\nx == 1\n"
    out = pr.answer_lines(a)
    assert out == "a \\=\\=假划线\\=\\= b\n\\=\\==\n```python\nx \\=\\= 1\n```"
    from core import annotations
    new = pr.appending(paper(), [{"q": "问", "a": a}])
    assert not annotations.MARK_TEXT_RE.search(new[new.index("## 追问"):])
    assert pr.answer_lines("```\ncode\n```") == "```\ncode\n```"           # 成对的不补
    assert pr.answer_lines("x\n  ```\ncode") == "x\n  ```\ncode\n```"


def test_送模型的正文去掉划线记号和批注号():
    text = paper(body="We ==propose a method==[^note-3] that works.\n\n[^note-3]: 我的批注")
    t = pr.paper_text(text)
    assert "We propose a method that works." in t and "==" not in t and "[^note-3]" not in t


def test_CRLF文件的追问标题不另开一节():
    one = pr.appending(paper(), [{"q": "问一", "a": "答一"}]).replace("\n", "\r\n")
    two = pr.appending(one, [{"q": "问二", "a": "答二"}])
    assert two.count("## 追问") == 1 and two.startswith(one.rstrip("\n"))
    assert pr.followup_range(one) is not None and "问一" not in clip_compose.body_of(one, True)


def test_回答里的标题行降级():
    a = "# 一级\n## 二级\n### 三级\n#### 四级\n  ## 缩进\n#话题\n[^note-1]: 假脚注\n[阅读原文](x)\n正文 ## 不在行首"
    new = pr.appending(paper(), [{"q": "## 问题也带井号", "a": a}])
    sec = new[new.index("## 追问"):]
    assert "#### 一级\n#### 二级\n#### 三级\n#### 四级\n#### 缩进\n#话题\n\\[^note-1]: 假脚注\n\\[阅读原文](x)" in sec
    import re
    assert re.findall(r"(?m)^## .*$", new)[-1] == "## 追问" and re.findall(r"(?m)^## ", sec) == ["## "]
    from core import annotations
    assert annotations.note_defs(new) == {}
    assert pr.followup_range(new)[1] == len(new)


def test_并发两次追加都留下(lib):
    p = _write(lib, "a.md", paper())
    errors = []

    def go(i):
        try:
            pr.save("Spark/论文/a.md", [{"q": f"问{i}", "a": f"答{i}"}])
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    ts = [threading.Thread(target=go, args=(i,)) for i in range(6)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    text = p.read_text(encoding="utf-8")
    assert not errors and text.count("## 追问") == 1
    assert all(f"### 问{i}\n\n答{i}\n" in text for i in range(6))
    assert text.startswith(paper().rstrip("\n"))


def test_存进本文前重新读盘(lib):
    p = _write(lib, "a.md", paper())
    p.write_text(paper() + "\n手机上刚加的一行\n", encoding="utf-8")   # 页面打开以后别处改过
    new = pr.save("Spark/论文/a.md", [{"q": "问", "a": "答"}])
    assert "手机上刚加的一行" in new and p.read_text(encoding="utf-8") == new


def test_存进本文的接口和校验(lib, client):
    _write(lib, "a.md", paper())
    ok = client.post("/read/api/paper_ask/save", json={"path": "论文/a.md", "turns": [{"q": "问", "a": "答"}]})
    assert ok.status_code == 200 and "问" in ok.get_json()["html"] and ok.get_json()["mtime"]
    (lib / "Spark" / "剪藏").mkdir(exist_ok=True)
    (lib / "Spark" / "剪藏" / "c.md").write_text(paper(), encoding="utf-8")
    bad = [{"path": "剪藏/c.md", "turns": [{"q": "q", "a": "a"}]},
           {"path": "论文/../剪藏/c.md", "turns": [{"q": "q", "a": "a"}]},
           {"path": "论文/a.md", "turns": []},
           {"path": "论文/a.md", "turns": [{"q": " ", "a": "a"}]},
           {"path": "论文/a.md", "turns": [{"q": "q", "a": "a" * (pr.MAX_ANSWER_CHARS + 1)}]},
           {"path": "论文/a.md", "turns": "x"},
           {"path": "论文/没有.md", "turns": [{"q": "q", "a": "a"}]}]
    for b in bad:
        assert client.post("/read/api/paper_ask/save", json=b).status_code == 400, b
    assert "### q" not in (lib / "Spark" / "剪藏" / "c.md").read_text(encoding="utf-8")


# ---------------------------------------------------------------- 会重写文件的操作保住「## 追问」

def _with_followup(**kw):
    return pr.appending(paper(**kw), [{"q": "问一", "a": "答一\n\n## 小标题"}])


@pytest.mark.parametrize("url", ["", "https://example.com/a.pdf"])
def test_重写摘要_翻译_改标题后追问还在(url):
    text = _with_followup(url=url)
    sec = text[pr.followup_range(text)[0]:]
    # 原文、翻译的范围不含追问
    assert "问一" not in clip_compose.body_of(text, True) and clip_compose.body_of(text, True).endswith("Second paragraph.")
    translated = clip_compose.replacing_body(text, "译过的正文\n\n> 中文", paper=True)
    assert translated.endswith(sec) and "译过的正文" in translated and "Second paragraph" not in translated
    assert ("[阅读原文](" in translated) == bool(url)
    resummarized = clip_compose.with_summary(text, {"tldr": "新的一句话", "tags": ["新"], "body": "- 要点"})
    assert resummarized.endswith(sec) and "新的一句话" in resummarized
    from apps.reader.server import _retitled
    assert _retitled(text, "新标题").endswith(sec)
    # 需不需要补翻译只看正文（追问里的英文不算）
    english = _with_followup(url=url).replace("答一", "This answer is entirely English and long enough to look like a paragraph.")
    assert clip_compose.wants_translation(english, paper=True) == clip_compose.wants_translation(paper(url=url), paper=True)
    from core import clip_capture
    assert clip_capture.needs_fill(english, paper=True) == clip_capture.needs_fill(paper(url=url), paper=True)
    # 读后感照旧
    from core import annotations
    with_refl = annotations.with_reflection(text, "感想")
    assert pr.followup_range(with_refl) and "问一" in with_refl[pr.followup_range(with_refl)[0]:]
    assert annotations.reflection(with_refl) == "感想"


def test_文章自己的追问小标题不算():
    # 剪藏（有原文链接）正文里自己的「## 追问」小标题：在「阅读原文」前面，不当追问一节
    text = paper(url="https://example.com/a", body="开头\n\n## 追问\n\n文章里的一节")
    assert pr.followup_range(text) is None and "文章里的一节" in clip_compose.body_of(text, True)
    new = pr.appending(text, [{"q": "问", "a": "答"}])
    assert new.count("## 追问") == 2 and "文章里的一节" in clip_compose.body_of(new, True) and "### 问" not in clip_compose.body_of(new, True)


def test_剪藏不认追问_翻译和重写摘要照旧覆盖全文():
    # 没有原文链接的剪藏，正文里自己有「## 追问」小标题：翻译、补翻译的判断、重写摘要都跟改动前一样，覆盖整个正文
    text = paper(source="手写", pdf="", body="开头\n\n## 追问\n\n文章里的一节，这一段也是正文")
    assert clip_compose.body_of(text) == "开头\n\n## 追问\n\n文章里的一节，这一段也是正文"
    assert clip_compose.article_of(text)["markdown"] == clip_compose.body_of(text)
    whole = clip_compose.replacing_body(text, "译过的整篇")
    assert whole.endswith("## 正文\n\n译过的整篇") and "文章里的一节" not in whole
    english = paper(source="手写", pdf="", body="Intro.\n\n## 追问\n\n" + "This tail paragraph is English and long enough to be translated. " * 4)
    assert clip_compose.wants_translation(english)
    assert not clip_compose.wants_translation(english, paper=True)
    from core import clip_capture
    assert clip_capture.needs_fill(english.replace("## 摘要", "## 摘要x")) is True
    # 论文才把它当边界
    assert "文章里的一节" not in clip_compose.body_of(text, True)
    assert clip_compose.replacing_body(text, "译", paper=True).endswith("\n\n## 追问\n\n文章里的一节，这一段也是正文\n")


# ---------------------------------------------------------------- 追问的任务：和讨论共用一个位子

def _wait(c, job_id):
    for _ in range(300):
        st = c.get(f"/review/api/progress/{job_id}").get_json()
        if st.get("done"):
            return st
        time.sleep(0.02)
    raise AssertionError("没结束")


def test_追问接口(lib, client, monkeypatch):
    _write(lib, "a.md", paper(body="Body text.\n\n## References\n\n[1] x"))
    calls = []
    monkeypatch.setattr(review, "_llm", lambda prompt, max_tokens=8000: calls.append(prompt) or "回答。")
    assert client.post("/review/api/paper_ask", json={"path": "Spark/论文/a.md"}).status_code == 400
    assert client.post("/review/api/paper_ask", json={"path": "Spark/剪藏/a.md", "question": "q"}).status_code == 400
    assert client.post("/review/api/paper_ask", json={"path": "Spark/论文/../论文/a.md", "question": "q"}).status_code == 400
    r = client.post("/review/api/paper_ask", json={"path": "Spark/论文/a.md", "question": "讲了什么",
                                                   "history": [{"q": "前一问", "a": "前一答"}]})
    st = _wait(client, r.get_json()["job_id"])
    assert st["ok"] and st["kind"] == "paper_ask" and st["result"] == {"answer": "回答。", "sources": []}
    assert "Body text." in calls[0] and "[1] x" not in calls[0] and "问：前一问\n答：前一答" in calls[0]


def test_追问和讨论一次只跑一个_本地模型时等复盘(lib, client, monkeypatch):
    import apps.review.server as srv
    _write(lib, "a.md", paper())
    body = {"path": "Spark/论文/a.md", "question": "q"}

    def running(kind):
        srv.JOBS["x"] = {"id": "x", "kind": kind, "params": {}, "done": False, "ok": False, "stage": "", "error": "",
                         "result": None, "created_at": 0, "finished_at": 0}
    try:
        with srv.LOCK:
            running("discuss")
        assert client.post("/review/api/paper_ask", json=body).status_code == 429
        with srv.LOCK:
            running("paper_ask")
        assert client.post("/review/api/discuss", json={"question": "q"}).status_code == 429
        assert client.post("/review/api/paper_ask", json=body).status_code == 429
        monkeypatch.setattr(srv, "_one_at_a_time", lambda: True)
        assert client.post("/review/api/run", json={"kind": "tag", "tag": "线"}).status_code == 429
        with srv.LOCK:
            running("tag")
        assert client.post("/review/api/paper_ask", json=body).status_code == 429
        assert client.post("/review/api/run", json={"kind": "paper_ask"}).status_code == 400
    finally:
        with srv.LOCK:
            srv.JOBS.pop("x", None)


# ---------------------------------------------------------------- 手机 PaperRead.swift 逐字一致

MAIN = r"""import Foundation
let cases = try! JSONSerialization.jsonObject(with: FileHandle.standardInput.readDataToEndOfFile()) as! [String: Any]
var out: [String: Any] = [:]
func turns(_ a: Any?) -> [DiscussPrompt.Turn] { (a as! [[String: String]]).map { DiscussPrompt.Turn(q: $0["q"]!, a: $0["a"]!) } }
out["prompt"] = (cases["prompt"] as! [[String: Any]]).map {
    PaperRead.answerPrompt(title: $0["title"] as! String, paper: $0["paper"] as! String, question: $0["question"] as! String, history: turns($0["history"]))
}
out["paper"] = (cases["texts"] as! [String]).map { PaperRead.paperText($0) }
out["append"] = (cases["append"] as! [[String: Any]]).map { PaperRead.appending($0["text"] as! String, turns: turns($0["turns"])) }
out["body"] = (cases["texts"] as! [String]).map { PaperRead.body($0) }
out["fromPDF"] = (cases["texts"] as! [String]).map { PaperRead.fromPDF($0) }
out["key"] = (cases["texts"] as! [String]).map { PaperRead.pdfKey($0) }
out["before"] = (cases["texts"] as! [String]).map { PaperRead.beforeFollowUp($0) }
out["notIn"] = PaperRead.notInPaper
out["mark"] = PaperRead.markPattern
print(String(decoding: try! JSONSerialization.data(withJSONObject: out), as: UTF8.self))
"""


def test_两端提示词和存盘格式逐字一致(tmp_path):
    if not shutil.which("swiftc"):
        pytest.skip("没有 swiftc")
    main = tmp_path / "main.swift"
    main.write_text(MAIN, encoding="utf-8")
    exe = tmp_path / "paper_read"
    shared = os.path.join(ROOT, "apps", "mobile-ios", "Shared")
    r = subprocess.run(["swiftc", "-o", str(exe), os.path.join(SWIFT, "DiscussPrompt.swift"), os.path.join(SWIFT, "PaperRead.swift"),
                        os.path.join(shared, "Paper.swift"), os.path.join(shared, "SparkError.swift"), str(main)],
                       capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr
    hist = [{"q": f"第{i}问", "a": f"第{i}答\n\n\n\n后面" + "长" * 900} for i in range(1, 9)]
    with_q = pr.appending(paper(), [{"q": "问一", "a": "答一"}])
    texts = [paper(), paper(source="arXiv", url="https://arxiv.org/abs/2601.00001"),
             paper(source="arXiv", url="https://arxiv.org/abs/2601.00001", body="## 1 Intro\n\nx\n\n## References\n\n[1] y"),
             paper(pdf=""), paper(pdf=PDF_KEY), paper(pdf="attachments/x.pdf"), paper(summary=False),
             paper(body="a\n\n![i](attachments/x.png)\n\n\n\nb\n\n7. REFERENCES\n\nc\n\nBibliography \n\nd"),
             paper(body="x" * (pr.MAX_PAPER_CHARS + 10)), paper(body="汉字" * 20000),
             with_q, with_q.rstrip("\n"), pr.appending(with_q, [{"q": "问二", "a": "答二"}]),
             paper(url="https://example.com/a", body="开头\n\n## 追问\n\n文章里的一节"),
             "没有文件头的字\n\n## 正文\n\nbody", "", "---\nsource: arXiv\npdf: attachments/" + HEX + ".pdf\n---\n\n## 正文\n\n段落",
             paper(source="arXiv", url="https://arxiv.org/abs/2601.00001", body="We study.\n\n## References\n\n[1] x"),
             paper(source="arXiv", url="https://arxiv.org/abs/2601.00001", body="x\n\n## 7. BIBLIOGRAPHY\n\n## Appendix"),
             paper(body="long " * 50 + "\n\n## References\n\n[1] x"),
             paper(body="We ==propose a method==[^note-3] that ===x=== works.\n\n[^note-3]: 批注"),
             with_q.replace("\n", "\r\n"), pr.appending(paper(), [{"q": "q", "a": "a"}]).replace("## 追问\n", "## 追问 \t\r\n")]
    from core import annotations
    refl = annotations.with_reflection(with_q, "感想") + "\n[^note-1]: 批注\n"
    turns2 = [{"q": "## 问\n题", "a": "# 一\n## 二\n### 三\n#### 四\n  ## 缩进\n#话题\n[^note-1]: x\n[阅读原文](y)\n\n\n正文"},
              {"q": "第二问", "a": "  前后空白  \n"},
              {"q": "第三问", "a": "a ==假== b\n===\n  ```python\nx == 1"}]
    cases = {
        "prompt": [{"title": "T", "paper": "正文", "question": "问", "history": []},
                   {"title": "标题 \"引号\"", "paper": "正文\n\n二段", "question": "还有", "history": hist}],
        "texts": texts,
        "append": [{"text": t, "turns": turns2} for t in texts + [refl, refl.rstrip("\n")]],
    }
    want = {
        "prompt": [pr.answer_prompt(c["title"], c["paper"], c["question"], c["history"]) for c in cases["prompt"]],
        "paper": [pr.paper_text(t) for t in texts],
        "append": [pr.appending(c["text"], c["turns"]) for c in cases["append"]],
        "body": [pr.paper_body(t) for t in texts],
        "fromPDF": [pr.from_pdf(t) for t in texts],
        "key": [pr.pdf_key(t) for t in texts],
        "before": [pr.before_followup(t) for t in texts],
    }
    run = subprocess.run([str(exe)], input=json.dumps(cases), capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    got = json.loads(run.stdout)
    assert got["notIn"] == pr.NOT_IN_PAPER
    from core import annotations
    assert got["mark"] == annotations.MARK_PATTERN
    for key, ws in want.items():
        assert len(got[key]) == len(ws), key
        for i, (g, w) in enumerate(zip(got[key], ws)):
            assert g == w, f"{key}[{i}]"


# ---------------------------------------------------------------- 写回前重读、和别的写操作排开

def test_手机翻译和重写摘要在最新那份上写回():
    src = open(os.path.join(SWIFT, "ReaderShell.swift"), encoding="utf-8").read()
    tr = src[src.index("private func translate()"):src.index("private func resummarize()")]
    rs = src[src.index("private func resummarize()"):src.index("static func isLeadBlock")]
    # 模型跑完以后重读手机上的文件，在最新那份上换；翻译时正文变了就不写（同电脑 clip_capture._fill_one）
    assert tr.index("ClipTranslate.run(") < tr.index("let latest = vault.localText(path)") < tr.index("replacingBody(latest, r.text, paper: paper)")
    assert "Self.translatable(latest, paper: paper) == body" in tr and "replacingBody(text" not in tr
    assert rs.index("Clips.summarize(") < rs.index("let latest = vault.localText(path)") < rs.index("DocEdit.withSummary(latest, s)")
    assert "withSummary(text" not in rs
    # 「## 追问」只对论文：调用方按路径传
    assert "PathRules.isPaper(path) ? PaperRead.beforeFollowUp(body) : body" in src
    ct = open(os.path.join(SWIFT, "ClipTranslate.swift"), encoding="utf-8").read()
    assert "if paper, end == rest.endIndex, PaperRead.hasFollowUp(text)" in ct


def test_网页存进本文不冲掉没存的读后感_和别的写排开():
    js = open(os.path.join(ROOT, "apps", "reader", "static", "paper-read.js"), encoding="utf-8").read()
    reader = open(os.path.join(ROOT, "apps", "reader", "static", "reader.js"), encoding="utf-8").read()
    assert "window.SparkDocWrite = {" in reader and ".refl-input" in reader and '.refl-draft[aria-busy="true"]' in reader
    save = js[js.index("async function save(turns)"):]
    assert save.index("w?.busy()") < save.index("w?.setBusy(true)") < save.index("paper_ask/save")
    assert save.index("doc.dataset.mtime = d.mtime") < save.index("if (w?.dirty())") < save.index("doc.innerHTML = d.html")
    assert "刷新后可见" in save and "w?.setBusy(false)" in save


def test_补摘要翻译在锁里重读写回(lib, monkeypatch):
    from core import clip_capture, clips
    p = _write(lib, "a.md", paper(url="https://example.com/a"))
    monkeypatch.setattr(clip_compose, "summarize", lambda *a, **k: {"tldr": "新", "tags": ["t"], "body": ""})
    done = threading.Event()
    with clips.DOC_LOCK:
        job = clip_capture._new_job("x")
        t = threading.Thread(target=lambda: (clip_capture._fill_one(job, "Spark/论文/a.md", "resummarize", str(lib), None, ""),
                                             done.set()))
        t.start()
        time.sleep(0.3)
        assert not done.is_set()   # 等着锁
        p.write_text(p.read_text(encoding="utf-8") + "\n## 追问\n\n### 锁里加的\n\n答\n", encoding="utf-8")
    t.join(5)
    clip_capture._update(job, done=True)
    text = p.read_text(encoding="utf-8")
    assert done.is_set() and "锁里加的" in text and 'tldr: "新"' in text


@pytest.mark.parametrize("url,body", [
    ("/read/api/highlight", {"text": "First paragraph"}),
    ("/read/api/note", {"text": "First paragraph", "before": "", "note": "批注"}),
    ("/read/api/reflection", {"text": "读后感"}),
])
def test_阅读页的笔记写文件在锁里(lib, client, url, body):
    from core import clips
    _write(lib, "a.md", paper())
    res = {}
    with clips.DOC_LOCK:
        t = threading.Thread(target=lambda: res.update(r=client.post(url, json=dict(body, path="论文/a.md"))))
        t.start()
        time.sleep(0.3)
        assert "r" not in res   # 等着锁
    t.join(5)
    assert res["r"].status_code == 200, res["r"].get_data(as_text=True)


def test_本地模型时跨模块一次只跑一件事(lib, client, monkeypatch):
    import apps.review.server as srv
    from core import clip_capture, remote_tasks
    _write(lib, "a.md", paper())
    monkeypatch.setattr(clip_capture, "_ask", lambda: (lambda *a, **k: ""))
    monkeypatch.setattr(clip_capture, "active_count", lambda: 1)
    body = {"path": "Spark/论文/a.md", "question": "q"}
    # 后端是 api：照旧不管阅读页的任务
    monkeypatch.setattr(srv, "_one_at_a_time", lambda: False)
    monkeypatch.setattr(review, "_llm", lambda prompt, max_tokens=8000: "答")
    r = client.post("/review/api/paper_ask", json=body)
    assert r.status_code == 200
    _wait(client, r.get_json()["job_id"])
    # 后端是 ollama：阅读页有剪藏、论文在写摘要或翻译，讨论、追问、生成复盘都等
    monkeypatch.setattr(srv, "_one_at_a_time", lambda: True)
    assert client.post("/review/api/paper_ask", json=body).status_code == 429
    assert client.post("/review/api/discuss", json={"question": "q"}).status_code == 429
    assert client.post("/review/api/run", json={"kind": "tag", "tag": "线"}).status_code == 429
    # 反过来：复盘那边有任务在跑，阅读页的补摘要、翻译在 ollama 时不开始
    monkeypatch.setattr(clip_capture, "review_jobs", lambda: 1)
    monkeypatch.setattr(clip_capture, "ollama_backend", lambda: True)
    with pytest.raises(remote_tasks.TaskError):
        clip_capture.start_fill({"paths": ["Spark/论文/a.md"], "mode": "fill"})
    monkeypatch.setattr(clip_capture, "ollama_backend", lambda: False)
    monkeypatch.setattr(clip_capture, "_fill_run", lambda *a: None)
    started = clip_capture.start_fill({"paths": ["Spark/论文/a.md"], "mode": "fill"})
    clip_capture._update(started["id"], done=True)
    # spark.py 接上了复盘的任务表
    import spark  # noqa: F401
    monkeypatch.undo()
    with srv.LOCK:
        srv.JOBS["x"] = {"id": "x", "kind": "tag", "done": False}
    try:
        assert clip_capture.review_jobs() == 1
    finally:
        with srv.LOCK:
            srv.JOBS.pop("x", None)
