"""Off-cloud test for the Atrium Assistant (no GCS, no network, no LLM).

Covers the RAG pieces end-to-end: chunking every workspace source, the BM25 index + date-filtered
retrieval, lenient answer parsing, the ask() seam, the Flask route (lazy index rebuild + ok/error
paths), and the team-only gating.

Run: python _assistant_localtest.py        # prints PASS / FAIL, exits 0 / 1
"""

import json
import os
import shutil
import sys
import tempfile
import types

# Stub google.cloud.storage BEFORE importing main (store/feedback construct a client at import).
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

_TMP = tempfile.mkdtemp(prefix="atrium_assistant_")
os.environ["WORKSPACE_LOCAL_DIR"] = _TMP
os.environ["REGISTRY_LOCAL_DIR"] = _TMP
os.environ["SESSION_SECRET"] = "test-secret"

import assistant_ai     # noqa: E402
import seed_workspace   # noqa: E402
import workspace        # noqa: E402
import main             # noqa: E402

CLIENT = "riverdance"
SUPER = {"ok": True, "user": "info@agoradatadriven.com", "clients": ["*"]}
CLIENT_LOGIN = {"ok": True, "user": "owner@riverdanceresort.com", "clients": [CLIENT]}


def _check(label, cond):
    if not cond:
        raise AssertionError(label)
    print("  [OK] %s" % label)


def _fake_archives():
    ch = {"id": "wch_test01", "title": "Carson Reed", "transcript_count": 1,
          "last_fetch": "2026-07-12T00:00:00Z"}
    videos = [
        {"id": "v1", "title": "Pricing AI retainers", "url": "https://youtu.be/v1",
         "published": "2026-07-01",
         "transcript": ("charge monthly retainers for AI receptionists " * 80).strip()},
        {"id": "v2", "title": "Old cold outreach video", "url": "https://youtu.be/v2",
         "published": "2024-01-15",
         "transcript": "cold outreach emails work best when personalized to the prospect."},
    ]
    return [(ch, videos)]


