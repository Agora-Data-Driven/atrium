"""Instagram archiving for the Atrium 'Watcher' tab (team-only) -- the INSTAGRAM twin of watcher.py.

Watcher archives every video a YouTube creator publishes (watcher.py) and every post a website
publishes (watcher_blog.py). This module does the same job for Instagram, so a competitor's reels
sit in the same archive, the same reader modal and the same Assistant index as their videos and
their blog. The three helpers mirror the other two modules one-for-one:

  * resolve_profile(url)  -- paste ANY instagram link (profile, post, reel) and get back the
                             canonical @handle + display name.              (~ resolve_channel)
  * list_posts(username)  -- EVERY post/reel on the profile (shortcode + caption + date), newest
                             first.                                          (~ list_videos)
  * fetch_post(url)       -- ONE post's full text: its caption, its alt text, and -- for a reel --
                             the WORDS ACTUALLY SPOKEN, transcribed from the video's audio by
                             Vertex Gemini.                                  (~ fetch_transcript)

A post is stored in EXACTLY the shape a video is stored in (`id`/`title`/`url`/`transcript`/
`error`/`permanent`/`published`), so `workspace.read_watcher_videos`, the creator grid, the reader
modal and `assistant_ai.build_chunks` need no per-source branching -- the composed text simply lands
in the `transcript` field. The id is the post's SHORTCODE (stable, URL-safe, already a path segment
in the reader route), which is why a re-list never duplicates a post.

Every failure is caught and returned as {ok: False, error: <human sentence>} -- nothing here ever
raises to a route. Failures carry `permanent`: True means retrying is pointless (post deleted, or a
private account), False means transient (throttling, network) and worth a retry. A throttling
response deliberately uses the SAME "rate-limiting" wording watcher.py uses, so the route's `blocked`
handling and the page's auto-retry-with-backoff loop treat Instagram exactly like a throttling
YouTube.

🔴 TWO OPT-IN pieces of configuration, both dormant by default (the AGENTS.md "no new infra unless
   an operator enables it" posture, same as WATCHER_PROXY_URL):

   1. `INSTAGRAM_SESSIONID` (Secret Manager `instagram-session`). Instagram serves the keyless
      /embed/captioned/ page for a SINGLE public post, and that is genuinely all it serves logged
      out -- a whole-profile listing is 401/403 without a session, every time. With the cookie set,
      `list_posts` pages the profile feed properly and single posts resolve through the richer
      media-info endpoint (real caption, real timestamp, the reel's video URL). Without it a pasted
      PROFILE returns a clear "Instagram needs a session for this" sentence rather than an empty
      listing that reads as "this account has no posts".
      ⚠️ Use a throwaway account. Automated access is against Instagram's ToS and a flagged session
      cookie is the account that pays for it, not this server. Rotate the secret if it stops working
      (an expired cookie surfaces as `needs_session`, never as "the post doesn't exist").

   2. `WATCHER_PROXY_URL` -- the SAME residential-proxy secret watcher.py already honours. Instagram
      blocks datacenter IPs harder than YouTube does, so a proxyless Cloud Run fetch is throttled
      quickly. Reused deliberately: one proxy secret for the whole tab.

Transcription (`transcribe=True`, the default) reuses the Vertex plumbing `intel_ai` already owns --
the runtime SA's own token, no new API, no new IAM, no new key -- and is BEST-EFFORT: a reel whose
audio can't be transcribed still archives its caption. It is skipped entirely for photo posts, for
videos over MAX_MEDIA_BYTES, and when no Vertex credentials are available.

Politeness: an identifiable UA, LOW serial concurrency with real pacing (Instagram punishes bursts
far more aggressively than a website does -- there is no parallel path here on purpose).

Testable off-cloud: every network call and the transcriber go through injectable seams
(`fetcher` -> html, `json_fetcher` -> dict, `media_fetcher` -> bytes, `transcriber` -> text).
"""

import json
import os
import re
import time


# A polite, identifiable UA (matches watcher / watcher_blog / atrium_health).
_UA = "Mozilla/5.0 (compatible; AgoraAtriumWatcher/1.0; +https://agoradatadriven.com)"
# Instagram's own public web-client app id -- the header every instagram.com page sends with its
# /api/v1/ calls. Keyless and stable (it identifies the WEB CLIENT, not a developer app), which is
# what keeps this module inside Atrium's "no new API key" rule.
_WEB_APP_ID = "936619743392459"

# Hard ceilings -- one profile's archive object stays bounded (mirrors watcher.MAX_VIDEOS /
# watcher_blog.MAX_POSTS).
MAX_POSTS = 2000
MAX_CAPTION_CHARS = 20000
# Feed pages are 33 items each; this caps the paging walk so one add can't run for ten minutes.
MAX_FEED_PAGES = 70

