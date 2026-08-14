"""Route-level smoke test for the auth foundation (Google sign-in, request-access, grant, impersonate,
legacy-redirect). Mirrors _atrium_smoketest.py: stubs google.cloud.storage, points the registry +
workspace at a temp dir, and drives Flask's test client. The Google token exchange is monkeypatched
(no network); everything else is the real app.

Run:  python _auth_smoketest.py   (exit 0 = pass)
"""

import os
import sys
import tempfile
import types

# 1. Stub google.cloud.storage BEFORE importing main.
_g = types.ModuleType("google"); _g.__path__ = []
_gc = types.ModuleType("google.cloud"); _gc.__path__ = []
_gs = types.ModuleType("google.cloud.storage")


class _FakeClient:
    def __init__(self, *a, **k):
        pass

    def bucket(self, *a, **k):
        raise RuntimeError("GCS disabled in smoke test (use the local backend)")


_gs.Client = _FakeClient
sys.modules.setdefault("google", _g)
sys.modules.setdefault("google.cloud", _gc)
sys.modules["google.cloud.storage"] = _gs

# 2. Env: local backend + a signed session + Google OAuth configured so the button/routes are live.
_TMP = tempfile.mkdtemp(prefix="auth_smoke_")
os.environ["WORKSPACE_LOCAL_DIR"] = _TMP
os.environ["REGISTRY_LOCAL_DIR"] = _TMP
os.environ["SESSION_SECRET"] = "test-secret"
# Set so `_mint_sso_on` actually mints: the ag_sso re-mint checks below are about the ONE cookie every
# sibling app (Sentinel, the dashboards, the website editor) trusts, and with no secret it no-ops.
os.environ["SSO_SECRET"] = "test-sso-secret"
os.environ["GOOGLE_OAUTH_CLIENT_ID"] = "cid.apps.googleusercontent.com"
os.environ["GOOGLE_OAUTH_CLIENT_SECRET"] = "csecret"
os.environ["GOOGLE_OAUTH_REDIRECT_URI"] = "https://portal.agoradatadriven.com/auth/google/callback"

import main   # noqa: E402
import store  # noqa: E402

SUPER = {"ok": True, "user": "info@agoradatadriven.com", "clients": ["*"]}
FAILS = []


def _check(label, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + label)
    if not cond:
        FAILS.append(label)


def _google_state(c):
    """Drive /auth/google/login so the session carries a fresh oauth_state, and return it."""
    r = c.get("/auth/google/login?next=/")
    assert r.status_code == 302 and "accounts.google.com" in r.headers["Location"], r.headers.get("Location")
    with c.session_transaction() as s:
        return s.get("oauth_state")


