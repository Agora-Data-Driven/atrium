"""Agora Atrium workspace store -- per-client CRUD over `workspace/<c>.json` (no database).

Atrium is the co-branded client workspace that grows the portal into a CRM. Each client's
workspace state lives in ONE private JSON object in the portal's EXISTING bucket
(agora-data-driven-platform-dash) under the `workspace/` prefix:

    workspace/<c>.json

This mirrors store.py's load-modify-save, last-write-wins pattern, but ONE object PER CLIENT
(so two clients' workspaces never contend on the same object). No new bucket, SA, IAM, or
service: the platform-dash runtime SA already has objectAdmin on this bucket.

Storage backend (selected by env, so this is testable OFF-cloud):
  * Default -- Google Cloud Storage. `google-cloud-storage` is imported LAZILY (only when the GCS
    backend is actually used), so a local test never needs the package or ADC configured.
  * Local  -- set WORKSPACE_LOCAL_DIR=<dir> to read/write plain JSON files under that directory
    instead of GCS. This lets you develop and smoke-test on a laptop WITHOUT touching the real
    bucket (see seed_workspace.py / _workspace_localtest.py).

Env overrides (all optional; the defaults are the literal standup values):
  * WORKSPACE_BUCKET  -- bucket to use (defaults to REGISTRY_BUCKET, the portal's private bucket).
  * WORKSPACE_PREFIX  -- object-name prefix (default "workspace/").
  * WORKSPACE_LOCAL_DIR -- if set, use the local-filesystem backend rooted at this directory.

All timestamps are UTC ISO-8601 with a trailing Z, matching feedback.py / the freshness contract.
"""

import datetime
import json
import os
import re
import uuid


# --- Config (read live from the env so tests can set it before the first call) ------------------
def _local_dir():
    """The local-filesystem backend root, or "" to use GCS."""
    return os.environ.get("WORKSPACE_LOCAL_DIR", "")


def _bucket_name():
    """The bucket holding workspace/<c>.json -- defaults to the portal's private registry bucket."""
    return (
        os.environ.get("WORKSPACE_BUCKET")
        or os.environ.get("REGISTRY_BUCKET")
        or "agora-data-driven-platform-dash"
    )


def _prefix():
    """Object-name prefix for workspace objects (keeps them grouped in the shared bucket)."""
    return os.environ.get("WORKSPACE_PREFIX", "workspace/")


def _object_name(client):
    """The object name for a client's workspace, e.g. 'workspace/riverdance.json'."""
    return "%s%s.json" % (_prefix(), client)


# --- Timestamp helpers (UTC, matching the rest of the contract) ---------------------------------
def now_iso():
    """UTC, second precision, ISO-8601 with a trailing Z (e.g. '2026-06-20T09:12:00Z')."""
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def now_label():
    """A friendly activity label like 'Today, 9:12 AM' (UTC clock)."""
    t = datetime.datetime.now(datetime.timezone.utc)
    return "Today, " + t.strftime("%I:%M %p").lstrip("0")


# --- Storage backend (GCS by default; local filesystem when WORKSPACE_LOCAL_DIR is set) ---------
_storage_client = None


def _gcs_client():
    """Lazily construct and cache a GCS client (so importing this module never needs ADC)."""
    global _storage_client
    if _storage_client is None:
        from google.cloud import storage  # lazy: only the GCS backend needs the package
        _storage_client = storage.Client()
    return _storage_client


def _read_object(name):
    """Return the raw bytes of object `name`, or None if it does not exist."""
    local = _local_dir()
    if local:
        path = os.path.join(local, name)
        if not os.path.isfile(path):
            return None
        with open(path, "rb") as fh:
            return fh.read()
    # ONE GCS round-trip: download and treat a missing object as None. The old exists()-then-download
    # did two round-trips on a per-request hot path (every workspace load reads its <c>.json here).
    from google.cloud.exceptions import NotFound  # lazy: only the GCS backend needs the package
    blob = _gcs_client().bucket(_bucket_name()).blob(name)
    try:
        return blob.download_as_bytes()
    except NotFound:
        return None


def _write_object(name, data, content_type="application/json"):
    """Write `data` (bytes) to object `name`, creating parent dirs for the local backend.

    `content_type` defaults to JSON (the workspace objects); pass an image mime when storing an
    uploaded creative so the GCS blob carries the right Content-Type.
    """
    local = _local_dir()
    if local:
        path = os.path.join(local, name)
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(data)
        return
    blob = _gcs_client().bucket(_bucket_name()).blob(name)
    blob.upload_from_string(data, content_type=content_type)


def _delete_object(name):
    """Delete object `name` if it exists (no error if it is already gone)."""
    local = _local_dir()
    if local:
        path = os.path.join(local, name)
        if os.path.isfile(path):
            os.remove(path)
        return
    blob = _gcs_client().bucket(_bucket_name()).blob(name)
    if blob.exists():
        blob.delete()


# --- Workspace I/O ------------------------------------------------------------------------------
def load_workspace(client):
    """Return the workspace dict for `client`, or None if it has not been seeded yet."""
    raw = _read_object(_object_name(client))
    if raw is None:
        return None
    return json.loads(raw.decode("utf-8"))


def save_workspace(client, ws):
    """Persist the workspace dict back to workspace/<c>.json (private; never made public)."""
    body = json.dumps(ws, indent=2, sort_keys=True).encode("utf-8")
    _write_object(_object_name(client), body)
    return ws


def workspace_exists(client):
    """True iff a workspace object already exists for `client` (used by the seed clobber-guard)."""
    return _read_object(_object_name(client)) is not None


def delete_workspace(client):
    """Delete a client's workspace JSON object (no error if absent). Used when removing a client.

    Only removes the workspace document itself; any uploaded creatives live under their own
    'workspace/creatives/<client>/' prefix and are left for a separate sweep (the bucket is private,
    so orphaned objects are inert -- never publicly reachable)."""
    _delete_object(_object_name(client))


def set_client_logo(client, logo_markup):
    """Replace the client's logo (brand.client_logo) with `logo_markup`, leaving everything else
    untouched. `logo_markup` is self-contained HTML/SVG (e.g. an <img> data: URI) rendered with
    |safe in the workspace + team console. Returns the new markup. Raises KeyError if no workspace."""
    def fn(ws):
        ws.setdefault("brand", {})["client_logo"] = logo_markup
        return ws["brand"]["client_logo"]
    return _mutate(client, fn)


# --- Website Health (team-only tab: site monitoring + tag detection) -----------------------------
# All state lives under one key, ws["website_health"] = {url, notes, last_check}. The last_check dict
# is the render-ready result from atrium_health.check_website (kept verbatim so the tab renders it).
def set_website_url(client, url):
    """Set the monitored website URL for the Website Health tab. Returns the stored url."""
    def fn(ws):
        wh = ws.setdefault("website_health", {})
        wh["url"] = (url or "").strip()
        return wh["url"]
    return _mutate(client, fn)


def set_website_notes(client, notes):
    """Set the team's free-text notes shown on the Website Health tab. Returns the stored notes."""
    def fn(ws):
        wh = ws.setdefault("website_health", {})
        wh["notes"] = notes or ""
        return wh["notes"]
    return _mutate(client, fn)


def save_website_check(client, result):
    """Store the latest health-check result (and the url it ran against). Returns the result."""
    def fn(ws):
        wh = ws.setdefault("website_health", {})
        if (result or {}).get("url"):
            wh["url"] = result["url"]
        wh["last_check"] = result or {}
        return wh["last_check"]
    return _mutate(client, fn)


# --- Watcher (team-only tab: watched YouTube channels + their transcript archives) ---------------
# The channel REGISTRY is small and lives in the workspace JSON: ws["watcher"]["channels"] =
# [{id, url, title, channel_id, video_count, transcript_count, failed_count, last_fetch, added_at}].
# Each channel's full video list + transcripts is its OWN private object (a big channel's archive
# runs to megabytes; keeping it out of workspace/<c>.json keeps the rewrite-in-full document small,
# mirroring the uploaded-creatives posture):
#     workspace/watcher/<c>/<channel_id>.json  ->  {"videos": [{id, title, url, transcript, ...}]}
def watcher_channels(ws):
    """The watched-channel registry list (never None)."""
    return list(((ws or {}).get("watcher") or {}).get("channels") or [])


def find_watcher_channel(ws, channel_id):
    """The registry entry with id `channel_id`, or None."""
    for ch in watcher_channels(ws):
        if ch.get("id") == channel_id:
            return ch
    return None


# --- Shared (estate-wide) archives ---------------------------------------------------------------
# A TEMPLATE source that is byte-identical for every client (ad-platform news, our own mentor
# library) is fetched and stored ONCE, not once per client: fifteen clients watching Search Engine
# Land would otherwise mean fifteen copies of a multi-megabyte archive, fifteen hits on a publisher
# who currently tolerates us, and fifteen embedding bills for the same text.
# The sharing is encoded in the ENTRY ID rather than in a flag, which is what keeps it cheap: a
# shared entry's id is deterministic and carries SHARED_PREFIX, so every archive caller in the app --
# the tab render, the fetch loop, the Sentinel bridge, the Assistant index -- resolves the right
# object through watcher_object_name below with NO signature change and no extra read.
SHARED_PREFIX = "wsh_"
# The workspace that OWNS every shared archive. Defaults to `agora` -- the same house workspace
# Sentinel's Mentor Library already reads over the internal bridge (ATRIUM_WATCHER_CLIENT_KEY), so
# reading one client's Watcher archive from another workspace is a pattern already in production.
HOUSE_CLIENT = os.environ.get("WATCHER_HOUSE_CLIENT", "agora").strip() or "agora"


def is_shared_channel_id(channel_id):
    """True iff this registry-entry id names a SHARED archive (owned by the house workspace)."""
    return (channel_id or "").startswith(SHARED_PREFIX)


def shared_channel_id(template_id):
    """The deterministic entry id for a shared template source -- the SAME id in every client.

    Deterministic on purpose, twice over: it makes apply_watcher_template naturally idempotent (a
    re-apply finds the entry already present), and it is what lets ONE stored archive serve every
    client without a lookup table anywhere."""
    slug = re.sub(r"[^a-z0-9]+", "-", (template_id or "").lower()).strip("-")
    return SHARED_PREFIX + slug


def watcher_object_name(client, channel_id):
    """Object name for one channel's archive, e.g. 'workspace/watcher/riverdance/wch_1a2b3c4d.json'.

    🔴 A SHARED entry (see SHARED_PREFIX) resolves to the HOUSE workspace, never the caller's -- that
    one redirect is the whole mechanism that makes a single archive serve every client. Every archive
    read/write in the app funnels through here, so shared sources work everywhere for free.
    ⚠️ EXCEPT `safe_scrape_local.py`, which builds this path BY HAND and never calls this function:
    teach it the same rule before shipping a shared YOUTUBE source, or Safe pull writes the
    transcripts under the requesting client and the app reads the house copy forever."""
    owner = HOUSE_CLIENT if is_shared_channel_id(channel_id) else client
    return "%swatcher/%s/%s.json" % (_prefix(), owner, channel_id)


def read_watcher_videos(client, channel_id):
    """The channel's stored video list (with transcripts), or [] when nothing is stored yet."""
    raw = _read_object(watcher_object_name(client, channel_id))
    if raw is None:
        return []
    try:
        return json.loads(raw.decode("utf-8")).get("videos") or []
    except (ValueError, AttributeError):
        return []


def write_watcher_videos(client, channel_id, videos):
    """Persist the channel's full video list (its own object, NOT the workspace JSON)."""
    body = json.dumps({"videos": videos}, indent=2, sort_keys=True).encode("utf-8")
    _write_object(watcher_object_name(client, channel_id), body)


def _watcher_entry(fields):
    """One watched-source registry entry (pure) -- the ONE place the entry shape is defined.

    Two fields exist for the source TEMPLATE. `entry_id` lets a caller FORCE the id, which the
    template does so a shared source carries the same deterministic id in every client (there is no
    separate `shared` flag to keep in sync -- is_shared_channel_id(entry["id"]) is the answer).
    `template_id` marks the entry as template-sourced: it is how the reconcile pass tells its own
    entries from hand-added ones, and what an opt-out gets recorded against on delete."""
    return {
        "id": (fields.get("entry_id") or "").strip() or _new_id("wch"),
        "url": fields.get("url", ""),
        "title": fields.get("title", ""),
        "channel_id": fields.get("channel_id", ""),
        # Classification: `platform` is the SOURCE TYPE and decides which fetcher runs for this
        # entry -- "youtube" (watcher.py: channel_id -> videos -> transcripts) or "blog"
        # (watcher_blog.py: site origin -> posts -> article text). Both store into the same archive
        # shape, so only the fetch/refresh branch cares. `industry` is the auto/AI label
        # (hand-editable); `kind` separates creators we learn from from rivals ("competitor").
        "platform": fields.get("platform", "youtube"),
        "industry": fields.get("industry", ""),
        "kind": fields.get("kind", "creator"),
        "template_id": fields.get("template_id", ""),
        "video_count": int(fields.get("video_count", 0) or 0),
        "transcript_count": 0,
        "failed_count": 0,
        "last_fetch": "",
        "added_at": now_iso(),
    }


def add_watcher_channel(client, fields):
    """Append a watched channel to the registry (newest first). Returns the stored entry."""
    entry = _watcher_entry(fields)

    def fn(ws):
        ws.setdefault("watcher", {}).setdefault("channels", []).insert(0, entry)
        return entry
    return _mutate(client, fn)


# The single-item scrapers have no parent channel/site, so their items are archived under ONE
# per-client pseudo-channel PER PLATFORM (marked `loose`): "Saved videos" for one-off YouTube links
# and "Saved articles" for one-off blog links. Each renders as a normal card and the Assistant
# indexes it like any other channel; only the source-wide actions (Check new / Auto-label) are
# hidden for it in the UI. `list_videos`/`list_posts` never run against it (its channel_id is "").
LOOSE_CHANNEL_TITLE = "Saved videos"
LOOSE_BLOG_TITLE = "Saved articles"


def ensure_loose_channel(client, platform="youtube"):
    """Find (or create, newest-first) the per-client loose pseudo-channel for `platform`.

    Keyed on platform as well as the `loose` marker so a saved article never lands in the video
    archive (their fetchers are different: one takes a video id, the other a page URL)."""
    def fn(ws):
        channels = ws.setdefault("watcher", {}).setdefault("channels", [])
        for ch in channels:
            if ch.get("loose") and (ch.get("platform") or "youtube") == platform:
                return ch
        entry = {
            "id": _new_id("wch"),
            "url": "",
            "title": LOOSE_BLOG_TITLE if platform == "blog" else LOOSE_CHANNEL_TITLE,
            "channel_id": "",
            "platform": platform, "industry": "", "kind": "creator", "loose": True,
            "video_count": 0, "transcript_count": 0, "failed_count": 0,
            "last_fetch": "", "added_at": now_iso(),
        }
        channels.insert(0, entry)
        return entry
    return _mutate(client, fn)


def update_watcher_channel(client, channel_id, fields):
    """Merge `fields` into a registry entry (counts / last_fetch / title). Returns the entry."""
    def fn(ws):
        # watcher_channels copies the list but not the dicts, so this is the live entry.
        ch = find_watcher_channel(ws, channel_id)
        if ch is None:
            raise KeyError("no watcher channel '%s'" % channel_id)
        ch.update(fields)
        return ch
    return _mutate(client, fn)


