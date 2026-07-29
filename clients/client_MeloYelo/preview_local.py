"""Serve the MeloYelo dashboard locally, straight off the built JSON — with a LIVE Sync.

No auth, no GCS, no cloud — it mirrors what the deployed `meloyelo-dash` service will serve
(`dashboard.html` at `/`, the private data JSON at `/data.json`). The dashboard's Sync button
POSTs `/refresh`, and here that runs the REAL pull (`job/main.py` — Unleashed + Campaign
Monitor live, CRM/Lark from the freshest context snapshot), so the local preview is living
data, not a frozen file. A run takes a couple of minutes; a cooldown stops repeat clicks from
hammering the client's APIs.

    py clients/client_MeloYelo/preview_local.py          # http://localhost:8146
    py clients/client_MeloYelo/preview_local.py 8155     # a different port

First build (either):  py clients/client_MeloYelo/job/main.py        (live pull)
                       py clients/client_MeloYelo/job/build_local.py (offline, xlsx only)
"""
import http.server
import json as jsonlib
import os
import socketserver
import subprocess
import sys
import threading
import time
import webbrowser

_HERE = os.path.dirname(os.path.abspath(__file__))
HTML = os.path.join(_HERE, "dash", "dashboard.html")
JSON = os.path.join(_HERE, "data", "meloyelo.json")
LIVE_JOB = os.path.join(_HERE, "job", "main.py")
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8146   # 8140 is RHE's preview — stay clear
COOLDOWN = int(os.environ.get("REFRESH_COOLDOWN_SECONDS", "300"))
_REFRESH_LOCK = threading.Lock()


class Handler(http.server.BaseHTTPRequestHandler):
    def _send(self, path, ctype):
        try:
            with open(path, "rb") as fh:
                body = fh.read()
        except OSError:
            self.send_error(404, "not built yet: %s" % os.path.basename(path))
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        route = self.path.split("?")[0]
        if route in ("/", "/index.html", "/dashboard.html"):
            self._send(HTML, "text/html; charset=utf-8")
        elif route in ("/data.json", "/meloyelo.json"):
            self._send(JSON, "application/json")
        else:
            self.send_error(404, "not found")

    def _json(self, obj, code=200):
        body = jsonlib.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        """POST /refresh -> run the LIVE pull (job/main.py), synchronously.

        The dashboard's Sync button shows its spinner for the duration, then refetches
        data.json. Cooldown: if the data file is younger than COOLDOWN seconds, report ok
        without re-pulling (a repeat click must not hammer the client's APIs)."""
        if self.path.split("?")[0] != "/refresh":
            self.send_error(404, "not found")
            return
        try:
            age = time.time() - os.path.getmtime(JSON)
        except OSError:
            age = COOLDOWN + 1
        if age < COOLDOWN:
            self._json({"ok": True, "note": "refreshed %d min ago — still fresh" % (age // 60)})
            return
        if not _REFRESH_LOCK.acquire(blocking=False):
            self._json({"ok": False, "note": "a refresh is already running"})
            return
        try:
            print("  [refresh] running live pull…", flush=True)
            r = subprocess.run([sys.executable, "-X", "utf8", LIVE_JOB],
                               capture_output=True, text=True, timeout=900)
            ok = r.returncode == 0
            print("  [refresh] %s" % ("done" if ok else ("FAILED\n" + (r.stderr or "")[-800:])),
                  flush=True)
            self._json({"ok": ok})
        except Exception as e:  # noqa: BLE001 — report, never crash the server
            print("  [refresh] error: %s" % e, flush=True)
            self._json({"ok": False})
        finally:
            _REFRESH_LOCK.release()

    def log_message(self, fmt, *args):   # keep the console readable
        if "200" not in (fmt % args):
            sys.stderr.write("  %s\n" % (fmt % args))
            sys.stderr.flush()


class Server(socketserver.ThreadingTCPServer):
    """Threaded on purpose — a single-threaded TCPServer wedges permanently the moment one
    browser holds a connection open mid-download of the multi-MB payload (see the Honey Tribe
    preview server for the war story)."""
    allow_reuse_address = True
    daemon_threads = True


def main():
    if not os.path.exists(JSON):
        print("[!] %s is missing — run job/build_local.py first." % JSON, flush=True)
        return 1
    with Server(("127.0.0.1", PORT), Handler) as httpd:
        url = "http://localhost:%d/" % PORT
        print("MeloYelo dashboard  ->  %s" % url, flush=True)
        print("   data: %s (%.1f MB)" % (JSON, os.path.getsize(JSON) / 1048576.0), flush=True)
        print("   Ctrl+C to stop.", flush=True)
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001 — a headless box just prints the URL
            pass
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
