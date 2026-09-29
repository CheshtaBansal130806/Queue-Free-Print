import os
from dotenv import load_dotenv

# Load local environment variables before reading application configuration.
load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY", "")
import re
import json
import uuid
import mimetypes
import subprocess
import shutil
import tempfile
import secrets
import io
import hmac
import hashlib
import requests
import time
from decimal import Decimal, InvalidOperation
from datetime import datetime, timedelta
from pathlib import Path
from flask import Flask, render_template, request, redirect, url_for, session, flash, send_file, jsonify
import mysql.connector
from mysql.connector import Error
from werkzeug.utils import secure_filename

try:
    import qrcode
except ImportError:
    qrcode = None

try:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    from reportlab.lib.utils import ImageReader
except ImportError:
    A4 = None
    canvas = None

try:
    from google import genai
    try:
        from google.genai import types as genai_types
    except ImportError:
        genai_types = None
except ImportError:
    genai = None
    genai_types = None

try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None

try:
    from PIL import Image
except ImportError:
    Image = None

try:
    from pywebpush import webpush, WebPushException
except ImportError:
    webpush = None
    WebPushException = Exception

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY") or secrets.token_urlsafe(48)

# Keep the login session available to dashboard AJAX requests.
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.getenv("COOKIE_SECURE", "0").strip().lower() in {"1", "true", "yes", "on"}


# ============================================================
# AUTHENTICATION + NO-BACK-CACHE PROTECTION
# ============================================================

PUBLIC_PATHS = {"/", "/login", "/register", "/logout", "/session-status", "/service-worker.js", "/push/vapid-public-key", "/push/subscribe"}


@app.before_request
def protect_logged_in_pages():
    # Static files and public pages remain accessible without login.
    if request.path.startswith("/static/") or request.path in PUBLIC_PATHS:
        return None

    # Every other application endpoint requires an authenticated session.
    # Existing route-level role checks continue to decide whether the
    # logged-in account is a User or Admin.
    if not session.get("user_id"):
        if request.method == "GET":
            return redirect(url_for("home"))
        return {"success": False, "error": "Please login first."}, 401

    # Run privacy cleanup at most once per hour while the server is active.
    # The cleanup only removes expired files; order metadata remains.
    global _LAST_RETENTION_CLEANUP
    now = time.monotonic()
    if now - _LAST_RETENTION_CLEANUP >= _RETENTION_CLEANUP_INTERVAL:
        try:
            cleanup_expired_documents()
        except Exception:
            pass
        _LAST_RETENTION_CLEANUP = now

    return None


@app.after_request
def prevent_protected_page_caching(response):
    # Do not let protected pages remain in browser cache/BFCache after logout.
    if request.path not in PUBLIC_PATHS and not request.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


UPLOAD_ROOT = Path(app.root_path) / "uploads"
UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
FILE_RETENTION_DAYS = 7
_LAST_RETENTION_CLEANUP = 0.0
_RETENTION_CLEANUP_INTERVAL = 3600

def _safe_upload_path(relative_path):
    """Resolve an upload path and ensure it stays inside the uploads directory."""
    if not relative_path:
        return None
    try:
        path = (Path(app.root_path) / str(relative_path)).resolve()
        root = UPLOAD_ROOT.resolve()
        return path if path == root or root in path.parents else None
    except (OSError, ValueError):
        return None

def _resolve_reorder_source_path(file_path, file_name, user_id):
    """Resolve a stored upload, including UUID-prefixed filenames.

    Reorder must use the actual stored file, not the original client filename.
    Older/newer versions may store paths differently, so try the database path
    first and then safely locate the UUID-prefixed file inside this user's
    upload directory.
    """
    path = _safe_upload_path(file_path)
    if path and path.is_file():
        return path

    user_dir = UPLOAD_ROOT / str(user_id)
    if not user_dir.is_dir():
        return None

    safe_name = secure_filename(file_name or "document") or "document"
    # Current storage format: <uuid>_<original-name>
    matches = list(user_dir.glob(f"*_{safe_name}"))
    if matches:
        return max(matches, key=lambda x: x.stat().st_mtime)

    # Fallback for legacy records that stored only the original filename.
    direct = user_dir / safe_name
    if direct.is_file():
        return direct

    return None


def cleanup_expired_documents():
    """Permanently delete uploaded source documents older than the retention period.

    Order metadata is intentionally retained. Only files under uploads/ are removed.
    """
    conn = cursor = None
    deleted = 0
    cutoff = datetime.now()
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        # uploaded_at is added by ensure_order_item_columns().
        cursor.execute("""
            SELECT item_id, file_path, uploaded_at
            FROM order_items
            WHERE file_path IS NOT NULL AND file_path <> ''
              AND uploaded_at IS NOT NULL
              AND uploaded_at <= DATE_SUB(NOW(), INTERVAL %s DAY)
        """, (FILE_RETENTION_DAYS,))
        rows = cursor.fetchall()
        for row in rows:
            path = _safe_upload_path(row.get("file_path"))
            if path and path.is_file():
                try:
                    path.unlink()
                    deleted += 1
                except OSError:
                    pass
        # Keep file_path as historical metadata, but mark it unavailable.
        # This prevents the UI/reorder logic from treating a deleted file as reusable.
        if rows:
            cursor.execute("""
                UPDATE order_items
                SET file_path = CASE
                    WHEN uploaded_at <= DATE_SUB(NOW(), INTERVAL %s DAY) THEN NULL
                    ELSE file_path END
                WHERE uploaded_at <= DATE_SUB(NOW(), INTERVAL %s DAY)
            """, (FILE_RETENTION_DAYS, FILE_RETENTION_DAYS))
            conn.commit()
    except Exception:
        if conn:
            conn.rollback()
    finally:
        close_db(conn, cursor)
    return deleted

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite")
# Edit-order analysis uses a lighter model so adding documents stays responsive.
EDIT_GEMINI_MODEL = os.getenv("EDIT_GEMINI_MODEL", "gemini-2.5-flash-lite")

# ============================================================
# WEB PUSH NOTIFICATIONS
# ============================================================
VAPID_PUBLIC_KEY = os.getenv("VAPID_PUBLIC_KEY", "")
VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY", "")
# .env stores the PEM newlines as literal \n escapes so the value stays one line.
if "\\n" in VAPID_PRIVATE_KEY:
    VAPID_PRIVATE_KEY = VAPID_PRIVATE_KEY.replace("\\n", "\n")
VAPID_SUBJECT = os.getenv("VAPID_SUBJECT", "mailto:admin@queuefreeprint.local")

# If VAPID keys are not supplied through environment variables, create a local
# key pair once so the project works out-of-the-box for local development.
if not VAPID_PRIVATE_KEY or not VAPID_PUBLIC_KEY:
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        import base64
        _vapid_dir = Path(app.root_path)
        _private_file = _vapid_dir / ".vapid_private.pem"
        _public_file = _vapid_dir / ".vapid_public.txt"
        if _private_file.exists() and _public_file.exists():
            VAPID_PRIVATE_KEY = _private_file.read_text(encoding="utf-8")
            VAPID_PUBLIC_KEY = _public_file.read_text(encoding="utf-8").strip()
        else:
            _private = ec.generate_private_key(ec.SECP256R1())
            VAPID_PRIVATE_KEY = _private.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption(),
            ).decode("utf-8")
            _public_bytes = _private.public_key().public_bytes(
                encoding=serialization.Encoding.X962,
                format=serialization.PublicFormat.UncompressedPoint,
            )
            VAPID_PUBLIC_KEY = base64.urlsafe_b64encode(_public_bytes).rstrip(b"=").decode("ascii")
            _private_file.write_text(VAPID_PRIVATE_KEY, encoding="utf-8")
            _public_file.write_text(VAPID_PUBLIC_KEY, encoding="utf-8")
    except Exception:
        # Push remains optional if cryptography is unavailable.
        VAPID_PUBLIC_KEY = os.getenv("VAPID_PUBLIC_KEY", "")
        VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY", "")

def send_push_notification(user_id, title, body, order_id=None):
    if not webpush or not VAPID_PRIVATE_KEY or not user_id:
        return
    conn = cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT subscription_id, endpoint, p256dh, auth FROM push_subscriptions WHERE user_id=%s", (user_id,))
        subscriptions = cursor.fetchall()
        payload = json.dumps({"title": title, "body": body, "order_id": order_id, "url": "/recent-orders"})
        expired = []
        for sub in subscriptions:
            try:
                webpush(subscription_info={"endpoint":sub["endpoint"],"keys":{"p256dh":sub["p256dh"],"auth":sub["auth"]}}, data=payload, vapid_private_key=VAPID_PRIVATE_KEY, vapid_claims={"sub":VAPID_SUBJECT})
            except Exception as exc:
                response = getattr(exc, "response", None)
                if getattr(response, "status_code", None) in (404,410):
                    expired.append(sub["subscription_id"])
        if expired:
            cursor.executemany("DELETE FROM push_subscriptions WHERE subscription_id=%s", [(x,) for x in expired])
            conn.commit()
    except Exception:
        pass
    finally:
        close_db(conn,cursor)


# Gemini can analyze many formats directly. For office/open-document formats
# we first convert the file to PDF with LibreOffice so page counting and
# document scanning work consistently across formats.
DIRECT_ANALYSIS_MIMES = {
    "application/pdf",
    "image/png", "image/jpeg", "image/webp", "image/bmp", "image/gif", "image/tiff",
    "text/plain", "text/csv", "text/html", "application/json", "text/xml",
}

CONVERTIBLE_EXTENSIONS = {
    ".doc", ".docx", ".docm", ".dot", ".dotx", ".xls", ".xlsx", ".xlsm",
    ".xlt", ".xltx", ".ppt", ".pptx", ".pptm", ".pps", ".ppsx",
    ".odt", ".ods", ".odp", ".odg", ".rtf", ".txt", ".csv", ".html", ".htm",
}


# ============================================================
# DATABASE CONNECTION
# ============================================================

def get_db_connection():
    return mysql.connector.connect(
        host=os.getenv("MYSQL_HOST", "localhost"),
        user=os.getenv("MYSQL_USER", "root"),
        password=os.getenv("MYSQL_PASSWORD", ""),
        database=os.getenv("MYSQL_DATABASE", "queue_free_print"),
    )


def close_db(conn, cursor=None):
    try:
        if cursor:
            cursor.close()
    finally:
        if conn:
            conn.close()


# ============================================================
# SESSION STATUS
# ============================================================

@app.route("/session-status")
def session_status():
    return {
        "logged_in": bool(session.get("user_id")),
        "role": session.get("role", ""),
    }


# ============================================================
# HOME
# ============================================================

@app.route("/")
def home():
    return render_template("index.html")


# ============================================================
# VALIDATION HELPERS
# ============================================================

EMAIL_PATTERN = re.compile(
    r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.(?:com|co\.in)$"
)

PASSWORD_PATTERN = re.compile(
    r"^(?=.*[A-Z])(?=.*[a-z])(?=.*\d)(?=.*[^A-Za-z0-9]).{8,}$"
)

PHONE_PATTERN = re.compile(r"^\d{10}$")


def ensure_shop_location_columns():
    """Add shop location fields to older QueueFree databases without deleting data."""
    conn = cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        columns = {
            "university_name": "ALTER TABLE print_shops ADD COLUMN university_name VARCHAR(200) NULL AFTER shop_number",
            "state": "ALTER TABLE print_shops ADD COLUMN state VARCHAR(100) NULL AFTER university_name",
            "address": "ALTER TABLE print_shops ADD COLUMN address VARCHAR(300) NULL AFTER state",
        }
        for column, statement in columns.items():
            cursor.execute(
                """SELECT COUNT(*) FROM information_schema.COLUMNS
                   WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'print_shops' AND COLUMN_NAME = %s""",
                (column,),
            )
            if cursor.fetchone()[0] == 0:
                cursor.execute(statement)
        conn.commit()
    except Exception:
        if conn:
            conn.rollback()
    finally:
        close_db(conn, cursor)



ensure_shop_location_columns()

def get_register_data(error=None, form_data=None):
    """Load register page data and keep fields empty unless values were submitted."""
    shops = []
    conn = cursor = None

    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT shop_id, shop_name, shop_number, university_name, state, address
            FROM print_shops
            ORDER BY shop_id ASC
            """
        )
        shops = cursor.fetchall()
    except Error:
        shops = []
    finally:
        close_db(conn, cursor)

    return render_template(
        "register.html",
        error=error,
        shops=shops,
        form_data=form_data or {},
    )


# ============================================================
# LOGIN
# ============================================================

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        role = request.form.get("role", "user").strip().lower()

        if role not in ("user", "admin"):
            return render_template("index.html", error="Please select a valid account type.")

        if not email or not password:
            return render_template("index.html", error="Please enter email and password.")

        conn = cursor = None
        account = None
        db_error = None

        try:
            conn = get_db_connection()
            cursor = conn.cursor(dictionary=True)

            # First find the account by email in the table belonging to
            # the selected role. Do NOT let one table authenticate the
            # other role.
            if role == "user":
                cursor.execute(
                    """
                    SELECT user_id AS account_id, name, email, password, role
                    FROM users
                    WHERE LOWER(TRIM(email)) = %s
                    LIMIT 1
                    """,
                    (email,),
                )
            else:
                cursor.execute(
                    """
                    SELECT admin_id AS account_id, name, email, password, 'admin' AS role
                    FROM admins
                    WHERE LOWER(TRIM(email)) = %s
                    LIMIT 1
                    """,
                    (email,),
                )

            account = cursor.fetchone()

        except Error as e:
            db_error = str(e)
        finally:
            close_db(conn, cursor)

        if db_error:
            return render_template(
                "index.html",
                error="Database connection error. Check MySQL/database name and try again."
            )

        if not account:
            return render_template(
                "index.html",
                error="No account found for this email under the selected account type."
            )

        # A user record must have role='user'. This prevents an admin-type
        # record accidentally stored in users from logging in as a user.
        if role == "user" and str(account.get("role", "")).lower() != "user":
            return render_template(
                "index.html",
                error="This account is not registered as a User. Select the correct account type."
            )

        if account["password"] != password:
            return render_template(
                "index.html",
                error="Incorrect password."
            )

        # Start a clean session so a previous Admin/User login cannot leak
        # into the new account type.
        session.clear()
        session["user_id"] = account["account_id"]
        session["name"] = account["name"]
        session["email"] = account["email"]
        session["phone"] = account.get("phone", "") or ""
        session["role"] = "admin" if role == "admin" else "user"
        session.permanent = False
        session.modified = True

        if role == "admin":
            return redirect(url_for("admin_dashboard"))
        return redirect(url_for("user_dashboard"))

    return render_template("index.html")


# ============================================================
# REGISTER
# ============================================================

def ensure_gst_schema(cursor):
    """Ensure every print shop can store its GSTIN without breaking existing databases."""
    cursor.execute("""
        SELECT COUNT(*) AS column_exists
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = 'print_shops'
          AND COLUMN_NAME = 'gst_number'
    """)
    row = cursor.fetchone() or {}
    if not row.get("column_exists"):
        cursor.execute(
            "ALTER TABLE print_shops ADD COLUMN gst_number VARCHAR(15) NULL AFTER state"
        )


@app.route("/register", methods=["GET", "POST"])
def register():

    if request.method == "GET":
        return get_register_data()

    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip()
    phone = request.form.get("phone", "").strip()
    password = request.form.get("password", "")
    confirm_password = request.form.get("confirm_password", "")
    role = request.form.get("role", "user").strip().lower()
    shop_id = request.form.get("shop_id", "").strip()
    university_name = request.form.get("university_name", "").strip()
    state = request.form.get("state", "").strip()
    shop_name = request.form.get("shop_name", "").strip()
    gst_number = request.form.get("gst_number", "").strip().upper()

    form_data = {
        "name": name,
        "email": email,
        "phone": phone,
        "shop_id": shop_id,
        "university_name": university_name,
        "state": state,
        "shop_name": shop_name,
        "gst_number": gst_number,
    }

    # -----------------------------
    # Basic validation
    # -----------------------------
    if role not in ("user", "admin"):
        return get_register_data("Please select a valid account type.", form_data)

    if not name or not email or not phone or not password or not confirm_password:
        return get_register_data("All required fields must be filled.", form_data)

    if not EMAIL_PATTERN.fullmatch(email):
        return get_register_data(
            "Enter a valid email ending with .com or .co.in.",
            form_data
        )

    if not PHONE_PATTERN.fullmatch(phone):
        return get_register_data(
            "Phone number must contain exactly 10 digits.",
            form_data
        )

    if not PASSWORD_PATTERN.fullmatch(password):
        return get_register_data(
            "Password must be at least 8 characters and contain A-Z, a-z, 0-9 and a symbol.",
            form_data
        )

    if password != confirm_password:
        return get_register_data("Passwords do not match.", form_data)

    if role == "admin" and not shop_id:
        return get_register_data("Please select a Shop ID / Shop Number.", form_data)

    if role == "admin" and not state:
        return get_register_data("State is required.", form_data)

    if role == "admin" and not shop_name:
        return get_register_data("Shop Name is required.", form_data)

    if role == "admin" and not gst_number:
        return get_register_data("GST Number is required.", form_data)

    if role == "admin" and not re.fullmatch(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][A-Z0-9]Z[A-Z0-9]$", gst_number):
        return get_register_data("Enter a valid 15-character GST Number.", form_data)

    conn = cursor = None

    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        ensure_gst_schema(cursor)

        # Email must be unique across both account types.
        cursor.execute(
            """
            SELECT user_id AS account_id, 'user' AS account_role
            FROM users
            WHERE email = %s
            UNION ALL
            SELECT admin_id AS account_id, 'admin' AS account_role
            FROM admins
            WHERE email = %s
            LIMIT 1
            """,
            (email, email),
        )

        if cursor.fetchone():
            return get_register_data(
                "This email is already registered.",
                form_data
            )

        if role == "user":
            # The original database dump may not have a phone column.
            # Add it once so all registration details can be stored.
            cursor.execute(
                """
                SELECT COUNT(*) AS column_exists
                FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = 'users'
                  AND COLUMN_NAME = 'phone'
                """
            )
            if not cursor.fetchone()["column_exists"]:
                cursor.execute(
                    "ALTER TABLE users ADD COLUMN phone varchar(20) NULL AFTER email"
                )

            # User data is stored ONLY in users table.
            cursor.execute(
                """
                INSERT INTO users (name, email, phone, password, role)
                VALUES (%s, %s, %s, %s, 'user')
                """,
                (name, email, phone, password),
            )

        else:
            # Admin registration creates a NEW shop. The entered Shop ID
            # must not already exist in print_shops.
            try:
                new_shop_id = int(shop_id)
            except (TypeError, ValueError):
                return get_register_data(
                    "Shop ID must be a positive number.",
                    form_data
                )

            if new_shop_id <= 0:
                return get_register_data(
                    "Shop ID must be a positive number.",
                    form_data
                )

            cursor.execute(
                "SELECT shop_id FROM print_shops WHERE shop_id = %s LIMIT 1",
                (new_shop_id,),
            )
            if cursor.fetchone():
                return get_register_data(
                    "This Shop ID already exists. Please enter a different Shop ID.",
                    form_data
                )

            # Create the corresponding owner account because print_shops.owner_id
            # references users.user_id. It is kept as an admin-role owner.
            cursor.execute(
                """
                INSERT INTO users (name, email, phone, password, role)
                VALUES (%s, %s, %s, %s, 'admin')
                """,
                (name, email, phone, password),
            )
            owner_id = cursor.lastrowid

            shop_number = f"SHOP-{new_shop_id:03d}"

            # Create the new shop automatically during Admin registration.
            cursor.execute(
                """
                INSERT INTO print_shops
                    (shop_id, shop_name, shop_number, university_name, state, gst_number, address, owner_id, is_open)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 1)
                """,
                (new_shop_id, shop_name, shop_number, university_name, state, gst_number, "", owner_id),
            )

            cursor.execute(
                """
                INSERT INTO admins (name, email, phone, password, shop_id)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (name, email, phone, password, new_shop_id),
            )

        conn.commit()

    except (Error, ValueError) as e:

        if conn:
            conn.rollback()

        return get_register_data(
            f"Registration failed: {e}",
            form_data
        )

    finally:
        close_db(conn, cursor)

    # Successful registration always returns to the home/login page.
    return redirect(url_for("home"))


# ============================================================
# ORDER / AI HELPERS
# ============================================================


