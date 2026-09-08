import os
import io
import re
import math
import secrets
import sqlite3
import time
import hashlib
import zipfile
from datetime import datetime, timedelta, date
from functools import wraps

from flask import (
    Flask, render_template, request, redirect, url_for,
    session, flash, g, abort, send_from_directory, send_file, Response, jsonify
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from PIL import Image, ImageFilter, ImageOps, ImageDraw, ImageFont

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Image as RLImage, Table, TableStyle, HRFlowable
)
from reportlab.lib import colors as rl_colors
from pypdf import PdfWriter, PdfReader
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment

# ======================================================================
# CONFIG
# ======================================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DATA_DIR = os.environ.get("DATA_DIR", BASE_DIR)

DB_PATH = os.path.join(DATA_DIR, "matrimonial.db")
PRIVATE_ORIGINALS_DIR = os.path.join(DATA_DIR, "storage", "private", "profile_originals")
PRIVATE_PROOFS_DIR = os.path.join(DATA_DIR, "storage", "private", "payment_proofs")
PRIVATE_WATERMARK_CACHE_DIR = os.path.join(DATA_DIR, "storage", "private", "watermark_cache")
# Help Shadi supporting documents (ration card photo, medical bill photo, etc.)
# — same privacy guarantee as payment proofs: never under static/, only ever
# served through an @admin_required route.
PRIVATE_HELP_SHADI_DOCS_DIR = os.path.join(DATA_DIR, "storage", "private", "help_shadi_docs")
PREVIEW_DIR = os.path.join(BASE_DIR, "static", "previews")
BRANDING_DIR = os.path.join(BASE_DIR, "static", "branding")
BANNERS_DIR = os.path.join(BASE_DIR, "static", "banners")

for d in (PRIVATE_ORIGINALS_DIR, PRIVATE_PROOFS_DIR, PRIVATE_WATERMARK_CACHE_DIR,
          PRIVATE_HELP_SHADI_DOCS_DIR, PREVIEW_DIR, BRANDING_DIR, BANNERS_DIR):
    os.makedirs(d, exist_ok=True)

ALLOWED_IMAGE_EXT = {"png", "jpg", "jpeg", "webp"}
MAX_IMAGE_BYTES = 15 * 1024 * 1024  # 15 MB per image (real phone camera photos can be large)

app = Flask(__name__)

app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-only-change-this-secret-key")
# Raised from 8MB: modern iPhone/Android camera photos routinely exceed that on
# their own, so a full request (photo + form fields) was getting cut off mid-upload
# before the app could even show a proper "file too large" message. 25MB covers a
# couple of full-res photos plus form fields with real headroom.
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024

app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("SESSION_COOKIE_SECURE", "false").lower() == "true"
app.permanent_session_lifetime = timedelta(minutes=45)

# ----------------------------------------------------------------------
# Admin credentials (env only — never editable from the UI, on purpose)
# ----------------------------------------------------------------------
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD_HASH = generate_password_hash(os.environ.get("ADMIN_PASSWORD", "changeme123"))

# ----------------------------------------------------------------------
# OTP delivery — pluggable. Default "console" backend just logs the OTP
# to the server log (works everywhere, costs nothing, fine for a soft
# launch / testing). Set OTP_PROVIDER=msg91 / fast2sms (and the matching
# *_API_KEY env vars) to actually deliver real SMS. This is a single,
# isolated function — plugging a real provider later is a contained
# change, not a rewrite.
# ----------------------------------------------------------------------
OTP_PROVIDER = os.environ.get("OTP_PROVIDER", "console").lower()


def send_raw_sms(phone, message):
    """Send an arbitrary text message via whichever OTP_PROVIDER is configured.
    Shared by OTP delivery and admin alert notifications."""
    if OTP_PROVIDER == "console":
        app.logger.warning(f"[DEV SMS] To {phone}: {message}")
        return True, "console"
    try:
        import requests
        if OTP_PROVIDER == "fast2sms":
            api_key = os.environ.get("FAST2SMS_API_KEY")
            if not api_key:
                raise RuntimeError("FAST2SMS_API_KEY not set")
            r = requests.post(
                "https://www.fast2sms.com/dev/bulkV2",
                headers={"authorization": api_key},
                data={"route": "q", "message": message, "numbers": phone},
                timeout=10,
            )
            return r.ok, r.text
        if OTP_PROVIDER == "msg91":
            api_key = os.environ.get("MSG91_API_KEY")
            if not api_key:
                raise RuntimeError("MSG91_API_KEY not set")
            r = requests.post(
                "https://control.msg91.com/api/v5/flow",
                headers={"authkey": api_key},
                json={"mobiles": f"91{phone}", "message": message},
                timeout=10,
            )
            return r.ok, r.text
        app.logger.warning(f"[SMS] Unknown OTP_PROVIDER={OTP_PROVIDER}; falling back to console log.")
        app.logger.warning(f"[DEV SMS] To {phone}: {message}")
        return True, "fallback-console"
    except Exception as e:
        app.logger.error(f"[SMS] Delivery failed via {OTP_PROVIDER}: {e}")
        app.logger.warning(f"[DEV SMS] To {phone}: {message}")
        return False, str(e)


def send_otp_sms(phone, code):
    """OTP delivery gets its own path (rather than reusing send_raw_sms) because
    Fast2SMS's DLT-free 'otp' route only accepts a bare numeric code — it renders
    its own fixed template server-side and rejects a free-text message. Every
    other provider/message still goes through send_raw_sms with full text."""
    message = f"Your OTP is {code}. It expires in {OTP_TTL_MINUTES} minutes. Do not share this with anyone."
    if OTP_PROVIDER == "fast2sms":
        api_key = os.environ.get("FAST2SMS_API_KEY")
        if not api_key:
            app.logger.error("[SMS] FAST2SMS_API_KEY not set")
            app.logger.warning(f"[DEV SMS] To {phone}: {message}")
            return False, "FAST2SMS_API_KEY not set"
        try:
            import requests
            r = requests.post(
                "https://www.fast2sms.com/dev/bulkV2",
                headers={"authorization": api_key},
                data={"route": "otp", "variables_values": code, "numbers": phone},
                timeout=10,
            )
            return r.ok, r.text
        except Exception as e:
            app.logger.error(f"[SMS] OTP delivery failed via fast2sms: {e}")
            app.logger.warning(f"[DEV SMS] To {phone}: {message}")
            return False, str(e)
    return send_raw_sms(phone, message)


# ----------------------------------------------------------------------
# Admin alerts — fired the moment a customer submits payment proof, so
# the admin doesn't have to keep refreshing the dashboard. Three
# independent channels, each optional and controlled by env vars:
#   1. SMS to ADMIN_NOTIFY_PHONE  (reuses the OTP_PROVIDER above)
#   2. Email to ADMIN_NOTIFY_EMAIL (via SMTP_* vars)
#   3. WhatsApp via a generic HTTP webhook (WHATSAPP_API_URL / WHATSAPP_API_TOKEN)
# None of these require code changes to enable — they light up as soon
# as the matching env vars are set. If nothing is configured, the
# in-admin-panel live badge (see /admin/api/pending-count) still works
# with zero setup as long as the admin keeps a tab open.
# ----------------------------------------------------------------------
def notify_admin(subject, message):
    admin_phone = os.environ.get("ADMIN_NOTIFY_PHONE", "").strip()
    if admin_phone:
        try:
            send_raw_sms(admin_phone, f"{subject}: {message}")
        except Exception as e:
            app.logger.error(f"[ADMIN-NOTIFY] SMS failed: {e}")

    admin_email = os.environ.get("ADMIN_NOTIFY_EMAIL", "").strip()
    smtp_host = os.environ.get("SMTP_HOST", "").strip()
    if admin_email and smtp_host:
        try:
            import smtplib
            from email.mime.text import MIMEText
            smtp_port = int(os.environ.get("SMTP_PORT", "587"))
            smtp_user = os.environ.get("SMTP_USER", "")
            smtp_pass = os.environ.get("SMTP_PASS", "")
            mail_from = os.environ.get("SMTP_FROM", smtp_user)
            msg = MIMEText(message)
            msg["Subject"] = subject
            msg["From"] = mail_from
            msg["To"] = admin_email
            with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as server:
                server.starttls()
                if smtp_user:
                    server.login(smtp_user, smtp_pass)
                server.sendmail(mail_from, [admin_email], msg.as_string())
        except Exception as e:
            app.logger.error(f"[ADMIN-NOTIFY] Email failed: {e}")

    wa_url = os.environ.get("WHATSAPP_API_URL", "").strip()
    wa_token = os.environ.get("WHATSAPP_API_TOKEN", "").strip()
    wa_to = os.environ.get("WHATSAPP_ADMIN_NUMBER", "").strip()
    if wa_url and wa_token and wa_to:
        try:
            import requests
            requests.post(
                wa_url,
                headers={"Authorization": f"Bearer {wa_token}"},
                json={"to": wa_to, "type": "text", "text": {"body": f"{subject}: {message}"}},
                timeout=10,
            )
        except Exception as e:
            app.logger.error(f"[ADMIN-NOTIFY] WhatsApp failed: {e}")


def create_admin_event(event_type, title, body="", related_code="", action_url=""):
    """The real internal 'something happened' feed, distinct from
    notify_admin() (external SMS/email/WhatsApp) above and distinct from
    the `notifications` table (admin-authored broadcast shown TO
    visitors — see /admin/notifications). This one is system-generated,
    admin-only, and drives the /admin/events page + its unread badge."""
    db = get_db()
    db.execute(
        "INSERT INTO admin_events (event_type, title, body, related_code, action_url, is_read, created_at) "
        "VALUES (?, ?, ?, ?, ?, 0, ?)",
        (event_type, title, body, related_code, action_url, datetime.now().isoformat()),
    )
    db_commit_retry(db)


# ======================================================================
# DATABASE
# ======================================================================
def db_commit_retry(db, max_retries=3, base_delay=0.15):
    """Commit with a short retry-with-backoff, specifically for the rare
    'database is locked' case that WAL mode + busy_timeout=5000 (set on
    every connection below) don't already absorb. busy_timeout is the
    primary defense — SQLite itself waits and retries for up to 5s before
    ever raising this error — so this is a thin backstop for the unlucky
    edge case where two writers still collide right as that window closes,
    not the main mechanism. Any other OperationalError is re-raised
    immediately; only 'locked' is worth retrying."""
    for attempt in range(max_retries):
        try:
            db.commit()
            return
        except sqlite3.OperationalError as e:
            if "locked" not in str(e).lower() or attempt == max_retries - 1:
                raise
            time.sleep(base_delay * (2 ** attempt))


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, timeout=10)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
        # WAL lets reads and writes happen concurrently instead of
        # blocking each other (default SQLite journal mode serializes
        # everything); busy_timeout makes SQLite itself wait and retry
        # for up to 5s on a locked database instead of raising
        # "database is locked" immediately; synchronous=NORMAL is the
        # standard safe pairing with WAL (still durable, less fsync
        # overhead than FULL). Safe to run on every connection — it's a
        # per-connection pragma, and journal_mode=WAL is a one-time
        # on-disk change that persists after the first call.
        g.db.execute("PRAGMA journal_mode=WAL")
        g.db.execute("PRAGMA busy_timeout=5000")
        g.db.execute("PRAGMA synchronous=NORMAL")
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def _ensure_column(db, table, col, coltype):
    cols = [r["name"] for r in db.execute(f"PRAGMA table_info({table})").fetchall()]
    if col not in cols:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS profile_counter (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            next_val INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_code TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            age INTEGER NOT NULL,
            gender TEXT NOT NULL,
            city TEXT NOT NULL,
            state TEXT,
            marital_status TEXT,
            education TEXT,
            profession TEXT,
            community TEXT,
            sect TEXT,
            mother_tongue TEXT,
            height TEXT,
            income TEXT,
            work_location TEXT,
            family_details TEXT,
            bio TEXT,
            contact_number TEXT,
            contact_visible INTEGER DEFAULT 1,
            admin_verified INTEGER DEFAULT 0,
            phone_verified INTEGER DEFAULT 0,
            photo_reviewed INTEGER DEFAULT 0,
            photo_original_name TEXT,
            photo_preview_name TEXT,
            is_active INTEGER DEFAULT 1,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS unlock_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_code TEXT UNIQUE NOT NULL,
            profile_id INTEGER NOT NULL,
            user_name TEXT,
            user_phone TEXT NOT NULL,
            payment_proof_name TEXT,
            message TEXT,
            status TEXT DEFAULT 'pending',
            requested_at TEXT NOT NULL,
            decided_at TEXT,
            FOREIGN KEY (profile_id) REFERENCES profiles (id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS banners (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slot TEXT NOT NULL,
            title TEXT,
            link_url TEXT,
            image_name TEXT NOT NULL,
            is_active INTEGER DEFAULT 1,
            start_date TEXT,
            end_date TEXT,
            sort_order INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS otp_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT NOT NULL,
            code_hash TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            attempts INTEGER DEFAULT 0,
            consumed INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS admin_activity_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_username TEXT,
            action TEXT,
            detail TEXT,
            ip TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS agents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            phone TEXT UNIQUE NOT NULL,
            notes TEXT,
            access_code_hash TEXT NOT NULL,
            is_active INTEGER DEFAULT 1,
            created_at TEXT NOT NULL,
            last_login_at TEXT
        );

        CREATE TABLE IF NOT EXISTS agent_activity_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id INTEGER NOT NULL,
            action TEXT,
            detail TEXT,
            ip TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (agent_id) REFERENCES agents (id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS self_registrations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_code TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            phone TEXT NOT NULL,
            age INTEGER,
            gender TEXT,
            city TEXT,
            marital_status TEXT,
            education TEXT,
            profession TEXT,
            bio TEXT,
            photo_original_name TEXT,
            photo_preview_name TEXT,
            payment_proof_name TEXT,
            status TEXT DEFAULT 'pending',
            profile_id INTEGER,
            requested_at TEXT NOT NULL,
            decided_at TEXT
        );

        CREATE TABLE IF NOT EXISTS custom_fonts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT NOT NULL,
            filename TEXT NOT NULL,
            weight TEXT DEFAULT '400',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS shop_categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            slug TEXT UNIQUE NOT NULL,
            sort_order INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS shop_products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category_id INTEGER,
            name TEXT NOT NULL,
            slug TEXT UNIQUE NOT NULL,
            description TEXT,
            price REAL NOT NULL,
            discount_price REAL,
            stock INTEGER DEFAULT 0,
            has_variants INTEGER DEFAULT 0,
            images TEXT,
            status TEXT DEFAULT 'active',
            featured INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (category_id) REFERENCES shop_categories (id) ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS shop_variants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL,
            size TEXT,
            color TEXT,
            stock INTEGER DEFAULT 0,
            FOREIGN KEY (product_id) REFERENCES shop_products (id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS shop_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_code TEXT UNIQUE NOT NULL,
            customer_name TEXT NOT NULL,
            customer_phone TEXT NOT NULL,
            customer_address TEXT NOT NULL,
            total_amount REAL NOT NULL,
            payment_status TEXT DEFAULT 'pending',
            order_status TEXT DEFAULT 'pending',
            payment_proof_name TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS shop_order_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            product_id INTEGER,
            variant_id INTEGER,
            product_name TEXT NOT NULL,
            variant_label TEXT,
            unit_price REAL NOT NULL,
            qty INTEGER NOT NULL,
            FOREIGN KEY (order_id) REFERENCES shop_orders (id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS photo_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id INTEGER NOT NULL,
            requester_phone TEXT NOT NULL,
            message TEXT,
            status TEXT DEFAULT 'pending',
            requested_at TEXT NOT NULL,
            decided_at TEXT,
            FOREIGN KEY (profile_id) REFERENCES profiles (id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_photo_requests_status ON photo_requests(status);
        CREATE INDEX IF NOT EXISTS idx_photo_requests_profile ON photo_requests(profile_id);
        CREATE INDEX IF NOT EXISTS idx_agents_phone ON agents(phone);

        -- Help Shadi — community-assistance requests. Entirely separate from
        -- the matrimonial profiles/payment flow: nobody applying here becomes
        -- a browsable profile, and nothing here is ever shown publicly.
        CREATE TABLE IF NOT EXISTS help_shadi_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guardian_name TEXT NOT NULL,
            candidate_name TEXT NOT NULL,
            contact TEXT NOT NULL,
            address TEXT NOT NULL,
            assistance_category TEXT NOT NULL,
            details TEXT,
            document_filename TEXT,
            status TEXT DEFAULT 'pending',
            admin_notes TEXT,
            created_at TEXT NOT NULL,
            decided_at TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_help_shadi_status ON help_shadi_requests(status);

        -- Admin event feed — a real internal "something happened" log,
        -- distinct from the `notifications` table above (which is an
        -- admin-authored broadcast shown TO visitors). This one is
        -- system-generated, admin-only, and drives the /admin/events page.
        CREATE TABLE IF NOT EXISTS admin_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            title TEXT NOT NULL,
            body TEXT,
            related_code TEXT,
            action_url TEXT,
            is_read INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_admin_events_unread ON admin_events(is_read);
        CREATE INDEX IF NOT EXISTS idx_agent_activity_agent ON agent_activity_log(agent_id);
        CREATE INDEX IF NOT EXISTS idx_self_reg_status ON self_registrations(status);
        CREATE INDEX IF NOT EXISTS idx_self_reg_phone ON self_registrations(phone);

        CREATE INDEX IF NOT EXISTS idx_profiles_code ON profiles(profile_code);
        CREATE INDEX IF NOT EXISTS idx_profiles_active ON profiles(is_active);
        CREATE INDEX IF NOT EXISTS idx_requests_status ON unlock_requests(status);
        CREATE INDEX IF NOT EXISTS idx_requests_phone ON unlock_requests(user_phone);
        CREATE INDEX IF NOT EXISTS idx_requests_profile ON unlock_requests(profile_id);
        CREATE INDEX IF NOT EXISTS idx_requests_requested_at ON unlock_requests(requested_at);
        CREATE INDEX IF NOT EXISTS idx_otp_phone ON otp_codes(phone);
        CREATE INDEX IF NOT EXISTS idx_banners_slot ON banners(slot);
        """
    )
    db.execute("INSERT OR IGNORE INTO profile_counter (id, next_val) VALUES (1, 10001)")

    for col, coltype in [
        ("state", "TEXT"), ("sect", "TEXT"), ("mother_tongue", "TEXT"), ("height", "TEXT"),
        ("income", "TEXT"), ("work_location", "TEXT"), ("family_details", "TEXT"),
        ("contact_visible", "INTEGER"), ("admin_verified", "INTEGER"),
        ("phone_verified", "INTEGER"), ("photo_reviewed", "INTEGER"),
        # Hobbies (comma-separated, used for the "similar profiles" matching)
        # and the short lifestyle questionnaire asked at profile creation.
        ("hobbies", "TEXT"),
        ("lifestyle_drinking", "TEXT"), ("lifestyle_smoking", "TEXT"),
        ("lifestyle_tobacco", "TEXT"), ("lifestyle_namaz", "TEXT"), ("lifestyle_roza", "TEXT"),
        ("declaration_accepted", "INTEGER"),
        # Reliable, DB-stored record of how this profile entered the system —
        # never inferred from the frontend. Set explicitly at insert time.
        ("registration_source", "TEXT"),
    ]:
        _ensure_column(db, "profiles", col, coltype)

    for col, coltype in [
        ("hobbies", "TEXT"),
        ("lifestyle_drinking", "TEXT"), ("lifestyle_smoking", "TEXT"),
        ("lifestyle_tobacco", "TEXT"), ("lifestyle_namaz", "TEXT"), ("lifestyle_roza", "TEXT"),
        ("declaration_accepted", "INTEGER"),
    ]:
        _ensure_column(db, "self_registrations", col, coltype)

    _ensure_column(db, "unlock_requests", "package_code", "TEXT")
    # Viewer's declaration that the info won't be misused/screenshotted,
    # recorded at the moment they submit an unlock/package request.
    _ensure_column(db, "unlock_requests", "viewer_declaration", "INTEGER")

    # Match Coins wallet redemption trail on unlock/package requests —
    # how many coins (if any) were used to discount this particular request,
    # and the INR value that represented at the time (coin value can change
    # later in Settings, so we snapshot it here rather than recompute).
    _ensure_column(db, "unlock_requests", "coins_used", "INTEGER")
    _ensure_column(db, "unlock_requests", "discount_amount", "REAL")
    _ensure_column(db, "shop_orders", "coins_used", "INTEGER")
    _ensure_column(db, "shop_orders", "discount_amount", "REAL")

    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS coin_wallets (
            phone TEXT PRIMARY KEY,
            balance INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS coin_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT NOT NULL,
            delta INTEGER NOT NULL,
            reason TEXT NOT NULL,
            reference TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS testimonials (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            groom_name TEXT NOT NULL,
            bride_name TEXT NOT NULL,
            event_date TEXT,
            photo_filename TEXT,
            rating INTEGER DEFAULT 5,
            short_quote TEXT,
            full_story TEXT,
            is_featured INTEGER DEFAULT 0,
            is_approved INTEGER DEFAULT 1,
            sort_order INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            target_group TEXT DEFAULT 'all',
            action_url TEXT,
            is_active INTEGER DEFAULT 1,
            created_at TEXT NOT NULL,
            expires_at TEXT
        );
        """
    )

    # Backfill: profiles created before this field existed. We can't know
    # retroactively which ones came through self-registration, so we default
    # them to "admin" (the original, only way profiles were created) — this
    # is documented as a known limitation for historical data.
    db.execute("UPDATE profiles SET registration_source = 'admin' WHERE registration_source IS NULL")
    db_commit_retry(db)

    defaults = {
        "brand_name": os.environ.get("BUSINESS_NAME", "Saif Matrimonial Services"),
        "brand_tagline": os.environ.get("BUSINESS_TAGLINE", "Trusted Connections, Blessed Beginnings"),
        "brand_phone": os.environ.get("BUSINESS_PHONE", "7762023966"),
        "whatsapp_number": os.environ.get("WHATSAPP_NUMBER", os.environ.get("BUSINESS_PHONE", "7762023966")),
        "brand_email": os.environ.get("BUSINESS_EMAIL", ""),
        "brand_location": os.environ.get("BUSINESS_LOCATION", "Kolkata, India"),
        "unlock_price": os.environ.get("UNLOCK_PRICE", "49"),
        "package_price": os.environ.get("PACKAGE_PRICE", "149"),
        "package_size": os.environ.get("PACKAGE_SIZE", "4"),
        "package_offer_enabled": os.environ.get("PACKAGE_OFFER_ENABLED", "1"),
        "registration_price": os.environ.get("REGISTRATION_PRICE", "11"),
        "upi_id": os.environ.get("UPI_ID", "yourupi@bank"),
        "primary_color": os.environ.get("PRIMARY_COLOR", "#0b5a44"),
        "secondary_color": os.environ.get("SECONDARY_COLOR", "#073e2f"),
        "accent_color": os.environ.get("ACCENT_COLOR", "#c9a86a"),
        "theme_preset": "emerald_champagne",
        "background_color": "#faf8f3",
        "card_color": "#ffffff",
        "text_color": "#262622",
        "muted_color": "#6b7268",
        "border_color": "#e6e1d6",
        "font_heading": "Playfair Display",
        "font_body": "Inter",
        "font_button": "Inter",
        "coin_value_inr": "2.2",
        "registration_bonus_coins": "5",
        "hero_heading": "Find Your Life Partner With Trust, Haya &amp; Purpose",
        "hero_subheading": "A serious, privacy-first matrimonial service for meaningful Nikah connections.",
        "footer_text": "",
        "logo_image": "",
        "favicon_image": "",
        "qr_image": "",
        "nav_bg_image": "",
        "hero_bg_image": "",
        "section_bg_image": "",
        "footer_bg_image": "",
        # Help Shadi's public-facing name is admin-editable — the route
        # paths (/help-shadi, /admin/help-shadi) never change, only what's
        # displayed in nav/footer/headings, so nothing breaks if it's renamed.
        "help_shadi_display_name": "Help Shadi",
        "instagram_url": "",
        "facebook_url": "",
        "youtube_url": "",
        "telegram_url": "",
    }
    for k, v in defaults.items():
        db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))

    db_commit_retry(db)
    db.close()


def next_profile_code(db):
    row = db.execute("SELECT next_val FROM profile_counter WHERE id = 1").fetchone()
    next_val = row["next_val"]
    db.execute("UPDATE profile_counter SET next_val = ? WHERE id = 1", (next_val + 1,))
    return f"SMS{next_val}"


def new_request_code():
    alphabet = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(8))


# ======================================================================
# SETTINGS (branding) — DB-backed, admin-editable. Env vars are only the
# first-run defaults (seeded once in init_db). No template hard-codes
# brand name / colors / contact info any more.
# ======================================================================
def load_settings():
    db = get_db()
    rows = db.execute("SELECT key, value FROM settings").fetchall()
    return {r["key"]: r["value"] for r in rows}


@app.before_request
def load_settings_into_g():
    g.settings = load_settings()


def get_setting(key, default=""):
    return (getattr(g, "settings", {}) or {}).get(key) or default


@app.context_processor
def inject_globals():
    s = getattr(g, "settings", {}) or {}
    return dict(
        business_name=s.get("brand_name", "Matrimonial Services"),
        business_tagline=s.get("brand_tagline", ""),
        business_phone=s.get("brand_phone", ""),
        whatsapp_number=s.get("whatsapp_number", s.get("brand_phone", "")),
        business_email=s.get("brand_email", ""),
        business_location=s.get("brand_location", ""),
        unlock_price=s.get("unlock_price", "49"),
        package_price=s.get("package_price", "149"),
        package_size=s.get("package_size", "4"),
        package_offer_enabled=s.get("package_offer_enabled", "1") == "1",
        registration_price=s.get("registration_price", "11"),
        upi_id=s.get("upi_id", "yourupi@bank"),
        primary_color=s.get("primary_color", "#0b5a44"),
        secondary_color=s.get("secondary_color", "#073e2f"),
        accent_color=s.get("accent_color", "#c9a86a"),
        background_color=s.get("background_color", "#faf8f3"),
        card_color=s.get("card_color", "#ffffff"),
        text_color=s.get("text_color", "#262622"),
        muted_color=s.get("muted_color", "#6b7268"),
        border_color=s.get("border_color", "#e6e1d6"),
        font_heading=s.get("font_heading", "Playfair Display"),
        font_body=s.get("font_body", "Inter"),
        font_button=s.get("font_button", "Inter"),
        custom_fonts=list_custom_fonts(),
        cart_count=cart_item_count(),
        wallet_balance=get_wallet_balance(verified_phone()),
        coin_value_inr=coin_value_inr(),
        hero_heading=s.get("hero_heading", ""),
        hero_subheading=s.get("hero_subheading", ""),
        footer_text=s.get("footer_text", ""),
        logo_image=s.get("logo_image", ""),
        qr_image=s.get("qr_image", ""),
        nav_bg_image=s.get("nav_bg_image", ""),
        hero_bg_image=s.get("hero_bg_image", ""),
        section_bg_image=s.get("section_bg_image", ""),
        footer_bg_image=s.get("footer_bg_image", ""),
        help_shadi_name=s.get("help_shadi_display_name", "Help Shadi"),
        instagram_url=s.get("instagram_url", ""),
        facebook_url=s.get("facebook_url", ""),
        youtube_url=s.get("youtube_url", ""),
        telegram_url=s.get("telegram_url", ""),
        top_banners=active_banners("top"),
        current_year=datetime.now().year,
    )


# ======================================================================
# APPEARANCE / THEME ENGINE
# ======================================================================
THEME_PRESETS = {
    "emerald_champagne": {
        "label": "Emerald & Champagne",
        "primary_color": "#0b5a44", "secondary_color": "#073e2f", "accent_color": "#c9a86a",
        "background_color": "#faf8f3", "card_color": "#ffffff", "text_color": "#262622",
        "muted_color": "#6b7268", "border_color": "#e6e1d6",
    },
    "midnight_gold": {
        "label": "Midnight & Gold",
        "primary_color": "#0d1321", "secondary_color": "#05070d", "accent_color": "#d4af37",
        "background_color": "#0f1420", "card_color": "#161d2e", "text_color": "#f2efe6",
        "muted_color": "#9aa2b5", "border_color": "#2a3348",
    },
    "burgundy_champagne": {
        "label": "Burgundy & Champagne",
        "primary_color": "#5c1a2b", "secondary_color": "#3c0f1c", "accent_color": "#d3b06a",
        "background_color": "#fbf6ef", "card_color": "#ffffff", "text_color": "#2a1f22",
        "muted_color": "#7a6b6c", "border_color": "#e9ddce",
    },
    "royal_plum": {
        "label": "Royal Plum",
        "primary_color": "#4a2545", "secondary_color": "#301930", "accent_color": "#b79a5e",
        "background_color": "#faf7f6", "card_color": "#ffffff", "text_color": "#2b2230",
        "muted_color": "#736a77", "border_color": "#e5dde3",
    },
    "olive_sand": {
        "label": "Olive & Sand",
        "primary_color": "#556b2f", "secondary_color": "#3a4a1f", "accent_color": "#a97142",
        "background_color": "#faf6ee", "card_color": "#ffffff", "text_color": "#2c2a22",
        "muted_color": "#736f5f", "border_color": "#e6ded0",
    },
}

THEME_COLOR_KEYS = [
    "primary_color", "secondary_color", "accent_color", "background_color",
    "card_color", "text_color", "muted_color", "border_color",
]

WEB_SAFE_FONTS = ["Inter", "Playfair Display", "Poppins", "Lora", "Cormorant Garamond", "Georgia", "Montserrat"]
FONT_DIR = os.path.join(BASE_DIR, "static", "fonts")
os.makedirs(FONT_DIR, exist_ok=True)
ALLOWED_FONT_EXT = {"woff2", "woff", "ttf"}
MAX_FONT_BYTES = 5 * 1024 * 1024


def _hex_to_rgb(hex_color):
    h = (hex_color or "").strip().lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) != 6:
        return None
    try:
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return None


