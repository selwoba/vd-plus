#!/usr/bin/env python3
"""
VD Plus - local server (browser UI + REST API).

The UI and the AI tools (ripper_mcp.py) both talk to this server, so every
download shows up in one queue/history no matter who started it.
Binds to 127.0.0.1 only.

Usage:
    python3 ripper_server.py [--port 8765] [--no-browser]
"""

import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import signal
import socketserver
import subprocess
import sys
import threading
import webbrowser
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import ripper_core as core

HERE = Path(__file__).resolve().parent
UI_FILE = HERE / "ui" / "index.html"
DEFAULT_PORT = 8765

MAX_BODY = 1 << 20

queue = None
# Per-launch secret. Written to a 0600 file so only this macOS user (the app, the
# MCP bridge) can read it; every API call must present it. Blocks other local
# accounts/processes from driving the downloader or reading history.
TOKEN = secrets.token_urlsafe(32)


def write_token():
    fd = os.open(core.TOKEN_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(TOKEN)
    os.chmod(core.TOKEN_PATH, 0o600)


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self):
        # HTTPServer.server_bind() does a reverse DNS lookup of the bind address (socket.getfqdn).
        # On a slow or offline network that blocks for 20+ seconds and the app gives up on the engine.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


def quit_server(code=0):
    """Stop every download first, so nothing keeps running after the app is gone."""
    try:
        if queue:
            queue.shutdown()
    finally:
        os._exit(code)


def split_urls(text):
    return [u for u in re.split(r"[\s,]+", text or "") if re.match(r"^https?://", u, re.I)]


