#!/bin/bash
# 双击启动 Spark：拉起两个 app，并打开落地页选任务。
# 关闭这个终端窗口（或按 Ctrl+C）会把它们一起停掉。
#
# caffeinate -i：批量处理和长报告都很耗时，避免电脑闲置休眠让任务卡住。

cd "$(dirname "$0")"
echo "正在准备依赖……"
pip3 install --user -q -r requirements.txt 2>/dev/null || true
caffeinate -i python3 spark.py