def delete_watcher_channel(client, channel_id):
    """Remove a channel from the registry AND delete its archive object. Returns the removed entry
    (or None if it wasn't there).

    Two TEMPLATE rules ride along:
    🔴 Removing a template source RECORDS THE OPT-OUT (ws["watcher"]["template"]["removed"]) so the
       reconcile pass never re-adds it. Without this the team deletes an irrelevant source, the next
       render puts it back, and they spend the week fighting us.
    🔴 A SHARED archive is NEVER deleted: it lives in the house workspace and still serves every
       other client, so this only drops THIS client's registry entry."""
    def fn(ws):
        channels = (ws.get("watcher") or {}).get("channels") or []
        for i, ch in enumerate(channels):
            if ch.get("id") == channel_id:
                removed = channels.pop(i)
                tid = (removed.get("template_id") or "").strip()
                if tid:
                    out = ws.setdefault("watcher", {}).setdefault("template", {}).setdefault("removed", [])
                    if tid not in out:
                        out.append(tid)
                return removed
        return None
    removed = _mutate(client, fn)
    if not is_shared_channel_id(channel_id):
        _delete_object(watcher_object_name(client, channel_id))
    return removed


# --- The Watcher source TEMPLATE (the default sources every client gets) -------------------------
# The catalog itself lives in watcher_template.py (pure, git-versioned, no workspace import); these
# three functions are the workspace side of it: what a client is MISSING (pure), the additive write
# that fixes it, and the bookkeeping read.
#
# 🔴 The reconcile is REGISTRY ONLY -- it adds entries and never fetches. That split is what makes
# "applies to every client automatically" safe: creating an entry is a few bytes, while filling its
# archive is a multi-megabyte network crawl, and the tab's existing "Fetch missing" / Safe-pull loops
# already know how to do that on a schedule that respects publishers. So this can run on every team
# render (it is a pure set-difference, and writes nothing once applied) instead of needing a job.
def watcher_template_state(ws):
    """The client's template bookkeeping: {"version": n, "applied_at": iso, "removed": [ids]}."""
    tpl = ((ws or {}).get("watcher") or {}).get("template") or {}
    return {"version": int(tpl.get("version") or 0),
            "applied_at": tpl.get("applied_at") or "",
            "removed": list(tpl.get("removed") or [])}


def pending_watcher_template(ws, sources):
    """The template `sources` this client is missing (pure -- no I/O). Feed straight to apply_…().

    Skipped: a source already in the registry by `template_id`, a source whose site the team already
    added BY HAND (matched on channel_id, so the template never plants a duplicate of a source they
    beat us to), and anything on the opt-out list."""
    removed = set(watcher_template_state(ws)["removed"])
    have_tpl, have_cid = set(), set()
    for ch in watcher_channels(ws):
        if ch.get("template_id"):
            have_tpl.add(ch["template_id"])
        if ch.get("channel_id"):
            have_cid.add((ch["channel_id"] or "").strip().lower().rstrip("/"))
    out = []
    for src in sources or []:
        tid = (src.get("template_id") or "").strip()
        if not tid or tid in removed or tid in have_tpl:
            continue
        cid = (src.get("channel_id") or "").strip().lower().rstrip("/")
        if cid and cid in have_cid:
            continue
        out.append(src)
    return out


def apply_watcher_template(client, sources, version=0):
    """Add every missing template source to `client`'s registry in ONE write. Returns what was added.

    Additive and idempotent: a hand-added source is never touched, an opted-out source never returns,
    and re-running against the same catalog adds nothing. The missing set is recomputed INSIDE the
    mutation because storage is last-write-wins -- the caller's pre-check can be stale by now. A
    shared source's id is derived here (not in the catalog) so watcher_template.py stays pure."""
    def fn(ws):
        todo = pending_watcher_template(ws, sources)
        w = ws.setdefault("watcher", {})
        channels = w.setdefault("channels", [])
        added = []
        for src in todo:
            fields = dict(src)
            if src.get("shared"):
                fields["entry_id"] = shared_channel_id(src["template_id"])
            entry = _watcher_entry(fields)
            channels.insert(0, entry)
            added.append(entry)
        tpl = w.setdefault("template", {})
        tpl["version"] = int(version or 0)
        tpl["applied_at"] = now_iso()
        tpl.setdefault("removed", [])
        return added
    return _mutate(client, fn)


def watcher_safe_pull_queue(ws):
    """Channel ids queued for the local safe scraper (never None).

    The Safe-pull button can't fetch from Cloud Run (YouTube blocks datacenter IPs regardless of
    pacing), so it queues the channel here and the operator's machine works through the queue with
    slow, polite pacing (safe_scrape_local.py --queue, run by a scheduled task)."""
    return list(((ws or {}).get("watcher") or {}).get("safe_pull") or [])


def queue_watcher_safe_pull(client, channel_id):
    """Add a channel to the safe-pull queue (idempotent). Returns the queue."""
    def fn(ws):
        w = ws.setdefault("watcher", {})
        queue = w.setdefault("safe_pull", [])
        if channel_id not in queue:
            queue.append(channel_id)
        return list(queue)
    return _mutate(client, fn)


def clear_watcher_safe_pull(client, channel_id):
    """Drop a channel from the safe-pull queue (the scraper finished it, or the channel is gone)."""
    def fn(ws):
        w = ws.setdefault("watcher", {})
        w["safe_pull"] = [c for c in (w.get("safe_pull") or []) if c != channel_id]
        return list(w["safe_pull"])
    return _mutate(client, fn)


# The local safe scraper (safe_scrape_local.py, on the operator's machine) writes ONE global
# heartbeat object as it works so the Watcher tab can show live progress instead of "check back
# later". It is NOT under workspace/watcher/<c>/ (that prefix is the per-client archive folders the
# scraper globs), so it never looks like a client slug.
def safe_pull_status_name():
    """Object name for the global safe-scraper heartbeat, e.g. 'workspace/watcher_safe_pull_status.json'."""
    return "%swatcher_safe_pull_status.json" % _prefix()


def read_safe_pull_status():
    """The local safe scraper's last heartbeat dict ({} if it has never run). Best-effort: a
    missing/corrupt object just reads as {} so the status route always answers."""
    raw = _read_object(safe_pull_status_name())
    if raw is None:
        return {}
    try:
        return json.loads(raw.decode("utf-8")) or {}
    except (ValueError, AttributeError):
        return {}


# --- Reports (client-visible tab: every meeting deck, date-first) --------------------------------
# The small per-report index rides in workspace/<c>.json (ws["reports"], newest first: id, title,
# date, origin, payload -- the structured slide content the generator/editor works on); each deck's
# rendered self-contained HTML is its OWN object (a deck runs to hundreds of KB -- same posture as
# creatives/watcher/mail archives), served ONLY through the authed /w/<c>/report/<id> route.
def reports_of(ws):
    """The workspace's report index entries (never None)."""
    return list((ws or {}).get("reports") or [])


def find_report(ws, report_id):
    """The report index entry with id `report_id`, or None."""
    for r in (ws or {}).get("reports") or []:
        if r.get("id") == report_id:
            return r
    return None


def report_object_name(client, report_id):
    """One report's rendered-deck object, e.g. 'workspace/reports/riverdance/rp_1a2b3c4d.html'."""
    return "%sreports/%s/%s.html" % (_prefix(), client, report_id)


def read_report_html(client, report_id):
    """The stored deck HTML (str), or None when it doesn't exist / can't decode."""
    raw = _read_object(report_object_name(client, report_id))
    if raw is None:
        return None
    try:
        return raw.decode("utf-8")
    except (UnicodeDecodeError, AttributeError):
        return None


def write_report_html(client, report_id, html):
    """Persist a report's rendered deck (its own object, NOT the workspace JSON)."""
    _write_object(report_object_name(client, report_id), (html or "").encode("utf-8"),
                  content_type="text/html; charset=utf-8")


def add_report(client, title, date, payload=None, origin="ai", report_id=None):
    """Create a report index entry (newest first). Returns it. The caller renders + writes the
    deck HTML separately (write_report_html) -- index and object are two writes on purpose, so a
    failed render never strands a phantom index row ahead of it."""
    entry = {
        "id": report_id or _new_id("rp"),
        "title": title or "Performance review",
        "date": (date or now_iso())[:10],
        "origin": origin or "ai",
        "payload": payload or {},
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }

    def fn(ws):
        ws.setdefault("reports", []).insert(0, entry)
        return entry
    return _mutate(client, fn)


def update_report(client, report_id, fields):
    """Patch a report entry's title/date/payload in place. Returns it (KeyError if missing)."""
    def fn(ws):
        entry = find_report(ws, report_id)
        if entry is None:
            raise KeyError("no report '%s'" % report_id)
        for k in ("title", "date", "payload"):
            if k in (fields or {}):
                entry[k] = fields[k]
        entry["updated_at"] = now_iso()
        return entry
    return _mutate(client, fn)


def delete_report(client, report_id):
    """Remove a report entry AND its deck object. Returns the removed entry (for the Trash)."""
    def fn(ws):
        reports = ws.get("reports", [])
        entry = next((r for r in reports if r.get("id") == report_id), None)
        if entry is not None:
            reports.remove(entry)
        return entry
    entry = _mutate(client, fn)
    if entry is not None:
        _delete_object(report_object_name(client, report_id))
    return entry


def insert_report(client, entry):
    """Re-insert a previously removed report entry (Trash restore; the caller re-renders the
    deck HTML from the entry's stored payload). Returns the entry."""
    def fn(ws):
        ws.setdefault("reports", []).insert(0, entry)
        return entry
    return _mutate(client, fn)


# --- Assistant (team-only tab: the workspace knowledge index) ------------------------------------
# The Assistant's retrieval index (chunks + BM25 stats over every workspace source) is ONE private
# object per client, rebuilt lazily when its fingerprint stops matching the live data. Like the
# watcher archives it stays OUT of workspace/<c>.json (it can run to many MB).
def assistant_index_name(client):
    """Object name for a client's assistant index, e.g. 'workspace/assistant/riverdance/index.json'."""
    return "%sassistant/%s/index.json" % (_prefix(), client)


def read_assistant_index(client):
    """The stored assistant index dict, or None when it hasn't been built yet (or is corrupt)."""
    raw = _read_object(assistant_index_name(client))
    if raw is None:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, AttributeError):
        return None


def write_assistant_index(client, index):
    """Persist the assistant index (its own object, NOT the workspace JSON)."""
    _write_object(assistant_index_name(client),
                  json.dumps(index, sort_keys=True).encode("utf-8"))


# The Assistant's saved CONVERSATIONS (team-shared chat history) are ONE private object per client,
# separate from workspace/<c>.json (they can grow with long transcripts of Q&A). Team-shared: any
# admin sees the same history for a client. Capped so the object stays small.
ASSISTANT_MAX_CONVERSATIONS = 60
ASSISTANT_MAX_TURNS = 60


def assistant_conversations_name(client):
    """Object name for a client's saved Assistant conversations."""
    return "%sassistant/%s/conversations.json" % (_prefix(), client)


def read_assistant_conversations(client):
    """The stored conversations dict `{"conversations": [...]}` (empty shape when none/corrupt)."""
    raw = _read_object(assistant_conversations_name(client))
    if raw is None:
        return {"conversations": []}
    try:
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) and isinstance(data.get("conversations"), list) \
            else {"conversations": []}
    except (ValueError, AttributeError):
        return {"conversations": []}


def _conversation_meta(c):
    """The list-view fields of a conversation (no turns) -- what history_list returns."""
    return {"id": c.get("id", ""), "title": c.get("title", "") or "Conversation",
            "updated": c.get("updated", ""), "created": c.get("created", ""),
            "turn_count": len(c.get("turns") or [])}


def list_assistant_conversations(client):
    """Metadata for every saved conversation, WITHOUT the turns (small payload). The stored list is
    kept newest-first by `save_assistant_conversation` (front-insertion), so no re-sort is needed --
    which also avoids ties when several are saved in the same second."""
    data = read_assistant_conversations(client)
    return [_conversation_meta(c) for c in (data.get("conversations") or [])]


def get_assistant_conversation(client, conv_id):
    """One full conversation (with turns) by id, or None."""
    for c in read_assistant_conversations(client).get("conversations") or []:
        if c.get("id") == conv_id:
            return c
    return None


def save_assistant_conversation(client, conv_id, title, turns):
    """Upsert a conversation by id (create or replace its turns/title/updated). Returns the metadata
    list (newest first). Turns are capped; the conversation list is capped to the newest N."""
    if not conv_id:
        return list_assistant_conversations(client)
    turns = [{"role": ("user" if (t or {}).get("role") == "user" else "bot"),
              "text": str((t or {}).get("text") or "")}
             for t in (turns or [])][-ASSISTANT_MAX_TURNS:]
    data = read_assistant_conversations(client)
    convs = data.get("conversations") or []
    now = now_iso()
    title = (title or "").strip()[:120] or "Conversation"
    created = now
    for c in convs:                       # preserve the original created stamp on an upsert
        if c.get("id") == conv_id:
            created = c.get("created") or now
            break
    rest = [c for c in convs if c.get("id") != conv_id]
    # Front-insert (newest first, deterministic regardless of same-second timestamps), then cap.
    convs = ([{"id": conv_id, "title": title, "turns": turns, "created": created, "updated": now}]
             + rest)[:ASSISTANT_MAX_CONVERSATIONS]
    data["conversations"] = convs
    _write_object(assistant_conversations_name(client),
                  json.dumps(data).encode("utf-8"))
    return list_assistant_conversations(client)


def delete_assistant_conversation(client, conv_id):
    """Remove a saved conversation by id. Returns the remaining metadata list."""
    data = read_assistant_conversations(client)
    data["conversations"] = [c for c in (data.get("conversations") or [])
                             if c.get("id") != conv_id]
    _write_object(assistant_conversations_name(client),
                  json.dumps(data).encode("utf-8"))
    return list_assistant_conversations(client)


# --- Mail (team-only tab: client email archive + AI digest) --------------------------------------
# Two layers, mirroring the Watcher posture exactly:
#   * The GLOBAL mailbox registry (which agency mailboxes are connected, agency-wide -- NOT
#     per-client) is ONE private object 'workspace/mail/_mailboxes.json' in the same registry
#     bucket. An imap entry carries its app password VERBATIM (it must be replayable to log in);
#     the object is private, read only by the runtime SA, and the password is NEVER rendered back
#     to any page (public_mailboxes strips it). Same storage posture as store.py's registry.
#   * Per client: a SMALL thread index in ws["mail"]["threads"] (subject/participants/dates/
#     summary -- never bodies) plus each thread's full message archive as its OWN object
#     'workspace/mail/<c>/<thread_key>.json' (bodies run long; the rewrite-in-full workspace JSON
#     stays small). ws["mail"] also holds `contacts` (the client's email addresses/domains that
#     drive the Gmail query), the rolling AI `digest`, and last_sync/last_error stamps.
MAIL_KINDS = ("dwd", "imap")
_MAIL_THREAD_CAP = 300          # index entries per client; oldest drop off (their objects deleted)
_MAIL_CONTACT_CAP = 40


def mail_registry_object_name():
    """The global connected-mailboxes object (the leading '_' can never collide with a client key)."""
    return "%smail/_mailboxes.json" % _prefix()


def mail_mailboxes():
    """The connected-mailbox list (never None). Each: {id, email, kind, app_password?, host?,
    added_at}. Passwords stay in this private object only -- use public_mailboxes for templates."""
    raw = _read_object(mail_registry_object_name())
    if raw is None:
        return []
    try:
        return json.loads(raw.decode("utf-8")).get("mailboxes") or []
    except (ValueError, AttributeError):
        return []


def _save_mail_mailboxes(boxes):
    _write_object(mail_registry_object_name(),
                  json.dumps({"mailboxes": boxes}, indent=2, sort_keys=True).encode("utf-8"))