def _relative_luminance(rgb):
    def chan(c):
        c = c / 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (chan(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(hex_a, hex_b):
    """WCAG contrast ratio between two hex colors. Returns None if either is invalid."""
    a, b = _hex_to_rgb(hex_a), _hex_to_rgb(hex_b)
    if not a or not b:
        return None
    la, lb = _relative_luminance(a), _relative_luminance(b)
    lighter, darker = max(la, lb), min(la, lb)
    return round((lighter + 0.05) / (darker + 0.05), 2)


def is_valid_hex(value):
    return bool(re.fullmatch(r"#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})", (value or "").strip()))


def list_custom_fonts():
    db = get_db()
    return db.execute("SELECT * FROM custom_fonts ORDER BY label").fetchall()


def slugify(text):
    text = (text or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text or secrets.token_hex(4)


def unique_slug(db, table, base_slug, exclude_id=None):
    slug = base_slug
    i = 2
    while True:
        q = f"SELECT id FROM {table} WHERE slug = ?"
        params = [slug]
        if exclude_id:
            q += " AND id != ?"
            params.append(exclude_id)
        if not db.execute(q, params).fetchone():
            return slug
        slug = f"{base_slug}-{i}"
        i += 1


# ======================================================================
# SHOP CART (session-based, guest checkout — no user accounts on this site)
# ======================================================================
def get_cart():
    return session.get("cart", [])  # list of {product_id, variant_id, qty}


def save_cart(cart):
    session["cart"] = cart
    session.modified = True


def cart_item_count():
    return sum(item.get("qty", 0) for item in get_cart())


def cart_details(db):
    """Resolve cart against live DB rows so price/stock is always current
    (never trust anything cached in the session for money)."""
    cart = get_cart()
    items = []
    total = 0.0
    changed = False
    kept = []
    for entry in cart:
        product = db.execute(
            "SELECT * FROM shop_products WHERE id = ? AND status = 'active'",
            (entry.get("product_id"),),
        ).fetchone()
        if not product:
            changed = True
            continue
        variant = None
        if entry.get("variant_id"):
            variant = db.execute(
                "SELECT * FROM shop_variants WHERE id = ? AND product_id = ?",
                (entry["variant_id"], product["id"]),
            ).fetchone()
            if not variant:
                changed = True
                continue
        available_stock = variant["stock"] if variant else product["stock"]
        qty = max(1, min(int(entry.get("qty", 1)), max(available_stock, 0) or 1))
        if qty != entry.get("qty"):
            changed = True
        if available_stock <= 0:
            changed = True
            continue
        unit_price = product["discount_price"] or product["price"]
        line_total = unit_price * qty
        total += line_total
        items.append({
            "product": product,
            "variant": variant,
            "qty": qty,
            "unit_price": unit_price,
            "line_total": line_total,
        })
        kept.append({"product_id": product["id"], "variant_id": variant["id"] if variant else None, "qty": qty})
    if changed:
        save_cart(kept)
    return items, round(total, 2)


# ======================================================================
# MATCH COINS — WALLET ENGINE
# Coins are a wallet-style discount credit, always tied to an
# OTP-verified phone number (never a phone typed into a form — that is
# not proof of ownership, same principle already used for unlock_requests).
# ======================================================================
def coin_value_inr():
    try:
        return float(get_setting("coin_value_inr", "2.2"))
    except (TypeError, ValueError):
        return 2.2


def registration_bonus_coins():
    try:
        return int(get_setting("registration_bonus_coins", "5"))
    except (TypeError, ValueError):
        return 5


def get_wallet_balance(phone):
    if not phone:
        return 0
    db = get_db()
    row = db.execute("SELECT balance FROM coin_wallets WHERE phone = ?", (phone,)).fetchone()
    return row["balance"] if row else 0


def credit_coins(phone, amount, reason, reference=None):
    if not phone or amount <= 0:
        return
    db = get_db()
    now = datetime.now().isoformat()
    db.execute(
        "INSERT INTO coin_wallets (phone, balance, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(phone) DO UPDATE SET balance = balance + excluded.balance, updated_at = excluded.updated_at",
        (phone, amount, now),
    )
    db.execute("INSERT INTO coin_ledger (phone, delta, reason, reference, created_at) VALUES (?, ?, ?, ?, ?)",
               (phone, amount, reason, reference, now))
    db_commit_retry(db)


def debit_coins(phone, amount, reason, reference=None):
    """Returns True and commits the debit only if the wallet actually has
    enough balance — never lets a balance go negative."""
    if not phone or amount <= 0:
        return True
    db = get_db()
    balance = get_wallet_balance(phone)
    if balance < amount:
        return False
    now = datetime.now().isoformat()
    db.execute("UPDATE coin_wallets SET balance = balance - ?, updated_at = ? WHERE phone = ?", (amount, now, phone))
    db.execute("INSERT INTO coin_ledger (phone, delta, reason, reference, created_at) VALUES (?, ?, ?, ?, ?)",
               (phone, -amount, reason, reference, now))
    db_commit_retry(db)
    return True


def refund_coins(phone, amount, reason, reference=None):
    """Used when an order/request that redeemed coins is later rejected/cancelled."""
    if amount:
        credit_coins(phone, amount, reason, reference)


# ======================================================================
# CSRF PROTECTION (lightweight, no external dependency)
# ======================================================================



def get_csrf_token():
    if "_csrf_token" not in session:
        session["_csrf_token"] = secrets.token_hex(24)
    return session["_csrf_token"]


app.jinja_env.globals["csrf_token"] = get_csrf_token


@app.before_request
def enforce_csrf():
    if request.method == "POST":
        token = session.get("_csrf_token")
        submitted = request.form.get("csrf_token")
        if not token or not submitted or not secrets.compare_digest(token, submitted):
            abort(400)


# ======================================================================
# SECURITY HEADERS
# ======================================================================
@app.after_request
def set_security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "same-origin"
    resp.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; "
        "style-src 'self' 'unsafe-inline'; script-src 'self'"
    )
    path = request.path or ""
    if (path.startswith(("/admin", "/my-requests", "/verify-access"))
            or "/photo" in path or path.endswith("/full") or path.endswith("/unlock")):
        resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    return resp


# ======================================================================
# RATE LIMITING (simple in-memory; fine for a single small worker)
# ======================================================================
def _client_ip():
    return request.headers.get("X-Forwarded-For", request.remote_addr or "unknown").split(",")[0].strip()


class RateLimiter:
    """Generic sliding-window counter with an optional lockout, keyed by
    any string (IP, phone, etc). Used for admin login and OTP requests."""

    def __init__(self, max_attempts, window_seconds, lockout_seconds):
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self.lockout_seconds = lockout_seconds
        self._store = {}

    def is_locked(self, key):
        entry = self._store.get(key)
        if not entry:
            return False
        count, first_seen, locked_until = entry
        return bool(locked_until and time.time() < locked_until)

    def register_attempt(self, key):
        count, first_seen, locked_until = self._store.get(key, (0, time.time(), None))
        now = time.time()
        if now - first_seen > self.window_seconds:
            count, first_seen = 0, now
        count += 1
        locked_until = now + self.lockout_seconds if count >= self.max_attempts else None
        self._store[key] = (count, first_seen, locked_until)

    def clear(self, key):
        self._store.pop(key, None)


login_limiter = RateLimiter(max_attempts=6, window_seconds=600, lockout_seconds=900)
otp_request_limiter = RateLimiter(max_attempts=5, window_seconds=600, lockout_seconds=600)
otp_verify_limiter = RateLimiter(max_attempts=6, window_seconds=600, lockout_seconds=600)
# Registration and unlock/payment submissions weren't behind any limiter —
# both accept a phone number + free-text fields with no OTP gate, so
# without this a script could spam either form indefinitely.
registration_limiter = RateLimiter(max_attempts=5, window_seconds=600, lockout_seconds=900)
unlock_submit_limiter = RateLimiter(max_attempts=10, window_seconds=600, lockout_seconds=600)
# Help Shadi submissions — same shape of form as registration (name/contact/
# free text + optional file), same abuse risk, same limiter pattern.
help_shadi_limiter = RateLimiter(max_attempts=5, window_seconds=600, lockout_seconds=900)


# ======================================================================
# IMAGE HANDLING
# ======================================================================
class ImageValidationError(Exception):
    pass


def _load_validated_image(file_storage):
    if not file_storage or not file_storage.filename:
        return None

    ext = file_storage.filename.rsplit(".", 1)[-1].lower() if "." in file_storage.filename else ""
    if ext not in ALLOWED_IMAGE_EXT:
        raise ImageValidationError("Unsupported file type. Use JPG, PNG or WEBP.")

    file_storage.stream.seek(0, os.SEEK_END)
    size = file_storage.stream.tell()
    file_storage.stream.seek(0)
    if size > MAX_IMAGE_BYTES:
        raise ImageValidationError("Image is too large (max 6 MB).")
    if size == 0:
        raise ImageValidationError("Empty file.")

    try:
        img = Image.open(file_storage.stream)
        img.verify()
    except Exception:
        raise ImageValidationError("This doesn't look like a valid image file.")

    file_storage.stream.seek(0)
    img = Image.open(file_storage.stream)
    img = ImageOps.exif_transpose(img)
    img = img.convert("RGB")
    return img


def save_profile_photo(file_storage):
    img = _load_validated_image(file_storage)
    if img is None:
        return None, None

    original_name = secrets.token_hex(16) + ".jpg"
    img.save(os.path.join(PRIVATE_ORIGINALS_DIR, original_name), "JPEG", quality=88)

    preview = img.copy()
    preview.thumbnail((320, 320))
    preview = preview.filter(ImageFilter.GaussianBlur(radius=14))
    preview_name = secrets.token_hex(16) + ".jpg"
    preview.save(os.path.join(PREVIEW_DIR, preview_name), "JPEG", quality=55)

    return original_name, preview_name


def save_payment_proof(file_storage):
    img = _load_validated_image(file_storage)
    if img is None:
        return None
    proof_name = secrets.token_hex(16) + ".jpg"
    img.save(os.path.join(PRIVATE_PROOFS_DIR, proof_name), "JPEG", quality=85)
    return proof_name


def save_help_shadi_document(file_storage):
    """Same validated-image-into-private-dir pattern as save_payment_proof —
    kept as its own function (rather than reusing save_payment_proof
    directly) so the storage location and any future validation rules for
    Help Shadi documents can diverge without touching payment-proof code."""
    img = _load_validated_image(file_storage)
    if img is None:
        return None
    doc_name = secrets.token_hex(16) + ".jpg"
    img.save(os.path.join(PRIVATE_HELP_SHADI_DOCS_DIR, doc_name), "JPEG", quality=85)
    return doc_name


def save_generic_image(file_storage, dest_dir, max_dim=1600):
    img = _load_validated_image(file_storage)
    if img is None:
        return None
    img.thumbnail((max_dim, max_dim))
    name = secrets.token_hex(12) + ".jpg"
    img.save(os.path.join(dest_dir, name), "JPEG", quality=90)
    return name


def delete_file_quietly(directory, filename):
    if not filename:
        return
    try:
        os.remove(os.path.join(directory, filename))
    except OSError:
        pass


def _watermark_font(size):
    for path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ):
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                pass
    return ImageFont.load_default()


def get_watermarked_photo(profile, viewer_phone):
    """Serve a per-viewer watermarked copy of the protected original,
    cached to disk. Tasteful, low-opacity, tiled — hard to crop out
    without damaging the photo, and it encodes the profile code plus a
    hash of the viewer's phone so a leaked copy is traceable."""
    original_name = profile["photo_original_name"]
    if not original_name:
        return None

    viewer_tag = hashlib.sha256((viewer_phone or "guest").encode()).hexdigest()[:8]
    cache_key = hashlib.sha256(f"{original_name}:{viewer_tag}".encode()).hexdigest() + ".jpg"
    cache_path = os.path.join(PRIVATE_WATERMARK_CACHE_DIR, cache_key)
    if os.path.exists(cache_path):
        return cache_path

    original_path = os.path.join(PRIVATE_ORIGINALS_DIR, original_name)
    if not os.path.exists(original_path):
        return None

    base = Image.open(original_path).convert("RGBA")
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    label = f"{get_setting('brand_name', 'Matrimonial')} | {profile['profile_code']} | {viewer_tag}"
    font_size = max(14, base.width // 28)
    font = _watermark_font(font_size)
    try:
        text_w = draw.textlength(label, font=font)
    except Exception:
        text_w = font_size * len(label) * 0.5
    step_x = int(text_w) + 60
    step_y = font_size * 5
    row = 0
    for oy in range(-step_y, base.height + step_y, step_y):
        shift = (step_x // 2) if row % 2 else 0
        for ox in range(-step_x, base.width + step_x, step_x):
            draw.text((ox + shift, oy), label, font=font, fill=(255, 255, 255, 60))
        row += 1
    watermarked = Image.alpha_composite(base, overlay).convert("RGB")
    watermarked.save(cache_path, "JPEG", quality=87)
    return cache_path


# ======================================================================
# ACCESS CONTROL HELPERS
# ======================================================================
def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("is_admin"):
            return redirect(url_for("admin_login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def log_admin_action(action, detail=""):
    db = get_db()
    db.execute(
        "INSERT INTO admin_activity_log (admin_username, action, detail, ip, created_at) VALUES (?, ?, ?, ?, ?)",
        (session.get("admin_username", ADMIN_USERNAME), action, detail, _client_ip(), datetime.now().isoformat()),
    )
    db_commit_retry(db)


def verified_phone():
    return session.get("verified_phone")


def user_has_unlocked(db, profile_id, phone):
    if not phone:
        return None
    return db.execute(
        "SELECT * FROM unlock_requests WHERE profile_id = ? AND user_phone = ? AND status = 'unlocked'",
        (profile_id, phone),
    ).fetchone()


# ======================================================================
# COMPATIBILITY / MATCH SCORE
#
# Deterministic, weighted, documented — NOT random. Compares two profile
# rows and returns an integer 0-100. Weights are named constants so they
# can be tuned later without touching the scoring logic itself.
#
# Architecture note: this site doesn't have full "user accounts" with
# saved partner-preferences — viewers are just a verified phone number.
# So the score is computed PROFILE vs PROFILE (the profile being viewed
# vs. another profile), which is what actually exists in the data. If a
# viewer's verified phone matches a profile they themselves registered
# (see get_viewer_profile below), we personalise the score against THAT
# profile instead — the closest honest equivalent of "viewer preferences"
# this architecture supports.
# ======================================================================
SCORE_WEIGHTS = {
    "age_gap": 20,          # closer age (within ~5 years) scores higher
    "gender_complement": 15,  # opposite gender is the baseline expectation on a matrimonial site
    "location": 15,          # same city > same state > different
    "education": 15,         # same/similar education level
    "profession": 10,        # same/related profession field
    "hobbies": 15,           # shared hobbies (scales with overlap count)
    "lifestyle": 10,          # similar answers on drinking/smoking/namaz/roza etc.
}


def _age_score(a, b):
    try:
        gap = abs(int(a) - int(b))
    except (TypeError, ValueError):
        return None
    if gap <= 2: return 1.0
    if gap <= 5: return 0.75
    if gap <= 8: return 0.4
    return 0.1


def _text_match_score(a, b):
    a = (a or "").strip().lower()
    b = (b or "").strip().lower()
    if not a or not b:
        return None
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.6
    return 0.15


def _hobbies_overlap_score(a, b):
    ha = {h.strip().lower() for h in (a or "").split(",") if h.strip()}
    hb = {h.strip().lower() for h in (b or "").split(",") if h.strip()}
    if not ha or not hb:
        return None
    overlap = len(ha & hb)
    union = len(ha | hb) or 1
    return overlap / union


def _lifestyle_score(p1, p2):
    fields = ["lifestyle_drinking", "lifestyle_smoking", "lifestyle_tobacco", "lifestyle_namaz", "lifestyle_roza"]
    scored = []
    for f in fields:
        v1, v2 = (p1[f] or "").strip(), (p2[f] or "").strip()
        if not v1 or not v2:
            continue
        scored.append(1.0 if v1 == v2 else 0.35)
    if not scored:
        return None
    return sum(scored) / len(scored)


def compute_compatibility_score(p1, p2):
    """Returns an int 0-100. Missing fields are excluded from the
    calculation entirely (weights re-normalised) rather than penalised —
    a profile shouldn't score low just because a field is empty."""
    components = {
        "age_gap": _age_score(p1["age"], p2["age"]),
        "gender_complement": (1.0 if (p1["gender"] or "") != (p2["gender"] or "") and p1["gender"] and p2["gender"] else (0.2 if p1["gender"] and p2["gender"] else None)),
        "location": _text_match_score(p1["city"], p2["city"]),
        "education": _text_match_score(p1["education"], p2["education"]),
        "profession": _text_match_score(p1["profession"], p2["profession"]),
        "hobbies": _hobbies_overlap_score(p1["hobbies"], p2["hobbies"]),
        "lifestyle": _lifestyle_score(p1, p2),
    }
    total_weight = 0
    weighted_sum = 0.0
    for key, val in components.items():
        if val is None:
            continue
        w = SCORE_WEIGHTS[key]
        total_weight += w
        weighted_sum += w * val
    if total_weight == 0:
        return None
    return round((weighted_sum / total_weight) * 100)


def get_viewer_profile(db, phone):
    """Best-effort: does this verified phone belong to a profile already
    in our system (self-registered or admin-added with this contact)?
    Used only to personalise the match score — never assumed to exist."""
    if not phone:
        return None
    return db.execute(
        "SELECT * FROM profiles WHERE contact_number = ? AND is_active = 1 LIMIT 1", (phone,)
    ).fetchone()


def get_recommended_profiles(db, profile, limit=4):
    """'You might also like' — other active profiles ranked by how many
    hobbies they share with this one, then by same-city, then newest.
    Scored in Python (SQLite has no easy set-intersection function) since
    the active profile list on a matrimonial site like this is small."""
    my_hobbies = {h.strip().lower() for h in (profile["hobbies"] or "").split(",") if h.strip()}
    candidates = db.execute(
        "SELECT * FROM profiles WHERE is_active = 1 AND id != ? ORDER BY created_at DESC LIMIT 200",
        (profile["id"],),
    ).fetchall()

    scored = []
    for c in candidates:
        c_hobbies = {h.strip().lower() for h in (c["hobbies"] or "").split(",") if h.strip()}
        shared = len(my_hobbies & c_hobbies)
        same_city = 1 if (c["city"] or "").strip().lower() == (profile["city"] or "").strip().lower() else 0
        # Weight shared hobbies highest, then same city — both are what the
        # site owner asked for ("matching hobbies + similar location").
        score = shared * 10 + same_city * 3
        if shared or same_city:
            scored.append((score, c))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [c for _, c in scored[:limit]]


PHONE_RE = re.compile(r"^[6-9]\d{9}$")


def valid_indian_phone(phone):
    return bool(PHONE_RE.match((phone or "").strip()))


# ======================================================================
# BANNERS
# ======================================================================
def active_banners(slot):
    db = get_db()
    today = date.today().isoformat()
    return db.execute(
        """
        SELECT * FROM banners
        WHERE slot = ? AND is_active = 1
          AND (start_date IS NULL OR start_date = '' OR start_date <= ?)
          AND (end_date IS NULL OR end_date = '' OR end_date >= ?)
        ORDER BY sort_order ASC, id DESC
        """,
        (slot, today, today),
    ).fetchall()


# ======================================================================
# PUBLIC ROUTES
# ======================================================================
PAGE_SIZE = 12


@app.route("/")
def index():
    db = get_db()

    filters = {
        "gender": request.args.get("gender", "").strip(),
        "min_age": request.args.get("min_age", "").strip(),
        "max_age": request.args.get("max_age", "").strip(),
        "city": request.args.get("city", "").strip(),
        "marital_status": request.args.get("marital_status", "").strip(),
        "education": request.args.get("education", "").strip(),
        "profession": request.args.get("profession", "").strip(),
        "community": request.args.get("community", "").strip(),
    }
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1

    where = ["is_active = 1"]
    params = []
    if filters["gender"] in ("Male", "Female"):
        where.append("gender = ?")
        params.append(filters["gender"])
    if filters["min_age"].isdigit():
        where.append("age >= ?")
        params.append(int(filters["min_age"]))
    if filters["max_age"].isdigit():
        where.append("age <= ?")
        params.append(int(filters["max_age"]))
    if filters["city"]:
        where.append("city LIKE ?")
        params.append(f"%{filters['city']}%")
    if filters["marital_status"]:
        where.append("marital_status = ?")
        params.append(filters["marital_status"])
    if filters["education"]:
        where.append("education LIKE ?")
        params.append(f"%{filters['education']}%")
    if filters["profession"]:
        where.append("profession LIKE ?")
        params.append(f"%{filters['profession']}%")
    if filters["community"]:
        where.append("community = ?")
        params.append(filters["community"])

    where_sql = " AND ".join(where)
    total = db.execute(f"SELECT COUNT(*) c FROM profiles WHERE {where_sql}", params).fetchone()["c"]
    total_pages = max(1, math.ceil(total / PAGE_SIZE))
    page = min(page, total_pages)
    offset = (page - 1) * PAGE_SIZE

    profiles = db.execute(
        f"SELECT * FROM profiles WHERE {where_sql} ORDER BY created_at DESC LIMIT ? OFFSET ?",
        params + [PAGE_SIZE, offset],
    ).fetchall()

    def distinct(col):
        return [r[0] for r in db.execute(
            f"SELECT DISTINCT {col} FROM profiles WHERE is_active=1 AND {col} IS NOT NULL AND {col} != '' ORDER BY {col}"
        ).fetchall()]

    filter_options = {
        "cities": distinct("city"),
        "marital_statuses": distinct("marital_status"),
        "communities": distinct("community"),
    }

    any_filter_active = any(filters.values())
    active_filters = {k: v for k, v in filters.items() if v}

    return render_template(
        "index.html", profiles=profiles, filters=filters, filter_options=filter_options,
        active_filters=active_filters,
        page=page, total_pages=total_pages, total=total, any_filter_active=any_filter_active,
        hero_banners=active_banners("hero"), mid_banners=active_banners("mid"),
        footer_banners=active_banners("footer"),
        testimonials=db.execute(
            "SELECT * FROM testimonials WHERE is_approved = 1 ORDER BY is_featured DESC, sort_order, created_at DESC LIMIT 12"
        ).fetchall(),
    )


@app.route("/how-it-works")
def how_it_works():
    return render_template("how_it_works.html")


@app.route("/faq")
def faq():
    return render_template("faq.html")


@app.route("/terms")
def terms():
    return render_template("terms.html")


@app.route("/privacy")
def privacy():
    return render_template("privacy.html")


@app.route("/profile/<profile_code>")
def profile_preview(profile_code):
    db = get_db()
    profile = db.execute(
        "SELECT * FROM profiles WHERE profile_code = ? AND is_active = 1", (profile_code,)
    ).fetchone()
    if not profile:
        abort(404)
    already_unlocked = user_has_unlocked(db, profile["id"], verified_phone())
    selection = _package_selection()
    recommended = get_recommended_profiles(db, profile)
    recommended_scored = [(rp, compute_compatibility_score(profile, rp)) for rp in recommended]
    viewer_profile = get_viewer_profile(db, verified_phone())
    match_score = compute_compatibility_score(viewer_profile, profile) if viewer_profile else None
    return render_template(
        "profile_preview.html", profile=profile, already_unlocked=already_unlocked,
        in_package=(profile_code in selection), package_selection=selection, package_size=_package_size(),
        recommended=recommended_scored, match_score=match_score,
    )


@app.route("/profile/<profile_code>/preview-image")
def profile_preview_image(profile_code):
    db = get_db()
    profile = db.execute("SELECT * FROM profiles WHERE profile_code = ?", (profile_code,)).fetchone()
    if not profile or not profile["photo_preview_name"]:
        abort(404)
    resp = send_from_directory(PREVIEW_DIR, profile["photo_preview_name"])
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp


@app.route("/profile/<profile_code>/photo")
def profile_original_photo(profile_code):
    db = get_db()
    profile = db.execute("SELECT * FROM profiles WHERE profile_code = ?", (profile_code,)).fetchone()
    if not profile:
        abort(404)

    phone = verified_phone()
    unlocked = user_has_unlocked(db, profile["id"], phone)
    if not unlocked:
        abort(403)

    path = get_watermarked_photo(profile, phone)
    if not path:
        abort(404)
    resp = send_file(path, mimetype="image/jpeg")
    resp.headers["Cache-Control"] = "no-store, private"
    resp.headers["X-Robots-Tag"] = "noindex"
    return resp


@app.route("/profile/<profile_code>/unlock", methods=["GET", "POST"])
def unlock(profile_code):
    db = get_db()
    profile = db.execute(
        "SELECT * FROM profiles WHERE profile_code = ? AND is_active = 1", (profile_code,)
    ).fetchone()
    if not profile:
        abort(404)

    if request.method == "POST":
        ip = _client_ip()
        if unlock_submit_limiter.is_locked(ip):
            flash("Too many submissions from this device. Please try again in a few minutes.", "error")
            return render_template("unlock.html", profile=profile)
        unlock_submit_limiter.register_attempt(ip)

        user_name = request.form.get("user_name", "").strip()[:120]
        user_phone = request.form.get("user_phone", "").strip()
        message = request.form.get("message", "").strip()[:500]
        consent = request.form.get("consent")
        viewer_declaration = request.form.get("viewer_declaration")

        errors = []
        if not valid_indian_phone(user_phone):
            errors.append("Please enter a valid 10-digit Indian mobile number.")
        if not consent:
            errors.append("Please confirm you have completed the payment.")
        if not viewer_declaration:
            errors.append("Please confirm the declaration about genuine use and not misusing/screenshotting the profile data.")

        # Match Coins redemption — only ever against the SESSION's OTP-verified
        # phone number, never the phone typed into this form (that's not proof
        # of ownership; see the note below about verified_phone()).
        unlock_price = float(get_setting("unlock_price", "49"))
        coins_requested = request.form.get("use_coins", type=int) or 0
        coins_used = 0
        discount_amount = 0.0
        wallet_phone = verified_phone()
        if coins_requested > 0:
            if not wallet_phone or wallet_phone != user_phone:
                errors.append("Verify this phone number (via My Requests) to use its Match Coins.")
            else:
                max_coins_by_value = int(unlock_price / coin_value_inr())
                coins_used = max(0, min(coins_requested, get_wallet_balance(wallet_phone), max_coins_by_value))
                discount_amount = round(coins_used * coin_value_inr(), 2)

        proof_file = request.files.get("payment_proof")
        proof_name = None
        if not proof_file or not proof_file.filename:
            errors.append("Please upload your payment screenshot.")
        else:
            try:
                proof_name = save_payment_proof(proof_file)
            except ImageValidationError as e:
                errors.append(str(e))

        if not errors:
            existing = db.execute(
                "SELECT * FROM unlock_requests WHERE profile_id = ? AND user_phone = ? AND status = 'pending'",
                (profile["id"], user_phone),
            ).fetchone()
            if existing:
                errors.append("You already have a pending request for this profile with this number.")

        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("unlock.html", profile=profile)

        if coins_used > 0:
            if not debit_coins(wallet_phone, coins_used, "unlock_redeem", profile["profile_code"]):
                flash("Your coin balance changed — please retry.", "error")
                return render_template("unlock.html", profile=profile)

        request_code = new_request_code()
        db.execute(
            """
            INSERT INTO unlock_requests
            (request_code, profile_id, user_name, user_phone, payment_proof_name, message,
             viewer_declaration, coins_used, discount_amount, status, requested_at)
            VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, 'pending', ?)
            """,
            (request_code, profile["id"], user_name, user_phone, proof_name, message,
             coins_used, discount_amount, datetime.now().isoformat()),
        )
        db_commit_retry(db)

        notify_admin(
            "New payment request",
            f"{user_name or 'A customer'} ({user_phone}) submitted payment proof for "
            f"profile {profile['profile_code']} ({profile['name']}). Request code: {request_code}. "
            f"{'Coins used: ' + str(coins_used) + f' (₹{discount_amount} off). ' if coins_used else ''}"
            f"Review: {request.url_root.rstrip('/')}{url_for('admin_requests')}",
        )
        create_admin_event(
            "payment",
            f"New payment request — {request_code}",
            f"{user_name or 'A customer'} ({user_phone}) for profile {profile['profile_code']} ({profile['name']}).",
            request_code,
            url_for("admin_requests"),
        )

        # NOTE: unlike v2, we deliberately do NOT auto-trust this session
        # with `verified_phone` here. A phone number typed into a form is
        # not proof of ownership. Real access to "My Requests" now
        # requires OTP verification of that number.
        return render_template("request_submitted.html", profile=profile, request_code=request_code)

    return render_template("unlock.html", profile=profile)


# ======================================================================
# PACKAGE UNLOCK — "3 profiles for ₹99" style bundle.
# The user picks up to `package_size` profiles (kept in the session, not
# the DB, until checkout — nothing is committed until they actually pay),
# then pays once and uploads one proof for the whole bundle. Internally
# this creates one unlock_request row per profile, all sharing the same
# package_code, so the admin can approve/reject the whole bundle in one
# click while each profile's unlock status is still tracked individually
# (exactly like the single-profile flow it reuses).
# ======================================================================
def _package_size():
    try:
        return max(2, int(get_setting("package_size", "3")))
    except ValueError:
        return 3


def _package_selection():
    return session.get("package_selection", [])


@app.route("/profile/<profile_code>/package-toggle", methods=["POST"])
def package_toggle(profile_code):
    db = get_db()
    profile = db.execute(
        "SELECT * FROM profiles WHERE profile_code = ? AND is_active = 1", (profile_code,)
    ).fetchone()
    if not profile:
        abort(404)

    selection = _package_selection()
    size = _package_size()
    if profile_code in selection:
        selection.remove(profile_code)
    elif len(selection) < size:
        selection.append(profile_code)
    else:
        flash(f"You can only select {size} profiles for the package. Remove one first.", "error")
    session["package_selection"] = selection
    session.modified = True

    next_url = request.form.get("next") or url_for("index")
    return redirect(next_url)


@app.route("/package/checkout", methods=["GET", "POST"])
def package_checkout():
    db = get_db()
    size = _package_size()
    selection = _package_selection()
    profiles = []
    if selection:
        placeholders = ",".join("?" * len(selection))
        profiles = db.execute(
            f"SELECT * FROM profiles WHERE profile_code IN ({placeholders}) AND is_active = 1",
            selection,
        ).fetchall()

    if request.method == "POST":
        if len(profiles) != size:
            flash(f"Please select exactly {size} profiles before checking out.", "error")
            return redirect(url_for("index"))

        user_name = request.form.get("user_name", "").strip()[:120]
        user_phone = request.form.get("user_phone", "").strip()
        message = request.form.get("message", "").strip()[:500]
        consent = request.form.get("consent")
        viewer_declaration = request.form.get("viewer_declaration")

        errors = []
        if not valid_indian_phone(user_phone):
            errors.append("Please enter a valid 10-digit Indian mobile number.")
        if not consent:
            errors.append("Please confirm you have completed the payment.")
        if not viewer_declaration:
            errors.append("Please confirm the declaration about genuine use and not misusing/screenshotting the profile data.")

        proof_file = request.files.get("payment_proof")
        proof_name = None
        if not proof_file or not proof_file.filename:
            errors.append("Please upload your payment screenshot.")
        else:
            try:
                proof_name = save_payment_proof(proof_file)
            except ImageValidationError as e:
                errors.append(str(e))

        # Match Coins redemption for the whole package — same rule as single
        # unlock: only against the OTP-verified session phone.
        package_price = float(get_setting("package_price", "149"))
        coins_requested = request.form.get("use_coins", type=int) or 0
        coins_used = 0
        discount_amount = 0.0
        wallet_phone = verified_phone()
        if coins_requested > 0:
            if not wallet_phone or wallet_phone != user_phone:
                errors.append("Verify this phone number (via My Requests) to use its Match Coins.")
            else:
                max_coins_by_value = int(package_price / coin_value_inr())
                coins_used = max(0, min(coins_requested, get_wallet_balance(wallet_phone), max_coins_by_value))
                discount_amount = round(coins_used * coin_value_inr(), 2)

        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("package_checkout.html", profiles=profiles, size=size)

        package_code = new_request_code()

        if coins_used > 0:
            if not debit_coins(wallet_phone, coins_used, "package_redeem", package_code):
                flash("Your coin balance changed — please retry.", "error")
                return render_template("package_checkout.html", profiles=profiles, size=size)

        for i, profile in enumerate(profiles):
            request_code = new_request_code()
            db.execute(
                """
                INSERT INTO unlock_requests
                (request_code, profile_id, user_name, user_phone, payment_proof_name, message,
                 viewer_declaration, coins_used, discount_amount, status, requested_at, package_code)
                VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, 'pending', ?, ?)
                """,
                (request_code, profile["id"], user_name, user_phone, proof_name, message,
                 coins_used if i == 0 else 0, discount_amount if i == 0 else 0.0,
                 datetime.now().isoformat(), package_code),
            )
        db_commit_retry(db)

        session.pop("package_selection", None)
        profile_list = ", ".join(p["profile_code"] for p in profiles)
        notify_admin(
            "New PACKAGE payment request",
            f"{user_name or 'A customer'} ({user_phone}) submitted payment proof for a "
            f"{size}-profile package ({profile_list}). Package code: {package_code}. "
            f"{'Coins used: ' + str(coins_used) + f' (₹{discount_amount} off). ' if coins_used else ''}"
            f"Review: {request.url_root.rstrip('/')}{url_for('admin_requests')}",
        )
        create_admin_event(
            "package",
            f"New package request — {package_code}",
            f"{user_name or 'A customer'} ({user_phone}) for {size} profiles ({profile_list}).",
            package_code,
            url_for("admin_requests"),
        )

        return render_template("package_submitted.html", profiles=profiles, package_code=package_code)

    return render_template("package_checkout.html", profiles=profiles, size=size)


# ======================================================================
# SELF REGISTRATION — "Register Yourself" for ₹{registration_price}.
# A visitor submits their own basic details + a small payment proof.
# This is NOT written straight into the public profiles table — it lands
# in self_registrations as 'pending' and only becomes a real, browsable
# profile once an admin reviews and approves it (mirrors the unlock_request
# manual-verification pattern already used across the site).
# ======================================================================
@app.route("/register-yourself", methods=["GET", "POST"])
def register_yourself():
    if request.method == "POST":
        ip = _client_ip()
        if registration_limiter.is_locked(ip):
            flash("Too many submissions from this device. Please try again in a few minutes.", "error")
            return render_template("register_yourself.html")
        registration_limiter.register_attempt(ip)

        name = request.form.get("name", "").strip()[:120]
        phone = request.form.get("phone", "").strip()
        age = request.form.get("age", "").strip()
        gender = request.form.get("gender", "").strip()
        city = request.form.get("city", "").strip()[:120]
        marital_status = request.form.get("marital_status", "").strip()[:60]
        education = request.form.get("education", "").strip()[:150]
        profession = request.form.get("profession", "").strip()[:150]
        bio = request.form.get("bio", "").strip()[:800]
        consent = request.form.get("consent")
        declaration = request.form.get("declaration")
        hobbies = ", ".join(h.strip() for h in request.form.getlist("hobbies") if h.strip())[:300]
        lifestyle_drinking = request.form.get("lifestyle_drinking", "").strip()[:30]
        lifestyle_smoking = request.form.get("lifestyle_smoking", "").strip()[:30]
        lifestyle_tobacco = request.form.get("lifestyle_tobacco", "").strip()[:30]
        lifestyle_namaz = request.form.get("lifestyle_namaz", "").strip()[:30]
        lifestyle_roza = request.form.get("lifestyle_roza", "").strip()[:30]

        errors = []
        if not name:
            errors.append("Please enter your name.")
        if not valid_indian_phone(phone):
            errors.append("Please enter a valid 10-digit Indian mobile number.")
        if not age.isdigit() or not (18 <= int(age) <= 90):
            errors.append("Please enter a valid age (18-90).")
        if gender not in ("Male", "Female"):
            errors.append("Please select your gender.")
        if not city:
            errors.append("Please enter your city.")
        if not consent:
            errors.append("Please confirm you have completed the ₹{} payment.".format(get_setting("registration_price", "11")))
        if not declaration:
            errors.append("Please confirm the declaration that all information you're submitting is true.")

        photo_original_name = photo_preview_name = None
        photo_file = request.files.get("photo")
        if photo_file and photo_file.filename:
            try:
                photo_original_name, photo_preview_name = save_profile_photo(photo_file)
            except ImageValidationError as e:
                errors.append(str(e))

        proof_file = request.files.get("payment_proof")
        proof_name = None
        if not proof_file or not proof_file.filename:
            errors.append("Please upload your payment screenshot.")
        else:
            try:
                proof_name = save_payment_proof(proof_file)
            except ImageValidationError as e:
                errors.append(str(e))

        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("register_yourself.html")

        db = get_db()
        request_code = new_request_code()
        db.execute(
            """
            INSERT INTO self_registrations
            (request_code, name, phone, age, gender, city, marital_status, education, profession, bio,
             hobbies, lifestyle_drinking, lifestyle_smoking, lifestyle_tobacco, lifestyle_namaz,
             lifestyle_roza, declaration_accepted,
             photo_original_name, photo_preview_name, payment_proof_name, status, requested_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, 'pending', ?)
            """,
            (request_code, name, phone, int(age), gender, city, marital_status, education, profession, bio,
             hobbies, lifestyle_drinking, lifestyle_smoking, lifestyle_tobacco, lifestyle_namaz, lifestyle_roza,
             photo_original_name, photo_preview_name, proof_name, datetime.now().isoformat()),
        )
        db_commit_retry(db)

        notify_admin(
            "New self-registration",
            f"{name} ({phone}) registered themselves for ₹{get_setting('registration_price', '11')}. "
            f"Request code: {request_code}. Review: {request.url_root.rstrip('/')}{url_for('admin_registrations')}",
        )
        create_admin_event(
            "registration",
            f"New self-registration — {request_code}",
            f"{name} ({phone}) registered for ₹{get_setting('registration_price', '11')}.",
            request_code,
            url_for("admin_registrations"),
        )

        return render_template("registration_submitted.html", request_code=request_code)

    return render_template("register_yourself.html")


# ======================================================================
# HELP SHADI — community-assistance feature. Deliberately separate from
# the matrimonial profiles/payment flow above: nobody who registers here
# becomes a profile, and none of their details are ever shown publicly.
# Reuses the site's existing qr_image/upi_id settings (via inject_globals)
# rather than a second QR setting, and the same manual-review pattern
# (pending -> admin decides) used everywhere else on the site.
# ======================================================================
HELP_SHADI_CATEGORIES = [
    "Nikah essentials", "Basic household essentials", "Food",
    "Venue support", "Other",
]


@app.route("/help-shadi")
def help_shadi():
    return render_template("help_shadi.html", categories=HELP_SHADI_CATEGORIES)


@app.route("/help-shadi/register", methods=["GET", "POST"])
def help_shadi_register():
    if request.method == "POST":
        ip = _client_ip()
        if help_shadi_limiter.is_locked(ip):
            flash("Too many submissions from this device. Please try again in a few minutes.", "error")
            return render_template("help_shadi_register.html", categories=HELP_SHADI_CATEGORIES)
        help_shadi_limiter.register_attempt(ip)

        guardian_name = request.form.get("guardian_name", "").strip()[:120]
        candidate_name = request.form.get("candidate_name", "").strip()[:120]
        contact = request.form.get("contact", "").strip()[:40]
        address = request.form.get("address", "").strip()[:400]
        assistance_category = request.form.get("assistance_category", "").strip()
        details = request.form.get("details", "").strip()[:1000]

        errors = []
        if not guardian_name:
            errors.append("Please enter the guardian's name.")
        if not candidate_name:
            errors.append("Please enter the candidate's name.")
        if not valid_indian_phone(contact):
            errors.append("Please enter a valid 10-digit Indian mobile number.")
        if not address:
            errors.append("Please enter the address.")
        if assistance_category not in HELP_SHADI_CATEGORIES:
            errors.append("Please select the type of assistance required.")
        if not details:
            errors.append("Please describe the situation briefly.")

        document_filename = None
        doc_file = request.files.get("document")
        if doc_file and doc_file.filename:
            try:
                document_filename = save_help_shadi_document(doc_file)
            except ImageValidationError as e:
                errors.append(str(e))

        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("help_shadi_register.html", categories=HELP_SHADI_CATEGORIES)

        db = get_db()
        db.execute(
            """
            INSERT INTO help_shadi_requests
            (guardian_name, candidate_name, contact, address, assistance_category,
             details, document_filename, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (guardian_name, candidate_name, contact, address, assistance_category,
             details, document_filename, datetime.now().isoformat()),
        )
        db_commit_retry(db)

        hs_name = get_setting("help_shadi_display_name", "Help Shadi")
        notify_admin(
            f"New {hs_name} request",
            f"{guardian_name} submitted a {hs_name} request for {candidate_name} "
            f"({assistance_category}). Contact: {contact}. "
            f"Review: {request.url_root.rstrip('/')}{url_for('admin_help_shadi')}",
        )
        create_admin_event(
            "help_shadi",
            f"New {hs_name} request — {candidate_name}",
            f"Guardian: {guardian_name}. Category: {assistance_category}. Contact: {contact}.",
            "",
            url_for("admin_help_shadi"),
        )

        return render_template("help_shadi_submitted.html")

    return render_template("help_shadi_register.html", categories=HELP_SHADI_CATEGORIES)


@app.route("/profile/<profile_code>/full")
def profile_full(profile_code):
    db = get_db()
    profile = db.execute("SELECT * FROM profiles WHERE profile_code = ?", (profile_code,)).fetchone()
    if not profile:
        abort(404)
    unlocked = user_has_unlocked(db, profile["id"], verified_phone())
    if not unlocked:
        flash("This profile isn't unlocked for your verified number yet.", "error")
        return redirect(url_for("verify_access"))
    viewer_profile = get_viewer_profile(db, verified_phone())
    match_score = compute_compatibility_score(viewer_profile, profile) if viewer_profile else None
    return render_template("profile_full.html", profile=profile, match_score=match_score)


@app.route("/profile/<profile_code>/request-more-photos", methods=["POST"])
def request_more_photos(profile_code):
    """Lets someone who has already unlocked a profile ask the admin for
    additional photos. This never auto-sends anything — it just logs a
    request and pings the admin, same manual-review pattern as everything
    else on this site."""
    db = get_db()
    profile = db.execute("SELECT * FROM profiles WHERE profile_code = ?", (profile_code,)).fetchone()
    if not profile:
        abort(404)

    phone = verified_phone()
    unlocked = user_has_unlocked(db, profile["id"], phone)
    if not unlocked:
        flash("Only someone who has unlocked this profile can request more photos.", "error")
        return redirect(url_for("verify_access"))

    message = request.form.get("message", "").strip()[:300]
    db.execute(
        """
        INSERT INTO photo_requests (profile_id, requester_phone, message, status, requested_at)
        VALUES (?, ?, ?, 'pending', ?)
        """,
        (profile["id"], phone, message, datetime.now().isoformat()),
    )
    db_commit_retry(db)
    notify_admin(
        "More photos requested",
        f"{phone} requested more photos for profile {profile['profile_code']} ({profile['name']}). "
        f"Review: {request.url_root.rstrip('/')}{url_for('admin_photo_requests')}",
    )
    create_admin_event(
        "photo_request",
        f"More photos requested — {profile['profile_code']}",
        f"{phone} for profile {profile['profile_code']} ({profile['name']}).",
        profile["profile_code"],
        url_for("admin_photo_requests"),
    )
    flash("Your request for more photos has been sent to our team.", "success")
    return redirect(url_for("profile_full", profile_code=profile_code))


# ======================================================================
# OTP-BASED ACCESS ("My Requests" / lightweight passwordless account)
#
# There is no separate "sign up" in this business — browsing is free and
# anonymous. The phone number a visitor uses when submitting an unlock
# request effectively *is* their account. To check "My Requests" from
# any device, they verify ownership of that number via a one-time SMS
# code. This replaces v2's phone+request-code check, which trusted
# whatever phone number was simply typed into a form.
# ======================================================================
OTP_LENGTH = 6
OTP_TTL_MINUTES = 5
OTP_RESEND_COOLDOWN_SECONDS = 45


@app.route("/verify-access", methods=["GET", "POST"])
def verify_access():
    if request.method == "POST":
        phone = request.form.get("phone", "").strip()
        if not valid_indian_phone(phone):
            flash("Please enter a valid 10-digit Indian mobile number.", "error")
            return render_template("verify_access.html")

        if otp_request_limiter.is_locked(phone):
            flash("Too many OTP requests for this number. Please try again later.", "error")
            return render_template("verify_access.html")

        last_sent = session.get("otp_last_sent_at")
        if last_sent and time.time() - last_sent < OTP_RESEND_COOLDOWN_SECONDS and session.get("otp_phone") == phone:
            session["otp_phone"] = phone
            return redirect(url_for("verify_access_confirm"))

        otp_request_limiter.register_attempt(phone)
        code = "".join(secrets.choice("0123456789") for _ in range(OTP_LENGTH))
        db = get_db()
        db.execute("DELETE FROM otp_codes WHERE phone = ?", (phone,))
        db.execute(
            "INSERT INTO otp_codes (phone, code_hash, expires_at, created_at) VALUES (?, ?, ?, ?)",
            (phone, generate_password_hash(code),
             (datetime.now() + timedelta(minutes=OTP_TTL_MINUTES)).isoformat(),
             datetime.now().isoformat()),
        )
        db_commit_retry(db)
        send_otp_sms(phone, code)

        session["otp_phone"] = phone
        session["otp_last_sent_at"] = time.time()
        if OTP_PROVIDER == "console":
            flash(f"Dev mode: SMS provider not configured, so here's your OTP directly: {code}", "success")
        else:
            flash("An OTP has been sent to your mobile number.", "success")
        return redirect(url_for("verify_access_confirm"))

    return render_template("verify_access.html")


@app.route("/verify-access/confirm", methods=["GET", "POST"])
def verify_access_confirm():
    phone = session.get("otp_phone")
    if not phone:
        return redirect(url_for("verify_access"))

    if request.method == "POST":
        entered = request.form.get("otp", "").strip()
        if otp_verify_limiter.is_locked(phone):
            flash("Too many incorrect attempts. Please request a new OTP.", "error")
            return redirect(url_for("verify_access"))

        db = get_db()
        row = db.execute(
            "SELECT * FROM otp_codes WHERE phone = ? AND consumed = 0 ORDER BY id DESC LIMIT 1", (phone,)
        ).fetchone()

        valid = False
        if row and datetime.fromisoformat(row["expires_at"]) >= datetime.now():
            if check_password_hash(row["code_hash"], entered):
                valid = True

        if not valid:
            otp_verify_limiter.register_attempt(phone)
            flash("Incorrect or expired OTP. Please try again.", "error")
            return render_template("verify_access_confirm.html", phone=phone)

        db.execute("UPDATE otp_codes SET consumed = 1 WHERE id = ?", (row["id"],))
        db_commit_retry(db)
        otp_verify_limiter.clear(phone)
        session.pop("otp_phone", None)
        session.pop("otp_last_sent_at", None)
        session.permanent = True
        session["verified_phone"] = phone
        flash("Verified successfully.", "success")
        return redirect(url_for("my_requests"))

    return render_template("verify_access_confirm.html", phone=phone)


@app.route("/my-requests")
def my_requests():
    phone = verified_phone()
    if not phone:
        return redirect(url_for("verify_access"))
    db = get_db()
    rows = db.execute(
        """
        SELECT ur.*, p.profile_code, p.name, p.age, p.city
        FROM unlock_requests ur JOIN profiles p ON p.id = ur.profile_id
        WHERE ur.user_phone = ?
        ORDER BY ur.requested_at DESC
        """,
        (phone,),
    ).fetchall()
    return render_template("my_requests.html", rows=rows, phone=phone)


@app.route("/my-requests/exit")
def exit_verification():
    session.pop("verified_phone", None)
    return redirect(url_for("index"))


# ======================================================================
# PAYMENT WAITING LOUNGE — a lightweight, no-login status-check API so a
# customer who just submitted a payment/registration doesn't have to
# manually revisit "My Requests" (which itself requires OTP verification)
# to see whether they're still pending. Deliberately returns the bare
# minimum: just enough to drive a stepper UI, nothing that could leak
# another customer's private details. request_code/package_code are
# 8-character codes drawn from a 32-symbol alphabet (32^8 ≈ 1.1 trillion
# combinations) — the same code the customer is already shown and told
# to save, so this doesn't create any new guessable-ID exposure beyond
# what already existed (the code was always their reference number for
# WhatsApp support).
# ======================================================================
def _track_code_valid(code):
    alphabet = set("ABCDEFGHJKMNPQRSTUVWXYZ23456789")
    return bool(code) and 4 <= len(code) <= 12 and set(code.upper()) <= alphabet


@app.route("/track/<code>")
def track_status(code):
    if not _track_code_valid(code):
        abort(404)
    code = code.upper()
    db = get_db()

    # A package_code covers several unlock_requests rows at once.
    pkg_rows = db.execute(
        "SELECT status FROM unlock_requests WHERE package_code = ?", (code,)
    ).fetchall()
    if pkg_rows:
        statuses = {r["status"] for r in pkg_rows}
        if statuses == {"approved"}:
            overall = "approved"
        elif "rejected" in statuses:
            overall = "rejected"
        else:
            overall = "pending"
        return jsonify({"found": True, "kind": "package", "status": overall})

    row = db.execute(
        "SELECT status FROM unlock_requests WHERE request_code = ?", (code,)
    ).fetchone()
    if row:
        return jsonify({"found": True, "kind": "unlock", "status": row["status"]})

    row = db.execute(
        "SELECT status FROM self_registrations WHERE request_code = ?", (code,)
    ).fetchone()
    if row:
        return jsonify({"found": True, "kind": "registration", "status": row["status"]})

    return jsonify({"found": False})


# ======================================================================
# AGENT PORTAL
#
# Agents are NEVER self-registered from the public site — only an admin
# can create one (Admin > Agents), which is when the identity/details
# check happens and a unique access code is generated and shown once.
# Logging in as that agent afterwards requires ALL of:
#   1. The agent's registered phone number
#   2. OTP verification of that number (reuses the same OTP infra as
#      customer "My Requests")
#   3. The access code the admin gave them at creation time
# The access code itself is never stored in plaintext, never appears in
# any template/JS/HTML, and is only ever compared server-side.
# ======================================================================
def agent_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("agent_id"):
            return redirect(url_for("agent_login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def log_agent_action(agent_id, action, detail=""):
    db = get_db()
    db.execute(
        "INSERT INTO agent_activity_log (agent_id, action, detail, ip, created_at) VALUES (?, ?, ?, ?, ?)",
        (agent_id, action, detail, _client_ip(), datetime.now().isoformat()),
    )
    db_commit_retry(db)


agent_otp_limiter = RateLimiter(max_attempts=5, window_seconds=600, lockout_seconds=600)
agent_code_limiter = RateLimiter(max_attempts=6, window_seconds=600, lockout_seconds=900)


@app.route("/agent/login", methods=["GET", "POST"])
def agent_login():
    if request.method == "POST":
        phone = request.form.get("phone", "").strip()
        if not valid_indian_phone(phone):
            flash("Please enter a valid 10-digit mobile number.", "error")
            return render_template("agent_login.html")

        db = get_db()
        agent = db.execute("SELECT * FROM agents WHERE phone = ? AND is_active = 1", (phone,)).fetchone()
        if not agent:
            # Deliberately vague — do not reveal whether the number is a
            # registered agent or not.
            flash("If this number is registered as an agent, an OTP has been sent.", "success")
            return render_template("agent_login.html")

        if agent_otp_limiter.is_locked(phone):
            flash("Too many OTP requests. Please try again later.", "error")
            return render_template("agent_login.html")

        agent_otp_limiter.register_attempt(phone)
        code = "".join(secrets.choice("0123456789") for _ in range(OTP_LENGTH))
        db.execute("DELETE FROM otp_codes WHERE phone = ?", (phone,))
        db.execute(
            "INSERT INTO otp_codes (phone, code_hash, expires_at, created_at) VALUES (?, ?, ?, ?)",
            (phone, generate_password_hash(code),
             (datetime.now() + timedelta(minutes=OTP_TTL_MINUTES)).isoformat(),
             datetime.now().isoformat()),
        )
        db_commit_retry(db)
        send_otp_sms(phone, code)

        session["agent_otp_phone"] = phone
        if OTP_PROVIDER == "console":
            flash(f"Dev mode: SMS provider not configured, so here's your OTP directly: {code}", "success")
        else:
            flash("An OTP has been sent to your registered mobile number.", "success")
        return redirect(url_for("agent_verify"))

    return render_template("agent_login.html")


@app.route("/agent/verify", methods=["GET", "POST"])
def agent_verify():
    phone = session.get("agent_otp_phone")
    if not phone:
        return redirect(url_for("agent_login"))

    if request.method == "POST":
        entered_otp = request.form.get("otp", "").strip()
        access_code = request.form.get("access_code", "").strip()

        if agent_code_limiter.is_locked(phone):
            flash("Too many incorrect attempts. Please start over.", "error")
            session.pop("agent_otp_phone", None)
            return redirect(url_for("agent_login"))

        db = get_db()
        otp_row = db.execute(
            "SELECT * FROM otp_codes WHERE phone = ? AND consumed = 0 ORDER BY id DESC LIMIT 1", (phone,)
        ).fetchone()
        otp_valid = (
            otp_row
            and datetime.fromisoformat(otp_row["expires_at"]) >= datetime.now()
            and check_password_hash(otp_row["code_hash"], entered_otp)
        )

        agent = db.execute("SELECT * FROM agents WHERE phone = ? AND is_active = 1", (phone,)).fetchone()
        code_valid = agent and access_code and check_password_hash(agent["access_code_hash"], access_code)

        if not (otp_valid and code_valid):
            agent_code_limiter.register_attempt(phone)
            flash("Incorrect OTP or access code.", "error")
            return render_template("agent_verify.html", phone=phone)

        db.execute("UPDATE otp_codes SET consumed = 1 WHERE id = ?", (otp_row["id"],))
        db.execute("UPDATE agents SET last_login_at = ? WHERE id = ?", (datetime.now().isoformat(), agent["id"]))
        db_commit_retry(db)
        agent_code_limiter.clear(phone)
        session.pop("agent_otp_phone", None)
        session.permanent = True
        session["agent_id"] = agent["id"]
        session["agent_name"] = agent["name"]
        log_agent_action(agent["id"], "login", f"from {_client_ip()}")
        flash(f"Welcome, {agent['name']}.", "success")
        return redirect(url_for("agent_dashboard"))

    return render_template("agent_verify.html", phone=phone)


@app.route("/agent/logout")
def agent_logout():
    if session.get("agent_id"):
        log_agent_action(session["agent_id"], "logout")
    session.pop("agent_id", None)
    session.pop("agent_name", None)
    return redirect(url_for("agent_login"))


AGENT_PAGE_SIZE = 15


@app.route("/agent/dashboard")
@agent_required
def agent_dashboard():
    db = get_db()
    filters = {
        "gender": request.args.get("gender", "").strip(),
        "min_age": request.args.get("min_age", "").strip(),
        "max_age": request.args.get("max_age", "").strip(),
        "city": request.args.get("city", "").strip(),
        "state": request.args.get("state", "").strip(),
        "marital_status": request.args.get("marital_status", "").strip(),
        "education": request.args.get("education", "").strip(),
        "profession": request.args.get("profession", "").strip(),
        "community": request.args.get("community", "").strip(),
        "sect": request.args.get("sect", "").strip(),
        "mother_tongue": request.args.get("mother_tongue", "").strip(),
        "verified_only": request.args.get("verified_only", "").strip(),
    }
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1

    where = ["is_active = 1"]
    params = []
    if filters["gender"] in ("Male", "Female"):
        where.append("gender = ?")
        params.append(filters["gender"])
    if filters["min_age"].isdigit():
        where.append("age >= ?")
        params.append(int(filters["min_age"]))
    if filters["max_age"].isdigit():
        where.append("age <= ?")
        params.append(int(filters["max_age"]))
    for field in ("city", "state", "education", "profession", "work_location"):
        if filters.get(field):
            where.append(f"{field} LIKE ?")
            params.append(f"%{filters[field]}%")
    for field in ("marital_status", "community", "sect", "mother_tongue"):
        if filters.get(field):
            where.append(f"{field} = ?")
            params.append(filters[field])
    if filters["verified_only"] == "1":
        where.append("admin_verified = 1")

    where_sql = " AND ".join(where)
    total = db.execute(f"SELECT COUNT(*) c FROM profiles WHERE {where_sql}", params).fetchone()["c"]
    total_pages = max(1, math.ceil(total / AGENT_PAGE_SIZE))
    page = min(page, total_pages)
    offset = (page - 1) * AGENT_PAGE_SIZE

    profiles = db.execute(
        f"SELECT * FROM profiles WHERE {where_sql} ORDER BY created_at DESC LIMIT ? OFFSET ?",
        params + [AGENT_PAGE_SIZE, offset],
    ).fetchall()

    def distinct(col):
        return [r[0] for r in db.execute(
            f"SELECT DISTINCT {col} FROM profiles WHERE is_active=1 AND {col} IS NOT NULL AND {col} != '' ORDER BY {col}"
        ).fetchall()]

    filter_options = {
        "cities": distinct("city"), "states": distinct("state"),
        "marital_statuses": distinct("marital_status"), "communities": distinct("community"),
        "sects": distinct("sect"), "mother_tongues": distinct("mother_tongue"),
    }
    any_filter_active = any(v for k, v in filters.items())
    active_filters = {k: v for k, v in filters.items() if v}

    return render_template(
        "agent_dashboard.html", profiles=profiles, filters=filters, filter_options=filter_options,
        page=page, total_pages=total_pages, total=total, any_filter_active=any_filter_active,
        active_filters=active_filters,
    )


@app.route("/agent/profile/<profile_code>")
@agent_required
def agent_profile_view(profile_code):
    db = get_db()
    profile = db.execute("SELECT * FROM profiles WHERE profile_code = ?", (profile_code,)).fetchone()
    if not profile:
        abort(404)
    log_agent_action(session["agent_id"], "view_profile", profile_code)
    return render_template("agent_profile_view.html", profile=profile)


@app.route("/agent/my-activity")
@agent_required
def agent_my_activity():
    db = get_db()
    rows = db.execute(
        "SELECT * FROM agent_activity_log WHERE agent_id = ? ORDER BY id DESC LIMIT 100",
        (session["agent_id"],),
    ).fetchall()
    return render_template("agent_activity.html", rows=rows)


# ======================================================================
# ADMIN — AGENT MANAGEMENT
# ======================================================================
@app.route("/admin/agents")
@admin_required
def admin_agents():
    db = get_db()
    agents = db.execute("SELECT * FROM agents ORDER BY created_at DESC").fetchall()
    return render_template("admin_agents.html", agents=agents)


@app.route("/admin/agents/add", methods=["GET", "POST"])
@admin_required
def admin_add_agent():
    if request.method == "POST":
        name = request.form.get("name", "").strip()[:120]
        phone = request.form.get("phone", "").strip()
        notes = request.form.get("notes", "").strip()[:500]

        errors = []
        if not name:
            errors.append("Agent name is required.")
        if not valid_indian_phone(phone):
            errors.append("Please enter a valid 10-digit mobile number.")

        db = get_db()
        if not errors and db.execute("SELECT 1 FROM agents WHERE phone = ?", (phone,)).fetchone():
            errors.append("An agent with this phone number already exists.")

        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("admin_agent_form.html", form={"name": name, "phone": phone, "notes": notes})

        access_code = "".join(secrets.choice("ABCDEFGHJKMNPQRSTUVWXYZ23456789") for _ in range(8))
        db.execute(
            "INSERT INTO agents (name, phone, notes, access_code_hash, is_active, created_at) VALUES (?, ?, ?, ?, 1, ?)",
            (name, phone, notes, generate_password_hash(access_code), datetime.now().isoformat()),
        )
        db_commit_retry(db)
        log_admin_action("add_agent", phone)
        flash("Agent created.", "success")
        return render_template("admin_agent_created.html", name=name, phone=phone, access_code=access_code)

    return render_template("admin_agent_form.html", form=None)


@app.route("/admin/agents/<int:agent_id>/toggle", methods=["POST"])
@admin_required
def admin_toggle_agent(agent_id):
    db = get_db()
    db.execute("UPDATE agents SET is_active = 1 - is_active WHERE id = ?", (agent_id,))
    db_commit_retry(db)
    log_admin_action("toggle_agent", str(agent_id))
    flash("Agent status updated.", "success")
    return redirect(url_for("admin_agents"))


@app.route("/admin/agents/<int:agent_id>/reset-code", methods=["POST"])
@admin_required
def admin_reset_agent_code(agent_id):
    db = get_db()
    agent = db.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
    if not agent:
        abort(404)
    access_code = "".join(secrets.choice("ABCDEFGHJKMNPQRSTUVWXYZ23456789") for _ in range(8))
    db.execute("UPDATE agents SET access_code_hash = ? WHERE id = ?", (generate_password_hash(access_code), agent_id))
    db_commit_retry(db)
    log_admin_action("reset_agent_code", agent["phone"])
    flash("Access code reset.", "success")
    return render_template("admin_agent_created.html", name=agent["name"], phone=agent["phone"], access_code=access_code)


@app.route("/admin/agents/<int:agent_id>/activity")
@admin_required
def admin_agent_activity(agent_id):
    db = get_db()
    agent = db.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
    if not agent:
        abort(404)
    rows = db.execute(
        "SELECT * FROM agent_activity_log WHERE agent_id = ? ORDER BY id DESC LIMIT 200", (agent_id,)
    ).fetchall()
    return render_template("admin_agent_activity.html", agent=agent, rows=rows)


# ======================================================================
# SEO — robots.txt / sitemap.xml
# ======================================================================
@app.route("/healthz")
def healthz():
    """Lightweight uptime-monitor target — verifies the app process is up
    AND the database is actually reachable (a hung/locked DB is the most
    common real failure mode, not just 'process is running')."""
    try:
        get_db().execute("SELECT 1").fetchone()
        return {"status": "ok"}, 200
    except Exception as e:
        app.logger.error(f"[HEALTHZ] database check failed: {e}")
        return {"status": "error", "detail": "database unreachable"}, 503


@app.route("/robots.txt")
def robots_txt():
    lines = [
        "User-agent: *",
        "Disallow: /admin",
        "Disallow: /agent",
        "Disallow: /my-requests",
        "Disallow: /verify-access",
        "Disallow: /profile/*/unlock",
        "Disallow: /profile/*/full",
        "Disallow: /profile/*/photo",
        f"Sitemap: {request.url_root.rstrip('/')}/sitemap.xml",
    ]
    return Response("\n".join(lines), mimetype="text/plain")


@app.route("/sitemap.xml")
def sitemap_xml():
    db = get_db()
    profiles = db.execute("SELECT profile_code FROM profiles WHERE is_active = 1").fetchall()
    urls = [url_for("index", _external=True), url_for("how_it_works", _external=True),
            url_for("faq", _external=True), url_for("terms", _external=True),
            url_for("privacy", _external=True)]
    urls += [url_for("profile_preview", profile_code=p["profile_code"], _external=True) for p in profiles]
    xml = ['<?xml version="1.0" encoding="UTF-8"?>', '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for u in urls:
        xml.append(f"<url><loc>{u}</loc></url>")
    xml.append("</urlset>")
    return Response("\n".join(xml), mimetype="application/xml")


# ======================================================================
# ADMIN — AUTH
# ======================================================================
@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    ip = _client_ip()
    if request.method == "POST":
        if login_limiter.is_locked(ip):
            flash("Too many failed attempts. Please try again in a few minutes.", "error")
            return render_template("admin_login.html")

        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if username == ADMIN_USERNAME and check_password_hash(ADMIN_PASSWORD_HASH, password):
            login_limiter.clear(ip)
            session.clear()
            session["is_admin"] = True
            session["admin_username"] = username
            session["admin_login_at"] = datetime.now().isoformat()
            session.permanent = True
            log_admin_action("login", f"from {ip}")
            nxt = request.args.get("next")
            return redirect(nxt or url_for("admin_dashboard"))

        login_limiter.register_attempt(ip)
        flash("Invalid credentials.", "error")
    return render_template("admin_login.html")


@app.route("/admin/logout")
def admin_logout():
    if session.get("is_admin"):
        log_admin_action("logout")
    session.pop("is_admin", None)
    session.pop("admin_username", None)
    return redirect(url_for("admin_login"))


# ======================================================================
# ADMIN — DASHBOARD
# ======================================================================
@app.route("/admin/api/pending-count")
@admin_required
def admin_api_pending_count():
    db = get_db()
    count = db.execute("SELECT COUNT(*) c FROM unlock_requests WHERE status='pending'").fetchone()["c"]
    reg_count = db.execute("SELECT COUNT(*) c FROM self_registrations WHERE status='pending'").fetchone()["c"]
    help_shadi_count = db.execute("SELECT COUNT(*) c FROM help_shadi_requests WHERE status='pending'").fetchone()["c"]
    unread_events = db.execute("SELECT COUNT(*) c FROM admin_events WHERE is_read = 0").fetchone()["c"]
    return {
        "pending": count + reg_count + help_shadi_count,
        "payment_pending": count,
        "registration_pending": reg_count,
        "help_shadi_pending": help_shadi_count,
        "unread_events": unread_events,
    }


@app.route("/admin/events")
@admin_required
def admin_events():
    db = get_db()
    rows = db.execute("SELECT * FROM admin_events ORDER BY id DESC LIMIT 200").fetchall()
    # Viewing the feed is what "seeing" a notification means here — mark
    # everything currently listed as read, same as opening a notification
    # inbox. The badge count reflects what's unread *before* this view.
    db.execute("UPDATE admin_events SET is_read = 1 WHERE is_read = 0")
    db_commit_retry(db)
    return render_template("admin_events.html", rows=rows)


# ======================================================================
# ADMIN — BACKUP
# On-demand, phone-friendly backup: builds the database (via SQLite's
# online .backup() API, which is safe to run while the app is live and
# writing under WAL mode — no downtime, no locking the site) plus every
# private/uploaded file into a single zip, streamed straight to the
# admin's device. This exists because Render's disk persistence for this
# app has never been confirmed (flagged since Part 1) — until that's
# verified, the safest backup is one the admin actually holds a copy of,
# not one that only lives on the same disk it's protecting against.
# ======================================================================
BACKUP_INCLUDE_DIRS = [
    ("profile_originals", PRIVATE_ORIGINALS_DIR),
    ("payment_proofs", PRIVATE_PROOFS_DIR),
    ("help_shadi_docs", PRIVATE_HELP_SHADI_DOCS_DIR),
    ("previews", PREVIEW_DIR),
    ("branding", BRANDING_DIR),
    ("banners", BANNERS_DIR),
]


@app.route("/admin/backup")
@admin_required
def admin_backup():
    db_size_mb = round(os.path.getsize(DB_PATH) / (1024 * 1024), 2) if os.path.exists(DB_PATH) else 0
    return render_template("admin_backup.html", db_size_mb=db_size_mb, data_dir=DATA_DIR)


@app.route("/admin/backup/download")
@admin_required
def admin_backup_download():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # Online backup — sqlite3's .backup() takes a consistent snapshot
        # even while other connections are actively writing (WAL-safe),
        # unlike a plain file copy which could grab a half-written page.
        tmp_db_path = os.path.join(DATA_DIR, f"_backup_tmp_{secrets.token_hex(6)}.db")
        try:
            src = sqlite3.connect(DB_PATH)
            dst = sqlite3.connect(tmp_db_path)
            with dst:
                src.backup(dst)
            src.close()
            dst.close()
            zf.write(tmp_db_path, "matrimonial.db")
        finally:
            if os.path.exists(tmp_db_path):
                os.remove(tmp_db_path)

        for arc_prefix, dir_path in BACKUP_INCLUDE_DIRS:
            if not os.path.isdir(dir_path):
                continue
            for fname in os.listdir(dir_path):
                fpath = os.path.join(dir_path, fname)
                if os.path.isfile(fpath):
                    zf.write(fpath, os.path.join(arc_prefix, fname))

    buf.seek(0)
    log_admin_action("backup_download", "")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                      download_name=f"saif_matrimonial_backup_{stamp}.zip")


@app.route("/admin")
@admin_required
def admin_dashboard():
    db = get_db()
    stats = {
        "total": db.execute("SELECT COUNT(*) c FROM profiles").fetchone()["c"],
        "active": db.execute("SELECT COUNT(*) c FROM profiles WHERE is_active=1").fetchone()["c"],
        "hidden": db.execute("SELECT COUNT(*) c FROM profiles WHERE is_active=0").fetchone()["c"],
        "pending": db.execute("SELECT COUNT(*) c FROM unlock_requests WHERE status='pending'").fetchone()["c"],
        "unlocked": db.execute("SELECT COUNT(*) c FROM unlock_requests WHERE status='unlocked'").fetchone()["c"],
        "rejected": db.execute("SELECT COUNT(*) c FROM unlock_requests WHERE status='rejected'").fetchone()["c"],
        "agents_total": db.execute("SELECT COUNT(*) c FROM agents").fetchone()["c"],
        "agents_active": db.execute("SELECT COUNT(*) c FROM agents WHERE is_active=1").fetchone()["c"],
        "reg_pending": db.execute("SELECT COUNT(*) c FROM self_registrations WHERE status='pending'").fetchone()["c"],
        "reg_total": db.execute("SELECT COUNT(*) c FROM self_registrations").fetchone()["c"],
        "photo_req_pending": db.execute("SELECT COUNT(*) c FROM photo_requests WHERE status='pending'").fetchone()["c"],
        "help_shadi_pending": db.execute("SELECT COUNT(*) c FROM help_shadi_requests WHERE status='pending'").fetchone()["c"],
        "help_shadi_total": db.execute("SELECT COUNT(*) c FROM help_shadi_requests").fetchone()["c"],
        "self_registered": db.execute("SELECT COUNT(*) c FROM profiles WHERE registration_source='self'").fetchone()["c"],
        "admin_registered": db.execute("SELECT COUNT(*) c FROM profiles WHERE registration_source='admin'").fetchone()["c"],
        "verified_profiles": db.execute("SELECT COUNT(*) c FROM profiles WHERE admin_verified=1").fetchone()["c"],
        "unread_events": db.execute("SELECT COUNT(*) c FROM admin_events WHERE is_read=0").fetchone()["c"],
        "shop_orders_pending": db.execute(
            "SELECT COUNT(*) c FROM shop_orders WHERE order_status='pending'"
        ).fetchone()["c"],
        "shop_orders_total": db.execute("SELECT COUNT(*) c FROM shop_orders").fetchone()["c"],
    }
    stats["needs_attention"] = (
        stats["pending"] + stats["reg_pending"] + stats["photo_req_pending"]
        + stats["help_shadi_pending"] + stats["shop_orders_pending"]
    )
    recent_pending = db.execute(
        """
        SELECT ur.*, p.profile_code, p.name FROM unlock_requests ur
        JOIN profiles p ON p.id = ur.profile_id
        WHERE ur.status = 'pending' ORDER BY ur.requested_at DESC LIMIT 5
        """
    ).fetchall()
    recent_activity = db.execute("SELECT * FROM admin_activity_log ORDER BY id DESC LIMIT 10").fetchall()
    return render_template("admin_dashboard.html", stats=stats, recent_pending=recent_pending,
                            recent_activity=recent_activity)


# ======================================================================
# ADMIN — PROFILES
# ======================================================================
# ======================================================================
# PROFILE PDF / EXCEL EXPORT  (admin panel — Section 3 & 4 of the spec)
# ======================================================================
PROFILE_FIELD_LABELS = [
    # (db_column, label) — used for both the PDF "Personal Details" table
    # and is intentionally short: empty values are skipped everywhere.
    ("age", "Age"), ("gender", "Gender"), ("marital_status", "Marital Status"),
    ("height", "Height"), ("mother_tongue", "Mother Tongue"), ("city", "City"),
    ("state", "State"), ("community", "Community"), ("sect", "Sect"),
]
PROFILE_CAREER_LABELS = [
    ("education", "Education"), ("profession", "Profession"),
    ("work_location", "Work Location"), ("income", "Income"),
]
PROFILE_LIFESTYLE_LABELS = [
    ("lifestyle_drinking", "Drinking"), ("lifestyle_smoking", "Smoking"),
    ("lifestyle_tobacco", "Gutkha / Tobacco"), ("lifestyle_namaz", "Namaz"),
    ("lifestyle_roza", "Roza (Fasting)"),
]


def _pdf_styles():
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(
        "BiodataName", parent=styles["Title"], fontSize=22, textColor=rl_colors.HexColor("#073e2f"),
        spaceAfter=2, alignment=TA_CENTER,
    ))
    styles.add(ParagraphStyle(
        "BiodataCode", parent=styles["Normal"], fontSize=10, textColor=rl_colors.HexColor("#6b7268"),
        alignment=TA_CENTER, spaceAfter=14,
    ))
    styles.add(ParagraphStyle(
        "SectionHead", parent=styles["Heading2"], fontSize=13, textColor=rl_colors.HexColor("#0b5a44"),
        spaceBefore=14, spaceAfter=6, borderPadding=0,
    ))
    styles.add(ParagraphStyle("BodyText2", parent=styles["Normal"], fontSize=10.5, leading=15))
    return styles


def _pdf_field_table(rows, styles):
    """Only include rows whose value is non-empty — never prints N/A/null."""
    data = [(r[0], r[1]) for r in rows if r[1]]
    if not data:
        return None
    table_data = [[Paragraph(f"<b>{k}</b>", styles["BodyText2"]), Paragraph(str(v), styles["BodyText2"])] for k, v in data]
    t = Table(table_data, colWidths=[42 * mm, 108 * mm])
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("LINEBELOW", (0, 0), (-1, -1), 0.4, rl_colors.HexColor("#e6e1d6")),
    ]))
    return t


def generate_profile_pdf_bytes(profile, business_name, brand_phone=""):
    """Builds a premium-styled matrimonial biodata PDF for one profile and
    returns it as BytesIO. Uses only real data from the DB row passed in —
    empty fields are simply omitted, never shown as N/A/null/undefined."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4, topMargin=16 * mm, bottomMargin=16 * mm,
        leftMargin=18 * mm, rightMargin=18 * mm,
        title=f"{profile['name']} — {profile['profile_code']}",
    )
    styles = _pdf_styles()
    story = []

    story.append(Paragraph(business_name.upper(), ParagraphStyle(
        "Brand", parent=styles["Normal"], fontSize=10, textColor=rl_colors.HexColor("#c9a86a"),
        alignment=TA_CENTER, spaceAfter=10,
    )))

    # Photo (if present) — original photo, since this is an internal admin document
    photo_path = os.path.join(PRIVATE_ORIGINALS_DIR, profile["photo_original_name"] or "")
    if profile["photo_original_name"] and os.path.exists(photo_path):
        try:
            img = RLImage(photo_path, width=55 * mm, height=68 * mm)
            img.hAlign = "CENTER"
            story.append(img)
            story.append(Spacer(1, 10))
        except Exception:
            pass

    story.append(Paragraph(profile["name"] or "Profile", styles["BiodataName"]))
    subtitle = f"{profile['profile_code']}"
    if profile["age"]:
        subtitle += f" &nbsp;·&nbsp; {profile['age']} yrs"
    if profile["city"]:
        subtitle += f" &nbsp;·&nbsp; {profile['city']}"
    story.append(Paragraph(subtitle, styles["BiodataCode"]))
    story.append(HRFlowable(width="100%", color=rl_colors.HexColor("#e8d9b8"), thickness=1))

    if profile["bio"]:
        story.append(Paragraph("About", styles["SectionHead"]))
        story.append(Paragraph(profile["bio"], styles["BodyText2"]))

    t = _pdf_field_table([(lbl, profile[col]) for col, lbl in PROFILE_FIELD_LABELS], styles)
    if t:
        story.append(Paragraph("Personal Details", styles["SectionHead"]))
        story.append(t)

    t = _pdf_field_table([(lbl, profile[col]) for col, lbl in PROFILE_CAREER_LABELS], styles)
    if t:
        story.append(Paragraph("Education & Career", styles["SectionHead"]))
        story.append(t)

    if profile["family_details"]:
        story.append(Paragraph("Family Details", styles["SectionHead"]))
        story.append(Paragraph(profile["family_details"], styles["BodyText2"]))

    if profile["hobbies"]:
        story.append(Paragraph("Hobbies & Interests", styles["SectionHead"]))
        story.append(Paragraph(profile["hobbies"], styles["BodyText2"]))

    t = _pdf_field_table([(lbl, profile[col]) for col, lbl in PROFILE_LIFESTYLE_LABELS], styles)
    if t:
        story.append(Paragraph("Lifestyle", styles["SectionHead"]))
        story.append(t)

    story.append(Paragraph("Contact", styles["SectionHead"]))
    if profile["contact_visible"] and profile["contact_number"]:
        story.append(Paragraph(f"Phone: {profile['contact_number']}", styles["BodyText2"]))
    else:
        contact_line = f"Please contact {business_name}"
        if brand_phone:
            contact_line += f" ({brand_phone})"
        contact_line += " to get in touch regarding this profile."
        story.append(Paragraph(contact_line, styles["BodyText2"]))

    def _footer(canvas, doc_):
        canvas.saveState()
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(rl_colors.HexColor("#6b7268"))
        canvas.drawString(18 * mm, 10 * mm, f"Generated {datetime.now().strftime('%d %b %Y')} · {business_name}")
        canvas.drawRightString(A4[0] - 18 * mm, 10 * mm, f"Page {doc_.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    buf.seek(0)
    return buf


PROFILE_EXCEL_COLUMNS = [
    ("profile_code", "Profile ID"), ("name", "Name"), ("gender", "Gender"), ("age", "Age"),
    ("registration_source", "Registration Source"), ("created_at", "Registration Date"),
    ("city", "City"), ("state", "State"), ("education", "Education"), ("profession", "Profession"),
    ("hobbies", "Hobbies / Interests"), ("marital_status", "Marital Status"),
    ("contact_number", "Contact Number"),
]


def generate_profiles_excel_bytes(profiles):
    """Builds an Excel report. Deliberately excludes anything auth/security
    related (there is none of that on the `profiles` row — no passwords or
    tokens live on this table) and only includes the columns listed above."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Profiles"

    header_fill = PatternFill(start_color="0B5A44", end_color="0B5A44", fill_type="solid")
    header_font = Font(color="FFFFFF", bold=True)
    for col_idx, (_, label) in enumerate(PROFILE_EXCEL_COLUMNS, start=1):
        cell = ws.cell(row=1, column=col_idx, value=label)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center")

    for row_idx, p in enumerate(profiles, start=2):
        for col_idx, (col, _) in enumerate(PROFILE_EXCEL_COLUMNS, start=1):
            val = p[col]
            if col == "created_at" and val:
                val = str(val)[:10]
            if col == "gender" and val is None:
                val = ""
            ws.cell(row=row_idx, column=col_idx, value=val if val is not None else "")

    for col_idx, (col, label) in enumerate(PROFILE_EXCEL_COLUMNS, start=1):
        width = max(14, min(38, len(label) + 6))
        ws.column_dimensions[chr(64 + col_idx) if col_idx <= 26 else "A"].width = width

    ws.freeze_panes = "A2"
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def generate_shop_invoice_pdf_bytes(order, items, business_name, brand_phone="", brand_address=""):
    """Builds a simple, clean invoice/receipt PDF for one shop order.
    Works for any order_status — clearly labels itself as a provisional
    receipt while payment is still pending, and as a paid invoice once
    payment_status is 'paid', so it never overstates what has actually
    been confirmed."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4, topMargin=18 * mm, bottomMargin=16 * mm,
        leftMargin=18 * mm, rightMargin=18 * mm,
        title=f"Invoice {order['order_code']}",
    )
    styles = _pdf_styles()
    story = []

    is_paid = (order["payment_status"] == "paid")
    doc_label = "INVOICE" if is_paid else "ORDER RECEIPT (Payment Pending Verification)"

    story.append(Paragraph(business_name.upper(), ParagraphStyle(
        "Brand2", parent=styles["Normal"], fontSize=10, textColor=rl_colors.HexColor("#c9a86a"),
        alignment=TA_CENTER, spaceAfter=4,
    )))
    story.append(Paragraph(doc_label, ParagraphStyle(
        "InvoiceHead", parent=styles["Title"], fontSize=17, textColor=rl_colors.HexColor("#073e2f"),
        alignment=TA_CENTER, spaceAfter=2,
    )))
    story.append(Paragraph(f"Order Code: {order['order_code']}", styles["BiodataCode"]))
    story.append(HRFlowable(width="100%", color=rl_colors.HexColor("#e8d9b8"), thickness=1))

    meta_rows = [
        ("Order Date", str(order["created_at"])[:16].replace("T", " ")),
        ("Customer Name", order["customer_name"]),
        ("Phone", order["customer_phone"]),
        ("Delivery Address", order["customer_address"] or "—"),
        ("Order Status", (order["order_status"] or "").capitalize()),
        ("Payment Status", (order["payment_status"] or "").capitalize()),
    ]
    t = _pdf_field_table(meta_rows, styles)
    if t:
        story.append(Spacer(1, 8))
        story.append(t)

    story.append(Paragraph("Items", styles["SectionHead"]))
    item_rows = [["Item", "Qty", "Unit Price", "Total"]]
    for it in items:
        label = it["product_name"]
        if it["variant_label"]:
            label += f" ({it['variant_label']})"
        item_rows.append([
            label, str(it["qty"]), f"Rs. {it['unit_price']:.0f}",
            f"Rs. {(it['unit_price'] * it['qty']):.0f}",
        ])
    items_table = Table(item_rows, colWidths=[80 * mm, 20 * mm, 30 * mm, 30 * mm])
    items_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), rl_colors.HexColor("#0b5a44")),
        ("TEXTCOLOR", (0, 0), (-1, 0), rl_colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 9.5),
        ("ALIGN", (1, 0), (-1, -1), "CENTER"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("LINEBELOW", (0, 0), (-1, -2), 0.4, rl_colors.HexColor("#e6e1d6")),
    ]))
    story.append(items_table)

    if order["discount_amount"]:
        story.append(Paragraph(
            f"Coin discount applied: Rs. {order['discount_amount']:.0f} "
            f"({order['coins_used']} coins)", styles["BodyText2"]
        ))

    story.append(Spacer(1, 6))
    story.append(Paragraph(f"<b>Total Paid: Rs. {order['total_amount']:.0f}</b>", ParagraphStyle(
        "TotalLine", parent=styles["BodyText2"], fontSize=13, alignment=TA_CENTER,
        textColor=rl_colors.HexColor("#073e2f"), spaceBefore=6,
    )))

    if not is_paid:
        story.append(Spacer(1, 10))
        story.append(Paragraph(
            "This is a provisional receipt. Your payment is still being verified by our team; "
            "it will be confirmed shortly. This is not a confirmation of payment.",
            ParagraphStyle("Notice", parent=styles["BodyText2"], fontSize=9, textColor=rl_colors.HexColor("#8a6d1f")),
        ))

    contact_line = f"Questions about this order? Contact {business_name}"
    if brand_phone:
        contact_line += f" — {brand_phone}"
    story.append(Spacer(1, 14))
    story.append(Paragraph(contact_line, styles["BodyText2"]))
    if brand_address:
        story.append(Paragraph(brand_address, styles["BodyText2"]))

    def _footer(canvas, doc_):
        canvas.saveState()
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(rl_colors.HexColor("#6b7268"))
        canvas.drawString(18 * mm, 10 * mm, f"Generated {datetime.now().strftime('%d %b %Y')} · {business_name}")
        canvas.drawRightString(A4[0] - 18 * mm, 10 * mm, f"Page {doc_.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    buf.seek(0)
    return buf


MAX_BULK_PDF_EXPORT = 150  # keeps bulk PDF generation from timing out the request


def merge_profile_pdfs(profiles, business_name, brand_phone=""):
    writer = PdfWriter()
    for p in profiles:
        single = generate_profile_pdf_bytes(p, business_name, brand_phone)
        reader = PdfReader(single)
        for page in reader.pages:
            writer.add_page(page)
    out = io.BytesIO()
    writer.write(out)
    out.seek(0)
    return out


@app.route("/admin/profiles")
@admin_required
def admin_profiles():
    db = get_db()
    q = request.args.get("q", "").strip()
    gender = request.args.get("gender", "").strip()
    city = request.args.get("city", "").strip()
    education = request.args.get("education", "").strip()
    profession = request.args.get("profession", "").strip()
    source = request.args.get("source", "").strip()          # self / admin
    status = request.args.get("status", "").strip()          # active / inactive
    verified = request.args.get("verified", "").strip()      # yes / no
    age_min = request.args.get("age_min", "").strip()
    age_max = request.args.get("age_max", "").strip()
    date_from = request.args.get("date_from", "").strip()
    date_to = request.args.get("date_to", "").strip()
    sort = request.args.get("sort", "newest")
    page = max(1, request.args.get("page", 1, type=int) or 1)
    per_page = 25

    where = []
    params = []
    if q:
        where.append("(profile_code LIKE ? OR name LIKE ?)")
        params += [f"%{q}%", f"%{q}%"]
    if gender:
        where.append("gender = ?"); params.append(gender)
    if city:
        where.append("city LIKE ?"); params.append(f"%{city}%")
    if education:
        where.append("education LIKE ?"); params.append(f"%{education}%")
    if profession:
        where.append("profession LIKE ?"); params.append(f"%{profession}%")
    if source in ("self", "admin"):
        where.append("registration_source = ?"); params.append(source)
    if status == "active":
        where.append("is_active = 1")
    elif status == "inactive":
        where.append("is_active = 0")
    if verified == "yes":
        where.append("admin_verified = 1")
    elif verified == "no":
        where.append("(admin_verified = 0 OR admin_verified IS NULL)")
    if age_min.isdigit():
        where.append("age >= ?"); params.append(int(age_min))
    if age_max.isdigit():
        where.append("age <= ?"); params.append(int(age_max))
    if date_from:
        where.append("created_at >= ?"); params.append(date_from)
    if date_to:
        where.append("created_at <= ?"); params.append(date_to + "T23:59:59")

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    sort_sql = {
        "newest": "created_at DESC", "oldest": "created_at ASC",
        "name": "name ASC", "age_asc": "age ASC", "age_desc": "age DESC",
    }.get(sort, "created_at DESC")

    total = db.execute(f"SELECT COUNT(*) c FROM profiles {where_sql}", params).fetchone()["c"]
    total_pages = max(1, math.ceil(total / per_page))
    page = min(page, total_pages)
    offset = (page - 1) * per_page

    profiles = db.execute(
        f"SELECT * FROM profiles {where_sql} ORDER BY {sort_sql} LIMIT ? OFFSET ?",
        params + [per_page, offset],
    ).fetchall()

    stats = {
        "total": db.execute("SELECT COUNT(*) c FROM profiles").fetchone()["c"],
        "self": db.execute("SELECT COUNT(*) c FROM profiles WHERE registration_source='self'").fetchone()["c"],
        "admin": db.execute("SELECT COUNT(*) c FROM profiles WHERE registration_source='admin'").fetchone()["c"],
        "verified": db.execute("SELECT COUNT(*) c FROM profiles WHERE admin_verified=1").fetchone()["c"],
        "active": db.execute("SELECT COUNT(*) c FROM profiles WHERE is_active=1").fetchone()["c"],
        "inactive": db.execute("SELECT COUNT(*) c FROM profiles WHERE is_active=0").fetchone()["c"],
    }

    filters = {
        "q": q, "gender": gender, "city": city, "education": education, "profession": profession,
        "source": source, "status": status, "verified": verified, "age_min": age_min, "age_max": age_max,
        "date_from": date_from, "date_to": date_to, "sort": sort,
    }
    return render_template(
        "admin_profiles.html", profiles=profiles, filters=filters, stats=stats,
        page=page, total_pages=total_pages, total=total,
    )


@app.route("/admin/profiles/<int:profile_id>/pdf")
@admin_required
def admin_profile_pdf(profile_id):
    db = get_db()
    profile = db.execute("SELECT * FROM profiles WHERE id = ?", (profile_id,)).fetchone()
    if not profile:
        abort(404)
    pdf_buf = generate_profile_pdf_bytes(
        profile, get_setting("brand_name", "Matrimonial Services"), get_setting("brand_phone", "")
    )
    log_admin_action("profile_pdf_download", profile["profile_code"])
    return send_file(
        pdf_buf, mimetype="application/pdf", as_attachment=True,
        download_name=f"{profile['profile_code']}_biodata.pdf",
    )


@app.route("/admin/profiles/export", methods=["POST"])
@admin_required
def admin_profiles_export():
    """Bulk export — Excel or PDF — of either a specific selection of
    profile IDs (checkboxes on the admin table) or 'all profiles matching
    the current filters' (the querystring is resubmitted as a hidden field)."""
    db = get_db()
    fmt = request.form.get("format", "excel")
    selected_ids = request.form.getlist("profile_ids")

    if selected_ids:
        placeholders = ",".join("?" for _ in selected_ids)
        profiles = db.execute(
            f"SELECT * FROM profiles WHERE id IN ({placeholders}) ORDER BY created_at DESC",
            [int(i) for i in selected_ids if i.isdigit()],
        ).fetchall()
    else:
        # "Export all filtered" — re-run the same filter logic against the querystring
        # that was active on the table (passed through as a hidden field).
        qs = request.form.get("filter_querystring", "")
        from urllib.parse import parse_qs
        parsed = {k: v[0] for k, v in parse_qs(qs).items()}
        where, params = [], []
        if parsed.get("q"):
            where.append("(profile_code LIKE ? OR name LIKE ?)"); params += [f"%{parsed['q']}%"] * 2
        if parsed.get("gender"):
            where.append("gender = ?"); params.append(parsed["gender"])
        if parsed.get("city"):
            where.append("city LIKE ?"); params.append(f"%{parsed['city']}%")
        if parsed.get("source") in ("self", "admin"):
            where.append("registration_source = ?"); params.append(parsed["source"])
        if parsed.get("status") == "active":
            where.append("is_active = 1")
        elif parsed.get("status") == "inactive":
            where.append("is_active = 0")
        if parsed.get("verified") == "yes":
            where.append("admin_verified = 1")
        elif parsed.get("verified") == "no":
            where.append("(admin_verified = 0 OR admin_verified IS NULL)")
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        profiles = db.execute(f"SELECT * FROM profiles {where_sql} ORDER BY created_at DESC", params).fetchall()

    if not profiles:
        flash("No profiles matched your selection/filters to export.", "error")
        return redirect(url_for("admin_profiles"))

    log_admin_action(f"bulk_export_{fmt}", f"{len(profiles)} profiles")

    if fmt == "excel":
        buf = generate_profiles_excel_bytes(profiles)
        return send_file(
            buf, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            as_attachment=True, download_name=f"profiles_export_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx",
        )

    # PDF
    if len(profiles) > MAX_BULK_PDF_EXPORT:
        flash(
            f"That's {len(profiles)} profiles — bulk PDF export is capped at {MAX_BULK_PDF_EXPORT} "
            f"at a time to avoid timing out. Please narrow your filters and try again.", "error",
        )
        return redirect(url_for("admin_profiles"))
    buf = merge_profile_pdfs(profiles, get_setting("brand_name", "Matrimonial Services"), get_setting("brand_phone", ""))
    return send_file(
        buf, mimetype="application/pdf", as_attachment=True,
        download_name=f"profiles_biodata_{datetime.now().strftime('%Y%m%d_%H%M')}.pdf",
    )


def _profile_form_fields():
    age_raw = request.form.get("age", "").strip()
    return {
        "name": request.form.get("name", "").strip()[:120],
        "age": age_raw,
        "gender": request.form.get("gender", "").strip(),
        "city": request.form.get("city", "").strip()[:120],
        "state": request.form.get("state", "").strip()[:120],
        "marital_status": request.form.get("marital_status", "").strip()[:60],
        "education": request.form.get("education", "").strip()[:150],
        "profession": request.form.get("profession", "").strip()[:150],
        "community": request.form.get("community", "").strip()[:150],
        "sect": request.form.get("sect", "").strip()[:150],
        "mother_tongue": request.form.get("mother_tongue", "").strip()[:60],
        "height": request.form.get("height", "").strip()[:30],
        "income": request.form.get("income", "").strip()[:60],
        "work_location": request.form.get("work_location", "").strip()[:150],
        "family_details": request.form.get("family_details", "").strip()[:1000],
        "bio": request.form.get("bio", "").strip()[:1500],
        "contact_number": request.form.get("contact_number", "").strip()[:20],
        "contact_visible": 1 if request.form.get("contact_visible") else 0,
        "admin_verified": 1 if request.form.get("admin_verified") else 0,
        "phone_verified": 1 if request.form.get("phone_verified") else 0,
        "photo_reviewed": 1 if request.form.get("photo_reviewed") else 0,
        "hobbies": ", ".join(h.strip() for h in request.form.getlist("hobbies") if h.strip())[:300],
        "lifestyle_drinking": request.form.get("lifestyle_drinking", "").strip()[:30],
        "lifestyle_smoking": request.form.get("lifestyle_smoking", "").strip()[:30],
        "lifestyle_tobacco": request.form.get("lifestyle_tobacco", "").strip()[:30],
        "lifestyle_namaz": request.form.get("lifestyle_namaz", "").strip()[:30],
        "lifestyle_roza": request.form.get("lifestyle_roza", "").strip()[:30],
    }


@app.route("/admin/profiles/add", methods=["GET", "POST"])
@admin_required
def admin_add_profile():
    db = get_db()
    if request.method == "POST":
        f = _profile_form_fields()
        errors = []
        if not (f["name"] and f["age"] and f["gender"] and f["city"]):
            errors.append("Name, age, gender and city are required.")
        try:
            age = int(f["age"])
            if age < 18 or age > 90:
                errors.append("Age must be between 18 and 90.")
        except ValueError:
            errors.append("Age must be a number.")
            age = None

        original_name = preview_name = None
        photo = request.files.get("photo")
        if photo and photo.filename:
            try:
                original_name, preview_name = save_profile_photo(photo)
            except ImageValidationError as e:
                errors.append(str(e))

        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("admin_add_profile.html", form=f)

        profile_code = next_profile_code(db)
        db.execute(
            """
            INSERT INTO profiles
            (profile_code, name, age, gender, city, state, marital_status, education, profession,
             community, sect, mother_tongue, height, income, work_location, family_details, bio,
             contact_number, contact_visible, admin_verified, phone_verified, photo_reviewed,
             hobbies, lifestyle_drinking, lifestyle_smoking, lifestyle_tobacco, lifestyle_namaz,
             lifestyle_roza, declaration_accepted, registration_source,
             photo_original_name, photo_preview_name, is_active, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'admin', ?, ?, 1, ?)
            """,
            (
                profile_code, f["name"], age, f["gender"], f["city"], f["state"], f["marital_status"],
                f["education"], f["profession"], f["community"], f["sect"], f["mother_tongue"], f["height"],
                f["income"], f["work_location"], f["family_details"], f["bio"], f["contact_number"],
                f["contact_visible"], f["admin_verified"], f["phone_verified"], f["photo_reviewed"],
                f["hobbies"], f["lifestyle_drinking"], f["lifestyle_smoking"], f["lifestyle_tobacco"],
                f["lifestyle_namaz"], f["lifestyle_roza"],
                original_name, preview_name, datetime.now().isoformat(),
            ),
        )
        db_commit_retry(db)
        log_admin_action("add_profile", profile_code)
        flash(f"Profile {profile_code} added.", "success")
        return redirect(url_for("admin_profiles"))

    return render_template("admin_add_profile.html", form=None)


@app.route("/admin/profiles/<int:profile_id>/edit", methods=["GET", "POST"])
@admin_required
def admin_edit_profile(profile_id):
    db = get_db()
    profile = db.execute("SELECT * FROM profiles WHERE id = ?", (profile_id,)).fetchone()
    if not profile:
        abort(404)

    if request.method == "POST":
        f = _profile_form_fields()
        errors = []
        if not (f["name"] and f["age"] and f["gender"] and f["city"]):
            errors.append("Name, age, gender and city are required.")
        try:
            age = int(f["age"])
            if age < 18 or age > 90:
                errors.append("Age must be between 18 and 90.")
        except ValueError:
            errors.append("Age must be a number.")
            age = None

        new_original, new_preview = None, None
        photo = request.files.get("photo")
        if photo and photo.filename:
            try:
                new_original, new_preview = save_profile_photo(photo)
            except ImageValidationError as e:
                errors.append(str(e))

        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("admin_edit_profile.html", profile=profile, form=f)

        if new_original:
            delete_file_quietly(PRIVATE_ORIGINALS_DIR, profile["photo_original_name"])
            delete_file_quietly(PREVIEW_DIR, profile["photo_preview_name"])
            db.execute(
                "UPDATE profiles SET photo_original_name=?, photo_preview_name=? WHERE id=?",
                (new_original, new_preview, profile_id),
            )

        db.execute(
            """
            UPDATE profiles SET name=?, age=?, gender=?, city=?, state=?, marital_status=?, education=?,
            profession=?, community=?, sect=?, mother_tongue=?, height=?, income=?, work_location=?,
            family_details=?, bio=?, contact_number=?, contact_visible=?, admin_verified=?,
            phone_verified=?, photo_reviewed=?, hobbies=?, lifestyle_drinking=?, lifestyle_smoking=?,
            lifestyle_tobacco=?, lifestyle_namaz=?, lifestyle_roza=? WHERE id=?
            """,
            (
                f["name"], age, f["gender"], f["city"], f["state"], f["marital_status"], f["education"],
                f["profession"], f["community"], f["sect"], f["mother_tongue"], f["height"], f["income"],
                f["work_location"], f["family_details"], f["bio"], f["contact_number"], f["contact_visible"],
                f["admin_verified"], f["phone_verified"], f["photo_reviewed"],
                f["hobbies"], f["lifestyle_drinking"], f["lifestyle_smoking"], f["lifestyle_tobacco"],
                f["lifestyle_namaz"], f["lifestyle_roza"], profile_id,
            ),
        )
        db_commit_retry(db)
        log_admin_action("edit_profile", profile["profile_code"])
        flash("Profile updated.", "success")
        return redirect(url_for("admin_profiles"))

    return render_template("admin_edit_profile.html", profile=profile, form=None)


@app.route("/admin/profiles/<int:profile_id>/toggle", methods=["POST"])
@admin_required
def admin_toggle_profile(profile_id):
    db = get_db()
    db.execute("UPDATE profiles SET is_active = 1 - is_active WHERE id = ?", (profile_id,))
    db_commit_retry(db)
    log_admin_action("toggle_profile", str(profile_id))
    return redirect(url_for("admin_profiles"))


@app.route("/admin/profiles/<int:profile_id>/delete", methods=["POST"])
@admin_required
def admin_delete_profile(profile_id):
    db = get_db()
    profile = db.execute("SELECT * FROM profiles WHERE id = ?", (profile_id,)).fetchone()
    if profile:
        delete_file_quietly(PRIVATE_ORIGINALS_DIR, profile["photo_original_name"])
        delete_file_quietly(PREVIEW_DIR, profile["photo_preview_name"])
        db.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))
        db_commit_retry(db)
        log_admin_action("delete_profile", profile["profile_code"])
        flash("Profile deleted.", "success")
    return redirect(url_for("admin_profiles"))


