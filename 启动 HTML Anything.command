#!/bin/bash
# 双击启动 HTML Anything（Spark 阅读页「美化」按钮背后的服务，http://127.0.0.1:3000）。
# 关闭这个终端窗口（或按 Ctrl+C）就停掉它。已经在跑的话只打开网页，不重复启动。
#
# 装在别处的话，用 HTML_ANYTHING_DIR 指过去。

HA_DIR="${HTML_ANYTHING_DIR:-$HOME/Developer/html-anything}"
URL="http://localhost:3000"

if lsof -tiTCP:3000 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "HTML Anything 已经在运行：$URL"
  open "$URL"
  exit 0
fi

if [ ! -d "$HA_DIR" ]; then
  echo "找不到 HTML Anything：$HA_DIR"
  echo "先 git clone https://github.com/nexu-io/html-anything 到这个位置。"
  read -r -p "按回车关闭……"
  exit 1
fi

cd "$HA_DIR" || exit 1
# 等端口起来再开网页；开发服务器首次编译要十来秒
( for _ in $(seq 1 60); do
    sleep 1
    if lsof -tiTCP:3000 -sTCP:LISTEN >/dev/null 2>&1; then open "$URL"; break; fi
  done ) &

echo "正在启动 HTML Anything（$HA_DIR）……"
exec npx pnpm@10.33.2 -F @html-anything/next dev