def add_mailbox(email, kind, app_password="", host="", client=""):
    """Connect (or re-save) a mailbox. Upserts by email so re-adding replaces the stored password.
    Returns the entry. Raises ValueError on a bad email/kind (the route surfaces the message).

    `client` assigns the mailbox to ONE client key: a dedicated inbox whose WHOLE contents are that
    client's, so its sync ingests everything (no contact filter). "" = shared: routed to every
    client by that client's contact list."""
    email = (email or "").strip().lower()
    if "@" not in email or "." not in email.split("@")[-1]:
        raise ValueError("That doesn't look like an email address.")
    if kind not in MAIL_KINDS:
        raise ValueError("Unknown mailbox kind.")
    if kind == "imap" and not (app_password or "").strip():
        raise ValueError("An IMAP mailbox needs its app password.")
    boxes = mail_mailboxes()
    entry = next((b for b in boxes if (b.get("email") or "").lower() == email), None)
    if entry is None:
        entry = {"id": _new_id("mb"), "email": email, "added_at": now_iso()}
        boxes.append(entry)
    entry["kind"] = kind
    entry["host"] = (host or "").strip()
    entry["client"] = (client or "").strip()
    if kind == "imap":
        entry["app_password"] = app_password.replace(" ", "").strip()
    else:
        entry.pop("app_password", None)
    _save_mail_mailboxes(boxes)
    return entry


def delete_mailbox(mailbox_id):
    """Disconnect a mailbox by id. Returns the removed entry, or None."""
    boxes = mail_mailboxes()
    for i, b in enumerate(boxes):
        if b.get("id") == mailbox_id:
            removed = boxes.pop(i)
            _save_mail_mailboxes(boxes)
            return removed
    return None


def find_mailbox(mailbox_id):
    """The mailbox entry with id `mailbox_id` (WITH its password -- server-side use only), or None."""
    for b in mail_mailboxes():
        if b.get("id") == mailbox_id:
            return b
    return None


def public_mailboxes():
    """The mailbox list SAFE for templates: the app password is never included."""
    out = []
    for b in mail_mailboxes():
        row = {k: b.get(k, "") for k in ("id", "email", "kind", "host", "added_at", "client")}
        row["has_password"] = bool(b.get("app_password"))
        out.append(row)
    return out


def mail_state(ws):
    """The client's mail block with every key present (never None) -- template-safe reads."""
    m = dict((ws or {}).get("mail") or {})
    m.setdefault("contacts", [])
    m.setdefault("threads", [])
    m.setdefault("digest", {})
    m.setdefault("last_sync", "")
    m.setdefault("last_error", "")
    m.setdefault("backlog", 0)
    m.setdefault("backfilled", False)
    return m


def mail_contacts(ws):
    """The client's contact emails/domains from an already-loaded workspace (never None)."""
    return [c for c in (mail_state(ws).get("contacts") or []) if isinstance(c, str) and c.strip()]


def set_mail_contacts(client, contacts):
    """Replace the client's contact list (accepts a list OR the textarea's comma/newline string).
    Trimmed, lowercased, de-duped, capped. Returns the stored list."""
    if isinstance(contacts, str):
        contacts = re.split(r"[,\n;]", contacts)
    cleaned, seen = [], set()
    for c in contacts or []:
        c = (c or "").strip().lower()
        if c and c not in seen:
            seen.add(c)
            cleaned.append(c)
        if len(cleaned) >= _MAIL_CONTACT_CAP:
            break

    def fn(ws):
        ws.setdefault("mail", {})["contacts"] = cleaned
        return cleaned
    return _mutate(client, fn)


def mail_threads(ws):
    """The client's thread INDEX entries (small; bodies live in per-thread objects). Never None."""
    return list(mail_state(ws).get("threads") or [])


def mail_thread_object_name(client, key):
    """One thread's archive object, e.g. 'workspace/mail/riverdance/mb_1a2b_18c9d4.json'."""
    return "%smail/%s/%s.json" % (_prefix(), client, key)


def read_mail_thread(client, key):
    """The stored thread dict (subject/participants/messages/summary), or None."""
    raw = _read_object(mail_thread_object_name(client, key))
    if raw is None:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, AttributeError):
        return None


def write_mail_thread(client, key, thread):
    """Persist one thread's full archive (its own object, NOT the workspace JSON)."""
    _write_object(mail_thread_object_name(client, key),
                  json.dumps(thread, indent=2, sort_keys=True).encode("utf-8"))


def delete_mail_thread_object(client, key):
    """Delete one thread's archive object (no error if absent)."""
    _delete_object(mail_thread_object_name(client, key))


def upsert_mail_thread_entry(client, entry):
    """Insert-or-update one INDEX entry by id, newest-first by last_date, capped.

    Returns the list of entry ids that fell off the cap -- the caller deletes their objects (object
    I/O stays out of the _mutate closure)."""
    def fn(ws):
        threads = ws.setdefault("mail", {}).setdefault("threads", [])
        existing = next((t for t in threads if t.get("id") == entry.get("id")), None)
        if existing is not None:
            keep_summary = existing.get("summary", "")
            existing.update(entry)
            if not entry.get("summary") and keep_summary:
                existing["summary"] = keep_summary
        else:
            threads.append(dict(entry))
        threads.sort(key=lambda t: t.get("last_date") or "", reverse=True)
        dropped = [t.get("id") for t in threads[_MAIL_THREAD_CAP:] if t.get("id")]
        del threads[_MAIL_THREAD_CAP:]
        return dropped
    return _mutate(client, fn)


def set_mail_thread_summary(client, key, summary):
    """Store a thread's AI summary on its index entry. Returns the entry, or None if it is gone."""
    def fn(ws):
        for t in ws.setdefault("mail", {}).setdefault("threads", []):
            if t.get("id") == key:
                t["summary"] = summary or ""
                return t
        return None
    return _mutate(client, fn)


def delete_mail_thread(client, key):
    """Remove a thread: its index entry AND its archive object. Returns the removed entry or None."""
    def fn(ws):
        threads = ws.setdefault("mail", {}).setdefault("threads", [])
        for i, t in enumerate(threads):
            if t.get("id") == key:
                return threads.pop(i)
        return None
    removed = _mutate(client, fn)
    _delete_object(mail_thread_object_name(client, key))
    return removed


def set_mail_digest(client, body):
    """Store the rolling AI digest (the Mail tab's briefing card). Returns the digest dict."""
    def fn(ws):
        m = ws.setdefault("mail", {})
        m["digest"] = {"body": body or "", "updated": now_iso()}
        return m["digest"]
    return _mutate(client, fn)


def mark_mail_sync(client, error="", backlog=None):
    """Stamp the last sync attempt: its error ("" on success) and, when given, the remaining
    backfill `backlog` (older conversations found but not yet fetched -- the Mail tab shows it).
    Best-effort, never raises."""
    def fn(ws):
        m = ws.setdefault("mail", {})
        m["last_sync"] = now_iso()
        m["last_error"] = error or ""
        if backlog is not None:
            try:
                m["backlog"] = max(0, int(backlog))
            except (TypeError, ValueError):
                pass
        return m
    try:
        return _mutate(client, fn)
    except Exception:
        return None


def set_mail_backfilled(client):
    """Latch the one-way backfill-complete flag: the wide first-sync window came back fully
    drained, so future syncs use the short overlap window. Returns the mail block."""
    def fn(ws):
        m = ws.setdefault("mail", {})
        m["backfilled"] = True
        return m
    return _mutate(client, fn)


# --- Uploaded creatives (binary objects in the SAME private bucket) -----------------------------
# A creative the team uploads for a content piece is stored as its OWN object alongside the
# workspace JSON (so a multi-KB image never bloats workspace/<c>.json, which is rewritten in full on
# every edit). The object stays private; it is only ever served through the authed proxy route in
# main.py (mirroring the /data.json posture -- buckets are never made public).
def creative_object_name(client, content_id):
    """Object name for a content piece's uploaded creative, e.g. 'workspace/creatives/riverdance/RVR-016'."""
    return "%screatives/%s/%s" % (_prefix(), client, content_id)


def write_creative(client, content_id, data, content_type="application/octet-stream"):
    """Store the uploaded creative bytes for a content piece. Returns the object name."""
    name = creative_object_name(client, content_id)
    _write_object(name, data, content_type=content_type)
    return name


def read_creative_bytes(client, content_id):
    """Return the raw bytes of a content piece's uploaded creative, or None if there is none."""
    return _read_object(creative_object_name(client, content_id))


def delete_creative(client, content_id):
    """Delete a content piece's uploaded creative object (no error if absent)."""
    _delete_object(creative_object_name(client, content_id))


# --- Multiple images per content piece (the approval ticket's picture row) ----------------------
# A content piece can carry SEVERAL images alongside, or instead of, the single legacy creative
# above. Each image is its OWN private object under a distinct '<content_id>.img/' prefix (so it
# never collides with the legacy single object at '<content_id>'); the workspace JSON records a
# small `images: [{id, mime}]` list -- never the bytes. Served only through the authed proxy route.
def creative_image_object_name(client, content_id, image_id):
    """Object name for ONE image, e.g. 'workspace/creatives/riverdance/RVR-016.img/img_ab12'."""
    return "%screatives/%s/%s.img/%s" % (_prefix(), client, content_id, image_id)


def add_content_image(client, content_id, image_id, data, mime, name=""):
    """Store one attached file (private object) and append {id, mime, name} to the piece's `images`
    list. Any file type is accepted -- images/videos render inline, others as a download chip; `name`
    is the original filename, used to label/download non-media files."""
    _write_object(creative_image_object_name(client, content_id, image_id), data,
                  content_type=mime or "application/octet-stream")

    def fn(ws):
        _camp, item = _find_content(ws, content_id)
        if item is None:
            raise KeyError("no content '%s'" % content_id)
        item.setdefault("images", []).append({"id": image_id, "mime": mime or "", "name": name or ""})
        return item["images"]
    return _mutate(client, fn)


def read_content_image_bytes(client, content_id, image_id):
    """Raw bytes of one image, or None if it does not exist."""
    return _read_object(creative_image_object_name(client, content_id, image_id))


def remove_content_image(client, content_id, image_id):
    """Delete one image (object + pointer). Returns the remaining list, or None if absent."""
    _delete_object(creative_image_object_name(client, content_id, image_id))

    def fn(ws):
        _camp, item = _find_content(ws, content_id)
        if item is None:
            return None
        item["images"] = [im for im in item.get("images", []) if im.get("id") != image_id]
        return item["images"]
    return _mutate(client, fn)


def signed_upload_url(client, content_id, mime, ttl_minutes=15):
    """A V4 signed PUT URL so the browser uploads a creative DIRECTLY to GCS, bypassing the app's
    request-size cap (Cloud Run caps requests at ~32 MiB; GCS has no such limit).

    Returns (url, object_name). On the local-fs backend (no GCS), returns (None, object_name) -- the
    caller falls back to the in-app upload route. Signing uses the runtime SA via the IAM signBlob
    API (the SA holds roles/iam.serviceAccountTokenCreator on itself), so NO key file is needed.
    """
    name = creative_object_name(client, content_id)
    if _local_dir():
        return None, name
    import google.auth  # lazy; only the GCS signing path needs these
    import google.auth.transport.requests
    # The signBlob IAM call needs a CLOUD-PLATFORM-scoped token; the storage client's default token is
    # storage-scoped only (otherwise: ACCESS_TOKEN_SCOPE_INSUFFICIENT). Mint a cloud-platform token
    # from the runtime SA via ADC and sign with it -- keyless (the SA holds Token Creator on itself).
    creds, _proj = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    creds.refresh(google.auth.transport.requests.Request())
    blob = _gcs_client().bucket(_bucket_name()).blob(name)
    url = blob.generate_signed_url(
        version="v4",
        expiration=datetime.timedelta(minutes=ttl_minutes),
        method="PUT",
        content_type=mime,
        service_account_email=getattr(creds, "service_account_email", None),
        access_token=creds.token,
    )
    return url, name


def creative_size(client, content_id):
    """Byte size of a content piece's uploaded creative, or None if it does not exist."""
    name = creative_object_name(client, content_id)
    local = _local_dir()
    if local:
        path = os.path.join(local, name)
        return os.path.getsize(path) if os.path.isfile(path) else None
    blob = _gcs_client().bucket(_bucket_name()).blob(name)
    if not blob.exists():
        return None
    blob.reload()
    return blob.size


def read_creative_range(client, content_id, start, end):
    """Return bytes [start, end] INCLUSIVE of a creative -- so the serve route can stream/seek video
    without loading the whole object into memory."""
    name = creative_object_name(client, content_id)
    local = _local_dir()
    if local:
        with open(os.path.join(local, name), "rb") as fh:
            fh.seek(start)
            return fh.read(end - start + 1)
    return _gcs_client().bucket(_bucket_name()).blob(name).download_as_bytes(start=start, end=end)


def stream_creative(client, content_id, start, end, chunk_size=262144):
    """Yield bytes [start, end] INCLUSIVE in chunks, so a large creative streams to the client
    without ever loading the whole object into memory (used by the serve route for video)."""
    name = creative_object_name(client, content_id)
    local = _local_dir()
    if local:
        # Local-fs is the dev/test backend; read the slice and CLOSE the file before yielding so no
        # OS handle lingers across the stream (Windows won't delete a file with an open handle). Prod
        # is GCS (below): chunked network reads, bounded memory, no local file handle.
        with open(os.path.join(local, name), "rb") as fh:
            fh.seek(start)
            data = fh.read(end - start + 1)
        for i in range(0, len(data), chunk_size):
            yield data[i:i + chunk_size]
        return
    # GCS: one seekable download stream (blob.open internally range-fetches), NOT one HTTP GET per
    # chunk -- so a large video streams over a single connection with bounded memory.
    blob = _gcs_client().bucket(_bucket_name()).blob(name)
    with blob.open("rb") as reader:
        if start:
            reader.seek(start)
        remaining = end - start + 1
        while remaining > 0:
            buf = reader.read(min(chunk_size, remaining))
            if not buf:
                break
            remaining -= len(buf)
            yield buf


def _mutate(client, fn):
    """Load -> apply `fn(ws)` -> save (last-write-wins). Returns whatever `fn` returns.

    Raises KeyError if the client has no workspace yet. Each client's workspace is its own object,
    so this read-modify-write only races with concurrent writes to the SAME client (acceptable for
    the low write volume here); cross-client edits never contend.
    """
    ws = load_workspace(client)
    if ws is None:
        raise KeyError("no workspace for client '%s'" % client)
    result = fn(ws)
    save_workspace(client, ws)
    return result


# --- Lookups ------------------------------------------------------------------------------------
def _find_content(ws, content_id):
    """Return (campaign, content) for `content_id` across all campaigns, or (None, None)."""
    for camp in ws.get("campaigns", []):
        for item in camp.get("content", []):
            if item.get("id") == content_id:
                return camp, item
    return None, None


def _find_campaign(ws, campaign_id):
    for camp in ws.get("campaigns", []):
        if camp.get("id") == campaign_id:
            return camp
    return None


def _find_conversation(ws, conversation_id):
    for conv in ws.get("conversations", []):
        if conv.get("id") == conversation_id:
            return conv
    return None


def _new_id(prefix):
    """A short, collision-resistant id like 'cv_1a2b3c4d'."""
    return "%s_%s" % (prefix, uuid.uuid4().hex[:8])