# ======================================================================
# ADMIN — UNLOCK REQUESTS
# ======================================================================
@app.route("/admin/requests")
@admin_required
def admin_requests():
    db = get_db()
    status_filter = request.args.get("status", "pending")
    query = """
        SELECT ur.*, p.profile_code, p.name AS profile_name
        FROM unlock_requests ur JOIN profiles p ON p.id = ur.profile_id
    """
    if status_filter != "all":
        query += " WHERE ur.status = ? ORDER BY ur.requested_at DESC"
        rows = db.execute(query, (status_filter,)).fetchall()
    else:
        query += " ORDER BY ur.requested_at DESC"
        rows = db.execute(query).fetchall()

    # Group rows that share a package_code into one bundle for display, so
    # the admin sees "3-profile package" as one card instead of 3 separate
    # identical-looking rows.
    packages = {}
    singles = []
    for r in rows:
        if r["package_code"]:
            packages.setdefault(r["package_code"], []).append(r)
        else:
            singles.append(r)

    return render_template("admin_requests.html", singles=singles, packages=packages, status_filter=status_filter)


@app.route("/admin/payment-proof/<int:request_id>")
@admin_required
def admin_payment_proof(request_id):
    db = get_db()
    row = db.execute("SELECT * FROM unlock_requests WHERE id = ?", (request_id,)).fetchone()
    if not row or not row["payment_proof_name"]:
        abort(404)
    resp = send_from_directory(PRIVATE_PROOFS_DIR, row["payment_proof_name"])
    resp.headers["Cache-Control"] = "no-store, private"
    return resp


