"""
启动入口：挑一个能用的端口，并且不跟已经在跑的实例打架。

行为：
  1. 从默认端口开始往后探 20 个端口
  2. 某个端口上已经跑着 Notes2Insight → 不再重复启动，直接打开浏览器
  3. 端口被别的程序占了 → 自动换下一个空闲端口
  4. 全被占满 → 给出明确提示而不是抛一串堆栈
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import urllib.error
import urllib.request
import webbrowser

BASE_PORT = int(os.environ.get("NOTES2INSIGHT_PORT", "8766"))
SCAN = 20


def _port_free(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) != 0


def _is_ours(port: int) -> bool:
    """端口上跑的是不是 Notes2Insight 自己。"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/env", timeout=1.5) as r:
            data = json.load(r)
        return isinstance(data, dict) and "default_vault" in data and "depths" in data
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return False


def pick() -> tuple[str, int]:
    """返回 ("reuse"|"start"|"none", port)。"""
    for port in range(BASE_PORT, BASE_PORT + SCAN):
        if _port_free(port):
            return "start", port
        if _is_ours(port):
            return "reuse", port
    return "none", 0


def main() -> int:
    action, port = pick()
    url = f"http://127.0.0.1:{port}"

    if action == "none":
        print(f"端口 {BASE_PORT}-{BASE_PORT + SCAN - 1} 都被占用了。")
        print("换个端口重试：NOTES2INSIGHT_PORT=9000 python3 launch.py")
        return 1

    if action == "reuse":
        print(f"Notes2Insight 已经在运行：{url}")
        print("（没有重复启动，直接打开浏览器。要关掉它，在原来那个窗口按 Ctrl+C。）")
        webbrowser.open(url)
        return 0

    if port != BASE_PORT:
        print(f"端口 {BASE_PORT} 被别的程序占用，改用 {port}。")

    os.environ["NOTES2INSIGHT_PORT"] = str(port)
    import server  # 必须在设好环境变量之后再导入，server 在导入时读取端口

    threading.Timer(2.0, lambda: webbrowser.open(url)).start()
    try:
        server.main()
    except KeyboardInterrupt:
        print("\n已停止。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
