import json
import os
import tempfile
import unittest
from unittest import mock

import pipeline


class ProcessJobRegressionTests(unittest.TestCase):
    def _prepare_existing_topic(self, root, summit_title, *, failed_summary=False):
        out_dir = os.path.join(root, pipeline.sanitize_filename(summit_title))
        transcripts_dir = os.path.join(out_dir, "transcripts")
        speech_dir = os.path.join(out_dir, "speech")
        os.makedirs(transcripts_dir, exist_ok=True)
        os.makedirs(speech_dir, exist_ok=True)

        entry = {
            "id": "existing-video",
            "title": "Test Speaker - Existing Talk",
            "url": "https://www.youtube.com/watch?v=existing-video",
            "duration": 60,
            "index": 1,
            "is_raw_session": False,
        }
        transcript_rel = "transcripts/001_Test Speaker - Existing Talk.md"
        speech_rel = "speech/001_Test Speaker - Existing Talk.md"
        summary = (
            {"tldr": "", "body": "_（摘要生成失败：temporary error）_"}
            if failed_summary else
            {"tldr": "Existing summary", "body": "- Existing point"}
        )
        transcript = pipeline.render_transcript_md(
            entry, summit_title, [(0, "Existing transcript paragraph.")], summary, "en",
            include_summary=False, speech_relative_path=speech_rel,
        )
        with open(os.path.join(out_dir, transcript_rel), "w", encoding="utf-8") as f:
            f.write(transcript)
        speech = pipeline.render_speech_md(
            entry, summit_title, "ORIGINAL SPEECH BODY", None, None,
            summary=summary, transcript_relative_path=transcript_rel, speech_lang_mode="original",
        )
        with open(os.path.join(out_dir, speech_rel), "w", encoding="utf-8") as f:
            f.write(speech)

        manifest = {
            "entries": {
                entry["id"]: {
                    "rank": 1,
                    "entry": entry,
                    "ok": True,
                    "error": None,
                    "relative_path": transcript_rel,
                    "speech_relative_path": speech_rel,
                    "summary": summary,
                }
            },
            "overall_summary": "Existing overall summary",
        }
        with open(os.path.join(out_dir, ".manifest.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f)
        return out_dir, entry, speech_rel

    def test_stop_skips_overall_summary_and_reports_unprocessed_entries(self):
        with tempfile.TemporaryDirectory() as root:
            summit_title = "Stop Regression"
            self._prepare_existing_topic(root, summit_title)
            pending = {
                "id": "pending-video",
                "title": "Pending Talk",
                "url": "https://www.youtube.com/watch?v=pending-video",
                "duration": 30,
                "index": 2,
                "is_raw_session": False,
            }
            logs = []
            with mock.patch.object(
                pipeline, "summarize", side_effect=AssertionError("summary must not run after stop")
            ):
                result = pipeline.process_job(
                    summit_title=summit_title,
                    source_url="https://www.youtube.com/playlist?list=test",
                    entries=[pending],
                    output_base_dir=root,
                    backend="cli",
                    api_key="",
                    model="",
                    lang_prefs=["en"],
                    do_summary=True,
                    stop_flag=lambda: True,
                    progress_cb=logs.append,
                )

            self.assertTrue(result["stopped"])
            self.assertEqual([pending], result["unprocessed_entries"])
            self.assertEqual([], result["rows"])
            self.assertEqual("stopped", logs[-1]["stage"])
            self.assertTrue(os.path.exists(result["index_path"]))

    def test_summary_retry_does_not_regenerate_existing_speech(self):
        with tempfile.TemporaryDirectory() as root:
            summit_title = "Backfill Regression"
            out_dir, entry, speech_rel = self._prepare_existing_topic(
                root, summit_title, failed_summary=True
            )

            def fake_summarize(prompt, *_args, **_kwargs):
                if "议题标题" in prompt:
                    return "TLDR: Updated summary\n- Updated point"
                return "Updated overall summary"

            with mock.patch.object(pipeline, "summarize", side_effect=fake_summarize), mock.patch.object(
                pipeline, "generate_speech_script",
                side_effect=AssertionError("existing speech must not be regenerated"),
            ):
                result = pipeline.process_job(
                    summit_title=summit_title,
                    source_url="https://www.youtube.com/playlist?list=test",
                    entries=[entry],
                    output_base_dir=root,
                    backend="cli",
                    api_key="",
                    model="",
                    lang_prefs=["en"],
                    do_summary=True,
                    do_speech_script=True,
                    skip_existing=True,
                )

            self.assertFalse(result["stopped"])
            with open(os.path.join(out_dir, speech_rel), encoding="utf-8") as f:
                speech = f.read()
            self.assertIn("ORIGINAL SPEECH BODY", speech)
            self.assertIn("Updated summary", speech)
            self.assertNotIn("摘要生成失败", speech)


if __name__ == "__main__":
    unittest.main()
