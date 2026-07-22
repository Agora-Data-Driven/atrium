"""Parse a copy-pasted Upwork message thread into an ordered, role-tagged, de-duplicated list of
messages -- so a team member can paste the raw Upwork chat and have Atrium turn it into a real
conversation (a full-thread reader card + an AI recap) in the Communications timeline.

Pure + infra-free: no network, no storage, no AI. `parse_upwork` is a small state machine over the
pasted lines; `main.py` calls it, stores the result like a Mail thread archive object (so the
EXISTING thread-reader modal renders it), and asks the Mail brain for the recap. Tested off-cloud by
`_upwork_import_localtest.py`.

What the raw Upwork paste looks like (representative):

    Saturday, Jul 11          <- day separator (no year)
    DM                        <- sender avatar initials (noise)
    Daniela Marquez           <- sender name
    12:59 AM                  <- time  => a real message starts here
    Hi Ian! ...               <- body (may span many lines / blank lines)
    Ian Gabriel Fernandez
    10:55 PM
    Hi Daniela, got it. ...
    ...
    Ian Gabriel Fernandez
    Jul 11, 2026 | 10:55 PM   <- FULL date w/ pipe => a QUOTED reply (a repeat of an earlier
    Hi Daniela, got it. ...      message Upwork shows above the reply); dropped as a duplicate.
    Show more                 <- end-of-quote marker
    2 files                   <- attachment run: a count line, then filenames + sizes
    email_1.pdf
    399 kB

Role assignment is deterministic: a sender whose name matches one of the team's Upwork display
names is `agora`; everyone else is `client`.
"""

import re
from datetime import date, datetime

_DAYS = "Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday"
_MONTHS = {m: i + 1 for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])}

RE_TIME = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*(AM|PM)\s*$", re.I)
RE_FULLDATE_PIPE = re.compile(
    r"^\s*[A-Z][a-z]{2,8}\s+\d{1,2},\s+\d{4}\s*\|\s*\d{1,2}:\d{2}\s*(AM|PM)\s*$", re.I)
RE_DAY = re.compile(r"^\s*(?:%s),\s+([A-Z][a-z]{2,8})\s+(\d{1,2})\s*$" % _DAYS)
RE_ATTACH_COUNT = re.compile(r"^\s*\d+\s+files?\s*$", re.I)
RE_SIZE = re.compile(r"^\s*\d+(?:\.\d+)?\s*(?:bytes|[kKmMgG]B)\s*$")
RE_FILENAME = re.compile(
    r"^\S.*\.(?:pdf|jpe?g|png|gif|webp|docx?|xlsx?|pptx?|csv|txt|zip|mp4|mov)\s*$", re.I)
RE_SHOW_MORE = re.compile(r"^\s*Show more\s*$", re.I)
# Upwork sprinkles SYSTEM-EVENT lines into a thread -- "<Name> sent an offer", "withdrew an offer",
# "accepted an offer", "sent you an invitation", "started the contract", etc. Each is followed by a
# time + a blurb, so the parser used to mistake them for chat messages (they polluted the message
# list AND the derived participants/title). A sender-position line matching this is treated as an
# event and dropped. A real display name never carries one of these verb+noun phrases.
RE_EVENT = re.compile(
    r"\b(?:sent|sends|withdrew|withdraws|updated|updates|accepted|accepts|declined|declines|"
    r"cancell?ed|cancels|ended|ends|started|starts|reopened|paused|closed|submitted|released|"
    r"requested|approved)\b.{0,24}\b(?:offer|contract|invitation|invite|milestone|proposal|"
    r"interview|job|payment|hours?)\b", re.I)


def _norm_names(agora_names):
    """A set of lowercased team display names + their first tokens (so 'Ian' matches 'Ian Gabriel
    Fernandez'). Accepts a list or a comma/newline/semicolon string."""
    if isinstance(agora_names, str):
        agora_names = re.split(r"[,\n;]", agora_names)
    names = set()
    for n in (agora_names or []):
        n = (n or "").strip().lower()
        if n:
            names.add(n)
    return names


def _is_agora(sender, agora_set):
    """True if `sender` is on the team. Matches the full name OR the first name (Upwork shows full
    display names, the operator may type just a first name)."""
    s = (sender or "").strip().lower()
    if not s:
        return False
    if s in agora_set:
        return True
    first = s.split()[0] if s.split() else s
    return first in agora_set