# --- Content review (client-facing approve / request-changes / note) ----------------------------
def decide_content(client, content_id, status, note=None):
    """Set a content piece's review status and stamp the decision time. Returns the content dict.

    `status` is "approved" or "changes". An optional `note` (the client's recommendation) is saved
    alongside the decision when provided.
    """
    def fn(ws):
        _camp, item = _find_content(ws, content_id)
        if item is None:
            raise KeyError("no content '%s'" % content_id)
        item["status"] = status
        item["decided_at"] = now_iso()
        if note is not None:
            item["client_note"] = note
        return item
    return _mutate(client, fn)


def set_content_note(client, content_id, note):
    """Persist the client's recommendation note on a content piece. Returns the content dict."""
    def fn(ws):
        _camp, item = _find_content(ws, content_id)
        if item is None:
            raise KeyError("no content '%s'" % content_id)
        item["client_note"] = note or ""
        return item
    return _mutate(client, fn)


# --- Conversations ------------------------------------------------------------------------------
def add_message(client, conversation_id, sender, sender_name, body, set_status=None, created_at=None):
    """Append a message to a conversation. Returns (conversation, message).

    `sender` is "client" or "agora". When `set_status` is given the thread's status is updated
    (e.g. a client message moves a thread to 'awaiting_reply').
    """
    def fn(ws):
        conv = _find_conversation(ws, conversation_id)
        if conv is None:
            raise KeyError("no conversation '%s'" % conversation_id)
        message = {
            "sender": sender,
            "sender_name": sender_name or "",
            "body": body or "",
            "created_at": created_at or now_iso(),
        }
        conv.setdefault("messages", []).append(message)
        if set_status:
            conv["status"] = set_status
        return conv, message
    return _mutate(client, fn)


def set_conversation_status(client, conversation_id, status):
    """Set a conversation's status ('awaiting_reply' or 'resolved'). Returns the conversation."""
    def fn(ws):
        conv = _find_conversation(ws, conversation_id)
        if conv is None:
            raise KeyError("no conversation '%s'" % conversation_id)
        conv["status"] = status
        return conv
    return _mutate(client, fn)


def add_conversation(client, subject, status="awaiting_reply", conversation_id=None):
    """Start a new conversation thread (team-facing). Returns the conversation dict."""
    def fn(ws):
        conv = {
            "id": conversation_id or _new_id("cv"),
            "subject": subject or "(no subject)",
            "status": status,
            "messages": [],
        }
        ws.setdefault("conversations", []).append(conv)
        return conv
    return _mutate(client, fn)


# --- Notification preferences (per logged-in user, keyed by email) ------------------------------
def default_notify():
    """Default notification prefs: on for master/content/changes/replies/summary, off for status/news."""
    return {
        "master": True,
        "content": True,
        "changes": True,
        "replies": True,
        "summary": True,
        "status": False,
        "news": False,
        "frequency": "instant",
    }


def get_notify(ws, user_email):
    """Return `user_email`'s notification prefs with defaults applied (never None)."""
    merged = default_notify()
    stored = (ws.get("notify") or {}).get(user_email)
    if stored:
        merged.update(stored)
    return merged


def set_notify(client, user_email, prefs):
    """Merge `prefs` into `user_email`'s notification settings and persist. Returns the merged dict."""
    def fn(ws):
        notify = ws.setdefault("notify", {})
        current = default_notify()
        if notify.get(user_email):
            current.update(notify[user_email])
        if prefs:
            current.update(prefs)
        notify[user_email] = current
        return current
    return _mutate(client, fn)


# --- Activity feed (Recent activity panel) ------------------------------------------------------
def add_activity(client, icon, text, time_label=None, limit=40):
    """Prepend an entry to the client's 'Recent activity' feed (most-recent first). Returns it.

    Capped at `limit` entries so the workspace object cannot grow without bound.
    """
    def fn(ws):
        entry = {"icon": icon or "bell", "text": text or "", "time_label": time_label or now_label()}
        activity = ws.setdefault("activity", [])
        activity.insert(0, entry)
        del activity[limit:]
        return entry
    return _mutate(client, fn)


# --- Team management: metrics / campaigns / content / calendar ----------------------------------
def set_metrics(client, metrics):
    """Replace the KPI metrics list (team-facing). Returns the metrics list."""
    def fn(ws):
        ws["metrics"] = list(metrics or [])
        return ws["metrics"]
    return _mutate(client, fn)


def set_goal(client, goal):
    """Store the per-client Monthly goal (label/format/target/exceed/breakthrough/current/
    source_metric; legacy 'stretch' is read as 'exceed'). Period is DERIVED at render time, never
    stored. Returns the goal dict."""
    def fn(ws):
        ws["goal"] = dict(goal or {})
        return ws["goal"]
    return _mutate(client, fn)


def set_reach(client, current, previous):
    """Store the per-client Total reach headline (this month + last month) shown on the Overview card."""
    def fn(ws):
        ws["reach"] = {"current": current, "previous": previous}
        return ws["reach"]
    return _mutate(client, fn)


def set_display_name(client, name):
    """Update the workspace's display name in place (a client rename), leaving all other content
    untouched. Returns the new name."""
    def fn(ws):
        ws["display_name"] = name
        return ws["display_name"]
    return _mutate(client, fn)


def set_dashboard_url(client, url, height=None, width=None):
    """Set the per-client Looker Studio embed URL (empty string hides the dashboard from the client)
    and, optionally, the report's native height + width in px. All read by atrium_view.dashboard().
    Width is the report's native canvas width; the embed scales to fill the container preserving
    aspect (see the Dashboard tab in atrium.html), so it no longer leaves a dead strip on the right."""
    def fn(ws):
        ws["dashboard_url"] = (url or "").strip()
        if height is not None:
            try:
                ws["dashboard_height"] = int(height)
            except (TypeError, ValueError):
                pass
        if width is not None:
            try:
                ws["dashboard_width"] = int(width)
            except (TypeError, ValueError):
                pass
        return ws["dashboard_url"]
    return _mutate(client, fn)


def set_overview_counts(client, today=None, split=None, series=None):
    """Update the headline counts used by Overview/Dashboard. Returns the workspace dict."""
    def fn(ws):
        if today is not None:
            ws["today"] = today
        if split is not None:
            ws["split"] = split
        if series is not None:
            ws["series"] = list(series)
        return ws
    return _mutate(client, fn)


def add_campaign(client, channel, name, eyebrow="", strategy=None, ai_summary="", campaign_id=None,
                 strategy_doc=""):
    """Add a campaign (team-facing). `channel` is 'paid' or 'organic'. Returns the campaign dict."""
    def fn(ws):
        camp = {
            "id": campaign_id or _new_id("cmp"),
            "channel": channel,
            "name": name or "(untitled campaign)",
            "eyebrow": eyebrow or "",
            "strategy": strategy or {"what": "", "why": ""},
            "ai_summary": ai_summary or "",
            "strategy_doc": strategy_doc or "",
            "content": [],
        }
        ws.setdefault("campaigns", []).append(camp)
        return camp
    return _mutate(client, fn)


def update_campaign(client, campaign_id, name=None, eyebrow=None, strategy=None, ai_summary=None,
                    channel=None, strategy_doc=None):
    """Edit a campaign's name / eyebrow / strategy / AI summary / channel / strategy doc. Returns it."""
    def fn(ws):
        camp = _find_campaign(ws, campaign_id)
        if camp is None:
            raise KeyError("no campaign '%s'" % campaign_id)
        if name is not None:
            camp["name"] = name
        if eyebrow is not None:
            camp["eyebrow"] = eyebrow
        if strategy is not None:
            camp["strategy"] = strategy
        if ai_summary is not None:
            camp["ai_summary"] = ai_summary
        if channel is not None:
            camp["channel"] = channel
        if strategy_doc is not None:
            camp["strategy_doc"] = strategy_doc
        return camp
    return _mutate(client, fn)


def set_strategy_doc(client, campaign_id, doc_url):
    """Attach (or clear) the Google Doc URL backing a campaign's AI summary. Returns the campaign."""
    def fn(ws):
        camp = _find_campaign(ws, campaign_id)
        if camp is None:
            raise KeyError("no campaign '%s'" % campaign_id)
        camp["strategy_doc"] = doc_url or ""
        return camp
    return _mutate(client, fn)


def delete_campaign(client, campaign_id):
    """Remove a campaign (and its content) from the workspace. Returns the removed campaign or None."""
    def fn(ws):
        camps = ws.get("campaigns", [])
        for i, camp in enumerate(camps):
            if camp.get("id") == campaign_id:
                return camps.pop(i)
        raise KeyError("no campaign '%s'" % campaign_id)
    return _mutate(client, fn)


def insert_campaign(client, campaign):
    """Re-insert a previously-removed campaign verbatim (Trash restore). Returns it.

    Appends the raw campaign dict back (its content, strategy, etc. intact) and re-mirrors any dated
    content onto the Content Calendar. Idempotent on the campaign id (won't duplicate)."""
    def fn(ws):
        c = dict(campaign or {})
        camps = ws.setdefault("campaigns", [])
        if any(x.get("id") == c.get("id") for x in camps):
            return c  # already present -- don't duplicate on a double-restore
        camps.append(c)
        for item in c.get("content", []):
            _sync_content_calendar(ws, c, item)
        return c
    return _mutate(client, fn)


def _content_event_kind(camp):
    """The content calendar 'kind' for a piece, derived from its campaign channel: a paid ads
    campaign mirrors as a 'paid' event, anything else as 'organic'."""
    return "paid" if (camp or {}).get("channel") == "paid" else "organic"


def _sync_content_calendar(ws, camp, item):
    """Keep a content piece's mirrored calendar event in step with the piece (called on add/edit).

    A content piece with a `date` shows up on the Content Calendar as a linked event (the event
    carries `content_id` back to the piece, plus the `tab` it lives under so the day-popup arrow can
    jump straight to it). The content piece is the source of truth for the event's date/label/kind/
    tab -- editing the piece OVERWRITES those on the linked event -- but the calendar keeps its own
    `status` (mark-as-done) untouched. A piece with no date has no event (any prior one is removed).
    """
    cid = item.get("id")
    if cid is None:
        return
    cal = ws.setdefault("calendar", [])
    existing = next((e for e in cal if e.get("content_id") == cid), None)
    date = str(item.get("date", "") or "").strip()
    if not date:
        if existing is not None:
            cal.remove(existing)
        return
    kind = _content_event_kind(camp)
    tab = "leadgen" if kind == "paid" else "organic"
    label = item.get("ref") or item.get("type_tag") or "Content"
    if existing is not None:
        existing["date"] = date
        existing["label"] = label
        existing["kind"] = kind
        existing["tab"] = tab
        existing["campaign_id"] = (camp or {}).get("id")
    else:
        cal.append({
            "date": date, "label": label, "kind": kind,
            "content_id": cid, "campaign_id": (camp or {}).get("id"), "tab": tab,
        })


def add_content(client, campaign_id, content):
    """Add a content piece to a campaign (team-facing); forces status 'awaiting'. Returns it.

    `content` is a dict of the content fields (ref, type_tag, sub_tag, platform, caption, date, etc.).
    Missing id/ref are generated; status is always reset to 'awaiting' for a fresh review. If the
    piece carries a `date`, it is also mirrored onto the Content Calendar as a linked event.
    """
    def fn(ws):
        camp = _find_campaign(ws, campaign_id)
        if camp is None:
            raise KeyError("no campaign '%s'" % campaign_id)
        item = dict(content or {})
        item.setdefault("id", _new_id("cnt"))
        # The id drives EVERY per-piece DOM hook, route, and JS selector. The add route derives it
        # from the human title (ref), so two pieces sharing a title would collide -- and a duplicate
        # id makes the second piece impossible to open/edit (the modal/card selector always resolves
        # to the FIRST match). Guarantee uniqueness across ALL campaigns, suffixing on a clash.
        existing = {it.get("id") for c in ws.get("campaigns", []) for it in c.get("content", [])}
        if item["id"] in existing:
            base, n = item["id"], 2
            while ("%s-%d" % (base, n)) in existing:
                n += 1
            item["id"] = "%s-%d" % (base, n)
        item.setdefault("ref", item["id"])
        item["status"] = "awaiting"
        item.setdefault("client_note", "")
        item.setdefault("decided_at", "")
        item.setdefault("comments", [])
        camp.setdefault("content", []).append(item)
        _sync_content_calendar(ws, camp, item)
        return item
    return _mutate(client, fn)


def update_content(client, content_id, fields):
    """Patch fields on an existing content piece (team-facing). Returns the content dict.

    If the patch touches the piece's date/title (or it already carries a date), the mirrored Content
    Calendar event is re-synced so the calendar always reflects the piece.
    """
    def fn(ws):
        camp, item = _find_content(ws, content_id)
        if item is None:
            raise KeyError("no content '%s'" % content_id)
        item.update(fields or {})
        _sync_content_calendar(ws, camp, item)
        return item
    return _mutate(client, fn)


def delete_content(client, content_id):
    """Remove a content piece from whatever campaign holds it (and its mirrored calendar event, if
    any). Returns the removed piece.

    Note: the caller is responsible for deleting any uploaded creative object via delete_creative().
    """
    def fn(ws):
        for camp in ws.get("campaigns", []):
            items = camp.get("content", [])
            for i, item in enumerate(items):
                if item.get("id") == content_id:
                    removed = items.pop(i)
                    ws["calendar"] = [e for e in ws.get("calendar", [])
                                      if e.get("content_id") != content_id]
                    return removed
        raise KeyError("no content '%s'" % content_id)
    return _mutate(client, fn)


def insert_content(client, campaign_id, content):
    """Re-insert a previously-removed content piece verbatim into its campaign (Trash restore).

    Restores the piece as it was (status/comments/date preserved) and re-mirrors its calendar event
    if it had a date. Raises KeyError if the campaign no longer exists (e.g. it was deleted too --
    restore the campaign instead). Idempotent on the content id."""
    def fn(ws):
        camp = _find_campaign(ws, campaign_id)
        if camp is None:
            raise KeyError("no campaign '%s'" % campaign_id)
        items = camp.setdefault("content", [])
        c = dict(content or {})
        if any(x.get("id") == c.get("id") for x in items):
            return c
        items.append(c)
        _sync_content_calendar(ws, camp, c)
        return c
    return _mutate(client, fn)


def move_content(client, content_id, target_campaign_id):
    """Reassign a content piece to a different campaign, preserving the piece verbatim.

    Detaches the piece from whichever campaign currently holds it and appends it to
    `target_campaign_id` (keeping its id/status/comments/creative/date). The mirrored Content
    Calendar event is re-synced against the DESTINATION campaign, so a cross-channel move re-tags
    the event's kind/tab (paid<->organic) to match the new campaign. Returns (target_campaign,
    content). No-op (returns the piece where it is) when it already lives in the target. Raises
    KeyError if the piece or the target campaign doesn't exist.
    """
    def fn(ws):
        target = _find_campaign(ws, target_campaign_id)
        if target is None:
            raise KeyError("no campaign '%s'" % target_campaign_id)
        src, item = _find_content(ws, content_id)
        if item is None:
            raise KeyError("no content '%s'" % content_id)
        if src is not None and src.get("id") == target_campaign_id:
            return target, item
        if src is not None:
            src["content"] = [it for it in src.get("content", []) if it.get("id") != content_id]
        target.setdefault("content", []).append(item)
        _sync_content_calendar(ws, target, item)
        return target, item
    return _mutate(client, fn)


