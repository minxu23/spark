"""论文第二阶段：把论文收进来（core/papers.py；手机 apps/mobile-ios/Shared/Paper.swift、Spark/Papers.swift）。
不联网：网络请求都换成录好的小样本（自己构造的 Atom XML、一小段 HTML、一个最小的带文字层的 PDF）。
两端要一致的规则用 swiftc 把 Paper.swift 单独编出来，对同一张表。"""

import io
import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
from datetime import date

import pytest

from core import annotations, cleanup, clip_capture, clip_compose, clips, papers, sources
from core import remote_tasks as rt

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHARED = os.path.join(ROOT, "apps", "mobile-ios", "Shared")
P = clips.PAPERS_DIR


# ---------------------------------------------------------------- 样本

def make_pdf(pages, title=None):
    """最小的带文字层的 PDF：每页几行 Helvetica 字。pages 里一页是一串行（空的一页没有字，当扫描版用）。"""
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>",
            f"<< /Type /Pages /Kids [{' '.join(f'{4 + 2 * i} 0 R' for i in range(len(pages)))}] /Count {len(pages)} >>".encode(),
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    for i, lines in enumerate(pages):
        ops = ["BT /F1 12 Tf 14 TL 72 720 Td"]
        ops += ["(" + ln.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") + ") Tj T*" for ln in lines]
        stream = "\n".join(ops + ["ET"]).encode()
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> "
                    f"/Contents {5 + 2 * i} 0 R >>".encode())
        objs.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    info = None
    if title:
        objs.append(f"<< /Title ({title}) >>".encode())
        info = len(objs)
    out = bytearray(b"%PDF-1.4\n")
    offs = []
    for i, o in enumerate(objs):
        offs.append(len(out))
        out += f"{i + 1} 0 obj\n".encode() + o + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode() + b"".join(f"{o:010d} 00000 n \n".encode() for o in offs)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R".encode() + (f" /Info {info} 0 R".encode() if info else b"")
    out += b" >>\nstartxref\n" + str(xref).encode() + b"\n%%EOF\n"
    return bytes(out)


PDF_LINES = [["Attention is all you need. We propose a new simple network architecture,",
              "the Transformer, based solely on attention mecha-", "nisms, dispensing with recurrence.", "Short end.",
              "Experiments on two machine translation tasks show these models to be superior."],
             ["Second page starts a new paragraph with enough words to count as text."]]
PDF = make_pdf(PDF_LINES, title="Sample Paper Title")
PDF_NO_TITLE = make_pdf(PDF_LINES)
SCANNED = make_pdf([[], [], ["p. 3"]])

ATOM = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <title type="html">ArXiv Query: id_list=1706.03762</title>
  <id>http://arxiv.org/api/x</id>
  <entry>
    <id>http://arxiv.org/abs/1706.03762v7</id>
    <updated>2023-08-02T00:41:18Z</updated>
    <published>2017-06-12T17:57:34Z</published>
    <title>Attention Is All
      You Need</title>
    <summary>  The dominant sequence transduction models are based on complex recurrent
  networks &amp; attention.</summary>
    <author><name>Ashish Vaswani</name></author>
    <author><name>Noam Shazeer</name><arxiv:affiliation>Google</arxiv:affiliation></author>
    <author><name>Niki  Parmar</name></author>
    <arxiv:primary_category term="cs.CL"/>
  </entry>