def run():
    main.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False, SESSION_COOKIE_SAMESITE="Lax")
    c = main.app.test_client()

    # --- Login page shows the Google button when configured ------------------------------------
    body = c.get("/login").get_data(as_text=True)
    _check("login page shows 'Sign in with Google'", "Sign in with Google" in body)
    _check("login page links the Google route", "/auth/google/login" in body)

    # --- /auth/google/login redirects to Google with a state ----------------------------------
    r = c.get("/auth/google/login?next=/")
    loc = r.headers.get("Location", "")
    _check("google login -> 302 to Google", r.status_code == 302 and loc.startswith(main.google_oauth.AUTH_ENDPOINT))
    _check("google login carries client_id + state", "client_id=cid" in loc and "state=" in loc)

    # --- Callback for an UNKNOWN email -> request-access page ----------------------------------
    # Stub the staff directory to a DEFINITIVE "denied" -- only a real answer may route to
    # request-access. Unstubbed it returns "unknown" (no secret configured off-cloud), which is the
    # retry path, not this one.
    main.sentinel_directory.user_status = lambda e: "denied"
    main.google_oauth.exchange_code = lambda code, redirect, **k: ("stranger@gmail.com", None)
    state = _google_state(c)
    body = c.get("/auth/google/callback?state=%s&code=x" % state).get_data(as_text=True)
    _check("unknown email -> request access page", "Request access" in body and "Send request" in body)
    _check("request access page shows the email", "stranger@gmail.com" in body)

    # --- Request access files a pending request that shows in the console ----------------------
    c.post("/auth/request-access", data={"email": "stranger@gmail.com", "message": "let me in"})
    pend = store.get_account("stranger@gmail.com")
    _check("request-access created a pending account", pend is not None and pend.get("status") == "pending")
    _check("pending account is passwordless (Google)", "pw_hash" not in (pend or {}))

    # --- Callback for a KNOWN active account -> logged in --------------------------------------
    store.upsert_google_account("owner@gmail.com", name="Owner", role="client",
                                clients=["riverdance"], status="active")
    main.google_oauth.exchange_code = lambda code, redirect, **k: ("owner@gmail.com", None)
    state = _google_state(c)
    r = c.get("/auth/google/callback?state=%s&code=x" % state)
    _check("known email -> 302 (signed in)", r.status_code == 302)
    with c.session_transaction() as s:
        _check("session established for the Google user", s.get("user") == "owner@gmail.com")
        _check("session granted the account's client", s.get("clients") == ["riverdance"])

    # --- Sentinel is the source of truth: an active Sentinel user (no portal account) signs in ---
    # The portal defers to Sentinel for a verified email it doesn't already know locally, so adding
    # someone in Sentinel (People -> Add Employee) is all it takes to enable their Google login.
    _sentinel_active = {"staff@agora.ph"}
    main.sentinel_directory.user_status = (
        lambda e: "active" if (e or "").strip().lower() in _sentinel_active else "denied")
    main.google_oauth.exchange_code = lambda code, redirect, **k: ("staff@agora.ph", None)
    state = _google_state(c)
    r = c.get("/auth/google/callback?state=%s&code=x" % state)
    _check("active Sentinel user (no portal account) -> 302 (signed in)", r.status_code == 302)
    with c.session_transaction() as s:
        _check("session established for the Sentinel user", s.get("user") == "staff@agora.ph")
        _check("Sentinel staff granted no client dashboards ([])", s.get("clients") == [])
    _check("no portal account was created for the Sentinel user",
           store.get_account("staff@agora.ph") is None)

    # --- A user NOT active in Sentinel (and not in the portal) is still routed to request-access --
    main.google_oauth.exchange_code = lambda code, redirect, **k: ("outsider@agora.ph", None)
    state = _google_state(c)
    body = c.get("/auth/google/callback?state=%s&code=x" % state).get_data(as_text=True)
    _check("non-Sentinel, non-portal email -> request access page",
           "Request access" in body and "outsider@agora.ph" in body)

    # --- Sentinel gave NO ANSWER (cold start / outage) -> retry, NOT request-access ---------------
    # A Sentinel cold start (scale-from-zero + Cloud SQL reconnect) used to look identical to "this
    # email is nobody", so a real staff member was intermittently shown Request access and only got
    # in by trying again. "unknown" must never be read as a denial.
    main.sentinel_directory.user_status = lambda e: "unknown"
    main.google_oauth.exchange_code = lambda code, redirect, **k: ("staff@agora.ph", None)
    with c.session_transaction() as s:
        s.clear()                      # the staff sign-in above left a live session
    state = _google_state(c)
    r = c.get("/auth/google/callback?state=%s&code=x" % state)
    body = r.get_data(as_text=True)
    _check("directory outage -> 503, not a sign-in", r.status_code == 503)
    # NOTE: login.html carries its own "Request access" link to /signup, so match the
    # request_access PAGE by its own headline instead.
    _check("directory outage -> NOT the request-access page",
           "no Agora account for this Google address" not in body)
    _check("directory outage -> asks the user to retry", "try signing in again" in body)
    with c.session_transaction() as s:
        _check("directory outage established no session", not s.get("ok"))

    # Restore the default (no Sentinel) for the remaining tests.
    main.sentinel_directory.user_status = lambda e: "denied"

    # --- Callback with a BAD state is rejected ------------------------------------------------
    _google_state(c)  # set a real state, then send a wrong one
    r = c.get("/auth/google/callback?state=WRONG&code=x")
    _check("bad state rejected (400)", r.status_code == 400)

    # --- As super admin: legacy /admin + /superadmin redirect to the console ------------------
    with c.session_transaction() as s:
        s.clear(); s.update(SUPER)
    _check("/admin -> 302 /admin/atrium",
           c.get("/admin").headers.get("Location", "").endswith("/admin/atrium"))
    _check("/superadmin -> 302 /admin/atrium",
           c.get("/superadmin").headers.get("Location", "").endswith("/admin/atrium"))

    # --- "/" has NO page of its own (the portal landing was deleted 2026-07-31) ---------------
    # It only decides where you belong: console for the team, their own workspace for a client with
    # exactly one, the marketing site for everyone else (a Sentinel staffer with no dashboards).
    r = c.get("/")
    _check("/ as super admin -> 302 console",
           r.status_code == 302 and r.headers.get("Location", "").endswith("/admin/atrium"))
    main.workspace.save_workspace("riverdance", {"display_name": "Riverdance"})
    with c.session_transaction() as s:
        s.clear(); s.update({"ok": True, "user": "owner@gmail.com", "clients": ["riverdance"]})
    r = c.get("/")
    _check("/ as a single-client login -> 302 that workspace",
           r.status_code == 302 and r.headers.get("Location", "").endswith("/w/riverdance/dashboard"))
    with c.session_transaction() as s:
        s.clear(); s.update({"ok": True, "user": "staff@agora.ph", "clients": []})
    r = c.get("/")
    _check("/ with no dashboards -> 302 the marketing site",
           r.status_code == 302 and r.headers.get("Location", "") == main.WEBSITE_HOME_URL)
    with c.session_transaction() as s:
        s.clear()
    r = c.get("/")
    _check("/ signed out -> 302 the login page (NOT the marketing site)",
           r.status_code == 302 and "/login" in r.headers.get("Location", ""))
    with c.session_transaction() as s:
        s.clear(); s.update(SUPER)

    # --- Console renders the app suite (the "Switch app" dropdown) + the grant form ------------
    # (The old Home-hub suite cards were replaced by a Switch-app dropdown; "Skill Mastery" is now
    # reached from inside Sentinel/Academy rather than a top-level card.)
    body = c.get("/admin/atrium").get_data(as_text=True)
    _check("console renders the app suite",
           "Atrium Admin" in body and "Website Editor" in body and "Sentinel" in body
           and "Switch app" in body)
    _check("console shows Grant-Google form", "Grant Google access to a Gmail" in body)
    _check("console shows the pending request", "stranger@gmail.com" in body)

    # --- Grant a Gmail to an existing client via the console ----------------------------------
    store.add_client("acme", "Acme Co")
    r = c.post("/admin/accounts/grant-google",
               data={"email": "newperson@gmail.com", "assign": "acme", "name": "New Person"})
    _check("grant-google -> 302 back to console", r.status_code == 302)
    _check("grant created an active Google account scoped to the client",
           store.resolve_google_login("newperson@gmail.com", super_admin_email=main.SUPER_ADMIN_EMAIL) == ["acme"])

    # --- Grant activates a PENDING request in place (no duplicate) ----------------------------
    c.post("/admin/accounts/grant-google", data={"email": "stranger@gmail.com", "assign": "acme"})
    strangers = [a for a in store.list_accounts() if a.get("email") == "stranger@gmail.com"]
    _check("granting a pending request activates it in place (one row)", len(strangers) == 1)
    _check("granted request is now active", strangers and strangers[0].get("status") == "active")

    # --- Impersonation: super admin acts as the client account, banner appears, then stops ----
    r = c.post("/admin/impersonate", data={"email": "owner@gmail.com"})
    _check("impersonate -> 302", r.status_code == 302)
    with c.session_transaction() as s:
        _check("session now the target user", s.get("user") == "owner@gmail.com")
        _check("impersonator remembered", s.get("impersonator") == "info@agoradatadriven.com")
    # Any HTML page now carries the injected 'Stop acting as' banner. (/profile, not "/" -- the
    # portal landing page was deleted 2026-07-31 and "/" is now a bare redirect with no HTML.)
    body = c.get("/profile").get_data(as_text=True)
    _check("impersonation banner injected on pages", "Stop acting as" in body and "Acting as" in body)
    r = c.post("/admin/stop-impersonating")
    _check("stop-impersonating -> 302", r.status_code == 302)
    with c.session_transaction() as s:
        _check("identity restored to the super admin", s.get("user") == "info@agoradatadriven.com")
        _check("impersonator cleared", s.get("impersonator") is None)

    # --- A regular client cannot impersonate --------------------------------------------------
    with c.session_transaction() as s:
        s.clear(); s.update({"ok": True, "user": "owner@gmail.com", "clients": ["riverdance"]})
    _check("non-root cannot impersonate (403)",
           c.post("/admin/impersonate", data={"email": "acme"}).status_code == 403)

    # --- An ALREADY-SIGNED-IN visitor to /login gets a FRESH ag_sso, not a bare redirect ---------
    # The 12h-vs-forever bug: this branch used to answer "you're already signed in, off you go" with
    # a redirect and NO cookie, while `ag_sso` expires after 12h and is minted nowhere else. A
    # sibling app that bounces here to GET that cookie was therefore sent straight back with nothing
    # fixed -> sentinel/login -> portal/login -> sentinel/login -> ... forever, with no form on
    # screen. Assert the cookie is really on the response; a 302 alone is what shipped the bug.
    def _sso_cookie(resp):
        for h in resp.headers.getlist("Set-Cookie"):
            if h.startswith(main.platform_sso.COOKIE_NAME + "="):
                return h.split("=", 1)[1].split(";", 1)[0]
        return None

    with c.session_transaction() as s:
        s.clear(); s.update(SUPER)
    nxt = "https://sentinel.agoradatadriven.com/login"
    r = c.get("/login?next=" + nxt)
    _check("authed GET /login -> 302 to next", r.status_code == 302 and r.headers.get("Location") == nxt)
    minted = _sso_cookie(r)
    _check("authed GET /login re-mints ag_sso", bool(minted))
    # `_verify` is this module's own verifier (the private name is the signer's; Sentinel's port
    # exposes it as `verify`). Checking the PAYLOAD, not just that a cookie was set: a cookie that
    # doesn't verify, or names nobody, would leave the far side exactly as stuck.
    payload = main.platform_sso._verify(os.environ["SSO_SECRET"], minted or "") or {}
    _check("the re-minted cookie verifies", bool(payload))
    _check("it names the signed-in user", payload.get("sub") == "info@agoradatadriven.com")
    _check("it carries the session's grants", payload.get("clients") == ["*"])
    _check("it is good for the full TTL, not the old cookie's leftovers",
           payload.get("exp", 0) - payload.get("iat", 0) == main.platform_sso.DEFAULT_TTL_SECONDS)

    # ?next= is attacker-controlled, so both login paths validate it (`_safe_next`) instead of
    # redirecting anywhere the query string names right after proving who you are.
    r = c.get("/login?next=https://evil.example.com/steal")
    _check("authed GET /login refuses an off-estate next",
           r.status_code == 302 and "evil.example.com" not in r.headers.get("Location", ""))
    _check("a relative next still works",
           c.get("/login?next=/admin/atrium").headers.get("Location") == "/admin/atrium")
    with main.app.test_request_context():  # it falls back to url_for(), which needs an app context
        _check("_post_login_destination drops an off-estate next",
               main._post_login_destination(["*"], "https://evil.example.com/x") != "https://evil.example.com/x")
        _check("_post_login_destination keeps an estate next",
               main._post_login_destination(["*"], nxt) == nxt)

    # --- ag_sso SLIDES on activity (2026-08-14) -------------------------------------------------
    # 🔴 The bug: ag_sso lives 12h and was minted ONLY by a login POST / the Google callback / the
    # already-authed /login bounce. Sentinel's own session is SEVEN DAYS, so ~12h in, a person still
    # signed into Sentinel lost the shared cookie underneath them and the embedded Mastery Engine
    # rendered its own login form inside the iframe ("mastery sometimes isn't signed in").
    print("\n-- ag_sso sliding window --")
    c2 = main.app.test_client()
    with c2.session_transaction() as s:
        s["ok"] = True
        s["user"] = "owner@gmail.com"
        s["clients"] = ["riverdance"]

    def _sso_header(resp):
        for h in resp.headers.getlist("Set-Cookie"):
            if h.startswith(main.platform_sso.COOKIE_NAME + "="):
                return h
        return None

    # `/` is an authed response that renders no workspace template — the hook is in
    # `_finalize_response`, so it applies to EVERY response, redirects included.
    r = c2.get("/")
    _check("an authed response mints ag_sso even though it is not /login", _sso_header(r) is not None)
    # Read the value out of the HEADER we just captured — Werkzeug's cookie-jar accessors differ
    # across versions, and the header is what the browser actually receives anyway.
    hdr = _sso_header(r) or ""
    raw = hdr.split("=", 1)[1].split(";")[0] if "=" in hdr else ""
    payload = main.platform_sso._verify(main.SSO_SECRET, raw)
    _check("the slid cookie verifies", bool(payload))
    _check("it names the signed-in user", (payload or {}).get("sub") == "owner@gmail.com")
    _check("it carries the session's grants", (payload or {}).get("clients") == ["riverdance"])
    _check("TTL is unchanged -- this slides the window, it does not lengthen it",
           (payload or {}).get("exp", 0) - (payload or {}).get("iat", 0)
           == main.platform_sso.DEFAULT_TTL_SECONDS)

    # THROTTLE: a second request moments later must NOT spend another Set-Cookie header.
    # 🔴 This is the check that found `SSO_REMINT_FLOOR_SECONDS`. The test client's host does not
    # match `COOKIE_DOMAIN` (.agoradatadriven.com), so the cookie never comes BACK — which is
    # exactly what a real misconfigured domain looks like. Without the floor, the "cookie is
    # missing → mint now" clause fired on every single response, forever.
    r2 = c2.get("/")
    _check("a second request within the floor does not re-mint", _sso_header(r2) is None)

    # ...but once the floor has passed, a browser that LOST the cookie gets one back WITHOUT waiting
    # out the full refresh interval — the throttle is bookkept in the session, so it would otherwise
    # go on believing the browser holds a cookie it does not.
    with c2.session_transaction() as s:
        s["sso_minted_at"] = int(__import__("time").time()) - (main.SSO_REMINT_FLOOR_SECONDS + 5)
    r3 = c2.get("/")
    _check("past the floor, a MISSING cookie is re-minted without waiting the full interval",
           _sso_header(r3) is not None)

    # 🔴 THE SECURITY CASE: logout must stay a logout. A blind after_request re-mint would hand the
    # cookie straight back on the very response that expires it.
    r4 = c2.get("/logout")
    hdr4 = _sso_header(r4) or ""
    _check("logout still EXPIRES ag_sso (the slide never re-mints it)",
           "Expires=Thu, 01 Jan 1970" in hdr4 or "Max-Age=0" in hdr4)
    _check("logout emits exactly ONE ag_sso header, not two that fight",
           len([h for h in r4.headers.getlist("Set-Cookie")
                if h.startswith(main.platform_sso.COOKIE_NAME + "=")]) == 1)

    # An anonymous visitor is never handed a credential.
    c3 = main.app.test_client()
    _check("an anonymous request is never minted a cookie", _sso_header(c3.get("/login")) is None)

    if FAILS:
        print("\n[auth-smoketest] FAIL (%d): %s" % (len(FAILS), ", ".join(FAILS)))
        return 1
    print("\n[auth-smoketest] PASS")
    return 0


if __name__ == "__main__":
    sys.exit(run())