@app.route("/admin/shop/orders/<int:order_id>/payment-proof")
@admin_required
def admin_shop_payment_proof(order_id):
    db = get_db()
    row = db.execute("SELECT * FROM shop_orders WHERE id = ?", (order_id,)).fetchone()
    if not row or not row["payment_proof_name"]:
        abort(404)
    resp = send_from_directory(PRIVATE_PROOFS_DIR, row["payment_proof_name"])
    resp.headers["Cache-Control"] = "no-store, private"
    return resp


@app.route("/admin/requests/<int:request_id>/<action>", methods=["POST"])
@admin_required
def admin_decide_request(request_id, action):
    if action not in ("unlock", "reject"):
        abort(400)
    db = get_db()
    new_status = "unlocked" if action == "unlock" else "rejected"
    row = db.execute("SELECT * FROM unlock_requests WHERE id = ?", (request_id,)).fetchone()
    db.execute(
        "UPDATE unlock_requests SET status = ?, decided_at = ? WHERE id = ?",
        (new_status, datetime.now().isoformat(), request_id),
    )
    db_commit_retry(db)
    if action == "reject" and row and row["coins_used"]:
        refund_coins(row["user_phone"], row["coins_used"], "unlock_reject_refund", str(request_id))
    log_admin_action(f"request_{action}", str(request_id))
    flash(f"Request marked as {new_status}.", "success")
    return redirect(url_for("admin_requests"))


