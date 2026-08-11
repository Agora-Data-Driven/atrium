"""ONE roster entry per HUMAN, merged across Sentinel and Atrium's two local sources.

🔴 The bug this removes (reported 2026-08-11). The delivery board's person filter listed the same
person twice — **"Nico" and "Nico Agustin"**, **"Samuel" and "Samuel Delos Santos"** — because
`main._team_roster()` merged the hardcoded `ATRIUM_TEAM` tuple with live portal accounts and deduped
on **exact name or exact email only**. `ATRIUM_TEAM` carries `Nico / nico@agoradatadriven.com`; the
live account is `Nico Agustin / agustinnico228@gmail.com`; neither key matched, so both were listed
under two different ids. Work assigned to one id was then invisible when filtering by the other, and
the filter read as unreliable.

The deeper cause is that Atrium kept **its own staff list at all**. The estate contract is that
**Sentinel owns EMPLOYEES** (its `users` table authorizes every login) and Atrium owns CLIENTS — and
the mirror-image of this was already fixed in the other direction, when Sentinel's hand-maintained
`clients` table was replaced by a mirror of Atrium's `/api/internal/clients`. So Sentinel's
`GET /api/internal/people` is the primary source here, and the local sources are now a FALLBACK plus
a way to keep pre-existing assignments resolvable.

## Identity, and why an `email ==` join is not enough

The same human has different addresses in the two systems. `ATRIUM_TEAM` alone spans
`@agoradatadriven.com`, `@100.digital` and `@bidbrain.com`; live portal accounts can be a personal
Gmail (which is how a lead displays as "Agustinnico228"); Sentinel's users are on another domain
again. So merging is a LADDER, and every rung refuses to act when the answer is ambiguous:

| # | Key | Example |
|---|---|---|
| 1 | exact email | `nico@agoradatadriven.com` == `nico@agoradatadriven.com` |
| 2 | email local part | `nico@agora.ph` -> `nico` |
| 3 | full display name | "Nico Agustin" |
| 4 | first name | "Nico Agustin" -> `nico` |

🔴 **Two people who share a rung merge into NOBODY at that rung** — they stay separate entries. Two
staff called Nico is a real possibility, and silently fusing them would attribute one person's work
to another on the board a manager staffs from. A visible duplicate is recoverable; a wrong
attribution is not. This is the same ladder, with the same refusal, that Sentinel's
`services/atrium_identity.py` already applies to resolve an Atrium lead onto a Sentinel user — the
two now agree by construction rather than by coincidence.

## Aliases are the whole reason this is safe to ship

Every stored `lead_id` / `support_ids` / sub-task `assignee_id` on every existing card is one of the
OLD ids. A merged person therefore carries **every** id that resolved to them (`aliases`), and
`canonical()` maps any of them back — so an assignment made last month still resolves to the same
human, the same name and the same avatar colour. **Nothing rewrites stored data**: this is a read-time
resolution, so it cannot corrupt a workspace and it can be reverted by reverting the code.

Pure module: no I/O, no Flask, no workspace import. `main.py` supplies the three source lists.
"""
from __future__ import annotations


def _norm(s) -> str:
    return (s or "").strip().lower()


def _local_part(email) -> str:
    return _norm(email).split("@")[0]


def _first_name(name) -> str:
    return _norm(name).split(" ")[0] if _norm(name) else ""


class Person:
    """One human, with every id that refers to them.

    `id` is the CANONICAL id — what the person filter's option value is and what a new assignment
    stores. `aliases` is every id (including `id`) that must resolve here, which is what keeps
    already-stored assignments working.
    """

    __slots__ = ("id", "name", "role", "aliases", "in_sentinel")

    def __init__(self, pid, name, role="", in_sentinel=False):
        self.id = pid
        self.name = name
        self.role = role
        self.aliases = {_norm(pid)} if pid else set()
        self.in_sentinel = in_sentinel

    def add_alias(self, pid):
        if pid:
            self.aliases.add(_norm(pid))

    def as_dict(self):
        # `id` + `name` is the shape every existing caller already consumes (the person filter's
        # options, the `names` map, and the roster the internal task envelope sends to Sentinel), so
        # switching the source changes no template and no far-side contract.
        return {"id": self.id, "name": self.name, "role": self.role,
                "in_sentinel": self.in_sentinel}


def _index(people, key_fn):
    """key -> [Person], for the rungs below. A key held by 2+ people is AMBIGUOUS and is skipped by
    `_match`, which is what makes 'two people called Nico' stay two people."""
    out: dict = {}
    for p in people:
        for alias in list(p.aliases):
            k = key_fn(alias, p)
            if k:
                out.setdefault(k, [])
                if p not in out[k]:
                    out[k].append(p)
    return out


