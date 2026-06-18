"""
RepairDesk — a self-hosted repair-shop CRM.

Run on a Mac so iMessage works:
    ./run.sh        (or: python3 app.py)

Then open http://localhost:5050  (admin)  — default password: changeme
Customers use:    http://<your-mac-ip>:5050/track  and  /request
"""
import os
import re
import csv
import io
import json
import secrets
import sqlite3
import socket
from datetime import datetime, timedelta
from functools import wraps
from io import BytesIO

from flask import (
    Flask, g, request, session, redirect, url_for, render_template,
    flash, abort, Response,
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

from imessage import send_imessage, imessage_available
from mailer import send_email, smtp_configured
from paypal import (paypal_configured, create_and_send_invoice as paypal_create_and_send,
                    invoice_view_url)
from inbound import read_imessages, read_imap
from labels import qr_png_bytes, code128_png_bytes

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "repairdesk.db")
SECRET_PATH = os.path.join(BASE_DIR, "secret.key")
DEFAULT_PORT = 5050  # not 5000 — macOS uses 5000 for AirPlay Receiver
MAGIC_LINK_TTL_MIN = 30  # customer sign-in links expire after this many minutes

DEFAULT_STATUSES = [
    ("Pending review", "slate"), ("Received", "blue"), ("Diagnosing", "blue"),
    ("Awaiting parts", "amber"), ("In progress", "amber"), ("Ready for pickup", "green"),
    ("Completed", "green"), ("Cancelled", "red"),
]
STATUS_COLOR_CHOICES = ["slate", "blue", "amber", "green", "red", "purple"]

DEFAULT_METHODS_IN = [
    "Drop Off at UPS", "Drop off on Site", "Ship In with USPS Ground",
    "Ship In with USPS Priority", "Ship In with UPS", "Using their own Shipping",
]
DEFAULT_METHODS_OUT = [
    "Pick up on Site", "Ship back with USPS Ground", "Ship back with USPS Priority",
    "Ship back with UPS", "Using their own Shipping",
]

ADDRESS_FIELDS = ["address1", "address2", "city", "state", "postal_code", "country"]

SETTING_KEYS = [
    "shop_name", "default_country_code", "base_url", "msg_template",
    "smtp_enabled", "smtp_host", "smtp_port", "smtp_security",
    "smtp_user", "smtp_pass", "smtp_from",
    "email_subject_template", "email_body_template",
    "label_message_template",
    "response_time_text", "turnaround_time_text",
    "show_repaired_counter", "repaired_baseline",
    "default_status", "intake_status",
    "portal_enabled", "portal_email_subject", "portal_email_body",
    "paypal_client_id", "paypal_secret", "paypal_env", "paypal_currency",
    "imessage_read_enabled", "imessage_last_rowid",
    "imap_enabled", "imap_host", "imap_port", "imap_user", "imap_pass", "imap_mailbox",
]

app = Flask(__name__)


def load_secret_key() -> bytes:
    if os.path.exists(SECRET_PATH):
        with open(SECRET_PATH, "rb") as f:
            return f.read()
    key = secrets.token_bytes(32)
    with open(SECRET_PATH, "wb") as f:
        f.write(key)
    os.chmod(SECRET_PATH, 0o600)
    return key


app.secret_key = load_secret_key()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  MAX_CONTENT_LENGTH=15 * 1024 * 1024)  # 15 MB cap on uploaded labels


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #
def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def detect_lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip


def _column_exists(db, table, col) -> bool:
    return any(r["name"] == col for r in db.execute(f"PRAGMA table_info({table})"))


def _table_exists(db, name) -> bool:
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                      (name,)).fetchone() is not None