def ensure_shop_location_schema(cursor):
    """Create shop-location/review storage without requiring a manual migration."""
    cursor.execute("""
        SELECT COUNT(*) AS c FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'print_shops'
          AND COLUMN_NAME = 'latitude'
    """)
    if not cursor.fetchone()["c"]:
        cursor.execute("ALTER TABLE print_shops ADD COLUMN latitude DECIMAL(10,7) NULL AFTER address")
    cursor.execute("""
        SELECT COUNT(*) AS c FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'print_shops'
          AND COLUMN_NAME = 'longitude'
    """)
    if not cursor.fetchone()["c"]:
        cursor.execute("ALTER TABLE print_shops ADD COLUMN longitude DECIMAL(10,7) NULL AFTER latitude")
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS shop_reviews (
            review_id INT UNSIGNED NOT NULL AUTO_INCREMENT,
            shop_id INT UNSIGNED NOT NULL,
            user_id INT UNSIGNED NOT NULL,
            rating TINYINT UNSIGNED NOT NULL,
            review_text VARCHAR(500) DEFAULT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (review_id),
            UNIQUE KEY uq_shop_user_review (shop_id, user_id),
            KEY idx_shop_reviews_shop (shop_id),
            CONSTRAINT fk_shop_reviews_shop FOREIGN KEY (shop_id) REFERENCES print_shops(shop_id) ON DELETE CASCADE,
            CONSTRAINT fk_shop_reviews_user FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
    """)


def ensure_payment_schema(cursor):
    """Create payment gateway/settings fields safely for existing QueueFree databases."""
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS payment_gateway_settings (
            setting_id INT UNSIGNED NOT NULL AUTO_INCREMENT,
            shop_id INT UNSIGNED NOT NULL,
            provider VARCHAR(30) NOT NULL DEFAULT 'razorpay',
            key_id VARCHAR(150) NOT NULL,
            key_secret VARCHAR(255) NOT NULL,
            is_enabled TINYINT(1) NOT NULL DEFAULT 1,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (setting_id),
            UNIQUE KEY uq_payment_shop_provider (shop_id, provider),
            CONSTRAINT fk_payment_gateway_shop FOREIGN KEY (shop_id) REFERENCES print_shops(shop_id) ON DELETE CASCADE ON UPDATE CASCADE
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)

    # Older QueueFree databases may already have payment_method as an ENUM.
    # Razorpay uses the value "razorpay", so normalize the column to VARCHAR
    # instead of relying on ADD COLUMN IF NOT EXISTS (which does not change
    # an existing ENUM definition).
    cursor.execute("""
        SELECT DATA_TYPE, COLUMN_TYPE
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'payments' AND COLUMN_NAME = 'payment_method'
        LIMIT 1
    """)
    payment_method_col = cursor.fetchone()
    if not payment_method_col:
        cursor.execute("ALTER TABLE payments ADD COLUMN payment_method VARCHAR(30) NULL AFTER payment_status")
    elif str(payment_method_col.get("DATA_TYPE", "")).lower() != "varchar":
        cursor.execute("ALTER TABLE payments MODIFY COLUMN payment_method VARCHAR(30) NULL")

    cursor.execute("""
        SELECT COUNT(*) AS c FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'payments' AND COLUMN_NAME = 'razorpay_signature'
    """)
    if not cursor.fetchone()["c"]:
        cursor.execute("ALTER TABLE payments ADD COLUMN razorpay_signature VARCHAR(255) NULL AFTER razorpay_order_id")

    # QR payment fields. These are added lazily so existing QueueFree databases
    # continue to work without a manual migration.
    for column, definition, after in [
        ("razorpay_qr_id", "VARCHAR(100) NULL", "razorpay_signature"),
        ("razorpay_qr_image_url", "VARCHAR(500) NULL", "razorpay_qr_id"),
        ("razorpay_qr_status", "VARCHAR(30) NULL", "razorpay_qr_image_url"),
        ("razorpay_payment_link_id", "VARCHAR(100) NULL", "razorpay_qr_status"),
        ("razorpay_payment_link_url", "VARCHAR(500) NULL", "razorpay_payment_link_id"),
    ]:
        cursor.execute("""
            SELECT COUNT(*) AS c FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'payments' AND COLUMN_NAME = %s
        """, (column,))
        if not cursor.fetchone()["c"]:
            cursor.execute(f"ALTER TABLE payments ADD COLUMN {column} {definition} AFTER {after}")


def get_payment_settings(cursor, shop_id):
    cursor.execute("""
        SELECT provider, key_id, key_secret, is_enabled
        FROM payment_gateway_settings
        WHERE shop_id = %s AND provider = 'razorpay'
        LIMIT 1
    """, (shop_id,))
    return cursor.fetchone()


def create_razorpay_order(key_id, key_secret, amount, receipt):
    """Create a server-side Razorpay order. Amount is Decimal INR."""
    paise = int((Decimal(str(amount)) * 100).quantize(Decimal('1')))
    if paise <= 0:
        raise ValueError("Payment amount must be greater than zero.")
    response = requests.post(
        "https://api.razorpay.com/v1/orders",
        auth=(key_id, key_secret),
        json={"amount": paise, "currency": "INR", "receipt": str(receipt), "payment_capture": 1},
        timeout=15,
    )
    try:
        data = response.json()
    except ValueError:
        data = {}
    if response.status_code >= 400 or not data.get("id"):
        message = data.get("error", {}).get("description") if isinstance(data.get("error"), dict) else None
        raise RuntimeError(message or "Razorpay could not create the payment order.")
    return data


def create_razorpay_upi_payment_link(key_id, key_secret, amount, order_id, customer_name, customer_contact, customer_email):
    """Deprecated for Test Mode. UPI Payment Links are Live-only in Razorpay.
    QueueFree uses Standard Checkout for UPI so Razorpay Test Mode can be tested
    with success@razorpay.
    """
    raise RuntimeError("UPI Payment Links are Live-only in Razorpay. QueueFree uses Standard Checkout for UPI in Test Mode.")

def fetch_razorpay_payment_link(key_id, key_secret, payment_link_id):
    response = requests.get(
        f"https://api.razorpay.com/v1/payment_links/{payment_link_id}",
        auth=(key_id, key_secret),
        timeout=15,
    )
    try:
        data = response.json()
    except ValueError:
        data = {}
    if response.status_code >= 400 or not data.get("id"):
        error = data.get("error", {}) if isinstance(data, dict) else {}
        message = error.get("description") if isinstance(error, dict) else None
        raise RuntimeError(message or "Razorpay could not fetch the UPI payment link status.")
    return data


def create_razorpay_upi_qr(key_id, key_secret, amount, order_id):
    """Create a one-time, fixed-amount Razorpay UPI QR for one print order."""
    paise = int((Decimal(str(amount)) * 100).quantize(Decimal("1")))
    if paise < 100:
        raise ValueError("UPI QR amount must be at least ₹1.00.")

    import time
    payload = {
        "type": "upi_qr",
        "name": f"QueueFree Order {order_id}",
        "usage": "single_use",
        "fixed_amount": True,
        "payment_amount": paise,
        "description": f"Queue-Free Print Order #QFP{int(order_id):06d}",
        "notes": {"queuefree_order_id": str(order_id)},
        "close_by": int(time.time()) + 7200,
    }
    response = requests.post(
        "https://api.razorpay.com/v1/payments/qr_codes",
        auth=(key_id, key_secret),
        json=payload,
        timeout=15,
    )
    try:
        data = response.json()
    except ValueError:
        data = {}
    if response.status_code >= 400 or not data.get("id") or not data.get("image_url"):
        error = data.get("error", {}) if isinstance(data, dict) else {}
        message = error.get("description") if isinstance(error, dict) else None
        raise RuntimeError(message or "Razorpay could not create the UPI QR. Enable Razorpay UPI QR for this account if required.")
    return data


def fetch_razorpay_qr(key_id, key_secret, qr_id):
    response = requests.get(
        f"https://api.razorpay.com/v1/payments/qr_codes/{qr_id}",
        auth=(key_id, key_secret),
        timeout=15,
    )
    try:
        data = response.json()
    except ValueError:
        data = {}
    if response.status_code >= 400 or not data.get("id"):
        error = data.get("error", {}) if isinstance(data, dict) else {}
        message = error.get("description") if isinstance(error, dict) else None
        raise RuntimeError(message or "Razorpay could not fetch the UPI QR status.")
    return data


def fetch_razorpay_qr_payments(key_id, key_secret, qr_id):
    response = requests.get(
        f"https://api.razorpay.com/v1/payments/qr_codes/{qr_id}/payments",
        auth=(key_id, key_secret),
        timeout=15,
    )
    try:
        data = response.json()
    except ValueError:
        data = {}
    if response.status_code >= 400:
        error = data.get("error", {}) if isinstance(data, dict) else {}
        message = error.get("description") if isinstance(error, dict) else None
        raise RuntimeError(message or "Razorpay could not fetch QR payments.")
    return data if isinstance(data, dict) else {}


def ensure_edit_tracking_schema(cursor):
    """Track edits so the admin live feed can show a changed pending order immediately."""
    cursor.execute("""
        SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'orders'
          AND COLUMN_NAME = 'last_edited_at'
    """)
    if not cursor.fetchone():
        cursor.execute("ALTER TABLE orders ADD COLUMN last_edited_at TIMESTAMP NULL DEFAULT NULL")


def ensure_delivery_schema(cursor):
    """Add secure QR-delivery fields and the delivered status to an existing database."""
    cursor.execute("""
        SELECT COUNT(*) AS c
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = 'orders'
          AND COLUMN_NAME = 'delivery_token'
    """)
    if not cursor.fetchone()["c"]:
        cursor.execute("ALTER TABLE orders ADD COLUMN delivery_token VARCHAR(128) NULL")
        cursor.execute("ALTER TABLE orders ADD UNIQUE KEY uq_orders_delivery_token (delivery_token)")

    for name, definition in {
        "delivery_pdf_path": "VARCHAR(500) NULL",
        "delivered_at": "TIMESTAMP NULL",
    }.items():
        cursor.execute("""
            SELECT COUNT(*) AS c
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = 'orders'
              AND COLUMN_NAME = %s
        """, (name,))
        if not cursor.fetchone()["c"]:
            cursor.execute(f"ALTER TABLE orders ADD COLUMN {name} {definition}")

    cursor.execute("""
        SELECT COLUMN_TYPE
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = 'orders'
          AND COLUMN_NAME = 'order_status'
    """)
    row = cursor.fetchone()
    if row and "delivered" not in str(row["COLUMN_TYPE"]):
        cursor.execute("""
            ALTER TABLE orders
            MODIFY order_status
            ENUM('pending','accepted','printing','ready','completed','declined','cancelled','delivered')
            NOT NULL DEFAULT 'pending'
        """)

    cursor.execute("""
        SELECT COLUMN_TYPE
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = 'order_history'
          AND COLUMN_NAME IN ('old_status','new_status')
        LIMIT 1
    """)
    history_row = cursor.fetchone()
    if history_row and "delivered" not in str(history_row["COLUMN_TYPE"]):
        cursor.execute("""
            ALTER TABLE order_history
            MODIFY old_status
            ENUM('pending','accepted','printing','ready','completed','declined','cancelled','delivered') DEFAULT NULL
        """)
        cursor.execute("""
            ALTER TABLE order_history
            MODIFY new_status
            ENUM('pending','accepted','printing','ready','completed','declined','cancelled','delivered') NOT NULL
        """)


def make_delivery_token():
    """Generate a high-entropy, non-sequential token for one order."""
    return secrets.token_urlsafe(32)


def _find_soffice():
    candidates = [
        shutil.which("soffice"), shutil.which("soffice.exe"),
        shutil.which("libreoffice"), shutil.which("libreoffice.exe"),
        r"C:\Program Files\LibreOffice\program\soffice.exe",
        r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
    ]
    return next((x for x in candidates if x and Path(x).exists()), None)


def _printable_pdf(source_path):
    """Return a PDF path for PDF/image/Office files. Temporary conversions are cleaned later."""
    source_path = Path(source_path)
    if source_path.suffix.lower() == ".pdf":
        return source_path, None

    temp_dir = Path(tempfile.mkdtemp(prefix="queuefree_print_"))
    try:
        # Images can be converted without LibreOffice.
        if Image and source_path.suffix.lower() in {".png",".jpg",".jpeg",".webp",".bmp",".gif",".tif",".tiff"}:
            out = temp_dir / "image.pdf"
            with Image.open(source_path) as img:
                img.convert("RGB").save(out, "PDF", resolution=150.0)
            return out, temp_dir

        soffice = _find_soffice()
        if not soffice:
            raise RuntimeError("LibreOffice is required to prepare non-PDF print documents.")
        profile = temp_dir / "lo_profile"
        profile.mkdir()
        subprocess.run([
            soffice, f"-env:UserInstallation={profile.as_uri()}",
            "--headless", "--convert-to", "pdf", "--outdir", str(temp_dir), str(source_path)
        ], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180, text=True)
        pdfs = list(temp_dir.glob("*.pdf"))
        if not pdfs:
            raise RuntimeError(f"Could not convert {source_path.name} to PDF.")
        return pdfs[0], temp_dir
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise


def build_delivery_pdf(order_id, token, student, shop, order, items):
    """
    Build the exact printable packet:
    QR cover page + original document pages. The originals are never modified.
    A cover is inserted before each copy of the complete document set.
    """
    if not qrcode or not canvas or PdfReader is None:
        raise RuntimeError("QR delivery requires qrcode, reportlab and pypdf. Run pip install -r requirements.txt")

    from pypdf import PdfWriter
    packet_dir = UPLOAD_ROOT / str(order["user_id"]) / f"order_{order_id}"
    packet_dir.mkdir(parents=True, exist_ok=True)
    output = packet_dir / f"QueueFree_Order_{order_id}_Printout.pdf"

    # Create a fresh cover for this order.
    cover_path = packet_dir / "QueueFree_QR_Cover.pdf"
    qr_url = url_for("verify_delivery", token=token, _external=True)
    qr_img = qrcode.make(qr_url)
    qr_bytes = io.BytesIO()
    qr_img.save(qr_bytes, format="PNG")
    qr_bytes.seek(0)

    c = canvas.Canvas(str(cover_path), pagesize=A4)
    width, height = A4
    c.setTitle(f"QueueFree Order {order_id}")
    c.setFont("Helvetica-Bold", 24)
    c.drawString(55, height - 65, "QueueFree")
    c.setFont("Helvetica-Bold", 16)
    c.drawString(55, height - 100, "Secure Printout Delivery")
    c.setFont("Helvetica", 11)
    y = height - 145
    details = [
        ("Order ID", f"ORD{order_id:03d}"),
        ("Student", student.get("name") or "Student"),
        ("Email", student.get("email") or "-"),
        ("Phone", student.get("phone") or "-"),
        ("Shop ID", shop.get("shop_number") or str(shop.get("shop_id") or "-")),
        ("Print Details", f"{order.get('copies',1)} copy/copies | {order.get('color','black_white').replace('_',' ').title()} | {order.get('print_side','single').title()} | {order.get('orientation','portrait').title()}"),
        ("Paper Type", order.get("paper_type") or "A4"),
        ("Page Range", order.get("page_range") or "All"),
    ]
    for label, value in details:
        c.setFont("Helvetica-Bold", 10)
        c.drawString(55, y, f"{label}:")
        c.setFont("Helvetica", 10)
        c.drawString(145, y, str(value)[:90])
        y -= 22

    c.drawImage(ImageReader(qr_bytes), width - 190, height - 355, width=135, height=135, preserveAspectRatio=True, mask="auto")
    c.setFont("Helvetica-Bold", 12)
    c.drawString(width - 205, height - 380, "SCAN TO COLLECT")
    c.setFont("Helvetica", 9)
    c.drawString(55, 85, "Scan this QR using the logged-in student's device.")
    c.drawString(55, 70, "The original document begins after this page.")
    c.save()

    writer = PdfWriter()
    temp_dirs = []
    try:
        prepared = []
        for item in items:
            src = Path(app.root_path) / item["file_path"]
            if not src.is_file():
                raise FileNotFoundError(f"Document file not found: {item['file_name']}")
            pdf, temp_dir = _printable_pdf(src)
            prepared.append(pdf)
            if temp_dir:
                temp_dirs.append(temp_dir)

        copies = max(1, int(order.get("copies") or 1))
        for _ in range(copies):
            writer.append(str(cover_path))
            for pdf in prepared:
                writer.append(str(pdf))

        with open(output, "wb") as fh:
            writer.write(fh)
    finally:
        for d in temp_dirs:
            shutil.rmtree(d, ignore_errors=True)

    return output


def prepare_delivery_packet(cursor, order_id):
    """Create/recreate the QR packet for a specific order and return its URL/path."""
    ensure_delivery_schema(cursor)
    cursor.execute("""
        SELECT o.*, u.name AS student_name, u.email AS student_email, u.phone AS student_phone,
               s.shop_id AS actual_shop_id, s.shop_number, s.shop_name
        FROM orders o
        JOIN users u ON u.user_id = o.user_id
        JOIN print_shops s ON s.shop_id = o.shop_id
        WHERE o.order_id = %s
        LIMIT 1
    """, (order_id,))
    order = cursor.fetchone()
    if not order:
        raise RuntimeError("Order not found.")

    token = order.get("delivery_token")
    if not token:
        token = make_delivery_token()
        cursor.execute("UPDATE orders SET delivery_token=%s WHERE order_id=%s", (token, order_id))

    cursor.execute("""
        SELECT file_name, file_path, copies, total_pages
        FROM order_items
        WHERE order_id=%s
        ORDER BY item_id
    """, (order_id,))
    items = cursor.fetchall()
    if not items:
        raise RuntimeError("No document files were found for this order.")

    pdf_path = build_delivery_pdf(
        order_id, token,
        {"name": order["student_name"], "email": order["student_email"], "phone": order["student_phone"]},
        {"shop_id": order["actual_shop_id"], "shop_number": order["shop_number"], "shop_name": order["shop_name"]},
        order, items
    )
    rel = str(pdf_path.relative_to(app.root_path)).replace("\\", "/")
    cursor.execute("UPDATE orders SET delivery_pdf_path=%s WHERE order_id=%s", (rel, order_id))
    return rel


def ensure_order_item_columns(cursor):
    """Keep the existing database compatible while adding new order-item fields."""
    columns = {
        "shop_id": "INT UNSIGNED NULL",
        "orientation": "VARCHAR(30) NULL",
        "print_side": "VARCHAR(30) NULL",
        "paper_type": "VARCHAR(50) NULL",
        "additional_requirements": "TEXT NULL",
        "total_pages": "INT UNSIGNED NULL",
        "blank_pages": "INT UNSIGNED NULL",
        "blurry_pages": "INT UNSIGNED NULL",
        "unrecognizable_pages": "INT UNSIGNED NULL",
        "uploaded_at": "DATETIME NULL",
    }
    for name, definition in columns.items():
        cursor.execute(
            """
            SELECT COUNT(*) AS c FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'order_items'
              AND COLUMN_NAME = %s
            """,
            (name,),
        )
        if not cursor.fetchone()["c"]:
            cursor.execute(f"ALTER TABLE order_items ADD COLUMN {name} {definition}")


def backfill_uploaded_at(cursor):
    """Set retention timestamps for older rows using their file mtime or order creation time."""
    cursor.execute("""
        SELECT oi.item_id, oi.file_path, o.created_at
        FROM order_items oi
        LEFT JOIN orders o ON o.order_id = oi.order_id
        WHERE oi.uploaded_at IS NULL
    """)
    rows = cursor.fetchall()
    for row in rows:
        timestamp = row.get("created_at") or datetime.now()
        path = _safe_upload_path(row.get("file_path"))
        if path and path.is_file():
            try:
                timestamp = datetime.fromtimestamp(path.stat().st_mtime)
            except OSError:
                pass
        cursor.execute("UPDATE order_items SET uploaded_at=%s WHERE item_id=%s", (timestamp, row["item_id"]))



def parse_ai_json(text):
    """Extract JSON even if Gemini wraps it in markdown fences."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except Exception:
        match = re.search(r"\{.*\}", text, flags=re.S)
        if match:
            try:
                return json.loads(match.group(0))
            except Exception:
                pass
    return None


def prepare_file_for_gemini(file_path, mime_type):
    """Return (path, mime_type, cleanup_path). Convert printable office formats to PDF."""
    suffix = Path(file_path).suffix.lower()
    if mime_type in DIRECT_ANALYSIS_MIMES:
        # Do not run LibreOffice for PDFs/images/text/CSV. Conversion was an unnecessary
        # source of delay in Edit Order, especially for TXT/CSV files.
        return Path(file_path), mime_type, None

    if suffix not in CONVERTIBLE_EXTENSIONS:
        # Try Gemini directly for any MIME type it may support. This also keeps
        # uncommon printable formats from being rejected unnecessarily.
        return Path(file_path), mime_type, None

    soffice = "libreoffice"
    output_dir = Path(file_path).parent / f"converted_{uuid.uuid4().hex}"
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            [soffice, "--headless", "--convert-to", "pdf", "--outdir", str(output_dir), str(file_path)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=45,
        )
        pdf_path = output_dir / f"{Path(file_path).stem}.pdf"
        if not pdf_path.exists():
            pdfs = list(output_dir.glob("*.pdf"))
            if pdfs:
                pdf_path = pdfs[0]
        if pdf_path.exists():
            return pdf_path, "application/pdf", output_dir
    except Exception:
        pass

    try:
        import shutil
        shutil.rmtree(output_dir, ignore_errors=True)
    except Exception:
        pass
    return Path(file_path), mime_type, None


def analyze_with_gemini(file_path, mime_type, model=None):
    """Analyze printable document/image formats with Gemini."""
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is not configured.")
    if genai is None:
        raise RuntimeError("google-genai is not installed. Run: pip install google-genai")

    analysis_path, analysis_mime, cleanup_path = prepare_file_for_gemini(file_path, mime_type)
    try:
        # PDFs, images and text files go directly to Gemini; office files are
        # converted to PDF first, allowing DOCX/XLSX/PPTX/ODT/RTF and similar
        # formats to be scanned using the same reliable page-based analysis.
        client = genai.Client(api_key=GEMINI_API_KEY)
        uploaded = client.files.upload(
            file=str(analysis_path),
            config={"mime_type": analysis_mime} if analysis_mime else None,
        )
        prompt = """
Analyze this print document for a college printing system. Return ONLY valid JSON with exactly these keys:
{
  "total_pages": number,
  "blank_pages": number,
  "duplicate_pages": number,
  "blurry_pages": number,
  "unrecognizable_pages": number,
  "summary": "short human-readable summary"
}
Rules:
- total_pages = actual number of printable pages after rendering the uploaded file.
- blank_pages = pages that are effectively empty or contain no meaningful printable content.
- duplicate_pages = pages that repeat the same printable content as another page in this document; count the repeated copy, not the first occurrence.
- blurry_pages = pages where visible text/content is actually too blurry or low quality to print reliably. Do NOT call a page blurry merely because it is text-only, has a small image, or has low extracted-text content.
- unrecognizable_pages = pages where visible text/content cannot be reliably read or recognized.
- Inspect the rendered page content, not only the PDF text layer. A scanned page with no extractable text is NOT automatically blank or unrecognizable.
- Do not count the same page in a category more than once unless it genuinely has multiple problems.
- For text-only files without pages, use total_pages=1.
- Use 0 when none are detected.
"""
        response = client.models.generate_content(
            model=model or GEMINI_MODEL,
            contents=[prompt, uploaded],
        )
        data = parse_ai_json(getattr(response, "text", ""))
        if not data:
            raise RuntimeError("Gemini returned an invalid analysis response.")
        return {
            "total_pages": max(1, int(data.get("total_pages", 1))),
            "blank_pages": max(0, int(data.get("blank_pages", 0))),
            "duplicate_pages": max(0, int(data.get("duplicate_pages", 0))),
            "blurry_pages": max(0, int(data.get("blurry_pages", 0))),
            "unrecognizable_pages": max(0, int(data.get("unrecognizable_pages", 0))),
            "summary": str(data.get("summary", "Analysis completed.")),
        }
    finally:
        if cleanup_path:
            try:
                import shutil
                shutil.rmtree(cleanup_path, ignore_errors=True)
            except Exception:
                pass


def local_page_count(file_path, mime_type):
    if mime_type == "application/pdf" and PdfReader:
        try:
            return len(PdfReader(str(file_path)).pages)
        except Exception:
            return 1
    return 1


# ============================================================
# SESSION HELPERS
# ============================================================

def ensure_logged_in_user():
    """Return True when the current session belongs to a User.

    This also repairs older/stale sessions where user_id exists but the
    role was not stored correctly. Admin sessions are never accepted here.
    """
    user_id = session.get("user_id")
    if not user_id:
        return False

    role = str(session.get("role", "")).strip().lower()
    if role == "admin":
        return False
    if role == "user":
        return True

    # Recover the role from the users table for an older session.
    conn = cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT user_id, name, email, role FROM users WHERE user_id=%s LIMIT 1",
            (user_id,),
        )
        account = cursor.fetchone()
        if account and str(account.get("role", "")).strip().lower() == "user":
            session["user_id"] = account["user_id"]
            session["name"] = account["name"]
            session["email"] = account["email"]
            session["role"] = "user"
            session.modified = True
            return True
    except Error:
        pass
    finally:
        close_db(conn, cursor)
    return False


# ============================================================
# USER DASHBOARD
# ============================================================


# ============================================================
# AI GOVERNMENT DOCUMENT ASSISTANT
# ============================================================

GOVERNMENT_ASSISTANT_SYSTEM_PROMPT = """
You are QueueFree Print's AI Government Document & Service Assistant for INDIA.

You are a GENERAL government-service assistant, not a small FAQ bot. You must understand questions about ANY Indian government document, certificate, ID, licence, registration, benefit, scheme, portal service, renewal, correction, status check, appointment, download, or application route that you can reasonably identify. Do not limit yourself to a hard-coded list.

Examples include Aadhaar, PAN, Passport, Driving Licence, Voter ID/EPIC, birth/death certificates, caste certificate, income certificate, domicile/residence certificate, EWS certificate, disability certificate/UDID, ration card, e-Shram, Ayushman Bharat, PM-KISAN, EPFO/PF, UAN, pension, GST, MSME/Udyam, vehicle RC, learner licence, pollution certificate, marriage certificate, land/revenue records, scholarships, exam certificates, government forms, DigiLocker documents, state e-District services, and other central/state/local government services. This list is illustrative, NOT exhaustive.

Your job is ONLY to explain what government service/document the user means and how to obtain, download, renew, correct, verify, or use it. You do NOT submit applications, issue certificates, approve documents, or claim to have completed a government service.

IMPORTANT:
- Understand natural language, spelling mistakes, abbreviations, English, Hindi, Hinglish, and other languages/scripts supported by the model.
- Resolve intent precisely: APPLY/NEW, DOWNLOAD, RENEW, CORRECT/UPDATE, REPLACE, VERIFY/STATUS, APPOINTMENT, ELIGIBILITY, DOCUMENTS REQUIRED, FEE, or PRINT.
- "How can I download my driving licence?" is a DOWNLOAD/ACCESS request, not an application request.
- "How can I apply for a driving licence?" is an APPLICATION request, not a download request.
- Answer the user's actual latest question; do not reuse an unrelated service from previous conversation.
- Respond in the SAME language as the user's latest message. For Hinglish, use natural Hinglish. Preserve official names and URLs.
- If a state/district is needed to give exact requirements, say that the rules vary and ask for the state only when necessary. Do not invent state-specific rules.

For every answer:
1. Identify the exact likely document/service and intent.
2. Give a short explanation.
3. Give eligibility/conditions when known; clearly mark state/district/service variation.
4. Give a practical current document checklist.
5. Give simple numbered application/download/renewal/correction steps.
6. Explain the correct official online portal, official app, authorised service centre, or department office.
7. Include fee/processing time only when reliably known; otherwise say to verify on the official portal.
8. Explain useful printing/scanning requirements for QueueFree.
9. Give official government sources only. Prefer .gov.in, .nic.in, india.gov.in, services.india.gov.in, official state-government domains, UIDAI, Parivahan, Income Tax, Passport Seva, Election Commission, DigiLocker, EPFO, GST, Udyam, etc.
10. Never invent a direct service URL. If unsure, link to the official department homepage and explain what service name to search.
11. Never ask the user for Aadhaar number, OTP, password, PIN, bank password, card details, or other secrets.
12. Never tell the user to upload sensitive identity documents into the chatbot.
13. Never claim QueueFree submits, approves, issues, or verifies a government document.
14. Do not answer with a generic "open the government portal and search" response when the requested service can be identified.
15. If the service cannot be confidently identified, state the likely possibilities and ask one short clarification.

Return ONLY valid JSON:
{
  "title": "short service/document name",
  "summary": "specific answer to the user's actual question",
  "eligibility": ["..."],
  "documents": ["..."],
  "steps": ["..."],
  "route": "official online portal / official app / service centre / office / mixed",
  "printing_requirements": ["..."],
  "sources": [{"name":"Official source name","url":"https://..."}],
  "disclaimer": "Requirements can change; verify the latest details on the linked official government source."
}
"""

def _clean_government_assistant_url(url):
    """Allow only likely official government URLs in assistant source cards."""
    from urllib.parse import urlparse
    try:
        parsed = urlparse(str(url or "").strip())
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme != "https" or not host:
            return ""
        official = (
            host.endswith(".gov.in")
            or host.endswith(".nic.in")
            or host in {
                "india.gov.in", "www.india.gov.in",
                "services.india.gov.in", "www.services.india.gov.in",
                "mygov.in", "www.mygov.in",
                "digilocker.gov.in", "www.digilocker.gov.in",
                "uidai.gov.in", "www.uidai.gov.in",
            }
        )
        return parsed.geturl() if official else ""
    except Exception:
        return ""

def _fallback_government_sources():
    return [
        {"name": "National Government Services Portal", "url": "https://services.india.gov.in/"},
        {"name": "National Portal of India", "url": "https://www.india.gov.in/"},
        {"name": "DigiLocker", "url": "https://www.digilocker.gov.in/"},
    ]

def _government_assistant_fallback(question):
    """Reliable official-source fallback with intent-aware Aadhaar guidance."""
    q=(question or "").lower().strip()
    aadhaar=any(x in q for x in ("aadhaar", "aadhar", "uidai", "e-aadhaar", "eaadhaar"))
    apply_words=("apply", "get a new", "new aadhaar", "enrol", "enrollment", "enrolment", "make an aadhaar", "create an aadhaar")
    download_words=("download", "get pdf", "pdf", "e-aadhaar", "eaadhaar", "print my aadhaar")
    if aadhaar and any(x in q for x in apply_words):
        return {
            "title":"Apply / Enrol for a New Aadhaar",
            "summary":"For a new Aadhaar, you need to complete Aadhaar enrolment in person at an authorised Aadhaar Enrolment Centre. UIDAI provides the official enrolment information, accepted-document list and centre-locator resources.",
            "eligibility":["Aadhaar enrolment is for residents who do not already have an Aadhaar number; enrolment is also available through UIDAI's specified processes for eligible residents and categories.","The exact document requirements can depend on the enrolment category and the documents you can provide."],
            "documents":["A valid Proof of Identity (PoI) document accepted by UIDAI","A valid Proof of Address (PoA) document accepted by UIDAI, where applicable","Additional documents may be required depending on the enrolment category; check UIDAI's current accepted-document list before visiting."],
            "steps":["Open the official UIDAI My Aadhaar website.","Use the Aadhaar Enrolment / Book Appointment or Aadhaar Seva Kendra option to find an authorised enrolment centre.","Check UIDAI's current List of Acceptable Documents and carry the required original documents.","Visit the authorised Aadhaar Enrolment Centre and provide the required demographic details and biometric information as instructed by the operator.","Collect the enrolment acknowledgement containing your Enrolment ID (EID) and keep it safely.","Use UIDAI's official status service to track the enrolment. After successful enrolment, use the official My Aadhaar service to access your Aadhaar services."],
            "route":"In-person at an authorised Aadhaar Enrolment Centre / Aadhaar Seva Kendra. Appointment availability and centre options should be checked on UIDAI's official portal.",
            "printing_requirements":["Print or carry the required supporting documents only if the centre requires physical copies; check the current UIDAI document list first.","QueueFree can print a downloaded UIDAI form or supporting document if you need a physical copy."],
            "sources":[
                {"name":"UIDAI — My Aadhaar","url":"https://uidai.gov.in/en/my-aadhaar"},
                {"name":"UIDAI — Enrolment & Updates","url":"https://uidai.gov.in/en/my-aadhaar/about-your-aadhaar/enrolment-update"},
                {"name":"UIDAI — Official Home / Find Aadhaar Centre","url":"https://www.uidai.gov.in/"}
            ],
            "disclaimer":"Requirements and centre procedures can change. Verify the latest accepted documents and appointment/centre details on UIDAI before visiting. QueueFree does not submit or issue Aadhaar."
        }
    if aadhaar and any(x in q for x in download_words):
        return {
            "title":"Download e-Aadhaar",
            "summary":"If you already have an Aadhaar number or eligible enrolment details, you can download e-Aadhaar through UIDAI's official MyAadhaar service.",
            "eligibility":["You should already have an Aadhaar number, or eligible enrolment/VID details."],
            "documents":["Aadhaar number, or eligible enrolment/VID details","Access to the mobile number registered with Aadhaar for OTP verification"],
            "steps":["Open the official UIDAI MyAadhaar portal.","Select the Download Aadhaar service.","Enter the requested Aadhaar, enrolment or VID details.","Complete the OTP verification sent to the registered mobile number.","Download the e-Aadhaar PDF."],
            "route":"Official online UIDAI MyAadhaar portal; the official Aadhaar app also provides e-Aadhaar services.",
            "printing_requirements":["You can upload the downloaded PDF to QueueFree Print if you need a physical copy.","Do not share your Aadhaar number or OTP with QueueFree or any third party."],
            "sources":[
                {"name":"UIDAI — My Aadhaar","url":"https://uidai.gov.in/en/my-aadhaar"},
                {"name":"UIDAI — e-Aadhaar FAQ","url":"https://uidai.gov.in/en/283-faqs/aadhaar-online-services/e-aadhaar/1888-from-where-resident-can-download-e-aadhaar.html"},
                {"name":"UIDAI — MyAadhaar Portal","url":"https://myaadhaar.uidai.gov.in/"}
            ],
            "disclaimer":"Government requirements and portal screens can change. Verify the latest details on the official UIDAI source before proceeding."
        }
    if aadhaar:
        return {
            "title":"Aadhaar — Please Specify the Service",
            "summary":"I can help with Aadhaar enrolment, downloading e-Aadhaar, updating details, checking status, or finding an Aadhaar centre. Tell me which Aadhaar service you need.",
            "eligibility":[],"documents":[],
            "steps":["For a new Aadhaar: ask 'How can I apply for a new Aadhaar?'","For an existing Aadhaar: ask 'How can I download my Aadhaar?'","For changes: ask 'How can I update my Aadhaar address?'"],
            "route":"Depends on the Aadhaar service; use UIDAI's official My Aadhaar portal as the starting point.",
            "printing_requirements":[],
            "sources":[{"name":"UIDAI — My Aadhaar","url":"https://uidai.gov.in/en/my-aadhaar"},{"name":"UIDAI — Official Home","url":"https://www.uidai.gov.in/"}],
            "disclaimer":"Verify the latest requirements on UIDAI's official website. QueueFree does not submit or issue Aadhaar."
        }
    return {
        "title":"Government Service Guidance",
        "summary":"The AI service is temporarily unavailable, but you can still start from the official government portals below. QueueFree can explain the service and help you print documents; it does not submit or issue government documents.",
        "eligibility":[],"documents":[],
        "steps":["Open the relevant official government portal.","Search for the required document or service.","Follow the current instructions shown by the government department."],
        "route":"Official government portal / service centre / department office, depending on the service.",
        "printing_requirements":["QueueFree Print can print forms or documents after you obtain them from the official source."],
        "sources":_fallback_government_sources(),
        "disclaimer":"Requirements can change; verify the latest details on the linked official government source."
    }

