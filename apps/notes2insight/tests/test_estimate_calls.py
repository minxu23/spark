"""启动前确认框里的「约 N 次模型调用」：pipeline.estimate_calls() 要跟流水线真实的
调用次数对得上。这里让真实的 pipeline.run 跑一遍（模型打桩、按提示词种类计数），
再跟估算比。笔记正文里不留空行，切块不会落在段落边界上，块数是确定的。"""

import os
import shutil
import tempfile
import unittest
from collections import Counter
from unittest import mock

from apps.notes2insight import pipeline


def _framework(n_clusters):
    parts = ["## 报告标题\n估算测试\n\n## 副标题\n核对调用次数\n\n## 主题簇"]
    for i in range(1, n_clusters + 1):
        parts.append(f"### T{i}. 主题{i}\n- 概括：第 {i} 个主题\n- 相关笔记：[1]\n- 核心张力：无")
    parts.append("## 全局判断\n- J1. 无 —— 依据 [1]")
    return "\n".join(parts)


class EstimateCallsTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="n2i_est_")
        self.out = tempfile.mkdtemp(prefix="n2i_est_out_")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.out, ignore_errors=True)

    def write(self, name, chars):
        with open(os.path.join(self.root, name), "w", encoding="utf-8") as f:
            f.write("字" * chars)
        return name

    def run_counted(self, notes, depth, max_note_chars=0):
        calls = Counter()
        clusters = pipeline.DEPTH_PRESETS[depth]["clusters"]

        def fake(prompt, backend, **kw):
            if "你正在撰写一份技术洞察报告" in prompt:
                calls["compose"] += 1
                return "## 一节\n正文"
            if "请据此设计一份" in prompt:
                calls["framework"] += 1
                return _framework(clusters)
            if "批次要点" in prompt:
                calls["framework"] += 1
                return "批次要点 [1]"
            calls["digest"] += 1          # 分块摘取和合卡都算摘取
            return "卡" * pipeline.EST_CARD_CHARS

        cfg = pipeline.RunConfig(vault_root=self.root, notes=notes, depth=depth, backend="api",
                                 output_dir=self.out, use_cache=False, max_note_chars=max_note_chars)
        with mock.patch.object(pipeline.llm, "complete", side_effect=fake):
            pipeline.run(cfg)
        calls["total"] = sum(calls.values())
        return dict(calls)

    def lengths(self, notes):
        from apps.notes2insight import vault
        return [len(pipeline._strip_noise(vault.read_note(self.root, n))) for n in notes]

    def check(self, notes, depth, max_note_chars=0):
        got = self.run_counted(notes, depth, max_note_chars)
        est = pipeline.estimate_calls(self.lengths(notes), depth, max_note_chars)
        self.assertEqual(est, {"digest": got.get("digest", 0), "framework": got.get("framework", 0),
                               "compose": got.get("compose", 0), "total": got["total"]})
        return est

    def test_短笔记每篇一次_加归纳和成文(self):
        notes = [self.write(f"n{i}.md", 500) for i in range(3)]
        est = self.check(notes, "brief")
        self.assertEqual(est, {"digest": 3, "framework": 1, "compose": 5, "total": 9})

    def test_长笔记分块再合卡(self):
        notes = [self.write("long.md", 70000), self.write("s.md", 300)]
        est = self.check(notes, "standard")
        self.assertEqual(est["digest"], 3 + 1 + 1)

    def test_单篇长度上限会减少分块(self):
        notes = [self.write("long.md", 70000)]
        est = self.check(notes, "brief", max_note_chars=30000)
        self.assertEqual(est["digest"], 2 + 1)

    def test_摘要卡太多时先分批预归并(self):
        notes = [self.write(f"n{i:02d}.md", 200) for i in range(50)]
        est = self.check(notes, "deep")
        self.assertGreater(est["framework"], 1)

    def test_没有材料时是零(self):
        self.assertEqual(pipeline.estimate_calls([], "standard"),
                         {"digest": 0, "framework": 0, "compose": 0, "total": 0})


if __name__ == "__main__":
    unittest.main()


class FrameworkBatchTests(unittest.TestCase):
    def test_预归并分批按实际卡片长度算_批尾不会被省略(self):
        from apps.notes2insight import pipeline
        refs = [pipeline.NoteRef(idx=i, path="很长的路径/" * 20 + f"{i}.md", title="很长的标题" * 30,
                                 date="2026-01-01", chars=1000, digest="要点。" * 300) for i in range(1, 300)]
        batches = pipeline._framework_batches(refs)
        self.assertGreater(len(batches), 1)
        for b in batches:
            self.assertNotIn("省略", pipeline._cards_text(b, budget=pipeline.FRAMEWORK_BATCH_CHARS))
