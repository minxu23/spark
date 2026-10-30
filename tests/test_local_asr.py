"""YouTube 没有字幕时的本地语音识别（apps/summit2md/local_asr.py）。不真的跑模型。"""

import json
import os
import subprocess
from unittest import mock

import pytest

from apps.summit2md import local_asr, pipeline
from core.llm import Stopped


def test_上下文只取专有名词_不要链接话题标签和平台名():
    terms = local_asr.context_terms(
        "Opening keynote | AI Now Summit 2026",
        "Mistral's founders discuss data sovereignty with Arthur Mensch. Read more https://mistral.ai/news "
        "#PublicServices #TrustInAI Follow us on LinkedIn @mistralai MistralAIOfficial")
    assert terms == ["AI", "Now", "Summit", "2026", "Mistral", "Arthur", "Mensch"]


def _cues(*texts):
    return [(f"00:00:{i:02d}.000 --> 00:00:{i + 1:02d}.000", t) for i, t in enumerate(texts)]


def test_删掉模型在无人讲话处念出的上下文():
    terms = ["Mistral", "AI", "Arthur", "Mensch", "Le", "Chat"]
    cues = _cues("welcome everyone to the summit",
                 "Mistral AI Arthur Mensch", "Le Chat Mistral AI",    # 连续两条全是上下文词：念提示词
                 "today we launch Le Chat for enterprises",
                 "Mistral AI",                                       # 单独一条：真实讲话也会这么说
                 "thank you")
    kept, dropped = local_asr.drop_context_echo(cues, terms)
    assert dropped == 2
    assert [t for _, t in kept] == ["welcome everyone to the summit", "today we launch Le Chat for enterprises",
                                    "Mistral AI", "thank you"]
    # 没有上下文就什么都不删
    assert local_asr.drop_context_echo(cues, []) == (cues, 0)


def test_VTT读写往返_能被字幕整理直接读(tmp_path):
    cues = _cues("hello there", "general kenobi")
    path = str(tmp_path / "x.vtt")
    local_asr.write_vtt(path, cues)
    with open(path) as f:
        assert local_asr.parse_vtt(f.read()) == cues
    assert [t for _, t in pipeline.vtt_to_paragraphs(path)] == ["hello there general kenobi"]


class _FakeYDL:
    """模拟 pipeline._youtube_dl：往 outtmpl 写一个音频文件，返回视频信息。"""
    def __init__(self, info):
        self.info, self.opts = info, None

    def __call__(self, opts):
        self.opts = opts
        fake = self

        class _Ctx:
            def __enter__(self_inner):
                class _Y:
                    def extract_info(self_y, url, download=False):
                        with open(opts["outtmpl"].replace("%(ext)s", "m4a"), "wb") as f:
                            f.write(b"audio")
                        return fake.info
                return _Y()

            def __exit__(self_inner, *a):
                return False
        return _Ctx()


def _fake_run(language="English", texts=("hello world", "Mistral AI Arthur", "Mensch Mistral AI", "bye now")):
    def run(cmd, stop_flag, timeout):
        if "mlx_qwen3_asr" in cmd:
            out = cmd[cmd.index("-o") + 1]
            os.makedirs(out, exist_ok=True)
            local_asr.write_vtt(os.path.join(out, "audio.vtt"), _cues(*texts))
            with open(os.path.join(out, "audio.json"), "w") as f:
                json.dump({"language": language, "text": " ".join(texts)}, f)
            run.asr_cmd = cmd
        else:   # ffmpeg 转 WAV
            open(cmd[cmd.index("-ar") + 2], "wb").close()
        return subprocess.CompletedProcess(cmd, 0, "", "")
    return run