# The biggest reel we will pull down and hand to Gemini. Vertex caps an inline-data request at
# ~20 MB and base64 inflates by 4/3, so 12 MB of mp4 is the practical ceiling. A typical 60-90s reel
# is 3-10 MB, so this covers nearly everything; anything bigger archives caption-only.
MAX_MEDIA_BYTES = 12 * 1024 * 1024


class FetchError(Exception):
    """A failed HTTP call, carrying the status code (0 = network/transport failure).

    The status is what separates a permanently-gone post (404) from a throttled one (429) and from
    "you need a session for this" (401/403), so the fetcher seam raises this rather than returning a
    bare string. Mirrors watcher_blog.FetchError."""

    def __init__(self, status=0, note=""):
        Exception.__init__(self, note or ("HTTP %s" % status))
        self.status = int(status or 0)


# --- Configuration seams -------------------------------------------------------------------------
def _proxies():
    """Optional egress proxy ({} when unset) -- the SAME secret watcher.py uses.

    One proxy for the whole Watcher tab is deliberate: both sources are blocked by the same thing
    (a datacenter IP), so splitting them into two secrets would only mean two things to forget."""
    url = os.environ.get("WATCHER_PROXY_URL", "").strip()
    if not url:
        return {}
    return {"http": url, "https": url}


def _sessionid():
    """The configured Instagram session cookie ('' when the secret isn't mounted)."""
    return os.environ.get("INSTAGRAM_SESSIONID", "").strip()


def session_configured():
    """True when an Instagram session cookie is available.

    Gates the two things that genuinely cannot work logged out (profile listing and the rich
    media-info lookup), so the UI can say "connect a session" instead of "nothing found"."""
    return bool(_sessionid())


def transcription_enabled():
    """True when reel audio should be transcribed (default ON; WATCHER_IG_TRANSCRIBE=0 turns it off).

    The call itself still degrades on its own if Vertex credentials are missing -- this switch is
    for an operator who wants captions only (e.g. while archiving a 500-reel profile)."""
    return (os.environ.get("WATCHER_IG_TRANSCRIBE", "1").strip() or "1") not in ("0", "false", "no")


def _headers(referer=""):
    """Browser-shaped headers Instagram's web endpoints require (app id + a same-site referer)."""
    h = {
        "User-Agent": _UA,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.8",
        "X-IG-App-ID": _WEB_APP_ID,
        "X-Requested-With": "XMLHttpRequest",
        "Referer": referer or "https://www.instagram.com/",
    }
    sid = _sessionid()
    if sid:
        # Instagram accepts the bare sessionid; ds_user_id/csrftoken are only needed for WRITES,
        # and this module never writes anything.
        h["Cookie"] = "sessionid=" + sid
    return h


# --- HTTP seams ----------------------------------------------------------------------------------
def _request(url, referer="", stream=False, timeout=25):
    """One GET through requests, raising FetchError with the status on anything but a 2xx."""
    import requests  # lazy, matching the rest of the app
    try:
        resp = requests.get(url, timeout=timeout, allow_redirects=True, stream=stream,
                            proxies=_proxies() or None, headers=_headers(referer))
    except Exception as exc:
        raise FetchError(0, exc.__class__.__name__)
    if resp.status_code >= 400:
        raise FetchError(resp.status_code)
    return resp


def _http_get(url, referer=""):
    """GET `url` as text."""
    return _request(url, referer).text


def _http_get_json(url, referer=""):
    """GET `url` and decode JSON.

    A logged-out request to an /api/v1/ endpoint often answers 200 with an HTML login page rather
    than a 401, so a decode failure is reported as the auth problem it actually is."""
    resp = _request(url, referer)
    try:
        return resp.json()
    except ValueError:
        raise FetchError(401, "Instagram answered with a login page instead of data")


def _http_get_bytes(url, max_bytes=MAX_MEDIA_BYTES):
    """Download up to `max_bytes` of a media URL. Returns (data, mime); data is b'' if oversized.

    Streamed and hard-capped: a reel must never be able to pull an unbounded body into a 2Gi
    container. An over-cap video is not an error -- the caller just archives the caption alone."""
    resp = _request(url, timeout=60, stream=True)
    mime = (resp.headers.get("Content-Type") or "video/mp4").split(";")[0].strip()
    declared = resp.headers.get("Content-Length")
    if declared and declared.isdigit() and int(declared) > max_bytes:
        resp.close()
        return b"", mime
    buf = bytearray()
    for chunk in resp.iter_content(65536):
        buf.extend(chunk)
        if len(buf) > max_bytes:
            resp.close()
            return b"", mime
    return bytes(buf), mime


