import os
import io
import re
import math
import secrets
import sqlite3
import time
import hashlib
from datetime import datetime, timedelta, date
from functools import wraps

from flask import (
    Flask, render_template, request, redirect, url_for,
    session, flash, g, abort, send_from_directory, send_file, Response
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from PIL import Image, ImageFilter, ImageOps, ImageDraw, ImageFont

# ======================================================================
# CONFIG
# ======================================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DATA_DIR = os.environ.get("DATA_DIR", BASE_DIR)

DB_PATH = os.path.join(DATA_DIR, "matrimonial.db")
PRIVATE_ORIGINALS_DIR = os.path.join(DATA_DIR, "storage", "private", "profile_originals")
PRIVATE_PROOFS_DIR = os.path.join(DATA_DIR, "storage", "private", "payment_proofs")
PRIVATE_WATERMARK_CACHE_DIR = os.path.join(DATA_DIR, "storage", "private", "watermark_cache")
PREVIEW_DIR = os.path.join(BASE_DIR, "static", "previews")
BRANDING_DIR = os.path.join(BASE_DIR, "static", "branding")
BANNERS_DIR = os.path.join(BASE_DIR, "static", "banners")

for d in (PRIVATE_ORIGINALS_DIR, PRIVATE_PROOFS_DIR, PRIVATE_WATERMARK_CACHE_DIR,
          PREVIEW_DIR, BRANDING_DIR, BANNERS_DIR):
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
    return send_raw_sms(phone, f"Your OTP is {code}. It expires in {OTP_TTL_MINUTES} minutes. Do not share this with anyone.")


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


# ======================================================================
# DATABASE
# ======================================================================
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
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
    }
    for k, v in defaults.items():
        db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))

    db.commit()
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
        hero_heading=s.get("hero_heading", ""),
        hero_subheading=s.get("hero_subheading", ""),
        footer_text=s.get("footer_text", ""),
        logo_image=s.get("logo_image", ""),
        qr_image=s.get("qr_image", ""),
        nav_bg_image=s.get("nav_bg_image", ""),
        hero_bg_image=s.get("hero_bg_image", ""),
        section_bg_image=s.get("section_bg_image", ""),
        footer_bg_image=s.get("footer_bg_image", ""),
        top_banners=active_banners("top"),
        current_year=datetime.now().year,
    )


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
    db.commit()


def verified_phone():
    return session.get("verified_phone")