def ensure_government_assistant_schema(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS government_assistant_chats (
            chat_id INT UNSIGNED NOT NULL AUTO_INCREMENT,
            user_id INT UNSIGNED NOT NULL,
            title VARCHAR(255) NOT NULL DEFAULT 'New Government Assistant Chat',
            messages JSON NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (chat_id),
            KEY idx_gov_chat_user_updated (user_id, updated_at),
            CONSTRAINT fk_gov_chat_user FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE ON UPDATE CASCADE
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)


GOVERNMENT_SERVICE_CATALOG = {
    "driving licence": {
        "keywords": ["driving licence","driving license","dl","dl download","license download","licence download"],
        "source": ("Parivahan Sewa", "https://parivahan.gov.in/"),
        "summary": "For a digital Driving Licence, use the official Parivahan services or DigiLocker. The exact option depends on whether you want to view/download an issued licence, check status, or apply for a new licence.",
        "steps_download": ["Open the official Parivahan Sewa portal or DigiLocker.", "Choose the Driving Licence / document service and complete the requested licence and identity verification details.", "If the document is available, view or download the digital copy. For DigiLocker, retrieve the issued Driving Licence from the appropriate issuer/service.", "If the licence is not found, use the official Parivahan status/help services rather than an unofficial website."],
        "steps_apply": ["Open the official Parivahan Sewa portal.", "Choose the relevant Driving Licence/Learner Licence service for your state.", "Complete the online application and appointment/fee steps shown by Parivahan.", "Attend the required test/verification appointment if instructed.", "Track the application through the official Parivahan service."]
    },
    "aadhaar": {
        "keywords": ["aadhaar","aadhar","uidai","e-aadhaar","eaadhaar"],
        "source": ("UIDAI — My Aadhaar", "https://uidai.gov.in/en/my-aadhaar"),
        "summary": "UIDAI provides separate services for new Aadhaar enrolment, updates, status checks and e-Aadhaar access. The correct route depends on whether you are applying for a new Aadhaar or accessing an already issued Aadhaar.",
        "steps_download": ["Open UIDAI My Aadhaar.", "Choose the e-Aadhaar/download service and complete the verification requested by UIDAI.", "Download the available e-Aadhaar and keep it securely."],
        "steps_apply": ["Open UIDAI My Aadhaar and use the enrolment/appointment or Aadhaar Seva Kendra information.", "Check UIDAI's current acceptable-document list.", "Visit an authorised enrolment centre with the required original documents and complete the demographic/biometric enrolment.", "Keep the enrolment acknowledgement/EID and use UIDAI's official status service."]
    },
    "pan": {
        "keywords": ["pan card","pan","permanent account number"],
        "source": ("Income Tax Department — PAN", "https://www.incometax.gov.in/"),
        "summary": "PAN application, correction and e-PAN access are handled through official Income Tax/PAN service routes. The exact route depends on whether you need a new PAN, correction, reprint or e-PAN.",
        "steps_download": ["Open the official Income Tax/PAN service route or the official PAN issuer service linked from the government portal.", "Choose the e-PAN/reprint or relevant access option.", "Complete the requested verification and download the document if available."],
        "steps_apply": ["Open the official Income Tax/PAN service route.", "Choose the appropriate new PAN application option.", "Enter the requested details and submit the application through the official process.", "Complete any identity/document verification requested and retain the acknowledgement."]
    },
    "passport": {
        "keywords": ["passport","passport renewal","renew passport"],
        "source": ("Passport Seva", "https://www.passportindia.gov.in/"),
        "summary": "Passport Seva provides official passport application, renewal, appointment and status services.",
        "steps_download": ["For an issued passport, use the official Passport Seva/DigiLocker service where a digital document is available; otherwise use the passport details for the official service you need."],
        "steps_apply": ["Register/login on Passport Seva.", "Choose fresh passport or re-issue/renewal as applicable.", "Complete the application and fee steps.", "Book an appointment at the designated Passport Seva facility.", "Carry the documents listed by Passport Seva and follow police verification/processing instructions."]
    },
    "voter id": {
        "keywords": ["voter id","voter card","epic","epic card","voter id download"],
        "source": ("Election Commission of India", "https://www.eci.gov.in/"),
        "summary": "The Election Commission provides official voter registration, correction, status and e-EPIC services.",
        "steps_download": ["Open the official Election Commission voter services route.", "Use the e-EPIC/download option when eligible and complete the requested verification.", "Download or save the digital EPIC where available."],
        "steps_apply": ["Use the official voter services route to register as a new voter.", "Complete the relevant registration form and upload/submit the requested documents.", "Track the application through the official voter service."]
    },
    "digilocker": {
        "keywords": ["digilocker","digital documents","issued documents"],
        "source": ("DigiLocker", "https://www.digilocker.gov.in/"),
        "summary": "DigiLocker lets users access issued digital documents from participating government departments and issuers.",
        "steps_download": ["Open DigiLocker and sign in.", "Go to issued documents/search for the required issuer and document.", "Complete the requested verification and fetch the issued document.", "Download or share the issued document as permitted by DigiLocker."]
    },
    "udyam": {
        "keywords": ["udyam","msme registration","msme certificate","udyam registration"],
        "source": ("Udyam Registration", "https://udyamregistration.gov.in/"),
        "summary": "Udyam Registration is the official MSME registration route. Use the official Udyam portal for new registration, updates and certificate access.",
        "steps_download": ["Open the official Udyam portal and use the appropriate print/download/verification service for an existing registration.", "Complete the requested verification to access the certificate."],
        "steps_apply": ["Open the official Udyam Registration portal.", "Choose the new registration route.", "Enter the requested enterprise details and complete the official verification/submission steps.", "Save the acknowledgement and certificate details."]
    },
    "gst": {
        "keywords": ["gst","gst registration","gst certificate"],
        "source": ("GST Portal", "https://www.gst.gov.in/"),
        "summary": "GST registration and taxpayer services are provided through the official GST portal.",
        "steps_download": ["Open the official GST portal and sign in.", "Use the relevant certificate/profile/download service for the registered taxpayer.", "Download the available GST certificate or document."],
        "steps_apply": ["Open the official GST portal.", "Choose the registration service and complete the application details.", "Upload the documents requested by the portal and complete verification.", "Track the application using the official GST service."]
    },
    "epfo": {
        "keywords": ["epfo","pf","provident fund","uan","pf passbook"],
        "source": ("EPFO", "https://www.epfindia.gov.in/"),
        "summary": "EPFO provides official UAN, provident-fund, passbook and claim-related services.",
        "steps_download": ["Open the official EPFO member/service portal.", "Sign in using the applicable official credentials.", "Use the passbook or document service available for your account and download the required record."]
    },
    "ration card": {
        "keywords": ["ration card","ration card download","food card"],
        "source": ("National Food Security Portal", "https://nfsa.gov.in/"),
        "summary": "Ration-card application and access are generally handled by the relevant state/UT food and civil-supplies authority. The exact portal and requirements vary by state.",
        "steps_download": ["Open the official state food/civil-supplies portal or the National Food Security Portal starting point.", "Use the state service to check the ration-card record or available download/print option.", "Follow the current state instructions."]
    },
    "birth certificate": {
        "keywords": ["birth certificate","birth registration","janam praman"],
        "source": ("National Government Services Portal", "https://services.india.gov.in/"),
        "summary": "Birth registration/certificate services are handled by the relevant local or state authority. The exact portal varies by place of birth/registration.",
        "steps_download": ["Open the National Government Services Portal or your official state/local civil-registration portal.", "Search for Birth Certificate/Birth Registration.", "Use the official record lookup/download/print service if available."]
    },
    "death certificate": {
        "keywords": ["death certificate","death registration"],
        "source": ("National Government Services Portal", "https://services.india.gov.in/"),
        "summary": "Death registration/certificate services are handled by the relevant local or state authority and can vary by place of registration.",
        "steps_download": ["Open the National Government Services Portal or the official state/local civil-registration portal.", "Search for Death Certificate.", "Use the official record lookup/download/print option where available."]
    },
    "caste certificate": {
        "keywords": ["caste certificate","sc certificate","st certificate","obc certificate"],
        "source": ("National Government Services Portal", "https://services.india.gov.in/"),
        "summary": "Caste-certificate application and issuance are handled by the relevant state/district authority. Eligibility, documents and portal route vary by state.",
        "steps_apply": ["Open the National Government Services Portal and select your state, or use the official state e-District portal.", "Search for Caste Certificate.", "Check the current state-specific eligibility and document list.", "Submit through the official online/service-centre route and track the application."]
    },
    "income certificate": {
        "keywords": ["income certificate","income proof certificate"],
        "source": ("National Government Services Portal", "https://services.india.gov.in/"),
        "summary": "Income-certificate services are normally provided by the relevant state/district revenue or e-District authority. Requirements vary by state.",
        "steps_apply": ["Open the National Government Services Portal or the official state e-District portal.", "Search for Income Certificate.", "Check the state-specific document list and eligibility.", "Apply through the official route and retain the acknowledgement."]
    },
    "domicile certificate": {
        "keywords": ["domicile certificate","residence certificate","resident certificate"],
        "source": ("National Government Services Portal", "https://services.india.gov.in/"),
        "summary": "Domicile/residence certificates are handled by the relevant state or district authority and requirements vary by state.",
        "steps_apply": ["Open the National Government Services Portal or the official state e-District portal.", "Search for Domicile/Residence Certificate.", "Review the current state-specific eligibility and documents.", "Apply online or through the authorised service-centre route."]
    },
    "disability certificate": {
        "keywords": ["disability certificate","udid","unique disability id"],
        "source": ("UDID — Department of Empowerment of Persons with Disabilities", "https://www.swavlambancard.gov.in/"),
        "summary": "The UDID system is the official route for disability certificates/Unique Disability ID services.",
        "steps_apply": ["Open the official UDID portal.", "Register/login and select the applicable disability certificate/UDID service.", "Complete the application and assessment steps shown by the official portal.", "Track the application and access the certificate/card when issued."]
    },
    "vehicle rc": {
        "keywords": ["vehicle rc","rc book","registration certificate","rc download"],
        "source": ("Parivahan Sewa", "https://parivahan.gov.in/"),
        "summary": "Vehicle Registration Certificate services are provided through official Parivahan services and, where available, DigiLocker.",
        "steps_download": ["Open the official Parivahan or DigiLocker service.", "Choose the vehicle/RC service and complete the requested verification.", "Access or download the available digital RC."]
    },
    "learner licence": {
        "keywords": ["learner licence","learner license","learning licence","llr"],
        "source": ("Parivahan Sewa", "https://parivahan.gov.in/"),
        "summary": "Learner Licence application and related services are provided through official Parivahan services.",
        "steps_apply": ["Open the official Parivahan Sewa portal.", "Select your state and the Learner Licence service.", "Complete the application, document and fee steps shown by the portal.", "Complete the required test/appointment process and track the application."]
    }
}

def _catalog_government_fallback(question):
    """Specific official guidance when Gemini is unavailable; covers common services and
    still produces a service-specific answer for previously unseen services."""
    q = re.sub(r"\s+", " ", (question or "").strip().lower())
    matched = None
    for name, item in GOVERNMENT_SERVICE_CATALOG.items():
        if any(k in q for k in item["keywords"]):
            matched = (name, item)
            break
    if matched:
        name, item = matched
        download_intent = any(x in q for x in ("download", "pdf", "digital", "e-copy", "e copy", "print"))
        apply_intent = any(x in q for x in ("apply", "application", "new ", "get ", "register", "enrol", "renew", "renewal", "make "))
        if download_intent and item.get("steps_download"):
            steps=item["steps_download"]
            summary=item["summary"]
        elif apply_intent and item.get("steps_apply"):
            steps=item["steps_apply"]
            summary=item["summary"]
        else:
            steps=item.get("steps_apply") or item.get("steps_download") or [
                "Open the official source listed below.",
                "Search for the named service and follow the current instructions shown by the government authority."
            ]
            summary=item["summary"]
        return {
            "title": name.title(),
            "summary": summary,
            "eligibility": ["Eligibility and exact requirements can vary by service, state, category or issuing authority. Verify the current official requirements before applying."],
            "documents": ["Use the current document checklist shown by the official issuing authority; requirements differ by service and state."],
            "steps": steps,
            "route": "Official online portal / authorised service centre / office, depending on the service and state",
            "printing_requirements": ["QueueFree can print official forms, downloaded certificates/licences, acknowledgements or supporting documents after you obtain them from the official source."],
            "sources": [{"name":item["source"][0],"url":item["source"][1]}, {"name":"National Government Services Portal","url":"https://services.india.gov.in/"}],
            "disclaimer":"Requirements, fees and procedures can change. Verify the latest details on the linked official government source. QueueFree does not submit or issue government documents."
        }
    # General service-specific fallback for any identifiable document/service name.
    cleaned = re.sub(r"^(how can i|how do i|where can i|how to|i want to|i need to|mujhe|main|mai)\s+", "", q, flags=re.I)
    cleaned = re.sub(r"\b(download|apply|get|make|obtain|renew|renewal|certificate|document|card|licence|license)\b", " ", cleaned, flags=re.I)
    service = re.sub(r"\s+", " ", cleaned).strip()[:80] or "the requested government service"
    return {
        "title": service.title(),
        "summary": f"For {service}, use the official government department/service portal. The exact eligibility, documents and process depend on the issuing authority and, for many services, your state or district.",
        "eligibility": ["Eligibility depends on the issuing department and service; verify the current official requirements."],
        "documents": ["Check the official service's current document checklist before applying or visiting a service centre."],
        "steps": [
            "Open the National Government Services Portal and search for the exact service/document.",
            "Open the result that belongs to the relevant government department or official state portal.",
            "Review the current eligibility, required documents, fee and application/download instructions.",
            "Complete the official online process or use the authorised service-centre/office route shown by the department.",
            "Keep the acknowledgement/reference number and use the department's official status/download service when applicable."
        ],
        "route":"Official government portal / authorised service centre / department office, depending on the service",
        "printing_requirements":["QueueFree can print the official form, acknowledgement or document after you obtain it from the government source."],
        "sources":[{"name":"National Government Services Portal","url":"https://services.india.gov.in/"},{"name":"National Portal of India","url":"https://www.india.gov.in/"},{"name":"DigiLocker","url":"https://www.digilocker.gov.in/"}],
        "disclaimer":"Requirements can change; verify the latest details on the linked official government source. QueueFree does not submit or issue government documents."
    }

def _generate_government_answer(question, history):
    if not GEMINI_API_KEY or genai is None:
        raise RuntimeError("Gemini is unavailable")
    history=history or []
    conversation=[]
    for m in history[-8:]:
        if isinstance(m,dict) and m.get("role") in ("user","assistant") and m.get("content"):
            content=m.get("content")
            if isinstance(content,dict): content=json.dumps(content,ensure_ascii=False)
            conversation.append(f'{m["role"].upper()}: {str(content)[:3500]}')
    prompt=GOVERNMENT_ASSISTANT_SYSTEM_PROMPT
    if conversation: prompt += "\n\nPrevious conversation:\n"+"\n".join(conversation)
    prompt += "\n\nUser's latest question:\n"+question
    prompt += "\n\nReturn ONLY the JSON object requested by the system prompt."
    client=genai.Client(api_key=GEMINI_API_KEY)
    models=[]
    for m in [GEMINI_MODEL, os.getenv("GOVERNMENT_ASSISTANT_MODEL","gemini-2.5-flash-lite"), "gemini-2.5-flash", "gemini-2.0-flash"]:
        if m and m not in models: models.append(m)
    last=None
    for model in models:
        try:
            response=client.models.generate_content(model=model, contents=prompt)
            data=parse_ai_json(getattr(response,"text","") or "")
            if isinstance(data,dict): return data
            raise RuntimeError("Invalid AI JSON")
        except Exception as exc:
            last=exc
    raise RuntimeError(str(last) if last else "Gemini request failed")

def _format_government_answer(data):
    sources=[]
    for source in data.get("sources") or []:
        if isinstance(source,dict):
            url=_clean_government_assistant_url(source.get("url"))
            if url: sources.append({"name":str(source.get("name") or "Official government source")[:120],"url":url})
    return {
        "title":str(data.get("title") or "Government Document / Service").strip(),
        "summary":str(data.get("summary") or "").strip(),
        "eligibility":[str(x).strip() for x in (data.get("eligibility") or []) if str(x).strip()][:8],
        "documents":[str(x).strip() for x in (data.get("documents") or []) if str(x).strip()][:12],
        "steps":[str(x).strip() for x in (data.get("steps") or []) if str(x).strip()][:12],
        "route":str(data.get("route") or "Check the official government portal").strip(),
        "printing_requirements":[str(x).strip() for x in (data.get("printing_requirements") or []) if str(x).strip()][:8],
        "sources":(sources or _fallback_government_sources())[:6],
        "disclaimer":str(data.get("disclaimer") or "Requirements can change; verify the latest details on the linked official government source.").strip()
    }

@app.route("/api/government-assistant/chats", methods=["GET"])
def government_assistant_chats():
    if not ensure_logged_in_user() or session.get("role") != "user": return jsonify({"success":False,"error":"Please log in as a user."}),401
    conn=cursor=None
    try:
        conn=get_db_connection(); cursor=conn.cursor(dictionary=True); ensure_government_assistant_schema(cursor); conn.commit()
        cursor.execute("SELECT chat_id,title,created_at,updated_at FROM government_assistant_chats WHERE user_id=%s ORDER BY updated_at DESC LIMIT 30",(session["user_id"],))
        rows=cursor.fetchall()
        return jsonify({"success":True,"chats":[{"chat_id":r["chat_id"],"title":r["title"],"created_at":str(r["created_at"]),"updated_at":str(r["updated_at"])} for r in rows]})
    except Exception:
        app.logger.exception("Could not load government assistant chats")
        return jsonify({"success":False,"error":"Could not load recent chats."}),500
    finally: close_db(conn,cursor)

@app.route("/api/government-assistant/chats", methods=["POST"])
def government_assistant_new_chat():
    if not ensure_logged_in_user() or session.get("role") != "user": return jsonify({"success":False,"error":"Please log in as a user."}),401
    conn=cursor=None
    try:
        conn=get_db_connection(); cursor=conn.cursor(); ensure_government_assistant_schema(cursor)
        cursor.execute("INSERT INTO government_assistant_chats (user_id,title,messages) VALUES (%s,%s,%s)",(session["user_id"],"New Government Assistant Chat",json.dumps([])))
        chat_id=cursor.lastrowid; conn.commit(); return jsonify({"success":True,"chat_id":chat_id,"title":"New Government Assistant Chat"})
    except Exception:
        if conn: conn.rollback()
        app.logger.exception("Could not create government assistant chat")
        return jsonify({"success":False,"error":"Could not create a new chat."}),500
    finally: close_db(conn,cursor)

@app.route("/api/government-assistant/chats/<int:chat_id>", methods=["GET"])
def government_assistant_get_chat(chat_id):
    if not ensure_logged_in_user() or session.get("role") != "user": return jsonify({"success":False,"error":"Please log in as a user."}),401
    conn=cursor=None
    try:
        conn=get_db_connection(); cursor=conn.cursor(dictionary=True); ensure_government_assistant_schema(cursor); conn.commit()
        cursor.execute("SELECT chat_id,title,messages FROM government_assistant_chats WHERE chat_id=%s AND user_id=%s LIMIT 1",(chat_id,session["user_id"]))
        row=cursor.fetchone()
        if not row: return jsonify({"success":False,"error":"Chat not found."}),404
        try: messages=json.loads(row["messages"] or "[]")
        except Exception: messages=[]
        return jsonify({"success":True,"chat_id":row["chat_id"],"title":row["title"],"messages":messages})
    except Exception:
        app.logger.exception("Could not load government assistant chat")
        return jsonify({"success":False,"error":"Could not load this chat."}),500
    finally: close_db(conn,cursor)

@app.route("/api/government-assistant/chats/<int:chat_id>", methods=["DELETE"])
def government_assistant_delete_chat(chat_id):
    if not ensure_logged_in_user() or session.get("role") != "user":
        return jsonify({"success":False,"error":"Please log in as a user."}),401
    conn=cursor=None
    try:
        conn=get_db_connection(); cursor=conn.cursor()
        ensure_government_assistant_schema(cursor)
        cursor.execute("DELETE FROM government_assistant_chats WHERE chat_id=%s AND user_id=%s", (chat_id, session["user_id"]))
        if cursor.rowcount == 0:
            conn.rollback()
            return jsonify({"success":False,"error":"Chat not found."}),404
        conn.commit()
        return jsonify({"success":True,"chat_id":chat_id})
    except Exception:
        if conn: conn.rollback()
        app.logger.exception("Could not delete government assistant chat")
        return jsonify({"success":False,"error":"Could not delete this chat."}),500
    finally:
        close_db(conn,cursor)

@app.route("/api/government-assistant/chats/<int:chat_id>/message", methods=["POST"])
def government_assistant_chat_message(chat_id):
    if not ensure_logged_in_user() or session.get("role") != "user": return jsonify({"success":False,"error":"Please log in as a user."}),401
    payload=request.get_json(silent=True) or {}; question=str(payload.get("question","")).strip()
    if not question: return jsonify({"success":False,"error":"Please enter a question."}),400
    if len(question)>2000: return jsonify({"success":False,"error":"Please keep the question under 2000 characters."}),400
    conn=cursor=None
    try:
        conn=get_db_connection(); cursor=conn.cursor(dictionary=True); ensure_government_assistant_schema(cursor); conn.commit()
        cursor.execute("SELECT chat_id,title,messages FROM government_assistant_chats WHERE chat_id=%s AND user_id=%s LIMIT 1",(chat_id,session["user_id"]))
        row=cursor.fetchone()
        if not row: return jsonify({"success":False,"error":"Chat not found."}),404
        try: messages=json.loads(row["messages"] or "[]")
        except Exception: messages=[]
        # Use deterministic official guidance for high-confidence Aadhaar intents so
        # "apply" is never incorrectly answered as "download" (and vice versa).
        q_lower=question.lower()
        is_aadhaar=any(x in q_lower for x in ("aadhaar", "aadhar", "uidai", "e-aadhaar", "eaadhaar"))
        is_apply=any(x in q_lower for x in ("apply", "get a new", "new aadhaar", "enrol", "enrollment", "enrolment", "make an aadhaar", "create an aadhaar"))
        is_download=any(x in q_lower for x in ("download", "get pdf", "e-aadhaar", "eaadhaar", "print my aadhaar"))
        if is_aadhaar and (is_apply or is_download):
            answer=_format_government_answer(_government_assistant_fallback(question)); mode="official-aadhaar-guidance"
        else:
            try:
                answer=_format_government_answer(_generate_government_answer(question,messages)); mode="gemini"
            except Exception:
                app.logger.exception("Gemini unavailable; using official fallback")
                answer=_format_government_answer(_catalog_government_fallback(question)); mode="official-fallback"
        messages.append({"role":"user","content":question,"created_at":datetime.now().isoformat(timespec="seconds")})
        messages.append({"role":"assistant","content":answer,"created_at":datetime.now().isoformat(timespec="seconds")})
        title=row["title"]
        if title=="New Government Assistant Chat": title=question[:70]+("…" if len(question)>70 else "")
        cursor.execute("UPDATE government_assistant_chats SET title=%s,messages=%s,updated_at=CURRENT_TIMESTAMP WHERE chat_id=%s AND user_id=%s",(title,json.dumps(messages,ensure_ascii=False),chat_id,session["user_id"]))
        conn.commit()
        return jsonify({"success":True,"chat_id":chat_id,"title":title,"question":question,"answer":answer,"mode":mode})
    except Exception:
        if conn: conn.rollback()
        app.logger.exception("Government assistant chat message failed")
        return jsonify({"success":False,"error":"The assistant could not save this chat message."}),500
    finally: close_db(conn,cursor)

@app.route("/government-assistant", methods=["GET", "POST"])
def government_assistant():
    if not ensure_logged_in_user():
        if session.get("role") == "admin": return redirect(url_for("admin_dashboard"))
        return redirect(url_for("login"))
    if request.method == "GET":
        return render_template("government_assistant.html", name=session.get("name", "User"))
    payload=request.get_json(silent=True) or request.form
    question=str(payload.get("question", "")).strip()
    if not question: return jsonify({"success":False,"error":"Please enter a government document or service question.","sources":_fallback_government_sources()}),400
    try:
        answer=_format_government_answer(_generate_government_answer(question, payload.get("history") or [])); mode="gemini"
    except Exception:
        app.logger.exception("Gemini unavailable; using official fallback")
        answer=_format_government_answer(_catalog_government_fallback(question)); mode="official-fallback"
    return jsonify({"success":True,"answer":answer,"mode":mode})


@app.route("/user-dashboard")
def user_dashboard():
    if not ensure_logged_in_user():
        if session.get("role") == "admin":
            return redirect(url_for("admin_dashboard"))
        return redirect(url_for("login"))

    orders = []
    notifications = []
    shops = []
    resume_payment = None
    conn = cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        cursor.execute("""
            SELECT o.order_id, o.document, o.copies, o.color, o.orientation,
                   o.print_side, o.page_range, o.paper_type, o.estimated_cost,
                   o.order_status, o.created_at, s.shop_name, s.shop_number
            FROM orders o
            LEFT JOIN print_shops s ON s.shop_id = o.shop_id
            LEFT JOIN payments p ON p.order_id = o.order_id
            WHERE o.user_id = %s
              AND o.order_status NOT IN ('cancelled', 'declined')
            ORDER BY o.created_at DESC
        """, (session["user_id"],))
        orders = cursor.fetchall()

        cursor.execute("""
            SELECT message, is_read, created_at
            FROM notifications
            WHERE user_id = %s
              AND message NOT LIKE 'Order #%% received.%%'
              AND message NOT LIKE '%%order has been received successfully%%'
            ORDER BY created_at DESC LIMIT 10
        """, (session["user_id"],))
        notifications = cursor.fetchall()

        cursor.execute("""
            SELECT shop_id, shop_name, shop_number, university_name, state, COALESCE(is_open, 1) AS is_open
            FROM print_shops ORDER BY state ASC, university_name ASC, shop_id ASC
        """)
        shops = cursor.fetchall()

        # Persist the payment page across browser refreshes. The session points
        # to the exact order created by the current payment flow; the database
        # remains the source of truth for its payment status.
        pending_payment_order_id = session.get("qfp_pending_payment_order_id")
        if pending_payment_order_id:
            cursor.execute("""
                SELECT o.order_id, o.shop_id, o.estimated_cost, o.order_status,
                       s.shop_number,
                       COALESCE(p.payment_status, 'pending') AS payment_status,
                       COALESCE(p.payment_method, 'unselected') AS payment_method,
                       p.razorpay_qr_image_url
                FROM orders o
                LEFT JOIN print_shops s ON s.shop_id = o.shop_id
                LEFT JOIN payments p ON p.order_id = o.order_id
                WHERE o.order_id = %s AND o.user_id = %s
                LIMIT 1
            """, (pending_payment_order_id, session["user_id"]))
            pending = cursor.fetchone()
            if pending and pending.get("payment_status") != "paid" and pending.get("order_status") not in ("cancelled", "declined"):
                resume_payment = {
                    "order_id": int(pending["order_id"]),
                    "shop": pending.get("shop_number") or "",
                    "cost": f"₹ {Decimal(str(pending.get('estimated_cost') or 0)):.2f}",
                    "payment_method": pending.get("payment_method") or "unselected",
                    "payment_status": pending.get("payment_status") or "pending",
                    "qr_image_url": pending.get("razorpay_qr_image_url") or "",
                    "online_payment_available": True,
                }
            else:
                session.pop("qfp_pending_payment_order_id", None)
    except Error:
        pass
    finally:
        close_db(conn, cursor)

    return render_template(
        "student_dashboard.html",
        name=session["name"],
        orders=orders,
        notifications=notifications,
        shops=shops,
        resume_payment=resume_payment,
    )


# ============================================================
# NOTIFICATIONS + WEB PUSH
# ============================================================

def get_notification_user_id():
    """Return the users.user_id that owns the current notification inbox."""
    user_id = session.get("user_id")
    if not user_id:
        return None
    if session.get("role") != "admin":
        return user_id

    conn = cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT s.owner_id
            FROM admins a
            JOIN print_shops s ON s.shop_id = a.shop_id
            WHERE a.admin_id = %s
            LIMIT 1
        """, (user_id,))
        row = cursor.fetchone()
        return row["owner_id"] if row and row.get("owner_id") else None
    except Error:
        return None
    finally:
        close_db(conn, cursor)


@app.route("/api/notifications")
def api_notifications():
    if "user_id" not in session or session.get("role") not in {"user", "admin"}:
        return {"success": False, "error": "Login required."}, 401

    notification_user_id = get_notification_user_id()
    if not notification_user_id:
        return {"success": False, "error": "Notification account not found."}, 404

    conn = cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        # Students must never see "Order Received". Admins must see ONLY
        # the single "Order Received" notification type.
        if session.get("role") == "admin":
            notification_filter = """
              AND notification_type = 'order'
              AND message LIKE 'Order #%% received.%%'
            """
        else:
            notification_filter = """
              AND message NOT LIKE 'Order #%% received.%%'
              AND message NOT LIKE '%%order has been received successfully%%'
            """

        cursor.execute(f"""
            SELECT notification_id, order_id, message, notification_type, is_read, created_at
            FROM notifications
            WHERE user_id=%s AND is_read=0
            {notification_filter}
            ORDER BY created_at DESC, notification_id DESC
            LIMIT 30
        """, (notification_user_id,))
        rows = cursor.fetchall()
        cursor.execute(f"""
            SELECT COUNT(*) AS unread_count
            FROM notifications
            WHERE user_id=%s AND is_read=0
            {notification_filter}
        """, (notification_user_id,))
        unread = int((cursor.fetchone() or {}).get("unread_count") or 0)
        for row in rows:
            if row.get("created_at"):
                row["created_at"] = row["created_at"].isoformat()
        return {"success": True, "notifications": rows, "unread_count": unread}
    except Error as exc:
        return {"success": False, "error": str(exc)}, 500
    finally:
        close_db(conn, cursor)


@app.route("/api/notifications/read", methods=["POST"])
def mark_notifications_read():
    if "user_id" not in session or session.get("role") not in {"user", "admin"}:
        return {"success": False, "error": "Login required."}, 401

    notification_user_id = get_notification_user_id()
    if not notification_user_id:
        return {"success": False, "error": "Notification account not found."}, 404

    conn = cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE notifications SET is_read=1 WHERE user_id=%s AND is_read=0",
            (notification_user_id,)
        )
        conn.commit()
        return {"success": True}
    except Error as exc:
        if conn:
            conn.rollback()
        return {"success": False, "error": str(exc)}, 500
    finally:
        close_db(conn, cursor)


@app.route("/push/vapid-public-key")
def vapid_public_key(): return {"public_key":VAPID_PUBLIC_KEY}

@app.route("/push/subscribe", methods=["POST"])
def push_subscribe():
    if not ensure_logged_in_user(): return {"success":False,"error":"Login required."},401
    data=request.get_json(silent=True) or {}; endpoint=(data.get("endpoint") or "").strip(); keys=data.get("keys") or {}; p256dh=(keys.get("p256dh") or "").strip(); auth=(keys.get("auth") or "").strip()
    if not endpoint or not p256dh or not auth: return {"success":False,"error":"Invalid push subscription."},400
    conn=cursor=None
    try:
        conn=get_db_connection(); cursor=conn.cursor()
        cursor.execute("""INSERT INTO push_subscriptions (user_id,endpoint,p256dh,auth) VALUES (%s,%s,%s,%s) ON DUPLICATE KEY UPDATE user_id=VALUES(user_id),p256dh=VALUES(p256dh),auth=VALUES(auth),updated_at=CURRENT_TIMESTAMP""",(session["user_id"],endpoint,p256dh,auth)); conn.commit()
        return {"success":True}
    except Error as exc:
        if conn: conn.rollback()
        return {"success":False,"error":str(exc)},500
    finally: close_db(conn,cursor)

@app.route("/service-worker.js")
def service_worker():
    response=app.response_class(render_template("service-worker.js"),mimetype="application/javascript"); response.headers["Cache-Control"]="no-cache"; return response

@app.route("/profile")
def profile():
    if not ensure_logged_in_user():
        if session.get("role") == "admin":
            return redirect(url_for("admin_dashboard"))
        return redirect(url_for("login"))

    profile_data = {
        "name": session.get("name", ""),
        "email": session.get("email", ""),
        "phone": "",
    }
    summary = {
        "total": 0,
        "completed": 0,
        "pending": 0,
        "cancelled": 0,
    }
    notifications = []

    conn = cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        cursor.execute("""
            SELECT name, email, phone
            FROM users
            WHERE user_id = %s
            LIMIT 1
        """, (session["user_id"],))
        account = cursor.fetchone()
        if account:
            profile_data.update(account)
            session["name"] = account.get("name") or session.get("name", "")
            session["email"] = account.get("email") or session.get("email", "")

        cursor.execute("""
            SELECT
                COUNT(*) AS total,
                COALESCE(SUM(CASE WHEN order_status IN ('completed','delivered') THEN 1 ELSE 0 END), 0) AS completed,
                COALESCE(SUM(CASE WHEN order_status IN ('pending','accepted','printing','ready') THEN 1 ELSE 0 END), 0) AS pending,
                COALESCE(SUM(CASE WHEN order_status = 'cancelled' THEN 1 ELSE 0 END), 0) AS cancelled
            FROM orders
            WHERE user_id = %s
        """, (session["user_id"],))
        counts = cursor.fetchone() or {}
        summary.update({
            "total": int(counts.get("total") or 0),
            "completed": int(counts.get("completed") or 0),
            "pending": int(counts.get("pending") or 0),
            "cancelled": int(counts.get("cancelled") or 0),
        })

        cursor.execute("""
            SELECT message, notification_type, created_at, is_read
            FROM notifications
            WHERE user_id = %s
              AND message NOT LIKE 'Order #%% received.%%'
              AND message NOT LIKE '%%order has been received successfully%%'
            ORDER BY created_at DESC
            LIMIT 20
        """, (session["user_id"],))
        notifications = cursor.fetchall()
    except Error:
        pass
    finally:
        close_db(conn, cursor)

    return render_template(
        "profile.html",
        profile_data=profile_data,
        summary=summary,
        notifications=notifications,
    )


@app.route("/profile/update", methods=["POST"])
def update_profile():
    if not ensure_logged_in_user():
        return {"success": False, "error": "Please login as a user."}, 401

    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip().lower()
    phone = request.form.get("phone", "").strip()
    user_id = session.get("user_id")

    if not name:
        return {"success": False, "error": "Full name is required."}, 400
    if not EMAIL_PATTERN.fullmatch(email):
        return {"success": False, "error": "Please enter a valid email address."}, 400
    if phone and not PHONE_PATTERN.fullmatch(phone):
        return {"success": False, "error": "Phone number must contain exactly 10 digits."}, 400

    conn = cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # Make sure phone exists in older databases. DATABASE() avoids any
        # mismatch between an environment variable and the connected DB.
        cursor.execute("""
            SELECT COUNT(*) AS column_exists
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = 'users'
              AND COLUMN_NAME = 'phone'
        """)
        if not cursor.fetchone()["column_exists"]:
            cursor.execute("ALTER TABLE users ADD COLUMN phone VARCHAR(20) NULL AFTER email")

        # Check for an email collision in either account table.
        cursor.execute(
            "SELECT user_id FROM users WHERE LOWER(TRIM(email))=%s AND user_id<>%s LIMIT 1",
            (email, user_id),
        )
        if cursor.fetchone():
            return {"success": False, "error": "This email is already registered with another account."}, 400

        cursor.execute(
            "SELECT admin_id FROM admins WHERE LOWER(TRIM(email))=%s LIMIT 1",
            (email,),
        )
        if cursor.fetchone():
            # The user's current email is not stored in admins, so any admin
            # match here is a genuine conflict.
            return {"success": False, "error": "This email is already registered with another account."}, 400

        # IMPORTANT: update by the primary-key user_id only. Do not depend on
        # the session's old name/email or on the role column for this update.
        cursor.execute(
            "UPDATE users SET name=%s, email=%s, phone=%s WHERE user_id=%s",
            (name, email, phone or None, user_id),
        )

        if cursor.rowcount == 0:
            conn.rollback()
            return {"success": False, "error": "User account was not found in the database."}, 404

        conn.commit()

        # Verify using a fresh cursor against the committed database row.
        cursor.close()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT user_id, name, email, phone FROM users WHERE user_id=%s LIMIT 1",
            (user_id,),
        )
        saved = cursor.fetchone()

        if not saved:
            return {"success": False, "error": "Profile update could not be verified."}, 500

        saved_name = str(saved.get("name") or "")
        saved_email = str(saved.get("email") or "")
        saved_phone = str(saved.get("phone") or "")

        # Update the live login session too, so every new dashboard render
        # immediately uses the new name/email.
        session["name"] = saved_name
        session["email"] = saved_email
        session.modified = True

        return {
            "success": True,
            "name": saved_name,
            "email": saved_email,
            "phone": saved_phone,
        }
    except Error as e:
        if conn:
            conn.rollback()
        return {"success": False, "error": f"Profile could not be updated: {e}"}, 500
    finally:
        close_db(conn, cursor)


@app.route("/profile/change-password", methods=["POST"])
def change_profile_password():
    if not ensure_logged_in_user():
        return {"success": False, "error": "Please login as a user."}, 401

    current_password = request.form.get("current_password", "")
    new_password = request.form.get("new_password", "")

    if not current_password or not new_password:
        return {"success": False, "error": "Please fill all password fields."}, 400
    if not PASSWORD_PATTERN.fullmatch(new_password):
        return {
            "success": False,
            "error": "New password must be at least 8 characters and include uppercase, lowercase, number and symbol."
        }, 400

    conn = cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT password FROM users WHERE user_id=%s LIMIT 1",
            (session["user_id"],),
        )
        account = cursor.fetchone()
        if not account or account.get("password") != current_password:
            return {"success": False, "error": "Current password is incorrect."}, 400

        if current_password == new_password:
            return {"success": False, "error": "New password must be different from the current password."}, 400

        cursor.execute(
            "UPDATE users SET password=%s WHERE user_id=%s",
            (new_password, session["user_id"]),
        )
        conn.commit()
        return {"success": True}
    except Error as e:
        if conn:
            conn.rollback()
        return {"success": False, "error": f"Password could not be updated: {e}"}, 500
    finally:
        close_db(conn, cursor)


def _local_fallback_analysis(file_path, mime_type):
    """Best-effort local analysis used only when Gemini cannot analyze a file."""
    from zipfile import ZipFile
    from xml.etree import ElementTree as ET

    path = Path(file_path)
    suffix = path.suffix.lower()
    total_pages = 1
    blank_pages = 0
    text_parts = []

    # PDF: use pypdf for reliable page count/text extraction.
    if suffix == ".pdf" and PdfReader:
        reader = PdfReader(str(path))
        total_pages = max(1, len(reader.pages))
        for page in reader.pages:
            try:
                txt = (page.extract_text() or "").strip()
            except Exception:
                txt = ""
            if not txt:
                blank_pages += 1
            elif len(text_parts) < 8:
                text_parts.append(txt[:800])

    # Office Open XML: count logical pages/slides and extract visible text.
    elif suffix in {".pptx", ".pptm", ".ppsx"}:
        with ZipFile(path) as zz:
            slides = sorted(
                n for n in zz.namelist()
                if re.match(r"ppt/slides/slide\d+\.xml$", n)
            )
            total_pages = max(1, len(slides))
            for n in slides[:8]:
                try:
                    root = ET.fromstring(zz.read(n))
                    vals = [e.text.strip() for e in root.iter()
                            if e.text and e.text.strip()]
                    if vals:
                        text_parts.append(" ".join(vals)[:800])
                    else:
                        blank_pages += 1
                except Exception:
                    pass

    elif suffix in {".docx", ".docm", ".dotx"}:
        with ZipFile(path) as zz:
            try:
                root = ET.fromstring(zz.read("word/document.xml"))
                vals = [e.text.strip() for e in root.iter()
                        if e.text and e.text.strip()]
                text_parts.append(" ".join(vals)[:5000])
                # DOCX has no guaranteed page count without a renderer.
                total_pages = 1
            except Exception:
                total_pages = 1

    elif suffix in {".xlsx", ".xlsm", ".xltx"}:
        with ZipFile(path) as zz:
            sheets = [n for n in zz.namelist()
                      if re.match(r"xl/worksheets/sheet\d+\.xml$", n)]
            total_pages = max(1, len(sheets))
            for n in sheets[:8]:
                try:
                    root = ET.fromstring(zz.read(n))
                    vals = [e.text.strip() for e in root.iter()
                            if e.text and e.text.strip()]
                    if vals:
                        text_parts.append(" ".join(vals)[:800])
                    else:
                        blank_pages += 1
                except Exception:
                    pass

    elif suffix in {".txt", ".csv", ".html", ".htm", ".xml", ".json", ".rtf"}:
        try:
            text = path.read_text(encoding="utf-8", errors="replace").strip()
            total_pages = 1
            if text:
                text_parts.append(text[:5000])
            else:
                blank_pages = 1
        except Exception:
            pass

    elif suffix in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff"}:
        try:
            from PIL import Image
            with Image.open(path) as im:
                if im.width < 600 or im.height < 600:
                    blurry_pages = 1
                else:
                    blurry_pages = 0
        except Exception:
            blurry_pages = 0
    else:
        total_pages = 1

    summary = "Local fallback analysis used because Gemini could not analyze this file."
    if text_parts:
        compact = re.sub(r"\s+", " ", " ".join(text_parts)).strip()
        summary += " Extracted content: " + compact[:900]
    return {
        "total_pages": max(1, int(total_pages)),
        "blank_pages": max(0, int(blank_pages)),
        "duplicate_pages": 0,
        "blurry_pages": max(0, int(locals().get("blurry_pages", 0))),
        "unrecognizable_pages": 0,
        "summary": summary,
        "analysis_engine": "local-fallback",
    }


def analyze_document_with_fallback(file_path, mime_type, model=None):
    """Try Gemini first; if it fails, always return a local analysis result."""
    try:
        result = analyze_with_gemini(file_path, mime_type, model=model)
        result["analysis_engine"] = "gemini"
        return result
    except Exception as gemini_error:
        fallback = _local_fallback_analysis(file_path, mime_type)
        fallback["gemini_error"] = str(gemini_error)[:500]
        return fallback


def _fast_edit_local_analysis(file_path, mime_type):
    """Very fast local print-quality analysis for Edit Order.

    Edit Order should feel instant, so it does not wait for a remote Gemini
    vision request.  It inspects page count, blank pages, repeated/duplicate
    pages and obvious image blur locally.  This is deterministic and normally
    completes in well under a second for normal PDFs/images.
    """
    from zipfile import ZipFile
    from xml.etree import ElementTree as ET

    path = Path(file_path)
    suffix = path.suffix.lower()
    total_pages = 1
    blank_pages = 0
    duplicate_pages = 0
    blurry_pages = 0
    unrecognizable_pages = 0
    page_signatures = []

    def add_signature(value):
        if value is None:
            return
        value = re.sub(r"\s+", " ", str(value)).strip().lower()
        if not value:
            return
        digest = hashlib.sha1(value.encode("utf-8", errors="ignore")).hexdigest()
        if digest in page_signatures:
            nonlocal duplicate_pages
            duplicate_pages += 1
        else:
            page_signatures.append(digest)

    if suffix == ".pdf" and PdfReader:
        reader = PdfReader(str(path))
        total_pages = max(1, len(reader.pages))
        for page in reader.pages:
            try:
                txt = (page.extract_text() or "").strip()
            except Exception:
                txt = ""
            if not txt:
                blank_pages += 1
            else:
                add_signature(txt)
            # If the PDF page contains an embedded scan, inspect its actual
            # image dimensions without rendering the whole PDF.
            try:
                page_images = list(page.images)
                if page_images and all((getattr(img, "width", 0) < 600 or getattr(img, "height", 0) < 600) for img in page_images):
                    blurry_pages += 1
            except Exception:
                pass

        # Optional lightweight rendering for PDFs when PyMuPDF is installed.
        # It is capped to avoid turning analysis into another slow operation.
        try:
            import fitz  # PyMuPDF, optional
            import statistics
            doc = fitz.open(str(path))
            for idx in range(min(len(doc), 12)):
                page = doc.load_page(idx)
                pix = page.get_pixmap(matrix=fitz.Matrix(0.65, 0.65), alpha=False)
                if pix.width < 80 or pix.height < 80:
                    continue
                # A tiny grayscale sample is enough to catch very soft scans.
                img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples) if Image else None
                if img:
                    gray = img.convert("L").resize((120, 120))
                    vals = list(gray.getdata())
                    # Mean absolute neighbour difference: very low = nearly flat/soft.
                    diffs = [abs(vals[i] - vals[i-1]) for i in range(1, len(vals))]
                    if diffs and statistics.mean(diffs) < 3.2 and blank_pages < total_pages:
                        blurry_pages += 1
            doc.close()
            blurry_pages = min(blurry_pages, total_pages - blank_pages)
        except Exception:
            pass

    elif suffix in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff"} and Image:
        try:
            import statistics
            with Image.open(path) as im:
                total_pages = 1
                gray = im.convert("L")
                if gray.width < 600 or gray.height < 600:
                    blurry_pages = 1
                else:
                    small = gray.resize((120, 120))
                    vals = list(small.getdata())
                    diffs = [abs(vals[i] - vals[i-1]) for i in range(1, len(vals))]
                    blurry_pages = 1 if diffs and statistics.mean(diffs) < 3.2 else 0
                add_signature(hashlib.sha1(im.tobytes()).hexdigest())
        except Exception:
            total_pages = 1

    elif suffix in {".pptx", ".pptm", ".ppsx"}:
        with ZipFile(path) as zz:
            slides = sorted(n for n in zz.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", n))
            total_pages = max(1, len(slides))
            for n in slides:
                try:
                    root = ET.fromstring(zz.read(n))
                    vals = [e.text.strip() for e in root.iter() if e.text and e.text.strip()]
                    if vals:
                        add_signature(" ".join(vals))
                    else:
                        blank_pages += 1
                except Exception:
                    blank_pages += 1

    elif suffix in {".txt", ".csv", ".html", ".htm", ".xml", ".json", ".rtf"}:
        try:
            text = path.read_text(encoding="utf-8", errors="replace").strip()
            total_pages = 1
            if text:
                add_signature(text)
            else:
                blank_pages = 1
        except Exception:
            blank_pages = 1

    elif suffix in {".docx", ".docm", ".dotx"}:
        with ZipFile(path) as zz:
            try:
                root = ET.fromstring(zz.read("word/document.xml"))
                vals = [e.text.strip() for e in root.iter() if e.text and e.text.strip()]
                text = " ".join(vals)
                total_pages = 1
                if text:
                    add_signature(text)
                else:
                    blank_pages = 1
            except Exception:
                total_pages = 1
                blank_pages = 1

    elif suffix in {".xlsx", ".xlsm", ".xltx"}:
        with ZipFile(path) as zz:
            sheets = [n for n in zz.namelist() if re.match(r"xl/worksheets/sheet\d+\.xml$", n)]
            total_pages = max(1, len(sheets))
            for n in sheets:
                try:
                    root = ET.fromstring(zz.read(n))
                    vals = [e.text.strip() for e in root.iter() if e.text and e.text.strip()]
                    if vals:
                        add_signature(" ".join(vals))
                    else:
                        blank_pages += 1
                except Exception:
                    blank_pages += 1
    else:
        total_pages = 1

    return {
        "success": True,
        "file_name": path.name,
        "total_pages": max(1, int(total_pages)),
        "blank_pages": max(0, int(blank_pages)),
        "duplicate_pages": max(0, int(duplicate_pages)),
        "blurry_pages": max(0, int(blurry_pages)),
        "unrecognizable_pages": max(0, int(unrecognizable_pages)),
        "summary": "Fast local document scan completed.",
        "analysis_engine": "fast-local",
    }


def analyze_edit_file_fast(file, model=None):
    """Accurate Edit Order analysis using the same Gemini document analyzer.

    The previous Edit Order path used a heuristic local scanner.  That scanner
    could mistake text extraction failures for blank pages and image dimensions
    for blur, producing incorrect print-quality results.  Edit Order now uses
    the page-aware Gemini analyzer first and falls back locally only if Gemini
    is unavailable.
    """
    safe_name = secure_filename(file.filename) or "document"
    temp_dir = UPLOAD_ROOT / "_analysis_edit"
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_path = temp_dir / f"{uuid.uuid4().hex}_{safe_name}"
    file.save(temp_path)
    mime_type = file.mimetype or mimetypes.guess_type(safe_name)[0] or "application/octet-stream"
    try:
        result = analyze_document_with_fallback(temp_path, mime_type, model=model)
        result["file_name"] = file.filename
        result.setdefault("duplicate_pages", 0)
        return {"success": True, **result}
    except Exception as exc:
        # Never make Edit Order unusable because a local parser missed a file.
        fallback = _local_fallback_analysis(temp_path, mime_type)
        fallback.update({
            "success": True,
            "file_name": file.filename,
            "duplicate_pages": 0,
            "analysis_engine": "fast-local-fallback",
        })
        return fallback
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except Exception:
            pass


def analyze_uploaded_file(file, model=None):
    """Save and analyze one uploaded file, returning its Gemini analysis."""
    safe_name = secure_filename(file.filename) or "document"
    temp_dir = UPLOAD_ROOT / "_analysis"
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_path = temp_dir / f"{uuid.uuid4().hex}_{safe_name}"
    file.save(temp_path)
    mime_type = file.mimetype or mimetypes.guess_type(safe_name)[0] or "application/octet-stream"
    try:
        result = analyze_document_with_fallback(temp_path, mime_type, model=model)
        return {
            "success": True,
            "file_name": file.filename,
            **result,
        }
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except Exception:
            pass


@app.route("/analyze-document", methods=["POST"])
def analyze_document():
    if not ensure_logged_in_user():
        return {"success": False, "error": "Please login as a user."}, 401
    file = request.files.get("document")
    if not file or not file.filename:
        return {"success": False, "error": "Please select a document first."}, 400
    try:
        return analyze_uploaded_file(file)
    except Exception as e:
        return {"success": False, "error": str(e)}, 400


@app.route("/analyze-edit-documents", methods=["POST"])
def analyze_edit_documents():
    """Fast AI analysis used only by Edit Order."""
    if not ensure_logged_in_user():
        return {"success": False, "error": "Please login as a user."}, 401

    files = [f for f in request.files.getlist("documents") if f and f.filename]
    if not files:
        return {"success": False, "error": "Please select at least one document."}, 400

    from concurrent.futures import ThreadPoolExecutor, as_completed
    results = [None] * len(files)
    # Analyze files concurrently. The edit-specific analyzer sends supported
    # files directly to Gemini instead of first uploading them to Gemini Files.
    with ThreadPoolExecutor(max_workers=min(4, len(files))) as executor:
        futures = {executor.submit(analyze_edit_file_fast, f, EDIT_GEMINI_MODEL): i for i, f in enumerate(files)}
        for future in as_completed(futures):
            index = futures[future]
            try:
                results[index] = future.result()
            except Exception as e:
                results[index] = {"success": False, "file_name": files[index].filename, "error": str(e)}

    failed = [r for r in results if not r or not r.get("success")]
    if failed:
        names = ", ".join(r.get("file_name", "document") for r in failed)
        return {"success": False, "error": f"AI could not analyze: {names}", "results": results}, 400
    return {"success": True, "results": results}


@app.route("/analyze-documents", methods=["POST"])
def analyze_documents():
    """Analyze multiple selected documents concurrently to reduce waiting time."""
    if not ensure_logged_in_user():
        return {"success": False, "error": "Please login as a user."}, 401

    files = [f for f in request.files.getlist("documents") if f and f.filename]
    if not files:
        return {"success": False, "error": "Please select at least one document."}, 400

    from concurrent.futures import ThreadPoolExecutor, as_completed

    results = [None] * len(files)
    with ThreadPoolExecutor(max_workers=min(4, len(files))) as executor:
        futures = {executor.submit(analyze_uploaded_file, f): i for i, f in enumerate(files)}
        for future in as_completed(futures):
            index = futures[future]
            try:
                results[index] = future.result()
            except Exception as e:
                results[index] = {
                    "success": False,
                    "file_name": files[index].filename,
                    "error": str(e),
                }

    failed = [r for r in results if not r or not r.get("success")]
    if failed:
        names = ", ".join(r.get("file_name", "document") for r in failed)
        return {
            "success": False,
            "error": f"Gemini could not analyze: {names}",
            "results": results,
        }, 400

    return {"success": True, "results": results}


def get_shop_print_cost(cursor, shop_id, paper_type, total_pages, copies):
    print_type_map = {
        "printing": "A4 Printing",
        "photo": "Photo Print",
        "sticker": "Sticker Print",
    }
    print_type = print_type_map.get(paper_type)
    if not print_type:
        raise ValueError("Invalid paper type selected.")

    # Add one hidden billable page BEFORE selecting the pricing slab.
    # Example: 1 actual page -> 2 billable pages, so the 2-5 slab applies.
    # The actual page count stored/displayed to the student remains unchanged.
    billable_pages = int(total_pages) + 1

    cursor.execute(
        """
        SELECT price_per_page, min_pages, max_pages
        FROM print_prices
        WHERE shop_id = %s
          AND print_type = %s
          AND (
              (min_pages <= %s AND max_pages >= %s)
              OR max_pages < %s
          )
        ORDER BY
            CASE
                WHEN min_pages <= %s AND max_pages >= %s THEN 0
                ELSE 1
            END,
            max_pages DESC
        LIMIT 1
        """,
        (
            shop_id,
            print_type,
            billable_pages,
            billable_pages,
            billable_pages,
            billable_pages,
            billable_pages,
        ),
    )
    price_row = cursor.fetchone()
    if not price_row:
        raise ValueError(
            f"No price is configured for {print_type} for {billable_pages} billable page(s) at the selected shop."
        )

    price_per_page = Decimal(str(price_row["price_per_page"]))
    total_cost = (
        Decimal(billable_pages) * price_per_page * Decimal(copies)
    ).quantize(Decimal("0.01"))

    return print_type, price_per_page, total_cost


@app.route("/analyze-print-preview", methods=["POST"])
def analyze_print_preview():
    """Build a fast print preview from the already-completed document AI analysis.

    The upload is intentionally NOT sent to Gemini again here. Document analysis is
    already mandatory before preview, so re-uploading the same files was the main
    source of the long preview delay. This endpoint deterministically applies every
    selected print preference and parses common additional requirements locally.
    """
    if not ensure_logged_in_user():
        return {"success": False, "error": "Please login as a user."}, 401

    files = [f for f in request.files.getlist("documents") if f and f.filename]
    additional = (request.form.get("additional_requirements") or "").strip()
    import re

    def safe_int(value, default=1, minimum=1, maximum=100):
        try:
            return min(max(int(value), minimum), maximum)
        except Exception:
            return default

    preferences = {
        "copies": safe_int(request.form.get("copies", "1"), 1, 1, 50),
        "color": request.form.get("color", "black_white"),
        "orientation": request.form.get("orientation", "portrait"),
        "print_side": request.form.get("print_side", "single"),
        "paper_type": request.form.get("paper_type", "printing"),
        "page_range": request.form.get("page_range", "all") or "all",
    }
    try:
        analyses = json.loads(request.form.get("analyses", "[]"))
        if not isinstance(analyses, list): analyses = []
    except Exception:
        analyses = []

    if not files:
        return {"success": False, "error": "Please upload at least one document first."}, 400

    text = additional.lower()
    # Common natural-language requirements. These are deliberately deterministic so
    # the preview is produced immediately instead of waiting for another AI upload.
    passport_match = re.search(
        r"(\d+)\s*(?:x\s*)?(?:passport(?:\s*size)?|passport-size|passport size)\s*(?:photo|photos|picture|pictures|pic|pics)?",
        text,
    ) or re.search(
        r"(?:passport(?:\s*size)?|passport-size|passport size)\s*(?:photo|photos|picture|pictures|pic|pics)?\s*(?:x|\*|:)\s*(\d+)",
        text,
    )
    requested_passports = safe_int(passport_match.group(1), 1, 1, 20) if passport_match else 0

    color = preferences["color"]
    orientation = preferences["orientation"]
    side = preferences["print_side"]
    paper = preferences["paper_type"]
    copies = preferences["copies"]

    if requested_passports:
        cols = 2 if requested_passports <= 4 else (3 if requested_passports <= 9 else 4)
        plan = {
            "mode": "passport_grid",
            "intent": "passport_photo",
            "copies_per_sheet": requested_passports,
            "columns": cols,
            "rows": (requested_passports + cols - 1) // cols,
            "crop": "passport",
            "summary": f"{requested_passports} passport-size photo copies arranged on the selected paper.",
        }
    else:
        # For ordinary prints, the requested Copies value is the number of visible
        # copies in the preview. This makes the preview match the actual order.
        cols = min(4, max(1, copies))
        plan = {
            "mode": "standard",
            "intent": "standard_print",
            "copies_per_sheet": copies,
            "columns": cols,
            "rows": (copies + cols - 1) // cols,
            "crop": "fit",
            "summary": "Preview applies all selected print preferences to the uploaded document.",
        }

    warnings = []
    if color == "black_white":
        warnings.append("Black & White selected: the preview is shown in grayscale and the placed order will use Black & White.")
    if side in ("double", "double_sided"):
        warnings.append("Double-sided selected: the preview represents the front-side layout; page pairing follows the selected document/page range.")
    if preferences["page_range"] != "all":
        warnings.append(f"Page range applied: {preferences['page_range']}.")
    if additional:
        warnings.append(f"Additional requirement applied: {additional}")

    plan["warnings"] = warnings
    plan["preferences"] = preferences
    plan["additional_requirements"] = additional
    plan["ai_document_analysis_used"] = bool(analyses)

    # No second Gemini file upload/request: the mandatory Gemini document analysis
    # already completed earlier is supplied in `analyses`. This keeps preview fast.
    return {
        "success": True,
        "plan": plan,
        "analyses": analyses,
        "engine": "ai-assisted-fast-preview",
    }

@app.route("/calculate-print-cost", methods=["POST"])
def calculate_print_cost():
    if not ensure_logged_in_user():
        return {"success": False, "error": "Please login as a user."}, 401

    try:
        shop_id = int(request.form.get("shop_id", "0"))
        total_pages = int(request.form.get("total_pages", "0"))
        copies = max(1, int(request.form.get("copies", "1")))
    except (TypeError, ValueError):
        return {"success": False, "error": "Invalid shop, page count or copies value."}, 400

    if shop_id <= 0 or total_pages <= 0:
        return {"success": False, "error": "A valid shop and Gemini page count are required."}, 400

    paper_type = request.form.get("paper_type", "printing")
    conn = cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT shop_id FROM print_shops WHERE shop_id = %s LIMIT 1",
            (shop_id,),
        )
        if not cursor.fetchone():
            return {"success": False, "error": "Selected Shop ID does not exist."}, 400

        print_type, price_per_page, total_cost = get_shop_print_cost(
            cursor, shop_id, paper_type, total_pages, copies
        )
        return {
            "success": True,
            "print_type": print_type,
            "total_pages": total_pages,
            "copies": copies,
            "price_per_page": f"{price_per_page:.2f}",
            "cost": f"{total_cost:.2f}",
        }
    except (Error, ValueError) as e:
        return {"success": False, "error": str(e)}, 400
    finally:
        close_db(conn, cursor)


@app.route("/place-order", methods=["POST"])
def place_order():
    if not ensure_logged_in_user():
        return {"success": False, "error": "Please login as a user."}, 401

    files = [f for f in request.files.getlist("documents") if f and f.filename]
    if not files:
        # Backward compatibility with an older single-document form.
        single = request.files.get("document")
        if single and single.filename:
            files = [single]
    if not files:
        return {"success": False, "error": "Please upload at least one document."}, 400

    try:
        shop_id = int(request.form.get("shop_id", "0"))
        copies = max(1, int(request.form.get("copies", "1")))
        total_pages = int(request.form.get("total_pages", "0") or 0)
        blank_pages = int(request.form.get("blank_pages", "0") or 0)
        blurry_pages = int(request.form.get("blurry_pages", "0") or 0)
        unrecognizable_pages = int(request.form.get("unrecognizable_pages", "0") or 0)
        analyses = json.loads(request.form.get("analyses", "[]"))
    except (ValueError, TypeError, json.JSONDecodeError):
        return {"success": False, "error": "Invalid order or document analysis data."}, 400

    if len(analyses) != len(files):
        return {"success": False, "error": "Please analyze every selected document before placing the order."}, 400

    calculated_total_pages = sum(max(0, int(a.get("total_pages", 0) or 0)) for a in analyses)
    if calculated_total_pages <= 0 or calculated_total_pages != total_pages:
        return {"success": False, "error": "The analyzed page count is invalid. Please analyze the documents again."}, 400

    conn = cursor = None
    saved_paths = []
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT shop_id, shop_name, shop_number, COALESCE(is_open, 1) AS is_open FROM print_shops WHERE shop_id=%s LIMIT 1", (shop_id,))
        shop = cursor.fetchone()
        if not shop:
            return {"success": False, "error": "Selected Shop ID does not exist."}, 400
        if not bool(shop.get("is_open", 1)):
            return {"success": False, "error": "This shop is currently closed. New orders cannot be placed right now."}, 400

        color = request.form.get("color", "black_white")
        orientation = request.form.get("orientation", "portrait")
        print_side = request.form.get("print_side", "single")
        page_range = request.form.get("page_range", "all") or "all"
        paper_type = request.form.get("paper_type", "printing")
        additional = request.form.get("additional_requirements", "").strip()

        _, _, cost = get_shop_print_cost(cursor, shop_id, paper_type, total_pages, copies)

        ensure_payment_schema(cursor)
        payment_config = get_payment_settings(cursor, shop_id)

        document_label = f"{len(files)} documents" if len(files) > 1 else secure_filename(files[0].filename) or "document"
        cursor.execute("""
            INSERT INTO orders
              (user_id, shop_id, document, copies, color, orientation, print_side,
               page_range, paper_type, additional_requirements, estimated_cost)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (session["user_id"], shop_id, document_label, copies, color, orientation,
              print_side, page_range, paper_type, additional, cost))
        order_id = cursor.lastrowid

        ensure_order_item_columns(cursor)
        for index, file in enumerate(files):
            analysis = analyses[index]
            safe_name = secure_filename(file.filename) or f"document_{index + 1}"
            user_dir = UPLOAD_ROOT / str(session["user_id"])
            user_dir.mkdir(parents=True, exist_ok=True)
            stored_name = f"{uuid.uuid4().hex}_{safe_name}"
            stored_path = user_dir / stored_name
            file.save(stored_path)
            saved_paths.append(stored_path)
            relative_path = str(stored_path.relative_to(app.root_path)).replace("\\", "/")
            item_pages = max(0, int(analysis.get("total_pages", 0) or 0))
            item_blank = max(0, int(analysis.get("blank_pages", 0) or 0))
            item_blurry = max(0, int(analysis.get("blurry_pages", 0) or 0))
            item_unrecognizable = max(0, int(analysis.get("unrecognizable_pages", 0) or 0))
            item_cost = (Decimal(item_pages) * (cost / Decimal(total_pages))).quantize(Decimal("0.01")) if total_pages else Decimal("0.00")
            cursor.execute("""
                INSERT INTO order_items
                  (order_id, file_name, file_path, copies, color, double_sided, page_range,
                   cost, shop_id, orientation, print_side, paper_type, additional_requirements,
                   total_pages, blank_pages, blurry_pages, unrecognizable_pages, uploaded_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (order_id, safe_name, relative_path, copies, color,
                  1 if print_side == "double" else 0, page_range, item_cost, shop_id,
                  orientation, print_side, paper_type, additional, item_pages,
                  item_blank, item_blurry, item_unrecognizable, datetime.now()))

        cursor.execute("""
            INSERT INTO document_analysis
              (order_id, blank_pages, duplicate_pages, invisible_text,
               total_pages, blurry_pages, unrecognizable_pages, analysis_status)
            VALUES (%s,%s,%s,%s,%s,%s,%s,'completed')
        """, (order_id, blank_pages, 0, 0, total_pages, blurry_pages, unrecognizable_pages))

        # Create the print order first. Payment is selected immediately after
        # Place Order, so the student can choose Online UPI or Cash at Shop.
        cursor.execute("""
            INSERT INTO payments
                (order_id, amount, payment_status, payment_method, razorpay_order_id)
            VALUES (%s, %s, 'pending', 'unselected', NULL)
        """, (order_id, cost))

        conn.commit()
        # Keep the payment flow recoverable after a browser refresh. This does
        # not mark the order paid; it only remembers which pending payment
        # screen the current student should return to.
        session["qfp_pending_payment_order_id"] = int(order_id)
        return {
            "success": True,
            "order_id": order_id,
            "shop": shop["shop_number"],
            "cost": f"₹ {cost:.2f}",
            "copies": copies,
            "documents": len(files),
            "online_payment_available": bool(payment_config and payment_config.get("is_enabled") and payment_config.get("key_id") and payment_config.get("key_secret"))
        }
    except (Error, ValueError) as e:
        if conn:
            conn.rollback()
        for path in saved_paths:
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass
        return {"success": False, "error": f"Order could not be placed: {e}"}, 500
    finally:
        close_db(conn, cursor)



# ============================================================
# PAYMENTS - RAZORPAY CHECKOUT + SERVER VERIFICATION
# ============================================================

@app.route("/payment/cash/<int:order_id>", methods=["POST"])
def select_cash_payment(order_id):
    """Select Cash at Shop for a pending print order.

    The order remains unpaid/pending until the shop receives the cash.
    This endpoint always returns JSON so the frontend never tries to parse
    an HTML 404/500 page as JSON.
    """
    if not ensure_logged_in_user():
        return jsonify({"success": False, "error": "Please login as a user."}), 401

    conn = cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        ensure_payment_schema(cursor)

        cursor.execute("""
            SELECT o.order_id, o.user_id, o.shop_id, o.order_status,
                   o.estimated_cost, p.amount AS payment_amount, p.payment_id, p.payment_status
            FROM orders o
            JOIN payments p ON p.order_id = o.order_id AND p.payment_status <> 'failed'
            WHERE o.order_id = %s AND o.user_id = %s
            ORDER BY p.payment_id DESC
            LIMIT 1
        """, (order_id, session["user_id"]))
        row = cursor.fetchone()

        if not row:
            return jsonify({"success": False, "error": "Payment order not found."}), 404

        if row["payment_status"] == "paid":
            return jsonify({"success": False, "error": "This order is already paid."}), 400

        if row["order_status"] in ("cancelled", "declined"):
            return jsonify({"success": False, "error": "This order is no longer active."}), 400

        cursor.execute("""
            UPDATE payments
            SET payment_status = 'pending',
                payment_method = 'cash',
                razorpay_order_id = NULL,
                razorpay_payment_id = NULL,
                razorpay_signature = NULL
            WHERE payment_id = %s
        """, (row["payment_id"],))

        conn.commit()
        session["qfp_pending_payment_order_id"] = int(order_id)

        return jsonify({
            "success": True,
            "order_id": int(order_id),
            "payment_method": "cash",
            "payment_status": "pending",
            "message": "Cash at Shop selected. Pay at the print shop when collecting your printout."
        })
    except (Error, ValueError) as e:
        if conn:
            conn.rollback()
        return jsonify({
            "success": False,
            "error": f"Could not select cash payment: {e}"
        }), 500
    finally:
        close_db(conn, cursor)


@app.route("/payment/start/<int:order_id>", methods=["POST"])
def start_payment(order_id):
    if not ensure_logged_in_user():
        return {"success": False, "error": "Please login as a user."}, 401

    conn = cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        ensure_payment_schema(cursor)
        cursor.execute("""
            SELECT o.order_id, o.user_id, o.shop_id, o.estimated_cost, p.amount AS payment_amount,
                   p.payment_id, p.payment_status, p.razorpay_order_id,
                   u.name AS customer_name, u.email AS customer_email, u.phone AS customer_phone
            FROM orders o
            JOIN payments p ON p.order_id=o.order_id AND p.payment_status <> 'failed'
            JOIN users u ON u.user_id=o.user_id
            WHERE o.order_id=%s AND o.user_id=%s ORDER BY p.payment_id DESC LIMIT 1
        """, (order_id, session["user_id"]))
        row = cursor.fetchone()
        if not row:
            return {"success": False, "error": "Payment order not found."}, 404
        if row["payment_status"] == "paid":
            return {"success": False, "error": "This order is already paid."}, 400

        config = get_payment_settings(cursor, row["shop_id"])
        if not config or not config.get("is_enabled") or not config.get("key_id") or not config.get("key_secret"):
            return {"success": False, "error": "Online UPI payment is not configured for this shop. Choose Cash at Shop or configure Razorpay."}, 400

        rp_order_id = row.get("razorpay_order_id")
        if not rp_order_id:
            rp_order = create_razorpay_order(config["key_id"], config["key_secret"], row["payment_amount"], f"QFP-{order_id}")
            rp_order_id = rp_order["id"]
            cursor.execute("""
                UPDATE payments
                SET payment_method='upi', razorpay_order_id=%s, razorpay_payment_id=NULL, razorpay_signature=NULL
                WHERE payment_id=%s
            """, (rp_order_id, row["payment_id"]))
            conn.commit()

        return {
            "success": True,
            "payment": {
                "provider": "razorpay",
                "method": "upi",
                "key_id": config["key_id"],
                "order_id": rp_order_id,
                "amount_paise": int((Decimal(str(row["payment_amount"])) * 100).quantize(Decimal("1"))),
                "amount": f"₹{Decimal(str(row['payment_amount'])):.2f}",
                "name": row.get("customer_name") or "QueueFree User",
                "email": row.get("customer_email") or "",
                "contact": row.get("customer_phone") or ""
            }
        }
    except (Error, ValueError, RuntimeError, requests.RequestException) as e:
        if conn:
            conn.rollback()
        return {"success": False, "error": str(e)}, 500
    finally:
        close_db(conn, cursor)

@app.route("/payment/upi/status/<int:order_id>", methods=["GET"])
def upi_payment_status(order_id):
    if not ensure_logged_in_user():
        return {"success": False, "error": "Please login as a user."}, 401
    conn = cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        ensure_payment_schema(cursor)
        cursor.execute("""
            SELECT o.order_id, o.user_id, o.shop_id, o.estimated_cost, p.amount AS payment_amount,
                   p.payment_id, p.payment_status, p.razorpay_payment_link_id
            FROM orders o JOIN payments p ON p.order_id=o.order_id AND p.payment_status <> 'failed'
            WHERE o.order_id=%s AND o.user_id=%s ORDER BY p.payment_id DESC LIMIT 1
        """, (order_id, session["user_id"]))
        row = cursor.fetchone()
        if not row:
            return {"success": False, "error": "Payment order not found."}, 404
        if row["payment_status"] == "paid":
            return {"success": True, "status": "paid", "payment_method": "upi"}
        if not row.get("razorpay_payment_link_id"):
            return {"success": True, "status": "pending", "payment_method": "upi"}
        config = get_payment_settings(cursor, row["shop_id"])
        if not config or not config.get("key_secret"):
            return {"success": False, "error": "Razorpay payment gateway is not configured."}, 400
        link = fetch_razorpay_payment_link(config["key_id"], config["key_secret"], row["razorpay_payment_link_id"])
        expected_amount = int((Decimal(str(row["payment_amount"])) * 100).quantize(Decimal("1")))
        if link.get("status") == "paid" and int(link.get("amount_paid") or 0) == expected_amount:
            payments = link.get("payments") or []
            captured = next((x for x in payments if x.get("status") == "captured" and int(x.get("amount") or 0) == expected_amount), None)
            payment_id = (captured or {}).get("payment_id")
            cursor.execute("""
                UPDATE payments
                SET payment_status='paid', amount=%s, payment_method='upi', razorpay_payment_id=%s
                WHERE payment_id=%s
            """, (row["estimated_cost"], payment_id, row["payment_id"]))
            cursor.execute("""
                INSERT INTO notifications (user_id, order_id, message, notification_type)
                SELECT s.owner_id, o.order_id, %s, 'order'
                FROM orders o JOIN print_shops s ON s.shop_id=o.shop_id
                WHERE o.order_id=%s
            """, (f"Order #{order_id} received and UPI payment confirmed.", order_id))
            conn.commit()
            return {"success": True, "status": "paid", "payment_method": "upi", "razorpay_payment_id": payment_id}
        conn.commit()
        return {"success": True, "status": link.get("status", "pending"), "payment_method": "upi"}
    except (Error, RuntimeError, requests.RequestException, ValueError) as e:
        if conn: conn.rollback()
        return {"success": False, "error": f"Could not verify UPI payment: {e}"}, 500
    finally:
        close_db(conn, cursor)


@app.route("/payment/qr/start/<int:order_id>", methods=["POST"])
def start_qr_payment(order_id):
    if not ensure_logged_in_user():
        return {"success": False, "error": "Please login as a user."}, 401
    conn = cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        ensure_payment_schema(cursor)
        cursor.execute("""
            SELECT o.order_id, o.user_id, o.shop_id, o.estimated_cost, p.amount AS payment_amount,
                   p.payment_id, p.payment_status, p.razorpay_qr_id, p.razorpay_qr_image_url
            FROM orders o JOIN payments p ON p.order_id=o.order_id AND p.payment_status <> 'failed'
            WHERE o.order_id=%s AND o.user_id=%s ORDER BY p.payment_id DESC LIMIT 1
        """, (order_id, session["user_id"]))
        row = cursor.fetchone()
        if not row:
            return {"success": False, "error": "Payment order not found."}, 404
        if row["payment_status"] == "paid":
            return {"success": False, "error": "This order is already paid."}, 400

        config = get_payment_settings(cursor, row["shop_id"])
        if not config or not config.get("is_enabled") or not config.get("key_id") or not config.get("key_secret"):
            return {"success": False, "error": "Razorpay UPI is not configured for this shop."}, 400
        if str(config.get("key_id") or "").startswith("rzp_test_"):
            return {"success": False, "error": "Razorpay UPI QR is Live-only. In Test Mode, use UPI Checkout with success@razorpay. Switch to Live Mode for real QR payments."}, 400

        qr_id = row.get("razorpay_qr_id")
        image_url = row.get("razorpay_qr_image_url")
        if not qr_id or not image_url:
            qr = create_razorpay_upi_qr(config["key_id"], config["key_secret"], row["payment_amount"], order_id)
            qr_id = qr["id"]
            image_url = qr["image_url"]
            cursor.execute("""
                UPDATE payments
                SET payment_method='upi_qr', razorpay_qr_id=%s, razorpay_qr_image_url=%s,
                    razorpay_qr_status='active', payment_status='pending'
                WHERE payment_id=%s
            """, (qr_id, image_url, row["payment_id"]))
            conn.commit()

        return {
            "success": True,
            "qr": {
                "qr_id": qr_id,
                "image_url": image_url,
                "status": "active",
                "amount": f"₹{Decimal(str(row['estimated_cost'])):.2f}"
            }
        }
    except (Error, ValueError, RuntimeError, requests.RequestException) as e:
        if conn: conn.rollback()
        return {"success": False, "error": f"Could not create UPI QR: {e}"}, 500
    finally:
        close_db(conn, cursor)


@app.route("/payment/qr/status/<int:order_id>", methods=["GET"])
def qr_payment_status(order_id):
    if not ensure_logged_in_user():
        return {"success": False, "error": "Please login as a user."}, 401
    conn = cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        ensure_payment_schema(cursor)
        cursor.execute("""
            SELECT o.order_id, o.user_id, o.shop_id, o.estimated_cost, p.amount AS payment_amount,
                   p.payment_id, p.payment_status, p.razorpay_qr_id
            FROM orders o JOIN payments p ON p.order_id=o.order_id AND p.payment_status <> 'failed'
            WHERE o.order_id=%s AND o.user_id=%s ORDER BY p.payment_id DESC LIMIT 1
        """, (order_id, session["user_id"]))
        row = cursor.fetchone()
        if not row:
            return {"success": False, "error": "Payment order not found."}, 404
        if row["payment_status"] == "paid":
            return {"success": True, "status": "paid", "payment_method": "upi_qr", "qr_status": "closed"}
        if not row.get("razorpay_qr_id"):
            return {"success": True, "status": "pending", "payment_method": "upi_qr", "qr_status": "active"}

        config = get_payment_settings(cursor, row["shop_id"])
        if not config or not config.get("key_id") or not config.get("key_secret"):
            return {"success": False, "error": "Razorpay UPI is not configured for this shop."}, 400
        payments = fetch_razorpay_qr_payments(config["key_id"], config["key_secret"], row["razorpay_qr_id"])
        items = payments.get("items") or []
        expected_amount = int((Decimal(str(row["payment_amount"])) * 100).quantize(Decimal("1")))
        captured = next((item for item in items if item.get("status") == "captured" and int(item.get("amount") or 0) == expected_amount), None)
        if captured:
            cursor.execute("""
                UPDATE payments
                SET payment_status='paid', payment_method='upi_qr', razorpay_payment_id=%s, razorpay_qr_status='closed'
                WHERE payment_id=%s
            """, (captured.get("id"), row["payment_id"]))
            cursor.execute("""
                INSERT INTO notifications (user_id, order_id, message, notification_type)
                SELECT s.owner_id, o.order_id, %s, 'order'
                FROM orders o JOIN print_shops s ON s.shop_id=o.shop_id
                WHERE o.order_id=%s
            """, (f"Order #{order_id} received and payment confirmed.", order_id))
            conn.commit()
            session.pop("qfp_pending_payment_order_id", None)
            return {"success": True, "status": "paid", "payment_method": "upi_qr", "qr_status": "closed", "razorpay_payment_id": captured.get("id")}

        return {"success": True, "status": "pending", "payment_method": "upi_qr", "qr_status": "active"}
    except (Error, RuntimeError, requests.RequestException, ValueError) as e:
        if conn: conn.rollback()
        return {"success": False, "error": f"Could not check UPI QR payment: {e}"}, 500
    finally:
        close_db(conn, cursor)


@app.route("/payment/verify", methods=["POST"])
def verify_payment():
    if not ensure_logged_in_user():
        return {"success": False, "error": "Please login as a user."}, 401

    payload = request.get_json(silent=True) or {}
    try:
        order_id = int(payload.get("order_id"))
    except (TypeError, ValueError):
        return {"success": False, "error": "Invalid order ID."}, 400

    razorpay_payment_id = str(payload.get("razorpay_payment_id") or "").strip()
    razorpay_order_id = str(payload.get("razorpay_order_id") or "").strip()
    razorpay_signature = str(payload.get("razorpay_signature") or "").strip()
    if not razorpay_payment_id or not razorpay_order_id or not razorpay_signature:
        return {"success": False, "error": "Payment verification data is incomplete."}, 400

    conn = cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        ensure_payment_schema(cursor)
        cursor.execute("""
            SELECT o.order_id, o.user_id, o.shop_id, o.estimated_cost, p.amount AS payment_amount,
                   p.payment_id, p.payment_status, p.razorpay_order_id
            FROM orders o
            JOIN payments p ON p.order_id = o.order_id AND p.payment_status <> 'failed'
            WHERE o.order_id = %s AND o.user_id = %s
            ORDER BY p.payment_id DESC
            LIMIT 1
        """, (order_id, session["user_id"]))
        payment = cursor.fetchone()
        if not payment:
            return {"success": False, "error": "Payment order not found."}, 404
        if payment["razorpay_order_id"] != razorpay_order_id:
            return {"success": False, "error": "Payment order mismatch."}, 400
        if payment["payment_status"] == "paid":
            return {"success": True, "message": "Payment already verified.", "order_id": order_id}

        config = get_payment_settings(cursor, payment["shop_id"])
        if not config or not config.get("key_secret"):
            return {"success": False, "error": "Payment gateway is not configured for this shop."}, 400

        expected = hmac.new(
            config["key_secret"].encode("utf-8"),
            f"{razorpay_order_id}|{razorpay_payment_id}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected, razorpay_signature):
            cursor.execute("UPDATE payments SET payment_status='failed' WHERE payment_id=%s", (payment["payment_id"],))
            conn.commit()
            return {"success": False, "error": "Payment signature verification failed."}, 400

        # Confirm the payment with Razorpay as a second server-side check.
        response = requests.get(
            f"https://api.razorpay.com/v1/payments/{razorpay_payment_id}",
            auth=(config["key_id"], config["key_secret"]),
            timeout=15,
        )
        try:
            gateway_payment = response.json()
        except ValueError:
            gateway_payment = {}
        if response.status_code >= 400 or gateway_payment.get("order_id") != razorpay_order_id:
            return {"success": False, "error": "Razorpay could not confirm this payment."}, 400

        expected_amount = int((Decimal(str(payment["payment_amount"])) * 100).quantize(Decimal("1")))
        if int(gateway_payment.get("amount") or 0) != expected_amount or gateway_payment.get("currency") != "INR":
            return {"success": False, "error": "Paid amount does not match the print order."}, 400
        if gateway_payment.get("status") not in {"authorized", "captured"}:
            return {"success": False, "error": "Payment has not been captured yet."}, 400

        cursor.execute("""
            UPDATE payments
            SET payment_status='paid', amount=%s, payment_method=%s,
                razorpay_payment_id=%s, razorpay_order_id=%s, razorpay_signature=%s
            WHERE payment_id=%s
        """, (payment["estimated_cost"], str(gateway_payment.get("method") or "upi"), razorpay_payment_id, razorpay_order_id, razorpay_signature, payment["payment_id"]))

        cursor.execute("""
            INSERT INTO notifications (user_id, order_id, message, notification_type)
            SELECT s.owner_id, o.order_id, %s, 'order'
            FROM orders o JOIN print_shops s ON s.shop_id=o.shop_id
            WHERE o.order_id=%s
        """, (f"Order #{order_id} received and payment confirmed.", order_id))
        conn.commit()
        session.pop("qfp_pending_payment_order_id", None)
        return {"success": True, "order_id": order_id, "message": "Payment successful."}
    except (Error, ValueError, RuntimeError, requests.RequestException) as e:
        if conn:
            conn.rollback()
        return {"success": False, "error": f"Payment verification failed: {e}"}, 500
    finally:
        close_db(conn, cursor)



@app.route("/payment/abandon/<int:order_id>", methods=["POST"])
def abandon_pending_payment(order_id):
    if not ensure_logged_in_user():
        return {"success": False, "error": "Please login as a user."}, 401
    if session.get("qfp_pending_payment_order_id") == int(order_id):
        session.pop("qfp_pending_payment_order_id", None)
    return {"success": True}


@app.route("/payment/status/<int:order_id>", methods=["GET"])
def payment_status(order_id):
    if not ensure_logged_in_user():
        return {"success": False, "error": "Please login as a user."}, 401
    conn = cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        ensure_payment_schema(cursor)
        cursor.execute("""
            SELECT p.payment_status, p.payment_method, p.razorpay_payment_id,
                   p.amount, o.estimated_cost, o.shop_id
            FROM payments p JOIN orders o ON o.order_id=p.order_id
            WHERE p.order_id=%s AND o.user_id=%s AND p.payment_status <> 'failed'
            ORDER BY p.payment_id DESC LIMIT 1
        """, (order_id, session["user_id"]))
        row = cursor.fetchone()
        if not row:
            return {"success": False, "error": "Payment not found."}, 404
        return {"success": True, "status": row["payment_status"], "payment_method": row.get("payment_method"), "razorpay_payment_id": row.get("razorpay_payment_id"), "amount": f"{Decimal(str(row['amount'])):.2f}"}
    except Error as e:
        return {"success": False, "error": str(e)}, 500
    finally:
        close_db(conn, cursor)


# ============================================================
# USER - RECENT ORDERS / ORDER HISTORY
# ============================================================

def get_order_details_for_user(order_id):
    """Return one order and its saved print-item details for the logged-in user."""
    if not ensure_logged_in_user():
        return None

    conn = cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        cursor.execute("""
            SELECT o.order_id, o.user_id, o.document, o.copies, o.color,
                   o.orientation, o.print_side, o.page_range, o.paper_type,
                   o.additional_requirements, o.estimated_cost,
                   o.order_status, o.created_at,
                   COALESCE((SELECT p2.payment_method FROM payments p2 WHERE p2.order_id=o.order_id AND p2.payment_status <> 'failed' ORDER BY p2.payment_id DESC LIMIT 1), 'unselected') AS payment_method,
                   COALESCE((SELECT p3.payment_status FROM payments p3 WHERE p3.order_id=o.order_id AND p3.payment_status <> 'failed' ORDER BY p3.payment_id DESC LIMIT 1), 'pending') AS payment_status,
                   s.shop_name, s.shop_number,
                   COALESCE(p.payment_status, 'pending') AS payment_status,
                   COALESCE(p.payment_method, 'unselected') AS payment_method,
                   oi.file_name, oi.file_path, oi.total_pages,
                   oi.blank_pages, oi.blurry_pages, oi.unrecognizable_pages, oi.uploaded_at
            FROM orders o
            LEFT JOIN print_shops s ON s.shop_id = o.shop_id
            LEFT JOIN order_items oi ON oi.order_id = o.order_id
            WHERE o.order_id = %s AND o.user_id = %s
            LIMIT 1
        """, (order_id, session["user_id"]))

        return cursor.fetchone()
    except Error:
        return None
    finally:
        close_db(conn, cursor)


@app.route("/profile/summary", methods=["GET"])
def profile_summary():
    if not ensure_logged_in_user():
        return {"success": False, "error": "Please login as a user."}, 401

    conn = cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT
                COUNT(*) AS total,
                COALESCE(SUM(CASE WHEN order_status IN ('completed','delivered') THEN 1 ELSE 0 END), 0) AS completed,
                COALESCE(SUM(CASE WHEN order_status IN ('pending','accepted','printing','ready') THEN 1 ELSE 0 END), 0) AS pending,
                COALESCE(SUM(CASE WHEN order_status = 'cancelled' THEN 1 ELSE 0 END), 0) AS cancelled
            FROM orders
            WHERE user_id = %s
        """, (session["user_id"],))
        counts = cursor.fetchone() or {}
        return {
            "success": True,
            "total": int(counts.get("total") or 0),
            "completed": int(counts.get("completed") or 0),
            "pending": int(counts.get("pending") or 0),
            "cancelled": int(counts.get("cancelled") or 0),
        }
    except Error as e:
        return {"success": False, "error": f"Could not load account summary: {e}"}, 500
    finally:
        close_db(conn, cursor)