# --- 1. Link parsing: what did the operator actually paste? ---------------------------------------
# Path segments that are Instagram's OWN routes, never a username. Without this list
# `instagram.com/reel/<code>` would resolve to a profile called "reel".
_RESERVED = frozenset((
    "p", "reel", "reels", "tv", "stories", "s", "explore", "accounts", "direct", "about", "legal",
    "developer", "developers", "api", "graphql", "ajax", "web", "static", "oauth", "challenge",
    "emails", "session", "push", "qr", "topics", "your_activity", "privacy", "terms", "help",
    "download", "sitemap", "lite", "igtv", "locations", "create", "settings", "archive",
))
_SHORTCODE = r"[0-9A-Za-z_-]{5,32}"


def is_instagram_url(url):
    """True when this link points at instagram.com (any shape, with or without a scheme)."""
    u = (url or "").strip().lower()
    if not u:
        return False
    u = re.sub(r"^https?://", "", u)
    u = re.sub(r"^www\.", "", u)
    return u.startswith("instagram.com/") or u == "instagram.com" or u.startswith("instagr.am/")


def normalize_ig_url(url):
    """Turn whatever was pasted into a fetchable instagram.com URL ('' when it isn't one).

    Accepts a bare `@handle` too, which is how people actually write an Instagram account."""
    raw = (url or "").strip()
    if not raw:
        return ""
    if raw.startswith("@") and re.fullmatch(r"@[A-Za-z0-9._]{1,30}", raw):
        return "https://www.instagram.com/" + raw[1:] + "/"
    if not is_instagram_url(raw):
        return ""
    if not re.match(r"^https?://", raw, re.I):
        raw = "https://" + raw.lstrip("/")
    return raw


def extract_shortcode(url):
    """The post/reel shortcode from any single-post link ('' when the link isn't one).

    Handles /p/, /reel/, /reels/ and the legacy /tv/, with or without a trailing slash, query string
    or a leading profile segment (`/<user>/reel/<code>/`, which is what the app's share sheet
    produces). A PROFILE link has no shortcode, which is exactly how the two are told apart."""
    u = normalize_ig_url(url)
    if not u:
        return ""
    m = re.search(r"/(?:p|reels?|tv)/(" + _SHORTCODE + r")", u)
    return m.group(1) if m else ""


def extract_username(url):
    """The @handle from a profile link ('' when the link isn't a profile).

    Deliberately returns '' for a post/reel link and for Instagram's own routes -- callers use the
    (shortcode, username) pair to decide which of the two paths a pasted link takes."""
    u = normalize_ig_url(url)
    if not u or extract_shortcode(u):
        return ""
    m = re.match(r"^https?://(?:www\.)?instagr(?:am\.com|\.am)/([A-Za-z0-9._]{1,30})", u, re.I)
    if not m:
        return ""
    name = m.group(1).strip(".").lower()
    if not name or name in _RESERVED:
        return ""
    return name


def profile_url(username):
    """The canonical profile URL for a handle."""
    return "https://www.instagram.com/%s/" % (username or "").strip().lower()


def post_url(shortcode):
    """The canonical post URL for a shortcode."""
    return "https://www.instagram.com/p/%s/" % shortcode


# Instagram shortcodes are the media's numeric primary key written in this base-64 alphabet. The
# conversion is pure arithmetic, which is what lets us reach the authenticated media-info endpoint
# (it is keyed by pk, while every link a human can copy carries the shortcode).
_B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
_B64_INDEX = {c: i for i, c in enumerate(_B64)}


def media_pk(shortcode):
    """The numeric media id behind a shortcode as a string ('' when it can't be decoded).

    Only the first 11 characters carry the id; longer shortcodes append carousel/child data that
    would corrupt the number if it were folded in."""
    code = (shortcode or "")[:11]
    if not code:
        return ""
    pk = 0
    for ch in code:
        if ch not in _B64_INDEX:
            return ""
        pk = pk * 64 + _B64_INDEX[ch]
    return str(pk)


# --- 2. resolve_profile: a pasted link -> the account's handle + display name ----------------------
def resolve_profile(url, json_fetcher=None, fetcher=None):
    """Resolve a pasted Instagram link to {ok, username, title, url, error, needs_session}.

    `username` is the handle, and it is the entry's `channel_id` -- stable, human-readable and
    unique per account, so a duplicate add is caught exactly the way a duplicate YouTube channel or
    website origin is. The display name is best-effort: an account we can't introspect still resolves
    (titled with its @handle), because the listing call reports the real problem in one place."""
    out = {"ok": False, "username": "", "title": "", "url": "", "error": "", "needs_session": False}
    username = extract_username(url)
    if not username:
        if extract_shortcode(url):
            # A single post was pasted where a profile was expected. Say which one it is rather than
            # "bad link" -- the operator's next click is a different dropdown option, not a retype.
            out["error"] = ("That's a link to ONE post. Choose \"Instagram post or reel\" to grab "
                            "just that one, or paste the profile link (instagram.com/@handle).")
            return out
        out["error"] = "That doesn't look like an Instagram profile link."
        return out
    out.update(username=username, url=profile_url(username), title="@" + username)
    info, err = _profile_info(username, json_fetcher, fetcher)
    if info is None:
        out["error"] = err.get("error", "")
        out["needs_session"] = bool(err.get("needs_session"))
        # A profile we cannot introspect is NOT a dead end when a session exists: the listing walk
        # below can still page the feed. Only a hard auth/absent answer fails the add.
        if err.get("fatal"):
            return out
    else:
        full = (info.get("full_name") or "").strip()
        out["title"] = full or ("@" + username)
    out.update(ok=True, error="")
    return out


