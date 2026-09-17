"""
CA 证书。

macOS 官方版 Python 如果没跑过 Install Certificates.command，系统里就没有可用的 CA，
https 请求会直接 CERTIFICATE_VERIFY_FAILED。`core/llm.py` 用 per-context 的方式解决
自己那几个调用，但 `yt_dlp` 是库调用、跑在同一个进程里、走默认上下文，够不着那个
context——只能靠这两个环境变量。

这原本是 `apps/summit2md/pipeline.py` 顶部的一个 import 副作用。两个 app 合进同一个
进程之后，那个副作用会一并波及 notes2insight，所以挪到这里变成一次**显式调用**。
实际影响是良性的（指向 certifi 的 CA 包，正是 notes2insight 缺 CA 时也会退回的同一
份），但它必须是看得见的，而不是藏在某个 import 里。
"""

from __future__ import annotations

import os


def ensure_ca_env() -> None:
    """让同进程里走默认 SSL 上下文的库（yt_dlp 等）也能找到 CA。

    用 setdefault：调用方自己配过就不覆盖。
    """
    try:
        import certifi
    except ImportError:
        return  # 没装就维持默认行为，报错信息本身已经足够指明原因
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())
