"""Serve the Campaign Uptime Monitor locally, on the SAME three routes as production.

    python clients/client_S7000/preview_local.py          # http://localhost:8150
    python clients/client_S7000/preview_local.py 8151     # a different port

Routes mirror the brief's architecture exactly, which is the point: reviewing locally reviews
the real thing.

    /                 an index linking the three scopes
    /internal/        both accounts        -> /internal/data.json
    /into/            INTO only            -> /into/data.json
    /service7000/     Service 7000 only    -> /service7000/data.json

`dashboard.html` fetches `data.json` RELATIVE to its own path, so one file serves all three routes
with no query strings and no client-side scope logic. The isolation stays server-side, here as in
Cloud Run.

Build the payloads first (or let the Sync button do it):
    python clients/client_S7000/job/build_local.py
    python clients/client_S7000/job/build_local.py --stale     # prove rule 10 renders correctly
"""
import http.server
import json
import os
import socketserver
import subprocess
import sys
import time
import webbrowser

_HERE = os.path.dirname(os.path.abspath(__file__))
HTML = os.path.join(_HERE, "dash", "dashboard.html")
DATA_DIR = os.path.join(_HERE, "data")
BUILDER = os.path.join(_HERE, "job", "build_local.py")
SCOPES = ("internal", "into", "service7000")
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8150

# The local Sync button really rebuilds. Cooldown keeps a click-happy reviewer from spawning a
# build every second; in production the same cooldown protects PAID Windsor calls.
REFRESH_COOLDOWN = 20
_last_build = [0.0]