def _profile_info(username, json_fetcher=None, fetcher=None):
    """The account's web-profile blob, or (None, {error, needs_session, fatal}).

    `web_profile_info` is the one endpoint that answers for a logged-out client often enough to be
    worth trying first; it carries the numeric user id the feed walk needs plus the first page of
    posts, so a successful call is both the resolve AND page one of the listing."""
    fn = json_fetcher or _http_get_json
    url = ("https://www.instagram.com/api/v1/users/web_profile_info/?username="
           + re.sub(r"[^a-z0-9._]", "", username.lower()))
    try:
        data = fn(url, profile_url(username))
    except FetchError as exc:
        return None, _auth_error(exc, "that profile")
    except Exception as exc:
        return None, {"error": "Could not reach Instagram (%s)." % exc.__class__.__name__,
                      "needs_session": False, "fatal": True}
    user = ((data or {}).get("data") or {}).get("user")
    if not isinstance(user, dict):
        return None, {"error": "Instagram returned no profile for @%s." % username,
                      "needs_session": not session_configured(), "fatal": False}
    if user.get("is_private") and not user.get("followed_by_viewer"):
        return None, {"error": "@%s is a private account — this server can't see its posts."
                               % username, "needs_session": False, "fatal": True}
    return user, {}


def _auth_error(exc, subject):
    """Turn a FetchError into the right human sentence + flags for a listing/resolve failure."""
    status = getattr(exc, "status", 0)
    if status in (401, 403):
        if session_configured():
            return {"error": "Instagram rejected the stored session — the cookie has probably "
                             "expired. Update the `instagram-session` secret and redeploy.",
                    "needs_session": True, "fatal": True}
        return {"error": "Instagram won't show %s to a logged-out server. Add an "
                         "`instagram-session` secret to archive whole profiles." % subject,
                "needs_session": True, "fatal": True}
    if status == 404:
        return {"error": "Instagram says %s doesn't exist." % subject,
                "needs_session": False, "fatal": True}
    if status == 429:
        return {"error": "Instagram is rate-limiting this server right now — try again in a few "
                         "minutes.", "needs_session": False, "fatal": True}
    return {"error": "Instagram didn't answer (%s). Try again in a few minutes."
                     % (status or "network error"), "needs_session": False, "fatal": True}


# --- 3. list_posts: every post on a profile -------------------------------------------------------
def _caption_of(node):
    """The caption text out of either shape Instagram serves (GraphQL edges or feed `caption`)."""
    cap = node.get("caption")
    if isinstance(cap, dict):
        return (cap.get("text") or "").strip()
    if isinstance(cap, str):
        return cap.strip()
    edges = ((node.get("edge_media_to_caption") or {}).get("edges") or [])
    if edges:
        return ((edges[0].get("node") or {}).get("text") or "").strip()
    return ""


def caption_title(caption, shortcode=""):
    """A card title for a post: its caption's first real line, trimmed.

    Instagram posts have no titles, so the caption's opening line is the only thing that reads like
    one in a grid of cards -- and it is what the Assistant's title-indexing (`_searchable`) needs to
    make a post findable by what it is about."""
    text = re.sub(r"\s+", " ", (caption or "").split("\n")[0]).strip()
    # Drop leading hashtag/mention TOKENS ("#ad Three NDA mistakes" -> "Three NDA mistakes"). Whole
    # tokens, not just the punctuation: stripping the "#" alone would title the card "ad Three NDA
    # mistakes". A caption that is nothing BUT hashtags keeps its last one rather than going blank.
    text = re.sub(r"^(?:[#@][\w.]+[ \t]+)+", "", text).strip()
    text = text or re.sub(r"\s+", " ", (caption or "")).strip()
    if not text:
        return ("Instagram post " + shortcode) if shortcode else "Instagram post"
    return text[:110].rstrip() + ("…" if len(text) > 110 else "")


def _iso_from_epoch(seconds):
    """An epoch timestamp -> 'YYYY-MM-DD' ('' when absent/unparseable)."""
    try:
        ts = int(seconds or 0)
    except (TypeError, ValueError):
        return ""
    if ts <= 0:
        return ""
    import datetime
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%d")