@app.route("/admin/requests/package/<package_code>/<action>", methods=["POST"])
@admin_required
def admin_decide_package(package_code, action):
    if action not in ("unlock", "reject"):
        abort(400)
    db = get_db()
    new_status = "unlocked" if action == "unlock" else "rejected"
    rows = db.execute("SELECT * FROM unlock_requests WHERE package_code = ?", (package_code,)).fetchall()
    db.execute(
        "UPDATE unlock_requests SET status = ?, decided_at = ? WHERE package_code = ?",
        (new_status, datetime.now().isoformat(), package_code),
    )
    db_commit_retry(db)
    if action == "reject":
        for row in rows:
            if row["coins_used"]:
                refund_coins(row["user_phone"], row["coins_used"], "package_reject_refund", package_code)
    log_admin_action(f"package_{action}", package_code)
    flash(f"Package marked as {new_status}.", "success")
    return redirect(url_for("admin_requests"))


# ======================================================================
# ADMIN — SELF REGISTRATIONS ("Register Yourself" ₹11 flow)
# ======================================================================
@app.route("/admin/registrations")
@admin_required
def admin_registrations():
    db = get_db()
    status_filter = request.args.get("status", "pending")
    if status_filter != "all":
        rows = db.execute(
            "SELECT * FROM self_registrations WHERE status = ? ORDER BY requested_at DESC", (status_filter,)
        ).fetchall()
    else:
        rows = db.execute("SELECT * FROM self_registrations ORDER BY requested_at DESC").fetchall()
    return render_template("admin_registrations.html", rows=rows, status_filter=status_filter)


