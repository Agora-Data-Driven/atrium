"""staff_roster — ONE roster entry per human, and the refusals that keep it honest.

Run: python _staff_roster_localtest.py   (pure, no network, no GCS, no Flask)

🔴 The reported bug, in one line: the delivery board's person filter listed **"Nico" and "Nico
Agustin"** (and "Samuel" / "Samuel Delos Santos") as two people, because the old merge deduped on
exact name or exact email only. Work assigned to one id was invisible when filtering by the other.

What is pinned here:
  * the four-rung ladder merges those two entries into ONE human;
  * 🔴 an AMBIGUOUS rung refuses — two real staff sharing a first name stay two people;
  * every historic id survives as an ALIAS, so assignments stored before the change still resolve;
  * nobody is ever dropped by merging;
  * Sentinel wins the canonical id and the display name, and `fallback_only` reproduces the old
    roster exactly for a Sentinel outage.
"""
from __future__ import annotations

import sys

import staff_roster

_fails = []


def _check(label, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + label)
    if not cond:
        _fails.append(label)


ATRIUM_TEAM = (
    {"name": "Charles", "email": "charles@100.digital"},
    {"name": "Nico", "email": "nico@agoradatadriven.com"},
    {"name": "Paulo", "email": "paulo@agoradatadriven.com"},
    {"name": "Samuel", "email": "samuel@agoradatadriven.com"},
)


def _acct(email, name, role="admin", status="active"):
    return {"email": email, "name": name, "role": role, "status": status}


def _by_name(people, name):
    return [p for p in people if p.name == name]


print("[staff_roster] the reported duplicate")
# The live shape: ATRIUM_TEAM says "Nico"; the portal account is "Nico Agustin" on a personal Gmail
# (the exact address Sentinel's own AGENTS.md records for this person).
people = staff_roster.build(
    [{"email": "nico@agora.ph", "name": "Nico Agustin", "role": "employee"}],
    [_acct("agustinnico228@gmail.com", "Nico Agustin")],
    ATRIUM_TEAM,
)
nicos = [p for p in people if "nico" in p.name.lower()]
_check("Nico appears exactly ONCE, not as 'Nico' + 'Nico Agustin'", len(nicos) == 1)
_check("all three of that person's ids resolve to them",
       nicos and nicos[0].aliases >= {"nico@agora.ph", "agustinnico228@gmail.com",
                                      "nico@agoradatadriven.com"})
_check("Sentinel's email is the canonical id", nicos and nicos[0].id == "nico@agora.ph")
_check("Sentinel's name wins the display name", nicos and nicos[0].name == "Nico Agustin")

aliases = staff_roster.alias_map(people)
_check("a card stored under the OLD Atrium id still resolves to that person",
       staff_roster.canonical(aliases, "nico@agoradatadriven.com") == "nico@agora.ph")
_check("...and so does the portal-account id",
       staff_roster.canonical(aliases, "agustinnico228@gmail.com") == "nico@agora.ph")
_check("canonical() is case-insensitive on stored ids",
       staff_roster.canonical(aliases, "Nico@AgoraDataDriven.com") == "nico@agora.ph")

names = staff_roster.name_map(people)
_check("the HISTORIC id maps to the display name too (a card renders a name, not an email)",
       names.get("nico@agoradatadriven.com") == "Nico Agustin")

print("[staff_roster] the second reported duplicate (a surname, not a nickname)")
people = staff_roster.build(
    [{"email": "samuel@agora.ph", "name": "Samuel Delos Santos", "role": "employee"}],
    [], ATRIUM_TEAM)
_check("Samuel / Samuel Delos Santos merge into one human",
       len([p for p in people if "samuel" in p.name.lower()]) == 1)

