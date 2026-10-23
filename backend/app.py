"""
SurplusLink Backend — Flask REST API + Static Frontend Server
Run locally:
    pip install -r requirements.txt
    python app.py
Open http://localhost:5000 in your browser.
"""

import math
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import Flask, g, jsonify, request, send_from_directory
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from werkzeug.exceptions import NotFound
from werkzeug.security import check_password_hash, generate_password_hash

# ---------------------------------------------------------------------------
# Path & App Configuration
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FRONTEND_DIR = os.path.abspath(os.path.join(BASE_DIR, "..", "frontend"))
DB_PATH = os.environ.get("DATABASE_PATH", os.path.join(BASE_DIR, "surpluslink.db"))

APP_ENV = os.environ.get("APP_ENV", "production").strip().lower()
if APP_ENV == "development":
    SECRET_KEY = os.environ.get("SECRET_KEY") or "surpluslink-dev-secret-key-change-in-prod"
    ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD") or "admin123"
else:
    SECRET_KEY = os.environ.get("SECRET_KEY")
    ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")
    missing_secrets = [
        name
        for name, value in (("SECRET_KEY", SECRET_KEY), ("ADMIN_PASSWORD", ADMIN_PASSWORD))
        if not value
    ]
    if missing_secrets:
        raise RuntimeError(
            "Missing required environment variable(s): " + ", ".join(missing_secrets)
        )
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@surpluslink.org")
TOKEN_MAX_AGE_SECONDS = 7 * 24 * 3600  # 7 days

VALID_FOOD_TYPES = {"cooked", "packaged", "produce", "bakery", "dairy"}

app = Flask(__name__, static_folder=None)
serializer = URLSafeTimedSerializer(SECRET_KEY)
limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    default_limits=["120 per minute"],
    headers_enabled=True,
)


@limiter.request_filter
def skip_rate_limits_outside_api():
    return not request.path.startswith("/api/")


@app.errorhandler(429)
def handle_rate_limit_exceeded(error):
    return jsonify({"error": "Too many requests. Please try again later."}), 429


@app.after_request
def add_security_headers(response):
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = (
        "camera=(), microphone=(), geolocation=(self)"
    )
    response.headers["Content-Security-Policy"] = "; ".join(
        (
            "default-src 'self'",
            "base-uri 'self'",
            "object-src 'none'",
            "frame-ancestors 'none'",
            "script-src 'self' https://unpkg.com",
            "style-src 'self' https://unpkg.com https://fonts.googleapis.com 'unsafe-inline'",
            "font-src 'self' https://fonts.gstatic.com",
            "img-src 'self' https://unpkg.com https://*.tile.openstreetmap.org",
            "connect-src 'self'",
        )
    )
    return response


# ---------------------------------------------------------------------------
# Time & Geo Helpers
# ---------------------------------------------------------------------------
def utc_now() -> datetime:
    """Return current UTC datetime (timezone-aware)."""
    return datetime.now(timezone.utc)


