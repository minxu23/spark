import os

from core import digest


# --------------------------------------------------------------------------
# 键
# --------------------------------------------------------------------------

def test_同样的因素算出同样的键():
    assert digest.cache_key("a", "b") == digest.cache_key("a", "b")


def test_任一因素变了键就变():
    assert digest.cache_key("a", "b") != digest.cache_key("a", "c")


def test_不同的参数组合算出不同的键():
    seen = {digest.cache_key(*p) for p in [("a", "b"), ("ab",), ("a", "b", "")]}
    assert len(seen) == 3


def test_已知边界_分隔符出现在参数里会撞键():
    """用 "|" 连接，所以 ("a|b",) 和 ("a","b") 撞成同一个键。

    这是已知且被接受的边界，不是遗漏：真要撞上，得有人把 "|" 写进关注点、而且
    刚好和另一组 (模型, 关注点) 拼出同一串——概率极低，后果也只是复用了一张稍微
    不对的摘要卡。修它要改键的算法，而改算法会让现有缓存整批作废，正是这一串
    改动想避免的事。将来若真要改，连同一次缓存迁移一起做。
    """
    assert digest.cache_key("a|b") == digest.cache_key("a", "b")


def test_内容哈希只看内容():
    assert digest.content_hash("正文") == digest.content_hash("正文")
    assert digest.content_hash("正文") != digest.content_hash("正文 ")


# --------------------------------------------------------------------------
# 存取
# --------------------------------------------------------------------------

def test_存取一轮(tmp_path):
    digest.cache_put("k1", "内容", str(tmp_path))
    assert digest.cache_get("k1", str(tmp_path)) == "内容"


def test_没命中返回_None(tmp_path):
    assert digest.cache_get("不存在", str(tmp_path)) is None


def test_目录不可写不该让任务失败(tmp_path):
    """缓存写不进去大不了下次重算，不该把整个任务带崩。"""
    target = tmp_path / "file"
    target.write_text("x", encoding="utf-8")
    digest.cache_put("k", "v", str(target))  # 拿文件当目录用，必然失败
    assert digest.cache_get("k", str(target)) is None


# --------------------------------------------------------------------------
# cached_call
# --------------------------------------------------------------------------

def test_第一次算第二次命中(tmp_path):
    calls = []

    def fn():
        calls.append(1)
        return "结果"

    a, hit_a = digest.cached_call("k", fn, cache_dir=str(tmp_path))
    b, hit_b = digest.cached_call("k", fn, cache_dir=str(tmp_path))
    assert (a, b) == ("结果", "结果")
    assert (hit_a, hit_b) == (False, True)
    assert len(calls) == 1, "命中缓存时不该再调用模型"


def test_关掉缓存就每次都算(tmp_path):
    calls = []
    digest.cached_call("k", lambda: calls.append(1) or "x", cache_dir=str(tmp_path))
    digest.cached_call("k", lambda: calls.append(1) or "x", cache_dir=str(tmp_path),
                       use_cache=False)
    assert len(calls) == 2


def test_空结果不进缓存(tmp_path):
    """上游把空内容当失败，别把失败缓存下来，否则重试永远拿到空的。"""
    digest.cached_call("k", lambda: "   \n ", cache_dir=str(tmp_path))
    assert digest.cache_get("k", str(tmp_path)) is None


# --------------------------------------------------------------------------
# 正文准备
# --------------------------------------------------------------------------

def test_截断会被明确告知():
    text, cut = digest.prepare_body("a" * 100, 10)
    assert (len(text), cut) == (10, True)


def test_没超上限就不动():
    text, cut = digest.prepare_body("abc", 10)
    assert (text, cut) == ("abc", False)


def test_上限为零表示不限制():
    text, cut = digest.prepare_body("a" * 1000, 0)
    assert (len(text), cut) == (1000, False)


def test_分块尽量切在段落边界():
    text = "第一段" * 10 + "\n\n" + "第二段" * 10
    parts = digest.chunks(text, 40)
    assert len(parts) > 1
    assert parts[0].endswith("第一段"), "应当在空行处断开，而不是把一句话劈两半"


def test_短文本不分块():
    assert digest.chunks("短", 100) == ["短"]


def test_去噪掉图片和连续空行():
    assert digest.strip_noise("a\n\n\n\n![图](x.png)b") == "a\n\nb"
