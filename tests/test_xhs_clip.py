"""小红书剪藏、分享文字里取链接、http 换 https、同一张图只存一份。

手机和 Mac 应用共用的 Swift（Shared/XHS.swift、ClipLink.swift、ClipImages.swift、Clipper.swift）没有 XCTest，
这里跟 test_doctime 一样用 swiftc 编一个小程序跑；电脑这边的 core/clips.py 直接测。都不连网，用 tests/fixtures 里的页面。
"""
import json
import os
import re
import shutil
import subprocess

import pytest

from core import clip_capture, clips, remote_tasks

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHARED = os.path.join(ROOT, "apps", "mobile-ios", "Shared")
SWIFT_FILES = [os.path.join(SHARED, f) for f in ("Clipper.swift", "XPost.swift", "DocTime.swift", "SparkError.swift",
                                                 "XHS.swift", "ClipLink.swift", "ClipImages.swift",
                                                 "Fixture.swift")]   # 联网前过的关口 NetGate（样例库模式）
FIXTURE = os.path.join(ROOT, "tests", "fixtures", "xhs_note.html")
NOTE_ID = "6b032d72000000001803883f"
FINAL = (f"https://www.rednote.com/discovery/item/{NOTE_ID}?app_platform=ios&app_version=9.55&xsec_source=app_share"
         "&type=normal&xsec_token=DUMMYTOKEN&author_share=1&xhsshare=CopyLink&shareRedId=DUMMY&share_id=DUMMY")

# 小红书首页（短链失效时跳到这里）：没有笔记数据，也没有 og 信息
HOME = """<!doctype html><html><head><meta charset="utf-8"><title>小红书 - 你的生活指南</title>
<meta name="description" content="在小红书，分享照片和视频，分享购物心得，记录生活故事，和最会生活的人做朋友。"></head>
<body><div>发现 直播 发布</div><script>window.__INITIAL_STATE__={"global":{"a":undefined},"noteData":{"routeQuery":{},"data":{}},"home":{}}</script></body></html>"""

# 公众号图片消息里同一张图的好几种写法（含脚本里的 \\x26amp;）
MMBIZ = [
    "https://mmbiz.qpic.cn/sz_mmbiz_jpg/AbC123xyz/640?wx_fmt=jpeg&from=appmsg",
    "https://mmbiz.qpic.cn/sz_mmbiz_jpg/AbC123xyz/0?wx_fmt=jpeg",
    "https://mmbiz.qpic.cn/sz_mmbiz_jpg/AbC123xyz/640?wx_fmt=jpeg\\x26amp;tp=webp\\x26amp;wxfrom=5\\x26amp;wx_lazy=1",
    "http://mmbiz.qpic.cn/sz_mmbiz_jpg/AbC123xyz/300?wx_fmt=jpeg&amp;tp=wxpic",
    "https://mmbiz.qpic.cn/sz_mmbiz_jpg/AbC123xyz?wx_fmt=jpeg",
    "https:\\/\\/mmbiz.qpic.cn\\/sz_mmbiz_jpg\\/AbC123xyz\\/1080?wx_fmt=jpeg\\x26from=appmsg",
    "https://mmbiz.qpic.cn/sz_mmbiz_jpg/AbC123xyz/640?wx_fmt=webp&tp=webp",
]
OTHER = "https://mmbiz.qpic.cn/sz_mmbiz_png/ZzZ999/640?wx_fmt=png&from=appmsg"

HARNESS = r'''
import Foundation
let input = try! JSONSerialization.jsonObject(with: FileManager.default.contents(atPath: CommandLine.arguments[1])!) as! [String: Any]
var out: [String: Any] = [:]
func dump(_ a: Clipper.Article) -> [String: Any] {
    ["url": a.url, "title": a.title, "source": a.source, "published": a.published, "markdown": a.markdown, "images": a.images,
     "titleGuessed": a.titleGuessed ?? false, "imagesOnly": a.imagesOnly ?? false, "publishedAt": a.publishedAt ?? ""]
}
var pages: [String: Any] = [:]
for (name, v) in input["pages"] as! [String: [String]] {
    do { pages[name] = dump(try XHS.article(v[0], final: URL(string: v[1])!)) } catch {
        pages[name] = ["error": error.localizedDescription, "gone": error is XHS.Gone]
    }
}
out["pages"] = pages
out["jsonSafe"] = (input["jsonSafe"] as! [String]).map(XHS.jsonSafe)
out["text"] = (input["text"] as! [String]).map(XHS.text)
out["extract"] = (input["extract"] as! [String]).map { ClipLink.firstURL(in: $0) ?? "" }
out["asciiExtract"] = (input["extract"] as! [String]).map { ClipLink.extract($0) ?? "" }
out["secure"] = (input["secure"] as! [String]).map { ClipLink.secure($0) }
out["identity"] = (input["identity"] as! [String]).map { ClipLink.identity($0) }
out["identity2"] = (input["identity2"] as! [String]).map { ClipLink.identity($0) }
out["insecure"] = (input["insecure"] as! [[Any]]).map { c in
    ClipLink.insecureOnly(URLError(URLError.Code(rawValue: c[0] as! Int)), original: URL(string: c[1] as! String)!, tried: URL(string: c[2] as! String)!)
}
out["keys"] = (input["keys"] as! [String]).map { ClipImages.key(URL(string: $0)!) }
out["stable"] = (input["keys"] as! [String]).map { ClipImages.stableName(URL(string: $0)!) }
out["fallback"] = (input["keys"] as! [String]).map { Clipper.fallbackURL(URL(string: $0)!)?.absoluteString ?? "" }
out["bodyNames"] = (input["bodyNames"] as! [[String]]).map { urls in ClipImages.canonical(urls.map { URL(string: $0)! }).map(Clipper.localName) }
out["noteID"] = (input["identity"] as! [String]).map { URL(string: $0).flatMap(XHS.noteID) ?? "" }
let mm = (input["mmbiz"] as! [String]).map { URL(string: ClipImages.clean($0))! }
out["mmbizClean"] = mm.map(\.absoluteString)
out["mmbizKeys"] = mm.map(ClipImages.key)
out["mmbizUnique"] = ClipImages.unique(mm).map(\.absoluteString)
out["pictures"] = Clipper.pictureURLs(input["wechat"] as! String, base: URL(string: "https://mp.weixin.qq.com/s/x")!).map(\.absoluteString)
let post = input["post"] as! [String: Any]
let r = Clipper.imagePost(post["parts"] as! [String], body: (post["body"] as! [String]).map { URL(string: $0)! },
                          pictures: (post["pictures"] as! [String]).map { URL(string: $0)! })
out["post"] = ["parts": r.0, "images": r.1]
var merged: [[String: Any]] = []
for m in input["merge"] as! [[String: Any]] {
    let x = ClipImages.mergeIdentical(markdown: m["markdown"] as! String, images: m["images"] as! [String: String],
                                      digests: m["digests"] as! [String: String], imagePost: m["imagePost"] as! Bool)
    merged.append(["markdown": x.markdown, "images": x.images])
}
out["merge"] = merged
print(String(data: try! JSONSerialization.data(withJSONObject: out, options: [.sortedKeys]), encoding: .utf8)!)
'''


