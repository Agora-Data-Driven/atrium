# -*- coding: utf-8 -*-
"""Incremental Telegram pull for the Agora Upwork-demand pipeline.

Replaces the manual "Telegram Desktop -> export chat history as JSON" step:
connects to Telegram as YOUR user account (MTProto via Telethon -- the Bot API
cannot read another bot's chat), pulls every message newer than the last pull
from the Zenfl Upwork Bot chat, and appends them to raw_files/pulled/*.jsonl
in the SAME shape the Desktop export uses (text_entities/inline_bot_buttons/
date), so process_upwork.py parses them with the exact same code path.

Files (all under raw_files/, all gitignored):
    telegram_api.json   {"api_id": 123, "api_hash": "...", "chat": "Zenfl Upwork Bot"}
                        -- create once from https://my.telegram.org -> API development tools
    telegram.session    Telethon session (created by --login; treat like a password)
    pull_state.json     {"last_id": N, ...} watermark; delete to re-bootstrap
    pulled/*.jsonl      one export-shaped message per line, one file per pull

Usage:
    python telegram_pull.py --login     one-time interactive sign-in (phone + code)
    python telegram_pull.py             incremental pull (prints NEW_MESSAGES=<n>)
    python telegram_pull.py --since 2026-07-13   first pull when no base export exists

First run bootstraps the watermark from the tail of raw_files/result.json (the
last exported message id), so only messages after the manual export are pulled.

Exit codes: 0 = ok, 2 = setup/login needed (refresh_upwork.ps1 surfaces this).
"""

import argparse
import glob
import json
import os
import re
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
# Every path is env-overridable so the SAME script runs on a laptop (defaults)
# and in the Cloud Run job (raw files on a GCS volume, session from a secret).
RAW_DIR = os.environ.get("TG_RAW_DIR", os.path.join(HERE, "..", "raw_files"))
BASE_EXPORT = os.environ.get("TG_BASE_EXPORT", os.path.join(RAW_DIR, "result.json"))
API_FILE = os.environ.get("TG_API_FILE", os.path.join(RAW_DIR, "telegram_api.json"))
SESSION = os.environ.get("TG_SESSION", os.path.join(RAW_DIR, "telegram"))  # + .session
STATE_FILE = os.environ.get("TG_STATE_FILE", os.path.join(RAW_DIR, "pull_state.json"))
PULLED_DIR = os.environ.get("TG_PULLED_DIR", os.path.join(RAW_DIR, "pulled"))

BOT_NAME = "Zenfl Upwork Bot"   # what process_upwork.py matches on ("from")

# Message dates must match the Desktop export's convention (Manila local time)
# regardless of where this runs — the Cloud Run container is UTC.
try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo(os.environ.get("TG_TZ", "Asia/Manila"))
except Exception:  # no tzdata: fall back to the machine's local time
    LOCAL_TZ = None

# Telethon entity class -> Telegram-Desktop-export text_entities type.
# Anything unmapped keeps its text as "plain" (the parser only reacts to
# bold / italic / blockquote / plain anyway).
ENTITY_TYPES = {
    "MessageEntityBold": "bold",
    "MessageEntityItalic": "italic",
    "MessageEntityBlockquote": "blockquote",
    "MessageEntityTextUrl": "text_link",
    "MessageEntityUrl": "link",
    "MessageEntityCode": "code",
    "MessageEntityPre": "pre",
    "MessageEntityUnderline": "underline",
    "MessageEntityStrike": "strikethrough",
    "MessageEntityMention": "mention",
    "MessageEntityHashtag": "hashtag",
    "MessageEntityEmail": "email",
}


def to_text_entities(text, entities):
    """Rebuild the Desktop export's flat text_entities list from a Telethon
    message. Telegram entity offsets/lengths are UTF-16 code units, so all
    slicing happens on the utf-16-le encoding. Nested entities keep only the
    OUTERMOST span (a blockquote swallows inner bolds -- parse_job wants the
    blockquote text whole; a link inside a bold title stays inside the bold
    text, and the title join doesn't care)."""
    if not text:
        return []
    b16 = text.encode("utf-16-le")
    n16 = len(b16) // 2

    def cut(lo, hi):
        return b16[lo * 2:hi * 2].decode("utf-16-le")

    spans = []
    for e in entities or []:
        typ = ENTITY_TYPES.get(type(e).__name__)
        if typ is None:
            continue  # custom emoji etc.: text stays, as part of a plain gap
        spans.append((e.offset, min(e.offset + e.length, n16), typ,
                      getattr(e, "url", None)))
    spans.sort(key=lambda s: (s[0], -s[1]))

    out, pos = [], 0
    for lo, hi, typ, url in spans:
        if lo < pos:        # nested/overlapping inside the previous span
            continue        # keep the outermost only
        if lo > pos:
            out.append({"type": "plain", "text": cut(pos, lo)})
        ent = {"type": typ, "text": cut(lo, hi)}
        if typ == "text_link" and url:
            ent["href"] = url
        out.append(ent)
        pos = hi
    if pos < n16:
        out.append({"type": "plain", "text": cut(pos, n16)})
    return out