def user_has_unlocked(db, profile_id, phone):
    if not phone:
        return None
    return db.execute(
        "SELECT * FROM unlock_requests WHERE profile_id = ? AND user_phone = ? AND status = 'unlocked'",
        (profile_id, phone),
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
    return render_template(
        "profile_preview.html", profile=profile, already_unlocked=already_unlocked,
        in_package=(profile_code in selection), package_selection=selection, package_size=_package_size(),
        recommended=recommended,
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

        request_code = new_request_code()
        db.execute(
            """
            INSERT INTO unlock_requests
            (request_code, profile_id, user_name, user_phone, payment_proof_name, message,
             viewer_declaration, status, requested_at)
            VALUES (?, ?, ?, ?, ?, ?, 1, 'pending', ?)
            """,
            (request_code, profile["id"], user_name, user_phone, proof_name, message, datetime.now().isoformat()),
        )
        db.commit()

        notify_admin(
            "New payment request",
            f"{user_name or 'A customer'} ({user_phone}) submitted payment proof for "
            f"profile {profile['profile_code']} ({profile['name']}). Request code: {request_code}. "
            f"Review: {request.url_root.rstrip('/')}{url_for('admin_requests')}",
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

        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("package_checkout.html", profiles=profiles, size=size)

        package_code = new_request_code()
        for profile in profiles:
            request_code = new_request_code()
            db.execute(
                """
                INSERT INTO unlock_requests
                (request_code, profile_id, user_name, user_phone, payment_proof_name, message,
                 viewer_declaration, status, requested_at, package_code)
                VALUES (?, ?, ?, ?, ?, ?, 1, 'pending', ?, ?)
                """,
                (request_code, profile["id"], user_name, user_phone, proof_name, message,
                 datetime.now().isoformat(), package_code),
            )
        db.commit()

        session.pop("package_selection", None)
        profile_list = ", ".join(p["profile_code"] for p in profiles)
        notify_admin(
            "New PACKAGE payment request",
            f"{user_name or 'A customer'} ({user_phone}) submitted payment proof for a "
            f"{size}-profile package ({profile_list}). Package code: {package_code}. "
            f"Review: {request.url_root.rstrip('/')}{url_for('admin_requests')}",
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
        db.commit()

        notify_admin(
            "New self-registration",
            f"{name} ({phone}) registered themselves for ₹{get_setting('registration_price', '11')}. "
            f"Request code: {request_code}. Review: {request.url_root.rstrip('/')}{url_for('admin_registrations')}",
        )

        return render_template("registration_submitted.html", request_code=request_code)

    return render_template("register_yourself.html")


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
    return render_template("profile_full.html", profile=profile)


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
    db.commit()
    notify_admin(
        "More photos requested",
        f"{phone} requested more photos for profile {profile['profile_code']} ({profile['name']}). "
        f"Review: {request.url_root.rstrip('/')}{url_for('admin_photo_requests')}",
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
        db.commit()
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
        db.commit()
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
    db.commit()


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
        db.commit()
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
        db.commit()
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
        db.commit()
        log_admin_action("add_agent", phone)
        flash("Agent created.", "success")
        return render_template("admin_agent_created.html", name=name, phone=phone, access_code=access_code)

    return render_template("admin_agent_form.html", form=None)


@app.route("/admin/agents/<int:agent_id>/toggle", methods=["POST"])
@admin_required
def admin_toggle_agent(agent_id):
    db = get_db()
    db.execute("UPDATE agents SET is_active = 1 - is_active WHERE id = ?", (agent_id,))
    db.commit()
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
    db.commit()
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
    return {"pending": count + reg_count, "payment_pending": count, "registration_pending": reg_count}


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
    }
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
@app.route("/admin/profiles")
@admin_required
def admin_profiles():
    db = get_db()
    q = request.args.get("q", "").strip()
    if q:
        profiles = db.execute(
            "SELECT * FROM profiles WHERE profile_code LIKE ? OR name LIKE ? ORDER BY created_at DESC",
            (f"%{q}%", f"%{q}%"),
        ).fetchall()
    else:
        profiles = db.execute("SELECT * FROM profiles ORDER BY created_at DESC").fetchall()
    return render_template("admin_profiles.html", profiles=profiles, q=q)


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
             lifestyle_roza, declaration_accepted,
             photo_original_name, photo_preview_name, is_active, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, 1, ?)
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
        db.commit()
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
        db.commit()
        log_admin_action("edit_profile", profile["profile_code"])
        flash("Profile updated.", "success")
        return redirect(url_for("admin_profiles"))

    return render_template("admin_edit_profile.html", profile=profile, form=None)


@app.route("/admin/profiles/<int:profile_id>/toggle", methods=["POST"])
@admin_required
def admin_toggle_profile(profile_id):
    db = get_db()
    db.execute("UPDATE profiles SET is_active = 1 - is_active WHERE id = ?", (profile_id,))
    db.commit()
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
        db.commit()
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


@app.route("/admin/requests/<int:request_id>/<action>", methods=["POST"])
@admin_required
def admin_decide_request(request_id, action):
    if action not in ("unlock", "reject"):
        abort(400)
    db = get_db()
    new_status = "unlocked" if action == "unlock" else "rejected"
    db.execute(
        "UPDATE unlock_requests SET status = ?, decided_at = ? WHERE id = ?",
        (new_status, datetime.now().isoformat(), request_id),
    )
    db.commit()
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
    db.execute(
        "UPDATE unlock_requests SET status = ?, decided_at = ? WHERE package_code = ?",
        (new_status, datetime.now().isoformat(), package_code),
    )
    db.commit()
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
        db.commit()
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
         lifestyle_roza, declaration_accepted,
         photo_original_name, photo_preview_name, is_active, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0, 0, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
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
    db.commit()
    log_admin_action("registration_approve", f"{reg_id} -> {profile_code}")
    flash(f"Registration approved — profile {profile_code} is now live. You can review/edit it any time.", "success")
    return redirect(url_for("admin_registrations"))


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
    db.commit()
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
    "footer_text",
]

# Background images the admin can upload from Appearance Settings without
# touching any code/CSS. Each maps a settings key -> the CSS variable that
# picks it up (see base.html, which turns these into --*-bg-image vars).
APPEARANCE_IMAGE_FIELDS = ["nav_bg_image", "hero_bg_image", "section_bg_image", "footer_bg_image"]


@app.route("/admin/settings", methods=["GET", "POST"])
@admin_required
def admin_settings():
    db = get_db()
    if request.method == "POST":
        for key in SETTINGS_TEXT_FIELDS:
            value = request.form.get(key, "").strip()[:500]
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

        db.commit()
        log_admin_action("update_settings")
        flash("Website settings updated.", "success")
        return redirect(url_for("admin_settings"))

    return render_template("admin_settings.html", settings=load_settings())


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
        db.commit()
        log_admin_action("add_banner", slot)
        flash("Banner added.", "success")
        return redirect(url_for("admin_banners"))

    return render_template("admin_banner_form.html", banner=None)


@app.route("/admin/banners/<int:banner_id>/toggle", methods=["POST"])
@admin_required
def admin_toggle_banner(banner_id):
    db = get_db()
    db.execute("UPDATE banners SET is_active = 1 - is_active WHERE id = ?", (banner_id,))
    db.commit()
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
        db.commit()
        log_admin_action("delete_banner", str(banner_id))
        flash("Banner deleted.", "success")
    return redirect(url_for("admin_banners"))


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