def _iso(month, day, year, hh, mm, ampm):
    """Build a sortable 'YYYY-MM-DDTHH:MM' from a day-header month/day + a 12h time, or '' if we
    don't have a day context yet."""
    if not month:
        return ""
    h = int(hh) % 12
    if ampm.upper() == "PM":
        h += 12
    try:
        return datetime(year, month, int(day), h, int(mm)).strftime("%Y-%m-%dT%H:%M")
    except ValueError:
        return ""


def sort_messages(messages):
    """Return `messages` ordered strictly oldest->newest by parsed date. Stable: an undated line
    keeps its neighbour's slot via a forward-filled key, and equal keys preserve the input order."""
    last_seen = ""
    keyed = []
    for idx, m in enumerate(messages or []):
        d = m.get("date") or last_seen
        if m.get("date"):
            last_seen = m["date"]
        keyed.append((d, idx, m))
    keyed.sort(key=lambda t: (t[0], t[1]))
    return [t[2] for t in keyed]


def _clean_body(buf):
    """Join a message's buffered lines, collapsing runs of blank lines to a single blank and
    trimming the ends."""
    text = "\n".join(buf)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def parse_upwork(raw, agora_names=None, year=None):
    """Parse pasted Upwork text into a conversation.

    Returns a dict:
      messages            [{from, to:"", date, role, body}, ...] in chat order (oldest first)
      participants        every distinct sender, in first-seen order
      client_participants senders NOT on the team
      agora_participants  senders on the team
      latest_date         the most recent message's ISO date (for the timeline card's date)
      title               a suggested title ('Upwork conversation with <client names>')
    """
    year = year or date.today().year
    agora_set = _norm_names(agora_names)
    lines = (raw or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    n = len(lines)

    messages = []
    cur = None            # the message being built: {..., "_buf": [...], "_attach": [...]}
    month = dayno = None  # current day-header context

    def next_nonempty(idx):
        k = idx
        while k < n and not lines[k].strip():
            k += 1
        return k if k < n else None

    def flush():
        # Emit the in-progress message if it carried any content.
        if cur is None:
            return
        body = _clean_body(cur.pop("_buf"))
        attach = cur.pop("_attach")
        if attach:
            body = (body + "\n" if body else "") + "\U0001F4CE " + ", ".join(attach)
        if body:
            cur["body"] = body
            messages.append(cur)

    def skip_quote(start):
        # A quoted reply block duplicates an earlier message -- skip it. Resume at the next real
        # boundary: a day header, a new message (sender + time), or just past a 'Show more'.
        k = start
        while k < n:
            s = lines[k].strip()
            if not s:
                k += 1
                continue
            if RE_SHOW_MORE.match(s):
                return k + 1
            if RE_DAY.match(s):
                return k
            j = next_nonempty(k + 1)
            if j is not None and RE_TIME.match(lines[j].strip()):
                return k
            k += 1
        return k

    i = 0
    while i < n:
        ln = lines[i].strip()
        if not ln:
            if cur is not None:
                cur["_buf"].append("")
            i += 1
            continue

        md = RE_DAY.match(ln)
        if md:
            flush()
            cur = None
            month = _MONTHS.get(md.group(1)[:3].title())
            dayno = int(md.group(2))
            i += 1
            continue

        # Is this line a sender name? (the next non-empty line tells us.)
        j = next_nonempty(i + 1)
        if j is not None:
            nl = lines[j].strip()
            if RE_FULLDATE_PIPE.match(nl):
                flush()
                cur = None
                i = skip_quote(j + 1)   # drop the quoted duplicate
                continue
            mt = RE_TIME.match(nl)
            if mt:
                flush()
                # An Upwork system event ("<Name> sent an offer") sits in the sender slot with a
                # timestamp under it -- skip the header + time; its blurb drops as preamble until
                # the next real message (cur is None, so buffered lines are discarded).
                if RE_EVENT.search(ln):
                    cur = None
                    i = j + 1
                    continue
                sender = ln
                cur = {
                    "from": sender, "to": "",
                    "date": _iso(month, dayno, year, mt.group(1), mt.group(2), mt.group(3)),
                    "role": "agora" if _is_agora(sender, agora_set) else "client",
                    "_buf": [], "_attach": [],
                }
                i = j + 1
                continue

        if RE_ATTACH_COUNT.match(ln) or RE_SIZE.match(ln):
            i += 1                      # a "2 files" marker / a size line -- skip
            continue
        if RE_FILENAME.match(ln):
            if cur is not None:
                cur["_attach"].append(ln)
            i += 1
            continue
        if RE_SHOW_MORE.match(ln):
            i += 1
            continue

        if cur is not None:
            cur["_buf"].append(ln)
        # else: preamble before the first message (e.g. a stray 'DM') -- drop it.
        i += 1

    flush()

    # Order strictly oldest->newest by timestamp (a pasted thread is usually chronological, but a
    # quoted reply-back or a copy that skips around can land a message out of order).
    messages = sort_messages(messages)

    participants, seen = [], set()
    for m in messages:
        s = m.get("from") or ""
        if s and s not in seen:
            seen.add(s)
            participants.append(s)
    client_participants = [s for s in participants
                           if not _is_agora(s, agora_set)]
    agora_participants = [s for s in participants if _is_agora(s, agora_set)]
    latest_date = max((m.get("date") or "" for m in messages), default="")

    who = ", ".join(client_participants) if client_participants else "the client"
    title = "Upwork conversation with %s" % who

    return {
        "messages": messages,
        "participants": participants,
        "client_participants": client_participants,
        "agora_participants": agora_participants,
        "latest_date": latest_date,
        "title": title,
    }


def _msg_sig(m):
    """A dedupe key for a message: its date + sender + trimmed body. Two pastes of the same
    conversation produce identical signatures for the overlapping messages."""
    return ((m.get("date") or ""), (m.get("from") or "").strip().lower(),
            (m.get("body") or "").strip())


def merge_messages(existing, incoming):
    """Union `existing` with `incoming`, dropping duplicates by signature, ordered oldest->newest.
    Returns (merged, added_count) where added_count = how many incoming messages were genuinely new.
    This is how "paste the fuller thread to add newer messages" folds into the stored conversation
    without creating duplicates."""
    seen = set()
    merged = []
    for m in (existing or []):
        sig = _msg_sig(m)
        if sig in seen:
            continue
        seen.add(sig)
        merged.append(m)
    added = 0
    for m in (incoming or []):
        sig = _msg_sig(m)
        if sig in seen:
            continue
        seen.add(sig)
        merged.append(m)
        added += 1
    return sort_messages(merged), added


def normalize_chat_thread(thread, agora_names=None):
    """Re-clean a STORED chat thread in place so an older import renders correctly without a
    re-paste. Idempotent. It (1) drops any message whose sender is an Upwork system event, (2)
    RE-TAGS each message's role from `agora_names` (so the team lands on the right even if the
    original import had no team name), (3) re-orders oldest->newest, and (4) recomputes
    participants + an auto-generated subject from the surviving client names.

    Returns True if anything changed. A subject the operator clearly hand-wrote (one that does NOT
    start with the auto "Upwork conversation with " prefix) is left untouched.
    """
    msgs = thread.get("messages") or []
    if not msgs:
        return False
    agora_set = _norm_names(agora_names)
    before = list(msgs)

    kept = [m for m in msgs if not RE_EVENT.search(m.get("from") or "")]
    for m in kept:
        m["role"] = "agora" if _is_agora(m.get("from"), agora_set) else "client"
    kept = sort_messages(kept)

    participants, seen = [], set()
    for m in kept:
        s = m.get("from") or ""
        if s and s not in seen:
            seen.add(s)
            participants.append(s)
    client_participants = [s for s in participants if not _is_agora(s, agora_set)]
    latest_date = max((m.get("date") or "" for m in kept), default="")

    changed = kept != before or thread.get("participants") != participants
    thread["messages"] = kept
    thread["participants"] = participants
    thread["last_date"] = latest_date
    subject = thread.get("subject") or ""
    if subject.startswith("Upwork conversation with ") or not subject:
        who = ", ".join(client_participants) if client_participants else "the client"
        new_subject = "Upwork conversation with %s" % who
        if new_subject != subject:
            thread["subject"] = new_subject
            changed = True
    return changed


def fallback_summary(parsed):
    """A plain, AI-free recap for when no model is configured (or the call fails). States the
    participants, the message count, and the date span."""
    msgs = parsed.get("messages") or []
    if not msgs:
        return ""
    dates = sorted(m.get("date", "")[:10] for m in msgs if m.get("date"))
    span = ""
    if dates:
        span = dates[0] if dates[0] == dates[-1] else "%s to %s" % (dates[0], dates[-1])
    who = ", ".join(parsed.get("participants") or []) or "the participants"
    return "Imported Upwork conversation between %s: %d message%s%s." % (
        who, len(msgs), "" if len(msgs) == 1 else "s", (" (%s)" % span) if span else "")
