"""手机推测发言人（apps/mobile-ios/Spark/SpeakerLabels.swift，整理稿那边的改动在 SpeechScript.swift）要跟电脑
（pipeline.infer_speakers / apply_host_name / guess_single_speaker / render_transcript_md）一样：
1. 提示词、正则、规则一字不差（Swift 那边的文字是从 pipeline.py 生成过去的，这里防止以后只改了一边）；
2. 用 swiftc 编出探针，同样的输入（假的模型回答）跑两边，比发给模型的提示词、分批、解析结果、写出来的文字。"""

import json
import os
import re
import shutil
import subprocess
from unittest import mock

import pytest

from apps.summit2md import pipeline, session_types

SPARK = os.path.join(os.path.dirname(__file__), "..", "apps", "mobile-ios", "Spark")
LABELS = os.path.join(SPARK, "SpeakerLabels.swift")
SPEECH_SWIFT = os.path.join(SPARK, "SpeechScript.swift")


def _source() -> str:
    with open(LABELS, encoding="utf-8") as f:
        return f.read()


def _prompt(name: str) -> str:
    m = re.search(rf'static let {name} = #"""\n(.*?)\n"""#', _source(), re.S)
    assert m, name
    return m.group(1)


def _line(name: str) -> str:
    m = re.search(rf'static let {name} = #"(.*)"#\n', _source())
    assert m, name
    return m.group(1)


# ---------------------------------------------------------------- 一字不差

def test_提示词一字不差():
    assert _prompt("labelPrompt") == pipeline.SPEAKER_LABEL_PROMPT
    assert _prompt("hostLabelPrompt") == pipeline.HOST_LABEL_PROMPT
    assert _line("ruleZh") == pipeline._speech_speaker_rule("zh", "{lang_name}")
    assert _line("ruleOriginal") == pipeline._speech_speaker_rule("original", "{lang_name}")


def test_正则和议题类型规则一字不差():
    assert _line("singleNamePattern") == pipeline._SINGLE_SPEAKER_NAME_RE.pattern
    assert _line("hostSplitPattern") == pipeline._HOST_SPLIT_RE.pattern
    assert _line("labelLinePattern") == pipeline._LABEL_LINE_RE.pattern
    swift = re.findall(r'^\s+\("(\w+)", #"(.*)"#\),$', _source(), re.M)
    assert swift == list(session_types.RULES)
    assert re.search(r'multiTypes = \[(.*?)\]', _source()).group(1).replace('"', "").replace(" ", "").split(",") \
        == list(session_types.MULTI_SPEAKER_TYPES)


def test_分批大小一致():
    src = _source()
    assert f"batchChars = {pipeline.SPEAKER_LABEL_BATCH_CHARS}" in src
    assert f"batchParas = {pipeline.SPEAKER_LABEL_BATCH_PARAS}" in src
    assert f'unknown = "未知发言人"' in src


# ---------------------------------------------------------------- 探针：假模型的回答，两边写一样的规则

NAMES = ["Ada Wren", "主持人", "未知发言人", "Bo"]
NAMES_NO_HOST = ["Ada Wren", "Bo", "未知发言人", "Bo"]


def fake_label_reply(prompt: str, kind: str) -> str:
    if "哪个标签是主持人" in prompt:
        return "「Bo」"
    m = re.search(r"下面给的是第 (\d+) 到第 (\d+) 段", prompt)
    if not m:
        return ""
    lo, hi = int(m.group(1)), int(m.group(2))
    normal = "\n".join(f"{n}: {NAMES[n % 4]}" for n in range(lo, hi + 1))
    if kind == "flaky":      # 第一次只答了一段解释，重问才答全
        return normal if "（注意：从" in prompt else "抱歉，我先说明一下。"
    if kind == "renumber":   # 每一批都从 1 重新编号
        return "\n".join(f"{n - lo + 1}: {NAMES[n % 4]}" for n in range(lo, hi + 1))
    if kind == "garbage":
        return "没有编号的回答"
    if kind == "nohost":
        return "\n".join(f"{n}: {NAMES_NO_HOST[n % 4]}" for n in range(lo, hi + 1))
    if kind == "allunknown":
        return "\n".join(f"{n}: 未知发言人" for n in range(lo, hi + 1))
    if kind == "messy":      # 夹着格式不对的行、重复的编号、范围外的编号
        return "\n".join([f"{lo}: Ada Wren", "bad line", f"{lo + 1} - nope", f"  {lo + 2}：  Bo  ", f"{lo + 2}: Cy",
                          f"{hi + 5}: outside", f"{lo + 3}:", "：5", f"{lo + 4}: 主持人（小俊）"])
    return normal


