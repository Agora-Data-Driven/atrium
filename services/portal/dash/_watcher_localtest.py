"""Off-cloud test for the Watcher tab (no GCS, no network) -- the parser, the data layer, and the
Flask routes.

Stubs google.cloud.storage and points the workspace store at a temp dir (like _atrium_smoketest),
stubs watcher's YouTube fetchers with canned pages, then proves: channel resolution, playlist
paging, transcript batching, workspace CRUD (registry + the per-channel archive object), the
team-only route gating, and the click-to-expand transcript GET.

Run: python _watcher_localtest.py        # prints PASS / FAIL, exits 0 / 1
"""

import os
import shutil
import sys
import tempfile
import types

# 1. Stub google.cloud.storage BEFORE importing main (store/feedback construct a client at import).
_g = types.ModuleType("google"); _g.__path__ = []
_gc = types.ModuleType("google.cloud"); _gc.__path__ = []
_gs = types.ModuleType("google.cloud.storage")


class _FakeClient:
    def __init__(self, *a, **k):
        pass

    def bucket(self, *a, **k):
        raise RuntimeError("GCS disabled in this test (use the local backend)")


_gs.Client = _FakeClient
sys.modules.setdefault("google", _g)
sys.modules.setdefault("google.cloud", _gc)
sys.modules["google.cloud.storage"] = _gs

_TMP = tempfile.mkdtemp(prefix="atrium_watcher_")
os.environ["WORKSPACE_LOCAL_DIR"] = _TMP
os.environ["REGISTRY_LOCAL_DIR"] = _TMP
os.environ["SESSION_SECRET"] = "test-secret"

import seed_workspace   # noqa: E402
import store            # noqa: E402
import watcher          # noqa: E402
import watcher_blog     # noqa: E402
import watcher_ig       # noqa: E402
import workspace        # noqa: E402
import main             # noqa: E402

CLIENT = "riverdance"
SUPER = {"ok": True, "user": "info@agoradatadriven.com", "clients": ["*"]}
CLIENT_LOGIN = {"ok": True, "user": "owner@riverdanceresort.com", "clients": [CLIENT]}

_CHANNEL_ID = "UC" + "a" * 22
_CHANNEL_HTML = ('<html><head><meta property="og:title" content="Data With Dana &amp; Co">'
                 '</head><body>"channelId":"%s"</body></html>' % _CHANNEL_ID)


def _hand_channels(client=CLIENT):
    """A client's HAND-ADDED watcher sources, with template-seeded ones filtered out.

    Every client is now pre-seeded from watcher_template.py (and the reconcile tops it up on any team
    render), so "the registry is empty" is no longer true of a real workspace. A check about what a
    TEST added has to ignore the template's entries to stay honest."""
    return [ch for ch in workspace.watcher_channels(workspace.load_workspace(client))
            if not ch.get("template_id")]


def _check(label, cond):
    if not cond:
        raise AssertionError(label)
    print("  [OK] %s" % label)


def _video_renderer(vid, title):
    return {"playlistVideoRenderer": {"videoId": vid, "title": {"runs": [{"text": title}]}}}


def _video_lockup(vid, title, ago=""):
    """The 2025+ lockupViewModel shape (what live YouTube now serves for playlist items)."""
    meta = {"title": {"content": title}}
    if ago:
        meta["metadata"] = {"contentMetadataViewModel": {"metadataRows": [
            {"metadataParts": [{"text": {"content": "12K views"}}, {"text": {"content": ago}}]}]}}
    return {"lockupViewModel": {"contentId": vid, "contentType": "LOCKUP_CONTENT_TYPE_VIDEO",
                                "metadata": {"lockupMetadataViewModel": meta}}}


def _browse_pages():
    """Two canned browse responses: page 1 (2 classic-renderer videos + a continuation), page 2
    (lockupViewModel videos, done) -- so BOTH item shapes are proven to parse."""
    page1 = {"contents": {"stuff": [
        _video_renderer("vid00000001", "How to model churn"),
        _video_renderer("vid00000002", "SQL window functions"),
        {"continuationItemRenderer": {"continuationEndpoint": {
            "continuationCommand": {"token": "TOKEN-2"}}}},
    ]}}
    page2 = {"onResponseReceivedActions": [
        _video_lockup("vid00000002", "SQL window functions"),   # duplicate: must de-dupe
        _video_lockup("vid00000003", "Pandas in production", ago="2 weeks ago"),
    ]}
    return {"first": page1, "TOKEN-2": page2}


# --- Canned website fixtures for the blog path (watcher_blog), served by _site_fetcher ------------
_SITE = "https://example-law.test"
# ⚠️ The `/blogs/*+*` rule is the REGRESSION CASE: read as a plain prefix it says "/blogs/", which
# would ban every post on the site. Proper wildcard matching must leave the posts allowed.
_ROBOTS = """
User-agent: *
Disallow: /checkout
Disallow: /blogs/*+*
Disallow: /private
Allow: /private/public-note

Sitemap: %s/sitemap.xml
""" % _SITE
_SITEMAP_INDEX = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>%s/sitemap_products_1.xml</loc></sitemap>
  <sitemap><loc>%s/sitemap_blogs_1.xml</loc></sitemap>
</sitemapindex>""" % (_SITE, _SITE)
_BLOG_SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
        xmlns:image="http://www.google.com/schemas/sitemap-image/1.1">
  <url><loc>%s/blogs/blog</loc><lastmod>2026-07-27T21:50:34-06:00</lastmod></url>
  <url>
    <loc>%s/blogs/blog/what-is-an-nda</loc><lastmod>2026-02-09T13:04:10-07:00</lastmod>
    <image:image><image:title>What Is an NDA &amp; Why You Need One</image:title></image:image>
  </url>
  <url><loc>%s/blogs/blog/hiring-contractors</loc><lastmod>2026-01-05T00:00:00-07:00</lastmod></url>
  <url><loc>%s/private/secret-draft</loc><lastmod>2026-01-06T00:00:00-07:00</lastmod></url>
</urlset>""" % (_SITE, _SITE, _SITE, _SITE)
# A realistic article page: real content wrapped in nav/footer/related-posts junk, the title and
# date only in metadata. The extractor must return the prose and NOTHING else.
_POST_HTML = """<html><head>
<meta property="og:site_name" content="Example Law Co">
<meta property="og:title" content="What Is an NDA and Why You Need One">
<meta property="article:published_time" content="2026-01-28 00:00:00 -0700">
<script type="application/ld+json">{"@type":"BlogPosting","author":{"@type":"Person","name":"Paige G"}}</script>
<title>What Is an NDA and Why You Need One | Example Law Co</title></head>
<body>
<nav class="site-nav"><a href="/">Home</a><a href="/shop">Shop</a><a href="/cart">Cart</a></nav>
<header class="promo">Free shipping on every contract template, today only, use code SAVE</header>
<div class="page-wrap">
  <div class="article--content rte">
    <p>%s</p>
    <h2>When do you need one?</h2>
    <p>%s</p>
    <p>%s</p>
  </div>
  <aside class="related-posts"><a href="/blogs/blog/other">Another post you might like</a>
    <a href="/blogs/blog/more">And one more post to read next</a></aside>
</div>
<footer class="site-footer">All rights reserved. Subscribe to our newsletter for legal tips.</footer>
<script>var tracking = {id: 42};</script></body></html>""" % (
    "An NDA is a contract where the parties agree not to share confidential information. " * 6,
    "Any time you hand a contractor, employee or collaborator something you would not publish. " * 6,
    "Give them time to review it, encourage questions, and keep a signed copy on file. " * 6)


def _site_fetcher(url):
    """Injected fetcher: serves the canned site, 404s anything else (no network in this test)."""
    pages = {
        _SITE + "/robots.txt": _ROBOTS,
        _SITE + "/sitemap.xml": _SITEMAP_INDEX,
        _SITE + "/sitemap_blogs_1.xml": _BLOG_SITEMAP,
        _SITE + "/": _POST_HTML,
        _SITE: _POST_HTML,
        _SITE + "/blogs/blog/what-is-an-nda": _POST_HTML,
        _SITE + "/blogs/blog/hiring-contractors": _POST_HTML,
    }
    if url in pages:
        return pages[url]
    raise watcher_blog.FetchError(404)