class Handler(BaseHTTPRequestHandler):
    server_version = "VDPlus/3.0"

    def log_message(self, *a):
        pass

    # Only answer requests addressed to this machine from our own page or local
    # tools (blocks DNS-rebinding and cross-site requests from other tabs).
    def _trusted(self):
        port = self.server.server_address[1]
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if self.headers.get("Host") not in allowed:
            return False
        origin = self.headers.get("Origin")
        return origin is None or origin.replace("http://", "") in allowed

    def _authed(self):
        hdr = self.headers.get("X-Ripper-Token", "")
        ck = SimpleCookie(self.headers.get("Cookie", "")).get("rt")
        return any(hmac.compare_digest(t, TOKEN) for t in (hdr, ck.value if ck else "") if t)

    def _send(self, code, body, ctype="application/json", headers=None):
        data = body if isinstance(body, bytes) else json.dumps(body, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        headers = dict(headers or {})
        self.send_header("Cache-Control", headers.pop("Cache-Control", "no-store"))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self._trusted():
            return self._send(403, {"error": "forbidden"})
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        p = u.path
        if p == "/api/ping":
            # Lets clients prove they reached the real VD Plus (not something squatting
            # on the port) without revealing the token: HMAC(token, client nonce).
            nonce = q.get("nonce", "")[:128]
            proof = hmac.new(TOKEN.encode(), nonce.encode(), hashlib.sha256).hexdigest()
            return self._send(200, {"app": "video-ripper", "proof": proof, "build": core.BUILD_ID})
        if p in ("/", "/index.html"):
            t = q.get("token", "")
            if t and hmac.compare_digest(t, TOKEN):
                # Swap the URL token for an HttpOnly cookie, then drop it from the URL.
                return self._send(302, b"", "text/plain", {
                    "Location": "/", "Set-Cookie": f"rt={TOKEN}; HttpOnly; SameSite=Strict; Path=/"})
            if not self._authed():
                return self._send(401, b"<h3 style='font-family:system-ui'>Open VD Plus from the app.</h3>",
                                  "text/html; charset=utf-8")
            return self._send(200, UI_FILE.read_bytes(), "text/html; charset=utf-8", {
                "Content-Security-Policy": "default-src 'self'; img-src * data: blob:; style-src 'self' 'unsafe-inline'; "
                                           "script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'"})
        if not self._authed():
            return self._send(401, {"error": "unauthorized"})
        if p == "/api/jobs":
            return self._send(200, core.all_jobs(queue, int(q.get("limit", 500))))
        if p == "/api/thumb":
            data, ctype = core.thumb_bytes(q.get("id"))
            if not data or not ctype:
                return self._send(404, {"error": "not found"})
            return self._send(200, data, ctype, {"Cache-Control": "private, max-age=86400"})
        if p == "/api/clipboard":
            txt = subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=3).stdout
            return self._send(200, {"text": txt[:20000]})
        if p == "/api/cookies":
            n = 0
            if core.COOKIE_FILE.exists():
                n = len({l.split("\t")[0].lstrip(".").replace("#HttpOnly_", "")
                         for l in core.COOKIE_FILE.read_text(errors="ignore").splitlines()
                         if l and not l.startswith("# ") and "\t" in l})
            return self._send(200, {"signed_in": core.COOKIE_FILE.exists(), "sites": n})
        if p == "/api/job":
            j = queue.get(q.get("id"))
            return self._send(200 if j else 404, core.public(j) if j and "proc" in j else (j or {"error": "not found"}))
        if p == "/api/wait":
            j = queue.wait(q.get("id"), min(float(q.get("timeout", 60)), 3600))
            return self._send(200 if j else 404, core.public(j) if j else {"error": "not found"})
        if p == "/api/history":
            return self._send(200, core.history(int(q.get("limit", 300)), q.get("q", "")))
        if p == "/api/settings":
            return self._send(200, core.get_settings())
        if p == "/api/subs":
            return self._send(200, core.subs_list())
        if p == "/api/status":
            return self._send(200, {"ytdlp": core.engine_version(), "ffmpeg": bool(core.FFMPEG),
                                    "engine_kind": core.ENGINE["kind"], "engine_outdated": core.engine_outdated(),
                                    "engine_latest": core.engine_state().get("latest", ""), "build": core.BUILD_ID,
                                    "browser_blocked": sorted(core._browser_blocked),
                                    "whisper": bool(core.WHISPER and any(m.exists() for m in core.WHISPER_MODELS)),
                                    "home": str(Path.home()), "python": sys.executable,
                                    "mcp": str(HERE / "ripper_mcp.py")})
        if p == "/api/export":
            data = {"history": [{"url": h["url"], "title": h["title"], "status": h["status"], "files": h["files"]}
                                for h in core.history(100000)],
                    "subscriptions": [{"url": s["url"], "title": s["title"]} for s in core.subs_list()]}
            return self._send(200, data)
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._trusted() or self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            return self._send(403, {"error": "forbidden"})
        if not self._authed():
            return self._send(401, {"error": "unauthorized"})
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            return self._send(413, {"error": "request too large"})
        try:
            b = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(b, dict):
                raise ValueError
        except Exception:
            return self._send(400, {"error": "bad json"})
        try:
            return self._route(self.path, b)
        except (ValueError, KeyError) as e:
            return self._send(400, {"error": str(e)})
        except Exception as e:
            return self._send(500, {"error": str(e)})

    def _route(self, p, b):
        ok = {"ok": True}
        if p == "/api/download":
            urls = b.get("url_list") or split_urls(b.get("urls", ""))
            if not urls:
                return self._send(400, {"error": "Paste at least one http(s) link."})
            src = b.get("source", "ui") if b.get("source") in ("ui", "ai") else "ui"
            ov = core.clean_overrides(b.get("overrides") or {})
            jobs = [queue.add(u, ov, source=src) for u in urls[:500]]
            return self._send(200, [core.public(j) for j in jobs])
        if p == "/api/analyze":
            return self._send(200, core.analyze(b["url"]))
        if p == "/api/search":
            return self._send(200, core.search(b["query"], b.get("limit", 20), b.get("site", "youtube")))
        if p in ("/api/cancel", "/api/pause", "/api/resume"):
            getattr(queue, p.rsplit("/", 1)[1])(b.get("id"))
            return self._send(200, ok)
        if p == "/api/retry":
            j = queue.retry(b.get("id"))
            return self._send(200, core.public(j) if j else {"error": "not found"})
        if p == "/api/clear":
            queue.clear_finished()
            return self._send(200, ok)
        if p == "/api/delete":
            ids = [str(i) for i in (b.get("ids") or [])][:2000]
            if b.get("all_finished"):
                ids = [j["id"] for j in core.all_jobs(queue, 100000)
                       if j["status"] in ("done", "error", "cancelled", "interrupted")]
            return self._send(200, {"removed": len(ids), "trashed": core.delete_jobs(queue, ids, bool(b.get("trash")))})
        if p == "/api/choose_folder":
            r = subprocess.run(["osascript", "-e", 'POSIX path of (choose folder with prompt "Save downloads to:")'],
                               capture_output=True, text=True, timeout=600)
            path = r.stdout.strip()
            if not path:
                return self._send(200, {"cancelled": True})
            return self._send(200, core.save_settings({"out_dir": path}))
        if p == "/api/cookies/clear":
            core.COOKIE_FILE.unlink(missing_ok=True)
            for f in core.COOKIE_TMP.glob("cookies-*.txt") if core.COOKIE_TMP.exists() else []:
                f.unlink(missing_ok=True)
            core.save_settings({"cookies": "", "google_cookies": ""})
            return self._send(200, ok)
        if p == "/api/history/clear":
            with core._db_lock, core.db() as con:
                con.execute("DELETE FROM jobs WHERE status IN ('done','error','cancelled','interrupted')")
            return self._send(200, ok)
        if p == "/api/reveal":
            j = queue.get(b["id"]) if b.get("id") else None
            files = [f for f in (j["files"] if j else []) if Path(f).exists()]
            target = b.get("path") or (files[-1] if files else None)
            if target and Path(target).exists():
                subprocess.run(["open", "-R", target])
            else:
                folder = Path(b.get("folder") or core.get_settings()["out_dir"]).expanduser()
                folder.mkdir(parents=True, exist_ok=True)
                subprocess.run(["open", str(folder)])
            return self._send(200, ok)
        if p == "/api/play":
            j = queue.get(b["id"])
            files = [f for f in (j["files"] if j else []) if Path(f).exists()]
            if files:
                subprocess.run(["open", files[-1]])
            return self._send(200, ok)
        if p == "/api/settings":
            return self._send(200, core.save_settings(b))
        if p == "/api/subs/add":
            sid = core.subs_add(b["url"], b.get("title", ""), b.get("only_new", True), b.get("overrides"))
            if not b.get("only_new", True):
                core.subs_check(queue, sid, force=True)
            return self._send(200, {"id": sid})
        if p == "/api/subs/delete":
            core.subs_delete(b["id"])
            return self._send(200, ok)
        if p == "/api/subs/toggle":
            core.subs_update(b["id"], enabled=1 if b.get("enabled") else 0)
            return self._send(200, ok)
        if p == "/api/subs/check":
            return self._send(200, {"queued": core.subs_check(queue, b.get("id"), force=True)})
        if p == "/api/import":
            n = 0
            data = b.get("data")
            if isinstance(data, dict):
                for s in data.get("subscriptions", []):
                    try:
                        core.subs_add(s["url"], s.get("title", ""), True)
                        n += 1
                    except Exception:
                        pass
                urls = [h["url"] for h in data.get("history", []) if b.get("redownload")]
            else:
                urls = split_urls(b.get("text", ""))
            for u in urls:
                queue.add(u, source="import")
                n += 1
            return self._send(200, {"imported": n})
        if p == "/api/transcript":
            return self._send(200, core.transcript(b["target"], b.get("lang", "en"), b.get("timestamps", True)))
        if p == "/api/metadata":
            return self._send(200, core.metadata(b["url"]))
        if p == "/api/frames":
            return self._send(200, core.extract_frames(b["path"], b.get("count", 12), b.get("every", 0),
                                                       b.get("width", 768)))
        if p == "/api/update":
            if queue.active_count():
                return self._send(200, {"ok": False, "busy": True, "version": core.engine_version(),
                                        "output": "Downloads are running. Update when they finish."})
            return self._send(200, core.update_engine())
        if p == "/api/quit":
            # Used by a newer copy of the app (or the AI bridge) to replace a stale engine.
            n = queue.active_count()
            if n and not b.get("force"):
                return self._send(200, {"ok": False, "active": n})
            self._send(200, ok)
            threading.Timer(0.2, quit_server).start()
            return
        self._send(404, {"error": "not found"})


def main():
    global queue
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    if not core.ENGINE:
        raise SystemExit("yt-dlp not found. Install with: brew install yt-dlp ffmpeg")
    url = f"http://127.0.0.1:{a.port}/"
    try:
        srv = Server(("127.0.0.1", a.port), Handler)
    except OSError:
        # Already running (app double-clicked twice, or started by the AI tools) - just open the UI.
        print(f"VD Plus already running at {url}")
        if not a.no_browser:
            try:
                webbrowser.open(f"{url}?token={core.TOKEN_PATH.read_text().strip()}")
            except OSError:
                webbrowser.open(url)
        return
    write_token()
    core.init_db(mark_interrupted=True)
    core.sweep_login_copies()
    queue = core.Queue()
    core.start_background(queue)
    signal.signal(signal.SIGTERM, lambda *_: quit_server())
    print(f"VD Plus running at {url}  (Ctrl+C to quit)", flush=True)
    if not a.no_browser:
        threading.Timer(0.5, webbrowser.open, args=(f"{url}?token={TOKEN}",)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        quit_server()


if __name__ == "__main__":
    main()