def _fixture() -> str:
    with open(FIXTURE, encoding="utf-8") as f:
        return f.read()


def _state(html: str) -> dict:
    s = html[html.index("window.__INITIAL_STATE__=") + len("window.__INITIAL_STATE__="):]
    return json.loads(re.sub(r"\bundefined\b", "null", s[:s.index("</script>")]))


def _with_state(html: str, state: dict) -> str:
    start = html.index("window.__INITIAL_STATE__=") + len("window.__INITIAL_STATE__=")
    end = html.index("</script>", start)
    return html[:start] + json.dumps(state, ensure_ascii=False) + html[end:]


def _pages() -> dict:
    page = _fixture()
    state = _state(page)
    note = state["noteData"]["data"]["noteData"]
    # 视频笔记：只剩封面
    video = json.loads(json.dumps(state))
    vn = video["noteData"]["data"]["noteData"]
    vn["type"], vn["imageList"] = "video", vn["imageList"][:1]
    # 没标题、正文带话题和制表符、时间是数字毫秒、作者写成 nickname、图只有 urlDefault 或 infoList 里的默认尺寸
    tagged = json.loads(json.dumps(state))
    tn = tagged["noteData"]["data"]["noteData"]
    tn.update(title="", time=1795370354000,
              desc="周末去了一趟植物园，人不多，银杏全黄了\t\n\t\n门票 20 元[笑哭R]，地铁直达\n#植物园[话题]# #秋天[话题]#",
              user={"nickname": "某某"},
              imageList=[{"urlDefault": "http://sns-webpic-qc.xhscdn.com/202611230000/abc/1040g2sg31aaaaaaaaaaaaaaaa!nd_dft_wlteh_webp_3"},
                         {"url": "", "infoList": [{"imageScene": "WB_PRV", "url": "http://sns-webpic-qc.xhscdn.com/x/1040g2sg31bbbbbbbbbbbbbbbb!nd_prv_wlteh_webp_3"},
                                                  {"imageScene": "WB_DFT", "url": "http://sns-webpic-qc.xhscdn.com/x/1040g2sg31bbbbbbbbbbbbbbbb!nd_dft_wlteh_webp_3"}]},
                         {"urlDefault": "http://sns-webpic-qc.xhscdn.com/202611239999/def/1040g2sg31aaaaaaaaaaaaaaaa!nd_dft_wlteh_webp_3"}])
    # 没有 JSON：只有 og 信息
    og = re.sub(r"<script>window.__INITIAL_STATE__=.*?</script>", "", page, flags=re.S)
    # 电脑版页面：note.noteDetailMap.<编号>.note
    desktop = _with_state(page, {"note": {"noteDetailMap": {NOTE_ID: {"note": note}}}})
    return {
        "real": [page, FINAL],
        "video": [_with_state(page, video), FINAL],
        "tagged": [_with_state(page, tagged), FINAL],
        "og": [og, FINAL],
        "desktop": [desktop, f"https://www.xiaohongshu.com/explore/{NOTE_ID}?xsec_token=DUMMYTOKEN"],
        "home": [HOME, "https://www.xiaohongshu.com/"],
        "notfound": [HOME, "https://www.rednote.com/404/sec_abc?source=xhs_sec_server"],
        "blank": ["<html><head><title>x</title></head><body>hi</body></html>", "https://www.xiaohongshu.com/explore/" + NOTE_ID],
        # 不是笔记的页面：个人主页、搜索页（有 og 信息，认不出笔记编号）
        "profile": [PROFILE, "https://www.xiaohongshu.com/user/profile/5a0000000000000000000001?xsec_token=T"],
        "search": [SEARCH, "https://www.xiaohongshu.com/search_result?keyword=x"],
        # 同一条笔记再打开一次：图地址的签名变了，文件名还一样
        "resigned": [page.replace("/202611230251/", "/202611239999/").replace("95ffce7286bcbc269924de0883b859b0", "0" * 32), FINAL],
    }