def _get_completed_receipt_data(order_id, user_id, cursor):
    """Load a completed/delivered order owned by the logged-in student."""
    ensure_gst_schema(cursor)
    cursor.execute("""
        SELECT
            o.order_id, o.document, o.copies, o.color, o.orientation,
            o.print_side, o.page_range, o.paper_type,
            o.additional_requirements, o.estimated_cost,
            o.order_status, o.created_at,
            u.name AS customer_name, u.email AS customer_email, u.phone AS customer_phone,
            s.shop_name, s.shop_number, s.university_name, s.state,
            s.address, s.gst_number,
            p.payment_method, p.payment_status, p.created_at AS payment_date
        FROM orders o
        JOIN users u ON u.user_id = o.user_id
        LEFT JOIN print_shops s ON s.shop_id = o.shop_id
        LEFT JOIN payments p ON p.payment_id = (
            SELECT p2.payment_id
            FROM payments p2
            WHERE p2.order_id = o.order_id
              AND p2.payment_status = 'paid'
            ORDER BY p2.payment_id DESC
            LIMIT 1
        )
        WHERE o.order_id = %s
          AND o.user_id = %s
          AND o.order_status IN ('completed', 'delivered')
        LIMIT 1
    """, (order_id, user_id))
    row = cursor.fetchone()
    if not row:
        return None

    row["amount"] = Decimal(str(row.get("estimated_cost") or 0))
    row["payment_method_display"] = {
        "cash": "Cash at Shop",
        "upi": "UPI",
        "upi_qr": "UPI QR",
        "razorpay": "UPI",
    }.get(str(row.get("payment_method") or "").lower(), str(row.get("payment_method") or "Paid"))
    return row