</feed>"""
ATOM_ERROR = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><entry>
<id>http://arxiv.org/api/errors#incorrect_id_format_for_1706</id><title>Error</title>
<summary>incorrect id format for 1706</summary></entry></feed>"""
ATOM_EMPTY = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>q</title></feed>"""
ATOMS = [ATOM, ATOM_ERROR, ATOM_EMPTY, b"<feed><broken", b"", b"<feed><entry><title>No namespace</title></entry></feed>"]

ABS = """<html><head>
<meta name="citation_title" content="Attention Is All You Need" />
<meta name="citation_author" content="Vaswani, Ashish" />
<meta name="citation_author" content="Shazeer, Noam" />
<meta name="citation_author" content="Plato" />
<meta name="citation_date" content="2017/06/12" />
<meta content="We &amp; they &#39;propose&#39;" name="citation_abstract" />
</head><body></body></html>"""
ABSES = [ABS, "<html><head><title>x</title></head></html>", '<meta name="citation_title" content="  T  &lt;x&gt; ">']

MATHS = ['<p>Let <math alttext="x^{2}" display="inline"><mi>x</mi></math> be.</p>',
         '<td><math alttext="\\sum_i a_i &lt; b" display="block" class="ltx_Math"><mrow/></math></td>',
         '<math display="block"><mi>y</mi></math> and <MATH ALTTEXT="Q">q</MATH>',
         "<math alttext='b' display='block'></math>", 'no math here']

PARA_PAGES = [PDF_LINES[0] + ["", "After blank line"], ["中文第一行接着", "第二行。", "x-", "Y not joined", "end-", "less"], ["   ", ""], []]

def _book(n):
    """n 页的论文：每页页首一行页眉、页尾一行页脚（页码不同），中间两行不同的正文。"""
    words = ["alpha", "beta", "gamma", "delta", "epsilon", "zeta"]
    return [f"Journal of Tests 2026\nBody {words[i - 1]} line one.\nSecond {words[-i]} line here\nPage {i} of {n}" for i in range(1, n + 1)]


CLEAN_CASES = [
    _book(6),                                                                                   # 0 页眉、页脚（数字归一）都去掉
    _book(2),                                                                                   # 1 短论文：页眉留着，「Page 1 of 2」是页码，照样去掉
    [f"Journal of Tests\nBody {i} x\n{i}" for i in range(1, 3)],                                # 2 短论文：页码去掉，页眉留着
    ["Intro text.\n3 / 12", "- 4 -\nMore text.", "Page 5\nEven more.", "x\nthe 12 / 13 ratio\n42"],   # 3 各种页码写法，只在页首页尾
    [f"Shared Title\nBody {i} here.\nFoot {i}x" for i in (1, 2)] + ["Shared Title\nOnly page three.\nzz"] + ["Other\nText\nqq"] * 2,   # 4 5 页里 3 页相同：够 3 页、也过半
    ["Running Head\nBody a.\nline", "Running Head\nBody b.\nline2", "plain\nBody c.\nz", "plain2\nBody d.\nq", "p3\nBody e.\nr"],   # 5 只在 2 页：留着
    ["Header X\n" + "\n".join(f"{w} line" for w in "abcdef") + "\nHeader X"] * 4,               # 6 同一行在页首又在页尾：每页都去
    ["A start.\nB two.\nMiddle Header\nC.\nD end.\nlast line\nfoot"] * 4,                       # 7 重复行在页中间：不动（页首页尾那几行也各页相同，会去）
    ["TOP\n" + "very long line " * 12 + "\nfoot"] * 4,                                         # 8 超长行不当页眉；TOP、foot 去
    ["Some intro-\nduction text continues here.\nThe x-\nY stays.\nEnd-\nlast"] + ["Body\nmore text\nthe end"] * 2,   # 9 断词：小写接，大写不接
    ["Text before.\n7. References\n[1] A. B. 2020."],                                           # 10
    ["Text.\nBIBLIOGRAPHY\n[1] x"], ["Text.\n参考文献\n[1] x"],                                 # 11 12
    ["Text one.\nReferences and notes\n[1] x", "Text two.\nReferences.\ny", "Text three.\nSee References\nz", "Text four.\nVIII) references\nw"],   # 13 只有最后一个认
    ["", "   ", "\n\n"], [],                                                                    # 14 15 空的
    ["中文页眉\n正文第一行\n第 1 页", "中文页眉\n正文第二行\n第 2 页", "中文页眉\n正文第三行\n第 3 页"],     # 16 中文页眉和页码
    ["H\nbody a\n1", "H\nbody b\n2", "H\nbody c\n3", "H\nbody d\n2023"],                               # 17 4 位数字也算页码
    ["Body\n12345", "Body\n123"],                                                               # 18 5 位数字不算页码；123 是页码
]


ARXIV_TABLE = [
    "https://arxiv.org/abs/1706.03762", "https://arxiv.org/abs/1706.03762v7", "http://arxiv.org/pdf/1706.03762v2.pdf",
    "https://www.arxiv.org/pdf/1706.03762", "https://arxiv.org/pdf/1706.03762.pdf", "https://arxiv.org/html/2310.06825v1",
    "https://arxiv.org/html/2310.06825v1/", "https://arxiv.org/abs/hep-th/9901001", "https://arxiv.org/abs/hep-th/9901001v2",
    "https://arxiv.org/pdf/math.GT/0309136", "https://arxiv.org/abs/1706.03762?context=cs", "https://arxiv.org/abs/1706.03762#x",
    "HTTPS://ArXiv.org/abs/1706.03762", "https://export.arxiv.org/abs/1706.03762", "  https://arxiv.org/abs/0704.0001  ",
    "https://www.alphaxiv.org/abs/1706.03762", "https://alphaxiv.org/abs/1706.03762", "https://arxiv.org.evil.com/abs/1706.03762",
    "https://evilarxiv.org/abs/1706.03762", "https://arxiv.org/list/cs.AI/recent", "https://arxiv.org/abs/", "https://arxiv.org/abs/1706.037",
    "ftp://arxiv.org/abs/1706.03762", "https://arxiv.org/abs/1706.03762v", "arxiv.org/abs/1706.03762", "https://user@arxiv.org/abs/1706.03762",
    "https://arxiv.org/abs/1706.03762/extra", "https://arxiv.org/ABS/1706.03762", "https://example.com/paper.pdf", "",
]
WANT_IDS = ["1706.03762"] * 5 + ["2310.06825"] * 2 + ["hep-th/9901001"] * 2 + ["math.GT/0309136"] + ["1706.03762"] * 4 \
    + ["0704.0001"] + [""] * 15

TITLE = "Attention Is All You Need"
DROP_CASES = [
    ("License: CC BY 4.0\n\n## Attention   is all you NEED \n\nAbstract text.\n\n## Attention Is All You Need\n", TITLE),
    ("## Attention Is All You Need\n\nBody", TITLE),
    ("Intro\n\n## 1 Introduction\n\n## Attention Is All You Need\n", TITLE),   # 第一个标题行不是：都不动
    ("### Attention Is All You Need\nnext line", TITLE),
    ("#Attention Is All You Need\n\nBody", TITLE),                                  # 不是标题行（# 后没有空白）
    ("Body only\n", TITLE), ("## \n\nBody", ""), ("## Other\n", TITLE), ("", TITLE),
]

AUTHOR_CASES = [[], ["A"], ["张三", "李四"], [f"Author {i}" for i in range(5)], [f"Author {i}" for i in range(6)],
                [f'N"{i}\\/é' for i in range(25)]]
HEAD_CASES = [([], "", ""), (["A", 'B "Q"'], "1706.03762", "attachments/0123456789abcdef.pdf"),
              ([f"P{i}" for i in range(25)], "hep-th/9901001", ""), (["中文 名"], "", "attachments/ffffffffffffffff.pdf")]


STAMP = "arXiv:2506.09108v1  [cs.LG]  10 Jun 2025"
STAMP_CASES = [
    [STAMP + "\nTitle of paper"], ["Title\n" + "arXiv:2506.09108v12 [cs.LG] 1 Jan 2025"], ["arXiv : 2506.09108   [stat.ML]\n 10   Jun  2025"],
    ["arXiv:2506.091v1 [cs.LG] 10 Jun 2025"], ["arXiv:2506.09108 [cs.LG] 10 Jun 2025"],
    ["arXiv:hep-th/9901001v2  [hep-th]  3 Feb 1999"], ["arXiv:hep-th/9901001v2  25 Jan 1999"], ["arXiv:math.GT/0309136v1 [math.GT] 8 Sep 2003"],
    ["Intro page only", STAMP], [],
    ["See arXiv:1706.03762 for details. arXiv:1706.03762v5 [cs.CL] is cited (2017)."],
    ["Ref: arXiv:1706.03762v5, 12 Jun 2017"], ["arXiv:2506.09108v1 [cs.LG]"], ["arXiv:2506.09108v1 10 Jun 2025"],
    ["cites arXiv:1706.03762 [cs.CL] then " + STAMP],
]
WANT_STAMPS = ["2506.09108", "2506.09108", "2506.09108", "", "2506.09108", "hep-th/9901001", "hep-th/9901001", "math.GT/0309136",
               "", "", "", "", "", "", "2506.09108"]
PUB_CASES = ["2025-06-10", "2025-06", "2025", "无", "", "June 2025", "2025/06/10", "2025-13", "2025-02-30", "1899", "1900", "1900-01-01",
             str(date.today().year + 1), str(date.today().year + 2), f"{date.today().year + 1}-12", " **2024-03** ", "2024-6", "20250610", "2025-06-10T00:00", "２０２４-０５"]
WANT_PUBS = ["2025-06-10", "2025-06-01", "2025-01-01", "", "", "", "", "", "", "", "1900-01-01", "1900-01-01",
             f"{date.today().year + 1}-01-01", "", f"{date.today().year + 1}-12-01", "2024-03-01", "", "", "", ""]
AUTHORS_RAW = ["A. One; B. Two ;C Three", "无", "", " ; ;", "Solo", "张三；李四", "A;;B; ", ";".join(f"N{i}" for i in range(25)), "**A; B**",
               "N/A", "未知", "Unknown", "none", "张三；李四; Wang Wu", "正文开头没有列出作者姓名，" * 8, "A; " + "x" * 81]

# ---------------------------------------------------------------- 两端对表（swiftc）

MAIN = r"""
import Foundation
let input = try! JSONSerialization.jsonObject(with: FileHandle.standardInput.readDataToEndOfFile()) as! [String: Any]
func meta(_ m: Paper.Meta?) -> Any {
    guard let m else { return NSNull() }
    return ["title": m.title, "authors": m.authors, "published": m.published, "summary": m.summary]
}
var out: [String: Any] = [:]
out["ids"] = (input["urls"] as! [String]).map { Paper.arxivID($0) ?? "" }
out["paperLink"] = (input["urls"] as! [String]).map { Paper.isPaperLink($0) }
out["atom"] = (input["atoms"] as! [String]).map { meta(Paper.parseAtom(Data(base64Encoded: $0)!)) }
out["abs"] = (input["abses"] as! [String]).map { meta(Paper.parseAbs($0)) }
out["math"] = (input["maths"] as! [String]).map { Paper.texMath($0) }
out["para"] = Paper.paragraphs(input["pages"] as! [String])
out["paraEach"] = (input["pages"] as! [String]).map { Paper.paragraphs([$0]) }
out["clean"] = (input["cleans"] as! [[String]]).map { Paper.cleanPages($0) }
out["pdfMd"] = (input["cleans"] as! [[String]]).map { Paper.pdfMarkdown($0) }
out["cleanConsts"] = [Paper.headerMinPages, Paper.headerMinCount, Paper.headerZone, Paper.headerMaxChars]
out["authors"] = (input["authors"] as! [[String]]).map { Paper.authorsLine($0) }
out["drop"] = (input["drops"] as! [[String]]).map { Paper.dropTitleHeading($0[0], title: $0[1]) }
out["head"] = (input["heads"] as! [[Any]]).map { Paper.headLines(authors: $0[0] as! [String], arxiv: $0[1] as! String, pdf: $0[2] as! String) }
out["names"] = (input["names"] as! [String]).map { Paper.pdfName(url: $0) }
out["dataNames"] = (input["pdfs"] as! [String]).map { Paper.pdfName(data: Data(base64Encoded: $0)!) }
out["pdf"] = (input["pdfs"] as! [String]).map { b -> Any in
    do { let t = try Paper.pdfText(Data(base64Encoded: b)!); return ["pages": t.pages.count, "title": t.title, "text": t.pages.joined(separator: "\n")] }
    catch { return ["error": error.localizedDescription] }
}
out["scanned"] = (input["scans"] as! [[String]]).map { Paper.scanned($0) }
out["fileTitles"] = (input["files"] as! [String]).map { Paper.title(fileName: $0) }
out["stamp"] = (input["stamps"] as! [[String]]).map { Paper.stampID($0) ?? "" }
out["published"] = (input["pubs"] as! [String]).map { Paper.parsePublished($0) }
out["authorsParsed"] = (input["authorsRaw"] as! [String]).map { Paper.parseAuthors($0) }
out["consts"] = [Paper.maxPDFBytes, Paper.maxPDFPages, Paper.scannedCharsPerPage, Paper.htmlMinChars, Paper.maxAuthors, Paper.authorsShown]
out["msgs"] = [Paper.scannedMessage, Paper.tooBigMessage, Paper.tooLongMessage, Paper.notPDFMessage, Paper.arxivSource, Paper.fileSource]
print(String(data: try! JSONSerialization.data(withJSONObject: out), encoding: .utf8)!)
"""

BIG = b"%PDF-1.4\n" + b"0" * (sources.PDF_MAX_BYTES + 1 - 9)
LONG = make_pdf([["page text that is long enough to count"]] * (sources.PDF_MAX_PAGES + 1))
PDFS = [PDF, PDF_NO_TITLE, SCANNED, LONG, BIG, b"not a pdf at all"]
SCANS = [[], [""], ["a" * 19], ["a" * 20], ["a" * 39, ""], [" \n\t " * 50, "a" * 40]]
FILES = ["my_paper-v2.pdf", "Report.PDF", "dir/x.pdf", ".pdf", "中文_论文.pdf", "noext"]
NAMES = ["https://arxiv.org/pdf/1706.03762", "https://example.com/a.pdf", ""]


@pytest.fixture(scope="module")
def swift(tmp_path_factory):
    if not shutil.which("swiftc"):
        pytest.skip("没有 swiftc")
    d = tmp_path_factory.mktemp("paperswift")
    (d / "main.swift").write_text(MAIN, encoding="utf-8")
    exe = d / "probe"
    r = subprocess.run(["swiftc", "-o", str(exe), os.path.join(SHARED, "Paper.swift"), os.path.join(SHARED, "SparkError.swift"),
                        str(d / "main.swift")], capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, r.stderr
    import base64
    payload = {"urls": ARXIV_TABLE, "atoms": [base64.b64encode(a).decode() for a in ATOMS], "abses": ABSES, "maths": MATHS,
               "pages": ["\n".join(p) for p in PARA_PAGES], "authors": AUTHOR_CASES, "heads": [list(h) for h in HEAD_CASES],
               "cleans": CLEAN_CASES, "names": NAMES, "drops": [list(c) for c in DROP_CASES], "pdfs": [base64.b64encode(p).decode() for p in PDFS], "scans": SCANS, "files": FILES,
               "stamps": STAMP_CASES, "pubs": PUB_CASES, "authorsRaw": AUTHORS_RAW}
    run = subprocess.run([str(exe)], input=json.dumps(payload), capture_output=True, text=True, timeout=120, check=True)
    return json.loads(run.stdout)


def test_arxiv链接识别_两端一致(swift):
    assert [papers.arxiv_id(u) for u in ARXIV_TABLE] == WANT_IDS
    assert swift["ids"] == WANT_IDS
    assert swift["paperLink"] == [papers.is_paper_link(u) for u in ARXIV_TABLE]
    assert papers.is_paper_link("https://example.com/paper.pdf") and not papers.is_paper_link("https://example.com/a")


def test_Atom和摘要页解析_两端一致(swift):
    m = papers.parse_atom(ATOM)
    assert m == {"title": "Attention Is All You Need", "authors": ["Ashish Vaswani", "Noam Shazeer", "Niki Parmar"],
                 "published": "2017-06-12",
                 "summary": "The dominant sequence transduction models are based on complex recurrent networks & attention."}
    assert [papers.parse_atom(a) for a in ATOMS[1:]] == [None] * 5
    assert swift["atom"] == [papers.parse_atom(a) for a in ATOMS]
    a = papers.parse_abs(ABS)
    assert a["title"] == "Attention Is All You Need" and a["authors"] == ["Ashish Vaswani", "Noam Shazeer", "Plato"]
    assert a["published"] == "2017-06-12" and a["summary"] == "We & they 'propose'"
    assert swift["abs"] == [papers.parse_abs(h) for h in ABSES]


def test_公式和段落_两端一致(swift):
    assert papers.tex_math(MATHS[0]) == "<p>Let $x^{2}$ be.</p>"
    assert papers.tex_math(MATHS[1]) == "<td><p>$$\\sum_i a_i &lt; b$$</p></td>"
    assert swift["math"] == [papers.tex_math(m) for m in MATHS]
    pages = ["\n".join(p) for p in PARA_PAGES]
    text = papers.paragraphs(pages)
    assert "attention mechanisms, dispensing" in text and "Short end.\n\nExperiments" in text
    assert "中文第一行接着第二行。" in text and "x- Y not joined" in text and "joined endless" in text
    assert swift["para"] == text
    assert swift["paraEach"] == [papers.paragraphs([p]) for p in pages]


def test_网页版开头的重复标题_两端一致(swift):
    got = [papers.drop_title_heading(md, t) for md, t in DROP_CASES]
    assert got[0] == "License: CC BY 4.0\n\nAbstract text.\n\n## Attention Is All You Need\n"   # 只去第一个标题行
    assert got[1] == "Body" and got[3] == "next line"
    assert got[2:3] + got[4:] == [md for md, _ in DROP_CASES[2:3] + DROP_CASES[4:]]
    assert swift["drop"] == got


def test_文件头新字段_两端写法逐字一致(swift):
    assert swift["authors"] == [clip_compose.authors_line(a) for a in AUTHOR_CASES]
    assert clip_compose.authors_line(AUTHOR_CASES[4]) == "Author 0、Author 1、Author 2、Author 3、Author 4 等 6 人"
    assert swift["head"] == [clip_compose.paper_head_lines(*h) for h in HEAD_CASES]
    assert clip_compose.paper_head_lines(*HEAD_CASES[1]) == [
        'authors: ["A", "B \\"Q\\""]', 'arxiv: "1706.03762"', 'pdf: "attachments/0123456789abcdef.pdf"']
    assert len(json.loads(clip_compose.paper_head_lines(*HEAD_CASES[2])[0][len("authors: "):])) == 20
    assert swift["names"] == [papers.pdf_name(n) for n in NAMES]


def test_PDF上限和扫描版_两端规则一致(swift):
    assert swift["consts"] == [sources.PDF_MAX_BYTES, sources.PDF_MAX_PAGES, sources.PDF_SCANNED_CHARS_PER_PAGE,
                               papers.HTML_MIN_CHARS, clip_compose.MAX_AUTHORS, clip_compose.AUTHORS_SHOWN]
    assert swift["msgs"] == [sources.PDF_SCANNED_MESSAGE, sources.PDF_TOO_BIG_MESSAGE, sources.PDF_TOO_LONG_MESSAGE,
                             papers.NOT_PDF_MESSAGE, papers.ARXIV_SOURCE, papers.FILE_SOURCE]
    assert sources.PDF_SCANNED_MESSAGE == "这份 PDF 是扫描版，取不出文字"
    assert swift["scanned"] == [sources.pdf_scanned(s) for s in SCANS] == [True, True, True, False, True, False]
    py = []
    for data in PDFS:
        try:
            pages = sources.pdf_pages(data)
            py.append({"pages": len(pages), "title": papers.pdf_title(data, "")})
        except RuntimeError as e:
            py.append({"error": str(e)})
    assert [x.get("error") for x in py[2:5]] == [sources.PDF_SCANNED_MESSAGE, sources.PDF_TOO_LONG_MESSAGE, sources.PDF_TOO_BIG_MESSAGE]
    for p, s in zip(py[:5], swift["pdf"][:5]):   # 文字不要求逐字一样，页数、标题、报错要一样
        assert {k: s[k] for k in p} == p
    assert "Transformer" in swift["pdf"][0]["text"] and "error" in py[5] and "error" in swift["pdf"][5]
    assert swift["fileTitles"] == [papers.pdf_title(b"", f) for f in FILES]
    assert swift["dataNames"] == [papers.pdf_name(d) for d in PDFS]


def test_扫描版_原有调用方照样报错():
    with pytest.raises(RuntimeError, match="扫描版"):
        sources._extract_pdf_text(SCANNED)
    assert "Transformer" in sources._extract_pdf_text(PDF)


# ---------------------------------------------------------------- 取内容（网络换成样本）

ARTICLE_HTML = ("<html><head><title>Attention</title></head><body><article class='ltx_document'><h1>Attention Is All You Need</h1>"
                + "".join(f"<section><h2>{i} Section</h2><p>Paragraph {i} with inline <math alttext='a_{i}' display='inline'>"
                          f"<mi>a</mi></math> math and enough words to make the body long. " * 6 + "</p>"
                          f"<table><tr><td><math alttext='E=mc^{i}' display='block'><mi>E</mi></math></td></tr></table></section>"
                          for i in range(8))
                + "<ul class='ltx_biblist'><li>[1] A. Reference, 2017.</li></ul></article></body></html>").encode()
SHORT_HTML = b"<html><body><article><p>" + b"Too short. " * 30 + b"</p></article></body></html>"


def http404(url):
    return urllib.error.HTTPError(url, 404, "Not Found", None, None)


class Net:
    """录好的样本：{网址: 字节或异常}；记下请求过哪些网址（不并发，一个一个）。"""

    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def __call__(self, url, timeout=papers.TIMEOUT, max_bytes=papers.MAX_PAGE_BYTES):
        self.calls.append(url)
        got = self.pages.get(url, urllib.error.URLError("没录这个"))
        if isinstance(got, Exception):
            raise got
        if len(got) > max_bytes:
            raise urllib.error.URLError("太大")
        return got


API = "https://export.arxiv.org/api/query?id_list=1706.03762"
HTML = "https://arxiv.org/html/1706.03762"
PDF_URL = "https://arxiv.org/pdf/1706.03762"
ABS_URL = "https://arxiv.org/abs/1706.03762"


def test_arxiv_网页版全文(monkeypatch):
    net = Net({API: ATOM, HTML: ARTICLE_HTML, PDF_URL: PDF})
    monkeypatch.setattr(papers, "GET", net)
    a, data, notes = papers.fetch_arxiv("1706.03762")
    assert net.calls == [API, HTML, PDF_URL] and notes == []
    assert a["title"] == "Attention Is All You Need" and a["source"] == "arXiv" and a["published"] == "2017-06-12"
    assert a["authors"] == ["Ashish Vaswani", "Noam Shazeer", "Niki Parmar"] and a["arxiv"] == "1706.03762"
    assert a["url"] == ABS_URL and a["pdf"] == papers.pdf_name(PDF_URL) and data == PDF
    assert "$a_3$" in a["markdown"] and "$$E=mc^2$$" in a["markdown"] and "[1] A. Reference" in a["markdown"]
    assert "Transformer" not in a["markdown"]   # 用的是网页版，不是 PDF 的字


def test_PDF清理_每条规则():
    c = papers.clean_pages
    # 1 页眉页脚：数字归一后相同的行去掉
    got = c(CLEAN_CASES[0])
    assert got[0] == "Body alpha line one.\nSecond zeta line here" and len(got) == 6
    assert all("Journal" not in g and "Page" not in g for g in got)
    # 短论文（少于 3 页）不去重，页码照样去
    assert c(CLEAN_CASES[1])[0] == "Journal of Tests 2026\nBody alpha line one.\nSecond zeta line here"
    assert c(CLEAN_CASES[2]) == ["Journal of Tests\nBody 1 x", "Journal of Tests\nBody 2 x"]
    # 2 页码只在页首页尾；正文里的「12 / 13」不动
    assert c(CLEAN_CASES[3]) == ["Intro text.", "More text.", "Even more.", "x\nthe 12 / 13 ratio"]
    # 出现的页数：至少 3 页且过半才去
    assert [g.split("\n")[0] for g in c(CLEAN_CASES[4])] == ["Body 1 here.", "Body 2 here.", "Only page three.", "Other", "Other"]
    assert [g.split("\n")[0] for g in c(CLEAN_CASES[5])][:2] == ["Running Head"] * 2
    assert c(CLEAN_CASES[6]) == ["b line\nc line\nd line\ne line"] * 4   # 页首页尾各 2 行（页眉出现两次）都去，中间 4 行留着
    got = c(CLEAN_CASES[7])
    assert all("Middle Header" in g for g in got) and "A start." not in got[0]
    assert "TOP" not in c(CLEAN_CASES[8])[0] and "very long line" in c(CLEAN_CASES[8])[0]
    # 3 断词：小写接上、大写不接（paragraphs 做）
    md = papers.pdf_pages_markdown(CLEAN_CASES[9])
    assert md.startswith("Some introduction text continues here. The x- Y stays.\n\nEndlast")
    # 4 参考文献：变成「## 」标题行，后面的内容照留
    assert c(CLEAN_CASES[10]) == ["Text before.\n\n## 7. References\n\n[1] A. B. 2020."]
    assert papers.pdf_pages_markdown(CLEAN_CASES[10]) == "Text before.\n\n## 7. References\n\n[1] A. B. 2020."
    assert c(CLEAN_CASES[11]) == ["Text.\n\n## BIBLIOGRAPHY\n\n[1] x"] and c(CLEAN_CASES[12]) == ["Text.\n\n## 参考文献\n\n[1] x"]
    got = c(CLEAN_CASES[13])
    assert got[:3] == CLEAN_CASES[13][:3] and got[3] == "Text four.\n\n## VIII) references\n\nw"
    # 空的、中文、4 位数字页码、5 位不算
    assert c(CLEAN_CASES[14]) == ["", "", "\n\n"] and c(CLEAN_CASES[15]) == []
    assert c(CLEAN_CASES[16]) == ["正文第一行", "正文第二行", "正文第三行"]
    assert c(CLEAN_CASES[17]) == ["body a", "body b", "body c", "body d"]
    assert c(CLEAN_CASES[18]) == ["Body\n12345", "Body"]


def test_PDF清理_arxiv网页版不走():
    src = open(os.path.join(ROOT, "core", "papers.py"), encoding="utf-8").read()
    assert "clean_pages" not in src.split("def _html_markdown")[1].split("def _pdf_markdown")[0]


def test_PDF清理_两端一致(swift):
    assert swift["cleanConsts"] == [papers.HEADER_MIN_PAGES, papers.HEADER_MIN_COUNT, papers.HEADER_ZONE, papers.HEADER_MAX_CHARS]
    assert swift["clean"] == [papers.clean_pages(c) for c in CLEAN_CASES]
    assert swift["pdfMd"] == [papers.pdf_pages_markdown(c) for c in CLEAN_CASES]


def test_PDF清理_走进收录(monkeypatch):
    words = ["alpha", "beta", "gamma", "delta"]
    pages = [f"Journal of Tests\nFirst {w} line.\nSecond {w} line.\nIntro text with inter-\nnational words {w}.\nTail {w} line.\nEnd {w} line.\n{i}"
             for i, w in enumerate(words, 1)]
    pages.append("Journal of Tests\nReferences\n[1] A. Smith. 2020.\n5")
    monkeypatch.setattr(sources, "pdf_pages", lambda data: pages)
    md = papers._pdf_markdown(b"%PDF")
    assert "Journal of Tests" not in md and "international words alpha." in md and "\n\n## References\n\n[1] A. Smith. 2020." in md
    assert clip_compose.before_references(md).endswith("End delta line.")


def test_论文摘要走论文提示词和手机接线(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(clip_capture, "_ask", lambda: (lambda prompt, **kw: seen.append(prompt) or "TLDR: 一句\nTAGS: 甲\n导读一段。\n## 方法\n两句。"))
    monkeypatch.setattr(clip_capture, "_update", lambda *a, **kw: None)
    for folder in (P, clips.CLIPS_DIR):
        article = papers._article("https://e.com/a", "T", "s", "中文正文。" * 20 + "\n\n## References\n\n[1] ref")
        summary = clip_capture._process("job", article, str(tmp_path), [], folder)
        if folder == P:
            assert summary["body"] == "导读一段。\n### 方法\n两句。"
    # 这篇论文没有发表时间和作者：前面多了问这两项的一句，后面还是论文提示词
    assert seen[0].startswith(clip_compose.PAPER_META_ASK + "你是资讯编辑。下面是我收藏的一篇论文") and "[1] ref" not in seen[0]
    assert seen[1].startswith("你是资讯编辑。下面是我收藏的一篇文章") and "[1] ref" in seen[1]
    spark = os.path.join(ROOT, "apps", "mobile-ios", "Spark")
    for f, needle in (("Clips.swift", "paper: folder == PathRules.paperFolder"),
                      ("ReaderShell.swift", "paper: PathRules.clipFolderOf(path) == PathRules.paperFolder")):
        assert needle in open(os.path.join(spark, f), encoding="utf-8").read()
    assert open(os.path.join(spark, "Papers.swift"), encoding="utf-8").read().count("Paper.pdfMarkdown(t.pages)") == 3


def test_arxiv_网页版去掉开头重复的标题(monkeypatch):
    page = (b"<html><body><article><p>License: CC BY 4.0</p><h1>Attention  is all you need</h1>"
            + b"<h2>1 Introduction</h2><p>" + b"Enough words in the body to count. " * 80 + b"</p></article></body></html>")
    monkeypatch.setattr(papers, "GET", Net({API: ATOM, HTML: page, PDF_URL: PDF}))
    a, _, _ = papers.fetch_arxiv("1706.03762")
    assert a["markdown"].startswith("License: CC BY 4.0\n\n## 1 Introduction") and "is all you need" not in a["markdown"]


def test_论文不自动翻译_剪藏照旧(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(clip_capture, "_ask", lambda: (lambda prompt, **kw: "TLDR: 一句\nTAGS: 甲\n- 要点"))
    monkeypatch.setattr(clip_capture, "_update", lambda *a, **kw: None)
    monkeypatch.setattr(clip_compose, "translate", lambda md, ask, progress=None: calls.append(md) or (md, 0))
    english = "This is a long English paragraph about attention models and translation. " * 10
    for folder in (P, clips.CLIPS_DIR):
        article = papers._article("https://e.com/a", "T", "s", english)
        summary = clip_capture._process("job", article, str(tmp_path), [], folder)
        assert summary["tldr"] == "一句"   # 摘要照常写
    assert calls == [english]                # 只有剪藏那一次调了翻译
    swift_src = open(os.path.join(ROOT, "apps", "mobile-ios", "Spark", "Clips.swift"), encoding="utf-8").read()
    assert "if folder != PathRules.paperFolder, ClipTranslate.needed(a.markdown) {" in swift_src


def test_arxiv_网页版404退回PDF(monkeypatch):
    for html in (http404(HTML), SHORT_HTML):
        monkeypatch.setattr(papers, "GET", Net({API: ATOM, HTML: html, PDF_URL: PDF}))
        a, data, notes = papers.fetch_arxiv("1706.03762")
        assert "attention mechanisms, dispensing with recurrence." in a["markdown"] and data == PDF
        assert a["title"] == "Attention Is All You Need" and notes == []


def test_arxiv_信息接口失败的退路(monkeypatch):
    # 信息接口坏了：退回摘要页
    net = Net({API: urllib.error.URLError("超时"), ABS_URL: ABS.encode(), HTML: http404(HTML), PDF_URL: PDF})
    monkeypatch.setattr(papers, "GET", net)
    a, _, notes = papers.fetch_arxiv("1706.03762")
    assert net.calls == [API, ABS_URL, HTML, PDF_URL]
    assert a["title"] == "Attention Is All You Need" and a["authors"][0] == "Ashish Vaswani" and notes == []
    # 摘要页也不通：用 PDF 里的标题，整篇照存
    monkeypatch.setattr(papers, "GET", Net({API: ATOM_ERROR, HTML: http404(HTML), PDF_URL: PDF}))
    a, _, notes = papers.fetch_arxiv("1706.03762")
    assert a["title"] == "Sample Paper Title" and a["authors"] == [] and a["published"] == "" and len(notes) == 1
    # 什么标题都没有：用编号，让模型起标题
    monkeypatch.setattr(papers, "GET", Net({HTML: http404(HTML), PDF_URL: PDF_NO_TITLE}))
    a, _, _ = papers.fetch_arxiv("1706.03762")
    assert a["title"] == "arXiv 1706.03762" and a["titleGuessed"] is True
    # 正文都没取到：有摘要就先存摘要；连摘要也没有才失败
    monkeypatch.setattr(papers, "GET", Net({API: ATOM, HTML: http404(HTML), PDF_URL: SCANNED}))
    a, data, notes = papers.fetch_arxiv("1706.03762")
    assert a["markdown"].startswith("The dominant") and "扫描版" in notes[0]
    monkeypatch.setattr(papers, "GET", Net({}))
    with pytest.raises(RuntimeError, match="没取到"):
        papers.fetch_arxiv("1706.03762")


def test_别的链接和本地文件(monkeypatch):
    url = "https://example.com/files/My_Great-Paper.pdf?x=1"
    monkeypatch.setattr(papers, "GET", Net({url: PDF_NO_TITLE}))
    a, data, _ = papers.fetch_link(url)
    assert a["title"] == "My Great Paper" and a["source"] == "example.com" and a["pdf"] == papers.pdf_name(url) and data
    assert a["titleGuessed"] is True   # 只能用文件名当标题：写摘要时让模型起一个
    page = "https://blog.example.com/post"
    monkeypatch.setattr(papers, "GET", Net({page: ARTICLE_HTML}))
    a, data, _ = papers.fetch_link(page)
    assert data == b"" and not a["pdf"] and a["title"] == "Attention Is All You Need" and "Paragraph 1" in a["markdown"]
    a, data, _ = papers.fetch_file(PDF, "x.pdf")
    assert a["url"] == "" and a["source"] == "PDF" and a["pdf"] == papers.pdf_name(PDF) and a["title"] == "Sample Paper Title"
    assert a["titleGuessed"] is False
    a, _, _ = papers.fetch_file(PDF_NO_TITLE, "my_file-v2.pdf")
    assert a["title"] == "my file v2" and a["titleGuessed"] is True
    # 手机两条路（本地文件、链接 PDF）也按「元数据里有没有标题」定 titleGuessed
    swift_src = open(os.path.join(ROOT, "apps", "mobile-ios", "Spark", "Papers.swift"), encoding="utf-8").read()
    assert swift_src.count("titleGuessed: t.title.isEmpty ? true : nil") == 2
    with pytest.raises(RuntimeError, match="不是 PDF"):
        papers.fetch_file(b"<html>", "x.pdf")


# ---------------------------------------------------------------- 存、查重（任务）

@pytest.fixture
def vault_root(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    (root / "Spark").mkdir(parents=True)
    monkeypatch.setenv("SPARK_VAULT", str(root))
    monkeypatch.setattr(clip_capture, "_JOBS", {})
    monkeypatch.setattr(clip_capture.github_sync, "after_task", lambda: None)
    monkeypatch.setattr(clip_capture, "_ask", lambda: None)
    return root


def _wait(job):
    for _ in range(500):
        s = clip_capture.status(job["id"])
        if s["done"]:
            return s
        time.sleep(.01)
    pytest.fail("没做完")


def test_存论文和三种查重(vault_root, monkeypatch):
    net = Net({API: ATOM, HTML: ARTICLE_HTML, PDF_URL: PDF})
    monkeypatch.setattr(papers, "GET", net)
    done = _wait(papers.start({"url": "https://arxiv.org/abs/1706.03762v1"}))
    assert not done["error"] and done["path"].startswith(f"{P}/20170612_Attention")
    text = (vault_root / done["path"]).read_text(encoding="utf-8")
    fm = clips.frontmatter(text)
    assert fm["source"] == "arXiv" and fm["published"] == "2017-06-12" and fm["arxiv"] == "1706.03762"
    assert fm["pdf"] == papers.pdf_name(PDF_URL) and json.loads(fm["authors"]) == ["Ashish Vaswani", "Noam Shazeer", "Niki Parmar"]
    assert "- 来源：arXiv\n- 作者：Ashish Vaswani、Noam Shazeer、Niki Parmar\n" in text
    assert (vault_root / P / fm["pdf"]).read_bytes() == PDF
    assert "Attention Is All You Need" in (vault_root / P / "索引.md").read_text(encoding="utf-8")
    # 文件头行的先后：剪藏原有的几行不变，论文的几行在最后
    keys = [ln.split(":", 1)[0] for ln in text.split("\n---\n", 1)[0].split("\n")[1:]]
    assert keys == ["title", "source", "url", "published", "clipped", "clipped_at", "authors", "arxiv", "pdf"]
    # 1) 同一个 arXiv 编号，换个版本、换种链接：直接返回已有的，不再抓
    net.calls.clear()
    again = _wait(papers.start({"url": "http://arxiv.org/pdf/1706.03762v7.pdf"}))
    assert again["path"] == done["path"] and again["existed"] and again["warning"] == papers.EXISTS_MESSAGE and net.calls == []
    # 2) 同一个网址
    page = "https://example.com/p.pdf"
    monkeypatch.setattr(papers, "GET", Net({page: make_pdf(PDF_LINES, title="Other")}))
    first = _wait(papers.start({"url": page}))
    net2 = Net({})
    monkeypatch.setattr(papers, "GET", net2)
    second = _wait(papers.start({"url": "https://example.com/p.pdf?utm_source=x"}))
    assert second["path"] == first["path"] and second["existed"] and net2.calls == []
    # 3) 同一份 PDF 文件（按内容哈希）
    up = _wait(papers.start_upload(PDF_NO_TITLE, "my_file.pdf"))
    assert not up["error"] and (vault_root / P / papers.pdf_name(PDF_NO_TITLE)).read_bytes() == PDF_NO_TITLE
    assert clips.frontmatter((vault_root / up["path"]).read_text(encoding="utf-8"))["source"] == "PDF"
    again = _wait(papers.start_upload(PDF_NO_TITLE, "renamed.pdf"))
    assert again["path"] == up["path"] and again["existed"]
    assert len(clips.load_clips(str(vault_root), P)) == 3


def test_上传拒绝非PDF和超限(vault_root):
    with pytest.raises(rt.TaskError, match="不是 PDF"):
        papers.start_upload(b"%PDX-" + PDF, "a.pdf")
    with pytest.raises(rt.TaskError, match="50MB"):
        papers.start_upload(BIG, "a.pdf")
    done = _wait(papers.start_upload(SCANNED, "scan.pdf"))
    assert done["error"] == sources.PDF_SCANNED_MESSAGE and not os.path.exists(vault_root / P / papers.pdf_name(SCANNED))
    assert clips.load_clips(str(vault_root), P) == []
    with pytest.raises(rt.TaskError, match="链接不对"):
        papers.start({"url": "file:///etc/passwd"})


def test_上传接口_分块传输也卡上限(vault_root, monkeypatch):
    """不带 Content-Length（分块传输）的超限请求：读到上限就停，回 413，不整个读进来。"""
    from tests.test_reader import _client
    monkeypatch.setattr(sources, "PDF_MAX_BYTES", 4000)
    monkeypatch.setattr(papers, "UPLOAD_SLACK", 100)
    body = (b"--b\r\nContent-Disposition: form-data; name=\"file\"; filename=\"big.pdf\"\r\n"
            b"Content-Type: application/pdf\r\n\r\n%PDF-" + b"0" * 200000 + b"\r\n--b--\r\n")

    class Counted(io.BytesIO):
        taken = 0

        def read(self, n=-1):
            out = super().read(n)
            Counted.taken += len(out)
            return out

        def readline(self, n=-1):
            out = super().readline(n)
            Counted.taken += len(out)
            return out
    r = _client().post("/read/api/papers/upload", input_stream=Counted(body),
                       content_type="multipart/form-data; boundary=b", headers={"Transfer-Encoding": "chunked"},
                       environ_overrides={"wsgi.input_terminated": True})   # 服务器收分块请求时这样标
    assert r.status_code == 413 and "50MB" in r.json["error"]
    assert Counted.taken < 100000   # 读到上限附近就停了，没把 20 万字节都读进来
    assert clips.load_clips(str(vault_root), P) == []


def test_任务位的忙碌提示分剪藏和论文(vault_root, monkeypatch):
    import threading
    gate = threading.Event()
    monkeypatch.setattr(papers, "fetch_link", lambda url: gate.wait(5) and None)
    job = papers.start({"url": "https://example.com/a.pdf"})
    assert clip_capture.status(job["id"])["kind"] == "paper"
    with pytest.raises(rt.TaskError, match="已有一篇论文正在处理，请等它做完"):
        clip_capture._new_job("x")
    gate.set()
    _wait(job)
    clip_job = clip_capture._new_job("剪藏")
    with pytest.raises(rt.TaskError, match="已有一篇剪藏正在处理，请等它做完"):
        papers.start({"url": "https://example.com/b.pdf"})
    clip_capture._update(clip_job, done=True)


def test_上传接口(vault_root, monkeypatch):
    from tests.test_reader import _client
    monkeypatch.setattr(sources, "PDF_MAX_BYTES", 4000)
    c = _client()
    r = c.post("/read/api/papers/upload", data={"file": (io.BytesIO(b"<html>not pdf</html>"), "fake.pdf")})
    assert r.status_code == 400 and "不是 PDF" in r.json["error"]
    r = c.post("/read/api/papers/upload", data={"file": (io.BytesIO(b"%PDF-" + b"0" * 4000), "big.pdf")})
    assert r.status_code == 413 and "50MB" in r.json["error"]
    r = c.post("/read/api/papers/upload", data={"file": (io.BytesIO(b"%PDF-" + b"0" * 9000), "big.pdf")})
    assert r.status_code == 413
    assert c.post("/read/api/papers/upload", data={}).status_code == 400
    r = c.post("/read/api/papers", json={"url": "not a url"})
    assert r.status_code == 400
    monkeypatch.setattr(sources, "PDF_MAX_BYTES", 50 * 1024 * 1024)
    r = c.post("/read/api/papers/upload", data={"file": (io.BytesIO(PDF), "ok.pdf")})
    assert r.status_code == 202
    assert _wait(r.json)["path"].startswith(f"{P}/")
    page = c.get("/read/f/%E8%AE%BA%E6%96%87/").get_data(as_text=True)
    assert 'class="add-paper"' in page and "papers.js" in page and "选 PDF 文件" in page
    assert 'class="add-paper"' not in c.get("/read/f/%E5%89%AA%E8%97%8F/").get_data(as_text=True)


# ---------------------------------------------------------------- 新字段经得住各种改文件头

def _paper_text():
    a = papers._article(ABS_URL, "Attention", "arXiv", "正文。", published="2017-06-12", authors=["A", 'B "b"'],
                        aid="1706.03762", pdf="attachments/0123456789abcdef.pdf")
    from datetime import datetime, timezone
    return clip_compose.render(a, "", {"tldr": "一句话", "tags": ["注意力"], "body": "- 要点"},
                               datetime(2026, 11, 24, 10, 0, tzinfo=timezone.utc))


PAPER_LINES = ['authors: ["A", "B \\"b\\""]', 'arxiv: "1706.03762"', 'pdf: "attachments/0123456789abcdef.pdf"']


def test_新字段经各种改文件头操作后保留():
    from apps.reader import server
    text = _paper_text()
    assert all(ln in text.split("\n") for ln in PAPER_LINES)
    ops = {
        "改名": lambda t: server._retitled(t, "新的标题"),
        "星标": lambda t: annotations.with_star(t, True),
        "取消星标": lambda t: annotations.with_star(annotations.with_star(t, True), False),
        "稍后再读": lambda t: annotations.with_later(t, True, "2026-11-24T10:00:00+00:00"),
        "移出稍后再读": lambda t: annotations.with_later(annotations.with_later(t, True, "2026-11-24T10:00:00+00:00"), False),
        "重写摘要": lambda t: clip_compose.with_summary(t, {"tldr": "新", "tags": ["新标签", "x"], "body": ""}),
        "补抓": lambda t: clip_capture._keep_flags(t, clip_compose.render(
            {"url": ABS_URL, "title": "x", "source": "s", "published": "", "markdown": "新正文"}, "", None, date(2026, 11, 24))),
    }
    for name, op in ops.items():
        out = op(text)
        head = out.split("\n---\n", 1)[0].split("\n")
        assert [ln for ln in head if ln.startswith(("authors:", "arxiv:", "pdf:"))] == PAPER_LINES, name


def test_ClipInbox旧条目能解码(tmp_path):
    """收件箱条目加了 paper、file，文章加了 authors、arxiv、pdf：旧版写的 meta.json 照样能读（字段都是可选的）。
    把 Clipper.swift 里这两个结构体原样摘出来单独编（ClipInbox 只在 iOS 上编，不能整份拿来）。"""
    if not shutil.which("swiftc"):
        pytest.skip("没有 swiftc")
    src = open(os.path.join(SHARED, "Clipper.swift"), encoding="utf-8").read()

    def block(start):
        i = src.index(start)
        depth, j = 0, src.index("{", i)
        while True:
            depth += {"{": 1, "}": -1}.get(src[j], 0)
            if depth == 0:
                return src[i:j + 1]
            j += 1
    article, entry = block("struct Article: Codable"), block("struct Entry: Codable, Identifiable")
    assert "var paper: Bool? = nil" in entry and "var file: String? = nil" in entry and "var authors: [String]? = nil" in article
    main = ("import Foundation\nenum Clipper {\n" + article + "\n}\nenum ClipInbox {\n" + entry + "\n}\n" + r"""