def run():
    seed_workspace.seed(register_client=False)
    ws = workspace.load_workspace(CLIENT)
    archives = _fake_archives()

    # --- Chunking: every source lands, long transcripts split ------------------------------------
    chunks = assistant_ai.build_chunks(ws, archives,
                                       dash_data={"kpis": {"leads": 42}, "daily": [{"d": 1}]})
    kinds = {c["kind"] for c in chunks}
    _check("chunks cover videos, intel, campaigns, content, metrics, dashboard",
           {"video", "intel", "campaign", "content", "metrics", "dashboard"} <= kinds)
    _check("long transcript split into word chunks",
           sum(1 for c in chunks if c["id"].startswith("yt:wch_test01:v1")) >= 1
           and all(len(c["text"].split()) <= assistant_ai.CHUNK_WORDS for c in chunks
                   if c["kind"] == "video"))

    # --- Fingerprint: changes when a source changes -----------------------------------------------
    fp1 = assistant_ai.fingerprint(ws, archives)
    archives[0][0]["transcript_count"] = 2
    fp2 = assistant_ai.fingerprint(ws, archives)
    _check("fingerprint moves when watcher data moves", fp1 != fp2)

    # --- The dashboard-export key: the workspace key is NOT the dashboard key ---------------------
    # 🔴 In production the two diverged for EVERY client: the console derives "riverdance-rv" from
    # the display name while the dashboard stack is "riverdance", so agora-data-driven-<c>-dash did
    # not exist, read_client_dash_data swallowed the 404, and the KPI export silently vanished from
    # the Assistant index AND the report deck (which came out 3 slides long).
    for url, expect in (
            ("https://riverdance-dash-c732u7m57a-as.a.run.app/", "riverdance"),
            ("https://tcs-dash-123456789012.asia-southeast1.run.app", "tcs"),
            ("https://riverdance.agoradatadriven.com", "riverdance"),
            ("", "riverdance-rv"),
            ("TBC", "riverdance-rv"),
            ("not a url at all", "riverdance-rv")):
        got = assistant_ai.dash_data_key("riverdance-rv", url)
        _check("dash_data_key(%r) -> %s" % (url or "(empty)", expect), got == expect)

    # --- The distilled layer (v4): dashboard digests, comms/tasks/reports chunks, summary chunks --
    ws2 = workspace.load_workspace(CLIENT)
    ws2["communications"] = [
        {"id": "cm1", "channel": "meeting", "audience": "client", "title": "July strategy call",
         "summary": "Agreed to double down on retargeting.", "date": "2026-07-10",
         "people": "Ian, Jade", "origin": "manual", "thread_key": ""},
        {"id": "mail_k1", "channel": "email", "audience": "client", "title": "Budget approval",
         "summary": "Client approved the August budget.", "date": "2026-07-12", "people": "",
         "origin": "mail", "thread_key": "k1"},
    ]
    ws2["tasks"] = [{"id": "tk_test1", "title": "Launch retargeting", "stage": "blocked",
                     "department": "Acquisition", "priority": "High", "due_date": "2026-08-01",
                     "hold_reason": "waiting on creative approval", "maintasks": [],
                     "comments": []}]
    ws2["reports"] = [{"id": "rp_t1", "title": "July review", "date": "2026-07-15",
                       "payload": {"why": ["CTR fell as frequency rose"]},
                       "created_at": "x", "updated_at": "x"}]
    arch2 = _fake_archives()
    arch2[0][1][0]["summary"] = "- Charge monthly retainers\nTakeaway: retainers beat one-off fees."
    mail_threads = [{"id": "k1", "subject": "Budget approval", "participants": ["client@x.com"],
                     "last_date": "2026-07-12",
                     "messages": [{"from": "client@x.com", "date": "2026-07-12",
                                   "body": "Approved, go ahead with the August budget increase."}],
                     "stats": {"awaiting_reply": False}}]
    # The Windsor-live dashboard shape (riverdance): per-ad/day rows -- the raw-CSV case the digest
    # layer exists for. Two campaigns, two ads, demographics + an email block.
    rows = [{"date": d, "ad": "V_Escape", "camp": "Summer", "spend": 100.0, "imps": 10000,
             "clicks": 150, "lclk": 120, "reach": 8000, "pur": 3, "rev": 900.0, "book_init": 5}
            for d in ("2026-07-01", "2026-07-02", "2026-07-08")]
    rows.append({"date": "2026-07-02", "ad": "S_Static", "camp": "Brand", "spend": 50.0,
                 "imps": 9000, "clicks": 30, "lclk": 20, "reach": 6000, "pur": 0, "rev": 0.0,
                 "book_init": 0})
    dash_rd = {"rows": rows, "dates": ["2026-07-01", "2026-07-02", "2026-07-08"],
               "demographics": {"age_gender": [{"date": "2026-07-01", "age": "25-34",
                                                "gender": "female", "spend": 60.0, "imps": 5000,
                                                "clicks": 80, "lclk": 60}],
                                "region": [{"date": "2026-07-01", "region": "Colorado",
                                            "spend": 90.0, "imps": 7000, "clicks": 100,
                                            "lclk": 80}]},
               "activecampaign": {"enabled": True, "campaigns": [
                   {"name": "July newsletter", "date": "2026-07-05", "sent": 1000, "opens": 400,
                    "clicks": 50}]}}
    chunks2 = assistant_ai.build_chunks(ws2, arch2, dash_data=dash_rd, mail_threads=mail_threads)
    ids2 = {c["id"] for c in chunks2}
    _check("distilled dashboard sections replace the raw JSON dump",
           {"dash:overview", "dash:campaigns", "dash:creatives", "dash:trend", "dash:audience",
            "dash:email"} <= ids2 and "dash:kpis" not in ids2 and "dash:daily" not in ids2)
    dash_txt = next(c["text"] for c in chunks2 if c["id"] == "dash:overview")
    _check("dashboard overview carries computed totals, not row dumps",
           "$350.00" in dash_txt and "ROAS" in dash_txt and '"spend"' not in dash_txt)
    _check("communications cards + snapshot are indexed",
           "comm:cm1" in ids2 and "comms:snapshot" in ids2)
    _check("an email card points at its thread archive (the unfold parent)",
           next(c for c in chunks2 if c["id"] == "comm:mail_k1").get("parent") == "mail:k1")
    _check("tasks are indexed with their id + a board snapshot",
           "task:tk_test1" in ids2 and "tasks:board" in ids2
           and "tk_test1" in next(c["text"] for c in chunks2 if c["id"] == "task:tk_test1"))
    _check("task chunks are undated (a transcript date-scope never hides the board)",
           not next(c for c in chunks2 if c["id"] == "task:tk_test1").get("date"))
    _check("reports are indexed",
           "report:rp_t1" in ids2
           and "CTR fell" in next(c["text"] for c in chunks2 if c["id"] == "report:rp_t1"))
    _check("a summarized video gains a digest chunk with a parent pointer",
           any(c["id"] == "yts:wch_test01:v1" and c.get("level") == "digest"
               and c.get("parent") == "yt:wch_test01:v1" for c in chunks2))
    _check("mail body chunks carry their thread parent (full level)",
           any(c.get("parent") == "mail:k1" and c.get("level") == "full" for c in chunks2))
    _check("the intel digest chunk exists", "intel:digest" in ids2)
    fpa = assistant_ai.fingerprint(ws2, arch2)
    ws2["communications"].append({"id": "cm9", "channel": "call", "audience": "client",
                                  "title": "Quick call", "summary": "notes",
                                  "date": "2026-07-14"})
    _check("fingerprint moves when communications move",
           fpa != assistant_ai.fingerprint(ws2, arch2))
    _check("fingerprint moves when the dashboard export moves (the stamp)",
           assistant_ai.fingerprint(ws2, arch2, dash_stamp="s1")
           != assistant_ai.fingerprint(ws2, arch2, dash_stamp="s2"))

    # --- Small-to-big expansion: a strong digest hit unfolds its best FULL sibling ---------------
    schunks = [
        {"id": "d1", "kind": "video", "title": "Ch — Vid (summary)", "url": "", "date": "",
         "text": "summary of the pricing talk", "parent": "p1", "level": "digest"},
        {"id": "f1", "kind": "video", "title": "Ch — Vid", "url": "", "date": "",
         "text": "full transcript about pricing retainers monthly", "parent": "p1",
         "level": "full"},
        {"id": "f2", "kind": "video", "title": "Ch — Vid", "url": "", "date": "",
         "text": "unrelated chatter about the weather", "parent": "p1", "level": "full"},
    ]
    sidx = assistant_ai.build_index(schunks)
    out = assistant_ai._expand_hits(sidx, [dict(schunks[0])], "pricing retainers")
    _check("small-to-big expansion pulls the most relevant full sibling after the digest",
           [c["id"] for c in out][:2] == ["d1", "f1"])
    _check("expansion never duplicates a chunk already retrieved",
           [c["id"] for c in assistant_ai._expand_hits(
               sidx, [dict(schunks[0]), dict(schunks[1])], "pricing retainers")].count("f1") == 1)

    # --- Watcher summaries: capped batch, in-place, skip-already-summarized ----------------------
    sum_arch = _fake_archives()
    updates, n_sum, serr = assistant_ai.summarize_videos(
        sum_arch, lambda s, u: ("- Bullet one\nTakeaway: do the thing.", ""), cap=1)
    _check("summarize_videos caps the batch and stores the summary in place",
           n_sum == 1 and serr == "" and "wch_test01" in updates
           and sum_arch[0][1][0]["summary"].startswith("- Bullet")
           and not sum_arch[0][1][1].get("summary"))
    _u2, n_sum2, _e2 = assistant_ai.summarize_videos(
        sum_arch, lambda s, u: ("- Bullet one\nTakeaway: do the thing.", ""), cap=8)
    _check("already-summarized videos are skipped on the next run",
           n_sum2 == 1 and sum_arch[0][1][1].get("summary"))
    _u3, n_sum3, serr3 = assistant_ai.summarize_videos(
        _fake_archives(), lambda s, u: ("", "model down"), cap=8)
    _check("a failing summarizer skips (retried later), never raises",
           n_sum3 == 0 and serr3 == "model down")

    # --- BM25 retrieval + the date filter ---------------------------------------------------------
    index = assistant_ai.build_index(chunks, fp=fp2)
    hits = assistant_ai.search(index, "how should I price AI retainers")
    _check("search ranks the pricing transcript first",
           hits and hits[0]["kind"] == "video" and "retainers" in hits[0]["text"])
    hits = assistant_ai.search(index, "cold outreach emails",
                               date_from="2026-01-01", date_to="")
    _check("date range excludes the 2024 video",
           all(c["id"] != "yt:wch_test01:v2:0" for c in hits))

    # --- Lenient answer parsing -------------------------------------------------------------------
    _check("parses plain JSON", assistant_ai._parse_answer('{"answer": "Charge monthly."}')
           == "Charge monthly.")
    _check("parses fenced JSON", assistant_ai._parse_answer('```json\n{"answer": "Yes."}\n```') == "Yes.")
    _check("falls back to raw text", assistant_ai._parse_answer("Just words.") == "Just words.")
    # Nearly-JSON salvage: the UI must NEVER be handed a raw JSON envelope (2026-07-13 failure:
    # junk between the answer string's closing quote and the brace made json.loads reject it all).
    broken = '{\n  "answer": "Reed says \\"charge monthly\\" [1].\\n\\nUse retainers."\n."\n}'
    _check("salvages junk inside the envelope",
           assistant_ai._parse_answer(broken) == 'Reed says "charge monthly" [1].\n\nUse retainers.')
    _check("salvages trailing junk after a valid object",
           assistant_ai._parse_answer('{"answer": "Done."} trailing noise') == "Done.")
    _check("tolerates raw newlines inside the answer string",
           assistant_ai._parse_answer('{"answer": "Line one.\nLine two."}') == "Line one.\nLine two.")
    _check("salvages output truncated at the token cap",
           assistant_ai._parse_answer('{"answer": "Cut off mid-sent') == "Cut off mid-sent")
    _check("a broken envelope never reaches the UI as raw JSON",
           not assistant_ai._parse_answer(broken).lstrip().startswith("{"))

    # --- ask() with a stubbed model ---------------------------------------------------------------
    answer, sources, err = assistant_ai.ask(
        "Riverdance", index, "how should I price AI retainers",
        caller=lambda system, user: ('{"answer": "Monthly retainers, per Carson."}', ""))
    _check("ask returns the answer + cited sources",
           answer == "Monthly retainers, per Carson." and err == ""
           and any("Carson Reed" in s["title"] for s in sources))
    answer, sources, err = assistant_ai.ask(
        "Riverdance", index, "how should I price AI retainers",
        caller=lambda system, user: ("", "no AI provider configured"))
    _check("model error surfaces as the error", err == "no AI provider configured" and answer == "")
    answer, sources, err = assistant_ai.ask("Riverdance", index, "zzqx unmatchable gibberish qqq")
    _check("no matching chunks -> friendly error", err != "" and "match" in err)

    # --- Depth: the detail control shapes prompts, and deep query-plans before answering ---------
    _check("depth styles the system prompt",
           "2-4 tight sentences" in assistant_ai._system_prompt("X", "quick")
           and "structured analysis" in assistant_ai._system_prompt("X", "deep")
           and "disagreement worth reporting" in assistant_ai._system_prompt("X"))
    _check("plan_queries parses the model's queries",
           assistant_ai.plan_queries("nick vs carson", [], lambda s, u: (
               '{"queries": ["nick saraev cold email", "carson reed paid ads"]}', ""))
           == ["nick saraev cold email", "carson reed paid ads"])
    _check("plan_queries degrades to [] on any failure",
           assistant_ai.plan_queries("q", [], lambda s, u: ("not json", "")) == []
           and assistant_ai.plan_queries("q", [], lambda s, u: ("", "model down")) == [])
    calls = []

    def deep_caller(system, user):
        calls.append(system)
        if "search queries" in system:
            return ('{"queries": ["pricing AI retainers", "cold outreach emails"]}', "")
        return ('{"answer": "Deep dive."}', "")

    answer, sources, err = assistant_ai.ask(
        "Riverdance", index, "how should I price AI retainers",
        depth="deep", caller=deep_caller)
    _check("deep ask plans queries, then answers with the deep prompt",
           answer == "Deep dive." and err == "" and len(calls) == 2
           and "structured analysis" in calls[1])
    _check("deep retrieval unions the planned queries' hits",
           any("cold outreach" in s["title"].lower() or s["kind"] == "video" for s in sources))

    # --- Hybrid retrieval: the semantic leg surfaces a chunk BM25 misses; RRF fuses the legs ------
    # A tiny deterministic "embedder": text -> a concept-count vector, so a query and a document that
    # share MEANING but no keywords ("coming back" ~ "loyalty/churn/repeat") land on the same vector.
    _CONCEPTS = (
        ("retention", "loyalty", "repeat", "coming back", "churn", "retain"),
        ("price", "pricing", "retainer", "charge", "fee", "cost"),
        ("email", "inbox", "outreach", "cold"),
    )

    def _fake_embed(texts):
        vecs = []
        for t in texts:
            tl = (t or "").lower()
            v = [float(sum(w in tl for w in grp)) for grp in _CONCEPTS]
            vecs.append(v if any(v) else [0.01, 0.01, 0.01])
        return vecs, ""

    hchunks = [
        {"id": "h_loyal", "kind": "content", "title": "Loyalty program", "url": "", "date": "",
         "text": "our loyalty program keeps churn low and drives repeat purchases"},
        {"id": "h_cold", "kind": "content", "title": "Cold email", "url": "", "date": "",
         "text": "cold outreach emails to brand new prospects"},
        {"id": "h_price", "kind": "content", "title": "Pricing", "url": "", "date": "",
         "text": "we charge a monthly retainer fee for the service"},
    ]
    hidx = assistant_ai.build_index(hchunks)
    _check("BM25 alone misses the semantically-related chunk",
           all(c["id"] != "h_loyal"
               for c in assistant_ai.search(hidx, "how do we get customers coming back")))
    assistant_ai.embed_index(hidx, _fake_embed)
    _check("embed_index attaches a semantic leg",
           assistant_ai.has_embeddings(hidx) and hidx["emb_count"] == 3 and hidx["emb_dim"] == 3)
    _check("packed vectors round-trip",
           assistant_ai._unpack_vec(hidx["emb"]["h_cold"], 3) is not None)

    # --- Incremental embedding: a rebuild reuses unchanged vectors, embeds only what changed --------
    # (the "was fast, now unusable" fix -- a Watcher fetch must not re-embed the whole corpus).
    embed_calls = []

    def _counting_embed(texts):
        embed_calls.append(list(texts))
        return _fake_embed(texts)

    # One new chunk, one changed chunk, one unchanged -> exactly two embed calls, one reused vector.
    hchunks2 = [
        dict(hchunks[0]),                                        # h_loyal: unchanged -> reuse
        {"id": "h_cold", "kind": "content", "title": "Cold email", "url": "", "date": "",
         "text": "cold outreach emails to WARM prospects now"},  # h_cold: content changed -> re-embed
        {"id": "h_new", "kind": "content", "title": "Upsell", "url": "", "date": "",
         "text": "upsell existing customers into a higher tier"},  # brand new -> embed
    ]
    hidx2 = assistant_ai.build_index(hchunks2)
    reused_vec = hidx["emb"]["h_loyal"]
    assistant_ai.embed_index(hidx2, _counting_embed, prev=hidx)
    embedded_texts = [t for batch in embed_calls for t in batch]
    _check("incremental embed skips unchanged chunks, embeds only new/changed ones",
           hidx2["emb_count"] == 3 and len(embedded_texts) == 2)
    _check("the unchanged chunk's stored vector is carried over verbatim (no re-embed)",
           hidx2["emb"]["h_loyal"] == reused_vec
           and not any("loyalty program" in t.lower() for t in embedded_texts))
    _check("a changed chunk IS re-embedded (stale vector never reused)",
           any("warm prospects" in t.lower() for t in embedded_texts))
    _check("first-ever embed (no prev) still embeds everything",
           (lambda i: (assistant_ai.embed_index(i, _fake_embed), i["emb_count"])[1])(
               assistant_ai.build_index(hchunks)) == 3)
    answer, sources, err = assistant_ai.ask(
        "X", hidx, "how do we get customers coming back",
        caller=lambda system, user: ('{"answer": "Lean on loyalty."}', ""),
        query_embedder=lambda q: (_fake_embed([q])[0][0], ""))
    _check("hybrid retrieval surfaces the chunk BM25 missed",
           err == "" and any(s["title"] == "Loyalty program" for s in sources))

    # --- RRF: rank-only fusion (incompatible BM25/cosine scales never fight) ----------------------
    _check("RRF ranks the item strong in BOTH lists first",
           assistant_ai._rrf([[5, 3, 1], [3, 9, 1]])[0] == 3)
    _check("RRF de-dupes across lists", set(assistant_ai._rrf([[1, 2], [2, 3]])) == {1, 2, 3})

    # --- Metadata pre-filter: confident single-source only, never a cross-source/generic question -
    # (email questions scope to email AND comms since v4 -- the Communications timeline mirrors
    # client-tier email threads, so an email ask must see both.)
    _check("single-source question infers its kind",
           assistant_ai._infer_kinds("how are we handling the client's email replies")
           == {"email", "comms"})
    _check("cross-source question stays unfiltered",
           assistant_ai._infer_kinds("compare our campaigns with what creators say in videos") is None)
    _check("generic question stays unfiltered",
           assistant_ai._infer_kinds("what should we focus on next quarter") is None)

    # --- Titles are searchable + a named creator survives a 'campaign' question (the 2026-07 bug) --
    # The creator/channel name lives only in a chunk's TITLE, never in the transcript body, so it
    # must be indexed for a name query to retrieve that creator's videos.
    _check("a video is retrievable by its creator NAME (title is indexed, not just the body)",
           any(h["kind"] == "video" for h in assistant_ai.search(index, "Carson Reed")))
    # "what would <creator> say about <campaign>" contains 'campaign' -> _infer_kinds alone would
    # scope to {content,campaign} and drop every transcript. The creator name must reopen video.
    _named_kinds = assistant_ai._infer_kinds("what would Carson Reed say about the summer campaign")
    _check("'campaign' question alone would exclude videos",
           _named_kinds is not None and "video" not in _named_kinds)
    _cross = assistant_ai.ask(
        "Riverdance", index, "what would Carson Reed say about the summer campaign",
        caller=lambda system, user: ('{"answer": "ok"}', ""))
    _check("a creator named beside 'campaign' still retrieves that creator's videos",
           _cross[2] == "" and any(s["kind"] == "video" and "Carson Reed" in s["title"]
                                   for s in _cross[1]))

    # --- Reranker seam: it gets {id,title,content} records and ITS order drives the cited sources --
    seen_rr = {}

    def _rerank_stub(query, records, top_n):
        seen_rr["keys"] = set(records[0].keys())
        picked = ([r for r in records if r["title"] == "Cold email"]
                  + [r for r in records if r["title"] != "Cold email"])
        return picked[:top_n or len(picked)], ""

    answer, sources, err = assistant_ai.ask(
        "X", hidx, "prospects loyalty retainer service",
        caller=lambda system, user: ('{"answer": "ok"}', ""), reranker=_rerank_stub)
    _check("reranker receives id/title/content records", {"id", "title", "content"} <= seen_rr["keys"])
    _check("rerank order drives the cited sources", sources and sources[0]["title"] == "Cold email")
    # A failing reranker must degrade to the fused order, never blow up the answer.
    answer, sources, err = assistant_ai.ask(
        "X", hidx, "prospects loyalty retainer service",
        caller=lambda system, user: ('{"answer": "ok"}', ""),
        reranker=lambda q, recs, n: (recs, "rerank error"))
    _check("failing reranker degrades gracefully", err == "" and answer == "ok" and sources)

    # --- Streaming ask: sources first, live thinking+answer deltas, steer injection ---------------
    seen_prompt = {}

    def _stream_caller(system, user):
        seen_prompt["system"] = system
        seen_prompt["user"] = user
        yield {"type": "thinking", "text": "weighing loyalty vs cold email"}
        yield {"type": "answer", "text": "Lean on "}
        yield {"type": "answer", "text": "loyalty."}
        yield {"type": "usage", "input_tokens": 12, "output_tokens": 5}

    evs = list(assistant_ai.ask_stream(
        "X", hidx, "how do we keep customers", steer="focus on retention only",
        query_embedder=lambda q: (_fake_embed([q])[0][0], ""), stream_caller=_stream_caller))
    types = [e["type"] for e in evs]
    _check("stream emits sources FIRST, then thinking, answer, usage",
           types[0] == "sources" and "thinking" in types and "answer" in types and "usage" in types)
    _check("stream answer deltas assemble the reply",
           "".join(e["text"] for e in evs if e["type"] == "answer") == "Lean on loyalty.")
    _check("streaming prompt is plain-markdown (no JSON envelope)",
           '"answer"' not in seen_prompt["system"] and "markdown" in seen_prompt["system"].lower())
    _check("the steer is injected into the answer prompt",
           "focus on retention only" in seen_prompt["user"])
    empty = list(assistant_ai.ask_stream("X", hidx, "zzqx nomatch qqq",
                                         stream_caller=_stream_caller))
    _check("stream with no hits -> a single error event",
           len(empty) == 1 and empty[0]["type"] == "error")

    # --- Plan checkpoint: returns the sub-questions + sources WITHOUT answering -------------------
    def _plan_caller(system, user):
        if "search queries" in system:
            return ('{"queries": ["loyalty retention", "cold email outreach"]}', "")
        return ("", "")

    queries, psources = assistant_ai.plan_stage(
        hidx, "keep customers vs win new ones", depth="deep",
        query_embedder=lambda q: (_fake_embed([q])[0][0], ""), plan_caller=_plan_caller)
    _check("plan_stage returns the planned sub-questions + sources",
           "keep customers vs win new ones" in queries and len(queries) >= 2 and psources)

    # --- Action proposals: split, stream filter, catalog-prompted ask, validation, execution ------
    vis, props = assistant_ai.split_actions(
        "Done.\n" + assistant_ai.ACTIONS_MARKER
        + '\n[{"action": "add_task", "params": {"title": "New LP"}, "note": "adds it"}]')
    _check("split_actions separates the visible answer from the proposals",
           vis == "Done." and props and props[0]["action"] == "add_task")
    _check("split_actions tolerates fenced JSON",
           assistant_ai.split_actions(
               "ok\n%s\n```json\n[{\"action\":\"reindex\"}]\n```" % assistant_ai.ACTIONS_MARKER
           )[1][0]["action"] == "reindex")
    _check("no marker -> no proposals",
           assistant_ai.split_actions("plain answer") == ("plain answer", []))

    tail = assistant_ai._ActionTail()
    seen_text = tail.feed("The answer is yes.\n===ATRIUM_")
    seen_text += tail.feed("ACTIONS===\n[{\"action\":\"reindex\"")
    seen_text += tail.feed(",\"params\":{}}]")
    rest, tprops = tail.finish()
    _check("stream filter never shows the marker (even split across deltas) and parses proposals",
           "ATRIUM_ACTIONS" not in (seen_text + rest)
           and "The answer is yes." in (seen_text + rest)
           and tprops and tprops[0]["action"] == "reindex")
    tail2 = assistant_ai._ActionTail()
    plain = tail2.feed("Just a normal answer with no proposals in it at all.")
    rest2, props2 = tail2.finish()
    _check("stream filter passes a plain answer through untouched",
           plain + rest2 == "Just a normal answer with no proposals in it at all."
           and props2 == [])

    inner = ("Proposing it now.\n" + assistant_ai.ACTIONS_MARKER
             + '\n[{"action": "add_task", "params": {"title": "Launch retargeting"}}]')
    acts_out = []
    answer, sources, err = assistant_ai.ask(
        "Riverdance", index, "add a task to launch the retargeting campaign",
        caller=lambda system, user: (json.dumps({"answer": inner}), ""),
        actions_catalog="- add_task(title*)", actions_out=acts_out)
    _check("ask strips the proposal block from the answer and fills actions_out",
           err == "" and "ATRIUM_ACTIONS" not in answer and acts_out
           and acts_out[0]["action"] == "add_task")
    _check("the catalog is taught to the model only when actions are enabled",
           "PROPOSE workspace actions" in assistant_ai._actions_note("- add_task(title*)")
           and assistant_ai._actions_note("") == "")

    def _act_stream(system, user):
        yield {"type": "answer", "text": "Sure - proposing.\n"}
        yield {"type": "answer",
               "text": assistant_ai.ACTIONS_MARKER + '\n[{"action":"reindex","params":{}}]'}

    evs = list(assistant_ai.ask_stream(
        "X", hidx, "how do we get customers coming back",
        query_embedder=lambda q: (_fake_embed([q])[0][0], ""),
        stream_caller=_act_stream, actions_catalog="- reindex()"))
    txt = "".join(e.get("text", "") for e in evs if e["type"] == "answer")
    _check("streamed proposals never render as text and arrive as one proposals event",
           "ATRIUM_ACTIONS" not in txt and "Sure - proposing." in txt
           and any(e["type"] == "proposals" and e["proposals"][0]["action"] == "reindex"
                   for e in evs))

    import assistant_actions
    # The task-WRITE actions (add_task / move_task / complete_task) were retired by D2 (2026-08-03)
    # and RESTORED 2026-08-04 (D2 amended — see main.py's route comment): `ws["tasks"]` is the
    # STORE on both surfaces, Sentinel lists it live over the bridge, and every executor calls the
    # same workspace.py writers, so an approved proposal is one record, not a racing copy. This
    # block proves the "here's what I completed this week" flow end to end below.
    clean, verr = assistant_actions.validate(
        {"action": "comment_task", "params": {"task": "SEED-TASK-FOR-ACTIONS",
                                             "body": "From AI"}, "note": "test"})
    _check("validate accepts a known action and labels it",
           clean is not None and verr == "" and clean["gate"] == "admin"
           and "SEED-TASK-FOR-ACTIONS" in clean["label"])
    _check("validate rejects unknown actions",
           assistant_actions.validate({"action": "rm_rf", "params": {}})[1].startswith("unknown"))
    for back in ("add_task", "move_task", "complete_task"):
        _check("the restored %s action validates + is offered to the model" % back,
               assistant_actions.validate({"action": back, "params": {"title": "x", "task": "x",
                                                                     "stage": "todo"}})[1] == ""
               and ("%s(" % back) in assistant_actions.catalog_text())
    _check("validate enforces required params",
           "missing required" in assistant_actions.validate(
               {"action": "comment_task", "params": {"task": "x"}})[1])
    _check("validate enforces choice params",
           "must be one of" in assistant_actions.validate(
               {"action": "add_intel", "params": {"section": "nope", "title": "t"}})[1])
    _check("every registry action is described in the catalog",
           all(name in assistant_actions.catalog_text()
               for name in ("comment_task", "generate_report", "edit_report",
                            "run_website_check", "reindex")))
    _check("a root-gated action refuses a non-root approver",
           assistant_actions.execute(
               CLIENT, assistant_actions.validate(
                   {"action": "set_website_notes", "params": {"notes": "x"}})[0],
               {"is_root": False})
           == (False, "only the super admin can approve this action"))
    # A task to comment on — written through the workspace helper, the way the bridge does.
    workspace.add_task(CLIENT, {"title": "SEED-TASK-FOR-ACTIONS"}, actor="tester")
    ok, msg = assistant_actions.execute(CLIENT, clean, {"actor": "Tester", "is_root": True})
    _check("an approved comment_task lands on the task's thread",
           ok and "SEED-TASK-FOR-ACTIONS" in msg
           and any(t.get("title") == "SEED-TASK-FOR-ACTIONS" and t.get("comments")
                   for t in workspace.load_workspace(CLIENT).get("tasks") or []))
    ok3, msg3 = assistant_actions.execute(
        CLIENT, assistant_actions.validate(
            {"action": "comment_task", "params": {"task": "no such task xyz",
                                                 "body": "hi"}})[0],
        {"actor": "Tester", "is_root": False})
    _check("an unresolvable target fails with a friendly reason, never raises",
           ok3 is False and "not found" in msg3)
    # The restored writers, end to end — the "here are the things I completed this week" flow:
    # add_task(stage=completed) files straight into the Completed column, client-facing by default
    # (the proposal is made looking at the client Tasks board); move_task / complete_task shuffle
    # it from there. A paraphrased stage ("Completed") canonicalises rather than 500s.
    okA, msgA = assistant_actions.execute(
        CLIENT, assistant_actions.validate(
            {"action": "add_task", "params": {"title": "DONE-THIS-WEEK",
                                              "stage": "Completed"}})[0],
        {"actor": "Tester", "is_root": False})
    _doneA = next((t for t in workspace.load_workspace(CLIENT).get("tasks") or []
                   if t.get("title") == "DONE-THIS-WEEK"), None)
    _check("an approved add_task(stage=completed) lands in Completed, client-facing",
           okA and "completed" in msgA and _doneA is not None
           and _doneA.get("stage") == "completed" and _doneA.get("client_facing") is True)
    okB, _msgB = assistant_actions.execute(
        CLIENT, assistant_actions.validate(
            {"action": "move_task", "params": {"task": "DONE-THIS-WEEK",
                                               "stage": "in_progress"}})[0],
        {"actor": "Tester", "is_root": False})
    _check("an approved move_task moves it by title",
           okB and any(t.get("title") == "DONE-THIS-WEEK" and t.get("stage") == "in_progress"
                       for t in workspace.load_workspace(CLIENT).get("tasks") or []))
    okC, _msgC = assistant_actions.execute(
        CLIENT, assistant_actions.validate(
            {"action": "complete_task", "params": {"task": "DONE-THIS-WEEK"}})[0],
        {"actor": "Tester", "is_root": False})
    _check("an approved complete_task marks it done",
           okC and any(t.get("title") == "DONE-THIS-WEEK" and t.get("stage") == "completed"
                       for t in workspace.load_workspace(CLIENT).get("tasks") or []))
    okD, _msgD = assistant_actions.execute(
        CLIENT, assistant_actions.validate(
            {"action": "add_task", "params": {"title": "INTERNAL-ONLY-ADD",
                                              "client_facing": "false"}})[0],
        {"actor": "Tester", "is_root": False})
    _check("an explicit client_facing=false keeps the card internal (default is true)",
           okD and any(t.get("title") == "INTERNAL-ONLY-ADD" and t.get("stage") == "todo"
                       and t.get("client_facing") is False
                       for t in workspace.load_workspace(CLIENT).get("tasks") or []))

    # --- The route: lazy rebuild, reindex, ask (stubbed), gating ---------------------------------
    main.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False, SESSION_COOKIE_SAMESITE="Lax")
    c = main.app.test_client()
    with c.session_transaction() as s:
        s.update(SUPER)

    r = c.post("/w/%s/admin/assistant" % CLIENT, data={"op": "reindex"})
    data = r.get_json()
    _check("op=reindex builds + stores the index", data["ok"] is True and data["chunks"] > 0)
    _check("index object persisted", workspace.read_assistant_index(CLIENT) is not None)

    r = c.post("/w/%s/admin/assistant" % CLIENT,
               data={"op": "execute", "action": json.dumps(
                   {"action": "comment_task", "params": {"task": "SEED-TASK-FOR-ACTIONS",
                                                         "body": "ship it"}})})
    data = r.get_json()
    _check("op=execute runs an approved action through the route",
           data["ok"] is True and "Commented" in data["message"])
    _check("op=execute rejects garbage",
           c.post("/w/%s/admin/assistant" % CLIENT,
                  data={"op": "execute", "action": "not json"}).get_json()["ok"] is False)

    r = c.post("/w/%s/admin/assistant" % CLIENT, data={"op": "ask", "question": ""})
    _check("empty question refused", r.get_json()["ok"] is False)

    r = c.post("/w/%s/admin/assistant" % CLIENT, data={"op": "settings", "depth": "deep"})
    _check("op=settings saves the depth",
           r.get_json()["ok"] is True
           and (workspace.load_workspace(CLIENT).get("assistant") or {}).get("depth") == "deep")
    r = c.post("/w/%s/admin/assistant" % CLIENT, data={"op": "settings", "depth": "bogus"})
    _check("unknown depth refused", r.get_json()["ok"] is False)
    r = c.post("/w/%s/admin/assistant" % CLIENT, data={"op": "settings", "model": ""})
    _check("saving the model alone leaves the depth untouched",
           r.get_json()["ok"] is True
           and (workspace.load_workspace(CLIENT).get("assistant") or {}).get("depth") == "deep")

    real_ask = assistant_ai.ask
    assistant_ai.ask = lambda name, idx, q, **kw: ("Answer from the stub.", [
        {"title": "Campaign: Summer Lead-Gen Push", "kind": "campaign", "date": "", "url": ""}], "")
    r = c.post("/w/%s/admin/assistant" % CLIENT,
               data={"op": "ask", "question": "What campaigns are running?",
                     "history": '[{"role":"user","text":"hi"}]'})
    data = r.get_json()
    _check("op=ask returns answer + sources",
           data["ok"] is True and data["answer"] == "Answer from the stub."
           and data["sources"][0]["kind"] == "campaign")
    assistant_ai.ask = real_ask

    # --- The streaming route: SSE frames for the answer stage AND the plan checkpoint ------------
    real_stream = assistant_ai.ask_stream
    assistant_ai.ask_stream = lambda name, idx, q, **kw: iter([
        {"type": "sources", "sources": [{"title": "Carson Reed", "kind": "video", "date": "", "url": ""}]},
        {"type": "thinking", "text": "considering the transcripts"},
        {"type": "answer", "text": "Monthly retainers."},
        {"type": "usage", "input_tokens": 3, "output_tokens": 2},
    ])
    r = c.post("/w/%s/admin/assistant/stream" % CLIENT,
               data={"question": "how to price?", "stage": "answer"})
    sse = r.get_data(as_text=True)
    _check("stream route emits SSE content-type", r.mimetype == "text/event-stream")
    _check("stream route forwards sources/thinking/answer + a priced usage + done",
           "event: sources" in sse and "event: thinking" in sse and "event: answer" in sse
           and "event: usage" in sse and "cost_usd" in sse and "event: done" in sse)
    assistant_ai.ask_stream = real_stream

    real_plan = assistant_ai.plan_stage
    assistant_ai.plan_stage = lambda idx, q, *a, **kw: (
        ["price retainers", "cold email"], [{"title": "Carson Reed", "kind": "video", "date": "", "url": ""}])
    r = c.post("/w/%s/admin/assistant/stream" % CLIENT,
               data={"question": "compare", "stage": "plan"})
    sse = r.get_data(as_text=True)
    _check("plan stage returns plan + sources, no answer",
           "event: plan" in sse and "event: sources" in sse and "event: answer" not in sse)
    assistant_ai.plan_stage = real_plan

    body = c.get("/w/%s/assistant" % CLIENT).get_data(as_text=True)
    _check("assistant pane renders for the team",
           'data-pane="assistant"' in body and 'id="ax-as-send"' in body)
    _check("detail (depth) selectors render in both surfaces, with the saved choice",
           'id="ax-as-depth"' in body and 'id="ax-asfab-depth"' in body
           and 'value="deep" selected' in body)

    with c.session_transaction() as s:
        s.clear()
        s.update(CLIENT_LOGIN)
    body = c.get("/w/%s/assistant" % CLIENT).get_data(as_text=True)
    _check("client hitting /assistant is bounced (no pane in the DOM)",
           'data-pane="assistant"' not in body)
    _check("client POST is forbidden",
           c.post("/w/%s/admin/assistant" % CLIENT,
                  data={"op": "ask", "question": "hi"}).status_code == 403)
    _check("client streaming POST is forbidden",
           c.post("/w/%s/admin/assistant/stream" % CLIENT,
                  data={"question": "hi"}).status_code == 403)

    # --- Conversation history: server-side team-shared save/list/get/delete -----------------------
    # workspace layer: upsert by id, newest-first, turns/list caps.
    workspace.save_assistant_conversation(CLIENT, "cv1", "Pricing chat",
                                          [{"role": "user", "text": "how to price?"},
                                           {"role": "bot", "text": "Monthly retainers."}])
    workspace.save_assistant_conversation(CLIENT, "cv2", "Campaigns chat",
                                          [{"role": "user", "text": "what campaigns?"}])
    lst = workspace.list_assistant_conversations(CLIENT)
    _check("history list returns both, newest first, without turns",
           len(lst) == 2 and lst[0]["id"] == "cv2" and "turns" not in lst[0]
           and lst[0]["turn_count"] == 1)
    workspace.save_assistant_conversation(CLIENT, "cv1", "Pricing chat v2",
                                          [{"role": "user", "text": "how to price?"},
                                           {"role": "bot", "text": "Monthly retainers."},
                                           {"role": "user", "text": "and support?"}])
    _check("saving same id UPSERTS (no duplicate) + bumps it to newest",
           len(workspace.list_assistant_conversations(CLIENT)) == 2
           and workspace.list_assistant_conversations(CLIENT)[0]["id"] == "cv1")
    got = workspace.get_assistant_conversation(CLIENT, "cv1")
    _check("get returns the full turns", got and len(got["turns"]) == 3
           and got["title"] == "Pricing chat v2")
    workspace.delete_assistant_conversation(CLIENT, "cv2")
    _check("delete removes just that conversation",
           [x["id"] for x in workspace.list_assistant_conversations(CLIENT)] == ["cv1"])

    # route ops (as the team; client is still logged in as CLIENT here -> must be forbidden first).
    _check("client history POST is forbidden",
           c.post("/w/%s/admin/assistant" % CLIENT,
                  data={"op": "history_list"}).status_code == 403)
    with c.session_transaction() as s:
        s.clear(); s.update(SUPER)
    r = c.post("/w/%s/admin/assistant" % CLIENT,
               data={"op": "history_save", "conv_id": "cv3", "title": "Route saved",
                     "turns": '[{"role":"user","text":"hi"},{"role":"bot","text":"hello"}]'})
    _check("op=history_save persists + returns the list",
           r.get_json()["ok"] is True
           and any(x["id"] == "cv3" for x in r.get_json()["conversations"]))
    r = c.post("/w/%s/admin/assistant" % CLIENT, data={"op": "history_get", "conv_id": "cv3"})
    _check("op=history_get returns the turns",
           r.get_json()["ok"] is True and len(r.get_json()["conversation"]["turns"]) == 2)
    r = c.post("/w/%s/admin/assistant" % CLIENT, data={"op": "history_get", "conv_id": "nope"})
    _check("op=history_get on a missing id -> friendly not-ok", r.get_json()["ok"] is False)
    r = c.post("/w/%s/admin/assistant" % CLIENT, data={"op": "history_delete", "conv_id": "cv3"})
    _check("op=history_delete removes it",
           r.get_json()["ok"] is True
           and not any(x["id"] == "cv3" for x in r.get_json()["conversations"]))

    # --- The rebuild is BOUNDED on the request path (the 2026-07-31 permanent-death fix) ----------
    # A full rebuild re-embeds the whole corpus. Run inside a request that blew BOTH the memory
    # limit and the request timeout -- and since the index is written LAST it persisted nothing, so
    # every later ask retried the identical doomed work. Chat was permanently dead, not just slow.
    _check("needs_full_embed: a version-mismatched index can carry nothing over",
           assistant_ai.needs_full_embed({"v": 3, "emb": {"a": "x"}, "emb_dim": 4}) is True)
    _check("needs_full_embed: an index with no semantic leg needs a full embed",
           assistant_ai.needs_full_embed({"v": assistant_ai.INDEX_VERSION}) is True)
    _check("needs_full_embed: a current, embedded index does NOT",
           assistant_ai.needs_full_embed(
               {"v": assistant_ai.INDEX_VERSION, "emb": {"a": "x"}, "emb_dim": 4}) is False)

    # carry_over keeps ONLY the vector maps, so the caller can release the old index's (multi-MB)
    # chunks before building + serializing the new one -- that retention was the memory peak.
    carried = assistant_ai.carry_over(
        {"v": 5, "chunks": [{"id": "big", "text": "x" * 10000}], "df": {"a": 1},
         "emb": {"c1": "packed"}, "emb_sig": {"c1": "sig"}, "emb_dim": 8})
    _check("carry_over keeps the vector maps",
           carried["emb"] == {"c1": "packed"} and carried["emb_sig"] == {"c1": "sig"}
           and carried["emb_dim"] == 8)
    _check("carry_over drops chunks + df (the bulk of a stored index)",
           "chunks" not in carried and "df" not in carried)

    # embed_index(max_new=) bounds the work: past the cap the rest is DEFERRED and RECORDED, so a
    # request can never stall on a whole-corpus embed and the leftovers stay visible to the job.
    calls = []

    def _emb(texts):
        calls.append(len(texts))
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts], ""

    def _mk(n):
        return assistant_ai.build_index(
            [{"id": "c%d" % i, "kind": "video", "title": "t%d" % i, "text": "body %d" % i}
             for i in range(n)], fp="fp")

    idx = _mk(10)
    assistant_ai.embed_index(idx, _emb, prev=None, max_new=4)
    _check("embed_index(max_new=4) embeds only 4 of 10", calls == [4])
    _check("embed_index records the deferred remainder", idx.get("emb_partial") == 6)
    calls[:] = []
    idx2 = _mk(10)
    assistant_ai.embed_index(idx2, _emb, prev=None, max_new=0)
    _check("embed_index uncapped (what the JOB uses) embeds everything", calls == [10])
    _check("an uncapped index carries no emb_partial marker", "emb_partial" not in idx2)

    # The queue: the ask path FLAGS the client instead of doing job-sized work inline.
    workspace.clear_assistant_reindex(CLIENT)
    _check("a cleared workspace is not flagged",
           workspace.assistant_reindex_pending(workspace.load_workspace(CLIENT)) is False)
    workspace.queue_assistant_reindex(CLIENT, "index built at v3, code is v5")
    ws_q = workspace.load_workspace(CLIENT)
    _check("queue_assistant_reindex flags it, with the reason",
           workspace.assistant_reindex_pending(ws_q) is True
           and "v3" in ws_q["assistant"]["reindex"]["reason"])
    workspace.clear_assistant_reindex(CLIENT, built_at="2026-07-31T00:00:00Z")
    _check("clear_assistant_reindex unflags it",
           workspace.assistant_reindex_pending(workspace.load_workspace(CLIENT)) is False)

    # End to end: a stale, version-mismatched index must be SERVED, never rebuilt on the ask path.
    stale = {"v": 3, "fingerprint": "old-fp", "built_at": "2026-07-18T10:00:59Z",
             "chunks": [{"id": "old1", "kind": "video", "title": "Kept", "text": "old content"}],
             "df": {"old": 1}, "n_docs": 1, "avgdl": 2.0,
             "emb": {"old1": "packed"}, "emb_sig": {"old1": "sig"}, "emb_dim": 4}
    workspace.write_assistant_index(CLIENT, stale)
    _real_cfg = main.intel_ai.embeddings_configured
    main.intel_ai.embeddings_configured = lambda: True
    try:
        served = main._assistant_index(workspace.load_workspace(CLIENT), CLIENT)
    finally:
        main.intel_ai.embeddings_configured = _real_cfg
    _check("a version-mismatched index is SERVED as-is, not rebuilt on the ask path",
           served.get("v") == 3 and served.get("built_at") == "2026-07-18T10:00:59Z")
    _check("the stored index is left untouched (no half-written rebuild)",
           workspace.read_assistant_index(CLIENT).get("v") == 3)
    _check("...and the client is queued for the assistant-reindex job",
           workspace.assistant_reindex_pending(workspace.load_workspace(CLIENT)) is True)

    # The JOB is what actually repairs it -- uncapped, off the request path.
    import assistant_reindex
    res = assistant_reindex.reindex_client(CLIENT, embedder=_emb, summarize=False)
    _check("the job rebuilds the index at the current INDEX_VERSION",
           res["ok"] is True
           and workspace.read_assistant_index(CLIENT)["v"] == assistant_ai.INDEX_VERSION)
    _check("the job clears the queue flag",
           workspace.assistant_reindex_pending(workspace.load_workspace(CLIENT)) is False)
    _check("a repaired client is no longer selected by the cheap tick",
           assistant_reindex.needs_reindex(CLIENT, workspace.load_workspace(CLIENT))[0] is False)


if __name__ == "__main__":
    try:
        run()
        print("PASS")
    except AssertionError as exc:
        print("FAIL: %s" % exc)
        sys.exit(1)
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
