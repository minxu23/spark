import os

from core import fs_browse


def _mkdirs(tmp_path, *names):
    for name in names:
        (tmp_path / name).mkdir()


def test_目录结尾时列出该目录下的所有子目录(tmp_path):
    _mkdirs(tmp_path, "alpha", "beta")
    (tmp_path / "afile.txt").write_text("x")
    got = fs_browse.browse_dir_suggestions(str(tmp_path) + "/")
    assert got == [str(tmp_path / "alpha"), str(tmp_path / "beta")]


def test_按未打完的前缀过滤(tmp_path):
    _mkdirs(tmp_path, "alpha", "another", "beta")
    got = fs_browse.browse_dir_suggestions(str(tmp_path / "al"))
    assert got == [str(tmp_path / "alpha")]


def test_不区分大小写(tmp_path):
    _mkdirs(tmp_path, "Alpha")
    got = fs_browse.browse_dir_suggestions(str(tmp_path / "al"))
    assert got == [str(tmp_path / "Alpha")]


def test_隐藏目录不出现在建议里(tmp_path):
    _mkdirs(tmp_path, ".git", "visible")
    got = fs_browse.browse_dir_suggestions(str(tmp_path) + "/")
    assert got == [str(tmp_path / "visible")]


def test_只列目录不列文件(tmp_path):
    _mkdirs(tmp_path, "sub")
    (tmp_path / "note.md").write_text("x")
    got = fs_browse.browse_dir_suggestions(str(tmp_path) + "/")
    assert got == [str(tmp_path / "sub")]


def test_上级目录不存在时返回空列表():
    assert fs_browse.browse_dir_suggestions("/这个路径/不存在/xyz") == []


def test_波浪号会被展开(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    _mkdirs(tmp_path, "Documents")
    got = fs_browse.browse_dir_suggestions("~/Doc")
    assert got == [str(tmp_path / "Documents")]


def test_空字符串默认列出用户主目录(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    _mkdirs(tmp_path, "only")
    got = fs_browse.browse_dir_suggestions("")
    assert got == [str(tmp_path / "only")]


def test_超过上限时截断(tmp_path):
    names = [f"dir{i:02d}" for i in range(30)]
    _mkdirs(tmp_path, *names)
    got = fs_browse.browse_dir_suggestions(str(tmp_path) + "/")
    assert len(got) == fs_browse.MAX_SUGGESTIONS


def test_目录本身存在时算靠谱(tmp_path):
    _mkdirs(tmp_path, "sub")
    assert fs_browse.dir_plausible(str(tmp_path / "sub")) is True


def test_目录不存在但上级存在时算靠谱(tmp_path):
    """给一个还没生成过的输出目录起名字，字面上自然不存在，但落点是真实的。"""
    assert fs_browse.dir_plausible(str(tmp_path / "还没生成过的节目")) is True


def test_上级目录也不存在时不算靠谱(tmp_path):
    assert fs_browse.dir_plausible(str(tmp_path / "并不存在" / "更深一层")) is False


def test_空字符串不算靠谱():
    assert fs_browse.dir_plausible("") is False
    assert fs_browse.dir_plausible("   ") is False
