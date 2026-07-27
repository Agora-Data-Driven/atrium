"""ONE-TIME Lark authorization for the MeloYelo production-orders pull.

You do this ONCE (about 2 minutes) — NOT every run. Lark refresh tokens are single-use, but the
export job stores the newest one back to the bucket after every pull, so the chain sustains
itself forever after this seed. (The token in the client's notebook died precisely because the
notebook printed it instead of storing it.)

Steps:
  1. Open this URL in a browser signed in to the client's Lark account (the app owner):

       https://open.larksuite.com/open-apis/authen/v1/index?app_id=<LARK_APP_ID>&redirect_uri=<the app's redirect URI>

     Approve, and copy the `code=` value off the redirect URL. Codes expire in minutes — go
     straight to step 2.

  2. Run this script (creds fall back to the notebook in context/ if env vars are unset):

       py clients/client_MeloYelo/job/lark_auth.py <paste-the-code>

  3. It prints ONE command that stores the refresh token in Secret Manager
     (`meloyelo-lark-refresh-seed`). Run it, then re-run deploy_meloyelo.ps1 (or just
     `gcloud run jobs update meloyelo-export ...` per the printed hint) so the job sees it.

The token itself is only ever shown as the Secret Manager command — don't paste it anywhere else.
"""
import json
import os
import re
import sys
import urllib.request

LARK_BASE = "https://open.larksuite.com/open-apis"
_HERE = os.path.dirname(os.path.abspath(__file__))
_NB = os.path.join(os.path.dirname(_HERE), "context", "Lark_Unleashed_Dash.ipynb")


def _from_notebook(pattern):
    try:
        nb = json.load(open(_NB, encoding="utf-8"))
    except (OSError, ValueError):
        return None
    src = "".join("".join(c.get("source", [])) for c in nb.get("cells", [])
                  if c.get("cell_type") == "code")
    m = re.search(pattern, src)
    return m.group(1) if m else None


def _post(path, payload, bearer=None):
    req = urllib.request.Request(LARK_BASE + path, data=json.dumps(payload).encode(),
                                 method="POST", headers={"Content-Type": "application/json"})
    if bearer:
        req.add_header("Authorization", "Bearer " + bearer)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    code = sys.argv[1].strip()
    app_id = os.environ.get("LARK_APP_ID") or _from_notebook(r'LARK_APP_ID\s*=\s*"([^"]+)"')
    app_secret = os.environ.get("LARK_APP_SECRET") or _from_notebook(r'LARK_APP_SECRET\s*=\s*"([^"]+)"')
    if not (app_id and app_secret):
        print("[!] no Lark app credentials — set LARK_APP_ID/LARK_APP_SECRET or keep the notebook in context/")
        return 1

    app_tok = _post("/auth/v3/app_access_token/internal",
                    {"app_id": app_id, "app_secret": app_secret})["app_access_token"]
    r = _post("/authen/v1/access_token",
              {"grant_type": "authorization_code", "code": code}, bearer=app_tok)
    if r.get("code") != 0:
        print("[!] exchange failed: %s" % r)
        print("    (codes expire in minutes — re-authorize and paste a fresh one)")
        return 1

    refresh = r["data"]["refresh_token"]
    tmp = os.path.join(os.environ.get("TEMP", "."), "meloyelo-lark-seed.txt")
    with open(tmp, "wb") as fh:            # UTF-8 bytes, no BOM, no trailing newline
        fh.write(refresh.encode("utf-8"))
    print("[OK] fresh refresh token written to a temp file (never printed).")
    print()
    print("Store it, then delete the temp file:")
    print()
    print("  gcloud secrets versions add meloyelo-lark-refresh-seed --project agora-data-driven --data-file=\"%s\"" % tmp)
    print("  del \"%s\"" % tmp)
    print()
    print("If the secret doesn't exist yet, rerun deploy_meloyelo.ps1 -LarkSeedFile \"%s\"" % tmp)
    print("The export job self-rotates the token from here on — this is a one-time step.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