def to_iso_utc(dt: datetime) -> str:
    """Format a datetime as an ISO-8601 UTC string ending in Z."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso_utc(value: str) -> datetime:
    """Parse an ISO-8601 datetime string into a timezone-aware UTC datetime."""
    if not value or not isinstance(value, str):
        raise ValueError("Invalid datetime string.")
    cleaned = value.strip()
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    dt = datetime.fromisoformat(cleaned)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Calculate great-circle distance between two coordinates in kilometers."""
    r = 6371.0  # Earth radius in km
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = (
        math.sin(dphi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    )
    return round(2 * r * math.atan2(math.sqrt(a), math.sqrt(1 - a)), 2)


# ---------------------------------------------------------------------------
# Database Connection & Schema Initialization
# ---------------------------------------------------------------------------
def get_db() -> sqlite3.Connection:
    """Get a per-request SQLite connection with row dictionary access."""
    if "db" not in g:
        conn = sqlite3.connect(DB_PATH, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        g.db = conn
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    """Close the SQLite connection at the end of every request."""
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    """Create tables if they do not exist and seed initial admin & sample data."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            phone TEXT NOT NULL,
            org TEXT DEFAULT '',
            role TEXT NOT NULL CHECK(role IN ('donor', 'receiver', 'admin')),
            password_hash TEXT NOT NULL,
            token_version INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
        """
    )

    cur.execute("PRAGMA table_info(users)")
    user_columns = {row["name"] for row in cur.fetchall()}
    if "token_version" not in user_columns:
        cur.execute(
            "ALTER TABLE users ADD COLUMN token_version INTEGER NOT NULL DEFAULT 0"
        )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS listings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            donor_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            description TEXT DEFAULT '',
            food_type TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            address TEXT NOT NULL,
            lat REAL NOT NULL,
            lng REAL NOT NULL,
            pickup_by TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'available'
                CHECK(status IN ('available', 'claimed', 'completed', 'expired', 'cancelled')),
            claimed_by INTEGER,
            created_at TEXT NOT NULL,
            claimed_at TEXT,
            completed_at TEXT,
            FOREIGN KEY (donor_id) REFERENCES users(id),
            FOREIGN KEY (claimed_by) REFERENCES users(id)
        )
        """
    )

    now_str = to_iso_utc(utc_now())

    # Ensure the configured admin account exists
    cur.execute(
        "SELECT id, password_hash FROM users WHERE email = ?", (ADMIN_EMAIL.lower(),)
    )
    admin_row = cur.fetchone()
    if not admin_row:
        cur.execute(
            """
            INSERT INTO users (name, email, phone, org, role, password_hash, created_at)
            VALUES (?, ?, ?, ?, 'admin', ?, ?)
            """,
            (
                "Platform Admin",
                ADMIN_EMAIL.lower(),
                "+1-555-0100",
                "SurplusLink HQ",
                generate_password_hash(ADMIN_PASSWORD),
                now_str,
            ),
        )
    elif not check_password_hash(admin_row["password_hash"], ADMIN_PASSWORD):
        cur.execute(
            "UPDATE users SET password_hash = ?, token_version = token_version + 1 "
            "WHERE id = ?",
            (generate_password_hash(ADMIN_PASSWORD), admin_row["id"]),
        )

    # Seed realistic demo donor, receiver, and sample listings on first run
    cur.execute("SELECT COUNT(*) AS cnt FROM listings")
    if cur.fetchone()["cnt"] == 0:
        cur.execute(
            """
            INSERT OR IGNORE INTO users (name, email, phone, org, role, password_hash, created_at)
            VALUES (?, ?, ?, ?, 'donor', ?, ?)
            """,
            (
                "Green Leaf Catering",
                "donor@surpluslink.org",
                "+1-555-0142",
                "Green Leaf Events & Kitchen",
                generate_password_hash("donor123"),
                now_str,
            ),
        )
        cur.execute(
            """
            INSERT OR IGNORE INTO users (name, email, phone, org, role, password_hash, created_at)
            VALUES (?, ?, ?, ?, 'receiver', ?, ?)
            """,
            (
                "Hope Community Pantry",
                "receiver@surpluslink.org",
                "+1-555-0188",
                "Hope Food Rescue NGO",
                generate_password_hash("receiver123"),
                now_str,
            ),
        )

        cur.execute("SELECT id FROM users WHERE email = 'donor@surpluslink.org'")
        donor_row = cur.fetchone()
        cur.execute("SELECT id FROM users WHERE email = 'receiver@surpluslink.org'")
        receiver_row = cur.fetchone()

        if donor_row and receiver_row:
            donor_id = donor_row["id"]
            receiver_id = receiver_row["id"]
            now = utc_now()

            sample_listings = [
                (
                    donor_id,
                    "Hot Vegetable Pulao & Dal Trays",
                    "Freshly prepared for a campus seminar that ended early. Packed in clean foil trays, ready to load.",
                    "cooked",
                    35,
                    "Student Union Dining Hall, Gate 2",
                    13.0674,
                    80.2376,
                    to_iso_utc(now + timedelta(hours=3, minutes=30)),
                    "available",
                    None,
                    to_iso_utc(now - timedelta(minutes=25)),
                    None,
                    None,
                ),
                (
                    donor_id,
                    "Artisan Sourdough Loaves & Muffins",
                    "Unsold morning bake in sealed paper bags. No refrigeration needed before pickup.",
                    "bakery",
                    20,
                    "24 Parkview Bakery Lane",
                    13.0475,
                    80.2508,
                    to_iso_utc(now + timedelta(hours=9)),
                    "available",
                    None,
                    to_iso_utc(now - timedelta(hours=1)),
                    None,
                    None,
                ),
                (
                    donor_id,
                    "Organic Farm Salad Greens & Tomatoes",
                    "Crates of washed spinach, lettuce, and cherry tomatoes stored in our walk-in cooler.",
                    "produce",
                    50,
                    "88 Market Square Cooperative",
                    13.0827,
                    80.2707,
                    to_iso_utc(now + timedelta(hours=18)),
                    "claimed",
                    receiver_id,
                    to_iso_utc(now - timedelta(hours=2)),
                    to_iso_utc(now - timedelta(minutes=40)),
                    None,
                ),
                (
                    donor_id,
                    "Packaged Whole Wheat Meal Boxes",
                    "Individually sealed vegetarian meal boxes from yesterday's conference.",
                    "packaged",
                    40,
                    "Tech Park Cafeteria Block B",
                    13.0102,
                    80.2255,
                    to_iso_utc(now - timedelta(hours=4)),
                    "completed",
                    receiver_id,
                    to_iso_utc(now - timedelta(hours=10)),
                    to_iso_utc(now - timedelta(hours=8)),
                    to_iso_utc(now - timedelta(hours=6)),
                ),
            ]

            cur.executemany(
                """
                INSERT INTO listings (
                    donor_id, title, description, food_type, quantity,
                    address, lat, lng, pickup_by, status,
                    claimed_by, created_at, claimed_at, completed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                sample_listings,
            )

    conn.commit()
    conn.close()