def _run_blog_checks(c):
    """The website-blog half of Watcher: the parser, then the routes (fetchers injected)."""
    # --- watcher_blog.py: site resolution ---------------------------------------------------------
    info = watcher_blog.resolve_site(_SITE, fetcher=_site_fetcher)
    _check("resolve_site finds the origin + og:site_name",
           info["ok"] and info["site"] == _SITE and info["title"] == "Example Law Co")
    _check("resolve_site rejects a non-URL", watcher_blog.resolve_site("not a website")["ok"] is False)
    _check("site_origin strips the path",
           watcher_blog.site_origin("https://a.test/blog/x?y=1#z") == "https://a.test")
    _check("normalize_site_url adds the scheme",
           watcher_blog.normalize_site_url("example.com/blog") == "https://example.com/blog")

    # --- watcher_blog.py: robots.txt (proper wildcard/Allow precedence, not prefix-matching) ------
    _sitemaps, rules = watcher_blog._robots(_SITE, _site_fetcher)
    _check("robots.txt sitemap declaration is read", _sitemaps == [_SITE + "/sitemap.xml"])
    _check("a `/blogs/*+*` rule does NOT ban every blog post (the prefix-matching regression)",
           watcher_blog._allowed(_SITE + "/blogs/blog/what-is-an-nda", _SITE, rules) is True)
    _check("...but it still blocks a URL that actually matches the wildcard",
           watcher_blog._allowed(_SITE + "/blogs/blog/a+b", _SITE, rules) is False)
    _check("a plain Disallow is honored",
           watcher_blog._allowed(_SITE + "/private/secret-draft", _SITE, rules) is False)
    _check("a longer Allow beats a shorter Disallow",
           watcher_blog._allowed(_SITE + "/private/public-note", _SITE, rules) is True)

    # --- watcher_blog.py: listing (sitemap index -> blog sitemap -> posts) -------------------------
    listing = watcher_blog.list_posts(_SITE, fetcher=_site_fetcher)
    urls = [p["url"] for p in listing["posts"]]
    _check("list_posts follows the index into the BLOG sitemap only",
           listing["ok"] and listing["source"] == "sitemap")
    _check("list_posts returns the posts, newest first",
           urls == [_SITE + "/blogs/blog/what-is-an-nda", _SITE + "/blogs/blog/hiring-contractors"])
    _check("the blog's own index page is dropped (it is a parent of other posts)",
           _SITE + "/blogs/blog" not in urls)
    _check("a robots-disallowed URL never reaches the archive",
           _SITE + "/private/secret-draft" not in urls)
    _check("a sitemap image:title becomes the post title",
           listing["posts"][0]["title"] == "What Is an NDA & Why You Need One")
    _check("a title-less entry falls back to a readable slug title",
           listing["posts"][1]["title"] == "Hiring Contractors")
    _check("lastmod becomes the publish date", listing["posts"][0]["published"] == "2026-02-09")
    _check("post ids are stable + URL-safe (usable as a route path segment)",
           listing["posts"][0]["id"] == watcher_blog.post_id(_SITE + "/blogs/blog/what-is-an-nda")
           and "/" not in listing["posts"][0]["id"])
    _check("a site with no sitemap and no links reports a friendly failure",
           watcher_blog.list_posts("https://nothing.test",
                                   fetcher=lambda u: (_ for _ in ()).throw(
                                       watcher_blog.FetchError(404)))["ok"] is False)

    # --- watcher_blog.py: article extraction ------------------------------------------------------
    art = watcher_blog.extract_article(_POST_HTML, _SITE + "/blogs/blog/what-is-an-nda")
    _check("extract_article reads the title from og:title",
           art["title"] == "What Is an NDA and Why You Need One")
    _check("extract_article reads the date from article:published_time", art["published"] == "2026-01-28")
    _check("extract_article reads the author from JSON-LD", art["author"] == "Paige G")
    _check("extract_article returns the article prose", "An NDA is a contract" in art["text"]
           and "Give them time to review it" in art["text"])
    _check("extract_article keeps the in-article heading", "When do you need one?" in art["text"])
    for junk in ("Free shipping", "All rights reserved", "Another post you might like",
                 "var tracking", "Home"):
        _check("extract_article drops the %r boilerplate" % junk[:18], junk not in art["text"])
    _check("extract_article survives a page with no article at all",
           watcher_blog.extract_article("<html><body><p>hi</p></body></html>")["text"] == "")

    # --- watcher_blog.py: fetch_post outcome mapping ----------------------------------------------
    r = watcher_blog.fetch_post(_SITE + "/blogs/blog/what-is-an-nda", fetcher=_site_fetcher)
    _check("fetch_post returns the body in the `transcript` field (the shared archive shape)",
           r["ok"] and "An NDA is a contract" in r["transcript"] and r["published"] == "2026-01-28")
    r = watcher_blog.fetch_post(_SITE + "/gone", fetcher=_site_fetcher)
    _check("a 404 post is a PERMANENT error (never retried)",
           r["ok"] is False and r["permanent"] is True)
    r = watcher_blog.fetch_post("x", fetcher=lambda u: (_ for _ in ()).throw(watcher_blog.FetchError(429)))
    _check("a 429 is transient and worded so the retry loop backs off",
           r["ok"] is False and r["permanent"] is False and "rate-limiting" in r["error"])

    # --- watcher_blog.py: batch fetching ----------------------------------------------------------
    entries = [watcher_blog.post_entry(p) for p in listing["posts"]]
    fetched, blocked = watcher_blog.fetch_posts_batch(entries, workers=1, pause=0,
                                                      fetcher=_site_fetcher)
    _check("fetch_posts_batch fills every pending post",
           fetched == 2 and blocked is False and all(e["transcript"] for e in entries))
    _check("the fetch HEALS the title and date from the page itself",
           entries[1]["title"] == "What Is an NDA and Why You Need One"
           and entries[1]["published"] == "2026-01-28")
    blk = [watcher_blog.post_entry({"url": _SITE + "/blogs/blog/a", "id": "b1"}),
           watcher_blog.post_entry({"url": _SITE + "/blogs/blog/b", "id": "b2"})]
    fetched, blocked = watcher_blog.fetch_posts_batch(
        blk, workers=1, pause=0, fetcher=lambda u: (_ for _ in ()).throw(watcher_blog.FetchError(429)))
    _check("a throttled site stops the batch WITHOUT poisoning posts",
           fetched == 0 and blocked is True and all(e["error"] == "" for e in blk))

    # --- Routes: add_site -> fetch -> read -> refresh -> delete (fetchers injected) ---------------
    real_resolve_site, real_list_posts = watcher_blog.resolve_site, watcher_blog.list_posts
    real_fetch_post = watcher_blog.fetch_post
    watcher_blog.resolve_site = lambda url, fetcher=None: real_resolve_site(url, fetcher=_site_fetcher)
    watcher_blog.list_posts = lambda site, fetcher=None, start_url="": real_list_posts(
        site, fetcher=_site_fetcher, start_url=start_url)
    watcher_blog.fetch_post = lambda url, fetcher=None: real_fetch_post(url, fetcher=_site_fetcher)

    r = c.post("/w/%s/admin/watcher" % CLIENT, data={"op": "add_site", "url": _SITE})
    data = r.get_json()
    _check("op=add_site lists every blog post", data["ok"] and data["posts"] == 2)
    site_chan = data["channel"]
    ch = workspace.find_watcher_channel(workspace.load_workspace(CLIENT), site_chan)
    _check("the site is registered as a `blog` platform keyed by its origin",
           ch["platform"] == "blog" and ch["channel_id"] == _SITE and ch["video_count"] == 2)
    _check("op=add_site refuses the same site twice",
           c.post("/w/%s/admin/watcher" % CLIENT,
                  data={"op": "add_site", "url": _SITE + "/blogs/blog"}).get_json()["ok"] is False)
    _check("op=add_site rejects a non-URL",
           c.post("/w/%s/admin/watcher" % CLIENT,
                  data={"op": "add_site", "url": "nonsense"}).get_json()["ok"] is False)

    body = c.get("/w/%s/watcher" % CLIENT).get_data(as_text=True)
    _check("the blog card renders with WEBSITE wording, not video wording",
           "Example Law Co" in body and "Open site" in body and "2 posts" in body
           and "Article not fetched yet." in body)
    _check("Safe pull is NOT offered on a website card (it is a YouTube-only escape hatch)",
           ('data-wtsafe="%s"' % site_chan) not in body)

    r = c.post("/w/%s/admin/watcher" % CLIENT, data={"op": "fetch", "channel_id": site_chan})
    data = r.get_json()
    _check("op=fetch pulls the article text for a blog channel",
           data["ok"] and data["done"] == 2 and data["remaining"] == 0)
    posts = workspace.read_watcher_videos(CLIENT, site_chan)
    _check("the article text landed in the archive",
           all("An NDA is a contract" in p["transcript"] for p in posts))
    ch = workspace.find_watcher_channel(workspace.load_workspace(CLIENT), site_chan)
    _check("blog registry counts update like a channel's", ch["transcript_count"] == 2)

    r = c.get("/w/%s/watcher/video/%s/%s" % (CLIENT, site_chan, posts[0]["id"]))
    data = r.get_json()
    _check("the reader GET serves the FULL article and says it is a blog",
           data["ok"] and data["platform"] == "blog" and "An NDA is a contract" in data["transcript"])

    _check("op=safe_pull is refused for a website (with a helpful message)",
           c.post("/w/%s/admin/watcher" % CLIENT,
                  data={"op": "safe_pull", "channel_id": site_chan}).get_json()["ok"] is False)

    r = c.post("/w/%s/admin/watcher" % CLIENT, data={"op": "refresh", "channel_id": site_chan})
    _check("op=refresh on a blog adds nothing when the site has not published",
           r.get_json()["ok"] is True and r.get_json()["new"] == 0)
    _check("refresh kept the fetched article text",
           all(p["transcript"] for p in workspace.read_watcher_videos(CLIENT, site_chan)))

    # A new post appears in the sitemap -> refresh adds ONLY that one.
    watcher_blog.list_posts = lambda site, fetcher=None, start_url="": {
        "ok": True, "error": "", "source": "sitemap", "posts": real_list_posts(
            site, fetcher=_site_fetcher, start_url=start_url)["posts"] + [
            {"id": watcher_blog.post_id(_SITE + "/blogs/blog/brand-new"),
             "url": _SITE + "/blogs/blog/brand-new", "title": "Brand New",
             "published": "2026-07-28", "published_text": "2026-07-28"}]}
    r = c.post("/w/%s/admin/watcher" % CLIENT, data={"op": "refresh", "channel_id": site_chan})
    _check("op=refresh adds only the newly published post", r.get_json()["new"] == 1)
    _check("the new post is pending while the old ones keep their text",
           len(workspace.read_watcher_videos(CLIENT, site_chan)) == 3
           and sum(1 for p in workspace.read_watcher_videos(CLIENT, site_chan)
                   if p["transcript"]) == 2)

    # --- One box, both sources: a non-YouTube link is scraped as an article ----------------------
    r = c.post("/w/%s/admin/watcher" % CLIENT,
               data={"op": "add_video", "url": _SITE + "/blogs/blog/what-is-an-nda"})
    data = r.get_json()
    _check("op=add_video auto-detects an ARTICLE link and scrapes it",
           data["ok"] and "An NDA is a contract" in data["transcript"] and data["already"] is False)
    loose_blog = next(ch for ch in workspace.watcher_channels(workspace.load_workspace(CLIENT))
                      if ch.get("loose") and ch.get("platform") == "blog")
    _check("it saved under a SEPARATE 'Saved articles' loose channel",
           loose_blog["title"] == workspace.LOOSE_BLOG_TITLE and loose_blog["transcript_count"] == 1)
    r = c.post("/w/%s/admin/watcher" % CLIENT,
               data={"op": "add_video", "url": _SITE + "/blogs/blog/what-is-an-nda"})
    _check("re-scraping the same article de-dupes",
           r.get_json()["already"] is True
           and len(workspace.read_watcher_videos(CLIENT, loose_blog["id"])) == 1)

    # --- Team-only gating on the blog routes -----------------------------------------------------
    with c.session_transaction() as s:
        s.clear()
        s.update(CLIENT_LOGIN)
    _check("a client cannot add a website", c.post(
        "/w/%s/admin/watcher" % CLIENT, data={"op": "add_site", "url": _SITE}).status_code == 403)
    _check("a client cannot read an archived article", c.get(
        "/w/%s/watcher/video/%s/%s" % (CLIENT, site_chan, posts[0]["id"])).status_code == 403)
    with c.session_transaction() as s:
        s.clear()
        s.update(SUPER)

    r = c.post("/w/%s/admin/watcher" % CLIENT, data={"op": "delete", "channel_id": site_chan})
    _check("op=delete removes the website archive", r.get_json()["ok"] is True)
    workspace.delete_watcher_channel(CLIENT, loose_blog["id"])
    watcher_blog.resolve_site, watcher_blog.list_posts = real_resolve_site, real_list_posts
    watcher_blog.fetch_post = real_fetch_post


# =================================================================================================
# INSTAGRAM (watcher_ig.py) -- the third source type. Canned fixtures for all three endpoints the
# module can talk to: the web-profile blob (the only one that sometimes answers logged out), the
# authenticated user feed (paging past post 12) and per-post media info. Nothing here touches the
# network -- every seam (json_fetcher / fetcher / media_fetcher / transcriber) is injected.
# =================================================================================================
_IG_USER = "greenlawco"
_IG_PK = "77712345"


def _ig_node(code, caption, taken_at, video=False):
    """One post in the web_profile_info (GraphQL) shape."""
    node = {"shortcode": code, "taken_at_timestamp": taken_at, "is_video": video,
            "edge_media_to_caption": {"edges": [{"node": {"text": caption}}]}}
    return {"node": node}


def _ig_feed_item(code, caption, taken_at, video=False):
    """One post in the private-API feed shape (`code` + a caption OBJECT, not GraphQL edges)."""
    item = {"code": code, "taken_at": taken_at, "caption": {"text": caption}}
    if video:
        item["video_versions"] = [{"url": "https://cdn.test/%s.mp4" % code}]
        item["product_type"] = "clips"
    return item


