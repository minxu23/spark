"""让 tests/ 里的 `import pipeline` / `import server` 在仓库根目录跑 pytest 时也能找到模块
（原来只有在 apps/summit2md 目录里跑才行）。"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