def sweep_expired_listings(db: sqlite3.Connection):
    """Automatically mark unclaimed listings past their pickup deadline as expired."""
    now_str = to_iso_utc(utc_now())
    db.execute(
        """
        UPDATE listings
        SET status = 'expired'
        WHERE status = 'available' AND pickup_by <= ?
        """,
        (now_str,),
    )
    db.commit()


# ---------------------------------------------------------------------------
# Authentication Helpers & Decorators
# ---------------------------------------------------------------------------
def create_token(user_id: int, role: str, token_version: int) -> str:
    """Create a signed bearer token containing the user's current token version."""
    return serializer.dumps(
        {"user_id": user_id, "role": role, "token_version": token_version}
    )


def get_current_user():
    """Return the authenticated user row from the Authorization header, or None."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return None
    token = auth_header.split(" ", 1)[1].strip()
    if not token:
        return None
    try:
        payload = serializer.loads(token, max_age=TOKEN_MAX_AGE_SECONDS)
    except (SignatureExpired, BadSignature):
        return None

    db = get_db()
    user = db.execute(
        "SELECT id, name, email, phone, org, role, token_version, created_at "
        "FROM users WHERE id = ?",
        (payload.get("user_id"),),
    ).fetchone()
    if not user or payload.get("token_version") != user["token_version"]:
        return None
    user_dict = dict(user)
    user_dict.pop("token_version")
    return user_dict


def auth_required(f):
    """Decorator requiring a valid Bearer token."""
    @wraps(f)
    def decorated(*args, **kwargs):
        user = get_current_user()
        if not user:
            return jsonify({"error": "Authentication required. Please log in."}), 401
        g.current_user = user
        return f(*args, **kwargs)
    return decorated


def role_required(*allowed_roles):
    """Decorator requiring the logged-in user to have one of the allowed roles."""
    def wrapper(f):
        @wraps(f)
        @auth_required
        def decorated(*args, **kwargs):
            if g.current_user["role"] not in allowed_roles:
                roles_str = ", ".join(allowed_roles)
                return jsonify(
                    {"error": f"Access denied. This action requires role: {roles_str}."}
                ), 403
            return f(*args, **kwargs)
        return decorated
    return wrapper


# ---------------------------------------------------------------------------
# Serialization & Privacy Rules
# ---------------------------------------------------------------------------
def serialize_listing(row: sqlite3.Row, current_user: dict | None, user_coords=None) -> dict:
    """
    Convert a joined listing row to a dictionary.
    Enforces phone privacy: phone numbers are visible ONLY to the donor and receiver
    involved (plus admin), and ONLY after a listing is claimed.
    """
    item = {
        "id": row["id"],
        "donor_id": row["donor_id"],
        "donor_name": row["donor_name"],
        "donor_org": row["donor_org"] or "",
        "title": row["title"],
        "description": row["description"] or "",
        "food_type": row["food_type"],
        "quantity": row["quantity"],
        "address": row["address"],
        "lat": row["lat"],
        "lng": row["lng"],
        "pickup_by": row["pickup_by"],
        "status": row["status"],
        "claimed_by": row["claimed_by"],
        "receiver_name": row["receiver_name"],
        "receiver_org": row["receiver_org"],
        "created_at": row["created_at"],
        "claimed_at": row["claimed_at"],
        "completed_at": row["completed_at"],
        "donor_phone": None,
        "receiver_phone": None,
        "distance_km": None,
    }

    # Reveal phone numbers only after claim and only to the donor/receiver involved (or admin)
    if current_user and row["status"] in ("claimed", "completed"):
        is_donor = current_user["id"] == row["donor_id"]
        is_receiver = current_user["id"] == row["claimed_by"]
        is_admin = current_user["role"] == "admin"
        if is_donor or is_receiver or is_admin:
            item["donor_phone"] = row["donor_phone"]
            item["receiver_phone"] = row["receiver_phone"]

    # Calculate distance if caller supplied lat/lng
    if user_coords is not None:
        u_lat, u_lng = user_coords
        item["distance_km"] = haversine_km(u_lat, u_lng, row["lat"], row["lng"])

    return item


LISTING_SELECT_SQL = """
    SELECT
        l.*,
        d.name AS donor_name,
        d.org AS donor_org,
        d.phone AS donor_phone,
        r.name AS receiver_name,
        r.org AS receiver_org,
        r.phone AS receiver_phone
    FROM listings l
    JOIN users d ON l.donor_id = d.id
    LEFT JOIN users r ON l.claimed_by = r.id
