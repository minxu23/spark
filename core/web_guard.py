"""只接受本机页面发来的请求。

服务只监听 127.0.0.1，但这挡不住两类来自浏览器的攻击：
- 你打开的任何网页都能对 http://127.0.0.1:8760 发一个"盲" POST（no-cors，
  浏览器不做预检），借本机服务读 key 文件、发起模型调用、写笔记库；
- DNS 重绑定：恶意域名解析到 127.0.0.1 后，页面就能以"同源"身份读接口返回。

对策：Host 必须是本机地址（重绑定时 Host 是对方域名）；会改动状态的请求如果
带了 Origin，就必须和当前 Host 同源（浏览器跨站请求一定会带 Origin；curl、
测试客户端不带 Origin，照常放行）。
"""

from __future__ import annotations

from flask import Flask, jsonify, request

_LOCAL_HOSTNAMES = {"127.0.0.1", "localhost", "::1"}
_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _hostname(host: str) -> str:
    host = (host or "").strip().lower()
    if host.startswith("["):
        return host[1:].split("]", 1)[0]
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def rejection():
    """不合规就返回 (响应, 403)，合规返回 None。"""
    host = request.host or ""
    if _hostname(host) not in _LOCAL_HOSTNAMES:
        return jsonify({"error": "只接受本机访问"}), 403
    if request.method in _UNSAFE_METHODS:
        origin = request.headers.get("Origin")
        if origin is not None and origin.rstrip("/").lower() != f"{request.scheme}://{host}".lower():
            return jsonify({"error": "拒绝来自其它网站的请求"}), 403
    return None


def install(app: Flask) -> None:
    app.before_request(rejection)
