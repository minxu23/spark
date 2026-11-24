"""手机样例库模式的开关（apps/mobile-ios/Shared/Fixture.swift）：用 swiftc 单独编出来跑，只写临时目录。

- 纯逻辑（三种结果：样例库 / 真实 / 拦下）：真机和 Release 永远是真实；模拟器 DEBUG 下给了 SPARK_FIXTURE_DIR 是样例库，
  否则 SPARK_SIM_REAL=1 是真实，都没有就拦下；分享扩展看 App Group 里的两种标记；
- 不是 DEBUG 构建、不是模拟器（这里是 macOS）时，给了环境变量也是关，样例库和拦下的代码根本不编进去；
- 开着时（编译时加 SPARK_FIXTURE_TEST 顶替「模拟器」这一条）：存储换位置、关口拦请求、
  拷样例库前清掉上一次的、数据线清单的 sha 跟 git 一样、UserDefaults.standard 换成样例库的 suite。
不读真实笔记库，不联网，不写 ~/Library。"""

import hashlib
import json
import os
import re
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHARED = os.path.join(ROOT, "apps", "mobile-ios", "Shared")
SRCS = [os.path.join(SHARED, "Fixture.swift"), os.path.join(SHARED, "SparkError.swift")]
FIXTURE_VAULT = os.path.join(ROOT, "apps", "mobile-ios", "dev", "fixture-vault")

