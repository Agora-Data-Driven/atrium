"""Website blog archiving for the Atrium 'Watcher' tab (team-only) -- the WEBSITE twin of watcher.py.

Watcher already archives every video a creator publishes; this module does the same job for every
BLOG POST a website publishes, so a competitor's writing is searchable next to their videos. The
three helpers mirror watcher.py one-for-one, so the route, the archive object, the UI and the
Assistant index all reuse the YouTube plumbing unchanged:

  * resolve_site(url)   -- paste ANY link on a site (homepage, /blog, one post) and get back the
                           site's canonical origin + display title.        (~ resolve_channel)
  * list_posts(origin)  -- EVERY blog post on the site (url + title + date), newest first, found
                           SITEMAP-FIRST (robots.txt -> sitemap index -> the blog sitemap) with an
                           index-page crawl as the fallback.               (~ list_videos)
  * fetch_post(url)     -- one post's full readable text, extracted from the page with a small
                           readability-style scorer (stdlib html.parser only -- NO new dependency,
                           matching atrium_docview's posture).             (~ fetch_transcript)

A post is stored in EXACTLY the shape a video is stored in (`id`/`title`/`url`/`transcript`/
`error`/`permanent`/`published`), so `workspace.read_watcher_videos`, the creator grid, the reader
modal and `assistant_ai.build_chunks` need no per-source branching: the post's text simply lands in
the `transcript` field. The id is a stable hash of the URL (URL-safe, so it can be a path segment in
the reader route, and stable so a re-list never duplicates a post).

Every failure is caught and returned as {ok: False, error: <human sentence>} -- nothing here ever
raises to a route. Fetch failures carry `permanent`: True means retrying is pointless (404/410),
False means transient (429/5xx/network) and worth a retry. A throttling response deliberately uses
the SAME "rate-limiting" wording watcher.py uses, so the route's `blocked` handling and the page's
auto-retry-with-backoff loop treat a slow website exactly like a throttling YouTube.

Politeness: an identifiable UA, robots.txt `Disallow` rules are honored, concurrency is modest and
paced. Unlike YouTube, ordinary websites don't block datacenter IPs, so this runs fine on Cloud Run
with no proxy and no Safe-pull detour.

Testable off-cloud: every network call goes through an injectable `fetcher(url) -> html` seam.
"""

import hashlib
import re
import time
from html.parser import HTMLParser


# A polite, identifiable UA (matches atrium_health / watcher).
_UA = "Mozilla/5.0 (compatible; AgoraAtriumWatcher/1.0; +https://agoradatadriven.com)"
# Hard ceilings -- one site's archive object stays bounded (mirrors watcher.MAX_VIDEOS).
MAX_POSTS = 2000
MAX_POST_CHARS = 200000
# How many sitemaps one discovery pass will read (a big shop's index fans out; stop somewhere).
MAX_SITEMAPS = 40


class FetchError(Exception):
    """A failed HTTP GET, carrying the status code (0 = network/transport failure).

    The status is what separates a permanently-gone post (404) from a throttled one (429), so the
    fetcher seam raises this rather than returning a bare string."""

    def __init__(self, status=0, note=""):
        Exception.__init__(self, note or ("HTTP %s" % status))
        self.status = int(status or 0)


def _decode(content, content_type=""):
    """Bytes -> text, honoring the charset the server (or the document) declares.

    `requests` guesses latin-1 for a charset-less text/html, which turns every smart quote into
    mojibake in the archived text -- so read the declared charset, then the document's own <meta>,
    and only then fall back to utf-8 (errors replaced; a stray byte must never sink a fetch)."""
    charset = ""
    m = re.search(r"charset=([\w-]+)", content_type or "", re.I)
    if m:
        charset = m.group(1)
    if not charset:
        head = content[:4096]
        m = (re.search(br'<meta[^>]+charset=["\']?([\w-]+)', head, re.I)
             or re.search(br'charset=["\']([\w-]+)', head, re.I))
        if m:
            charset = m.group(1).decode("ascii", "ignore")
    for enc in (charset, "utf-8"):
        if not enc:
            continue
        try:
            return content.decode(enc, "replace")
        except (LookupError, UnicodeDecodeError):
            continue
    return content.decode("utf-8", "replace")