let dec = JSONDecoder(); dec.dateDecodingStrategy = .iso8601
let old = #"{"id":"a","url":"https://x.com/a","note":"","created":"2026-11-01T10:00:00Z","later":true,"article":{"url":"https://x.com/a","title":"T","source":"S","published":"","markdown":"m","images":{}}}"#
let e = try! dec.decode(ClipInbox.Entry.self, from: Data(old.utf8))
let enc = JSONEncoder(); enc.dateEncodingStrategy = .iso8601
var f = ClipInbox.Entry(id: "b", url: "", note: "", created: Date(timeIntervalSince1970: 0), paper: true, file: "x.pdf")
f.article = Clipper.Article(url: "", title: "t", source: "PDF", published: "", markdown: "m", images: [:], authors: ["A"], arxiv: "1", pdf: "attachments/x.pdf")
let back = try! dec.decode(ClipInbox.Entry.self, from: try! enc.encode(f))
print(e.later == true, e.paper == nil, e.file == nil, e.article?.authors == nil, e.article?.title == "T",
      back.paper == true, back.file == "x.pdf", back.article?.authors == ["A"], back.article?.pdf == "attachments/x.pdf")
""")
    (tmp_path / "main.swift").write_text(main, encoding="utf-8")
    exe = tmp_path / "probe"
    r = subprocess.run(["swiftc", "-o", str(exe), str(tmp_path / "main.swift")], capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr
    out = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30, check=True).stdout.split()
    assert out == ["true"] * 9


# ---------------------------------------------------------------- 手机交来的 save 任务里的 PDF 键

KEY = f"{P}/attachments/0123456789abcdef.pdf"


def test_save任务的pdf键():
    t = rt.parse_task({"type": "save", "path": f"{P}/20261101_a.md", "content": "x", "replace": True,
                       "images": {KEY: "https://arxiv.org/pdf/1706.03762", f"{P}/attachments/0123456789abcdef.png": "https://e.com/a.png"}})
    assert KEY in t["images"]
    bad_cases = [
        (f"{clips.CLIPS_DIR}/20261101_a.md", {KEY: "https://e.com/a.pdf"}),                                   # 剪藏不收论文的 PDF
        (f"{clips.CLIPS_DIR}/20261101_a.md", {f"{clips.CLIPS_DIR}/attachments/0123456789abcdef.pdf": "https://e.com/a.pdf"}),
        (f"{P}/20261101_a.md", {KEY: "file:///etc/passwd"}),                                                  # 不是 http(s)
        (f"{P}/20261101_a.md", {KEY: "ftp://e.com/a.pdf"}),
        (f"{P}/20261101_a.md", {f"{P}/attachments/0123456789ABCDEF.pdf": "https://e.com/a.pdf"}),
        (f"{P}/20261101_a.md", {f"{P}/attachments/x/0123456789abcdef.pdf": "https://e.com/a.pdf"}),
        (f"{P}/20261101_a.md", {KEY + "\n": "https://e.com/a.pdf"}),
    ]
    for path, images in bad_cases:
        with pytest.raises(rt.TaskError, match="images"):
            rt.parse_task({"type": "save", "path": path, "content": "x", "images": images})
    assert not clips.image_key_ok(KEY, "https://e.com/a.pdf")   # 图片的规则不变


def test_下载PDF原件_上限和内容检查(tmp_path, monkeypatch):
    got = []

    def fake(url, timeout=20, max_bytes=0, user_agent=""):
        got.append((url, max_bytes))
        return {"https://e.com/good.pdf": PDF, "https://e.com/html.pdf": b"<html>login</html>"}[url]
    monkeypatch.setattr(sources, "http_get", fake)
    good, bad = f"{P}/attachments/{'a' * 16}.pdf", f"{P}/attachments/{'b' * 16}.pdf"
    ok, failed = clips.download_images({good: "https://e.com/good.pdf", bad: "https://e.com/html.pdf"}, str(tmp_path))
    assert (ok, failed) == (1, 1)
    assert (tmp_path / good).read_bytes() == PDF and not (tmp_path / bad).exists()
    assert all(m == sources.PDF_MAX_BYTES for _, m in got)


def test_手机交来的论文带PDF(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    (root / "Spark").mkdir(parents=True)
    monkeypatch.setenv("SPARK_VAULT", str(root))
    monkeypatch.setattr(sources, "http_get", lambda url, timeout=20, max_bytes=0, user_agent="": PDF)
    rel = f"{P}/20170612_Attention.md"
    out = rt.save_file(rt.parse_task({"type": "save", "path": rel, "content": _paper_text(), "replace": True, "new": True,
                                      "key": "paperpdf001", "images": {KEY: PDF_URL}}))
    assert "论文存进了笔记库" in out and (root / KEY).read_bytes() == PDF
    # 本地 PDF 的论文：任务里没有 pdf 项，文件头的 pdf: 行原样留着（电脑上没有这个文件）
    rel2 = f"{P}/20261101_本地.md"
    text = _paper_text().replace("attachments/0123456789abcdef.pdf", "attachments/fedcba9876543210.pdf")
    rt.save_file(rt.parse_task({"type": "save", "path": rel2, "content": text, "replace": True, "new": True, "key": "paperpdf002"}))
    assert (root / rel2).read_text(encoding="utf-8") == text and not (root / P / "attachments" / "fedcba9876543210.pdf").exists()


# ---------------------------------------------------------------- 清理工具不动 PDF

@pytest.fixture
def clean_env(tmp_path, monkeypatch):
    v = tmp_path / "vault"
    (v / "Spark").mkdir(parents=True)
    monkeypatch.setenv("SPARK_VAULT", str(v))
    monkeypatch.setenv("SPARK_SETTINGS_FILE", str(tmp_path / "home" / "settings.json"))
    monkeypatch.delenv(cleanup.HOME_ENV, raising=False)
    monkeypatch.setattr(rt, "trash_dir", lambda: str(tmp_path / "trash"))
    monkeypatch.setattr(cleanup, "_HOOKS", {"busy_jobs": lambda: 0, "syncing": lambda: False})
    monkeypatch.setattr(cleanup, "_model", lambda: (None, None))
    monkeypatch.setattr(cleanup, "_after", lambda: None)
    return v


def test_清理工具不移走被引用的PDF(clean_env, tmp_path):
    att = clean_env / P / "attachments"
    att.mkdir(parents=True)
    (att / "0123456789abcdef.pdf").write_bytes(PDF)        # 文件头 pdf: 行引用的
    (att / "fedcba9876543210.pdf").write_bytes(PDF)        # 谁都没引用的 PDF：也不动（清理工具只管图片）
    (att / "aaaaaaaaaaaaaaaa.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 100)   # 没人用的图：照常清
    (clean_env / P / "20170612_Attention.md").write_text(_paper_text(), encoding="utf-8")
    found = cleanup._classify(str(clean_env))["images"]
    assert not any(r.endswith(".pdf") for r in found)
    assert cleanup._safe_target(str(clean_env), f"{P}/attachments/0123456789abcdef.pdf") is None
    cleanup.run_regular(force=True)
    trashed = sorted(os.listdir(tmp_path / "trash")) if (tmp_path / "trash").exists() else []
    assert trashed == ["aaaaaaaaaaaaaaaa.png"]
    assert (att / "0123456789abcdef.pdf").exists() and (att / "fedcba9876543210.pdf").exists()


# ---------------------------------------------------------------- 真实试跑入口（不联网，网络换成样本）

def test_试跑入口写到输出目录(tmp_path, monkeypatch):
    monkeypatch.setattr(papers, "GET", Net({API: ATOM, HTML: http404(HTML), PDF_URL: PDF}))
    out = papers.capture_to_dir("https://arxiv.org/abs/1706.03762", str(tmp_path / "out"))
    text = open(out, encoding="utf-8").read()
    assert os.path.dirname(out) == str(tmp_path / "out") and 'arxiv: "1706.03762"' in text
    assert (tmp_path / "out" / papers.pdf_name(PDF_URL)).read_bytes() == PDF
    f = tmp_path / "local.pdf"
    f.write_bytes(PDF)
    out = papers.capture_to_dir(str(f), str(tmp_path / "out"))
    assert "source: \"PDF\"" in open(out, encoding="utf-8").read()
    assert re.search(r"tools/paper_trial\.py", open(os.path.join(ROOT, "tools", "paper_trial.py"), encoding="utf-8").read())


def test_PDF清理_只在少数页页首出现的短行要留着():
    """「一半以上的页」这条：20 页里某个短行只在 3 页的页首出现，不是页眉，不能删。"""
    pages = [("Table 1\n" if i in (2, 9, 15) else "") + f"Body text of page number {chr(97 + i)} goes here.\nMore words follow." for i in range(20)]
    got = papers.clean_pages(pages)
    assert [g.startswith("Table 1\n") for g in got] == [i in (2, 9, 15) for i in range(20)]


# ---------------------------------------------------------------- 本地 PDF 的发表时间和作者

def test_戳记识别_两端一致(swift):
    assert [papers.stamp_id(c) for c in STAMP_CASES] == WANT_STAMPS
    assert swift["stamp"] == WANT_STAMPS


def test_发表日期和作者解析_两端一致(swift):
    assert [clip_compose.parse_published(v) for v in PUB_CASES] == WANT_PUBS
    assert swift["published"] == WANT_PUBS
    want = [["A. One", "B. Two", "C Three"], [], [], [], ["Solo"], ["张三", "李四"], ["A", "B"], [f"N{i}" for i in range(20)], ["A", "B"],
            [], [], [], [], ["张三", "李四", "Wang Wu"], [], []]
    assert [clip_compose.parse_authors(v) for v in AUTHORS_RAW] == want
    assert swift["authorsParsed"] == want


def _stamp_pdf(title=None):
    return make_pdf([[STAMP, "A Great Paper", "Body text of the paper goes here."], ["Page two."]], title=title)


def _stamp_atom(aid="2506.09108"):
    return ATOM.replace(b"1706.03762", aid.encode()).replace(b"2017-06-12", b"2025-06-10")


def test_带戳记的PDF_取信息写进文章(monkeypatch):
    pdf = _stamp_pdf()
    api = "https://export.arxiv.org/api/query?id_list=2506.09108"
    net = Net({api: _stamp_atom()})
    monkeypatch.setattr(papers, "GET", net)
    a, data, _ = papers.fetch_file(pdf, "x.pdf")
    assert a["arxiv"] == "2506.09108" and a["published"] == "2025-06-10" and a["authors"][0] == "Ashish Vaswani"
    assert a["url"] == "" and a["source"] == "PDF" and a["pdf"] == papers.pdf_name(pdf) and a["title"] == "x" and data == pdf
    # 链接 PDF：来源、网址不变
    url = "https://example.com/files/p.pdf"
    monkeypatch.setattr(papers, "GET", Net({url: pdf, api: _stamp_atom()}))
    a, _, _ = papers.fetch_link(url)
    assert a["arxiv"] == "2506.09108" and a["published"] == "2025-06-10" and a["url"] == url and a["source"] == "example.com"
    assert a["pdf"] == papers.pdf_name(url)
    # 没有戳记：不问接口
    net = Net({})
    monkeypatch.setattr(papers, "GET", net)
    a, _, _ = papers.fetch_file(PDF, "x.pdf")
    assert net.calls == [] and a["arxiv"] == "" and a["published"] == "" and a["authors"] == []
    # 戳记不在第一页：不算
    late = make_pdf([["First page, no stamp here at all."], [STAMP]])
    a, _, _ = papers.fetch_file(late, "x.pdf")
    assert net.calls == [] and a["arxiv"] == ""


def test_带戳记的PDF_信息接口失败照原样存(monkeypatch):
    pdf = _stamp_pdf()
    monkeypatch.setattr(papers, "GET", Net({}))   # 接口和摘要页都不通
    a, data, notes = papers.fetch_file(pdf, "x.pdf")
    assert a["arxiv"] == "" and a["published"] == "" and a["authors"] == [] and data == pdf and notes == []
    api = "https://export.arxiv.org/api/query?id_list=2506.09108"
    monkeypatch.setattr(papers, "GET", Net({api: urllib.error.URLError("超时")}))
    a, _, _ = papers.fetch_file(pdf, "x.pdf")
    assert a["arxiv"] == "" and a["published"] == ""


def test_带戳记的PDF_按arxiv编号查重(vault_root, monkeypatch):
    api = "https://export.arxiv.org/api/query?id_list=2506.09108"
    monkeypatch.setattr(papers, "GET", Net({api: _stamp_atom(), "https://arxiv.org/html/2506.09108": http404("x"),
                                            "https://arxiv.org/pdf/2506.09108": _stamp_pdf()}))
    first = _wait(papers.start_upload(_stamp_pdf(), "a.pdf"))
    fm = clips.frontmatter((vault_root / first["path"]).read_text(encoding="utf-8"))
    assert fm["arxiv"] == "2506.09108" and fm["published"] == "2025-06-10" and fm["source"] == "PDF" and json.loads(fm["authors"])
    text = (vault_root / first["path"]).read_text(encoding="utf-8")
    assert "- 作者：" in text and "2025年06月10日" in text
    # 另一份内容不同、带同一个戳记的 PDF：按编号认出来
    other = make_pdf([[STAMP, "A Great Paper v2", "Different body bytes."]])
    again = _wait(papers.start_upload(other, "b.pdf"))
    assert again["existed"] and again["path"] == first["path"]
    # 用 arXiv 链接收同一篇：也认出来
    link = _wait(papers.start({"url": "https://arxiv.org/abs/2506.09108"}))
    assert link["existed"] and link["path"] == first["path"]
    assert len(clips.load_clips(str(vault_root), P)) == 1


def test_论文提示词_缺项才多一句_两端逐字一致():
    spark = open(os.path.join(ROOT, "apps", "mobile-ios", "Spark", "Clips.swift"), encoding="utf-8").read()
    ask = clip_compose.PAPER_META_ASK
    assert "static let paperMetaAsk = \"" + ask[:-2].replace("\n", "\\n") + "\\n\\n\"" in spark
    assert "if askedMeta { prompt = paperMetaAsk + prompt }" in spark
    base = papers._article("https://e.com/a", "T", "s", "正文。" * 20, guessed=False)
    plain = clip_compose.summary_prompt(base, [], paper=True)
    assert plain.startswith("你是资讯编辑。下面是我收藏的一篇论文")
    assert clip_compose.summary_prompt(base, [], paper=True, ask_meta=True) == ask + plain
    # summarize：缺项才问，已经有 published 和 authors 的不问；不传 ask_meta（重写摘要）不问
    seen = []
    raw = "PUBLISHED: 2024-05\nAUTHORS: A One; B Two\nTLDR: 一句\nTAGS: 甲\n导读。"
    ask_fn = lambda prompt, **kw: seen.append(prompt) or raw
    s = clip_compose.summarize(base, [], ask_fn, paper=True, ask_meta=True)
    assert seen[-1].startswith(ask) and s["published"] == "2024-05-01" and s["authors"] == ["A One", "B Two"] and s["tldr"] == "一句"
    full = dict(base, published="2017-06-12", authors=["X"])
    clip_compose.summarize(full, [], ask_fn, paper=True, ask_meta=True)
    assert seen[-1] == clip_compose.summary_prompt(full, [], paper=True)
    only_pub = dict(base, published="2017-06-12")
    clip_compose.summarize(only_pub, [], ask_fn, paper=True, ask_meta=True)
    assert seen[-1].startswith(ask)
    s = clip_compose.summarize(base, [], ask_fn, paper=True)
    assert seen[-1] == plain and "published" not in s
    # 剪藏：一个字不变，PUBLISHED 行不解析
    s = clip_compose.summarize(base, [], ask_fn, ask_meta=True)
    assert seen[-1] == clip_compose.summary_prompt(base, []) and "published" not in s
    # 解析：无、乱写、未来年份都当没有
    s = clip_compose.parse_summary("PUBLISHED: 无\nAUTHORS: 无\nTLDR: x\nTAGS: a\n- b", meta=True)
    assert s["published"] == "" and s["authors"] == []
    s = clip_compose.parse_summary("PUBLISHED: 去年\nAUTHORS: ;\nTLDR: x\nTAGS: a\n- b", meta=True)
    assert s["published"] == "" and s["authors"] == []


def test_arxiv链接收的论文_提示词不变(vault_root, monkeypatch):
    seen = []
    monkeypatch.setattr(clip_capture, "_ask", lambda: (lambda prompt, **kw: seen.append(prompt) or "TLDR: 一句\nTAGS: 甲\n导读。"))
    net = Net({API: ATOM, HTML: ARTICLE_HTML, PDF_URL: PDF})
    monkeypatch.setattr(papers, "GET", net)
    done = _wait(papers.start({"url": "https://arxiv.org/abs/1706.03762"}))
    assert not done["error"] and len(seen) == 1 and not seen[0].startswith(clip_compose.PAPER_META_ASK)
    assert "PUBLISHED:" not in seen[0]


def test_本地PDF_模型顺带取到的发表时间和作者写进文件头(vault_root, monkeypatch):
    seen = []
    raw = "PUBLISHED: 2024-05\nAUTHORS: Jane Roe; John Doe\nTLDR: 一句\nTAGS: 甲\n导读一段。"
    monkeypatch.setattr(clip_capture, "_ask", lambda: (lambda prompt, **kw: seen.append(prompt) or raw))
    monkeypatch.setattr(papers, "GET", Net({}))
    done = _wait(papers.start_upload(PDF_NO_TITLE, "my_file.pdf"))
    assert not done["error"] and seen[0].startswith(clip_compose.PAPER_META_ASK)
    text = (vault_root / done["path"]).read_text(encoding="utf-8")
    fm = clips.frontmatter(text)
    assert fm["published"] == "2024-05-01" and json.loads(fm["authors"]) == ["Jane Roe", "John Doe"]
    assert "- 作者：Jane Roe、John Doe\n" in text and "- 发布时间：2024年05月01日" in text
    # 写不成摘要：照原样存，没有这两项
    monkeypatch.setattr(clip_capture, "_ask", lambda: (lambda prompt, **kw: "乱写一气"))
    done = _wait(papers.start_upload(PDF, "other.pdf"))
    fm = clips.frontmatter((vault_root / done["path"]).read_text(encoding="utf-8"))
    assert "published" not in fm and "authors" not in fm


def test_模型顺带取的发表时间和作者_只补缺的那一项(vault_root, monkeypatch):
    """已有发表时间、缺作者（或反过来）：模型照样两行都回，已有的那一项不能被盖掉。"""
    raw = "PUBLISHED: 2024-05\nAUTHORS: Jane Roe; John Doe\nTLDR: 一句\nTAGS: 甲\n导读一段。"
    monkeypatch.setattr(clip_capture, "_ask", lambda: (lambda prompt, **kw: raw))
    for have, want_pub, want_authors in (({"published": "2020-01-02"}, "2020-01-02", ["Jane Roe", "John Doe"]),
                                         ({"authors": ["Old Author"]}, "2024-05-01", ["Old Author"])):
        monkeypatch.setattr(clip_capture, "_JOBS", {})   # 任务位一次一篇：每轮清空
        article = papers._article("", "T", papers.FILE_SOURCE, "正文。" * 50, **have)
        clip_capture._process(clip_capture._new_job("准备中"), article, str(vault_root), [], clips.PAPERS_DIR)
        assert article["published"] == want_pub and list(article["authors"]) == want_authors
