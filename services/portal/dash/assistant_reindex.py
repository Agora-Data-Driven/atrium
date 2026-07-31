"""Assistant index rebuild -- the FULL, unbounded rebuild, off the request path.

Runs as a Cloud Run JOB (`assistant-reindex`), REUSING the platform-dash image + runtime SA
(mirrors intel_refresh.py / mail_refresh.py exactly). No new service/bucket/SA: it writes the SAME
`workspace/assistant/<c>/index.json` object the app's Rebuild button writes, through the SAME
assistant_ai helpers, so the stored shape is identical whichever path produced it.

WHY THIS EXISTS (2026-07-31). A full rebuild re-chunks every source and re-embeds the whole corpus.
On a big workspace (30 MiB of Watcher transcripts, ~6.8k chunks) that measured ~344s of Vertex
calls and a peak that OOM-killed a 512 MiB container. Two properties turned that from "slow" into
"permanently dead":

  * the index is written LAST, so a rebuild that died persisted NOTHING; and
  * the reuse check requires `stored v == INDEX_VERSION`, so after a version bump every single ask
    re-attempted the identical doomed rebuild. Chat never recovered on its own.

So the ask path no longer does this work at all: main._assistant_index keeps answering from the
index it already has and flags the client (workspace.queue_assistant_reindex). This job is what
actually repairs it -- uncapped, with a job's timeout instead of a request's.

Selection: by default every client FLAGGED pending by the ask path -- a cheap check, since the flag
rides in the workspace JSON the run already loads, which is what makes the frequent scheduled tick
nearly free. `--sweep` / REINDEX_SWEEP=1 additionally opens each stored index and picks up any that
is missing, below INDEX_VERSION, or only partially embedded (🔴 that downloads every index -- tens
of MB per client -- so it is the post-version-bump sweep, never the tick). `--all` / REINDEX_ALL=1
forces every client; `--client <key>` / REINDEX_CLIENT does exactly one (what the Rebuild button
triggers).

One bad client never sinks the run. Off-cloud testable via WORKSPACE_LOCAL_DIR + REGISTRY_LOCAL_DIR
(see _assistant_localtest.py); `reindex_client` takes the same injectable seams the app passes, so
the pipeline runs with no network in tests.
"""

import os
import sys
import time

import assistant_ai
import intel_ai
import store
import workspace


def _enabled():
    """True iff the job is switched on. Fail-closed (default OFF), like intel_refresh/mail_refresh."""
    return os.environ.get("ASSISTANT_REINDEX_ENABLED", "") in ("1", "true", "True")


def _embedder():
    """The chunk embedder (RETRIEVAL_DOCUMENT), or None when embeddings aren't configured.
    Same seam main._assistant_embedder wires -- same SA token, GCP-billed, no API key."""
    if not intel_ai.embeddings_configured():
        return None
    return lambda texts: intel_ai.embed_texts(texts, task_type="RETRIEVAL_DOCUMENT")


def _caller(ws):
    """A (system, user) -> (text, err) model seam for the per-video summaries, or None."""
    mid = ((ws.get("assistant") or {}).get("model") or "").strip() \
        or ((ws.get("intel_ai") or {}).get("model") or "").strip() \
        or intel_ai.default_model()
    meta = intel_ai.model_meta(mid)
    if not meta or not intel_ai.model_available(mid):
        return None

    def caller(system, user):
        text, err, _think = intel_ai._call(meta, system, user, None, 8192, think=False)
        return text, err
    return caller


def needs_reindex(client, ws, inspect=False):
    """(bool, reason) -- whether this client is due for a full rebuild.

    The PENDING FLAG is the cheap check and the one the scheduled run uses: it lives in the
    workspace JSON we already loaded, and the ask path is what sets it. 🔴 `inspect` (the --sweep
    path) additionally opens the stored index to check its version -- that DOWNLOADS the whole
    object, which is 34 MiB for a big client, so it must never run on the frequent tick."""
    if workspace.assistant_reindex_pending(ws):
        q = ((ws.get("assistant") or {}).get("reindex") or {})
        return True, (q.get("reason") or "queued by the ask path")
    if not inspect:
        return False, ""
    index = workspace.read_assistant_index(client)
    if index is None:
        return True, "no index built yet"
    if index.get("v") != assistant_ai.INDEX_VERSION:
        return True, "index v%s, code v%s" % (index.get("v"), assistant_ai.INDEX_VERSION)
    if index.get("emb_partial"):
        return True, "%s chunk(s) never embedded" % index["emb_partial"]
    return False, ""