# 🔴 TLS compatibility -- why this exists (it is NOT decoration):
# Python's default SSL context negotiates a cipher list no mainstream browser sends, and several
# big CDNs (Cloudflare in front of Shopify is the one that bit us on thelegalpaige.com) bucket that
# handshake as "automated" and answer EVERY html request with `429 local_rate_limited` -- forever,
# no matter how slowly you ask. The same URL, same UA, over curl returns 200. Pinning an explicit,
# ordinary cipher suite (the OpenSSL default set every browser also negotiates) is enough for the
# edge to serve the page normally.
# We do NOT pretend to be a browser: the UA still says AgoraAtriumWatcher, robots.txt Disallow rules
# are obeyed, Retry-After is honored, and concurrency stays low. This only makes our TLS handshake
# ordinary so that publicly crawlable pages are actually served to us.
_CIPHERS = "ECDHE+AESGCM:ECDHE+CHACHA20:DHE+AESGCM:ECDHE+AES:!aNULL:!MD5:!DSS"
_SESSION = None


def _session():
    """One shared requests Session with the compatibility TLS context (built once, lazily)."""
    global _SESSION
    if _SESSION is not None:
        return _SESSION
    import ssl
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.poolmanager import PoolManager

    class _TlsAdapter(HTTPAdapter):
        def init_poolmanager(self, connections, maxsize, block=False, **kw):
            ctx = ssl.create_default_context()
            try:
                ctx.set_ciphers(_CIPHERS)
            except Exception:
                pass    # an OpenSSL build without one of these: the default context still works
            self.poolmanager = PoolManager(num_pools=connections, maxsize=maxsize, block=block,
                                           ssl_context=ctx, **kw)

    sess = requests.Session()
    try:
        sess.mount("https://", _TlsAdapter())
    except Exception:
        pass        # any adapter problem: fall back to the plain session rather than failing
    _SESSION = sess
    return sess


def _http_get(url):
    """GET `url` as text. Raises FetchError (with the status) on anything but a 2xx.

    Gzipped sitemaps (`.xml.gz`, what several CMSs serve) are decompressed here so every caller
    sees plain text -- the injectable fetcher seam stays `url -> html`."""
    try:
        resp = _session().get(url, timeout=25, allow_redirects=True, headers={
            "User-Agent": _UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.8",
        })
    except Exception as exc:
        raise FetchError(0, exc.__class__.__name__)
    if resp.status_code >= 400:
        raise FetchError(resp.status_code)
    content = resp.content or b""
    if url.lower().split("?")[0].endswith(".gz") or content[:2] == b"\x1f\x8b":
        import gzip
        try:
            content = gzip.decompress(content)
        except Exception:
            pass  # not actually gzip; fall through and decode what we got
    return _decode(content, resp.headers.get("Content-Type", ""))


# --- 1. resolve_site: a pasted link -> the site's origin + display name ---------------------------
def normalize_site_url(url):
    """Turn whatever the operator pasted into a fetchable http(s) URL ('' if hopeless)."""
    url = (url or "").strip()
    if not url or url.startswith(("mailto:", "javascript:", "#")):
        return ""
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url.lstrip("/")
    if not re.match(r"^https?://[^/\s.]+\.[^/\s]+", url, re.I):
        return ""   # no dotted host -- not a website link
    return url


def site_origin(url):
    """The scheme://host of `url` ('' when it isn't a URL). This is a blog channel's `channel_id`:
    stable, human-readable, and unique per site, so duplicate adds are caught the same way a
    duplicate YouTube channel is."""
    m = re.match(r"^(https?://[^/?#]+)", normalize_site_url(url) or "", re.I)
    return m.group(1).rstrip("/") if m else ""


def resolve_site(url, fetcher=None):
    """Resolve a pasted website link to {ok, site, title, url, error}.

    `site` is the origin (the archive's identity); `url` is the page the operator actually pasted
    (kept so a blog living at /blog is where the crawl fallback starts). The title comes from
    og:site_name, then <title>, then the bare host -- always something readable on the card."""
    fetcher = fetcher or _http_get
    page_url = normalize_site_url(url)
    origin = site_origin(page_url)
    if not origin:
        return {"ok": False, "site": "", "title": "", "url": page_url,
                "error": "That doesn't look like a website link (try https://example.com/blog)."}
    host = origin.split("//", 1)[-1]
    try:
        html = fetcher(page_url) or ""
    except FetchError as exc:
        if exc.status in (401, 403, 429):
            return {"ok": False, "site": origin, "title": "", "url": page_url,
                    "error": "That site refused the request (HTTP %s) — it may block automated "
                             "readers." % exc.status}
        html = ""   # unreachable landing page is survivable: the sitemap may still answer
    except Exception:
        html = ""
    title = (_meta(html, "og:site_name") or _clean_title(_tag_text(html, "title"))
             or host.replace("www.", ""))
    return {"ok": True, "site": origin, "title": title[:80], "url": page_url, "error": ""}


def _meta(html, prop):
    """The content of <meta property|name="prop"> ('' when absent)."""
    m = re.search(r'<meta[^>]+(?:property|name)=["\']%s["\'][^>]*>' % re.escape(prop), html or "", re.I)
    if not m:
        return ""
    c = re.search(r'content=["\']([^"\']*)["\']', m.group(0), re.I)
    return _unescape(c.group(1)).strip() if c else ""


