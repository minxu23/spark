"""端到端验证：pipeline.run() 能不能真的把「拖入文件」这批 note 当成普通笔记处理完。

这是架构上的核心主张——不用改 pipeline.py 一行代码，因为 RunConfig.vault_root
本来就可以是任意目录——这条测试就是验证这个主张本身，而不是假设它成立。
LLM 调用层按 test_llm_cache.py 的方式打桩（不花真钱），但上传→抽取→落盘→
digest→framework 解析→compose→assemble→写文件整条链路全部是真实代码路径。
"""

import os
import shutil
import tempfile
import unittest

from apps.notes2insight import pipeline, uploads


FAKE_FRAMEWORK = """## 报告标题
上传文件测试报告

## 副标题
验证拖入文件的端到端链路

### T1. 测试主题
概括：这是一个用于验证端到端链路的测试主题。
"""


def _fake_complete(prompt, backend, **kwargs):
    if "## 报告标题" in prompt:
        return FAKE_FRAMEWORK
    return "（桩返回的正文，不校验具体内容，只校验链路能跑通）"


class UploadPipelineEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.dest = tempfile.mkdtemp(prefix="n2i_e2e_upload_")
        self.output_dir = tempfile.mkdtemp(prefix="n2i_e2e_output_")

    def tearDown(self):
        shutil.rmtree(self.dest, ignore_errors=True)
        shutil.rmtree(self.output_dir, ignore_errors=True)

    def test_上传的文件能走完整条流水线生成报告(self):
        notes, errors = uploads.save_batch(self.dest, [
            ("推理成本笔记.md", "推理成本正在快速下降，这是第一篇材料的核心观点。".encode("utf-8")),
            ("ASIC 路线.txt", "自研 ASIC 是另一条路线，这是第二篇材料的核心观点。".encode("utf-8")),
        ])
        self.assertEqual(errors, [])
        self.assertEqual(len(notes), 2)

        cfg = pipeline.RunConfig(
            vault_root=self.dest,                       # 关键：不是真实笔记库，是这次上传落地的临时目录
            notes=[n["path"] for n in notes],
            focus="",
            depth="brief",
            backend="api",
            output_dir=self.output_dir,
            use_cache=False,
        )

        import unittest.mock as mock
        with mock.patch.object(pipeline.llm, "complete", side_effect=_fake_complete):
            result = pipeline.run(cfg)

        out_path = result["path"]
        self.assertTrue(os.path.isfile(out_path), f"报告文件没有真正写出来：{out_path}")
        with open(out_path, encoding="utf-8") as f:
            content = f.read()

        # 报告标题来自桩返回的框架，证明 digest → framework 解析 → assemble 整条链路
        # 确实读到了上传文件里的内容，不是空跑
        self.assertIn("上传文件测试报告", content)
        self.assertIn("技术洞察报告_上传文件测试报告", os.path.basename(out_path))

        # 来源索引里应该能看到这两篇上传笔记的标题
        self.assertIn("推理成本笔记", content)
        self.assertIn("ASIC 路线", content)
