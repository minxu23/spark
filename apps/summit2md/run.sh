#!/bin/bash
set -e
cd "$(dirname "$0")"
pip3 install --user -q -r requirements.txt
# caffeinate -i：批量处理耗时较长，避免电脑闲置休眠导致任务卡住
caffeinate -i python3 server.py
