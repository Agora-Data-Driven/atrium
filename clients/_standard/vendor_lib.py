r"""Re-vendor the Agora dashboard standard's shared blocks into every dashboard that opts in.

WHY THIS EXISTS
    A client dashboard is ONE self-contained HTML file -- no build step, no external JS, because
    the deploy is a single artifact and the esprima gate parses the inline script. That rules out
    <script src="lib.js">. So the shared helper library is VENDORED: physically copied into every
    dashboard between sentinel comments, the same posture as freshness.py and platform_sso.py.

    "Fix it everywhere or nowhere" only holds if re-vendoring is one command. This is it.

WHAT IT SYNCS
    dash/_lib.js    -> between  // >>> AGORA STANDARD LIB v1 ...      and  // <<< AGORA STANDARD LIB v1
    dash/_shell.css -> between  /* >>> AGORA STANDARD SHELL CSS v1 ... and  /* <<< AGORA STANDARD SHELL CSS v1 */

    A file only receives a block if it CARRIES that block's sentinels. That is the opt-in.

    The CSS block is deliberately opt-in-per-file and is used ONLY by the two reference
    dashboards. Real client dashboards carry a re-branded copy of the stylesheet, because
    identity is supposed to differ per client -- structure is not. Their conformance is checked
    by class vocabulary (check_standard.py), not by byte-identity.

USAGE
    py -3 clients/_standard/vendor_lib.py                 # sync every dashboard that opts in
    py -3 clients/_standard/vendor_lib.py --check          # fail if anything is out of date
    py -3 clients/_standard/vendor_lib.py <path> [...]     # sync just these files

EXIT CODES
    0  everything in sync (or written)
    1  --check found a stale copy, or a sentinel pair is malformed
"""

import glob
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CLIENTS = os.path.dirname(HERE)
REPO = os.path.dirname(CLIENTS)

LIB_SRC = os.path.join(HERE, "dash", "_lib.js")
CSS_SRC = os.path.join(HERE, "dash", "_shell.css")

BLOCKS = (
    {
        "name": "lib",
        "src": LIB_SRC,
        "open": "// >>> AGORA STANDARD LIB v1",
        "close": "// <<< AGORA STANDARD LIB v1",
    },
    {
        "name": "css",
        "src": CSS_SRC,
        "open": "/* >>> AGORA STANDARD SHELL CSS v1",
        "close": "/* <<< AGORA STANDARD SHELL CSS v1 */",
    },
    {
        "name": "conform",
        "src": os.path.join(HERE, "dash", "_conform.css"),
        "open": "/* >>> AGORA STANDARD CONFORM v1",
        "close": "/* <<< AGORA STANDARD CONFORM v1 */",
    },
)


def _read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _write(path, text):
    # Bytes, not text mode: on Windows text mode writes cp1252 and mangles anything non-ASCII
    # (a smart quote becomes 0x92 and the next UTF-8 read blows up).
    with open(path, "wb") as fh:
        fh.write(text.encode("utf-8"))


def splice(html, block, payload):
    """Replace whatever sits between a sentinel pair. Returns (new_html, changed, error).

    Two deliberate choices, both of which are scar tissue from corrupting a dashboard:

    * The payload is REJECTED if it contains either sentinel. A payload that quotes its own
      sentinels makes the splice non-idempotent: run 1 writes them into the file, run 2 finds
      them and cuts there, and the tail of the real block is orphaned outside its comment --
      which showed up only as an esprima failure a thousand lines away.
    * The close sentinel is found with rfind, i.e. the LAST occurrence. First-occurrence
      matching is what turned a quoted sentinel into a corrupted file rather than an error.
    """
    if block["open"] in payload or block["close"] in payload:
        return html, False, (
            "%s payload quotes its own sentinel -- vendoring it would corrupt the file. "
            "Describe the sentinels instead of reproducing them." % block["name"]
        )
    o = html.find(block["open"])
    if o < 0:
        return html, False, None  # file does not opt in to this block
    if html.find(block["open"], o + 1) >= 0:
        return html, False, "opening sentinel for %s appears more than once" % block["name"]
    # The opening sentinel is a whole line; keep the line itself and replace from its newline.
    o_end = html.find("\n", o)
    if o_end < 0:
        return html, False, "opening sentinel for %s has no newline after it" % block["name"]
    c = html.rfind(block["close"])
    if c < o_end:
        return html, False, "opening sentinel for %s has no matching close" % block["name"]
    new = html[: o_end + 1] + payload.rstrip("\n") + "\n" + html[c:]
    return new, new != html, None


def process(path, check_only):
    html = _read(path)
    original = html
    problems = []
    touched = []
    for block in BLOCKS:
        if not os.path.isfile(block["src"]):
            problems.append("missing source %s" % block["src"])
            continue
        payload = _read(block["src"])
        html, changed, err = splice(html, block, payload)
        if err:
            problems.append(err)
        elif changed:
            touched.append(block["name"])
    rel = os.path.relpath(path, REPO)
    if problems:
        for p in problems:
            sys.stderr.write("[FAIL] %s: %s\n" % (rel, p))
        return 1
    if html == original:
        print("[OK]   %s (in sync)" % rel)
        return 0
    if check_only:
        sys.stderr.write(
            "[STALE] %s: vendored %s block(s) differ from source -- run vendor_lib.py\n"
            % (rel, "+".join(touched))
        )
        return 1
    _write(path, html)
    print("[WROTE] %s (%s)" % (rel, "+".join(touched)))
    return 0


def targets(argv):
    if argv:
        return [os.path.abspath(a) for a in argv]
    found = sorted(glob.glob(os.path.join(HERE, "dash", "dashboard-*.html")))
    found += sorted(glob.glob(os.path.join(CLIENTS, "*", "dash", "dashboard.html")))
    return found


def main(argv):
    check_only = "--check" in argv
    args = [a for a in argv if not a.startswith("--")]
    files = targets(args)
    if not files:
        sys.stderr.write("[ERROR] no dashboards found\n")
        return 1
    rc = 0
    for f in files:
        if not os.path.isfile(f):
            sys.stderr.write("[FAIL] not a file: %s\n" % f)
            rc = 1
            continue
        rc |= process(f, check_only)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
