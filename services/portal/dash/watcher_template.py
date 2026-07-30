"""The Watcher source TEMPLATE -- the default watched sources EVERY client gets, automatically.

Watcher used to start empty: a new client's tab held nothing until somebody remembered to paste
sources into it, so what we monitored for a client was per-client folklore. This module is the
catalog that fixes that -- the twin of `service_templates.py` (which seeds a task's work breakdown
exactly this way): a code-defined, git-versioned list of sources applied at ONBOARDING and
back-filled to every EXISTING client on their next team render.

TWO TIERS, because an archive is stored PER CLIENT:

  * SHARED (`shared: True`) -- a source byte-identical for every client (ad-platform news, our own
    mentor library). Fetched and stored ONCE for the whole estate: the entry id is deterministic
    (`workspace.shared_channel_id`) and carries `workspace.SHARED_PREFIX`, so
    `workspace.watcher_object_name` resolves it to the HOUSE workspace instead of the caller's.
    Fifteen clients watching Search Engine Land = one archive, one fetch, one Safe-pull slot --
    not fifteen copies of a multi-megabyte object, fifteen hits on the publisher and fifteen
    embedding bills.
  * PER-CLIENT (`shared: False`) -- a source whose archive genuinely belongs to one client. The
    template deliberately ships NONE of these: they are DERIVED per client (their local press,
    their named competitors, their trade titles) and proposed for a human to approve, which is a
    separate step and not this file's job.

SEGMENTS: `UNIVERSAL` applies to everyone; every other segment is keyed by industry and matched
loosely against the Company tab's free-text `industry` (`segments_for`). Onboarding only knows the
universal set (a brand-new workspace has no Company profile yet) -- the reconcile pass picks up the
industry segments on the first render after somebody fills the Company tab in, which is why the
reconcile runs on every team render rather than once at creation.

Adding a source = one dict in `_SOURCES` + a `TEMPLATE_VERSION` bump. Every client picks it up on
their next Watcher render. Removing a source from `_SOURCES` does NOT retract it from clients that
already have it (the reconcile is additive by design); delete it per client, which records the
opt-out so it never comes back.

🔴 BLOG SOURCES ONLY, deliberately. A website's posts fetch fine from Cloud Run -- no proxy, ~330
posts listed in ~2s -- while YouTube blocks datacenter IPs, so a YouTube template source would route
the entire template through the operator machine's Safe-pull queue (12-20s per video, one
residential IP) and swamp it for weeks. A YouTube source CAN be shared later, but
`safe_scrape_local.py` builds its archive path BY HAND (`workspace/watcher/<client>/<id>.json`,
never through `watcher_object_name`), so it has to learn the shared-id rule first or Safe pull will
write those transcripts where nothing reads them.

🔴 `kind` stays within the existing creator|competitor pair. The Watcher UI treats it as a two-state
TOGGLE (`atrium.html`: `current === "creator" ? "competitor" : "creator"`) with a hardcoded filter
dropdown and one CSS chip class per value, so a third kind ("authority" for a county alerts page,
"platform" for a Meta newsroom) is a template change, not a data change. Platform news is filed as
`creator` in the meantime -- something we learn from, as opposed to a rival.

Pure: no I/O, no network, no workspace access. `workspace.apply_watcher_template` does the writing
and `main._watcher_reconcile` wires the two together.
"""

# Bump whenever _SOURCES changes. Stored per client as ws["watcher"]["template"]["version"], purely
# as bookkeeping for "when did this client last match the catalog" -- the reconcile decides what to
# add by DIFFING template ids, never by comparing versions, so a client that skipped v2 still gets
# every v2 source when it lands on v3.
TEMPLATE_VERSION = 1

# The segment every client gets, no matter what they do.
UNIVERSAL = "universal"

# Free-text industry hints -> segment key. Matched as substrings against the Company tab's industry
# (lower-cased), so "RV resort & campground" and "Hospitality / tourism" both land on travel.
_SEGMENT_HINTS = {
    "travel-hospitality": (
        "travel", "tourism", "tourist", "hospitality", "hotel", "motel", "resort", "lodging",
        "campground", "camping", "campsite", "rv", "caravan", "vacation", "holiday", "destination",
        "attraction", "airbnb", "short-term rental", "str",
    ),
}