@app.route("/api/digital-receipt/<int:order_id>", methods=["GET"])
def digital_receipt_data(order_id):
    if not ensure_logged_in_user():
        return {"success": False, "error": "Please login as a user."}, 401

    conn = cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        row = _get_completed_receipt_data(order_id, session["user_id"], cursor)
        if not row:
            return {"success": False, "error": "Digital receipt is available only for your completed orders."}, 404
        if row.get("payment_status") != "paid":
            return {"success": False, "error": "Payment for this order is not marked as paid yet."}, 400

        return {
            "success": True,
            "receipt": {
                "receipt_no": f"QFP-{int(row['order_id']):06d}",
                "order_id": int(row["order_id"]),
                "customer_name": row.get("customer_name") or "Customer",
                "customer_email": row.get("customer_email") or "",
                "customer_phone": row.get("customer_phone") or "",
                "shop_name": row.get("shop_name") or "QueueFree Print Centre",
                "shop_number": row.get("shop_number") or "",
                "gst_number": row.get("gst_number") or "Not provided",
                "university_name": row.get("university_name") or "",
                "state": row.get("state") or "",
                "address": row.get("address") or "",
                "document": row.get("document") or "Document",
                "copies": int(row.get("copies") or 1),
                "color": "Color" if row.get("color") == "color" else "Black & White",
                "orientation": str(row.get("orientation") or "portrait").title(),
                "print_side": "Double-Sided" if row.get("print_side") == "double" else "Single-Sided",
                "page_range": row.get("page_range") or "All Pages",
                "paper_type": row.get("paper_type") or "Printing",
                "additional_requirements": row.get("additional_requirements") or "None",
                "amount": f"{row['amount']:.2f}",
                "payment_method": row["payment_method_display"],
                "order_date": row["created_at"].strftime("%d %b %Y, %I:%M %p") if row.get("created_at") else "",
                "payment_date": row["payment_date"].strftime("%d %b %Y, %I:%M %p") if row.get("payment_date") else "",
                "status": str(row.get("order_status") or "completed").title(),
            }
        }
    except Error as e:
        return {"success": False, "error": f"Could not load digital receipt: {e}"}, 500
    finally:
        close_db(conn, cursor)


@app.route("/digital-receipt/<int:order_id>/download", methods=["GET"])
def download_digital_receipt(order_id):
    if not ensure_logged_in_user():
        return redirect(url_for("login"))

    conn = cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        row = _get_completed_receipt_data(order_id, session["user_id"], cursor)
        if not row:
            return "Digital receipt is available only for your completed orders.", 404
        if row.get("payment_status") != "paid":
            return "Payment for this order is not marked as paid yet.", 400
        if canvas is None or A4 is None:
            return "PDF generation is unavailable. Please install reportlab.", 500

        buffer = io.BytesIO()
        pdf = canvas.Canvas(buffer, pagesize=A4)
        width, height = A4

        left = 48
        right = width - 48
        y = height - 52

        # Header
        pdf.setFillColorRGB(0.12, 0.12, 0.12)
        pdf.setFont("Helvetica-Bold", 20)
        pdf.drawString(left, y, "Queue-Free")
        pdf.setFillColorRGB(0.40, 0.40, 0.40)
        pdf.drawString(left + 112, y, "Print")
        pdf.setFillColorRGB(0.12, 0.12, 0.12)
        pdf.setFont("Helvetica-Bold", 15)
        pdf.drawRightString(right, y, "DIGITAL RECEIPT")
        y -= 20
        pdf.setStrokeColorRGB(0.82, 0.82, 0.82)
        pdf.line(left, y, right, y)
        y -= 28

        # Receipt meta
        pdf.setFont("Helvetica-Bold", 9)
        pdf.setFillColorRGB(0.35, 0.35, 0.35)
        pdf.drawString(left, y, "RECEIPT NO.")
        pdf.drawString(left + 150, y, "ORDER ID")
        pdf.drawRightString(right, y, "PAYMENT STATUS")
        y -= 14
        pdf.setFont("Helvetica-Bold", 11)
        pdf.setFillColorRGB(0.10, 0.10, 0.10)
        pdf.drawString(left, y, f"QFP-{int(row['order_id']):06d}")
        pdf.drawString(left + 150, y, f"#ORD{int(row['order_id']):03d}")
        pdf.drawRightString(right, y, "PAID")
        y -= 30

        # Shop/customer cards
        card_w = (right - left - 18) / 2
        card_h = 118
        pdf.setFillColorRGB(0.97, 0.97, 0.97)
        pdf.roundRect(left, y - card_h, card_w, card_h, 8, fill=1, stroke=0)
        pdf.roundRect(left + card_w + 18, y - card_h, card_w, card_h, 8, fill=1, stroke=0)

        pdf.setFillColorRGB(0.20, 0.20, 0.20)
        pdf.setFont("Helvetica-Bold", 10)
        pdf.drawString(left + 14, y - 20, "PRINT CENTRE")
        pdf.setFont("Helvetica-Bold", 11)
        pdf.drawString(left + 14, y - 39, str(row.get("shop_name") or "QueueFree Print Centre")[:42])
        pdf.setFont("Helvetica", 9)
        shop_lines = [
            f"Shop ID: {row.get('shop_number') or 'N/A'}",
            f"GSTIN: {row.get('gst_number') or 'Not provided'}",
            f"State: {row.get('state') or 'N/A'}",
        ]
        yy = y - 57
        for line in shop_lines:
            pdf.drawString(left + 14, yy, line[:55])
            yy -= 14

        cx = left + card_w + 32
        pdf.setFont("Helvetica-Bold", 10)
        pdf.drawString(cx, y - 20, "CUSTOMER")
        pdf.setFont("Helvetica-Bold", 11)
        pdf.drawString(cx, y - 39, str(row.get("customer_name") or "Customer")[:42])
        pdf.setFont("Helvetica", 9)
        customer_lines = [
            f"Email: {row.get('customer_email') or 'N/A'}",
            f"Phone: {row.get('customer_phone') or 'N/A'}",
            f"Order Date: {row['created_at'].strftime('%d %b %Y, %I:%M %p') if row.get('created_at') else 'N/A'}",
        ]
        yy = y - 57
        for line in customer_lines:
            pdf.drawString(cx, yy, line[:55])
            yy -= 14

        y -= card_h + 28

        # Print details
        pdf.setFont("Helvetica-Bold", 11)
        pdf.setFillColorRGB(0.12, 0.12, 0.12)
        pdf.drawString(left, y, "PRINT DETAILS")
        y -= 16
        pdf.setFillColorRGB(0.20, 0.20, 0.20)
        pdf.setFont("Helvetica-Bold", 9)
        pdf.drawString(left, y, "DOCUMENT")
        pdf.drawString(left + 240, y, "COPIES")
        pdf.drawRightString(right, y, "AMOUNT")
        y -= 16
        pdf.setFont("Helvetica", 9.5)
        doc_name = str(row.get("document") or "Document")
        pdf.drawString(left, y, doc_name[:42])
        pdf.drawString(left + 240, y, str(row.get("copies") or 1))
        pdf.setFont("Helvetica-Bold", 11)
        pdf.drawRightString(right, y, f"Rs. {row['amount']:.2f}")
        y -= 12
        pdf.setStrokeColorRGB(0.88, 0.88, 0.88)
        pdf.line(left, y, right, y)
        y -= 22

        details = [
            ("Colour", "Color" if row.get("color") == "color" else "Black & White"),
            ("Orientation", str(row.get("orientation") or "portrait").title()),
            ("Print Side", "Double-Sided" if row.get("print_side") == "double" else "Single-Sided"),
            ("Page Range", row.get("page_range") or "All Pages"),
            ("Paper Type", row.get("paper_type") or "Printing"),
            ("Payment Method", row["payment_method_display"]),
        ]
        pdf.setFont("Helvetica", 9.5)
        for label, value in details:
            pdf.setFillColorRGB(0.40, 0.40, 0.40)
            pdf.drawString(left, y, label)
            pdf.setFillColorRGB(0.12, 0.12, 0.12)
            pdf.setFont("Helvetica-Bold", 9.5)
            pdf.drawRightString(right, y, str(value)[:70])
            pdf.setFont("Helvetica", 9.5)
            y -= 18

        y -= 8
        pdf.setFillColorRGB(0.96, 0.96, 0.96)
        pdf.roundRect(left, y - 54, right - left, 54, 7, fill=1, stroke=0)
        pdf.setFillColorRGB(0.25, 0.25, 0.25)
        pdf.setFont("Helvetica", 9)
        pdf.drawString(left + 14, y - 19, "TOTAL PAID")
        pdf.setFillColorRGB(0.10, 0.10, 0.10)
        pdf.setFont("Helvetica-Bold", 17)
        pdf.drawRightString(right - 14, y - 21, f"Rs. {row['amount']:.2f}")
        pdf.setFillColorRGB(0.45, 0.45, 0.45)
        pdf.setFont("Helvetica", 8)
        pdf.drawString(left + 14, y - 38, "Payment received successfully for the completed print order.")
        y -= 82

        pdf.setFillColorRGB(0.40, 0.40, 0.40)
        pdf.setFont("Helvetica", 8.5)
        pdf.drawString(left, y, f"Payment Date: {row['payment_date'].strftime('%d %b %Y, %I:%M %p') if row.get('payment_date') else 'N/A'}")
        pdf.drawRightString(right, y, "This is a computer-generated digital receipt.")
        y -= 18
        pdf.drawCentredString(width / 2, y, "Thank you for using Queue-Free Print.")

        pdf.save()
        buffer.seek(0)

        return send_file(
            buffer,
            mimetype="application/pdf",
            as_attachment=True,
            download_name=f"QueueFree_Receipt_ORD{int(row['order_id']):03d}.pdf",
        )
    except Error as e:
        return f"Could not generate digital receipt: {e}", 500
    finally:
        close_db(conn, cursor)


