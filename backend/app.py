"""ParkZen API: manager-confirmed YOLO parking updates and customer availability."""
from datetime import datetime, timezone, timedelta
from functools import wraps
import json
import os
from pathlib import Path
import secrets
import sqlite3
from io import BytesIO
from collections import defaultdict, deque
from werkzeug.security import check_password_hash, generate_password_hash
from flask import Flask, g, jsonify, request, send_file
from flask_cors import CORS
from ultralytics import YOLO
import qrcode

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("PARKZEN_DB", ROOT / "parkzen.sqlite3"))
UPLOAD_DIR = ROOT / "uploads"
MODEL_PATH = ROOT / "best.pt"
APP = Flask(__name__)
APP.config["MAX_CONTENT_LENGTH"] = 12 * 1024 * 1024
configured_origins = os.environ.get("PARKZEN_ORIGIN", "http://localhost:5173,http://127.0.0.1:5173")
CORS(APP, resources={r"/api/*": {"origins": [origin.strip() for origin in configured_origins.split(",") if origin.strip()]}})
model = None
request_log = defaultdict(deque)

DESTINATIONS = [
    ("mall-a", "Mall A", "mall", "HITEC City, Hyderabad, Telangana", 17.4495, 78.3915, "Mall A"),
    ("mall-b", "Mall B", "mall", "Kukatpally, Hyderabad, Telangana", 17.4849, 78.3870, "Mall B"),
    ("mall-c", "Mall C", "mall", "Gachibowli, Hyderabad, Telangana", 17.4401, 78.3489, "Mall C"),
    ("restaurant-a", "Restaurant A", "restaurant", "Madhapur, Hyderabad, Telangana", 17.4483, 78.3915, "Restaurant A"),
    ("restaurant-b", "Restaurant B", "restaurant", "Jubilee Hills, Hyderabad, Telangana", 17.4310, 78.4070, "Restaurant B"),
    ("restaurant-c", "Restaurant C", "restaurant", "Banjara Hills, Hyderabad, Telangana", 17.4156, 78.4347, "Restaurant C"),
]

def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, timeout=10)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys=ON")
        g.db.execute("PRAGMA busy_timeout=10000")
    return g.db

@APP.teardown_appcontext
def close_db(_error):
    connection = g.pop("db", None)
    if connection:
        connection.close()

