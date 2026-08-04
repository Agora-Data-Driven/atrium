"""File a CLIENT'S ASK into Sentinel's intake queue (decision D3 / WP 3.3 of
sentinel/docs/TASKBOARD_REBUILD.md).

🔴 Why this exists. The Progress tab's quick-add composer used to call `workspace.add_task`, so
anything a client typed during a live call became a card on the delivery board immediately —
unowned, unestimated, unscheduled, and indistinguishable from work the agency had actually
committed to. The board stopped meaning "what we are doing". Sentinel owns delivery now (D1/D2),
so the ask is FILED there and a human turns it into a task by accepting it. The client keeps the
one thing they genuinely use — capturing an ask mid-call — without writing onto the board.

Transport is the exact HMAC pattern `sentinel_directory` already uses (and the mastery engine
before it): HMAC-SHA256 over `"task-request:{ts}"` with the shared `platform-sso-key` both apps
mount. No new secret, no CORS, no browser credentials, a 5-minute replay window on Sentinel's side.

🔴 `source_ref` is REQUIRED for correctness, not for tidiness. This call is retried, and a client
on a bad connection will tap Send twice; Sentinel de-dupes on this id so the same ask cannot be
filed twice. It is minted from the workspace key + the request's own content + minute, so a genuine
second ask ("add a banner" twice, deliberately) still gets through a minute later.

Everything is gated and best-effort in the sense that a missing secret/URL is reported honestly —
but UNLIKE the login lookup, a failure here is NOT swallowed: if we cannot file the ask, the client
must be told, or they will believe the agency has it. `file_request` returns (ok, error).
"""
from __future__ import annotations

import hashlib
import hmac
import os
import time

import requests

_FAST_TIMEOUT_SECONDS = 4
# Sentinel is Cloud Run over Cloud SQL: a scale-from-zero cold start routinely outruns the fast
# path. Retried ONCE with a long timeout — the same cold-start defence sentinel_directory uses.
_RETRY_TIMEOUT_SECONDS = 15
_PURPOSE = "task-request"


def _secret() -> str:
    return (os.environ.get("PLATFORM_SSO_SECRET") or os.environ.get("SSO_SECRET") or "").strip()


def _base_url() -> str:
    """Sentinel's origin. SENTINEL_API_URL wins; otherwise SENTINEL_URL minus its /login suffix
    (that env var points at the sign-in page, not the API root)."""
    api = (os.environ.get("SENTINEL_API_URL") or "").strip()
    if api:
        return api.rstrip("/")
    base = (os.environ.get("SENTINEL_URL") or "").strip().rstrip("/")
    if base.endswith("/login"):
        base = base[: -len("/login")]
    return base


def configured() -> bool:
    return bool(_secret() and _base_url())


def source_ref(client_key: str, title: str, note: str = "") -> str:
    """A stable id for THIS ask, so a retry or a double-tap files once.

    Includes the minute so that deliberately repeating the same request later still lands: the
    duplicate we must stop is a resend seconds apart, not the same words next week.
    """
    minute = int(time.time() // 60)
    raw = f"{client_key}|{title.strip().lower()}|{note.strip().lower()}|{minute}"
    return "atr_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


def send_feedback(atrium_task_id: str, body: str, *, client_key: str = "", kind: str = "comment",
                  author_name: str = "", source_ref: str = "") -> tuple[bool, str]:
    """Push a client's comment / change request onto the linked Sentinel row (D4 / WP 3.5).

    🔴 BEST-EFFORT, unlike `file_request`. The client's words are already saved HERE the moment
    this is called — the workspace comment is written first and is the record. This call is the
    notification to the delivery side, so a Sentinel outage must not fail the client's comment or
    lose it: the caller ignores the result and the client sees their message posted, which is
    true. (Contrast the intake queue, where Sentinel IS the only store and silence would be a lie.)

    `source_ref` is Atrium's comment id, which makes the far side idempotent — without it a retry
    would keep raising the change-request counter and the pill could never honestly clear.
    """
    if not configured():
        return False, "not configured"
    ts = str(int(time.time()))
    sig = hmac.new(_secret().encode(), f"task-feedback:{ts}".encode(), hashlib.sha256).hexdigest()
    payload = {
        "atrium_task_id": atrium_task_id,
        # Atrium's task ids are only unique WITHIN a workspace, so the key is what stops two
        # clients' cards colliding on the far side.
        "client": client_key,
        "body": body[:4000],
        "kind": "changes" if kind == "changes" else "comment",
        "author_name": author_name[:160],
        "source_ref": source_ref,
    }
    try:
        resp = requests.post(f"{_base_url()}/api/internal/task-feedback", json=payload,
                             headers={"X-Academy-Ts": ts, "X-Academy-Sig": sig},
                             timeout=_FAST_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        return False, str(exc)
    return (resp.status_code == 200), ("" if resp.status_code == 200 else f"HTTP {resp.status_code}")


def file_request(client_key: str, title: str, *, details: str = "",
                 requester_name: str = "", requester_email: str = "") -> tuple[bool, str]:
    """POST the ask to Sentinel. Returns (ok, error_message).

    🔴 A failure is RETURNED, never swallowed. The login lookup can degrade silently because a
    missing answer there just means "fall through"; here, silence would tell a client their request
    was received when nobody has it.
    """
    if not configured():
        return False, "Requests aren't connected yet — tell your account manager directly."
    ts = str(int(time.time()))
    sig = hmac.new(_secret().encode(), f"{_PURPOSE}:{ts}".encode(), hashlib.sha256).hexdigest()
    payload = {
        "client": client_key,
        "title": title[:200],
        "details": details[:2000],
        "requester_name": requester_name[:160],
        "requester_email": requester_email[:200],
        "source_ref": source_ref(client_key, title, details),
    }
    url = f"{_base_url()}/api/internal/task-request"
    headers = {"X-Academy-Ts": ts, "X-Academy-Sig": sig}

    for timeout in (_FAST_TIMEOUT_SECONDS, _RETRY_TIMEOUT_SECONDS):
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
        except requests.RequestException:
            continue                      # transport failure: retry once, then give up
        if resp.status_code == 200:
            return True, ""
        # A definitive 4xx is the caller's fault and will fail identically on a retry.
        if 400 <= resp.status_code < 500:
            try:
                return False, str(resp.json().get("detail") or "That request was rejected.")
            except ValueError:
                return False, "That request was rejected."
        # 5xx falls through to the retry, then out.
    return False, "Couldn't reach the team's system just now. Please try again in a moment."
