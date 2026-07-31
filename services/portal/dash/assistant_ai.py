"""The Atrium Assistant (team-only tab): retrieval-augmented chat over EVERYTHING in a workspace.

Sources it reads (all already in the portal's hands, no new database):
  * The Company profile          -- who the client IS: the facts, the brand guide, the story
                                    sections and the product catalogue, each its own chunk
                                    (digest.company_sections). The grounding every other answer
                                    leans on -- a campaign question is answered better by a model
                                    that knows the company behind the campaign.
  * Watcher transcript archives  -- every video's transcript, chunked ~1000 words, PLUS a cached
                                    per-video AI summary (digest chunk) once summarize_videos ran
  * Market Intelligence          -- every briefing entry (both sections) + a rolling digest
  * Campaigns + content          -- strategy, AI summary, every piece + its comments
  * Workspace metrics            -- the metrics/today/split snapshot the client sees
  * Content calendar + client conversations + website health notes
  * Communications timeline      -- every card (email/upwork/slack/meeting/call/note) + a snapshot
  * Delivery board tasks         -- one compact chunk per task (with its id) + a board snapshot
  * Generated client reports     -- each deck's payload (what we told the client, and when)
  * Client dashboard data        -- the per-client `<c>.json` export, indexed as DERIVED insight
                                    sections (digest.dashboard_sections), never a raw JSON dump
                                    (OPT-IN: needs the portal SA granted objectViewer on the
                                    client's dash bucket — run enable_assistant_dash_data.ps1;
                                    absent access is skipped).

Beyond answering, the assistant can PROPOSE workspace actions (add/move tasks, edit intel, build
or edit a report, ...) via the ===ATRIUM_ACTIONS=== protocol below — proposals are validated and
EXECUTED ONLY after a human approves (assistant_actions.py owns the registry + executors).

How it works (deliberately no new infra -- mirrors the workspace-JSON posture):
  1. `build_chunks` flattens every source into small text chunks with a kind/title/date.
  2. `build_index` computes a classic BM25 index over them (pure Python, no dependencies) and the
     whole thing is stored as ONE private object per client (workspace/assistant/<c>/index.json).
     `fingerprint` detects when the underlying data changed, so the index rebuilds lazily.
     `embed_index` OPTIONALLY augments it with a SEMANTIC leg: every chunk is embedded once (Vertex
     text-embedding-005, via an injected embedder) and the unit vectors are packed compactly into
     the same index object -- so retrieval is HYBRID (keyword + meaning) when embeddings are wired,
     and pure BM25 (unchanged) when they are not.
  3. `ask` runs HYBRID retrieval and answers with the SAME provider plumbing as the intel brain
     (`intel_ai._call`, default model -- Vertex Gemini when configured):
       * metadata PRE-FILTER -- an unambiguous single-source question ("how are we doing on email?")
         scopes retrieval to that kind before scoring; an optional date range scopes dated sources.
       * per query, a BM25 ranking AND (when embedded) a cosine-similarity ranking;
       * those rankings are fused with RECIPROCAL RANK FUSION (rank-only, so incompatible BM25 and
         cosine scales never fight) into one candidate pool;
       * the pool is (optionally) RE-RANKED by a cross-encoder (Vertex Ranking API, via an injected
         reranker) -- retrieve wide, then keep the truly-relevant few;
       * the survivors are packed into a grounded prompt; the model answers ONLY from the excerpts
         and names its sources (returned so the UI can show citation chips).
     The admin's DEPTH control ('quick'|'standard'|'deep') shapes it: deep first asks the model to
     PLAN extra search queries (so a comparative question retrieves each entity's actual positions),
     retrieves wider, turns provider thinking ON, and asks for a structured analysis; quick keeps it
     to a few sentences. Every depth is allowed to SYNTHESIZE across excerpts -- differing
     recommendations count as disagreement even when nobody names the other.

Pure + testable: chunking/indexing/BM25/RRF/fusion are dependency-free; the semantic leg, query
embedding, and rerank are all INJECTED (an `embedder`, `query_embedder`, `reranker`), and `ask`
also accepts a `caller` injection, so tests run with no network and a default deploy with no
embeddings behaves exactly like the old BM25-only path. Every failure degrades to
(\"\", sources, reason) -- never a raise.
"""

import base64
import hashlib
import json
import math
import re
import struct
import urllib.parse

import digest

# Small, boring stopword list -- enough to keep BM25 focused without a dependency.
_STOP = frozenset(
    "a an and are as at be but by for from has have how i if in into is it its me my not of on or "
    "our so that the their them they this to was we what when where which who why will with you "
    "your".split())

CHUNK_WORDS = 1000          # transcript chunk size (words)
TOP_K = 18                  # excerpts handed to the model per question
MAX_CONTEXT_CHARS = 90000   # hard cap on packed context (stays well inside Gemini's window)
INDEX_VERSION = 5           # bump to force a one-time rebuild when the index SHAPE changes
                            # (v4: the distilled layer -- dashboard digests instead of raw JSON,
                            #  communications/tasks/reports indexed, video summary chunks with
                            #  parent pointers for small-to-big expansion)
                            # (v5: the Company profile -- facts/brand/story/products indexed)
                            # 🔴 BUMPING THIS INVALIDATES EVERY STORED VECTOR (the chunk shape
                            # changes, so every emb_sig mismatches) -> the next rebuild re-embeds
                            # the WHOLE corpus. That is JOB work, never request work: see
                            # needs_full_embed + assistant_reindex.py.

# The request path must never pay for an unbounded embed. When a rebuild needs more NEW vectors
# than this, the ask embeds this many, marks the index `emb_partial`, and queues the client for the
# assistant-reindex JOB (which runs uncapped) to finish it. Sized so the worst case is a few
# seconds of Vertex calls, not the ~344s full re-embed that blew the request timeout on 2026-07-31.
EMBED_MAX_NEW_INLINE = 400

# How many mail thread archives feed the index (newest first). Mirrored by main._assistant_mail.
MAIL_ARCHIVE_CAP = 150


def _tokens(text):
    """Lowercase word tokens minus stopwords (the BM25 vocabulary)."""
    return [t for t in re.findall(r"[a-z0-9']+", (text or "").lower())
            if len(t) > 1 and t not in _STOP]


def _searchable(chunk):
    """What BM25/embeddings actually index for a chunk: its TITLE plus its body.

    The title carries the ENTITY NAME a user searches by -- the creator/channel name ("Fuel Your
    Wander"), the video title, the campaign name, the email subject -- and that name is usually
    ABSENT from the body (a transcript rarely says the channel's own name). Indexing the title too
    is what lets "what would Fuel Your Wander say about ..." retrieve that creator's transcripts;
    without it the name is invisible to retrieval and the Assistant reports it has no such content."""
    title = (chunk.get("title") or "").strip()
    text = chunk.get("text") or ""
    return (title + "\n" + text) if title else text