def _tag_text(html, tag):
    """The inner text of the first <tag>...</tag> ('' when absent)."""
    m = re.search(r"<%s[^>]*>(.*?)</%s>" % (tag, tag), html or "", re.I | re.S)
    return _unescape(re.sub(r"<[^>]+>", " ", m.group(1))).strip() if m else ""


def _clean_title(title):
    """A page <title> trimmed of the trailing ' | Site name' boilerplate."""
    for sep in ("|", "–", "—", "·", " - "):
        if sep in (title or ""):
            head = title.split(sep)[0].strip()
            if len(head) >= 12:      # keep it only when the head is a real title, not "Blog"
                return head
    return (title or "").strip()


def _unescape(text):
    import html
    return html.unescape(text or "")


# --- 2. list_posts: every blog post on the site --------------------------------------------------
# URL shapes that mean "this is an article" across the CMSs a client site actually uses (Shopify
# /blogs/<blog>/<handle>, WordPress /blog/<slug> or /2026/07/<slug>, Squarespace/Ghost /blog/,
# Webflow /post/, HubSpot /blog/, plus the usual editorial folders).
_POST_HINTS = ("/blog/", "/blogs/", "/post/", "/posts/", "/article/", "/articles/", "/news/",
               "/insights/", "/journal/", "/stories/", "/story/", "/resources/", "/guides/",
               "/learn/", "/updates/", "/podcasts/", "/press/")
_DATE_PATH = re.compile(r"/(19|20)\d{2}/\d{1,2}/")
# Listing / taxonomy pages that live under the same folders but are NOT posts.
_LISTING_PATH = re.compile(r"/(page|tag|tagged|category|categories|author|archive|archives|feed|"
                           r"search|amp)(/|$)", re.I)
_NON_HTML = re.compile(r"\.(jpe?g|png|gif|webp|svg|pdf|zip|mp[34]|mov|avi|css|js|json|xml|ico)$", re.I)
# Sitemap file names that mean "this one holds the articles" (checked before the generic filter, so
# a shop's product sitemap is never even downloaded).
_BLOG_SITEMAP = re.compile(r"(blog|post|article|news|entries|journal|stories|insights)", re.I)


def _rep_pattern(value):
    """Compile one robots.txt path rule to a regex (`*` = anything, trailing `$` = end anchor).

    ⚠️ Prefix-matching the text before the first `*` is NOT good enough and silently breaks whole
    sites: Shopify's robots carries `Disallow: /blogs/*+*` (block blog URLs containing a '+'), whose
    literal prefix is `/blogs/` -- read as a prefix that bans EVERY blog post on the site, which is
    exactly the opposite of what it says. So the wildcards are translated properly."""
    anchored = value.endswith("$")
    pattern = re.escape(value[:-1] if anchored else value).replace(r"\*", ".*")
    return re.compile("^" + pattern + ("$" if anchored else ""))


def _host_of(url):
    """The lowercased host of a URL, port stripped ('' when it isn't one)."""
    m = re.match(r"^https?://([^/?#]+)", url or "", re.I)
    return m.group(1).lower().split(":")[0] if m else ""


def _path_of(url):
    """The path of a URL, query + fragment stripped ('/' when there is none)."""
    return re.sub(r"^https?://[^/?#]*", "", (url or "").split("#")[0].split("?")[0], flags=re.I) or "/"


def _same_site(url, origin):
    """True when `url` belongs to the same site as `origin`, ignoring a leading `www.`.

    ⚠️ Deliberately NOT `url.startswith(origin)`. A site that canonicalises apex -> www (or
    http -> https) serves its sitemap through the redirect, so every <loc> in it carries the
    CANONICAL host while `origin` came from what the operator pasted. Comparing the raw strings
    threw away the entire sitemap and silently fell back to a one-page crawl -- an archive that
    reported success while holding a handful of the site's posts."""
    a, b = _host_of(url), _host_of(origin)
    if not a or not b:
        return False
    return (a[4:] if a.startswith("www.") else a) == (b[4:] if b.startswith("www.") else b)


