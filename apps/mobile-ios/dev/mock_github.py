"""
开发用的假 GitHub：在本机 127.0.0.1 上模拟手机客户端用到的那几个 GitHub 接口，
把一个本地文件夹当成仓库内容。模拟器里「连接」页展开「开发用」，API 地址填
http://127.0.0.1:8799，令牌随便填（比如 test-token），就能不连真 GitHub 调界面。

    python3 dev/mock_github.py /path/to/fixture-vault [--port 8799]

fixture-vault 的结构跟 spark-vault 仓库一样（Spark/…、output/…）。Issue 只存在内存里，
重启就没了；/_mock/issues/<n>/status 可以手动改一个任务的状态，模拟电脑端接手。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

OWNER = "minxu23"
SKIP_DIRS = {".cache", "logs", ".git"}


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class Repo:
    def __init__(self, root: str):
        self.root = os.path.realpath(root)
        self.lock = threading.Lock()
        self.issues: dict[int, dict] = {}
        self.comments: dict[int, list[dict]] = {}
        self.next_id = 1
        self.scan()

    def scan(self) -> None:
        self.files: dict[str, tuple[str, bytes]] = {}   # path -> (sha, content)
        self.by_sha: dict[str, bytes] = {}
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for f in filenames:
                if f == ".DS_Store":
                    continue
                full = os.path.join(dirpath, f)
                data = open(full, "rb").read()
                sha = hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()
                rel = os.path.relpath(full, self.root).replace(os.sep, "/")
                self.files[rel] = (sha, data)
                self.by_sha[sha] = data
        self.head = hashlib.sha1("".join(sorted(s for s, _ in self.files.values())).encode()).hexdigest()


class Handler(BaseHTTPRequestHandler):
    repo: Repo

    def log_message(self, fmt, *args):
        print(f"{self.command} {self.path} -> {args[1] if len(args) > 1 else ''}")

    def _send(self, code: int, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _auth(self) -> bool:
        if not (self.headers.get("Authorization") or "").startswith("Bearer "):
            self._send(401, {"message": "Bad credentials"})
            return False
        return True

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def _issue_json(self, i: dict) -> dict:
        return {**i, "labels": [{"name": n} for n in i["labels"]], "user": {"login": OWNER}}

    def do_GET(self):
        if not self._auth():
            return
        u = urlparse(self.path)
        parts = [unquote(p) for p in u.path.strip("/").split("/")]
        r = self.repo
        if parts == ["user"]:
            return self._send(200, {"login": OWNER})
        if len(parts) < 3 or parts[0] != "repos":
            return self._send(404, {"message": "Not Found"})
        rest = parts[3:]
        if rest[:2] == ["commits", "main"]:
            r.scan()
            return self._send(200, {"sha": r.head, "commit": {"committer": {"date": now()}}})
        if rest[:2] == ["git", "trees"]:
            return self._send(200, {"sha": r.head, "truncated": False, "tree": [
                {"path": p, "type": "blob", "sha": s, "size": len(d)} for p, (s, d) in r.files.items()]})
        if rest[:2] == ["git", "blobs"] and len(rest) == 3:
            data = r.by_sha.get(rest[2])
            return self._send(200, data, "application/octet-stream") if data is not None else self._send(404, {"message": "Not Found"})
        if rest == ["issues"]:
            q = parse_qs(u.query)
            state = (q.get("state") or ["open"])[0]
            with r.lock:
                items = [self._issue_json(i) for i in r.issues.values() if state == "all" or i["state"] == state]
            return self._send(200, sorted(items, key=lambda i: i["number"], reverse=True))
        if len(rest) == 3 and rest[0] == "issues" and rest[2] == "comments":
            return self._send(200, r.comments.get(int(rest[1]), []))
        return self._send(404, {"message": "Not Found"})

    def do_POST(self):
        u = urlparse(self.path)
        parts = [unquote(p) for p in u.path.strip("/").split("/")]
        r = self.repo
        if parts[:2] == ["_mock", "issues"] and len(parts) == 4 and parts[3] == "status":
            # 开发用：模拟电脑端写状态 {"state": "running|failed|done", "text": "...", "current": 1, "total": 3}
            b = self._body()
            n = int(parts[2])
            with r.lock:
                issue = r.issues[n]
                issue["labels"] = [l for l in issue["labels"] if l not in ("进行中", "失败")]
                if b.get("state") == "running":
                    issue["labels"].append("进行中")
                elif b.get("state") == "failed":
                    issue["labels"].append("失败")
                elif b.get("state") == "done":
                    issue["state"], issue["closed_at"] = "closed", now()
                meta = {k: v for k, v in b.items() if k != "text"}
                r.comments.setdefault(n, []).append({"id": r.next_id, "updated_at": now(),
                    "body": f"<!-- spark-status {json.dumps(meta, ensure_ascii=False)} -->\n{b.get('text', '')}"})
                r.next_id += 1
            return self._send(200, {"ok": True})
        if not self._auth():
            return
        rest = parts[3:]
        if rest == ["issues"]:
            b = self._body()
            with r.lock:
                n = r.next_id
                r.next_id += 1
                r.issues[n] = {"number": n, "title": b.get("title", ""), "body": b.get("body", ""), "state": "open",
                               "labels": list(b.get("labels") or []), "created_at": now(), "closed_at": None}
                out = self._issue_json(r.issues[n])
            return self._send(201, out)
        return self._send(404, {"message": "Not Found"})

    def do_DELETE(self):
        if not self._auth():
            return
        parts = [unquote(p) for p in urlparse(self.path).path.strip("/").split("/")]
        rest = parts[3:]
        if len(rest) == 4 and rest[0] == "issues" and rest[2] == "labels":
            with self.repo.lock:
                i = self.repo.issues.get(int(rest[1]))
                if i and rest[3] in i["labels"]:
                    i["labels"].remove(rest[3])
            return self._send(200, [])
        return self._send(404, {"message": "Not Found"})

    def do_PATCH(self):
        if not self._auth():
            return
        parts = [unquote(p) for p in urlparse(self.path).path.strip("/").split("/")]
        rest = parts[3:]
        if len(rest) == 2 and rest[0] == "issues":
            b = self._body()
            with self.repo.lock:
                self.repo.issues[int(rest[1])].update({k: v for k, v in b.items() if k in ("state", "title", "body")})
            return self._send(200, self._issue_json(self.repo.issues[int(rest[1])]))
        return self._send(404, {"message": "Not Found"})


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    ap.add_argument("--port", type=int, default=8799)
    args = ap.parse_args()
    Handler.repo = Repo(args.root)
    print(f"假 GitHub：http://127.0.0.1:{args.port}，{len(Handler.repo.files)} 个文件，仓库 {OWNER}/spark-vault")
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