@app.route("/admin/registrations/<int:reg_id>/proof")
@admin_required
def admin_registration_proof(reg_id):
    db = get_db()
    row = db.execute("SELECT * FROM self_registrations WHERE id = ?", (reg_id,)).fetchone()
    if not row or not row["payment_proof_name"]:
        abort(404)
    resp = send_from_directory(PRIVATE_PROOFS_DIR, row["payment_proof_name"])
    resp.headers["Cache-Control"] = "no-store, private"
    return resp


@app.route("/admin/registrations/<int:reg_id>/<action>", methods=["POST"])
@admin_required
def admin_decide_registration(reg_id, action):
    if action not in ("approve", "reject"):
        abort(400)
    db = get_db()
    row = db.execute("SELECT * FROM self_registrations WHERE id = ?", (reg_id,)).fetchone()
    if not row:
        abort(404)

    if action == "reject":
        db.execute(
            "UPDATE self_registrations SET status = 'rejected', decided_at = ? WHERE id = ?",
            (datetime.now().isoformat(), reg_id),
        )
        db_commit_retry(db)
        log_admin_action("registration_reject", str(reg_id))
        flash("Registration rejected.", "success")
        return redirect(url_for("admin_registrations"))

    # Approve: turn this self-registration into a real, browsable profile —
    # reusing the exact same profiles table every admin-added profile uses.
    profile_code = next_profile_code(db)
    db.execute(
        """
        INSERT INTO profiles
        (profile_code, name, age, gender, city, marital_status, education, profession,
         bio, contact_number, contact_visible, admin_verified, phone_verified, photo_reviewed,
         hobbies, lifestyle_drinking, lifestyle_smoking, lifestyle_tobacco, lifestyle_namaz,
         lifestyle_roza, declaration_accepted, registration_source,
         photo_original_name, photo_preview_name, is_active, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0, 0, 0, ?, ?, ?, ?, ?, ?, ?, 'self', ?, ?, 1, ?)
        """,
        (profile_code, row["name"], row["age"], row["gender"], row["city"], row["marital_status"],
         row["education"], row["profession"], row["bio"], row["phone"],
         row["hobbies"], row["lifestyle_drinking"], row["lifestyle_smoking"], row["lifestyle_tobacco"],
         row["lifestyle_namaz"], row["lifestyle_roza"], row["declaration_accepted"],
         row["photo_original_name"], row["photo_preview_name"], datetime.now().isoformat()),
    )
    new_profile = db.execute("SELECT id FROM profiles WHERE profile_code = ?", (profile_code,)).fetchone()
    db.execute(
        "UPDATE self_registrations SET status = 'approved', decided_at = ?, profile_id = ? WHERE id = ?",
        (datetime.now().isoformat(), new_profile["id"], reg_id),
    )
    db_commit_retry(db)
    log_admin_action("registration_approve", f"{reg_id} -> {profile_code}")

    bonus = registration_bonus_coins()
    if bonus > 0:
        credit_coins(row["phone"], bonus, "registration_bonus", profile_code)

    flash(f"Registration approved — profile {profile_code} is now live. You can review/edit it any time. "
          f"{bonus} Match Coins credited to {row['phone']}.", "success")
    return redirect(url_for("admin_registrations"))


# ======================================================================
# ADMIN — HELP SHADI
# ======================================================================
HELP_SHADI_STATUSES = ("pending", "under_review", "approved", "rejected", "completed")


@app.route("/admin/help-shadi")
@admin_required
def admin_help_shadi():
    db = get_db()
    status_filter = request.args.get("status", "pending")
    if status_filter != "all":
        rows = db.execute(
            "SELECT * FROM help_shadi_requests WHERE status = ? ORDER BY created_at DESC", (status_filter,)
        ).fetchall()
    else:
        rows = db.execute("SELECT * FROM help_shadi_requests ORDER BY created_at DESC").fetchall()
    return render_template("admin_help_shadi.html", rows=rows, status_filter=status_filter,
                            statuses=HELP_SHADI_STATUSES)


@app.route("/admin/help-shadi/<int:req_id>/document")
@admin_required
def admin_help_shadi_document(req_id):
    db = get_db()
    row = db.execute("SELECT * FROM help_shadi_requests WHERE id = ?", (req_id,)).fetchone()
    if not row or not row["document_filename"]:
        abort(404)
    resp = send_from_directory(PRIVATE_HELP_SHADI_DOCS_DIR, row["document_filename"])
    resp.headers["Cache-Control"] = "no-store, private"
    return resp


@app.route("/admin/help-shadi/<int:req_id>/<action>", methods=["POST"])
@admin_required
def admin_decide_help_shadi(req_id, action):
    if action not in HELP_SHADI_STATUSES:
        abort(400)
    db = get_db()
    row = db.execute("SELECT * FROM help_shadi_requests WHERE id = ?", (req_id,)).fetchone()
    if not row:
        abort(404)

    admin_notes = request.form.get("admin_notes", "").strip()[:800]
    decided_at = datetime.now().isoformat() if action in ("approved", "rejected", "completed") else row["decided_at"]
    db.execute(
        "UPDATE help_shadi_requests SET status = ?, admin_notes = ?, decided_at = ? WHERE id = ?",
        (action, admin_notes or row["admin_notes"], decided_at, req_id),
    )
    db_commit_retry(db)
    log_admin_action(f"help_shadi_{action}", str(req_id))
    flash(f"Help Shadi request marked as {action.replace('_', ' ')}.", "success")
    return redirect(url_for("admin_help_shadi", status=request.args.get("status", "pending")))


