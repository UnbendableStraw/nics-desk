"""Read inbound customer replies and normalize them for attaching to tickets.

Two sources, both of which need the owner's Mac and/or network, so they are **not** exercised
in the build container — only their pure helpers are unit-tested. Matching a normalized
message to a customer/ticket and recording it lives in app.py (`ingest_inbound`) and is fully
tested.

  - iMessage: reads the Messages history at ~/Library/Messages/chat.db (read-only). The
    Python process needs **Full Disk Access** (System Settings → Privacy & Security).
  - Email: polls an IMAP mailbox for UNSEEN messages and marks them seen.

Normalized inbound message:
  {channel:'imessage'|'email', external_id:str, sender:str, subject:str, text:str, sent_at:str}
"""

import email
import email.utils
import imaplib
import os
import sqlite3
from datetime import datetime, timezone, timedelta
from email.header import decode_header

CHAT_DB = os.path.expanduser("~/Library/Messages/chat.db")
APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)


# ----- pure helpers (unit-tested) ----------------------------------------- #
def apple_time_to_iso(value):
    """Messages stores `date` as nanoseconds since 2001-01-01 (older macOS: seconds)."""
    try:
        secs = value / 1e9 if value and value > 1e11 else (value or 0)
        return (APPLE_EPOCH + timedelta(seconds=secs)).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def decode_mime(value):
    """Decode an RFC 2047 header (e.g. a Subject) to a plain string."""
    if not value:
        return ""
    parts = []
    for chunk, enc in decode_header(value):
        if isinstance(chunk, bytes):
            try:
                parts.append(chunk.decode(enc or "utf-8", errors="replace"))
            except (LookupError, TypeError):
                parts.append(chunk.decode("utf-8", errors="replace"))
        else:
            parts.append(chunk)
    return "".join(parts).strip()


def plain_body(msg):
    """Best-effort plain-text body from an email.message.Message."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and \
                    "attachment" not in str(part.get("Content-Disposition", "")):
                payload = part.get_payload(decode=True) or b""
                return payload.decode(part.get_content_charset() or "utf-8",
                                      errors="replace").strip()
        return ""
    payload = msg.get_payload(decode=True) or b""
    return payload.decode(msg.get_content_charset() or "utf-8", errors="replace").strip()


def date_to_iso(value):
    try:
        dt = email.utils.parsedate_to_datetime(value)
        return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ----- readers (Mac / network; run live by the owner) --------------------- #
def imessage_readable():
    return os.path.exists(CHAT_DB) and os.access(CHAT_DB, os.R_OK)


def read_imessages(since_rowid=0, limit=200):
    """Inbound iMessages with ROWID greater than `since_rowid`.
    Returns (messages, new_cursor, error)."""
    if not imessage_readable():
        return [], since_rowid, ("Can't read Messages history. Make sure you're on the Mac and "
                                 "have granted this app Full Disk Access.")
    out, cursor = [], since_rowid
    try:
        con = sqlite3.connect(f"file:{CHAT_DB}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT m.ROWID AS rid, m.text, m.date, h.id AS sender "
            "FROM message m JOIN handle h ON h.ROWID = m.handle_id "
            "WHERE m.is_from_me = 0 AND m.ROWID > ? AND m.text IS NOT NULL "
            "ORDER BY m.ROWID ASC LIMIT ?", (since_rowid, limit)).fetchall()
        for r in rows:
            cursor = max(cursor, r["rid"])
            text = (r["text"] or "").strip()
            if not text:
                continue
            out.append({"channel": "imessage", "external_id": f"im-{r['rid']}",
                        "sender": r["sender"] or "", "subject": "", "text": text,
                        "sent_at": apple_time_to_iso(r["date"])})
        con.close()
    except Exception as e:  # noqa: BLE001
        return [], since_rowid, f"Couldn't read Messages: {e}"
    return out, cursor, None


def read_imap(cfg, limit=100):
    """Poll an IMAP mailbox for UNSEEN messages. Returns (messages, error)."""
    if cfg.get("imap_enabled") != "1":
        return [], None
    host, user, pw = cfg.get("imap_host"), cfg.get("imap_user"), cfg.get("imap_pass")
    if not (host and user and pw):
        return [], "Email reading is on but the IMAP host, user, or password is missing."
    out = []
    try:
        M = imaplib.IMAP4_SSL(host, int(cfg.get("imap_port") or 993))
        M.login(user, pw)
        M.select(cfg.get("imap_mailbox") or "INBOX")
        typ, data = M.search(None, "UNSEEN")
        ids = (data[0].split() if data and data[0] else [])[:limit]
        for num in ids:
            typ, msgdata = M.fetch(num, "(RFC822)")
            if not msgdata or not msgdata[0]:
                continue
            msg = email.message_from_bytes(msgdata[0][1])
            sender = email.utils.parseaddr(msg.get("From", ""))[1]
            mid = (msg.get("Message-ID") or f"imap-{num.decode()}").strip()
            out.append({"channel": "email", "external_id": mid,
                        "sender": sender, "subject": decode_mime(msg.get("Subject", "")),
                        "text": plain_body(msg), "sent_at": date_to_iso(msg.get("Date"))})
            M.store(num, "+FLAGS", "\\Seen")
        M.logout()
    except Exception as e:  # noqa: BLE001
        return [], f"Couldn't read email over IMAP: {e}"
    return out, None
