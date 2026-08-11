"""Ask Sentinel whether a verified email is an active user.

The portal is the ONE app that runs Google OAuth, but Sentinel is where staff are added/assigned
(People -> Add Employee). This module lets the portal DEFER to Sentinel on a verified email it
doesn't already know locally: added-in-Sentinel therefore means can-sign-in-with-Google, and
deactivated-in-Sentinel means blocked -- with no copy of the user duplicated into the portal
registry (Sentinel stays the single source of truth).

Transport reuses the exact HMAC pattern the mastery engine already uses against Sentinel's
`/api/internal/*` endpoints: an HMAC-SHA256 signature over `"user-lookup:{ts}"` with the shared
secret both apps mount (Secret Manager `platform-sso-key`). No new secret, no CORS, no browser
credentials, a 5-minute replay window on Sentinel's side.

Everything here is best-effort and gated: if the secret or Sentinel URL is unset, or Sentinel is
unreachable / slow / returns non-200, `lookup_user` returns None so the caller simply falls through
to its existing behavior. A default or local deploy is unaffected; a Sentinel outage can never break
portal login.

🔴 None means "no answer", NOT "denied" — and the two must never be conflated. Sentinel is a Cloud
Run service over Cloud SQL: a cold start (scale-from-zero + a fresh DB connection) routinely takes
longer than the fast-path timeout, so the FIRST lookup for a staff-only login can fail while the
retry seconds later succeeds. Two defenses against bouncing a real staff member to the
request-access page on that transient:

1. `lookup_user` RETRIES a transport failure / 5xx once with a much longer timeout
   (`_RETRY_TIMEOUT_SECONDS`), which covers the cold start.
2. `user_status` is TRI-STATE ("active" / "denied" / "unknown") so the caller can tell a real
   "this email is nobody" from "Sentinel didn't answer" — and only the former routes to
   request-access (see `main._resolve_login_email`).
"""
from __future__ import annotations

import hashlib
import hmac
import os
import time

import requests

# The shared HMAC secret (same one used to sign `ag_sso`). Read at call time so tests/deploys that
# set it after import still work.
_TIMEOUT_SECONDS = 3
# A cold Sentinel routinely outlives the fast-path timeout (Cloud Run scale-from-zero + a Cloud SQL
# reconnect), so a transport failure / 5xx is retried ONCE with a generous window. That turns a
# spurious "no answer -> request access" bounce into a slow-but-successful login. A clean 4xx/200 is
# a definitive answer and is never retried. The pause is module-level so tests can zero it.
_RETRY_TIMEOUT_SECONDS = 12
_RETRY_PAUSE_SECONDS = 1
_PURPOSE = "user-lookup"


def _secret() -> str:
    return (os.environ.get("SSO_SECRET", "") or "").strip()


# The portal's canonical Sentinel host. Kept in sync with main.py's SENTINEL_URL default: neither is
# set as an env var in the deploy, so we must carry the custom-domain default here too, or the
# Sentinel fallback silently no-ops (SENTINEL_URL="" -> no base -> lookup returns None).
_DEFAULT_SENTINEL_BASE = "https://sentinel.agoradatadriven.com"


def _api_base() -> str:
    """Base URL for Sentinel's API (no trailing slash).

    Prefers an explicit SENTINEL_API_URL; otherwise derives from SENTINEL_URL (which points at the
    login page, e.g. https://sentinel.agoradatadriven.com/login) by stripping a trailing /login;
    otherwise falls back to the known custom domain so the fallback works even when neither env var
    is set (which is the case in the live deploy — main.py relies on the same code default).
    """
    base = (os.environ.get("SENTINEL_API_URL", "") or "").strip()
    if not base:
        base = (os.environ.get("SENTINEL_URL", "") or "").strip()
        if base.endswith("/login"):
            base = base[: -len("/login")]
    if not base:
        base = _DEFAULT_SENTINEL_BASE
    return base.rstrip("/")


def _attempt(base, secret, norm, timeout):
    """One signed lookup call. Returns (data_or_None, retryable): `data` is Sentinel's dict on a
    200, else None; `retryable` is True only for a transport failure or a 5xx (a cold start /
    outage), never for a definitive 4xx answer."""
    ts = str(int(time.time()))
    sig = hmac.new(secret.encode(), f"{_PURPOSE}:{ts}".encode(), hashlib.sha256).hexdigest()
    try:
        resp = requests.get(
            f"{base}/api/internal/user-lookup",
            params={"email": norm},
            headers={"X-Academy-Ts": ts, "X-Academy-Sig": sig},
            timeout=timeout,
        )
    except requests.RequestException:
        return None, True
    if resp.status_code != 200:
        return None, resp.status_code >= 500
    try:
        data = resp.json()
    except ValueError:
        return None, False
    if not isinstance(data, dict):
        return None, False
    return data, False


