"""Daily Market Intelligence refresh -- an AI brain curates REAL news into every client's intel tab.

Runs as a Cloud Run JOB (`intel-refresh`) on a daily Cloud Scheduler tick, REUSING the platform-dash
image + runtime SA. No new service/bucket/SA: it writes the SAME `workspace/<c>.json` objects the
app already does, and it reads public RSS over HTTPS (intel_feed) -- keyless. The ONLY added infra is
the two provider API keys (GEMINI_API_KEY / DEEPSEEK_API_KEY), mounted from Secret Manager, and even
those are optional (see the fallback below).

RESEARCH METHOD = RETRIEVE-THEN-CURATE (see intel_ai.py). For each client and each of the two
sections we:
  1. RETRIEVE a pool of REAL candidate articles from keyless Google News RSS + publisher feeds
     (intel_feed). Media Buying News is universal (ad-platform queries + Search Engine Land PPC);
     Business Research is per-client, keyed off the client's own `intel_topics` (falling back to a
     generic marketing set). The FIRST run for a client pulls a 12-MONTH window (backfill); every
     run after pulls just the last few days.
  2. CURATE with the client's selected model (intel_ai.curate) -- it picks the most relevant items,
     writes a client-facing 1-2 sentence summary, and keeps the REAL link/source/date. The admin's
     tunable per-section prompt steers what to pick.
  3. REPLACE only the section's AUTO entries (workspace.replace_auto_intel), so hand-added / pinned
     entries are preserved.

Gated + graceful, like feedback_ai: the job is a logged no-op unless INTEL_AUTO_ENABLED=1. If a
client has no model selected, or no provider key is configured, or the model call fails, that
section FALLS BACK to the plain-RSS fill (the previous behaviour) -- the tab always fills. A dead
feed / a client with no workspace is logged and skipped, never fatal. Off-cloud testable via
WORKSPACE_LOCAL_DIR + REGISTRY_LOCAL_DIR; `refresh_client` takes injectable `fetcher` (RSS) and
`ai_fetcher` (LLM) seams so the whole pipeline runs with no network in tests.
"""

import os
import sys
from concurrent.futures import ThreadPoolExecutor

import intel_ai
import intel_feed
import store
import workspace

# --- What each section pulls --------------------------------------------------------------------
# Media Buying News is universal -- the same ad-platform updates matter to every client. Use
# publisher feeds that actually resolve from a datacenter IP (Cloud Run): ppc.land is PPC-specific
# and high-volume, Search Engine Journal covers paid heavily. (Search Engine Land's category feed
# 000s from Cloud Run, so it was dropped -- these give the AI a reliable candidate pool even when
# Google News rate-limits the ad-platform queries below.)
MEDIA_BUYING_FEEDS = (
    "https://ppc.land/rss/",
    "https://www.searchenginejournal.com/feed/",
)
MEDIA_BUYING_QUERIES = (
    "Google Ads update",
    "Meta Ads Manager update",
    "TikTok advertising",
    "LinkedIn Ads update",
)

# Business Research fixed publisher feeds (keyless RSS) -- the universal floor. The per-client
# QUERIES come from the client's own intel_topics (workspace.get_intel_topics); when a client has no
# topics set, this generic marketing set is used so the section still fills.
BUSINESS_RESEARCH_FEEDS = (
    "https://www.marketingdive.com/feeds/news/",
    "https://www.searchenginejournal.com/feed/",
)
BUSINESS_RESEARCH_FALLBACK_QUERIES = (
    "digital marketing industry trends",
    "advertising industry news",
    "consumer marketing trends",
)

# The look-back window and article target are ADMIN-CONFIGURED per client (intel_ai.window_of /
# count_of, set in the AI Research Brain panel). We hand the model a candidate pool a few times the
# requested count so it has real choice, floored so a small count still gets a decent pool.
_MIN_CANDIDATE_POOL = 30

# Heading/source defaults that make an auto entry read like the hand-written ones.
_BUSINESS_HEADING = "Industry News"
_MEDIA_HEADING = "Platform Update"
_BODY_MAX = 280


def _enabled():
    """True iff the daily auto-refresh is switched on. Fail-closed (default OFF), like feedback_ai."""
    return os.environ.get("INTEL_AUTO_ENABLED", "") in ("1", "true", "True")