# --- 1. Flatten the workspace into chunks ---------------------------------------------------------
# Chunks come in two LEVELS (the small-to-big / parent-document retrieval pattern):
#   * "digest" -- a distilled insight chunk (a video's AI summary, a communication card, a computed
#     dashboard section). Small, high-signal, what retrieval should usually surface.
#   * "full"   -- the underlying raw material (transcript excerpts, full email thread text). A
#     digest chunk carries a `parent` id shared with its full chunks, so _expand_hits can UNFOLD
#     the full document behind a top-ranked digest when the question needs the detail.
# Level-less chunks (metrics, calendar, ...) have no hierarchy and behave exactly as before.
def build_chunks(ws, archives, dash_data=None, mail_threads=None):
    """Every source in the workspace as a flat list of
    {id, kind, title, url, date, text[, parent, level]} chunks.

    `archives` is [(channel_entry, videos), ...] -- the Watcher registry entries with their video
    lists (loaded by the caller so this stays I/O-free). `dash_data` is the optional client
    dashboard JSON (None when the bucket isn't readable) -- indexed as DERIVED insight sections
    (digest.dashboard_sections), never as a raw JSON dump. `mail_threads` is the optional list of
    loaded Mail thread archives (subject + full messages), so the chat can answer over the
    client's actual email correspondence too."""
    chunks = []

    def add(cid, kind, title, text, url="", date="", parent="", level=""):
        text = (text or "").strip()
        if text:
            c = {"id": cid, "kind": kind, "title": title, "url": url,
                 "date": date, "text": text}
            if parent:
                c["parent"] = parent
            if level:
                c["level"] = level
            chunks.append(c)

    # The Company profile: who the client IS -- the facts, the brand guide, each story section and
    # the product catalogue, each its own chunk (digest.company_sections). This is the only source
    # that answers "what do they sell?" / "what tone do we write in?" / "how long have they traded?",
    # and it grounds every other answer: a campaign question is answered better by a model that
    # knows the company behind the campaign. Deliberately UNDATED -- a date-range scope on
    # transcripts must never make the client's own identity invisible.
    for sid, title, text in digest.company_sections(ws):
        add("company:%s" % sid, "company", title, text)

    # Watcher transcripts. Each video with a cached AI summary gets a compact DIGEST chunk (the
    # usual retrieval target); the raw transcript stays indexed as FULL chunks under the same
    # parent id, so a strong digest hit can unfold the actual words (small-to-big).
    for ch, videos in archives or []:
        cname = ch.get("title", "channel")
        for v in videos:
            vparent = "yt:%s:%s" % (ch.get("id", ""), v.get("id", ""))
            summary = (v.get("summary") or "").strip()
            if summary:
                add("yts:%s:%s" % (ch.get("id", ""), v.get("id", "")),
                    "video", "%s — %s (summary)" % (cname, v.get("title", "")),
                    summary, url=v.get("url", ""), date=v.get("published", ""),
                    parent=vparent, level="digest")
            words = (v.get("transcript") or "").split()
            for i in range(0, len(words), CHUNK_WORDS):
                part = " ".join(words[i:i + CHUNK_WORDS])
                add("%s:%d" % (vparent, i // CHUNK_WORDS),
                    "video", "%s — %s" % (cname, v.get("title", "")),
                    part, url=v.get("url", ""), date=v.get("published", ""),
                    parent=vparent, level="full")

    # Market Intelligence briefing entries, plus one rolling digest of the freshest items (the
    # single strong hit for broad "what's happening out there?" questions).
    intel = ws.get("intel") or {}
    for section, label in (("business_research", "Business Research"),
                           ("media_buying", "Media Buying News")):
        for e in intel.get(section) or []:
            add("intel:%s" % e.get("id", ""), "intel",
                "Market Intelligence (%s): %s" % (label, e.get("title", "")),
                " ".join(filter(None, [e.get("title", ""), e.get("body", ""),
                                       e.get("relevance", ""), "Source: " + (e.get("source") or "")])),
                url=e.get("link", ""), date=e.get("date", ""))
    idig = digest.intel_digest(ws)
    if idig:
        add("intel:digest", "intel", "Market Intelligence digest (latest items, both sections)",
            idig)

    # Campaigns: strategy + AI summary, then every content piece with its status and comments.
    for camp in ws.get("campaigns") or []:
        strategy = camp.get("strategy") or {}
        add("camp:%s" % camp.get("id", ""), "campaign",
            "Campaign: %s" % camp.get("name", ""),
            " ".join(filter(None, ["Channel: %s." % (camp.get("channel") or ""),
                                   json.dumps(strategy) if strategy else "",
                                   camp.get("ai_summary", "")])))
        for p in camp.get("content") or []:
            comments = "; ".join("%s: %s" % (c.get("sender_name") or c.get("sender", ""),
                                             c.get("body", ""))
                                 for c in p.get("comments") or [])
            label = " ".join(filter(None, [p.get("ref", ""), p.get("type_tag", "")]))
            add("content:%s" % p.get("id", ""), "content",
                "Content piece %s (%s)" % (label or p.get("id", ""), camp.get("name", "")),
                " ".join(filter(None, [p.get("sub_tag", ""), p.get("platform", ""),
                                       "status " + (p.get("status") or ""),
                                       p.get("caption", ""), p.get("client_note", ""),
                                       ("Comments: " + comments) if comments else ""])),
                date=p.get("date", ""))

    # The workspace metrics snapshot (what the client's overview shows).
    metrics = {k: ws.get(k) for k in ("metrics", "today", "split") if ws.get(k)}
    if metrics:
        add("metrics", "metrics", "Workspace metrics snapshot", json.dumps(metrics))
    series = ws.get("series")
    if series:
        add("series", "metrics", "Leads time series", json.dumps(series))

    # Calendar, conversations, website health.
    cal = ws.get("calendar") or []
    if cal:
        add("calendar", "calendar", "Content calendar",
            "; ".join("%s: %s (%s)" % (e.get("date", ""), e.get("label", ""),
                                       "done" if e.get("status") == "done" else "planned")
                      for e in cal))
    for conv in ws.get("conversations") or []:
        add("conv:%s" % conv.get("id", ""), "conversation",
            "Client conversation: %s" % conv.get("subject", ""),
            "; ".join("%s: %s" % (m.get("from", ""), m.get("body", ""))
                      for m in conv.get("messages") or []))
    wh = ws.get("website_health") or {}
    if wh.get("url") or wh.get("notes"):
        add("health", "website", "Website health",
            "Site: %s. Notes: %s. Last check: %s"
            % (wh.get("url", ""), wh.get("notes", ""), json.dumps(wh.get("last_check") or {})))

    # The unified Communications timeline: every card (email/upwork/slack/meeting/call/note) is a
    # DIGEST chunk; an email/upwork card points at its full thread archive (parent mail:<key>) so
    # the small-to-big expansion can unfold the actual correspondence. One computed snapshot chunk
    # answers the broad "what's the latest with this client?" in a single hit.
    for it in ws.get("communications") or []:
        parent = ("mail:%s" % it.get("thread_key")) if it.get("thread_key") else ""
        add("comm:%s" % it.get("id", ""), "comms",
            "Communication (%s): %s" % (it.get("channel") or "note", it.get("title") or ""),
            " ".join(filter(None, [
                "Audience: %s." % ("team-only" if it.get("audience") == "team"
                                   else "client-visible"),
                ("People: %s." % it.get("people")) if it.get("people") else "",
                it.get("summary") or ""])),
            date=(it.get("date") or "")[:10], parent=parent, level="digest" if parent else "")
    csnap = digest.comms_snapshot(ws)
    if csnap:
        add("comms:snapshot", "comms", "Communications overview (all channels, latest first)",
            csnap)

    # The delivery board: one compact chunk per task (id included, so the assistant can PROPOSE
    # precise actions on it) + a board snapshot. Tasks are deliberately UNDATED chunks -- a
    # date-range scope on transcripts must never make the task list invisible.
    for t in ws.get("tasks") or []:
        add("task:%s" % t.get("id", ""), "task",
            "Task: %s" % (t.get("title") or "(untitled)"), digest.task_text(t))
    tsnap = digest.tasks_snapshot(ws)
    if tsnap:
        add("tasks:board", "task", "Delivery board snapshot (stages, blockers, launches)", tsnap)

    # Generated client reports: what we told (or will tell) the client, and when.
    for r in ws.get("reports") or []:
        add("report:%s" % r.get("id", ""), "report",
            "Client report: %s (%s)" % (r.get("title") or "report", (r.get("date") or "")[:10]),
            _flatten_text(r.get("payload") or {}), date=(r.get("date") or "")[:10])

    # Client email threads (the Mail tab's archive), chunked like transcripts so one long thread
    # can yield several retrievable excerpts. The summary rides along for cheap high-level hits.
    snapshot = []
    for t in mail_threads or []:
        head = "Email thread with %s" % (", ".join(t.get("participants") or [])[:200] or "the client")
        st = t.get("stats") or {}
        snapshot.append("%s | last message %s | awaiting AGORA reply: %s | avg AGORA reply time: %s"
                        % (t.get("subject", "(no subject)"), (t.get("last_date") or "?")[:10],
                           "YES" if st.get("awaiting_reply") else "no",
                           ("%s hours" % st["avg_response_hours"])
                           if isinstance(st.get("avg_response_hours"), (int, float)) else "n/a"))
        body_lines = ["Subject: %s" % t.get("subject", "")]
        if st.get("awaiting_reply"):
            body_lines.append("Status: the last word is the client's -- an AGORA reply is due.")
        if t.get("summary"):
            body_lines.append("Summary: %s" % t.get("summary"))
        for m in t.get("messages") or []:
            body_lines.append("From %s on %s: %s" % (m.get("from", ""), (m.get("date") or "")[:10],
                                                     m.get("body", "")))
        words = "\n".join(body_lines).split()
        mparent = "mail:%s" % t.get("id", "")
        for i in range(0, len(words), CHUNK_WORDS):
            add("%s:%d" % (mparent, i // CHUNK_WORDS), "email",
                "%s — %s" % (head, t.get("subject", "")),
                " ".join(words[i:i + CHUNK_WORDS]), date=(t.get("last_date") or "")[:10],
                parent=mparent, level="full")
    # One computed responsiveness snapshot across ALL threads, so "how well are we handling this
    # client's email?" retrieves real numbers (reply speed, threads left hanging) in one hit.
    if snapshot:
        add("mail:responsiveness", "email",
            "Email responsiveness snapshot (reply speed, who owes whom)", "\n".join(snapshot))

    # The client dashboard export (opt-in source; None when unreadable) -- indexed as DERIVED
    # insight sections (totals/momentum, campaigns, creatives, weekly trend, audience, email),
    # never as a raw JSON dump: focused sections retrieve on merit and leave context budget for
    # other sources, where one 5,000-row dump matched on noise and drowned the prompt.
    for sid, title, text in digest.dashboard_sections(dash_data or {}):
        add("dash:%s" % sid, "dashboard", title, text)

    return chunks


def _flatten_text(v):
    """A report payload (nested dicts/lists of strings) as flat searchable text."""
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, dict):
        return " ".join(filter(None, ("%s: %s" % (k, _flatten_text(x)) for k, x in v.items())))
    if isinstance(v, list):
        return " ".join(filter(None, (_flatten_text(x) for x in v)))
    return "" if v is None else str(v)


def fingerprint(ws, archives, dash_stamp=None):
    """A cheap change-detector for the index: rebuild whenever any source moved.

    `dash_stamp` is the dashboard export's last-modified marker (blob metadata, probed by the
    caller) -- without it a refreshed dashboard never re-indexed until some other source moved."""
    import hashlib
    intel = ws.get("intel") or {}
    company = ws.get("company") or {}
    desc = {
        # The company profile is small, so hash its whole derived text -- any edit to a fact, the
        # brand guide, a story section or a product re-indexes, with no per-field bookkeeping.
        "company": digest.company_brief(ws) if company else "",
        "watcher": [(ch.get("id"), ch.get("transcript_count"), ch.get("last_fetch"))
                    for ch, _v in archives or []],
        # Cached per-video AI summaries change the chunk set without touching the registry entry.
        "summaries": [(ch.get("id"), sum(1 for v in vids or [] if v.get("summary")))
                      for ch, vids in archives or []],
        "intel": [len(intel.get("business_research") or []), len(intel.get("media_buying") or [])],
        "campaigns": [(c.get("id"), len(c.get("content") or [])) for c in ws.get("campaigns") or []],
        "metrics": ws.get("metrics"),
        "calendar": len(ws.get("calendar") or []),
        "conversations": [(c.get("id"), len(c.get("messages") or []))
                          for c in ws.get("conversations") or []],
        "communications": [(c.get("id"), c.get("date"), len(c.get("summary") or ""))
                           for c in ws.get("communications") or []],
        "tasks": [(t.get("id"), t.get("stage"), len(t.get("comments") or []), t.get("due_date"))
                  for t in ws.get("tasks") or []],
        "reports": [(r.get("id"), r.get("updated_at") or r.get("created_at"))
                    for r in ws.get("reports") or []],
        "mail": [(t.get("id"), t.get("message_count"), t.get("last_date"))
                 for t in ((ws.get("mail") or {}).get("threads") or [])],
        "dash": dash_stamp,
    }
    return hashlib.md5(json.dumps(desc, sort_keys=True, default=str).encode("utf-8")).hexdigest()


# --- 2. BM25 index --------------------------------------------------------------------------------
def build_index(chunks, fp=""):
    """A stored-JSON BM25 index: chunks + document frequencies + average length.

    Pure + dependency-free (no network). The SEMANTIC leg (per-chunk vectors) is attached SEPARATELY
    by `embed_index` so this stays testable off-cloud and a no-embeddings deploy is unchanged."""
    df = {}
    lengths = []
    for c in chunks:
        toks = _tokens(_searchable(c))
        lengths.append(len(toks))
        for t in set(toks):
            df[t] = df.get(t, 0) + 1
    from workspace import now_iso
    return {"v": INDEX_VERSION, "fingerprint": fp, "built_at": now_iso(), "chunks": chunks, "df": df,
            "n_docs": len(chunks),
            "avgdl": (sum(lengths) / len(lengths)) if lengths else 0.0}


# --- 2b. Semantic leg: embed chunks + pack vectors compactly into the index -----------------------
# Vectors are unit-normalised and packed as little-endian float16 (`struct` 'e') then base64'd, so
# cosine similarity at query time is a plain dot product and the whole vector store stays small
# (256 dims -> ~512 bytes/chunk -> ~0.7 KB base64) even for a client with thousands of chunks. All
# stdlib -- no numpy, no vector DB.
def _normalize(vec):
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


def _pack_vec(vec):
    """Unit-normalise then pack a float vector to a compact base64 float16 string."""
    v = _normalize(vec)
    return base64.b64encode(struct.pack("<%de" % len(v), *v)).decode("ascii")


def _unpack_vec(b64, dim):
    """Decode a packed vector back to a list[float] of length `dim`, or None if it can't."""
    try:
        return list(struct.unpack("<%de" % dim, base64.b64decode(b64)))
    except Exception:
        return None


def _emb_sig(chunk):
    """A short content signature for a chunk's searchable text. Used to decide whether a carried-over
    vector is still valid: a chunk id can be REUSED with new content (e.g. the 'metrics' snapshot
    changes every refresh, a transcript chunk is re-fetched), so reuse must key on content, not id."""
    return hashlib.md5(_searchable(chunk).encode("utf-8")).hexdigest()


def carry_over(prev):
    """Just the vector maps of `prev` -- what embed_index reuses, and NOTHING else.

    A stored index is mostly `chunks` (34 MiB / 6.5k chunks for a big workspace, and several times
    that once parsed). The rebuild only ever reads prev's emb/emb_sig/emb_dim, so lifting those out
    lets the caller DROP the rest before it builds and serializes the new index. Holding the whole
    previous index across that peak is what pushed the container past its memory limit on
    2026-07-31 -- see main._assistant_index."""
    p = prev or {}
    return {"emb": p.get("emb") or {}, "emb_sig": p.get("emb_sig") or {},
            "emb_dim": p.get("emb_dim") or 0}


def needs_full_embed(prev):
    """True when rebuilding from `prev` would have to embed the WHOLE corpus (job work, not request).

    Two cases: `prev` carries no semantic leg at all, or it was built at a DIFFERENT INDEX_VERSION
    -- a shape change alters every chunk's searchable text, so every stored emb_sig mismatches and
    nothing can be carried over. On a big workspace that is minutes of Vertex calls; because the
    index is written LAST, such a rebuild never persisted and every later ask retried it, which is
    exactly how the Assistant went permanently dead on 2026-07-31."""
    p = prev or {}
    return (not p.get("emb")) or (not p.get("emb_dim")) or p.get("v") != INDEX_VERSION


def dash_stamp(client, dashboard_url=""):
    """The dashboard export blob's last-modified marker (metadata-only GET), or None.

    Feeds the index fingerprint so a refreshed dashboard re-indexes. 🔴 The ask path and the
    assistant-reindex JOB must derive this IDENTICALLY -- if the job computed a different stamp,
    the index it wrote would read as stale on the very next ask and the client would requeue
    forever. Any failure (no bucket, no access, local dev) is None, which fingerprints consistently.

    ⚠️ KNOWN BUG, deliberately preserved here: this probes `agora-data-driven-<client>-dash`, but
    the dashboard stack's key is `dash_data_key(client, dashboard_url)` and the two DIVERGE for
    every production client (see dash_data_key) -- so in production this is always None and a
    dashboard refresh alone never re-indexes. Fixing it changes the fingerprint for EVERY client
    and forces an estate-wide rebuild, so it is a change of its own, not a side effect of this
    one. `dashboard_url` is already threaded through for when that fix is made."""
    try:
        from google.cloud import storage  # lazy
        blob = storage.Client().bucket("agora-data-driven-%s-dash" % client) \
                               .get_blob("%s.json" % client)
        return blob.updated.isoformat() if blob is not None and blob.updated else None
    except Exception:
        return None


def gather_sources(client, ws):
    """Every input the index is built from: (archives, mail_threads, dash_data, stamp).

    ONE definition, shared by the ask path (main._assistant_index) and the assistant-reindex JOB.
    They MUST agree: the fingerprint is derived from exactly these, so a job that gathered a
    different set would write an index the next ask considers stale -- an endless requeue loop."""
    import workspace
    archives = [(ch, workspace.read_watcher_videos(client, ch.get("id", "")))
                for ch in workspace.watcher_channels(ws)]
    mail = []
    for t in workspace.mail_threads(ws)[:MAIL_ARCHIVE_CAP]:
        full = workspace.read_mail_thread(client, t.get("id", ""))
        if full:
            mail.append(full)
    url = (ws or {}).get("dashboard_url", "")
    return archives, mail, read_client_dash_data(client, url), dash_stamp(client, url)


def embed_index(index, embedder, prev=None, max_new=0):
    """Attach a semantic leg to `index` IN PLACE: embed every chunk's text and store packed vectors.

    `max_new` (0 = uncapped, what the JOB uses) bounds how many chunks may be embedded in one call.
    Over the cap the remainder is left unembedded and recorded as `emb_partial`, so the request path
    can never stall on a full-corpus embed -- those chunks keep working on the BM25 leg until the
    assistant-reindex job fills them in.

    INCREMENTAL: when `prev` (this client's previous index) carries a semantic leg, any chunk whose id
    AND content signature are unchanged REUSES its stored vector instead of re-embedding. So a Watcher
    fetch that adds a few transcripts embeds only the handful of new chunks, not the whole corpus --
    the fix for the "was fast, now unusable" stall where every fetch re-embedded thousands of chunks
    synchronously inside the ask request. With no `prev` (or a `prev` without embeddings) this embeds
    everything, exactly as before.

    `embedder(list_of_texts) -> (list_of_vectors, error)` is injected (the default caller wires it to
    intel_ai.embed_texts). A per-chunk vector may be None (its batch failed) -- those chunks just
    miss the semantic leg (BM25 still covers them). Any total failure leaves the index BM25-only.
    Returns the same index dict (so callers can `index = embed_index(index, fn)`)."""
    chunks = index.get("chunks") or []
    if not chunks or embedder is None:
        return index
    prev = prev or {}
    prev_emb = prev.get("emb") or {}
    prev_sig = prev.get("emb_sig") or {}
    prev_dim = prev.get("emb_dim") or 0

    emb, sig, dim = {}, {}, 0
    to_embed = []                     # (chunk_id, searchable_text) for the chunks that actually need it
    for c in chunks:
        cid = c["id"]
        s = _emb_sig(c)
        packed = prev_emb.get(cid)
        if packed is not None and prev_dim and prev_sig.get(cid) == s:
            emb[cid] = packed         # unchanged -> reuse the stored vector, skip the embed call
            sig[cid] = s
            dim = dim or prev_dim
        else:
            to_embed.append((cid, _searchable(c), s))

    # Bound the work when a cap is in force (the ask path). The deferred chunks simply have no
    # vector yet -- BM25 still retrieves them -- and `emb_partial` tells the caller to hand the
    # rest to the job rather than silently shipping a half-embedded index forever.
    deferred = 0
    if max_new and len(to_embed) > max_new:
        deferred = len(to_embed) - max_new
        to_embed = to_embed[:max_new]

    if to_embed:
        try:
            vectors, _err = embedder([t for _cid, t, _s in to_embed])
        except Exception:
            vectors = None
        for (cid, _t, s), v in zip(to_embed, vectors or []):
            if not v:
                continue
            dim = dim or len(v)
            emb[cid] = _pack_vec(v)
            sig[cid] = s

    if emb:
        index["emb"] = emb
        index["emb_sig"] = sig        # per-embedded-chunk content signature (powers the next reuse)
        index["emb_dim"] = dim
        index["emb_count"] = len(emb)
    if deferred:
        index["emb_partial"] = deferred
    else:
        index.pop("emb_partial", None)
    return index


def has_embeddings(index):
    """True iff `index` carries a semantic leg (so the ask path should run HYBRID retrieval)."""
    return bool((index or {}).get("emb")) and bool((index or {}).get("emb_dim"))


# --- 2c. Metadata filtering -----------------------------------------------------------------------
# All source kinds the chunker emits (used to validate an inferred single-source filter).
_KINDS = {"video", "intel", "campaign", "content", "metrics", "calendar", "conversation",
          "website", "email", "dashboard", "comms", "task", "report", "company"}

# Conservative source inference: a phrase group -> the chunk kinds it means. Pre-filtering is
# POWERFUL but dangerous (wrongly excluding relevant chunks), so we only ever apply it when EXACTLY
# ONE group matches the question (an unambiguous single-source ask like "how are we handling email?")
# -- a cross-source question ("campaigns vs what creators say") matches several groups and stays
# unfiltered. `ask` also relaxes the filter if it would leave nothing eligible.
_KIND_HINTS = (
    (("email", "inbox", "reply", "replied", "responsiveness", "correspond"),
     {"email", "comms"}),
    (("transcript", "video", "youtube", "creator", "episode", "watched"), {"video"}),
    (("market intelligence", "industry news", "competitor news", "briefing", "the news"), {"intel"}),
    (("campaign", "content piece", "ad copy", "creative", "caption"), {"content", "campaign"}),
    (("dashboard", "kpi", "roas", "cpl", "cost per lead", "spend"), {"dashboard", "metrics"}),
    (("meeting", "call notes", "upwork", "communication", "told the client", "client said"),
     {"comms", "email", "conversation"}),
    (("task", "tasks", "deliverable", "blocked", "to-do", "delivery board"), {"task"}),
    (("report", "presentation", "deck", "slides"), {"report"}),
    (("brand voice", "tone of voice", "their products", "product catalogue", "product catalog",
      "what do they sell", "company history", "about the company", "brand guide", "founded",
      "positioning", "who are they"), {"company"}),
)


def _infer_kinds(question):
    """A confident single-source scope for `question`, or None to search every source.

    Returns a set of chunk kinds only when EXACTLY ONE hint group matches; otherwise None (so a
    multi-source or generic question is never over-filtered)."""
    ql = (question or "").lower()
    matched = [kinds for phrases, kinds in _KIND_HINTS if any(p in ql for p in phrases)]
    return matched[0] if len(matched) == 1 else None


def _creator_names(chunks):
    """The lowercased channel/creator names present in the index (the part of a video chunk's title
    before the ' — <video title>' separator). Cheap; derived from the chunks already in hand."""
    names = set()
    for c in chunks:
        if c.get("kind") == "video":
            name = (c.get("title") or "").split(" — ", 1)[0].strip().lower()
            if name:
                names.add(name)
    return names


def _question_names_creator(question, chunks):
    """True if `question` mentions a creator/channel this workspace actually watches.

    This is why "what would Fuel Your Wander say about the Colorado Escape Campaign" must NOT be
    scoped to campaign-only: it names a creator, so it is inherently cross-source. `_infer_kinds`
    can't know that (it only matches the literal word "creator"), but the watched channels' names
    are right here in the index."""
    ql = (question or "").lower()
    return any(name in ql for name in _creator_names(chunks))


def _passes(chunk, date_from, date_to, kinds):
    """Metadata gate: an optional kind scope + a date range (dated chunks only; undated always pass)."""
    if kinds is not None and chunk.get("kind") not in kinds:
        return False
    date = chunk.get("date") or ""
    if date and ((date_from and date < date_from) or (date_to and date > date_to)):
        return False
    return True


# --- 2d. The retriever: BM25 + cosine, both over the SAME in-memory prep (built once per ask) ------
# The old search re-tokenised the WHOLE corpus on EVERY query -- crippling for deep mode's multi-query
# retrieval. This tokenises once per ask, then every query (and every RRF leg) is cheap dict work.
class _Retriever:
    """One-shot retrieval helper over a stored index. Tokenises + decodes vectors LAZILY and ONCE,
    then serves any number of BM25 / cosine queries. `allowed` is the pre-filtered chunk-index set."""

    def __init__(self, index):
        self.index = index or {}
        self.chunks = self.index.get("chunks") or []
        self.n = len(self.chunks)
        self._tf = None       # per-chunk {term: count}
        self._dl = None       # per-chunk token length
        self._df = dict(self.index.get("df") or {})
        self._avgdl = self.index.get("avgdl") or 0.0
        self._emb = None      # {chunk_idx: unit vector}
        self._emb_dim = self.index.get("emb_dim") or 0

    def _ensure_bm25(self):
        if self._tf is not None:
            return
        self._tf, self._dl = [], []
        for c in self.chunks:
            toks = _tokens(_searchable(c))
            tf = {}
            for t in toks:
                tf[t] = tf.get(t, 0) + 1
            self._tf.append(tf)
            self._dl.append(len(toks))
        if not self._avgdl:
            self._avgdl = (sum(self._dl) / self.n) if self.n else 1.0
        if not self._df:
            for tf in self._tf:
                for t in tf:
                    self._df[t] = self._df.get(t, 0) + 1

    def _ensure_emb(self):
        if self._emb is not None:
            return
        self._emb = {}
        raw = self.index.get("emb") or {}
        if not raw or not self._emb_dim:
            return
        idx_of = {c.get("id"): i for i, c in enumerate(self.chunks)}
        for cid, b64 in raw.items():
            i = idx_of.get(cid)
            if i is None:
                continue
            v = _unpack_vec(b64, self._emb_dim)
            if v:
                self._emb[i] = v

    def bm25(self, query, allowed):
        """Chunk indices in `allowed`, ranked by BM25 for `query` (descending, positive scores only)."""
        self._ensure_bm25()
        q_terms = _tokens(query)
        if not q_terms:
            return []
        n_docs = self.n or 1
        avgdl = self._avgdl or 1.0
        scored = []
        for i in allowed:
            tf = self._tf[i]
            dl = self._dl[i]
            if not dl:
                continue
            score = 0.0
            for t in q_terms:
                f = tf.get(t)
                if not f:
                    continue
                df = self._df.get(t, 0)
                idf = math.log((n_docs - df + 0.5) / (df + 0.5) + 1.0)
                score += idf * (f * 2.5) / (f + 1.5 * (0.25 + 0.75 * dl / avgdl))
            if score > 0:
                scored.append((score, i))
        scored.sort(key=lambda s: -s[0])
        return [i for _s, i in scored]

    def cosine(self, qvec, allowed):
        """Chunk indices in `allowed` that HAVE a vector, ranked by cosine similarity to `qvec`.
        Stored vectors are unit-normalised, so cosine is a dot product against the normalised query."""
        self._ensure_emb()
        if not self._emb or not qvec:
            return []
        q = _normalize(qvec)
        scored = []
        for i in allowed:
            v = self._emb.get(i)
            if v is None:
                continue
            scored.append((sum(a * b for a, b in zip(q, v)), i))
        scored.sort(key=lambda s: -s[0])
        return [i for _s, i in scored]


# --- 2e. Small-to-big expansion -------------------------------------------------------------------
# Digest chunks retrieve BEST (small, high-signal) but sometimes the answer needs the words behind
# them ("what exactly did they say?"). When a top hit is a digest with a `parent`, unfold the most
# question-relevant FULL chunks under the same parent into the context -- compact by default, the
# whole document when it matters. Bounded so expansion can never crowd out source diversity.
_EXPAND_TOP = 4        # only the best few digest hits are expanded
_EXPAND_PER = 2        # full sibling chunks pulled in per expanded hit
_EXPAND_BUDGET = 30000  # total chars of expansion text


def _expand_hits(index, hits, question):
    """`hits` with each top digest hit followed by its best full-text siblings. Pure; no I/O."""
    chunks = index.get("chunks") or []
    by_parent = {}
    for i, c in enumerate(chunks):
        if c.get("level") == "full" and c.get("parent"):
            by_parent.setdefault(c["parent"], []).append(i)
    if not by_parent:
        return hits
    have = {c.get("id") for c in hits}
    retr = _Retriever(index)
    out, used, expanded = [], 0, 0
    for c in hits:
        out.append(c)
        parent = c.get("parent")
        if (c.get("level") != "digest" or not parent or parent not in by_parent
                or expanded >= _EXPAND_TOP or used >= _EXPAND_BUDGET):
            continue
        siblings = by_parent[parent]
        ranked = retr.bm25(question, siblings) or siblings
        added = 0
        for i in ranked:
            sib = chunks[i]
            if sib.get("id") in have:
                continue
            out.append(sib)
            have.add(sib.get("id"))
            used += len(sib.get("text") or "")
            added += 1
            if added >= _EXPAND_PER or used >= _EXPAND_BUDGET:
                break
        if added:
            expanded += 1
    return out


def _rrf(rankings, k=60, limit=None):
    """Reciprocal Rank Fusion of several ranked index-lists into one. Rank-only (score scales never
    fight): each list contributes 1/(k + rank) to an item's fused score. Returns fused indices desc."""
    scores = {}
    for ranking in rankings:
        for rank, idx in enumerate(ranking):
            scores[idx] = scores.get(idx, 0.0) + 1.0 / (k + rank + 1)
    fused = sorted(scores, key=lambda i: -scores[i])
    return fused[:limit] if limit else fused


# --- 2f. Watcher video summaries (the hierarchical-summarization leg) -----------------------------
# One cached AI summary per transcript, stored back INTO the archive object (v["summary"]) so it is
# written once and indexed forever. Deliberately run ONLY from the explicit Reindex path (and the
# report generator) -- never inside a chat request, where a batch of model calls would stall the
# stream the way whole-corpus re-embedding once did.
_SUMMARY_SYSTEM = (
    "You distill a creator/competitor video transcript (or blog article) for a marketing agency's "
    "intelligence archive. Write a compact plain-text summary: one line stating what the piece is "
    "about, then 4-7 short bullet lines ('- ') with the concrete claims, strategies, numbers, and "
    "recommendations it makes, then one line 'Takeaway: ...' with what a marketer should act on. "
    "No preamble, no markdown headings, under 180 words.")

SUMMARY_BATCH = 8           # summaries per reindex run (the rest catch the next run)
_SUMMARY_MAX_WORDS = 9000   # transcript words handed to the summarizer


def summarize_videos(archives, caller, cap=SUMMARY_BATCH):
    """Fill missing `summary` fields on archived videos, at most `cap` model calls.

    Mutates the video dicts IN PLACE and returns (updates, count, last_error) where `updates` maps
    channel_id -> its (mutated) videos list, so the caller persists exactly the archives that
    changed. `caller(system, user) -> (text, err)` is the model seam; any per-video failure is
    skipped (retried on a later run) and never raises."""
    updates, count, last_err = {}, 0, ""
    for ch, videos in archives or []:
        for v in videos or []:
            if count >= cap:
                return updates, count, last_err
            transcript = (v.get("transcript") or "").strip()
            if not transcript or (v.get("summary") or "").strip():
                continue
            body = " ".join(transcript.split()[:_SUMMARY_MAX_WORDS])
            user = "Title: %s\nChannel/site: %s\n\nTranscript:\n%s" % (
                v.get("title", ""), ch.get("title", ""), body)
            try:
                text, err = caller(_SUMMARY_SYSTEM, user)
            except Exception as e:  # a summarizer crash must never sink the reindex
                text, err = "", str(e)
            if err or not (text or "").strip():
                last_err = err or "empty summary"
                continue
            v["summary"] = text.strip()
            updates[ch.get("id", "")] = videos
            count += 1
    return updates, count, last_err


# BM25-only top-k (kept for callers/tests that want a single lexical ranking without the full ask
# pipeline). A date range filters DATED chunks; `kinds` optionally scopes by source kind.
def search(index, query, k=TOP_K, date_from="", date_to="", kinds=None):
    """Top-k chunks for `query` by BM25, honouring the metadata filter (kind + date range)."""
    chunks = index.get("chunks") or []
    allowed = [i for i, c in enumerate(chunks) if _passes(c, date_from, date_to, kinds)]
    idxs = _Retriever(index).bm25(query, allowed)
    return [chunks[i] for i in idxs[:k]]


# --- 3. Grounded answer ---------------------------------------------------------------------------
# The admin's detail control. Depth shapes retrieval width, provider thinking, and answer style;
# "deep" additionally query-plans (one extra model call) so comparative questions retrieve each
# entity's actual positions instead of just chunks containing the question's words.
DEPTHS = ("quick", "standard", "deep")
DEFAULT_DEPTH = "standard"

# Per-depth retrieval shape: (RRF candidate pool BEFORE rerank, FINAL excerpts handed to the model).
# "Retrieve wide, keep few": the pool is intentionally large (the reranker, when on, sorts it by true
# relevance); the final cap keeps the prompt focused. Without a reranker the top `final` of the fused
# pool are used directly. deep multi-queries + widest pool; quick is lean end-to-end.
_DEPTH_RETRIEVE = {"quick": (25, 8), "standard": (45, TOP_K), "deep": (60, 24)}

_DEPTH_STYLE = {
    "quick": ("Answer in 2-4 tight sentences: the direct answer and the headline numbers or names, "
              "nothing else."),
    "standard": ("Be direct and specific; a focused paragraph or two, with a short list when "
                 "comparing items."),
    "deep": ("Give a thorough, structured analysis. Synthesize ACROSS excerpts: compare positions, "
             "surface patterns and tensions, quote the key lines, and close with what it means for "
             "this client. Prefer short headed sections or bullet lists over one long wall of text."),
}


def _system_prompt(client_name, depth=DEFAULT_DEPTH, as_json=True):
    """The grounded-answer contract. `as_json` picks the OUTPUT shape: True = the `{"answer": ...}`
    envelope the synchronous path parses; False = PLAIN markdown for the STREAMING path (a JSON
    wrapper can't be streamed to the user without showing braces)."""
    tail = (" Answer with JSON only: {\"answer\": \"<your answer>\"}" if as_json else
            " Answer in clear GitHub-flavored markdown (headings, bold, and lists are welcome). Do "
            "NOT wrap your answer in JSON or code fences — just write the answer.")
    return (
        "You are the AGORA team's Atrium assistant for the client \"%s\". You answer questions "
        "using ONLY the numbered context excerpts provided — the client's company profile (who "
        "they are, their brand guide, their products), campaigns, metrics, market intelligence, "
        "watched-creator video transcripts, and dashboard data. "
        "Quote numbers and names from the excerpts. When you use an excerpt, mention its source "
        "naturally (e.g. 'in Carson Reed's video ...', 'per the dashboard KPIs'). "
        "Comparative or analytical questions deserve real synthesis: when asked about "
        "disagreements, differences, or comparisons, contrast what each source emphasizes or "
        "recommends — two creators pushing different strategies (say, cold email vs paid ads) IS "
        "a disagreement worth reporting even if neither ever mentions the other. Make clear which "
        "part is stated in the excerpts and which part is your inference from them. If the "
        "excerpts truly contain nothing relevant, say so plainly — never invent facts. "
        % client_name
        + _DEPTH_STYLE.get(depth, _DEPTH_STYLE[DEFAULT_DEPTH])
        + tail
    )


def _steer_note(steer):
    """A short prompt suffix carrying the team's mid-flight steer (from the plan checkpoint or a
    pause). Empty when there is none."""
    steer = (steer or "").strip()
    if not steer:
        return ""
    return ("\n\nIMPORTANT — the AGORA team reviewed your approach and added this guidance. Follow "
            "it, and let it override your earlier direction where they conflict:\n%s" % steer[:1000])


# --- Action proposals (propose -> approve -> execute) ---------------------------------------------
# The assistant can DO things -- but only by PROPOSING them. When the team's message asks for a
# change, the model appends a marker line + a JSON array of {action, params, note}; the server
# validates each proposal against assistant_actions.ACTIONS and the UI renders approval cards.
# NOTHING executes until a human clicks Approve (the Sentinel approval posture): the model has no
# execute path at all, so a hallucinated or hostile proposal is inert by construction.
ACTIONS_MARKER = "===ATRIUM_ACTIONS==="


def _actions_note(catalog):
    """The system-prompt suffix that teaches the model the proposal protocol. `catalog` is the
    human-readable action list from assistant_actions.catalog_text(); empty = actions disabled."""
    if not catalog:
        return ""
    return (
        "\n\nYou can also PROPOSE workspace actions for the team to approve -- you cannot execute "
        "anything yourself. Available actions:\n%s\n"
        "When (and ONLY when) the team asks you to change something -- add or move a task, mark "
        "one done, comment, edit market intelligence, log a communication, build or edit a "
        "report, run a check -- finish your reply, then on a new line write exactly %s followed "
        "by a JSON array like "
        "[{\"action\": \"<name>\", \"params\": {...}, \"note\": \"one line on what this does\"}]. "
        "Propose the minimal set of actions that fulfils the request, and use exact ids from the "
        "excerpts when you have them (tasks state their id). Never say a change has been made -- "
        "every proposal waits for the team's approval. If the message is only a question, do not "
        "write the marker at all." % (catalog, ACTIONS_MARKER))


def split_actions(text):
    """Split an answer into (visible_text, proposals). Lenient like _parse_answer: fenced or
    junk-wrapped JSON after the marker still parses; anything unparseable -> no proposals."""
    text = text or ""
    if ACTIONS_MARKER not in text:
        return text, []
    head, _sep, tail = text.partition(ACTIONS_MARKER)
    raw = tail.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-z]*\s*|\s*```$", "", raw, flags=re.I)
    proposals = []
    start = raw.find("[")
    if start >= 0:
        try:
            parsed, _end = json.JSONDecoder(strict=False).raw_decode(raw, start)
            if isinstance(parsed, list):
                proposals = [p for p in parsed
                             if isinstance(p, dict) and (p.get("action") or "").strip()]
        except ValueError:
            proposals = []
    return head.rstrip(), proposals


class _ActionTail:
    """Streaming filter for the proposal block: forward answer deltas while holding back a small
    tail (the marker could straddle two deltas), and once the marker appears, swallow the rest of
    the stream as the proposal JSON. The UI never sees a half-rendered marker."""

    _HOLD = len(ACTIONS_MARKER) + 2

    def __init__(self):
        self._buf = ""
        self._tail = ""
        self._in_actions = False

    def feed(self, text):
        """Forwardable visible text for this delta (may be '')."""
        if self._in_actions:
            self._tail += text
            return ""
        self._buf += text
        p = self._buf.find(ACTIONS_MARKER)
        if p >= 0:
            visible = self._buf[:p].rstrip()
            self._tail = self._buf[p + len(ACTIONS_MARKER):]
            self._in_actions = True
            self._buf = ""
            return visible
        if len(self._buf) <= self._HOLD:
            return ""
        visible, self._buf = self._buf[:-self._HOLD], self._buf[-self._HOLD:]
        return visible

    def finish(self):
        """(remaining_visible_text, proposals) once the stream has ended."""
        if self._in_actions:
            _head, proposals = split_actions(ACTIONS_MARKER + self._tail)
            return "", proposals
        out, self._buf = self._buf, ""
        return out, []


_PLAN_SYSTEM = (
    "You write search queries for a keyword (BM25) index over a marketing client's workspace: "
    "watched-creator video transcripts, market intelligence, campaigns and content, metrics, and "
    "client conversations. Given the team's question, return JSON only: {\"queries\": [\"...\"]} "
    "— 2 to 5 short keyword queries that together cover every entity, topic, and angle the "
    "question needs. For comparative questions write one query per entity/stance (e.g. 'Nick "
    "Saraev lead generation advice' and 'Carson Reed client acquisition ads') plus one for the "
    "shared topic. Plain words only, no boolean syntax.")


def plan_queries(question, history, caller):
    """Deep mode's retrieval plan: extra search queries from the model. Never raises — any failure
    (model error, non-JSON, wrong shape) returns [] so deep degrades to single-query retrieval."""
    ctx = ""
    recent = [(t.get("text") or "")[:200] for t in (history or [])[-4:] if t.get("role") == "user"]
    if recent:
        ctx = "Earlier questions, for context: %s\n\n" % "; ".join(recent)
    try:
        raw, err = caller(_PLAN_SYSTEM, ctx + "Question: %s" % question)
        if err:
            return []
        raw = (raw or "").strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```[a-z]*\s*|\s*```$", "", raw, flags=re.I)
        parsed = json.loads(raw, strict=False)
        qs = parsed.get("queries") if isinstance(parsed, dict) else None
        return [str(q).strip() for q in (qs or []) if str(q).strip()][:5]
    except Exception:
        return []


def _user_prompt(question, hits, history):
    lines = ["Context excerpts:"]
    used = 0
    for i, c in enumerate(hits, 1):
        body = c["text"]
        if used + len(body) > MAX_CONTEXT_CHARS:
            body = body[:max(0, MAX_CONTEXT_CHARS - used)]
        used += len(body)
        date = (" | " + c["date"]) if c.get("date") else ""
        lines.append("[%d] %s (%s%s)\n%s" % (i, c["title"], c["kind"], date, body))
        if used >= MAX_CONTEXT_CHARS:
            break
    if history:
        lines.append("\nRecent conversation:")
        for turn in history[-6:]:
            lines.append("%s: %s" % ("Team" if turn.get("role") == "user" else "Assistant",
                                     (turn.get("text") or "")[:600]))
    lines.append("\nQuestion: %s" % question)
    return "\n\n".join(lines)


def _scan_answer_string(raw):
    """Salvage the "answer" value out of a BROKEN JSON envelope, or "" if there is none.

    Walks the string literal character by character (honoring backslash escapes), so it survives
    garbage between the closing quote and the brace, a missing closing brace (output truncated at
    the token cap), and raw newlines inside the string. The collected literal is decoded with
    json.loads; if even that fails (e.g. truncated mid-escape) the common escapes are unescaped by
    hand — the goal is that the UI NEVER has to display a raw JSON blob."""
    m = re.search(r'"answer"\s*:\s*"', raw)
    if not m:
        return ""
    i, n, out = m.end(), len(raw), []
    while i < n:
        ch = raw[i]
        if ch == "\\" and i + 1 < n:
            out.append(raw[i:i + 2])
            i += 2
            continue
        if ch == '"':
            break
        out.append(ch)
        i += 1
    literal = "".join(out)
    try:
        return str(json.loads('"%s"' % literal, strict=False)).strip()
    except ValueError:
        _esc = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r",
                "t": "\t"}
        return re.sub(r'\\(["\\/bfnrt])', lambda mm: _esc[mm.group(1)], literal).strip()


def _parse_answer(raw):
    """The model answers {"answer": ...}; parse leniently, salvage nearly-JSON, fall back to raw.

    The providers run in JSON mode yet still occasionally emit an envelope json.loads rejects —
    stray characters after the answer string, raw newlines/tabs inside it, or output cut at the
    token cap. Salvage progressively: strict parse (strict=False allows raw control chars in
    strings) → parse ignoring trailing junk → hand-scan the "answer" string literal."""
    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-z]*\s*|\s*```$", "", raw, flags=re.I)
    try:
        parsed = json.loads(raw, strict=False)
        if isinstance(parsed, dict) and parsed.get("answer"):
            return str(parsed["answer"])
    except ValueError:
        pass
    start = raw.find("{")
    if start >= 0:
        try:
            parsed, _end = json.JSONDecoder(strict=False).raw_decode(raw, start)
            if isinstance(parsed, dict) and parsed.get("answer"):
                return str(parsed["answer"])
        except ValueError:
            pass
        salvaged = _scan_answer_string(raw)
        if salvaged:
            return salvaged
    return raw