def _post_from_node(node):
    """One listing entry from a GraphQL/feed node ({} when it carries no shortcode)."""
    code = (node.get("shortcode") or node.get("code") or "").strip()
    if not code:
        return {}
    caption = _caption_of(node)
    published = _iso_from_epoch(node.get("taken_at_timestamp") or node.get("taken_at"))
    return {"id": code, "url": post_url(code), "title": caption_title(caption, code),
            "caption": caption, "published": published, "published_text": published,
            "is_video": bool(node.get("is_video") or node.get("video_versions")
                             or node.get("product_type") in ("clips", "igtv"))}


def list_posts(username, json_fetcher=None, fetcher=None):
    """Every post on `username` as {ok, posts, error, needs_session, source} (newest first).

    Two legs, in order:
      1. `web_profile_info` -- the profile blob, which already carries the newest ~12 posts. This is
         the leg that sometimes answers for a logged-out client, so it runs first and its posts are
         kept whatever happens next.
      2. the user FEED (`/api/v1/feed/user/<pk>/`), paged by `next_max_id` -- the only way to reach
         post 13 and beyond, and it needs the session cookie.

    A profile that yields ONLY leg 1 still succeeds, reporting `source` so the caller can say "the
    newest 12" rather than implying it archived everything."""
    username = (username or "").strip().lower().lstrip("@")
    if not username:
        return {"ok": False, "posts": [], "error": "No Instagram account given.",
                "needs_session": False, "source": ""}
    posts, seen = [], set()
    user, err = _profile_info(username, json_fetcher, fetcher)
    if user is None and err.get("fatal"):
        return {"ok": False, "posts": [], "error": err.get("error", ""),
                "needs_session": bool(err.get("needs_session")), "source": ""}
    user_pk = ""
    if user is not None:
        user_pk = str(user.get("id") or "").strip()
        media = (user.get("edge_owner_to_timeline_media") or {})
        for edge in (media.get("edges") or []):
            p = _post_from_node(edge.get("node") or {})
            if p and p["id"] not in seen:
                seen.add(p["id"])
                posts.append(p)

    if not session_configured():
        if posts:
            return {"ok": True, "posts": posts, "error": "", "needs_session": True,
                    "source": "public"}
        return {"ok": False, "posts": [], "needs_session": True, "source": "",
                "error": "Instagram won't list a profile for a logged-out server. Add an "
                         "`instagram-session` secret (see watcher_ig.py) to archive whole profiles, "
                         "or paste single post links instead."}

    feed_posts, feed_err = _walk_feed(user_pk or username, username, json_fetcher, fetcher)
    for p in feed_posts:
        if p["id"] not in seen:
            seen.add(p["id"])
            posts.append(p)
    if not posts:
        return {"ok": False, "posts": [], "source": "",
                "error": feed_err or "That account has no visible posts.",
                "needs_session": "session" in (feed_err or "")}
    posts.sort(key=lambda p: p.get("published") or "", reverse=True)
    return {"ok": True, "posts": posts[:MAX_POSTS], "error": "", "needs_session": False,
            "source": "feed" if feed_posts else "public"}


def _walk_feed(user_pk, username, json_fetcher=None, fetcher=None):
    """Page the authenticated user feed. Returns (posts, error) -- a partial walk still returns
    what it collected, because half an archive beats none (mirrors watcher.list_videos).

    🔴 Bounded by WALL CLOCK, not just by page count. `add_profile` is a single HTTP request and
    Cloud Run kills it at the service timeout; at 33 posts a page plus the pacing sleep, a prolific
    account would blow through that and the operator would get a dead request with NOTHING stored.
    Stopping at the budget archives what we have -- and "Check new" resumes from there."""
    if not re.fullmatch(r"\d+", str(user_pk or "")):
        return [], ("Instagram didn't return the account's internal id, so only its newest posts "
                    "could be listed.")
    fn = json_fetcher or _http_get_json
    ref = profile_url(username)
    out, cursor, pages = [], "", 0
    deadline = time.time() + LIST_BUDGET_SECONDS
    while pages < MAX_FEED_PAGES and len(out) < MAX_POSTS:
        url = "https://www.instagram.com/api/v1/feed/user/%s/?count=33" % user_pk
        if cursor:
            url += "&max_id=" + cursor
        try:
            data = fn(url, ref)
        except FetchError as exc:
            return out, _auth_error(exc, "that profile's posts")["error"]
        except Exception as exc:
            return out, "Listing stopped early (%s)." % exc.__class__.__name__
        items = (data or {}).get("items") or []
        for item in items:
            p = _post_from_node(item or {})
            if p:
                out.append(p)
        pages += 1
        cursor = str((data or {}).get("next_max_id") or "")
        if not (data or {}).get("more_available") or not cursor or not items:
            break
        if time.time() > deadline:
            # Not an error: the archive holds everything listed so far, and "Check new" picks up
            # the rest. Saying so beats silently implying the account only has this many posts.
            return out, ""
        time.sleep(_PAGE_PAUSE)
    return out, ""