_IG_PROFILE = {"data": {"user": {
    "id": _IG_PK, "full_name": "Green Law Co", "is_private": False,
    "edge_owner_to_timeline_media": {"count": 4, "edges": [
        _ig_node("IGnewest0001", "#ad Three NDA mistakes founders make\nSave this one.", 1785000000,
                 video=True),
        _ig_node("IGsecond0002", "Contract templates that actually hold up", 1784000000),
    ]}}}}
_IG_FEED = {"items": [
    _ig_feed_item("IGnewest0001", "Three NDA mistakes founders make", 1785000000, video=True),
    _ig_feed_item("IGolder00003", "What a retainer really buys you", 1783000000),
    _ig_feed_item("IGoldest0004", "How we price fixed-fee work", 1782000000),
], "more_available": False, "next_max_id": ""}
_IG_MEDIA = {"items": [{
    "code": "IGnewest0001", "taken_at": 1785000000,
    "caption": {"text": "Three NDA mistakes founders make\nSave this one. #legal #founders"},
    "accessibility_caption": "Photo of a person holding a document",
    "user": {"username": _IG_USER},
    "video_versions": [{"url": "https://cdn.test/IGnewest0001.mp4"}],
}]}
# The keyless embed page -- the ONE thing Instagram serves a logged-out client.
_IG_EMBED = ('<html><body><div class="Caption">'
             '<a class="CaptionUsername" href="/greenlawco/">greenlawco</a>'
             'Contract templates that actually hold up<br>Link in bio.'
             '<div class="CaptionComments">View all 31 comments</div></div>'
             '<script>window.__additionalDataLoaded(\'extra\', '
             '{"shortcode_media":{"shortcode":"IGsecond0002","taken_at_timestamp":1784000000,'
             '"owner":{"username":"greenlawco"},'
             '"edge_media_to_caption":{"edges":[{"node":{"text":"Contract templates that actually '
             'hold up"}}]}}});</script></body></html>')


def _ig_json(url, referer=""):
    """Injected JSON seam: serves the three canned endpoints, 404s anything else."""
    if "web_profile_info" in url:
        return _IG_PROFILE
    if "/feed/user/" in url:
        return _IG_FEED
    if "/media/" in url and "/info/" in url:
        return _IG_MEDIA
    raise watcher_ig.FetchError(404)


def _ig_html(url, referer=""):
    """Injected HTML seam: the embed page for any post."""
    if "/embed/" in url:
        return _IG_EMBED
    raise watcher_ig.FetchError(404)


def _ig_media_bytes(url):
    """Injected media seam: pretend we downloaded a small mp4."""
    return b"\x00fake-mp4-bytes", "video/mp4"


def _ig_transcriber(data, mime, prompt, usage_out=None):
    """Injected ASR seam: what Gemini would return for the reel's audio."""
    if usage_out is not None:
        usage_out.update({"model": "gemini-2.5-flash", "input_tokens": 900, "output_tokens": 40})
    return "So the first mistake founders make with an NDA is signing the other side's version.", ""


class _IgSession(object):
    """Set/clear INSTAGRAM_SESSIONID around a block.

    The module reads the env on EVERY call on purpose (a secret can be rotated without a redeploy),
    so the tests flip it the same way rather than caching a flag."""

    def __init__(self, value):
        self.value = value
        self.prev = None

    def __enter__(self):
        self.prev = os.environ.get("INSTAGRAM_SESSIONID")
        if self.value:
            os.environ["INSTAGRAM_SESSIONID"] = self.value
        else:
            os.environ.pop("INSTAGRAM_SESSIONID", None)
        return self

    def __exit__(self, *exc):
        if self.prev is None:
            os.environ.pop("INSTAGRAM_SESSIONID", None)
        else:
            os.environ["INSTAGRAM_SESSIONID"] = self.prev
        return False