def fake_speech_reply(prompt: str, kind: str) -> str:
    if prompt.startswith("你是专业译者"):
        return "[1] 你好\n\n[2] 好的\n\n[3] 嗯\n\n[4] 哦"
    if kind == "refuse":
        return "I'm sorry, but I can't help with this transcript."
    if prompt.startswith("你是文字编辑兼译者"):
        return "**Ada Wren**: 你好,世界。\n\n**【Bo】**：好的"
    return "**【Ada Wren】**：Hello there.\n\n**发言人：Bo**：Good point."


def paragraphs_of(n: int, size: int = 40):
    return [[0, f"Paragraph {i} " + "x" * size] for i in range(1, n + 1)]


INFER = [
    {"name": "短", "paragraphs": paragraphs_of(12), "title": "Ep 1: Chips", "description": "嘉宾 Ada Wren", "showTitle": "Chip Talk",
     "hostName": "", "kind": "normal"},
    {"name": "按段数分三批", "paragraphs": paragraphs_of(450, 5), "title": "Ep 2", "description": "d" * 4000, "showTitle": "Ep 2 Show Ep 2",
     "hostName": "Ada Wren", "kind": "normal"},
    {"name": "按字数分批", "paragraphs": [[0, "y" * 30000] for _ in range(5)], "title": "Ep 3", "description": "", "showTitle": "S",
     "hostName": "Ada Wren、Bo", "kind": "normal"},
    {"name": "重问", "paragraphs": paragraphs_of(30), "title": "Ep 4", "description": "", "showTitle": "S", "hostName": "", "kind": "flaky"},
    {"name": "后一批从1重新编号", "paragraphs": paragraphs_of(250, 5), "title": "Ep 5", "description": "", "showTitle": "S", "hostName": "",
     "kind": "renumber"},
    {"name": "格式不对的行", "paragraphs": paragraphs_of(5), "title": "Ep 6", "description": "", "showTitle": "S", "hostName": "Ada Wren",
     "kind": "messy"},
    {"name": "完全认不出", "paragraphs": paragraphs_of(5), "title": "Ep 7", "description": "", "showTitle": "S", "hostName": "", "kind": "garbage"},
    {"name": "表情和组合字符", "paragraphs": [[0, "😀 café 🇨🇳 " * 5], [0, "é" * 20]], "title": "Ep 8 😀", "description": "简介 😀" * 2000,
     "showTitle": "S", "hostName": "", "kind": "normal"},
]

# label：电脑上 infer_speakers 之后 apply_host_name 的整条流程
LABEL = [
    {"name": "主持人换成姓名", "paragraphs": paragraphs_of(8), "title": "Ep 1", "description": "", "showTitle": "Chip Talk",
     "hostName": "Ada Wren", "kind": "normal"},
    {"name": "主持人被听错：问模型哪个是主持人", "paragraphs": paragraphs_of(8), "title": "Ep 2", "description": "", "showTitle": "Chip Talk",
     "hostName": "Cara Lee", "kind": "nohost"},
    {"name": "没设主持人", "paragraphs": paragraphs_of(8), "title": "Ep 3", "description": "", "showTitle": "S", "hostName": "", "kind": "normal"},
    {"name": "几位主持人不换", "paragraphs": paragraphs_of(8), "title": "Ep 4", "description": "", "showTitle": "S", "hostName": "Ada Wren、Cara Lee",
     "kind": "normal"},
    {"name": "全是未知", "paragraphs": paragraphs_of(8), "title": "Ep 5", "description": "", "showTitle": "S", "hostName": "", "kind": "allunknown"},
    {"name": "认不出", "paragraphs": paragraphs_of(8), "title": "Ep 6", "description": "", "showTitle": "S", "hostName": "", "kind": "garbage"},
]