"""


# ---------------------------------------------------------------------------
# Validation Helpers
# ---------------------------------------------------------------------------
EMAIL_REGEX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
COMMON_PASSWORDS = {
    "1234567890",
    "admin12345",
    "letmein123",
    "password1!",
    "password123",
    "qwerty12345",
}


def validate_listing_payload(data: dict):
    """Validate listing fields and enforce the 48-hour food safety deadline rule."""
    title = (data.get("title") or "").strip()
    description = (data.get("description") or "").strip()
    food_type = (data.get("food_type") or "").strip().lower()
    address = (data.get("address") or "").strip()
    pickup_by_raw = data.get("pickup_by")

    if len(title) < 3 or len(title) > 120:
        return None, "Title must be between 3 and 120 characters."
    if len(description) > 1000:
        return None, "Description must be 1000 characters or fewer."
    if food_type not in VALID_FOOD_TYPES:
        return None, f"Food type must be one of: {', '.join(sorted(VALID_FOOD_TYPES))}."
    if len(address) < 4 or len(address) > 200:
        return None, "Address must be between 4 and 200 characters."

    try:
        quantity = int(data.get("quantity"))
        if quantity < 1 or quantity > 5000:
            raise ValueError()
    except (TypeError, ValueError):
        return None, "Quantity must be a whole number of servings between 1 and 5000."

    try:
        lat = float(data.get("lat"))
        lng = float(data.get("lng"))
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            raise ValueError()
    except (TypeError, ValueError):
        return None, "Please pick a valid location on the map (latitude and longitude)."

    try:
        pickup_dt = parse_iso_utc(pickup_by_raw)
    except ValueError:
        return None, "Please provide a valid pickup deadline date and time."

    now = utc_now()
    if pickup_dt <= now:
        return None, "Pickup deadline must be in the future."
    if pickup_dt > now + timedelta(hours=48):
        return None, "For food safety, pickup deadline must be at most 48 hours from now."

    cleaned = {
        "title": title,
        "description": description,
        "food_type": food_type,
        "quantity": quantity,
        "address": address,
        "lat": lat,
        "lng": lng,
        "pickup_by": to_iso_utc(pickup_dt),
    }
    return cleaned, None


# ---------------------------------------------------------------------------
# Auth API Routes
# ---------------------------------------------------------------------------
@app.route("/api/register", methods=["POST"])
@limiter.limit("5 per hour")
def api_register():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip().lower()
    phone = (data.get("phone") or "").strip()
    org = (data.get("org") or "").strip()
    role = (data.get("role") or "").strip().lower()
    password = data.get("password") or ""

    if len(name) < 2:
        return jsonify({"error": "Please enter your full name (at least 2 characters)."}), 400
    if not EMAIL_REGEX.match(email):
        return jsonify({"error": "Please enter a valid email address."}), 400
    if len(phone) < 7 or len(phone) > 25:
        return jsonify({"error": "Please enter a valid contact phone number."}), 400
    if role not in ("donor", "receiver"):
        return jsonify({"error": "Please choose either 'donor' or 'receiver' as your role."}), 400
    if len(password) < 10:
        return jsonify({"error": "Password must be at least 10 characters long."}), 400
    if password.casefold() in COMMON_PASSWORDS:
        return jsonify({"error": "Please choose a less common password."}), 400

    db = get_db()
    existing = db.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
    if existing:
        return jsonify({"error": "An account with this email already exists."}), 409

    now_str = to_iso_utc(utc_now())
    cur = db.execute(
        """
        INSERT INTO users (name, email, phone, org, role, password_hash, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (name, email, phone, org, role, generate_password_hash(password), now_str),
    )
    db.commit()
    user_id = cur.lastrowid

    user = {
        "id": user_id,
        "name": name,
        "email": email,
        "phone": phone,
        "org": org,
        "role": role,
        "created_at": now_str,
    }
    token = create_token(user_id, role, 0)
    return jsonify({"token": token, "user": user}), 201