@app.route("/recent-orders")
def recent_orders():
    if not ensure_logged_in_user():
        if session.get("role") == "admin":
            return redirect(url_for("admin_dashboard"))
        return redirect(url_for("login"))

    orders = []
    conn = cursor = None

    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        cursor.execute("""
            SELECT o.order_id, o.document, o.copies, o.color,
                   o.orientation, o.print_side, o.page_range, o.paper_type,
                   o.additional_requirements, o.estimated_cost,
                   o.order_status, o.created_at,
                   COALESCE((SELECT p2.payment_method FROM payments p2 WHERE p2.order_id=o.order_id AND p2.payment_status <> 'failed' ORDER BY p2.payment_id DESC LIMIT 1), 'unselected') AS payment_method,
                   COALESCE((SELECT p3.payment_status FROM payments p3 WHERE p3.order_id=o.order_id AND p3.payment_status <> 'failed' ORDER BY p3.payment_id DESC LIMIT 1), 'pending') AS payment_status,
                   s.shop_name, s.shop_number, s.gst_number,
                   (SELECT MIN(oi2.uploaded_at) FROM order_items oi2 WHERE oi2.order_id = o.order_id) AS uploaded_at,
                   CASE WHEN EXISTS (SELECT 1 FROM order_items oi2 WHERE oi2.order_id = o.order_id)
                             AND NOT EXISTS (SELECT 1 FROM order_items oi3
                                             WHERE oi3.order_id = o.order_id
                                               AND (oi3.uploaded_at IS NULL
                                                    OR oi3.uploaded_at <= DATE_SUB(NOW(), INTERVAL %s DAY)
                                                    OR oi3.file_path IS NULL
                                                    OR oi3.file_path = ''))
                        THEN 1 ELSE 0 END AS reorder_available,
                   COALESCE((SELECT SUM(COALESCE(oi.total_pages, 0))
                             FROM order_items oi WHERE oi.order_id = o.order_id), 0) AS total_pages,
                   COALESCE((SELECT SUM(COALESCE(oi.blank_pages, 0))
                             FROM order_items oi WHERE oi.order_id = o.order_id), 0) AS blank_pages,
                   COALESCE((SELECT SUM(COALESCE(oi.blurry_pages, 0))
                             FROM order_items oi WHERE oi.order_id = o.order_id), 0) AS blurry_pages,
                   COALESCE((SELECT SUM(COALESCE(oi.unrecognizable_pages, 0))
                             FROM order_items oi WHERE oi.order_id = o.order_id), 0) AS unrecognizable_pages
            FROM orders o
            LEFT JOIN print_shops s ON s.shop_id = o.shop_id
            WHERE o.user_id = %s
              AND o.order_status NOT IN ('cancelled', 'declined')
            ORDER BY o.created_at DESC
        """, (FILE_RETENTION_DAYS, session["user_id"]))

        orders = cursor.fetchall()
    except Error as e:
        flash(f"Could not load your orders: {e}", "error")
    finally:
        close_db(conn, cursor)

    return render_template(
        "recentstudentorder.html",
        orders=orders,
        name=session.get("name", "Student")
    )