def _run_ig_checks(c):
    """The Instagram third of Watcher: link parsing, the two listing legs, reel transcription, and
    the routes (every network + AI seam injected)."""
    # --- Link parsing: what did the operator paste? -----------------------------------------------
    for link, code in (("https://www.instagram.com/p/ABC123xyz/", "ABC123xyz"),
                       ("https://instagram.com/reel/ABC123xyz", "ABC123xyz"),
                       ("https://www.instagram.com/reels/ABC123xyz/?igsh=1", "ABC123xyz"),
                       ("instagram.com/tv/ABC123xyz/", "ABC123xyz"),
                       ("https://www.instagram.com/greenlawco/reel/ABC123xyz/", "ABC123xyz")):
        _check("extract_shortcode reads %r" % link[-24:], watcher_ig.extract_shortcode(link) == code)
    _check("extract_shortcode returns '' for a PROFILE link (that is how the two are told apart)",
           watcher_ig.extract_shortcode("https://www.instagram.com/greenlawco/") == "")
    _check("extract_username reads a profile handle",
           watcher_ig.extract_username("https://www.instagram.com/greenlawco/?hl=en") == "greenlawco")
    _check("extract_username returns '' for a post link",
           watcher_ig.extract_username("https://www.instagram.com/p/ABC123xyz/") == "")
    _check("extract_username never treats an Instagram ROUTE as a username",
           watcher_ig.extract_username("https://www.instagram.com/explore/") == ""
           and watcher_ig.extract_username("https://www.instagram.com/accounts/login/") == "")
    _check("a bare @handle normalizes to a profile URL",
           watcher_ig.extract_username("@greenlawco") == "greenlawco")
    _check("is_instagram_url is true with or without a scheme",
           watcher_ig.is_instagram_url("instagram.com/x") and watcher_ig.is_instagram_url(
               "https://www.instagram.com/") and not watcher_ig.is_instagram_url("example.com/blog"))

    # --- shortcode <-> media pk (pure base64 arithmetic; it is how the authed endpoint is reached) -
    _check("media_pk decodes the shortcode alphabet", watcher_ig.media_pk("B") == "1"
           and watcher_ig.media_pk("BA") == "64" and watcher_ig.media_pk("BB") == "65")
    _check("media_pk refuses a character outside the alphabet", watcher_ig.media_pk("A!B") == "")
    _check("media_pk uses only the first 11 chars (the tail is carousel data, not the id)",
           watcher_ig.media_pk("BBBBBBBBBBBZZZ") == watcher_ig.media_pk("BBBBBBBBBBB"))

    # --- Titles + body composition ----------------------------------------------------------------
    _check("caption_title takes the caption's first real line",
           watcher_ig.caption_title("Three NDA mistakes\nSave this", "X") == "Three NDA mistakes")
    _check("caption_title strips a leading hashtag/mention so the card reads as a title",
           watcher_ig.caption_title("#ad Three NDA mistakes", "X") == "Three NDA mistakes")
    _check("a caption-less post still gets a title",
           watcher_ig.caption_title("", "ABC123") == "Instagram post ABC123")
    body = watcher_ig.compose_body("The caption", "The spoken words", "The alt text")
    _check("compose_body labels each part so writing and speech stay distinguishable",
           "Caption:" in body and "Spoken in this reel:" in body and "Shown in the image:" in body
           and "The spoken words" in body)
    _check("compose_body of nothing is empty (which is what marks a post unreadable)",
           watcher_ig.compose_body("", "", "") == "")

    # --- resolve_profile --------------------------------------------------------------------------
    with _IgSession("fake-session"):
        info = watcher_ig.resolve_profile("https://www.instagram.com/greenlawco/",
                                          json_fetcher=_ig_json)
        _check("resolve_profile returns the handle + display name",
               info["ok"] and info["username"] == _IG_USER and info["title"] == "Green Law Co")
    _check("a PROFILE op given a POST link says which dropdown option to use instead",
           "Instagram post" in watcher_ig.resolve_profile(
               "https://www.instagram.com/p/ABC123xyz/")["error"])
    _check("resolve_profile rejects a non-Instagram link",
           watcher_ig.resolve_profile("https://example.com/blog")["ok"] is False)

    # --- list_posts: the public leg alone vs the authenticated feed walk ---------------------------
    with _IgSession(""):
        pub = watcher_ig.list_posts(_IG_USER, json_fetcher=_ig_json)
        _check("logged out, list_posts still returns what the public blob carries",
               pub["ok"] and len(pub["posts"]) == 2 and pub["source"] == "public")
        _check("...and flags needs_session so the UI can say the history is incomplete",
               pub["needs_session"] is True)
        empty = watcher_ig.list_posts("nobody", json_fetcher=lambda u, r="": {"data": {}})
        _check("logged out with nothing public, the failure names the missing secret",
               empty["ok"] is False and "instagram-session" in empty["error"])
    with _IgSession("fake-session"):
        full = watcher_ig.list_posts(_IG_USER, json_fetcher=_ig_json)
        codes = [p["id"] for p in full["posts"]]
        _check("with a session the feed walk reaches every post",
               full["ok"] and full["source"] == "feed" and len(codes) == 4)
        _check("posts come back newest-first and de-duplicated across the two legs",
               codes == ["IGnewest0001", "IGsecond0002", "IGolder00003", "IGoldest0004"])
        _check("a post's date comes from its epoch timestamp",
               full["posts"][0]["published"] == "2026-07-25")
        _check("the caption's first line becomes the card title",
               full["posts"][0]["title"] == "Three NDA mistakes founders make")
        _check("a private account fails with a plain reason, not an empty listing",
               watcher_ig.list_posts("x", json_fetcher=lambda u, r="": {
                   "data": {"user": {"is_private": True}}})["ok"] is False)

    # --- fetch_post: the authed media path (with reel transcription) ------------------------------
    with _IgSession("fake-session"):
        usage = {}
        r = watcher_ig.fetch_post("https://www.instagram.com/reel/IGnewest0001/",
                                  json_fetcher=_ig_json, media_fetcher=_ig_media_bytes,
                                  transcriber=_ig_transcriber, usage_out=usage)
        _check("fetch_post returns the body in the `transcript` field (the shared archive shape)",
               r["ok"] and "Three NDA mistakes" in r["transcript"])
        _check("a reel's SPOKEN words are transcribed into the same body",
               r["spoken"] is True and "signing the other side's version" in r["transcript"])
        _check("Instagram's own alt text is archived too",
               "Photo of a person holding a document" in r["transcript"])
        _check("the post's real date and author come back", r["published"] == "2026-07-25"
               and r["author"] == _IG_USER)
        _check("the transcription's token usage is reported so it can be billed to the client",
               usage["input_tokens"] == 900 and usage["output_tokens"] == 40)

        r = watcher_ig.fetch_post("https://www.instagram.com/reel/IGnewest0001/",
                                  json_fetcher=_ig_json, media_fetcher=_ig_media_bytes,
                                  transcriber=_ig_transcriber, transcribe=False)
        _check("transcribe=False archives the caption alone (no Gemini call)",
               r["ok"] and r["spoken"] is False and "signing the other side" not in r["transcript"])

        r = watcher_ig.fetch_post("https://www.instagram.com/reel/IGnewest0001/",
                                  json_fetcher=_ig_json,
                                  media_fetcher=lambda u: (b"", "video/mp4"),
                                  transcriber=_ig_transcriber)
        _check("an OVERSIZED video degrades to caption-only instead of failing the post",
               r["ok"] is True and r["spoken"] is False)
        r = watcher_ig.fetch_post("https://www.instagram.com/reel/IGnewest0001/",
                                  json_fetcher=_ig_json, media_fetcher=_ig_media_bytes,
                                  transcriber=lambda *a, **k: ("", "vertex is down"))
        _check("a FAILED transcription degrades the same way (never marks the post failed)",
               r["ok"] is True and r["spoken"] is False and "Three NDA mistakes" in r["transcript"])

    # --- fetch_post: the keyless embed path + error mapping ---------------------------------------
    with _IgSession(""):
        r = watcher_ig.fetch_post("https://www.instagram.com/p/IGsecond0002/", fetcher=_ig_html)
        _check("logged out, the embed page still yields the caption",
               r["ok"] and "Contract templates that actually hold up" in r["transcript"])
        _check("the embed page's date is read from its own JSON blob", r["published"] == "2026-07-14")
        r = watcher_ig.fetch_post("https://www.instagram.com/p/GONE12345/",
                                  fetcher=lambda u, ref="": (_ for _ in ()).throw(
                                      watcher_ig.FetchError(404)))
        _check("a deleted post is a PERMANENT error (never retried)",
               r["ok"] is False and r["permanent"] is True)
        r = watcher_ig.fetch_post("https://www.instagram.com/p/BUSY12345/",
                                  fetcher=lambda u, ref="": (_ for _ in ()).throw(
                                      watcher_ig.FetchError(429)))
        _check("throttling is transient and worded so the shared retry loop backs off",
               r["ok"] is False and r["permanent"] is False and "rate-limiting" in r["error"])
        _check("a non-Instagram link is rejected outright",
               watcher_ig.fetch_post("https://example.com/x")["permanent"] is True)

    # --- Batch fetching (the contract the page's Fetch-missing loop depends on) -------------------
    with _IgSession("fake-session"):
        entries = [watcher_ig.post_entry({"id": i["code"], "caption": i["caption"]["text"]})
                   for i in _IG_FEED["items"]]
        total = {}
        fetched, blocked = watcher_ig.fetch_posts_batch(
            entries, pause=0, json_fetcher=_ig_json, media_fetcher=_ig_media_bytes,
            transcriber=_ig_transcriber, usage_out=total)
        _check("fetch_posts_batch fills every pending post",
               fetched == 3 and blocked is False and all(e["transcript"] for e in entries))
        _check("batch usage ACCUMULATES across the reels (not just the last one)",
               total["input_tokens"] == 2700 and total["calls"] == 3)
        # 🔴 The wall-clock budget exists because the archive is written only AFTER the batch
        # returns: a batch that outlives Cloud Run's request timeout would discard every transcript
        # it had already paid Gemini for. A spent budget must BANK the finished item, not drop it.
        slow = [watcher_ig.post_entry({"id": i["code"]}) for i in _IG_FEED["items"]]
        fetched, blocked = watcher_ig.fetch_posts_batch(
            slow, pause=0, budget_seconds=-1, json_fetcher=_ig_json,
            media_fetcher=_ig_media_bytes, transcriber=_ig_transcriber)
        _check("a spent time budget stops the batch but KEEPS the item it just fetched",
               fetched == 1 and blocked is False and slow[0]["transcript"]
               and not slow[1]["transcript"])
        _check("...and the unfetched rest stay PENDING, so the page's loop resumes on them",
               all(e["error"] == "" for e in slow[1:]))
        blk = [watcher_ig.post_entry({"id": "IGblocked001"}),
               watcher_ig.post_entry({"id": "IGblocked002"})]
        fetched, blocked = watcher_ig.fetch_posts_batch(
            blk, pause=0, json_fetcher=lambda u, r="": (_ for _ in ()).throw(
                watcher_ig.FetchError(429)),
            fetcher=lambda u, r="": (_ for _ in ()).throw(watcher_ig.FetchError(429)))
        _check("a throttled Instagram stops the batch WITHOUT poisoning posts",
               fetched == 0 and blocked is True and all(e["error"] == "" for e in blk))

    # --- Routes: add_profile -> fetch -> read -> refresh -> delete (seams injected) ---------------
    real_resolve, real_list, real_fetch = (watcher_ig.resolve_profile, watcher_ig.list_posts,
                                           watcher_ig.fetch_post)
    watcher_ig.resolve_profile = lambda url, json_fetcher=None, fetcher=None: real_resolve(
        url, json_fetcher=_ig_json)
    watcher_ig.list_posts = lambda user, json_fetcher=None, fetcher=None: real_list(
        user, json_fetcher=_ig_json)
    watcher_ig.fetch_post = lambda url, **kw: real_fetch(
        url, json_fetcher=_ig_json, media_fetcher=_ig_media_bytes, transcriber=_ig_transcriber,
        usage_out=kw.get("usage_out"))
    session = _IgSession("fake-session")
    session.__enter__()
    try:
        r = c.post("/w/%s/admin/watcher" % CLIENT,
                   data={"op": "add_profile", "url": "https://www.instagram.com/greenlawco/"})
        data = r.get_json()
        _check("op=add_profile lists every post on the account", data["ok"] and data["posts"] == 4)
        ig_chan = data["channel"]
        ch = workspace.find_watcher_channel(workspace.load_workspace(CLIENT), ig_chan)
        _check("the account registers as an `instagram` platform keyed by its @handle",
               ch["platform"] == "instagram" and ch["channel_id"] == _IG_USER
               and ch["video_count"] == 4)
        _check("op=add_profile refuses the same account twice",
               c.post("/w/%s/admin/watcher" % CLIENT,
                      data={"op": "add_profile",
                            "url": "instagram.com/greenlawco"}).get_json()["ok"] is False)
        _check("op=add_profile rejects a link that isn't a profile",
               c.post("/w/%s/admin/watcher" % CLIENT,
                      data={"op": "add_profile", "url": "example.com"}).get_json()["ok"] is False)

        body = c.get("/w/%s/watcher" % CLIENT).get_data(as_text=True)
        _check("the Instagram card renders with INSTAGRAM wording, not video or blog wording",
               "Green Law Co" in body and "Open profile" in body and "4 posts" in body
               and "Caption not fetched yet." in body)
        _check("the platform filter offers Instagram", ">Instagram<" in body)
        _check("Safe pull is NOT offered on an Instagram card (the local scraper can't fetch it)",
               ('data-wtsafe="%s"' % ig_chan) not in body)
        _check("op=safe_pull is refused for Instagram (with a helpful message)",
               c.post("/w/%s/admin/watcher" % CLIENT,
                      data={"op": "safe_pull", "channel_id": ig_chan}).get_json()["ok"] is False)

        r = c.post("/w/%s/admin/watcher" % CLIENT, data={"op": "fetch", "channel_id": ig_chan})
        data = r.get_json()
        _check("op=fetch pulls captions + reel transcripts for an Instagram channel",
               data["ok"] and data["done"] == 4 and data["remaining"] == 0)
        posts = workspace.read_watcher_videos(CLIENT, ig_chan)
        _check("the caption AND the spoken words landed in the archive",
               all(p["transcript"] for p in posts)
               and any("signing the other side's version" in p["transcript"] for p in posts))
        tally = workspace.assistant_usage(workspace.load_workspace(CLIENT))
        _check("the reels' Gemini spend was banked into the client's Assistant tally",
               tally["input_tokens"] >= 900 and tally["cost_usd"] > 0)

        r = c.get("/w/%s/watcher/video/%s/%s" % (CLIENT, ig_chan, posts[0]["id"]))
        data = r.get_json()
        _check("the reader GET serves the FULL post text and says it is Instagram",
               data["ok"] and data["platform"] == "instagram"
               and "Three NDA mistakes" in data["transcript"])

        r = c.post("/w/%s/admin/watcher" % CLIENT, data={"op": "refresh", "channel_id": ig_chan})
        _check("op=refresh on an account adds nothing when it has not posted",
               r.get_json()["ok"] is True and r.get_json()["new"] == 0)
        _check("refresh kept the fetched text",
               all(p["transcript"] for p in workspace.read_watcher_videos(CLIENT, ig_chan)))

        # --- ONE box, THREE sources: an instagram.com link is scraped as a single post ------------
        r = c.post("/w/%s/admin/watcher" % CLIENT,
                   data={"op": "add_video", "url": "https://www.instagram.com/reel/IGnewest0001/"})
        data = r.get_json()
        _check("op=add_video auto-detects an INSTAGRAM link and scrapes just that post",
               data["ok"] and data["platform"] == "instagram" and data["spoken"] is True
               and data["already"] is False)
        loose_ig = next(ch for ch in workspace.watcher_channels(workspace.load_workspace(CLIENT))
                        if ch.get("loose") and ch.get("platform") == "instagram")
        _check("it saved under a SEPARATE 'Saved posts' loose channel",
               loose_ig["title"] == workspace.LOOSE_IG_TITLE and loose_ig["transcript_count"] == 1)
        r = c.post("/w/%s/admin/watcher" % CLIENT,
                   data={"op": "add_video", "url": "https://www.instagram.com/p/IGnewest0001/"})
        _check("re-scraping the same post de-dupes",
               r.get_json()["already"] is True
               and len(workspace.read_watcher_videos(CLIENT, loose_ig["id"])) == 1)

        # --- Team-only gating ---------------------------------------------------------------------
        with c.session_transaction() as s:
            s.clear()
            s.update(CLIENT_LOGIN)
        _check("a client cannot add an Instagram account", c.post(
            "/w/%s/admin/watcher" % CLIENT,
            data={"op": "add_profile", "url": "instagram.com/x"}).status_code == 403)
        with c.session_transaction() as s:
            s.clear()
            s.update(SUPER)

        # --- The internal (Academy) bridge exposes add_profile, and still refuses curation ---------
        _check("the internal bridge accepts add_profile",
               "add_profile" in main._INTERNAL_WATCHER_ADD_OPS)
        for banned in ("delete", "meta", "label", "safe_pull"):
            _check("the internal bridge still refuses %s" % banned,
                   banned not in main._INTERNAL_WATCHER_ADD_OPS)

        r = c.post("/w/%s/admin/watcher" % CLIENT, data={"op": "delete", "channel_id": ig_chan})
        _check("op=delete removes the Instagram archive", r.get_json()["ok"] is True)
        workspace.delete_watcher_channel(CLIENT, loose_ig["id"])
    finally:
        session.__exit__()
        watcher_ig.resolve_profile, watcher_ig.list_posts = real_resolve, real_list
        watcher_ig.fetch_post = real_fetch


