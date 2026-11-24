"""手机上的崩溃记录（apps/mobile-ios/Spark/CrashLog.swift）：用 swiftc 单独编出来跑，只写临时目录。
环形记录最多 60 条、形状没变不重复记、查重复 id、记录文件只留 5 份；
再在 macOS 上真抛一个没人接的异常，看处理函数写没写记录、有没有接着调原来的处理函数。"""

import json
import os
import shutil
import subprocess

import pytest

SRC = os.path.join(os.path.dirname(__file__), "..", "apps", "mobile-ios", "Spark", "CrashLog.swift")

MAIN = r'''
import Foundation
let args = CommandLine.arguments
let dir = URL(fileURLWithPath: args[1])
var out: [String: Any] = [:]

// 环形记录：容量 3，记 5 种形状，留下最后 3 条，从旧到新
var ring = CrashLog.Ring(capacity: 3)
for i in 0..<5 { ring.add(.init(list: "L\(i)", rows: [i], duplicates: [])) }
out["ring"] = ring.ordered.map(\.list)
// 同一个列表形状没变不再记；变了才记
var r2 = CrashLog.Ring(capacity: 60)
var added: [Bool] = []
added.append(r2.add(.init(list: "最新", rows: [3, 2], duplicates: [])))
added.append(r2.add(.init(list: "最新", rows: [3, 2], duplicates: [])))
added.append(r2.add(.init(list: "书架", rows: [3, 2], duplicates: [])))
added.append(r2.add(.init(list: "最新", rows: [3, 3], duplicates: [])))
added.append(r2.add(.init(list: "最新", rows: [3, 3], duplicates: ["a"])))
out["added"] = added
var big = CrashLog.Ring()
for i in 0..<100 { big.add(.init(list: "L", rows: [i], duplicates: [])) }
out["bigCount"] = big.ordered.count
out["bigFirst"] = big.ordered.first!.rows
out["bigLast"] = big.ordered.last!.rows

out["dups"] = CrashLog.duplicates(["a", "b", "a", "c", "b", "a", "d"])
out["noDups"] = CrashLog.duplicates(["a", "b", "c"])
out["bigDups"] = CrashLog.duplicates((0..<200_000).map { "p\($0 % 199_999)" })

let names = ["crash-20261101T000000.000Z-a.json", "crash-20261103T000000.000Z-a.json", "crash-20261102T000000.000Z-a.json",
             "duplicate-20261101T000000.000Z-a.json", "crash-notes.txt"]
out["prune"] = CrashLog.prune(names, prefix: "crash-", keep: 2)
out["pruneNone"] = CrashLog.prune(names, prefix: "duplicate-", keep: 5)

// 写 8 份，只留最新 5 份；另一种记录不受影响
let w = dir.appending(path: "w")
CrashLog.write(["kind": "other"], prefix: "duplicate-", in: w, now: Date(timeIntervalSince1970: 0))
for i in 0..<8 { CrashLog.write(["n": i], prefix: "crash-", in: w, now: Date(timeIntervalSince1970: Double(i) * 60)) }
let left = try! FileManager.default.contentsOfDirectory(atPath: w.path).sorted()
out["left"] = left.map { String($0.prefix(6)) }
out["leftN"] = left.filter { $0.hasPrefix("crash-") }.map { name -> Int in
    let d = try! Data(contentsOf: w.appending(path: name))
    return (try! JSONSerialization.jsonObject(with: d) as! [String: Any])["n"] as! Int
}

// 没有重复 id 的列表不写文件
CrashLog.folderOverride = dir.appending(path: "diag")
CrashLog.list("任务页", [["a", "b"], ["c"]])
print(String(data: try! JSONSerialization.data(withJSONObject: out, options: [.sortedKeys]), encoding: .utf8)!)
fflush(stdout)

if args.count > 2 && args[2] == "crash" {
    // 先装一个「原来的」处理函数，看崩溃记录写完后有没有接着调它
    NSSetUncaughtExceptionHandler { _ in FileHandle.standardError.write("PREVIOUS-HANDLER\n".data(using: .utf8)!) }
    CrashLog.install()
    CrashLog.list("最新", [["x", "y"], ["z"]])
    CrashLog.list("复盘", [["r1", "r1", "r2"]])
    CrashLog.list("复盘", [["r1", "r2"]])
    CrashLog.list("复盘", [["r1", "r1", "r2"]])   // 同一个列表同一组重复 id：只写一次
    Thread.sleep(forTimeInterval: 0.5)   // 重复 id 的记录在后台写
    NSException(name: .internalInconsistencyException, reason: "Invalid update: invalid number of rows in section 1", userInfo: nil).raise()
}
'''


@pytest.fixture(scope="module")
def probe(tmp_path_factory):
    if not shutil.which("swiftc"):
        pytest.skip("没有 swiftc")
    d = tmp_path_factory.mktemp("crashlog")
    main = d / "main.swift"
    main.write_text(MAIN, encoding="utf-8")
    exe = d / "probe"
    r = subprocess.run(["swiftc", "-o", str(exe), SRC, str(main)], capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr
    return exe


def test_环形记录_查重_留几份(probe, tmp_path):
    run = subprocess.run([str(probe), str(tmp_path)], capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    got = json.loads(run.stdout)
    assert got["ring"] == ["L2", "L3", "L4"]
    assert got["added"] == [True, False, True, True, True]
    assert got["bigCount"] == 60 and got["bigFirst"] == [40] and got["bigLast"] == [99]
    assert got["dups"] == ["a", "b"]
    assert got["noDups"] == []
    assert got["bigDups"] == ["p0"]
    assert got["prune"] == ["crash-20261101T000000.000Z-a.json"]
    assert got["pruneNone"] == []
    assert got["left"] == ["crash-"] * 5 + ["duplic"]
    assert got["leftN"] == [3, 4, 5, 6, 7], "留下的是最新的 5 份"
    assert not (tmp_path / "diag").exists(), "没有重复 id 不写文件"


def test_没人接的异常写成记录_再交给原来的处理函数(probe, tmp_path):
    run = subprocess.run([str(probe), str(tmp_path), "crash"], capture_output=True, text=True, timeout=60)
    assert run.returncode != 0, "异常没人接，进程要照样退出"
    assert "PREVIOUS-HANDLER" in run.stderr, "原来的处理函数要接着调"
    diag = tmp_path / "diag"
    crashes = sorted(diag.glob("crash-*.json"))
    assert len(crashes) == 1
    rec = json.loads(crashes[0].read_text(encoding="utf-8"))
    assert rec["name"] == "NSInternalInconsistencyException"
    assert "invalid number of rows" in rec["reason"]
    assert rec["stack"] and isinstance(rec["stack"], list)
    assert rec["version"] and rec["at"]
    assert [x["list"] for x in rec["lists"]] == ["任务页", "最新", "复盘", "复盘", "复盘"]
    assert rec["lists"][1]["rows"] == [2, 1] and rec["lists"][1]["sections"] == 2
    assert rec["lists"][2]["duplicates"] == ["r1"] and rec["lists"][3]["duplicates"] == []
    dups = sorted(diag.glob("duplicate-*.json"))
    assert len(dups) == 1, "重复 id 没崩也要留一份记录"
    d = json.loads(dups[0].read_text(encoding="utf-8"))
    assert d["shape"]["list"] == "复盘" and d["shape"]["duplicates"] == ["r1"]