# 判断同一篇用的标识：手机和电脑逐字一样（审查时找出的几处不一致：:443、没编码的中文、%20 和 +、?x 和 ?x=、#/ 路由）
IDENT = [
    "http://example.com/a", "https://example.com/a", "https://EXAMPLE.com/a", "https://example.com/a?utm_source=x",
    "https://example.com/a#x", "https://example.com/#/post/1", "https://example.com/#/post/2", "https://example.com/#!/p/3",
    "https://example.com/a?spm=1", "https://example.com/a?id=1", "https://example.com/a?id=2",
    "https://example.com:8443/a", "http://example.com:80/a", "https://example.com:443/a", "http://example.com:8080/a",
    "https://mp.weixin.qq.com/s?__biz=MzA&mid=1&idx=1&sn=abc", "https://mp.weixin.qq.com/s?__biz=MzA&mid=1&idx=2&sn=abc",
    "https://mp.weixin.qq.com/s/AbCdEf", "https://mp.weixin.qq.com/s/abcdef",
    f"https://www.xiaohongshu.com/explore/{NOTE_ID}?xsec_token=A", f"https://www.rednote.com/discovery/item/{NOTE_ID.upper()}?xsec_token=B",
    "http://xhslink.com/o/qr4LgnaedG", "https://xhslink.com/o/qr4LgnaedG",
    "https://example.com/%E4%B8%AD", "https://example.com/中", "https://example.com/a?q=a+b", "https://example.com/a?q=a%20b",
    "https://example.com/a?x", "https://example.com/a?x=", "https://example.com/a?UTM_SOURCE=x", "https://example.com/a?",
    "https://news.ycombinator.com/item?id=1",
]

# 图的 key、按 key 起的文件名、公众号原图的退路
KEYS = [
    "https://example.com/chart?from=2020", "https://example.com/chart?from=2021",          # 别的网站：参数不同就是不同的图
    "https://example.com/a.png?tp=webp", "http://example.com/a.png#x",
    "https://fe-static.xhscdn.com/a/x/1.png", "https://fe-static.xhscdn.com/b/x/1.png",    # 小红书静态图：编号太短，按路径
    "https://sns-web-i10.rednotecdn.com/202611230251/aaaa/notes_pre_post/1040g3k8327sl8fir70b048ripjauhgvk6kld92o!rnh5jpg",
    "http://sns-web-i10.rednotecdn.com/202611239999/bbbb/notes_pre_post/1040g3k8327sl8fir70b048ripjauhgvk6kld92o!rnh5jpg",
    "https://mmbiz.qpic.cn/sz_mmbiz_jpg/AbC123xyz/0?wx_fmt=jpeg", "https://mmbiz.qpic.cn/sz_mmbiz_jpg/AbC123xyz?wx_fmt=jpeg",
    "https://mmbiz.qpic.cn/sz_mmbiz_jpg/AbC123xyz/640?wx_fmt=jpeg", "https://example.com/0",
]

# 两篇普通公众号文章正文里的图（审查时从线上抓的，各 48、38 张）和这一批以前的文件名：正文的图名字一个都不能变
with open(os.path.join(ROOT, "tests", "fixtures", "wechat_body_image_names.json"), encoding="utf-8") as _f:
    BODY_NAMES = json.load(_f)

A1, A2, A3, A4 = (f"attachments/{c * 16}.jpg" for c in "1234")
W640, W0 = "https://mmbiz.qpic.cn/sz_mmbiz_jpg/AbC123xyz/640?wx_fmt=jpeg", "https://mmbiz.qpic.cn/sz_mmbiz_jpg/AbC123xyz/0?wx_fmt=jpeg"
U = {A1: "https://example.com/1.jpg", A2: "https://example.com/2.jpg", A3: "https://example.com/3.jpg", A4: "https://example.com/4.jpg"}
# 字节合并的几种情况（手机、电脑跑同一组）
MERGE_CASES = [
    # 0 纯图片的文章：两张不同地址的图紧挨着、字节一样（只有这两个）→ 合并，留先出现的 A2
    {"markdown": f"文字\n\n![]({A2})\n\n![]({A1})\n\n![]({A3})", "images": {A1: U[A1], A2: U[A2], A3: U[A3]},
     "digests": {A1: "same", A2: "same", A3: "diff"}, "imagePost": True,
     "want_md": f"文字\n\n![]({A2})\n\n![]({A3})", "want": [A2, A3]},
    # 1 普通文章：同一张图的两个版本（key 一样），作者放了两次 → 一个文件，两处引用都留着
    {"markdown": f"开头\n\n![]({A1})\n\n中间\n\n![]({A2})\n\n结尾", "images": {A1: W0, A2: W640},
     "digests": {A1: "same", A2: "same"}, "imagePost": False,
     "want_md": f"开头\n\n![]({A1})\n\n中间\n\n![]({A1})\n\n结尾", "want": [A1]},
    # 2 没下下来的图（没有 digest）不动
    {"markdown": f"![]({A1})\n\n![]({A2})", "images": {A1: U[A1], A2: U[A2]}, "digests": {A1: "same"}, "imagePost": True,
     "want_md": f"![]({A1})\n\n![]({A2})", "want": [A1, A2]},
    # 3 三张不同的图都下成了同一张占位图（紧挨着）→ 不合并
    {"markdown": f"![]({A1})\n\n![]({A2})\n\n![]({A3})", "images": {A1: U[A1], A2: U[A2], A3: U[A3]},
     "digests": {A1: "ph", A2: "ph", A3: "ph"}, "imagePost": True,
     "want_md": f"![]({A1})\n\n![]({A2})\n\n![]({A3})", "want": [A1, A2, A3]},
    # 4 纯图片的文章：字节一样但不挨着、key 不同 → 不合并
    {"markdown": f"![]({A1})\n\n![]({A3})\n\n![]({A2})", "images": {A1: U[A1], A2: U[A2], A3: U[A3]},
     "digests": {A1: "same", A2: "same", A3: "x"}, "imagePost": True,
     "want_md": f"![]({A1})\n\n![]({A3})\n\n![]({A2})", "want": [A1, A2, A3]},
    # 5 普通文章：key 不同、字节一样、紧挨着 → 不合并
    {"markdown": f"![]({A1})\n\n![]({A2})", "images": {A1: U[A1], A2: U[A2]}, "digests": {A1: "same", A2: "same"}, "imagePost": False,
     "want_md": f"![]({A1})\n\n![]({A2})", "want": [A1, A2]},
]