@app.route("/api/login", methods=["POST"])
@limiter.limit("10 per minute")
def api_login():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    if not email or not password:
        return jsonify({"error": "Please enter both email and password."}), 400

    db = get_db()
    row = db.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    if not row or not check_password_hash(row["password_hash"], password):
        return jsonify({"error": "Invalid email or password."}), 401

    user = {
        "id": row["id"],
        "name": row["name"],
        "email": row["email"],
        "phone": row["phone"],
        "org": row["org"],
        "role": row["role"],
        "created_at": row["created_at"],
    }
    token = create_token(row["id"], row["role"], row["token_version"])
    return jsonify({"token": token, "user": user})


@app.route("/api/me", methods=["GET"])
@auth_required
def api_me():
    return jsonify({"user": g.current_user})


# ---------------------------------------------------------------------------
# Listings API Routes
# ---------------------------------------------------------------------------
@app.route("/api/listings", methods=["GET"])
def api_get_listings():
    db = get_db()
    sweep_expired_listings(db)
    current_user = get_current_user()

    status_param = (request.args.get("status") or "").strip().lower()
    food_type = (request.args.get("food_type") or "").strip().lower()
    search_q = (request.args.get("q") or "").strip().lower()

    # Parse optional coordinates for distance calculation and sorting
    user_coords = None
    lat_arg = request.args.get("lat")
    lng_arg = request.args.get("lng")
    if lat_arg is not None and lng_arg is not None and lat_arg != "" and lng_arg != "":
        try:
            user_coords = (float(lat_arg), float(lng_arg))
        except ValueError:
            return jsonify({"error": "Invalid lat/lng query parameters."}), 400

    where_clauses = []
    params = []

    if status_param == "all":
        # Only admin may request status=all across the platform
        if not current_user or current_user["role"] != "admin":
            return jsonify({"error": "Admin access required for status=all."}), 403
    elif status_param in ("available", "claimed", "completed", "expired", "cancelled"):
        where_clauses.append("l.status = ?")
        params.append(status_param)
    else:
        # Default live board view shows available and claimed active listings
        where_clauses.append("l.status IN ('available', 'claimed')")

    if food_type and food_type in VALID_FOOD_TYPES:
        where_clauses.append("l.food_type = ?")
        params.append(food_type)

    if search_q:
        where_clauses.append(
            "(LOWER(l.title) LIKE ? OR LOWER(l.description) LIKE ? OR LOWER(l.address) LIKE ?)"
        )
        like_val = f"%{search_q}%"
        params.extend([like_val, like_val, like_val])

    sql = LISTING_SELECT_SQL
    if where_clauses:
        sql += " WHERE " + " AND ".join(where_clauses)
    sql += " ORDER BY l.pickup_by ASC"

    rows = db.execute(sql, params).fetchall()
    listings = [serialize_listing(r, current_user, user_coords) for r in rows]

    # Sort by distance if lat/lng were provided
    if user_coords is not None:
        listings.sort(key=lambda item: (item["distance_km"] is None, item["distance_km"]))

    return jsonify({"listings": listings})