def _run_lazy_pane_checks(c):
    """The Watcher pane is LAZY -- rendered, and its archives read, ONLY on the Watcher tab.

    🔴 This guards a PERFORMANCE contract, which is why it counts reads instead of only checking
    markup. Building the pane calls read_watcher_videos once per watched source, and each call
    downloads that source's whole archive object (transcripts run to megabytes). It used to run on
    EVERY team render, so opening Dashboard downloaded every archive to build a pane it never showed
    -- and since Atrium HTML is no-store, every refresh paid it again.

    Three things have to stay in step or the tab breaks in one of two ways:
      * main.atrium passes `watcher=[]` off-tab      -- pane rendered without data => BLANK tab
      * atrium.html renders the pane only when active
      * the #ax-nav click handler skips preventDefault for an absent pane => otherwise DEAD LINK
    All three are asserted below."""
    with c.session_transaction() as s:
        s.clear()
        s.update(SUPER)

    # A hand-added source WITH a stored archive, built straight through the workspace helpers -- no
    # fetcher stubs needed, since we only care that the render does or doesn't read the object.
    entry = workspace.add_watcher_channel(CLIENT, {
        "url": "https://www.youtube.com/@lazycheck", "title": "Lazy Check",
        "channel_id": "UC" + "z" * 22, "platform": "youtube"})
    workspace.write_watcher_videos(CLIENT, entry["id"], [
        {"id": "vidlazy0001", "title": "LAZYMARKERVIDEO", "url": "",
         "transcript": "a transcript body " * 20}])

    real_read = workspace.read_watcher_videos
    reads = []

    def _counting_read(client, channel_id):
        reads.append(channel_id)
        return real_read(client, channel_id)

    workspace.read_watcher_videos = _counting_read
    try:
        body = c.get("/w/%s/dashboard" % CLIENT).get_data(as_text=True)
        _check("a NON-Watcher tab reads ZERO archive objects (was one per watched source)",
               reads == [])
        _check("the Watcher pane is absent from a non-Watcher render",
               'data-pane="watcher"' not in body)
        _check("...but its nav LINK still ships, so the tab stays reachable",
               ("/w/%s/watcher" % CLIENT) in body)
        _check("the nav handler NAVIGATES for an absent pane (no preventDefault => no dead link)",
               """if (!document.querySelector('[data-pane="' + aq(tab) + '"]')) { return; }"""
               in body)
        _check("the archive marker never leaks into a non-Watcher render",
               "LAZYMARKERVIDEO" not in body)

        del reads[:]
        body = c.get("/w/%s/watcher" % CLIENT).get_data(as_text=True)
        _check("the Watcher tab DOES read its archives and render the cards",
               entry["id"] in reads and 'data-pane="watcher"' in body
               and "LAZYMARKERVIDEO" in body)
    finally:
        workspace.read_watcher_videos = real_read
        workspace.delete_watcher_channel(CLIENT, entry["id"])


def _run_template_checks():
    """The Watcher source TEMPLATE: the shared-archive redirect, and the reconcile's four invariants
    (additive, idempotent, opt-out is permanent, hand-added wins). Pure data layer -- no network."""
    import watcher_template

    # --- The catalog (pure) ----------------------------------------------------------------------
    universal = watcher_template.sources_for(watcher_template.UNIVERSAL)
    _check("catalog: the universal segment is non-empty", len(universal) > 0)
    _check("catalog: every source is a blog (YouTube would swamp Safe pull -- see the module doc)",
           all(s["platform"] == "blog" for s in watcher_template.catalog()))
    _check("catalog: every kind stays in the UI's two-state creator|competitor toggle",
           all(s["kind"] in ("creator", "competitor") for s in watcher_template.catalog()))
    _check("catalog: template_ids are unique (they are the reconcile key)",
           len({s["template_id"] for s in watcher_template.catalog()})
           == len(watcher_template.catalog()))
    _check("catalog: sources_for returns COPIES (the catalog can't be mutated through it)",
           universal[0] is not watcher_template.sources_for(watcher_template.UNIVERSAL)[0])

    travel = watcher_template.segments_for("RV resort & campground")
    _check("segments: a free-text industry matches its segment AND still gets universal",
           "travel-hospitality" in travel and watcher_template.UNIVERSAL in travel)
    _check("segments: an unrecognised industry still gets the universal set",
           watcher_template.segments_for("Wholesale plumbing") == (watcher_template.UNIVERSAL,))
    _check("segments: a blank industry (a day-one workspace) still gets the universal set",
           watcher_template.segments_for("") == (watcher_template.UNIVERSAL,))
    _check("segments: an industry segment adds sources on top of universal",
           len(watcher_template.sources_for(travel)) > len(universal))

    # --- The shared-archive redirect: ONE object serves every client ------------------------------
    shared_id = workspace.shared_channel_id("search-engine-land")
    _check("shared id: deterministic and prefixed",
           shared_id == workspace.shared_channel_id("search-engine-land")
           and workspace.is_shared_channel_id(shared_id))
    _check("shared id: a normal per-client entry id is NOT shared",
           not workspace.is_shared_channel_id("wch_1a2b3c4d"))
    _check("shared archive resolves to the HOUSE workspace from ANY client",
           workspace.watcher_object_name("riverdance", shared_id)
           == workspace.watcher_object_name("someotherclient", shared_id)
           == "%swatcher/%s/%s.json" % (workspace._prefix(), workspace.HOUSE_CLIENT, shared_id))
    _check("a per-client archive still resolves under its own client",
           workspace.watcher_object_name("riverdance", "wch_9f9f9f9f")
           == "%swatcher/riverdance/wch_9f9f9f9f.json" % workspace._prefix())

    # --- The reconcile, on a throwaway client ----------------------------------------------------
    tpl_client = "tplclient"
    workspace.save_workspace(tpl_client, {"version": 1, "client": tpl_client,
                                          "display_name": "Template Client"})
    src = watcher_template.sources_for(watcher_template.UNIVERSAL)

    pending = workspace.pending_watcher_template(workspace.load_workspace(tpl_client), src)
    _check("reconcile: a fresh client is missing every universal source", len(pending) == len(src))

    added = workspace.apply_watcher_template(tpl_client, src, watcher_template.TEMPLATE_VERSION)
    ws = workspace.load_workspace(tpl_client)
    _check("reconcile: applying adds every source once", len(added) == len(src)
           and len(workspace.watcher_channels(ws)) == len(src))
    _check("reconcile: shared sources got their deterministic house ids",
           all(workspace.is_shared_channel_id(e["id"]) for e in added if e["template_id"]))
    _check("reconcile: the version + applied_at stamp is written",
           workspace.watcher_template_state(ws)["version"] == watcher_template.TEMPLATE_VERSION
           and workspace.watcher_template_state(ws)["applied_at"])

    # Idempotent: re-running the same catalog is a no-op.
    _check("reconcile: nothing is pending after applying (pure pre-check is honest)",
           workspace.pending_watcher_template(ws, src) == [])
    _check("reconcile: re-applying adds NOTHING (idempotent)",
           workspace.apply_watcher_template(tpl_client, src, watcher_template.TEMPLATE_VERSION) == []
           and len(workspace.watcher_channels(workspace.load_workspace(tpl_client))) == len(src))

    # Additive: a hand-added source survives a reconcile untouched.
    hand = workspace.add_watcher_channel(tpl_client, {
        "url": "https://example.com/blog", "title": "Hand added", "channel_id": "https://example.com",
        "platform": "blog", "kind": "competitor"})
    workspace.apply_watcher_template(tpl_client, src, watcher_template.TEMPLATE_VERSION)
    ws = workspace.load_workspace(tpl_client)
    _check("reconcile: a hand-added source is never touched",
           workspace.find_watcher_channel(ws, hand["id"]) is not None
           and workspace.find_watcher_channel(ws, hand["id"])["template_id"] == "")

    # Hand-added wins: the template never plants a duplicate of a site the team beat us to.
    beat_us = dict(src[0])
    dup_ws = {"version": 1, "client": "dupclient", "watcher": {"channels": [
        {"id": "wch_manual01", "channel_id": beat_us["channel_id"], "title": "Added by hand first",
         "platform": "blog", "template_id": ""}]}}
    _check("reconcile: a source already added BY HAND is not duplicated by the template",
           all(p["template_id"] != beat_us["template_id"]
               for p in workspace.pending_watcher_template(dup_ws, src)))

    # Opt-out is permanent: deleting a template source must not resurrect it.
    victim = next(e for e in workspace.watcher_channels(ws) if e.get("template_id"))
    workspace.delete_watcher_channel(tpl_client, victim["id"])
    ws = workspace.load_workspace(tpl_client)
    _check("delete: removing a template source records the opt-out",
           victim["template_id"] in workspace.watcher_template_state(ws)["removed"])
    workspace.apply_watcher_template(tpl_client, src, watcher_template.TEMPLATE_VERSION)
    ws = workspace.load_workspace(tpl_client)
    _check("delete: the reconcile NEVER re-adds an opted-out source",
           workspace.find_watcher_channel(ws, victim["id"]) is None
           and all(ch.get("template_id") != victim["template_id"]
                   for ch in workspace.watcher_channels(ws)))

    # Deleting a shared source from one client must not destroy the house archive.
    keeper = next(e for e in workspace.watcher_channels(ws) if e.get("template_id"))
    workspace.write_watcher_videos(tpl_client, keeper["id"],
                                   [{"id": "p1", "transcript": "house copy"}])
    house_path = os.path.join(_TMP, workspace.watcher_object_name(tpl_client, keeper["id"]))
    _check("shared archive was written to the house path", os.path.exists(house_path))
    workspace.delete_watcher_channel(tpl_client, keeper["id"])
    _check("delete: a SHARED archive survives (it still serves every other client)",
           os.path.exists(house_path)
           and workspace.read_watcher_videos("someotherclient", keeper["id"])[0]["transcript"]
           == "house copy")

    # A per-client archive, by contrast, IS deleted with its entry (unchanged behaviour).
    solo = workspace.add_watcher_channel(tpl_client, {
        "url": "https://solo.example/blog", "title": "Solo", "channel_id": "https://solo.example",
        "platform": "blog"})
    workspace.write_watcher_videos(tpl_client, solo["id"], [{"id": "s1", "transcript": "mine"}])
    solo_path = os.path.join(_TMP, workspace.watcher_object_name(tpl_client, solo["id"]))
    workspace.delete_watcher_channel(tpl_client, solo["id"])
    _check("delete: a PER-CLIENT archive is still removed with its entry",
           not os.path.exists(solo_path))

    # --- Onboarding applies the template automatically --------------------------------------------
    import onboard_client
    new_key = "brandnewclient"
    onboard_client.onboard(new_key, "Brand New Client")
    new_ws = workspace.load_workspace(new_key)
    _check("onboarding: a brand-new client's Watcher is pre-seeded with the universal sources",
           len(workspace.watcher_channels(new_ws)) == len(src))
    _check("onboarding: those entries are marked template-sourced",
           all(ch.get("template_id") for ch in workspace.watcher_channels(new_ws)))

    # --- The industry segment unlocks on the first render after the Company tab is filled ---------
    workspace.set_company_profile(new_key, {"industry": "RV resort & campground"})
    ind_ws = workspace.load_workspace(new_key)
    _check("reconcile: filling the Company industry makes the industry sources pending",
           len(workspace.pending_watcher_template(
               ind_ws, watcher_template.sources_for(
                   watcher_template.segments_for(main._client_industry(ind_ws))))) > 0)
    fresh = main._watcher_reconcile(new_key, ind_ws)
    _check("reconcile: _watcher_reconcile adds them and returns the RE-READ workspace",
           len(workspace.watcher_channels(fresh))
           == len(watcher_template.sources_for(watcher_template.segments_for("rv resort"))))
    _check("reconcile: a second call is a no-op and returns the same workspace object",
           main._watcher_reconcile(new_key, fresh) is fresh)


