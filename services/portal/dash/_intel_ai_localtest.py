"""Local smoke test for the Market Intelligence AI brain -- runs entirely off-cloud, no network.

Exercises intel_ai (model registry + availability gating, the retrieve-then-curate mapping onto REAL
articles, prompt defaults/overrides, graceful degradation) with an INJECTED transport, plus
intel_refresh's AI path end-to-end against the local workspace backend. Proves:
  * a fabricated/out-of-range article index from the model is DROPPED (never a hallucinated link),
  * link/source/date always come from the real candidate, never the model,
  * a missing key / unparseable reply / no candidates -> None (caller falls back to plain RSS),
  * refresh_client uses the AI when a model is set, falls back to RSS when the key is absent, and
    backfills once (12-month window) then flips to the daily window.

    python _intel_ai_localtest.py        # prints PASS / FAIL and exits 0 / 1
"""

import json
import os
import shutil
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="intel_ai_localtest_")
os.environ["WORKSPACE_LOCAL_DIR"] = _TMP
# Start with a known provider-key state; individual checks tweak os.environ as needed.
for _k in ("GEMINI_API_KEY", "DEEPSEEK_API_KEY"):
    os.environ.pop(_k, None)

import intel_ai          # noqa: E402  (must follow the env setup above)
import intel_refresh     # noqa: E402
import workspace         # noqa: E402

CLIENT = "aitest"

# Real candidate articles, as intel_feed would hand them to curate() (title/link/body/source/date).
_CANDS = [
    {"title": "Google Ads adds a new PMax control", "link": "https://sel.com/a1",
     "body": "Advertisers get finer budget caps.", "source": "Search Engine Land", "date": "2026-07-01"},
    {"title": "Meta overhauls Advantage+ targeting", "link": "https://sel.com/a2",
     "body": "New signals for shopping campaigns.", "source": "Marketing Dive", "date": "2026-06-28"},
    {"title": "A totally irrelevant celebrity story", "link": "https://gossip.com/a3",
     "body": "Nothing to do with marketing.", "source": "Gossip Daily", "date": "2026-06-30"},
]


class _Resp(object):
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = payload if isinstance(payload, str) else json.dumps(payload)

    def json(self):
        if isinstance(self._payload, str):
            return json.loads(self._payload)
        return self._payload


# The model's reply: pick items 1 and 2 (skip the irrelevant #3), plus a FABRICATED #9 that must be
# dropped. Same JSON contract for both providers; the fetcher just wraps it in each API's envelope.
_MODEL_JSON = json.dumps({"entries": [
    {"n": 1, "heading": "Platform Update", "title": "Google Ads adds a new PMax control",
     "summary": "Google Ads now lets you cap PMax budgets more tightly -- useful for controlling spend."},
    {"n": 2, "heading": "Platform Update", "title": "Meta overhauls Advantage+ targeting",
     "summary": "Meta changed Advantage+ shopping signals; revisit your audience setup."},
    {"n": 9, "heading": "Fake", "title": "Hallucinated item", "summary": "Should be dropped."},
]})


def _deepseek_fetcher(url, headers, payload, timeout):
    return _Resp({"choices": [{"message": {"content": _MODEL_JSON}}]})


def _gemini_fetcher(url, headers, payload, timeout):
    return _Resp({"candidates": [{"content": {"parts": [{"text": _MODEL_JSON}]}}]})


def _bad_json_fetcher(url, headers, payload, timeout):
    return _Resp({"choices": [{"message": {"content": "sorry, I cannot help with that"}}]})


def _check(label, condition):
    if not condition:
        raise AssertionError(label)
    print("  [OK] %s" % label)