def _robots(origin, fetcher):
    """(sitemap_urls, rules) from the site's robots.txt -- both empty when it is unreadable.

    We read robots for TWO reasons: it is where a site declares its sitemaps, and its rules for
    `User-agent: *` are honored by everything below (an archive is not a reason to be rude).
    `rules` is [(regex, allowed, specificity)] evaluated by `_allowed` under the standard
    longest-match-wins precedence, so a site's `Allow:` exceptions are respected too.

    ⚠️ A group may declare SEVERAL user-agents before its rules (`User-agent: *` then
    `User-agent: Googlebot` then `Disallow: /`). Those share one rule block, so `applies` ORs
    across consecutive agent lines and only resets when a new group starts (an agent line that
    follows a rule line). Treating each agent line independently made such a group apply to
    Googlebot only -- i.e. we ignored a site that had asked everyone to stay out."""
    sitemaps, rules = [], []
    applies, in_rules = False, False
    try:
        text = fetcher(origin + "/robots.txt") or ""
    except Exception:
        return [], []
    for line in text.splitlines():
        line = line.split("#")[0].strip()
        if not line or ":" not in line:
            continue
        field, _, value = line.partition(":")
        field, value = field.strip().lower(), value.strip()
        if field == "sitemap" and value:
            sitemaps.append(value)
        elif field == "user-agent":
            if in_rules:            # rules seen already -> this agent line opens a NEW group
                applies, in_rules = False, False
            applies = applies or value == "*"
        elif field in ("allow", "disallow"):
            in_rules = True
            if applies and value.startswith("/"):
                rules.append((_rep_pattern(value), field == "allow", len(value)))
    return sitemaps, rules


def _allowed(url, origin, rules):
    """True when `url` is on this site and robots.txt permits fetching it.

    Standard REP precedence: the LONGEST matching rule wins, and Allow beats Disallow on a tie;
    no matching rule means allowed."""
    if not _same_site(url, origin):
        return False
    path = _path_of(url)
    best, verdict = -1, True
    for pattern, allow, length in rules:
        if pattern.match(path) and (length > best or (length == best and allow)):
            best, verdict = length, allow
    return verdict


def _sitemap_blocks(xml, tag):
    """The inner XML of every <tag>...</tag> block (sitemap entries or url entries)."""
    return re.findall(r"<%s\b[^>]*>(.*?)</%s>" % (tag, tag), xml or "", re.I | re.S)


def _first_tag(block, tag):
    """The text of the first <tag> (namespace-prefixed or not) inside an entry block."""
    m = re.search(r"<(?:\w+:)?%s\b[^>]*>(.*?)</(?:\w+:)?%s>" % (tag, tag), block or "", re.I | re.S)
    return _unescape(re.sub(r"<!\[CDATA\[|\]\]>", "", m.group(1))).strip() if m else ""


def _iso_date(text):
    """The YYYY-MM-DD inside any date-ish string ('' when there isn't one)."""
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", text or "")
    return m.group(0) if m else ""


def _collect_sitemap(url, fetcher, seen, out, depth=0):
    """Walk one sitemap (recursing through <sitemapindex> children) appending url entries to `out`.

    Best-effort at every step: an unreadable child sitemap is skipped, never fatal. When an index
    lists a clearly blog-shaped child we follow ONLY those, so a Shopify store's 10k-product
    sitemap is never downloaded at all."""
    if url in seen or len(seen) >= MAX_SITEMAPS or depth > 3 or len(out) >= MAX_POSTS:
        return
    seen.add(url)
    try:
        xml = fetcher(url) or ""
    except Exception:
        return
    if "<sitemapindex" in xml.lower():
        children = [_first_tag(b, "loc") for b in _sitemap_blocks(xml, "sitemap")]
        children = [c for c in children if c]
        blogs = [c for c in children if _BLOG_SITEMAP.search(c)]
        for child in (blogs or children):
            _collect_sitemap(child, fetcher, seen, out, depth + 1)
        return
    blog_sitemap = bool(_BLOG_SITEMAP.search(url))
    for block in _sitemap_blocks(xml, "url"):
        loc = _first_tag(block, "loc")
        if not loc:
            continue
        out.append({
            "url": loc,
            "date": _iso_date(_first_tag(block, "lastmod")),
            # Shopify (and a few others) carry the article's real title in the image entry -- a free,
            # accurate title before a single post page is fetched.
            "title": _first_tag(block, "title"),
            "from_blog_sitemap": blog_sitemap,
        })
        if len(out) >= MAX_POSTS * 4:   # generous: filtering below cuts this down hard
            return


def _looks_like_post(url, origin, from_blog_sitemap):
    """True when this URL looks like an individual article rather than a listing/product page."""
    if not url.startswith(origin) or _NON_HTML.search(url.split("?")[0]):
        return False
    path = url[len(origin):].split("#")[0].split("?")[0]
    if not path or path == "/" or _LISTING_PATH.search(path):
        return False
    segments = [s for s in path.split("/") if s]
    if not segments:
        return False
    if from_blog_sitemap:
        # A sitemap named "blogs"/"posts"/"news" holds articles by definition -- accept everything
        # in it and let the parent-URL rule below drop the blog's own index pages.
        return True
    low = path.lower()
    return any(h in low + "/" for h in _POST_HINTS) or bool(_DATE_PATH.search(low))