MAIN = r'''
import Foundation
let args = CommandLine.arguments
var out: [String: Any] = [:]

#if SPARK_FIXTURE_TEST
// 判定的各种输入：键是「debug|sim|目录|SPARK_SIM_REAL」
var modes: [String: String] = [:]
for debug in [false, true] {
    for sim in [false, true] {
        for dir in [nil, "", "   ", "/tmp/fixture-vault"] as [String?] {
            for real in [nil, "", "0", "1"] as [String?] {
                modes["\(debug)|\(sim)|\(dir ?? "nil")|\(real ?? "nil")"] =
                    Fixture.mode(debug: debug, simulator: sim, sourceDir: dir, simReal: real).rawValue
            }
        }
    }
}
out["modes"] = modes
var ext: [String: String] = [:]
for debug in [false, true] {
    for sim in [false, true] {
        for fx in [false, true] {
            for rl in [false, true] {
                ext["\(debug)|\(sim)|\(fx)|\(rl)"] = Fixture.extensionMode(debug: debug, simulator: sim, fixtureMarker: fx, realMarker: rl).rawValue
            }
        }
    }
}
out["ext"] = ext
out["current"] = Fixture.currentMode.rawValue
out["isBlocked"] = Fixture.isBlocked
#endif
out["isOn"] = Fixture.isOn
out["badge"] = Fixture.badge
out["offline"] = Fixture.offlineMessage
let real = URL(fileURLWithPath: "/real/support", isDirectory: true)
out["support"] = Fixture.support(real).path
out["caches"] = Fixture.caches(URL(fileURLWithPath: "/real/caches")).path
out["group"] = Fixture.groupContainer(URL(fileURLWithPath: "/real/group"))?.path ?? "nil"
do { try NetGate.check(); out["gate"] = "pass" } catch { out["gate"] = error.localizedDescription }
out["blocked"] = NetGate.blocked

#if SPARK_FIXTURE_TEST
if args.count > 2 {
    let tmp = URL(fileURLWithPath: args[1], isDirectory: true)
    Fixture.testRoot = tmp
    out["supportOn"] = Fixture.support(real).path
    out["cachesOn"] = Fixture.caches(URL(fileURLWithPath: "/real/caches")).path
    out["groupOn"] = Fixture.groupContainer(URL(fileURLWithPath: "/real/group"))?.path ?? "nil"
    // 上一次留下的东西：拷之前要清掉
    let fm = FileManager.default
    try! fm.createDirectory(at: Fixture.appRoot.appending(path: "support/vault"), withIntermediateDirectories: true)
    try! Data("old".utf8).write(to: Fixture.appRoot.appending(path: "support/vault/stale.md"))
    try! fm.createDirectory(at: Fixture.groupRoot!.appending(path: "Library/Spark/readlog"), withIntermediateDirectories: true)
    try! Data("old".utf8).write(to: Fixture.groupRoot!.appending(path: "Library/Spark/readlog/phone.jsonl"))
    try! Data("x".utf8).write(to: Fixture.groupRoot!.appending(path: "real"))   // 上次用真实模式启动过
    let source = URL(fileURLWithPath: args[2], isDirectory: true)
    try! Fixture.installFiles(source: source, appRoot: Fixture.appRoot, groupRoot: Fixture.groupRoot)
    let vault = Fixture.appRoot.appending(path: "support/vault")
    out["staleGone"] = !fm.fileExists(atPath: vault.appending(path: "stale.md").path)
    out["readlogGone"] = !fm.fileExists(atPath: Fixture.groupRoot!.appending(path: "Library/Spark/readlog/phone.jsonl").path)
    out["marker"] = fm.fileExists(atPath: Fixture.groupRoot!.appending(path: "active").path)
    out["realMarkerGone"] = !fm.fileExists(atPath: Fixture.groupRoot!.appending(path: "real").path)
    // 反过来：真实模式启动留「真实」标记、清掉样例库标记
    Fixture.markRealMode(groupRoot: Fixture.groupRoot)
    out["afterReal"] = [fm.fileExists(atPath: Fixture.groupRoot!.appending(path: "active").path),
                        fm.fileExists(atPath: Fixture.groupRoot!.appending(path: "real").path)]
    try! Fixture.installFiles(source: source, appRoot: Fixture.appRoot, groupRoot: Fixture.groupRoot)
    out["afterFixture"] = [fm.fileExists(atPath: Fixture.groupRoot!.appending(path: "active").path),
                           fm.fileExists(atPath: Fixture.groupRoot!.appending(path: "real").path)]
    out["manifest"] = String(decoding: try! Data(contentsOf: vault.appending(path: ".spark-cable.json")), as: UTF8.self)
    out["dsStoreGone"] = !fm.fileExists(atPath: vault.appending(path: "Spark/.DS_Store").path)
    out["cachesMade"] = fm.fileExists(atPath: Fixture.appRoot.appending(path: "caches").path)
    // 找不到样例库目录：报错，不留半份
    do {
        try Fixture.installFiles(source: tmp.appending(path: "no-such-dir"), appRoot: Fixture.appRoot, groupRoot: Fixture.groupRoot)
        out["missing"] = "no error"
    } catch { out["missing"] = error.localizedDescription }
    out["vaultAfterMissing"] = fm.fileExists(atPath: vault.path)
    // UserDefaults.standard 换成样例库的 suite（不往里写，免得在 ~/Library/Preferences 留文件）
    let suite = UserDefaults(suiteName: "spark-fixture-test-\(UUID().uuidString)")!
    let before = UserDefaults.standard
    Fixture.pinStandardDefaults(to: suite)
    out["pinned"] = UserDefaults.standard === suite && before !== suite
    // 兜底的 URLProtocol：http(s) 都接，本地文件不接
    out["noNetHTTP"] = Fixture.NoNetwork.canInit(with: URLRequest(url: URL(string: "https://example.com/x")!))
    out["noNetFile"] = Fixture.NoNetwork.canInit(with: URLRequest(url: URL(fileURLWithPath: "/tmp/x")))
    out["blob"] = Fixture.blobSHA(Data("hello\n".utf8))
}
#endif
print(String(data: try! JSONSerialization.data(withJSONObject: out, options: [.sortedKeys]), encoding: .utf8)!)
'''


