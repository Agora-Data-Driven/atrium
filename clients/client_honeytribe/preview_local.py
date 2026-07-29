"""Serve the Honey Tribe dashboard locally, straight off the built JSON.

No auth, no GCS, no cloud — it mirrors what the deployed `honeytribe-dash` service serves
(`dashboard.html` at `/`, the private data JSON at `/data.json`) so what you review locally is
what ships.

    python clients/client_honeytribe/preview_local.py          # http://localhost:8090
    python clients/client_honeytribe/preview_local.py 8095     # a different port

Build/refresh the data first with:  python clients/client_honeytribe/job/build_local.py
"""
import http.server
import os
import re
import socketserver
import sys
import webbrowser

_HERE = os.path.dirname(os.path.abspath(__file__))
HTML = os.path.join(_HERE, "dash", "dashboard.html")
JSON = os.path.join(_HERE, "data", "honeytribe.json")
# cached creative images, when the export ran with HONEYTRIBE_CREATIVE_LOCAL_DIR
CREATIVES = os.path.join(_HERE, "data", "creatives")
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8090


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
        elif route in ("/data.json", "/honeytribe.json"):
            self._send(JSON, "application/json")
        elif route.startswith("/creative-img/"):
            cid = route[len("/creative-img/"):]
            if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", cid or ""):
                self._send(os.path.join(CREATIVES, cid), "image/jpeg")
            else:
                self.send_error(404)
        else:
            self.send_error(404, "not found")

    def log_message(self, fmt, *args):   # keep the console readable
        if "200" not in (fmt % args):
            sys.stderr.write("  %s\n" % (fmt % args))
            sys.stderr.flush()


class Server(socketserver.ThreadingTCPServer):
    """Threaded on purpose.

    A single-threaded TCPServer serves exactly one request at a time, so ONE browser holding a
    connection open (or a client that walks away mid-download of the ~5 MB payload) wedges the
    server permanently: the port still shows LISTENING and new connections still get ESTABLISHED,
    they just never receive a byte. That looked exactly like a crash and cost a debugging detour.
    """
    allow_reuse_address = True
    daemon_threads = True       # never let a stray connection keep the process alive


def main():
    if not os.path.exists(JSON):
        print("[!] %s is missing — run job/build_local.py first." % JSON, flush=True)
        return 1
    with Server(("127.0.0.1", PORT), Handler) as httpd:
        url = "http://localhost:%d/" % PORT
        # flush: stdout is a pipe when run in the background, so buffered output makes a healthy
        # server look like a silent/hung one (the same trap as PYTHONUNBUFFERED in the export job).
        print("Honey Tribe dashboard  ->  %s" % url, flush=True)
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