def _retrieve(index, question, queries, depth, date_from, date_to, query_embedder, reranker):
    """The HYBRID retrieval pipeline. Returns the final list of chunk dicts (best first).

      1. metadata PRE-FILTER (kind scope + date range), relaxing the kind scope if it empties the set;
      2. per query: a BM25 ranking + (when embedded + a query vector) a cosine ranking;
      3. RRF-fuse every ranking into one candidate pool (rank-only -- BM25 and cosine scales can't
         fight), capped to the depth's pool size;
      4. optionally cross-encoder RE-RANK the pool (retrieve wide, keep few), else take the fused top.
    `query_embedder(q) -> (vec, err)` and `reranker(query, records, top_n) -> (records, err)` are
    injected; both are optional and every failure degrades to the lexical/fused result."""
    chunks = index.get("chunks") or []
    pool_cap, final = _DEPTH_RETRIEVE.get(depth, _DEPTH_RETRIEVE[DEFAULT_DEPTH])

    kinds = _infer_kinds(question)
    # A named creator makes the question cross-source: keep the watched-video chunks in scope so
    # "what would <creator> say about <campaign>" retrieves the creator's transcripts, not just the
    # campaign (a lone 'campaign' keyword otherwise filters every transcript out before scoring).
    if kinds is not None and "video" not in kinds and _question_names_creator(question, chunks):
        kinds = set(kinds) | {"video"}
    allowed = [i for i, c in enumerate(chunks) if _passes(c, date_from, date_to, kinds)]
    if not allowed and kinds is not None:                 # scope too tight -> drop the kind filter
        allowed = [i for i, c in enumerate(chunks) if _passes(c, date_from, date_to, None)]
    if not allowed:
        return []

    retr = _Retriever(index)
    hybrid = has_embeddings(index) and query_embedder is not None
    rankings = []
    for q in queries:
        rankings.append(retr.bm25(q, allowed)[:100])
        if hybrid:
            qvec, _verr = query_embedder(q)
            if qvec:
                rankings.append(retr.cosine(qvec, allowed)[:100])
    fused = _rrf(rankings, limit=pool_cap)
    if not fused:
        return []

    if reranker is not None and len(fused) > 1:
        records = [{"id": str(i), "title": chunks[i].get("title", ""),
                    "content": chunks[i].get("text", "")} for i in fused]
        ranked, _rerr = reranker(question, records, final)
        order = []
        for r in ranked:
            try:
                order.append(int(r["id"]))
            except (TypeError, ValueError, KeyError):
                pass
        fused = order or fused
    return _expand_hits(index, [chunks[i] for i in fused[:final]], question)