def _match(people, email, name):
    """The person `(email, name)` unambiguously refers to, or None.

    Stops at the first rung that yields EXACTLY ONE candidate; a rung with several is skipped rather
    than guessed at, and if every rung is ambiguous the caller creates a separate entry.
    """
    em, nm = _norm(email), _norm(name)
    rungs = [
        (em, _index(people, lambda a, p: a)),                              # 1. exact email
        (_local_part(em), _index(people, lambda a, p: _local_part(a))),    # 2. email local part
        (nm, _index(people, lambda a, p: _norm(p.name))),                  # 3. full display name
        (_first_name(nm), _index(people, lambda a, p: _first_name(p.name))),  # 4. first name
    ]
    for key, idx in rungs:
        if not key:
            continue
        hits = idx.get(key) or []
        if len(hits) == 1:
            return hits[0]
    return None


def build(sentinel_people=None, accounts=None, atrium_team=(), *, fallback_only=False):
    """The merged roster, one entry per human, sorted by name.

    Sources, in PRECEDENCE order — earlier wins the canonical id and the display name:

      1. **Sentinel's active users** (`sentinel_directory.list_people`). The source of truth for
         staff, so its email becomes the canonical id and its name the display name.
      2. **Live portal admin/superadmin accounts** — they may hold an id an existing card already
         references, and a portal admin who is not in Sentinel is still someone the board must be
         able to show (an unmerged one is kept as their own entry rather than dropped).
      3. **`ATRIUM_TEAM`** — the legacy hand-maintained tuple, now purely a floor: it guarantees the
         historic ids stay resolvable and that the board still works with Sentinel unreachable.

    `fallback_only=True` is what a caller passes when Sentinel gave NO answer (`list_people()` ->
    None). It builds from sources 2+3 alone — i.e. exactly today's roster — because the alternative
    on a Sentinel blip is an empty person filter.

    🔴 A person is NEVER dropped by merging. An entry that matches nobody becomes its own Person; an
    entry that matches becomes an ALIAS of the person it matched. Total humans out <= total entries
    in, and every id in is an alias of exactly one person out.
    """
    people: list = []

    if not fallback_only:
        for r in (sentinel_people or []):
            email = (r.get("email") or "").strip()
            if not email:
                continue
            # Sentinel is source-of-truth, so its rows seed the list and never merge INTO an earlier
            # rung — two Sentinel rows are two real staff records, whatever they are called.
            people.append(Person(email, (r.get("name") or "").strip() or email.split("@")[0],
                                 r.get("role") or "", in_sentinel=True))

    for a in (accounts or []):
        if a.get("status") not in ("active", "pending"):
            continue
        if a.get("role") not in ("admin", "superadmin"):
            continue      # a client-role login is not a member of the delivery team
        email = (a.get("email") or "").strip()
        name = (a.get("name") or "").strip() or (
            email.split("@")[0].split(".")[0].title() if email else "?")
        hit = _match(people, email, name)
        if hit is not None:
            hit.add_alias(email)      # same human, second address — keep the id resolvable
            continue
        people.append(Person(email, name, a.get("role") or ""))

    for m in (atrium_team or ()):
        email, name = (m.get("email") or "").strip(), (m.get("name") or "").strip()
        hit = _match(people, email, name)
        if hit is not None:
            hit.add_alias(email)
            continue
        people.append(Person(email, name))

    people.sort(key=lambda p: (p.name or "").lower())
    return people


def roster(people):
    """The [{id, name, role, in_sentinel}] list the templates and the internal envelope consume."""
    return [p.as_dict() for p in people]


def alias_map(people) -> dict:
    """Every known id (lower-cased) -> the canonical id it resolves to.

    This is what lets a card assigned under an OLD id still match the filter and still render the
    right name: `main._task_board` maps each stored owner id through it when building the card's
    filter haystack.
    """
    out = {}
    for p in people:
        for alias in p.aliases:
            out[alias] = p.id
    return out


def name_map(people) -> dict:
    """Canonical id -> display name, AND every alias -> the same name.

    Both directions are needed because a stored owner id may be an alias while the filter's option
    value is canonical, and `_person_name` is called with whichever the data happens to hold.
    """
    out = {}
    for p in people:
        out[p.id] = p.name
        for alias in p.aliases:
            out.setdefault(alias, p.name)
    return out


def canonical(aliases: dict, pid) -> str:
    """The canonical id for a stored owner id, or the id itself when it is unknown.

    Unknown ids pass THROUGH rather than becoming "" — a person who has left the roster still owns
    the work they were assigned, and blanking them would make historic cards read as Unassigned.
    """
    if not pid:
        return ""
    return aliases.get(_norm(pid)) or pid