print("[staff_roster] the refusals")
# 🔴 Two REAL staff sharing a first name must stay two people. Fusing them would attribute one
# person's work to the other on the board a manager staffs from — strictly worse than a duplicate.
people = staff_roster.build(
    [{"email": "nico.a@agora.ph", "name": "Nico Agustin"},
     {"email": "nico.b@agora.ph", "name": "Nico Bautista"}],
    [], ({"name": "Nico", "email": "nico@agoradatadriven.com"},))
_check("two Sentinel users called Nico stay TWO people",
       len([p for p in people if p.in_sentinel]) == 2
       and len(_by_name(people, "Nico Agustin")) == 1
       and len(_by_name(people, "Nico Bautista")) == 1)
_check("neither of them absorbed the other's id",
       staff_roster.canonical(staff_roster.alias_map(people), "nico.a@agora.ph") != \
       staff_roster.canonical(staff_roster.alias_map(people), "nico.b@agora.ph"))
_check("the ambiguous legacy 'Nico' is kept as its OWN entry, never fused into one of them",
       len([p for p in people if p.name == "Nico"]) == 1)
_check("...and nobody is lost to the ambiguity", len(people) == 3)

print("[staff_roster] merging never drops anybody")
people = staff_roster.build(
    [{"email": "zhen@agora.ph", "name": "Zhen"}],
    [_acct("charles@100.digital", "Charles"), _acct("outsider@elsewhere.com", "Outsider")],
    ATRIUM_TEAM)
_check("a portal admin who is NOT in Sentinel still appears",
       len(_by_name(people, "Outsider")) == 1)
_check("an ATRIUM_TEAM member with no Sentinel row still appears",
       len(_by_name(people, "Paulo")) == 1)
_check("every id fed in is an alias of exactly one person out",
       len(staff_roster.alias_map(people)) >= 6)
_check("the roster is sorted by name",
       [p["name"] for p in staff_roster.roster(people)]
       == sorted([p["name"] for p in staff_roster.roster(people)], key=str.lower))

print("[staff_roster] client logins and inactive accounts are not the delivery team")
people = staff_roster.build([], [
    _acct("aclient@corp.com", "A Client", role="client"),
    _acct("gone@100.digital", "Gone", status="disabled"),
    _acct("pending@100.digital", "Pending Person", status="pending"),
], ())
_check("a client-role login is never on the roster", not _by_name(people, "A Client"))
_check("a disabled account is off the roster", not _by_name(people, "Gone"))
# An invited-but-not-yet-activated teammate is a real person you may assign work to; dropping them
# silently is what caused the original "not all names show up" report.
_check("a PENDING admin is still assignable", len(_by_name(people, "Pending Person")) == 1)

print("[staff_roster] a Sentinel outage reproduces the old roster")
# 🔴 `list_people()` returning None means "couldn't ask", never "no staff". fallback_only builds from
# the two LOCAL sources alone — an empty person filter would be far worse than a stale one.
people = staff_roster.build(None, [_acct("charles@100.digital", "Charles")], ATRIUM_TEAM,
                            fallback_only=True)
_check("the fallback roster still carries the whole local team", len(people) == len(ATRIUM_TEAM))
_check("the fallback roster contains no Sentinel-sourced person",
       all(not p.in_sentinel for p in people))
_check("a Sentinel row is ignored entirely when fallback_only is set",
       not staff_roster.build([{"email": "x@agora.ph", "name": "Ghost"}], [], (),
                             fallback_only=True))

print("[staff_roster] unknown ids pass through rather than blanking")
aliases = staff_roster.alias_map(staff_roster.build([], [], ATRIUM_TEAM))
_check("somebody who has left the roster still owns their work",
       staff_roster.canonical(aliases, "departed@old.com") == "departed@old.com")
_check("an empty id stays empty (that is 'Unassigned', not a person)",
       staff_roster.canonical(aliases, "") == "" and staff_roster.canonical(aliases, None) == "")

print()
if _fails:
    print("[staff_roster] FAIL — %d check(s):" % len(_fails))
    for f in _fails:
        print("   - " + f)
    sys.exit(1)
print("[staff_roster] PASS")