# ======================================================================
# ADMIN — "REQUEST MORE PHOTOS" QUEUE
# ======================================================================
@app.route("/admin/photo-requests")
@admin_required
def admin_photo_requests():
    db = get_db()
    status_filter = request.args.get("status", "pending")
    query = """
        SELECT pr.*, p.profile_code, p.name AS profile_name
        FROM photo_requests pr JOIN profiles p ON p.id = pr.profile_id
    """
    if status_filter != "all":
        query += " WHERE pr.status = ? ORDER BY pr.requested_at DESC"
        rows = db.execute(query, (status_filter,)).fetchall()
    else:
        query += " ORDER BY pr.requested_at DESC"
        rows = db.execute(query).fetchall()
    return render_template("admin_photo_requests.html", rows=rows, status_filter=status_filter)


@app.route("/admin/photo-requests/<int:req_id>/<action>", methods=["POST"])
@admin_required
def admin_decide_photo_request(req_id, action):
    if action not in ("fulfilled", "rejected"):
        abort(400)
    db = get_db()
    db.execute(
        "UPDATE photo_requests SET status = ?, decided_at = ? WHERE id = ?",
        (action, datetime.now().isoformat(), req_id),
    )
    db_commit_retry(db)
    log_admin_action(f"photo_request_{action}", str(req_id))
    flash(f"Photo request marked as {action}.", "success")
    return redirect(url_for("admin_photo_requests"))


# ======================================================================
# ADMIN — WEBSITE SETTINGS (branding)
# ======================================================================
SETTINGS_TEXT_FIELDS = [
    "brand_name", "brand_tagline", "brand_phone", "whatsapp_number", "brand_email",
    "brand_location", "unlock_price", "package_price", "package_size", "package_offer_enabled",
    "registration_price", "upi_id",
    "primary_color", "secondary_color", "accent_color", "hero_heading", "hero_subheading",
    "footer_text", "coin_value_inr", "registration_bonus_coins",
    "help_shadi_display_name",
    "instagram_url", "facebook_url", "youtube_url", "telegram_url",
]

# Background images the admin can upload from Appearance Settings without
# touching any code/CSS. Each maps a settings key -> the CSS variable that
# picks it up (see base.html, which turns these into --*-bg-image vars).
APPEARANCE_IMAGE_FIELDS = ["nav_bg_image", "hero_bg_image", "section_bg_image", "footer_bg_image"]

SOCIAL_URL_FIELDS = ["instagram_url", "facebook_url", "youtube_url", "telegram_url"]


def _sanitize_social_url(value):
    """Only ever allow http(s) links to be saved as a social URL — these
    render straight into an href attribute, so a javascript: or data:
    scheme here would be a stored-XSS vector even though only an admin
    can set it. Empty stays empty (hides the icon)."""
    value = (value or "").strip()
    if not value:
        return ""
    if not re.match(r"^https?://", value, re.IGNORECASE):
        return ""
    return value[:300]