# Every template source. `template_id` is the STABLE reconcile key -- it is what tells a template
# entry from a hand-added one and what an opt-out is recorded against, so NEVER rename one (a rename
# reads as "old source removed, new source added" to every client). `channel_id` is the site origin,
# matching what watcher_blog.resolve_site returns, so a client who already added the site by hand is
# recognised and not given a duplicate. `industry` is pre-filled to skip the auto-label AI call.
_SOURCES = (
    # -- Universal: the ad-platform + search world every client of a marketing agency lives in. ---
    {
        "template_id": "search-engine-land",
        "segment": UNIVERSAL,
        "title": "Search Engine Land",
        "url": "https://searchengineland.com/",
        "channel_id": "https://searchengineland.com",
        "platform": "blog",
        "kind": "creator",
        "industry": "Search & Paid Media",
        "shared": True,
    },
    {
        "template_id": "search-engine-roundtable",
        "segment": UNIVERSAL,
        "title": "Search Engine Roundtable",
        "url": "https://www.seroundtable.com/",
        "channel_id": "https://www.seroundtable.com",
        "platform": "blog",
        "kind": "creator",
        "industry": "Search & Paid Media",
        "shared": True,
    },
    {
        "template_id": "google-ads-blog",
        "segment": UNIVERSAL,
        "title": "Google Ads & Commerce Blog",
        "url": "https://blog.google/products/ads-commerce/",
        "channel_id": "https://blog.google",
        "platform": "blog",
        "kind": "creator",
        "industry": "Paid Media",
        "shared": True,
    },
    {
        "template_id": "meta-newsroom",
        "segment": UNIVERSAL,
        "title": "Meta Newsroom",
        "url": "https://about.fb.com/news/",
        "channel_id": "https://about.fb.com",
        "platform": "blog",
        "kind": "creator",
        "industry": "Paid Media",
        "shared": True,
    },
    {
        "template_id": "tiktok-newsroom",
        "segment": UNIVERSAL,
        "title": "TikTok Newsroom",
        "url": "https://newsroom.tiktok.com/",
        "channel_id": "https://newsroom.tiktok.com",
        "platform": "blog",
        "kind": "creator",
        "industry": "Paid Media",
        "shared": True,
    },
    # -- Travel & hospitality (Riverdance and any other destination business). --------------------
    {
        "template_id": "skift",
        "segment": "travel-hospitality",
        "title": "Skift",
        "url": "https://skift.com/",
        "channel_id": "https://skift.com",
        "platform": "blog",
        "kind": "creator",
        "industry": "Travel & Hospitality",
        "shared": True,
    },
    {
        "template_id": "rvbusiness",
        "segment": "travel-hospitality",
        "title": "RVBusiness",
        "url": "https://rvbusiness.com/",
        "channel_id": "https://rvbusiness.com",
        "platform": "blog",
        "kind": "creator",
        "industry": "Travel & Hospitality",
        "shared": True,
    },
)


def segments(ws_industry=None):
    """Every segment key the catalog defines (universal first, then the rest, stable order)."""
    seen, out = set(), [UNIVERSAL]
    seen.add(UNIVERSAL)
    for src in _SOURCES:
        seg = src.get("segment") or UNIVERSAL
        if seg not in seen:
            seen.add(seg)
            out.append(seg)
    return tuple(out)


def segments_for(industry):
    """The segments that apply to a client whose Company-tab industry reads `industry` (free text).

    Always includes UNIVERSAL, so a client with a blank or unrecognised industry still gets the
    ad-platform sources -- the template is never empty. Matching is substring-based on purpose:
    industry is typed by a human, not picked from a list."""
    text = (industry or "").strip().lower()
    out = [UNIVERSAL]
    for seg, hints in sorted(_SEGMENT_HINTS.items()):
        if any(h in text for h in hints):
            out.append(seg)
    return tuple(out)


def sources_for(segs):
    """Every source belonging to `segs` (a segment key or an iterable of them), catalog order.

    Returns fresh dicts -- the caller writes them into a workspace, so the catalog must not be
    mutable through the return value."""
    if isinstance(segs, str):
        segs = (segs,)
    wanted = set(segs or ())
    return [dict(s) for s in _SOURCES if (s.get("segment") or UNIVERSAL) in wanted]


def find(template_id):
    """One source by its stable `template_id` (a fresh dict), or None."""
    tid = (template_id or "").strip()
    for s in _SOURCES:
        if s["template_id"] == tid:
            return dict(s)
    return None


def catalog():
    """The whole catalog as fresh dicts (for a settings/admin surface listing what we monitor)."""
    return [dict(s) for s in _SOURCES]
