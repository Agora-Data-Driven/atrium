"""Read SENTINEL's delivery board, for the operator console's Task Board + Delivery Calendar.

🔴 Why this module exists — the bug it removes. `/admin/atrium` → Task Board used to be assembled
from `ws["tasks"]` of every client workspace. But since decision D2 (2026-08-03) a task is OWNED by
Sentinel and `ws["tasks"]` holds only the **client-safe projection** Sentinel pushes over the bridge
— a copy that exists at all only once somebody hit Send to Atrium and the client had an
`atrium_client_id` to publish into. So every unpublished row was structurally invisible on a board
whose own subtitle says "every client deliverable across every workspace", and the console
disagreed with Sentinel about how much work the agency had. Nothing was broken in the render; the
console was reading the wrong source.

It reads Sentinel's `GET /api/internal/board` instead. That is a STAFF surface on both ends: this
one is behind `is_superadmin()`, and the payload carries assignees, priorities, service charges,
internal notes and hold reasons. It is the exact opposite of the client bridge — nothing here may
ever reach a `/w/<client>` template, which still reads `ws["tasks"]` and is untouched.

Transport is the platform's existing server-to-server HMAC, identical to `sentinel_directory` and
`sentinel_requests`: HMAC-SHA256 over `"board:{ts}"` with the shared `platform-sso-key` both apps
mount. No new secret, no CORS, no browser credentials, a 5-minute replay window on Sentinel's side.

🔴 FAIL-SOFT, and `None` is not `[]`. A Sentinel outage must not blank the console — but it must not
look like an empty board either, because "the agency has no work" is a claim, and the console draws
counts off it. `fetch_board()` returns None for "couldn't ask" so the caller can fall back to the
projections it still holds locally AND say so, versus [] meaning Sentinel genuinely has no tasks.
Same reasoning as `sentinel_directory.user_status`'s tri-state, and the same cold-start retry:
Sentinel is Cloud Run over Cloud SQL, so the first call after a scale-from-zero routinely outlives a
short timeout.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time

import requests

log = logging.getLogger(__name__)

# 🔴 These are deliberately TIGHTER than sentinel_directory's, and the reason is the caller. This
# call blocks a PAGE RENDER — `/admin/agora` also draws clients, accounts, activity and the bin — so
# the cost of waiting is paid by an operator staring at a blank console, whereas a slow login lookup
# was the difference between signing in and being told to request access. One retry, because a cold
# Sentinel (Cloud Run scale-from-zero + a Cloud SQL reconnect) genuinely does outlive the fast path
# and the first console load of the morning is exactly when it happens; a short one, because the
# fallback is honest and reloading is one keystroke.
#
# The real fix for the blocking call is to load this pane over fetch() instead of server-rendering
# it, which is a bigger change than this one: `task_cols` also feeds #tk-store, #cal-store and the
# swimlanes builder. Worst case here is bounded at ~18s and only on a cold Sentinel.
_TIMEOUT_SECONDS = 6
_RETRY_TIMEOUT_SECONDS = 12
_PURPOSE = "board"

# Kept in step with sentinel_directory._DEFAULT_SENTINEL_BASE: neither SENTINEL_URL nor
# SENTINEL_API_URL is set in the live deploy, so without this default the console would silently
# fall back to the projections forever.
_DEFAULT_SENTINEL_BASE = "https://sentinel.agoradatadriven.com"


def _secret() -> str:
    return (os.environ.get("PLATFORM_SSO_SECRET") or os.environ.get("SSO_SECRET") or "").strip()


def _api_base() -> str:
    """Sentinel's origin. SENTINEL_API_URL wins; else SENTINEL_URL minus its /login suffix (that
    env var points at the sign-in page, not the API root); else the known custom domain."""
    base = (os.environ.get("SENTINEL_API_URL", "") or "").strip()
    if not base:
        base = (os.environ.get("SENTINEL_URL", "") or "").strip()
        if base.endswith("/login"):
            base = base[: -len("/login")]
    if not base:
        base = _DEFAULT_SENTINEL_BASE
    return base.rstrip("/")


def configured() -> bool:
    """Can we ask at all? `SENTINEL_BOARD_MIRROR=0` is the kill switch.

    It exists for one scenario that has happened on this estate before: Sentinel is down hard, every
    console load spends the full timeout, and the operator needs the rest of the page NOW. Setting it
    drops the console back to the client-safe projections immediately — which the pane then says out
    loud, so nobody mistakes the short board for the whole one.
    """
    if (os.environ.get("SENTINEL_BOARD_MIRROR", "") or "").strip() in ("0", "false", "off"):
        return False
    return bool(_secret() and _api_base())


def _attempt(base, secret, timeout):
    """One signed call. Returns (tasks_or_None, retryable) — `retryable` is True only for a
    transport failure or a 5xx (a cold start / outage), never for a definitive 4xx."""
    ts = str(int(time.time()))
    sig = hmac.new(secret.encode(), f"{_PURPOSE}:{ts}".encode(), hashlib.sha256).hexdigest()
    try:
        resp = requests.get(
            f"{base}/api/internal/board",
            headers={"X-Academy-Ts": ts, "X-Academy-Sig": sig},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        log.warning("sentinel board fetch failed: %s", exc)
        return None, True
    if resp.status_code != 200:
        # 🔴 A 404 here means "Sentinel isn't deployed with this endpoint yet", NOT "no tasks" —
        # the same read-a-404-correctly trap sentinel/atrium_tasks._gone_or_missing_route documents.
        # Either way it is not an answer, so it degrades to None and never to an empty board.
        log.warning("sentinel board fetch: HTTP %s", resp.status_code)
        return None, resp.status_code >= 500
    try:
        data = resp.json()
    except ValueError:
        return None, False
    if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
        return None, False
    return data["tasks"], False


def fetch_board():
    """Sentinel's live tasks as a list of Atrium-shaped task dicts, or None.

    None means "no answer" (unconfigured, unreachable, non-200, malformed) and must NOT be read as
    an empty board — see the module docstring. [] is a real answer.
    """
    if not configured():
        return None
    secret, base = _secret(), _api_base()
    tasks, retryable = _attempt(base, secret, _TIMEOUT_SECONDS)
    if tasks is None and retryable:
        tasks, _ = _attempt(base, secret, _RETRY_TIMEOUT_SECONDS)
    return tasks