@app.route("/api/order-wait-time/<int:order_id>", methods=["GET"])
def order_wait_time(order_id):
    """Return a live shop-specific estimate until the order is ready.

    Factors: queue position, analyzed printable pages, copies, colour,
    single/double-sided work, and the live workload already at the shop.
    It is recalculated on every request so the estimate changes as orders move.
    """
    if not ensure_logged_in_user():
        return {"success": False, "error": "Please login as a user."}, 401

    conn = cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        cursor.execute("""
            SELECT o.order_id, o.user_id, o.shop_id, o.created_at, o.order_status,
                   o.copies, o.color, o.print_side,
                   COALESCE(SUM(COALESCE(oi.total_pages, 0)), 0) AS total_pages,
                   COALESCE(SUM(COALESCE(oi.total_pages, 0) * COALESCE(oi.copies, o.copies, 1)), 0) AS print_pages,
                   MAX(CASE WHEN oi.color = 'color' OR o.color = 'color' THEN 1 ELSE 0 END) AS has_color,
                   MAX(CASE WHEN oi.print_side = 'double' OR o.print_side = 'double' THEN 1 ELSE 0 END) AS has_double
            FROM orders o
            LEFT JOIN order_items oi ON oi.order_id = o.order_id
            WHERE o.order_id = %s AND o.user_id = %s
            GROUP BY o.order_id, o.user_id, o.shop_id, o.created_at, o.order_status,
                     o.copies, o.color, o.print_side
            LIMIT 1
        """, (order_id, session["user_id"]))
        target = cursor.fetchone()
        if not target:
            return {"success": False, "error": "Order not found."}, 404

        active_statuses = ("pending", "accepted", "printing")
        if target["order_status"] not in active_statuses:
            status_text = str(target["order_status"]).replace("_", " ")
            return {
                "success": True, "order_id": order_id, "status": target["order_status"],
                "active": False, "queue_position": 0, "orders_ahead": 0,
                "estimated_wait_minutes": 0,
                "estimated_wait_text": f"No waiting time — order is already {status_text}."
            }

        def work_minutes(row):
            pages = max(1, int(row.get("print_pages") or 0))
            color = bool(row.get("has_color"))
            double = bool(row.get("has_double"))
            # Practical local estimate; the live queue/workload is the important part.
            ppm = 15.0 if color else 22.0
            setup = 1.5 + (0.5 if double else 0.0)
            return setup + pages / ppm

        cursor.execute("""
            SELECT o.order_id, o.created_at, o.order_status, o.copies, o.color, o.print_side,
                   COALESCE(SUM(COALESCE(oi.total_pages, 0) * COALESCE(oi.copies, o.copies, 1)), 0) AS print_pages,
                   MAX(CASE WHEN oi.color = 'color' OR o.color = 'color' THEN 1 ELSE 0 END) AS has_color,
                   MAX(CASE WHEN oi.print_side = 'double' OR o.print_side = 'double' THEN 1 ELSE 0 END) AS has_double
            FROM orders o
            LEFT JOIN order_items oi ON oi.order_id = o.order_id
            WHERE o.shop_id = %s
              AND o.order_status IN ('pending','accepted','printing')
              AND (o.created_at < %s OR (o.created_at = %s AND o.order_id < %s))
            GROUP BY o.order_id, o.created_at, o.order_status, o.copies, o.color, o.print_side
            ORDER BY o.created_at ASC, o.order_id ASC
        """, (target["shop_id"], target["created_at"], target["created_at"], order_id))
        ahead = cursor.fetchall()

        def remaining_for(row):
            estimate = work_minutes(row)
            if row.get("order_status") != "printing":
                return estimate
            cursor.execute("""
                SELECT changed_at
                FROM order_history
                WHERE order_id = %s AND new_status = 'printing'
                ORDER BY changed_at DESC, history_id DESC
                LIMIT 1
            """, (row["order_id"],))
            history = cursor.fetchone()
            if not history or not history.get("changed_at"):
                return estimate
            started = history["changed_at"]
            elapsed = max(0.0, (datetime.now() - started).total_seconds() / 60.0)
            return max(0.5, estimate - elapsed)

        workload_ahead = sum(remaining_for(row) for row in ahead)
        queue_position = len(ahead) + 1

        if target["order_status"] == "printing":
            # For a printing order, show the remaining time for this order itself.
            wait_minutes = remaining_for(target)
        else:
            # Include the target's own print work: this is the estimated time until
            # the print is ready, not merely the time until printing starts.
            wait_minutes = workload_ahead + work_minutes(target) + (len(ahead) * 0.75)

        rounded = max(1, int(round(wait_minutes)))
        if rounded < 60:
            text = f"Estimated wait: ~{rounded} min"
        else:
            hours, mins = divmod(rounded, 60)
            text = f"Estimated wait: ~{hours} hr {mins} min" if mins else f"Estimated wait: ~{hours} hr"

        return {
            "success": True,
            "order_id": order_id,
            "shop_id": target["shop_id"],
            "status": target["order_status"],
            "active": True,
            "queue_position": queue_position,
            "orders_ahead": len(ahead),
            "print_pages": int(target.get("print_pages") or 0),
            "estimated_wait_minutes": rounded,
            "estimated_wait_text": text,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
    except Error as e:
        return {"success": False, "error": f"Could not calculate waiting time: {e}"}, 500
    finally:
        close_db(conn, cursor)


@app.route("/cancel-order/<int:order_id>", methods=["POST"])
def cancel_order(order_id):
    if not ensure_logged_in_user():
        return {"success": False, "error": "Please login as a user."}, 401

    conn = cursor = None
    file_paths = []
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        cursor.execute("""
            SELECT order_id, order_status
            FROM orders
            WHERE order_id = %s AND user_id = %s
            LIMIT 1
        """, (order_id, session["user_id"]))
        order = cursor.fetchone()

        if not order:
            return {"success": False, "error": "Order not found."}, 404

        if order["order_status"] in ("completed", "delivered"):
            return {"success": False, "error": "Completed orders cannot be cancelled."}, 400

        # Idempotent: if a previous request already cancelled it, report success.
        if order["order_status"] == "cancelled":
            return {"success": True, "order_id": order_id, "status": "cancelled"}

        cursor.execute("""
            SELECT file_path
            FROM order_items
            WHERE order_id = %s
        """, (order_id,))
        file_paths = [row["file_path"] for row in cursor.fetchall() if row.get("file_path")]

        # Update the parent order first, then remove item/notification data.
        cursor.execute("""
            UPDATE orders
            SET order_status = 'cancelled'
            WHERE order_id = %s AND user_id = %s
              AND order_status NOT IN ('completed', 'delivered', 'cancelled')
        """, (order_id, session["user_id"]))

        if cursor.rowcount != 1:
            conn.rollback()
            return {"success": False, "error": "Order could not be cancelled. Please try again."}, 400

        cursor.execute("DELETE FROM notifications WHERE order_id = %s", (order_id,))
        cursor.execute("DELETE FROM order_items WHERE order_id = %s", (order_id,))
        conn.commit()

        # Remove uploaded files only after the database transaction succeeds.
        for relative_path in file_paths:
            try:
                path = (Path(app.root_path) / relative_path).resolve()
                if path.is_file() and UPLOAD_ROOT.resolve() in path.parents:
                    path.unlink()
            except (OSError, ValueError):
                pass

        return {"success": True, "order_id": order_id, "status": "cancelled"}

    except Error as e:
        if conn:
            conn.rollback()
        return {"success": False, "error": f"Order could not be cancelled: {e}"}, 500
    finally:
        close_db(conn, cursor)



@app.route("/edit-order/<int:order_id>", methods=["GET"])
def edit_order_details(order_id):
    if not ensure_logged_in_user():
        return {"success": False, "error": "Please login as a user."}, 401
    conn = cursor = None
    try:
        conn = get_db_connection(); cursor = conn.cursor(dictionary=True)
        ensure_order_item_columns(cursor)
        cursor.execute("""
            SELECT o.order_id, o.user_id, o.shop_id, o.document, o.copies, o.color,
                   o.orientation, o.print_side, o.page_range, o.paper_type,
                   o.additional_requirements, o.estimated_cost, o.order_status,
                   COALESCE((SELECT SUM(p.amount) FROM payments p WHERE p.order_id=o.order_id AND p.payment_status='paid'),0) AS paid_amount,
                   s.shop_name, s.shop_number
            FROM orders o LEFT JOIN print_shops s ON s.shop_id=o.shop_id
            WHERE o.order_id=%s AND o.user_id=%s LIMIT 1
        """, (order_id, session["user_id"]))
        order = cursor.fetchone()
        if not order:
            return {"success": False, "error": "Order not found."}, 404
        if order["order_status"] != "pending":
            return {"success": False, "error": "Only pending orders can be edited."}, 400
        cursor.execute("""
            SELECT item_id, file_name, file_path, total_pages, blank_pages,
                   blurry_pages, unrecognizable_pages, copies, color, orientation,
                   print_side, page_range, paper_type, additional_requirements
            FROM order_items WHERE order_id=%s ORDER BY item_id ASC
        """, (order_id,))
        items = cursor.fetchall()
        return {"success": True, "order": order, "items": items}
    except Error as e:
        return {"success": False, "error": f"Could not load edit data: {e}"}, 500
    finally:
        close_db(conn, cursor)


@app.route("/edit-order/<int:order_id>/cost", methods=["POST"])
def edit_order_cost(order_id):
    """Return the live edited total/residual without changing the order."""
    if not ensure_logged_in_user():
        return {"success": False, "error": "Please login as a user."}, 401
    conn = cursor = None
    try:
        removed_ids = {int(x) for x in json.loads(request.form.get("removed_item_ids", "[]"))}
        new_pages = max(0, int(request.form.get("new_pages", "0") or 0))
        copies = max(1, int(request.form.get("copies", "1") or 1))
        paper_type = request.form.get("paper_type", "printing")
        conn = get_db_connection(); cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT order_id, shop_id, order_status FROM orders WHERE order_id=%s AND user_id=%s LIMIT 1", (order_id, session["user_id"]))
        order = cursor.fetchone()
        if not order or order["order_status"] != "pending":
            return {"success": False, "error": "Only pending orders can be edited."}, 400
        cursor.execute("SELECT item_id, total_pages FROM order_items WHERE order_id=%s ORDER BY item_id ASC", (order_id,))
        existing = cursor.fetchall()
        existing_pages = sum(max(0, int(x.get("total_pages") or 0)) for x in existing if int(x["item_id"]) not in removed_ids)
        total_pages = existing_pages + new_pages
        if total_pages <= 0:
            return {"success": False, "error": "At least one printable page is required."}, 400
        _, _, new_cost = get_shop_print_cost(cursor, order["shop_id"], paper_type, total_pages, copies)
        cursor.execute("SELECT COALESCE(SUM(amount),0) AS paid_amount FROM payments WHERE order_id=%s AND payment_status='paid'", (order_id,))
        paid = Decimal(str(cursor.fetchone()["paid_amount"] or 0))
        residual = max(Decimal("0.00"), (Decimal(str(new_cost)) - paid).quantize(Decimal("0.01")))
        return {"success": True, "total_pages": total_pages, "new_cost": float(new_cost), "paid_amount": float(paid), "residual_amount": float(residual)}
    except (ValueError, TypeError, json.JSONDecodeError, Error) as exc:
        return {"success": False, "error": str(exc)}, 400
    finally:
        close_db(conn, cursor)


@app.route("/edit-order/<int:order_id>", methods=["POST"])
def save_edit_order(order_id):
    if not ensure_logged_in_user():
        return {"success": False, "error": "Please login as a user."}, 401

    files = [f for f in request.files.getlist("documents") if f and f.filename]
    try:
        removed_ids = [int(x) for x in json.loads(request.form.get("removed_item_ids", "[]"))]
        analyses = json.loads(request.form.get("analyses", "[]"))
        copies = max(1, int(request.form.get("copies", "1")))
        total_pages_new = int(request.form.get("total_pages", "0") or 0)
        blank_new = int(request.form.get("blank_pages", "0") or 0)
        duplicate_new = int(request.form.get("duplicate_pages", "0") or 0)
        blurry_new = int(request.form.get("blurry_pages", "0") or 0)
        unrec_new = int(request.form.get("unrecognizable_pages", "0") or 0)
    except (ValueError, TypeError, json.JSONDecodeError):
        return {"success": False, "error": "Invalid edit data."}, 400

    if len(analyses) != len(files):
        return {"success": False, "error": "Please analyze every newly added document before saving."}, 400
    if not files and total_pages_new != 0:
        return {"success": False, "error": "Invalid new document analysis."}, 400

    conn = cursor = None; saved_paths=[]
    try:
        conn=get_db_connection(); cursor=conn.cursor(dictionary=True)
        ensure_order_item_columns(cursor)
        ensure_edit_tracking_schema(cursor)
        cursor.execute("SELECT * FROM orders WHERE order_id=%s AND user_id=%s FOR UPDATE", (order_id, session["user_id"]))
        order=cursor.fetchone()
        if not order: return {"success": False, "error": "Order not found."},404
        if order["order_status"] != "pending": return {"success": False, "error": "This order can no longer be edited because the shop has accepted it."},400

        cursor.execute("SELECT * FROM order_items WHERE order_id=%s ORDER BY item_id ASC", (order_id,))
        existing=cursor.fetchall()
        existing_ids={int(x["item_id"]) for x in existing}
        removed=set(removed_ids) & existing_ids
        remaining=[x for x in existing if int(x["item_id"]) not in removed]
        if not remaining and not files:
            return {"success": False, "error": "At least one document must remain in the order."},400

        # New files are analyzed client-side with the same Gemini endpoint used by the dashboard.
        if files and total_pages_new <= 0:
            return {"success": False, "error": "Please analyze the newly added documents first."},400

        color=request.form.get("color", order["color"])
        orientation=request.form.get("orientation", order["orientation"])
        print_side=request.form.get("print_side", order["print_side"])
        page_range=request.form.get("page_range", order["page_range"]) or "all"
        paper_type=request.form.get("paper_type", order["paper_type"])
        additional=request.form.get("additional_requirements", order.get("additional_requirements") or "").strip()
        total_pages_existing=sum(int(x.get("total_pages") or 0) for x in remaining)
        total_pages=total_pages_existing + total_pages_new
        if total_pages <= 0: return {"success": False, "error": "The edited order must contain at least one valid page."},400
        _,_,new_cost=get_shop_print_cost(cursor, order["shop_id"], paper_type, total_pages, copies)

        # Paid amount is the sum of historical successful payments for this same order.
        cursor.execute("SELECT COALESCE(SUM(amount),0) AS paid_amount FROM payments WHERE order_id=%s AND payment_status='paid'", (order_id,))
        paid_amount=Decimal(str(cursor.fetchone()["paid_amount"] or 0))
        residual=max(Decimal("0.00"), (Decimal(str(new_cost))-paid_amount).quantize(Decimal("0.01")))

        # Remove selected old item rows and their files.
        for item in existing:
            if int(item["item_id"]) in removed:
                path=_safe_upload_path(item.get("file_path"))
                if path and path.is_file():
                    try: path.unlink()
                    except OSError: pass
                cursor.execute("DELETE FROM order_items WHERE item_id=%s AND order_id=%s", (item["item_id"],order_id))

        # Update all retained documents to the new order-level preferences.
        for item in remaining:
            cursor.execute("""
                UPDATE order_items SET copies=%s,color=%s,double_sided=%s,page_range=%s,
                    orientation=%s,print_side=%s,paper_type=%s,additional_requirements=%s,
                    shop_id=%s,cost=%s WHERE item_id=%s AND order_id=%s
            """, (copies,color,1 if print_side=="double" else 0,page_range,orientation,print_side,
                  paper_type,additional,order["shop_id"],Decimal(str(new_cost))*Decimal(str(item.get("total_pages") or 0))/Decimal(str(total_pages)),item["item_id"],order_id))

        user_dir=UPLOAD_ROOT/str(session["user_id"]); user_dir.mkdir(parents=True,exist_ok=True)
        for idx,file in enumerate(files):
            analysis=analyses[idx]; safe_name=secure_filename(file.filename) or f"document_{idx+1}"
            stored=user_dir/f"{uuid.uuid4().hex}_{safe_name}"; file.save(stored); saved_paths.append(stored)
            rel=str(stored.relative_to(app.root_path)).replace("\\","/")
            pages=max(0,int(analysis.get("total_pages",0) or 0))
            item_cost=(Decimal(str(new_cost))*Decimal(pages)/Decimal(str(total_pages))).quantize(Decimal("0.01")) if total_pages else Decimal("0.00")
            cursor.execute("""
                INSERT INTO order_items (order_id,file_name,file_path,copies,color,double_sided,page_range,cost,
                    shop_id,orientation,print_side,paper_type,additional_requirements,total_pages,blank_pages,
                    blurry_pages,unrecognizable_pages,uploaded_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,(order_id,safe_name,rel,copies,color,1 if print_side=="double" else 0,page_range,item_cost,order["shop_id"],
                  orientation,print_side,paper_type,additional,pages,max(0,int(analysis.get("blank_pages",0) or 0)),
                  max(0,int(analysis.get("blurry_pages",0) or 0)),max(0,int(analysis.get("unrecognizable_pages",0) or 0)),datetime.now()))

        document_label=f"{len(remaining)+len(files)} documents" if len(remaining)+len(files)>1 else (remaining[0].get("file_name") if remaining else (secure_filename(files[0].filename) or "document"))
        # Keep compatibility with existing QueueFree databases that do not have
        # the optional `total_cost` column. `estimated_cost` is the canonical
        # order total used by the current application.
        cursor.execute("""
            UPDATE orders SET document=%s,copies=%s,color=%s,orientation=%s,print_side=%s,page_range=%s,
                paper_type=%s,additional_requirements=%s,estimated_cost=%s,last_edited_at=NOW()
            WHERE order_id=%s AND user_id=%s AND order_status='pending'
        """,(document_label,copies,color,orientation,print_side,page_range,paper_type,additional,str(new_cost),order_id,session["user_id"]))

        # Refresh the order-level analysis summary for the edited order.
        cursor.execute("SELECT COALESCE(SUM(blank_pages),0) AS blank_pages, COALESCE(SUM(blurry_pages),0) AS blurry_pages, COALESCE(SUM(unrecognizable_pages),0) AS unrecognizable_pages, COALESCE(SUM(total_pages),0) AS total_pages FROM order_items WHERE order_id=%s", (order_id,))
        agg = cursor.fetchone() or {}
        try:
            cursor.execute("DELETE FROM document_analysis WHERE order_id=%s", (order_id,))
            cursor.execute("""
                INSERT INTO document_analysis
                  (order_id, blank_pages, duplicate_pages, invisible_text, total_pages, blurry_pages, unrecognizable_pages, analysis_status)
                VALUES (%s,%s,%s,0,%s,%s,%s,'completed')
            """, (order_id, int(agg.get('blank_pages') or 0), max(0, duplicate_new), int(agg.get('total_pages') or 0), int(agg.get('blurry_pages') or 0), int(agg.get('unrecognizable_pages') or 0)))
        except Exception as analysis_error:
            app.logger.warning("Could not refresh edit analysis for order %s: %s", order_id, analysis_error)

        ensure_payment_schema(cursor)
        # Reuse the existing payment row because some QueueFree databases enforce
        # UNIQUE(order_id) on payments. Never insert a second payment row for an edit.
        cursor.execute("SELECT payment_id, amount, payment_status, payment_method FROM payments WHERE order_id=%s ORDER BY payment_id DESC LIMIT 1 FOR UPDATE", (order_id,))
        payment_row = cursor.fetchone()
        if payment_row:
            if residual > 0:
                cursor.execute("""
                    UPDATE payments
                    SET amount=%s, payment_status='pending', payment_method='unselected',
                        razorpay_payment_id=NULL, razorpay_order_id=NULL, razorpay_signature=NULL,
                        razorpay_qr_id=NULL, razorpay_qr_image_url=NULL, razorpay_qr_status=NULL,
                        razorpay_payment_link_id=NULL, razorpay_payment_link_url=NULL
                    WHERE payment_id=%s
                """, (residual, payment_row['payment_id']))
            elif payment_row.get('payment_status') == 'paid':
                cursor.execute("UPDATE payments SET amount=%s WHERE payment_id=%s", (new_cost, payment_row['payment_id']))
            else:
                cursor.execute("UPDATE payments SET amount=%s, payment_status='paid' WHERE payment_id=%s", (new_cost, payment_row['payment_id']))
        elif residual > 0:
            cursor.execute("INSERT INTO payments (order_id,amount,payment_status,payment_method,razorpay_order_id) VALUES (%s,%s,'pending','unselected',NULL)", (order_id,residual))
        conn.commit()
        payment_config=get_payment_settings(cursor,order["shop_id"])
        session["qfp_pending_payment_order_id"]=int(order_id) if residual>0 else None
        return {"success":True,"order_id":order_id,"new_cost":f"₹ {new_cost:.2f}","paid_amount":f"₹ {paid_amount:.2f}","residual":f"₹ {residual:.2f}","residual_amount":float(residual),"shop":order.get("shop_number") or order.get("shop_name") or "N/A","online_payment_available":bool(payment_config and payment_config.get("is_enabled") and payment_config.get("key_id") and payment_config.get("key_secret"))}
    except (Error,ValueError) as e:
        if conn: conn.rollback()
        for path in saved_paths:
            try:path.unlink(missing_ok=True)
            except Exception:pass
        return {"success":False,"error":f"Order could not be updated: {e}"},500
    finally: close_db(conn,cursor)

@app.route("/reorder/<int:order_id>", methods=["POST"])
def reorder_order(order_id):
    if not ensure_logged_in_user():
        return {"success": False, "error": "Please login as a user."}, 401

    conn = cursor = None
    new_file_path = None

    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # Make sure retention/reorder columns exist before querying them.
        # This is important for databases created before the 7-day privacy update.
        ensure_order_item_columns(cursor)

        # Older orders created before the 7-day privacy update may have a
        # NULL uploaded_at value. Backfill it from the actual file timestamp
        # (or the order creation time) before checking reorder eligibility.
        # This keeps existing completed orders reorderable instead of failing
        # simply because the new retention column was added later.
        backfill_uploaded_at(cursor)
        conn.commit()

        # Fetch the old order and its complete saved print configuration.
        cursor.execute("""
            SELECT o.*, s.shop_name, s.shop_number,
                   oi.file_name, oi.file_path, oi.total_pages,
                   oi.blank_pages, oi.blurry_pages, oi.unrecognizable_pages
            FROM orders o
            LEFT JOIN print_shops s ON s.shop_id = o.shop_id
            LEFT JOIN order_items oi ON oi.order_id = o.order_id
            WHERE o.order_id = %s AND o.user_id = %s
            LIMIT 1
        """, (order_id, session["user_id"]))
        old = cursor.fetchone()

        if not old:
            return {"success": False, "error": "Order not found."}, 404

        # Reorder is intended for completed/delivered documents.
        if old["order_status"] not in ("completed", "delivered"):
            return {
                "success": False,
                "error": "Only completed orders can be reordered."
            }, 400

        cursor.execute("""
            SELECT file_name, file_path, uploaded_at, total_pages, blank_pages, blurry_pages, unrecognizable_pages
            FROM order_items
            WHERE order_id = %s
            ORDER BY item_id ASC
        """, (order_id,))
        source_items = cursor.fetchall()
        retention_cutoff = datetime.now() - timedelta(days=FILE_RETENTION_DAYS)

        # Every source document must still exist and be within the 7-day window.
        # Validate each item explicitly so multi-file orders reorder reliably.
        if not source_items:
            return {"success": False, "error": "No document is attached to this order."}, 410

        for item in source_items:
            uploaded_at = item.get("uploaded_at")
            file_path = item.get("file_path")
            source_path = _resolve_reorder_source_path(
                file_path, item.get("file_name"), session["user_id"]
            )
            # For legacy rows, derive retention age from the actual UUID file
            # when uploaded_at was never stored.
            if not uploaded_at and source_path and source_path.is_file():
                uploaded_at = datetime.fromtimestamp(source_path.stat().st_mtime)
            if (not uploaded_at or uploaded_at <= retention_cutoff
                    or not source_path or not source_path.is_file()):
                return {
                    "success": False,
                    "error": "This document is more than 7 days old or has already been permanently deleted for privacy. Reorder is no longer available."
                }, 410

        # Create a fresh copy of every source document so multi-file orders
        # keep all original files and each new order gets its own 7-day retention window.
        import shutil
        user_dir = UPLOAD_ROOT / str(session["user_id"])
        user_dir.mkdir(parents=True, exist_ok=True)

        copied_items = []
        try:
            for source in source_items:
                safe_name = secure_filename(source.get("file_name") or "document") or "document"
                # IMPORTANT: use the resolver result from the validation pass.
                # UUID-prefixed uploads may not be reconstructable from file_name.
                source_path = _resolve_reorder_source_path(
                    source.get("file_path"), source.get("file_name"), session["user_id"]
                )
                if not source_path or not source_path.is_file():
                    raise FileNotFoundError(
                        f"Stored document could not be found for {safe_name}"
                    )
                stored_name = f"{uuid.uuid4().hex}_{safe_name}"
                destination = user_dir / stored_name
                shutil.copy2(source_path, destination)
                copied_items.append((source, destination))

            # Insert a completely new order. The database generates a new order_id.
            cursor.execute("""
                INSERT INTO orders
                  (user_id, shop_id, document, copies, color, orientation,
                   print_side, page_range, paper_type, additional_requirements,
                   estimated_cost, order_status)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending')
            """, (
                session["user_id"], old["shop_id"], old["document"], old["copies"],
                old["color"], old["orientation"], old["print_side"], old["page_range"],
                old["paper_type"], old["additional_requirements"], old["estimated_cost"],
            ))
            new_order_id = cursor.lastrowid

            for source, destination in copied_items:
                relative_path = str(destination.relative_to(app.root_path)).replace("\\", "/")
                cursor.execute("""
                    INSERT INTO order_items
                      (order_id, file_name, file_path, copies, color, double_sided,
                       page_range, cost, shop_id, orientation, print_side, paper_type,
                       additional_requirements, total_pages, blank_pages,
                       blurry_pages, unrecognizable_pages, uploaded_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """, (
                    new_order_id,
                    source.get("file_name") or "document",
                    relative_path,
                    old["copies"], old["color"],
                    1 if old["print_side"] == "double" else 0,
                    old["page_range"], old["estimated_cost"], old["shop_id"],
                    old["orientation"], old["print_side"], old["paper_type"],
                    old["additional_requirements"], source.get("total_pages") or 0,
                    source.get("blank_pages") or 0, source.get("blurry_pages") or 0,
                    source.get("unrecognizable_pages") or 0, datetime.now(),
                ))

        except Exception:
            for _, destination in copied_items:
                try:
                    destination.unlink(missing_ok=True)
                except OSError:
                    pass
            raise

        # Reorder creates a complete new order, so downstream payment/admin
        # screens receive the same supporting rows as a freshly placed order.
        try:
            cursor.execute("""
                INSERT INTO print_settings
                  (order_id, copies, color, double_sided, page_range, cost)
                VALUES (%s,%s,%s,%s,%s,%s)
            """, (
                new_order_id, old["copies"], old["color"],
                1 if old["print_side"] == "double" else 0,
                old["page_range"], old["estimated_cost"]
            ))
        except Exception as e:
            app.logger.warning("Could not create print_settings for reorder %s: %s", new_order_id, e)

        try:
            cursor.execute("""
                INSERT INTO payments
                  (order_id, amount, payment_status, payment_method, razorpay_order_id)
                VALUES (%s,%s,'pending','unselected',NULL)
            """, (new_order_id, old["estimated_cost"]))
        except Exception as e:
            app.logger.warning("Could not create payment row for reorder %s: %s", new_order_id, e)

        try:
            cursor.execute("""
                INSERT INTO document_analysis
                  (order_id, blank_pages, duplicate_pages, invisible_text,
                   total_pages, blurry_pages, unrecognizable_pages, analysis_status)
                SELECT %s, COALESCE(SUM(blank_pages),0), 0, 0,
                       COALESCE(SUM(total_pages),0), COALESCE(SUM(blurry_pages),0),
                       COALESCE(SUM(unrecognizable_pages),0), 'completed'
                FROM order_items WHERE order_id=%s
            """, (new_order_id, new_order_id))
        except Exception as e:
            app.logger.warning("Could not create document_analysis for reorder %s: %s", new_order_id, e)

        cursor.execute("""
            INSERT INTO notifications
              (user_id, order_id, message, notification_type)
            VALUES (%s,%s,%s,'order')
        """, (
            session["user_id"],
            new_order_id,
            f"Your print order #{new_order_id} has been placed again successfully."
        ))

        conn.commit()

        # Reorder is a brand-new unpaid order. Send the student into the same
        # payment flow used by a freshly placed order.
        session["qfp_pending_payment_order_id"] = int(new_order_id)

        return {
            "success": True,
            "old_order_id": order_id,
            "new_order_id": new_order_id,
            "payment_required": True
        }

    except Exception as e:
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass

        # If copying succeeded but the database transaction failed, remove
        # every newly-created file so a failed reorder never leaves orphaned
        # documents in the uploads directory.
        try:
            for _, destination in locals().get("copied_items", []):
                try:
                    destination.unlink(missing_ok=True)
                except OSError:
                    pass
        except Exception:
            pass

        # Always return JSON so the frontend can show the real reason instead
        # of falling into its generic fetch/JSON parsing error.
        app.logger.exception("Reorder failed for order_id=%s", order_id)
        return jsonify({
            "success": False,
            "error": f"Reorder failed: {type(e).__name__}: {str(e)[:300]}"
        }), 500
    finally:
        close_db(conn, cursor)


# ============================================================
# ADMIN - ADD SHOP
# ============================================================

@app.route("/admin/add-shop", methods=["POST"])
def add_shop():

    # Only logged-in admin can add a shop.
    if "user_id" not in session or session.get("role") != "admin":
        return redirect(url_for("login"))

    # Get values from Admin form.
    shop_name = request.form.get("shop_name", "").strip()
    shop_number = request.form.get("shop_number", "").strip()

    # Validate empty fields.
    if not shop_name or not shop_number:

        flash(
            "Shop name and shop number are required.",
            "error"
        )

        return redirect(url_for("admin_dashboard"))

    conn = cursor = None

    try:

        conn = get_db_connection()
        cursor = conn.cursor()

        # ----------------------------------------------------
        # Check duplicate shop number
        # ----------------------------------------------------

        cursor.execute(
            """
            SELECT shop_id
            FROM print_shops
            WHERE shop_number = %s
            """,
            (shop_number,),
        )

        existing_shop = cursor.fetchone()

        if existing_shop:

            flash(
                "Shop number already exists.",
                "error"
            )

            return redirect(url_for("admin_dashboard"))

        # ----------------------------------------------------
        # Insert shop
        #
        # shop_id is NOT included because MySQL AUTO_INCREMENT
        # will generate the Shop ID automatically.
        # ----------------------------------------------------

        cursor.execute(
            """
            INSERT INTO print_shops
                (shop_name, shop_number, owner_id)
            VALUES
                (%s, %s, %s)
            """,
            (
                shop_name,
                shop_number,
                session["user_id"],
            ),
        )

        # Get the newly generated Shop ID.
        new_shop_id = cursor.lastrowid

        conn.commit()

        flash(
            f"Shop added successfully. Shop ID: {new_shop_id}",
            "success"
        )

    except Error as e:

        if conn:
            conn.rollback()

        flash(
            f"Failed to add shop: {e}",
            "error"
        )

    finally:
        close_db(conn, cursor)

    return redirect(url_for("admin_dashboard"))


# ============================================================
# ADMIN DASHBOARD
# ============================================================

@app.route("/admin/orders/pending", methods=["GET"])
def admin_pending_orders():
    """Return the logged-in admin shop's current pending orders as JSON.
    Used by the dashboard for live order updates without a page refresh.
    Edited orders are intentionally hidden until the edit payment flow reaches a selected/success state.
    """
    if "user_id" not in session or session.get("role") != "admin":
        return jsonify({"success": False, "error": "Admin login required."}), 401

    conn = cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT shop_id FROM admins WHERE admin_id=%s LIMIT 1", (session["user_id"],))
        admin = cursor.fetchone()
        if not admin or admin.get("shop_id") is None:
            return jsonify({"success": False, "error": "No shop is assigned to this admin."}), 403

        ensure_edit_tracking_schema(cursor)
        cursor.execute("""
            SELECT o.order_id, u.name AS customer, o.document, o.order_status,
                   o.created_at, o.last_edited_at, p.payment_status, p.payment_method
            FROM orders o
            JOIN users u ON u.user_id=o.user_id
            LEFT JOIN payments p ON p.order_id=o.order_id
            WHERE o.shop_id=%s
              AND o.order_status='pending'
              AND COALESCE(p.payment_method, 'unselected') <> 'unselected'
            ORDER BY o.created_at ASC
        """, (admin["shop_id"],))
        rows = cursor.fetchall()
        return jsonify({
            "success": True,
            "orders": [
                {
                    "order_id": int(r["order_id"]),
                    "customer": r.get("customer") or "Student",
                    "document": r.get("document") or "Document",
                    "order_status": r.get("order_status") or "pending"
                }
                for r in rows
            ]
        })
    except Error as e:
        return jsonify({"success": False, "error": f"Could not load pending orders: {e}"}), 500
    finally:
        close_db(conn, cursor)


@app.route("/admin-dashboard")
def admin_dashboard():

    if "user_id" not in session:
        return redirect(url_for("login"))

    if session.get("role") != "admin":
        return redirect(url_for("user_dashboard"))

    orders = []
    active_print_order = None

    shops = []

    stats = {
        "completed": 0,
        "cancelled": 0,
        "pages": 0,
        "revenue": Decimal("0.00"),
    }

    conn = cursor = None

    try:

        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # ----------------------------------------------------
        # Orders for the logged-in Admin's Shop ID only
        # ----------------------------------------------------

        cursor.execute(
            "SELECT shop_id FROM admins WHERE admin_id = %s LIMIT 1",
            (session["user_id"],),
        )
        admin_row = cursor.fetchone()
        admin_shop_id = admin_row["shop_id"] if admin_row else None

        if admin_shop_id is not None:
            cursor.execute(
                """
                SELECT
                    o.order_id,
                    u.name AS customer,
                    o.document,
                    o.copies,
                    o.order_status,
                    o.estimated_cost,
                    o.created_at,
                    o.shop_id,
                    s.shop_name,
                    s.shop_number
                FROM orders o
                JOIN users u
                    ON u.user_id = o.user_id
                LEFT JOIN print_shops s
                    ON s.shop_id = o.shop_id
                WHERE o.shop_id = %s AND o.order_status = 'pending'
                ORDER BY o.created_at ASC
                """,
                (admin_shop_id,),
            )
            orders = cursor.fetchall()

            # Restore the exact accepted/printing order that was open before a
            # browser refresh. The session is only a pointer; all document and
            # order data is reconstructed from the database.
            active_print_order_id = session.get("qfp_active_print_order_id")
            if active_print_order_id:
                cursor.execute("""
                    SELECT o.order_id, o.order_status, o.shop_id,
                           COALESCE(p.payment_status, 'pending') AS payment_status,
                           COALESCE(p.payment_method, 'unselected') AS payment_method,
                           COALESCE(p.amount, o.estimated_cost) AS payment_amount
                    FROM orders o
                    LEFT JOIN payments p ON p.order_id = o.order_id
                    WHERE o.order_id = %s AND o.shop_id = %s
                      AND o.order_status IN ('accepted','printing')
                    LIMIT 1
                """, (active_print_order_id, admin_shop_id))
                active = cursor.fetchone()
                if active:
                    cursor.execute("""
                        SELECT oi.item_id, oi.file_name, oi.copies, oi.color,
                               oi.orientation, oi.print_side, oi.page_range,
                               oi.paper_type, oi.additional_requirements,
                               oi.total_pages, oi.blank_pages, oi.blurry_pages,
                               oi.unrecognizable_pages
                        FROM order_items oi
                        WHERE oi.order_id = %s
                        ORDER BY oi.item_id
                    """, (active_print_order_id,))
                    active_items = cursor.fetchall()
                    preferences = []
                    for item in active_items:
                        preferences.append({
                            "item_id": int(item.get("item_id")),
                            "file_name": item.get("file_name") or "Document",
                            "copies": int(item.get("copies") or 1),
                            "color": item.get("color") or "black_white",
                            "orientation": item.get("orientation") or "portrait",
                            "print_side": item.get("print_side") or "single",
                            "page_range": item.get("page_range") or "all",
                            "paper_type": item.get("paper_type") or "printing",
                            "additional_requirements": item.get("additional_requirements") or "",
                            "total_pages": int(item.get("total_pages") or 0),
                            "blank_pages": int(item.get("blank_pages") or 0),
                            "blurry_pages": int(item.get("blurry_pages") or 0),
                            "unrecognizable_pages": int(item.get("unrecognizable_pages") or 0),
                            "document_url": url_for("open_order_document", order_id=int(active_print_order_id), item_id=int(item.get("item_id"))),
                        })
                    if preferences:
                        active_print_order = {
                            "order_id": int(active_print_order_id),
                            "document_url": preferences[0]["document_url"],
                            "delivery_pdf_url": url_for("admin_delivery_pdf", order_id=int(active_print_order_id)),
                            "preferences": preferences,
                            "payment": {
                                "method": active.get("payment_method") or "unselected",
                                "status": active.get("payment_status") or "pending",
                                "amount": f"₹{Decimal(str(active.get('payment_amount') or 0)):.2f}",
                            },
                        }
                else:
                    session.pop("qfp_active_print_order_id", None)
        else:
            orders = []

        # ----------------------------------------------------
        # Shops
        # ----------------------------------------------------

        cursor.execute(
            """
            SELECT
                shop_id,
                shop_name,
                shop_number,
                owner_id,
                created_at
            FROM print_shops
            ORDER BY shop_id ASC
            """
        )

        shops = cursor.fetchall()

        # ----------------------------------------------------
        # Today's Statistics - logged-in Admin's Shop only
        # ----------------------------------------------------
        # completed/cancelled = status changes recorded today in order_history.
        # pages/revenue = today's accepted orders only.
        # PDF page count comes from Gemini's saved total_pages value
        # in order_items. Copies are multiplied so every printed copy
        # contributes its actual number of pages.
        if admin_shop_id is not None:
            cursor.execute(
                """
                SELECT
                    (
                        SELECT COUNT(DISTINCT h.order_id)
                        FROM order_history h
                        JOIN orders oh ON oh.order_id = h.order_id
                        WHERE oh.shop_id = %s
                          AND h.new_status IN ('ready','delivered')
                          AND h.changed_at >= CURDATE()
                          AND h.changed_at < CURDATE() + INTERVAL 1 DAY
                    ) AS completed,

                    (
                        SELECT COUNT(DISTINCT h.order_id)
                        FROM order_history h
                        JOIN orders oh ON oh.order_id = h.order_id
                        WHERE oh.shop_id = %s
                          AND h.new_status = 'cancelled'
                          AND h.changed_at >= CURDATE()
                          AND h.changed_at < CURDATE() + INTERVAL 1 DAY
                    ) AS cancelled,

                    COALESCE(
                        SUM(
                            CASE
                                WHEN o.order_status IN ('ready','delivered')
                                     AND EXISTS (
                                         SELECT 1
                                         FROM order_history hp
                                         WHERE hp.order_id = o.order_id
                                           AND hp.new_status IN ('ready','delivered')
                                           AND hp.changed_at >= CURDATE()
                                           AND hp.changed_at < CURDATE() + INTERVAL 1 DAY
                                     )
                                THEN COALESCE(oi.total_pages, 0) * o.copies
                                ELSE 0
                            END
                        ),
                        0
                    ) AS pages,

                    COALESCE(
                        (
                            SELECT SUM(ao.estimated_cost)
                            FROM orders ao
                            WHERE ao.shop_id = %s
                              AND EXISTS (
                                  SELECT 1
                                  FROM order_history ah
                                  WHERE ah.order_id = ao.order_id
                                    AND ah.new_status = 'accepted'
                                    AND ah.changed_at >= CURDATE()
                                    AND ah.changed_at < CURDATE() + INTERVAL 1 DAY
                              )
                        ),
                        0
                    ) AS revenue

                FROM orders o
                LEFT JOIN order_items oi
                    ON oi.order_id = o.order_id
                WHERE o.shop_id = %s
                """,
                (admin_shop_id, admin_shop_id, admin_shop_id, admin_shop_id),
            )

            row = cursor.fetchone()
            if row:
                stats = row

    except Error:
        pass

    finally:
        close_db(conn, cursor)

    return render_template(
        "admin_dashboard.html",
        name=session["name"],
        orders=orders,
        shops=shops,
        stats=stats,
        active_print_order=active_print_order,
    )


# ============================================================
# ADMIN - MARK CASH PAYMENT AS RECEIVED
# ============================================================

@app.route("/admin/payment/cash-received/<int:order_id>", methods=["POST"])
def mark_cash_received(order_id):
    """Mark a Cash at Shop payment as paid after the shop receives cash."""
    if "user_id" not in session or session.get("role") != "admin":
        return jsonify({"success": False, "error": "Admin login required."}), 401

    conn = cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        ensure_payment_schema(cursor)

        cursor.execute(
            "SELECT shop_id FROM admins WHERE admin_id=%s LIMIT 1",
            (session["user_id"],),
        )
        admin = cursor.fetchone()
        if not admin or admin.get("shop_id") is None:
            return jsonify({"success": False, "error": "No shop is assigned to this admin."}), 403

        cursor.execute(
            """
            SELECT o.order_id, o.order_status, p.payment_id,
                   p.payment_status, p.payment_method, p.amount
            FROM orders o
            LEFT JOIN payments p ON p.order_id=o.order_id
            WHERE o.order_id=%s AND o.shop_id=%s
            ORDER BY p.payment_id DESC
            LIMIT 1
            """,
            (order_id, admin["shop_id"]),
        )
        row = cursor.fetchone()
        if not row:
            return jsonify({"success": False, "error": "Order not found for this shop."}), 404

        if not row.get("payment_id"):
            return jsonify({"success": False, "error": "Payment record not found for this order."}), 404

        if row.get("payment_method") != "cash":
            return jsonify({"success": False, "error": "This order is not a Cash at Shop payment."}), 400

        if row.get("payment_status") == "paid":
            return jsonify({
                "success": True,
                "order_id": order_id,
                "payment_status": "paid",
                "payment_method": "cash",
                "message": "Cash payment was already marked as received."
            })

        cursor.execute(
            """
            UPDATE payments
            SET payment_status='paid',
                amount=(SELECT estimated_cost FROM orders WHERE order_id=%s),
                paid_at=NOW()
            WHERE payment_id=%s
              AND payment_method='cash'
              AND payment_status <> 'paid'
            """,
            (order_id, row["payment_id"]),
        )

        if cursor.rowcount != 1:
            conn.rollback()
            return jsonify({"success": False, "error": "Cash payment could not be updated."}), 500

        conn.commit()

        return jsonify({
            "success": True,
            "order_id": order_id,
            "payment_status": "paid",
            "payment_method": "cash",
            "amount": str(row.get("amount") or "0"),
            "message": "Cash payment marked as received successfully."
        })

    except (Error, ValueError) as e:
        if conn:
            conn.rollback()
        return jsonify({"success": False, "error": f"Could not mark cash as received: {e}"}), 500
    finally:
        close_db(conn, cursor)


# ============================================================
# ADMIN - UPDATE ORDER STATUS
# ============================================================

@app.route(
    "/admin/order/<int:order_id>/<status>",
    methods=["POST"]
)
def update_order(order_id, status):

    if (
        "user_id" not in session
        or session.get("role") != "admin"
    ):
        return {"success": False, "error": "Admin login required."}, 401

    if status == "accept":
        status = "accepted"
    elif status == "decline":
        status = "declined"

    if status not in {"accepted", "printing", "ready", "completed", "cancelled", "declined"}:
        return {"success": False, "error": "Invalid order status."}, 400

    conn = cursor = None
    file_to_delete = None

    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        ensure_delivery_schema(cursor)

        # The admin can act only on orders belonging to the admin's own shop.
        cursor.execute(
            "SELECT shop_id FROM admins WHERE admin_id = %s LIMIT 1",
            (session["user_id"],),
        )
        admin = cursor.fetchone()
        if not admin or admin["shop_id"] is None:
            return {"success": False, "error": "No shop is assigned to this admin."}, 403

        cursor.execute(
            """
            SELECT order_id, user_id, order_status, document
            FROM orders
            WHERE order_id = %s AND shop_id = %s
            LIMIT 1
            """,
            (order_id, admin["shop_id"]),
        )
        order = cursor.fetchone()

        if not order:
            return {"success": False, "error": "Order not found for this shop."}, 404

        # Printing starts only after the online transaction is verified.
        cursor.execute("SELECT payment_status, payment_method, amount FROM payments WHERE order_id=%s LIMIT 1", (order_id,))
        payment = cursor.fetchone()
        if status == "accepted" and (not payment or (payment.get("payment_status") != "paid" and payment.get("payment_method") != "cash")):
            return {"success": False, "error": "Select Online UPI and complete payment, or choose Cash at Shop before the shop can accept this order."}, 400

        if status == "declined":
            # A declined order is kept in the orders table as CANCELLED so the
            # student's account summary can count it for that specific user.
            # The Recent Orders page already hides cancelled orders.
            old_status = order["order_status"]

            cursor.execute(
                """
                UPDATE orders
                SET order_status = 'cancelled'
                WHERE order_id = %s AND shop_id = %s
                """,
                (order_id, admin["shop_id"]),
            )

            if cursor.rowcount != 1:
                conn.rollback()
                return {"success": False, "error": "Order could not be cancelled."}, 500

            cursor.execute(
                """
                INSERT INTO order_history
                    (order_id, old_status, new_status)
                VALUES
                    (%s, %s, 'cancelled')
                """,
                (order_id, old_status),
            )

            # Notify only the student who placed this particular order.
            cursor.execute(
                """
                INSERT INTO notifications
                    (user_id, order_id, message, notification_type)
                SELECT user_id, order_id, %s, 'order'
                FROM orders
                WHERE order_id = %s AND shop_id = %s
                """,
                (
                    f"Your print order #{order_id} was declined by the shop.",
                    order_id,
                    admin["shop_id"],
                ),
            )

            conn.commit()
            if session.get("qfp_active_print_order_id") == int(order_id):
                session.pop("qfp_active_print_order_id", None)
            send_push_notification(order["user_id"], "Order Declined", f"Your order #ORD{order_id:03d} was declined by the shop.", order_id)

            return {
                "success": True,
                "order_id": order_id,
                "status": "cancelled",
                "deleted": False,
            }

        # Accept is intentionally limited to pending orders from this shop.
        if status == "accepted" and order["order_status"] != "pending":
            return {"success": False, "error": "Only pending orders can be accepted."}, 400

        old_status = order["order_status"]
        accepted_file_paths = []

        accepted_preferences = []
        if status == "accepted":
            cursor.execute(
                """
                SELECT
                    oi.item_id, oi.file_name, oi.copies, oi.color,
                    oi.orientation, oi.print_side, oi.page_range, oi.paper_type,
                    oi.additional_requirements, oi.total_pages, oi.blank_pages,
                    oi.blurry_pages, oi.unrecognizable_pages, oi.file_path
                FROM order_items oi
                WHERE oi.order_id = %s
                ORDER BY oi.item_id
                """,
                (order_id,),
            )
            accepted_items = cursor.fetchall()
            accepted_file_paths = [row["file_path"] for row in accepted_items if row.get("file_path")]
            accepted_preferences = [
                {
                    "item_id": int(row.get("item_id")),
                    "file_name": row.get("file_name") or "Document",
                    "copies": int(row.get("copies") or 1),
                    "color": row.get("color") or "black_white",
                    "orientation": row.get("orientation") or "portrait",
                    "print_side": row.get("print_side") or ("double" if row.get("double_sided") else "single"),
                    "page_range": row.get("page_range") or "all",
                    "paper_type": row.get("paper_type") or "printing",
                    "additional_requirements": row.get("additional_requirements") or "",
                    "total_pages": int(row.get("total_pages") or 0),
                    "blank_pages": int(row.get("blank_pages") or 0),
                    "blurry_pages": int(row.get("blurry_pages") or 0),
                    "unrecognizable_pages": int(row.get("unrecognizable_pages") or 0),
                    "document_url": url_for("open_order_document", order_id=order_id, item_id=row.get("item_id")),
                }
                for row in accepted_items
            ]

        delivery_pdf_rel = None
        if status == "accepted":
            # Create the QR cover + original-document packet before the order is
            # committed, so the admin can print the secure packet immediately.
            try:
                delivery_pdf_rel = prepare_delivery_packet(cursor, order_id)
            except Exception as exc:
                if conn:
                    conn.rollback()
                return {"success": False, "error": f"Could not prepare secure QR printout: {exc}"}, 500

        # The old "completed" button means printing is finished. From now on
        # that transition is represented as READY; QR verification changes it
        # from READY -> DELIVERED.
        target_status = "ready" if status == "completed" else status

        cursor.execute(
            """
            UPDATE orders
            SET order_status = %s
            WHERE order_id = %s AND shop_id = %s
            """,
            (target_status, order_id, admin["shop_id"]),
        )

        cursor.execute(
            """
            INSERT INTO order_history
                (order_id, old_status, new_status)
            VALUES
                (%s, %s, %s)
            """,
            (order_id, old_status, status),
        )

        if status == "accepted":
            cursor.execute(
                """
                INSERT INTO notifications (user_id, order_id, message, notification_type)
                SELECT user_id, order_id, %s, 'order'
                FROM orders WHERE order_id=%s AND shop_id=%s
                """,
                (f"Your order #ORD{order_id:03d} has been accepted by the shop.", order_id, admin["shop_id"])
            )

        conn.commit()

        if status == "accepted":
            if not accepted_file_paths:
                return {"success": False, "error": "Order accepted, but no document files were found."}, 404

            missing = [p for p in accepted_file_paths if not (Path(app.root_path) / p).is_file()]
            if missing:
                return {"success": False, "error": "Order accepted, but one or more document files were not found."}, 404

            # Persist the admin's active printing workspace so a refresh can
            # reconstruct the same document popup instead of losing the order.
            session["qfp_active_print_order_id"] = int(order_id)
            send_push_notification(order["user_id"], "Order Accepted", f"Your order #ORD{order_id:03d} has been accepted by the shop.", order_id)
            return {
                "success": True,
                "order_id": order_id,
                "status": "accepted",
                "document_url": accepted_preferences[0].get("document_url") if accepted_preferences else "",
                "delivery_pdf_url": url_for("admin_delivery_pdf", order_id=order_id),
                "preferences": accepted_preferences,
                "payment": {
                    "method": payment.get("payment_method") if payment else "unselected",
                    "status": payment.get("payment_status") if payment else "pending",
                    "amount": f"₹{Decimal(str(payment.get('amount') or 0)):.2f}" if payment else "₹0.00"
                },
            }

        if status == "completed":
            # Pages are counted only when the admin confirms that printing is
            # actually finished. The order is now READY for QR-based collection. The page count comes from the mandatory
            # Gemini analysis saved for this exact order.
            cursor.execute(
                """
                SELECT COALESCE(da.total_pages, oi.total_pages, 0) AS total_pages,
                       o.copies,
                       o.estimated_cost
                FROM orders o
                LEFT JOIN document_analysis da ON da.order_id = o.order_id
                LEFT JOIN order_items oi ON oi.order_id = o.order_id
                WHERE o.order_id = %s AND o.shop_id = %s
                LIMIT 1
                """,
                (order_id, admin["shop_id"]),
            )
            print_data = cursor.fetchone()
            if not print_data or int(print_data.get("total_pages") or 0) <= 0:
                conn.rollback()
                return {"success": False, "error": "Gemini page analysis is missing for this order."}, 400

            pages_printed = int(print_data["total_pages"] or 0) * int(print_data["copies"] or 1)

            # Once the admin confirms printing is finished, mark the order
            # completed and notify only the student who placed this order.
            cursor.execute(
                """
                INSERT INTO notifications
                  (user_id, order_id, message, notification_type)
                SELECT user_id, order_id, %s, 'order'
                FROM orders
                WHERE order_id = %s AND shop_id = %s
                """,
                (f"Your print order #{order_id} is ready. Scan the QR code on the printout to collect it.",
                 order_id, admin["shop_id"]),
            )
            conn.commit()
            send_push_notification(order["user_id"], "Order Ready — Scan QR", f"Your order #ORD{order_id:03d} is ready. Scan the QR code on the printout to collect it.", order_id)
            return {
                "success": True,
                "order_id": order_id,
                "status": "ready",
                "pages_printed": pages_printed,
                "revenue_added": float(print_data.get("estimated_cost") or 0),
            }

        if status == "declined":
            return redirect(url_for("admin_dashboard"))

        return {"success": True, "order_id": order_id, "status": status}

    except Error as e:
        if conn:
            conn.rollback()
        return {"success": False, "error": f"Could not update order: {e}"}, 500

    finally:
        close_db(conn, cursor)



@app.route("/admin/order/<int:order_id>/delivery-pdf")
def admin_delivery_pdf(order_id):
    """Serve the generated QR-cover print packet only to that shop's admin."""
    if "user_id" not in session or session.get("role") != "admin":
        return redirect(url_for("login"))

    conn = cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        ensure_delivery_schema(cursor)
        cursor.execute("""
            SELECT o.delivery_pdf_path, a.shop_id
            FROM orders o
            JOIN admins a ON a.shop_id = o.shop_id
            WHERE o.order_id=%s AND a.admin_id=%s
            LIMIT 1
        """, (order_id, session["user_id"]))
        row = cursor.fetchone()
        if not row or not row.get("delivery_pdf_path"):
            return "Secure QR printout has not been generated yet.", 404
        file_path = Path(app.root_path) / row["delivery_pdf_path"]
        if not file_path.is_file():
            return "Secure QR printout file not found.", 404
        return send_file(file_path, mimetype="application/pdf", as_attachment=False,
                         download_name=f"QueueFree_Order_{order_id}_Printout.pdf")
    except Error as exc:
        return f"Could not open secure printout: {exc}", 500
    finally:
        close_db(conn, cursor)


@app.route("/delivery-scan")
def delivery_scan():
    if "user_id" not in session or session.get("role") != "user":
        return redirect(url_for("login"))
    return render_template("delivery_scan.html")


@app.route("/verify-delivery/<token>", methods=["GET"])
def verify_delivery(token):
    """Verify QR ownership against the currently logged-in student."""
    if "user_id" not in session or session.get("role") != "user":
        return redirect(url_for("login"))

    token = (token or "").strip()
    if not token or len(token) < 40:
        return render_template("delivery_scan.html", result={
            "success": False, "title": "Invalid QR Code",
            "message": "This QR code is not a valid QueueFree delivery code."
        })

    conn = cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        ensure_delivery_schema(cursor)
        cursor.execute("""
            SELECT o.order_id, o.user_id, o.order_status, o.shop_id,
                   s.shop_number, s.shop_name
            FROM orders o
            LEFT JOIN print_shops s ON s.shop_id=o.shop_id
            WHERE o.delivery_token=%s
            LIMIT 1
        """, (token,))
        order = cursor.fetchone()

        if not order:
            return render_template("delivery_scan.html", result={
                "success": False, "title": "Invalid QR Code",
                "message": "This QR code does not belong to a valid QueueFree order."
            })

        if int(order["user_id"]) != int(session["user_id"]):
            return render_template("delivery_scan.html", result={
                "success": False, "title": "Not Authorized",
                "message": "This printout belongs to another student. Order status was not changed."
            })

        if order["order_status"] == "delivered":
            return render_template("delivery_scan.html", result={
                "success": True, "already": True, "title": "Already Delivered",
                "message": f"Order ORD{order['order_id']:03d} has already been verified and delivered."
            })

        if order["order_status"] != "ready":
            return render_template("delivery_scan.html", result={
                "success": False, "title": "Not Ready",
                "message": f"Order ORD{order['order_id']:03d} is currently '{order['order_status']}'. Only READY orders can be delivered."
            })

        cursor.execute("""
            UPDATE orders
            SET order_status='delivered', delivered_at=CURRENT_TIMESTAMP
            WHERE order_id=%s AND user_id=%s AND delivery_token=%s AND order_status='ready'
        """, (order["order_id"], session["user_id"], token))
        if cursor.rowcount != 1:
            conn.rollback()
            return render_template("delivery_scan.html", result={
                "success": False, "title": "Verification Failed",
                "message": "The order could not be verified. Please scan again."
            })

        cursor.execute("""
            INSERT INTO order_history (order_id, old_status, new_status)
            VALUES (%s, 'ready', 'delivered')
        """, (order["order_id"],))
        cursor.execute("""
            INSERT INTO notifications (user_id, order_id, message, notification_type)
            VALUES (%s, %s, %s, 'order')
        """, (session["user_id"], order["order_id"],
              f"Order #{order['order_id']} delivered successfully."))
        conn.commit()

        return render_template("delivery_scan.html", result={
            "success": True, "title": "OK / Verified",
            "message": f"Order ORD{order['order_id']:03d} verified successfully. Status changed to DELIVERED.",
            "order_id": order["order_id"],
            "shop_number": order.get("shop_number") or str(order["shop_id"])
        })
    except Error as exc:
        if conn:
            conn.rollback()
        return render_template("delivery_scan.html", result={
            "success": False, "title": "Verification Error",
            "message": f"Could not verify the QR code: {exc}"
        }), 500
    finally:
        close_db(conn, cursor)


def escape_html(value):
    """Escape text before placing it in the inline preview error page."""
    import html
    return html.escape(str(value or ""))


# ============================================================
# ADMIN - OPEN ORDER DOCUMENT
# ============================================================

# ============================================================
# ADMIN - OPEN A SINGLE ORDER DOCUMENT
# ============================================================

@app.route("/admin/order/<int:order_id>/document")
def open_order_document(order_id):
    """Open exactly one selected order document; never merge order files."""

    if (
        "user_id" not in session
        or session.get("role") != "admin"
    ):
        return redirect(url_for("login"))

    item_id = request.args.get("item_id", type=int)
    conn = cursor = None

    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        cursor.execute(
            "SELECT shop_id FROM admins WHERE admin_id = %s LIMIT 1",
            (session["user_id"],),
        )
        admin = cursor.fetchone()

        if not admin or admin["shop_id"] is None:
            return "No shop is assigned to this admin.", 403

        query = """
            SELECT oi.item_id, oi.file_path, oi.file_name
            FROM order_items oi
            JOIN orders o ON o.order_id = oi.order_id
            WHERE oi.order_id = %s AND o.shop_id = %s
        """
        params = [order_id, admin["shop_id"]]

        if item_id is not None:
            query += " AND oi.item_id = %s"
            params.append(item_id)

        query += " ORDER BY oi.item_id LIMIT 1"

        cursor.execute(query, tuple(params))
        item = cursor.fetchone()

        if not item:
            return "Selected document not found.", 404

        file_path = Path(app.root_path) / item["file_path"]
        if not file_path.is_file():
            return f"Document file not found: {item['file_name']}", 404

        # Display the selected document inside the existing popup instead of
        # triggering a browser download. Browsers can render PDFs/images inline,
        # but generally download Office documents such as PPTX/DOCX/XLSX.
        # Convert those display-incompatible formats to a temporary PDF only
        # for preview; the original document is never changed.
        mime_type = mimetypes.guess_type(item["file_name"] or str(file_path))[0] or "application/octet-stream"
        browser_inline_mimes = {
            "application/pdf",
            "image/png", "image/jpeg", "image/gif", "image/webp", "image/svg+xml",
            "text/plain", "text/html", "text/csv",
        }
        preview_path = file_path
        preview_mime = mime_type
        cleanup_dir = None

        if mime_type not in browser_inline_mimes:
            # Office/LibreOffice documents are not reliably rendered by browsers.
            # Convert a temporary copy to PDF and serve that PDF inline in the
            # existing iframe. Never fall back to sending the original Office
            # file because that causes Chrome/Edge to download it.
            output_dir = Path(tempfile.mkdtemp(prefix="queuefree_preview_"))
            try:
                soffice_candidates = [
                    shutil.which("soffice"),
                    shutil.which("soffice.exe"),
                    shutil.which("libreoffice"),
                    shutil.which("libreoffice.exe"),
                    r"C:\Program Files\LibreOffice\program\soffice.exe",
                    r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
                ]
                soffice = next((x for x in soffice_candidates if x and Path(x).exists()), None)
                if not soffice:
                    raise RuntimeError("LibreOffice/soffice was not found for document preview.")

                # Use an isolated temporary user profile so an already-running
                # LibreOffice process cannot lock or interfere with conversion.
                lo_profile = output_dir / "lo_profile"
                lo_profile.mkdir(parents=True, exist_ok=True)
                subprocess.run(
                    [
                        soffice,
                        f"-env:UserInstallation={lo_profile.as_uri()}",
                        "--headless",
                        "--convert-to", "pdf",
                        "--outdir", str(output_dir),
                        str(file_path),
                    ],
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=180,
                    text=True,
                )
                converted = output_dir / f"{file_path.stem}.pdf"
                if not converted.exists():
                    pdfs = [x for x in output_dir.glob("*.pdf") if x.is_file()]
                    if pdfs:
                        converted = pdfs[0]
                if not converted.exists():
                    raise RuntimeError("The document could not be converted to a preview PDF.")

                preview_path = converted
                preview_mime = "application/pdf"
                cleanup_dir = output_dir
            except Exception as preview_error:
                # Do not send the original Office/binary document here: the
                # browser would download it. Return a readable inline error
                # instead, while leaving the original file untouched.
                shutil.rmtree(output_dir, ignore_errors=True)
                return (
                    f"<html><body style='font-family:Arial;padding:30px'>"
                    f"<h2>Preview unavailable</h2>"
                    f"<p>{escape_html(str(preview_error))}</p>"
                    f"</body></html>",
                    500,
                    {"Content-Type": "text/html; charset=utf-8", "Content-Disposition": "inline"},
                )

        response = send_file(
            preview_path,
            as_attachment=False,
            mimetype=preview_mime,
        )
        response.headers["Content-Disposition"] = "inline"
        response.headers["X-Content-Type-Options"] = "nosniff"

        if cleanup_dir:
            @response.call_on_close
            def _cleanup_preview():
                shutil.rmtree(cleanup_dir, ignore_errors=True)

        return response

    finally:
        close_db(conn, cursor)


# ============================================================
# ADMIN - ANALYTICS / SETTINGS PAGES
# ============================================================

@app.route("/analytics")
def analytics():
    if "user_id" not in session or session.get("role") != "admin":
        return redirect(url_for("login"))
    return render_template("analytics.html", name=session.get("name", "Admin"))


@app.route("/analytics/data")
def analytics_data():
    """Return real analytics for the logged-in admin's shop and selected month."""
    if "user_id" not in session or session.get("role") != "admin":
        return {"success": False, "error": "Admin login required."}, 401

    month = (request.args.get("month") or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}", month):
        month = __import__("datetime").datetime.now().strftime("%Y-%m")

    conn = cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        cursor.execute(
            "SELECT shop_id FROM admins WHERE admin_id = %s LIMIT 1",
            (session["user_id"],)
        )
        admin = cursor.fetchone()
        if not admin or admin["shop_id"] is None:
            return {"success": False, "error": "No shop is assigned to this admin."}, 403

        shop_id = admin["shop_id"]
        start_date = f"{month}-01"
        # MySQL calculates the first day of the next month for the exclusive end.
        cursor.execute(
            """
            SELECT COALESCE(COUNT(*), 0) AS total_orders
            FROM orders o
            WHERE o.shop_id = %s
              AND o.created_at >= %s
              AND o.created_at < DATE_ADD(%s, INTERVAL 1 MONTH)
            """,
            (shop_id, start_date, start_date)
        )
        summary = cursor.fetchone() or {}

        # Pages Printed belong to the day the shop actually completed the
        # printing, not merely the day the order was placed.
        cursor.execute(
            """
            SELECT COALESCE(SUM(
                (
                    SELECT COALESCE(SUM(COALESCE(oi.total_pages, 0) * COALESCE(oi.copies, 1)), 0)
                    FROM order_items oi
                    WHERE oi.order_id = h.order_id
                )
            ), 0) AS pages_printed
            FROM order_history h
            JOIN orders o ON o.order_id = h.order_id
            WHERE o.shop_id = %s
              AND h.new_status = 'completed'
              AND h.changed_at >= %s
              AND h.changed_at < DATE_ADD(%s, INTERVAL 1 MONTH)
            """,
            (shop_id, start_date, start_date)
        )
        page_summary = cursor.fetchone() or {}
        summary["pages_printed"] = page_summary.get("pages_printed", 0)

        # Revenue is the estimated cost of orders accepted by this shop.
        # It is based on the accepted event date, so it remains counted even
        # after the order later moves to printing/ready/completed.
        cursor.execute(
            """
            SELECT COALESCE(SUM(o.estimated_cost), 0) AS total_revenue
            FROM orders o
            JOIN order_history h ON h.order_id = o.order_id
            WHERE o.shop_id = %s
              AND h.new_status = 'accepted'
              AND h.changed_at >= %s
              AND h.changed_at < DATE_ADD(%s, INTERVAL 1 MONTH)
            """,
            (shop_id, start_date, start_date)
        )
        revenue_row = cursor.fetchone() or {}

        # One row per day. The frontend fills missing days with zero, so the
        # table always shows day 1 through the last day of the selected month.
        cursor.execute(
            """
            SELECT DATE(o.created_at) AS day, COUNT(*) AS orders
            FROM orders o
            WHERE o.shop_id = %s
              AND o.created_at >= %s
              AND o.created_at < DATE_ADD(%s, INTERVAL 1 MONTH)
            GROUP BY DATE(o.created_at)
            ORDER BY day
            """,
            (shop_id, start_date, start_date)
        )
        daily_rows = cursor.fetchall()

        # Pages are recorded against the actual completion date.
        cursor.execute(
            """
            SELECT
                DATE(h.changed_at) AS day,
                COALESCE(SUM(
                    (
                        SELECT COALESCE(SUM(COALESCE(oi.total_pages, 0) * COALESCE(oi.copies, 1)), 0)
                        FROM order_items oi
                        WHERE oi.order_id = h.order_id
                    )
                ), 0) AS pages
            FROM order_history h
            JOIN orders o ON o.order_id = h.order_id
            WHERE o.shop_id = %s
              AND h.new_status = 'completed'
              AND h.changed_at >= %s
              AND h.changed_at < DATE_ADD(%s, INTERVAL 1 MONTH)
            GROUP BY DATE(h.changed_at)
            ORDER BY day
            """,
            (shop_id, start_date, start_date)
        )
        page_rows = cursor.fetchall()

        # Daily revenue is grouped by the day each order was accepted.
        cursor.execute(
            """
            SELECT DATE(h.changed_at) AS day,
                   COALESCE(SUM(o.estimated_cost), 0) AS revenue
            FROM order_history h
            JOIN orders o ON o.order_id = h.order_id
            WHERE o.shop_id = %s
              AND h.new_status = 'accepted'
              AND h.changed_at >= %s
              AND h.changed_at < DATE_ADD(%s, INTERVAL 1 MONTH)
            GROUP BY DATE(h.changed_at)
            ORDER BY day
            """,
            (shop_id, start_date, start_date)
        )
        revenue_rows = cursor.fetchall()

        cursor.execute(
            """
            SELECT
                HOUR(o.created_at) AS hour_value,
                COUNT(*) AS orders
            FROM orders o
            WHERE o.shop_id = %s
              AND o.created_at >= %s
              AND o.created_at < DATE_ADD(%s, INTERVAL 1 MONTH)
            GROUP BY HOUR(o.created_at)
            ORDER BY hour_value
            """,
            (shop_id, start_date, start_date)
        )
        hourly_rows = cursor.fetchall()

        cursor.execute(
            """
            SELECT
                o.color,
                COUNT(*) AS orders
            FROM orders o
            WHERE o.shop_id = %s
              AND o.created_at >= %s
              AND o.created_at < DATE_ADD(%s, INTERVAL 1 MONTH)
              AND o.order_status <> 'cancelled'
            GROUP BY o.color
            """,
            (shop_id, start_date, start_date)
        )
        type_rows = cursor.fetchall()

        cursor.execute(
            """
            SELECT
                o.order_status,
                COUNT(*) AS orders
            FROM orders o
            WHERE o.shop_id = %s
              AND o.created_at >= %s
              AND o.created_at < DATE_ADD(%s, INTERVAL 1 MONTH)
            GROUP BY o.order_status
            """,
            (shop_id, start_date, start_date)
        )
        status_rows = cursor.fetchall()

        import calendar
        year, month_number = map(int, month.split("-"))
        days_in_month = calendar.monthrange(year, month_number)[1]

        daily = {}
        for row in daily_rows:
            day = row["day"].strftime("%Y-%m-%d")
            daily[day] = {
                "orders": int(row["orders"] or 0),
                "pages": 0,
                "revenue": 0.0
            }

        for row in page_rows:
            day = row["day"].strftime("%Y-%m-%d")
            daily.setdefault(day, {"orders": 0, "pages": 0, "revenue": 0.0})
            daily[day]["pages"] = int(row["pages"] or 0)

        for row in revenue_rows:
            day = row["day"].strftime("%Y-%m-%d")
            daily.setdefault(day, {"orders": 0, "pages": 0, "revenue": 0.0})
            daily[day]["revenue"] = float(row["revenue"] or 0)

        complete_daily = []
        for day_number in range(1, days_in_month + 1):
            day_obj = __import__("datetime").date(year, month_number, day_number)
            key = day_obj.strftime("%Y-%m-%d")
            complete_daily.append({
                "date": key,
                "day": day_obj.strftime("%a"),
                "orders": daily.get(key, {}).get("orders", 0),
                "revenue": daily.get(key, {}).get("revenue", 0.0),
                "pages": daily.get(key, {}).get("pages", 0)
            })

        hourly = {int(r["hour_value"]): int(r["orders"] or 0) for r in hourly_rows}
        hour_ranges = [(9, 11), (11, 13), (13, 15), (15, 17), (17, 19)]
        peak_hours = []
        for start_hour, end_hour in hour_ranges:
            count = sum(hourly.get(h, 0) for h in range(start_hour, end_hour))
            peak_hours.append({
                "label": f"{start_hour % 12 or 12:02d} {'AM' if start_hour < 12 else 'PM'} - "
                         f"{end_hour % 12 or 12:02d} {'AM' if end_hour < 12 else 'PM'}",
                "orders": count
            })

        max_peak = max((x["orders"] for x in peak_hours), default=0)
        for item in peak_hours:
            item["percentage"] = round((item["orders"] / max_peak) * 100) if max_peak else 0

        types = {"black_white": 0, "color": 0}
        for row in type_rows:
            if row["color"] in types:
                types[row["color"]] = int(row["orders"] or 0)
        type_total = types["black_white"] + types["color"]
        type_percent = {
            "black_white": round(types["black_white"] * 100 / type_total) if type_total else 0,
            "color": round(types["color"] * 100 / type_total) if type_total else 0
        }

        statuses = {"completed": 0, "cancelled": 0}
        for row in status_rows:
            if row["order_status"] in statuses:
                statuses[row["order_status"]] = int(row["orders"] or 0)

        # Current calendar week (Monday through Sunday) for the Printing Activity chart.
        # This is intentionally independent of the month selected in the summary filter.
        from datetime import date, timedelta
        today = date.today()
        week_start = today - timedelta(days=today.weekday())
        week_end = week_start + timedelta(days=7)

        cursor.execute(
            """
            SELECT DATE(o.created_at) AS day, COUNT(*) AS orders
            FROM orders o
            WHERE o.shop_id = %s
              AND o.created_at >= %s
              AND o.created_at < %s
            GROUP BY DATE(o.created_at)
            ORDER BY day
            """,
            (shop_id, week_start, week_end)
        )
        week_order_rows = cursor.fetchall()

        cursor.execute(
            """
            SELECT DATE(h.changed_at) AS day,
                   COALESCE(SUM(o.estimated_cost), 0) AS revenue
            FROM order_history h
            JOIN orders o ON o.order_id = h.order_id
            WHERE o.shop_id = %s
              AND h.new_status = 'accepted'
              AND h.changed_at >= %s
              AND h.changed_at < %s
            GROUP BY DATE(h.changed_at)
            ORDER BY day
            """,
            (shop_id, week_start, week_end)
        )
        week_revenue_rows = cursor.fetchall()

        cursor.execute(
            """
            SELECT DATE(h.changed_at) AS day,
                   COALESCE(SUM((
                       SELECT COALESCE(SUM(COALESCE(oi.total_pages, 0) * COALESCE(oi.copies, 1)), 0)
                       FROM order_items oi
                       WHERE oi.order_id = h.order_id
                   )), 0) AS pages
            FROM order_history h
            JOIN orders o ON o.order_id = h.order_id
            WHERE o.shop_id = %s
              AND h.new_status = 'completed'
              AND h.changed_at >= %s
              AND h.changed_at < %s
            GROUP BY DATE(h.changed_at)
            ORDER BY day
            """,
            (shop_id, week_start, week_end)
        )
        week_page_rows = cursor.fetchall()

        week_map = {}
        for row in week_order_rows:
            key = row["day"].strftime("%Y-%m-%d")
            week_map[key] = {"orders": int(row["orders"] or 0), "revenue": 0.0, "pages": 0}
        for row in week_revenue_rows:
            key = row["day"].strftime("%Y-%m-%d")
            week_map.setdefault(key, {"orders": 0, "revenue": 0.0, "pages": 0})["revenue"] = float(row["revenue"] or 0)
        for row in week_page_rows:
            key = row["day"].strftime("%Y-%m-%d")
            week_map.setdefault(key, {"orders": 0, "revenue": 0.0, "pages": 0})["pages"] = int(row["pages"] or 0)

        current_week = []
        for offset in range(7):
            day_obj = week_start + timedelta(days=offset)
            key = day_obj.strftime("%Y-%m-%d")
            values = week_map.get(key, {"orders": 0, "revenue": 0.0, "pages": 0})
            current_week.append({
                "date": key,
                "day": day_obj.strftime("%a"),
                "orders": values["orders"],
                "revenue": values["revenue"],
                "pages": values["pages"]
            })

        return {
            "success": True,
            "month": month,
            "total_orders": int(summary.get("total_orders") or 0),
            "total_revenue": float(revenue_row.get("total_revenue") or 0),
            "pages_printed": int(summary.get("pages_printed") or 0),
            "daily": complete_daily,
            "current_week": current_week,
            "peak_hours": peak_hours,
            "peak_max": max_peak,
            "printing_type": type_percent,
            "order_status": statuses
        }

    except (Error, ValueError) as e:
        return {"success": False, "error": f"Could not load analytics: {e}"}, 500
    finally:
        close_db(conn, cursor)


@app.route("/api/maps-config", methods=["GET"])
def maps_config():
    if not session.get("user_id"):
        return {"success":False,"error":"Login required."},401
    return {"success":True,"api_key":GOOGLE_MAPS_API_KEY}


@app.route("/api/nearby-shops", methods=["GET"])
def nearby_shops():
    if session.get("role") != "user":
        return {"success": False, "error": "Student login required."}, 403
    try:
        lat=float(request.args.get("lat", ""))
        lng=float(request.args.get("lng", ""))
    except (TypeError, ValueError):
        return {"success": False, "error": "Valid location is required."}, 400
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return {"success": False, "error": "Invalid location."}, 400
    conn=cursor=None
    try:
        conn=get_db_connection(); cursor=conn.cursor(dictionary=True)
        ensure_shop_location_schema(cursor)
        cursor.execute("""
            SELECT s.shop_id, s.shop_name, s.shop_number, s.university_name, s.state, s.address,
                   s.latitude, s.longitude, COALESCE(s.is_open,1) AS is_open,
                   COALESCE(ROUND(AVG(sr.rating),1),0) AS rating, COUNT(sr.review_id) AS review_count,
                   GROUP_CONCAT(CASE WHEN sr.review_text IS NOT NULL AND sr.review_text <> '' THEN sr.review_text END
                                ORDER BY sr.created_at DESC SEPARATOR '||') AS reviews
            FROM print_shops s
            LEFT JOIN shop_reviews sr ON sr.shop_id=s.shop_id
            WHERE s.latitude IS NOT NULL AND s.longitude IS NOT NULL
            GROUP BY s.shop_id
        """)
        shops=[]
        from math import radians, sin, cos, asin, sqrt
        for r in cursor.fetchall():
            # MySQL DECIMAL values are returned as Decimal objects; convert
            # coordinates to float before doing distance math.
            shop_lat = float(r["latitude"])
            shop_lng = float(r["longitude"])
            dlat = shop_lat - lat
            dlng = shop_lng - lng
            a = sin(radians(dlat)/2)**2 + cos(radians(lat))*cos(radians(shop_lat)) * sin(radians(dlng)/2)**2
            distance = 6371.0088 * 2 * asin(min(1, sqrt(a)))
            if distance <= 1.0:
                r["distance_km"]=round(distance,2)
                r["rating"]=float(r["rating"] or 0)
                r["review_count"]=int(r["review_count"] or 0)
                r["reviews"]=[x for x in (r.get("reviews") or "").split("||") if x][:5]
                r["latitude"] = shop_lat; r["longitude"] = shop_lng
                shops.append(r)
        shops.sort(key=lambda x:x["distance_km"])
        return {"success":True,"shops":shops,"radius_km":1}
    except Error as e:
        return {"success":False,"error":f"Could not load nearby shops: {e}"},500
    finally:
        close_db(conn,cursor)


@app.route("/admin/shop-location", methods=["POST"])
def save_shop_location():
    if session.get("role") != "admin":
        return {"success":False,"error":"Admin login required."},403
    try:
        lat=float(request.form.get("latitude", "")); lng=float(request.form.get("longitude", ""))
    except (TypeError, ValueError):
        return {"success":False,"error":"Please pick a valid location on the map."},400
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return {"success":False,"error":"Invalid map location."},400
    conn=cursor=None
    try:
        conn=get_db_connection(); cursor=conn.cursor(dictionary=True)
        ensure_shop_location_schema(cursor)
        cursor.execute("SELECT shop_id FROM admins WHERE admin_id=%s LIMIT 1",(session["user_id"],))
        admin=cursor.fetchone()
        if not admin or not admin.get("shop_id"):
            return {"success":False,"error":"No shop is linked to this admin."},400
        cursor.execute("UPDATE print_shops SET latitude=%s, longitude=%s WHERE shop_id=%s",(lat,lng,admin["shop_id"]))
        conn.commit()
        return {"success":True,"message":"Shop location saved successfully.","latitude":lat,"longitude":lng}
    except Error as e:
        if conn: conn.rollback()
        return {"success":False,"error":f"Could not save shop location: {e}"},500
    finally:
        close_db(conn,cursor)


@app.route("/settings")
def settings():
    if "user_id" not in session or session.get("role") != "admin":
        return redirect(url_for("login"))

    conn = cursor = None
    is_open = True
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        ensure_payment_schema(cursor)
        ensure_gst_schema(cursor)
        ensure_shop_location_schema(cursor)

        payment_settings = None
        cursor.execute("""
            SELECT pgs.key_id, pgs.is_enabled, pgs.updated_at
            FROM payment_gateway_settings pgs
            JOIN admins a ON a.shop_id = pgs.shop_id
            WHERE a.admin_id=%s AND pgs.provider='razorpay' LIMIT 1
        """, (session["user_id"],))
        payment_settings = cursor.fetchone()

        pricing = {}
        cursor.execute(
            """
            SELECT p.print_type, p.min_pages, p.max_pages, p.price_per_page
            FROM print_prices p
            JOIN admins a ON a.shop_id = p.shop_id
            WHERE a.admin_id = %s
            ORDER BY p.print_type, p.min_pages
            """,
            (session["user_id"],),
        )
        for price_row in cursor.fetchall():
            print_type = price_row["print_type"]
            page_key = f"{price_row['min_pages']}-{price_row['max_pages']}"
            pricing.setdefault(print_type, {})[page_key] = price_row["price_per_page"]

        cursor.execute(
            """
            SELECT a.name, a.email, a.phone, a.shop_id,
                   s.shop_name, s.university_name, s.state, s.gst_number, s.address,
                   s.latitude, s.longitude, COALESCE(s.is_open, 1) AS is_open
            FROM admins a
            LEFT JOIN print_shops s ON s.shop_id = a.shop_id
            WHERE a.admin_id = %s
            LIMIT 1
            """,
            (session["user_id"],),
        )
        row = cursor.fetchone()
        if row is not None:
            is_open = bool(row.get("is_open", 1))
            profile = row
        else:
            profile = {
                "name": session.get("name", "Admin"),
                "email": "",
                "phone": "",
                "shop_id": "",
            }
    except Error:
        profile = {
            "name": session.get("name", "Admin"),
            "email": "",
            "phone": "",
            "shop_id": "",
        }
    finally:
        close_db(conn, cursor)

    return render_template(
        "settings.html",
        name=profile.get("name") or session.get("name", "Admin"),
        profile=profile,
        shop_is_open=is_open,
        pricing=pricing,
        payment_settings=payment_settings,
        google_maps_api_key=GOOGLE_MAPS_API_KEY,
    )


@app.route("/admin/change-password", methods=["POST"])
def change_admin_password():
    if "user_id" not in session or session.get("role") != "admin":
        return {"success": False, "error": "Admin login required."}, 401

    current_password = request.form.get("current_password", "")
    new_password = request.form.get("new_password", "")
    confirm_password = request.form.get("confirm_password", "")

    if not current_password or not new_password or not confirm_password:
        return {"success": False, "error": "Please fill all password fields."}, 400

    if not PASSWORD_PATTERN.fullmatch(new_password):
        return {
            "success": False,
            "error": "New password must be at least 8 characters and include A-Z, a-z, 0-9 and a symbol."
        }, 400

    if new_password != confirm_password:
        return {"success": False, "error": "New password and confirm password do not match."}, 400

    if current_password == new_password:
        return {"success": False, "error": "New password must be different from the current password."}, 400

    conn = cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # Check the current password in the logged-in admin's database record.
        cursor.execute(
            "SELECT password FROM admins WHERE admin_id = %s LIMIT 1",
            (session["user_id"],),
        )
        admin = cursor.fetchone()

        if not admin:
            return {"success": False, "error": "Admin account not found."}, 404

        if admin.get("password") != current_password:
            return {"success": False, "error": "Current password is incorrect."}, 400

        # Change the password only after all validation and the current
        # password check have succeeded.
        cursor.execute(
            "UPDATE admins SET password = %s WHERE admin_id = %s",
            (new_password, session["user_id"]),
        )
        conn.commit()

        return {"success": True, "message": "Password updated successfully."}

    except Error as e:
        if conn:
            conn.rollback()
        return {"success": False, "error": f"Password could not be updated: {e}"}, 500
    finally:
        close_db(conn, cursor)


@app.route("/admin/payment-settings", methods=["POST"])
def save_payment_settings():
    if "user_id" not in session or session.get("role") != "admin":
        return {"success": False, "error": "Admin login required."}, 401

    key_id = request.form.get("razorpay_key_id", "").strip()
    key_secret = request.form.get("razorpay_key_secret", "").strip()
    enabled = request.form.get("enabled", "1") == "1"
    if not key_id or not key_secret:
        return {"success": False, "error": "Razorpay Key ID and Key Secret are required."}, 400
    if not key_id.startswith(("rzp_test_", "rzp_live_")):
        return {"success": False, "error": "Enter a valid Razorpay Key ID (rzp_test_... or rzp_live_...)."}, 400

    conn = cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        ensure_payment_schema(cursor)
        cursor.execute("SELECT shop_id FROM admins WHERE admin_id=%s LIMIT 1", (session["user_id"],))
        admin = cursor.fetchone()
        if not admin or admin.get("shop_id") is None:
            return {"success": False, "error": "No shop is assigned to this admin."}, 403
        cursor.execute("""
            INSERT INTO payment_gateway_settings (shop_id, provider, key_id, key_secret, is_enabled)
            VALUES (%s,'razorpay',%s,%s,%s)
            ON DUPLICATE KEY UPDATE key_id=VALUES(key_id), key_secret=VALUES(key_secret), is_enabled=VALUES(is_enabled)
        """, (admin["shop_id"], key_id, key_secret, 1 if enabled else 0))
        conn.commit()
        return {"success": True, "message": "Razorpay payment settings saved successfully.", "key_id": key_id, "enabled": enabled}
    except Error as e:
        if conn: conn.rollback()
        return {"success": False, "error": f"Could not save payment settings: {e}"}, 500
    finally:
        close_db(conn, cursor)


@app.route("/admin/printing-prices", methods=["POST"])
def save_printing_prices():
    if "user_id" not in session or session.get("role") != "admin":
        return {"success": False, "error": "Admin login required."}, 401

    print_type = request.form.get("print_type", "").strip()
    allowed_ranges = {
        "A4 Printing": [(1, 1), (2, 5), (6, 10)],
        "Photo Print": [(1, 5)],
        "Sticker Print": [(1, 5)],
    }
    if print_type not in allowed_ranges:
        return {"success": False, "error": "Invalid printing type."}, 400

    prices = []
    try:
        for min_pages, max_pages in allowed_ranges[print_type]:
            field_name = f"price_{min_pages}_{max_pages}"
            raw_price = request.form.get(field_name, "").strip()
            if raw_price == "":
                return {"success": False, "error": "Please enter all prices before saving."}, 400
            price = Decimal(raw_price)
            if price < 0:
                raise ValueError
            prices.append((min_pages, max_pages, price))
    except (InvalidOperation, ValueError):
        return {"success": False, "error": "Please enter valid non-negative prices."}, 400

    conn = cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT shop_id FROM admins WHERE admin_id = %s LIMIT 1",
            (session["user_id"],),
        )
        admin = cursor.fetchone()
        if not admin or admin.get("shop_id") is None:
            return {"success": False, "error": "No shop is assigned to this admin."}, 403

        shop_id = admin["shop_id"]
        cursor.execute(
            "DELETE FROM print_prices WHERE shop_id = %s AND print_type = %s",
            (shop_id, print_type),
        )
        for min_pages, max_pages, price in prices:
            cursor.execute(
                """
                INSERT INTO print_prices
                    (shop_id, print_type, min_pages, max_pages, price_per_page)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (shop_id, print_type, min_pages, max_pages, price),
            )

        conn.commit()
        return {
            "success": True,
            "message": f"{print_type} prices saved successfully.",
        }
    except Error as e:
        if conn:
            conn.rollback()
        return {"success": False, "error": f"Could not save printing prices: {e}"}, 500
    finally:
        close_db(conn, cursor)


@app.route("/admin/profile/update", methods=["POST"])
def update_admin_profile():
    if "user_id" not in session or session.get("role") != "admin":
        return {"success": False, "error": "Admin login required."}, 401

    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip()
    phone = request.form.get("phone", "").strip()
    university_name = request.form.get("university_name", "").strip()
    state = request.form.get("state", "").strip()
    shop_name = request.form.get("shop_name", "").strip()
    address = request.form.get("address", "").strip()

    if not name:
        return {"success": False, "error": "Full name is required."}, 400
    if not email:
        return {"success": False, "error": "Email is required."}, 400
    if not university_name:
        return {"success": False, "error": "University / College Name is required."}, 400
    if not state:
        return {"success": False, "error": "State is required."}, 400
    if not shop_name:
        return {"success": False, "error": "Shop Name is required."}, 400

    conn = cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        cursor.execute(
            "SELECT admin_id FROM admins WHERE admin_id = %s LIMIT 1",
            (session["user_id"],),
        )
        admin = cursor.fetchone()
        if not admin:
            return {"success": False, "error": "Admin account not found."}, 404

        cursor.execute(
            "SELECT admin_id FROM admins WHERE email = %s AND admin_id <> %s LIMIT 1",
            (email, session["user_id"]),
        )
        if cursor.fetchone():
            return {"success": False, "error": "This email is already in use."}, 400

        cursor.execute(
            """
            UPDATE admins
            SET name = %s, email = %s, phone = %s
            WHERE admin_id = %s
            """,
            (name, email, phone or None, session["user_id"]),
        )

        cursor.execute(
            """
            UPDATE print_shops
            SET shop_name = %s, university_name = %s, state = %s, address = %s
            WHERE shop_id = (SELECT shop_id FROM admins WHERE admin_id = %s)
            """,
            (shop_name, university_name, state, address or None, session["user_id"]),
        )
        conn.commit()

        session["name"] = name
        return {
            "success": True,
            "name": name,
            "email": email,
            "phone": phone,
            "university_name": university_name,
            "state": state,
            "shop_name": shop_name,
            "address": address,
            "message": "Profile changes saved successfully.",
        }
    except Error as e:
        if conn:
            conn.rollback()
        return {"success": False, "error": f"Could not save profile changes: {e}"}, 500
    finally:
        close_db(conn, cursor)


@app.route("/admin/shop-status", methods=["POST"])
def update_shop_status():
    if "user_id" not in session or session.get("role") != "admin":
        return {"success": False, "error": "Admin login required."}, 401

    raw_status = request.form.get("is_open", "").strip().lower()
    if raw_status not in {"0", "1", "true", "false"}:
        return {"success": False, "error": "Invalid shop status."}, 400

    is_open = 1 if raw_status in {"1", "true"} else 0
    conn = cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT shop_id FROM admins WHERE admin_id = %s LIMIT 1",
            (session["user_id"],),
        )
        admin = cursor.fetchone()
        if not admin or admin.get("shop_id") is None:
            return {"success": False, "error": "No shop is assigned to this admin."}, 403

        cursor.execute(
            "UPDATE print_shops SET is_open = %s WHERE shop_id = %s",
            (is_open, admin["shop_id"]),
        )
        conn.commit()

        return {
            "success": True,
            "is_open": bool(is_open),
            "message": "Shop is now open." if is_open else "Shop is now closed.",
        }
    except Error as e:
        if conn:
            conn.rollback()
        return {"success": False, "error": f"Could not update shop status: {e}"}, 500
    finally:
        close_db(conn, cursor)


# ============================================================
# LOGOUT
# ============================================================

@app.route("/logout", methods=["GET", "POST"])
def logout():

    session.clear()

    response = redirect(url_for("home"))
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


# ============================================================
# RUN APPLICATION
# ============================================================

if __name__ == "__main__":
    # Ensure the retention timestamp exists and backfill legacy orders before serving.
    try:
        _conn = _cursor = None
        _conn = get_db_connection()
        _cursor = _conn.cursor(dictionary=True)
        ensure_order_item_columns(_cursor)
        backfill_uploaded_at(_cursor)
        _conn.commit()
    except Exception:
        if _conn:
            _conn.rollback()
    finally:
        close_db(_conn, _cursor)
    cleanup_expired_documents()
    app.run(debug=True)
