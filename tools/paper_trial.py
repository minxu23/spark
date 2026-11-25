"""论文的真实试跑：用电脑的流程（core/papers.py）把一个 arXiv 链接、任意链接或本地 PDF 处理成论文 Markdown，
写到指定的输出目录，不写笔记库。不进主流程，也不进 pytest。

    python3 tools/paper_trial.py https://arxiv.org/abs/1706.03762 /tmp/papers-out
    python3 tools/paper_trial.py ~/Downloads/paper.pdf /tmp/papers-out --summarize

--summarize：用设置里的模型写摘要（会联网调模型；不翻译）。不带就只取内容。
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import papers  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="把一篇论文处理成 Markdown，写到输出目录（不写笔记库）")
    ap.add_argument("source", help="arXiv 链接、任意链接或本地 PDF 路径")
    ap.add_argument("out_dir", help="输出目录")
    ap.add_argument("--summarize", action="store_true", help="用设置里的模型写摘要")
    args = ap.parse_args()
    print(papers.capture_to_dir(args.source, args.out_dir, summarize=args.summarize))


if __name__ == "__main__":
    main()