# Instagram punishes bursts far harder than a website does, so every walk here is paced.
_PAGE_PAUSE = 1.2
# How long ONE listing request may spend paging before it banks what it has. Cloud Run's default
# request timeout is 300s and the caller still has to AI-label and write the archive afterwards.
LIST_BUDGET_SECONDS = 150


# --- 4. fetch_post: ONE post's caption + (for a reel) its spoken transcript ------------------------
_PERMANENT_STATUS = {
    404: "That post has been deleted or made private.",
    410: "That post is gone.",
    451: "That post is not available for legal reasons (451).",
}
_THROTTLE_STATUS = (401, 403, 408, 429, 500, 502, 503, 504)
# The two sections of the composed archive text. Labelled rather than concatenated so a human
# reading the modal (and the Assistant reading the chunk) can tell the writing from the speech.
_CAPTION_HEAD = "Caption:"
_SPOKEN_HEAD = "Spoken in this reel:"
_ALT_HEAD = "Shown in the image:"


def compose_body(caption="", spoken="", alt=""):
    """The archive `transcript` text for one post, from its parts ('' when there is nothing)."""
    blocks = []
    if (caption or "").strip():
        blocks.append("%s\n%s" % (_CAPTION_HEAD, caption.strip()[:MAX_CAPTION_CHARS]))
    if (alt or "").strip():
        blocks.append("%s\n%s" % (_ALT_HEAD, alt.strip()))
    if (spoken or "").strip():
        blocks.append("%s\n%s" % (_SPOKEN_HEAD, spoken.strip()))
    return "\n\n".join(blocks)


def _media_info(shortcode, json_fetcher=None):
    """The authenticated media-info blob for a post, or None.

    Richer than the embed page in every way that matters (full caption, real timestamp, the reel's
    playable video URL, the generated alt text) -- and it needs the session cookie, so it is tried
    first and quietly skipped when there is none."""
    pk = media_pk(shortcode)
    if not pk or not session_configured():
        return None
    fn = json_fetcher or _http_get_json
    try:
        data = fn("https://www.instagram.com/api/v1/media/%s/info/" % pk, post_url(shortcode))
    except Exception:
        return None    # best-effort: the embed page below is the backstop
    items = (data or {}).get("items") or []
    return items[0] if items and isinstance(items[0], dict) else None


_EMBED_JSON = re.compile(r"window\.__additionalDataLoaded\s*\(\s*'[^']*'\s*,\s*(\{.*?\})\s*\)\s*;",
                         re.S)


def _embed_page(shortcode, fetcher=None):
    """Parse the keyless /embed/captioned/ page into a media-ish dict, or None.

    This is the ONE Instagram endpoint that still serves a public post to a logged-out client, so it
    is the whole no-session story. The page carries a `__additionalDataLoaded` JSON blob for most
    posts (caption, owner, timestamp, video_url); when it doesn't, the visible caption markup is
    scraped as a last resort so a post still archives SOMETHING."""
    fn = fetcher or _http_get
    url = "https://www.instagram.com/p/%s/embed/captioned/" % shortcode
    html = fn(url, post_url(shortcode))
    if not html:
        return None
    m = _EMBED_JSON.search(html)
    if m:
        try:
            blob = json.loads(m.group(1))
        except ValueError:
            blob = {}
        media = blob.get("shortcode_media") or blob.get("graphql", {}).get("shortcode_media")
        if isinstance(media, dict):
            return media
    caption = _scrape_embed_caption(html)
    owner = re.search(r'class="[^"]*UsernameText[^"]*"[^>]*>([^<]+)<', html or "")
    if not caption and not owner:
        return None
    return {"shortcode": shortcode, "caption": caption,
            "owner": {"username": (owner.group(1).strip() if owner else "")}}


def _scrape_embed_caption(html):
    """The caption text out of the embed page's own markup ('' when it carries none)."""
    m = re.search(r'<div class="Caption"(.*?)</div>', html or "", re.S)
    if not m:
        return ""
    inner = m.group(1)
    # Drop the username link and the "view all N comments" tail the embed page appends, then strip
    # tags and unescape -- the caption is plain text in the archive.
    inner = re.sub(r'<a[^>]*class="[^"]*CaptionUsername[^"]*"[^>]*>.*?</a>', "", inner, flags=re.S)
    inner = re.sub(r'<div class="CaptionComments".*$', "", inner, flags=re.S)
    inner = re.sub(r"<br\s*/?>", "\n", inner)
    inner = re.sub(r"<[^>]+>", "", inner)
    import html as _html
    return re.sub(r"[ \t]+", " ", _html.unescape(inner)).strip()


