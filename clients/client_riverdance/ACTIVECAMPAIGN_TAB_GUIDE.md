# ActiveCampaign “Email” Tab — Implementation Guide

A portable, copy-adaptable guide for adding a second **Email · ActiveCampaign** tab to a marketing
dashboard, matched to the existing dashboard's brand and interactions. This is the exact feature
built for the Riverdance dashboard; the code blocks below are the real, deployed implementation.

> **Live reference:** the built dashboard renders both tabs from a single JSON payload. This
> document is everything you need to rebuild it in another repo.

---

## 0. What you are building

- **ActiveCampaign is the 2nd tab** — "Meta Ads" (or your existing ad tab) first, **"Email ·
  ActiveCampaign"** second. The tab shows a live count badge (e.g. "36" = sent campaigns).
- **Brand-matched** — it reuses the existing dashboard's theme tokens (CSS custom properties),
  serif headings, ribbon, logos, and light/dark support. No new visual language.
- **A shared, toggleable date range drives the whole thing** — one From/To picker plus
  7d / 14d / 30d / Max presets drive **both tabs**:
  - Email tab: campaign KPIs, the per-campaign chart, the engagement grid, and the campaigns table
    all re-window to the selected range.
  - Ad tab: the Audience/demographics section responds too (it pulls **per-day** breakdowns so it
    can be re-aggregated for any window).
- **Interactive** — metric chips, a Rates ↔ Volume toggle, sortable table columns, clickable KPI
  tiles, and an **email-campaign dropdown filter** (pick a campaign → the whole tab scopes to it,
  with a "Show all campaigns" banner). On the ad tab, clicking a creative scopes that whole tab.
- **Data the client + media buyer need** — contacts, recipients, open / click / click-to-open
  rates, **unsub & bounce (deliverability / spam signals)**, a per-campaign table
  (recipients / opens / clicks / CTOR / unsubs / bounces), lists & subscriber counts, and
  automations (entered / completed / completion %).
- **Two polish details** baked in: the chart's **average line is subtle** (faint dotted, not a bold
  dash), and campaigns are **fully paginated** on the pull so nothing is capped.

---

## 1. Architecture & assumptions

This implementation assumes the common "single JSON + self-contained page" pattern:

```
  BUILD STEP (server-side)            DATA                 DASHBOARD (browser)
  a job/function pulls each source -> one JSON blob  ->    one self-contained HTML page
  (ads API, ActiveCampaign API)      (uploaded/served)     reads DATA.* and renders everything
```

Map these three roles onto your target repo:

1. **A server-side build step** that assembles the dashboard's data (Node, Python, a job, a
   serverless function). You add an ActiveCampaign pull here and attach the result under an
   `activecampaign` key.
2. **The data JSON** the page consumes (fetched at runtime, or inlined). You add one
   `activecampaign` object to it (shape in §6).
3. **The dashboard page** — HTML/CSS/JS. You add the tab nav, the Email pane, and the render JS.

Two portability notes:

- **Pull server-side, never in the browser.** The ActiveCampaign token must not ship to the
  client. Keep it in a secret and call the API from the build step. (An in-browser "enter your
  token" form leaks the key to anyone who opens dev tools.)
- **If your inline JS passes an ES5-era linter/gate** (e.g. esprima 4.x), keep the dashboard JS
  ES5/ES2015-safe: `var`, classic functions, `&&`/`||` guards — no `?.`, no `??`, no arrow/`let`/
  `const`. All JS below already follows this.

---

## 2. Step 1 — Pull ActiveCampaign (server-side)

### The API

- **Base URL:** `https://<account>.api-us1.com` (each account has its own subdomain).
- **Auth:** header `Api-Token: <key>` on every request.
- **Endpoints used** (all `GET /api/3/...`, most return a `meta.total` for paging):
  - `campaigns?filters[status]=5&orders[sdate]=DESC&limit=100&offset=N` — **sent** campaigns,
    newest first, with counters: `send_amt`, `uniqueopens`, `opens`, `uniquelinkclicks`,
    `linkclicks`, `unsubscribes`, `hardbounces`, `softbounces`, `uniqueforwards`, `uniquereplies`,
    `socialshares`, `sdate`.
  - `contacts?limit=1` — read `meta.total` for the whole contact base.
  - `lists?limit=100` — each has `name`, `active_subscribers`, `non_deleted_subscribers`.
  - `automations?limit=100` — each has `name`, `entered`, `exited`, `status` (`"1"` = active).
  - `deals?limit=1` — a `403` means the plan has **no CRM**; treat as "CRM not enabled".

> **Rates are derived, not fetched.** ActiveCampaign returns raw counters. Compute open rate =
> uniqueopens ÷ send_amt, click rate = uniquelinkclicks ÷ send_amt, click-to-open = clicks ÷ opens,
> unsub rate = unsubscribes ÷ send_amt, bounce rate = (hard+soft) ÷ send_amt — in the page, so every
> window/filter recomputes correctly (§5).

### Reference module (`activecampaign.py`)

Stdlib-only, best-effort (never crashes the build), token from env or a secret store. Adapt the
language/secret-source to your repo; the endpoint logic and output shape are what matter.

```python
"""ActiveCampaign email-marketing pull — returns a compact `activecampaign` block to embed in the
dashboard's data JSON. Stdlib-only, best-effort: any failure (no key, network, a disabled feature
such as the CRM) degrades to an empty/partial block with an `error` string and NEVER crashes the
export. The API token is read from the environment (mounted secret) or Secret Manager and is never
logged or persisted.

Config / secrets:
  ACTIVECAMPAIGN_URL      — account API base, e.g. https://<account>.api-us1.com   (env)
  ACTIVECAMPAIGN_API_KEY  — the Api-Token (from a secret store, mounted as env)

Shape returned (matched BY NAME to the dashboard's DATA.activecampaign.*):
  { enabled, account, url, fetched, crm_enabled, error,
    totals{contacts,campaigns,campaigns_sent,lists,automations},
    campaigns[]  (SENT only, newest first — id,name,date,sent,opens,opens_total,clicks,
                  clicks_total,unsubs,bounces,forwards,replies,socialshares),
    lists[]      (id,name,subscribers,total  — top by subscribers),
    automations[](id,name,entered,exited,active) }
"""
import json
import os
import urllib.request
from urllib.parse import urlencode, urlparse

# If you use Google Secret Manager as a fallback, set these; otherwise the env var is enough.
PROJECT = os.environ.get("GCP_PROJECT", "")
AC_SECRET = "riverdance-activecampaign-key"   # Secret Manager id (fallback when env not set)
_TIMEOUT = 60


def _num(x):
    try:
        return int(float(x)) if x is not None and x != "" else 0
    except (TypeError, ValueError):
        return 0


def _base_url():
    return (os.environ.get("ACTIVECAMPAIGN_URL", "") or "").strip().rstrip("/")


def _api_key():
    """AC Api-Token from env (mounted secret) or, as a fallback, Secret Manager directly."""
    k = (os.environ.get("ACTIVECAMPAIGN_API_KEY", "") or "").strip()
    if k:
        return k
    try:
        from google.cloud import secretmanager
        sm = secretmanager.SecretManagerServiceClient()
        name = "projects/%s/secrets/%s/versions/latest" % (PROJECT, AC_SECRET)
        return sm.access_secret_version(name={"name": name}).payload.data.decode("utf-8").strip()
    except Exception:  # noqa: BLE001 — no key available is a graceful "disabled", not a crash
        return ""


def _account_name(url):
    try:
        host = urlparse(url).hostname or ""
        return host.split(".", 1)[0] if host else ""
    except Exception:  # noqa: BLE001
        return ""


def _get(base, key, path, params=None):
    """One GET against /api/3/<path>. Returns the parsed JSON dict (raises on transport error)."""
    q = urlencode(params or {})
    sep = "&" if "?" in path else "?"
    full = "%s/api/3/%s%s%s" % (base, path.lstrip("/"), sep if q else "", q)
    req = urllib.request.Request(full, headers={"Api-Token": key, "User-Agent": "email-tab/1.0"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _meta_total(payload):
    try:
        return _num((payload.get("meta") or {}).get("total"))
    except Exception:  # noqa: BLE001
        return 0


def _iso_date(s):
    """'2026-07-10T14:30:00-05:00' -> '2026-07-10' (date part only; None -> '')."""
    if not s:
        return ""
    return str(s).split("T", 1)[0]


def _campaign_row(c):
    return {
        "id": c.get("id"),
        "name": (c.get("name") or "Untitled").strip(),
        "date": _iso_date(c.get("sdate") or c.get("ldate") or c.get("mdate")),
        "sent": _num(c.get("send_amt")),
        "opens": _num(c.get("uniqueopens")),
        "opens_total": _num(c.get("opens")),
        "clicks": _num(c.get("uniquelinkclicks")),
        "clicks_total": _num(c.get("linkclicks")),
        "unsubs": _num(c.get("unsubscribes")),
        "bounces": _num(c.get("hardbounces")) + _num(c.get("softbounces")),
        "forwards": _num(c.get("uniqueforwards")),
        "replies": _num(c.get("uniquereplies")),
        "socialshares": _num(c.get("socialshares")),
    }


def _fetch_campaigns(base, key, limit=100, max_pages=20):
    """ALL sent campaigns (status 5), newest first — paged so nothing is capped. Returns
    (rows, total_sent)."""
    rows, total, offset = [], 0, 0
    for _ in range(max_pages):
        payload = _get(base, key, "campaigns", {
            "limit": limit, "offset": offset, "orders[sdate]": "DESC", "filters[status]": 5,
        })
        batch = payload.get("campaigns") or []
        total = _meta_total(payload) or total
        rows.extend(_campaign_row(c) for c in batch)
        offset += limit
        if len(batch) < limit or (total and offset >= total):
            break
    rows = [r for r in rows if r["sent"] > 0 or r["date"]]
    return rows, total or len(rows)


def _fetch_lists(base, key, cap=100):
    payload = _get(base, key, "lists", {"limit": cap})
    out = []
    for l in (payload.get("lists") or []):
        out.append({
            "id": l.get("id"), "name": (l.get("name") or "Untitled").strip(),
            "subscribers": _num(l.get("active_subscribers")),
            "total": _num(l.get("non_deleted_subscribers")) or _num(l.get("subscriber_count")),
        })
    out.sort(key=lambda r: r["subscribers"], reverse=True)
    return out, _meta_total(payload)


def _fetch_automations(base, key, cap=100):
    payload = _get(base, key, "automations", {"limit": cap})
    out = []
    for a in (payload.get("automations") or []):
        out.append({
            "id": a.get("id"), "name": (a.get("name") or "Untitled").strip(),
            "entered": _num(a.get("entered")), "exited": _num(a.get("exited")),
            "active": str(a.get("status")) == "1",
        })
    out.sort(key=lambda r: r["entered"], reverse=True)
    return out, _meta_total(payload)


def _fetch_contacts_total(base, key):
    return _meta_total(_get(base, key, "contacts", {"limit": 1}))


def _crm_enabled(base, key):
    """Deals require a CRM upgrade — a 403 means CRM is off. Best-effort True/False."""
    try:
        _get(base, key, "deals", {"limit": 1})
        return True
    except urllib.error.HTTPError as e:  # noqa: PERF203
        return e.code != 403
    except Exception:  # noqa: BLE001
        return False


def fetch():
    """Assemble the `activecampaign` block. Never raises — returns {enabled:False,error:...} on failure."""
    base, key = _base_url(), _api_key()
    if not base or not key:
        return {"enabled": False, "error": "ActiveCampaign not configured (URL/key missing)"}

    out = {
        "enabled": True, "account": _account_name(base), "url": base,
        "fetched": _iso_date(__import__("datetime").date.today().isoformat()),
        "crm_enabled": False, "error": "",
        "totals": {"contacts": 0, "campaigns": 0, "campaigns_sent": 0, "lists": 0, "automations": 0},
        "campaigns": [], "lists": [], "automations": [],
    }
    errs = []
    try:
        out["totals"]["contacts"] = _fetch_contacts_total(base, key)
    except Exception as e:  # noqa: BLE001
        errs.append("contacts: %s" % str(e)[:80])
    try:
        rows, sent_total = _fetch_campaigns(base, key)
        out["campaigns"] = rows
        out["totals"]["campaigns_sent"] = sent_total or len(rows)
        all_c = _get(base, key, "campaigns", {"limit": 1})
        out["totals"]["campaigns"] = _meta_total(all_c)
    except Exception as e:  # noqa: BLE001
        errs.append("campaigns: %s" % str(e)[:80])
    try:
        lists, ltot = _fetch_lists(base, key)
        out["lists"] = lists
        out["totals"]["lists"] = ltot or len(lists)
    except Exception as e:  # noqa: BLE001
        errs.append("lists: %s" % str(e)[:80])
    try:
        autos, atot = _fetch_automations(base, key)
        out["automations"] = autos
        out["totals"]["automations"] = atot or len(autos)
    except Exception as e:  # noqa: BLE001
        errs.append("automations: %s" % str(e)[:80])
    out["crm_enabled"] = _crm_enabled(base, key)
    if errs:
        out["error"] = "; ".join(errs)
    return out


if __name__ == "__main__":
    print(json.dumps(fetch(), indent=2))
```

### Wire it into your build step

Attach the block under `activecampaign` on the data object you already assemble:

```python
data["activecampaign"] = activecampaign.fetch()   # best-effort; {"enabled": False, ...} if unconfigured
```

Provide `ACTIVECAMPAIGN_URL` (env) and `ACTIVECAMPAIGN_API_KEY` (secret mounted as env). If either
is missing, `fetch()` returns `{"enabled": false, "error": "..."}` and the tab shows a friendly
"not connected" state.

---

## 3. Step 2 — Make the whole dashboard respond to the date range

### 3a. A shared date domain + presets

The picker spans the **union** of every source's dates, so "Max" covers everything while each
chart still only plots its own real days. Presets are computed by date arithmetic (works across
sources), not by array index.

```javascript
function setPreset(days){ var last=DOMAIN[DOMAIN.length-1];
  if(days===0){ rStart=DOMAIN[0]; } else { rStart=shiftDays(last,-(days-1)); if(rStart<DOMAIN[0])rStart=DOMAIN[0]; }
  rEnd=last; document.getElementById("rv-start").value=rStart; document.getElementById("rv-end").value=rEnd;
  var b=document.querySelectorAll("#rv-presets button"); for(var i=0;i<b.length;i++){ b[i].setAttribute("aria-pressed",parseInt(b[i].getAttribute("data-days"),10)===days?"true":"false"); } renderAll(); }
```

`DOMAIN` (the union of ad-days + email send-dates) and the picker min/max are built in boot() —
see §5d. `renderAll()` re-renders **both** tabs, so any range change updates everything.

### 3b. Per-day demographics on the ad tab (so Audience re-windows)

If your ad source exposes demographic/geo breakdowns, pull them **per day** (add a `date` field to
the breakdown request) and store one row per (date × dimension). Example (Meta via a connector):

```python
AGE_GENDER_FIELDS = ["date", "age", "gender", "spend", "impressions", "clicks", "link_clicks"]
REGION_FIELDS     = ["date", "region", "spend", "impressions", "clicks", "link_clicks"]

def _demo_row(r, dims):
    out = {"date": r.get("date"), "spend": round(_num(r.get("spend")), 2),
           "imps": int(_num(r.get("impressions"))), "clicks": int(_num(r.get("clicks"))),
           "lclk": int(_num(r.get("link_clicks")))}
    for d in dims:
        out[d] = r.get(d) or "Unknown"
    return out
```

The page-side re-aggregation (note the `inWin()` filter and grouping by region name so per-day
rows don't duplicate):

```javascript
function renderDemographics(){
  var dm=DATA.demographics||{}, ag=dm.age_gender||[], rg=dm.region||[];
  // Breakdowns are pulled per-day, so scope them to the selected date range (rows with no date
  // — older data — are always kept so the section never blanks after a data-shape change).
  function inWin(r){ return (!r.date) || (r.date>=rStart && r.date<=rEnd); }
  ag=ag.filter(inWin); rg=rg.filter(inWin);
  var AGES=["18-24","25-34","35-44","45-54","55-64","65+"], byAge={};
  ag.forEach(function(r){ var a=r.age||"Unknown"; if(!byAge[a])byAge[a]={label:a,imps:0,clicks:0,spend:0}; byAge[a].imps+=r.imps; byAge[a].clicks+=r.clicks; byAge[a].spend+=r.spend; });
  var ageItems=AGES.filter(function(a){ return byAge[a]; }).map(function(a){ return byAge[a]; });
  var GEN=["female","male","unknown"], byG={};
  ag.forEach(function(r){ var g=r.gender||"unknown"; if(!byG[g])byG[g]={label:g.charAt(0).toUpperCase()+g.slice(1),imps:0,clicks:0,spend:0}; byG[g].imps+=r.imps; byG[g].clicks+=r.clicks; byG[g].spend+=r.spend; });
  var genItems=GEN.filter(function(g){ return byG[g]; }).map(function(g){ return byG[g]; });
  var byR={}; rg.forEach(function(r){ var k=r.region||"Unknown"; if(!byR[k])byR[k]={label:k,imps:0,clicks:0,spend:0}; byR[k].imps+=r.imps; byR[k].clicks+=r.clicks; byR[k].spend+=r.spend; });
  var regItems=Object.keys(byR).map(function(k){ return byR[k]; }).filter(function(r){ return r.imps>0; }).sort(function(a,b){ return b.imps-a.imps; }).slice(0,6);
  barsV("rv-age",ageItems); barsH("rv-gender",genItems); barsH("rv-region",regItems);
}
```

### 3c. The email tab windows by campaign send-date

`acCampsInWindow()` (in the AC engine, §4c) filters `campaigns[]` to `date` within `[rStart,rEnd]`,
and every email KPI/chart/table is computed from that windowed set.

---

## 4. Step 3 — The Email tab UI (2nd tab)

### 4a. CSS (append to your stylesheet)

Reuses your existing theme tokens (`--card`, `--line`, `--green`, `--muted`, …). Rename tokens to
match your design system. Includes the tab bar, the filter banner, the creative-filter
affordances, and the email helpers (lists, badges, the campaign `<select>`).

```css
/* ---- Tabs (Meta Ads / Email) ---- */
  .rv-tabs{display:flex;gap:4px;margin:16px 0 0;border-bottom:1px solid var(--line);flex-wrap:wrap}
  .rv-tab{appearance:none;border:none;background:transparent;color:var(--muted);font:700 12.5px var(--sans);
    padding:11px 16px;border-radius:11px 11px 0 0;cursor:pointer;position:relative;display:flex;align-items:center;gap:8px}
  .rv-tab .ic{font-size:15px;line-height:1}
  .rv-tab .cnt{font-size:10px;font-weight:700;color:var(--muted);background:var(--card2);border:1px solid var(--line);border-radius:999px;padding:1px 7px}
  .rv-tab:hover{color:var(--ink)}
  .rv-tab[aria-selected="true"]{color:var(--green-d)}
  .rv-tab[aria-selected="true"] .cnt{color:var(--green-d);border-color:color-mix(in srgb,var(--green) 45%,var(--line))}
  .rv-tab[aria-selected="true"]::after{content:"";position:absolute;left:12px;right:12px;bottom:-1px;height:3px;border-radius:3px 3px 0 0;background:linear-gradient(90deg,var(--green),var(--green-l))}
  .rv-tab-pane[hidden]{display:none}

  /* ---- Creative filter banner (click a creative -> whole dashboard scopes to it) ---- */
  .rv-fbanner{display:none;align-items:center;gap:11px;margin:16px 0 0;padding:10px 14px;
    border:1px solid color-mix(in srgb,var(--green) 45%,var(--line));background:var(--green-soft);border-radius:12px;font-size:12.5px;color:var(--ink)}
  .rv-fbanner.on{display:flex}
  .rv-fbanner .lab{font-weight:700;color:var(--green-d);text-transform:uppercase;letter-spacing:.06em;font-size:10px;white-space:nowrap}
  .rv-fbanner b{color:var(--ink)}
  .rv-fbanner .th{width:26px;height:26px;border-radius:6px;object-fit:cover;flex:none;box-shadow:var(--shadow)}
  .rv-fbanner button{margin-left:auto;appearance:none;border:1px solid var(--green);background:var(--card);color:var(--green-d);font:700 11px var(--sans);padding:5px 12px;border-radius:999px;cursor:pointer;white-space:nowrap}
  .rv-fbanner button:hover{background:var(--green);color:#fff}
  .rv-gcard[aria-pressed="true"]{outline:2px solid var(--green);outline-offset:1px}
  .rv-gcard[aria-pressed="true"] .rv-gimg::before{content:"Filtering \2713";position:absolute;z-index:2;left:9px;bottom:9px;
    background:var(--green);color:#fff;font-size:9px;font-weight:700;letter-spacing:.05em;text-transform:uppercase;padding:3px 8px;border-radius:999px}
  .rv-gdetail{position:absolute;bottom:9px;right:9px;z-index:2;appearance:none;border:none;background:rgba(20,20,20,.62);color:#fff;
    font-size:9px;font-weight:700;letter-spacing:.05em;text-transform:uppercase;padding:4px 9px;border-radius:999px;cursor:pointer;opacity:0;transition:opacity .12s}
  .rv-gcard:hover .rv-gdetail{opacity:1}
  .rv-gdetail:hover{background:var(--green)}

  /* ---- Email tab helpers ---- */
  .rv-note{padding:14px 16px;border:1px dashed var(--line);border-radius:var(--radius);color:var(--muted);font-size:12.5px;background:var(--card2)}
  .rv-lists{display:flex;flex-direction:column;gap:9px}
  .rv-lrow{display:grid;grid-template-columns:1fr auto;gap:4px 12px;align-items:center}
  .rv-lrow .ln{font-size:12px;color:var(--ink);font-weight:600}
  .rv-lrow .lv{font-family:var(--serif);font-size:14px;font-weight:600;font-variant-numeric:tabular-nums}
  .rv-lrow .lbar{grid-column:1/3;height:7px;background:var(--grid);border-radius:5px;overflow:hidden}
  .rv-lrow .lbar i{display:block;height:100%;border-radius:5px;background:linear-gradient(90deg,var(--green),var(--green-l))}
  .rv-abadge{font-size:9px;letter-spacing:.05em;text-transform:uppercase;font-weight:700;padding:2px 8px;border-radius:999px}
  .rv-abadge.on{color:var(--good);background:color-mix(in srgb,var(--good) 14%,transparent)}
  .rv-abadge.off{color:var(--muted);background:var(--card2);border:1px solid var(--line)}
  .rv-ac-select{appearance:none;border:1px solid var(--line);background:var(--card);color:var(--ink);
    font:600 11.5px var(--sans);padding:6px 30px 6px 11px;border-radius:999px;cursor:pointer;max-width:280px;
    background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='6' viewBox='0 0 10 6'%3E%3Cpath d='M1 1l4 4 4-4' fill='none' stroke='%23948B72' stroke-width='1.5' stroke-linecap='round'/%3E%3C/svg%3E");
    background-repeat:no-repeat;background-position:right 11px center;text-overflow:ellipsis}
  .rv-ac-select:hover{border-color:var(--green)}
  .rv-ac-select:focus{outline:none;border-color:var(--green);box-shadow:0 0 0 2px var(--green-soft)}
```

### 4b. HTML — tab nav + panes

Add a tab bar, then **wrap your existing dashboard sections** in the first pane, and add the Email
pane as the second. (The `<select>`/badge ids are referenced by the JS.)

Tab nav:

```html
<div class="rv-tabs" id="rv-tabs" role="tablist" aria-label="Dashboard sections">
      <button type="button" class="rv-tab" id="rv-tabbtn-meta" role="tab" data-tab="meta" aria-selected="true" aria-controls="rv-tab-meta"><span class="ic">&#128202;</span>Meta Ads</button>
      <button type="button" class="rv-tab" id="rv-tabbtn-ac" role="tab" data-tab="ac" aria-selected="false" aria-controls="rv-tab-ac"><span class="ic">&#9993;</span>Email &middot; ActiveCampaign<span class="cnt" id="rv-ac-tabcnt"></span></button>
    </div>
```

Then wrap existing content:

```html
<div id="rv-tab-meta" class="rv-tab-pane" role="tabpanel" aria-labelledby="rv-tabbtn-meta">
  <!-- ... all your EXISTING dashboard sections (KPIs, charts, gallery, tables) ... -->
</div>
```

The Email pane (second tab):

```html
<!-- ========================= ACTIVECAMPAIGN TAB ========================= -->
    <div id="rv-tab-ac" class="rv-tab-pane" role="tabpanel" aria-labelledby="rv-tabbtn-ac" hidden>
      <div id="rv-ac-live">
        <div class="rv-fbanner" id="rv-ac-filter" role="status">
          <span class="lab">Filtered to campaign</span>
          <span>Every email metric below is scoped to <b id="rv-ac-filter-name"></b>.</span>
          <button type="button" id="rv-ac-filter-clear">Show all campaigns</button>
        </div>
        <section class="rv-sec" style="margin-top:16px">
          <div class="rv-eyebrow" id="rv-ac-window"></div>
          <div class="rv-kpis" id="rv-ac-kpis"></div>
        </section>

        <section class="rv-sec tight">
          <div class="rv-card">
            <div class="rv-hero-controls">
              <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
                <select id="rv-ac-campaign" class="rv-ac-select" aria-label="Filter by email campaign"><option value="">All campaigns in range</option></select>
                <div class="rv-chips" id="rv-ac-chips" role="group" aria-label="Email metric"></div>
              </div>
              <div class="rv-seg" id="rv-ac-view" role="group" aria-label="View">
                <button type="button" data-acview="rate" aria-pressed="true">Rates</button>
                <button type="button" data-acview="vol">Volume</button>
              </div>
            </div>
            <div class="rv-chart-head">
              <div class="now" id="rv-ac-hero-now"></div>
              <div class="rv-legend">
                <span><i id="rv-ac-leg-i"></i><span id="rv-ac-leg-l">Open rate</span></span>
                <span><i class="dash"></i>Average</span>
              </div>
            </div>
            <svg id="rv-ac-hero" viewBox="0 0 1000 180" role="img" aria-label="Email campaign performance over time"></svg>
          </div>
        </section>

        <section class="rv-sec">
          <div class="rv-two">
            <div>
              <div class="rv-eyebrow">Audience</div>
              <h2 class="rv-h">Lists &amp; subscribers</h2>
              <p class="rv-sub">Active subscribers on each list. The bar is relative to the largest list.</p>
              <div class="rv-card"><div class="rv-lists" id="rv-ac-lists"></div></div>
            </div>
            <div>
              <div class="rv-eyebrow">Contact base</div>
              <h2 class="rv-h">Who we can reach</h2>
              <p class="rv-sub">The whole ActiveCampaign contact database powering campaigns &amp; automations.</p>
              <div class="rv-eff" id="rv-ac-base"></div>
              <div class="rv-ctx" style="grid-template-columns:1fr;margin-top:13px">
                <div class="rv-card"><div class="lab">Account</div><div class="big" id="rv-ac-acct"></div><div class="row" id="rv-ac-acct-chips"></div></div>
              </div>
            </div>
          </div>
        </section>

        <section class="rv-sec">
          <div class="rv-eyebrow">Engagement</div>
          <h2 class="rv-h">How subscribers respond</h2>
          <p class="rv-sub">Averaged across every email sent in this window &mdash; the health of the list at a glance.</p>
          <div class="rv-eff" id="rv-ac-eff"></div>
        </section>

        <section class="rv-sec">
          <div class="rv-eyebrow">Campaigns</div>
          <h2 class="rv-h">Every email, by the numbers</h2>
          <p class="rv-sub" id="rv-ac-camp-sub"></p>
          <div class="rv-tblwrap">
            <table class="rv-tbl">
              <thead><tr id="rv-ac-head">
                <th class="sortable" data-acsort="name">Campaign<span class="arr"></span></th>
                <th class="sortable" data-acsort="date">Sent<span class="arr"></span></th>
                <th class="sortable" data-acsort="sent">Recipients<span class="arr"></span></th>
                <th class="sortable" data-acsort="opens">Opens<span class="arr"></span></th>
                <th class="sortable" data-acsort="openrate">Open&nbsp;%<span class="arr"></span></th>
                <th class="sortable" data-acsort="clicks">Clicks<span class="arr"></span></th>
                <th class="sortable" data-acsort="clickrate">Click&nbsp;%<span class="arr"></span></th>
                <th class="sortable" data-acsort="ctor">CTOR<span class="arr"></span></th>
                <th class="sortable" data-acsort="unsubs">Unsubs<span class="arr"></span></th>
                <th class="sortable" data-acsort="bounces">Bounces<span class="arr"></span></th>
              </tr></thead>
              <tbody id="rv-ac-campaigns"></tbody>
            </table>
          </div>
        </section>

        <section class="rv-sec">
          <div class="rv-eyebrow">Automations</div>
          <h2 class="rv-h">Journeys running in the background</h2>
          <p class="rv-sub" id="rv-ac-auto-sub"></p>
          <div class="rv-tblwrap">
            <table class="rv-tbl">
              <thead><tr>
                <th>Automation</th><th>Status</th><th>Entered</th><th>Completed</th><th>In&nbsp;progress</th><th>Completion&nbsp;%</th>
              </tr></thead>
              <tbody id="rv-ac-autos"></tbody>
            </table>
          </div>
        </section>
      </div>
      <div id="rv-ac-empty" hidden><section class="rv-sec" style="margin-top:18px"><div class="rv-note" id="rv-ac-empty-msg"></div></section></div>
    </div><!-- /#rv-tab-ac -->
```

### 4c. JS — the Email engine

Drop this block into your dashboard script. It renders every Email-tab section from
`DATA.activecampaign`, derives all rates, and implements the campaign dropdown filter + the subtle
average line. No external dependencies.

```javascript
/* ============================ ACTIVECAMPAIGN (EMAIL) ============================ */
function shiftDays(iso,n){ var t=new Date(iso+"T00:00:00Z"); t.setUTCDate(t.getUTCDate()+n); return t.toISOString().slice(0,10); }
function pct(v){ return r2(v)+"%"; }
function acFmt(u,v){ if(u==="pct")return pct(v); return num(Math.round(v)); }
function acDate(iso){ if(!iso)return "—"; var p=iso.split("-"); return MM[parseInt(p[1],10)-1]+" "+parseInt(p[2],10)+", "+p[0]; }
function acCampsInWindow(){ if(!AC.enabled||!AC.campaigns)return [];
  var w=AC.campaigns.filter(function(c){ return c.date && c.date>=rStart && c.date<=rEnd; });
  return w; }
function acAgg(camps){ var t={sent:0,opens:0,clicks:0,unsubs:0,bounces:0,forwards:0,replies:0,socialshares:0,opens_total:0,clicks_total:0};
  camps.forEach(function(c){ t.sent+=c.sent;t.opens+=c.opens;t.clicks+=c.clicks;t.unsubs+=c.unsubs;t.bounces+=c.bounces;
    t.forwards+=c.forwards;t.replies+=c.replies;t.socialshares+=c.socialshares;t.opens_total+=c.opens_total;t.clicks_total+=c.clicks_total; });
  return t; }
function acRates(t){ return { openrate:t.sent?r2(t.opens/t.sent*100):0, clickrate:t.sent?r2(t.clicks/t.sent*100):0,
  ctor:t.opens?r2(t.clicks/t.opens*100):0, unsubrate:t.sent?r2(t.unsubs/t.sent*100):0, bouncerate:t.sent?r2(t.bounces/t.sent*100):0 }; }
function acVal(c,key){ if(key==="openrate")return c.sent?c.opens/c.sent*100:0; if(key==="clickrate")return c.sent?c.clicks/c.sent*100:0;
  if(key==="ctor")return c.opens?c.clicks/c.opens*100:0; if(key==="unsubrate")return c.sent?c.unsubs/c.sent*100:0; return c[key]||0; }

function renderAcChips(){ var w=document.getElementById("rv-ac-chips"); w.innerHTML="";
  var keys=(acView==="rate")?AC_CHIP_RATE:AC_CHIP_VOL;
  if(keys.indexOf(acMetric)<0)acMetric=keys[0];
  keys.forEach(function(key){ var b=document.createElement("button"); b.type="button"; b.className="rv-chip-btn";
    b.setAttribute("data-acmetric",key); b.setAttribute("aria-pressed",key===acMetric?"true":"false"); b.textContent=AC_METRICS[key].short; w.appendChild(b); }); }

function renderAcKpis(t,rates){
  var camps=lastAcCamps.slice().sort(function(a,b){ return a.date<b.date?-1:(a.date>b.date?1:0); });
  function ser(key){ return camps.map(function(c){ return acVal(c,key); }); }
  var T=(AC.totals||{});
  var tiles=[
    {k:"Contacts",v:num(T.contacts||0),c:"in the database"},
    {k:"Recipients",v:num(t.sent),c:"emails delivered",metric:"sent"},
    {k:"Open rate",v:pct(rates.openrate),c:num(t.opens)+" unique opens",accent:true,metric:"openrate"},
    {k:"Click rate",v:pct(rates.clickrate),c:num(t.clicks)+" unique clicks",metric:"clickrate"},
    {k:"Click-to-open",v:pct(rates.ctor),c:"clicks ÷ opens",metric:"ctor"},
    {k:"Unsub rate",v:pct(rates.unsubrate),c:"<b>"+num(t.unsubs)+"</b> unsubscribed",accent:true,metric:"unsubrate"},
    {k:"Bounce rate",v:pct(rates.bouncerate),c:num(t.bounces)+" bounced"},
    {k:"Campaigns",v:num(camps.length),c:"sent in this window"}];
  var w=document.getElementById("rv-ac-kpis"); w.innerHTML="";
  tiles.forEach(function(t2){ var d=document.createElement("div"); d.className="rv-tile"+(t2.accent?" accent":"");
    var sp=t2.metric?('<span class="spark">'+sparkline(ser(t2.metric).length?ser(t2.metric):[0,0])+'</span>'):'';
    if(t2.metric){ d.setAttribute("data-acmetric",t2.metric); d.setAttribute("role","button"); d.setAttribute("tabindex","0"); d.setAttribute("aria-pressed",t2.metric===acMetric?"true":"false"); }
    else { d.style.cursor="default"; }
    d.innerHTML='<div class="k"><span>'+t2.k+'</span>'+sp+'</div><div class="v">'+t2.v+'</div><div class="c">'+t2.c+'</div>'; w.appendChild(d); }); }

function renderAcHero(camps,mkey,view){
  var svg=document.getElementById("rv-ac-hero"); clear(svg);
  var cfg=AC_METRICS[mkey]; var c2=camps.slice().sort(function(a,b){ return a.date<b.date?-1:(a.date>b.date?1:0); });
  document.getElementById("rv-ac-leg-l").textContent=cfg.label;
  var vals=c2.map(function(c){ return acVal(c,mkey); }); var N=c2.length;
  var W=1000,H=180,mL=54,mR=14,mT=12,mB=30;
  if(!N){ var t0=el("text",{class:"rv-axis",x:W/2,y:H/2,"text-anchor":"middle"}); t0.textContent="No email campaigns sent in this window"; svg.appendChild(t0);
    document.getElementById("rv-ac-hero-now").textContent=""; return; }
  var maxV=0; vals.forEach(function(v){ if(v>maxV)maxV=v; }); if(maxV<=0)maxV=1;
  var pw=Math.pow(10,Math.floor(Math.log(maxV)/Math.log(10))); maxV=Math.ceil(maxV/pw*1.05)*pw;
  var iw=W-mL-mR,ih=H-mT-mB,bw=Math.min(iw/N*0.62,46);
  function X(i){ return mL+(N<2?iw/2:iw*i/(N-1)); } function Y(v){ return mT+ih-(v/maxV)*ih; }
  for(var g=0;g<=4;g++){ var yv=maxV*g/4,y=Y(yv); svg.appendChild(el("line",{class:"rv-gl",x1:mL,y1:y,x2:W-mR,y2:y}));
    var lab=el("text",{class:"rv-axis",x:mL-7,y:y+3,"text-anchor":"end"}); lab.textContent=(cfg.unit==="pct")?(r2(yv)+"%"):shortNum(yv); svg.appendChild(lab); }
  var avg=vals.reduce(function(a,b){return a+b;},0)/N;
  c2.forEach(function(c,i){ var cx=X(i),v=vals[i],y=Y(v),h=mT+ih-y,x=cx-bw/2;
    var bar=el("rect",{x:x,y:y,width:bw,height:Math.max(h,0),rx:3,style:"fill:"+PAL[i%PAL.length]});
    bar.addEventListener("mousemove",function(ev){ acHeroTip(c,ev); }); bar.addEventListener("mouseleave",hideTip); svg.appendChild(bar); });
  var ay=Y(avg); svg.appendChild(el("line",{x1:mL,y1:ay,x2:W-mR,y2:ay,"stroke-width":1,"stroke-dasharray":"2 5","stroke-linecap":"round",style:"fill:none;stroke:var(--muted);stroke-opacity:.45"}));
  var step=N>18?4:(N>10?2:1);
  c2.forEach(function(c,i){ if(i%step===0||i===N-1){ var lab=el("text",{class:"rv-axis",x:X(i),y:H-11,"text-anchor":"middle"}); lab.textContent=mday(c.date); svg.appendChild(lab); } });
  var totLine=(view==="rate")?("Avg <b>"+pct(avg)+"</b> · "+N+" campaign"+(N===1?"":"s")):("<b>"+num(vals.reduce(function(a,b){return a+b;},0))+"</b> total · "+N+" campaign"+(N===1?"":"s"));
  document.getElementById("rv-ac-hero-now").innerHTML=totLine;
}
function acHeroTip(c,ev){ var or=c.sent?r2(c.opens/c.sent*100):0, clr=c.sent?r2(c.clicks/c.sent*100):0;
  showTip('<div class="td">'+esc(c.name)+'</div><div class="tr"><span class="m">'+acDate(c.date)+'</span><span></span></div>'+
    '<div class="tr"><span class="m">Recipients</span><span>'+num(c.sent)+'</span></div>'+
    '<div class="tr"><span class="m">Open rate</span><span>'+or+'%</span></div>'+
    '<div class="tr"><span class="m">Click rate</span><span>'+clr+'%</span></div>'+
    '<div class="tr"><span class="m">Unsubs</span><span>'+num(c.unsubs)+'</span></div>', ev.clientX, ev.clientY); }

function renderAcLists(){ var box=document.getElementById("rv-ac-lists"); box.innerHTML="";
  var ls=(AC.lists||[]).filter(function(l){ return l.subscribers>0 || l.total>0; }).slice(0,8);
  if(!ls.length){ box.innerHTML='<div class="rv-note">No subscriber lists returned.</div>'; return; }
  var maxV=0; ls.forEach(function(l){ if(l.subscribers>maxV)maxV=l.subscribers; }); maxV=maxV||1;
  ls.forEach(function(l){ var row=document.createElement("div"); row.className="rv-lrow";
    row.innerHTML='<span class="ln">'+esc(l.name)+'</span><span class="lv">'+num(l.subscribers)+'</span><span class="lbar"><i style="width:'+Math.max(l.subscribers/maxV*100,2)+'%"></i></span>'; box.appendChild(row); }); }

function renderAcBase(){ var T=(AC.totals||{}); var camps=lastAcCamps;
  var cells=[{k:"Contacts",v:num(T.contacts||0)},{k:"Lists",v:num(T.lists||0)},
    {k:"Automations",v:num(T.automations||0)},{k:"Campaigns sent",v:num(T.campaigns_sent||camps.length)}];
  var w=document.getElementById("rv-ac-base"); w.innerHTML=""; cells.forEach(function(e){ var d=document.createElement("div"); d.innerHTML='<div class="k">'+e.k+'</div><div class="v">'+e.v+'</div>'; w.appendChild(d); }); }

function renderAcEff(t,rates){ var effs=[{k:"Open rate",v:pct(rates.openrate)},{k:"Click rate",v:pct(rates.clickrate)},
  {k:"Click-to-open",v:pct(rates.ctor)},{k:"Unsubscribe rate",v:pct(rates.unsubrate)},{k:"Bounce rate",v:pct(rates.bouncerate)},
  {k:"Forwards",v:num(t.forwards)},{k:"Replies",v:num(t.replies)},{k:"Social shares",v:num(t.socialshares)}];
  var w=document.getElementById("rv-ac-eff"); w.innerHTML=""; effs.forEach(function(e){ var d=document.createElement("div"); d.innerHTML='<div class="k">'+e.k+'</div><div class="v">'+e.v+'</div>'; w.appendChild(d); }); }

function acSortVal(c,key){ if(key==="name")return (c.name||"").toLowerCase(); if(key==="openrate")return acVal(c,"openrate");
  if(key==="clickrate")return acVal(c,"clickrate"); if(key==="ctor")return acVal(c,"ctor"); return c[key]||0; }
function renderAcTable(){ var body=document.getElementById("rv-ac-campaigns"); body.innerHTML="";
  var camps=lastAcCamps.slice(), key=acSort.key, dir=acSort.dir;
  camps.sort(function(a,b){ var va=acSortVal(a,key),vb=acSortVal(b,key); if(key==="name"||key==="date"){ return va<vb?-1*dir:(va>vb?1*dir:0); } return (va-vb)*dir; });
  var maxO=0; camps.forEach(function(c){ var o=acVal(c,"openrate"); if(o>maxO)maxO=o; }); maxO=maxO||1;
  camps.forEach(function(c){ var or=acVal(c,"openrate"),clr=acVal(c,"clickrate"),ctor=acVal(c,"ctor"); var tr=document.createElement("tr");
    tr.innerHTML='<td><div class="rv-adname"><b>'+esc(c.name)+'</b><div class="rv-bar"><i style="width:'+(or/maxO*100)+'%"></i></div></div></td>'+
      '<td>'+acDate(c.date)+'</td><td>'+num(c.sent)+'</td><td>'+num(c.opens)+'</td><td>'+pct(or)+'</td><td>'+num(c.clicks)+'</td><td>'+pct(clr)+'</td><td>'+pct(ctor)+'</td><td>'+num(c.unsubs)+'</td><td>'+num(c.bounces)+'</td>'; body.appendChild(tr); });
  var t=acAgg(lastAcCamps),r=acRates(t); var tot=document.createElement("tr"); tot.className="tot";
  tot.innerHTML='<td>All campaigns</td><td>'+lastAcCamps.length+'</td><td>'+num(t.sent)+'</td><td>'+num(t.opens)+'</td><td>'+pct(r.openrate)+'</td><td>'+num(t.clicks)+'</td><td>'+pct(r.clickrate)+'</td><td>'+pct(r.ctor)+'</td><td>'+num(t.unsubs)+'</td><td>'+num(t.bounces)+'</td>'; body.appendChild(tot);
  var ths=document.querySelectorAll("#rv-ac-head th"); for(var i=0;i<ths.length;i++){ var th=ths[i],sk=th.getAttribute("data-acsort"),arr=th.querySelector(".arr");
    if(sk===key){ th.setAttribute("aria-sort",dir<0?"descending":"ascending"); if(arr)arr.textContent=dir<0?"▼":"▲"; }
    else { th.removeAttribute("aria-sort"); if(arr)arr.textContent=""; } } }

function renderAcAutos(){ var body=document.getElementById("rv-ac-autos"); body.innerHTML="";
  var autos=(AC.automations||[]).filter(function(a){ return a.entered>0 || a.active; });
  document.getElementById("rv-ac-auto-sub").textContent=autos.length+" automation"+(autos.length===1?"":"s")+" with contacts, ranked by how many have entered.";
  autos.forEach(function(a){ var prog=a.entered-a.exited; if(prog<0)prog=0; var comp=a.entered?Math.round(a.exited/a.entered*100):0; var tr=document.createElement("tr");
    tr.innerHTML='<td><div class="rv-adname"><b>'+esc(a.name)+'</b></div></td>'+
      '<td><span class="rv-abadge '+(a.active?"on":"off")+'">'+(a.active?"Active":"Off")+'</span></td>'+
      '<td>'+num(a.entered)+'</td><td>'+num(a.exited)+'</td><td>'+num(prog)+'</td><td>'+comp+'%</td>'; body.appendChild(tr); });
  if(!autos.length){ var tr=document.createElement("tr"); tr.innerHTML='<td colspan="6" style="text-align:left;color:var(--muted)">No automations with contacts yet.</td>'; body.appendChild(tr); } }

function syncAcActive(){ var chips=document.querySelectorAll("#rv-ac-chips [data-acmetric]"); for(var i=0;i<chips.length;i++){ chips[i].setAttribute("aria-pressed",chips[i].getAttribute("data-acmetric")===acMetric?"true":"false"); }
  var tiles=document.querySelectorAll("#rv-ac-kpis [data-acmetric]"); for(var j=0;j<tiles.length;j++){ tiles[j].setAttribute("aria-pressed",tiles[j].getAttribute("data-acmetric")===acMetric?"true":"false"); } }

function renderAC(){
  var live=document.getElementById("rv-ac-live"), empty=document.getElementById("rv-ac-empty"), cnt=document.getElementById("rv-ac-tabcnt");
  if(!AC||!AC.enabled){ live.hidden=true; empty.hidden=false;
    document.getElementById("rv-ac-empty-msg").textContent="ActiveCampaign is not connected for this account yet"+((AC&&AC.error)?(" ("+AC.error+")"):"")+". Once the email key is configured, this tab fills in automatically.";
    if(cnt)cnt.textContent=""; return; }
  live.hidden=false; empty.hidden=true;
  var windowCamps=acCampsInWindow();
  // If the currently-selected campaign fell out of the new date range, drop the filter.
  if(acCampFilter){ var stillHere=false; windowCamps.forEach(function(c){ if(String(c.id)===String(acCampFilter))stillHere=true; }); if(!stillHere)acCampFilter=""; }
  renderAcCampaignSelect(windowCamps);
  var camps=acCampFilter?windowCamps.filter(function(c){ return String(c.id)===String(acCampFilter); }):windowCamps;
  lastAcCamps=camps;
  var t=acAgg(camps), rates=acRates(t);
  if(cnt)cnt.textContent=num((AC.totals&&AC.totals.campaigns_sent)||windowCamps.length);
  var fc=acCampFilter?camps[0]:null;
  document.getElementById("rv-ac-window").textContent="Email performance · "+acDate(rStart)+" – "+acDate(rEnd)+" · "+(fc?("1 campaign · "+fc.name):(windowCamps.length+" campaign"+(windowCamps.length===1?"":"s")+" sent"));
  document.getElementById("rv-ac-camp-sub").textContent=camps.length?((fc?("Showing 1 campaign — "+fc.name+". "):(camps.length+" campaign"+(camps.length===1?"":"s")+" sent in this window. "))+"Click a column to sort."):"No email campaigns were sent in this date range — widen the range above to see more.";
  syncAcCampFilter(fc);
  renderAcChips(); renderAcKpis(t,rates); renderAcHero(camps,acMetric,acView);
  renderAcLists(); renderAcBase(); renderAcEff(t,rates); renderAcTable(); renderAcAutos();
  var ac=document.getElementById("rv-ac-acct"); if(ac)ac.textContent=(AC.account||"ActiveCampaign");
  var chip=document.getElementById("rv-ac-acct-chips"); if(chip){ chip.innerHTML="";
    [["Contacts",num((AC.totals||{}).contacts||0)],["CRM",AC.crm_enabled?"Enabled":"Not enabled"],["Source","ActiveCampaign API"]].forEach(function(p){ var s=document.createElement("span"); s.className="rv-chip"; s.innerHTML=p[0]+": <b>"+esc(p[1])+"</b>"; chip.appendChild(s); }); }
  syncAcActive();
}
function setAcMetric(key){ if(!AC_METRICS[key])return; acMetric=key; renderAcHero(lastAcCamps,acMetric,acView); syncAcActive(); }
function setAcView(v){ acView=v; if(acView==="rate"&&AC_CHIP_RATE.indexOf(acMetric)<0)acMetric=AC_CHIP_RATE[0]; if(acView==="vol"&&AC_CHIP_VOL.indexOf(acMetric)<0)acMetric=AC_CHIP_VOL[0];
  renderAcChips(); renderAcHero(lastAcCamps,acMetric,acView); syncAcActive();
  var b=document.querySelectorAll("#rv-ac-view button"); for(var i=0;i<b.length;i++){ b[i].setAttribute("aria-pressed",b[i].getAttribute("data-acview")===v?"true":"false"); } }
function renderAcCampaignSelect(windowCamps){
  var sel=document.getElementById("rv-ac-campaign"); if(!sel)return;
  var ordered=windowCamps.slice().sort(function(a,b){ return a.date<b.date?1:(a.date>b.date?-1:0); }); // newest first
  var html='<option value="">All campaigns in range ('+windowCamps.length+')</option>';
  ordered.forEach(function(c){ var lbl=c.name+" — "+mday(c.date); html+='<option value="'+esc(String(c.id))+'"'+(String(c.id)===String(acCampFilter)?' selected':'')+'>'+esc(lbl)+'</option>'; });
  sel.innerHTML=html; sel.value=acCampFilter||"";
}
function syncAcCampFilter(fc){ var ban=document.getElementById("rv-ac-filter"); if(!ban)return;
  if(fc){ ban.classList.add("on"); document.getElementById("rv-ac-filter-name").textContent=fc.name; }
  else { ban.classList.remove("on"); } }
function setAcCampFilter(id){ acCampFilter=id||""; renderAC(); }
```

Tab switching + the "render both tabs" entry point:

```javascript
function setTab(name){ activeTab=name;
  document.getElementById("rv-tab-meta").hidden=(name!=="meta");
  document.getElementById("rv-tab-ac").hidden=(name!=="ac");
  var btns=document.querySelectorAll("#rv-tabs .rv-tab"); for(var i=0;i<btns.length;i++){ btns[i].setAttribute("aria-selected",btns[i].getAttribute("data-tab")===name?"true":"false"); }
  document.getElementById("rv-mast-title").textContent=(name==="ac")?"Email Performance":"Meta Ads Performance";
  if(name==="ac"){ renderAC(); } else { render(); }
}
function renderAll(){ render(); renderAC(); }
function renderAll(){ render(); renderAC(); }
```

---

## 5. Step 4 — Interactivity & the shared filters

### 5a. State variables

```javascript
var adFilter="";                 // when set, the ENTIRE Meta tab scopes to this one creative
var DOMAIN=[];                    // union of Meta + ActiveCampaign dates (drives the range picker)
var activeTab="meta";
var PAL=["var(--c1)","var(--c2)","var(--c3)","var(--c4)","var(--c5)","var(--c6)","var(--c7)"];

// ---- ActiveCampaign (email) config + state ----
var AC=(DATA && DATA.activecampaign) ? DATA.activecampaign : {enabled:false};
var AC_METRICS={ openrate:{label:"Open rate",short:"Open rate",unit:"pct",kind:"rate"},
  clickrate:{label:"Click rate",short:"Click rate",unit:"pct",kind:"rate"},
  ctor:{label:"Click-to-open",short:"CTOR",unit:"pct",kind:"rate"},
  unsubrate:{label:"Unsub rate",short:"Unsub rate",unit:"pct",kind:"rate"},
  sent:{label:"Recipients",short:"Recipients",unit:"int",kind:"vol"},
  opens:{label:"Unique opens",short:"Opens",unit:"int",kind:"vol"},
  clicks:{label:"Unique clicks",short:"Clicks",unit:"int",kind:"vol"} };
var AC_CHIP_RATE=["openrate","clickrate","ctor","unsubrate"];
var AC_CHIP_VOL=["sent","opens","clicks"];
// ---- ActiveCampaign (email) config + state ----
var AC=(DATA && DATA.activecampaign) ? DATA.activecampaign : {enabled:false};
var AC_METRICS={ openrate:{label:"Open rate",short:"Open rate",unit:"pct",kind:"rate"},
  clickrate:{label:"Click rate",short:"Click rate",unit:"pct",kind:"rate"},
  ctor:{label:"Click-to-open",short:"CTOR",unit:"pct",kind:"rate"},
  unsubrate:{label:"Unsub rate",short:"Unsub rate",unit:"pct",kind:"rate"},
  sent:{label:"Recipients",short:"Recipients",unit:"int",kind:"vol"},
  opens:{label:"Unique opens",short:"Opens",unit:"int",kind:"vol"},
  clicks:{label:"Unique clicks",short:"Clicks",unit:"int",kind:"vol"} };
var AC_CHIP_RATE=["openrate","clickrate","ctor","unsubrate"];
var AC_CHIP_VOL=["sent","opens","clicks"];
var acMetric="openrate", acView="rate", acSort={key:"date",dir:-1}, lastAcCamps=[], acCampFilter="";
```

### 5b. Ad-tab: click a creative to scope the whole tab

`computeRange()` honors a module-level `adFilter` (empty = all). Clicking a creative toggles it;
`render()` shows a banner and hides the demographics section (ad platforms rarely expose
per-creative demographics).

```javascript
function computeRange(s,e){
  var dts=DATA.dates.filter(function(d){ return d>=s && d<=e; });
  if(!dts.length){ dts=DATA.dates.slice(); }
  var set={}; dts.forEach(function(d){ set[d]=1; });
  var fr=DATA.rows.filter(function(r){ return set[r.date] && (!adFilter || r.ad===adFilter); });
  var kpi=aggregate(fr);
  var byd={}; dts.forEach(function(d){ byd[d]=[]; }); fr.forEach(function(r){ byd[r.date].push(r); });
  var daily=dts.map(function(d){ var b=aggregate(byd[d]); b.date=d; return b; });
  var bya={}; fr.forEach(function(r){ (bya[r.ad]=bya[r.ad]||[]).push(r); });
  var ads=Object.keys(bya).map(function(name){ var b=aggregate(bya[name]); b.ad=name; return b; });
  ads.sort(function(a,b){ return b.spend-a.spend; });
  return {start:dts[0],end:dts[dts.length-1],days:dts.length,kpi:kpi,daily:daily,ads:ads};
}
```

```javascript
function render(){
  var st=computeRange(rStart,rEnd); lastDaily=st.daily;
  var fm=adFilter?metaFor(adFilter):null;
  var scope=fm?(" · creative: "+(fm.label||adFilter)):"";
  document.getElementById("rv-window").textContent="Campaign at a glance · "+mday(st.start)+" – "+mday(st.end)+", 2026 · "+st.days+" days"+scope;
  document.getElementById("rv-through").textContent=mday(st.end)+", 2026";
  lastAds=st.ads; lastKpi=st.kpi;
  renderKpis(st.kpi,st.daily); renderHero(st.daily,heroMetric,viewMode);
  renderFunnel(st.kpi); renderDow(st.daily); renderCumulative(st.daily);
  renderDemographics(); renderGallery(st.ads); renderEff(st.kpi); renderTable(); renderContext(st.kpi); syncActive();
  syncAdFilter(fm);
}
function syncAdFilter(fm){
  var ban=document.getElementById("rv-ad-filter"), aud=document.getElementById("rv-audience-sec");
  if(fm){ ban.classList.add("on"); document.getElementById("rv-ad-filter-name").textContent=fm.title||fm.label||adFilter;
    var th=document.getElementById("rv-ad-filter-thumb"); if(fm.img){ th.src=fm.img; th.hidden=false; } else { th.hidden=true; }
    if(aud)aud.hidden=true; }
  else { ban.classList.remove("on"); if(aud)aud.hidden=false; }
}
function setAdFilter(ad){ adFilter=(adFilter===ad)?"":ad; render();
  if(adFilter){ var b=document.getElementById("rv-ad-filter"); if(b&&b.scrollIntoView)b.scrollIntoView({behavior:"smooth",block:"nearest"}); } }
```

### 5c. Email-tab: campaign dropdown filter, chips, toggle, subtle average line

These live inside the AC engine block (§4c): `renderAcCampaignSelect()` populates the `<select>`
from the windowed campaigns; `setAcCampFilter()` scopes every email KPI/chart/table to one
campaign and shows the "Filtered to campaign" banner; `setAcMetric()`/`setAcView()` drive the
chart; the average line is drawn faint (`stroke:var(--muted);stroke-opacity:.45`).

### 5d. Boot wiring (event listeners + the date domain)

Builds the shared `DOMAIN`, wires the date inputs to `renderAll()`, and attaches all tab / gallery
/ Email-control listeners. Call `renderAll()` at the end.

```javascript
  // ActiveCampaign block may only exist once data.json is fetched, so (re)read it here, then
  // build the shared date domain = union of Meta ad days + email send dates (drives the picker).
  AC=(DATA && DATA.activecampaign) ? DATA.activecampaign : {enabled:false};
  var domSet={}; DATA.dates.forEach(function(d){ domSet[d]=1; });
  if(AC.enabled && AC.campaigns){ AC.campaigns.forEach(function(c){ if(c.date)domSet[c.date]=1; }); }
  DOMAIN=Object.keys(domSet).sort(); if(!DOMAIN.length){ DOMAIN=DATA.dates.slice(); }

  var lo=DOMAIN[0], hi=DOMAIN[DOMAIN.length-1];
  var si=document.getElementById("rv-start"), ei=document.getElementById("rv-end");
  si.min=lo; si.max=hi; ei.min=lo; ei.max=hi; rStart=lo; rEnd=hi; si.value=lo; ei.value=hi;
  si.addEventListener("change",function(){ var v=si.value||lo; if(v<lo)v=lo; if(v>rEnd)v=rEnd; rStart=v; si.value=v; clearPresets(); renderAll(); });
  ei.addEventListener("change",function(){ var v=ei.value||hi; if(v>hi)v=hi; if(v<rStart)v=rStart; rEnd=v; ei.value=v; clearPresets(); renderAll(); });

  document.getElementById("rv-presets").addEventListener("click",function(ev){ var b=ev.target.closest?ev.target.closest("button"):ev.target; if(!b||b.getAttribute("data-days")===null)return; setPreset(parseInt(b.getAttribute("data-days"),10)); });
  document.getElementById("rv-view").addEventListener("click",function(ev){ var b=ev.target.closest?ev.target.closest("button"):ev.target; if(!b||!b.getAttribute("data-view"))return; setView(b.getAttribute("data-view")); });
  document.getElementById("rv-chips").addEventListener("click",function(ev){ var b=ev.target.closest?ev.target.closest("button"):ev.target; if(!b||!b.getAttribute("data-metric"))return; setMetric(b.getAttribute("data-metric")); });
  var kpi=document.getElementById("rv-kpis");
  function tileFrom(t){ while(t&&t!==kpi){ if(t.getAttribute&&t.getAttribute("data-metric"))return t; t=t.parentNode; } return null; }
  kpi.addEventListener("click",function(ev){ var t=tileFrom(ev.target); if(t){ setMetric(t.getAttribute("data-metric")); } });
  kpi.addEventListener("keydown",function(ev){ if(ev.key==="Enter"||ev.key===" "){ var t=tileFrom(ev.target); if(t){ ev.preventDefault(); setMetric(t.getAttribute("data-metric")); } } });

  // Tabs
  document.getElementById("rv-tabs").addEventListener("click",function(ev){ var b=ev.target.closest?ev.target.closest(".rv-tab"):ev.target; if(!b||!b.getAttribute("data-tab"))return; setTab(b.getAttribute("data-tab")); });

  // Creative gallery: clicking a card SCOPES the whole Meta dashboard to that creative (toggle);
  // the small "Detail" chip still opens the per-creative modal.
  var gal=document.getElementById("rv-gallery");
  function galHit(t){ while(t&&t!==gal){ if(t.getAttribute){ if(t.getAttribute("data-detail"))return {kind:"detail",ad:t.getAttribute("data-detail")}; if(t.getAttribute("data-ad"))return {kind:"card",ad:t.getAttribute("data-ad")}; } t=t.parentNode; } return null; }
  gal.addEventListener("click",function(ev){ var h=galHit(ev.target); if(!h)return; if(h.kind==="detail"){ ev.stopPropagation(); openCreative(h.ad); } else { setAdFilter(h.ad); } });
  gal.addEventListener("keydown",function(ev){ if(ev.key==="Enter"||ev.key===" "){ var h=galHit(ev.target); if(h&&h.kind==="card"){ ev.preventDefault(); setAdFilter(h.ad); } } });
  document.getElementById("rv-ad-filter-clear").addEventListener("click",function(){ adFilter=""; render(); });
  document.getElementById("rv-modal-close").addEventListener("click",closeCreative);
  document.getElementById("rv-modal").addEventListener("click",function(ev){ if(ev.target===this){ closeCreative(); } });
  document.addEventListener("keydown",function(ev){ if(ev.key==="Escape"){ closeCreative(); } });

  // sortable Meta table headers
  var head=document.getElementById("rv-ads-head");
  head.addEventListener("click",function(ev){ var th=ev.target; while(th&&th!==head){ if(th.getAttribute&&th.getAttribute("data-sort")){ var key=th.getAttribute("data-sort");
        if(tableSort.key===key){ tableSort.dir=-tableSort.dir; } else { tableSort.key=key; tableSort.dir=(key==="label")?1:-1; } renderTable(); return; } th=th.parentNode; } });

  // ActiveCampaign controls: campaign dropdown filter, metric chips, rate/volume toggle, KPI tiles, sortable table
  document.getElementById("rv-ac-campaign").addEventListener("change",function(){ setAcCampFilter(this.value); });
  document.getElementById("rv-ac-filter-clear").addEventListener("click",function(){ setAcCampFilter(""); });
  document.getElementById("rv-ac-view").addEventListener("click",function(ev){ var b=ev.target.closest?ev.target.closest("button"):ev.target; if(!b||!b.getAttribute("data-acview"))return; setAcView(b.getAttribute("data-acview")); });
  document.getElementById("rv-ac-chips").addEventListener("click",function(ev){ var b=ev.target.closest?ev.target.closest("button"):ev.target; if(!b||!b.getAttribute("data-acmetric"))return; setAcMetric(b.getAttribute("data-acmetric")); });
  var ackpi=document.getElementById("rv-ac-kpis");
  function acTileFrom(t){ while(t&&t!==ackpi){ if(t.getAttribute&&t.getAttribute("data-acmetric"))return t; t=t.parentNode; } return null; }
  ackpi.addEventListener("click",function(ev){ var t=acTileFrom(ev.target); if(t){ setAcMetric(t.getAttribute("data-acmetric")); } });
  ackpi.addEventListener("keydown",function(ev){ if(ev.key==="Enter"||ev.key===" "){ var t=acTileFrom(ev.target); if(t){ ev.preventDefault(); setAcMetric(t.getAttribute("data-acmetric")); } } });
  var achead=document.getElementById("rv-ac-head");
  achead.addEventListener("click",function(ev){ var th=ev.target; while(th&&th!==achead){ if(th.getAttribute&&th.getAttribute("data-acsort")){ var key=th.getAttribute("data-acsort");
        if(acSort.key===key){ acSort.dir=-acSort.dir; } else { acSort.key=key; acSort.dir=(key==="name")?1:-1; } renderAcTable(); return; } th=th.parentNode; } });

  renderAll();
}
```

---

## 6. Data shape reference (the contract)

The page reads exactly these keys. Match them by name end-to-end.

```jsonc
{
  // ... your existing ad-tab data: dates[], rows[], creatives[], demographics{age_gender[],region[]} ...
  // demographics rows now each carry a "date" (see 3b) so they can be windowed.

  "activecampaign": {
    "enabled": true,
    "account": "fortiuscap64937",
    "url": "https://fortiuscap64937.api-us1.com",
    "fetched": "2026-07-14",
    "crm_enabled": false,
    "error": "",
    "totals": { "contacts": 7101, "campaigns": 89, "campaigns_sent": 36, "lists": 16, "automations": 22 },
    "campaigns": [
      { "id": "150", "name": "Owner's List - DVS promo", "date": "2026-07-10",
        "sent": 14, "opens": 4, "opens_total": 5, "clicks": 1, "clicks_total": 1,
        "unsubs": 0, "bounces": 0, "forwards": 0, "replies": 0, "socialshares": 0 }
    ],
    "lists":       [ { "id": "5", "name": "Riverdance", "subscribers": 3130, "total": 3689 } ],
    "automations": [ { "id": "17", "name": "Open House", "entered": 571, "exited": 32, "active": true } ]
  }
}
```

Rates (open %, click %, CTOR, unsub %, bounce %) are **derived in the page** from these counters —
never stored — so every window and campaign-filter recomputes them correctly.

---

## 7. Deploy / secrets (generic)

1. **Store the token as a secret** (Secret Manager / Vault / repo secret). Never commit it. Write
   it with no trailing newline.
2. **Pass config to the build step:** `ACTIVECAMPAIGN_URL` (env) and `ACTIVECAMPAIGN_API_KEY`
   (secret mounted as env). Grant the build's identity read access to the secret.
3. **Rebuild + redeploy** the build step (so the JSON gains the `activecampaign` block) and the
   dashboard (so it renders the new tab). Omitting the config is safe — the tab shows a
   "not connected" state and the rest of the dashboard is unaffected.
4. **Rotate the token** if it was ever shared in plaintext (chat, tickets).

---

## 8. Verification checklist

- [ ] Build step emits `data.activecampaign.enabled === true` with real `campaigns[]`, `lists[]`,
      `automations[]`, and `totals`.
- [ ] Email tab is **second**; the badge shows the sent-campaign count.
- [ ] Changing the date range re-windows: email KPIs/chart/table AND ad-tab demographics (a narrow
      window shows smaller numbers than "Max").
- [ ] The campaign dropdown scopes the whole email tab; "Show all campaigns" clears it.
- [ ] Rates match a hand-calc on one campaign (opens ÷ recipients, etc.).
- [ ] The token never appears in any client-served response (`view-source`, network tab).
- [ ] If your repo has a JS syntax gate, the dashboard still passes it.
- [ ] (Nice) Headless-render the page and screenshot both tabs to confirm layout in light + dark.

---

### Appendix — account snapshots vs. windowed data

`contacts`, `lists`, and `automations` are **current snapshots** from ActiveCampaign (there is no
cheap historical time-series for them), so they intentionally do **not** shrink with the date
range — label them "in the database" / "current". Everything genuinely time-based (campaigns and
everything derived from them) does respond. Windowing those snapshots would require crawling
per-contact join/entry dates (`contactAutomations`, list-membership dates) — a much heavier pull;
add it only if a client specifically needs historical list growth.