def add_content_comment(client, content_id, sender, sender_name, body, created_at=None,
                        kind="comment", set_status=None):
    """Append a threaded comment to a content piece. Returns (content, comment).

    `sender` is "client" or "agora". `kind` is "comment" (default) or "changes" — a "Request changes"
    comment, rendered as a flagged light-red bubble. When `set_status` is given (e.g. "changes"), the
    piece's review status + decided_at are stamped in the SAME write, so requesting changes through
    the comment thread also records the decision atomically. Comments are an ongoing discussion on a
    creative, separate from the one-shot `client_note`.
    """
    def fn(ws):
        _camp, item = _find_content(ws, content_id)
        if item is None:
            raise KeyError("no content '%s'" % content_id)
        comment = {
            "id": _new_id("cm"),
            "sender": sender,
            "sender_name": sender_name or "",
            "body": body or "",
            "created_at": created_at or now_iso(),
            "kind": kind or "comment",
        }
        if (kind or "comment") == "changes":
            comment["resolved"] = False
        item.setdefault("comments", []).append(comment)
        if set_status:
            item["status"] = set_status
            item["decided_at"] = now_iso()
        return item, comment
    return _mutate(client, fn)


def resolve_content_comment(client, content_id, comment_id):
    """Mark a "Request changes" comment resolved. Returns (content, comment, status).

    When the piece has no remaining UNRESOLVED changes-comments and its status is still 'changes', it
    returns to 'awaiting' (back in the review queue). Raises KeyError if the piece or comment is gone.
    """
    def fn(ws):
        _camp, item = _find_content(ws, content_id)
        if item is None:
            raise KeyError("no content '%s'" % content_id)
        target = next((c for c in item.get("comments", []) if c.get("id") == comment_id), None)
        if target is None:
            raise KeyError("no comment '%s'" % comment_id)
        target["resolved"] = True
        unresolved = [c for c in item.get("comments", [])
                      if c.get("kind") == "changes" and not c.get("resolved")]
        if not unresolved and item.get("status") == "changes":
            item["status"] = "awaiting"
            item["decided_at"] = now_iso()
        return item, target, item.get("status")
    return _mutate(client, fn)


def delete_content_comment(client, content_id, comment_id):
    """Remove a single comment from a content piece's thread. Returns (content, status).

    Mirrors `resolve_content_comment`: if deleting the comment leaves no remaining UNRESOLVED
    changes-comments and the piece is still 'changes', it returns to 'awaiting' (back in the review
    queue). Raises KeyError if the piece or comment is gone.
    """
    def fn(ws):
        _camp, item = _find_content(ws, content_id)
        if item is None:
            raise KeyError("no content '%s'" % content_id)
        comments = item.get("comments", [])
        target = next((c for c in comments if c.get("id") == comment_id), None)
        if target is None:
            raise KeyError("no comment '%s'" % comment_id)
        item["comments"] = [c for c in comments if c.get("id") != comment_id]
        unresolved = [c for c in item["comments"]
                      if c.get("kind") == "changes" and not c.get("resolved")]
        if not unresolved and item.get("status") == "changes":
            item["status"] = "awaiting"
            item["decided_at"] = now_iso()
        return item, item.get("status")
    return _mutate(client, fn)


def set_content_image(client, content_id, object_name, mime):
    """Record that a content piece now has an uploaded creative (object name + mime). Returns it."""
    def fn(ws):
        _camp, item = _find_content(ws, content_id)
        if item is None:
            raise KeyError("no content '%s'" % content_id)
        item["image_object"] = object_name
        item["image_mime"] = mime or "application/octet-stream"
        return item
    return _mutate(client, fn)


def clear_content_image(client, content_id):
    """Forget a content piece's uploaded creative (does NOT delete the object). Returns the piece."""
    def fn(ws):
        _camp, item = _find_content(ws, content_id)
        if item is None:
            raise KeyError("no content '%s'" % content_id)
        item.pop("image_object", None)
        item.pop("image_mime", None)
        return item
    return _mutate(client, fn)


def add_calendar_event(client, date, label, kind):
    """Append a calendar event ('paid'|'organic'|'due'|'milestone'). Returns it."""
    def fn(ws):
        event = {"date": date, "label": label or "", "kind": kind or "milestone"}
        ws.setdefault("calendar", []).append(event)
        return event
    return _mutate(client, fn)


def edit_calendar_event(client, index, date, label, kind):
    """Edit the calendar event at `index` (date/label/kind) in place. Returns it, or None if out of range.
    A blank date or kind is ignored (the existing value is kept); the label is set as given (may be empty)."""
    def fn(ws):
        events = ws.get("calendar", [])
        if 0 <= index < len(events):
            event = events[index]
            if date:
                event["date"] = date
            event["label"] = label or ""
            if kind:
                event["kind"] = kind
            return event
        return None
    return _mutate(client, fn)


def delete_calendar_event(client, index):
    """Remove the calendar event at `index` (as ordered in the stored list). Returns it, or None."""
    def fn(ws):
        events = ws.get("calendar", [])
        if 0 <= index < len(events):
            return events.pop(index)
        return None
    return _mutate(client, fn)


def insert_calendar_event(client, event):
    """Re-insert a previously-removed calendar event verbatim (Trash restore). Returns it.

    Only used to restore PERSONAL (non-content) events -- a content-linked event is owned by its
    piece and is recreated by restoring the content, so this never re-adds a content-linked one."""
    def fn(ws):
        ev = dict(event or {})
        ws.setdefault("calendar", []).append(ev)
        return ev
    return _mutate(client, fn)


def set_calendar_status(client, index, status):
    """Set or clear a calendar event's status (e.g. 'done'/'ready') at `index`. An empty status clears
    it. The calendar view treats a 'done'/'ready' event as accomplished (green ✓, 'ahead' if future).
    Returns the updated event, or None if the index is out of range."""
    def fn(ws):
        events = ws.get("calendar", [])
        if 0 <= index < len(events):
            if status:
                events[index]["status"] = status
            else:
                events[index].pop("status", None)
            return events[index]
        return None
    return _mutate(client, fn)


# --- Task tracker: the internal delivery board + the client Progress tab -------------------------
# One more additive key, ws["tasks"] = [task, ...] (no new infra -- see TASK_TRACKER_INTEGRATION.md).
# A task is a deliverable travelling across four stages. The INTERNAL board (operator console) sees
# everything; the client Progress tab sees only client_facing tasks, and only their client-safe
# fields (never lead/support/owners, priority, internal_notes, or account_manager_id).
#
# Task shape (spec §3.2, extended 2026-07 with the two-level work breakdown + dates/charge):
#   id, title, stage, department, lead_id, support_ids[], priority, labels[], campaign,
#   content_type, start_date, due_date (the LAUNCH date -- key canonical, label "Launch date"),
#   service_charge (internal only),
#   maintasks[] ({id, text, assignee_id, subs[] ({id, text, done, assignee_id, dod})}),
#     -- dod = optional INTERNAL "done when" (team overlay only; never in the client Progress shape),
#   comments[] (the content-comment shape incl. kind:"changes" + resolved), history[],
#   client_facing, client_note, deliverable_url,        <- client-safe
#   reporter ("agora"|"client") + reporter_name,        <- who FILED it (client-safe, set once at add)
#   internal_notes, account_manager_id                  <- internal only
# LEGACY: tasks written before the two-level model carry a flat subtasks[] -- normalize_task()
# migrates that into one maintask in place (called by _find_task, so every mutation persists it).
# Stage KEYS are canonical (never rename). The set was aligned to Sentinel's board wording on
# 2026-07-27 (replacing in_process/for_launch/launched/closed); on 2026-07-29 For Review and
# Waiting for Client were REMOVED at the user's request -- both just meant "blocked on someone",
# so they collapse into Blocked, which now sits right after In Progress on the board.
# `_STAGE_ALIASES` keeps any task written under a retired key readable.
TASK_STAGES = ("todo", "in_progress", "blocked", "revision", "completed")
# Old key -> new key, applied on read by normalize_task so legacy/imported rows never vanish off the
# board (an unknown stage would otherwise fall into the first column silently). for_review /
# waiting_client also arrive from a stale Sentinel bridge write -- they land on Blocked, their
# nearest surviving meaning (for_launch was "in review" too, so it follows them there).
_STAGE_ALIASES = {"in_process": "in_progress", "for_launch": "blocked",
                  "launched": "completed", "closed": "completed",
                  "for_review": "blocked", "waiting_client": "blocked"}


def canon_stage(stage):
    """Return a valid stage key for `stage`, translating legacy keys; default 'todo'."""
    s = (stage or "").strip()
    s = _STAGE_ALIASES.get(s, s)
    return s if s in TASK_STAGES else "todo"
TASK_PRIORITIES = ("Low", "Medium", "High", "Urgent")
# The fields update_task will patch (id/comments/maintasks/history have their own helpers).
_TASK_FIELDS = ("title", "department", "lead_id", "support_ids", "priority", "labels", "campaign",
                "content_type", "start_date", "due_date", "service_charge",
                "on_hold", "hold_reason",
                "client_facing", "client_note", "deliverable_url",
                "internal_notes", "account_manager_id")


def tasks_of(ws):
    """The workspace's task list (never None), each task normalized to the two-level shape."""
    return [normalize_task(t) for t in (ws or {}).get("tasks") or []]


def normalize_task(task):
    """Migrate a task IN PLACE to the two-level model: maintasks[] each owning subs[].

    A legacy flat subtasks[] becomes one maintask (named after the content type, owned by the
    lead) so no data is lost; the old key is dropped. Tasks created before start_date existed
    get one backfilled from their creation day. Idempotent -- a normalized task passes
    through untouched."""
    if task is None:
        return None
    # Stage keys moved to Sentinel's set (2026-07-27); translate legacy keys on read so an old task
    # lands in the right column instead of silently falling into the first one.
    task["stage"] = canon_stage(task.get("stage"))
    if not task.get("start_date") and task.get("created_at"):
        task["start_date"] = str(task["created_at"])[:10]
    if not isinstance(task.get("maintasks"), list):
        legacy = task.pop("subtasks", None) or []
        task["maintasks"] = [{
            "id": _new_id("mt"),
            "text": task.get("content_type") or "Deliverable",
            "assignee_id": task.get("lead_id") or "",
            "subs": legacy,
        }] if legacy else []
    else:
        task.pop("subtasks", None)
        for m in task["maintasks"]:
            if not isinstance(m.get("subs"), list):
                m["subs"] = []
    return task


def task_subtasks(task):
    """Every sub-task across the task's main tasks, flattened (for counts + the close-guard)."""
    out = []
    for m in (task or {}).get("maintasks") or []:
        out.extend(m.get("subs") or [])
    return out


def _find_task(ws, task_id):
    """The task dict with id `task_id` (normalized in place), or None."""
    for t in ws.get("tasks", []):
        if t.get("id") == task_id:
            return normalize_task(t)
    return None


def task_open_changes(task):
    """The task's UNRESOLVED "Request changes" comments (the client-flag is derived, not stored)."""
    return [c for c in (task or {}).get("comments", [])
            if c.get("kind") == "changes" and not c.get("resolved")]


def _clean_support(lead_id, support_ids):
    """Support people minus the lead (the lead is never double-counted as their own support)."""
    seen, out = set(), []
    for s in support_ids or []:
        s = (s or "").strip() if isinstance(s, str) else s
        if s and s != lead_id and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def task_completed_at(task):
    """When this task last moved to `completed`, from its own history ("" when it never did).

    Read off the history rather than a new column: `_task_history` already records every stage move
    as {field:"stage", new:<stage>, at:<iso>}, so the completion date is derivable for tasks that
    finished long before anyone needed the date. The report's Delivery slide uses it to show only
    work that shipped SINCE the previous report."""
    at = ""
    for h in (task or {}).get("history") or []:
        if h.get("field") == "stage" and _STAGE_ALIASES.get(h.get("new"), h.get("new")) == "completed":
            when = str(h.get("at") or "")
            if when > at:
                at = when
    return at


def _task_history(task, actor, field, old, new):
    """Append one activity entry to a task's history (stage moves, edits)."""
    task.setdefault("history", []).append({
        "actor": actor or "", "field": field, "old": old or "", "new": new or "", "at": now_iso(),
    })


def add_task(client, fields, actor=""):
    """Create a task on the client's board. Returns the stored task dict.

    `fields` is a dict of the task fields; the stage defaults to todo and is validated,
    support_ids never contains lead_id, and a "created" history entry is stamped."""
    f = dict(fields or {})
    stage = f.get("stage") or "todo"
    stage = _STAGE_ALIASES.get(stage, stage)
    if stage not in TASK_STAGES:
        raise KeyError("no task stage '%s'" % stage)
    lead = (f.get("lead_id") or "").strip()
    task = {
        "id": f.get("id") or _new_id("tk"),
        "title": f.get("title") or "(untitled task)",
        "stage": stage,
        "department": f.get("department") or "",
        "lead_id": lead,
        "support_ids": _clean_support(lead, f.get("support_ids")),
        "priority": f.get("priority") if f.get("priority") in TASK_PRIORITIES else "Medium",
        "labels": list(f.get("labels") or []),
        "campaign": f.get("campaign") or "",
        "content_type": f.get("content_type") or "",
        # Every service has a start date: work starts when it's created unless told otherwise.
        "start_date": f.get("start_date") or now_iso()[:10],
        "due_date": f.get("due_date") or "",
        "service_charge": f.get("service_charge") or "",
        "on_hold": bool(f.get("on_hold")),
        "hold_reason": f.get("hold_reason") or "",
        # A service can be seeded with a pre-built work breakdown (from service_templates.py); it is
        # ordinary stored data from here on -- rename/add/delete via the normal helpers.
        "maintasks": f.get("maintasks") if isinstance(f.get("maintasks"), list) else [],
        "comments": [],
        "history": [],
        "client_facing": bool(f.get("client_facing")),
        # Who FILED the task -- auto-stamped by the route from the session, never a form choice
        # (the Progress tab's quick-add tags client vs agora so live-call capture is attributed).
        "reporter": f.get("reporter") if f.get("reporter") in ("agora", "client") else "agora",
        "reporter_name": f.get("reporter_name") or "",
        "client_note": f.get("client_note") or "",
        "deliverable_url": f.get("deliverable_url") or "",
        "internal_notes": f.get("internal_notes") or "",
        "account_manager_id": f.get("account_manager_id") or lead,
        "created_at": now_iso(),
    }
    _task_history(task, actor, "created", "", task["title"])

    def fn(ws):
        ws.setdefault("tasks", []).append(task)
        return task
    return _mutate(client, fn)


def update_task(client, task_id, fields, actor=""):
    """Patch a task's editable fields (never id/stage/subtasks/comments -- those have their own
    helpers). Enforces priority + the lead-never-in-support rule. Returns the task."""
    def fn(ws):
        task = _find_task(ws, task_id)
        if task is None:
            raise KeyError("no task '%s'" % task_id)
        f = dict(fields or {})
        if "priority" in f and f["priority"] not in TASK_PRIORITIES:
            f.pop("priority")
        for k in _TASK_FIELDS:
            if k in f:
                task[k] = f[k]
        task["lead_id"] = (task.get("lead_id") or "").strip()
        task["support_ids"] = _clean_support(task["lead_id"], task.get("support_ids"))
        task["client_facing"] = bool(task.get("client_facing"))
        _task_history(task, actor, "edited", "", "task updated")
        return task
    return _mutate(client, fn)


def move_task_stage(client, task_id, stage, actor=""):
    """Move a task to `stage`. Records the move in the task's history. Returns the task; no-op if
    it is already there.

    EVERY move is allowed, on purpose (2026-07-28). A move to `completed` used to be BLOCKED while
    a sub-task was still open, a client change request was unresolved, or the service had no steps
    at all -- and a refused drop reads as a broken board rather than as a rule. The blockers are
    still SURFACED on both boards (the progress bar, the "Changes requested" tag, the open-changes
    count); they just no longer veto the move. Callers still catch ValueError, so re-introducing a
    guard here needs no route change."""
    stage = _STAGE_ALIASES.get(stage, stage)
    if stage not in TASK_STAGES:
        raise KeyError("no task stage '%s'" % stage)

    def fn(ws):
        task = _find_task(ws, task_id)
        if task is None:
            raise KeyError("no task '%s'" % task_id)
        if task.get("stage") == stage:
            return task
        _task_history(task, actor, "stage", task.get("stage", ""), stage)
        task["stage"] = stage
        return task
    return _mutate(client, fn)