def to_export_msg(msg):
    """Telethon Message -> the Desktop-export dict shape parse_job() reads."""
    out = {
        "id": msg.id,
        "type": "message",
        "date": msg.date.astimezone(LOCAL_TZ).strftime("%Y-%m-%dT%H:%M:%S"),
        "from": BOT_NAME,
        "text_entities": to_text_entities(msg.message or "", msg.entities),
    }
    rows = []
    markup = getattr(msg, "reply_markup", None)
    for row in getattr(markup, "rows", None) or []:
        btns = []
        for btn in getattr(row, "buttons", None) or []:
            if getattr(btn, "url", None):
                btns.append({"type": "url", "text": btn.text or "", "data": btn.url})
        if btns:
            rows.append(btns)
    if rows:
        out["inline_bot_buttons"] = rows
    return out


def bootstrap_last_id():
    """Watermark for the very first pull: the last message id in the manual
    base export (messages are chronological, so the max id in the file tail
    is the newest). Reads only the last few MB of the ~1 GB file."""
    if not os.path.exists(BASE_EXPORT):
        return None
    size = os.path.getsize(BASE_EXPORT)
    with open(BASE_EXPORT, "rb") as f:
        f.seek(max(0, size - 4 * 1024 * 1024))
        tail = f.read()
    ids = re.findall(rb'"id":\s*(\d+)', tail)
    return max(int(i) for i in ids) if ids else None


def load_json(path):
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def die_setup(why):
    print(why)
    print("Setup: 1) https://my.telegram.org -> API development tools -> create an app")
    print('       2) write %s as {"api_id": <id>, "api_hash": "<hash>"}' % API_FILE)
    print("       3) run:  python telegram_pull.py --login   (one-time phone + code)")
    sys.exit(2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--login", action="store_true", help="one-time interactive sign-in")
    ap.add_argument("--since", help="YYYY-MM-DD first-pull start when no base export/state exists")
    ap.add_argument("--limit", type=int, default=None, help="cap messages this run (testing)")
    args = ap.parse_args()

    api = load_json(API_FILE) or {}
    api_id = int(os.environ.get("TELEGRAM_API_ID", api.get("api_id") or 0) or 0)
    api_hash = os.environ.get("TELEGRAM_API_HASH", api.get("api_hash") or "")
    if not api_id or not api_hash:
        die_setup("telegram_api.json missing or incomplete.")
    chat = api.get("chat") or BOT_NAME

    from telethon.sync import TelegramClient  # deferred: keep --help fast
    from telethon.tl.types import Message

    client = TelegramClient(SESSION, api_id, api_hash)
    client.flood_sleep_threshold = 3600  # auto-sleep through any rate limit

    if args.login:
        client.start()  # prompts for phone + code (+ 2FA password if set)
        me = client.get_me()
        print("Signed in as %s (+%s). Session saved to %s.session" %
              (me.first_name, me.phone, SESSION))
        client.disconnect()
        return

    client.connect()
    if not client.is_user_authorized():
        client.disconnect()
        die_setup("Not signed in (no valid session).")

    state = load_json(STATE_FILE) or {}

    # resolve the bot chat: cached id -> exact dialog-name match -> direct lookup
    entity = None
    if state.get("chat_id"):
        try:
            entity = client.get_entity(state["chat_id"])
        except Exception:
            entity = None
    if entity is None:
        for dlg in client.iter_dialogs():
            if dlg.name == chat:
                entity = dlg.entity
                break
    if entity is None:
        try:
            entity = client.get_entity(chat)
        except Exception:
            client.disconnect()
            print("Cannot find chat %r among your dialogs." % chat)
            sys.exit(1)

    last_id = state.get("last_id")
    offset_date = None
    if last_id is None:
        last_id = bootstrap_last_id()
        if last_id is not None:
            print("bootstrapped watermark from base export: last_id=%d" % last_id)
        elif args.since:
            offset_date = datetime.strptime(args.since, "%Y-%m-%d")
            last_id = 0
            print("no base export; pulling since %s" % args.since)
        else:
            client.disconnect()
            print("No pull_state.json, no base export. Pass --since YYYY-MM-DD "
                  "for the first pull (pulling the whole chat is ~160k messages).")
            sys.exit(1)

    os.makedirs(PULLED_DIR, exist_ok=True)
    out_path = os.path.join(
        PULLED_DIR, "pull_%s.jsonl" % datetime.now().strftime("%Y%m%d_%H%M%S"))
    n_new = n_written = 0
    max_id = last_id
    fh = None
    try:
        for msg in client.iter_messages(entity, min_id=last_id, reverse=True,
                                        offset_date=offset_date, limit=args.limit):
            n_new += 1
            max_id = max(max_id, msg.id)
            if msg.out or not isinstance(msg, Message):
                continue  # my own /commands + service messages: never job posts
            if fh is None:
                fh = open(out_path, "w", encoding="utf-8")
            fh.write(json.dumps(to_export_msg(msg), ensure_ascii=False) + "\n")
            n_written += 1
            if n_written % 500 == 0:
                print("  ... %d messages" % n_written, flush=True)
    finally:
        if fh is not None:
            fh.close()
        client.disconnect()

    if max_id > (state.get("last_id") or 0):
        state.update({
            "last_id": max_id,
            "chat_id": getattr(entity, "id", None),
            "chat_title": getattr(entity, "first_name", None) or getattr(entity, "title", ""),
            "updated": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        })
        save_json(STATE_FILE, state)
    if fh is not None:
        print("wrote %s (%d messages)" % (out_path, n_written))
    title = getattr(entity, "first_name", None) or getattr(entity, "title", "")
    if title and title != BOT_NAME:
        print("WARNING: chat title is %r, labeled as %r for the processor" % (title, BOT_NAME))
    print("NEW_MESSAGES=%d" % n_written)


if __name__ == "__main__":
    main()