def _title_from_slug(url):
    """A readable placeholder title from the URL slug ('what-is-an-nda' -> 'What Is An Nda').

    Only ever shown until the post is fetched, where the page's real og:title replaces it."""
    slug = [s for s in url.split("#")[0].split("?")[0].split("/") if s]
    slug = slug[-1] if slug else ""
    slug = re.sub(r"\.(html?|php|aspx?)$", "", slug, flags=re.I)
    words = re.split(r"[-_+]+", slug)
    return " ".join(w.capitalize() for w in words if w)[:160] or "Untitled post"


def post_id(url):
    """A stable, URL-safe id for a post (a hash of its URL).

    Stable so re-listing a site never duplicates a post; hex so it can be a path segment in the
    reader route (`/w/<c>/watcher/video/<channel>/<id>`), which a raw URL could never be."""
    return "bp" + hashlib.sha1((url or "").strip().rstrip("/").encode("utf-8")).hexdigest()[:16]


def _entries_to_posts(entries, origin):
    """Filter + de-duplicate raw sitemap entries down to the individual posts, newest first.

    Two rules do the work: the shape filter (`_looks_like_post`) and the PARENT rule -- a URL that
    is a prefix of other collected URLs is a listing page (`/blogs/blog` above
    `/blogs/blog/<article>`), so it is dropped. That is what removes a blog's own index pages
    without hardcoding any one CMS's layout."""
    kept, seen = [], set()
    for e in entries:
        url = (e.get("url") or "").strip().rstrip("/")
        if not url or url in seen:
            continue
        if not _looks_like_post(url, origin, e.get("from_blog_sitemap")):
            continue
        seen.add(url)
        kept.append(dict(e, url=url))
    prefixes = set()
    for e in kept:
        parts = e["url"].split("/")
        for i in range(4, len(parts)):     # scheme, '', host, then each path segment
            prefixes.add("/".join(parts[:i]))
    posts = []
    for e in kept:
        if e["url"] in prefixes:
            continue                        # a parent of other posts == a listing page
        posts.append({
            "id": post_id(e["url"]),
            "url": e["url"],
            "title": (e.get("title") or "").strip()[:200] or _title_from_slug(e["url"]),
            "published": e.get("date", ""),
            "published_text": e.get("date", ""),
        })
    posts.sort(key=lambda p: p["published"], reverse=True)   # newest first, like a video listing
    return posts[:MAX_POSTS]


# Index pages worth trying when a site publishes no usable sitemap at all.
_INDEX_GUESSES = ("/blog", "/blogs/blog", "/blog/", "/news", "/articles", "/insights", "/resources")


def _crawl_links(page_url, origin, fetcher, rules):
    """Every same-site link on one page, as raw entries (the no-sitemap fallback)."""
    try:
        html = fetcher(page_url) or ""
    except Exception:
        return []
    out = []
    for href in re.findall(r'<a\b[^>]+href=["\']([^"\'#]+)', html, re.I):
        href = _unescape(href).strip()
        if href.startswith("/"):
            href = origin + href
        elif not href.lower().startswith("http"):
            continue
        if _allowed(href, origin, rules):
            out.append({"url": href, "date": "", "title": "", "from_blog_sitemap": False})
    return out


def list_posts(site_url, fetcher=None, start_url=""):
    """Every blog post on the site as {ok, posts: [{id, title, url, published, ...}], error, source}.

    Sitemap-first (robots.txt declarations, then the conventional paths), because a sitemap is the
    only listing that is guaranteed complete -- an index page shows one page of recent posts. Only
    when NO sitemap yields posts does it fall back to crawling the blog index for links, and the
    `source` field says which path produced the result so the operator knows what they got."""
    fetcher = fetcher or _http_get
    origin = site_origin(site_url)
    if not origin:
        return {"ok": False, "posts": [], "error": "Bad website address.", "source": ""}
    declared, rules = _robots(origin, fetcher)
    entries, seen = [], set()
    for sm in declared + [origin + p for p in ("/sitemap.xml", "/sitemap_index.xml",
                                               "/wp-sitemap.xml", "/sitemap-index.xml",
                                               "/blog-sitemap.xml", "/sitemap/sitemap-index.xml")]:
        _collect_sitemap(sm, fetcher, seen, entries)
        if len(entries) >= MAX_POSTS:
            break
    entries = [e for e in entries if _allowed((e.get("url") or "").rstrip("/"), origin, rules)]
    posts = _entries_to_posts(entries, origin)
    source = "sitemap"
    if not posts:
        # No usable sitemap: crawl the pasted page (and the conventional blog indexes) for links.
        source = "crawl"
        crawled = []
        pages = [p for p in [normalize_site_url(start_url) or None] if p]
        pages += [origin + g for g in _INDEX_GUESSES]
        for page in pages[:8]:
            crawled += _crawl_links(page, origin, fetcher, rules)
            if len(crawled) > MAX_POSTS * 4:
                break
        posts = _entries_to_posts(crawled, origin)
    if not posts:
        return {"ok": False, "posts": [], "source": source,
                "error": "No blog posts found on that site — paste the blog's own URL "
                         "(like example.com/blog) and try again."}
    return {"ok": True, "posts": posts, "error": "", "source": source}