def delete_task(client, task_id):
    """Remove a task from the board. Returns the removed task dict (so the route can Trash it)."""
    def fn(ws):
        tasks = ws.get("tasks", [])
        for i, t in enumerate(tasks):
            if t.get("id") == task_id:
                return tasks.pop(i)
        raise KeyError("no task '%s'" % task_id)
    return _mutate(client, fn)


def insert_task(client, task):
    """Re-insert a previously-removed task verbatim (Trash restore). Idempotent on the task id."""
    def fn(ws):
        t = dict(task or {})
        tasks = ws.setdefault("tasks", [])
        if any(x.get("id") == t.get("id") for x in tasks):
            return t  # already present -- don't duplicate on a double-restore
        tasks.append(t)
        return t
    return _mutate(client, fn)


def upsert_tasks(client, tasks):
    """Merge a list of tasks into the client's board BY ID (used by the JSON import/restore).

    Non-destructive: an incoming task whose id already exists REPLACES that task in place; a new id
    is appended. Nothing is ever removed. Returns (added, updated) counts."""
    def fn(ws):
        board = ws.setdefault("tasks", [])
        index = {t.get("id"): i for i, t in enumerate(board) if t.get("id")}
        added = updated = 0
        for raw in tasks or []:
            t = dict(raw or {})
            tid = t.get("id")
            if tid and tid in index:
                board[index[tid]] = t
                updated += 1
            else:
                board.append(t)
                if tid:
                    index[tid] = len(board) - 1
                added += 1
        return added, updated
    return _mutate(client, fn)


def add_task_comment(client, task_id, sender, sender_name, body, kind="comment"):
    """Append a threaded comment to a task. Returns (task, comment).

    Mirrors add_content_comment: `sender` is "client" or "agora"; kind "changes" is a client
    "Request changes" (flagged bubble, carries `resolved`) -- the task-level flag is DERIVED from
    unresolved changes-comments (task_open_changes), not stored."""
    def fn(ws):
        task = _find_task(ws, task_id)
        if task is None:
            raise KeyError("no task '%s'" % task_id)
        comment = {
            "id": _new_id("cm"),
            "sender": sender,
            "sender_name": sender_name or "",
            "body": body or "",
            "created_at": now_iso(),
            "kind": kind or "comment",
        }
        if (kind or "comment") == "changes":
            comment["resolved"] = False
        task.setdefault("comments", []).append(comment)
        return task, comment
    return _mutate(client, fn)


def resolve_task_comment(client, task_id, comment_id):
    """Mark a task's "Request changes" comment resolved (TEAM action). Returns
    (task, comment, open_changes_left). Raises KeyError if the task or comment is gone."""
    def fn(ws):
        task = _find_task(ws, task_id)
        if task is None:
            raise KeyError("no task '%s'" % task_id)
        target = next((c for c in task.get("comments", []) if c.get("id") == comment_id), None)
        if target is None:
            raise KeyError("no comment '%s'" % comment_id)
        target["resolved"] = True
        return task, target, len(task_open_changes(task))
    return _mutate(client, fn)


def add_maintask(client, task_id, text, assignee_id=""):
    """Append a main task (a named group of sub-tasks with its own owner). Returns (task, maintask)."""
    def fn(ws):
        task = _find_task(ws, task_id)
        if task is None:
            raise KeyError("no task '%s'" % task_id)
        main = {"id": _new_id("mt"), "text": text or "", "assignee_id": assignee_id or "", "subs": []}
        task.setdefault("maintasks", []).append(main)
        return task, main
    return _mutate(client, fn)


def _find_maintask(task, maintask_id):
    for m in (task or {}).get("maintasks", []):
        if m.get("id") == maintask_id:
            return m
    return None


def set_maintask_owner(client, task_id, maintask_id, assignee_id):
    """Assign (or clear, with "") the owner of one main task. Returns (task, maintask)."""
    def fn(ws):
        task = _find_task(ws, task_id)
        if task is None:
            raise KeyError("no task '%s'" % task_id)
        main = _find_maintask(task, maintask_id)
        if main is None:
            raise KeyError("no maintask '%s'" % maintask_id)
        main["assignee_id"] = assignee_id or ""
        return task, main
    return _mutate(client, fn)


def rename_maintask(client, task_id, maintask_id, text):
    """Rename one main task (its sub-tasks stay put). Returns (task, maintask)."""
    def fn(ws):
        task = _find_task(ws, task_id)
        if task is None:
            raise KeyError("no task '%s'" % task_id)
        main = _find_maintask(task, maintask_id)
        if main is None:
            raise KeyError("no maintask '%s'" % maintask_id)
        main["text"] = text or main.get("text") or ""
        return task, main
    return _mutate(client, fn)


def set_task_hold(client, task_id, on_hold, reason="", actor=""):
    """Put a task on hold or resume it (ongoing). `on_hold` is a plain boolean -- no dates. The
    reason is internal (never shown to the client). Stamps a hold/resume history entry. Returns
    the task."""
    def fn(ws):
        task = _find_task(ws, task_id)
        if task is None:
            raise KeyError("no task '%s'" % task_id)
        held = bool(on_hold)
        task["on_hold"] = held
        task["hold_reason"] = (reason or "") if held else ""
        _task_history(task, actor, "hold", "", "on hold" if held else "resumed")
        return task
    return _mutate(client, fn)


def delete_maintask(client, task_id, maintask_id):
    """Remove one main task (and its sub-tasks) from a task. Returns the task."""
    def fn(ws):
        task = _find_task(ws, task_id)
        if task is None:
            raise KeyError("no task '%s'" % task_id)
        task["maintasks"] = [m for m in task.get("maintasks", []) if m.get("id") != maintask_id]
        return task
    return _mutate(client, fn)


def add_subtask(client, task_id, text, assignee_id=None, maintask_id="", dod=""):
    """Append a sub-task ({id, text, done, assignee_id, dod}) under a main task. Returns (task, sub).

    `maintask_id` picks the group; without one the sub-task lands in the LAST main task, and a
    task with no main tasks yet grows a "Deliverable" group first (so legacy callers still work).
    `dod` is the optional INTERNAL "done when" definition (never shown to the client)."""
    def fn(ws):
        task = _find_task(ws, task_id)
        if task is None:
            raise KeyError("no task '%s'" % task_id)
        mains = task.setdefault("maintasks", [])
        main = _find_maintask(task, maintask_id) if maintask_id else None
        if main is None:
            if maintask_id:
                raise KeyError("no maintask '%s'" % maintask_id)
            if not mains:
                mains.append({"id": _new_id("mt"), "text": task.get("content_type") or "Deliverable",
                              "assignee_id": task.get("lead_id") or "", "subs": []})
            main = mains[-1]
        sub = {"id": _new_id("st"), "text": text or "", "done": False,
               "assignee_id": assignee_id or "", "dod": (dod or "").strip()}
        main.setdefault("subs", []).append(sub)
        return task, sub
    return _mutate(client, fn)


def _find_subtask(task, subtask_id):
    """The sub-task with `subtask_id`, searched across every main task (ids are unique)."""
    for s in task_subtasks(task):
        if s.get("id") == subtask_id:
            return s
    return None


def set_subtask_done(client, task_id, subtask_id, done):
    """Check / uncheck one sub-task. Returns (task, subtask)."""
    def fn(ws):
        task = _find_task(ws, task_id)
        if task is None:
            raise KeyError("no task '%s'" % task_id)
        sub = _find_subtask(task, subtask_id)
        if sub is None:
            raise KeyError("no subtask '%s'" % subtask_id)
        sub["done"] = bool(done)
        return task, sub
    return _mutate(client, fn)


def edit_subtask(client, task_id, subtask_id, text=None, dod=None):
    """Edit a sub-task's text and/or its INTERNAL "done when" (dod). Each field is patched only when
    passed (None = leave unchanged); an empty string clears the dod. `text` is never blanked -- a
    blank rename keeps the existing text (mirrors rename_maintask). Returns (task, subtask)."""
    def fn(ws):
        task = _find_task(ws, task_id)
        if task is None:
            raise KeyError("no task '%s'" % task_id)
        sub = _find_subtask(task, subtask_id)
        if sub is None:
            raise KeyError("no subtask '%s'" % subtask_id)
        if text is not None and text.strip():
            sub["text"] = text.strip()
        if dod is not None:
            sub["dod"] = dod.strip()
        return task, sub
    return _mutate(client, fn)


def set_subtask_owner(client, task_id, subtask_id, assignee_id):
    """Assign (or clear, with "") the owner of one sub-task. Returns (task, subtask)."""
    def fn(ws):
        task = _find_task(ws, task_id)
        if task is None:
            raise KeyError("no task '%s'" % task_id)
        sub = _find_subtask(task, subtask_id)
        if sub is None:
            raise KeyError("no subtask '%s'" % subtask_id)
        sub["assignee_id"] = assignee_id or ""
        return task, sub
    return _mutate(client, fn)


def delete_subtask(client, task_id, subtask_id):
    """Remove one sub-task from whichever main task holds it. Returns the task."""
    def fn(ws):
        task = _find_task(ws, task_id)
        if task is None:
            raise KeyError("no task '%s'" % task_id)
        for m in task.get("maintasks", []):
            m["subs"] = [s for s in m.get("subs", []) if s.get("id") != subtask_id]
        return task
    return _mutate(client, fn)


def set_task_maintasks(client, task_id, maintasks, actor=""):
    """Replace a task's WHOLE work breakdown in one write. Returns the task.

    The array-shaped twin of add_maintask / rename_maintask / add_subtask / ... : the Sentinel
    board's detail drawer edits the breakdown as one array and PATCHes the lot, so it needs a
    single-call setter rather than a dozen fine-grained ops (see main.py's
    `/api/internal/task-update`). The console keeps using the fine-grained helpers.

    Two rules make it safe to accept an array from another system:
      * an id the task doesn't already hold gets a FRESH canonical id -- a caller that invents
        placeholder ids for new rows ("st_new_1721…") can never mint an Atrium id.
      * a sub-task that KEEPS its id keeps its internal `dod` ("done when") unless the caller
        explicitly sent one. Sentinel neither shows nor sends `dod`, and an edit from over there
        must not silently drop a field that surface can't see.
    """
    def fn(ws):
        task = _find_task(ws, task_id)
        if task is None:
            raise KeyError("no task '%s'" % task_id)
        known_mains = {m.get("id") for m in task.get("maintasks") or []}
        known_subs = {s.get("id"): s for s in task_subtasks(task)}
        clean = []
        for m in maintasks or []:
            if not isinstance(m, dict):
                continue
            subs = []
            for s in m.get("subs") or []:
                if not isinstance(s, dict):
                    continue
                text = str(s.get("text") or "").strip()
                if not text:
                    continue
                prev = known_subs.get(s.get("id"))
                dod = s.get("dod")
                subs.append({
                    "id": s.get("id") if prev is not None else _new_id("st"),
                    "text": text,
                    "done": bool(s.get("done")),
                    "assignee_id": s.get("assignee_id") or "",
                    "dod": (dod if dod is not None else (prev or {}).get("dod", "")) or "",
                })
            clean.append({
                "id": m.get("id") if m.get("id") in known_mains else _new_id("mt"),
                "text": str(m.get("text") or "").strip() or "Untitled",
                "assignee_id": m.get("assignee_id") or "",
                "subs": subs,
            })
        task["maintasks"] = clean
        _task_history(task, actor, "breakdown", "", "work breakdown updated")
        return task
    return _mutate(client, fn)


# --- Client Communications: ONE multi-channel timeline (team-written, client-read) --------------
# All conversations -- email, Upwork, Slack, meetings, calls, notes -- live in a single list
# ws["communications"], newest first. Each entry:
#   {id, channel, audience, title, summary, date, people, origin, thread_key}
# * channel  -- one of COMM_CHANNELS (the coloured badge on the card).
# * audience -- "client" (the client sees it) or "team" (internal only; server-filtered out of the
#               client render, exactly like a client_facing:false task never reaches _progress_tasks).
# * people   -- "who was involved" (meeting attendees / call participants / email participants).
# * origin   -- "manual" (hand-added) or "mail" (mirrored from the Mail sync's client recap).
# * thread_key -- for an email card, the Mail thread key so "Read full thread" can open the archive.
# Email thread recaps are mirrored here by the Mail sync (upsert_email_summary, stable "mail_<key>"
# id, audience "client"). The two legacy split lists (email_summaries / meeting_summaries) migrate
# into this one list the first time it is touched -- no data is lost.
COMM_CHANNELS = ("email", "upwork", "slack", "meeting", "call", "note")


def _clean_channel(channel):
    channel = (channel or "").strip().lower()
    return channel if channel in COMM_CHANNELS else "note"


def _clean_audience(audience):
    return "team" if (audience or "").strip().lower() == "team" else "client"


def _ensure_communications(ws):
    """Return ws["communications"], migrating the legacy email_summaries / meeting_summaries lists
    into it the first time (in place). Idempotent -- safe to call on every read and mutation."""
    if isinstance(ws.get("communications"), list):
        return ws["communications"]
    items = []
    for e in (ws.get("email_summaries") or []):
        eid = str(e.get("id") or _new_id("cm"))
        is_mail = eid.startswith("mail_")
        items.append({
            "id": eid, "channel": "email", "audience": "client",
            "title": e.get("subject") or "(no subject)", "summary": e.get("summary") or "",
            "date": e.get("date") or "", "people": "",
            "origin": "mail" if is_mail else "manual",
            "thread_key": eid[5:] if is_mail else "",
        })
    for m in (ws.get("meeting_summaries") or []):
        items.append({
            "id": str(m.get("id") or _new_id("cm")), "channel": "meeting", "audience": "client",
            "title": m.get("title") or "(untitled meeting)", "summary": m.get("summary") or "",
            "date": m.get("date") or "", "people": m.get("attendees") or "",
            "origin": "manual", "thread_key": "",
        })
    items.sort(key=lambda it: it.get("date") or "", reverse=True)
    ws["communications"] = items
    ws.pop("email_summaries", None)
    ws.pop("meeting_summaries", None)
    return items


def communications_list(ws):
    """The normalized communications list from an already-loaded workspace (never None). Migrates the
    legacy lists in memory; the migration persists on the next mutation."""
    return list(_ensure_communications(ws))


def add_communication(client, channel, title, summary, date=None, people="",
                      audience="client", origin="manual", thread_key="", item_id=None):
    """Add a communication entry (newest first) to the unified timeline. Returns it."""
    def fn(ws):
        item = {
            "id": item_id or _new_id("cm"),
            "channel": _clean_channel(channel),
            "audience": _clean_audience(audience),
            "title": title or "",
            "summary": summary or "",
            "date": date or now_iso(),
            "people": people or "",
            "origin": origin or "manual",
            "thread_key": thread_key or "",
        }
        _ensure_communications(ws).insert(0, item)
        return item
    return _mutate(client, fn)