PROFILE = ('<html><head><meta property="og:title" content="某人的个人主页 - 小红书">'
           '<meta property="og:image" content="https://sns-avatar-qc.xhscdn.com/avatar/1040g2jo31sl5b9ce5m6g48ripjauhgvk22oq9m0"></head>'
           '<body><script>window.__INITIAL_STATE__={"user":{"userPageData":{"basicInfo":{"nickname":"某人"}}}}</script></body></html>')
SEARCH = ('<html><head><meta property="og:title" content="小红书搜索 - 小红书"><meta property="og:description" content="xx"></head>'
          '<body><script>window.__INITIAL_STATE__={"search":{}}</script></body></html>')

WECHAT = ("<html><head><meta property=\"og:image\" content=\"https://mmbiz.qpic.cn/og/should-not-win/0\"></head><body><script>"
          + "".join(f"var a{i} = {{cdn_url: '{u}'}};" for i, u in enumerate(MMBIZ + [OTHER, OTHER.replace('640', '0')]))
          + "</script></body></html>")


@pytest.fixture(scope="module")
def swift():
    if not shutil.which("swiftc"):
        pytest.skip("没有 swiftc")
    import tempfile
    tmp = tempfile.mkdtemp(prefix="xhs-swift-")
    try:
        main = os.path.join(tmp, "main.swift")
        with open(main, "w", encoding="utf-8") as f:
            f.write(HARNESS)
        exe = os.path.join(tmp, "harness")
        r = subprocess.run(["swiftc", "-O", "-o", exe, *SWIFT_FILES, main], capture_output=True, text=True, timeout=600)
        assert r.returncode == 0, r.stderr
        a1, a2 = "attachments/1111111111111111.jpg", "attachments/2222222222222222.jpg"
        a3 = "attachments/3333333333333333.jpg"
        data = {
            "pages": _pages(),
            "jsonSafe": ['{"a":undefined,"b":"undefined","c":[undefined, 1],"d":"x\\"undefined"}', '{"undefinedKey":1,"e":undefinedish}'],
            "text": ["第一段\t\n\t\n第二段 #话题甲[话题]##话题乙[话题]#\n[哭惹R] 第三行", "开头 #[话题]# 中间[话题]结尾 #A[话题]#"],
            "extract": [
                "两小时从 0 训出 64M 大模型，全开源 http://xhslink.com/o/qr4LgnaedG 复制后打开【小红书】查看笔记！",
                "看看这个https://example.com/a?b=1，挺好的",
                "链接：https://example.com/path)。",
                "(see https://en.wikipedia.org/wiki/Foo_(bar)) 和 https://second.example.com",
                "没有链接的文字 example.com",
                "末尾英文句号 https://example.com/x.",
                "前面 https://a.example.com/1 后面 https://b.example.com/2",
                "https://zh.wikipedia.org/wiki/中国 好",
                "https://例子.中国/a",
                "[文章](https://example.com/a_(b))",
                "**https://a.com/x**",
                "https://example.com/a）】》”",
                "“https://example.com/a”后面",
            ],
            "secure": ["http://xhslink.com/o/qr4LgnaedG", "https://example.com/a", "http://example.com:80/x?y=1", "ftp://x/y",
                       "http://localhost:8760/x", "http://192.168.1.2/x", "http://nas.local/x", "http://[::1]/x", "http://10.0.0.1/x",
                       "http://172.20.1.1/x", "http://172.40.1.1/x"],
            "identity": [
                f"https://www.xiaohongshu.com/explore/{NOTE_ID}?xsec_token=AAA",
                f"https://www.rednote.com/discovery/item/{NOTE_ID}?xsec_token=BBB&share_id=x",
                "http://Example.com/a?utm_source=wx&id=3#frag",
                "https://example.com/a?id=3",
                "http://xhslink.com/o/qr4LgnaedG",
            ],
            "identity2": IDENT,
            "insecure": [
                [-1200, "http://a.com/x", "https://a.com/x"],     # 加密握手失败，原链接是 http
                [-1004, "http://a.com/x", "https://a.com/x"],     # 443 连不上，原链接是 http
                [-1004, "https://a.com/x", "https://a.com/x"],    # 原链接本来就是 https：网站只是打不开
                [-1001, "http://a.com/x", "https://a.com/x"],     # 超时
                [-1004, "http://a.com:8080/x", "https://a.com:8080/x"],
                [-1202, "http://a.com/x", "https://a.com/x"],     # 证书不受信任
            ],
            "keys": KEYS,
            "bodyNames": [[u for u, _ in a["pairs"]] for a in BODY_NAMES],
            "mmbiz": MMBIZ + [OTHER],
            "wechat": WECHAT,
            "post": {
                # 图片消息：正文里的图和页面数据里的图有重叠，正文里同一张图的两种写法各出现一次
                "parts": ["简介一段", "![](IMG:0)\n\n![](IMG:1)\n\n![](IMG:2)"],
                "body": [MMBIZ[0], OTHER, MMBIZ[1]],
                "pictures": [MMBIZ[1], "https://mmbiz.qpic.cn/sz_mmbiz_jpg/NEW777/0?wx_fmt=jpeg", OTHER],
            },
            "merge": MERGE_CASES,
        }
        inp = os.path.join(tmp, "in.json")
        with open(inp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        r = subprocess.run([exe, inp], capture_output=True, text=True, timeout=60, env={**os.environ, "TZ": "Asia/Shanghai"})
        assert r.returncode == 0, r.stderr
        yield json.loads(r.stdout)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_fixture_is_trimmed_and_anonymous():
    page = _fixture()
    assert len(page) < 12000
    assert "DUMMYTOKEN" in page and not re.search(r"xsec_token=(?!DUMMYTOKEN)", page)
    assert re.search(r'"userId": ?"0{24}"', page)
    assert "undefined" in page   # 原样保留不加引号的 undefined


def test_real_note(swift):
    a = swift["pages"]["real"]
    assert a["title"] == "两小时从 0 训出 64M 大模型，全开源"
    assert a["source"] == "字节笔记本"
    assert a["url"] == f"https://www.xiaohongshu.com/explore/{NOTE_ID}?xsec_token=DUMMYTOKEN"
    assert a["published"] == "2026-11-23" and a["publishedAt"].startswith("2026-11-23T")
    assert a["titleGuessed"] is False and a["imagesOnly"] is False   # 正文有好几段字
    md = a["markdown"]
    assert md.startswith("GitHub 上有个 68.5k 星的项目叫 minimind")
    assert "\t" not in md and "\n\n最小的模型只有 26M 参数" in md
    assert "登录" not in md and "ICP" not in md   # 页面上的导航栏、页脚不进正文
    assert len(a["images"]) == 3 and md.count("![](attachments/") == 3
    assert all(u.startswith("https://sns-web-i10.rednotecdn.com/") for u in a["images"].values())
    # 图按笔记里的顺序
    order = [a["images"][m] for m in re.findall(r"!\[\]\((attachments/[^)]+)\)", md)]
    assert [u.rsplit("/", 1)[1].split("!")[0] for u in order] == [
        "1040g3k8327sl8fir70b048ripjauhgvk6kld92o", "1040g3k8327sl8fir708048ripjauhgvkhv61ejg", "1040g3k8327sl8fir707g48ripjauhgvkqvpgks0"]


def test_desktop_page_shape(swift):
    a = swift["pages"]["desktop"]
    assert a["title"] == swift["pages"]["real"]["title"] and len(a["images"]) == 3
    assert a["url"].endswith("xsec_token=DUMMYTOKEN")


def test_video_note(swift):
    a = swift["pages"]["video"]
    assert "（视频笔记，视频没有存）" in a["markdown"] and len(a["images"]) == 1


def test_tags_tabs_guessed_title_and_ms_time(swift):
    a = swift["pages"]["tagged"]
    assert a["title"] == "周末去了一趟植物园，人不多，银杏全黄了" and a["titleGuessed"] is True
    assert a["source"] == "某某"
    assert a["publishedAt"].startswith("2026-11-23T")
    assert "门票 20 元[笑哭R]，地铁直达" in a["markdown"]
    assert "#植物园 #秋天" in a["markdown"] and "[话题]" not in a["markdown"] and "\t" not in a["markdown"]
    assert a["imagesOnly"] is True   # 字少，图是主体：写摘要时给模型看图
    # urlDefault 的两个签名不同、文件编号一样的地址只存一张；infoList 取默认尺寸那张；都换成 https
    assert sorted(u.rsplit("/", 1)[1] for u in a["images"].values()) == ["1040g2sg31aaaaaaaaaaaaaaaa!nd_dft_wlteh_webp_3", "1040g2sg31bbbbbbbbbbbbbbbb!nd_dft_wlteh_webp_3"]
    assert all(u.startswith("https://") for u in a["images"].values())


def test_og_fallback(swift):
    a = swift["pages"]["og"]
    assert a["title"] == "两小时从 0 训出 64M 大模型，全开源"   # 去掉「 - rednote」
    assert a["markdown"].startswith("GitHub 上有个 68.5k 星的项目") and len(a["images"]) == 3
    assert a["url"] == f"https://www.xiaohongshu.com/explore/{NOTE_ID}?xsec_token=DUMMYTOKEN"


@pytest.mark.parametrize("name", ["home", "notfound", "blank", "profile", "search"])
def test_dead_pages_give_clear_error(swift, name):
    # gone：确定打不开，收件箱报一次错就不再自动重试
    assert swift["pages"][name] == {"error": "这条小红书笔记打不开：可能已删除、链接过期，或者需要登录", "gone": True}


def test_xhs_image_names_survive_new_signatures(swift):
    """签名变了的同一条笔记：图的文件名一样（重新下载、补下过期的图时对得上），地址是新的。"""
    old, new = swift["pages"]["real"]["images"], swift["pages"]["resigned"]["images"]
    assert set(old) == set(new) and old != new
    # 电脑上按同一套规则在笔记页里找图（重新下载这张图）
    found = clips.remote_images(_fixture(), FINAL)
    assert all(found.get(k) == v for k, v in old.items())


def test_dead_entries_and_duplicates_in_inbox_code():
    """收件箱：确定打不开的报一次错就删、不再重试；重复的写明是剪藏里的哪一篇（在「书架 → 剪藏」列表顶上的状态行里）。"""
    with open(os.path.join(ROOT, "apps", "mobile-ios", "Spark", "Clips.swift"), encoding="utf-8") as f:
        src = f.read()
    assert "if e.gone == true || e.created < Date().addingTimeInterval(-3 * 86400) { ClipInbox.remove(e.id) }" in src
    assert "跟剪藏里的《\\(old.title)》是同一篇，没有再存" in src
    assert "if let s = processor.status {" in src
    with open(os.path.join(SHARED, "Clipper.swift"), encoding="utf-8") as f:
        clip = f.read()
    assert "guard let u = URL(string: e.original ?? e.url)" in clip and "if e.article == nil, e.gone != true {" in clip


def test_json_safe(swift):
    first, second = swift["jsonSafe"]
    assert json.loads(first) == {"a": None, "b": "undefined", "c": [None, 1], "d": 'x"undefined'}
    assert second == '{"undefinedKey":1,"e":undefinedish}'


def test_desc_text(swift):
    assert swift["text"] == ["第一段\n\n第二段 #话题甲 #话题乙\n\n[哭惹R] 第三行", "开头 中间结尾 #A"]


def test_extract_link_from_share_text(swift):
    assert swift["extract"] == [
        "http://xhslink.com/o/qr4LgnaedG",
        "https://example.com/a?b=1",
        "https://example.com/path",
        "https://en.wikipedia.org/wiki/Foo_(bar)",
        "https://example.com",                     # 不带 http 的域名系统也认（跟这一批以前一样，系统补的是 https）
        "https://example.com/x",
        "https://a.example.com/1",
        "https://zh.wikipedia.org/wiki/%E4%B8%AD%E5%9B%BD",   # 路径里的中文
        "https://xn--fsqu00a.xn--fiqs8s/a",                   # 中文域名
        "https://example.com/a_(b)",
        "https://a.com/x",
        "https://example.com/a",
        "https://example.com/a",
    ]
    # 后备（系统一个链接都没认出来时）只认 ASCII
    assert swift["asciiExtract"][:3] == ["http://xhslink.com/o/qr4LgnaedG", "https://example.com/a?b=1", "https://example.com/path"]


def test_insecure_only_message_rules(swift):
    assert swift["insecure"] == [True, True, False, False, False, True]


def test_image_keys_and_stable_names(swift):
    k = swift["keys"]
    assert k[0] != k[1]                                   # chart?from=2020 / 2021 是两张图
    assert k[2] == "//example.com/a.png?tp=webp" and k[3] == "//example.com/a.png"
    assert k[4] != k[5]                                   # 小红书静态图编号太短
    assert k[6] == k[7] == "xhs/1040g3k8327sl8fir70b048ripjauhgvk6kld92o"
    assert k[8] == k[9] == k[10] == "qpic/sz_mmbiz_jpg/AbC123xyz"
    assert swift["stable"][6] == swift["stable"][7]
    assert swift["fallback"][8] == W640 and swift["fallback"][9] == "https://mmbiz.qpic.cn/sz_mmbiz_jpg/AbC123xyz/640?wx_fmt=jpeg"
    assert swift["fallback"][10] == "" and swift["fallback"][0] == ""
    # 电脑上逐字一样
    assert [clips.image_key(u) for u in KEYS] == k
    assert [clips.stable_name(u) for u in KEYS] == swift["stable"]
    assert [clips.fallback_url(u) for u in KEYS] == swift["fallback"]


def test_body_image_names_unchanged(swift):
    """普通文章正文里的图，文件名跟这一批以前一样（以前存的剪藏引用着它们）：两篇线上文章 48 + 38 张图。"""
    for article, got in zip(BODY_NAMES, swift["bodyNames"]):
        assert got == [n for _, n in article["pairs"]], article["article"]
    assert sum(len(a["pairs"]) for a in BODY_NAMES) == 48 + 38


def test_http_upgrade_and_identity(swift):
    assert swift["secure"] == ["https://xhslink.com/o/qr4LgnaedG", "https://example.com/a", "https://example.com/x?y=1", "ftp://x/y",
                               "http://localhost:8760/x", "http://192.168.1.2/x", "http://nas.local/x", "http://[::1]/x",
                               "http://10.0.0.1/x", "http://172.20.1.1/x", "https://172.40.1.1/x"]   # 本机、局域网不换
    ids = swift["identity"]
    assert ids[0] == ids[1] == f"xhs:{NOTE_ID}"          # 同一条笔记两次分享的地址
    assert ids[2] == "https://example.com/a?id=3" == ids[3]
    assert swift["noteID"] == [NOTE_ID, NOTE_ID, "", "", ""]
    # 单页网站的 #/ 路由是两篇
    i2 = dict(zip(IDENT, swift["identity2"]))
    assert i2["https://example.com/#/post/1"] != i2["https://example.com/#/post/2"]
    assert i2["https://example.com/a#x"] == i2["https://example.com/a"] == i2["http://example.com:80/a"] == i2["https://example.com:443/a"]
    assert i2["https://example.com/%E4%B8%AD"] == i2["https://example.com/中"]
    # 电脑上逐字一样
    assert [clips.link_identity(u) for u in IDENT] == swift["identity2"]
    assert [clips.link_identity(u) for u in ("https://www.xiaohongshu.com/explore/" + NOTE_ID + "?xsec_token=AAA",
                                             "https://www.rednote.com/discovery/item/" + NOTE_ID.upper() + "?x=1",
                                             "http://Example.com/a?utm_source=wx&id=3#frag")] == [
        f"xhs:{NOTE_ID}", f"xhs:{NOTE_ID}", "https://example.com/a?id=3"]


def test_mmbiz_variants_collapse(swift):
    keys = swift["mmbizKeys"]
    assert len(set(keys[:len(MMBIZ)])) == 1 and keys[-1] != keys[0]
    assert "\\x26" not in "".join(swift["mmbizClean"]) and "&amp;" not in "".join(swift["mmbizClean"])
    assert swift["mmbizUnique"] == ["https://mmbiz.qpic.cn/sz_mmbiz_jpg/AbC123xyz/0?wx_fmt=jpeg", OTHER]
    # 电脑上的同一套
    cleaned = [clips.clean_image_url(u) for u in MMBIZ + [OTHER]]
    assert cleaned == swift["mmbizClean"]
    assert len({clips.image_key(u) for u in cleaned[:len(MMBIZ)]}) == 1
    assert clips.unique_image_urls(cleaned) == swift["mmbizUnique"]


def test_picture_urls_dedupe(swift):
    assert swift["pictures"] == ["https://mmbiz.qpic.cn/sz_mmbiz_jpg/AbC123xyz/0?wx_fmt=jpeg",
                                 "https://mmbiz.qpic.cn/sz_mmbiz_png/ZzZ999/0?wx_fmt=png&from=appmsg"]


def test_image_post_body_and_pictures_overlap(swift):
    parts, images = swift["post"]["parts"], swift["post"]["images"]
    md = "\n\n".join(parts)
    refs = re.findall(r"!\[\]\(([^)]+)\)", md)
    assert len(refs) == len(set(refs)) == 3 == len(images)   # 两张重复的图各存一份，新图接在后面
    assert sorted(images.values()) == sorted(["https://mmbiz.qpic.cn/sz_mmbiz_jpg/AbC123xyz/0?wx_fmt=jpeg", OTHER,
                                              "https://mmbiz.qpic.cn/sz_mmbiz_jpg/NEW777/0?wx_fmt=jpeg"])
    assert set(refs) == set(images)   # 没有指向没存的图的引用
    # 只在页面数据里的图按 key 起名，电脑「重新下载这张图」在页面的 cdn_url 里按同一个名字找得到；正文里的图照旧按地址起名
    new = "https://mmbiz.qpic.cn/sz_mmbiz_jpg/NEW777/0?wx_fmt=jpeg"
    name = next(k for k, v in images.items() if v == new)
    assert name == clips.stable_name(new)
    page = "<script>var x = {cdn_url: 'https://mmbiz.qpic.cn/sz_mmbiz_jpg/NEW777/640?wx_fmt=jpeg\\x26amp;from=appmsg'};</script>"
    assert name in clips.remote_images(page, "https://mp.weixin.qq.com/s/x")
    assert clips.local_name(OTHER if OTHER in images.values() else OTHER.replace("/640", "/0")) in images


@pytest.mark.parametrize("i", range(len(MERGE_CASES)))
def test_merge_identical_swift(swift, i):
    case, got = MERGE_CASES[i], swift["merge"][i]
    assert got["markdown"] == case["want_md"] and sorted(got["images"]) == case["want"]
    assert set(re.findall(r"\]\(([^)]+)\)", got["markdown"])) <= set(got["images"])   # 没有指向没了的图的引用


# ---------------------------------------------------------------- 电脑这边

def _write(root, rel, data):
    full = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "wb") as f:
        f.write(data)


