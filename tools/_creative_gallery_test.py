"""Creative-gallery test for every Meta-fed client (off-cloud: no network, no GCS, no API cost).

Covers the three rules that decide whether a client sees real ads or a wall of grey boxes:

  1. `_is_link_preview` / `_usable_image` -- Meta serves an external image PROXY
     (`external-<edge>.xx.fbcdn.net/emg1/...?url=<page>`) when a creative has no real image. It is
     a VALID near-blank image, so the browser's onerror never fires and the branded-tile fallback
     never gets its chance; the card renders a grey box. It must be rejected at the source.
  2. `cache_creative_images` -- byte-identical artwork REUSED across ad variants must still be
     cached. An earlier version treated any duplicate hash as a placeholder and deleted real
     artwork; this test is the guard against that regression coming back.
  3. `_clean_headline` -- Meta's `title` is frequently a display link ("fb.me") or an unrendered
     dynamic-ad template ("{{product.name}}"), neither of which is a headline.

Run: python tools/_creative_gallery_test.py
"""
import os
import sys
import tempfile
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLIENTS = [                                  # (label, package dir, local-cache env var)
    ("RHE", "client_RHE", "RHE_CREATIVE_LOCAL_DIR"),
    ("honeytribe", "client_honeytribe", "HONEYTRIBE_CREATIVE_LOCAL_DIR"),
]
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40    # enough to pass for an image body

PROXY = "https://external-fra5-1.xx.fbcdn.net/emg1/v/t13/998244?url=https%3A%2F%2Ffb.com"
REAL = "https://scontent-fra3-2.xx.fbcdn.net/v/t45.1600-4/533600982_n.png"


class _FakeResp(object):
    def __init__(self, data, ctype):
        self._d, self.headers = data, {"Content-Type": ctype}

    def read(self):
        return self._d

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _load(pkg):
    """Import a client's job module in isolation (they all happen to be named `main`)."""
    sys.path.insert(0, os.path.join(ROOT, "clients", pkg, "job"))
    sys.modules.pop("main", None)
    import main as mod
    return mod


def run(label, pkg, envvar, fails):
    def check(cond, msg):
        print(("  ok   " if cond else "  FAIL ") + msg)
        if not cond:
            fails.append("%s: %s" % (label, msg))

    print("== %s ==" % label)
    job = _load(pkg)
    try:
        check(job._is_link_preview(PROXY), "the emg1 external proxy is a link preview")
        check(not job._is_link_preview(REAL), "a real scontent image is NOT a link preview")
        check(job._usable_image("", PROXY) == "", "proxy-only creative yields no image")
        check(job._usable_image(REAL, PROXY) == REAL, "a real image_url beats a proxy thumbnail")
        check(job._usable_image("", REAL) == REAL, "a real thumbnail is used when image_url is empty")
        check(job._usable_image(PROXY, REAL) == REAL, "a proxy image_url is skipped for a real thumb")

        tmp = tempfile.mkdtemp()
        os.environ[envvar] = tmp
        orig = urllib.request.urlopen
        urllib.request.urlopen = lambda req, timeout=None: _FakeResp(PNG, "image/png")
        try:
            creatives = {"enabled": True, "items": [
                {"cid": "a", "thumb": REAL, "head": "A"},
                {"cid": "b", "thumb": REAL, "head": "B"},   # byte-identical: REUSED artwork
                {"cid": "c", "thumb": "", "head": "C"},     # nothing usable to cache
            ]}
            rows = [{"cid": "a", "imps": 900}, {"cid": "b", "imps": 500}, {"cid": "c", "imps": 100}]
            by = {c["cid"]: c for c in job.cache_creative_images(creatives, rows)["items"]}
            check(by["a"].get("cached") is True, "the busiest creative is cached")
            check(by["b"].get("cached") is True,
                  "REUSED artwork sharing bytes is still cached (not read as a placeholder)")
            check(not by["c"].get("cached"), "a creative with no usable image is not cached")
            check(sorted(os.listdir(tmp)) == ["a", "b"], "exactly the two real images hit storage")

            # A blob left behind by a run that predates the link-preview rule must be PURGED,
            # not re-flagged as cached -- otherwise we serve Meta's grey tile out of our own
            # bucket, where (unlike Meta's CDN link) it never expires.
            with open(os.path.join(tmp, "c"), "wb") as fh:
                fh.write(PNG)
            again = {c["cid"]: c for c in job.cache_creative_images(
                {"enabled": True, "items": [{"cid": "c", "thumb": "", "head": "C"}]},
                [{"cid": "c", "imps": 100}])["items"]}
            check(not again["c"].get("cached"), "a stale placeholder is not re-flagged as cached")
            check(not os.path.exists(os.path.join(tmp, "c")),
                  "the stale placeholder object is deleted from storage")
        finally:
            urllib.request.urlopen = orig
            os.environ.pop(envvar, None)

        check(job._clean_headline("{{product.name}}", "Ad 1") == "Ad 1",
              "an unrendered dynamic-ad template falls back to the ad name")
        check(job._clean_headline("fb.me", "Ad 2") == "Ad 2", "a display link falls back to the ad name")
        check(job._clean_headline("Convert. Rent. Repeat.", "Ad 3") == "Convert. Rent. Repeat.",
              "a real headline is kept verbatim")
    finally:
        sys.path.pop(0)
        sys.modules.pop("main", None)


def main():
    fails = []
    for label, pkg, envvar in CLIENTS:
        run(label, pkg, envvar, fails)
    print()
    if fails:
        print("RESULT: %d failure(s)" % len(fails))
        for f in fails:
            print("  - " + f)
        return 1
    print("RESULT: all creative-gallery checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