TITLES = ["Sam Altman - The Future of AI", "Jensen Huang: Keynote", "Panel: AI in Healthcare", "Fireside Chat with Dario",
          "Episode 12: Chips", "The Daily Interview with X", "[12] Alice Smith – Opening", "Q&A with the team", "Workshop: Building Agents",
          "", "Startup Spotlight: Foo", "A B C D E F - six words", "李飞飞 - 谈 AI", "圆桌：算力", "Lex Fridman: Conversation with Sam",
          "Panel-level packaging - Dr. Jo", "123456 Mia Chen - Talk"]

HOSTS = ["", "Ada Wren", "Ada Wren、Bo, Cy；Ada Wren", " , ", "A/B", "  张三 ， 李四 "]

CHUNKS = [
    {"paragraphs": [[0, "a" * 20], [0, "b" * 20], [0, "c" * 20], [0, "d" * 20], [0, "e" * 20], [0, "f" * 20]],
     "speakers": ["甲", "甲", "乙", "乙", "乙", "未知发言人"], "maxChars": 60},
    {"paragraphs": [[0, "a" * 20], [0, "b" * 20], [0, "c" * 20]], "speakers": ["甲", "甲", "甲"], "maxChars": 30},
    {"paragraphs": [[0, "a" * 20], [0, "b" * 20], [0, "c" * 20]], "speakers": ["甲"], "maxChars": 100},      # 发言人比段落少
    {"paragraphs": [[0, "a" * 20], [0, "b" * 20], [0, "c" * 20]], "speakers": None, "maxChars": 45},
]

INSTR = [{"mode": m, "lang": "英语", "names": n} for m in ("zh", "original") for n in ([], ["Ada Wren"], ["Ada Wren", "Bo", "Cy"])]

NORM = [
    {"names": ["Ada Wren", "Bo"],
     "text": "**【Ada Wren】**：hi\n\n**发言人：Bo**: there\n**发言人 Bo**：x\n\n**[Bo]**: y\n**Bo（嘉宾）**：z\n**Zed**: stays\n"
             "   **Ada Wren**: ok\n**Ada Wren** : spaced\n**发言人A**：stays too\n正文里的 **粗体** 不动\n"},
    {"names": [], "text": "**【Ada Wren】**：hi"},
]

TRANSCRIPT_URL = "https://www.youtube.com/watch?v=abc_DEF-123"
RENDER = [
    {"name": "多人", "mode": "multi", "speakers": ["A", "A", "B", "未知发言人", "A"], "url": TRANSCRIPT_URL, "times": [0, 5, 65, 3700, 3800]},
    {"name": "单人", "mode": "single", "speakers": ["Ada Wren"] * 5, "url": TRANSCRIPT_URL, "times": [0, 5, 65, 3700, 3800]},
    {"name": "没有发言人", "mode": None, "speakers": None, "url": TRANSCRIPT_URL, "times": [0, 5, 65, 3700, 3800]},
    {"name": "没有时间也没有查询串", "mode": "multi", "speakers": ["A", "B", "B", "A", "A"], "url": "https://x.example/ep", "times": [0, 0, 0, 0, 0]},
]

SPEECH = [
    {"name": "多人，外语", "lang": "en", "mode": "multi", "speakers": ["Ada Wren", "Ada Wren", "Bo", "Bo"], "kind": "normal"},
    {"name": "多人，中文", "lang": "zh", "mode": "multi", "speakers": ["Ada Wren", "Bo", "Bo", "Ada Wren"], "kind": "normal"},
    {"name": "单人", "lang": "en", "mode": "single", "speakers": ["Ada Wren"] * 4, "kind": "normal"},
    {"name": "没有发言人跟以前一样", "lang": "en", "mode": None, "speakers": None, "kind": "normal"},
    {"name": "没有发言人，中文", "lang": "zh", "mode": None, "speakers": None, "kind": "normal"},
    {"name": "模型拒绝：拆开重来，后一半补上发言人", "lang": "en", "mode": "multi", "speakers": ["Ada Wren"] * 4, "kind": "refuse"},
]

