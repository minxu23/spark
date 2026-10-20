"""Separate speech outputs: generation, resume and filename maintenance use temporary files only."""
from pathlib import Path
from unittest.mock import patch

import pytest

from apps.summit2md import pipeline as p


ENTRY = {"id": "split-video", "title": "A talk", "url": "https://www.youtube.com/watch?v=split-video", "duration": 60}


@pytest.mark.parametrize("lang,original,translation,expected", [
    ("en", "English one.\n\nEnglish two.", {1: "中文一。", 2: "中文二。"}, "zh"),
    ("zh", "中文一。\n\n中文二。", {1: "English one.", 2: "English two."}, "en"),
])
def test_generate_separate_reuses_original(lang, original, translation, expected):
    with patch.object(p, "_generate_original_language_script", return_value=original) as generate, \
         patch.object(p, "_translate_speech_paragraphs", return_value=translation) as translate:
        body, mode = p.generate_speech_script(ENTRY, [(0, original)], None, None, lang, "separate", "api", "", "model")
    assert mode == "separate"
    assert body[expected] == "\n\n".join(translation.values())
    assert body["en" if lang == "en" else "zh"] == original
    assert generate.call_count == translate.call_count == 1
    assert translate.call_args.kwargs["target_lang"] == expected


def test_incomplete_translation_is_retryable():
    with patch.object(p, "_generate_original_language_script", return_value="One.\n\nTwo."), \
         patch.object(p, "_translate_speech_paragraphs", return_value={1: "一。"}):
        with pytest.raises(p.SummarizeError, match="不完整"):
            p.generate_speech_script(ENTRY, [(0, "One. Two.")], None, None, "en", "separate", "api", "", "model")


@pytest.mark.parametrize("fresh", [False, True])
def test_process_writes_two_files_and_resumes_missing_english(tmp_path, fresh):
    out = tmp_path / "Meeting"
    transcript = "transcripts/001_A talk.md"
    speech = "speech/001_A talk.md"
    if not fresh:
        (out / "transcripts").mkdir(parents=True)
        (out / transcript).write_text(p.render_transcript_md(ENTRY, "Meeting", [(0, "English text.")], None, "en"))
        p._save_manifest(str(out), {"entries": {ENTRY["id"]: {"entry": ENTRY, "rank": 1, "ok": True, "relative_path": transcript}}})
    vtt = tmp_path / "source.vtt"
    vtt.write_text("WEBVTT\n\n00:00:00.000 --> 00:00:02.000\nEnglish text.\n")
    kwargs = dict(summit_title="Meeting", source_url=ENTRY["url"], entries=[ENTRY], output_base_dir=str(tmp_path),
                  backend="api", api_key="", model="model", lang_prefs=["en"], do_summary=False,
                  do_speech_script=True, speech_lang_mode="separate")
    with patch.object(p, "download_subtitle", return_value={"path": str(vtt), "lang": "en", "description": "", "kind": "auto"}) as download, \
         patch.object(p, "generate_speech_script", return_value=({"zh": "中文正文。", "en": "English body."}, "separate")) as generate:
        p.process_job(**kwargs)
        assert download.call_count == int(fresh)
        assert generate.call_count == 1
        manifest = p._load_manifest(str(out))
        row = manifest["entries"][ENTRY["id"]]
        assert row["speech_relative_path"] == speech
        en_rel = row["speech_en_relative_path"]
        assert en_rel == "speech/001_A talk_EN.md"
        assert "中文正文。" in (out / speech).read_text()
        assert "English body." not in (out / speech).read_text()
        assert "English body." in (out / en_rel).read_text()
        assert "中文正文。" not in (out / en_rel).read_text()
        p.process_job(**kwargs)
        assert generate.call_count == 1
        # Simulate an unavailable companion without deleting any files.
        (out / en_rel).rename(tmp_path / "saved_english.md")
        p.process_job(**kwargs)
        assert generate.call_count == 2
        assert download.call_count == int(fresh)
        assert (out / en_rel).exists()


def test_renumber_preserves_english_suffix(tmp_path):
    (tmp_path / "speech").mkdir()
    row = {"rank": 2, "entry": ENTRY, "speech_relative_path": "speech/002_A talk.md", "speech_en_relative_path": "speech/002_A talk_EN.md"}
    for key in ("speech_relative_path", "speech_en_relative_path"):
        (tmp_path / row[key]).write_text("body")
    p._renumber_by_order(str(tmp_path), {"entries": {ENTRY["id"]: row}}, {ENTRY["id"]: 1}, lambda **kw: None)
    assert row["speech_en_relative_path"] == "speech/001_A talk_EN.md"
    assert (tmp_path / row["speech_en_relative_path"]).read_text() == "body"


@pytest.mark.parametrize("target,marker", [("en", "into fluent English"), ("zh", "翻译成中文")])
def test_translation_uses_requested_language(target, marker):
    with patch.object(p, "_cached_summarize", return_value="[1] Translated paragraph") as call:
        assert p._translate_speech_paragraphs(["原文"], "中文", "api", "", "m", "", target_lang=target) == {1: "Translated paragraph"}
    assert marker in call.call_args.args[0]


def test_date_rename_preserves_companion(tmp_path):
    (tmp_path / "speech").mkdir()
    row = {"rank": 1, "ok": True, "entry": dict(ENTRY, publish_date="20261018"),
           "speech_relative_path": "speech/001_A talk.md", "speech_en_relative_path": "speech/001_A talk_EN.md"}
    for key in ("speech_relative_path", "speech_en_relative_path"):
        (tmp_path / row[key]).write_text("body")
    p._save_manifest(str(tmp_path), {"content_type": "series", "entries": {ENTRY["id"]: row}})
    p.rename_series_by_date(str(tmp_path))
    saved = p._load_manifest(str(tmp_path))["entries"][ENTRY["id"]]
    assert saved["speech_en_relative_path"] == "speech/20261018_A talk_EN.md"
    assert saved["speech_relative_path"] == "speech/20261018_A talk.md"
    assert (tmp_path / saved["speech_en_relative_path"]).exists()