@pytest.mark.parametrize("i", range(len(MERGE_CASES)))
def test_merge_identical_python(tmp_path, i):
    case, root = MERGE_CASES[i], str(tmp_path)
    for name, digest in case["digests"].items():
        _write(root, f"{clips.CLIPS_DIR}/{name}", digest.encode() * 100)
    images = {f"{clips.CLIPS_DIR}/{k}": v for k, v in case["images"].items()}
    out_md, out_images = clips.merge_identical(case["markdown"], images, root, drop_repeats=case["imagePost"])
    assert out_md == case["want_md"] and sorted(k[len(clips.CLIPS_DIR) + 1:] for k in out_images) == case["want"]


def test_merge_variants_python():
    k = [f"{clips.CLIPS_DIR}/{clips.local_name(clips.clean_image_url(u))}" for u in MMBIZ[:3]]
    images = {k[0]: clips.clean_image_url(MMBIZ[0]), k[1]: clips.clean_image_url(MMBIZ[1]), k[2]: clips.clean_image_url(MMBIZ[2])}
    md = "\n\n".join(f"![]({x[len(clips.CLIPS_DIR) + 1:]})" for x in k)
    out_md, out_images = clips.merge_variants(md, images, drop_repeats=True)
    assert list(out_images) == [k[1]] and out_md == f"![]({k[1][len(clips.CLIPS_DIR) + 1:]})"