# --- 3. fetch_post: one post's readable text -----------------------------------------------------
# Everything whose text is never article prose. Skipped wholesale during extraction (and their
# subtrees), which is most of the boilerplate removal on a modern page.
_SKIP_TAGS = frozenset(("script", "style", "noscript", "svg", "head", "nav", "header", "footer",
                        "aside", "form", "iframe", "template", "button", "select", "option",
                        "picture", "video", "audio", "canvas", "figure"))
# Tags that force a line break when rendering the chosen subtree to text.
_BLOCK_TAGS = frozenset(("p", "div", "section", "article", "main", "li", "tr", "br", "ul", "ol",
                         "pre", "blockquote", "figcaption", "h1", "h2", "h3", "h4", "h5", "h6",
                         "dd", "dt", "table", "hr"))
_VOID_TAGS = frozenset(("br", "img", "hr", "input", "meta", "link", "source", "col", "area",
                        "base", "embed", "param", "track", "wbr"))
_CONTAINER_TAGS = frozenset(("article", "main", "div", "section", "td"))
_CONTENT_HINT = re.compile(r"(article|post|entry|content|blog|story|rte|body-copy|prose|"
                           r"rich-?text|markdown)", re.I)
_JUNK_HINT = re.compile(r"(nav|menu|footer|header|sidebar|comment|related|share|social|promo|"
                        r"banner|cookie|popup|modal|newsletter|subscribe|breadcrumb|pagination|"
                        r"widget|recirc|author-bio|byline|tag-|search|cart|checkout|announce)", re.I)


class _Node(object):
    """One element in the lightweight parse tree (text children are plain strings)."""

    __slots__ = ("tag", "ident", "kids", "depth")

    def __init__(self, tag, ident="", depth=0):
        self.tag = tag
        self.ident = ident      # class + id, lowercased -- what the hint patterns match
        self.kids = []
        self.depth = depth


class _TreeBuilder(HTMLParser):
    """Build a forgiving element tree (unclosed/stray tags are tolerated, as real pages have both).

    A full tree (rather than a regex sweep) is what lets the scorer below compare CONTAINERS --
    picking the one div that holds the article out of a page with 40 of them is the whole job."""

    def __init__(self):
        HTMLParser.__init__(self, convert_charrefs=True)
        self.root = _Node("[document]")
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        a = dict(attrs)
        node = _Node(tag, ("%s %s" % (a.get("class") or "", a.get("id") or "")).lower(),
                     self.stack[-1].depth + 1)
        self.stack[-1].kids.append(node)
        if tag not in _VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.stack[-1].kids.append(_Node(tag.lower(), "", self.stack[-1].depth + 1))

    def handle_endtag(self, tag):
        tag = tag.lower()
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return
        # A close tag with no open partner (very common in the wild): ignore it.

    def handle_data(self, data):
        if not data:
            return
        # Whitespace is kept as a single space so `<b>a</b> <i>b</i>` doesn't become "ab".
        self.stack[-1].kids.append(data if data.strip() else " ")


def _walk_text(node, buf, inside_link=None):
    """Append `node`'s visible text to `buf`, skipping boilerplate subtrees and marking link text.

    `inside_link` (a one-item list used as a counter) lets the scorer measure link density in the
    same single pass that collects the text."""
    for kid in node.kids:
        if isinstance(kid, str):
            buf.append(kid)
            if inside_link is not None and inside_link[0]:
                inside_link.append(len(kid))
            continue
        if kid.tag in _SKIP_TAGS:
            continue
        block = kid.tag in _BLOCK_TAGS
        if block:
            buf.append("\n")
        if inside_link is not None and kid.tag == "a":
            inside_link[0] += 1
            _walk_text(kid, buf, inside_link)
            inside_link[0] -= 1
        else:
            _walk_text(kid, buf, inside_link)
        if block:
            buf.append("\n")


def _node_text(node):
    """(text, link_char_count) for a subtree -- the two numbers the scorer needs."""
    buf, marker = [], [0]
    _walk_text(node, buf, marker)
    return _tidy("".join(buf)), sum(marker[1:])