def ask(client_name, index, question, history=None, date_from="", date_to="", model=None,
        caller=None, usage_out=None, depth=DEFAULT_DEPTH, query_embedder=None, reranker=None,
        actions_catalog="", actions_out=None):
    """Answer `question` from the workspace index with HYBRID retrieval. Returns (answer, sources, error).

    `actions_catalog` (assistant_actions.catalog_text()) enables the propose-actions protocol;
    RAW proposals the model emitted are appended to the `actions_out` list (the caller validates
    them against the registry before showing approval cards -- nothing here executes anything).

    `depth` ('quick'|'standard'|'deep') is the admin's detail control: deep query-plans first (an
    extra model call), retrieves wider, turns provider thinking ON, and asks for a structured
    analysis; quick trims retrieval and asks for a few sentences. `sources` is the de-duplicated
    list of {title, kind, date, url} actually retrieved (shown as citation chips).
    `caller(system, user)` -> (text, error) is the LLM seam (used for BOTH the plan and the answer
    call in deep mode); the default uses the intel brain's provider plumbing with its default
    model. A `usage_out` dict is filled by the default caller with the SUMMED token counts of
    every call + the model id (the spend tally).
    `query_embedder(q) -> (vec, err)` adds the SEMANTIC leg (fused with BM25 via RRF) when the index
    was embedded; `reranker(query, records, top_n) -> (records, err)` adds a cross-encoder rerank of
    the candidate pool. Both are optional -- omit them (or leave the index unembedded) and this is
    exactly the old BM25 path. Neither ever raises: a failure degrades to lexical/fused retrieval."""
    depth = depth if depth in DEPTHS else DEFAULT_DEPTH
    if caller is None:
        import intel_ai
        mid = model or intel_ai.default_model()

        def _model_call(system, user, think):
            if not mid or not intel_ai.model_available(mid):
                return "", "no AI provider configured"
            u = {}
            text, err, _think = intel_ai._call(intel_ai.model_meta(mid), system, user,
                                               None, 8192, usage_out=u, think=think)
            if usage_out is not None:
                usage_out["model"] = mid
                for k in ("input_tokens", "output_tokens"):
                    usage_out[k] = usage_out.get(k, 0) + u.get(k, 0)
            return text, err

        def plan_call(system, user):
            return _model_call(system, user, False)   # planning is extraction — keep it fast

        def answer_call(system, user):
            return _model_call(system, user, depth == "deep")
    else:
        plan_call = answer_call = caller

    queries = [question]
    if depth == "deep":
        queries += [q for q in plan_queries(question, history or [], plan_call)
                    if q.lower() != question.lower()]

    hits = _retrieve(index, question, queries, depth, date_from, date_to, query_embedder, reranker)
    if not hits:
        return ("", [], "Nothing in this workspace matches that question — try rephrasing, or "
                        "fetch more data first.")
    sources, seen = [], set()
    for c in hits:
        key = c["title"]
        if key not in seen:
            seen.add(key)
            sources.append({"title": c["title"], "kind": c["kind"],
                            "date": c.get("date", ""), "url": c.get("url", "")})
    raw, err = answer_call(_system_prompt(client_name, depth) + _actions_note(actions_catalog),
                           _user_prompt(question, hits, history or []))
    if err:
        return "", sources, err
    answer, proposals = split_actions(_parse_answer(raw))
    if actions_out is not None:
        actions_out.extend(proposals)
    if not answer and not proposals:
        return "", sources, "The model returned an empty answer — try again."
    return answer, sources, ""