@app.route("/admin/settings", methods=["GET", "POST"])
@admin_required
def admin_settings():
    db = get_db()
    if request.method == "POST":
        for key in SETTINGS_TEXT_FIELDS:
            value = request.form.get(key, "").strip()[:500]
            if key in SOCIAL_URL_FIELDS:
                value = _sanitize_social_url(value)
            db.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                       "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

        logo = request.files.get("logo")
        if logo and logo.filename:
            try:
                name = save_generic_image(logo, BRANDING_DIR, max_dim=600)
                if name:
                    db.execute("INSERT INTO settings (key, value) VALUES ('logo_image', ?) "
                               "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (name,))
            except ImageValidationError as e:
                flash(f"Logo not saved: {e}", "error")

        favicon = request.files.get("favicon")
        if favicon and favicon.filename:
            try:
                name = save_generic_image(favicon, BRANDING_DIR, max_dim=256)
                if name:
                    db.execute("INSERT INTO settings (key, value) VALUES ('favicon_image', ?) "
                               "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (name,))
            except ImageValidationError as e:
                flash(f"Favicon not saved: {e}", "error")

        qr = request.files.get("qr_image")
        if qr and qr.filename:
            try:
                name = save_generic_image(qr, BRANDING_DIR, max_dim=800)
                if name:
                    db.execute("INSERT INTO settings (key, value) VALUES ('qr_image', ?) "
                               "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (name,))
            except ImageValidationError as e:
                flash(f"QR code not saved: {e}", "error")

        # Appearance backgrounds — admin can set these without touching any
        # code. Each is optional; leaving the field empty keeps the current
        # image (or the CSS default gradient if none was ever set).
        for field in APPEARANCE_IMAGE_FIELDS:
            f = request.files.get(field)
            if f and f.filename:
                try:
                    name = save_generic_image(f, BRANDING_DIR, max_dim=2000)
                    if name:
                        db.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (field, name))
                except ImageValidationError as e:
                    flash(f"{field.replace('_', ' ').title()} not saved: {e}", "error")
            if request.form.get(f"clear_{field}") == "1":
                db.execute("INSERT INTO settings (key, value) VALUES (?, '') "
                           "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (field,))

        db_commit_retry(db)
        log_admin_action("update_settings")
        flash("Website settings updated.", "success")
        return redirect(url_for("admin_settings"))

    return render_template("admin_settings.html", settings=load_settings())


def save_font_file(file_storage):
    if not file_storage or not file_storage.filename:
        return None
    ext = file_storage.filename.rsplit(".", 1)[-1].lower() if "." in file_storage.filename else ""
    if ext not in ALLOWED_FONT_EXT:
        raise ImageValidationError("Unsupported font file. Use WOFF2, WOFF or TTF.")
    file_storage.stream.seek(0, os.SEEK_END)
    size = file_storage.stream.tell()
    file_storage.stream.seek(0)
    if size > MAX_FONT_BYTES:
        raise ImageValidationError("Font file is too large (max 5 MB).")
    if size == 0:
        raise ImageValidationError("Empty font file.")
    name = secrets.token_hex(12) + "." + ext
    file_storage.save(os.path.join(FONT_DIR, name))
    return name


@app.route("/admin/settings/appearance", methods=["GET", "POST"])
@admin_required
def admin_appearance():
    db = get_db()
    if request.method == "POST":
        action = request.form.get("action", "save_theme")

        if action == "apply_preset":
            preset_key = request.form.get("preset")
            preset = THEME_PRESETS.get(preset_key)
            if not preset:
                flash("Unknown theme preset.", "error")
                return redirect(url_for("admin_appearance"))
            for key in THEME_COLOR_KEYS:
                db.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                           "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, preset[key]))
            db.execute("INSERT INTO settings (key, value) VALUES ('theme_preset', ?) "
                       "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (preset_key,))
            db_commit_retry(db)
            log_admin_action("apply_theme_preset", preset_key)
            flash(f"Applied \"{preset['label']}\" theme to the whole site.", "success")
            return redirect(url_for("admin_appearance"))

        if action == "reset_theme":
            preset = THEME_PRESETS["emerald_champagne"]
            for key in THEME_COLOR_KEYS:
                db.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                           "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, preset[key]))
            db.execute("INSERT INTO settings (key, value) VALUES ('theme_preset', 'emerald_champagne') "
                       "ON CONFLICT(key) DO UPDATE SET value=excluded.value")
            db.execute("INSERT INTO settings (key, value) VALUES ('font_heading', 'Playfair Display') "
                       "ON CONFLICT(key) DO UPDATE SET value=excluded.value")
            db.execute("INSERT INTO settings (key, value) VALUES ('font_body', 'Inter') "
                       "ON CONFLICT(key) DO UPDATE SET value=excluded.value")
            db.execute("INSERT INTO settings (key, value) VALUES ('font_button', 'Inter') "
                       "ON CONFLICT(key) DO UPDATE SET value=excluded.value")
            db_commit_retry(db)
            log_admin_action("reset_theme")
            flash("Theme reset to default.", "success")
            return redirect(url_for("admin_appearance"))

        if action == "save_theme":
            errors = []
            values = {}
            for key in THEME_COLOR_KEYS:
                val = request.form.get(key, "").strip()
                if not is_valid_hex(val):
                    errors.append(f"{key.replace('_', ' ').title()} is not a valid hex color.")
                    continue
                values[key] = val
            # Contrast safety: text-on-background and text-on-card must stay readable.
            if "text_color" in values and "background_color" in values:
                ratio = contrast_ratio(values["text_color"], values["background_color"])
                if ratio is not None and ratio < 4.5:
                    errors.append(f"Low contrast — Text Color on Background Color is only {ratio}:1 (need 4.5:1). Please choose another color.")
            if "text_color" in values and "card_color" in values:
                ratio = contrast_ratio(values["text_color"], values["card_color"])
                if ratio is not None and ratio < 4.5:
                    errors.append(f"Low contrast — Text Color on Card Color is only {ratio}:1 (need 4.5:1). Please choose another color.")
            if errors:
                for e in errors:
                    flash(e, "error")
                return redirect(url_for("admin_appearance"))

            for key, val in values.items():
                db.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                           "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, val))
            db.execute("INSERT INTO settings (key, value) VALUES ('theme_preset', 'custom') "
                       "ON CONFLICT(key) DO UPDATE SET value=excluded.value")

            for key in ("font_heading", "font_body", "font_button"):
                val = request.form.get(key, "").strip()[:120]
                if val:
                    db.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                               "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, val))

            db_commit_retry(db)
            log_admin_action("update_theme")
            flash("Theme saved and applied to the whole site.", "success")
            return redirect(url_for("admin_appearance"))

        if action == "upload_font":
            label = request.form.get("font_label", "").strip()[:80]
            weight = request.form.get("font_weight", "400")
            font_file = request.files.get("font_file")
            if not label:
                flash("Give the font a name first.", "error")
                return redirect(url_for("admin_appearance"))
            try:
                name = save_font_file(font_file)
                if not name:
                    flash("Choose a font file to upload.", "error")
                    return redirect(url_for("admin_appearance"))
                db.execute(
                    "INSERT INTO custom_fonts (label, filename, weight, created_at) VALUES (?, ?, ?, ?)",
                    (label, name, weight, datetime.now().isoformat()),
                )
                db_commit_retry(db)
                log_admin_action("upload_font", label)
                flash(f'Font "{label}" uploaded. Select it from the dropdown above.', "success")
            except ImageValidationError as e:
                flash(f"Font not uploaded: {e}", "error")
            return redirect(url_for("admin_appearance"))

        if action == "delete_font":
            font_id = request.form.get("font_id")
            row = db.execute("SELECT * FROM custom_fonts WHERE id = ?", (font_id,)).fetchone()
            if row:
                delete_file_quietly(FONT_DIR, row["filename"])
                db.execute("DELETE FROM custom_fonts WHERE id = ?", (font_id,))
                db_commit_retry(db)
                log_admin_action("delete_font", row["label"])
                flash("Font removed.", "success")
            return redirect(url_for("admin_appearance"))

    settings = load_settings()
    return render_template(
        "admin_appearance.html",
        settings=settings,
        presets=THEME_PRESETS,
        web_safe_fonts=WEB_SAFE_FONTS,
        custom_fonts=list_custom_fonts(),
    )


# ======================================================================
# ADMIN — BANNERS
# ======================================================================
@app.route("/admin/banners")
@admin_required
def admin_banners():
    db = get_db()
    banners = db.execute("SELECT * FROM banners ORDER BY slot, sort_order, id DESC").fetchall()
    return render_template("admin_banners.html", banners=banners)


@app.route("/admin/banners/add", methods=["GET", "POST"])
@admin_required
def admin_add_banner():
    if request.method == "POST":
        slot = request.form.get("slot", "hero")
        title = request.form.get("title", "").strip()[:150]
        link_url = request.form.get("link_url", "").strip()[:500]
        start_date = request.form.get("start_date", "").strip()
        end_date = request.form.get("end_date", "").strip()
        try:
            sort_order = int(request.form.get("sort_order", "0"))
        except ValueError:
            sort_order = 0

        image = request.files.get("image")
        if not image or not image.filename:
            flash("Please choose a banner image.", "error")
            return render_template("admin_banner_form.html", banner=None)
        try:
            image_name = save_generic_image(image, BANNERS_DIR, max_dim=1600)
        except ImageValidationError as e:
            flash(str(e), "error")
            return render_template("admin_banner_form.html", banner=None)

        db = get_db()
        db.execute(
            """INSERT INTO banners (slot, title, link_url, image_name, is_active, start_date, end_date,
               sort_order, created_at) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?)""",
            (slot, title, link_url, image_name, start_date or None, end_date or None,
             sort_order, datetime.now().isoformat()),
        )
        db_commit_retry(db)
        log_admin_action("add_banner", slot)
        flash("Banner added.", "success")
        return redirect(url_for("admin_banners"))

    return render_template("admin_banner_form.html", banner=None)


@app.route("/admin/banners/<int:banner_id>/toggle", methods=["POST"])
@admin_required
def admin_toggle_banner(banner_id):
    db = get_db()
    db.execute("UPDATE banners SET is_active = 1 - is_active WHERE id = ?", (banner_id,))
    db_commit_retry(db)
    log_admin_action("toggle_banner", str(banner_id))
    return redirect(url_for("admin_banners"))


@app.route("/admin/banners/<int:banner_id>/delete", methods=["POST"])
@admin_required
def admin_delete_banner(banner_id):
    db = get_db()
    banner = db.execute("SELECT * FROM banners WHERE id = ?", (banner_id,)).fetchone()
    if banner:
        delete_file_quietly(BANNERS_DIR, banner["image_name"])
        db.execute("DELETE FROM banners WHERE id = ?", (banner_id,))
        db_commit_retry(db)
        log_admin_action("delete_banner", str(banner_id))
        flash("Banner deleted.", "success")
    return redirect(url_for("admin_banners"))


# ======================================================================
# ISLAMIC SHOP — PUBLIC FRONTEND
# ======================================================================
SHOP_IMAGES_DIR = os.path.join(BASE_DIR, "static", "shop")
os.makedirs(SHOP_IMAGES_DIR, exist_ok=True)


def shop_categories_list(db):
    return db.execute("SELECT * FROM shop_categories ORDER BY sort_order, name").fetchall()


@app.route("/shop")
def shop_index():
    db = get_db()
    categories = shop_categories_list(db)
    featured = db.execute(
        "SELECT * FROM shop_products WHERE status = 'active' AND featured = 1 ORDER BY updated_at DESC LIMIT 8"
    ).fetchall()
    cat_slug = request.args.get("category", "").strip()
    q = request.args.get("q", "").strip()

    query = "SELECT p.*, c.name as category_name FROM shop_products p LEFT JOIN shop_categories c ON c.id = p.category_id WHERE p.status = 'active'"
    params = []
    if cat_slug:
        query += " AND c.slug = ?"
        params.append(cat_slug)
    if q:
        query += " AND (p.name LIKE ? OR p.description LIKE ?)"
        params += [f"%{q}%", f"%{q}%"]
    query += " ORDER BY p.featured DESC, p.created_at DESC"
    products = db.execute(query, params).fetchall()

    return render_template(
        "shop_index.html", categories=categories, featured=featured, products=products,
        active_category=cat_slug, q=q,
    )


@app.route("/shop/product/<slug>")
def shop_product_detail(slug):
    db = get_db()
    product = db.execute(
        "SELECT p.*, c.name as category_name, c.slug as category_slug FROM shop_products p "
        "LEFT JOIN shop_categories c ON c.id = p.category_id WHERE p.slug = ? AND p.status = 'active'",
        (slug,),
    ).fetchone()
    if not product:
        abort(404)
    variants = db.execute("SELECT * FROM shop_variants WHERE product_id = ? ORDER BY id", (product["id"],)).fetchall()
    related = db.execute(
        "SELECT * FROM shop_products WHERE category_id = ? AND id != ? AND status = 'active' LIMIT 4",
        (product["category_id"], product["id"]),
    ).fetchall()
    return render_template("shop_product.html", product=product, variants=variants, related=related)


@app.route("/cart")
def shop_cart_view():
    db = get_db()
    items, total = cart_details(db)
    return render_template("shop_cart.html", items=items, total=total)


@app.route("/cart/add", methods=["POST"])
def shop_cart_add():
    db = get_db()
    product_id = request.form.get("product_id", type=int)
    variant_id = request.form.get("variant_id", type=int) or None
    qty = max(1, request.form.get("qty", default=1, type=int) or 1)

    product = db.execute("SELECT * FROM shop_products WHERE id = ? AND status = 'active'", (product_id,)).fetchone()
    if not product:
        flash("This product is no longer available.", "error")
        return redirect(url_for("shop_index"))

    if product["has_variants"]:
        variant = db.execute("SELECT * FROM shop_variants WHERE id = ? AND product_id = ?", (variant_id, product_id)).fetchone()
        if not variant:
            flash("Please choose a size/colour option.", "error")
            return redirect(url_for("shop_product_detail", slug=product["slug"]))
        available = variant["stock"]
    else:
        variant_id = None
        available = product["stock"]

    if available <= 0:
        flash("Sorry, this item is out of stock.", "error")
        return redirect(url_for("shop_product_detail", slug=product["slug"]))

    cart = get_cart()
    for entry in cart:
        if entry["product_id"] == product_id and entry.get("variant_id") == variant_id:
            entry["qty"] = min(entry["qty"] + qty, available)
            break
    else:
        cart.append({"product_id": product_id, "variant_id": variant_id, "qty": min(qty, available)})
    save_cart(cart)
    flash(f'Added "{product["name"]}" to your cart.', "success")
    return redirect(request.referrer or url_for("shop_index"))


@app.route("/cart/update", methods=["POST"])
def shop_cart_update():
    db = get_db()
    product_id = request.form.get("product_id", type=int)
    variant_id = request.form.get("variant_id", type=int) or None
    qty = request.form.get("qty", type=int)
    cart = get_cart()
    if qty is not None and qty <= 0:
        cart = [e for e in cart if not (e["product_id"] == product_id and e.get("variant_id") == variant_id)]
    else:
        for entry in cart:
            if entry["product_id"] == product_id and entry.get("variant_id") == variant_id:
                entry["qty"] = qty
    save_cart(cart)
    return redirect(url_for("shop_cart_view"))


@app.route("/cart/remove", methods=["POST"])
def shop_cart_remove():
    product_id = request.form.get("product_id", type=int)
    variant_id = request.form.get("variant_id", type=int) or None
    cart = [e for e in get_cart() if not (e["product_id"] == product_id and e.get("variant_id") == variant_id)]
    save_cart(cart)
    flash("Item removed from cart.", "success")
    return redirect(url_for("shop_cart_view"))


@app.route("/checkout", methods=["GET", "POST"])
def shop_checkout():
    db = get_db()
    items, total = cart_details(db)
    if not items:
        flash("Your cart is empty.", "error")
        return redirect(url_for("shop_index"))

    if request.method == "POST":
        customer_name = request.form.get("customer_name", "").strip()[:120]
        customer_phone = request.form.get("customer_phone", "").strip()
        customer_address = request.form.get("customer_address", "").strip()[:500]
        consent = request.form.get("consent")

        errors = []
        if not customer_name:
            errors.append("Please enter your name.")
        if not valid_indian_phone(customer_phone):
            errors.append("Please enter a valid 10-digit Indian mobile number.")
        if not customer_address:
            errors.append("Please enter your shipping address.")
        if not consent:
            errors.append("Please confirm you have completed the payment.")

        # Match Coins redemption — only against the OTP-verified session phone.
        coins_requested = request.form.get("use_coins", type=int) or 0
        coins_used = 0
        discount_amount = 0.0
        wallet_phone = verified_phone()
        if coins_requested > 0:
            if not wallet_phone or wallet_phone != customer_phone:
                errors.append("Verify this phone number (via My Requests) to use its Match Coins.")
            else:
                max_coins_by_value = int(total / coin_value_inr()) if coin_value_inr() > 0 else 0
                coins_used = max(0, min(coins_requested, get_wallet_balance(wallet_phone), max_coins_by_value))
                discount_amount = round(coins_used * coin_value_inr(), 2)

        proof_file = request.files.get("payment_proof")
        proof_name = None
        if not proof_file or not proof_file.filename:
            errors.append("Please upload your payment screenshot.")
        else:
            try:
                proof_name = save_payment_proof(proof_file)
            except ImageValidationError as e:
                errors.append(str(e))

        # Re-validate the cart against the live DB right before committing —
        # stock/price could have changed since the page was rendered, and
        # money-related decisions must never trust anything stale.
        items, total = cart_details(db)
        if not items:
            errors.append("Your cart is empty.")

        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("shop_checkout.html", items=items, total=total)

        if coins_used > 0:
            if not debit_coins(wallet_phone, coins_used, "shop_redeem", None):
                flash("Your coin balance changed — please retry.", "error")
                return render_template("shop_checkout.html", items=items, total=total)

        order_code = "ORD" + secrets.token_hex(4).upper()
        now = datetime.now().isoformat()
        final_total = round(max(total - discount_amount, 0), 2)
        cur = db.execute(
            "INSERT INTO shop_orders (order_code, customer_name, customer_phone, customer_address, "
            "total_amount, payment_status, order_status, payment_proof_name, coins_used, discount_amount, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'pending', 'pending', ?, ?, ?, ?, ?)",
            (order_code, customer_name, customer_phone, customer_address, final_total, proof_name,
             coins_used, discount_amount, now, now),
        )
        order_id = cur.lastrowid

        for item in items:
            product = item["product"]
            variant = item["variant"]
            db.execute(
                "INSERT INTO shop_order_items (order_id, product_id, variant_id, product_name, "
                "variant_label, unit_price, qty) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (order_id, product["id"], variant["id"] if variant else None, product["name"],
                 (f"{variant['size'] or ''} {variant['color'] or ''}".strip() if variant else None),
                 item["unit_price"], item["qty"]),
            )
            # Reserve stock immediately so two customers can't both buy the
            # last unit while payment proofs are being manually reviewed.
            # Restored automatically if the admin cancels/rejects the order.
            if variant:
                db.execute("UPDATE shop_variants SET stock = MAX(stock - ?, 0) WHERE id = ?", (item["qty"], variant["id"]))
            else:
                db.execute("UPDATE shop_products SET stock = MAX(stock - ?, 0) WHERE id = ?", (item["qty"], product["id"]))

        db_commit_retry(db)
        save_cart([])

        notify_admin(
            "New Islamic Shop order",
            f"{customer_name} ({customer_phone}) placed order {order_code} for ₹{final_total}"
            f"{f' (after ₹{discount_amount} coin discount, {coins_used} coins used)' if coins_used else ''}. "
            f"Review: {request.url_root.rstrip('/')}{url_for('admin_shop_orders')}",
        )
        create_admin_event(
            "shop_order",
            f"New shop order — {order_code}",
            f"{customer_name} ({customer_phone}) for ₹{final_total}.",
            order_code,
            url_for("admin_shop_orders"),
        )
        return redirect(url_for("shop_order_status", order_code=order_code))

    return render_template("shop_checkout.html", items=items, total=total)


@app.route("/shop/order/<order_code>")
def shop_order_status(order_code):
    db = get_db()
    order = db.execute("SELECT * FROM shop_orders WHERE order_code = ?", (order_code,)).fetchone()
    if not order:
        abort(404)
    items = db.execute("SELECT * FROM shop_order_items WHERE order_id = ?", (order["id"],)).fetchall()
    return render_template("shop_order_status.html", order=order, items=items)


@app.route("/shop/order/<order_code>/invoice")
def shop_order_invoice(order_code):
    """Public invoice/receipt download — gated only by knowledge of the
    order_code, same trust model as shop_order_status above (the code is
    the customer's reference number, shown to them right after checkout
    and never listed publicly anywhere)."""
    db = get_db()
    order = db.execute("SELECT * FROM shop_orders WHERE order_code = ?", (order_code,)).fetchone()
    if not order:
        abort(404)
    items = db.execute("SELECT * FROM shop_order_items WHERE order_id = ?", (order["id"],)).fetchall()
    pdf_buf = generate_shop_invoice_pdf_bytes(
        order, items, get_setting("brand_name", "Matrimonial Services"),
        get_setting("brand_phone", ""), get_setting("brand_location", ""),
    )
    return send_file(
        pdf_buf, mimetype="application/pdf", as_attachment=True,
        download_name=f"{order['order_code']}_invoice.pdf",
    )


@app.route("/track-order", methods=["GET", "POST"])
def track_order():
    """Self-service order lookup. shop_order_status already shows full
    status/timeline for a known order_code — this page exists so a
    customer who didn't bookmark that link (or is on a different device)
    can find their order again just by typing the code they were given
    at checkout."""
    if request.method == "POST":
        code = request.form.get("order_code", "").strip().upper()
        if not code:
            flash("Please enter your Order ID.", "error")
            return render_template("track_order.html")
        db = get_db()
        order = db.execute("SELECT order_code FROM shop_orders WHERE order_code = ?", (code,)).fetchone()
        if not order:
            flash("We couldn't find an order with that ID. Please check and try again.", "error")
            return render_template("track_order.html")
        return redirect(url_for("shop_order_status", order_code=order["order_code"]))
    return render_template("track_order.html")


# ======================================================================
# ADMIN — SUCCESS STORY TESTIMONIALS (homepage showcase)
# ======================================================================
TESTIMONIALS_DIR = os.path.join(BASE_DIR, "static", "testimonials")
os.makedirs(TESTIMONIALS_DIR, exist_ok=True)


@app.route("/admin/testimonials")
@admin_required
def admin_testimonials():
    db = get_db()
    rows = db.execute("SELECT * FROM testimonials ORDER BY is_featured DESC, sort_order, created_at DESC").fetchall()
    return render_template("admin_testimonials.html", rows=rows)


@app.route("/admin/testimonials/add", methods=["GET", "POST"])
@admin_required
def admin_testimonial_add():
    if request.method == "POST":
        groom_name = request.form.get("groom_name", "").strip()[:100]
        bride_name = request.form.get("bride_name", "").strip()[:100]
        if not groom_name or not bride_name:
            flash("Groom and Bride names are required.", "error")
            return render_template("admin_testimonial_form.html", t=None)

        photo_name = None
        photo_file = request.files.get("photo")
        if photo_file and photo_file.filename:
            try:
                photo_name = save_generic_image(photo_file, TESTIMONIALS_DIR, max_dim=1000)
            except ImageValidationError as e:
                flash(f"Photo not saved: {e}", "error")

        db = get_db()
        db.execute(
            "INSERT INTO testimonials (groom_name, bride_name, event_date, photo_filename, rating, "
            "short_quote, full_story, is_featured, is_approved, sort_order, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
            (groom_name, bride_name, request.form.get("event_date", "").strip(), photo_name,
             request.form.get("rating", 5, type=int) or 5, request.form.get("short_quote", "").strip()[:300],
             request.form.get("full_story", "").strip()[:3000],
             1 if request.form.get("is_featured") == "1" else 0,
             request.form.get("sort_order", 0, type=int) or 0, datetime.now().isoformat()),
        )
        db_commit_retry(db)
        log_admin_action("add_testimonial", f"{groom_name} & {bride_name}")
        flash("Success story added.", "success")
        return redirect(url_for("admin_testimonials"))

    return render_template("admin_testimonial_form.html", t=None)


@app.route("/admin/testimonials/<int:t_id>/edit", methods=["GET", "POST"])
@admin_required
def admin_testimonial_edit(t_id):
    db = get_db()
    t = db.execute("SELECT * FROM testimonials WHERE id = ?", (t_id,)).fetchone()
    if not t:
        abort(404)

    if request.method == "POST":
        groom_name = request.form.get("groom_name", "").strip()[:100]
        bride_name = request.form.get("bride_name", "").strip()[:100]
        if not groom_name or not bride_name:
            flash("Groom and Bride names are required.", "error")
            return render_template("admin_testimonial_form.html", t=t)

        photo_name = t["photo_filename"]
        photo_file = request.files.get("photo")
        if photo_file and photo_file.filename:
            try:
                photo_name = save_generic_image(photo_file, TESTIMONIALS_DIR, max_dim=1000)
                if t["photo_filename"]:
                    delete_file_quietly(TESTIMONIALS_DIR, t["photo_filename"])
            except ImageValidationError as e:
                flash(f"Photo not updated: {e}", "error")

        db.execute(
            "UPDATE testimonials SET groom_name=?, bride_name=?, event_date=?, photo_filename=?, rating=?, "
            "short_quote=?, full_story=?, is_featured=?, sort_order=? WHERE id=?",
            (groom_name, bride_name, request.form.get("event_date", "").strip(), photo_name,
             request.form.get("rating", 5, type=int) or 5, request.form.get("short_quote", "").strip()[:300],
             request.form.get("full_story", "").strip()[:3000],
             1 if request.form.get("is_featured") == "1" else 0,
             request.form.get("sort_order", 0, type=int) or 0, t_id),
        )
        db_commit_retry(db)
        log_admin_action("edit_testimonial", f"{groom_name} & {bride_name}")
        flash("Success story updated.", "success")
        return redirect(url_for("admin_testimonials"))

    return render_template("admin_testimonial_form.html", t=t)


@app.route("/admin/testimonials/<int:t_id>/toggle-approved", methods=["POST"])
@admin_required
def admin_testimonial_toggle_approved(t_id):
    db = get_db()
    t = db.execute("SELECT * FROM testimonials WHERE id = ?", (t_id,)).fetchone()
    if t:
        db.execute("UPDATE testimonials SET is_approved = ? WHERE id = ?", (0 if t["is_approved"] else 1, t_id))
        db_commit_retry(db)
        log_admin_action("toggle_testimonial_approved", str(t_id))
    return redirect(url_for("admin_testimonials"))


@app.route("/admin/testimonials/<int:t_id>/delete", methods=["POST"])
@admin_required
def admin_testimonial_delete(t_id):
    db = get_db()
    t = db.execute("SELECT * FROM testimonials WHERE id = ?", (t_id,)).fetchone()
    if t:
        if t["photo_filename"]:
            delete_file_quietly(TESTIMONIALS_DIR, t["photo_filename"])
        db.execute("DELETE FROM testimonials WHERE id = ?", (t_id,))
        db_commit_retry(db)
        log_admin_action("delete_testimonial", str(t_id))
        flash("Success story deleted.", "success")
    return redirect(url_for("admin_testimonials"))


# ======================================================================
# ADMIN — NOTIFICATION BROADCAST ENGINE
# Lightweight, DB-polled banner/bell system (no push service / 3rd party
# needed) — the client polls /api/notifications every 60s while on-site.
# ======================================================================
@app.route("/admin/notifications", methods=["GET", "POST"])
@admin_required
def admin_notifications():
    db = get_db()
    if request.method == "POST":
        action = request.form.get("action", "create")
        if action == "create":
            title = request.form.get("title", "").strip()[:120]
            body = request.form.get("body", "").strip()[:500]
            if not title or not body:
                flash("Title and message are required.", "error")
            else:
                expires_days = request.form.get("expires_days", type=int)
                expires_at = (datetime.now() + timedelta(days=expires_days)).isoformat() if expires_days else None
                db.execute(
                    "INSERT INTO notifications (title, body, target_group, action_url, is_active, created_at, expires_at) "
                    "VALUES (?, ?, ?, ?, 1, ?, ?)",
                    (title, body, request.form.get("target_group", "all"),
                     request.form.get("action_url", "").strip()[:300] or None,
                     datetime.now().isoformat(), expires_at),
                )
                db_commit_retry(db)
                log_admin_action("create_notification", title)
                flash("Notification pushed live.", "success")
        elif action == "deactivate":
            db.execute("UPDATE notifications SET is_active = 0 WHERE id = ?", (request.form.get("notif_id"),))
            db_commit_retry(db)
            flash("Notification withdrawn.", "success")
        elif action == "delete":
            db.execute("DELETE FROM notifications WHERE id = ?", (request.form.get("notif_id"),))
            db_commit_retry(db)
            flash("Notification deleted.", "success")
        return redirect(url_for("admin_notifications"))

    rows = db.execute("SELECT * FROM notifications ORDER BY created_at DESC LIMIT 50").fetchall()
    return render_template("admin_notifications.html", rows=rows)


@app.route("/api/notifications")
def api_notifications():
    """Polled by the client-side bell/toast. target_group is a coarse,
    non-PII segmentation: 'new_users' = never verified a phone on this
    device/session, 'unregistered' = same idea (kept as a distinct option
    for the admin's messaging intent), 'all' = everyone."""
    db = get_db()
    now = datetime.now().isoformat()
    is_returning = bool(verified_phone())
    rows = db.execute(
        "SELECT * FROM notifications WHERE is_active = 1 AND (expires_at IS NULL OR expires_at > ?) "
        "ORDER BY created_at DESC LIMIT 5",
        (now,),
    ).fetchall()
    visible = []
    for r in rows:
        if r["target_group"] == "new_users" and is_returning:
            continue
        if r["target_group"] == "unregistered" and is_returning:
            continue
        visible.append({"id": r["id"], "title": r["title"], "body": r["body"], "action_url": r["action_url"]})
    return jsonify({"notifications": visible})



@app.route("/admin/shop/categories", methods=["GET", "POST"])
@admin_required
def admin_shop_categories():
    db = get_db()
    if request.method == "POST":
        action = request.form.get("action")
        if action == "add":
            name = request.form.get("name", "").strip()[:80]
            if not name:
                flash("Category name is required.", "error")
            else:
                slug = unique_slug(db, "shop_categories", slugify(name))
                db.execute("INSERT INTO shop_categories (name, slug, sort_order) VALUES (?, ?, ?)",
                           (name, slug, request.form.get("sort_order", 0, type=int) or 0))
                db_commit_retry(db)
                log_admin_action("add_shop_category", name)
                flash(f'Category "{name}" added.', "success")
        elif action == "delete":
            cat_id = request.form.get("category_id")
            db.execute("UPDATE shop_products SET category_id = NULL WHERE category_id = ?", (cat_id,))
            db.execute("DELETE FROM shop_categories WHERE id = ?", (cat_id,))
            db_commit_retry(db)
            log_admin_action("delete_shop_category", str(cat_id))
            flash("Category deleted. Its products are now uncategorised.", "success")
        elif action == "rename":
            cat_id = request.form.get("category_id")
            name = request.form.get("name", "").strip()[:80]
            if name:
                db.execute("UPDATE shop_categories SET name = ? WHERE id = ?", (name, cat_id))
                db_commit_retry(db)
                flash("Category updated.", "success")
        return redirect(url_for("admin_shop_categories"))

    categories = db.execute(
        "SELECT c.*, (SELECT COUNT(*) FROM shop_products p WHERE p.category_id = c.id) as product_count "
        "FROM shop_categories c ORDER BY c.sort_order, c.name"
    ).fetchall()
    return render_template("admin_shop_categories.html", categories=categories)


# ======================================================================
# ADMIN — ISLAMIC SHOP: PRODUCTS
# ======================================================================
@app.route("/admin/shop/products")
@admin_required
def admin_shop_products():
    db = get_db()
    cat_id = request.args.get("category", type=int)
    status = request.args.get("status", "")
    q = request.args.get("q", "").strip()

    query = "SELECT p.*, c.name as category_name FROM shop_products p LEFT JOIN shop_categories c ON c.id = p.category_id WHERE 1=1"
    params = []
    if cat_id:
        query += " AND p.category_id = ?"
        params.append(cat_id)
    if status:
        query += " AND p.status = ?"
        params.append(status)
    if q:
        query += " AND p.name LIKE ?"
        params.append(f"%{q}%")
    query += " ORDER BY p.created_at DESC"
    products = db.execute(query, params).fetchall()
    categories = shop_categories_list(db)
    return render_template("admin_shop_products.html", products=products, categories=categories,
                            cat_id=cat_id, status=status, q=q)


def _save_product_images(files):
    names = []
    for f in files:
        if f and f.filename:
            try:
                name = save_generic_image(f, SHOP_IMAGES_DIR, max_dim=1400)
                if name:
                    names.append(name)
            except ImageValidationError as e:
                flash(f"One image was skipped: {e}", "error")
    return names


@app.route("/admin/shop/products/add", methods=["GET", "POST"])
@admin_required
def admin_shop_product_add():
    db = get_db()
    categories = shop_categories_list(db)
    if request.method == "POST":
        name = request.form.get("name", "").strip()[:150]
        errors = []
        try:
            price = float(request.form.get("price", "0") or 0)
        except ValueError:
            price = 0
            errors.append("Price must be a number.")
        discount_raw = request.form.get("discount_price", "").strip()
        discount_price = None
        if discount_raw:
            try:
                discount_price = float(discount_raw)
                if discount_price >= price:
                    errors.append("Discount price must be lower than the regular price.")
            except ValueError:
                errors.append("Discount price must be a number.")
        if not name:
            errors.append("Product name is required.")
        if price <= 0:
            errors.append("Price must be greater than 0.")

        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("admin_shop_product_form.html", categories=categories, product=None, variants=[])

        slug = unique_slug(db, "shop_products", slugify(name))
        now = datetime.now().isoformat()
        images = _save_product_images(request.files.getlist("images"))
        has_variants = 1 if request.form.get("has_variants") == "1" else 0

        cur = db.execute(
            "INSERT INTO shop_products (category_id, name, slug, description, price, discount_price, "
            "stock, has_variants, images, status, featured, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (request.form.get("category_id", type=int), name, slug, request.form.get("description", "").strip()[:2000],
             price, discount_price, request.form.get("stock", 0, type=int) or 0, has_variants,
             ",".join(images), request.form.get("status", "active"),
             1 if request.form.get("featured") == "1" else 0, now, now),
        )
        product_id = cur.lastrowid

        if has_variants:
            sizes = request.form.getlist("variant_size[]")
            colors = request.form.getlist("variant_color[]")
            stocks = request.form.getlist("variant_stock[]")
            for size, color, stock in zip(sizes, colors, stocks):
                if size.strip() or color.strip():
                    db.execute("INSERT INTO shop_variants (product_id, size, color, stock) VALUES (?, ?, ?, ?)",
                               (product_id, size.strip()[:40], color.strip()[:40], int(stock or 0)))

        db_commit_retry(db)
        log_admin_action("add_shop_product", name)
        flash(f'Product "{name}" created.', "success")
        return redirect(url_for("admin_shop_products"))

    return render_template("admin_shop_product_form.html", categories=categories, product=None, variants=[])


@app.route("/admin/shop/products/<int:product_id>/edit", methods=["GET", "POST"])
@admin_required
def admin_shop_product_edit(product_id):
    db = get_db()
    product = db.execute("SELECT * FROM shop_products WHERE id = ?", (product_id,)).fetchone()
    if not product:
        abort(404)
    categories = shop_categories_list(db)
    variants = db.execute("SELECT * FROM shop_variants WHERE product_id = ? ORDER BY id", (product_id,)).fetchall()

    if request.method == "POST":
        name = request.form.get("name", "").strip()[:150]
        errors = []
        try:
            price = float(request.form.get("price", "0") or 0)
        except ValueError:
            price = 0
            errors.append("Price must be a number.")
        discount_raw = request.form.get("discount_price", "").strip()
        discount_price = None
        if discount_raw:
            try:
                discount_price = float(discount_raw)
                if discount_price >= price:
                    errors.append("Discount price must be lower than the regular price.")
            except ValueError:
                errors.append("Discount price must be a number.")
        if not name:
            errors.append("Product name is required.")
        if price <= 0:
            errors.append("Price must be greater than 0.")

        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("admin_shop_product_form.html", categories=categories, product=product, variants=variants)

        images = list(filter(None, (product["images"] or "").split(",")))
        if request.form.get("remove_images"):
            removed = set(request.form.getlist("remove_images"))
            for img in removed:
                delete_file_quietly(SHOP_IMAGES_DIR, img)
            images = [i for i in images if i not in removed]
        images += _save_product_images(request.files.getlist("images"))

        has_variants = 1 if request.form.get("has_variants") == "1" else 0
        new_slug = product["slug"]
        if slugify(name) != product["slug"].rsplit("-", 1)[0]:
            new_slug = unique_slug(db, "shop_products", slugify(name), exclude_id=product_id)

        db.execute(
            "UPDATE shop_products SET category_id=?, name=?, slug=?, description=?, price=?, discount_price=?, "
            "stock=?, has_variants=?, images=?, status=?, featured=?, updated_at=? WHERE id=?",
            (request.form.get("category_id", type=int), name, new_slug, request.form.get("description", "").strip()[:2000],
             price, discount_price, request.form.get("stock", 0, type=int) or 0, has_variants,
             ",".join(images), request.form.get("status", "active"),
             1 if request.form.get("featured") == "1" else 0, datetime.now().isoformat(), product_id),
        )

        if has_variants:
            db.execute("DELETE FROM shop_variants WHERE product_id = ?", (product_id,))
            sizes = request.form.getlist("variant_size[]")
            colors = request.form.getlist("variant_color[]")
            stocks = request.form.getlist("variant_stock[]")
            for size, color, stock in zip(sizes, colors, stocks):
                if size.strip() or color.strip():
                    db.execute("INSERT INTO shop_variants (product_id, size, color, stock) VALUES (?, ?, ?, ?)",
                               (product_id, size.strip()[:40], color.strip()[:40], int(stock or 0)))
        else:
            db.execute("DELETE FROM shop_variants WHERE product_id = ?", (product_id,))

        db_commit_retry(db)
        log_admin_action("edit_shop_product", name)
        flash("Product updated.", "success")
        return redirect(url_for("admin_shop_products"))

    return render_template("admin_shop_product_form.html", categories=categories, product=product, variants=variants)


@app.route("/admin/shop/products/<int:product_id>/delete", methods=["POST"])
@admin_required
def admin_shop_product_delete(product_id):
    db = get_db()
    product = db.execute("SELECT * FROM shop_products WHERE id = ?", (product_id,)).fetchone()
    if product:
        for img in filter(None, (product["images"] or "").split(",")):
            delete_file_quietly(SHOP_IMAGES_DIR, img)
        db.execute("DELETE FROM shop_products WHERE id = ?", (product_id,))
        db_commit_retry(db)
        log_admin_action("delete_shop_product", product["name"])
        flash("Product deleted.", "success")
    return redirect(url_for("admin_shop_products"))


@app.route("/admin/shop/products/<int:product_id>/toggle", methods=["POST"])
@admin_required
def admin_shop_product_toggle(product_id):
    db = get_db()
    product = db.execute("SELECT * FROM shop_products WHERE id = ?", (product_id,)).fetchone()
    if product:
        new_status = "inactive" if product["status"] == "active" else "active"
        db.execute("UPDATE shop_products SET status = ?, updated_at = ? WHERE id = ?",
                   (new_status, datetime.now().isoformat(), product_id))
        db_commit_retry(db)
        log_admin_action("toggle_shop_product", f"{product['name']} -> {new_status}")
    return redirect(request.referrer or url_for("admin_shop_products"))


# ======================================================================
# ADMIN — ISLAMIC SHOP: ORDERS
# ======================================================================
SHOP_ORDER_STATUSES = ["pending", "confirmed", "processing", "shipped", "delivered", "cancelled"]
SHOP_PAYMENT_STATUSES = ["pending", "paid", "failed", "refunded"]


@app.route("/admin/shop/orders")
@admin_required
def admin_shop_orders():
    db = get_db()
    status = request.args.get("status", "")
    query = "SELECT * FROM shop_orders WHERE 1=1"
    params = []
    if status:
        query += " AND order_status = ?"
        params.append(status)
    query += " ORDER BY created_at DESC"
    orders = db.execute(query, params).fetchall()
    return render_template("admin_shop_orders.html", orders=orders, status=status, statuses=SHOP_ORDER_STATUSES)


@app.route("/admin/shop/orders/<int:order_id>")
@admin_required
def admin_shop_order_detail(order_id):
    db = get_db()
    order = db.execute("SELECT * FROM shop_orders WHERE id = ?", (order_id,)).fetchone()
    if not order:
        abort(404)
    items = db.execute("SELECT * FROM shop_order_items WHERE order_id = ?", (order_id,)).fetchall()
    return render_template("admin_shop_order_detail.html", order=order, items=items,
                            order_statuses=SHOP_ORDER_STATUSES, payment_statuses=SHOP_PAYMENT_STATUSES)


@app.route("/admin/shop/orders/<int:order_id>/update", methods=["POST"])
@admin_required
def admin_shop_order_update(order_id):
    db = get_db()
    order = db.execute("SELECT * FROM shop_orders WHERE id = ?", (order_id,)).fetchone()
    if not order:
        abort(404)
    new_order_status = request.form.get("order_status", order["order_status"])
    new_payment_status = request.form.get("payment_status", order["payment_status"])

    # If an order is cancelled or a payment marked failed/refunded after having
    # reserved stock, restore that stock so it can be sold again.
    if new_order_status == "cancelled" and order["order_status"] != "cancelled":
        items = db.execute("SELECT * FROM shop_order_items WHERE order_id = ?", (order_id,)).fetchall()
        for item in items:
            if item["variant_id"]:
                db.execute("UPDATE shop_variants SET stock = stock + ? WHERE id = ?", (item["qty"], item["variant_id"]))
            elif item["product_id"]:
                db.execute("UPDATE shop_products SET stock = stock + ? WHERE id = ?", (item["qty"], item["product_id"]))
        if order["coins_used"]:
            refund_coins(order["customer_phone"], order["coins_used"], "shop_cancel_refund", order["order_code"])

    db.execute("UPDATE shop_orders SET order_status = ?, payment_status = ?, updated_at = ? WHERE id = ?",
               (new_order_status, new_payment_status, datetime.now().isoformat(), order_id))
    db_commit_retry(db)
    log_admin_action("update_shop_order", f"{order['order_code']} -> {new_order_status}/{new_payment_status}")
    flash("Order updated.", "success")
    return redirect(url_for("admin_shop_order_detail", order_id=order_id))


# ======================================================================
# ERROR PAGES
# ======================================================================
@app.errorhandler(404)
def err_404(e):
    return render_template("error.html", code=404, title="Page Not Found",
                            message="The page you're looking for doesn't exist or has moved."), 404


@app.errorhandler(403)
def err_403(e):
    return render_template("error.html", code=403, title="Access Denied",
                            message="You don't have permission to view this."), 403


@app.errorhandler(413)
def err_413(e):
    return render_template("error.html", code=413, title="File Too Large",
                            message="The file you tried to upload is too large."), 413


@app.errorhandler(429)
def err_429(e):
    return render_template("error.html", code=429, title="Too Many Attempts",
                            message="You've tried this a few too many times. Please wait a few minutes and try again."), 429


@app.errorhandler(503)
def err_503(e):
    return render_template("error.html", code=503, title="Temporarily Unavailable",
                            message="We're briefly unavailable — please try again in a moment."), 503


@app.errorhandler(400)
def err_400(e):
    return render_template("error.html", code=400, title="Something Went Wrong",
                            message="Your form session expired. Please go back and try again."), 400


@app.errorhandler(500)
def err_500(e):
    return render_template("error.html", code=500, title="Something Went Wrong",
                            message="An unexpected error occurred. Please try again shortly."), 500


# ======================================================================
init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
