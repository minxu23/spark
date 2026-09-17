#!/bin/bash
set -e
cd "$(dirname "$0")"
pip3 install --user -q -r requirements.txt
# caffeinate -i：生成长报告耗时较久，避免电脑闲置休眠导致任务卡住
caffeinate -i python3 launch.py