SCENARIOS = {"infer": INFER, "label": LABEL, "titles": TITLES, "hosts": HOSTS, "chunks": CHUNKS, "instr": INSTR, "norm": NORM,
             "render": RENDER, "speech": SPEECH}

MAIN = r'''
import Foundation

final class Rec: @unchecked Sendable {
    private let lock = NSLock()
    private var items: [String] = []
    func add(_ s: String) { lock.lock(); items.append(s); lock.unlock() }
    var all: [String] { lock.lock(); defer { lock.unlock() }; return items }
}
struct Boom: Error, LocalizedError { var errorDescription: String? { "模型出错" } }

let NAMES = ["Ada Wren", "主持人", "未知发言人", "Bo"]
let NAMES_NO_HOST = ["Ada Wren", "Bo", "未知发言人", "Bo"]

func labelReply(_ prompt: String, _ kind: String) -> String {
    if prompt.contains("哪个标签是主持人") { return "「Bo」" }
    guard let r = prompt.range(of: #"下面给的是第 (\d+) 到第 (\d+) 段"#, options: .regularExpression) else { return "" }
    let nums = prompt[r].split(whereSeparator: { !($0.isASCII && $0.isNumber) }).compactMap { Int($0) }
    let lo = nums[0], hi = nums[1]
    let normal = (lo...hi).map { "\($0): \(NAMES[$0 % 4])" }.joined(separator: "\n")
    switch kind {
    case "flaky": return prompt.contains("（注意：从") ? normal : "抱歉，我先说明一下。"
    case "renumber": return (lo...hi).map { "\($0 - lo + 1): \(NAMES[$0 % 4])" }.joined(separator: "\n")
    case "garbage": return "没有编号的回答"
    case "nohost": return (lo...hi).map { "\($0): \(NAMES_NO_HOST[$0 % 4])" }.joined(separator: "\n")
    case "allunknown": return (lo...hi).map { "\($0): 未知发言人" }.joined(separator: "\n")
    case "messy":
        return ["\(lo): Ada Wren", "bad line", "\(lo + 1) - nope", "  \(lo + 2)：  Bo  ", "\(lo + 2): Cy",
                "\(hi + 5): outside", "\(lo + 3):", "：5", "\(lo + 4): 主持人（小俊）"].joined(separator: "\n")
    default: return normal
    }
}

func speechReply(_ prompt: String, _ kind: String) -> String {
    if prompt.hasPrefix("你是专业译者") { return "[1] 你好\n\n[2] 好的\n\n[3] 嗯\n\n[4] 哦" }
    if kind == "refuse" { return "I'm sorry, but I can't help with this transcript." }
    if prompt.hasPrefix("你是文字编辑兼译者") { return "**Ada Wren**: 你好,世界。\n\n**【Bo】**：好的" }
    return "**【Ada Wren】**：Hello there.\n\n**发言人：Bo**：Good point."
}

func paras(_ a: Any?) -> [(Double, String)] {
    (a as? [[Any]] ?? []).map { (($0[0] as? NSNumber)?.doubleValue ?? 0, $0[1] as? String ?? "") }
}
func asked(_ rec: Rec, _ reply: @escaping @Sendable (String) -> String) -> SpeechScript.Ask {
    { prompt, _, _ in rec.add(prompt); return reply(prompt) }
}
func describe(_ o: SpeakerLabels.Outcome) -> [String: Any] {
    switch o {
    case .labeled(let r): return ["kind": "labeled", "mode": r.mode, "speakers": r.speakers]
    case .skipped: return ["kind": "skipped"]
    case .unknown: return ["kind": "unknown"]
    case .failed(let why): return ["kind": "failed", "why": why]
    }
}

let sc = try! JSONSerialization.jsonObject(with: Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1]))) as! [String: Any]
var out: [String: Any] = [:]

var inferOut: [[String: Any]] = []
for s in sc["infer"] as! [[String: Any]] {
    let rec = Rec(), kind = s["kind"] as! String
    let labels = try await SpeakerLabels.infer(paragraphs: paras(s["paragraphs"]), title: s["title"] as! String, description: s["description"] as! String,
                                               showTitle: s["showTitle"] as! String, hostName: s["hostName"] as! String,
                                               ask: asked(rec) { labelReply($0, kind) })
    inferOut.append(["prompts": rec.all, "labels": Dictionary(uniqueKeysWithValues: labels.map { (String($0.key), $0.value) })])
}
out["infer"] = inferOut

var labelOut: [[String: Any]] = []
for s in sc["label"] as! [[String: Any]] {
    let rec = Rec(), kind = s["kind"] as! String
    let o = try await SpeakerLabels.label(title: s["title"] as! String, paragraphs: paras(s["paragraphs"]), description: s["description"] as! String,
                                          showTitle: s["showTitle"] as! String, hostName: s["hostName"] as! String, dialogue: true,
                                          ask: asked(rec) { labelReply($0, kind) })
    labelOut.append(["outcome": describe(o), "prompts": rec.all])
}
out["label"] = labelOut

out["titles"] = (sc["titles"] as! [String]).map { t -> [String: Any] in
    let single = SpeakerLabels.guessSingleSpeaker(t)
    let plan: String
    switch SpeakerLabels.plan(title: t, dialogue: false) {
    case .single(let n): plan = "single:" + n
    case .multi: plan = "multi"
    case .none: plan = "none"
    }
    var d: [String: Any] = ["kind": SpeakerLabels.sessionKind(t), "plan": plan]
    if let single { d["single"] = single }
    if case .multi = SpeakerLabels.plan(title: t, dialogue: true) { d["dialogue"] = "multi" }
    return d
}

out["hosts"] = (sc["hosts"] as! [String]).map { ["names": SpeakerLabels.parseHostNames($0), "line": SpeakerLabels.hostPromptLine($0)] as [String: Any] }

out["chunks"] = (sc["chunks"] as! [[String: Any]]).map { s in
    SpeechScript.buildChunks(paras(s["paragraphs"]), speakers: s["speakers"] as? [String], maxChars: s["maxChars"] as! Int)
}

out["instr"] = (sc["instr"] as! [[String: Any]]).map {
    SpeakerLabels.instruction(mode: $0["mode"] as! String, langName: $0["lang"] as! String, names: $0["names"] as! [String])
}

out["norm"] = (sc["norm"] as! [[String: Any]]).map { SpeakerLabels.normalize($0["text"] as! String, names: $0["names"] as! [String]) }

out["render"] = (sc["render"] as! [[String: Any]]).map { s -> [String: Any] in
    let times = s["times"] as! [Double]
    let texts = (0..<times.count).map { "第 \($0) 段" }
    let ps = (0..<times.count).map { (times[$0], texts[$0]) }
    var result: SpeakerLabels.Result?
    if let mode = s["mode"] as? String, let sp = s["speakers"] as? [String] { result = SpeakerLabels.Result(mode: mode, speakers: sp) }
    return ["header": SpeakerLabels.headerLine(result) ?? "",
            "body": SpeakerLabels.transcriptBody(paragraphs: ps, result: result, url: s["url"] as! String).joined(separator: "\n")]
}

var speechOut: [[String: Any]] = []
for s in sc["speech"] as! [[String: Any]] {
    let rec = Rec(), kind = s["kind"] as! String
    var result: SpeakerLabels.Result?
    if let mode = s["mode"] as? String, let sp = s["speakers"] as? [String] { result = SpeakerLabels.Result(mode: mode, speakers: sp) }
    let ps = (1...4).map { (0.0, "Line \($0) of the talk.") }
    let r = try await SpeechScript.generate(title: "Ep", paragraphs: ps, lang: s["lang"] as! String, sourceType: nil, speakers: result,
                                            ask: asked(rec) { speechReply($0, kind) })
    speechOut.append(["prompts": rec.all.sorted(), "text": r?.text ?? "", "mode": r?.mode ?? ""])
}
out["speech"] = speechOut

// 只有手机这边有的：出错、全是未知、不是对谈时的处理
var own: [String: Any] = [:]
let ps8 = (1...8).map { (0.0, "Para \($0)") }
let boom: SpeechScript.Ask = { _, _, _ in throw Boom() }
own["throws"] = describe(try await SpeakerLabels.label(title: "Ep", paragraphs: ps8, description: "", showTitle: "S", hostName: "", dialogue: true, ask: boom))
let rec0 = Rec()
own["plainTitleNotDialogue"] = describe(try await SpeakerLabels.label(title: "the future of chips", paragraphs: ps8, description: "", showTitle: "S", hostName: "",
                                                                       dialogue: false, ask: asked(rec0) { labelReply($0, "normal") }))
own["plainTitleCalls"] = rec0.all.count
own["singleTitle"] = describe(try await SpeakerLabels.label(title: "Sam Altman - The Future of AI", paragraphs: ps8, description: "", showTitle: "S", hostName: "",
                                                             dialogue: false, ask: asked(rec0) { labelReply($0, "normal") }))
own["singleCalls"] = rec0.all.count
own["emptyParagraphs"] = describe(try await SpeakerLabels.label(title: "Ep", paragraphs: [], description: "", showTitle: "S", hostName: "", dialogue: true, ask: boom))
let unknownOutcome = SpeakerLabels.Outcome.unknown, failedOutcome = SpeakerLabels.Outcome.failed("x")
own["missed"] = [unknownOutcome.missed, failedOutcome.missed, SpeakerLabels.Outcome.skipped.missed,
                 SpeakerLabels.Outcome.labeled(SpeakerLabels.Result(mode: "multi", speakers: ["A"])).missed]
out["own"] = own

print(String(data: try! JSONSerialization.data(withJSONObject: out, options: [.sortedKeys]), encoding: .utf8)!)
'''