def _tidy(text):
    """Collapse scraped whitespace into readable paragraphs (blank line between blocks)."""
    text = text.replace("\r", "\n").replace(" ", " ")
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _score_node(node):
    """A readability-style score for one container: how much of it is real prose.

    Long text scores high, link-heavy text scores low (that is a nav or a related-posts rail), and
    the class/id name nudges either way. Returns (score, text) -- 0 for anything too small."""
    text, link_chars = _node_text(node)
    length = len(text)
    if length < 200:
        return 0.0, text
    density = min(1.0, link_chars / float(length))
    if density > 0.5:
        return 0.0, text
    score = length * (1.0 - density)
    paragraphs = text.count("\n\n")
    score *= 1.0 + min(paragraphs, 20) / 40.0
    if _CONTENT_HINT.search(node.ident):
        score *= 1.35
    if _JUNK_HINT.search(node.ident):
        score *= 0.4
    if node.tag == "article":
        score *= 1.2
    return score, text


def _candidates(node, out):
    """Every container element in the tree, depth-first (the scorer's input set)."""
    for kid in node.kids:
        if isinstance(kid, str) or kid.tag in _SKIP_TAGS:
            continue
        if kid.tag in _CONTAINER_TAGS:
            out.append(kid)
        _candidates(kid, out)
    return out


def extract_article(html, url=""):
    """A blog page's title, body text, publish date and author -- {title, text, published, author}.

    The body is the highest-scoring container on the page (see `_score_node`); when several score
    within 10% of each other the DEEPEST wins, which picks the article's own wrapper instead of the
    page-level div that merely contains it plus a sidebar. If nothing scores (a JS-rendered page,
    a paywall) the text comes back empty and the caller reports a friendly failure."""
    html = html or ""
    published = (_iso_date(_meta(html, "article:published_time"))
                 or _iso_date(_meta(html, "article:modified_time"))
                 or _iso_date(_ld_field(html, "datePublished"))
                 or _iso_date(_ld_field(html, "dateCreated"))
                 or _iso_date(_time_attr(html)))
    author = _meta(html, "article:author") or _ld_field(html, "author") or ""
    title = (_meta(html, "og:title") or _ld_field(html, "headline")
             or _clean_title(_tag_text(html, "h1")) or _clean_title(_tag_text(html, "title"))
             or (_title_from_slug(url) if url else ""))
    parser = _TreeBuilder()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        pass    # a malformed page still yields whatever was parsed before the fault
    best_text, best_score, best_depth = "", 0.0, -1
    scored = []
    for node in _candidates(parser.root, []):
        score, text = _score_node(node)
        if score > 0:
            scored.append((score, node.depth, text))
            if score > best_score:
                best_score, best_text, best_depth = score, text, node.depth
    for score, depth, text in scored:
        # Near-tie: prefer the tighter (deeper) container -- the article, not its page wrapper.
        if score >= best_score * 0.9 and depth > best_depth:
            best_text, best_depth = text, depth
    # An LD-JSON articleBody (some CMSs publish the whole post there) wins if it is richer.
    body = _ld_field(html, "articleBody")
    if len(body) > len(best_text):
        best_text = _tidy(body)
    return {"title": (title or "").strip()[:200], "text": best_text[:MAX_POST_CHARS],
            "published": published, "author": (author or "").strip()[:80]}


def _ld_field(html, field):
    """A top-level string field from any JSON-LD block on the page ('' when absent).

    Read with a regex rather than json.loads because a lot of real templates emit LD blocks that
    aren't valid JSON (unescaped quotes, trailing commas) -- one bad block must not cost us the
    date on every other page."""
    for m in re.finditer(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', html or "",
                         re.I | re.S):
        block = m.group(1)
        f = re.search(r'"%s"\s*:\s*"((?:[^"\\]|\\.)*)"' % re.escape(field), block)
        if f:
            try:
                import json
                return _unescape(json.loads('"%s"' % f.group(1)))
            except ValueError:
                return _unescape(f.group(1))
        if field == "author":     # often an object: "author": {"@type":"Person","name":"..."}
            f = re.search(r'"author"\s*:\s*\{[^}]*?"name"\s*:\s*"([^"]*)"', block, re.S)
            if f:
                return _unescape(f.group(1))
    return ""


def _time_attr(html):
    """The first <time datetime="..."> value on the page ('' when absent)."""
    m = re.search(r'<time[^>]+datetime=["\']([^"\']+)', html or "", re.I)
    return m.group(1) if m else ""


# Error phrasing kept deliberately IDENTICAL in shape to watcher.fetch_transcript's, including the
# literal "rate-limiting" that main.py and the page JS match on to back off instead of failing.
_PERMANENT_STATUS = {
    404: "That post no longer exists (404).",
    410: "That post has been removed (410).",
    451: "That post is not available for legal reasons (451).",
}
_THROTTLE_STATUS = (403, 408, 429, 500, 502, 503, 504)


