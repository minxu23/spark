"""手机写整理稿（apps/mobile-ios/Spark/SpeechScript.swift）跟电脑用的提示词、正则要一字不差：
Swift 那边的文字是从 pipeline.py 生成过去的，这里防止以后只改了一边。"""

import os
import re

from apps.summit2md import pipeline

SWIFT = os.path.join(os.path.dirname(__file__), "..", "apps", "mobile-ios", "Spark", "SpeechScript.swift")


def _source() -> str:
    with open(SWIFT, encoding="utf-8") as f:
        return f.read()


def _prompt(name: str) -> str:
    m = re.search(rf'static let {name} = #"""\n(.*?)\n"""#', _source(), re.S)
    assert m, name
    return m.group(1)


def _line(name: str) -> str:
    m = re.search(rf'static let {name} = #"(.*)"#\n', _source())
    assert m, name
    return m.group(1)


def test_提示词一字不差():
    assert _prompt("tidyMono") == pipeline.SPEECH_SCRIPT_PROMPT_MONO
    assert _prompt("tidyZh") == pipeline.SPEECH_SCRIPT_PROMPT_ZH
    assert _prompt("translate") == pipeline.SPEECH_SCRIPT_TRANSLATE_PROMPT
    assert _prompt("englishToZh") == pipeline.SPEECH_ENGLISH_TO_ZH_PROMPT
    assert _line("refusalReminder") == pipeline.SPEECH_REFUSAL_REMINDER
    assert _line("keepLanguageReminder") == pipeline._check_kept_language("hello there", "你好你好你好")


def test_正则一字不差():
    for name, pattern in (("edgeNotePattern", pipeline._CHUNK_EDGE_NOTE), ("englishSentencePattern", pipeline._ENGLISH_SENTENCE_RE),
                          ("englishWordPattern", pipeline._ENGLISH_WORD_RE), ("linkTargetPattern", pipeline._LINK_TARGET_RE),
                          ("labelColonPattern", pipeline._ZH_LABEL_COLON_RE), ("halfPunctPattern", pipeline._ZH_HALF_PUNCT_RE),
                          ("speakerLabelPattern", pipeline._SPEAKER_LABEL_RE)):
        assert _line(name) == pattern.pattern, name
    swift = re.findall(r'^\s+\(#"(.*)"#, (true|false)\),$', _source(), re.M)
    assert [(p, f == "true") for p, f in swift] == [(r.pattern, bool(r.flags & re.I)) for r in pipeline._REFUSAL_RES]


def test_分块和重试的大小一致():
    src = _source()
    assert f"chunkOriginal = {pipeline.SPEECH_CHUNK_CHARS['original']}, chunkZhFromZh = {pipeline.SPEECH_CHUNK_CHARS['zh_from_zh']}" in src
    assert f"translateBatch = {pipeline.TRANSLATE_BATCH_CHARS}" in src
    assert f"retryBatches = {list(pipeline.TRANSLATE_RETRY_BATCH_CHARS)}" in src
    assert f"parallel = {pipeline.SPEECH_PARALLEL}" in src