# --- Streaming ask + plan checkpoint --------------------------------------------------------------
# The chat UI streams: it shows the model's reasoning live and lets the team PAUSE mid-thinking to
# steer. `plan_stage` is the optional pre-answer checkpoint (Claude-style "plan mode"): it does the
# retrieval + (deep) query planning and returns what the assistant WILL look at, so the team can
# approve or redirect BEFORE a single answer token is written. `ask_stream` then streams the answer
# (reasoning first, then the reply), honouring any `steer` the team added at the checkpoint or a pause.
def _build_queries(question, depth, history, plan_caller):
    """The retrieval queries for `question`: just the question, plus (deep) the planned sub-queries."""
    queries = [question]
    if depth == "deep" and plan_caller is not None:
        queries += [q for q in plan_queries(question, history or [], plan_caller)
                    if q.lower() != question.lower()]
    return queries


def _sources_of(hits):
    """The de-duplicated citation list (by title) for a set of retrieved chunks."""
    sources, seen = [], set()
    for c in hits:
        key = c["title"]
        if key not in seen:
            seen.add(key)
            sources.append({"title": c["title"], "kind": c["kind"],
                            "date": c.get("date", ""), "url": c.get("url", "")})
    return sources


def plan_stage(index, question, history=None, depth=DEFAULT_DEPTH, date_from="", date_to="",
               query_embedder=None, reranker=None, plan_caller=None):
    """The plan-mode checkpoint. Returns (queries, sources): the sub-questions the assistant will
    search and the sources it retrieved -- shown to the team to approve or steer BEFORE answering.
    Never raises; an empty `sources` means nothing matched (the UI says so)."""
    depth = depth if depth in DEPTHS else DEFAULT_DEPTH
    queries = _build_queries(question, depth, history, plan_caller)
    hits = _retrieve(index, question, queries, depth, date_from, date_to, query_embedder, reranker)
    return queries, _sources_of(hits)