def initialize():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT UNIQUE NOT NULL, mobile TEXT NOT NULL DEFAULT '', password TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'customer', created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS destinations(id TEXT PRIMARY KEY, name TEXT NOT NULL, type TEXT NOT NULL, address TEXT NOT NULL, latitude REAL NOT NULL, longitude REAL NOT NULL, parking_ref TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS availability(destination_id TEXT PRIMARY KEY REFERENCES destinations(id), total INTEGER NOT NULL, available INTEGER NOT NULL, occupied INTEGER NOT NULL, updated_at TEXT NOT NULL, manager_id INTEGER REFERENCES users(id));
        CREATE TABLE IF NOT EXISTS predictions(id INTEGER PRIMARY KEY, destination_id TEXT NOT NULL, manager_id INTEGER NOT NULL, image_path TEXT NOT NULL, total INTEGER NOT NULL, available INTEGER NOT NULL, occupied INTEGER NOT NULL, slots TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, confirmed_at TEXT);
        CREATE TABLE IF NOT EXISTS activity(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, destination_id TEXT NOT NULL, action TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS floors(id INTEGER PRIMARY KEY, destination_id TEXT NOT NULL REFERENCES destinations(id) ON DELETE CASCADE, name TEXT NOT NULL, capacity INTEGER NOT NULL DEFAULT 0, operating_open TEXT NOT NULL DEFAULT '00:00', operating_close TEXT NOT NULL DEFAULT '23:59', maintenance INTEGER NOT NULL DEFAULT 0, UNIQUE(destination_id,name));
        CREATE TABLE IF NOT EXISTS zones(id INTEGER PRIMARY KEY, floor_id INTEGER NOT NULL REFERENCES floors(id) ON DELETE CASCADE, name TEXT NOT NULL, UNIQUE(floor_id,name));
        CREATE TABLE IF NOT EXISTS slots(id INTEGER PRIMARY KEY, zone_id INTEGER NOT NULL REFERENCES zones(id) ON DELETE CASCADE, code TEXT NOT NULL UNIQUE, vehicle_type TEXT NOT NULL DEFAULT 'car', accessible INTEGER NOT NULL DEFAULT 0, ev_charging INTEGER NOT NULL DEFAULT 0, distance_meters INTEGER, status TEXT NOT NULL DEFAULT 'available');
        CREATE TABLE IF NOT EXISTS vehicles(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, plate TEXT NOT NULL, vehicle_type TEXT NOT NULL DEFAULT 'car', UNIQUE(user_id,plate));
        CREATE TABLE IF NOT EXISTS reservations(id INTEGER PRIMARY KEY, booking_ref TEXT NOT NULL UNIQUE, user_id INTEGER NOT NULL REFERENCES users(id), slot_id INTEGER NOT NULL REFERENCES slots(id), vehicle_id INTEGER REFERENCES vehicles(id), start_at TEXT NOT NULL, end_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'confirmed', quoted_amount REAL NOT NULL DEFAULT 0, qr_nonce TEXT NOT NULL UNIQUE, checked_in_at TEXT, checked_out_at TEXT, created_at TEXT NOT NULL, cancelled_at TEXT);
        CREATE INDEX IF NOT EXISTS idx_reservations_slot_time ON reservations(slot_id,start_at,end_at,status);
        CREATE TABLE IF NOT EXISTS slot_holds(id INTEGER PRIMARY KEY, slot_id INTEGER NOT NULL REFERENCES slots(id) ON DELETE CASCADE, user_id INTEGER NOT NULL REFERENCES users(id), start_at TEXT NOT NULL, end_at TEXT NOT NULL, expires_at TEXT NOT NULL, hold_ref TEXT NOT NULL UNIQUE);
        CREATE INDEX IF NOT EXISTS idx_holds_slot_time ON slot_holds(slot_id,start_at,end_at,expires_at);
        CREATE TABLE IF NOT EXISTS membership_plans(id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, discount_percent REAL NOT NULL DEFAULT 0, points_per_transaction INTEGER NOT NULL DEFAULT 0, duration_days INTEGER NOT NULL DEFAULT 365, priority_booking INTEGER NOT NULL DEFAULT 0, price REAL NOT NULL DEFAULT 0, benefits TEXT NOT NULL DEFAULT '', eligible_categories TEXT NOT NULL DEFAULT 'all');
        CREATE TABLE IF NOT EXISTS memberships(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE, plan_id INTEGER NOT NULL REFERENCES membership_plans(id), membership_number TEXT NOT NULL UNIQUE, points_balance INTEGER NOT NULL DEFAULT 0, valid_until TEXT NOT NULL, created_at TEXT NOT NULL, cancelled_at TEXT);
        CREATE TABLE IF NOT EXISTS loyalty_transactions(id INTEGER PRIMARY KEY, membership_id INTEGER NOT NULL REFERENCES memberships(id), kind TEXT NOT NULL, points INTEGER NOT NULL, reference TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS parking_passes(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, pass_type TEXT NOT NULL, price REAL NOT NULL, valid_from TEXT NOT NULL, valid_until TEXT NOT NULL, destination_id TEXT REFERENCES destinations(id), vehicle_id INTEGER REFERENCES vehicles(id), status TEXT NOT NULL DEFAULT 'active', usage_limit INTEGER, usage_count INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, cancelled_at TEXT);
        CREATE TABLE IF NOT EXISTS notifications(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, title TEXT NOT NULL, message TEXT NOT NULL, link TEXT, read_at TEXT, created_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(user_id,read_at,created_at);
        CREATE TABLE IF NOT EXISTS support_tickets(id INTEGER PRIMARY KEY, ticket_ref TEXT NOT NULL UNIQUE, user_id INTEGER NOT NULL REFERENCES users(id), category TEXT NOT NULL, kind TEXT NOT NULL, priority TEXT NOT NULL DEFAULT 'normal', subject TEXT NOT NULL, description TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open', assigned_to INTEGER REFERENCES users(id), created_at TEXT NOT NULL, updated_at TEXT NOT NULL, resolved_at TEXT);
        CREATE TABLE IF NOT EXISTS ticket_replies(id INTEGER PRIMARY KEY, ticket_id INTEGER NOT NULL REFERENCES support_tickets(id) ON DELETE CASCADE, user_id INTEGER NOT NULL REFERENCES users(id), message TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS audit_logs(id INTEGER PRIMARY KEY, user_id INTEGER REFERENCES users(id), action TEXT NOT NULL, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, details TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS system_policies(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS waitlist_entries(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, destination_id TEXT NOT NULL REFERENCES destinations(id), slot_id INTEGER REFERENCES slots(id), start_at TEXT NOT NULL, end_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'waiting', offer_expires_at TEXT, created_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_waitlist_lookup ON waitlist_entries(destination_id,start_at,end_at,status,created_at);
        CREATE TABLE IF NOT EXISTS payments(id INTEGER PRIMARY KEY, reservation_id INTEGER NOT NULL UNIQUE REFERENCES reservations(id), user_id INTEGER NOT NULL REFERENCES users(id), amount REAL NOT NULL, currency TEXT NOT NULL DEFAULT 'INR', status TEXT NOT NULL DEFAULT 'pending', provider TEXT NOT NULL DEFAULT 'test', transaction_ref TEXT UNIQUE, created_at TEXT NOT NULL, verified_at TEXT);
        CREATE TABLE IF NOT EXISTS refunds(id INTEGER PRIMARY KEY, payment_id INTEGER NOT NULL REFERENCES payments(id), amount REAL NOT NULL, status TEXT NOT NULL DEFAULT 'pending', reference TEXT UNIQUE, created_at TEXT NOT NULL);
        """)
        defaults = {
            "hourly_rate": "50",
            "hold_minutes": "10",
            "cancellation_hours": "2",
            "no_show_grace_minutes": "30",
            "max_advance_days": "30",
            "points_value": "0.1",
            "points_step": "10",
            "max_points_per_checkout": "1000",
        }
        for key, value in defaults.items():
            conn.execute("INSERT OR IGNORE INTO system_policies(key,value,updated_at) VALUES(?,?,?)", (key, value, now()))
        for row in DESTINATIONS:
            conn.execute("INSERT OR IGNORE INTO destinations VALUES(?,?,?,?,?,?,?)", row)
        if not conn.execute("SELECT 1 FROM users WHERE role='manager'").fetchone():
            conn.execute("INSERT INTO users(name,email,mobile,password,role,created_at) VALUES(?,?,?,?,?,?)", ("Parking Manager", "manager@parkzen.local", "", generate_password_hash(os.environ.get("PARKZEN_MANAGER_PASSWORD", "1234")), "manager", now()))
        if not conn.execute("SELECT 1 FROM users WHERE role='admin'").fetchone():
            conn.execute("INSERT INTO users(name,email,mobile,password,role,created_at) VALUES(?,?,?,?,?,?)", ("ParkZen Administrator", "admin@parkzen.local", "", generate_password_hash(os.environ.get("PARKZEN_ADMIN_PASSWORD", "1234")), "admin", now()))
        for destination_id, *_ in DESTINATIONS:
            conn.execute("INSERT OR IGNORE INTO floors(destination_id,name,capacity) VALUES(?,?,?)", (destination_id, "B1", 20))
            floor = conn.execute("SELECT id FROM floors WHERE destination_id=? AND name='B1'", (destination_id,)).fetchone()
            conn.execute("INSERT OR IGNORE INTO zones(floor_id,name) VALUES(?,?)", (floor[0], "A"))
            zone = conn.execute("SELECT id FROM zones WHERE floor_id=? AND name='A'", (floor[0],)).fetchone()
            for number in range(1, 21):
                conn.execute("INSERT OR IGNORE INTO slots(zone_id,code) VALUES(?,?)", (zone[0], f"B1-A{number:02d}"))
        plan_columns = {row[1] for row in conn.execute("PRAGMA table_info(membership_plans)").fetchall()}
        if "price" not in plan_columns:
            conn.execute("ALTER TABLE membership_plans ADD COLUMN price REAL NOT NULL DEFAULT 0")
        if "benefits" not in plan_columns:
            conn.execute("ALTER TABLE membership_plans ADD COLUMN benefits TEXT NOT NULL DEFAULT ''")
        if "eligible_categories" not in plan_columns:
            conn.execute("ALTER TABLE membership_plans ADD COLUMN eligible_categories TEXT NOT NULL DEFAULT 'all'")
        membership_columns = {row[1] for row in conn.execute("PRAGMA table_info(memberships)").fetchall()}
        if "cancelled_at" not in membership_columns:
            conn.execute("ALTER TABLE memberships ADD COLUMN cancelled_at TEXT")
        for name, discount, points, priority, price, benefits in (("Basic", 0, 1, 0, 0, "Entry tier; earn points"), ("Silver", 5, 2, 0, 499, "5% parking discount; faster rewards"), ("Gold", 10, 3, 1, 999, "10% parking discount; priority booking"), ("Platinum", 15, 5, 1, 1999, "15% parking discount; priority booking; premium rewards")):
            conn.execute("INSERT OR IGNORE INTO membership_plans(name,discount_percent,points_per_transaction,priority_booking,price,benefits) VALUES(?,?,?,?,?,?)", (name, discount, points, priority, price, benefits))
        slot_columns = {row[1] for row in conn.execute("PRAGMA table_info(slots)").fetchall()}
        if "ai_status" not in slot_columns:
            conn.execute("ALTER TABLE slots ADD COLUMN ai_status TEXT")

def load_model():
    global model
    if model is None:
        if not MODEL_PATH.exists():
            raise RuntimeError(f"ParkZen model file not found: {MODEL_PATH}")
        model = YOLO(str(MODEL_PATH))
    return model

def current_user():
    token = request.headers.get("Authorization", "").removeprefix("Bearer ")
    if not token:
        return None
    return db().execute("SELECT users.*,sessions.token FROM sessions JOIN users ON users.id=sessions.user_id WHERE sessions.token=?", (token,)).fetchone()

def require_role(*roles):
    def decorate(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            user = current_user()
            if not user:
                return jsonify(error="Please sign in to continue."), 401
            if roles and user["role"] not in roles:
                return jsonify(error="You do not have access to this page."), 403
            g.user = user
            return fn(*args, **kwargs)
        return wrapped
    return decorate

def user_json(user):
    return {"id": user["id"], "name": user["name"], "email": user["email"], "mobile": user["mobile"], "role": user["role"]}

def parse_iso(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None

def notify(user_id, title, message, link=None):
    db().execute("INSERT INTO notifications(user_id,title,message,link,created_at) VALUES(?,?,?,?,?)", (user_id, title, message, link, now()))

def audit(action, entity_type, entity_id, details=None):
    db().execute("INSERT INTO audit_logs(user_id,action,entity_type,entity_id,details,created_at) VALUES(?,?,?,?,?,?)", (getattr(g, "user", None)["id"] if getattr(g, "user", None) else None, action, entity_type, str(entity_id), json.dumps(details or {}), now()))

def policy(key, fallback):
    row = db().execute("SELECT value FROM system_policies WHERE key=?", (key,)).fetchone()
    try:
        return float(row["value"]) if row else fallback
    except (TypeError, ValueError):
        return fallback

def process_expirations():
    conn = db()
    current = now()
    expired_holds = conn.execute("SELECT * FROM slot_holds WHERE expires_at<=?", (current,)).fetchall()
    for hold in expired_holds:
        conn.execute("DELETE FROM slot_holds WHERE id=?", (hold["id"],))
        audit("hold_expired", "slot_hold", hold["id"])
    grace = timedelta(minutes=policy("no_show_grace_minutes", 30))
    candidates = conn.execute("SELECT * FROM reservations WHERE status='confirmed'").fetchall()
    for reservation in candidates:
        start = parse_iso(reservation["start_at"])
        if start and datetime.now(timezone.utc) > start + grace:
            conn.execute("UPDATE reservations SET status='no_show' WHERE id=?", (reservation["id"],))
            notify(reservation["user_id"], "Reservation released", f"Booking {reservation['booking_ref']} was released after the no-show grace period.", "/reservations")
            audit("reservation_no_show", "reservation", reservation["id"])
            offer_waitlist(reservation["slot_id"], reservation["start_at"], reservation["end_at"])
    conn.commit()
    return len(expired_holds)

def offer_waitlist(slot_id, start_at, end_at):
    conn = db()
    slot = conn.execute("""SELECT destinations.id AS destination_id FROM slots
        JOIN zones ON zones.id=slots.zone_id JOIN floors ON floors.id=zones.floor_id
        JOIN destinations ON destinations.id=floors.destination_id WHERE slots.id=?""", (slot_id,)).fetchone()
    if not slot:
        return
    candidate = conn.execute("""SELECT * FROM waitlist_entries WHERE destination_id=? AND status='waiting'
        AND start_at<? AND end_at>? ORDER BY created_at LIMIT 1""", (slot["destination_id"], end_at, start_at)).fetchone()
    if candidate:
        expires = (datetime.now(timezone.utc) + timedelta(minutes=policy("hold_minutes", 10))).isoformat()
        conn.execute("UPDATE waitlist_entries SET status='offered',slot_id=?,offer_expires_at=? WHERE id=?", (slot_id, expires, candidate["id"]))
        notify(candidate["user_id"], "Parking slot available", "A released slot is available for you to claim before the offer expires.", "/waitlist")
        audit("waitlist_offer_created", "waitlist", candidate["id"], {"slot_id": slot_id})
        conn.commit()

def expire_waitlist_offers():
    conn = db()
    conn.execute("UPDATE waitlist_entries SET status='expired' WHERE status='offered' AND offer_expires_at<=?", (now(),))
    conn.commit()

def rate_limited(key, limit=60, window=60):
    current = datetime.now(timezone.utc).timestamp()
    events = request_log[key]
    while events and current - events[0] > window:
        events.popleft()
    if len(events) >= limit:
        return True
    events.append(current)
    return False

@APP.before_request
def protect_api():
    if request.path.startswith("/api/") and rate_limited(request.remote_addr or "unknown"):
        return jsonify(error="Too many requests. Please retry shortly."), 429
    if request.path.startswith("/api/"):
        process_expirations()
        expire_waitlist_offers()

def reservation_json(row):
    item = dict(row)
    item["qr_reference"] = item.pop("qr_nonce")
    return item

def owned_reservation(reservation_id):
    row = db().execute("SELECT reservations.*,slots.code AS slot_code,destinations.name AS destination_name FROM reservations JOIN slots ON slots.id=reservations.slot_id JOIN zones ON zones.id=slots.zone_id JOIN floors ON floors.id=zones.floor_id JOIN destinations ON destinations.id=floors.destination_id WHERE reservations.id=?", (reservation_id,)).fetchone()
    if not row or (g.user["role"] == "customer" and row["user_id"] != g.user["id"]):
        return None
    return row

def destination_json(row):
    item = dict(row)
    availability = db().execute("SELECT total,available,occupied,updated_at FROM availability WHERE destination_id=?", (item["id"],)).fetchone()
    item["availability"] = dict(availability) if availability else None
    item["status"] = "No recent update" if not availability else ("Parking full" if availability["available"] == 0 else "Limited parking" if availability["available"] / max(1, availability["total"]) <= 0.2 else "Parking available")
    return item

@APP.get("/api/health")
def health():
    return jsonify(status="ok", model="configured" if MODEL_PATH.exists() else "missing")

@APP.post("/api/auth/register")
def register():
    data = request.get_json(silent=True) or {}
    name, email, mobile, password = (str(data.get(key, "")).strip() for key in ("name", "email", "mobile", "password"))
    if not name or "@" not in email or len(mobile) < 7 or len(password) < 8:
        return jsonify(error="Enter your name, a valid email, mobile number, and a password of at least 8 characters."), 400
    try:
        cur = db().execute("INSERT INTO users(name,email,mobile,password,role,created_at) VALUES(?,?,?,?,?,?)", (name, email.lower(), mobile, generate_password_hash(password), "customer", now()))
        db().commit()
        return jsonify(message="Account created. Please sign in."), 201
    except sqlite3.IntegrityError:
        return jsonify(error="An account with this email already exists."), 409

@APP.post("/api/auth/login")
def login():
    data = request.get_json(silent=True) or {}
    user = db().execute("SELECT * FROM users WHERE email=?", (str(data.get("email", "")).strip().lower(),)).fetchone()
    if not user or not check_password_hash(user["password"], str(data.get("password", ""))):
        return jsonify(error="Email or password is incorrect."), 401
    if data.get("role") and data["role"] != user["role"]:
        return jsonify(error=f"This account does not have {data['role']} access."), 403
    token = secrets.token_urlsafe(32)
    db().execute("INSERT INTO sessions VALUES(?,?,?)", (token, user["id"], now()))
    db().commit()
    return jsonify(token=token, user=user_json(user))

@APP.post("/api/auth/logout")
@require_role()
def logout():
    db().execute("DELETE FROM sessions WHERE token=?", (g.user["token"],)); db().commit()
    return jsonify(message="Signed out")

@APP.get("/api/auth/me")
@require_role()
def me():
    return jsonify(user=user_json(g.user))

@APP.get("/api/destinations")
def destinations():
    term = f"%{request.args.get('q','').strip()}%"
    kind = request.args.get("type", "all")
    rows = db().execute("SELECT * FROM destinations WHERE (name LIKE ? OR address LIKE ?) AND (?='all' OR type=?) ORDER BY name", (term, term, kind, kind)).fetchall()
    return jsonify(destinations=[destination_json(row) for row in rows])

@APP.get("/api/availability/updates")
def availability_updates():
    since = request.args.get("since", "").strip()
    rows = db().execute("""SELECT destinations.id AS destination_id,destinations.name,availability.total,availability.available,
        availability.occupied,availability.updated_at FROM availability
        JOIN destinations ON destinations.id=availability.destination_id
        WHERE (?='' OR availability.updated_at>?) ORDER BY availability.updated_at DESC""", (since, since)).fetchall()
    return jsonify(server_time=now(), changed=bool(rows), updates=[dict(row) for row in rows])

@APP.get("/api/destinations/<destination_id>")
def destination_detail(destination_id):
    row = db().execute("SELECT * FROM destinations WHERE id=?", (destination_id,)).fetchone()
    if not row: return jsonify(error="Destination not found."), 404
    item = destination_json(row)
    user = current_user()
    if user and user["role"] == "customer":
        db().execute("INSERT INTO activity(user_id,destination_id,action,created_at) VALUES(?,?,?,?)", (user["id"], destination_id, "Viewed parking", now())); db().commit()
    return jsonify(destination=item)

@APP.get("/api/history")
@require_role("customer")
def customer_history():
    rows = db().execute("SELECT activity.created_at,activity.action,destinations.* FROM activity JOIN destinations ON destinations.id=activity.destination_id WHERE user_id=? ORDER BY activity.id DESC LIMIT 50", (g.user["id"],)).fetchall()
    return jsonify(history=[dict(row) for row in rows])

@APP.get("/api/parking/<destination_id>")
def parking_structure(destination_id):
    destination = db().execute("SELECT * FROM destinations WHERE id=?", (destination_id,)).fetchone()
    if not destination:
        return jsonify(error="Parking location not found."), 404
    floors = []
    for floor in db().execute("SELECT * FROM floors WHERE destination_id=? ORDER BY id", (destination_id,)).fetchall():
        slots = db().execute("""SELECT slots.*, EXISTS(
            SELECT 1 FROM reservations r WHERE r.slot_id=slots.id AND r.status IN ('confirmed','checked_in')
            AND r.start_at < ? AND r.end_at > ?
        ) AS reserved FROM slots JOIN zones ON zones.id=slots.zone_id
        WHERE zones.floor_id=? ORDER BY slots.code""", (now(), now(), floor["id"])).fetchall()
        floors.append({**dict(floor), "slots": [dict(slot) for slot in slots]})
    return jsonify(destination=dict(destination), floors=floors)

@APP.get("/api/vehicles")
@require_role("customer")
def list_vehicles():
    return jsonify(vehicles=[dict(row) for row in db().execute("SELECT id,plate,vehicle_type FROM vehicles WHERE user_id=? ORDER BY id", (g.user["id"],))])

@APP.post("/api/vehicles")
@require_role("customer")
def add_vehicle():
    data = request.get_json(silent=True) or {}
    plate, vehicle_type = str(data.get("plate", "")).strip().upper(), str(data.get("vehicle_type", "car")).strip().lower()
    if not plate or vehicle_type not in {"car", "two_wheeler", "large", "ev"}:
        return jsonify(error="Enter a vehicle plate and a supported vehicle type."), 400
    try:
        cur = db().execute("INSERT INTO vehicles(user_id,plate,vehicle_type) VALUES(?,?,?)", (g.user["id"], plate, vehicle_type)); db().commit()
    except sqlite3.IntegrityError:
        return jsonify(error="You already registered that vehicle."), 409
    return jsonify(vehicle={"id": cur.lastrowid, "plate": plate, "vehicle_type": vehicle_type}), 201

@APP.get("/api/reservations")
@require_role("customer", "manager", "admin")
def reservations():
    query = """SELECT r.*, slots.code AS slot_code, destinations.name AS destination_name
               FROM reservations r JOIN slots ON slots.id=r.slot_id JOIN zones ON zones.id=slots.zone_id
               JOIN floors ON floors.id=zones.floor_id JOIN destinations ON destinations.id=floors.destination_id"""
    args = ()
    if g.user["role"] == "customer":
        query += " WHERE r.user_id=?"; args = (g.user["id"],)
    query += " ORDER BY r.start_at DESC LIMIT 100"
    return jsonify(reservations=[reservation_json(row) for row in db().execute(query, args).fetchall()])

@APP.post("/api/reservation-holds")
@require_role("customer")
def create_hold():
    data = request.get_json(silent=True) or {}
    slot_id = data.get("slot_id")
    start, end = parse_iso(data.get("start_at")), parse_iso(data.get("end_at"))
    if not isinstance(slot_id, int) or not start or not end or end <= start:
        return jsonify(error="Choose a valid slot and time interval."), 400
    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conflict = conn.execute("""SELECT 1 FROM reservations WHERE slot_id=? AND status IN ('confirmed','checked_in')
            AND start_at<? AND end_at>? UNION SELECT 1 FROM slot_holds WHERE slot_id=? AND expires_at>? AND start_at<? AND end_at>?""",
            (slot_id, end.isoformat(), start.isoformat(), slot_id, now(), end.isoformat(), start.isoformat())).fetchone()
        if conflict:
            conn.rollback()
            return jsonify(error="This slot is already held or reserved."), 409
        hold_ref = f"PZH-{secrets.token_hex(6).upper()}"
        expires = (datetime.now(timezone.utc) + timedelta(minutes=policy("hold_minutes", 10))).isoformat()
        cur = conn.execute("INSERT INTO slot_holds(slot_id,user_id,start_at,end_at,expires_at,hold_ref) VALUES(?,?,?,?,?,?)", (slot_id, g.user["id"], start.isoformat(), end.isoformat(), expires, hold_ref))
        conn.commit()
        return jsonify(hold_id=cur.lastrowid, hold_ref=hold_ref, expires_at=expires), 201
    except sqlite3.OperationalError:
        conn.rollback()
        return jsonify(error="The hold service is busy. Please retry."), 503

@APP.delete("/api/reservation-holds/<int:hold_id>")
@require_role("customer")
def release_hold(hold_id):
    db().execute("DELETE FROM slot_holds WHERE id=? AND user_id=?", (hold_id, g.user["id"])); db().commit()
    return jsonify(message="Hold released.")

@APP.post("/api/waitlist")
@require_role("customer")
def join_waitlist():
    data = request.get_json(silent=True) or {}
    destination_id = str(data.get("destination_id", "")).strip()
    start, end = parse_iso(data.get("start_at")), parse_iso(data.get("end_at"))
    if not destination_id or not start or not end or end <= start or not db().execute("SELECT 1 FROM destinations WHERE id=?", (destination_id,)).fetchone():
        return jsonify(error="Choose a valid location and time interval."), 400
    cur = db().execute("INSERT INTO waitlist_entries(user_id,destination_id,slot_id,start_at,end_at,created_at) VALUES(?,?,?,?,?,?)", (g.user["id"], destination_id, data.get("slot_id"), start.isoformat(), end.isoformat(), now()))
    db().commit()
    return jsonify(waitlist_id=cur.lastrowid, message="You joined the waitlist."), 201

@APP.get("/api/waitlist")
@require_role("customer", "manager", "admin")
def get_waitlist():
    query = "SELECT waitlist_entries.*,destinations.name AS destination_name FROM waitlist_entries JOIN destinations ON destinations.id=waitlist_entries.destination_id"
    args = ()
    if g.user["role"] == "customer":
        query += " WHERE user_id=?"; args = (g.user["id"],)
    query += " ORDER BY waitlist_entries.created_at"
    return jsonify(waitlist=[dict(row) for row in db().execute(query, args).fetchall()])

@APP.delete("/api/waitlist/<int:entry_id>")
@require_role("customer")
def leave_waitlist(entry_id):
    db().execute("UPDATE waitlist_entries SET status='left' WHERE id=? AND user_id=? AND status='waiting'", (entry_id, g.user["id"])); db().commit()
    return jsonify(message="You left the waitlist.")

@APP.post("/api/waitlist/<int:entry_id>/claim")
@require_role("customer")
def claim_waitlist(entry_id):
    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        entry = conn.execute("SELECT * FROM waitlist_entries WHERE id=? AND user_id=? AND status='offered' AND offer_expires_at>?", (entry_id, g.user["id"], now())).fetchone()
        if not entry or not entry["slot_id"]:
            conn.rollback()
            return jsonify(error="This waitlist offer is no longer available."), 409
        conflict = conn.execute("SELECT 1 FROM reservations WHERE slot_id=? AND status IN ('confirmed','checked_in') AND start_at<? AND end_at>?", (entry["slot_id"], entry["end_at"], entry["start_at"])).fetchone()
        if conflict:
            conn.rollback()
            return jsonify(error="The released slot was claimed by another booking."), 409
        ref, nonce = f"PZ-{secrets.token_hex(6).upper()}", secrets.token_urlsafe(24)
        amount = round(max(1, (parse_iso(entry["end_at"]) - parse_iso(entry["start_at"])).total_seconds() / 3600) * policy("hourly_rate", 50), 2)
        cur = conn.execute("""INSERT INTO reservations(booking_ref,user_id,slot_id,start_at,end_at,quoted_amount,qr_nonce,created_at)
            VALUES(?,?,?,?,?,?,?,?)""", (ref, g.user["id"], entry["slot_id"], entry["start_at"], entry["end_at"], amount, nonce, now()))
        conn.execute("UPDATE waitlist_entries SET status='fulfilled' WHERE id=?", (entry_id,))
        notify(g.user["id"], "Waitlist booking confirmed", f"Your waitlist offer became booking {ref}.", "/reservations")
        audit("waitlist_fulfilled", "waitlist", entry_id, {"reservation_id": cur.lastrowid})
        conn.commit()
        row = conn.execute("SELECT r.*,slots.code AS slot_code FROM reservations r JOIN slots ON slots.id=r.slot_id WHERE r.id=?", (cur.lastrowid,)).fetchone()
        return jsonify(reservation=reservation_json(row)), 201
    except sqlite3.OperationalError:
        conn.rollback()
        return jsonify(error="The booking service is busy. Please retry."), 503

@APP.post("/api/reservations")
@require_role("customer")
def create_reservation():
    data = request.get_json(silent=True) or {}
    slot_id = data.get("slot_id")
    start, end = parse_iso(data.get("start_at")), parse_iso(data.get("end_at"))
    if not isinstance(slot_id, int) or not start or not end or start.tzinfo is None or end.tzinfo is None or start <= datetime.now(timezone.utc) or end <= start:
        return jsonify(error="Choose a future start and an end time after it."), 400
    if (end - start) > timedelta(hours=24) or start > datetime.now(timezone.utc) + timedelta(days=policy("max_advance_days", 30)):
        return jsonify(error="Reservations cannot exceed 24 hours."), 400
    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        slot = conn.execute("SELECT slots.*,floors.operating_open,floors.operating_close,floors.maintenance FROM slots JOIN zones ON zones.id=slots.zone_id JOIN floors ON floors.id=zones.floor_id WHERE slots.id=?", (slot_id,)).fetchone()
        if not slot or slot["maintenance"]:
            conn.rollback(); return jsonify(error="This parking slot is unavailable."), 409
        vehicle_id = data.get("vehicle_id")
        if vehicle_id:
            vehicle = conn.execute("SELECT * FROM vehicles WHERE id=? AND user_id=?", (vehicle_id, g.user["id"])).fetchone()
            if not vehicle or (vehicle["vehicle_type"] != slot["vehicle_type"] and slot["vehicle_type"] != "car"):
                conn.rollback(); return jsonify(error="The selected vehicle is not compatible with this slot."), 400
        overlap = conn.execute("""SELECT 1 FROM reservations WHERE slot_id=? AND status IN ('confirmed','checked_in')
            AND start_at < ? AND end_at > ? LIMIT 1""", (slot_id, end.isoformat(), start.isoformat())).fetchone()
        active_hold = conn.execute("SELECT 1 FROM slot_holds WHERE slot_id=? AND expires_at>? AND start_at<? AND end_at>? LIMIT 1", (slot_id, now(), end.isoformat(), start.isoformat())).fetchone()
        if overlap or active_hold:
            conn.rollback(); return jsonify(error="That slot is no longer available for the selected time."), 409
        ref, nonce = f"PZ-{secrets.token_hex(6).upper()}", secrets.token_urlsafe(24)
        amount = round(max(1, (end - start).total_seconds() / 3600) * policy("hourly_rate", 50), 2)
        cur = conn.execute("""INSERT INTO reservations(booking_ref,user_id,slot_id,vehicle_id,start_at,end_at,quoted_amount,qr_nonce,created_at)
            VALUES(?,?,?,?,?,?,?,?,?)""", (ref, g.user["id"], slot_id, vehicle_id, start.isoformat(), end.isoformat(), amount, nonce, now()))
        notify(g.user["id"], "Reservation confirmed", f"Your booking {ref} is confirmed.", f"/reservations/{cur.lastrowid}")
        audit("reservation_created", "reservation", cur.lastrowid, {"slot_id": slot_id})
        conn.commit()
        row = conn.execute("SELECT r.*,slots.code AS slot_code FROM reservations r JOIN slots ON slots.id=r.slot_id WHERE r.id=?", (cur.lastrowid,)).fetchone()
        return jsonify(reservation=reservation_json(row)), 201
    except sqlite3.OperationalError:
        conn.rollback()
        return jsonify(error="The booking service is busy. Please retry."), 503

@APP.post("/api/reservations/<int:reservation_id>/cancel")
@require_role("customer", "manager", "admin")
def cancel_reservation(reservation_id):
    row = db().execute("SELECT * FROM reservations WHERE id=?", (reservation_id,)).fetchone()
    if not row or (g.user["role"] == "customer" and row["user_id"] != g.user["id"]):
        return jsonify(error="Reservation not found."), 404
    if row["status"] not in ("confirmed", "held"):
        return jsonify(error="This reservation cannot be cancelled."), 409
    if g.user["role"] == "customer":
        start = parse_iso(row["start_at"])
        if start and start - datetime.now(timezone.utc) < timedelta(hours=policy("cancellation_hours", 2)):
            return jsonify(error="This reservation is inside the cancellation deadline."), 409
    db().execute("UPDATE reservations SET status='cancelled',cancelled_at=? WHERE id=?", (now(), reservation_id))
    offer_waitlist(row["slot_id"], row["start_at"], row["end_at"])
    notify(row["user_id"], "Reservation cancelled", f"Booking {row['booking_ref']} was cancelled.", "/reservations")
    audit("reservation_cancelled", "reservation", reservation_id); db().commit()
    return jsonify(message="Reservation cancelled.")

@APP.post("/api/reservations/<int:reservation_id>/check-in")
@require_role("customer", "manager", "admin")
def check_in(reservation_id):
    row = db().execute("SELECT * FROM reservations WHERE id=?", (reservation_id,)).fetchone()
    if not row or (g.user["role"] == "customer" and row["user_id"] != g.user["id"]):
        return jsonify(error="Reservation not found."), 404
    if row["status"] != "confirmed":
        return jsonify(error="This booking is not eligible for check-in."), 409
    if row["qr_nonce"] != str((request.get_json(silent=True) or {}).get("qr_reference", "")):
        return jsonify(error="The QR reference is invalid."), 400
    db().execute("UPDATE reservations SET status='checked_in',checked_in_at=? WHERE id=?", (now(), reservation_id))
    audit("reservation_check_in", "reservation", reservation_id); db().commit()
    return jsonify(message="Check-in recorded.")

@APP.get("/api/reservations/<int:reservation_id>/qr")
@require_role("customer", "manager", "admin")
def reservation_qr(reservation_id):
    row = owned_reservation(reservation_id)
    if not row:
        return jsonify(error="Reservation not found."), 404
    if row["status"] not in ("confirmed", "checked_in"):
        return jsonify(error="A QR code is only available for an active reservation."), 409
    image = qrcode.make(row["qr_nonce"])
    output = BytesIO()
    image.save(output, format="PNG")
    output.seek(0)
    return send_file(output, mimetype="image/png", download_name=f"{row['booking_ref']}.png")

@APP.post("/api/reservations/<int:reservation_id>/check-out")
@require_role("customer", "manager", "admin")
def check_out(reservation_id):
    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError:
        return jsonify(error="The parking session is busy. Please retry."), 503
    row = conn.execute("SELECT * FROM reservations WHERE id=?", (reservation_id,)).fetchone()
    if not row or (g.user["role"] == "customer" and row["user_id"] != g.user["id"]):
        conn.rollback()
        return jsonify(error="Reservation not found."), 404
    if row["status"] != "checked_in":
        conn.rollback()
        return jsonify(error="Only an active parking session can be checked out."), 409
    checked_out = now()
    duration_hours = max(1, (parse_iso(checked_out) - parse_iso(row["checked_in_at"])).total_seconds() / 3600)
    amount = round(duration_hours * policy("hourly_rate", 50), 2)
    membership = db().execute("""SELECT memberships.id,memberships.points_balance,membership_plans.discount_percent,membership_plans.points_per_transaction
        FROM memberships JOIN membership_plans ON membership_plans.id=memberships.plan_id
        WHERE memberships.user_id=? AND memberships.valid_until>? AND memberships.cancelled_at IS NULL""", (row["user_id"], now())).fetchone()
    discount = round(amount * membership["discount_percent"] / 100, 2) if membership else 0
    original_amount = amount
    amount = round(amount - discount, 2)
    data = request.get_json(silent=True) or {}
    points_redeemed = int(data.get("points_redeemed", 0) or 0)
    redemption_value = policy("points_value", 0.1)
    if points_redeemed < 0 or points_redeemed > policy("max_points_per_checkout", 1000) or points_redeemed % int(policy("points_step", 10)) != 0:
        conn.rollback()
        return jsonify(error="Points must be redeemed in valid increments."), 400
    if points_redeemed and not membership:
        conn.rollback()
        return jsonify(error="An active membership is required to redeem points."), 409
    if points_redeemed > (membership["points_balance"] if membership and "points_balance" in membership.keys() else 0):
        conn.rollback()
        return jsonify(error="Insufficient loyalty points."), 409
    points_discount = round(points_redeemed * redemption_value, 2)
    amount = round(max(0, amount - points_discount), 2)
    db().execute("UPDATE reservations SET status='completed',checked_out_at=?,quoted_amount=? WHERE id=?", (checked_out, amount, reservation_id))
    if membership:
        reference = f"checkout-{reservation_id}"
        points = int(duration_hours * membership["points_per_transaction"])
        if points and not db().execute("SELECT 1 FROM loyalty_transactions WHERE reference=?", (reference,)).fetchone():
            db().execute("UPDATE memberships SET points_balance=points_balance+? WHERE id=?", (points, membership["id"]))
            db().execute("INSERT INTO loyalty_transactions(membership_id,kind,points,reference,created_at) VALUES(?,?,?,?,?)", (membership["id"], "earned", points, reference, now()))
        if points_redeemed:
            db().execute("UPDATE memberships SET points_balance=points_balance-? WHERE id=?", (points_redeemed, membership["id"]))
            db().execute("INSERT INTO loyalty_transactions(membership_id,kind,points,reference,created_at) VALUES(?,?,?,?,?)", (membership["id"], "redeemed", -points_redeemed, f"redeem-{reservation_id}", now()))
    db().execute("INSERT OR IGNORE INTO payments(reservation_id,user_id,amount,status,provider,created_at) VALUES(?,?,?,?,?,?)", (reservation_id, row["user_id"], amount, "pending", "test", now()))
    notify(row["user_id"], "Parking session completed", f"Booking {row['booking_ref']} has been checked out. Final amount: {amount:.2f}.")
    audit("reservation_check_out", "reservation", reservation_id, {"original_amount": original_amount, "membership_discount": discount, "points_redeemed": points_redeemed, "amount": amount}); db().commit()
    return jsonify(message="Check-out recorded.", original_amount=original_amount, membership_discount=discount, points_redeemed=points_redeemed, points_discount=points_discount, final_amount=amount, checked_out_at=checked_out)

@APP.post("/api/payments/<int:reservation_id>/test-confirm")
@require_role("customer")
def test_payment_confirm(reservation_id):
    row = db().execute("SELECT * FROM reservations WHERE id=? AND user_id=?", (reservation_id, g.user["id"])).fetchone()
    if not row:
        return jsonify(error="Reservation not found."), 404
    payment = db().execute("SELECT * FROM payments WHERE reservation_id=?", (reservation_id,)).fetchone()
    if not payment:
        return jsonify(error="A bill is not available for this reservation yet."), 409
    if payment["status"] == "paid":
        return jsonify(payment=dict(payment))
    reference = f"TEST-{secrets.token_hex(8).upper()}"
    db().execute("UPDATE payments SET status='paid',transaction_ref=?,verified_at=? WHERE id=?", (reference, now(), payment["id"]))
    notify(g.user["id"], "Test payment confirmed", f"Payment {reference} was recorded in test mode.", "/reservations")
    audit("payment_verified", "payment", payment["id"], {"provider": "test"})
    db().commit()
    return jsonify(payment=dict(db().execute("SELECT * FROM payments WHERE id=?", (payment["id"],)).fetchone()))

@APP.get("/api/policies")
@require_role("admin", "manager")
def get_policies():
    return jsonify(policies={row["key"]: row["value"] for row in db().execute("SELECT key,value FROM system_policies ORDER BY key")})

@APP.patch("/api/policies")
@require_role("admin")
def update_policies():
    data = request.get_json(silent=True) or {}
    allowed = {"hourly_rate", "hold_minutes", "cancellation_hours", "no_show_grace_minutes", "max_advance_days", "points_value", "points_step", "max_points_per_checkout"}
    conn = db()
    for key, value in data.items():
        if key not in allowed:
            continue
        try:
            numeric = float(value)
            if numeric < 0:
                raise ValueError
        except (TypeError, ValueError):
            return jsonify(error=f"Invalid policy value for {key}."), 400
        conn.execute("INSERT INTO system_policies(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at", (key, str(value), now()))
    audit("policies_updated", "system", "policies", data); conn.commit()
    return jsonify(policies={row["key"]: row["value"] for row in conn.execute("SELECT key,value FROM system_policies ORDER BY key")})

@APP.patch("/api/admin/membership-plans/<int:plan_id>")
@require_role("admin")
def update_membership_plan(plan_id):
    data = request.get_json(silent=True) or {}
    plan = db().execute("SELECT * FROM membership_plans WHERE id=?", (plan_id,)).fetchone()
    if not plan:
        return jsonify(error="Membership plan not found."), 404
    fields = {}
    for key in ("discount_percent", "points_per_transaction", "duration_days", "priority_booking", "price", "benefits", "eligible_categories"):
        if key in data:
            if key in {"discount_percent", "price"}:
                value = float(data[key])
                if value < 0 or (key == "discount_percent" and value > 100):
                    return jsonify(error=f"Invalid {key}."), 400
            elif key in {"points_per_transaction", "duration_days", "priority_booking"}:
                value = int(data[key])
                if value < 0:
                    return jsonify(error=f"Invalid {key}."), 400
            else:
                value = str(data[key]).strip()
            fields[key] = value
    if not fields:
        return jsonify(error="No plan fields supplied."), 400
    assignments = ", ".join(f"{key}=?" for key in fields)
    db().execute(f"UPDATE membership_plans SET {assignments} WHERE id=?", (*fields.values(), plan_id))
    audit("membership_plan_updated", "membership_plan", plan_id, fields); db().commit()
    return jsonify(plan=dict(db().execute("SELECT * FROM membership_plans WHERE id=?", (plan_id,)).fetchone()))

@APP.get("/api/passes")
@require_role("customer")
def list_passes():
    rows = db().execute("""SELECT parking_passes.*,destinations.name AS destination_name,vehicles.plate
        FROM parking_passes LEFT JOIN destinations ON destinations.id=parking_passes.destination_id
        LEFT JOIN vehicles ON vehicles.id=parking_passes.vehicle_id WHERE parking_passes.user_id=? ORDER BY valid_from DESC""", (g.user["id"],)).fetchall()
    return jsonify(passes=[dict(row) for row in rows])

@APP.post("/api/passes")
@require_role("customer")
def purchase_pass():
    data = request.get_json(silent=True) or {}
    pass_type = str(data.get("pass_type", "")).lower()
    durations = {"daily": 1, "weekly": 7, "monthly": 30}
    prices = {"daily": 100, "weekly": 500, "monthly": 1500}
    if pass_type not in durations:
        return jsonify(error="Choose a daily, weekly, or monthly pass."), 400
    if not membership_payment_allowed(data):
        return jsonify(error="Pass purchase requires a configured payment provider. Test mode is disabled."), 503
    destination_id = data.get("destination_id")
    vehicle_id = data.get("vehicle_id")
    if destination_id and not db().execute("SELECT 1 FROM destinations WHERE id=?", (destination_id,)).fetchone():
        return jsonify(error="Destination not found."), 404
    if vehicle_id and not db().execute("SELECT 1 FROM vehicles WHERE id=? AND user_id=?", (vehicle_id, g.user["id"])).fetchone():
        return jsonify(error="Vehicle is not registered to this account."), 403
    start = datetime.now(timezone.utc)
    end = start + timedelta(days=durations[pass_type])
    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    cur = conn.execute("""INSERT INTO parking_passes(user_id,pass_type,price,valid_from,valid_until,destination_id,vehicle_id,created_at)
        VALUES(?,?,?,?,?,?,?,?)""", (g.user["id"], pass_type, prices[pass_type], start.isoformat(), end.isoformat(), destination_id, vehicle_id, now()))
    audit("parking_pass_activated", "parking_pass", cur.lastrowid, {"pass_type": pass_type, "payment_mode": "test"})
    conn.commit()
    return jsonify(pass_record=dict(conn.execute("SELECT * FROM parking_passes WHERE id=?", (cur.lastrowid,)).fetchone())), 201

@APP.get("/api/admin/analytics")
@require_role("admin", "manager")
def analytics():
    conn = db()
    return jsonify(
        reservations_by_status=[dict(row) for row in conn.execute("SELECT status,COUNT(*) AS count FROM reservations GROUP BY status ORDER BY status")],
        occupancy={"facilities": conn.execute("SELECT COUNT(*) FROM destinations").fetchone()[0], "spaces": conn.execute("SELECT COUNT(*) FROM slots").fetchone()[0], "active_sessions": conn.execute("SELECT COUNT(*) FROM reservations WHERE status='checked_in'").fetchone()[0]},
        payments=[dict(row) for row in conn.execute("SELECT status,COUNT(*) AS count,COALESCE(SUM(amount),0) AS amount FROM payments GROUP BY status")],
        support=[dict(row) for row in conn.execute("SELECT status,COUNT(*) AS count FROM support_tickets GROUP BY status")],
    )

@APP.post("/api/manager/floors")
@require_role("manager", "admin")
def create_floor():
    data = request.get_json(silent=True) or {}
    destination_id = str(data.get("destination_id", "")).strip()
    name = str(data.get("name", "")).strip().upper()
    capacity = int(data.get("capacity", 0) or 0)
    if not destination_id or not name or capacity <= 0 or not db().execute("SELECT 1 FROM destinations WHERE id=?", (destination_id,)).fetchone():
        return jsonify(error="Provide a valid destination, floor name, and positive capacity."), 400
    try:
        cur = db().execute("INSERT INTO floors(destination_id,name,capacity) VALUES(?,?,?)", (destination_id, name, capacity))
        db().execute("INSERT INTO zones(floor_id,name) VALUES(?,?)", (cur.lastrowid, "A")); db().commit()
    except sqlite3.IntegrityError:
        return jsonify(error="That floor already exists at this location."), 409
    audit("floor_created", "floor", cur.lastrowid); db().commit()
    return jsonify(floor={"id": cur.lastrowid, "name": name, "capacity": capacity}), 201

@APP.get("/api/manager/structure")
@require_role("manager", "admin")
def manager_structure():
    result = []
    for destination in db().execute("SELECT * FROM destinations ORDER BY name").fetchall():
        floors = []
        for floor in db().execute("SELECT * FROM floors WHERE destination_id=? ORDER BY id", (destination["id"],)).fetchall():
            zones = []
            for zone in db().execute("SELECT * FROM zones WHERE floor_id=? ORDER BY name", (floor["id"],)).fetchall():
                slots = db().execute("SELECT * FROM slots WHERE zone_id=? ORDER BY code", (zone["id"],)).fetchall()
                zones.append({**dict(zone), "slots": [dict(slot) for slot in slots]})
            floors.append({**dict(floor), "zones": zones})
        result.append({**dict(destination), "floors": floors})
    return jsonify(destinations=result)

@APP.post("/api/manager/zones")
@require_role("manager", "admin")
def create_zone():
    data = request.get_json(silent=True) or {}
    floor_id, name = data.get("floor_id"), str(data.get("name", "")).strip().upper()
    if not isinstance(floor_id, int) or not name:
        return jsonify(error="Provide a floor and zone name."), 400
    try:
        cur = db().execute("INSERT INTO zones(floor_id,name) VALUES(?,?)", (floor_id, name))
        db().commit()
    except sqlite3.IntegrityError:
        return jsonify(error="That zone already exists on this floor."), 409
    audit("zone_created", "zone", cur.lastrowid); db().commit()
    return jsonify(zone={"id": cur.lastrowid, "floor_id": floor_id, "name": name}), 201

@APP.post("/api/manager/slots")
@require_role("manager", "admin")
def create_slot():
    data = request.get_json(silent=True) or {}
    zone_id = data.get("zone_id")
    code = str(data.get("code", "")).strip().upper()
    vehicle_type = str(data.get("vehicle_type", "car")).strip().lower()
    if not isinstance(zone_id, int) or not code or vehicle_type not in {"car", "two_wheeler", "large", "ev"}:
        return jsonify(error="Provide a zone, unique slot code, and supported vehicle type."), 400
    try:
        cur = db().execute("""INSERT INTO slots(zone_id,code,vehicle_type,accessible,ev_charging,distance_meters)
            VALUES(?,?,?,?,?,?)""", (zone_id, code, vehicle_type, int(bool(data.get("accessible"))), int(bool(data.get("ev_charging"))), data.get("distance_meters")))
        db().commit()
    except sqlite3.IntegrityError:
        return jsonify(error="That slot code already exists."), 409
    audit("slot_created", "slot", cur.lastrowid); db().commit()
    return jsonify(slot={"id": cur.lastrowid, "zone_id": zone_id, "code": code, "vehicle_type": vehicle_type}), 201

@APP.patch("/api/manager/floors/<int:floor_id>")
@require_role("manager", "admin")
def update_floor(floor_id):
    data = request.get_json(silent=True) or {}
    fields = {}
    if "name" in data and str(data["name"]).strip():
        fields["name"] = str(data["name"]).strip().upper()
    if "capacity" in data:
        try:
            fields["capacity"] = max(0, int(data["capacity"]))
        except (TypeError, ValueError):
            return jsonify(error="Capacity must be a non-negative integer."), 400
    if "maintenance" in data:
        fields["maintenance"] = int(bool(data["maintenance"]))
    for key in ("operating_open", "operating_close"):
        if key in data:
            fields[key] = str(data[key]).strip()
    if not fields:
        return jsonify(error="No floor changes supplied."), 400
    assignments = ", ".join(f"{key}=?" for key in fields)
    try:
        db().execute(f"UPDATE floors SET {assignments} WHERE id=?", (*fields.values(), floor_id))
        audit("floor_updated", "floor", floor_id, fields); db().commit()
    except sqlite3.IntegrityError:
        return jsonify(error="That floor name already exists at this location."), 409
    return jsonify(message="Floor updated.")

@APP.patch("/api/manager/slots/<int:slot_id>")
@require_role("manager", "admin")
def update_slot(slot_id):
    data = request.get_json(silent=True) or {}
    fields = {}
    if "status" in data and data["status"] in {"available", "occupied", "disabled"}:
        fields["status"] = data["status"]
    for key in ("vehicle_type",):
        if key in data and data[key] in {"car", "two_wheeler", "large", "ev"}:
            fields[key] = data[key]
    for key in ("accessible", "ev_charging"):
        if key in data:
            fields[key] = int(bool(data[key]))
    if not fields:
        return jsonify(error="No valid slot changes supplied."), 400
    assignments = ", ".join(f"{key}=?" for key in fields)
    db().execute(f"UPDATE slots SET {assignments} WHERE id=?", (*fields.values(), slot_id))
    audit("slot_updated", "slot", slot_id, fields); db().commit()
    return jsonify(message="Slot updated.")

@APP.post("/api/support/<int:ticket_id>/replies")
@require_role("manager", "admin", "customer")
def support_reply(ticket_id):
    ticket = db().execute("SELECT * FROM support_tickets WHERE id=?", (ticket_id,)).fetchone()
    if not ticket or (g.user["role"] == "customer" and ticket["user_id"] != g.user["id"]):
        return jsonify(error="Support ticket not found."), 404
    message = str((request.get_json(silent=True) or {}).get("message", "")).strip()
    if not message:
        return jsonify(error="Reply cannot be empty."), 400
    db().execute("INSERT INTO ticket_replies(ticket_id,user_id,message,created_at) VALUES(?,?,?,?)", (ticket_id, g.user["id"], message, now()))
    new_status = "in_progress" if g.user["role"] in {"manager", "admin"} else ticket["status"]
    db().execute("UPDATE support_tickets SET status=?,updated_at=? WHERE id=?", (new_status, now(), ticket_id))
    if g.user["id"] != ticket["user_id"]:
        notify(ticket["user_id"], "Support ticket updated", f"Your ticket {ticket['ticket_ref']} has a new reply.", f"/support/{ticket_id}")
    db().commit()
    return jsonify(message="Reply added.")

@APP.get("/api/support/<int:ticket_id>")
@require_role("manager", "admin", "customer")
def support_detail(ticket_id):
    ticket = db().execute("SELECT * FROM support_tickets WHERE id=?", (ticket_id,)).fetchone()
    if not ticket or (g.user["role"] == "customer" and ticket["user_id"] != g.user["id"]):
        return jsonify(error="Support ticket not found."), 404
    replies = db().execute("SELECT ticket_replies.*,users.name AS author FROM ticket_replies JOIN users ON users.id=ticket_replies.user_id WHERE ticket_id=? ORDER BY ticket_replies.id", (ticket_id,)).fetchall()
    return jsonify(ticket=dict(ticket), replies=[dict(reply) for reply in replies])

@APP.get("/api/memberships/me")
@require_role("customer")
def membership():
    row = db().execute("""SELECT memberships.*,membership_plans.name,membership_plans.discount_percent,
        membership_plans.points_per_transaction,membership_plans.priority_booking,membership_plans.price,membership_plans.benefits
        FROM memberships JOIN membership_plans ON membership_plans.id=memberships.plan_id WHERE user_id=?""", (g.user["id"],)).fetchone()
    if not row:
        plan = db().execute("SELECT * FROM membership_plans WHERE name='Basic'").fetchone()
        expiry = (datetime.now(timezone.utc) + timedelta(days=plan["duration_days"])).isoformat()
        number = f"PZM-{secrets.token_hex(5).upper()}"
        db().execute("INSERT INTO memberships(user_id,plan_id,membership_number,valid_until,created_at) VALUES(?,?,?,?,?)", (g.user["id"], plan["id"], number, expiry, now())); db().commit()
        row = db().execute("""SELECT memberships.*,membership_plans.name,membership_plans.discount_percent,
            membership_plans.points_per_transaction,membership_plans.priority_booking,membership_plans.price,membership_plans.benefits FROM memberships JOIN membership_plans ON membership_plans.id=memberships.plan_id WHERE user_id=?""", (g.user["id"],)).fetchone()
    plans = [dict(item) for item in db().execute("SELECT * FROM membership_plans ORDER BY price").fetchall()]
    transactions = [dict(item) for item in db().execute("SELECT * FROM loyalty_transactions WHERE membership_id=? ORDER BY id DESC", (row["id"],)).fetchall()]
    return jsonify(membership=dict(row), plans=plans, transactions=transactions)

@APP.get("/api/membership-plans")
def membership_plans():
    return jsonify(plans=[dict(row) for row in db().execute("SELECT * FROM membership_plans ORDER BY price").fetchall()])

def membership_payment_allowed(data):
    return str(data.get("payment_mode", "")).lower() == "test" and os.environ.get("PARKZEN_TEST_PAYMENTS") == "1"

@APP.post("/api/memberships/purchase")
@require_role("customer")
def purchase_membership():
    data = request.get_json(silent=True) or {}
    plan = db().execute("SELECT * FROM membership_plans WHERE id=?", (data.get("plan_id"),)).fetchone()
    if not plan:
        return jsonify(error="Membership plan not found."), 404
    if not membership_payment_allowed(data):
        return jsonify(error="Membership activation requires a configured payment provider. Test mode is disabled."), 503
    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    current = conn.execute("SELECT * FROM memberships WHERE user_id=?", (g.user["id"],)).fetchone()
    expiry = datetime.now(timezone.utc) + timedelta(days=plan["duration_days"])
    if current and current["valid_until"] > now() and current["cancelled_at"] is None:
        conn.rollback()
        return jsonify(error="An active membership already exists. Use renewal or cancellation first."), 409
    if current:
        conn.execute("UPDATE memberships SET plan_id=?,valid_until=?,cancelled_at=NULL WHERE id=?", (plan["id"], expiry.isoformat(), current["id"]))
        membership_id = current["id"]
    else:
        number = f"PZM-{secrets.token_hex(5).upper()}"
        membership_id = conn.execute("INSERT INTO memberships(user_id,plan_id,membership_number,valid_until,created_at) VALUES(?,?,?,?,?)", (g.user["id"], plan["id"], number, expiry.isoformat(), now())).lastrowid
    notify(g.user["id"], "Membership activated", f"{plan['name']} membership is active until {expiry.date().isoformat()}.", "/membership")
    audit("membership_activated", "membership", membership_id, {"plan_id": plan["id"], "payment_mode": "test"})
    conn.commit()
    return jsonify(message="Membership activated in explicitly enabled test mode.", membership_id=membership_id, valid_until=expiry.isoformat()), 201

@APP.post("/api/memberships/renew")
@require_role("customer")
def renew_membership():
    data = request.get_json(silent=True) or {}
    if not membership_payment_allowed(data):
        return jsonify(error="Membership renewal requires a configured payment provider. Test mode is disabled."), 503
    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    current = conn.execute("SELECT memberships.*,membership_plans.duration_days FROM memberships JOIN membership_plans ON membership_plans.id=memberships.plan_id WHERE user_id=?", (g.user["id"],)).fetchone()
    if not current:
        conn.rollback()
        return jsonify(error="No membership exists to renew."), 404
    base = parse_iso(current["valid_until"]) if current["valid_until"] > now() and current["cancelled_at"] is None else datetime.now(timezone.utc)
    expiry = base + timedelta(days=current["duration_days"])
    conn.execute("UPDATE memberships SET valid_until=?,cancelled_at=NULL WHERE id=?", (expiry.isoformat(), current["id"]))
    audit("membership_renewed", "membership", current["id"], {"payment_mode": "test"})
    conn.commit()
    return jsonify(message="Membership renewed in explicitly enabled test mode.", valid_until=expiry.isoformat())

@APP.post("/api/memberships/cancel")
@require_role("customer")
def cancel_membership():
    row = db().execute("SELECT * FROM memberships WHERE user_id=?", (g.user["id"],)).fetchone()
    if not row or row["cancelled_at"] is not None:
        return jsonify(error="No active membership exists."), 409
    db().execute("UPDATE memberships SET cancelled_at=? WHERE id=?", (now(), row["id"]))
    audit("membership_cancelled", "membership", row["id"]); db().commit()
    return jsonify(message="Membership cancelled. Benefits remain unavailable immediately.")

@APP.get("/api/notifications")
@require_role()
def notifications():
    return jsonify(unread=db().execute("SELECT COUNT(*) FROM notifications WHERE user_id=? AND read_at IS NULL", (g.user["id"],)).fetchone()[0], notifications=[dict(row) for row in db().execute("SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT 50", (g.user["id"],)).fetchall()])

@APP.post("/api/notifications/<int:notification_id>/read")
@require_role()
def mark_notification_read(notification_id):
    db().execute("UPDATE notifications SET read_at=? WHERE id=? AND user_id=?", (now(), notification_id, g.user["id"])); db().commit()
    return jsonify(message="Notification marked as read.")

@APP.get("/api/support")
@require_role("customer", "manager", "admin")
def support_list():
    query, args = "SELECT * FROM support_tickets", ()
    if g.user["role"] == "customer":
        query += " WHERE user_id=?"; args = (g.user["id"],)
    query += " ORDER BY updated_at DESC LIMIT 100"
    return jsonify(tickets=[dict(row) for row in db().execute(query, args).fetchall()])

@APP.post("/api/support")
@require_role("customer")
def support_create():
    data = request.get_json(silent=True) or {}
    subject, description = str(data.get("subject", "")).strip(), str(data.get("description", "")).strip()
    kind, category = str(data.get("kind", "question")), str(data.get("category", "general"))
    if not subject or not description or kind not in {"question", "suggestion", "complaint"}:
        return jsonify(error="Provide a subject, description, and valid support type."), 400
    ref = f"PZT-{secrets.token_hex(5).upper()}"
    cur = db().execute("""INSERT INTO support_tickets(ticket_ref,user_id,category,kind,priority,subject,description,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?)""", (ref, g.user["id"], category, kind, "normal", subject, description, now(), now()))
    db().commit()
    return jsonify(ticket={"id": cur.lastrowid, "ticket_ref": ref}), 201

@APP.get("/api/manager/predictions")
@require_role("manager")
def prediction_history():
    rows = db().execute("SELECT predictions.*,destinations.name AS destination_name FROM predictions JOIN destinations ON destinations.id=predictions.destination_id WHERE status='confirmed' ORDER BY id DESC LIMIT 50").fetchall()
    return jsonify(history=[dict(row) for row in rows])

@APP.post("/api/manager/analyze")
@require_role("manager")
def analyze():
    destination_id = request.form.get("destination_id", "")
    image = request.files.get("image")
    destination = db().execute("SELECT * FROM destinations WHERE id=?", (destination_id,)).fetchone()
    if not destination: return jsonify(error="Choose a valid parking location."), 400
    if not image or not image.filename: return jsonify(error="Choose a parking image to analyze."), 400
    suffix = Path(image.filename).suffix.lower()
    if suffix not in {".jpg", ".jpeg", ".png", ".webp", ".mp4", ".avi", ".mov", ".mkv"}:
        return jsonify(error="Upload a JPG, PNG, WebP image, or a supported video."), 400
    path = UPLOAD_DIR / f"{secrets.token_hex(12)}{Path(image.filename).suffix.lower()}"
    image.save(path)
    try:
        requested_floor = request.form.get("floor_id")
        requested_zone = request.form.get("zone_id")
        slot_query = """SELECT slots.id,slots.code,floors.name AS floor_name,zones.name AS zone_name
            FROM slots JOIN zones ON zones.id=slots.zone_id JOIN floors ON floors.id=zones.floor_id
            WHERE floors.destination_id=?"""
        slot_args = [destination_id]
        if requested_floor:
            slot_query += " AND floors.id=?"
            slot_args.append(requested_floor)
        if requested_zone:
            slot_query += " AND zones.id=?"
            slot_args.append(requested_zone)
        slot_rows = db().execute(slot_query + " ORDER BY floors.id,zones.id,slots.code", slot_args).fetchall()
        if not slot_rows:
            path.unlink(missing_ok=True)
            return jsonify(error="Configure at least one parking slot for this facility before analyzing occupancy."), 409
        results = list(load_model()(str(path), conf=0.25))
        if not results:
            path.unlink(missing_ok=True)
            return jsonify(error="The model returned no frames for this input."), 422
        detections = []
        names = load_model().names
        for result in results:
            for box in result.boxes:
                class_id = int(box.cls[0])
                class_name = str(names.get(class_id, class_id)).lower()
                if class_name not in {"space-empty", "space-occupied"}:
                    continue
                detections.append({"status": "available" if class_name == "space-empty" else "occupied", "class_name": class_name, "confidence": round(float(box.conf[0]), 4), "coordinates": [round(float(x), 2) for x in box.xyxy[0].tolist()]})
        detections.sort(key=lambda item: (item["coordinates"][1], item["coordinates"][0]))
        if len(detections) != len(slot_rows):
            path.unlink(missing_ok=True)
            return jsonify(error=f"The model detected {len(detections)} spaces but the selected scope has {len(slot_rows)} configured slots. Select the matching floor or zone; partial coverage cannot be published as full availability."), 422
        for index, detection in enumerate(detections):
            detection["slot_id"] = slot_rows[index]["id"]
            detection["slot_code"] = slot_rows[index]["code"]
            detection["floor_name"] = slot_rows[index]["floor_name"]
            detection["zone_name"] = slot_rows[index]["zone_name"]
        available = sum(slot["status"] == "available" for slot in detections)
        occupied = sum(slot["status"] == "occupied" for slot in detections)
        cur = db().execute("INSERT INTO predictions(destination_id,manager_id,image_path,total,available,occupied,slots,status,created_at) VALUES(?,?,?,?,?,?,?,?,?)", (destination_id, g.user["id"], path.name, len(detections), available, occupied, __import__('json').dumps(detections), "pending", now()))
        db().commit()
        return jsonify(prediction_id=cur.lastrowid, destination_id=destination_id, destination_name=destination["name"], total=len(detections), available=available, occupied=occupied, slots=detections), 201
    except Exception as error:
        path.unlink(missing_ok=True)
        APP.logger.exception("YOLO analysis failed")
        return jsonify(error=f"AI analysis failed: {error}"), 500

@APP.post("/api/manager/confirm/<int:prediction_id>")
@require_role("manager")
def confirm(prediction_id):
    row = db().execute("SELECT * FROM predictions WHERE id=? AND status='pending' AND manager_id=?", (prediction_id, g.user["id"])).fetchone()
    if not row: return jsonify(error="This analysis is no longer awaiting confirmation."), 404
    confirmed = now()
    db().execute("UPDATE predictions SET status='confirmed',confirmed_at=? WHERE id=?", (confirmed, prediction_id))
    for detection in json.loads(row["slots"]):
        db().execute("UPDATE slots SET ai_status=? WHERE id=?", (detection["status"], detection["slot_id"]))
    db().execute("INSERT INTO availability VALUES(?,?,?,?,?,?) ON CONFLICT(destination_id) DO UPDATE SET total=excluded.total,available=excluded.available,occupied=excluded.occupied,updated_at=excluded.updated_at,manager_id=excluded.manager_id", (row["destination_id"], row["total"], row["available"], row["occupied"], confirmed, g.user["id"]))
    audit("ai_prediction_confirmed", "prediction", prediction_id, {"mapped_slots": len(json.loads(row["slots"]))})
    db().commit()
    return jsonify(message="Parking availability updated.", destination_id=row["destination_id"], updated_at=confirmed)

@APP.get("/api/profile")
@require_role()
def profile():
    return jsonify(user=user_json(g.user))

@APP.patch("/api/profile")
@require_role()
def update_profile():
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip(); mobile = str(data.get("mobile", "")).strip()
    if not name or len(mobile) < 7: return jsonify(error="Enter a name and valid mobile number."), 400
    db().execute("UPDATE users SET name=?,mobile=? WHERE id=?", (name, mobile, g.user["id"])); db().commit()
    return jsonify(user=user_json(db().execute("SELECT * FROM users WHERE id=?", (g.user["id"],)).fetchone()))

@APP.get("/api/admin/overview")
@require_role("admin")
def admin_overview():
    conn = db()
    return jsonify(
        customers=conn.execute("SELECT COUNT(*) FROM users WHERE role='customer'").fetchone()[0],
        managers=conn.execute("SELECT COUNT(*) FROM users WHERE role='manager'").fetchone()[0],
        destinations=conn.execute("SELECT COUNT(*) FROM destinations").fetchone()[0],
        floors=conn.execute("SELECT COUNT(*) FROM floors").fetchone()[0],
        spaces=conn.execute("SELECT COUNT(*) FROM slots").fetchone()[0],
        reservations_today=conn.execute("SELECT COUNT(*) FROM reservations WHERE date(start_at)=date('now')").fetchone()[0],
        active_sessions=conn.execute("SELECT COUNT(*) FROM reservations WHERE status='checked_in'").fetchone()[0],
        open_tickets=conn.execute("SELECT COUNT(*) FROM support_tickets WHERE status IN ('open','in_progress')").fetchone()[0],
        confirmed_updates=conn.execute("SELECT COUNT(*) FROM predictions WHERE status='confirmed'").fetchone()[0],
        latest_updates=[dict(row) for row in conn.execute("SELECT destinations.name,availability.total,availability.available,availability.occupied,availability.updated_at FROM availability JOIN destinations ON destinations.id=availability.destination_id ORDER BY availability.updated_at DESC")],
    )

@APP.get("/api/admin/users")
@require_role("admin")
def admin_users():
    rows = db().execute("SELECT id,name,email,mobile,role,created_at FROM users ORDER BY role,name").fetchall()
    return jsonify(users=[dict(row) for row in rows])

initialize()

if __name__ == "__main__":
    APP.run(host="127.0.0.1", port=5000, debug=os.environ.get("PARKZEN_DEBUG") == "1")