def _article(url, markdown="正文", images=None, images_only=False):
    a = {"url": url, "title": "标题", "source": "某某", "published": "", "markdown": markdown, "images": images or {}}
    if images_only:
        a["imagesOnly"] = True
    return {"article": a, "note": ""}


def test_capture_warns_on_same_xhs_note(tmp_path, monkeypatch):
    """Mac 上同一条笔记换个分享链接再存：照旧另存（跟以前一样），提醒以前存过。"""
    monkeypatch.setattr(clip_capture, "_JOBS", {})
    monkeypatch.setattr(clip_capture, "_ask", lambda: None)
    monkeypatch.setattr(clip_capture.github_sync, "after_task", lambda: None)
    root = str(tmp_path)
    _write(root, f"{clips.CLIPS_DIR}/20261122_旧的.md",
           f"---\ntitle: \"旧的\"\nurl: \"https://www.xiaohongshu.com/explore/{NOTE_ID}?xsec_token=OLD\"\n---\n\n正文\n".encode())
    for url, warned in ((f"https://www.xiaohongshu.com/explore/{NOTE_ID}?xsec_token=NEW", True),
                        ("https://www.xiaohongshu.com/explore/" + "a" * 24 + "?xsec_token=NEW", False)):
        article, note = clip_capture.parse(_article(url))
        job = clip_capture._new_job("正在保存")
        clip_capture._run(job, article, note, root)
        st = clip_capture.status(job)
        assert st["error"] == "" and st["path"]
        assert ("以前存过" in st["warning"]) is warned