def reindex_client(client, ws=None, embedder=None, caller=None, summarize=True):
    """Rebuild ONE client's index completely and persist it. Returns a summary dict.

    Uncapped on purpose (`max_new=0`): this is a job, not a request. Mirrors the app's own rebuild
    -- including the cached per-video AI summaries, which are deliberately never done on the ask
    path so chat latency stays flat."""
    ws = workspace.load_workspace(client) if ws is None else ws
    if ws is None:
        return {"client": client, "ok": False, "error": "no workspace"}
    t0 = time.time()
    embedder = _embedder() if embedder is None else embedder

    archives, mail, dash_data, stamp = assistant_ai.gather_sources(client, ws)

    # Fill in any missing per-video summaries first: they ADD chunks, so doing it before the
    # fingerprint keeps the written index consistent with what the next ask will compute.
    summarized = 0
    if summarize:
        c = _caller(ws) if caller is None else caller
        if c is not None:
            updates, summarized, _err = assistant_ai.summarize_videos(archives, c)
            for ch_id, videos in updates.items():
                workspace.write_watcher_videos(client, ch_id, videos)
            if summarized:
                # The archives were mutated in place, so re-read nothing -- but the registry counts
                # that feed the fingerprint changed, so reload the workspace before fingerprinting.
                ws = workspace.load_workspace(client) or ws
                archives, mail, dash_data, stamp = assistant_ai.gather_sources(client, ws)

    fp = assistant_ai.fingerprint(ws, archives, dash_stamp=stamp)
    chunks = assistant_ai.build_chunks(ws, archives, dash_data=dash_data, mail_threads=mail)
    index = assistant_ai.build_index(chunks, fp=fp)
    if embedder is not None:
        prev = workspace.read_assistant_index(client)
        carried = assistant_ai.carry_over(prev)
        prev = None      # release the old index before the new one is serialized (see carry_over)
        assistant_ai.embed_index(index, embedder, prev=carried, max_new=0)
    workspace.write_assistant_index(client, index)
    workspace.clear_assistant_reindex(client, built_at=index.get("built_at", ""))
    return {"client": client, "ok": True, "chunks": len(chunks),
            "embedded": index.get("emb_count", 0), "summarized": summarized,
            "seconds": round(time.time() - t0, 1)}


def reindex_all(force_all=False, inspect=False, only=""):
    """Rebuild every SELECTED client. Returns a list of per-client summaries.

    Selection: `only` -> that one client unconditionally; `force_all` (--all) -> every client
    unconditionally; otherwise every client `needs_reindex` reports as due (with `inspect` the
    check also opens each stored index -- see needs_reindex)."""
    out = []
    for c in store.list_clients():
        key = c.get("key")
        if not key or key == "template":
            continue
        if only and key != only:
            continue
        ws = workspace.load_workspace(key)
        if ws is None:
            continue
        due, reason = (True, "forced") if (only or force_all) \
            else needs_reindex(key, ws, inspect=inspect)
        if not due:
            continue
        print("[assistant-reindex] %s -- %s" % (key, reason or "forced"))
        try:
            res = reindex_client(key, ws=ws)
        except Exception as exc:            # one bad client must not sink the whole run
            print("[assistant-reindex] %s FAILED: %s" % (key, exc), file=sys.stderr)
            out.append({"client": key, "ok": False, "error": str(exc)[:200]})
            continue
        out.append(res)
        if res.get("ok"):
            print("[assistant-reindex] %s -> %d chunks, %d embedded, %d summarized in %ss"
                  % (key, res["chunks"], res["embedded"], res["summarized"], res["seconds"]))
    return out


def main(argv=None):
    """Job entry point. No-op (logs why) unless ASSISTANT_REINDEX_ENABLED=1."""
    argv = sys.argv[1:] if argv is None else argv
    force_all = "--all" in argv or os.environ.get("REINDEX_ALL", "") in ("1", "true", "True")
    inspect = "--sweep" in argv or os.environ.get("REINDEX_SWEEP", "") in ("1", "true", "True")
    only = ""
    if "--client" in argv:
        i = argv.index("--client")
        only = argv[i + 1] if i + 1 < len(argv) else ""
    only = only or os.environ.get("REINDEX_CLIENT", "")

    if not _enabled():
        print("[assistant-reindex] disabled (set ASSISTANT_REINDEX_ENABLED=1); nothing to do.")
        return
    print("[assistant-reindex] starting (all=%s, sweep=%s, client=%s, embeddings=%s)"
          % (force_all, inspect, only or "(queued)", intel_ai.embeddings_configured()))
    results = reindex_all(force_all=force_all, inspect=inspect, only=only)
    ok = [r for r in results if r.get("ok")]
    print("[assistant-reindex] done -- %d rebuilt, %d failed"
          % (len(ok), len(results) - len(ok)))


if __name__ == "__main__":
    main()