def fetch_post(url, fetcher=None):
    """One blog post's full text: {ok, transcript, title, published, error, permanent}.

    Named `transcript` on purpose -- a post is stored in the same archive field a video transcript
    is, so every consumer (the reader modal, the Assistant index, the counts) works unchanged."""
    out = {"ok": False, "transcript": "", "title": "", "published": "", "author": "",
           "error": "", "permanent": False}
    fetcher = fetcher or _http_get
    try:
        html = fetcher(url)
    except FetchError as exc:
        if exc.status in _PERMANENT_STATUS:
            out.update(error=_PERMANENT_STATUS[exc.status], permanent=True)
        elif exc.status in _THROTTLE_STATUS:
            out["error"] = ("The site is rate-limiting or blocking this server right now — the "
                            "fetch will retry on its own.")
        else:
            out["error"] = "Could not fetch that page (%s)." % (exc.status or "network error")
        return out
    except Exception as exc:
        out["error"] = "Could not fetch that page (%s)." % exc.__class__.__name__
        return out
    article = extract_article(html, url)
    if not article["text"]:
        out.update(error="No readable article text on that page.", permanent=True,
                   title=article["title"], published=article["published"])
        return out
    out.update(ok=True, transcript=article["text"], title=article["title"],
               published=article["published"], author=article["author"])
    return out


# --- 4. Batch fetching (the route's "Fetch missing" loop) -----------------------------------------
# Websites don't block datacenter IPs the way YouTube does, so a modest, paced concurrency is both
# safe and polite -- no proxy, no Safe-pull detour. Tuned to finish a few-hundred-post blog in a
# handful of batches while never hammering one host.
POST_WORKERS = 6
POST_BATCH = 24


def _apply_post(v, result, now):
    """Record ONE post fetch onto an archive entry IN PLACE. Returns "done" or "blocked".

    Same contract as watcher._apply_result -- a throttling response leaves the entry PENDING (a
    session condition, not a fact about the post), so the next pass retries only what is missing.
    Unlike a video, a post fetch also yields its real title and publish date, so both are healed
    here (the listing only had the slug and the sitemap's lastmod)."""
    if not result["ok"] and "rate-limiting" in result["error"]:
        return "blocked"
    v["fetched_at"] = now
    if result.get("title"):
        v["title"] = result["title"]
    if result.get("published"):
        v["published"] = result["published"]
        v["published_text"] = result["published"]
    if result["ok"]:
        v["transcript"] = result["transcript"]
        v["language"] = ""
        v["generated"] = False
        v["error"] = ""
        v["permanent"] = False
    else:
        v["error"] = result["error"]
        v["permanent"] = bool(result["permanent"])
    return "done"


def fetch_posts_batch(posts, limit=POST_BATCH, pause=0.2, workers=POST_WORKERS, fetcher=None):
    """Fetch up to `limit` pending posts IN PLACE. Returns (fetched, blocked).

    Mirrors watcher.fetch_transcripts_batch exactly (pending = no text and no error; every dict
    mutation happens on THIS thread in the as_completed loop; `blocked` only when the whole batch
    made zero progress), so the route and the page's auto-retry loop are shared code."""
    from workspace import now_iso  # local import: avoids a cycle at module load
    pending = [v for v in posts if not (v.get("transcript") or v.get("error"))][:limit]
    if not pending:
        return 0, False

    def one(v):
        return fetch_post(v.get("url", ""), fetcher=fetcher)

    if workers <= 1:
        done = 0
        for v in pending:
            if _apply_post(v, one(v), now_iso()) == "blocked":
                return done, True
            done += 1
            if pause:
                time.sleep(pause)
        return done, False

    from concurrent.futures import ThreadPoolExecutor, as_completed
    done, throttled = 0, 0
    with ThreadPoolExecutor(max_workers=min(workers, len(pending))) as pool:
        futures = {pool.submit(one, v): v for v in pending}
        for fut in as_completed(futures):
            try:
                result = fut.result()
            except Exception:
                continue    # unexpected thread failure: leave it pending, retried next wave
            if _apply_post(futures[fut], result, now_iso()) == "blocked":
                throttled += 1
            else:
                done += 1
    return done, (done == 0 and throttled > 0)


def post_entry(p):
    """A fresh archive entry for one listed post (no text yet) -- the blog twin of a video entry."""
    return {"id": p.get("id") or post_id(p.get("url", "")),
            "title": p.get("title", ""), "url": p.get("url", ""),
            "transcript": "", "language": "", "generated": False,
            "error": "", "permanent": False, "fetched_at": "",
            "published_text": p.get("published_text", ""), "published": p.get("published", "")}