def init_db():
    """Create tables, run lightweight migrations, and seed defaults. Idempotent."""
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS customers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            first_name TEXT NOT NULL DEFAULT '', last_name TEXT NOT NULL DEFAULT '',
            phone TEXT NOT NULL DEFAULT '', email TEXT NOT NULL DEFAULT '',
            notes TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS prefixes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL, label TEXT NOT NULL DEFAULT '',
            counter INTEGER NOT NULL DEFAULT 1000, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS issues (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL, estimated_cost TEXT NOT NULL DEFAULT '',
            active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS services (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL, cost TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL DEFAULT '',
            active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS devices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS device_issues (
            device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
            issue_id INTEGER NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
            PRIMARY KEY (device_id, issue_id)
        );
        CREATE TABLE IF NOT EXISTS device_services (
            device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
            service_id INTEGER NOT NULL REFERENCES services(id) ON DELETE CASCADE,
            PRIMARY KEY (device_id, service_id)
        );
        CREATE TABLE IF NOT EXISTS assets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER NOT NULL REFERENCES customers(id),
            device TEXT NOT NULL DEFAULT '', serial TEXT NOT NULL DEFAULT '',
            notes TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS methods (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            direction TEXT NOT NULL, label TEXT NOT NULL, cost TEXT NOT NULL DEFAULT '',
            sort INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1,
            is_shipping INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS repairs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repair_number TEXT UNIQUE NOT NULL,
            customer_id INTEGER NOT NULL REFERENCES customers(id),
            device TEXT NOT NULL DEFAULT '', issue TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'Received', price TEXT NOT NULL DEFAULT '',
            public_token TEXT UNIQUE NOT NULL, source TEXT NOT NULL DEFAULT 'staff',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS repair_issues (
            repair_id INTEGER NOT NULL REFERENCES repairs(id) ON DELETE CASCADE,
            issue_id INTEGER NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
            PRIMARY KEY (repair_id, issue_id)
        );
        CREATE TABLE IF NOT EXISTS repair_services (
            repair_id INTEGER NOT NULL REFERENCES repairs(id) ON DELETE CASCADE,
            service_id INTEGER NOT NULL REFERENCES services(id) ON DELETE CASCADE,
            PRIMARY KEY (repair_id, service_id)
        );
        CREATE TABLE IF NOT EXISTS shipments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repair_id INTEGER NOT NULL REFERENCES repairs(id) ON DELETE CASCADE,
            direction TEXT NOT NULL DEFAULT 'out',
            carrier TEXT NOT NULL DEFAULT '', service TEXT NOT NULL DEFAULT '',
            tracking TEXT NOT NULL DEFAULT '', cost TEXT NOT NULL DEFAULT '',
            fmt TEXT NOT NULL DEFAULT 'pdf', label_pdf BLOB, qr_png BLOB,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS updates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repair_id INTEGER NOT NULL REFERENCES repairs(id) ON DELETE CASCADE,
            status TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '',
            channel TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS statuses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE, color TEXT NOT NULL DEFAULT 'slate',
            sort INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS asset_media (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
            kind TEXT NOT NULL DEFAULT 'image', label TEXT NOT NULL DEFAULT '',
            filename TEXT NOT NULL DEFAULT '', mime TEXT NOT NULL DEFAULT '',
            data BLOB, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS method_devices (
            method_id INTEGER NOT NULL REFERENCES methods(id) ON DELETE CASCADE,
            device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
            PRIMARY KEY (method_id, device_id)
        );
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE, name TEXT NOT NULL DEFAULT '',
            password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'technician',
            active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS magic_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL, token TEXT NOT NULL UNIQUE,
            expires_at TEXT NOT NULL, used_at TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS message_templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL, body TEXT NOT NULL DEFAULT '',
            sort INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repair_id INTEGER NOT NULL REFERENCES repairs(id) ON DELETE CASCADE,
            sender TEXT NOT NULL, author TEXT NOT NULL DEFAULT '',
            body TEXT NOT NULL, via TEXT NOT NULL DEFAULT 'app',
            external_id TEXT, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS paypal_invoices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repair_id INTEGER NOT NULL REFERENCES repairs(id) ON DELETE CASCADE,
            invoice_id TEXT NOT NULL, recipient TEXT NOT NULL DEFAULT '',
            currency TEXT NOT NULL DEFAULT 'USD', amount TEXT NOT NULL DEFAULT '',
            view_url TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'sent',
            created_at TEXT NOT NULL
        );
        """
    )
    # Customer address columns.
    for col in ADDRESS_FIELDS:
        if not _column_exists(db, "customers", col):
            db.execute(f"ALTER TABLE customers ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
    # Repair columns added over time.
    for col, ddl in [("issue_id", "INTEGER"), ("asset_id", "INTEGER"),
                     ("fulfillment", "TEXT NOT NULL DEFAULT ''"),
                     ("device_id", "INTEGER"),
                     ("inbound_method", "TEXT NOT NULL DEFAULT ''"),
                     ("outbound_method", "TEXT NOT NULL DEFAULT ''"),
                     ("label_format", "TEXT NOT NULL DEFAULT ''"),
                     ("warranty_of", "INTEGER")]:  # original repair a warranty ticket came from
        if not _column_exists(db, "repairs", col):
            db.execute(f"ALTER TABLE repairs ADD COLUMN {col} {ddl}")
    # chat_messages gained channel tracking for inbound iMessage/email replies.
    if _table_exists(db, "chat_messages"):
        if not _column_exists(db, "chat_messages", "via"):
            db.execute("ALTER TABLE chat_messages ADD COLUMN via TEXT NOT NULL DEFAULT 'app'")
        if not _column_exists(db, "chat_messages", "external_id"):
            db.execute("ALTER TABLE chat_messages ADD COLUMN external_id TEXT")
    if not _column_exists(db, "methods", "is_shipping"):
        db.execute("ALTER TABLE methods ADD COLUMN is_shipping INTEGER NOT NULL DEFAULT 0")
    for col in ["supports_qr"]:
        if not _column_exists(db, "methods", col):
            db.execute(f"ALTER TABLE methods ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0")
    for col in ["carrier", "service"]:
        if not _column_exists(db, "methods", col):
            db.execute(f"ALTER TABLE methods ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
    for col in ["days_min", "days_max"]:
        if not _column_exists(db, "methods", col):
            db.execute(f"ALTER TABLE methods ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0")
    # Per-option "requires a mailing address" flag (Phase 2). Default off; seed sensible
    # values for the built-in ship-in / ship-back options on first add (below).
    if not _column_exists(db, "methods", "requires_address"):
        db.execute("ALTER TABLE methods ADD COLUMN requires_address INTEGER NOT NULL DEFAULT 0")
        # Anything that already needs a shipping label (ship-in) plainly needs an address;
        # so do the ship-back options. Backfill so existing shops keep working sensibly.
        db.execute("UPDATE methods SET requires_address=1 WHERE is_shipping=1")
        db.execute("UPDATE methods SET requires_address=1 "
                   "WHERE direction='out' AND LOWER(label) LIKE 'ship%'")
    if not _column_exists(db, "issues", "description"):
        db.execute("ALTER TABLE issues ADD COLUMN description TEXT NOT NULL DEFAULT ''")
    if not _column_exists(db, "devices", "prefix_id"):
        db.execute("ALTER TABLE devices ADD COLUMN prefix_id INTEGER")
    db.execute(
        "CREATE TABLE IF NOT EXISTS issue_media ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "issue_id INTEGER NOT NULL REFERENCES issues(id) ON DELETE CASCADE, "
        "kind TEXT NOT NULL DEFAULT 'image', filename TEXT NOT NULL DEFAULT '', "
        "mime TEXT NOT NULL DEFAULT '', data BLOB, created_at TEXT NOT NULL)")
    db.commit()

    for r in db.execute("SELECT id, issue_id FROM repairs WHERE issue_id IS NOT NULL"):
        db.execute("INSERT OR IGNORE INTO repair_issues(repair_id, issue_id) VALUES(?,?)",
                   (r["id"], r["issue_id"]))
    db.execute("UPDATE repairs SET outbound_method=fulfillment "
               "WHERE outbound_method='' AND fulfillment!=''")
    db.commit()

    def raw_setting(key, default=""):
        row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def ensure(key, value):
        if db.execute("SELECT 1 FROM settings WHERE key=?", (key,)).fetchone() is None:
            db.execute("INSERT INTO settings(key, value) VALUES(?,?)", (key, value))

    if db.execute("SELECT COUNT(*) AS n FROM prefixes").fetchone()["n"] == 0:
        legacy_code = raw_setting("repair_prefix", "R") or "R"
        try:
            legacy_counter = int(raw_setting("repair_counter", "1000"))
        except ValueError:
            legacy_counter = 1000
        db.execute("INSERT INTO prefixes(code,label,counter,created_at) VALUES(?,?,?,?)",
                   (legacy_code, "General", legacy_counter, now_iso()))
    db.commit()
    ensure("default_prefix_id",
           str(db.execute("SELECT id FROM prefixes ORDER BY id LIMIT 1").fetchone()["id"]))

    if db.execute("SELECT COUNT(*) AS n FROM statuses").fetchone()["n"] == 0:
        for i, (nm, color) in enumerate(DEFAULT_STATUSES):
            db.execute("INSERT INTO statuses(name,color,sort,created_at) VALUES(?,?,?,?)",
                       (nm, color, i, now_iso()))
    db.commit()
    # Which status new repairs start in (staff vs customer intake). Fall back if renamed/removed.
    names = [s["name"] for s in db.execute("SELECT name FROM statuses ORDER BY sort, id")]
    ensure("default_status", "Received" if "Received" in names else (names[0] if names else "Received"))
    ensure("intake_status", "Pending review" if "Pending review" in names else (names[0] if names else "Pending review"))

    if db.execute("SELECT COUNT(*) AS n FROM methods").fetchone()["n"] == 0:
        for i, lab in enumerate(DEFAULT_METHODS_IN):
            low = lab.lower()
            ship = 1 if low.startswith("ship in") else 0
            carrier = "USPS" if "usps" in low else ("UPS" if "ups" in low else "")
            # USPS Label Broker QR works out of the box; UPS QR needs extra onboarding, so off by default.
            qr = 1 if (ship and "usps" in low) else 0
            req_addr = ship  # ship-in options need a from/return address
            db.execute("INSERT INTO methods(direction,label,cost,sort,active,is_shipping,"
                       "supports_qr,carrier,service,requires_address,created_at) "
                       "VALUES('in',?,?,?,1,?,?,?,'',?,?)",
                       (lab, "0", i, ship, qr, carrier, req_addr, now_iso()))
        for i, lab in enumerate(DEFAULT_METHODS_OUT):
            low = lab.lower()
            carrier = "USPS" if "usps" in low else ("UPS" if "ups" in low else "")
            req_addr = 1 if low.startswith("ship back") else 0  # ship-back needs a destination address
            db.execute("INSERT INTO methods(direction,label,cost,sort,active,is_shipping,"
                       "supports_qr,carrier,service,requires_address,created_at) "
                       "VALUES('out',?,?,?,1,0,0,?,'',?,?)",
                       (lab, "0", i, carrier, req_addr, now_iso()))

    ensure("shop_name", "My Repair Shop")
    ensure("default_country_code", "+1")
    ensure("base_url", f"http://{detect_lan_ip()}:{DEFAULT_PORT}")
    ensure("admin_password_hash", generate_password_hash("changeme"))
    # Staff accounts (replaces the single shared password). On first upgrade we seed one
    # Owner account that reuses the existing shared-password hash, so whatever password the
    # shop already uses keeps working — they just sign in as username "owner" now.
    if db.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"] == 0:
        seed_hash = raw_setting("admin_password_hash") or generate_password_hash("changeme")
        db.execute("INSERT INTO users(username,name,password_hash,role,active,created_at) "
                   "VALUES('owner','Owner',?,?,1,?)",
                   (seed_hash, "owner", now_iso()))
    db.commit()
    ensure("msg_template",
           "Hi {first_name}, update on your repair {repair_number} ({device}): "
           "status is now \u201c{status}\u201d. Track it any time: {track_url}")
    ensure("smtp_enabled", "0")
    ensure("smtp_host", "")
    ensure("smtp_port", "587")
    ensure("smtp_security", "starttls")
    ensure("smtp_user", "")
    ensure("smtp_pass", "")
    ensure("smtp_from", "")
    ensure("email_subject_template",
           "{shop_name}: repair {repair_number} is now {status}")
    ensure("email_body_template",
           "Hi {first_name},\n\n"
           "Here's an update on your repair {repair_number} ({device}).\n\n"
           "Status: {status}\nService: {issue_name}\n\n"
           "You can check progress any time here:\n{track_url}\n\n"
           "Thanks,\n{shop_name}")
    # Shipping-label message default.
    # Shipping-label message to the customer (used when you upload a label and send it).
    ensure("label_message_template",
           "Hi {first_name}, here's the shipping label for your repair {repair_number}. "
           "{label_line} Tracking: {tracking}")
    # Customer-facing timing + social proof on the request/track pages.
    ensure("response_time_text", "1\u20132 business days")
    ensure("turnaround_time_text", "3\u20135 business days")
    ensure("show_repaired_counter", "1")
    ensure("repaired_baseline", "0")
    # Customer portal (passwordless email magic-link sign-in).
    ensure("portal_enabled", "1")
    ensure("portal_email_subject", "{shop_name}: your sign-in link")
    ensure("portal_email_body",
           "Hi {first_name},\n\n"
           "Here's your secure link to sign in and view your repairs at {shop_name}:\n\n"
           "{link}\n\n"
           "The link works once and expires in {minutes} minutes. If you didn't request it, "
           "you can ignore this email.\n\n"
           "Thanks,\n{shop_name}")
    # PayPal invoicing (owner adds REST app credentials; defaults to safe sandbox mode).
    ensure("paypal_client_id", "")
    ensure("paypal_secret", "")
    ensure("paypal_env", "sandbox")
    ensure("paypal_currency", "USD")
    # Reading inbound replies (Mac chat.db for iMessage; IMAP for email).
    ensure("imessage_read_enabled", "0")
    ensure("imessage_last_rowid", "0")
    ensure("imap_enabled", "0")
    ensure("imap_host", "")
    ensure("imap_port", "993")
    ensure("imap_user", "")
    ensure("imap_pass", "")
    ensure("imap_mailbox", "INBOX")
    # A few starter message templates the owner can edit or delete.
    if db.execute("SELECT COUNT(*) AS n FROM message_templates").fetchone()["n"] == 0:
        starters = [
            ("Received", "Hi {first_name}, we've received your {device} ({repair_number}) and "
                         "will take a look soon. Track it anytime: {track_url}"),
            ("Estimate ready", "Hi {first_name}, we've assessed your {device}. The estimate is "
                               "{total}. Reply to approve and we'll get started. — {shop_name}"),
            ("Waiting on parts", "Hi {first_name}, quick update on {repair_number}: we're waiting "
                                 "on a part for your {device}. We'll let you know the moment it's in."),
            ("Ready for pickup", "Good news {first_name}! Your {device} ({repair_number}) is "
                                 "repaired and ready for pickup. — {shop_name}"),
        ]
        for i, (name, body) in enumerate(starters):
            db.execute("INSERT INTO message_templates(name, body, sort, active, created_at) "
                       "VALUES(?,?,?,1,?)", (name, body, i, now_iso()))
    db.commit()
    db.close()


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def setting(key, default=""):
    row = get_db().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    get_db().execute(
        "INSERT INTO settings(key, value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    get_db().commit()


def smtp_cfg() -> dict:
    return {k: setting(k) for k in
            ["smtp_enabled", "smtp_host", "smtp_port", "smtp_security",
             "smtp_user", "smtp_pass", "smtp_from"]}


def email_ready() -> bool:
    return smtp_configured(smtp_cfg())


def paypal_cfg() -> dict:
    return {k: setting(k) for k in
            ["paypal_client_id", "paypal_secret", "paypal_env", "paypal_currency"]}


def paypal_ready() -> bool:
    return paypal_configured(paypal_cfg())


def all_statuses():
    return get_db().execute("SELECT * FROM statuses ORDER BY sort, id").fetchall()


def status_names():
    return [s["name"] for s in all_statuses()]


def status_colors():
    return {s["name"]: s["color"] for s in all_statuses()}


def default_status():
    names = status_names()
    s = setting("default_status", "Received")
    return s if s in names else (names[0] if names else "Received")


def intake_status():
    names = status_names()
    s = setting("intake_status", "Pending review")
    return s if s in names else (names[0] if names else "Pending review")


def money_val(text) -> float:
    if text is None:
        return 0.0
    m = re.search(r"\d+(?:\.\d+)?", str(text).replace(",", ""))
    return float(m.group()) if m else 0.0


def money_range(text):
    """Parse a cost that may be a single value ('$129') or a range ('$50-70') -> (low, high)."""
    if text is None:
        return (0.0, 0.0)
    nums = re.findall(r"\d+(?:\.\d+)?", str(text).replace(",", ""))
    if not nums:
        return (0.0, 0.0)
    vals = [float(n) for n in nums[:2]]
    if len(vals) == 1:
        return (vals[0], vals[0])
    return (min(vals), max(vals))


def money_fmt(v: float) -> str:
    if v <= 0:
        return ""
    return f"${int(v)}" if abs(v - int(v)) < 1e-9 else f"${v:,.2f}"


def fmt_range(low: float, high: float) -> str:
    """'$50–$70' for a range, '$129' for a single value, '' for nothing."""
    if high <= 0:
        return ""
    if abs(low - high) < 1e-9:
        return money_fmt(low)
    return f"{money_fmt(low)}\u2013{money_fmt(high)}"


def _int_or_zero(v):
    try:
        return max(0, int(v))
    except (TypeError, ValueError):
        return 0


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def fmt_dt(value: str) -> str:
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").strftime(
            "%b %-d, %Y \u00b7 %-I:%M %p")
    except Exception:
        return value


def next_repair_number(prefix_id) -> str:
    db = get_db()
    row = db.execute("SELECT id FROM prefixes WHERE id=?", (prefix_id,)).fetchone()
    if not row:
        prefix_id = setting("default_prefix_id")
        row = db.execute("SELECT id FROM prefixes WHERE id=?", (prefix_id,)).fetchone()
    if not row:
        cur = db.execute("INSERT INTO prefixes(code,label,counter,created_at) "
                         "VALUES('R','General',1000,?)", (now_iso(),))
        prefix_id = cur.lastrowid
    db.execute("UPDATE prefixes SET counter = counter + 1 WHERE id=?", (prefix_id,))
    db.commit()
    p = db.execute("SELECT code, counter FROM prefixes WHERE id=?", (prefix_id,)).fetchone()
    return f"{p['code']}-{p['counter']}"


def normalize_phone(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return ""
    if raw.startswith("+"):
        return "+" + re.sub(r"\D", "", raw[1:])
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 10:
        cc = re.sub(r"\D", "", setting("default_country_code", "+1"))
        return f"+{cc}{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    return f"+{digits}" if digits else ""


def address_from_form(form) -> dict:
    return {f: form.get(f, "").strip() for f in ADDRESS_FIELDS}


def active_issues():
    return get_db().execute("SELECT * FROM issues WHERE active=1 ORDER BY name").fetchall()


def active_services():
    return get_db().execute("SELECT * FROM services WHERE active=1 ORDER BY name").fetchall()


def all_prefixes():
    return get_db().execute("SELECT * FROM prefixes ORDER BY code").fetchall()


def methods_for(direction):
    return get_db().execute(
        "SELECT * FROM methods WHERE direction=? AND active=1 ORDER BY sort, id",
        (direction,)).fetchall()


def shipping_inbound_labels():
    """Inbound method labels flagged as requiring a shipping label."""
    return [m["label"] for m in get_db().execute(
        "SELECT label FROM methods WHERE direction='in' AND active=1 AND is_shipping=1")]


def inbound_method(label):
    """Look up a single inbound method row by its label."""
    if not label:
        return None
    return get_db().execute(
        "SELECT * FROM methods WHERE direction='in' AND label=? ORDER BY id LIMIT 1",
        (label,)).fetchone()


def method_supports_qr(label) -> bool:
    m = inbound_method(label)
    return bool(m and m["is_shipping"] and m["supports_qr"])


def shipping_meta_map():
    """For the request form: {label: {'qr': 0/1}} for active inbound shipping options."""
    out = {}
    for m in get_db().execute(
            "SELECT label, supports_qr FROM methods "
            "WHERE direction='in' AND active=1 AND is_shipping=1"):
        out[m["label"]] = {"qr": int(m["supports_qr"])}
    return out


def attached_issues(repair_id):
    return get_db().execute(
        "SELECT i.* FROM repair_issues ri JOIN issues i ON i.id=ri.issue_id "
        "WHERE ri.repair_id=? ORDER BY i.name", (repair_id,)).fetchall()


def attached_services(repair_id):
    return get_db().execute(
        "SELECT s.* FROM repair_services rs JOIN services s ON s.id=rs.service_id "
        "WHERE rs.repair_id=? ORDER BY s.name", (repair_id,)).fetchall()


def device_issue_links():
    """issue_id -> set(device_id)."""
    links = {}
    for row in get_db().execute("SELECT device_id, issue_id FROM device_issues"):
        links.setdefault(row["issue_id"], set()).add(row["device_id"])
    return links


def device_service_links():
    """service_id -> set(device_id)."""
    links = {}
    for row in get_db().execute("SELECT device_id, service_id FROM device_services"):
        links.setdefault(row["service_id"], set()).add(row["device_id"])
    return links


def links_by_device(table, key):
    """device_id -> set(other_id) for device_issues / device_services."""
    out = {}
    for row in get_db().execute(f"SELECT device_id, {key} FROM {table}"):
        out.setdefault(row["device_id"], set()).add(row[key])
    return out


def links_by_method():
    """method_id -> set(device_id) from method_devices (Phase 2 shipping↔device links)."""
    out = {}
    for row in get_db().execute("SELECT method_id, device_id FROM method_devices"):
        out.setdefault(row["method_id"], set()).add(row["device_id"])
    return out


def method_row(direction, label):
    """A single method row by direction + label (label alone is ambiguous: some labels,
    e.g. 'Using their own Shipping', exist in both directions)."""
    if not label:
        return None
    return get_db().execute(
        "SELECT * FROM methods WHERE direction=? AND label=? ORDER BY id LIMIT 1",
        (direction, label)).fetchone()


def method_requires_address(direction, label) -> bool:
    m = method_row(direction, label)
    return bool(m and m["requires_address"])


def shipping_form_data():
    """For the request form: inbound/outbound options with their device links + flags.
    `devices` is a list of catalog device ids the option is restricted to, or None for
    'applies to every device' (the default when an option has no explicit links)."""
    links = links_by_method()

    def pack(direction):
        rows = []
        for m in methods_for(direction):
            devs = sorted(links.get(m["id"], set()))
            rows.append({
                "label": m["label"],
                "cost": (m["cost"] or "0"),
                "ship": int(m["is_shipping"]),
                "qr": int(m["supports_qr"]),
                "requires_address": int(m["requires_address"]),
                "days": _f_daystr(m),
                "devices": devs if devs else None,
            })
        return rows

    return {"in": pack("in"), "out": pack("out")}


def _sum_ranges(pairs):
    lo = sum(p[0] for p in pairs)
    hi = sum(p[1] for p in pairs)
    return (lo, hi)


def issues_total(repair_id):
    return _sum_ranges([money_range(i["estimated_cost"]) for i in attached_issues(repair_id)])


def services_total(repair_id):
    return _sum_ranges([money_range(s["cost"]) for s in attached_services(repair_id)])


def method_cost(label):
    if not label:
        return (0.0, 0.0)
    row = get_db().execute("SELECT cost FROM methods WHERE label=? ORDER BY id LIMIT 1",
                           (label,)).fetchone()
    return money_range(row["cost"]) if row else (0.0, 0.0)


def shipping_total(repair):
    return _sum_ranges([method_cost(_field(repair, "inbound_method")),
                        method_cost(_field(repair, "outbound_method"))])


def estimate_total(repair):
    rid = repair["id"]
    return _sum_ranges([issues_total(rid), services_total(rid), shipping_total(repair)])


def repaired_count_raw() -> int:
    """Total devices repaired: sum of category counters (which bake in any high starting
    numbers like 1,000 or 5,000) plus an optional flat baseline the shop can set."""
    db = get_db()
    base = db.execute("SELECT COALESCE(SUM(counter), 0) AS n FROM prefixes").fetchone()["n"]
    try:
        extra = int(setting("repaired_baseline", "0") or 0)
    except ValueError:
        extra = 0
    return int(base) + extra


def repaired_count_display() -> int:
    """Rounded-down 'More than X' figure."""
    n = repaired_count_raw()
    if n >= 200:
        return n - (n % 100)
    if n >= 20:
        return n - (n % 10)
    return n


def issue_media_for(issue_id):
    return get_db().execute(
        "SELECT id, kind, mime FROM issue_media WHERE issue_id=? ORDER BY id", (issue_id,)).fetchall()


def link_or_create_asset(customer_id, device, serial):
    serial = (serial or "").strip()
    if not serial:
        return None
    db = get_db()
    row = db.execute("SELECT id FROM assets WHERE customer_id=? AND serial=?",
                     (customer_id, serial)).fetchone()
    if row:
        return row["id"]
    cur = db.execute(
        "INSERT INTO assets(customer_id,device,serial,notes,created_at) VALUES(?,?,?,?,?)",
        (customer_id, device, serial, "", now_iso()))
    return cur.lastrowid


# --------------------------------------------------------------------------- #
# Auth + CSRF
# --------------------------------------------------------------------------- #
# Role tiers and what each may do. Owner ⊇ Manager ⊇ Technician.
#   users           — manage staff accounts (Owner only)
#   settings        — edit shop settings (name, SMTP, templates, password reset of self)
#   catalog         — manage the catalogs: prefixes, statuses, issues, services, devices, shipping
#   customers.manage— add/edit/delete/import customers
#   repairs.delete  — delete a repair ticket
#   repairs.work    — the day-to-day: create/edit tickets, statuses, messages, labels, assets
ROLES = ["owner", "manager", "technician"]
ROLE_LABELS = {"owner": "Owner", "manager": "Manager", "technician": "Technician"}
PERMISSIONS = {
    "owner": {"users", "settings", "catalog", "customers.manage",
              "repairs.delete", "repairs.work"},
    "manager": {"settings", "catalog", "customers.manage",
                "repairs.delete", "repairs.work"},
    "technician": {"repairs.work"},
}


def current_user():
    """The logged-in, still-active user row (cached per request), or None."""
    if "_user" not in g:
        g._user = None
        uid = session.get("uid")
        if uid:
            row = get_db().execute(
                "SELECT * FROM users WHERE id=? AND active=1", (uid,)).fetchone()
            g._user = row
    return g._user


def has_perm(perm, role=None) -> bool:
    if role is None:
        u = current_user()
        if not u:
            return False
        role = u["role"]
    return perm in PERMISSIONS.get(role, set())


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user():
            session.pop("uid", None)  # stale/disabled session
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def permission_required(perm):
    """Require a logged-in user whose role grants `perm`."""
    def deco(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            u = current_user()
            if not u:
                session.pop("uid", None)
                return redirect(url_for("login", next=request.path))
            if not has_perm(perm, u["role"]):
                abort(403)
            return view(*args, **kwargs)
        return wrapped
    return deco


PUBLIC_POST_ENDPOINTS = {"login", "track", "submit_request", "portal_login"}


@app.before_request
def csrf_protect():
    if request.method == "POST" and request.endpoint not in PUBLIC_POST_ENDPOINTS:
        token = session.get("_csrf")
        if not token or not secrets.compare_digest(token, request.form.get("_csrf", "")):
            abort(400, "Invalid or missing form token. Reload the page and try again.")


@app.context_processor
def inject_globals():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_urlsafe(32)
    u = current_user() if request.endpoint else None
    return {
        "csrf_token": session.get("_csrf", ""),
        "shop_name": setting("shop_name", "Repair Shop") if request.endpoint else "Repair Shop",
        "STATUS_COLORS": status_colors(),
        "fmt_dt": fmt_dt,
        "is_admin": bool(u),
        "current_user": u,
        "user_role": (u["role"] if u else None),
        "role_labels": ROLE_LABELS,
        "can": has_perm,  # {{ can('catalog') }} in templates
        "customer": current_customer() if request.endpoint else None,
        "portal_on": setting("portal_enabled", "1") == "1",
    }


# --------------------------------------------------------------------------- #
# Customer accounts (passwordless email magic-link sign-in)
# --------------------------------------------------------------------------- #
def current_customer():
    """A representative customer row for the signed-in portal email (most-recent row
    with that email), cached per request, or None. Customers are matched by email so the
    portal works even if a person has several customer rows."""
    if "_customer" not in g:
        g._customer = None
        email = session.get("cust_email")
        if email:
            g._customer = get_db().execute(
                "SELECT * FROM customers WHERE LOWER(email)=LOWER(?) AND email<>'' "
                "ORDER BY id DESC LIMIT 1", (email,)).fetchone()
    return g._customer


def customer_login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_customer():
            session.pop("cust_email", None)
            return redirect(url_for("portal_login"))
        return view(*args, **kwargs)
    return wrapped


def make_magic_link_token(email):
    """Issue a fresh single-use token for `email`, invalidating any earlier unused ones."""
    db = get_db()
    db.execute("DELETE FROM magic_links WHERE LOWER(email)=LOWER(?) AND used_at IS NULL", (email,))
    token = secrets.token_urlsafe(32)
    expires = (datetime.now() + timedelta(minutes=MAGIC_LINK_TTL_MIN)).strftime("%Y-%m-%d %H:%M:%S")
    db.execute("INSERT INTO magic_links(email, token, expires_at, created_at) VALUES(?,?,?,?)",
               (email, token, expires, now_iso()))
    db.commit()
    return token


def consume_magic_link(token):
    """Validate + burn a token. Returns the email on success, else None."""
    if not token:
        return None
    db = get_db()
    row = db.execute("SELECT * FROM magic_links WHERE token=?", (token,)).fetchone()
    if not row or row["used_at"]:
        return None
    try:
        expires = datetime.strptime(row["expires_at"], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    if datetime.now() > expires:
        return None
    db.execute("UPDATE magic_links SET used_at=? WHERE id=?", (now_iso(), row["id"]))
    db.commit()
    return row["email"]


def send_magic_link(email, customer_row, token):
    """Email the sign-in link. Returns (ok, detail). Logs the link to the console as a
    fallback so a solo owner can still sign in while testing without SMTP configured."""
    link = f"{setting('base_url').rstrip('/')}{url_for('portal_verify', token=token)}"
    fields = SafeDict(
        first_name=(customer_row["first_name"] if customer_row else "") or "there",
        shop_name=setting("shop_name"), link=link, minutes=str(MAGIC_LINK_TTL_MIN))
    subject = setting("portal_email_subject").format_map(fields)
    body = setting("portal_email_body").format_map(fields)
    ok, detail = send_email(smtp_cfg(), email, subject, body)
    if not ok:
        print(f"[PORTAL LINK] for {email}: {link}")  # dev/no-SMTP fallback
    return ok, detail


@app.template_filter("costlow")
def _f_costlow(text):
    return money_range(text)[0]


@app.template_filter("costhigh")
def _f_costhigh(text):
    return money_range(text)[1]


@app.template_filter("daystr")
def _f_daystr(m):
    """A method row -> '2–3 days' / '3 days' / ''."""
    try:
        lo, hi = int(m["days_min"] or 0), int(m["days_max"] or 0)
    except (KeyError, IndexError, TypeError, ValueError):
        return ""
    if lo and hi and lo != hi:
        return f"{lo}\u2013{hi} days"
    n = hi or lo
    return f"{n} day{'s' if n != 1 else ''}" if n else ""


# --------------------------------------------------------------------------- #
# Messaging
# --------------------------------------------------------------------------- #
class SafeDict(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def _field(repair, key, default=""):
    try:
        val = repair[key]
    except (KeyError, IndexError):
        return default
    return val if val not in (None, "") else default


def build_fields(repair) -> SafeDict:
    track_url = f"{setting('base_url').rstrip('/')}/r/{repair['public_token']}"
    issue_names = _field(repair, "issue_names")
    if not issue_names:
        issue_names = ", ".join(i["name"] for i in attached_issues(repair["id"]))
    inbound = _field(repair, "inbound_method")
    outbound = _field(repair, "outbound_method")
    fulfillment = " → ".join(x for x in (inbound, outbound) if x)
    total = fmt_range(*estimate_total(repair))
    return SafeDict(
        first_name=_field(repair, "first_name", "there"),
        last_name=_field(repair, "last_name"),
        repair_number=repair["repair_number"],
        device=_field(repair, "device") or _field(repair, "device_name") or "your device",
        issue=_field(repair, "issue"),
        issue_name=issue_names,
        status=repair["status"],
        price=total, total=total,
        serial=_field(repair, "asset_serial"),
        inbound=inbound, outbound=outbound, fulfillment=fulfillment,
        track_url=track_url, shop_name=setting("shop_name"),
    )


def render_imessage_body(repair):
    return setting("msg_template").format_map(build_fields(repair))


def render_email_subject(repair):
    return setting("email_subject_template").format_map(build_fields(repair))


def render_email_body(repair):
    return setting("email_body_template").format_map(build_fields(repair))


def log_update(repair_id, status, note, channel):
    get_db().execute(
        "INSERT INTO updates(repair_id, status, note, channel, created_at) VALUES(?,?,?,?,?)",
        (repair_id, status, note, channel, now_iso()))
    get_db().commit()


def notify(repair, channels):
    out = []
    if "imessage" in channels:
        body = render_imessage_body(repair)
        ok, detail = send_imessage(repair["phone"], body)
        if ok:
            log_update(repair["id"], "", body, "imessage")
        out.append(("iMessage", ok, detail))
    if "email" in channels:
        subject = render_email_subject(repair)
        ok, detail = send_email(smtp_cfg(), repair["email"], subject, render_email_body(repair))
        if ok:
            log_update(repair["id"], "", f"Email: {subject}", "email")
        out.append(("Email", ok, detail))
    return out


def active_templates():
    return get_db().execute(
        "SELECT * FROM message_templates WHERE active=1 ORDER BY sort, id").fetchall()


def all_templates():
    return get_db().execute(
        "SELECT * FROM message_templates ORDER BY sort, id").fetchall()


def rendered_templates(repair):
    """Active templates with their placeholders filled in for this repair, ready to drop
    into the message composer."""
    fields = build_fields(repair)
    return [{"id": t["id"], "name": t["name"], "body": t["body"].format_map(fields)}
            for t in active_templates()]


# --------------------------------------------------------------------------- #
# Auth routes
# --------------------------------------------------------------------------- #
@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user():
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        row = get_db().execute(
            "SELECT * FROM users WHERE username=? AND active=1", (username,)).fetchone()
        if row and check_password_hash(row["password_hash"], password):
            session.clear()
            session["uid"] = row["id"]
            session.permanent = True
            return redirect(request.args.get("next") or url_for("dashboard"))
        flash("That username and password didn't match.", "error")
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
@login_required
def logout():
    session.clear()
    return redirect(url_for("login"))


# --------------------------------------------------------------------------- #
# Dashboard
# --------------------------------------------------------------------------- #
@app.route("/")
@login_required
def dashboard():
    db = get_db()
    q = request.args.get("q", "").strip()
    status_filter = request.args.get("status", "").strip()

    sql = ("SELECT r.*, c.first_name, c.last_name, c.phone, "
           "(SELECT GROUP_CONCAT(i.name, ', ') FROM repair_issues ri "
           " JOIN issues i ON i.id=ri.issue_id WHERE ri.repair_id=r.id) AS issue_names "
           "FROM repairs r JOIN customers c ON c.id = r.customer_id WHERE 1=1")
    params = []
    if status_filter:
        sql += " AND r.status = ?"
        params.append(status_filter)
    if q:
        sql += (" AND (r.repair_number LIKE ? OR c.last_name LIKE ? OR c.first_name LIKE ? "
                "OR r.device LIKE ? OR c.phone LIKE ?)")
        params += [f"%{q}%"] * 5
    sql += " ORDER BY r.updated_at DESC"
    repairs = db.execute(sql, params).fetchall()

    counts = {s: 0 for s in status_names()}
    for row in db.execute("SELECT status, COUNT(*) n FROM repairs GROUP BY status"):
        counts[row["status"]] = row["n"]
    return render_template(
        "dashboard.html", repairs=repairs, statuses=status_names(), counts=counts,
        q=q, status_filter=status_filter,
        stat_pending=counts.get("Pending review", 0),
        stat_active=sum(counts.get(s, 0) for s in
                        ["Received", "Diagnosing", "Awaiting parts", "In progress"]),
        stat_ready=counts.get("Ready for pickup", 0),
        inbound_on=(setting("imessage_read_enabled", "0") == "1"
                    or setting("imap_enabled", "0") == "1"),
        imessage_ok=imessage_available())


# --------------------------------------------------------------------------- #
# Repairs
# --------------------------------------------------------------------------- #
def create_repair(customer_id, device, notes, status, price, source, prefix_id,
                  device_id=None, issue_ids=None, asset_id=None,
                  inbound="", outbound="", service_ids=None, label_format="",
                  warranty_of=None):
    db = get_db()
    number = next_repair_number(prefix_id)
    token = secrets.token_urlsafe(9)
    ts = now_iso()
    cur = db.execute(
        "INSERT INTO repairs(repair_number,customer_id,device,issue,status,price,"
        "public_token,source,device_id,asset_id,inbound_method,outbound_method,"
        "label_format,warranty_of,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (number, customer_id, device, notes, status, price, token, source,
         device_id, asset_id, inbound, outbound, label_format, warranty_of, ts, ts))
    rid = cur.lastrowid
    for iid in (issue_ids or []):
        db.execute("INSERT OR IGNORE INTO repair_issues(repair_id, issue_id) VALUES(?,?)",
                   (rid, int(iid)))
    for sid in (service_ids or []):
        db.execute("INSERT OR IGNORE INTO repair_services(repair_id, service_id) VALUES(?,?)",
                   (rid, int(sid)))
    log_update(rid, status, "Ticket created.", "system")
    return rid


def get_repair(rid):
    return get_db().execute(
        "SELECT r.*, c.first_name, c.last_name, c.phone, c.email, "
        "c.address1, c.address2, c.city, c.state, c.postal_code, c.country, "
        "a.serial AS asset_serial, a.device AS asset_device, d.name AS device_name, "
        "(SELECT repair_number FROM repairs o WHERE o.id=r.warranty_of) AS warranty_of_number, "
        "(SELECT GROUP_CONCAT(i.name, ', ') FROM repair_issues ri "
        " JOIN issues i ON i.id=ri.issue_id WHERE ri.repair_id=r.id) AS issue_names "
        "FROM repairs r JOIN customers c ON c.id = r.customer_id "
        "LEFT JOIN assets a ON a.id = r.asset_id "
        "LEFT JOIN devices d ON d.id = r.device_id WHERE r.id=?", (rid,)).fetchone()


@app.route("/repairs/new", methods=["GET", "POST"])
@login_required
def repair_new():
    db = get_db()
    if request.method == "POST":
        first = request.form.get("first_name", "").strip()
        last = request.form.get("last_name", "").strip()
        phone = normalize_phone(request.form.get("phone", ""))
        email = request.form.get("email", "").strip()
        addr = address_from_form(request.form)
        device = request.form.get("device", "").strip()
        notes = request.form.get("issue", "").strip()
        price = request.form.get("price", "").strip()
        serial = request.form.get("serial", "").strip()
        prefix_id = request.form.get("prefix_id", "").strip() or setting("default_prefix_id")
        device_id = request.form.get("device_id", "").strip() or None
        issue_ids = request.form.getlist("issue_ids")
        service_ids = request.form.getlist("service_ids")
        inbound = request.form.get("inbound_method", "").strip()
        outbound = request.form.get("outbound_method", "").strip()
        existing_id = request.form.get("customer_id", "").strip()

        if not last:
            flash("A last name is required (customers look up repairs by it).", "error")
            return redirect(url_for("repair_new"))

        if existing_id:
            customer_id = int(existing_id)
            # Update address if the form provided any address values.
            if any(addr.values()):
                db.execute(
                    "UPDATE customers SET address1=?,address2=?,city=?,state=?,postal_code=?,country=? "
                    "WHERE id=?", (addr["address1"], addr["address2"], addr["city"], addr["state"],
                                   addr["postal_code"], addr["country"], customer_id))
        else:
            cur = db.execute(
                "INSERT INTO customers(first_name,last_name,phone,email,address1,address2,"
                "city,state,postal_code,country,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (first, last, phone, email, addr["address1"], addr["address2"], addr["city"],
                 addr["state"], addr["postal_code"], addr["country"], now_iso()))
            customer_id = cur.lastrowid

        if device_id and not device:
            dn = db.execute("SELECT name FROM devices WHERE id=?", (device_id,)).fetchone()
            if dn:
                device = dn["name"]

        asset_id = link_or_create_asset(customer_id, device, serial)
        rid = create_repair(customer_id, device, notes, default_status(), price, "staff",
                            prefix_id, device_id, issue_ids, asset_id, inbound, outbound,
                            service_ids)
        db.commit()
        flash("Repair ticket created.", "ok")
        return redirect(url_for("repair_detail", rid=rid))

    customers = db.execute(
        "SELECT id, first_name, last_name, phone, email FROM customers "
        "ORDER BY last_name, first_name").fetchall()
    devices = db.execute("SELECT * FROM devices WHERE active=1 ORDER BY name").fetchall()
    return render_template("repair_new.html", customers=customers,
                           prefixes=all_prefixes(), issues=active_issues(),
                           services=active_services(), devices=devices,
                           methods_in=methods_for("in"), methods_out=methods_for("out"),
                           default_prefix_id=setting("default_prefix_id"))


@app.route("/repairs/<int:rid>")
@login_required
def repair_detail(rid):
    repair = get_repair(rid)
    if not repair:
        abort(404)
    history = get_db().execute(
        "SELECT * FROM updates WHERE repair_id=? ORDER BY id DESC", (rid,)).fetchall()
    track_url = f"{setting('base_url').rstrip('/')}/r/{repair['public_token']}"
    attached_i = attached_issues(rid)
    attached_s = attached_services(rid)
    attached_i_ids = {i["id"] for i in attached_i}
    attached_s_ids = {s["id"] for s in attached_s}
    available_i = [i for i in active_issues() if i["id"] not in attached_i_ids]
    available_s = [s for s in active_services() if s["id"] not in attached_s_ids]
    shipments = get_db().execute(
        "SELECT id, direction, carrier, service, tracking, cost, fmt, created_at, "
        "(label_pdf IS NOT NULL) AS has_pdf, (qr_png IS NOT NULL) AS has_qr "
        "FROM shipments WHERE repair_id=? ORDER BY id DESC", (rid,)).fetchall()
    warranty_children = get_db().execute(
        "SELECT id, repair_number, status FROM repairs WHERE warranty_of=? ORDER BY id", (rid,)).fetchall()
    return render_template(
        "repair_detail.html", r=repair, history=history, statuses=status_names(),
        attached_issues=attached_i, available_issues=available_i,
        attached_services=attached_s, available_services=available_s,
        issues_total=fmt_range(*issues_total(rid)),
        services_total=fmt_range(*services_total(rid)),
        shipping_total=fmt_range(*shipping_total(repair)),
        estimate_total=fmt_range(*estimate_total(repair)),
        methods_in=methods_for("in"), methods_out=methods_for("out"),
        shipments=shipments, label_pref=repair["label_format"],
        msg_preview=render_imessage_body(repair),
        email_subject=render_email_subject(repair),
        email_body=render_email_body(repair),
        msg_templates=rendered_templates(repair),
        warranty_children=warranty_children,
        chat=chat_messages(rid),
        invoices=repair_invoices(rid), paypal_ready=paypal_ready(),
        track_url=track_url, imessage_ok=imessage_available(), email_ok=email_ready())


@app.route("/repairs/<int:rid>/status", methods=["POST"])
@login_required
def repair_status(rid):
    repair = get_repair(rid)
    if not repair:
        abort(404)
    new_status = request.form.get("status", "").strip()
    note = request.form.get("note", "").strip()
    if new_status not in status_names():
        flash("Unknown status.", "error")
        return redirect(url_for("repair_detail", rid=rid))
    get_db().execute("UPDATE repairs SET status=?, updated_at=? WHERE id=?",
                     (new_status, now_iso(), rid))
    get_db().commit()
    log_update(rid, new_status, note or f"Status set to {new_status}.", "status")

    channels = []
    if request.form.get("notify_imessage") == "on":
        channels.append("imessage")
    if request.form.get("notify_email") == "on":
        channels.append("email")
    if channels:
        results = notify(get_repair(rid), channels)
        good = [lbl for lbl, ok, _ in results if ok]
        bad = [f"{lbl}: {d}" for lbl, ok, d in results if not ok]
        if good and not bad:
            flash(f"Status updated. Customer notified by {', '.join(good)}.", "ok")
        elif good and bad:
            flash(f"Status updated. Sent {', '.join(good)}. Problem with {'; '.join(bad)}", "error")
        else:
            flash(f"Status updated, but notification failed — {'; '.join(bad)}", "error")
    else:
        flash("Status updated.", "ok")
    return redirect(url_for("repair_detail", rid=rid))


@app.route("/repairs/<int:rid>/message", methods=["POST"])
@login_required
def repair_message(rid):
    repair = get_repair(rid)
    if not repair:
        abort(404)
    channel = request.form.get("channel", "imessage")
    body = request.form.get("body", "").strip()
    if not body:
        flash("Nothing to send.", "error")
        return redirect(url_for("repair_detail", rid=rid))
    if channel == "email":
        subject = request.form.get("subject", "").strip() or f"Update on {repair['repair_number']}"
        ok, detail = send_email(smtp_cfg(), repair["email"], subject, body)
        if ok:
            log_update(rid, "", f"Email: {subject}", "email")
            flash("Email sent.", "ok")
        else:
            flash(f"Could not send email: {detail}", "error")
    else:
        ok, detail = send_imessage(repair["phone"], body)
        if ok:
            log_update(rid, "", body, "imessage")
            flash("iMessage sent.", "ok")
        else:
            flash(f"Could not send: {detail}", "error")
    return redirect(url_for("repair_detail", rid=rid))


@app.route("/repairs/<int:rid>/edit", methods=["POST"])
@login_required
def repair_edit(rid):
    repair = get_repair(rid)
    if not repair:
        abort(404)
    db = get_db()
    device = request.form.get("device", "").strip()
    notes = request.form.get("issue", "").strip()
    inbound = request.form.get("inbound_method", "").strip()
    outbound = request.form.get("outbound_method", "").strip()
    device_id = request.form.get("device_id", "").strip() or None
    serial = request.form.get("serial", "").strip()
    addr = address_from_form(request.form)

    db.execute(
        "UPDATE customers SET first_name=?, last_name=?, phone=?, email=?, "
        "address1=?, address2=?, city=?, state=?, postal_code=?, country=? WHERE id=?",
        (request.form.get("first_name", "").strip(),
         request.form.get("last_name", "").strip(),
         normalize_phone(request.form.get("phone", "")),
         request.form.get("email", "").strip(),
         addr["address1"], addr["address2"], addr["city"], addr["state"],
         addr["postal_code"], addr["country"], repair["customer_id"]))

    asset_id = repair["asset_id"]
    if serial:
        asset_id = link_or_create_asset(repair["customer_id"], device, serial)

    db.execute("UPDATE repairs SET device=?, issue=?, device_id=?, asset_id=?, "
               "inbound_method=?, outbound_method=?, updated_at=? WHERE id=?",
               (device, notes, device_id, asset_id, inbound, outbound, now_iso(), rid))
    db.commit()
    flash("Details saved.", "ok")
    return redirect(url_for("repair_detail", rid=rid))


@app.route("/repairs/<int:rid>/issues/add", methods=["POST"])
@login_required
def repair_issue_add(rid):
    iid = request.form.get("issue_id", "").strip()
    if iid:
        get_db().execute("INSERT OR IGNORE INTO repair_issues(repair_id, issue_id) VALUES(?,?)",
                         (rid, int(iid)))
        get_db().execute("UPDATE repairs SET updated_at=? WHERE id=?", (now_iso(), rid))
        get_db().commit()
        flash("Issue added to ticket.", "ok")
    return redirect(url_for("repair_detail", rid=rid))


@app.route("/repairs/<int:rid>/issues/<int:iid>/remove", methods=["POST"])
@login_required
def repair_issue_remove(rid, iid):
    get_db().execute("DELETE FROM repair_issues WHERE repair_id=? AND issue_id=?", (rid, iid))
    get_db().execute("UPDATE repairs SET updated_at=? WHERE id=?", (now_iso(), rid))
    get_db().commit()
    flash("Issue removed.", "ok")
    return redirect(url_for("repair_detail", rid=rid))


@app.route("/repairs/<int:rid>/services/add", methods=["POST"])
@login_required
def repair_service_add(rid):
    sid = request.form.get("service_id", "").strip()
    if sid:
        get_db().execute("INSERT OR IGNORE INTO repair_services(repair_id, service_id) VALUES(?,?)",
                         (rid, int(sid)))
        get_db().execute("UPDATE repairs SET updated_at=? WHERE id=?", (now_iso(), rid))
        get_db().commit()
        flash("Service added to ticket.", "ok")
    return redirect(url_for("repair_detail", rid=rid))


@app.route("/repairs/<int:rid>/services/<int:sid>/remove", methods=["POST"])
@login_required
def repair_service_remove(rid, sid):
    get_db().execute("DELETE FROM repair_services WHERE repair_id=? AND service_id=?", (rid, sid))
    get_db().execute("UPDATE repairs SET updated_at=? WHERE id=?", (now_iso(), rid))
    get_db().commit()
    flash("Service removed.", "ok")
    return redirect(url_for("repair_detail", rid=rid))


@app.route("/repairs/<int:rid>/delete", methods=["POST"])
@permission_required("repairs.delete")
def repair_delete(rid):
    repair = get_repair(rid)
    if not repair:
        abort(404)
    get_db().execute("DELETE FROM repairs WHERE id=?", (rid,))
    get_db().commit()
    flash(f"Repair {repair['repair_number']} deleted.", "ok")
    return redirect(url_for("dashboard"))


@app.route("/repairs/<int:rid>/label")
@login_required
def repair_label(rid):
    repair = get_repair(rid)
    if not repair:
        abort(404)
    return render_template("label.html", r=repair)


# --------------------------------------------------------------------------- #
# Shipping labels (manual upload)
# --------------------------------------------------------------------------- #
ALLOWED_LABEL_EXT = {"pdf", "png", "jpg", "jpeg", "gif", "webp"}


@app.route("/repairs/<int:rid>/ship", methods=["POST"])
@login_required
def shipment_create(rid):
    """Store a shipping label the shop made themselves, ready to send to the customer."""
    repair = get_repair(rid)
    if not repair:
        abort(404)
    db = get_db()
    direction = request.form.get("direction", "in")
    tracking = request.form.get("tracking", "").strip()
    carrier = request.form.get("carrier", "").strip()

    file = request.files.get("label")
    if not file or not file.filename:
        flash("Choose a label file to upload (PDF or image).", "error")
        return redirect(url_for("repair_detail", rid=rid))
    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in ALLOWED_LABEL_EXT:
        flash("That file type isn't supported. Upload a PDF or an image (PNG/JPG).", "error")
        return redirect(url_for("repair_detail", rid=rid))
    data = file.read()
    if not data:
        flash("That file looks empty.", "error")
        return redirect(url_for("repair_detail", rid=rid))

    # A PDF is treated as a printable label; an image is treated as a scannable QR/label image.
    if ext == "pdf":
        pdf_bytes, qr_bytes, fmt = data, None, "pdf"
    else:
        pdf_bytes, qr_bytes, fmt = None, data, "qr"

    db.execute("INSERT INTO shipments(repair_id,direction,carrier,service,tracking,cost,fmt,"
               "label_pdf,qr_png,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
               (rid, direction, carrier, "", tracking, "", fmt, pdf_bytes, qr_bytes, now_iso()))
    db.commit()
    kind = "PDF label" if fmt == "pdf" else "QR/label image"
    log_update(rid, "", f"Uploaded a {kind}{(' (' + tracking + ')') if tracking else ''}.", "shipping")
    flash(f"{kind} uploaded — you can now send it to the customer.", "ok")
    return redirect(url_for("repair_detail", rid=rid))


@app.route("/shipments/<int:sid>/label.pdf")
@login_required
def shipment_pdf(sid):
    row = get_db().execute("SELECT label_pdf, repair_id FROM shipments WHERE id=?", (sid,)).fetchone()
    if not row or row["label_pdf"] is None:
        abort(404)
    return Response(row["label_pdf"], mimetype="application/pdf",
                    headers={"Content-Disposition": f"inline; filename=label-{sid}.pdf"})


def _sniff_image_mime(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"


@app.route("/shipments/<int:sid>/qr.png")
def shipment_qr(sid):
    row = get_db().execute("SELECT qr_png FROM shipments WHERE id=?", (sid,)).fetchone()
    if not row or row["qr_png"] is None:
        abort(404)
    return Response(row["qr_png"], mimetype=_sniff_image_mime(bytes(row["qr_png"])))


@app.route("/shipments/<int:sid>/delete", methods=["POST"])
@login_required
def shipment_delete(sid):
    row = get_db().execute("SELECT repair_id FROM shipments WHERE id=?", (sid,)).fetchone()
    if not row:
        abort(404)
    rid = row["repair_id"]
    get_db().execute("DELETE FROM shipments WHERE id=?", (sid,))
    get_db().commit()
    flash("Label removed.", "ok")
    return redirect(url_for("repair_detail", rid=rid))


@app.route("/shipments/<int:sid>/message", methods=["POST"])
@login_required
def shipment_message(sid):
    db = get_db()
    ship = db.execute("SELECT * FROM shipments WHERE id=?", (sid,)).fetchone()
    if not ship:
        abort(404)
    repair = get_repair(ship["repair_id"])
    channel = request.form.get("channel", "imessage")
    base = setting("base_url").rstrip("/")
    pdf_url = f"{base}/shipments/{sid}/label.pdf" if ship["label_pdf"] is not None else ""
    qr_url = f"{base}/shipments/{sid}/qr.png" if ship["qr_png"] is not None else ""

    if ship["fmt"] == "qr":
        label_line = f"Your shipping label (open on your phone to scan/show): {qr_url}"
    elif ship["fmt"] == "both":
        label_line = f"Printable label: {pdf_url} — or open this on your phone: {qr_url}"
    else:
        label_line = f"Your printable shipping label: {pdf_url}"

    fields = build_fields(repair)
    fields["label_line"] = label_line
    fields["tracking"] = ship["tracking"]
    body = setting("label_message_template").format_map(fields)

    if channel == "email":
        ok, detail = send_email(smtp_cfg(), repair["email"],
                                f"Shipping label for {repair['repair_number']}", body)
        kind = "Email"
    else:
        ok, detail = send_imessage(repair["phone"], body)
        kind = "iMessage"
    if ok:
        log_update(repair["id"], "", f"{kind}: sent shipping label.", channel)
        flash(f"Label sent to customer by {kind}.", "ok")
    else:
        flash(f"Couldn't send {kind}: {detail}", "error")
    return redirect(url_for("repair_detail", rid=repair["id"]))


# --------------------------------------------------------------------------- #
# Images
# --------------------------------------------------------------------------- #
@app.route("/qr/<token>.png")
def qr_image(token):
    if not get_db().execute("SELECT 1 FROM repairs WHERE public_token=?", (token,)).fetchone():
        abort(404)
    url = f"{setting('base_url').rstrip('/')}/r/{token}"
    return Response(qr_png_bytes(url), mimetype="image/png")


@app.route("/barcode/<number>.png")
def barcode_image(number):
    if not get_db().execute("SELECT 1 FROM repairs WHERE repair_number=?", (number,)).fetchone():
        abort(404)
    try:
        return Response(code128_png_bytes(number), mimetype="image/png")
    except Exception:
        abort(500)


# --------------------------------------------------------------------------- #
# Issues catalog (with device association)
# --------------------------------------------------------------------------- #
@app.route("/issues")
@permission_required("catalog")
def issues_page():
    db = get_db()
    rows = db.execute(
        "SELECT i.*, (SELECT COUNT(*) FROM repair_issues ri WHERE ri.issue_id=i.id) AS use_count "
        "FROM issues i ORDER BY i.active DESC, i.name").fetchall()
    devices = db.execute("SELECT * FROM devices WHERE active=1 ORDER BY name").fetchall()
    links = {}
    for row in db.execute("SELECT device_id, issue_id FROM device_issues"):
        links.setdefault(row["issue_id"], set()).add(row["device_id"])
    media = {}
    for m in db.execute("SELECT id, issue_id, kind, filename FROM issue_media ORDER BY id"):
        media.setdefault(m["issue_id"], []).append(m)
    return render_template("issues.html", issues=rows, devices=devices, links=links, media=media)


@app.route("/issues/add", methods=["POST"])
@permission_required("catalog")
def issues_add():
    name = request.form.get("name", "").strip()
    cost = request.form.get("estimated_cost", "").strip()
    desc = request.form.get("description", "").strip()
    if not name:
        flash("Give the issue a name.", "error")
    else:
        get_db().execute("INSERT INTO issues(name, estimated_cost, description, active, created_at) "
                         "VALUES(?,?,?,1,?)", (name, cost, desc, now_iso()))
        get_db().commit()
        flash("Issue added.", "ok")
    return redirect(url_for("issues_page"))


@app.route("/issues/<int:iid>", methods=["POST"])
@permission_required("catalog")
def issues_update(iid):
    db = get_db()
    name = request.form.get("name", "").strip()
    cost = request.form.get("estimated_cost", "").strip()
    desc = request.form.get("description", "").strip()
    active = 1 if request.form.get("active") == "on" else 0
    if name:
        db.execute("UPDATE issues SET name=?, estimated_cost=?, description=?, active=? WHERE id=?",
                   (name, cost, desc, active, iid))
    # Reset associated devices from submitted checkboxes.
    db.execute("DELETE FROM device_issues WHERE issue_id=?", (iid,))
    for did in request.form.getlist("device_ids"):
        db.execute("INSERT OR IGNORE INTO device_issues(device_id, issue_id) VALUES(?,?)",
                   (int(did), iid))
    db.commit()
    flash("Issue saved.", "ok")
    return redirect(url_for("issues_page"))


@app.route("/issues/<int:iid>/delete", methods=["POST"])
@permission_required("catalog")
def issues_delete(iid):
    db = get_db()
    used = db.execute("SELECT COUNT(*) n FROM repair_issues WHERE issue_id=?", (iid,)).fetchone()["n"]
    if used:
        db.execute("UPDATE issues SET active=0 WHERE id=?", (iid,))
        flash("Issue is used by existing repairs, so it was deactivated instead of deleted.", "ok")
    else:
        db.execute("DELETE FROM issues WHERE id=?", (iid,))
        flash("Issue deleted.", "ok")
    db.commit()
    return redirect(url_for("issues_page"))


IMAGE_EXT = {"gif", "png", "jpg", "jpeg", "webp"}
AUDIO_EXT = {"mp3", "wav", "ogg", "m4a"}
MEDIA_MIME = {"gif": "image/gif", "png": "image/png", "jpg": "image/jpeg",
              "jpeg": "image/jpeg", "webp": "image/webp", "mp3": "audio/mpeg",
              "wav": "audio/wav", "ogg": "audio/ogg", "m4a": "audio/mp4"}


@app.route("/issues/<int:iid>/media/add", methods=["POST"])
@permission_required("catalog")
def issue_media_add(iid):
    if not get_db().execute("SELECT 1 FROM issues WHERE id=?", (iid,)).fetchone():
        abort(404)
    file = request.files.get("media")
    if not file or not file.filename:
        flash("Choose a GIF/image or an MP3/audio file.", "error")
        return redirect(url_for("issues_page"))
    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in IMAGE_EXT and ext not in AUDIO_EXT:
        flash("Unsupported file. Use a GIF/PNG/JPG image or an MP3/WAV/OGG audio file.", "error")
        return redirect(url_for("issues_page"))
    data = file.read()
    if not data:
        flash("That file looked empty.", "error")
        return redirect(url_for("issues_page"))
    kind = "image" if ext in IMAGE_EXT else "audio"
    get_db().execute("INSERT INTO issue_media(issue_id, kind, filename, mime, data, created_at) "
                     "VALUES(?,?,?,?,?,?)",
                     (iid, kind, secure_filename(file.filename), MEDIA_MIME.get(ext, ""),
                      data, now_iso()))
    get_db().commit()
    flash(f"Added {kind} to the issue.", "ok")
    return redirect(url_for("issues_page"))


@app.route("/issues/media/<int:mid>")
def issue_media_serve(mid):
    """Public so the customer request form can show GIFs / play audio."""
    row = get_db().execute("SELECT mime, data FROM issue_media WHERE id=?", (mid,)).fetchone()
    if not row or row["data"] is None:
        abort(404)
    return Response(row["data"], mimetype=row["mime"] or "application/octet-stream")


@app.route("/issues/media/<int:mid>/delete", methods=["POST"])
@permission_required("catalog")
def issue_media_delete(mid):
    get_db().execute("DELETE FROM issue_media WHERE id=?", (mid,))
    get_db().commit()
    flash("Media removed.", "ok")
    return redirect(url_for("issues_page"))


# --------------------------------------------------------------------------- #
# Additional Services catalog
# --------------------------------------------------------------------------- #
@app.route("/services")
@permission_required("catalog")
def services_page():
    db = get_db()
    rows = db.execute(
        "SELECT s.*, (SELECT COUNT(*) FROM repair_services rs WHERE rs.service_id=s.id) AS use_count "
        "FROM services s ORDER BY s.active DESC, s.name").fetchall()
    devices = db.execute("SELECT * FROM devices WHERE active=1 ORDER BY name").fetchall()
    links = device_service_links()
    return render_template("services.html", services=rows, devices=devices, links=links)


@app.route("/services/add", methods=["POST"])
@permission_required("catalog")
def services_add():
    name = request.form.get("name", "").strip()
    cost = request.form.get("cost", "").strip()
    desc = request.form.get("description", "").strip()
    if not name:
        flash("Give the service a name.", "error")
    else:
        get_db().execute("INSERT INTO services(name, cost, description, active, created_at) "
                         "VALUES(?,?,?,1,?)", (name, cost, desc, now_iso()))
        get_db().commit()
        flash("Service added.", "ok")
    return redirect(url_for("services_page"))


@app.route("/services/<int:sid>", methods=["POST"])
@permission_required("catalog")
def services_update(sid):
    db = get_db()
    name = request.form.get("name", "").strip()
    cost = request.form.get("cost", "").strip()
    desc = request.form.get("description", "").strip()
    active = 1 if request.form.get("active") == "on" else 0
    if name:
        db.execute("UPDATE services SET name=?, cost=?, description=?, active=? WHERE id=?",
                   (name, cost, desc, active, sid))
    # Reset associated devices from the submitted picker.
    db.execute("DELETE FROM device_services WHERE service_id=?", (sid,))
    for did in request.form.getlist("device_ids"):
        db.execute("INSERT OR IGNORE INTO device_services(device_id, service_id) VALUES(?,?)",
                   (int(did), sid))
    db.commit()
    flash("Service saved.", "ok")
    return redirect(url_for("services_page"))


@app.route("/services/<int:sid>/delete", methods=["POST"])
@permission_required("catalog")
def services_delete(sid):
    db = get_db()
    used = db.execute("SELECT COUNT(*) n FROM repair_services WHERE service_id=?", (sid,)).fetchone()["n"]
    if used:
        db.execute("UPDATE services SET active=0 WHERE id=?", (sid,))
        flash("Service is used by existing repairs, so it was deactivated instead of deleted.", "ok")
    else:
        db.execute("DELETE FROM services WHERE id=?", (sid,))
        flash("Service deleted.", "ok")
    db.commit()
    return redirect(url_for("services_page"))


# --------------------------------------------------------------------------- #
# Devices catalog
# --------------------------------------------------------------------------- #
@app.route("/devices")
@permission_required("catalog")
def devices_page():
    db = get_db()
    devices = db.execute("SELECT * FROM devices ORDER BY active DESC, name").fetchall()
    issue_links = links_by_device("device_issues", "issue_id")
    service_links = links_by_device("device_services", "service_id")
    return render_template("devices.html", devices=devices,
                           issues=active_issues(), services=active_services(),
                           prefixes=all_prefixes(), default_prefix_id=setting("default_prefix_id"),
                           issue_links=issue_links, service_links=service_links)


@app.route("/devices/add", methods=["POST"])
@permission_required("catalog")
def devices_add():
    name = request.form.get("name", "").strip()
    prefix_id = request.form.get("prefix_id", "").strip() or None
    if not name:
        flash("Give the device a name.", "error")
    else:
        get_db().execute("INSERT INTO devices(name, prefix_id, active, created_at) VALUES(?,?,1,?)",
                         (name, prefix_id, now_iso()))
        get_db().commit()
        flash("Device added.", "ok")
    return redirect(url_for("devices_page"))


@app.route("/devices/<int:did>", methods=["POST"])
@permission_required("catalog")
def devices_update(did):
    name = request.form.get("name", "").strip()
    active = 1 if request.form.get("active") == "on" else 0
    prefix_id = request.form.get("prefix_id", "").strip() or None
    db = get_db()
    if name:
        db.execute("UPDATE devices SET name=?, active=?, prefix_id=? WHERE id=?",
                   (name, active, prefix_id, did))
    db.execute("DELETE FROM device_issues WHERE device_id=?", (did,))
    for iid in request.form.getlist("issue_ids"):
        db.execute("INSERT OR IGNORE INTO device_issues(device_id, issue_id) VALUES(?,?)",
                   (did, int(iid)))
    db.execute("DELETE FROM device_services WHERE device_id=?", (did,))
    for sid in request.form.getlist("service_ids"):
        db.execute("INSERT OR IGNORE INTO device_services(device_id, service_id) VALUES(?,?)",
                   (did, int(sid)))
    db.commit()
    flash("Device saved.", "ok")
    return redirect(url_for("devices_page"))


@app.route("/devices/<int:did>/delete", methods=["POST"])
@permission_required("catalog")
def devices_delete(did):
    db = get_db()
    db.execute("UPDATE repairs SET device_id=NULL WHERE device_id=?", (did,))
    db.execute("DELETE FROM devices WHERE id=?", (did,))
    db.commit()
    flash("Device deleted.", "ok")
    return redirect(url_for("devices_page"))


# --------------------------------------------------------------------------- #
# Assets
# --------------------------------------------------------------------------- #
@app.route("/assets")
@login_required
def assets_page():
    db = get_db()
    q = request.args.get("q", "").strip()
    sql = ("SELECT a.*, c.first_name, c.last_name, COUNT(r.id) AS repair_count "
           "FROM assets a JOIN customers c ON c.id = a.customer_id "
           "LEFT JOIN repairs r ON r.asset_id = a.id WHERE 1=1")
    params = []
    if q:
        sql += (" AND (a.serial LIKE ? OR a.device LIKE ? OR c.last_name LIKE ? "
                "OR c.first_name LIKE ?)")
        params += [f"%{q}%"] * 4
    sql += " GROUP BY a.id ORDER BY c.last_name, a.device"
    assets = db.execute(sql, params).fetchall()
    customers = db.execute("SELECT id, first_name, last_name FROM customers "
                           "ORDER BY last_name, first_name").fetchall()
    media = {}
    for m in db.execute("SELECT id, asset_id, kind, label FROM asset_media ORDER BY id"):
        media.setdefault(m["asset_id"], []).append(m)
    return render_template("assets.html", assets=assets, customers=customers, q=q, media=media)


@app.route("/assets/add", methods=["POST"])
@login_required
def assets_add():
    customer_id = request.form.get("customer_id", "").strip()
    device = request.form.get("device", "").strip()
    serial = request.form.get("serial", "").strip()
    notes = request.form.get("notes", "").strip()
    if not customer_id or not (device or serial):
        flash("Pick a customer and enter at least a device or serial.", "error")
    else:
        get_db().execute("INSERT INTO assets(customer_id,device,serial,notes,created_at) "
                         "VALUES(?,?,?,?,?)", (int(customer_id), device, serial, notes, now_iso()))
        get_db().commit()
        flash("Asset added.", "ok")
    return redirect(url_for("assets_page"))


@app.route("/assets/<int:aid>", methods=["POST"])
@login_required
def assets_update(aid):
    get_db().execute("UPDATE assets SET device=?, serial=?, notes=? WHERE id=?",
                     (request.form.get("device", "").strip(),
                      request.form.get("serial", "").strip(),
                      request.form.get("notes", "").strip(), aid))
    get_db().commit()
    flash("Asset saved.", "ok")
    return redirect(url_for("assets_page"))


@app.route("/assets/<int:aid>/transfer", methods=["POST"])
@login_required
def assets_transfer(aid):
    new_owner = request.form.get("customer_id", "").strip()
    if new_owner:
        get_db().execute("UPDATE assets SET customer_id=? WHERE id=?", (int(new_owner), aid))
        get_db().commit()
        flash("Asset transferred to the new owner.", "ok")
    return redirect(url_for("assets_page"))


@app.route("/assets/<int:aid>/delete", methods=["POST"])
@login_required
def assets_delete(aid):
    db = get_db()
    db.execute("UPDATE repairs SET asset_id=NULL WHERE asset_id=?", (aid,))
    db.execute("DELETE FROM assets WHERE id=?", (aid,))
    db.commit()
    flash("Asset deleted.", "ok")
    return redirect(url_for("assets_page"))


@app.route("/assets/<int:aid>/media/add", methods=["POST"])
@login_required
def asset_media_add(aid):
    if not get_db().execute("SELECT 1 FROM assets WHERE id=?", (aid,)).fetchone():
        abort(404)
    file = request.files.get("media")
    if not file or not file.filename:
        flash("Choose an image to upload.", "error")
        return redirect(url_for("assets_page"))
    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in IMAGE_EXT:
        flash("Asset photos must be an image (PNG/JPG/GIF/WEBP).", "error")
        return redirect(url_for("assets_page"))
    data = file.read()
    if not data:
        flash("That file looked empty.", "error")
        return redirect(url_for("assets_page"))
    label = request.form.get("label", "").strip()
    get_db().execute("INSERT INTO asset_media(asset_id, kind, label, filename, mime, data, created_at) "
                     "VALUES(?,?,?,?,?,?,?)",
                     (aid, "image", label, secure_filename(file.filename),
                      MEDIA_MIME.get(ext, "image/jpeg"), data, now_iso()))
    get_db().commit()
    flash("Photo added to the asset.", "ok")
    return redirect(url_for("assets_page"))


@app.route("/assets/media/<int:mid>")
@login_required
def asset_media_serve(mid):
    row = get_db().execute("SELECT mime, data FROM asset_media WHERE id=?", (mid,)).fetchone()
    if not row or row["data"] is None:
        abort(404)
    return Response(row["data"], mimetype=row["mime"] or "image/jpeg")


@app.route("/assets/media/<int:mid>/delete", methods=["POST"])
@login_required
def asset_media_delete(mid):
    get_db().execute("DELETE FROM asset_media WHERE id=?", (mid,))
    get_db().commit()
    flash("Photo removed.", "ok")
    return redirect(url_for("assets_page"))


# --------------------------------------------------------------------------- #
# Customers + import + edit/delete
# --------------------------------------------------------------------------- #
@app.route("/customers")
@login_required
def customers():
    rows = get_db().execute(
        "SELECT c.*, COUNT(DISTINCT r.id) AS repair_count, "
        "COUNT(DISTINCT a.id) AS asset_count FROM customers c "
        "LEFT JOIN repairs r ON r.customer_id = c.id "
        "LEFT JOIN assets a ON a.customer_id = c.id "
        "GROUP BY c.id ORDER BY c.last_name, c.first_name").fetchall()
    return render_template("customers.html", customers=rows)


@app.route("/customers/<int:cid>", methods=["GET"])
@login_required
def customer_detail(cid):
    db = get_db()
    customer = db.execute("SELECT * FROM customers WHERE id=?", (cid,)).fetchone()
    if not customer:
        abort(404)
    repairs = db.execute(
        "SELECT r.*, (SELECT GROUP_CONCAT(i.name, ', ') FROM repair_issues ri "
        "JOIN issues i ON i.id=ri.issue_id WHERE ri.repair_id=r.id) AS issue_names "
        "FROM repairs r WHERE r.customer_id=? ORDER BY r.updated_at DESC", (cid,)).fetchall()
    assets = db.execute("SELECT * FROM assets WHERE customer_id=? ORDER BY device", (cid,)).fetchall()
    return render_template("customer_detail.html", c=customer, repairs=repairs, assets=assets)


@app.route("/customers/<int:cid>/edit", methods=["POST"])
@permission_required("customers.manage")
def customer_edit(cid):
    if not get_db().execute("SELECT 1 FROM customers WHERE id=?", (cid,)).fetchone():
        abort(404)
    addr = address_from_form(request.form)
    get_db().execute(
        "UPDATE customers SET first_name=?, last_name=?, phone=?, email=?, notes=?, "
        "address1=?, address2=?, city=?, state=?, postal_code=?, country=? WHERE id=?",
        (request.form.get("first_name", "").strip(),
         request.form.get("last_name", "").strip(),
         normalize_phone(request.form.get("phone", "")),
         request.form.get("email", "").strip(),
         request.form.get("notes", "").strip(),
         addr["address1"], addr["address2"], addr["city"], addr["state"],
         addr["postal_code"], addr["country"], cid))
    get_db().commit()
    flash("Customer saved.", "ok")
    return redirect(url_for("customer_detail", cid=cid))


@app.route("/customers/<int:cid>/delete", methods=["POST"])
@permission_required("customers.manage")
def customer_delete(cid):
    db = get_db()
    n = db.execute("SELECT COUNT(*) n FROM repairs WHERE customer_id=?", (cid,)).fetchone()["n"]
    if n:
        flash(f"This customer has {n} repair(s). Delete or reassign those first.", "error")
        return redirect(url_for("customer_detail", cid=cid))
    db.execute("DELETE FROM assets WHERE customer_id=?", (cid,))
    db.execute("DELETE FROM customers WHERE id=?", (cid,))
    db.commit()
    flash("Customer deleted.", "ok")
    return redirect(url_for("customers"))


@app.route("/customers/import", methods=["POST"])
@permission_required("customers.manage")
def customers_import():
    file = request.files.get("file")
    if not file or not file.filename:
        flash("Choose a CSV file first.", "error")
        return redirect(url_for("customers"))
    try:
        text = file.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        flash("Couldn't read that file. Save it as CSV (UTF-8) and retry.", "error")
        return redirect(url_for("customers"))
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        flash("That CSV had no header row.", "error")
        return redirect(url_for("customers"))
    norm = {h.lower().strip(): h for h in reader.fieldnames}

    def pick(row, *names):
        for n in names:
            if n in norm:
                return (row.get(norm[n]) or "").strip()
        return ""

    db = get_db()
    existing = {p["phone"] for p in db.execute("SELECT phone FROM customers WHERE phone != ''")}
    added = skipped = 0
    for row in reader:
        first = pick(row, "first name", "first", "firstname")
        last = pick(row, "last name", "last", "lastname", "surname")
        full = pick(row, "name", "full name", "customer", "customer name")
        if not (first or last) and full:
            parts = full.split()
            first, last = (" ".join(parts[:-1]), parts[-1]) if len(parts) > 1 else (full, "")
        phone = normalize_phone(pick(row, "phone", "mobile", "cell", "phone number", "telephone"))
        email = pick(row, "email", "e-mail", "email address")
        notes = pick(row, "notes", "note", "comments")
        addr1 = pick(row, "address", "address 1", "street", "address line 1")
        city = pick(row, "city", "town")
        state = pick(row, "state", "province", "region")
        postal = pick(row, "zip", "zip code", "postal code", "postcode")
        if not (first or last or phone or email):
            continue
        if phone and phone in existing:
            skipped += 1
            continue
        db.execute("INSERT INTO customers(first_name,last_name,phone,email,notes,"
                   "address1,city,state,postal_code,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                   (first, last, phone, email, notes, addr1, city, state, postal, now_iso()))
        if phone:
            existing.add(phone)
        added += 1
    db.commit()
    flash(f"Imported {added} customer(s). Skipped {skipped} duplicate(s) by phone.", "ok")
    return redirect(url_for("customers"))


# --------------------------------------------------------------------------- #
# Settings (+ prefixes + shipping methods)
# --------------------------------------------------------------------------- #
@app.route("/settings", methods=["GET", "POST"])
@permission_required("settings")
def settings_page():
    if request.method == "POST":
        for key in SETTING_KEYS:
            if key in request.form:
                val = request.form.get(key, "").strip()
                # A blank Secret/password means "keep the saved one" (never echoed back).
                if key in ("paypal_secret", "imap_pass") and not val:
                    continue
                set_setting(key, val)
        set_setting("smtp_enabled", "1" if request.form.get("smtp_enabled") == "on" else "0")
        set_setting("show_repaired_counter",
                    "1" if request.form.get("show_repaired_counter") == "on" else "0")
        set_setting("portal_enabled", "1" if request.form.get("portal_enabled") == "on" else "0")
        set_setting("imessage_read_enabled",
                    "1" if request.form.get("imessage_read_enabled") == "on" else "0")
        set_setting("imap_enabled", "1" if request.form.get("imap_enabled") == "on" else "0")
        new_pw = request.form.get("new_password", "")
        if new_pw:
            if new_pw == request.form.get("confirm_password", ""):
                # Change the signed-in user's own password (not a shared one).
                u = current_user()
                get_db().execute("UPDATE users SET password_hash=? WHERE id=?",
                                 (generate_password_hash(new_pw), u["id"]))
                get_db().commit()
                flash("Settings saved and your password changed.", "ok")
            else:
                flash("Passwords didn't match — other settings saved.", "error")
                return redirect(url_for("settings_page"))
        else:
            flash("Settings saved.", "ok")
        return redirect(url_for("settings_page"))

    prefixes = get_db().execute(
        "SELECT p.*, (SELECT COUNT(*) FROM repairs r WHERE r.repair_number LIKE p.code || '-%') "
        "AS use_count FROM prefixes p ORDER BY p.code").fetchall()
    u = current_user()
    return render_template(
        "settings.html", s={k: setting(k) for k in SETTING_KEYS},
        prefixes=prefixes, default_prefix_id=setting("default_prefix_id"),
        imessage_ok=imessage_available(), email_ok=email_ready(),
        repaired_count_now=repaired_count_display(),
        statuses=all_statuses(), status_color_choices=STATUS_COLOR_CHOICES,
        using_default_pw=check_password_hash(u["password_hash"], "changeme"))


@app.route("/settings/prefixes/add", methods=["POST"])
@permission_required("catalog")
def prefixes_add():
    code = request.form.get("code", "").strip().upper()
    label = request.form.get("label", "").strip()
    try:
        start = int(request.form.get("start_number", "1001"))
    except ValueError:
        start = 1001
    if not re.match(r"^[A-Z0-9]{1,6}$", code):
        flash("Prefix code should be 1–6 letters or numbers (e.g. PH, LT, TAB).", "error")
        return redirect(url_for("settings_page"))
    db = get_db()
    if db.execute("SELECT 1 FROM prefixes WHERE code=?", (code,)).fetchone():
        flash("That prefix code already exists.", "error")
        return redirect(url_for("settings_page"))
    db.execute("INSERT INTO prefixes(code,label,counter,created_at) VALUES(?,?,?,?)",
               (code, label, start - 1, now_iso()))
    db.commit()
    flash(f"Added device type {code}. Next ticket: {code}-{start}.", "ok")
    return redirect(url_for("settings_page"))


@app.route("/settings/prefixes/<int:pid>", methods=["POST"])
@permission_required("catalog")
def prefixes_update(pid):
    db = get_db()
    label = request.form.get("label", "").strip()
    try:
        next_num = int(request.form.get("next_number", "0"))
    except ValueError:
        next_num = 0
    if next_num > 0:
        db.execute("UPDATE prefixes SET label=?, counter=? WHERE id=?", (label, next_num - 1, pid))
    else:
        db.execute("UPDATE prefixes SET label=? WHERE id=?", (label, pid))
    if request.form.get("make_default") == "on":
        set_setting("default_prefix_id", str(pid))
    db.commit()
    flash("Device type saved.", "ok")
    return redirect(url_for("settings_page"))


@app.route("/settings/prefixes/<int:pid>/delete", methods=["POST"])
@permission_required("catalog")
def prefixes_delete(pid):
    db = get_db()
    p = db.execute("SELECT code FROM prefixes WHERE id=?", (pid,)).fetchone()
    if not p:
        abort(404)
    used = db.execute("SELECT COUNT(*) n FROM repairs WHERE repair_number LIKE ?",
                      (p["code"] + "-%",)).fetchone()["n"]
    total = db.execute("SELECT COUNT(*) n FROM prefixes").fetchone()["n"]
    if used:
        flash(f"Can't delete {p['code']} — {used} repair(s) use it.", "error")
    elif total <= 1:
        flash("Keep at least one device type.", "error")
    else:
        db.execute("DELETE FROM prefixes WHERE id=?", (pid,))
        if setting("default_prefix_id") == str(pid):
            nd = db.execute("SELECT id FROM prefixes ORDER BY id LIMIT 1").fetchone()
            set_setting("default_prefix_id", str(nd["id"]))
        db.commit()
        flash(f"Deleted device type {p['code']}.", "ok")
    return redirect(url_for("settings_page"))


# --------------------------------------------------------------------------- #
# Shipping & handling options  (Phase 2 — own page, device links, addr toggle)
# --------------------------------------------------------------------------- #
def _device_opts():
    """Tag-picker option list of active catalog devices: [{id, name}, ...]."""
    return [{"id": d["id"], "name": d["name"]}
            for d in get_db().execute(
                "SELECT id, name FROM devices WHERE active=1 ORDER BY name")]


@app.route("/shipping")
@permission_required("catalog")
def shipping_page():
    db = get_db()
    method_links = links_by_method()
    return render_template(
        "shipping.html",
        methods_in=db.execute("SELECT * FROM methods WHERE direction='in' ORDER BY sort, id").fetchall(),
        methods_out=db.execute("SELECT * FROM methods WHERE direction='out' ORDER BY sort, id").fetchall(),
        device_opts=_device_opts(), method_links=method_links,
        device_count=db.execute("SELECT COUNT(*) n FROM devices WHERE active=1").fetchone()["n"])


@app.route("/shipping/add", methods=["POST"])
@permission_required("catalog")
def shipping_add():
    direction = request.form.get("direction", "").strip()
    label = request.form.get("label", "").strip()
    cost = request.form.get("cost", "").strip()
    is_ship = 1 if request.form.get("is_shipping") == "on" else 0
    supports_qr = 1 if request.form.get("supports_qr") == "on" else 0
    requires_addr = 1 if request.form.get("requires_address") == "on" else 0
    carrier = request.form.get("carrier", "").strip()
    dmin = _int_or_zero(request.form.get("days_min"))
    dmax = _int_or_zero(request.form.get("days_max"))
    if direction in ("in", "out") and label:
        nxt = get_db().execute("SELECT COALESCE(MAX(sort),0)+1 s FROM methods WHERE direction=?",
                               (direction,)).fetchone()["s"]
        get_db().execute(
            "INSERT INTO methods(direction,label,cost,sort,active,is_shipping,supports_qr,"
            "requires_address,carrier,service,days_min,days_max,created_at) "
            "VALUES(?,?,?,?,1,?,?,?,?,'',?,?,?)",
            (direction, label, cost, nxt, is_ship, supports_qr, requires_addr, carrier,
             dmin, dmax, now_iso()))
        get_db().commit()
        flash("Shipping option added.", "ok")
    else:
        flash("Give the option a name.", "error")
    return redirect(url_for("shipping_page"))


@app.route("/shipping/<int:mid>", methods=["POST"])
@permission_required("catalog")
def shipping_update(mid):
    db = get_db()
    label = request.form.get("label", "").strip()
    cost = request.form.get("cost", "").strip()
    active = 1 if request.form.get("active") == "on" else 0
    is_ship = 1 if request.form.get("is_shipping") == "on" else 0
    supports_qr = 1 if request.form.get("supports_qr") == "on" else 0
    requires_addr = 1 if request.form.get("requires_address") == "on" else 0
    carrier = request.form.get("carrier", "").strip()
    dmin = _int_or_zero(request.form.get("days_min"))
    dmax = _int_or_zero(request.form.get("days_max"))
    if not label:
        flash("An option needs a name.", "error")
        return redirect(url_for("shipping_page"))
    db.execute("UPDATE methods SET label=?, cost=?, active=?, is_shipping=?, supports_qr=?, "
               "requires_address=?, carrier=?, days_min=?, days_max=? WHERE id=?",
               (label, cost, active, is_ship, supports_qr, requires_addr, carrier,
                dmin, dmax, mid))
    # Replace this option's device links. No links == "applies to every device".
    db.execute("DELETE FROM method_devices WHERE method_id=?", (mid,))
    for did in request.form.getlist("device_ids"):
        try:
            db.execute("INSERT OR IGNORE INTO method_devices(method_id, device_id) VALUES(?,?)",
                       (mid, int(did)))
        except ValueError:
            pass
    db.commit()
    flash("Shipping option saved.", "ok")
    return redirect(url_for("shipping_page"))


@app.route("/shipping/<int:mid>/delete", methods=["POST"])
@permission_required("catalog")
def shipping_delete(mid):
    get_db().execute("DELETE FROM methods WHERE id=?", (mid,))  # cascades method_devices
    get_db().commit()
    flash("Shipping option deleted.", "ok")
    return redirect(url_for("shipping_page"))


# --------------------------------------------------------------------------- #
# Message templates  (owner/manager-managed; used in the ticket composer)
# --------------------------------------------------------------------------- #
TEMPLATE_PLACEHOLDERS = ["first_name", "last_name", "device", "repair_number", "status",
                         "total", "issue_name", "track_url", "shop_name"]


@app.route("/templates")
@permission_required("catalog")
def templates_page():
    return render_template("templates.html", templates=all_templates(),
                           placeholders=TEMPLATE_PLACEHOLDERS)


@app.route("/templates/add", methods=["POST"])
@permission_required("catalog")
def templates_add():
    name = request.form.get("name", "").strip()
    body = request.form.get("body", "").strip()
    if not name or not body:
        flash("A template needs a name and some text.", "error")
        return redirect(url_for("templates_page"))
    nxt = get_db().execute("SELECT COALESCE(MAX(sort),0)+1 s FROM message_templates").fetchone()["s"]
    get_db().execute("INSERT INTO message_templates(name, body, sort, active, created_at) "
                     "VALUES(?,?,?,1,?)", (name, body, nxt, now_iso()))
    get_db().commit()
    flash("Template added.", "ok")
    return redirect(url_for("templates_page"))


@app.route("/templates/<int:tid>", methods=["POST"])
@permission_required("catalog")
def templates_update(tid):
    name = request.form.get("name", "").strip()
    body = request.form.get("body", "").strip()
    active = 1 if request.form.get("active") == "on" else 0
    if not name or not body:
        flash("A template needs a name and some text.", "error")
        return redirect(url_for("templates_page"))
    get_db().execute("UPDATE message_templates SET name=?, body=?, active=? WHERE id=?",
                     (name, body, active, tid))
    get_db().commit()
    flash("Template saved.", "ok")
    return redirect(url_for("templates_page"))


@app.route("/templates/<int:tid>/delete", methods=["POST"])
@permission_required("catalog")
def templates_delete(tid):
    get_db().execute("DELETE FROM message_templates WHERE id=?", (tid,))
    get_db().commit()
    flash("Template deleted.", "ok")
    return redirect(url_for("templates_page"))


# --------------------------------------------------------------------------- #
# Staff users & roles  (Owner only)
# --------------------------------------------------------------------------- #
def _active_owner_count(exclude_id=None):
    sql = "SELECT COUNT(*) n FROM users WHERE role='owner' AND active=1"
    params = []
    if exclude_id is not None:
        sql += " AND id<>?"
        params.append(exclude_id)
    return get_db().execute(sql, params).fetchone()["n"]


@app.route("/users")
@permission_required("users")
def users_page():
    users = get_db().execute(
        "SELECT * FROM users ORDER BY active DESC, "
        "CASE role WHEN 'owner' THEN 0 WHEN 'manager' THEN 1 ELSE 2 END, username").fetchall()
    return render_template("users.html", users=users, roles=ROLES, role_labels=ROLE_LABELS,
                           me=current_user())


@app.route("/users/add", methods=["POST"])
@permission_required("users")
def users_add():
    username = request.form.get("username", "").strip().lower()
    name = request.form.get("name", "").strip()
    role = request.form.get("role", "technician").strip()
    pw = request.form.get("password", "")
    if not re.match(r"^[a-z0-9_.-]{2,32}$", username):
        flash("Username should be 2–32 characters: letters, numbers, dot, dash, underscore.", "error")
        return redirect(url_for("users_page"))
    if role not in ROLES:
        flash("Pick a valid role.", "error")
        return redirect(url_for("users_page"))
    if len(pw) < 6:
        flash("Give the new user a password of at least 6 characters.", "error")
        return redirect(url_for("users_page"))
    db = get_db()
    if db.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
        flash("That username is taken.", "error")
        return redirect(url_for("users_page"))
    db.execute("INSERT INTO users(username,name,password_hash,role,active,created_at) "
               "VALUES(?,?,?,?,1,?)",
               (username, name, generate_password_hash(pw), role, now_iso()))
    db.commit()
    flash(f"Added {ROLE_LABELS[role]} “{username}”.", "ok")
    return redirect(url_for("users_page"))


@app.route("/users/<int:uid>", methods=["POST"])
@permission_required("users")
def users_update(uid):
    db = get_db()
    target = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if not target:
        abort(404)
    name = request.form.get("name", "").strip()
    role = request.form.get("role", target["role"]).strip()
    active = 1 if request.form.get("active") == "on" else 0
    if role not in ROLES:
        flash("Pick a valid role.", "error")
        return redirect(url_for("users_page"))
    # Don't let the shop lock itself out: keep at least one active owner.
    losing_owner = (target["role"] == "owner" and target["active"]
                    and (role != "owner" or active == 0))
    if losing_owner and _active_owner_count(exclude_id=uid) == 0:
        flash("This is the last active Owner — promote someone else to Owner first.", "error")
        return redirect(url_for("users_page"))
    db.execute("UPDATE users SET name=?, role=?, active=? WHERE id=?",
               (name, role, active, uid))
    # Optional password reset by an Owner.
    new_pw = request.form.get("password", "")
    if new_pw:
        if len(new_pw) < 6:
            flash("Saved, but the new password was too short (min 6) — left unchanged.", "error")
        else:
            db.execute("UPDATE users SET password_hash=? WHERE id=?",
                       (generate_password_hash(new_pw), uid))
            flash(f"Saved “{target['username']}” and reset their password.", "ok")
            db.commit()
            return redirect(url_for("users_page"))
    db.commit()
    flash(f"Saved “{target['username']}”.", "ok")
    return redirect(url_for("users_page"))


@app.route("/users/<int:uid>/delete", methods=["POST"])
@permission_required("users")
def users_delete(uid):
    db = get_db()
    target = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if not target:
        abort(404)
    me = current_user()
    if me and me["id"] == uid:
        flash("You can't delete your own account while signed in.", "error")
        return redirect(url_for("users_page"))
    if target["role"] == "owner" and target["active"] and _active_owner_count(exclude_id=uid) == 0:
        flash("That's the last active Owner — can't delete it.", "error")
        return redirect(url_for("users_page"))
    db.execute("DELETE FROM users WHERE id=?", (uid,))
    db.commit()
    flash(f"Deleted “{target['username']}”.", "ok")
    return redirect(url_for("users_page"))


@app.route("/settings/statuses/add", methods=["POST"])
@permission_required("catalog")
def statuses_add():
    name = request.form.get("name", "").strip()
    color = request.form.get("color", "slate").strip() or "slate"
    if not name:
        flash("Give the status a name.", "error")
    elif get_db().execute("SELECT 1 FROM statuses WHERE name=?", (name,)).fetchone():
        flash("A status with that name already exists.", "error")
    else:
        nxt = get_db().execute("SELECT COALESCE(MAX(sort),0)+1 s FROM statuses").fetchone()["s"]
        get_db().execute("INSERT INTO statuses(name,color,sort,created_at) VALUES(?,?,?,?)",
                         (name, color, nxt, now_iso()))
        get_db().commit()
        flash("Status added.", "ok")
    return redirect(url_for("settings_page"))


@app.route("/settings/statuses/<int:sid>", methods=["POST"])
@permission_required("catalog")
def statuses_update(sid):
    db = get_db()
    row = db.execute("SELECT * FROM statuses WHERE id=?", (sid,)).fetchone()
    if not row:
        abort(404)
    name = request.form.get("name", "").strip()
    color = request.form.get("color", row["color"]).strip() or "slate"
    sort = _int_or_zero(request.form.get("sort", row["sort"]))
    if not name:
        flash("Status name can't be empty.", "error")
        return redirect(url_for("settings_page"))
    clash = db.execute("SELECT 1 FROM statuses WHERE name=? AND id<>?", (name, sid)).fetchone()
    if clash:
        flash("Another status already uses that name.", "error")
        return redirect(url_for("settings_page"))
    if name != row["name"]:
        # Keep existing repairs and the default/intake pointers in sync with the rename.
        db.execute("UPDATE repairs SET status=? WHERE status=?", (name, row["name"]))
        db.execute("UPDATE updates SET status=? WHERE status=?", (name, row["name"]))
        for key in ("default_status", "intake_status"):
            if setting(key) == row["name"]:
                set_setting(key, name)
    db.execute("UPDATE statuses SET name=?, color=?, sort=? WHERE id=?", (name, color, sort, sid))
    db.commit()
    flash("Status saved.", "ok")
    return redirect(url_for("settings_page"))


@app.route("/settings/statuses/<int:sid>/delete", methods=["POST"])
@permission_required("catalog")
def statuses_delete(sid):
    db = get_db()
    row = db.execute("SELECT * FROM statuses WHERE id=?", (sid,)).fetchone()
    if not row:
        abort(404)
    used = db.execute("SELECT COUNT(*) n FROM repairs WHERE status=?", (row["name"],)).fetchone()["n"]
    total = db.execute("SELECT COUNT(*) n FROM statuses").fetchone()["n"]
    if used:
        flash(f"Can't delete “{row['name']}” — {used} repair(s) are in that status.", "error")
    elif total <= 1:
        flash("Keep at least one status.", "error")
    else:
        db.execute("DELETE FROM statuses WHERE id=?", (sid,))
        db.commit()
        flash("Status deleted.", "ok")
    return redirect(url_for("settings_page"))


# --------------------------------------------------------------------------- #
# Public portal
# --------------------------------------------------------------------------- #
def _public_history(repair):
    if not repair:
        return []
    return get_db().execute(
        "SELECT status, note, created_at FROM updates "
        "WHERE repair_id=? AND channel IN ('status','system') ORDER BY id ASC",
        (repair["id"],)).fetchall()


@app.route("/track", methods=["GET", "POST"])
def track():
    result = None
    searched = False
    if request.method == "POST":
        searched = True
        number = request.form.get("repair_number", "").strip()
        last = request.form.get("last_name", "").strip()
        if number and last:
            result = get_db().execute(
                "SELECT r.*, c.first_name, c.last_name, "
                "(SELECT GROUP_CONCAT(i.name, ', ') FROM repair_issues ri "
                " JOIN issues i ON i.id=ri.issue_id WHERE ri.repair_id=r.id) AS issue_names "
                "FROM repairs r JOIN customers c ON c.id = r.customer_id "
                "WHERE r.repair_number = ? AND LOWER(c.last_name) = LOWER(?)",
                (number, last)).fetchone()
    return render_template("track.html", result=result, searched=searched,
                           est=fmt_range(*estimate_total(result)) if result else "",
                           history=_public_history(result),
                           asset_photos=public_asset_photos(result) if result else [],
                           **_public_extras())


@app.route("/r/<token>")
def repair_token(token):
    repair = get_db().execute(
        "SELECT r.*, c.first_name, c.last_name, "
        "(SELECT GROUP_CONCAT(i.name, ', ') FROM repair_issues ri "
        " JOIN issues i ON i.id=ri.issue_id WHERE ri.repair_id=r.id) AS issue_names "
        "FROM repairs r JOIN customers c ON c.id = r.customer_id WHERE r.public_token=?",
        (token,)).fetchone()
    if not repair:
        abort(404)
    if current_user():
        return redirect(url_for("repair_detail", rid=repair["id"]))
    # The owning, signed-in customer can see + use the chat thread; anonymous link viewers can't.
    can_chat = bool(_customer_owns_repair(repair["id"]))
    return render_template("public_status.html", result=repair,
                           est=fmt_range(*estimate_total(repair)),
                           history=_public_history(repair),
                           asset_photos=public_asset_photos(repair),
                           can_chat=can_chat,
                           chat=chat_messages(repair["id"]) if can_chat else [])


# --------------------------------------------------------------------------- #
# Customer portal
# --------------------------------------------------------------------------- #
@app.route("/portal")
def portal_home():
    cust = current_customer()
    if not cust:
        return redirect(url_for("portal_login"))
    email = session.get("cust_email", "")
    repairs = get_db().execute(
        "SELECT r.*, c.first_name, c.last_name, "
        "(SELECT GROUP_CONCAT(i.name, ', ') FROM repair_issues ri "
        " JOIN issues i ON i.id=ri.issue_id WHERE ri.repair_id=r.id) AS issue_names "
        "FROM repairs r JOIN customers c ON c.id = r.customer_id "
        "WHERE LOWER(c.email)=LOWER(?) ORDER BY r.updated_at DESC", (email,)).fetchall()
    rows = [{"r": r, "est": fmt_range(*estimate_total(r))} for r in repairs]
    assets = get_db().execute(
        "SELECT a.device, a.serial FROM assets a JOIN customers c ON c.id=a.customer_id "
        "WHERE LOWER(c.email)=LOWER(?) AND a.serial<>'' ORDER BY a.id DESC", (email,)).fetchall()
    return render_template("portal_home.html", cust=cust, rows=rows, assets=assets,
                           **_public_extras())


@app.route("/portal/login", methods=["GET", "POST"])
def portal_login():
    if current_customer():
        return redirect(url_for("portal_home"))
    if setting("portal_enabled", "1") != "1":
        return render_template("portal_login.html", disabled=True)
    if request.method == "POST":
        email = request.form.get("email", "").strip()
        if not email:
            flash("Enter the email you gave us.", "error")
            return render_template("portal_login.html", disabled=False)
        if not email_ready():
            # Can't send links without SMTP. Don't leak whether the email exists.
            flash("Email sign-in isn't available right now. You can still check a repair with "
                  "your repair number and last name.", "error")
            return render_template("portal_login.html", disabled=False)
        cust = get_db().execute(
            "SELECT * FROM customers WHERE LOWER(email)=LOWER(?) AND email<>'' "
            "ORDER BY id DESC LIMIT 1", (email,)).fetchone()
        if cust:
            token = make_magic_link_token(email)
            send_magic_link(email, cust, token)
        # Same response whether or not the email matched (anti-enumeration).
        return render_template("portal_login.html", sent=True, sent_to=email)
    return render_template("portal_login.html", disabled=False)


@app.route("/portal/verify/<token>")
def portal_verify(token):
    email = consume_magic_link(token)
    if not email:
        flash("That sign-in link is invalid or has expired. Request a fresh one.", "error")
        return redirect(url_for("portal_login"))
    session["cust_email"] = email
    session.permanent = True
    return redirect(url_for("portal_home"))


@app.route("/portal/logout", methods=["POST"])
def portal_logout():
    session.pop("cust_email", None)
    return redirect(url_for("portal_login"))


def public_asset_photos(repair):
    """Image media for a repair's asset, for customer-facing views (arrival/departure
    condition photos). Empty list if the repair has no asset or no photos."""
    if not repair or not repair["asset_id"]:
        return []
    return get_db().execute(
        "SELECT id, label, kind FROM asset_media WHERE asset_id=? AND kind='image' ORDER BY id",
        (repair["asset_id"],)).fetchall()


@app.route("/r/<token>/photo/<int:mid>")
def repair_photo(token, mid):
    """Serve an asset condition photo, scoped to a repair's public token so only someone
    holding that repair's link can view its photos."""
    row = get_db().execute(
        "SELECT m.mime, m.data FROM asset_media m "
        "JOIN repairs r ON r.asset_id = m.asset_id "
        "WHERE r.public_token=? AND m.id=? AND m.kind='image'", (token, mid)).fetchone()
    if not row or row["data"] is None:
        abort(404)
    return Response(row["data"], mimetype=row["mime"] or "image/jpeg")


@app.route("/portal/warranty/<int:rid>", methods=["POST"])
@customer_login_required
def portal_warranty(rid):
    cust = current_customer()
    email = session.get("cust_email", "")
    # The repair must belong to the signed-in customer (matched by email).
    orig = get_db().execute(
        "SELECT r.* FROM repairs r JOIN customers c ON c.id=r.customer_id "
        "WHERE r.id=? AND LOWER(c.email)=LOWER(?)", (rid, email)).fetchone()
    if not orig:
        abort(404)
    if orig["warranty_of"]:
        flash("That's already a warranty repair. Start a warranty from the original ticket.", "error")
        return redirect(url_for("portal_home"))
    # Number from the device's own prefix when we know it; else the default.
    prefix_id = setting("default_prefix_id")
    if orig["device_id"]:
        drow = get_db().execute("SELECT prefix_id FROM devices WHERE id=?",
                                (orig["device_id"],)).fetchone()
        if drow and drow["prefix_id"]:
            prefix_id = str(drow["prefix_id"])
    new_rid = create_repair(
        cust["id"], orig["device"], "Warranty request — see original repair.",
        intake_status(), "", "warranty", prefix_id,
        device_id=orig["device_id"], asset_id=orig["asset_id"], warranty_of=orig["id"])
    new = get_repair(new_rid)
    flash(f"Warranty request started — your new ticket is {new['repair_number']}.", "ok")
    return redirect(url_for("repair_token", token=new["public_token"]))


# --------------------------------------------------------------------------- #
# Per-repair chat (shop ↔ customer)
# --------------------------------------------------------------------------- #
def chat_messages(rid):
    return get_db().execute(
        "SELECT * FROM chat_messages WHERE repair_id=? ORDER BY id", (rid,)).fetchall()


def post_chat(rid, sender, author, body):
    body = (body or "").strip()
    if not body:
        return False
    get_db().execute(
        "INSERT INTO chat_messages(repair_id, sender, author, body, created_at) VALUES(?,?,?,?,?)",
        (rid, sender, author, body[:4000], now_iso()))
    # Surface the latest exchange on the ticket timeline too.
    log_update(rid, "", f"{'You' if sender=='shop' else 'Customer'}: {body[:80]}", "chat")
    return True


@app.route("/repairs/<int:rid>/chat", methods=["POST"])
@login_required
def repair_chat(rid):
    repair = get_repair(rid)
    if not repair:
        abort(404)
    u = current_user()
    if post_chat(rid, "shop", (u["name"] or u["username"]) if u else "Shop",
                 request.form.get("body", "")):
        flash("Message posted to the customer's chat.", "ok")
    else:
        flash("Nothing to post.", "error")
    return redirect(url_for("repair_detail", rid=rid) + "#chat")


def _customer_owns_repair(rid):
    email = session.get("cust_email", "")
    if not email:
        return None
    return get_db().execute(
        "SELECT r.* FROM repairs r JOIN customers c ON c.id=r.customer_id "
        "WHERE r.id=? AND LOWER(c.email)=LOWER(?)", (rid, email)).fetchone()


@app.route("/portal/chat/<int:rid>", methods=["POST"])
@customer_login_required
def portal_chat(rid):
    repair = _customer_owns_repair(rid)
    if not repair:
        abort(404)
    cust = current_customer()
    post_chat(rid, "customer", (cust["first_name"] if cust else "") or "Customer",
              request.form.get("body", ""))
    return redirect(url_for("repair_token", token=repair["public_token"]) + "#chat")


# --------------------------------------------------------------------------- #
# Reading inbound replies (iMessage via chat.db, email via IMAP) onto tickets
# --------------------------------------------------------------------------- #
def imap_cfg() -> dict:
    return {k: setting(k) for k in
            ["imap_enabled", "imap_host", "imap_port", "imap_user", "imap_pass", "imap_mailbox"]}


def _only_digits(s):
    return re.sub(r"\D", "", s or "")


def _customer_for_sender(sender):
    """Match an inbound sender (an email address or a phone number) to a customer."""
    s = (sender or "").strip()
    if not s:
        return None
    db = get_db()
    if "@" in s:
        return db.execute(
            "SELECT * FROM customers WHERE LOWER(email)=LOWER(?) AND email<>'' "
            "ORDER BY id DESC LIMIT 1", (s,)).fetchone()
    tail = _only_digits(s)[-10:]
    if len(tail) < 7:
        return None
    # Compare on the last 10 digits to be forgiving about +1 / formatting differences.
    for c in db.execute("SELECT * FROM customers WHERE phone<>'' ORDER BY id DESC"):
        if _only_digits(c["phone"]).endswith(tail):
            return c
    return None


def _repair_for_inbound(customer_id, subject="", text=""):
    """The ticket an inbound reply belongs to: a repair number mentioned in the message wins;
    otherwise the customer's most recently updated repair."""
    db = get_db()
    for token in re.findall(r"[A-Za-z]{1,6}-\d{2,}", f"{subject} {text}"):
        r = db.execute("SELECT * FROM repairs WHERE repair_number=? AND customer_id=?",
                       (token.upper(), customer_id)).fetchone()
        if r:
            return r
    return db.execute(
        "SELECT * FROM repairs WHERE customer_id=? ORDER BY updated_at DESC, id DESC LIMIT 1",
        (customer_id,)).fetchone()


def _inbound_already_seen(via, external_id):
    if not external_id:
        return False
    return get_db().execute("SELECT 1 FROM chat_messages WHERE via=? AND external_id=?",
                            (via, external_id)).fetchone() is not None


def ingest_inbound(messages):
    """Attach normalized inbound messages to tickets as customer chat messages, deduping by
    (channel, external_id). Returns counts {attached, skipped, unmatched}."""
    db = get_db()
    res = {"attached": 0, "skipped": 0, "unmatched": 0}
    for m in messages:
        via = m.get("channel", "app")
        ext = m.get("external_id")
        if _inbound_already_seen(via, ext):
            res["skipped"] += 1
            continue
        body = (m.get("text") or "").strip()
        if not body:
            res["skipped"] += 1
            continue
        cust = _customer_for_sender(m.get("sender", ""))
        rep = _repair_for_inbound(cust["id"], m.get("subject", ""), body) if cust else None
        if not (cust and rep):
            res["unmatched"] += 1
            continue
        db.execute(
            "INSERT INTO chat_messages(repair_id, sender, author, body, via, external_id, created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (rep["id"], "customer", (cust["first_name"] or "Customer"), body[:4000], via, ext,
             m.get("sent_at") or now_iso()))
        log_update(rep["id"], "",
                   f"{'iMessage' if via == 'imessage' else 'Email'} reply: {body[:80]}", "chat")
        res["attached"] += 1
    db.commit()
    return res


@app.route("/inbound/poll", methods=["POST"])
@permission_required("settings")
def inbound_poll():
    """Pull new iMessage/email replies and attach them. Safe to call repeatedly (deduped).
    On the Mac this can be wired to a launchd/cron job that POSTs here periodically."""
    messages, errors = [], []
    if setting("imessage_read_enabled", "0") == "1":
        since = int(setting("imessage_last_rowid", "0") or 0)
        ims, cursor, err = read_imessages(since)
        if err:
            errors.append(err)
        else:
            messages += ims
            set_setting("imessage_last_rowid", str(cursor))
    if setting("imap_enabled", "0") == "1":
        ems, err = read_imap(imap_cfg())
        if err:
            errors.append(err)
        else:
            messages += ems
    if setting("imessage_read_enabled", "0") != "1" and setting("imap_enabled", "0") != "1":
        flash("Turn on iMessage and/or email reading in Settings first.", "error")
        return redirect(request.referrer or url_for("dashboard"))
    res = ingest_inbound(messages)
    for e in errors:
        flash(e, "error")
    flash(f"Checked for replies — attached {res['attached']}, "
          f"skipped {res['skipped']} already seen, {res['unmatched']} unmatched.", "ok")
    return redirect(request.referrer or url_for("dashboard"))


# --------------------------------------------------------------------------- #
# PayPal invoicing  (Owner/Manager; owner confirms before sending)
# --------------------------------------------------------------------------- #
def _quote_amount(text):
    """A single billable amount from a cost range: prefer the high end, fall back to low."""
    lo, hi = money_range(text)
    return round(hi if hi > 0 else lo, 2)


def invoice_lines_for(repair):
    """Default invoice line items auto-filled from a repair's issues, services, and the
    cost of its chosen shipping methods. The owner can edit these before sending."""
    rid = repair["id"]
    lines = []
    for i in attached_issues(rid):
        lines.append({"name": i["name"], "qty": 1, "amount": _quote_amount(i["estimated_cost"])})
    for s in attached_services(rid):
        lines.append({"name": s["name"], "qty": 1, "amount": _quote_amount(s["cost"])})
    for direction, key in (("in", "inbound_method"), ("out", "outbound_method")):
        label = _field(repair, key)
        if label:
            m = method_row(direction, label)
            amt = _quote_amount(m["cost"]) if m else 0.0
            if amt > 0:
                lines.append({"name": f"Shipping: {label}", "qty": 1, "amount": amt})
    return lines


def repair_invoices(rid):
    return get_db().execute(
        "SELECT * FROM paypal_invoices WHERE repair_id=? ORDER BY id DESC", (rid,)).fetchall()


@app.route("/repairs/<int:rid>/invoice")
@permission_required("settings")
def repair_invoice(rid):
    """Step 1 — prepare: show editable, auto-filled line items for the owner to confirm."""
    repair = get_repair(rid)
    if not repair:
        abort(404)
    lines = invoice_lines_for(repair)
    total = round(sum(l["amount"] * l["qty"] for l in lines), 2)
    return render_template("invoice.html", r=repair, lines=lines, total=total,
                           currency=setting("paypal_currency", "USD"),
                           paypal_ready=paypal_ready(), env=setting("paypal_env", "sandbox"))


@app.route("/repairs/<int:rid>/invoice/send", methods=["POST"])
@permission_required("settings")
def repair_invoice_send(rid):
    """Step 2 — confirm & send: build the invoice from the confirmed form and send via PayPal."""
    repair = get_repair(rid)
    if not repair:
        abort(404)
    if not paypal_ready():
        flash("PayPal isn't set up yet — add your credentials in Settings.", "error")
        return redirect(url_for("repair_invoice", rid=rid))
    recipient = request.form.get("recipient_email", "").strip()
    if not recipient:
        flash("Add the customer's email to send the invoice to.", "error")
        return redirect(url_for("repair_invoice", rid=rid))
    names = request.form.getlist("item_name")
    amounts = request.form.getlist("item_amount")
    qtys = request.form.getlist("item_qty")
    items, total = [], 0.0
    for n, a, q in zip(names, amounts, qtys):
        n = n.strip()
        if not n:
            continue
        try:
            amt = round(float(a or 0), 2)
            qty = max(int(q or 1), 1)
        except ValueError:
            flash("Line amounts must be numbers.", "error")
            return redirect(url_for("repair_invoice", rid=rid))
        items.append({"name": n, "qty": qty, "amount": amt})
        total += amt * qty
    if not items:
        flash("Add at least one line item before sending.", "error")
        return redirect(url_for("repair_invoice", rid=rid))

    currency = setting("paypal_currency", "USD")
    invoice = {
        "recipient_email": recipient,
        "recipient_first": _field(repair, "first_name"),
        "recipient_last": _field(repair, "last_name"),
        "currency": currency, "shop_name": setting("shop_name"),
        "note": request.form.get("note", "").strip()
                or f"Invoice for repair {repair['repair_number']}.",
        "items": items,
    }
    ok, result = paypal_create_and_send(paypal_cfg(), invoice)
    if ok:
        get_db().execute(
            "INSERT INTO paypal_invoices(repair_id, invoice_id, recipient, currency, amount, "
            "view_url, status, created_at) VALUES(?,?,?,?,?,?, 'sent', ?)",
            (rid, result["id"], recipient, currency, f"{total:.2f}",
             result.get("view_url", ""), now_iso()))
        log_update(rid, "", f"PayPal invoice sent to {recipient} for "
                            f"{currency} {total:.2f}", "system")
        get_db().commit()
        flash(f"Invoice sent to {recipient} ({currency} {total:.2f}).", "ok")
    else:
        flash(f"Couldn't send the invoice — {result}", "error")
    return redirect(url_for("repair_detail", rid=rid))


def _issue_media_list(issue_id):
    return [{"id": m["id"], "kind": m["kind"]} for m in issue_media_for(issue_id)]


def _device_catalog_map():
    """device_id (str) -> {issues:[...], services:[...]}, plus 'other' -> everything active."""
    db = get_db()
    def irow(i):
        lo, hi = money_range(i["estimated_cost"])
        return {"id": i["id"], "name": i["name"], "cost_low": lo, "cost_high": hi,
                "cost_label": fmt_range(lo, hi), "desc": i["description"],
                "media": _issue_media_list(i["id"])}
    def srow(s):
        lo, hi = money_range(s["cost"])
        return {"id": s["id"], "name": s["name"], "cost_low": lo, "cost_high": hi,
                "cost_label": fmt_range(lo, hi), "desc": s["description"]}
    issues = {i["id"]: irow(i) for i in active_issues()}
    services = {s["id"]: srow(s) for s in active_services()}
    out = {"other": {"issues": list(issues.values()), "services": list(services.values())}}
    di = {}
    for row in db.execute("SELECT device_id, issue_id FROM device_issues"):
        di.setdefault(row["device_id"], []).append(row["issue_id"])
    ds = {}
    for row in db.execute("SELECT device_id, service_id FROM device_services"):
        ds.setdefault(row["device_id"], []).append(row["service_id"])
    for d in db.execute("SELECT id FROM devices WHERE active=1"):
        out[str(d["id"])] = {
            "issues": [issues[i] for i in di.get(d["id"], []) if i in issues],
            "services": [services[s] for s in ds.get(d["id"], []) if s in services],
        }
    return out


def _public_extras():
    return dict(response_time=setting("response_time_text"),
                turnaround_time=setting("turnaround_time_text"),
                repaired_count=(repaired_count_display()
                                if setting("show_repaired_counter", "1") == "1" else 0))


def _request_context():
    devices = get_db().execute("SELECT * FROM devices WHERE active=1 ORDER BY name").fetchall()
    return dict(prefixes=all_prefixes(), devices=devices,
                device_catalog_map=json.dumps(_device_catalog_map()),
                methods_in=methods_for("in"), methods_out=methods_for("out"),
                shipping_labels=json.dumps(shipping_inbound_labels()),
                shipping_meta=json.dumps(shipping_meta_map()),
                shipping_data=json.dumps(shipping_form_data()),
                cust=current_customer(),
                **_public_extras())


@app.route("/request", methods=["GET"])
def request_form():
    return render_template("request.html", done=False, **_request_context())


@app.route("/request", methods=["POST"], endpoint="submit_request")
def submit_request():
    db = get_db()
    cust = current_customer()
    first = request.form.get("first_name", "").strip()
    last = request.form.get("last_name", "").strip()
    phone = normalize_phone(request.form.get("phone", ""))
    email = request.form.get("email", "").strip()
    # A signed-in customer's identity comes from their record, not the form.
    if cust:
        first = cust["first_name"]; last = cust["last_name"]
        phone = cust["phone"]; email = cust["email"]
    addr = address_from_form(request.form)
    comments = request.form.get("comments", "").strip()
    inbound = request.form.get("inbound_method", "").strip()
    outbound = request.form.get("outbound_method", "").strip()
    label_format = request.form.get("label_format", "").strip()

    try:
        device_count = int(request.form.get("device_count", "0") or 0)
    except ValueError:
        device_count = 0

    lines = []
    for i in range(max(device_count, 0)):
        dev_id = request.form.get(f"device_id_{i}", "").strip()
        dev_text = request.form.get(f"device_text_{i}", "").strip()
        serial = request.form.get(f"serial_{i}", "").strip()
        issue_ids = request.form.getlist(f"issues_{i}")
        svc_ids = request.form.getlist(f"services_{i}")
        label = dev_text
        catalog_id = None
        if dev_id and dev_id != "other":
            row = db.execute("SELECT name FROM devices WHERE id=?", (dev_id,)).fetchone()
            if row:
                label = label or row["name"]
                catalog_id = int(dev_id)
        if label or issue_ids or svc_ids:
            lines.append((label, catalog_id, issue_ids, serial, svc_ids))

    # Validation.
    if not (last and (phone or email)) or not lines:
        flash("Please add your last name, a phone or email, and at least one device.", "error")
        return render_template("request.html", done=False, **_request_context()), 400
    # A chosen option that requires a mailing address must come with the core address fields.
    if method_requires_address("in", inbound) or method_requires_address("out", outbound):
        need = ["address1", "city", "state", "postal_code"]
        if not all(addr.get(k) for k in need):
            flash("The shipping option you chose needs a mailing address — please fill in "
                  "address, city, state, and ZIP.", "error")
            return render_template("request.html", done=False, **_request_context()), 400
    if inbound in shipping_inbound_labels():
        if method_supports_qr(inbound):
            # QR-capable option: customer must choose PDF or QR.
            if label_format not in ("pdf", "qr"):
                flash("You picked a ship-in option, so please choose how you'd like your shipping "
                      "label (printable PDF or scannable QR code).", "error")
                return render_template("request.html", done=False, **_request_context()), 400
        else:
            # PDF-only shipping option: nothing to choose, default to PDF.
            label_format = "pdf"
    else:
        label_format = ""

    if cust:
        # Logged-in customer: reuse their record, refresh address if they provided one.
        customer_id = cust["id"]
        if any(addr.values()):
            db.execute(
                "UPDATE customers SET address1=?,address2=?,city=?,state=?,postal_code=?,country=? "
                "WHERE id=?", (addr["address1"], addr["address2"], addr["city"], addr["state"],
                               addr["postal_code"], addr["country"], customer_id))
    else:
        cur = db.execute(
            "INSERT INTO customers(first_name,last_name,phone,email,address1,address2,city,state,"
            "postal_code,country,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (first, last, phone, email, addr["address1"], addr["address2"], addr["city"],
             addr["state"], addr["postal_code"], addr["country"], now_iso()))
        customer_id = cur.lastrowid

    default_prefix = setting("default_prefix_id")
    created = []
    for idx, (label, catalog_id, issue_ids, serial, svc_ids) in enumerate(lines):
        # Number the ticket from the picked device's own prefix; "Other" falls back to default.
        prefix_id = default_prefix
        if catalog_id:
            drow = db.execute("SELECT prefix_id FROM devices WHERE id=?", (catalog_id,)).fetchone()
            if drow and drow["prefix_id"]:
                prefix_id = str(drow["prefix_id"])
        asset_id = link_or_create_asset(customer_id, label, serial)
        rid = create_repair(customer_id, label, comments, intake_status(), "",
                            "customer", prefix_id, catalog_id, issue_ids, asset_id,
                            inbound, outbound, svc_ids, label_format)
        created.append(get_repair(rid))
    db.commit()
    confirm = [{"number": r["repair_number"], "device": r["device"],
                "issue_names": r["issue_names"], "total": fmt_range(*estimate_total(r))}
               for r in created]
    return render_template("request.html", done=True, confirm=confirm,
                           inbound=inbound, outbound=outbound, label_format=label_format,
                           **_public_extras())


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
@app.errorhandler(404)
def not_found(_e):
    return render_template("error.html", code=404,
                           message="That page or repair couldn't be found."), 404


@app.errorhandler(400)
def bad_request(e):
    return render_template("error.html", code=400,
                           message=getattr(e, "description", "Bad request.")), 400


@app.errorhandler(403)
def forbidden(_e):
    return render_template("error.html", code=403,
                           message="You don't have permission to do that. "
                                   "Ask an Owner if you need access."), 403


def setting_bootstrap(key, port):
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    db.close()
    return row["value"] if row else f"http://localhost:{port}"


if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", DEFAULT_PORT))
    print("\n  RepairDesk is running.")
    print(f"  Admin:     http://localhost:{port}")
    print(f"  Customers: {setting_bootstrap('base_url', port)}/track\n")
    if not imessage_available():
        print("  NOTE: not on macOS — iMessage sending is in dev/log mode.\n")
    app.run(host="0.0.0.0", port=port, debug=False)