def test_识别结果存进缓存_重试直接复用(tmp_path):
    ydl = _FakeYDL({"title": "Opening keynote", "description": "Mistral AI founders Arthur Mensch speak",
                    "upload_date": "20260630", "duration": 60})
    run = _fake_run()
    with mock.patch.object(local_asr, "unavailable_reason", return_value=""), \
            mock.patch.object(local_asr, "ffmpeg_path", return_value="ffmpeg"), \
            mock.patch.object(local_asr, "_run", side_effect=run):
        got = local_asr.transcribe("vid1", "https://www.youtube.com/watch?v=vid1", str(tmp_path), youtube_dl=ydl)
    assert got["kind"] == "asr" and got["lang"] == "en" and got["upload_date"] == "20260630"
    assert got["dropped_echo_cues"] == 2
    assert run.asr_cmd[run.asr_cmd.index("--model") + 1] == "Qwen/Qwen3-ASR-1.7B"
    assert run.asr_cmd[run.asr_cmd.index("--context") + 1] == "Mistral AI Arthur Mensch"
    assert [t for _, t in pipeline.vtt_to_paragraphs(got["path"])] == ["hello world bye now"]
    meta = json.load(open(tmp_path / "vid1.asr.json"))
    assert meta["model"] == "Qwen/Qwen3-ASR-1.7B" and meta["description"].startswith("Mistral")
    # 临时音频不留在缓存目录
    assert sorted(os.listdir(tmp_path)) == ["vid1.asr.json", "vid1.asr.vtt"]
    # 再来一次：不下载、不识别
    with mock.patch.object(local_asr, "_run", side_effect=AssertionError("不该再跑")):
        again = local_asr.transcribe("vid1", "u", str(tmp_path), youtube_dl=None)
    assert again["path"] == got["path"] and again["kind"] == "asr"


def test_识别不出内容或工具没装_报清楚原因(tmp_path):
    ydl = _FakeYDL({"title": "t", "description": "", "duration": 60})
    with mock.patch.object(local_asr, "unavailable_reason", return_value=""), \
            mock.patch.object(local_asr, "ffmpeg_path", return_value="ffmpeg"), \
            mock.patch.object(local_asr, "_run", side_effect=_fake_run(texts=("",))):
        with pytest.raises(RuntimeError, match="没有识别出内容"):
            local_asr.transcribe("v2", "u", str(tmp_path), youtube_dl=ydl)
    with mock.patch.object(local_asr, "unavailable_reason", return_value="没有安装 mlx-qwen3-asr"):
        with pytest.raises(RuntimeError, match="mlx-qwen3-asr"):
            local_asr.transcribe("v3", "u", str(tmp_path), youtube_dl=ydl)


def test_停止任务时结束识别子进程():
    with pytest.raises(Stopped):
        local_asr._run(["sleep", "30"], stop_flag=lambda: True, timeout=60)


def test_没有字幕时改用本地识别_其他原因不识别(tmp_path):
    entry = {"id": "abcdefghijk", "title": "Opening keynote"}
    got = {"lang": "en", "path": "p", "description": "", "upload_date": "", "kind": "asr"}
    logs = []
    with mock.patch.object(local_asr, "unavailable_reason", return_value=""), \
            mock.patch.object(local_asr, "transcribe", return_value=got) as tr:
        sub, problem = pipeline.transcribe_without_captions(entry, str(tmp_path),
                                                            report=lambda **kw: logs.append(kw.get("log")))
    assert sub == got and problem == "" and tr.called
    assert "本地语音识别" in logs[0]
    with mock.patch.object(local_asr, "unavailable_reason", return_value="本地语音识别只支持 Apple Silicon 的 Mac"):
        sub, problem = pipeline.transcribe_without_captions(entry, str(tmp_path))
    assert sub is None and "Apple Silicon" in problem


def test_下载音频失败的报错不带终端颜色码(tmp_path):
    def broken(opts):
        raise RuntimeError("\x1b[0;31mERROR:\x1b[0m [youtube] x: This video is unavailable")
    with mock.patch.object(local_asr, "unavailable_reason", return_value=""):
        with pytest.raises(RuntimeError) as ei:
            local_asr.transcribe("v4", "u", str(tmp_path), youtube_dl=broken)
    assert str(ei.value) == "下载音频失败：ERROR: [youtube] x: This video is unavailable"
