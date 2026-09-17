#!/bin/bash
# 双击这个文件即可启动 Summit2MD：装依赖、开服务、自动打开浏览器。
# 关闭这个终端窗口（或按 Ctrl+C）即可停止服务。
#
# 用 caffeinate 包住服务进程，防止长时间批量处理时电脑因为锁屏/闲置进入休眠——
# 休眠会让正在跑的任务卡住不动，唤醒后可能需要好几十分钟才能恢复。
# 只阻止"闲置休眠"，不会让屏幕常亮，锁屏本身不受影响。

cd "$(dirname "$0")"

echo "正在准备依赖……"
pip3 install --user -q -r requirements.txt

caffeinate -i python3 server.py &
SERVER_PID=$!
# caffeinate 不会把结束信号转发给它包住的子进程，而且它 spawn 出来的 python3
# 在进程树里不一定显示成 caffeinate 的直接子进程（不能指望 pkill -P 找到它）。
# 这里退出时直接按端口号找到真正在监听 8765 的进程一起结束，比按进程树找更可靠，
# 避免残留占用端口的僵尸进程。
trap 'lsof -ti:8765 2>/dev/null | xargs kill 2>/dev/null; kill $SERVER_PID 2>/dev/null' EXIT

echo "正在启动服务……"
for i in $(seq 1 30); do
  if curl -s -o /dev/null "http://127.0.0.1:8765/"; then
    open "http://127.0.0.1:8765"
    break
  fi
  sleep 0.5
done

wait "$SERVER_PID"