@app.route("/api/listings", methods=["POST"])
@role_required("donor", "admin")
def api_create_listing():
    data = request.get_json(silent=True) or {}
    cleaned, err = validate_listing_payload(data)
    if err:
        return jsonify({"error": err}), 400

    db = get_db()
    now_str = to_iso_utc(utc_now())
    cur = db.execute(
        """
        INSERT INTO listings (
            donor_id, title, description, food_type, quantity,
            address, lat, lng, pickup_by, status, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'available', ?)
        """,
        (
            g.current_user["id"],
            cleaned["title"],
            cleaned["description"],
            cleaned["food_type"],
            cleaned["quantity"],
            cleaned["address"],
            cleaned["lat"],
            cleaned["lng"],
            cleaned["pickup_by"],
            now_str,
        ),
    )
    db.commit()

    row = db.execute(
        LISTING_SELECT_SQL + " WHERE l.id = ?", (cur.lastrowid,)
    ).fetchone()
    return jsonify({"listing": serialize_listing(row, g.current_user)}), 201


@app.route("/api/listings", methods=["PUT"])
@app.route("/api/listings/<int:listing_id>", methods=["PUT"])
@role_required("donor", "admin")
def api_update_listing(listing_id=None):
    data = request.get_json(silent=True) or {}
    if listing_id is None:
        listing_id = data.get("id")
    if not listing_id:
        return jsonify({"error": "Listing ID is required."}), 400

    db = get_db()
    sweep_expired_listings(db)
    existing = db.execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone()
    if not existing:
        return jsonify({"error": "Listing not found."}), 404

    if g.current_user["role"] != "admin" and existing["donor_id"] != g.current_user["id"]:
        return jsonify({"error": "You can only edit your own listings."}), 403

    if existing["status"] != "available":
        return jsonify(
            {"error": f"Cannot edit a listing once its status is '{existing['status']}'."}
        ), 400

    cleaned, err = validate_listing_payload(data)
    if err:
        return jsonify({"error": err}), 400

    db.execute(
        """
        UPDATE listings
        SET title = ?, description = ?, food_type = ?, quantity = ?,
            address = ?, lat = ?, lng = ?, pickup_by = ?
        WHERE id = ?
        """,
        (
            cleaned["title"],
            cleaned["description"],
            cleaned["food_type"],
            cleaned["quantity"],
            cleaned["address"],
            cleaned["lat"],
            cleaned["lng"],
            cleaned["pickup_by"],
            listing_id,
        ),
    )
    db.commit()

    row = db.execute(LISTING_SELECT_SQL + " WHERE l.id = ?", (listing_id,)).fetchone()
    return jsonify({"listing": serialize_listing(row, g.current_user)})


@app.route("/api/listings", methods=["DELETE"])
@app.route("/api/listings/<int:listing_id>", methods=["DELETE"])
@role_required("donor", "admin")
def api_delete_listing(listing_id=None):
    if listing_id is None:
        data = request.get_json(silent=True) or {}
        listing_id = data.get("id") or request.args.get("id")
    if not listing_id:
        return jsonify({"error": "Listing ID is required."}), 400

    db = get_db()
    existing = db.execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone()
    if not existing:
        return jsonify({"error": "Listing not found."}), 404

    if g.current_user["role"] != "admin" and existing["donor_id"] != g.current_user["id"]:
        return jsonify({"error": "You can only delete your own listings."}), 403

    db.execute("DELETE FROM listings WHERE id = ?", (listing_id,))
    db.commit()
    return jsonify({"message": "Listing deleted successfully.", "id": int(listing_id)})