def test_capture_run_merges_duplicate_images(tmp_path, monkeypatch):
    monkeypatch.setattr(clip_capture, "_JOBS", {})
    root = str(tmp_path)
    u1, u2 = clips.clean_image_url(MMBIZ[0]), clips.clean_image_url(MMBIZ[1])
    u3, u4 = "https://example.com/a.jpg", "https://example.com/b.jpg"
    n = {u: clips.local_name(u) for u in (u1, u2, u3, u4)}
    images = {f"{clips.CLIPS_DIR}/{n[u]}": u for u in (u1, u2, u3, u4)}
    md = "简介\n\n" + "\n\n".join(f"![]({n[u]})" for u in (u1, u2, u3, u4))
    payload = {u3: b"pic", u4: b"pic", u2: b"orig"}   # u3、u4 地址不同、字节一样

    def download(imgs, root_):
        for key, url in imgs.items():
            _write(root_, key, payload.get(url, b"x"))
        return len(imgs), 0
    monkeypatch.setattr(clips, "download_images", download)
    monkeypatch.setattr(clip_capture, "_ask", lambda: None)
    monkeypatch.setattr(clip_capture.github_sync, "after_task", lambda: None)
    article, note = clip_capture.parse(_article("https://mp.weixin.qq.com/s/x", md, {k[len(clips.CLIPS_DIR) + 1:]: v for k, v in images.items()},
                                                images_only=True))
    job = clip_capture._new_job("正在下载图片")
    clip_capture._run(job, article, note, root)
    st = clip_capture.status(job)
    assert st["error"] == "", st
    with open(os.path.join(root, *st["path"].split("/")), encoding="utf-8") as f:
        text = f.read()
    refs = re.findall(r"!\[\]\((attachments/[^)]+)\)", text)
    assert refs == [n[u2], n[u3]]   # 公众号同一张图留原图（/0），字节一样的两张留先出现的
    for r in refs:
        assert os.path.exists(os.path.join(root, clips.CLIPS_DIR, r))
    assert not os.path.exists(os.path.join(root, clips.CLIPS_DIR, n[u1]))   # 小图版本没下