def _video_url_of(media):
    """The playable mp4 URL for a reel/video post ('' for a photo or carousel-of-photos)."""
    for v in (media.get("video_versions") or []):
        if isinstance(v, dict) and v.get("url"):
            return v["url"]
    if media.get("video_url"):
        return media["video_url"]
    # A carousel: transcribe the first video child, which is what a reel-in-a-carousel actually is.
    for child in (media.get("carousel_media") or []):
        url = _video_url_of(child if isinstance(child, dict) else {})
        if url:
            return url
    return ""


def _alt_of(media):
    """Instagram's own generated alt text for the image ('' when absent)."""
    for key in ("accessibility_caption", "accessibility_text"):
        text = (media.get(key) or "").strip()
        if text:
            return text
    return ""


def fetch_post(url, json_fetcher=None, fetcher=None, media_fetcher=None, transcriber=None,
               transcribe=None, usage_out=None):
    """ONE post's full text: {ok, transcript, title, published, author, error, permanent, spoken}.

    Named `transcript` on purpose -- a post is stored in the same archive field a video transcript
    is, so every consumer (the reader modal, the Assistant index, the counts) works unchanged.

    Two lookups, richest first: the authenticated media-info endpoint, then the keyless embed page.
    A reel's audio is then transcribed by Vertex Gemini (best-effort -- a failed or skipped
    transcription still archives the caption, and `spoken` reports whether the words made it in)."""
    out = {"ok": False, "transcript": "", "title": "", "published": "", "author": "",
           "error": "", "permanent": False, "spoken": False}
    shortcode = extract_shortcode(url) or (url or "").strip()
    if not re.fullmatch(_SHORTCODE, shortcode or ""):
        out.update(error="That doesn't look like an Instagram post or reel link.", permanent=True)
        return out

    media = _media_info(shortcode, json_fetcher)
    if media is None:
        try:
            media = _embed_page(shortcode, fetcher)
        except FetchError as exc:
            if exc.status in _PERMANENT_STATUS:
                out.update(error=_PERMANENT_STATUS[exc.status], permanent=True)
            elif exc.status in _THROTTLE_STATUS:
                out["error"] = ("Instagram is rate-limiting or blocking this server right now — the "
                                "fetch will retry on its own.")
            else:
                out["error"] = "Could not fetch that post (%s)." % (exc.status or "network error")
            return out
        except Exception as exc:
            out["error"] = "Could not fetch that post (%s)." % exc.__class__.__name__
            return out
    if not media:
        out.update(error="Instagram returned nothing for that post — it may be private or deleted.",
                   permanent=True)
        return out

    caption = _caption_of(media)
    alt = _alt_of(media)
    out["published"] = _iso_from_epoch(media.get("taken_at_timestamp") or media.get("taken_at"))
    owner = media.get("owner") or media.get("user") or {}
    out["author"] = (owner.get("username") or "").strip() if isinstance(owner, dict) else ""
    out["title"] = caption_title(caption, shortcode)

    spoken = ""
    want = transcription_enabled() if transcribe is None else bool(transcribe)
    video_url = _video_url_of(media) if want else ""
    if video_url:
        spoken = _transcribe_video(video_url, media_fetcher, transcriber, usage_out)

    body = compose_body(caption, spoken, alt)
    if not body:
        out.update(error="That post has no caption, no alt text and no transcribable audio.",
                   permanent=True)
        return out
    out.update(ok=True, transcript=body, spoken=bool(spoken))
    return out


_TRANSCRIBE_PROMPT = (
    "Transcribe the spoken words in this video verbatim, in the language spoken. Return ONLY the "
    "transcript as plain prose — no timestamps, no speaker labels, no commentary, no preamble. If "
    "nobody speaks, return an empty response."
)


def _transcribe_video(video_url, media_fetcher=None, transcriber=None, usage_out=None):
    """The words spoken in a reel ('' when unavailable) -- ALWAYS best-effort, never raises.

    Reuses the Vertex plumbing intel_ai already owns (the runtime SA's own token: no new API, no new
    IAM, no new key). Skipped silently for an oversized video or missing credentials, because a
    caption-only archive is a perfectly good outcome and a failed transcription must never mark the
    post as failed."""
    try:
        data, mime = (media_fetcher or _http_get_bytes)(video_url)
    except Exception:
        return ""
    if not data:
        return ""     # oversized or empty: caption-only, by design
    fn = transcriber
    if fn is None:
        try:
            import intel_ai
        except Exception:
            return ""
        fn = intel_ai.transcribe_media
    try:
        text, err = fn(data, mime or "video/mp4", _TRANSCRIBE_PROMPT, usage_out=usage_out)
    except Exception:
        return ""
    if err:
        return ""
    return re.sub(r"\s+", " ", text or "").strip()