def _build(tmp_path, name, flags):
    d = tmp_path / f"build-{name}"
    d.mkdir()
    main = d / "main.swift"   # 多个文件一起编时，只有叫 main.swift 的能写顶层代码
    main.write_text(MAIN, encoding="utf-8")
    exe = d / name
    r = subprocess.run(["swiftc", *flags, "-o", str(exe), *SRCS, str(main)], capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr
    return exe


def _run(exe, *args, fixture_dir=None, sim_real=None):
    env = {k: v for k, v in os.environ.items() if k not in ("SPARK_FIXTURE_DIR", "SPARK_SIM_REAL")}
    if fixture_dir is not None:
        env["SPARK_FIXTURE_DIR"] = fixture_dir
    if sim_real is not None:
        env["SPARK_SIM_REAL"] = sim_real
    r = subprocess.run([str(exe), *args], capture_output=True, text=True, timeout=60, env=env)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def _git_blob(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


ON_FLAGS = ["-D", "DEBUG", "-D", "SPARK_FIXTURE_TEST"]


@pytest.mark.skipif(not shutil.which("swiftc"), reason="没有 swiftc")
def test_mode_three_outcomes(tmp_path):
    exe = _build(tmp_path, "modes", ON_FLAGS)
    got = _run(exe)
    for key, mode in got["modes"].items():
        debug, sim, d, real = key.split("|")
        if debug != "true" or sim != "true":
            want = "real"                       # 真机、Release 永远是真实
        elif d.strip() not in ("nil", ""):
            want = "fixture"                    # 给了样例库目录：样例库（哪怕也给了 SPARK_SIM_REAL）
        elif real == "1":
            want = "real"                       # 显式要真实数据
        else:
            want = "blocked"                    # 都没说：拦下
        assert mode == want, key
    for key, mode in got["ext"].items():
        debug, sim, fx, rl = key.split("|")
        if debug != "true" or sim != "true":
            want = "real"
        elif fx == "true":
            want = "fixture"                    # 两种标记都在时按样例库，不碰真实数据
        elif rl == "true":
            want = "real"
        else:
            want = "blocked"
        assert mode == want, key


@pytest.mark.skipif(not shutil.which("swiftc"), reason="没有 swiftc")
def test_process_mode_from_environment(tmp_path):
    """进程里定一次：没给环境变量是拦下；SPARK_SIM_REAL=1 是真实（存储原样、关口放行）；给了样例库目录是样例库"""
    exe = _build(tmp_path, "env", ON_FLAGS)
    blocked = _run(exe)
    assert blocked["current"] == "blocked" and blocked["isBlocked"] is True and blocked["isOn"] is False
    assert _run(exe, sim_real="0")["current"] == "blocked"
    real = _run(exe, sim_real="1")
    assert real["current"] == "real" and real["isBlocked"] is False and real["isOn"] is False
    assert real["support"] == "/real/support" and real["gate"] == "pass"
    fx = _run(exe, fixture_dir="/tmp/fixture-vault", sim_real="1")
    assert fx["current"] == "fixture" and fx["isOn"] is True and fx["isBlocked"] is False


@pytest.mark.skipif(not shutil.which("swiftc"), reason="没有 swiftc")
@pytest.mark.parametrize("flags", [[], ["-D", "DEBUG"], ["-O"]])
def test_off_without_simulator_even_with_env(tmp_path, flags):
    """不是模拟器（Release 或 DEBUG 都一样）：给了环境变量也是关，存储原样、关口放行"""
    exe = _build(tmp_path, "probe", flags)
    for env in [None, "/tmp/fixture-vault"]:
        got = _run(exe, fixture_dir=env)
        assert got["isOn"] is False
        assert got["support"] == "/real/support"
        assert got["caches"] == "/real/caches"
        assert got["group"] == "/real/group"
        assert got["gate"] == "pass" and got["blocked"] is False


@pytest.mark.skipif(not shutil.which("swiftc") or not shutil.which("nm"), reason="没有 swiftc / nm")
def test_fixture_code_not_compiled_in_without_simulator(tmp_path):
    """真机、Release：拷样例库、换 UserDefaults 的代码根本不编进去"""
    for flags in [[], ["-D", "DEBUG"]]:
        exe = _build(tmp_path, "nm" + "".join(flags).replace("-", ""), flags)
        syms = subprocess.run(["nm", str(exe)], capture_output=True, text=True, timeout=60).stdout
        for name in ["installFiles", "pinStandardDefaults", "cableManifest", "NoNetwork", "extensionMode", "currentMode",
                     "markRealMode", "guardTitle"]:
            assert name not in syms, (flags, name)


@pytest.mark.skipif(not shutil.which("swiftc"), reason="没有 swiftc")
def test_on_switches_storage_and_blocks_network(tmp_path):
    exe = _build(tmp_path, "on", ON_FLAGS)
    # 没给环境变量：不是样例库（是拦下，见 test_process_mode_from_environment）
    off = _run(exe)
    assert off["isOn"] is False and off["gate"] == "pass" and off["support"] == "/real/support"

    src = tmp_path / "src"
    shutil.copytree(FIXTURE_VAULT, src)
    (src / "Spark" / ".DS_Store").write_bytes(b"junk")
    work = tmp_path / "work"
    got = _run(exe, str(work), str(src), fixture_dir=str(src))
    assert got["isOn"] is True
    assert got["gate"] == "样例库模式不联网" and got["blocked"] is True
    assert got["badge"] == "样例库"
    assert got["supportOn"] == str(work / "app" / "support")
    assert got["cachesOn"] == str(work / "app" / "caches")
    assert got["groupOn"] == str(work / "group")
    assert got["staleGone"] and got["readlogGone"] and got["marker"] and got["dsStoreGone"] and got["cachesMade"]
    assert got["realMarkerGone"] is True
    assert got["afterReal"] == [False, True] and got["afterFixture"] == [True, False]   # 两种标记互斥
    assert "找不到样例库目录" in got["missing"]
    assert got["vaultAfterMissing"] is False   # 先清掉上一次的，再发现源目录不在：不留旧数据
    assert got["pinned"] is True
    assert got["noNetHTTP"] is True and got["noNetFile"] is False
    assert got["blob"] == _git_blob(b"hello\n")

    # 数据线清单：每个文件都在，sha 跟 git 的 blob sha 一样，大小对得上；不含清单自己和 .DS_Store
    m = json.loads(got["manifest"])
    assert re.match(r"\d{4}-\d{2}-\d{2}T", m["generated_at"])
    want = {}
    for d, _, files in os.walk(FIXTURE_VAULT):
        for f in files:
            if f == ".DS_Store":
                continue
            p = os.path.join(d, f)
            data = open(p, "rb").read()
            want[os.path.relpath(p, FIXTURE_VAULT).replace(os.sep, "/")] = [_git_blob(data), len(data)]
    assert m["files"] == want
    assert "Spark/Quiet Workshop/.manifest.json" in m["files"]


def test_group_id_matches_shared_store():
    fixture = open(os.path.join(SHARED, "Fixture.swift"), encoding="utf-8").read()
    task = open(os.path.join(SHARED, "SparkTask.swift"), encoding="utf-8").read()
    a = re.search(r'static let groupID = "([^"]+)"', fixture).group(1)
    b = re.search(r'static let groupID = "([^"]+)"', task).group(1)
    assert a == b


def test_entry_prepares_before_app_state():
    """入口先 Fixture.prepare()，再 SparkApp.main()；SparkApp 自己不再是 @main"""
    src = open(os.path.join(ROOT, "apps", "mobile-ios", "Spark", "SparkApp.swift"), encoding="utf-8").read()
    entry = src[src.index("@main"):src.index("struct SparkApp")]
    assert entry.index("Fixture.prepare()") < entry.index("SparkApp.main()")
    assert src.count("@main") == 1
