"""Serve the Rooming House Expert dashboard locally, straight off the built JSON.

No auth, no GCS, no cloud — it mirrors what the deployed `RHE-dash` service serves
(`dashboard.html` at `/`, the private data JSON at `/data.json`) so what you review locally is
what ships.

    python clients/client_RHE/preview_local.py          # http://localhost:8140
    python clients/client_RHE/preview_local.py 8145     # a different port

Build/refresh the data first with:  python clients/client_RHE/job/main.py
"""
import http.server
import os
import socketserver
import sys
import webbrowser

_HERE = os.path.dirname(os.path.abspath(__file__))
HTML = os.path.join(_HERE, "dash", "dashboard.html")
JSON = os.path.join(_HERE, "data", "RHE.json")
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8140


NO_OPEN = "--no-open" in sys.argv


class Server(socketserver.ThreadingTCPServer):
    """Threaded on purpose: the payload is ~5 MB, and a single-threaded TCPServer serving it to
    one browser blocks every other request (including the automated browser checks) until the
    download finishes."""
    allow_reuse_address = True
    daemon_threads = True


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass                                    # keep the console readable

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
        elif route in ("/data.json", "/RHE.json"):
            self._send(JSON, "application/json")
        else:
            self.send_error(404, "not found")

def main():
    if not os.path.exists(JSON):
        print("[!] %s is missing — run job/main.py first." % JSON)
        return 1
    with Server(("127.0.0.1", PORT), Handler) as httpd:
        url = "http://localhost:%d/" % PORT
        print("Rooming House Expert dashboard  ->  %s" % url)
        print("   data: %s (%.1f MB)" % (JSON, os.path.getsize(JSON) / 1048576.0))
        print("   Ctrl+C to stop.")
        if not NO_OPEN:
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