@pytest.fixture(scope="module")
def probe(tmp_path_factory):
    if not shutil.which("swiftc"):
        pytest.skip("没有 swiftc")
    d = tmp_path_factory.mktemp("speaker_probe")
    (d / "main.swift").write_text(MAIN, encoding="utf-8")
    (d / "scenarios.json").write_text(json.dumps(SCENARIOS, ensure_ascii=False), encoding="utf-8")
    exe = d / "probe"
    r = subprocess.run(["swiftc", "-o", str(exe), SPEECH_SWIFT, LABELS, str(d / "main.swift")], capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, r.stderr
    run = subprocess.run([str(exe), str(d / "scenarios.json")], capture_output=True, text=True, timeout=300)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


def _paras(raw):
    return [(float(t), x) for t, x in raw]


def _fake(kind_fn, kind, prompts):
    def ask(prompt, backend, **kw):
        prompts.append(prompt)
        return kind_fn(prompt, kind)
    return ask


def test_推测的提示词_分批_解析和电脑一样(probe):
    for sc, got in zip(INFER, probe["infer"]):
        prompts = []
        with mock.patch.object(pipeline, "_cached_summarize", _fake(fake_label_reply, sc["kind"], prompts)):
            expected = pipeline.infer_speakers(_paras(sc["paragraphs"]), sc["title"], sc["description"], "anthropic", "k", "m",
                                               show_title=sc["showTitle"], host_name=sc["hostName"])
        assert got["prompts"] == prompts, sc["name"]
        assert got["labels"] == {str(n): name for n, name in expected.items()}, sc["name"]


def test_分批的实际个数():
    # 450 段、每批最多 200 段 → 3 批；5 个 3 万字的段落、每批最多 10 万字 → 2 批（一小时的节目一般一批）
    prompts = []
    with mock.patch.object(pipeline, "_cached_summarize", _fake(fake_label_reply, "normal", prompts)):
        pipeline.infer_speakers(_paras(INFER[1]["paragraphs"]), "t", "", "anthropic", "k", "m")
        assert len(prompts) == 3
        prompts.clear()
        pipeline.infer_speakers(_paras(INFER[2]["paragraphs"]), "t", "", "anthropic", "k", "m")
        assert len(prompts) == 2
        prompts.clear()
        one_hour = [(0.0, "z" * 350) for _ in range(150)]    # 一小时英文节目大约 150 段、5 万字
        pipeline.infer_speakers(one_hour, "t", "", "anthropic", "k", "m")
        assert len(prompts) == 1


def test_整条流程_主持人换成姓名_全是未知不写标签(probe):
    for sc, got in zip(LABEL, probe["label"]):
        prompts = []
        with mock.patch.object(pipeline, "_cached_summarize", _fake(fake_label_reply, sc["kind"], prompts)):
            paragraphs = _paras(sc["paragraphs"])
            labels = pipeline.infer_speakers(paragraphs, sc["title"], sc["description"], "anthropic", "k", "m",
                                             show_title=sc["showTitle"], host_name=sc["hostName"])
            speakers = [labels.get(i, "未知发言人") for i in range(1, len(paragraphs) + 1)] if labels else None
            if speakers and sc["hostName"]:
                speakers = pipeline.apply_host_name(speakers, paragraphs, sc["hostName"], sc["showTitle"], "anthropic", "k", "m")
        assert got["prompts"] == prompts, sc["name"]
        if speakers is None:
            assert got["outcome"]["kind"] == "failed", sc["name"]
        elif set(speakers) == {"未知发言人"}:
            assert got["outcome"] == {"kind": "unknown"}, sc["name"]
        else:
            assert got["outcome"] == {"kind": "labeled", "mode": "multi", "speakers": speakers}, sc["name"]


def test_主持人换名的具体结果(probe):
    by = {sc["name"]: got for sc, got in zip(LABEL, probe["label"])}
    # 「主持人」换成设置里的姓名；没有「主持人」标签时问了模型一次
    assert "主持人" not in by["主持人换成姓名"]["outcome"]["speakers"] and "Ada Wren" in by["主持人换成姓名"]["outcome"]["speakers"]
    assert len(by["主持人被听错：问模型哪个是主持人"]["prompts"]) == 2
    assert "Cara Lee" in by["主持人被听错：问模型哪个是主持人"]["outcome"]["speakers"]
    assert "Bo" not in by["主持人被听错：问模型哪个是主持人"]["outcome"]["speakers"]
    assert "主持人" in by["几位主持人不换"]["outcome"]["speakers"]
    assert len(by["没设主持人"]["prompts"]) == 1


def test_单人视频和多人对话的判断和电脑一样(probe):
    for title, got in zip(TITLES, probe["titles"]):
        assert got["kind"] == session_types.from_title(title), title
        single = pipeline.guess_single_speaker(title)
        assert got.get("single") == single, title
        if single:
            expected = "single:" + single
        else:
            expected = "multi" if session_types.is_multi_speaker({"title": title}) else "none"
        assert got["plan"] == expected, title
        # 对谈节目：每集都当多人对话，不看标题（电脑上 session_type_override = discussion）
        assert session_types.is_multi_speaker({"title": title, "session_type_override": "discussion"})
        assert got["dialogue"] == "multi"


def test_主持人姓名的解析和提示行一样(probe):
    for host, got in zip(HOSTS, probe["hosts"]):
        assert got["names"] == pipeline.parse_host_names(host), host
        assert got["line"] == pipeline.host_prompt_line(host), host


def test_整理稿分块带发言人和电脑一样(probe):
    for sc, got in zip(CHUNKS, probe["chunks"]):
        mode = "multi" if sc["speakers"] else None
        assert got == pipeline.build_transcript_chunks(_paras(sc["paragraphs"]), sc["speakers"], mode, sc["maxChars"])


def test_整理稿的发言人规则和标签归一和电脑一样(probe):
    for sc, got in zip(INSTR, probe["instr"]):
        assert got == pipeline._speech_speaker_instruction(sc["mode"], sc["lang"], sc["names"])
    for sc, got in zip(NORM, probe["norm"]):
        assert got == pipeline.normalize_speaker_labels(sc["text"], sc["names"])


def test_写进文字记录的格式和电脑一样(probe):
    for sc, got in zip(RENDER, probe["render"]):
        times = sc["times"]
        paragraphs = [(float(t), f"第 {i} 段") for i, t in enumerate(times)]
        entry = {"title": "T", "url": sc["url"], "duration": 3725, "publish_date": "20261103", "sub_kind": "auto"}
        md = pipeline.render_transcript_md(entry, "节目", paragraphs, None, "en", speakers=sc["speakers"], speaker_mode=sc["mode"],
                                           include_summary=False, content_type="series")
        header = re.findall(r"^- 发言人：.*$", md, re.M)
        assert [got["header"]] == (header or [""]), sc["name"]
        assert md.split("## 文字记录\n\n", 1)[1] == got["body"] + "\n" or md.split("## 文字记录\n\n", 1)[1] == got["body"], sc["name"]
        # 整理稿文件头也是同一行
        speech_md = pipeline.render_speech_md(entry, "节目", "正文", sc["mode"], sc["speakers"], content_type="series")
        assert re.findall(r"^- 发言人：.*$", speech_md, re.M) == header, sc["name"]


def test_整理稿的提示词和结果和电脑一样(probe):
    for sc, got in zip(SPEECH, probe["speech"]):
        prompts = []
        paragraphs = [(0.0, f"Line {i} of the talk.") for i in range(1, 5)]
        with mock.patch.object(pipeline, "_cached_summarize", _fake(fake_speech_reply, sc["kind"], prompts)):
            text, mode = pipeline.generate_speech_script({"title": "Ep"}, paragraphs, sc["speakers"], sc["mode"], sc["lang"],
                                                         "bilingual", "anthropic", "k", "m")
        assert got["prompts"] == sorted(prompts), sc["name"]
        assert got["text"] == text and got["mode"] == mode, sc["name"]
    by = {sc["name"]: got for sc, got in zip(SPEECH, probe["speech"])}
    # 多人：提示词里有发言人规则和名单，文字记录按「[发言人]」分段；单人：写了演讲者
    multi = by["多人，外语"]["prompts"][0] if "[Ada Wren]" in by["多人，外语"]["prompts"][0] else by["多人，外语"]["prompts"][1]
    assert "[Ada Wren]\nLine 1" in multi and "本期发言人只有：Ada Wren、Bo" in multi
    assert any("演讲者：Ada Wren" in p for p in by["单人"]["prompts"])
    assert all("本期发言人只有" not in p and "[Ada Wren]" not in p for p in by["没有发言人跟以前一样"]["prompts"])
    # 标签认得出的统一成名单里的名字
    assert "**Ada Wren**：Hello there." in by["多人，外语"]["text"] and "**Bo**：Good point." in by["多人，外语"]["text"]


def test_手机特有的处理_出错_格式不对_全是未知_不是对谈(probe):
    own = probe["own"]
    # 出错：不抛、不影响这一期，标成没推测出来
    assert own["throws"] == {"kind": "failed", "why": "模型出错"}
    # 不是对谈、标题也不像：不问模型；标题是「人名 - 标题」：直接用名字，不问模型
    assert own["plainTitleNotDialogue"] == {"kind": "skipped"} and own["plainTitleCalls"] == 0
    assert own["singleTitle"]["kind"] == "labeled" and own["singleTitle"]["mode"] == "single"
    assert own["singleTitle"]["speakers"] == ["Sam Altman"] * 8 and own["singleCalls"] == 0
    assert own["emptyParagraphs"] == {"kind": "skipped"}
    # 结果说明里「发言人没推测出来」只算真的试过没成的
    assert own["missed"] == [True, True, False, False]
    by = {sc["name"]: got for sc, got in zip(LABEL, probe["label"])}
    assert by["全是未知"]["outcome"] == {"kind": "unknown"} and by["认不出"]["outcome"]["kind"] == "failed"