# NB: this template is filled with str.replace, not %-formatting: the inline CSS is full of
# literal `%` (max-width:100%) which %-formatting reads as format specifiers and rejects.
INDEX = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Campaign Uptime Monitor, local preview</title>
<style>
 body{font-family:system-ui,-apple-system,'Segoe UI',sans-serif;background:#F6F8FA;color:#12171D;
      margin:0;display:flex;align-items:center;justify-content:center;min-height:100vh;padding:24px}
 .w{max-width:660px;width:100%}
 h1{font-size:23px;margin:0 0 6px;letter-spacing:-.02em}
 p{color:#5B6672;font-size:14px;margin:0 0 22px;line-height:1.55}
 a.card{display:flex;align-items:center;gap:14px;background:#fff;border:1px solid #E6EAEF;
   border-radius:14px;padding:16px 18px;margin-bottom:11px;text-decoration:none;color:inherit;
   box-shadow:0 1px 2px rgba(16,24,40,.05);transition:.15s}
 a.card:hover{box-shadow:0 12px 30px -12px rgba(16,24,40,.25);transform:translateY(-1px)}
 .sw{width:42px;height:42px;border-radius:10px;flex:none}
 .n{font-weight:700;font-size:15px}
 .d{font-size:12.5px;color:#7C8794;margin-top:2px}
 .m{font-family:ui-monospace,Menlo,monospace;font-size:11.5px;color:#7C8794;margin-left:auto}
 .note{font-size:12px;color:#7C8794;margin-top:18px;line-height:1.6}
 code{background:#EDF1F5;border-radius:4px;padding:1px 5px;font-size:11.5px}
</style></head><body><div class="w">
<h1>Campaign Uptime Monitor</h1>
<p>Local preview. Three scoped routes off one pipeline, exactly as the build ships. Each
route can only reach its own payload.</p>
__CARDS__
<div class="note">Rebuild the data with
<code>python clients/client_S7000/job/build_local.py</code>, or add <code>--stale</code> to fake a
broken Windsor pull and confirm the dashboard reports a <b>pipeline</b> failure rather than 14 dead
campaigns. The Sync button on each route rebuilds for you.</div>
</div></body></html>
"""

CARDS = {
    "internal": ("Internal", "Both accounts · Agora palette", "#4FA84A"),
    "into": ("INTO Schüleraustausch", "INTO only · teal brand", "#0A6B63"),
    "service7000": ("Service 7000 AG", "Service 7000 only · blue brand", "#0A4EA3"),
}


def _index_html():
    rows = []
    for s in SCOPES:
        name, desc, colour = CARDS[s]
        path = os.path.join(DATA_DIR, "%s.json" % s)
        if os.path.exists(path):
            with open(path, "rb") as fh:
                n = len(json.loads(fh.read().decode("utf-8")).get("campaigns", []))
            meta = "%d campaigns" % n
        else:
            meta = "not built"
        rows.append(
            '<a class="card" href="/%s/"><span class="sw" style="background:%s"></span>'
            '<span><span class="n">%s</span><span class="d">%s</span></span>'
            '<span class="m">%s</span></a>' % (s, colour, name, desc, meta)
        )
    return INDEX.replace("__CARDS__", "\n".join(rows)).encode("utf-8")


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "S7000Preview/1.0"

    def _send(self, body, ctype, status=200):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path, ctype):
        try:
            with open(path, "rb") as fh:
                body = fh.read()
        except OSError:
            self._send(b"not built yet - run job/build_local.py", "text/plain; charset=utf-8", 404)
            return
        self._send(body, ctype)

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler's naming
        route = self.path.split("?")[0]
        if route in ("/", "/index.html"):
            self._send(_index_html(), "text/html; charset=utf-8")
            return
        parts = [p for p in route.split("/") if p]
        if parts and parts[0] in SCOPES:
            scope = parts[0]
            rest = parts[1] if len(parts) > 1 else ""
            if rest in ("", "index.html", "dashboard.html"):
                self._file(HTML, "text/html; charset=utf-8")
                return
            if rest == "data.json":
                self._file(os.path.join(DATA_DIR, "%s.json" % scope), "application/json")
                return
        if route == "/healthz":
            self._send(b"ok", "text/plain")
            return
        self._send(b"not found", "text/plain", 404)

    def do_POST(self):  # noqa: N802
        route = self.path.split("?")[0]
        parts = [p for p in route.split("/") if p]
        if len(parts) == 2 and parts[0] in SCOPES and parts[1] == "refresh":
            now = time.time()
            if now - _last_build[0] < REFRESH_COOLDOWN:
                self._send(json.dumps({"ok": False, "reason": "rebuilt moments ago"}).encode(),
                           "application/json")
                return
            _last_build[0] = now
            env = dict(os.environ)
            env["PYTHONUTF8"] = "1"
            try:
                r = subprocess.run([sys.executable, BUILDER], capture_output=True, timeout=120,
                                   env=env)
                ok = r.returncode == 0
                out = {"ok": ok}
                if not ok:
                    out["reason"] = r.stderr.decode("utf-8", "replace")[-300:]
            except Exception as e:  # noqa: BLE001 - Sync must degrade to a reload, never 500
                out = {"ok": False, "reason": str(e)[:200]}
            self._send(json.dumps(out).encode(), "application/json")
            return
        self._send(b'{"error":"not found"}', "application/json", 404)

    def log_message(self, fmt, *args):
        msg = fmt % args
        if " 200 " not in msg:
            sys.stderr.write("  %s\n" % msg)
            sys.stderr.flush()


class Server(socketserver.ThreadingTCPServer):
    """Threaded on purpose.

    A single-threaded TCPServer serves one request at a time, so ONE browser holding a connection
    open wedges the whole server: the port still shows LISTENING and new connections still get
    ESTABLISHED, they just never receive a byte. That looks exactly like a crash.
    """

    allow_reuse_address = True
    daemon_threads = True


def main():
    missing = [s for s in SCOPES if not os.path.exists(os.path.join(DATA_DIR, "%s.json" % s))]
    if missing:
        print("[i] building missing payloads: %s" % ", ".join(missing), flush=True)
        env = dict(os.environ)
        env["PYTHONUTF8"] = "1"
        subprocess.run([sys.executable, BUILDER], env=env)
    with Server(("127.0.0.1", PORT), Handler) as httpd:
        url = "http://localhost:%d/" % PORT
        # flush: stdout is a pipe when this runs in the background, so buffered output makes a
        # healthy server look silent.
        print("Campaign Uptime Monitor  ->  %s" % url, flush=True)
        for s in SCOPES:
            print("   %-13s %s%s/" % (s, url, s), flush=True)
        print("   Ctrl+C to stop.", flush=True)
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001 - a headless box just prints the URL
            pass
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