def _dedupe(rows):
    """Drop entries that repeat a title or link (feeds + queries overlap), preserving order."""
    out, seen = [], set()
    for r in rows:
        key = (r.get("title") or "").strip().lower() or (r.get("link") or "").strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(r)
    return out


def _gather(feeds, queries, limit, window=None, fetcher=None):
    """Fetch every feed + query IN PARALLEL, interleave round-robin, dedupe, return up to `limit` rows.

    Rows carry {title, link, body, source, date} -- the REAL articles the AI curates from (its input;
    the model keeps each chosen article's real link/source/date).

    Two deliberate choices here fix the "irrelevant research" problem:
      * PARALLEL fetch -- a slow or dead feed no longer blocks the others (each still self-limits via
        intel_feed's per-request timeout and can never raise), so retrieval is bounded by the SLOWEST
        source, not their sum.
      * ROUND-ROBIN interleave instead of a global newest-first sort. A date-sort let one high-volume
        source (e.g. Search Engine Journal, which posts ~20x/day) flood the pool and crowd the
        client's own keyword hits out of the top `limit` entirely -- so the model never even saw
        them. Interleaving takes each source's freshest item first, guaranteeing every keyword/feed
        is represented before the model ranks by relevance."""
    urls = [u for u in list(feeds) if u]
    urls += [u for u in (intel_feed.google_news_url(q, window=window) for q in queries) if u]
    if not urls:
        return []
    per_source = [[] for _ in urls]

    def _fetch(idx):
        return idx, [r for r in intel_feed.fetch_feed(urls[idx], limit=limit, fetcher=fetcher)
                     if r.get("title")]

    with ThreadPoolExecutor(max_workers=min(8, len(urls))) as ex:
        for idx, rows in ex.map(_fetch, range(len(urls))):
            per_source[idx] = rows

    # Round-robin: source0[0], source1[0], ..., source0[1], source1[1], ... -- each source's top
    # item outranks any source's second item, so no single feed can monopolise the candidate list.
    interleaved = []
    depth = max((len(s) for s in per_source), default=0)
    for d in range(depth):
        for s in per_source:
            if d < len(s):
                interleaved.append(s[d])
    return _dedupe(interleaved)[:limit]


def _curate_section(client, ws, section, feeds, queries, heading, count, window,
                    model, prompt, ai_fetcher, fetcher):
    """RETRIEVE real candidates + CURATE them with the model. Returns (entries, err) -- NO write.

    RETRIEVES candidate articles (the AI's input) from RSS over the admin's look-back `window`, then
    CURATES with the selected model. It does NOT touch the workspace: the caller writes the results,
    so the two sections can curate CONCURRENTLY without racing the read-modify-write workspace JSON.
    There is NO news-feed fallback: on failure it returns (None, <short reason>)."""
    pool = max(_MIN_CANDIDATE_POOL, count * 3)
    candidates = _gather(feeds, queries, pool, window=window, fetcher=fetcher)
    if not candidates:
        return None, "no source articles found"
    entries, err = intel_ai.curate(
        section,
        ws.get("display_name") or client,
        workspace.get_intel_topics(ws),
        candidates,
        prompt=prompt,
        model=model,
        limit=count,
        heading_default=heading,
        fetcher=ai_fetcher,
    )
    if entries:
        return entries, ""
    return None, err or "the model returned nothing"


