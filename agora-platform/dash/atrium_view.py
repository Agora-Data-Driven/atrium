"""Presentation helpers for the Agora Atrium workspace (pure functions, no I/O).

The Atrium template renders ALL data with Jinja in HTML and keeps inline <script> blocks free of
Jinja (so scripts/_validate_dash_js.py / esprima only ever sees real JS). A few things are awkward
to compute in a template -- the leads sparkline geometry, the month calendar grid, the cross-campaign
"awaiting" rollup -- so we compute them here in Python and pass the result in as a `view` dict.

Everything here is a pure function of the workspace dict + the current time, so it is trivially
testable and never touches GCS.
"""

import calendar as _calendar
import datetime


# --- Small derivations --------------------------------------------------------------------------
def initials(user):
    """Up to two uppercase initials for an avatar, derived from an email/login string."""
    if not user:
        return "?"
    local = user.split("@")[0]
    parts = [p for p in local.replace("_", ".").split(".") if p]
    if len(parts) >= 2:
        return (parts[0][:1] + parts[1][:1]).upper()
    return (local[:2] or "?").upper()


_GENERIC_MAILBOXES = {
    "info", "admin", "hello", "team", "contact", "support", "sales", "office", "owner",
}


def greeting_name(user, ws):
    """A friendly first name for the greeting -- the login's name, else the client's first word."""
    if user and "@" in user:
        local = user.split("@")[0]
        if local.lower() not in _GENERIC_MAILBOXES:
            return local.split(".")[0].split("_")[0].title()
    display = (ws.get("display_name") or "there").strip()
    return display.split()[0] if display else "there"


# --- Dashboard sparkline (geometry only; rendered as an inline <polyline>/<path>) ----------------
def sparkline(series, width=560, height=72, pad=8):
    """Return polyline points + an area path for a leads sparkline over `series`.

    Pure geometry -> the template drops `line`/`area` straight into an SVG, so no JS is needed.
    """
    values = [float(v) for v in (series or [])]
    n = len(values)
    if n == 0:
        return {"w": width, "h": height, "line": "", "area": "", "lastx": 0, "lasty": height}
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0
    inner_w = width - 2 * pad
    inner_h = height - 2 * pad
    step = inner_w / (n - 1) if n > 1 else 0.0

    pts = []
    for i, v in enumerate(values):
        x = pad + i * step
        y = pad + (1.0 - (v - lo) / span) * inner_h
        pts.append((round(x, 2), round(y, 2)))

    line = " ".join("%s,%s" % (x, y) for x, y in pts)
    baseline = height - pad
    area = "M %s,%s " % (pts[0][0], baseline)
    area += " ".join("L %s,%s" % (x, y) for x, y in pts)
    area += " L %s,%s Z" % (pts[-1][0], baseline)
    return {"w": width, "h": height, "line": line, "area": area,
            "lastx": pts[-1][0], "lasty": pts[-1][1]}


# --- "Where your leads came from" split bar -----------------------------------------------------
def split_percents(split):
    """Return paid/organic counts and integer percentages (summing to ~100)."""
    paid = int((split or {}).get("paid", 0))
    organic = int((split or {}).get("organic", 0))
    total = paid + organic
    if total <= 0:
        return {"paid": paid, "organic": organic, "total": 0, "paid_pct": 0, "organic_pct": 0}
    paid_pct = int(round(paid * 100.0 / total))
    return {"paid": paid, "organic": organic, "total": total,
            "paid_pct": paid_pct, "organic_pct": 100 - paid_pct}


# --- Cross-campaign rollups (Overview) ----------------------------------------------------------
def _channel_tab(channel):
    """The tab a piece of content lives under, by channel."""
    return "leadgen" if channel == "paid" else "organic"


def awaiting_items(ws):
    """Every content piece still 'awaiting', flattened with its campaign + target tab."""
    out = []
    for camp in ws.get("campaigns", []):
        for item in camp.get("content", []):
            if item.get("status") == "awaiting":
                out.append({
                    "ref": item.get("ref", item.get("id", "")),
                    "type_tag": item.get("type_tag", ""),
                    "platform": item.get("platform", ""),
                    "channel": camp.get("channel", ""),
                    "campaign_name": camp.get("name", ""),
                    "tab": _channel_tab(camp.get("channel", "")),
                })
    return out


# --- Month calendar grid ------------------------------------------------------------------------
_MONTHS = ["", "January", "February", "March", "April", "May", "June",
           "July", "August", "September", "October", "November", "December"]


def _display_month(events, today):
    """Pick the month to show: today's month if it has events, else the earliest event's month."""
    in_today = [e for e in events if str(e.get("date", "")).startswith(today.strftime("%Y-%m"))]
    if in_today or not events:
        return today.year, today.month
    earliest = min(str(e.get("date", "")) for e in events if e.get("date"))
    return int(earliest[0:4]), int(earliest[5:7])


def month_grid(events, today):
    """Build a Sunday-start month grid for the relevant month, mapping events onto day cells.

    Returns {label, year, month, weeks:[[cell,...]]} where each cell has day/in_month/is_today and
    the events landing on that date.
    """
    events = events or []
    year, month = _display_month(events, today)
    by_date = {}
    for e in events:
        by_date.setdefault(str(e.get("date", "")), []).append(e)

    cal = _calendar.Calendar(firstweekday=6)  # 6 = Sunday
    weeks = []
    for week in cal.monthdatescalendar(year, month):
        cells = []
        for d in week:
            iso = d.isoformat()
            cells.append({
                "day": d.day,
                "in_month": d.month == month,
                "is_today": d == today,
                "iso": iso,
                "events": by_date.get(iso, []),
            })
        weeks.append(cells)
    return {"label": "%s %d" % (_MONTHS[month], year), "year": year, "month": month, "weeks": weeks}


# --- The full view context ----------------------------------------------------------------------
def build(ws, client, user, active_tab, now=None):
    """Assemble the `view` dict the Atrium template needs beyond the raw workspace `ws`."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    today = now.date()
    awaiting = awaiting_items(ws)
    return {
        "client": client,
        "active_tab": active_tab,
        "initials": initials(user),
        "greeting_name": greeting_name(user, ws),
        "spark": sparkline(ws.get("series", [])),
        "split": split_percents(ws.get("split", {})),
        "awaiting_total": len(awaiting),
        "attention": awaiting,
        "campaigns_live": len(ws.get("campaigns", [])),
        "calendar": month_grid(ws.get("calendar", []), today),
    }