# --- 5. Batch fetching (the route's "Fetch missing" loop) -----------------------------------------
# SERIAL and paced, with NO parallel path -- unlike watcher_blog's modest concurrency. Instagram
# treats a burst from one IP as automation immediately, and a rotating proxy doesn't help the way it
# does for YouTube because the SESSION COOKIE is the thing being rate-limited, not the address.
POST_BATCH = 8
POST_PAUSE = 2.0
# 🔴 A WALL-CLOCK budget, and it is load-bearing here in a way it isn't for the other two sources.
# A reel costs a multi-MB download PLUS a Gemini transcription (tens of seconds each), so eight of
# them can easily outlive Cloud Run's 300s request timeout -- and because the archive is written
# only AFTER the batch returns, a timeout would throw away every transcript the batch had paid for.
# Stopping at the budget banks the work; the page's loop just calls again, which is exactly what it
# already does after every batch.
FETCH_BUDGET_SECONDS = 120


def _apply_post(v, result, now):
    """Record ONE post fetch onto an archive entry IN PLACE. Returns "done" or "blocked".

    Same contract as watcher._apply_result / watcher_blog._apply_post -- a throttling response
    leaves the entry PENDING (a session condition, not a fact about the post), so the next pass
    retries only what is missing. Like a blog post (and unlike a video), the fetch also yields the
    real title and date, so both are healed here."""
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
        v["generated"] = bool(result.get("spoken"))   # the words came from ASR, not from Instagram
        v["error"] = ""
        v["permanent"] = False
    else:
        v["error"] = result["error"]
        v["permanent"] = bool(result["permanent"])
    return "done"


def fetch_posts_batch(posts, limit=POST_BATCH, pause=POST_PAUSE, fetcher=None, json_fetcher=None,
                      media_fetcher=None, transcriber=None, usage_out=None,
                      budget_seconds=FETCH_BUDGET_SECONDS):
    """Fetch up to `limit` pending posts IN PLACE. Returns (fetched, blocked).

    Mirrors watcher_blog.fetch_posts_batch's contract (pending = no text and no error; `blocked`
    means the caller's loop should back off and retry the same set), so the route and the page's
    auto-retry loop are shared code. There is no parallel path on purpose -- see the notes above.

    Stops early once `budget_seconds` is spent so the caller can bank what it has (see
    FETCH_BUDGET_SECONDS); a short return is indistinguishable from a small batch to the page's
    loop, which simply calls again.

    `usage_out` ACCUMULATES the batch's transcription spend (one Gemini call per reel), because the
    caller banks it once per request into the client's tally -- a per-post dict would leave only the
    last reel's tokens standing."""
    from workspace import now_iso  # local import: avoids a cycle at module load
    pending = [v for v in posts if not (v.get("transcript") or v.get("error"))][:limit]
    if not pending:
        return 0, False
    deadline = time.time() + budget_seconds if budget_seconds else None
    done = 0
    for v in pending:
        one = {} if usage_out is not None else None
        result = fetch_post(v.get("url", "") or v.get("id", ""), json_fetcher=json_fetcher,
                            fetcher=fetcher, media_fetcher=media_fetcher, transcriber=transcriber,
                            usage_out=one)
        if one:
            add_usage(usage_out, one)
        if _apply_post(v, result, now_iso()) == "blocked":
            return done, True
        done += 1
        # Checked AFTER the write-in-place, so the item just paid for is always kept.
        if deadline is not None and time.time() > deadline:
            return done, False
        if pause:
            time.sleep(pause)
    return done, False


def add_usage(total, one):
    """Fold ONE transcription's token usage into a running `total` dict (mutates `total`)."""
    if not isinstance(total, dict) or not isinstance(one, dict):
        return total
    if one.get("model") and not total.get("model"):
        total["model"] = one["model"]
    for key in ("input_tokens", "output_tokens"):
        total[key] = int(total.get(key) or 0) + int(one.get(key) or 0)
    total["calls"] = int(total.get("calls") or 0) + 1
    return total


def post_entry(p):
    """A fresh archive entry for one listed post (no text yet) -- the Instagram twin of a video entry.

    The listing already knows the caption, but it is NOT stored as the body: "has a transcript" has
    to keep meaning "we fetched this one", or every post would count as done the moment it was
    listed and Fetch missing would have nothing to do."""
    code = (p.get("id") or "").strip()
    return {"id": code, "title": p.get("title") or caption_title(p.get("caption", ""), code),
            "url": p.get("url") or post_url(code),
            "transcript": "", "language": "", "generated": False,
            "error": "", "permanent": False, "fetched_at": "",
            "published_text": p.get("published_text", ""), "published": p.get("published", "")}