# ---------------------------------------------------------------------------
# Claim Lifecycle Routes (Claim, Release, Complete, Cancel)
# ---------------------------------------------------------------------------
@app.route("/api/listings/<int:listing_id>/claim", methods=["POST"])
@role_required("receiver")
def api_claim_listing(listing_id: int):
    db = get_db()
    sweep_expired_listings(db)

    now_str = to_iso_utc(utc_now())

    # Race-safe single guarded UPDATE so two receivers cannot claim simultaneously
    cur = db.execute(
        """
        UPDATE listings
        SET status = 'claimed', claimed_by = ?, claimed_at = ?
        WHERE id = ? AND status = 'available' AND pickup_by > ?
        """,
        (g.current_user["id"], now_str, listing_id, now_str),
    )
    db.commit()

    if cur.rowcount == 0:
        exists = db.execute("SELECT status FROM listings WHERE id = ?", (listing_id,)).fetchone()
        if not exists:
            return jsonify({"error": "Listing not found."}), 404
        return jsonify(
            {
                "error": (
                    f"Unable to claim: this listing is already '{exists['status']}' "
                    "or its pickup deadline has passed."
                )
            }
        ), 409

    row = db.execute(LISTING_SELECT_SQL + " WHERE l.id = ?", (listing_id,)).fetchone()
    return jsonify({"listing": serialize_listing(row, g.current_user)})


@app.route("/api/listings/<int:listing_id>/release", methods=["POST"])
@role_required("receiver", "admin")
def api_release_listing(listing_id: int):
    db = get_db()
    existing = db.execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone()
    if not existing:
        return jsonify({"error": "Listing not found."}), 404

    if existing["status"] != "claimed":
        return jsonify({"error": "Only claimed listings can be released."}), 400

    if g.current_user["role"] != "admin" and existing["claimed_by"] != g.current_user["id"]:
        return jsonify({"error": "You can only release a listing that you claimed."}), 403

    now_str = to_iso_utc(utc_now())
    new_status = "available" if existing["pickup_by"] > now_str else "expired"

    db.execute(
        """
        UPDATE listings
        SET status = ?, claimed_by = NULL, claimed_at = NULL
        WHERE id = ? AND status = 'claimed'
        """,
        (new_status, listing_id),
    )
    db.commit()

    row = db.execute(LISTING_SELECT_SQL + " WHERE l.id = ?", (listing_id,)).fetchone()
    return jsonify({"listing": serialize_listing(row, g.current_user)})


@app.route("/api/listings/<int:listing_id>/complete", methods=["POST"])
@role_required("receiver", "donor", "admin")
def api_complete_listing(listing_id: int):
    db = get_db()
    existing = db.execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone()
    if not existing:
        return jsonify({"error": "Listing not found."}), 404

    if existing["status"] != "claimed":
        return jsonify({"error": "Only a claimed listing can be marked as picked up."}), 400

    is_owner_receiver = existing["claimed_by"] == g.current_user["id"]
    is_owner_donor = existing["donor_id"] == g.current_user["id"]
    is_admin = g.current_user["role"] == "admin"

    if not (is_owner_receiver or is_owner_donor or is_admin):
        return jsonify({"error": "Only the assigned receiver or donor can mark this picked up."}), 403

    now_str = to_iso_utc(utc_now())
    db.execute(
        """
        UPDATE listings
        SET status = 'completed', completed_at = ?
        WHERE id = ? AND status = 'claimed'
        """,
        (now_str, listing_id),
    )
    db.commit()

    row = db.execute(LISTING_SELECT_SQL + " WHERE l.id = ?", (listing_id,)).fetchone()
    return jsonify({"listing": serialize_listing(row, g.current_user)})