def run():
    seed_workspace.seed(register_client=False)
    main.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False, SESSION_COOKIE_SAMESITE="Lax")
    c = main.app.test_client()

    # --- watcher.py: channel resolution + playlist paging (injected fetchers, no network) --------
    info = watcher.resolve_channel("@datawithdana", fetcher=lambda url: _CHANNEL_HTML)
    _check("resolve_channel finds id + title",
           info["ok"] and info["channel_id"] == _CHANNEL_ID and info["title"] == "Data With Dana & Co")
    _check("resolve_channel rejects a non-youtube link",
           watcher.resolve_channel("https://example.com/foo")["ok"] is False)

    pages = _browse_pages()

    def poster(url, payload):
        return pages[payload.get("continuation", "first")]

    listing = watcher.list_videos(_CHANNEL_ID, poster=poster)
    _check("list_videos pages + de-dupes (3 unique videos)",
           listing["ok"] and [v["id"] for v in listing["videos"]]
           == ["vid00000001", "vid00000002", "vid00000003"])
    _check("list_videos rejects a bad id", watcher.list_videos("nope")["ok"] is False)
    _check("lockup upload age captured", listing["videos"][2]["published_text"] == "2 weeks ago")

    # --- watcher.py: single-video id extraction + oEmbed title resolution (injected fetcher) ------
    _check("extract_video_id: watch?v=", watcher.extract_video_id(
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=5s") == "dQw4w9WgXcQ")
    _check("extract_video_id: youtu.be", watcher.extract_video_id(
        "https://youtu.be/dQw4w9WgXcQ?si=abc") == "dQw4w9WgXcQ")
    _check("extract_video_id: /shorts/", watcher.extract_video_id(
        "https://www.youtube.com/shorts/dQw4w9WgXcQ") == "dQw4w9WgXcQ")
    _check("extract_video_id: bare id", watcher.extract_video_id("dQw4w9WgXcQ") == "dQw4w9WgXcQ")
    _check("extract_video_id: channel link has no video id",
           watcher.extract_video_id("https://www.youtube.com/@datawithdana") == "")
    rv = watcher.resolve_video("https://youtu.be/dQw4w9WgXcQ",
                               fetcher=lambda url: '{"title": "A & B talk", "author_name": "Dana"}')
    _check("resolve_video reads oEmbed title/author",
           rv["ok"] and rv["video_id"] == "dQw4w9WgXcQ" and rv["title"] == "A & B talk"
           and rv["url"] == "https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    # oEmbed 401s (embedding-restricted) but the watch page still carries the real title -> scrape it.
    def _oembed_401_page_ok(url):
        if "/oembed" in url:
            raise IOError("401")
        return ('<meta property="og:title" content="Learn Email Marketing in 39 Minutes!">'
                '"ownerChannelName":"Alex Hormozi"')
    rv = watcher.resolve_video("pLhQOYMGa88", fetcher=_oembed_401_page_ok)
    _check("resolve_video falls back to watch-page og:title when oEmbed fails",
           rv["ok"] and rv["title"] == "Learn Email Marketing in 39 Minutes!"
           and rv["author"] == "Alex Hormozi")
    rv = watcher.resolve_video("dQw4w9WgXcQ", fetcher=lambda url: (_ for _ in ()).throw(IOError()))
    _check("resolve_video degrades to id title when both oEmbed and the page fail",
           rv["ok"] and rv["title"] == "Video dQw4w9WgXcQ")
    _check("resolve_video rejects a non-video link",
           watcher.resolve_video("https://example.com/foo")["ok"] is False)

    import datetime as _dt
    _now = _dt.datetime(2026, 7, 12, tzinfo=_dt.timezone.utc)
    _check("published_estimate: weeks", watcher.published_estimate("2 weeks ago", _now) == "2026-06-28")
    _check("published_estimate: years",
           watcher.published_estimate("Streamed 1 year ago", _now) in ("2025-07-11", "2025-07-12"))
    _check("published_estimate: garbage is empty", watcher.published_estimate("hello") == "")

    # A rate-limit is a session condition: the batch stops, reports blocked, and NO video is
    # marked failed -- the next fetch resumes over the exact same missing set.
    real_fetch_fn = watcher.fetch_transcript
    watcher.fetch_transcript = lambda vid: {
        "ok": False, "transcript": "", "language": "", "generated": False,
        "error": "YouTube is rate-limiting or blocking this server right now — re-run later.",
        "permanent": False}
    blocked_vids = [{"id": "a", "transcript": "", "error": ""}, {"id": "b", "transcript": "", "error": ""}]
    n, blocked = watcher.fetch_transcripts_batch(blocked_vids, pause=0)
    _check("rate-limit stops the batch WITHOUT poisoning videos",
           n == 0 and blocked is True and blocked_vids[0]["error"] == "" and blocked_vids[1]["error"] == "")
    watcher.fetch_transcript = real_fetch_fn

    # --- Parallel fetch path (proxy-gated concurrency for the Cloud Run "Fetch missing" loop) -----
    _saved_proxy = os.environ.get("WATCHER_PROXY_URL")
    os.environ["WATCHER_PROXY_URL"] = "http://user-rotate:pw@p.example:80"
    _check("proxied() is True when WATCHER_PROXY_URL is set", watcher.proxied() is True)

    # workers>1 fetches the whole batch concurrently; each distinct dict gets its OWN transcript --
    # proves the thread pool's results are applied to the right video on the main thread.
    watcher.fetch_transcript = lambda vid: {
        "ok": True, "transcript": "t-" + vid, "language": "en", "generated": False,
        "error": "", "permanent": False}
    par_vids = [{"id": "v%02d" % i, "transcript": "", "error": ""} for i in range(20)]
    n, blocked = watcher.fetch_transcripts_batch(par_vids, limit=40, workers=8)
    _check("parallel path fetches the whole batch concurrently, results routed correctly",
           n == 20 and blocked is False and all(v["transcript"] == "t-" + v["id"] for v in par_vids))

    cap_vids = [{"id": "c%02d" % i, "transcript": "", "error": ""} for i in range(20)]
    n, _b = watcher.fetch_transcripts_batch(cap_vids, limit=5, workers=8)
    _check("parallel path honors the batch limit",
           n == 5 and sum(1 for v in cap_vids if v["transcript"]) == 5)

    # A stray rate-limit on SOME rotating IPs must NOT stop the run: the good ones land, the
    # rate-limited stay pending (unpoisoned), blocked stays False because progress was made.
    def _mixed(vid):
        if vid.endswith(("1", "3")):
            return {"ok": False, "transcript": "", "language": "", "generated": False,
                    "error": "YouTube is rate-limiting or blocking this server right now.",
                    "permanent": False}
        return {"ok": True, "transcript": "t-" + vid, "language": "en", "generated": False,
                "error": "", "permanent": False}
    watcher.fetch_transcript = _mixed
    mix_vids = [{"id": "m%d" % i, "transcript": "", "error": ""} for i in range(6)]
    n, blocked = watcher.fetch_transcripts_batch(mix_vids, workers=8)
    still_pending = [v for v in mix_vids if not v["transcript"] and not v["error"]]
    _check("parallel: a partial rate-limit keeps the run going and leaves stragglers pending",
           n == 4 and blocked is False and len(still_pending) == 2
           and all(v["id"] in ("m1", "m3") for v in still_pending))

    # Only when the WHOLE batch is rate-limited does parallel report blocked -- and poisons nothing.
    watcher.fetch_transcript = lambda vid: {
        "ok": False, "transcript": "", "language": "", "generated": False,
        "error": "YouTube is rate-limiting or blocking this server right now.", "permanent": False}
    allblk = [{"id": "z%d" % i, "transcript": "", "error": ""} for i in range(6)]
    n, blocked = watcher.fetch_transcripts_batch(allblk, workers=8)
    _check("parallel: a whole-batch rate-limit reports blocked and poisons nothing",
           n == 0 and blocked is True and all(v["error"] == "" for v in allblk))

    watcher.fetch_transcript = real_fetch_fn
    if _saved_proxy is None:
        os.environ.pop("WATCHER_PROXY_URL", None)
        _check("proxied() is False when WATCHER_PROXY_URL is unset", watcher.proxied() is False)
    else:
        os.environ["WATCHER_PROXY_URL"] = _saved_proxy

    # --- watcher.py: transcript fetch error paths (package stubbed, no network) ------------------
    real_import = watcher._import_transcript_api

    def _raise_import():
        raise ImportError("not installed")

    watcher._import_transcript_api = _raise_import
    r = watcher.fetch_transcript("vid00000001")
    _check("missing package degrades to a friendly error",
           r["ok"] is False and "not installed" in r["error"])

    class _Track:
        language_code = "en"
        is_generated = False

        def fetch(self):
            return [{"text": "hello"}, {"text": "world  again"}]

    class _Api1x:  # the 1.x instance API surface
        def __init__(self, *a, **k):
            pass

        def list(self, vid):
            return [_Track()]

    fake = types.ModuleType("youtube_transcript_api")
    fake.YouTubeTranscriptApi = _Api1x
    watcher._import_transcript_api = lambda: fake
    r = watcher.fetch_transcript("vid00000001")
    _check("stubbed 1.x API returns normalized text",
           r["ok"] and r["transcript"] == "hello world again" and r["language"] == "en")

    class _Disabled(Exception):
        pass

    _Disabled.__name__ = "TranscriptsDisabled"

    class _ApiRaises(_Api1x):
        def list(self, vid):
            raise _Disabled()

    fake.YouTubeTranscriptApi = _ApiRaises
    r = watcher.fetch_transcript("vid00000001")
    _check("disabled subtitles is a PERMANENT error", r["ok"] is False and r["permanent"] is True)
    watcher._import_transcript_api = real_import

    # --- workspace.py: registry + per-channel archive object -------------------------------------
    entry = workspace.add_watcher_channel(CLIENT, {"url": "u", "title": "T", "channel_id": _CHANNEL_ID,
                                                   "video_count": 3})
    _check("channel registered", workspace.find_watcher_channel(
        workspace.load_workspace(CLIENT), entry["id"])["title"] == "T")
    marker = "TRANSCRIPT-MARKER-93f1"
    workspace.write_watcher_videos(CLIENT, entry["id"], [{"id": "v1", "transcript": marker}])
    _check("archive object round-trips",
           workspace.read_watcher_videos(CLIENT, entry["id"])[0]["transcript"] == marker)
    obj_path = os.path.join(_TMP, workspace.watcher_object_name(CLIENT, entry["id"]))
    _check("archive is its OWN object (not in the workspace JSON)",
           os.path.isfile(obj_path)
           and marker not in open(os.path.join(_TMP, "workspace", CLIENT + ".json")).read())
    workspace.delete_watcher_channel(CLIENT, entry["id"])
    _check("delete removes registry entry + object",
           _hand_channels() == [] and not os.path.isfile(obj_path))

    # --- Routes: add -> fetch -> expand -> refresh -> delete (fetchers stubbed) ------------------
    with c.session_transaction() as s:
        s.update(SUPER)

    real_resolve, real_list, real_fetch = (watcher.resolve_channel, watcher.list_videos,
                                           watcher.fetch_transcript)
    watcher.resolve_channel = lambda url, fetcher=None: {
        "ok": True, "channel_id": _CHANNEL_ID, "title": "Data With Dana",
        "url": "https://www.youtube.com/channel/" + _CHANNEL_ID, "error": ""}
    watcher.list_videos = lambda cid, poster=None: {"ok": True, "error": "", "videos": [
        {"id": "vid00000001", "title": "How to model churn"},
        {"id": "vid00000002", "title": "SQL window functions"}]}
    watcher.fetch_transcript = lambda vid: {
        "ok": True, "transcript": "transcript for " + vid, "language": "en",
        "generated": False, "error": "", "permanent": False}

    r = c.post("/w/%s/admin/watcher" % CLIENT, data={"op": "add", "url": "@datawithdana"})
    _check("op=add returns ok", r.status_code == 200 and r.get_json()["ok"] is True)
    chan = r.get_json()["channel"]
    r = c.post("/w/%s/admin/watcher" % CLIENT, data={"op": "add", "url": "@datawithdana"})
    _check("duplicate channel is refused", r.get_json()["ok"] is False)

    ch = workspace.find_watcher_channel(workspace.load_workspace(CLIENT), chan)
    _check("channel classified with defaults (youtube creator, no AI -> empty industry)",
           ch["platform"] == "youtube" and ch["kind"] == "creator" and ch["industry"] == "")

    # Hand-edit the classification, then flip it via the AI label op (AI stubbed).
    r = c.post("/w/%s/admin/watcher" % CLIENT,
               data={"op": "meta", "channel_id": chan, "industry": "Data Science", "kind": "competitor"})
    ch = workspace.find_watcher_channel(workspace.load_workspace(CLIENT), chan)
    _check("op=meta sets industry + kind",
           r.get_json()["ok"] is True and ch["industry"] == "Data Science" and ch["kind"] == "competitor")
    _check("op=meta rejects a bogus kind",
           c.post("/w/%s/admin/watcher" % CLIENT,
                  data={"op": "meta", "channel_id": chan, "kind": "frenemy"}).get_json()["ok"] is False)
    real_autolabel = main._watcher_autolabel
    main._watcher_autolabel = lambda title, titles: ("AI Automation", "")
    r = c.post("/w/%s/admin/watcher" % CLIENT, data={"op": "label", "channel_id": chan})
    ch = workspace.find_watcher_channel(workspace.load_workspace(CLIENT), chan)
    _check("op=label re-runs the AI industry label",
           r.get_json()["industry"] == "AI Automation" and ch["industry"] == "AI Automation")
    main._watcher_autolabel = real_autolabel
    body = c.get("/w/%s/watcher" % CLIENT).get_data(as_text=True)
    _check("filter bar + creator grid render (industry option present)",
           'id="ax-wt-fsearch"' in body and 'id="ax-wt-cgrid"' in body and "AI Automation" in body)

    body = c.get("/w/%s/watcher" % CLIENT).get_data(as_text=True)
    _check("watcher tab renders pending cards",
           "How to model churn" in body and "Transcript not fetched yet" in body)

    r = c.post("/w/%s/admin/watcher" % CLIENT, data={"op": "fetch", "channel_id": chan})
    data = r.get_json()
    _check("op=fetch pulls both transcripts", data["ok"] and data["done"] == 2 and data["remaining"] == 0)
    ch = workspace.find_watcher_channel(workspace.load_workspace(CLIENT), chan)
    _check("registry counts updated", ch["transcript_count"] == 2 and ch["failed_count"] == 0)

    r = c.get("/w/%s/watcher/video/%s/vid00000001" % (CLIENT, chan))
    _check("expand GET serves the FULL transcript",
           r.status_code == 200 and r.get_json()["transcript"] == "transcript for vid00000001")

    watcher.list_videos = lambda cid, poster=None: {"ok": True, "error": "", "videos": [
        {"id": "vid00000009", "title": "NEW upload"},
        {"id": "vid00000001", "title": "How to model churn"},
        {"id": "vid00000002", "title": "SQL window functions"}]}
    r = c.post("/w/%s/admin/watcher" % CLIENT, data={"op": "refresh", "channel_id": chan})
    _check("op=refresh adds only the new video", r.get_json()["new"] == 1)
    vids = workspace.read_watcher_videos(CLIENT, chan)
    _check("new video is prepended, old transcripts kept",
           vids[0]["id"] == "vid00000009" and vids[1]["transcript"] == "transcript for vid00000001")

    # --- Single-video scraper: op=add_video saves under a "Saved videos" loose channel -----------
    real_resolve_video = watcher.resolve_video
    watcher.resolve_video = lambda url: {
        "ok": True, "video_id": "loosevid001", "title": "One-off talk", "author": "Someone",
        "url": "https://www.youtube.com/watch?v=loosevid001", "error": ""}
    # fetch_transcript is still stubbed to succeed ("transcript for <vid>").
    r = c.post("/w/%s/admin/watcher" % CLIENT, data={"op": "add_video", "url": "https://youtu.be/loosevid001"})
    data = r.get_json()
    _check("op=add_video returns the fetched transcript",
           data["ok"] and data["transcript"] == "transcript for loosevid001"
           and data["words"] == 3 and data["already"] is False)
    loose = next(ch for ch in workspace.watcher_channels(workspace.load_workspace(CLIENT)) if ch.get("loose"))
    _check("a 'Saved videos' loose channel was created (no real channel_id)",
           loose["title"] == workspace.LOOSE_CHANNEL_TITLE and loose["channel_id"] == ""
           and loose["transcript_count"] == 1)
    lvids = workspace.read_watcher_videos(CLIENT, loose["id"])
    _check("the video landed in the loose archive with its transcript",
           len(lvids) == 1 and lvids[0]["id"] == "loosevid001"
           and lvids[0]["transcript"] == "transcript for loosevid001")
    r = c.post("/w/%s/admin/watcher" % CLIENT, data={"op": "add_video", "url": "loosevid001"})
    _check("re-scraping the same video de-dupes (already=True, still one loose video)",
           r.get_json()["already"] is True
           and len(workspace.read_watcher_videos(CLIENT, loose["id"])) == 1)
    # A rate-limit saves the video PENDING (no transcript, no error) so Safe pull can finish it later.
    watcher.resolve_video = lambda url: {
        "ok": True, "video_id": "blockedvid1", "title": "Blocked one", "author": "",
        "url": "https://www.youtube.com/watch?v=blockedvid1", "error": ""}
    saved_fetch = watcher.fetch_transcript
    watcher.fetch_transcript = lambda vid: {
        "ok": False, "transcript": "", "language": "", "generated": False,
        "error": "YouTube is rate-limiting or blocking this server right now — re-run later.",
        "permanent": False}
    r = c.post("/w/%s/admin/watcher" % CLIENT, data={"op": "add_video", "url": "blockedvid1"})
    data = r.get_json()
    _check("a rate-limited single video reports blocked with no transcript",
           data["ok"] and data["blocked"] is True and data["transcript"] == "")
    bv = next(v for v in workspace.read_watcher_videos(CLIENT, loose["id"]) if v["id"] == "blockedvid1")
    _check("blocked video stays pending (retryable, not marked failed)",
           bv["transcript"] == "" and bv["error"] == "")
    watcher.fetch_transcript = saved_fetch
    watcher.resolve_video = real_resolve_video
    _check("op=add_video rejects a link that isn't a URL at all",
           c.post("/w/%s/admin/watcher" % CLIENT,
                  data={"op": "add_video", "url": "just some words"}).get_json()["ok"] is False)
    # Clean the loose channel up so the later 'no channels left' assertion holds.
    workspace.delete_watcher_channel(CLIENT, loose["id"])

    # --- Safe pull: queue the channel for the local slow scraper ---------------------------------
    r = c.post("/w/%s/admin/watcher" % CLIENT, data={"op": "safe_pull", "channel_id": chan})
    _check("op=safe_pull queues the channel", r.get_json()["ok"] is True and
           workspace.watcher_safe_pull_queue(workspace.load_workspace(CLIENT)) == [chan])
    c.post("/w/%s/admin/watcher" % CLIENT, data={"op": "safe_pull", "channel_id": chan})
    _check("op=safe_pull is idempotent",
           workspace.watcher_safe_pull_queue(workspace.load_workspace(CLIENT)) == [chan])
    body = c.get("/w/%s/watcher" % CLIENT).get_data(as_text=True)
    _check("queued card renders the Safe-pull note", "Safe pull queued" in body)

    # --- Live Safe-pull status: queued counts + the local scraper's heartbeat --------------------
    st = c.get("/w/%s/watcher/safe-pull-status" % CLIENT).get_json()
    _check("safe-pull-status lists the queued channel with counts",
           st["ok"] and st["queued"] == [chan] and chan in st["channels"]
           and "done" in st["channels"][chan] and "total" in st["channels"][chan])
    _check("safe-pull-status agent absent before the scraper ever runs",
           st["agent"]["present"] is False and st["agent"]["active"] is False)
    # Simulate a fresh heartbeat from the local scraper fetching THIS channel right now.
    import json as _json
    workspace._write_object(workspace.safe_pull_status_name(), _json.dumps({
        "updated": workspace.now_iso(), "mode": "queue", "phase": "fetching", "client": CLIENT,
        "channel_id": chan, "channel_title": "Data With Dana", "current_video": "How to model churn",
        "done": 2, "pending": 3, "total": 5, "cooldown_until": "",
    }).encode("utf-8"))
    st = c.get("/w/%s/watcher/safe-pull-status" % CLIENT).get_json()
    _check("safe-pull-status reflects a live scraper heartbeat",
           st["agent"]["active"] is True and st["agent"]["on_this_client"] is True
           and st["agent"]["phase"] == "fetching"
           and st["agent"]["current_video"] == "How to model churn")
    workspace._delete_object(workspace.safe_pull_status_name())

    # --- The internal Watcher bridge (Sentinel's Mentor Library imports from this archive) --------
    # HMAC-gated, cross-workspace, and the channel id comes back namespaced "<client>:<channel_id>"
    # so the follow-up calls can find the archive object without the caller tracking workspaces.
    import hashlib as _hashlib
    import hmac as _hmac
    import time as _time
    real_secret = main.SSO_SECRET
    main.SSO_SECRET = "bridge-test-secret"
    store.add_client(CLIENT, "Riverdance RV")     # list_clients() is what the bridge iterates

    def _signed(purpose, path):
        ts = str(int(_time.time()))
        sig = _hmac.new(main.SSO_SECRET.encode(), ("%s:%s" % (purpose, ts)).encode(),
                        _hashlib.sha256).hexdigest()
        return c.get(path, headers={"X-Academy-Ts": ts, "X-Academy-Sig": sig})

    _check("unsigned internal watcher call is refused",
           c.get("/api/internal/watcher/channels").status_code == 401)
    _check("a wrong signature is refused",
           c.get("/api/internal/watcher/channels",
                 headers={"X-Academy-Ts": str(int(_time.time())),
                          "X-Academy-Sig": "0" * 64}).status_code == 401)

    chans = _signed("watcher-channels", "/api/internal/watcher/channels").get_json()["channels"]
    mine = [ch for ch in chans if ch["channel_key"] == chan]
    _check("channels come back namespaced <client>:<channel_id> with their workspace",
           len(mine) == 1 and mine[0]["id"] == "%s:%s" % (CLIENT, chan)
           and mine[0]["client_key"] == CLIENT and mine[0]["client_name"])
    _check("channel carries the counts + classification the picker renders",
           mine[0]["transcript_count"] >= 1 and mine[0]["video_count"] >= 1
           and mine[0]["kind"] and mine[0]["platform"] == "youtube")
    _check("an unrelated ?client= filter narrows it away",
           _signed("watcher-channels",
                   "/api/internal/watcher/channels?client=nobody").get_json()["channels"] == [])

    ns = "%s:%s" % (CLIENT, chan)
    vids = _signed("watcher-videos",
                   "/api/internal/watcher/videos?channel=%s" % ns).get_json()["videos"]
    fetched = [v for v in vids if v["has_transcript"]]
    _check("videos list is light (has_transcript + words, NO transcript body)",
           fetched and "transcript" not in fetched[0] and fetched[0]["words"] > 0)

    got = _signed("watcher-transcript", "/api/internal/watcher/transcript?channel=%s&video=%s"
                  % (ns, fetched[0]["id"])).get_json()
    _check("transcript returns the full text Sentinel copies in",
           got["ok"] and got["transcript"] and got["title"])
    pending = [v for v in vids if not v["has_transcript"]]
    if pending:
        _check("an unfetched item 404s (Sentinel says 'not available yet', imports nothing)",
               _signed("watcher-transcript", "/api/internal/watcher/transcript?channel=%s&video=%s"
                       % (ns, pending[0]["id"])).status_code == 404)
    _check("an unknown video 404s",
           _signed("watcher-transcript",
                   "/api/internal/watcher/transcript?channel=%s&video=nope" % ns).status_code == 404)
    _check("a channel with no workspace to resolve it is a 400, not a 500",
           _signed("watcher-videos",
                   "/api/internal/watcher/videos?channel=%s" % chan).status_code == 400)

    # Bulk: the whole channel in one call (what Sentinel's "Import all" uses).
    bulk = _signed("watcher-transcripts",
                   "/api/internal/watcher/transcripts?channel=%s" % ns).get_json()
    _check("bulk returns every FETCHED transcript, bodies included",
           bulk["ok"] and len(bulk["transcripts"]) == len(fetched)
           and all(t["transcript"] for t in bulk["transcripts"]))
    _check("bulk reports total + a next_offset of 0 when it is done",
           bulk["total"] == len(fetched) and bulk["next_offset"] == 0)
    _check("bulk skips unfetched items rather than emitting empty transcripts",
           len(bulk["transcripts"]) <= len(vids))
    # Squeeze the byte budget so paging actually engages, then walk it like the client does.
    real_budget = main._INTERNAL_TRANSCRIPTS_BUDGET
    main._INTERNAL_TRANSCRIPTS_BUDGET = 1        # 1 byte -> one item per page
    walked, off, hops = [], 0, 0
    while hops < 20:
        hops += 1
        page = _signed("watcher-transcripts",
                       "/api/internal/watcher/transcripts?channel=%s&offset=%d" % (ns, off)).get_json()
        walked.extend(page["transcripts"])
        off = page["next_offset"]
        if off <= 0:
            break
    _check("a squeezed budget pages instead of truncating (same items, never an empty page)",
           [t["id"] for t in walked] == [t["id"] for t in bulk["transcripts"]] and hops > 1)
    main._INTERNAL_TRANSCRIPTS_BUDGET = real_budget

    # --- The bridge's ONE write: the Academy adds a source without a browser session --------------
    # Same HMAC gate as the reads, and it runs the EXACT `_watcher_op_*` helpers the team console's
    # own form runs -- that shared path is the point, so a source added from the Academy is
    # indistinguishable from one the team pasted into Atrium.
    def _signed_post(purpose, path, payload):
        ts = str(int(_time.time()))
        sig = _hmac.new(main.SSO_SECRET.encode(), ("%s:%s" % (purpose, ts)).encode(),
                        _hashlib.sha256).hexdigest()
        return c.post(path, json=payload,
                      headers={"X-Academy-Ts": ts, "X-Academy-Sig": sig})

    _check("unsigned internal watcher ADD is refused",
           c.post("/api/internal/watcher/add",
                  json={"client": CLIENT, "op": "add", "url": "x"}).status_code == 401)
    _check("a wrong signature on the write is refused",
           c.post("/api/internal/watcher/add", json={"client": CLIENT, "op": "add", "url": "x"},
                  headers={"X-Academy-Ts": str(int(_time.time())),
                           "X-Academy-Sig": "0" * 64}).status_code == 401)
    _check("an op the bridge does not expose (delete) is a 400, never a silent success",
           _signed_post("watcher-add", "/api/internal/watcher/add",
                        {"client": CLIENT, "op": "delete", "channel": chan}).status_code == 400)
    _check("an unknown client is 404 (never a no-op that reads as 'added')",
           _signed_post("watcher-add", "/api/internal/watcher/add",
                        {"client": "nobody", "op": "add", "url": "@x"}).status_code == 404)
    _check("op=add with no url is a 400",
           _signed_post("watcher-add", "/api/internal/watcher/add",
                        {"client": CLIENT, "op": "add"}).status_code == 400)

    # Add a SECOND channel over the bridge and prove it lands in the same registry + archive.
    _BRIDGE_CID = "UC" + "b" * 22
    watcher.resolve_channel = lambda url, fetcher=None: {
        "ok": True, "channel_id": _BRIDGE_CID, "title": "Academy Adds This",
        "url": "https://www.youtube.com/channel/" + _BRIDGE_CID, "error": ""}
    watcher.list_videos = lambda cid, poster=None: {"ok": True, "error": "", "videos": [
        {"id": "vid00000009", "title": "Prompt engineering"}]}
    added = _signed_post("watcher-add", "/api/internal/watcher/add",
                         {"client": CLIENT, "op": "add", "url": "@academyadds",
                          "actor": "info@agoradatadriven.com"}).get_json()
    _check("op=add over the bridge reports the new channel + its listing size",
           added["ok"] is True and added["channel"] and added["videos"] == 1
           and added["title"] == "Academy Adds This")
    bridged = workspace.find_watcher_channel(workspace.load_workspace(CLIENT), added["channel"])
    _check("the bridged source is an ORDINARY registry entry (same shape the console writes)",
           bridged is not None and bridged["channel_id"] == _BRIDGE_CID
           and bridged["platform"] == "youtube" and bridged["kind"] == "creator")
    _check("bodies are NOT fetched by the add (the caller loops op=fetch, same as the tab)",
           workspace.read_watcher_videos(CLIENT, added["channel"])[0]["transcript"] == "")
    _check("the duplicate guard applies to the bridge too",
           _signed_post("watcher-add", "/api/internal/watcher/add",
                        {"client": CLIENT, "op": "add", "url": "@academyadds"}).get_json()["ok"]
           is False)

    filled = _signed_post("watcher-add", "/api/internal/watcher/add",
                          {"client": CLIENT, "op": "fetch", "channel": added["channel"]}).get_json()
    _check("op=fetch over the bridge fills the bodies and reports the loop's progress",
           filled["ok"] is True and filled["fetched"] == 1 and filled["remaining"] == 0
           and filled["total"] == 1 and filled["blocked"] is False)
    _check("the fetched transcript is in the archive object the Academy then reads",
           workspace.read_watcher_videos(CLIENT, added["channel"])[0]["transcript"]
           == "transcript for vid00000009")
    _check("op=fetch on a channel this workspace doesn't hold is a 404",
           _signed_post("watcher-add", "/api/internal/watcher/add",
                        {"client": CLIENT, "op": "fetch", "channel": "wch_nope"}).status_code == 404)

    # A single pasted link: the loose "Saved videos" channel, transcript fetched inline.
    _saved_resolve_video = watcher.resolve_video
    watcher.resolve_video = lambda url: {
        "ok": True, "video_id": "brdg0000001", "title": "One pasted talk",
        "url": "https://www.youtube.com/watch?v=brdg0000001", "author": "", "error": ""}
    one = _signed_post("watcher-add", "/api/internal/watcher/add",
                       {"client": CLIENT, "op": "add_video",
                        "url": "https://youtu.be/brdg0000001"}).get_json()
    _check("op=add_video over the bridge saves the video AND returns its text immediately",
           one["ok"] is True and one["video_id"] == "brdg0000001"
           and one["transcript"] == "transcript for brdg0000001" and one["blocked"] is False)
    _check("it landed in the per-client loose pseudo-channel, like the tab's own single-add",
           workspace.find_watcher_channel(workspace.load_workspace(CLIENT),
                                          one["channel"]).get("loose") is True)
    watcher.resolve_video = _saved_resolve_video
    for _c in (added["channel"], one["channel"]):
        workspace.delete_watcher_channel(CLIENT, _c)   # leave the registry as we found it

    main.SSO_SECRET = real_secret

    # --- Team-only gating: a client must never see or touch Watcher ------------------------------
    with c.session_transaction() as s:
        s.clear()
        s.update(CLIENT_LOGIN)
    body = c.get("/w/%s/watcher" % CLIENT).get_data(as_text=True)
    _check("client hitting /watcher is bounced (no watcher pane in the DOM)",
           'data-pane="watcher"' not in body and "How to model churn" not in body)
    _check("client POST is forbidden",
           c.post("/w/%s/admin/watcher" % CLIENT,
                  data={"op": "delete", "channel_id": chan}).status_code == 403)
    _check("client transcript GET is forbidden",
           c.get("/w/%s/watcher/video/%s/vid00000001" % (CLIENT, chan)).status_code == 403)
    _check("client safe-pull-status GET is forbidden",
           c.get("/w/%s/watcher/safe-pull-status" % CLIENT).status_code == 403)

    with c.session_transaction() as s:
        s.clear()
        s.update(SUPER)
    r = c.post("/w/%s/admin/watcher" % CLIENT, data={"op": "delete", "channel_id": chan})
    _check("op=delete removes the channel AND its safe-pull entry", r.get_json()["ok"] is True
           and _hand_channels() == []
           and workspace.watcher_safe_pull_queue(workspace.load_workspace(CLIENT)) == [])

    watcher.resolve_channel, watcher.list_videos, watcher.fetch_transcript = (
        real_resolve, real_list, real_fetch)

    # --- The website-blog half of the tab (same archive, different fetcher) ----------------------
    print("  -- website blogs --")
    _run_blog_checks(c)
    _check("every hand-added source was cleaned up (only template sources remain)",
           _hand_channels() == [])

    # --- The Instagram third of the tab (same archive again, a third fetcher) --------------------
    print("  -- instagram --")
    _run_ig_checks(c)
    _check("every hand-added Instagram source was cleaned up too", _hand_channels() == [])

    # --- The LAZY pane (archives are read only on the Watcher tab) -------------------------------
    print("  -- lazy pane --")
    _run_lazy_pane_checks(c)

    # --- The source TEMPLATE (default sources every client gets) --------------------------------
    print("  -- source template --")
    _run_template_checks()


if __name__ == "__main__":
    try:
        run()
        print("PASS")
    except AssertionError as exc:
        print("FAIL: %s" % exc)
        sys.exit(1)
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