def lookup_user(email):
    """Return Sentinel's record for `email` as a dict, or None.

    dict shape (on a successful call): {"found": bool, "active": bool, "name": str, "role": str}.
    None means "couldn't ask / not configured / error" -- the caller should treat it as "no answer",
    NOT as "denied". Callers key their allow decision off dict["active"] being True.

    A transport failure / 5xx is retried once with `_RETRY_TIMEOUT_SECONDS`: Sentinel's cold start
    (scale-from-zero + Cloud SQL reconnect) regularly outlives the fast-path timeout, and the retry
    is what keeps a staff-only Google login from flapping to the request-access page.
    """
    norm = (email or "").strip().lower()
    if not norm:
        return None
    secret = _secret()
    base = _api_base()
    if not secret or not base:
        return None
    data, retryable = _attempt(base, secret, norm, _TIMEOUT_SECONDS)
    if data is None and retryable:
        if _RETRY_PAUSE_SECONDS:
            time.sleep(_RETRY_PAUSE_SECONDS)
        data, _ = _attempt(base, secret, norm, _RETRY_TIMEOUT_SECONDS)
    return data


# --- The STAFF ROSTER (2026-08-11) --------------------------------------------------------------
_PEOPLE_PURPOSE = "academy-people"
# 🔴 DELIBERATELY FAIL-FAST AND NEVER RETRIED, unlike `lookup_user` above — and the asymmetry is the
# point. There, "no answer" costs a real staff member their login, so the retry is worth 12 seconds.
# Here the fallback is the roster Atrium was using anyway (`main._team_roster`'s local sources), which
# is perfectly usable — so a slow Sentinel must cost the operator console nothing. `/admin/atrium`
# ALREADY blocks on `sentinel_board.fetch_board()` (6s + a 12s retry); adding a second blocking
# retry to the same render is how a console load reaches 30 seconds.
_PEOPLE_TIMEOUT_SECONDS = 3
# One call per process per TTL. `_team_roster()` is called several times in a single request (the
# board, the person filter, the internal task envelope), and it must never be several HTTP calls.
_PEOPLE_TTL_SECONDS = 300
_people_cache: dict = {"at": 0.0, "people": None}


def list_people(force: bool = False):
    """Active Sentinel users as [{email, name, role}], or None for "couldn't ask".

    This is Sentinel's `GET /api/internal/people` (purpose `academy-people`), whose own docstring
    calls it "for a sister app's person picker" — which is exactly this. Sentinel is the source of
    truth for STAFF (its `users` table authorizes every login), so the delivery board's roster
    belongs here rather than in a hand-edited tuple that drifts.

    🔴 None is "no answer", never "the agency has no staff" — the same tri-state contract as
    `lookup_user`. A caller that read `[]` as an answer would empty the board's person filter during
    a Sentinel blip. `[]` is a real (if implausible) answer and is cached like any other.

    Cached for `_PEOPLE_TTL_SECONDS`. A roster changes when somebody is hired; a five-minute lag is
    invisible, and the alternative is an HTTP call inside a template render.
    """
    now = time.time()
    if not force and _people_cache["people"] is not None \
            and (now - _people_cache["at"]) < _PEOPLE_TTL_SECONDS:
        return _people_cache["people"]
    secret, base = _secret(), _api_base()
    if not secret or not base:
        return None
    ts = str(int(now))
    sig = hmac.new(secret.encode(), f"{_PEOPLE_PURPOSE}:{ts}".encode(), hashlib.sha256).hexdigest()
    try:
        resp = requests.get(
            f"{base}/api/internal/people",
            headers={"X-Academy-Ts": ts, "X-Academy-Sig": sig},
            timeout=_PEOPLE_TIMEOUT_SECONDS,
        )
    except requests.RequestException:
        return None
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    rows = data.get("people") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return None
    people = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        email = (r.get("email") or "").strip()
        if not email:
            continue          # an id-less person cannot be assigned work or filtered on
        people.append({"email": email,
                       "name": (r.get("name") or "").strip() or email.split("@")[0],
                       "role": (r.get("role") or "").strip()})
    _people_cache["at"], _people_cache["people"] = now, people
    return people


def user_status(email) -> str:
    """Tri-state answer to 'may this verified email sign in?': "active", "denied", or "unknown".

    "unknown" means Sentinel gave NO ANSWER (unconfigured, unreachable, timed out, non-200) even
    after the retry. It must NEVER be treated as a denial -- only a definitive "denied" routes a
    Google login to the request-access page (see `main._resolve_login_email`).
    """
    data = lookup_user(email)
    if data is None:
        return "unknown"
    if data.get("found") and data.get("active"):
        return "active"
    return "denied"