def upsert_email_summary(client, item_id, subject, summary, date=None):
    """Insert-or-update the Mail sync's mirrored email recap in the unified timeline, BY ID.

    Each thread's client-facing recap is mirrored under a stable 'mail_<key>' id (channel "email",
    audience "client"), so a thread that gains messages UPDATES its card in place instead of stacking
    duplicates. Hand-added entries are untouched. Returns the entry."""
    key = str(item_id)[5:] if str(item_id).startswith("mail_") else ""
    def fn(ws):
        items = _ensure_communications(ws)
        for it in items:
            if it.get("id") == item_id:
                if subject:
                    it["title"] = subject
                it["summary"] = summary or ""
                if date:
                    it["date"] = date
                it["channel"] = "email"
                it["audience"] = "client"
                it["origin"] = "mail"
                if key:
                    it["thread_key"] = key
                return it
        item = {"id": item_id, "channel": "email", "audience": "client",
                "title": subject or "(no subject)", "summary": summary or "",
                "date": date or now_iso(), "people": "", "origin": "mail", "thread_key": key}
        items.insert(0, item)
        return item
    return _mutate(client, fn)


def delete_communication(client, item_id):
    """Delete a communication entry by id. Returns the remaining list."""
    def fn(ws):
        items = _ensure_communications(ws)
        ws["communications"] = [it for it in items if it.get("id") != item_id]
        return ws["communications"]
    return _mutate(client, fn)


def update_communication(client, item_id, fields):
    """Edit a communication entry's fields in place by id (channel/audience/title/summary/date/
    people). Returns the updated item, or None if not found."""
    allowed = ("channel", "audience", "title", "summary", "date", "people")
    def fn(ws):
        for it in _ensure_communications(ws):
            if it.get("id") == item_id:
                for k in allowed:
                    if k in (fields or {}):
                        if k == "channel":
                            it[k] = _clean_channel(fields[k])
                        elif k == "audience":
                            it[k] = _clean_audience(fields[k])
                        else:
                            it[k] = fields[k]
                return it
        return None
    return _mutate(client, fn)


# Back-compat wrappers (kept so the Mail sync + any older callers/tests keep working).
def add_email_summary(client, subject, summary, date=None, email_id=None):
    """Add a hand-written email summary. Thin wrapper over add_communication (channel 'email')."""
    return add_communication(client, "email", subject, summary, date=date,
                             audience="client", origin="manual", item_id=email_id)


def add_meeting_summary(client, title, summary, attendees="", date=None, meeting_id=None):
    """Add a meeting summary / notes. Thin wrapper over add_communication (channel 'meeting')."""
    return add_communication(client, "meeting", title, summary, date=date, people=attendees,
                             audience="client", origin="manual", item_id=meeting_id)


# --- The Company profile: who the client actually is (team-written, client-read) ----------------
# ONE key, ws["company"], holding everything the agency knows about the client AS A BUSINESS -- the
# facts, the story, the brand, the catalogue. It is the workspace's answer to "who are we working
# for?", and it is what grounds the Assistant, the report decks and the research brain in the
# client's own reality instead of the agency's guesses.
#
# Shape (every part optional; a blank profile is a legal, fully-shaped profile):
#   {"profile":  {one_liner, industry, founded, hq, website, size, customers},   # at a glance
#    "brand":    {tagline, voice, tone, personality, colors, fonts, dos, donts, assets_url},
#    "sections": [{id, heading, body}],       # the story: About / History / Mission / Positioning
#    "products": [{id, name, summary, price, audience, url, status}]}            # what they sell
#
# `sections` and `products` are ORDERED lists the team arranges by hand (move_company_item), not
# date-sorted feeds -- a company story reads top to bottom, so "newest first" would be wrong here.
# Same no-new-infra posture as Market Intelligence below: one more key in the same workspace JSON.
COMPANY_PROFILE_FIELDS = ("one_liner", "industry", "founded", "hq", "website", "size", "customers")
COMPANY_BRAND_FIELDS = ("tagline", "voice", "tone", "personality", "colors", "fonts",
                        "dos", "donts", "assets_url")
# The two ordered lists, each with its own field set. `kind` is the list name in every helper below.
COMPANY_LISTS = {
    "sections": ("heading", "body"),
    "products": ("name", "summary", "price", "audience", "url", "status"),
}
_COMPANY_ID_PREFIX = {"sections": "cs", "products": "cp"}


# --- The engagement: what we are DOING for this client, and what counts as success --------------
# Declared ONCE here and read by everything: the report spine (which slides), build_facts (which
# numbers), the intel research prompts, the Assistant's grounding, and later the dashboard template.
#
# Why its own key rather than the Company tab or the campaign list:
#   * `company` is who the client IS -- stable, client-owned. An objective changes each quarter and
#     carries our internal targets. Different lifecycle.
#   * `campaigns` is the content-approval structure; a client can run four campaigns under one
#     objective and you would be declaring it four times, then watching them drift.
#   * the task board's `department` is who does the work, not what success means (Acquisition covers
#     lead gen AND sales).
#   * the dashboard export would need a per-client job deploy to change, and portal-only clients
#     have none.
#
# 🔴 The objective alone is not enough. A deck cannot report lead generation if it does not know
# WHICH COLUMN IS THE CONVERSION, and it cannot say "on track" without a target. That is what
# `conversion` carries, per objective.
ENGAGEMENT_OBJECTIVES = ("leadgen", "sales", "awareness")
ENGAGEMENT_CONVERSION_FIELDS = ("event", "source", "label", "target_cpl", "target_volume",
                                "target_roas", "target_aov", "truth_revenue", "truth_note")


def _ensure_engagement(ws):
    """The workspace's engagement block, normalized IN PLACE and returned (never None)."""
    eng = ws.get("engagement")
    if not isinstance(eng, dict):
        eng = {}
    objs = [o for o in (eng.get("objectives") or []) if o in ENGAGEMENT_OBJECTIVES]
    eng["objectives"] = objs
    eng["primary"] = eng.get("primary") if eng.get("primary") in objs else (objs[0] if objs else "")
    conv = eng.get("conversion") if isinstance(eng.get("conversion"), dict) else {}
    clean = {}
    for obj in ENGAGEMENT_OBJECTIVES:
        block = conv.get(obj) if isinstance(conv.get(obj), dict) else {}
        clean[obj] = {f: str(block.get(f) or "") for f in ENGAGEMENT_CONVERSION_FIELDS}
    eng["conversion"] = clean
    eng["notes"] = str(eng.get("notes") or "")
    ws["engagement"] = eng
    return eng


def engagement_of(ws):
    """The fully-shaped engagement from an already-loaded workspace (a copy, never None)."""
    eng = _ensure_engagement(dict(ws or {}))
    return {"objectives": list(eng["objectives"]), "primary": eng["primary"],
            "conversion": {k: dict(v) for k, v in eng["conversion"].items()},
            "notes": eng["notes"]}


def set_engagement(client, fields):
    """Patch the engagement block: objectives/primary/notes and per-objective conversion fields.

    Patches ONLY what the caller passes, exactly like set_company_profile -- a partial form post
    must never blank the targets someone else filled in."""
    def fn(ws):
        eng = _ensure_engagement(ws)
        if isinstance((fields or {}).get("objectives"), list):
            eng["objectives"] = [o for o in fields["objectives"] if o in ENGAGEMENT_OBJECTIVES]
            if eng["primary"] not in eng["objectives"]:
                eng["primary"] = eng["objectives"][0] if eng["objectives"] else ""
        if (fields or {}).get("primary") in eng["objectives"]:
            eng["primary"] = fields["primary"]
        if "notes" in (fields or {}):
            eng["notes"] = str(fields.get("notes") or "")
        conv = (fields or {}).get("conversion")
        if isinstance(conv, dict):
            for obj, block in conv.items():
                if obj in ENGAGEMENT_OBJECTIVES and isinstance(block, dict):
                    for f in ENGAGEMENT_CONVERSION_FIELDS:
                        if f in block:
                            eng["conversion"][obj][f] = str(block.get(f) or "")
        return engagement_of(ws)
    return _mutate(client, fn)


def _ensure_company(ws):
    """The workspace's company profile, normalized IN PLACE and returned (never None).

    Guarantees every block exists with every known field, so the template and the Assistant can
    read `company.brand.voice` without a `default` filter and an older workspace (written before
    this feature) upgrades silently on the next read."""
    comp = ws.get("company")
    if not isinstance(comp, dict):
        comp = {}
    profile = comp.get("profile") if isinstance(comp.get("profile"), dict) else {}
    comp["profile"] = {f: str(profile.get(f) or "") for f in COMPANY_PROFILE_FIELDS}
    brand = comp.get("brand") if isinstance(comp.get("brand"), dict) else {}
    comp["brand"] = {f: str(brand.get(f) or "") for f in COMPANY_BRAND_FIELDS}
    for kind, fields in COMPANY_LISTS.items():
        items = comp.get(kind)
        clean = []
        for it in (items if isinstance(items, list) else []):
            if not isinstance(it, dict):
                continue
            row = {"id": it.get("id") or _new_id(_COMPANY_ID_PREFIX[kind])}
            for f in fields:
                row[f] = str(it.get(f) or "")
            clean.append(row)
        comp[kind] = clean
    ws["company"] = comp
    return comp


def company_profile(ws):
    """The fully-shaped company profile from an already-loaded workspace (a copy, never None).

    Read-side helper for the template, the Assistant index and the report generator. Normalizes in
    memory only -- the shape persists on the next mutation, exactly like _ensure_communications."""
    comp = _ensure_company(dict(ws or {}))
    return {"profile": dict(comp["profile"]), "brand": dict(comp["brand"]),
            "sections": [dict(x) for x in comp["sections"]],
            "products": [dict(x) for x in comp["products"]]}


def company_is_empty(ws):
    """True when nothing has been recorded about the company yet (drives the tab's empty state)."""
    comp = company_profile(ws)
    return not (any(comp["profile"].values()) or any(comp["brand"].values())
                or comp["sections"] or comp["products"])


def set_company_profile(client, fields):
    """Patch the at-a-glance facts. Only fields PRESENT in `fields` are written (a partial form post
    can never blank the rest). Returns the profile block."""
    def fn(ws):
        comp = _ensure_company(ws)
        for f in COMPANY_PROFILE_FIELDS:
            if f in (fields or {}):
                comp["profile"][f] = str(fields[f] or "")
        return dict(comp["profile"])
    return _mutate(client, fn)


def set_company_brand(client, fields):
    """Patch the branding block (same present-fields-only rule). Returns the brand block."""
    def fn(ws):
        comp = _ensure_company(ws)
        for f in COMPANY_BRAND_FIELDS:
            if f in (fields or {}):
                comp["brand"][f] = str(fields[f] or "")
        return dict(comp["brand"])
    return _mutate(client, fn)


def _company_kind(kind):
    """Canonical company list name, or None when `kind` is not one of the two lists."""
    return kind if kind in COMPANY_LISTS else None


def company_items(ws, kind):
    """One company list ('sections' | 'products') from a loaded workspace. Raises KeyError on a bad
    kind, so a typo fails loudly instead of silently returning an empty list."""
    if _company_kind(kind) is None:
        raise KeyError("no company list '%s'" % kind)
    return [dict(x) for x in _ensure_company(dict(ws or {}))[kind]]


def add_company_item(client, kind, entry, item_id=None):
    """Append a story section or a product to its list (APPEND, not insert -- these are ordered by
    hand, so a new item lands at the end where the author left off). Returns the item."""
    if _company_kind(kind) is None:
        raise KeyError("no company list '%s'" % kind)

    def fn(ws):
        comp = _ensure_company(ws)
        item = {"id": item_id or _new_id(_COMPANY_ID_PREFIX[kind])}
        for f in COMPANY_LISTS[kind]:
            item[f] = str((entry or {}).get(f, "") or "")
        comp[kind].append(item)
        return item
    return _mutate(client, fn)


def update_company_item(client, kind, item_id, fields):
    """Edit one item's fields in place by id. Returns the item, or None when it isn't there."""
    if _company_kind(kind) is None:
        raise KeyError("no company list '%s'" % kind)

    def fn(ws):
        for it in _ensure_company(ws)[kind]:
            if it.get("id") == item_id:
                for f in COMPANY_LISTS[kind]:
                    if f in (fields or {}):
                        it[f] = str(fields[f] or "")
                return it
        return None
    return _mutate(client, fn)


def delete_company_item(client, kind, item_id):
    """Delete one item by id. Returns (removed_item_or_None, remaining_list) -- the caller needs the
    removed payload to stash it in the Bin."""
    if _company_kind(kind) is None:
        raise KeyError("no company list '%s'" % kind)

    def fn(ws):
        comp = _ensure_company(ws)
        removed = next((it for it in comp[kind] if it.get("id") == item_id), None)
        comp[kind] = [it for it in comp[kind] if it.get("id") != item_id]
        return removed, list(comp[kind])
    return _mutate(client, fn)


def insert_company_item(client, kind, item, index=None):
    """Re-insert a soft-deleted item (the Bin's Restore). Appends when `index` is None or out of
    range; re-mints a missing id. Returns the item."""
    if _company_kind(kind) is None:
        raise KeyError("no company list '%s'" % kind)

    def fn(ws):
        comp = _ensure_company(ws)
        row = {"id": (item or {}).get("id") or _new_id(_COMPANY_ID_PREFIX[kind])}
        for f in COMPANY_LISTS[kind]:
            row[f] = str((item or {}).get(f, "") or "")
        at = len(comp[kind]) if index is None else max(0, min(int(index), len(comp[kind])))
        comp[kind].insert(at, row)
        return row
    return _mutate(client, fn)


def move_company_item(client, kind, item_id, delta):
    """Move one item up (-1) or down (+1) in its list. Returns the reordered list.

    Ordering is the whole point of these lists (a story reads top to bottom), so this is a
    first-class writer, not a UI convenience. A move past either end is a no-op, never an error."""
    if _company_kind(kind) is None:
        raise KeyError("no company list '%s'" % kind)

    def fn(ws):
        comp = _ensure_company(ws)
        items = comp[kind]
        idx = next((i for i, it in enumerate(items) if it.get("id") == item_id), None)
        if idx is None:
            return list(items)
        target = idx + (1 if int(delta or 0) > 0 else -1)
        if 0 <= target < len(items):
            items[idx], items[target] = items[target], items[idx]
        return list(items)
    return _mutate(client, fn)


# --- Market Intelligence: the weekly briefing (team-written, client-read) -----------------------
# A team-curated briefing the client reads, split into two sections that each hold a list of
# entries (newest first). One key, ws["intel"] = {"business_research": [...], "media_buying": [...]}.
# An entry is {id, heading, title, body, source, link, date} -- mirroring the "Weekly Intelligence
# Report" shape (a sub-heading + headline + paragraph + a source tag/link). Same load-modify-save
# posture as the Client Communications summaries above; no new infra.
# 🔴 `conditions` (added 2026-07-30) is the section that had no home: the wildfire that closed the
# highway, the heat wave, the local event, the regulation change. It is usually the single biggest
# explanation for a bad week, it is NOT what Watcher does (that is creators/competitors), and the
# report's Landscape slide leads with it. Client-visible + team-curated like the other two.
INTEL_SECTIONS = ("business_research", "media_buying", "conditions")
_INTEL_FIELDS = ("heading", "title", "body", "relevance", "source", "link", "date")


def _intel_key(section):
    """Canonical intel-section key, or None if `section` is not one of the two valid sections."""
    return section if section in INTEL_SECTIONS else None


def add_intel_entry(client, section, entry, entry_id=None):
    """Add a Market Intelligence entry (newest first) to `section`. Returns the entry.

    `section` is 'business_research' or 'media_buying'; an unknown section raises KeyError. `entry`
    is a dict of any of the intel fields (heading/title/body/source/link/date); missing ones default
    to empty strings."""
    key = _intel_key(section)
    if key is None:
        raise KeyError("no intel section '%s'" % section)

    def fn(ws):
        item = {"id": entry_id or _new_id("intel")}
        for f in _INTEL_FIELDS:
            item[f] = (entry or {}).get(f, "") or ""
        ws.setdefault("intel", {}).setdefault(key, []).insert(0, item)
        return item
    return _mutate(client, fn)