def ask_stream(client_name, index, question, history=None, date_from="", date_to="",
               depth=DEFAULT_DEPTH, steer="", query_embedder=None, reranker=None,
               plan_caller=None, stream_caller=None, actions_catalog=""):
    """Stream an answer to `question`. A GENERATOR yielding event dicts (mirrors intel_ai.stream_call):
      {"type":"sources","sources":[...]}         -- retrieved citations (emitted first)
      {"type":"thinking","text":<delta>}         -- reasoning delta (the live think panel)
      {"type":"answer","text":<delta>}           -- answer delta (plain markdown)
      {"type":"proposals","proposals":[...]}     -- RAW action proposals (only with actions_catalog;
                                                    the caller validates before showing approvals)
      {"type":"usage", ...}                       -- token counts (from the provider, near the end)
      {"type":"error","message":<reason>}        -- a short reason; the stream then ends

    `steer` (from the plan checkpoint or a pause-and-restart) is injected so the answer follows the
    team's redirection. `stream_caller(system, user) -> iterator of intel_ai stream events` is the
    injected model seam (tests pass a fake); `plan_caller(system,user)->(text,err)` powers deep's
    query planning. Retrieval reuses the hybrid pipeline (query_embedder/reranker optional).
    With `actions_catalog` set, the answer stream is filtered so the proposal block never renders
    as text -- it arrives once, parsed, as the `proposals` event."""
    depth = depth if depth in DEPTHS else DEFAULT_DEPTH
    queries = _build_queries(question, depth, history, plan_caller)
    hits = _retrieve(index, question, queries, depth, date_from, date_to, query_embedder, reranker)
    if not hits:
        yield {"type": "error", "message": ("Nothing in this workspace matches that question — try "
                                            "rephrasing, or fetch more data first.")}
        return
    yield {"type": "sources", "sources": _sources_of(hits)}
    if stream_caller is None:
        yield {"type": "error", "message": "no streaming model configured"}
        return
    system = _system_prompt(client_name, depth, as_json=False) + _actions_note(actions_catalog)
    user = _user_prompt(question, hits, history or []) + _steer_note(steer)
    filt = _ActionTail() if actions_catalog else None
    got_answer = False
    for ev in stream_caller(system, user):
        if not isinstance(ev, dict):
            continue
        if ev.get("type") == "answer":
            if ev.get("text"):
                got_answer = True
            if filt is not None:
                visible = filt.feed(ev.get("text") or "")
                if visible:
                    yield {"type": "answer", "text": visible}
                continue
        yield ev
    if filt is not None:
        rest, proposals = filt.finish()
        if rest:
            yield {"type": "answer", "text": rest}
        if proposals:
            yield {"type": "proposals", "proposals": proposals}
    if not got_answer:
        yield {"type": "error", "message": "The model returned an empty answer — try again."}