INBOX_HARNESS = r'''
import Foundation
enum SharedStore { static let groupID = "test" }
let dec = JSONDecoder()
dec.dateDecodingStrategy = .iso8601
// 这一批以前写的收件箱条目（没有 original、gone）
let old = #"{"id":"a","url":"https://example.com/x","note":"","created":"2026-11-20T00:00:00Z","error":"网页打不开"}"#
let e = try! dec.decode(ClipInbox.Entry.self, from: Data(old.utf8))
let enc = JSONEncoder()
enc.dateEncodingStrategy = .iso8601
var n = e
n.original = "http://example.com/x"
n.gone = true
let back = try! dec.decode(ClipInbox.Entry.self, from: try! enc.encode(n))
print(e.original == nil && e.gone == nil && e.error == "网页打不开", back.original ?? "", back.gone ?? false)
'''


def test_old_inbox_entries_still_decode(tmp_path):
    """收件箱条目加了 original、gone 两个可选字段：以前写的 meta.json 照样读得出来。
    ClipInbox 只在 iOS 上编译，这里把 #if !os(macOS) 换掉、补一个 SharedStore 在 Mac 上编。"""
    if not shutil.which("swiftc"):
        pytest.skip("没有 swiftc")
    with open(os.path.join(SHARED, "Clipper.swift"), encoding="utf-8") as f:
        src = f.read().replace("#if !os(macOS)", "#if true")
    (tmp_path / "Clipper.swift").write_text(src, encoding="utf-8")
    (tmp_path / "main.swift").write_text(INBOX_HARNESS, encoding="utf-8")
    files = [str(tmp_path / "Clipper.swift")] + [f for f in SWIFT_FILES if not f.endswith("Clipper.swift")] + [str(tmp_path / "main.swift")]
    exe = str(tmp_path / "inbox")
    r = subprocess.run(["swiftc", "-o", exe, *files], capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, r.stderr
    assert subprocess.run([exe], capture_output=True, text=True, timeout=60).stdout.split() == ["true", "http://example.com/x", "true"]


def test_download_falls_back_to_640(tmp_path, monkeypatch):
    """公众号原图（/0）下不来时退回去下 /640，文件名不变；别的图没有退路。"""
    from core import sources
    got = []

    def fake(url, timeout=20, max_bytes=0, user_agent=""):
        got.append(url)
        if "/0?" in url or url.endswith("/0"):
            raise OSError("too big")
        return b"img"
    monkeypatch.setattr(sources, "http_get", fake)
    key = f"{clips.CLIPS_DIR}/attachments/{'a' * 16}.jpg"
    assert clips.download_images({key: W0}, str(tmp_path)) == (1, 0)
    assert got == [W0, W640] and (tmp_path / clips.CLIPS_DIR / f"attachments/{'a' * 16}.jpg").read_bytes() == b"img"


def test_refresh_signed_xhs_images(tmp_path, monkeypatch):
    """手机交来的小红书剪藏：图的签名地址过期下不来，电脑重新打开笔记（用手机浏览器的 UA）拿新地址补下。"""
    from core import sources
    page = _fixture()
    seen = []

    def fake(url, timeout=20, max_bytes=0, user_agent=""):
        seen.append((url, user_agent))
        if "rednotecdn.com" in url:
            if "/expired/" in url:
                raise OSError("403")
            return b"img"
        return page.encode()
    monkeypatch.setattr(sources, "http_get", fake)
    fresh = clips.remote_images(page, FINAL)
    name = next(k for k in fresh if k.startswith("attachments/") and "rednotecdn" in fresh[k] and k == clips.stable_name(fresh[k]))
    key = f"{clips.CLIPS_DIR}/{name}"
    stale = {key: "https://sns-web-i10.rednotecdn.com/expired/x/notes_pre_post/" + fresh[name].rsplit("/", 1)[1]}
    root = str(tmp_path)
    assert clips.download_images(stale, root) == (0, 1)
    url = f"https://www.xiaohongshu.com/explore/{NOTE_ID}?xsec_token=DUMMYTOKEN"
    assert clips.refresh_signed_images(stale, url, root) == 1
    assert (url, clips.IPHONE_UA) in seen and os.path.exists(os.path.join(root, *key.split("/")))
    # 不是小红书的不管
    assert clips.refresh_signed_images({f"{clips.CLIPS_DIR}/attachments/{'b' * 16}.jpg": "https://example.com/b.jpg"},
                                       "https://example.com/post", root) == 0


def test_redownload_xhs_image_matches_by_file_id(tmp_path, monkeypatch):
    """Mac 阅读页「重新下载这张图」：小红书笔记按文件编号找图，不按每次都变的签名地址。"""
    from core import sources
    page = _fixture()
    seen = []

    def fake(url, timeout=20, max_bytes=0, user_agent=""):
        seen.append(user_agent)
        return page.encode()
    monkeypatch.setattr(sources, "http_get", fake)
    downloaded = {}
    monkeypatch.setattr(clips, "download_images", lambda images, root="": downloaded.update(images) or (1, 0))
    src = clips.stable_name("https://sns-web-i10.rednotecdn.com/OLD/SIG/notes_pre_post/1040g3k8327sl8fir708048ripjauhgvkhv61ejg!rnh5jpg")
    text = f"---\nurl: \"https://www.xiaohongshu.com/explore/{NOTE_ID}?xsec_token=DUMMYTOKEN\"\n---\n\n![]({src})\n"
    assert clips.redownload_image(text, src, str(tmp_path)) == ""
    assert seen == [clips.IPHONE_UA]
    assert list(downloaded.values())[0].endswith("1040g3k8327sl8fir708048ripjauhgvkhv61ejg!rnh5jpg")