def update_intel_entry(client, section, entry_id, fields):
    """Edit a Market Intelligence entry's fields in place by id. Returns the entry, or None if not
    found. Only the recognised intel fields are written; unknown keys are ignored.

    Editing an AUTO-pulled entry (one the daily refresh wrote) PINS it: the `auto` flag is dropped so
    a hand-correction survives the next refresh (which only ever replaces still-auto entries)."""
    key = _intel_key(section)
    if key is None:
        raise KeyError("no intel section '%s'" % section)

    def fn(ws):
        for it in ws.get("intel", {}).get(key, []):
            if it.get("id") == entry_id:
                for f in _INTEL_FIELDS:
                    if f in (fields or {}):
                        it[f] = fields[f]
                it.pop("auto", None)  # an admin edit pins the entry (no longer auto-managed)
                return it
        return None
    return _mutate(client, fn)


def delete_intel_entry(client, section, entry_id):
    """Delete a Market Intelligence entry by id from `section`. Returns the remaining list."""
    key = _intel_key(section)
    if key is None:
        raise KeyError("no intel section '%s'" % section)

    def fn(ws):
        lst = ws.setdefault("intel", {}).setdefault(key, [])
        ws["intel"][key] = [it for it in lst if it.get("id") != entry_id]
        return ws["intel"][key]
    return _mutate(client, fn)


# Valid bulk actions on a set of intel entries (team-only, in-place).
INTEL_BULK_ACTIONS = ("delete", "favourite", "unfavourite")


def bulk_intel(client, section, action, entry_ids):
    """Apply a bulk `action` to the intel entries whose ids are in `entry_ids`. Returns the list.

    * delete       -- remove them.
    * favourite    -- star them (`favourite: True`) AND pin them (drop the `auto` flag) so the daily
                      refresh never sweeps a favourited story away.
    * unfavourite  -- clear the star (leaves it pinned; a hand-touched entry stays non-auto).
    Unknown ids are ignored; an unknown action raises ValueError."""
    key = _intel_key(section)
    if key is None:
        raise KeyError("no intel section '%s'" % section)
    if action not in INTEL_BULK_ACTIONS:
        raise ValueError("bad intel bulk action '%s'" % action)
    ids = set(i for i in (entry_ids or []) if i)

    def fn(ws):
        lst = ws.setdefault("intel", {}).setdefault(key, [])
        if action == "delete":
            ws["intel"][key] = [it for it in lst if it.get("id") not in ids]
            return ws["intel"][key]
        for it in lst:
            if it.get("id") in ids:
                if action == "favourite":
                    it["favourite"] = True
                    it.pop("auto", None)   # pin: survives the next replace_auto_intel
                else:
                    it.pop("favourite", None)
        return lst
    return _mutate(client, fn)


# --- Market Intelligence: per-client research topics + the daily auto-refresh -------------------
# The intel tab can auto-fill from real news every day (services/intel-refresh, fed by intel_feed).
# Two extra pieces of state, both additive (no new infra -- still one workspace JSON per client):
#   * ws["intel_topics"]  -- a list of keyword strings the daily refresh searches for the client's
#     Business Research section (e.g. ["RV industry", "motorhome sales"]). Empty -> the refresh job
#     falls back to a generic marketing set. Team-edited from inside the workspace.
#   * each auto-pulled entry carries `"auto": True`. replace_auto_intel swaps out exactly those on
#     each run, so hand-added (and admin-edited, see update_intel_entry) entries are NEVER clobbered.
_MAX_INTEL_TOPICS = 12


def get_intel_topics(ws):
    """The client's research-keyword list from an already-loaded workspace dict (never None)."""
    topics = (ws or {}).get("intel_topics") or []
    return [t for t in topics if isinstance(t, str) and t.strip()]


def set_intel_topics(client, topics):
    """Replace the client's Business-Research keyword list (trimmed, de-duped, capped). Returns it.

    Accepts a list of strings OR a single comma/newline-separated string (what the admin textarea
    posts); blanks are dropped and order is preserved (first occurrence wins)."""
    if isinstance(topics, str):
        topics = re.split(r"[,\n]", topics)
    cleaned, seen = [], set()
    for t in topics or []:
        t = (t or "").strip()
        key = t.lower()
        if t and key not in seen:
            seen.add(key)
            cleaned.append(t)
        if len(cleaned) >= _MAX_INTEL_TOPICS:
            break

    def fn(ws):
        ws["intel_topics"] = cleaned
        return cleaned
    return _mutate(client, fn)


def replace_auto_intel(client, section, entries):
    """Swap the AUTO entries of a section for `entries`, preserving hand-added/pinned ones.

    `entries` is a list of intel-field dicts (heading/title/body/source/link/date) the daily refresh
    built from real news; each is stored with a fresh id and `auto:True`. Manual entries (no `auto`
    flag) are kept untouched. Returns the section's resulting entry list."""
    key = _intel_key(section)
    if key is None:
        raise KeyError("no intel section '%s'" % section)

    def fn(ws):
        existing = ws.setdefault("intel", {}).setdefault(key, [])
        kept = [it for it in existing if not it.get("auto")]
        fresh = []
        for e in entries or []:
            item = {"id": _new_id("intel"), "auto": True}
            for f in _INTEL_FIELDS:
                item[f] = (e or {}).get(f, "") or ""
            fresh.append(item)
        ws["intel"][key] = fresh + kept  # the view re-sorts by date, so order here is immaterial
        return ws["intel"][key]
    return _mutate(client, fn)


# How many auto (non-favourite) entries a section keeps. The daily refresh ADDS to the list rather
# than wiping it, so this caps unbounded growth -- the oldest plain-auto entries drop off. Manual
# and favourited entries are never dropped.
_MAX_AUTO_PER_SECTION = 60


def add_auto_intel(client, section, entries, cap=_MAX_AUTO_PER_SECTION):
    """ADD freshly-curated `entries` to a section, de-duped against what's already there.

    Unlike replace_auto_intel (which swapped the whole auto set each run), this GROWS the list: an
    article whose link/title already exists in the section is skipped, genuinely new ones are added
    (stored `auto:True`, newest-first). Manual and favourited entries are always kept; only plain
    `auto` entries are capped (oldest by date drop past `cap`). Returns the section's entry list."""
    key = _intel_key(section)
    if key is None:
        raise KeyError("no intel section '%s'" % section)

    def _keys(it):
        return ((it.get("link") or "").strip().lower(), (it.get("title") or "").strip().lower())

    def fn(ws):
        existing = ws.setdefault("intel", {}).setdefault(key, [])
        seen_links, seen_titles = set(), set()
        for it in existing:
            lnk, ttl = _keys(it)
            if lnk:
                seen_links.add(lnk)
            if ttl:
                seen_titles.add(ttl)
        added = []
        for e in entries or []:
            item = {}
            for f in _INTEL_FIELDS:
                item[f] = (e or {}).get(f, "") or ""
            lnk, ttl = _keys(item)
            if (lnk and lnk in seen_links) or (ttl and ttl in seen_titles):
                continue  # already have this story -- don't duplicate it
            item["id"] = _new_id("intel")
            item["auto"] = True
            added.append(item)
            if lnk:
                seen_links.add(lnk)
            if ttl:
                seen_titles.add(ttl)
        combined = added + existing            # newest additions first (view re-sorts anyway)
        # Cap ONLY plain-auto entries; keep every manual/favourited one untouched.
        protected = [x for x in combined if not x.get("auto") or x.get("favourite")]
        autos = [x for x in combined if x.get("auto") and not x.get("favourite")]
        autos.sort(key=lambda e: (e.get("date") or ""), reverse=True)
        ws["intel"][key] = protected + autos[:max(0, int(cap))]
        return ws["intel"][key]
    return _mutate(client, fn)


# --- Market Intelligence: the AI 'brain' config (which model + the tunable prompts) --------------
# ws["intel_ai"] holds the per-client research settings the team edits in place (see intel_ai.py):
#   * model           -- the selected model id ("" -> feature off; the refresh keeps the plain-RSS
#                        fill). Validated against intel_ai.MODELS by the route before it is stored.
#   * business_prompt / media_prompt -- the admin-tunable editorial guidance for each section
#                        (blank -> intel_ai's module default is used at refresh time).
#   * backfilled      -- set True after the first 12-month backfill run, so daily runs use the short
#                        recent window instead of re-pulling a year every day.
#   * show_thinking   -- "1" if the admin wants the model's reasoning + considered-articles captured
#                        and shown (slower; a debugging aid). Blank/"" = off (fast).
#   * last_run / last_model / last_error -- best-effort run metadata surfaced to the admin.
#   * last_trace      -- best-effort per-section diagnostics from the last run (candidates, reasoning,
#                        raw output), written by mark_intel_run and shown when show_thinking is on.
# It is one more additive workspace key (no new infra), mirroring intel_topics above.
_INTEL_AI_FIELDS = ("model", "business_prompt", "media_prompt", "window", "count", "show_thinking")


def get_intel_ai(ws):
    """The client's AI research settings from an already-loaded workspace dict (never None).

    Always returns a dict with at least the editable fields present (blank if unset) so callers and
    templates can read them without guarding."""
    cfg = dict((ws or {}).get("intel_ai") or {})
    for f in _INTEL_AI_FIELDS:
        cfg.setdefault(f, "")
    return cfg


def set_intel_ai(client, fields):
    """Merge `fields` into the client's intel_ai config (only recognised keys). Returns the config.

    Used by the admin 'AI research settings' save. `model` is stored verbatim (the route validates
    it against intel_ai.MODELS first); the prompts are trimmed. Unknown keys are ignored."""
    def fn(ws):
        cfg = ws.setdefault("intel_ai", {})
        for f in _INTEL_AI_FIELDS:
            if f in (fields or {}):
                val = fields.get(f)
                cfg[f] = ("" if val is None else str(val)).strip()
        return cfg
    return _mutate(client, fn)


# --- Assistant (team-only chat): its own model choice + a running spend tally --------------------
def set_assistant_model(client, model_id):
    """Persist the Assistant's model choice ("" = automatic: intel brain's model, else the default).
    Stored verbatim -- the route validates against intel_ai.MODELS first. Returns the config."""
    def fn(ws):
        cfg = ws.setdefault("assistant", {})
        cfg["model"] = (model_id or "").strip()
        return cfg
    return _mutate(client, fn)


def set_assistant_depth(client, depth):
    """Persist the Assistant's answer depth ('quick'|'standard'|'deep'). Stored verbatim -- the
    route validates against assistant_ai.DEPTHS first. Returns the config."""
    def fn(ws):
        cfg = ws.setdefault("assistant", {})
        cfg["depth"] = (depth or "").strip()
        return cfg
    return _mutate(client, fn)


# --- Assistant reindex queue ---------------------------------------------------------------------
# A FULL index rebuild (re-chunk + re-embed the whole corpus) is too heavy for a request: on a big
# workspace it is minutes of Vertex calls and a multi-hundred-MB peak, and because the index is
# written LAST a rebuild that dies persists nothing -- so every later ask retried the same doomed
# work and the Assistant stayed permanently dead (2026-07-31). The ask path now flags the client
# here instead and keeps answering from the index it already has; the `assistant-reindex` JOB
# (assistant_reindex.py) does the heavy rebuild out of band and clears the flag.
def assistant_reindex_pending(ws):
    """True iff this workspace is already flagged for a full Assistant reindex.

    PURE (no I/O) on purpose: the ask path checks the workspace it already holds, so a client that
    is queued does not pay for a redundant workspace write on every single question."""
    return bool((((ws or {}).get("assistant") or {}).get("reindex") or {}).get("pending"))


def queue_assistant_reindex(client, reason=""):
    """Flag `client` for a full Assistant index rebuild by the assistant-reindex job.

    Idempotent -- re-queuing only refreshes the reason/timestamp. Returns the queue entry."""
    def fn(ws):
        q = ws.setdefault("assistant", {}).setdefault("reindex", {})
        q["pending"] = True
        q["reason"] = (reason or "")[:200]
        q["queued_at"] = now_iso()
        return dict(q)
    return _mutate(client, fn)


def clear_assistant_reindex(client, built_at=""):
    """Clear the reindex flag once the job has rebuilt this client's index."""
    def fn(ws):
        q = ws.setdefault("assistant", {}).setdefault("reindex", {})
        q["pending"] = False
        q["reason"] = ""
        q["last_run"] = built_at or now_iso()
        return dict(q)
    return _mutate(client, fn)


def _blank_usage():
    return {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "calls": 0, "by_model": {}}


def assistant_usage(ws):
    """The workspace's all-time Assistant spend tally (always fully-shaped, never None)."""
    u = ((ws or {}).get("assistant") or {}).get("usage") or {}
    out = _blank_usage()
    for k in ("input_tokens", "output_tokens", "calls"):
        try:
            out[k] = int(u.get(k) or 0)
        except (TypeError, ValueError):
            pass
    try:
        out["cost_usd"] = float(u.get("cost_usd") or 0.0)
    except (TypeError, ValueError):
        pass
    out["by_model"] = dict(u.get("by_model") or {})
    return out


def add_assistant_usage(client, model_id, input_tokens, output_tokens, cost_usd):
    """Accumulate one Assistant call into the client's all-time spend tally (mirrors mastery-engine's
    per-user tally). Returns the updated tally so the response can carry the fresh totals."""
    def fn(ws):
        cfg = ws.setdefault("assistant", {})
        tally = cfg.setdefault("usage", _blank_usage())
        tally["input_tokens"] = int(tally.get("input_tokens") or 0) + int(input_tokens or 0)
        tally["output_tokens"] = int(tally.get("output_tokens") or 0) + int(output_tokens or 0)
        tally["cost_usd"] = float(tally.get("cost_usd") or 0.0) + float(cost_usd or 0.0)
        tally["calls"] = int(tally.get("calls") or 0) + 1
        key = model_id or "unknown"
        by = tally.setdefault("by_model", {})
        m = by.setdefault(key, {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "calls": 0})
        m["input_tokens"] = int(m.get("input_tokens") or 0) + int(input_tokens or 0)
        m["output_tokens"] = int(m.get("output_tokens") or 0) + int(output_tokens or 0)
        m["cost_usd"] = float(m.get("cost_usd") or 0.0) + float(cost_usd or 0.0)
        m["calls"] = int(m.get("calls") or 0) + 1
        return assistant_usage(ws)
    return _mutate(client, fn)


def mark_intel_run(client, model, error="", backfilled=None, traces=None):
    """Record run metadata after a refresh attempt (best-effort; never raises out of the job).

    `model` is the model id that ran (or ""); `error` is a short message on failure ("" on success);
    `backfilled=True` latches the 12-month-backfill-done flag so daily runs stay on the short window;
    `traces` (a {section: diagnostics} dict) is stored as `last_trace` for the show-reasoning panel."""
    def fn(ws):
        cfg = ws.setdefault("intel_ai", {})
        cfg["last_run"] = now_iso()
        cfg["last_model"] = model or ""
        cfg["last_error"] = error or ""
        if backfilled is not None:
            cfg["backfilled"] = bool(backfilled)
        if traces is not None:
            cfg["last_trace"] = traces
        return cfg
    try:
        return _mutate(client, fn)
    except Exception:
        return None


def intel_backfilled(ws):
    """True iff this client has already had its first 12-month backfill run."""
    return bool((ws or {}).get("intel_ai", {}).get("backfilled"))
