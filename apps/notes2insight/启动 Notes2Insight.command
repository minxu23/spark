#!/bin/bash
cd "$(dirname "$0")"

echo "正在启动 Notes2Insight…"
if ! command -v python3 >/dev/null 2>&1; then
  echo "未找到 python3，请先安装 Python 3。"
  read -n 1 -s -r -p "按任意键关闭窗口"
  exit 1
fi

pip3 install --user -q -r requirements.txt 2>/dev/null || true

# launch.py 负责选端口、复用已在运行的实例、自动开浏览器
# caffeinate -i：生成长报告耗时较久，避免电脑休眠打断任务
caffeinate -i python3 launch.py
STATUS=$?

echo
if [ $STATUS -eq 0 ]; then
  read -n 1 -s -r -p "按任意键关闭窗口"
else
  read -n 1 -s -r -p "启动失败（见上面的提示），按任意键关闭窗口"
fi
