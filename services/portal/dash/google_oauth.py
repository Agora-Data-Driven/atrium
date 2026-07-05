"""Google Sign-In for the portal -- OAuth 2.0 authorization-code flow (pure stdlib + lazy requests).

Central auth: the portal is the ONE place that runs the Google flow. It resolves a *verified* Google
email and hands it to the normal account resolution in main.py (`_resolve_login_email`) -> the same
session + shared `.agoradatadriven.com` SSO cookie a password login mints, so the website editor and
every dashboard trust a Google login exactly like a password login.

Why no new dependency and no JWKS fetch:
  We use the confidential-client authorization-code flow. The `id_token` is exchanged DIRECTLY with
  Google's token endpoint over TLS, authenticated by our client_secret -- so the response itself is
  trusted. We decode the id_token payload for the email and DEFENSIVELY re-check iss / aud / exp /
  email_verified. (A public JWKS signature check would only matter if we accepted a token from an
  untrusted channel, which we never do.) The token exchange uses `requests`, already a portal dep.

OFF by default (opt-in, mirrors the other features): if GOOGLE_OAUTH_CLIENT_ID /
GOOGLE_OAUTH_CLIENT_SECRET are unset, `is_configured()` is False, the login page hides the button,
and the routes redirect back to the password login -- so a default deploy is unaffected.
"""

import base64
import json
import os
import secrets

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
# Google may stamp either form of the issuer; accept both.
_VALID_ISS = {"accounts.google.com", "https://accounts.google.com"}
# The default host the portal is served on (used to build the redirect URI when not set explicitly).
_DEFAULT_BASE = "https://portal.agoradatadriven.com"


def client_id():
    """The OAuth 2.0 client ID (Google Cloud Console -> Credentials), or "" when unset."""
    return os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "").strip()


def client_secret():
    """The OAuth 2.0 client secret, or "" when unset."""
    return os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "").strip()


def is_configured():
    """True iff both the client ID and secret are present -- otherwise Google sign-in stays off."""
    return bool(client_id() and client_secret())


def redirect_uri():
    """The OAuth redirect URI. MUST match one registered on the OAuth client EXACTLY.

    Prefers an explicit GOOGLE_OAUTH_REDIRECT_URI; otherwise derives it from PORTAL_BASE_URL (or the
    production default). Kept identical between the auth request and the token exchange -- Google
    rejects a mismatch.
    """
    env = os.environ.get("GOOGLE_OAUTH_REDIRECT_URI", "").strip()
    if env:
        return env
    base = os.environ.get("PORTAL_BASE_URL", _DEFAULT_BASE).strip().rstrip("/")
    return "%s/auth/google/callback" % base


def new_state():
    """A fresh, unguessable CSRF state token (stored in the session, echoed back by Google)."""
    return secrets.token_urlsafe(24)


def auth_url(state, redirect, login_hint=None):
    """Build the Google consent-screen URL to redirect the browser to."""
    from urllib.parse import urlencode  # lazy: only the redirect path needs it
    params = {
        "client_id": client_id(),
        "redirect_uri": redirect,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "access_type": "online",
        "prompt": "select_account",
    }
    if login_hint:
        params["login_hint"] = login_hint
    return "%s?%s" % (AUTH_ENDPOINT, urlencode(params))


def _b64d(seg):
    """URL-safe base64 decode of one JWT segment (adds the stripped '=' padding back)."""
    seg += "=" * (-len(seg) % 4)
    return base64.urlsafe_b64decode(seg)


def decode_id_token(id_token):
    """Return the JWT payload dict. No signature check -- see the module docstring (the token came
    straight from Google's TLS token endpoint, authenticated by our client secret). Raises
    ValueError on a structurally malformed token."""
    parts = str(id_token or "").split(".")
    if len(parts) != 3:
        raise ValueError("malformed id_token")
    try:
        return json.loads(_b64d(parts[1]))
    except Exception as e:  # noqa: BLE001 -- any decode/parse error is a bad token
        raise ValueError("undecodable id_token: %s" % e)


def _claims_ok(payload, now=None):
    """Defensive claim checks: issuer is Google, audience is us, and it hasn't expired."""
    import time  # lazy
    current = int(now if now is not None else time.time())
    if payload.get("iss") not in _VALID_ISS:
        return False
    if payload.get("aud") != client_id():
        return False
    if int(payload.get("exp", 0)) < current:
        return False
    return True


def exchange_code(code, redirect, fetcher=None, now=None):
    """Exchange an authorization `code` for the VERIFIED Google email.

    Returns (email_lowercased, None) on success, or (None, "reason") on any failure. `fetcher(url,
    data) -> dict` is injectable for tests; the default POSTs to Google's token endpoint via requests.
    """
    def _default_fetcher(url, data):
        import requests  # lazy: only the live path needs the network client
        resp = requests.post(url, data=data, timeout=15)
        return resp.json()

    fetch = fetcher or _default_fetcher
    if not code:
        return None, "no_code"
    try:
        tok = fetch(TOKEN_ENDPOINT, {
            "code": code,
            "client_id": client_id(),
            "client_secret": client_secret(),
            "redirect_uri": redirect,
            "grant_type": "authorization_code",
        })
    except Exception:  # noqa: BLE001 -- network/JSON failure -> treat as a failed exchange
        return None, "token_exchange_failed"
    id_token = (tok or {}).get("id_token")
    if not id_token:
        return None, (tok or {}).get("error") or "no_id_token"
    try:
        payload = decode_id_token(id_token)
    except ValueError:
        return None, "bad_id_token"
    if not _claims_ok(payload, now=now):
        return None, "claims_invalid"
    email = payload.get("email")
    if not email:
        return None, "no_email"
    # Google sends email_verified as a real bool; accept the string forms defensively too.
    if payload.get("email_verified") not in (True, "true", "True"):
        return None, "email_unverified"
    return email.strip().lower(), None