@app.route("/api/listings/<int:listing_id>/cancel", methods=["POST"])
@role_required("donor", "admin")
def api_cancel_listing(listing_id: int):
    db = get_db()
    existing = db.execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone()
    if not existing:
        return jsonify({"error": "Listing not found."}), 404

    if g.current_user["role"] != "admin" and existing["donor_id"] != g.current_user["id"]:
        return jsonify({"error": "You can only cancel your own listings."}), 403

    if existing["status"] not in ("available", "claimed"):
        return jsonify(
            {"error": f"Cannot cancel a listing whose status is '{existing['status']}'."}
        ), 400

    db.execute(
        """
        UPDATE listings
        SET status = 'cancelled'
        WHERE id = ? AND status IN ('available', 'claimed')
        """,
        (listing_id,),
    )
    db.commit()

    row = db.execute(LISTING_SELECT_SQL + " WHERE l.id = ?", (listing_id,)).fetchone()
    return jsonify({"listing": serialize_listing(row, g.current_user)})


# ---------------------------------------------------------------------------
# User Activity, Stats & Admin Routes
# ---------------------------------------------------------------------------
@app.route("/api/my", methods=["GET"])
@auth_required
def api_my_activity():
    db = get_db()
    sweep_expired_listings(db)
    user = g.current_user

    if user["role"] == "donor":
        rows = db.execute(
            LISTING_SELECT_SQL + " WHERE l.donor_id = ? ORDER BY l.created_at DESC",
            (user["id"],),
        ).fetchall()
    elif user["role"] == "receiver":
        rows = db.execute(
            LISTING_SELECT_SQL + " WHERE l.claimed_by = ? ORDER BY l.claimed_at DESC",
            (user["id"],),
        ).fetchall()
    else:
        # Admin sees all listings they posted or claimed
        rows = db.execute(
            LISTING_SELECT_SQL
            + " WHERE l.donor_id = ? OR l.claimed_by = ? ORDER BY l.created_at DESC",
            (user["id"], user["id"]),
        ).fetchall()

    listings = [serialize_listing(r, user) for r in rows]
    return jsonify({"listings": listings})


@app.route("/api/stats", methods=["GET"])
def api_stats():
    db = get_db()
    sweep_expired_listings(db)

    meals_row = db.execute(
        "SELECT COALESCE(SUM(quantity), 0) AS total FROM listings WHERE status = 'completed'"
    ).fetchone()
    pickups_row = db.execute(
        "SELECT COUNT(*) AS cnt FROM listings WHERE status = 'completed'"
    ).fetchone()
    listed_row = db.execute(
        "SELECT COUNT(*) AS cnt FROM listings WHERE status = 'available'"
    ).fetchone()
    donors_row = db.execute(
        "SELECT COUNT(*) AS cnt FROM users WHERE role = 'donor'"
    ).fetchone()
    receivers_row = db.execute(
        "SELECT COUNT(*) AS cnt FROM users WHERE role = 'receiver'"
    ).fetchone()

    return jsonify(
        {
            "meals_shared": meals_row["total"],
            "pickups_completed": pickups_row["cnt"],
            "listed_now": listed_row["cnt"],
            "donors": donors_row["cnt"],
            "receivers": receivers_row["cnt"],
        }
    )


@app.route("/api/admin/users", methods=["GET"])
@role_required("admin")
def api_admin_users():
    db = get_db()
    rows = db.execute(
        """
        SELECT
            u.id, u.name, u.email, u.phone, u.org, u.role, u.created_at,
            (SELECT COUNT(*) FROM listings l WHERE l.donor_id = u.id) AS listings_posted,
            (SELECT COUNT(*) FROM listings l WHERE l.claimed_by = u.id) AS listings_claimed
        FROM users u
        ORDER BY u.created_at DESC
        """
    ).fetchall()
    return jsonify({"users": [dict(r) for r in rows]})


# ---------------------------------------------------------------------------
# Static Frontend Serving
# ---------------------------------------------------------------------------
@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def serve_frontend(path: str):
    if path.startswith("api/"):
        return jsonify({"error": "API endpoint not found."}), 404
    try:
        return send_from_directory(FRONTEND_DIR, path)
    except NotFound:
        return send_from_directory(FRONTEND_DIR, "index.html")


# Initialize database tables and seed data when module loads (works with both python app.py and gunicorn)
init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"SurplusLink running at http://localhost:{port}")
    if app.debug:
        print(f"Admin credentials: {ADMIN_EMAIL} / {ADMIN_PASSWORD}")
    app.run(
        host="0.0.0.0",
        port=port,
        debug=os.environ.get("FLASK_DEBUG", "0") == "1",
    )