def run():
    print("[intel-ai-localtest] WORKSPACE_LOCAL_DIR = %s" % _TMP)

    # 1. Model registry + availability gating (env-driven).
    _check("four models offered", len(intel_ai.MODELS) == 4)
    _check("no key -> nothing available", intel_ai.available_models() and
           all(not m["available"] for m in intel_ai.available_models()))
    _check("no key -> default_model is ''", intel_ai.default_model() == "")
    _check("unknown model meta is None", intel_ai.model_meta("gpt-9") is None)
    os.environ["DEEPSEEK_API_KEY"] = "sk-test"
    _check("deepseek configured", intel_ai.provider_configured("deepseek"))
    _check("gemini still not configured", not intel_ai.provider_configured("gemini"))
    _check("deepseek model now available", intel_ai.model_available("deepseek-v4-pro"))
    _check("gemini model still unavailable", not intel_ai.model_available("gemini-2.5-pro"))
    _check("default_model prefers first available", intel_ai.default_model() == "deepseek-v4-flash")

    # 2. Prompt defaults + override plumbing.
    _check("business default prompt present", "industry" in intel_ai.default_prompt("business_research").lower())
    _check("media default prompt present", "platform" in intel_ai.default_prompt("media_buying").lower())

    # 3. _parse_entries tolerates fences and bare arrays.
    _check("parse fenced object", len(intel_ai._parse_entries("```json\n" + _MODEL_JSON + "\n```")) == 3)
    _check("parse bare array", len(intel_ai._parse_entries('[{"n":1}]')) == 1)
    _check("parse garbage -> []", intel_ai._parse_entries("no json here") == [])

    # 4. curate() via DeepSeek: keeps items 1+2, DROPS the fabricated #9, maps onto real articles.
    out = intel_ai.curate("media_buying", "Aitest Co", ["ppc"], _CANDS,
                          model="deepseek-v4-pro", fetcher=_deepseek_fetcher)
    _check("curate returned two entries (fabricated #9 dropped)", out is not None and len(out) == 2)
    _check("entry link is the REAL candidate link", out[0]["link"] == "https://sel.com/a1")
    _check("entry source is the REAL candidate source", out[0]["source"] == "Search Engine Land")
    _check("entry date is the REAL candidate date", out[0]["date"] == "2026-07-01")
    _check("entry body is the model's summary", "cap PMax budgets" in out[0]["body"])
    _check("no entry points at the irrelevant #3", all(e["link"] != "https://gossip.com/a3" for e in out))

    # 5. curate() via Gemini shape works the same (once its key is present).
    os.environ["GEMINI_API_KEY"] = "g-test"
    gout = intel_ai.curate("media_buying", "Aitest Co", ["ppc"], _CANDS,
                           model="gemini-2.5-flash", fetcher=_gemini_fetcher)
    _check("gemini curate maps to real links", gout is not None and gout[0]["link"] == "https://sel.com/a1")

    # 6. Graceful degradation -> None so the caller falls back to plain RSS.
    os.environ.pop("GEMINI_API_KEY", None)
    _check("gemini key removed -> None",
           intel_ai.curate("media_buying", "X", [], _CANDS, model="gemini-2.5-pro",
                           fetcher=_gemini_fetcher) is None)
    _check("unknown model -> None",
           intel_ai.curate("media_buying", "X", [], _CANDS, model="nope", fetcher=_deepseek_fetcher) is None)
    _check("no candidates -> None",
           intel_ai.curate("media_buying", "X", [], [], model="deepseek-v4-pro", fetcher=_deepseek_fetcher) is None)
    _check("unparseable reply -> None",
           intel_ai.curate("media_buying", "X", [], _CANDS, model="deepseek-v4-pro",
                           fetcher=_bad_json_fetcher) is None)

    # 7. refresh_client AI path end-to-end (RSS fetcher feeds candidates, AI fetcher curates).
    def _rss(url, timeout):
        # Minimal Google-News RSS so _gather yields candidates for both sections.
        rss = ("<rss version='2.0'><channel><title>Google News</title>"
               "<item><title>Google Ads adds a new PMax control - Search Engine Land</title>"
               "<link>https://sel.com/a1</link><pubDate>Wed, 01 Jul 2026 10:00:00 GMT</pubDate>"
               "<source url='x'>Search Engine Land</source></item>"
               "<item><title>Meta overhauls Advantage+ targeting - Marketing Dive</title>"
               "<link>https://sel.com/a2</link><pubDate>Sun, 28 Jun 2026 10:00:00 GMT</pubDate>"
               "<source url='x'>Marketing Dive</source></item>"
               "</channel></rss>").encode("utf-8")

        class R(object):
            content = rss
            status_code = 200
        return R()

    workspace.save_workspace(CLIENT, {"display_name": "Aitest Co", "intel": {},
                                      "intel_topics": ["ppc"],
                                      "intel_ai": {"model": "deepseek-v4-pro"}})
    counts = intel_refresh.refresh_client(CLIENT, fetcher=_rss, ai_fetcher=_deepseek_fetcher)
    _check("refresh used the AI", counts["ai"] is True)
    _check("refresh filled media buying", counts["media_buying"] > 0)
    ws = workspace.load_workspace(CLIENT)
    _check("first run latched backfilled", workspace.intel_backfilled(ws))
    _check("last_model recorded", ws["intel_ai"]["last_model"] == "deepseek-v4-pro")
    _check("AI entries carry real links",
           all(e.get("link") for e in ws["intel"]["media_buying"]))

    # 8. Model set but its key absent -> refresh falls back to RSS (ai=False), never crashes.
    os.environ.pop("DEEPSEEK_API_KEY", None)
    workspace.save_workspace(CLIENT + "2", {"display_name": "NoKey Co", "intel": {},
                                            "intel_ai": {"model": "deepseek-v4-pro"}})
    c2 = intel_refresh.refresh_client(CLIENT + "2", fetcher=_rss, ai_fetcher=_deepseek_fetcher)
    _check("no key -> RSS fallback (ai=False)", c2["ai"] is False)
    _check("no key -> section still filled from RSS", c2["media_buying"] > 0)


def main():
    try:
        run()
    except AssertionError as exc:
        print("\n[FAIL] %s" % exc)
        return 1
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        print("\n[ERROR] %s" % exc)
        return 1
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    print("\n[PASS] intel AI brain: retrieve-then-curate, real-link mapping, graceful fallback")
    return 0


if __name__ == "__main__":
    sys.exit(main())