# --- Optional source: the client's dashboard data export ------------------------------------------
# A Cloud Run host is either the classic `<service>-<projecthash>-<regioncode>.a.run.app` or the
# newer `<service>-<projectnumber>.<region>.run.app`. Both carry the service name in the first label.
_RUN_HOST_OLD = re.compile(r"^(?P<svc>.+?)-[a-z0-9]{8,}-[a-z]{2,3}$")
_RUN_HOST_NEW = re.compile(r"^(?P<svc>.+?)-\d{6,}$")


def dash_data_key(client, dashboard_url=""):
    """Which dashboard stack's KPI export belongs to this workspace.

    🔴 The portal's client key and the dashboard stack's key DIVERGED in production. The console
    derives a workspace key from the display name ("Riverdance RV" -> `riverdance-rv`), while the
    dashboard stack was stood up years earlier under a short key (`riverdance`). So
    `agora-data-driven-<client>-dash` did not exist for a SINGLE client, `read_client_dash_data`
    swallowed the 404, and the KPI export silently vanished from both the Assistant's index and the
    report deck's fact pack -- which is why a generated deck came out three slides long.

    The workspace already knows the answer: `dashboard_url` is the embed the Dashboard tab renders,
    and it points at the real stack. Read the key from there; fall back to the client key (which is
    correct whenever the two were never allowed to diverge)."""
    raw = (dashboard_url or "").strip()
    if not raw:
        return client
    if "//" not in raw:
        raw = "https://" + raw
    try:
        host = urllib.parse.urlsplit(raw).hostname or ""
    except ValueError:
        return client
    # A hostname, or nothing: free text ("TBC", a note someone typed in the field) must fall back
    # to the client key, never become a bucket name.
    if "." not in host or not re.match(r"^[a-z0-9][a-z0-9.-]*$", host):
        return client
    label = host.split(".")[0]
    if host.endswith(".run.app"):
        for rx in (_RUN_HOST_OLD, _RUN_HOST_NEW):
            m = rx.match(label)
            if m:
                label = m.group("svc")
                break
        if label.endswith("-dash"):
            label = label[:-len("-dash")]
        return label or client
    if label and label not in ("www", "portal"):     # a custom domain: <c>.agoradatadriven.com
        return label
    return client


def read_client_dash_data(client, dashboard_url=""):
    """The per-client dashboard JSON (`<k>.json` in agora-data-driven-<k>-dash), or None.

    `k` is `dash_data_key(client, dashboard_url)` -- the workspace key is NOT reliably the dashboard
    key (see above). Opt-in: the portal SA needs objectViewer on that bucket
    (enable_assistant_dash_data.ps1). Any failure -- no bucket (portal-only client), no permission,
    bad JSON -- returns None."""
    key = dash_data_key(client, dashboard_url)
    try:
        from google.cloud import storage  # lazy
        blob = storage.Client().bucket("agora-data-driven-%s-dash" % key).blob("%s.json" % key)
        return json.loads(blob.download_as_bytes().decode("utf-8"))
    except Exception:
        return None