def refresh_client(client, ws=None, fetcher=None, ai_fetcher=None):
    """Curate fresh news into both sections for one client and ADD it to the existing lists.

    `ws` may be passed to avoid a reload; `fetcher` is the intel_feed (RSS) seam and `ai_fetcher` is
    the intel_ai (LLM transport) seam -- both for tests. Returns zeros if the client has no workspace
    OR no model selected (nothing runs without a brain -- there is no news-feed fallback). The
    look-back window and the article target are admin-configured (intel_ai.window_of / count_of);
    each run ADDS new, de-duped stories, so history accumulates over time."""
    if ws is None:
        ws = workspace.load_workspace(client)
    if ws is None:
        return {"media_buying": 0, "business_research": 0, "ai": False}

    cfg = workspace.get_intel_ai(ws)
    model = cfg.get("model") or ""
    if not model:
        workspace.mark_intel_run(client, "", error="No AI model selected — pick one in AI Research Brain.")
        return {"media_buying": 0, "business_research": 0, "ai": False}
    if not intel_ai.model_available(model):
        workspace.mark_intel_run(client, "", error="%s isn't available on the server (check its API access)." % model)
        return {"media_buying": 0, "business_research": 0, "ai": False}

    window = intel_ai.window_of(cfg)
    count = intel_ai.count_of(cfg)

    # Business Research is keyed on the CLIENT's OWN industry. When the client has set keywords,
    # curate ONLY from their keyword searches -- the generic marketing-industry feeds (Marketing
    # Dive, Search Engine Journal) are about the AGENCY's world, not the client's, and mixing them
    # in flooded the pool with off-topic SEO/AI stories. They remain the floor ONLY when a client
    # has no keywords yet, so the section still fills.
    topics = workspace.get_intel_topics(ws)
    biz_queries = tuple(topics) if topics else BUSINESS_RESEARCH_FALLBACK_QUERIES
    biz_feeds = () if topics else BUSINESS_RESEARCH_FEEDS

    specs = (
        ("media_buying", MEDIA_BUYING_FEEDS, MEDIA_BUYING_QUERIES, _MEDIA_HEADING, cfg.get("media_prompt")),
        ("business_research", biz_feeds, biz_queries, _BUSINESS_HEADING, cfg.get("business_prompt")),
    )

    # RETRIEVE + CURATE both sections CONCURRENTLY (the slow part -- two LLM calls that used to run
    # back-to-back now overlap, roughly halving wall time). The WRITES happen afterwards in THIS
    # thread, one section at a time, because the workspace JSON is a read-modify-write and two
    # concurrent writers would clobber each other (last-write-wins).
    try:
        with ThreadPoolExecutor(max_workers=len(specs)) as ex:
            futures = {
                sec: ex.submit(_curate_section, client, ws, sec, feeds, queries, heading,
                               count, window, model, prompt, ai_fetcher, fetcher)
                for (sec, feeds, queries, heading, prompt) in specs
            }
            results = {sec: fut.result() for sec, fut in futures.items()}
    except Exception as exc:
        workspace.mark_intel_run(client, model, error=str(exc)[:200])
        raise

    counts, errs, used_ai = {}, [], False
    for sec in ("media_buying", "business_research"):
        entries, err = results.get(sec, (None, "did not run"))
        if entries:
            workspace.add_auto_intel(client, sec, entries)
            counts[sec] = len(entries)
            used_ai = True
        else:
            counts[sec] = 0
            if err:
                errs.append(err)

    err = "; ".join(dict.fromkeys(errs))        # surface each distinct reason a section couldn't fill
    workspace.mark_intel_run(client, model if used_ai else "", error=err)
    return {"media_buying": counts["media_buying"],
            "business_research": counts["business_research"], "ai": used_ai}


def refresh_all(fetcher=None, ai_fetcher=None):
    """Refresh every registered client (skipping the worked-example `template`). Returns a summary."""
    summary = {}
    for c in store.list_clients():
        key = c.get("key")
        if not key or key == "template":
            continue
        try:
            counts = refresh_client(key, fetcher=fetcher, ai_fetcher=ai_fetcher)
        except Exception as exc:  # one bad client must not sink the whole run
            print("[intel-refresh] %s FAILED: %s" % (key, exc), file=sys.stderr)
            continue
        summary[key] = counts
        print("[intel-refresh] %s -> media_buying=%d business_research=%d ai=%s"
              % (key, counts["media_buying"], counts["business_research"], counts["ai"]))
    return summary


def main():
    """Job entry point. No-op (logs why) unless INTEL_AUTO_ENABLED=1."""
    if not _enabled():
        print("[intel-refresh] disabled (set INTEL_AUTO_ENABLED=1 to run); nothing to do.")
        return
    brain = intel_ai.default_model() or "(none configured -> news-feed fallback)"
    print("[intel-refresh] starting daily refresh (brain available: %s)" % brain)
    summary = refresh_all()
    print("[intel-refresh] done -- %d client(s) refreshed" % len(summary))


if __name__ == "__main__":
    main()
