# app.py - Flask server for Smart Helmet accident alerts (MySQL backend)

import ipaddress
import json
import os
import ssl
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

# Load .env from server dir so ESP32_CAM_STREAM_URL etc. can be set without env vars
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.isfile(_env_path):
    try:
        from dotenv import load_dotenv
        load_dotenv(_env_path)
    except ImportError:
        pass

import pymysql
from flask import Flask, request, jsonify, render_template, redirect, url_for, Response, session
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__, static_folder="public", static_url_path="")
app.secret_key = os.environ.get("SECRET_KEY", "guardian-helmet-dev-key")

# ESP32 connection status: updated on /alert POST and /api/ping GET; dashboard polls /api/status
_last_esp32_seen = None
ESP32_CONNECTED_SEC = 120  # consider "connected" if activity in last 2 minutes

# Optional ESP32-CAM dash cam: MJPEG stream URL (e.g. http://192.168.1.100/stream)
ESP32_CAM_STREAM_URL = os.environ.get("ESP32_CAM_STREAM_URL", "http://172.20.10.3/stream").strip()
FIXED_ADMIN_USERNAME = os.environ.get("FIXED_ADMIN_USERNAME", "admin").strip() or "admin"
FIXED_ADMIN_PASSWORD = os.environ.get("FIXED_ADMIN_PASSWORD", "admin123").strip() or "admin123"
IPROG_SMS_API_TOKEN = os.environ.get("IPROG_SMS_API_TOKEN", "").strip()
IPROG_SMS_BASE_URL = os.environ.get("IPROG_SMS_BASE_URL", "https://sms.iprogtech.com").strip().rstrip("/")
IPROG_SMS_ENABLED = os.environ.get("IPROG_SMS_ENABLED", "1").strip().lower() in {"1", "true", "yes", "on"}
SEMAPHORE_API_KEY = os.environ.get("SEMAPHORE_API_KEY", "").strip()
SEMAPHORE_SMS_BASE_URL = os.environ.get("SEMAPHORE_SMS_BASE_URL", "https://api.semaphore.co").strip().rstrip("/")
SEMAPHORE_SSL_VERIFY = os.environ.get("SEMAPHORE_SSL_VERIFY", "1").strip().lower() in {"1", "true", "yes", "on"}
SEMAPHORE_USE_PRIORITY = os.environ.get("SEMAPHORE_USE_PRIORITY", "0").strip().lower() in {"1", "true", "yes", "on"}
SEMAPHORE_QUERY_STRING = os.environ.get("SEMAPHORE_QUERY_STRING", "0").strip().lower() in {"1", "true", "yes", "on"}
SMS_TIMEZONE = os.environ.get("SMS_TIMEZONE", "Asia/Manila").strip() or "Asia/Manila"
# OpenStreetMap Nominatim reverse geocoding (free; identify your app via User-Agent per policy)
REVERSE_GEOCODE_ENABLED = os.environ.get("REVERSE_GEOCODE_ENABLED", "1").strip().lower() in {"1", "true", "yes", "on"}
NOMINATIM_BASE_URL = os.environ.get("NOMINATIM_BASE_URL", "https://nominatim.openstreetmap.org").strip().rstrip("/")
NOMINATIM_USER_AGENT = os.environ.get("NOMINATIM_USER_AGENT", "GuardianHelmet/1.0 (accident-alerts)").strip() or "GuardianHelmet/1.0"
REVERSE_GEOCODE_TIMEOUT = float(os.environ.get("REVERSE_GEOCODE_TIMEOUT", "8"))
REVERSE_GEOCODE_MAX_CHARS = int(os.environ.get("REVERSE_GEOCODE_MAX_CHARS", "350"))
# Approximate area from the helmet’s HTTP connection (public IP), not the GPS module
IP_GEOLOCATION_ENABLED = os.environ.get("IP_GEOLOCATION_ENABLED", "1").strip().lower() in {"1", "true", "yes", "on"}
IP_API_BASE = os.environ.get("IP_API_BASE", "http://ip-api.com").strip().rstrip("/")
# SMS place names + map: use helmet GPS from JSON (1) or only device/network IP position (0 = default).
SMS_USE_HELMET_GPS_FOR_ADDRESS = os.environ.get(
    "SMS_USE_HELMET_GPS_FOR_ADDRESS", "0"
).strip().lower() in {"1", "true", "yes", "on"}

# ESP32 (helmet) IP — set in .env or in Settings in the web app (stored in config.json)
CONFIG_JSON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


def get_esp32_ip():
    """ESP32 IP: from config.json (saved in UI) first, then env ESP32_IP."""
    try:
        if os.path.isfile(CONFIG_JSON_PATH):
            with open(CONFIG_JSON_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                ip = (data.get("esp32_ip") or "").strip()
                if ip:
                    return ip
    except Exception:
        pass
    return os.environ.get("ESP32_IP", "").strip()


def save_esp32_ip(ip):
    """Save ESP32 IP to config.json."""
    ip = (ip or "").strip()
    data = {}
    if os.path.isfile(CONFIG_JSON_PATH):
        try:
            with open(CONFIG_JSON_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            pass
    data["esp32_ip"] = ip
    with open(CONFIG_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    return ip


# MySQL config — defaults match XAMPP (localhost, root, no password)
MYSQL_CONFIG = {
    "host": os.environ.get("MYSQL_HOST", "localhost"),
    "port": int(os.environ.get("MYSQL_PORT", "3306")),
    "user": os.environ.get("MYSQL_USER", "root"),
    "password": os.environ.get("MYSQL_PASSWORD", ""),
    "database": os.environ.get("MYSQL_DATABASE", "guardian_helmet"),
    "cursorclass": pymysql.cursors.DictCursor,
    "autocommit": False,
}


def get_db():
    return pymysql.connect(**MYSQL_CONFIG)


# Routes that do not require login (ESP32 and public)
LOGIN_EXEMPT = {
    "/login", "/login/admin", "/logout",
    "/alert", "/api/ping", "/api/emergency-phones",
}


def _is_login_exempt(path):
    if path in LOGIN_EXEMPT:
        return True
    if path.startswith("/static") or path.startswith("/images"):
        return True
    if "/api/alerts/" in path and "/sos-sent" in path:
        return True
    return False


@app.before_request
def require_login():
    if _is_login_exempt(request.path):
        return None
    if session.get("user_id") or session.get("guardian_user_id"):
        role = session.get("user_role", "admin")
        guardian_only_endpoints = {"guardian_dashboard", "guardian_camera_page", "guardian_emergency_page"}
        admin_only_endpoints = {
            "index", "camera_page", "settings_page", "accounts_list", "account_create", "account_edit",
            "account_update", "account_delete", "data_logs", "data_logs_export", "reports",
            "reports_backup_alerts", "reports_backup_contacts", "reports_generate",
        }
        if role == "guardian" and request.endpoint in admin_only_endpoints:
            return redirect(url_for("guardian_dashboard"))
        if role == "admin" and request.endpoint in guardian_only_endpoints:
            return redirect(url_for("index"))
        return None
    return redirect(url_for("login_page", next=request.path))


def init_db():
    conn = pymysql.connect(
        host=MYSQL_CONFIG["host"],
        port=MYSQL_CONFIG["port"],
        user=MYSQL_CONFIG["user"],
        password=MYSQL_CONFIG["password"],
        cursorclass=pymysql.cursors.DictCursor,
    )
    conn.autocommit(True)
    with conn.cursor() as cur:
        cur.execute(f"CREATE DATABASE IF NOT EXISTS `{MYSQL_CONFIG['database']}`")
    conn.close()

    conn = get_db()
    with conn.cursor() as cur:
        cur.execute(
            """CREATE TABLE IF NOT EXISTS alerts (
                id INT AUTO_INCREMENT PRIMARY KEY,
                latitude DOUBLE,
                longitude DOUBLE,
                acceleration DOUBLE,
                tilt_x DOUBLE,
                tilt_y DOUBLE,
                timestamp VARCHAR(64),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        for col in ("acknowledged_at", "sos_sent_at"):
            try:
                cur.execute("ALTER TABLE alerts ADD COLUMN " + col + " TIMESTAMP NULL")
            except Exception:
                pass
        try:
            cur.execute("ALTER TABLE alerts ADD COLUMN vibration_triggered TINYINT(1) DEFAULT 0")
        except Exception:
            pass
        cur.execute(
            """CREATE TABLE IF NOT EXISTS emergency_contacts (
                id INT AUTO_INCREMENT PRIMARY KEY,
                name VARCHAR(128) NOT NULL,
                email VARCHAR(256),
                phone VARCHAR(64),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        cur.execute(
            """CREATE TABLE IF NOT EXISTS users (
                id INT AUTO_INCREMENT PRIMARY KEY,
                username VARCHAR(64) NOT NULL UNIQUE,
                password_hash VARCHAR(256) NOT NULL,
                email VARCHAR(128),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        # Keep one fixed admin account available at all times.
        cur.execute("SELECT id FROM users WHERE username = %s", (FIXED_ADMIN_USERNAME,))
        fixed_admin = cur.fetchone()
        fixed_admin_hash = generate_password_hash(FIXED_ADMIN_PASSWORD)
        if fixed_admin:
            cur.execute(
                "UPDATE users SET password_hash = %s WHERE id = %s",
                (fixed_admin_hash, fixed_admin["id"]),
            )
        else:
            cur.execute(
                "INSERT INTO users (username, password_hash, email) VALUES (%s, %s, %s)",
                (FIXED_ADMIN_USERNAME, fixed_admin_hash, "admin@guardian.local"),
            )
        cur.execute(
            """CREATE TABLE IF NOT EXISTS guardian_users (
                id INT AUTO_INCREMENT PRIMARY KEY,
                username VARCHAR(64) NOT NULL UNIQUE,
                password_hash VARCHAR(256) NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        cur.execute("SELECT id FROM guardian_users WHERE username = %s", ("guardian",))
        if not cur.fetchone():
            cur.execute(
                "INSERT INTO guardian_users (username, password_hash) VALUES (%s, %s)",
                ("guardian", generate_password_hash("guardian123")),
            )
    conn.commit()
    conn.close()


def _current_user():
    """Return dict with id, username for the logged-in user, or None."""
    role = session.get("user_role", "admin")
    uid = session.get("user_id") or session.get("guardian_user_id")
    if not uid:
        return None
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, username FROM users WHERE id = %s", (uid,))
            row = cur.fetchone()
            if not row and role == "guardian":
                # Backward compatibility for any legacy guardian-only account.
                cur.execute("SELECT id, username FROM guardian_users WHERE id = %s", (uid,))
                row = cur.fetchone()
            if row:
                row["role"] = role
            return row
    finally:
        conn.close()


def _unacknowledged_alert_count():
    """Number of alerts that have not been acknowledged (for nav bell indicator)."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) as n FROM alerts WHERE acknowledged_at IS NULL")
            return cur.fetchone()["n"]
    finally:
        conn.close()


@app.context_processor
def inject_device_config():
    """Make ESP32 IP, cam URL, current user, and unacknowledged alert count available in all templates."""
    return {
        "esp32_ip": get_esp32_ip(),
        "esp32_cam_stream_url": ESP32_CAM_STREAM_URL,
        "current_user": _current_user(),
        "unacknowledged_alert_count": _unacknowledged_alert_count(),
    }


def _user_count():
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) as n FROM users")
            return cur.fetchone()["n"]
    finally:
        conn.close()


@app.route("/login", methods=["GET", "POST"])
def login_page():
    """Guardian login (default login page)."""
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        if not username or not password:
            return render_template("login.html", error="Username and password required.", is_admin_login=False)
        conn = get_db()
        try:
            with conn.cursor() as cur:
                # Guardian login uses normal user accounts too, so created accounts can sign in here.
                cur.execute("SELECT id, password_hash FROM users WHERE username = %s", (username,))
                row = cur.fetchone()
                source = "users"
                if not row:
                    # Fallback for legacy guardian-only seed account.
                    cur.execute("SELECT id, password_hash FROM guardian_users WHERE username = %s", (username,))
                    row = cur.fetchone()
                    source = "guardian_users"
            if not row or not check_password_hash(row["password_hash"], password):
                return render_template("login.html", error="Invalid guardian username or password.", is_admin_login=False)
            session.clear()
            if source == "users":
                session["user_id"] = row["id"]
            else:
                session["guardian_user_id"] = row["id"]
            session["user_role"] = "guardian"
            session.permanent = True
        finally:
            conn.close()
        next_url = request.form.get("next") or request.args.get("next") or url_for("guardian_dashboard")
        if not next_url.startswith("/guardian"):
            next_url = url_for("guardian_dashboard")
        return redirect(next_url)
    return render_template("login.html", error=request.args.get("error"), is_admin_login=False)


@app.route("/login/admin", methods=["GET", "POST"])
def admin_login_page():
    """Admin login page keeps existing admin behavior."""
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        if not username or not password:
            return render_template("login.html", error="Username and password required.", has_users=_user_count() > 0, is_admin_login=True)
        if username != FIXED_ADMIN_USERNAME:
            return render_template("login.html", error="Invalid admin credentials.", has_users=_user_count() > 0, is_admin_login=True)
        conn = get_db()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT id, password_hash FROM users WHERE username = %s", (FIXED_ADMIN_USERNAME,))
                row = cur.fetchone()
                db_ok = bool(row and check_password_hash(row["password_hash"], password))
                env_ok = (password == FIXED_ADMIN_PASSWORD)
                if not (db_ok or env_ok):
                    return render_template("login.html", error="Invalid admin credentials.", has_users=_user_count() > 0, is_admin_login=True)
                # Self-heal fixed admin row/hash so future logins remain stable.
                fixed_hash = generate_password_hash(FIXED_ADMIN_PASSWORD)
                if row:
                    cur.execute("UPDATE users SET password_hash = %s WHERE id = %s", (fixed_hash, row["id"]))
                    admin_id = row["id"]
                else:
                    cur.execute(
                        "INSERT INTO users (username, password_hash, email) VALUES (%s, %s, %s)",
                        (FIXED_ADMIN_USERNAME, fixed_hash, "admin@guardian.local"),
                    )
                    admin_id = cur.lastrowid
            conn.commit()
            session.clear()
            session["user_id"] = admin_id
            session["user_role"] = "admin"
            session.permanent = True
        finally:
            conn.close()
        next_url = request.form.get("next") or request.args.get("next") or url_for("index")
        return redirect(next_url)
    return render_template("login.html", error=request.args.get("error"), has_users=_user_count() > 0, is_admin_login=True)


@app.route("/logout")
def logout_page():
    """Log out and redirect to login."""
    session.pop("user_id", None)
    session.pop("guardian_user_id", None)
    session.pop("user_role", None)
    return redirect(url_for("login_page"))


@app.route("/register", methods=["GET", "POST"])
def register_page():
    """Public registration is disabled."""
    return redirect(url_for("login_page", error="Registration is disabled. Contact admin to create an account."))


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/camera")
def camera_page():
    """Dash cam / helmet camera: live stream and status."""
    return render_template("camera.html")


@app.route("/guardian")
def guardian_dashboard():
    """Guardian dashboard."""
    return render_template("index.html", guardian_view=True)


@app.route("/guardian/camera")
def guardian_camera_page():
    """Guardian dash cam page."""
    return render_template("camera.html", guardian_view=True)


@app.route("/guardian/emergency")
def guardian_emergency_page():
    """Guardian emergency contacts page."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name, email, phone, created_at FROM emergency_contacts ORDER BY id")
            contacts = cur.fetchall()
        for c in contacts:
            c["created_at"] = str(c["created_at"]) if c.get("created_at") else ""
    finally:
        conn.close()
    return render_template("emergency.html", contacts=contacts, guardian_view=True)


@app.route("/settings", methods=["GET", "POST"])
def settings_page():
    """Configure ESP32 IP so the web app is connected to the helmet device."""
    if request.method == "POST":
        ip = (request.form.get("esp32_ip") or "").strip()
        save_esp32_ip(ip)
        return redirect(url_for("settings_page") + "?saved=1")
    return render_template(
        "settings.html",
        esp32_ip=get_esp32_ip(),
        saved=request.args.get("saved") == "1",
    )


def _normalize_phone(phone):
    """Normalize phone to digits and leading + for international."""
    if not phone:
        return None
    s = "".join(c for c in str(phone).strip() if c.isdigit() or c == "+")
    if not s:
        return None
    if not s.startswith("+"):
        s = "+" + s
    return s


def _touch_esp32_seen():
    global _last_esp32_seen
    _last_esp32_seen = datetime.now(timezone.utc)


def _get_emergency_phones():
    """Read emergency contacts with phone values and normalize them."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT phone FROM emergency_contacts WHERE phone IS NOT NULL AND phone != ''")
            rows = cur.fetchall()
    finally:
        conn.close()
    phones = []
    for r in rows:
        p = _normalize_phone(r.get("phone"))
        if p:
            phones.append(p)
    return phones


def _format_sms_phone(phone):
    """Format for iProg endpoint; expects PH mobile number, +63 or 09 accepted."""
    if not phone:
        return None
    p = str(phone).strip()
    if p.startswith("+"):
        p = p[1:]
    return p


def _get_client_ip(req):
    """IP of the device that sent the HTTP request (helmet ESP32), honoring reverse proxies."""
    if req is None:
        return None
    for key in ("X-Forwarded-For", "X-Real-IP", "CF-Connecting-IP"):
        h = req.headers.get(key)
        if h:
            return h.split(",")[0].strip()
    return (req.remote_addr or "").strip() or None


def _lookup_server_wan_geo():
    """
    City/region for this machine's public (WAN) IP — used when the helmet only appears as a LAN address.
    ip-api.com with no IP in the path returns the requester's public address.
    """
    if not IP_GEOLOCATION_ENABLED:
        return None
    url = f"{IP_API_BASE}/json/?fields=status,message,country,regionName,city,lat,lon"
    req = urlrequest.Request(
        url,
        headers={"User-Agent": NOMINATIM_USER_AGENT},
        method="GET",
    )
    try:
        with urlrequest.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="ignore"))
        if data.get("status") != "success":
            return None
        parts = [data.get("city"), data.get("regionName"), data.get("country")]
        label = ", ".join(p for p in parts if p)
        if not label:
            return None
        la = data.get("lat")
        lo = data.get("lon")
        return {
            "label": f"Approximate area (this site’s internet / home WAN): {label}",
            "lat": float(la) if la is not None else None,
            "lon": float(lo) if lo is not None else None,
        }
    except Exception:
        return None


def _lookup_ip_geo(ip):
    """
    Approximate city/region from the helmet's IP. Public IP → that location.
    Private LAN IP → fall back to this server's WAN location (same building / ISP area as typical home tests).
    """
    if not IP_GEOLOCATION_ENABLED:
        return None
    if not ip:
        return None
    try:
        a = ipaddress.ip_address(ip.split("%")[0])
        is_private = a.is_private or a.is_loopback or a.is_reserved or a.is_link_local or a.is_multicast
    except ValueError:
        return None

    if is_private:
        wan = _lookup_server_wan_geo()
        if wan and wan.get("lat") is not None and wan.get("lon") is not None:
            return {
                "label": wan["label"],
                "lat": wan["lat"],
                "lon": wan["lon"],
                "private_client": True,
                "wan_fallback": True,
                "client_ip": ip,
            }
        return {
            "label": None,
            "lat": None,
            "lon": None,
            "private_client": True,
            "wan_fallback": False,
            "client_ip": ip,
        }

    url = f"{IP_API_BASE}/json/{urlparse.quote(ip)}?fields=status,message,country,regionName,city,lat,lon"
    req = urlrequest.Request(
        url,
        headers={"User-Agent": NOMINATIM_USER_AGENT},
        method="GET",
    )
    try:
        with urlrequest.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="ignore"))
        if data.get("status") != "success":
            return None
        parts = [data.get("city"), data.get("regionName"), data.get("country")]
        label = ", ".join(p for p in parts if p)
        return {
            "label": label or None,
            "lat": data.get("lat"),
            "lon": data.get("lon"),
            "private_client": False,
            "wan_fallback": False,
            "client_ip": ip,
        }
    except Exception:
        return None


def _address_from_nominatim_payload(data):
    """
    Build a readable place name from Nominatim JSON: street/house, barangay-level,
    city, province/region, postcode, country. Falls back to display_name.
    """
    addr = data.get("address")
    if not isinstance(addr, dict):
        addr = {}

    def _add(segments, s):
        s = (s or "").strip()
        if not s:
            return
        if not segments or segments[-1].lower() != s.lower():
            segments.append(s)

    segments = []

    hn = (addr.get("house_number") or "").strip()
    road = (
        addr.get("road")
        or addr.get("pedestrian")
        or addr.get("path")
        or addr.get("residential")
        or ""
    ).strip()
    if hn and road:
        _add(segments, f"{hn} {road}")
    elif road:
        _add(segments, road)
    elif hn:
        _add(segments, hn)

    for key in ("neighbourhood", "suburb", "quarter", "village"):
        v = addr.get(key)
        if isinstance(v, str) and v.strip():
            _add(segments, v.strip())

    city_done = False
    for key in ("city_district", "town", "city", "municipality"):
        v = addr.get(key)
        if isinstance(v, str) and v.strip():
            _add(segments, v.strip())
            city_done = True
            break

    if not city_done:
        for key in ("county", "state_district"):
            v = addr.get(key)
            if isinstance(v, str) and v.strip():
                _add(segments, v.strip())
                break

    for key in ("state", "region"):
        v = addr.get(key)
        if isinstance(v, str) and v.strip():
            _add(segments, v.strip())
            break

    pc = addr.get("postcode")
    if isinstance(pc, str) and pc.strip():
        _add(segments, pc.strip())

    country = addr.get("country")
    if isinstance(country, str) and country.strip():
        _add(segments, country.strip())

    if len(segments) >= 2:
        out = ", ".join(segments)
    elif len(segments) == 1:
        out = segments[0]
    else:
        out = ""

    if not out or len(out) < 12:
        out = (data.get("display_name") or "").strip()

    if not out:
        return None
    if len(out) > REVERSE_GEOCODE_MAX_CHARS:
        out = out[: REVERSE_GEOCODE_MAX_CHARS - 1].rstrip(" ,") + "…"
    return out


def _reverse_geocode(lat, lon):
    """Return human-readable address from coordinates via OSM Nominatim, or None."""
    if not REVERSE_GEOCODE_ENABLED:
        return None
    try:
        la = float(lat)
        lo = float(lon)
    except (TypeError, ValueError):
        return None
    if abs(la) < 1e-9 and abs(lo) < 1e-9:
        return None
    q = urlparse.urlencode(
        {
            "lat": str(la),
            "lon": str(lo),
            "format": "json",
            "addressdetails": "1",
        }
    )
    url = f"{NOMINATIM_BASE_URL}/reverse?{q}"
    req = urlrequest.Request(
        url,
        headers={
            "User-Agent": NOMINATIM_USER_AGENT,
            "Accept": "application/json",
            "Accept-Language": "en",
        },
        method="GET",
    )
    try:
        with urlrequest.urlopen(req, timeout=REVERSE_GEOCODE_TIMEOUT) as resp:
            if resp.status != 200:
                return None
            data = json.loads(resp.read().decode("utf-8", errors="ignore"))
        return _address_from_nominatim_payload(data)
    except Exception:
        return None


def _format_sms_time(ts):
    """Format alert timestamp to local timezone for SMS readability."""
    try:
        tz = ZoneInfo(SMS_TIMEZONE)
    except Exception:
        tz = timezone.utc

    dt = None
    if isinstance(ts, str) and ts.strip():
        raw = ts.strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(raw)
        except Exception:
            dt = None

    if dt is None:
        dt = datetime.now(timezone.utc)
    elif dt.tzinfo is None:
        # ESP payloads are typically UTC-like but may omit timezone.
        dt = dt.replace(tzinfo=timezone.utc)

    local_dt = dt.astimezone(tz)
    return local_dt.strftime("%Y-%m-%d %I:%M:%S %p"), str(tz)


def _build_alert_sms_message(
    alert_id,
    lat,
    lon,
    accel,
    tilt_x,
    tilt_y,
    ts,
    vibration_triggered,
    place_address=None,
    gps_stale=False,
    device_area_text=None,
    map_lat=None,
    map_lon=None,
    has_gps_fix=False,
    sms_used_network_for_map=False,
):
    """
    Named address comes from Nominatim using map_lat/map_lon (network IP position by default).
    Helmet GPS (lat/lon args) is optional info only when SMS uses network for place name.
    """
    kind = "ACCIDENT ALERT" if not vibration_triggered else "VIBRATION + ACCIDENT ALERT"
    trigger = "vibration + sensor thresholds" if vibration_triggered else "sensor thresholds (acceleration/tilt)"
    mla = float(map_lat if map_lat is not None else lat)
    mlo = float(map_lon if map_lon is not None else lon)
    map_url = f"https://maps.google.com/?q={mla:.6f},{mlo:.6f}"
    time_text, tz_name = _format_sms_time(ts)

    if place_address:
        address_block = (
            "PLACE / ADDRESS (from device network location → map lookup, not the helmet GPS chip):\n"
            f"{place_address}"
        )
    elif abs(mla) < 1e-9 and abs(mlo) < 1e-9:
        address_block = (
            "PLACE / ADDRESS: unavailable — no network-based position (check IP geolocation / internet)."
        )
    else:
        address_block = (
            "PLACE / ADDRESS: named lookup failed for this position — use Lat/Lon and Map link below."
        )

    if device_area_text:
        net_line = device_area_text
    else:
        net_line = "Network / device connection: unavailable (enable IP_GEOLOCATION_ENABLED)."

    stale_line = ""
    if gps_stale and SMS_USE_HELMET_GPS_FOR_ADDRESS:
        stale_line = (
            "\nGPS note: no satellite lock at crash — map used last known helmet GPS.\n"
        )

    if SMS_USE_HELMET_GPS_FOR_ADDRESS:
        gps_extra = (
            f"Helmet GPS module: {'fix at alert' if has_gps_fix and not gps_stale else ('last known' if gps_stale else 'no fix')}\n"
        )
    else:
        if has_gps_fix or abs(float(lat)) >= 1e-9 or abs(float(lon)) >= 1e-9:
            gps_extra = (
                f"Helmet GPS reading (not used for address above): {float(lat):.6f}, {float(lon):.6f}\n"
            )
        else:
            gps_extra = "Helmet GPS reading: no fix (optional sensor only).\n"

    if not SMS_USE_HELMET_GPS_FOR_ADDRESS:
        coord_label = "Approximate Lat/Lon (device network — used for map & named address):"
    elif sms_used_network_for_map:
        coord_label = "Approximate Lat/Lon (network fallback — used for map & named address):"
    else:
        coord_label = "Lat/Lon (helmet GPS — map position):"

    return (
        "GUARDIAN HELMET ACCIDENT LOG\n"
        f"Type: {kind}\n"
        f"Alert ID: {alert_id}\n"
        f"Trigger: {trigger}\n"
        f"Time ({tz_name}): {time_text}\n"
        f"{address_block}\n"
        f"{net_line}\n"
        f"{stale_line}"
        f"{gps_extra}"
        f"{coord_label}\n"
        f"{mla:.6f}, {mlo:.6f}\n"
        f"Map: {map_url}\n"
        f"Acceleration: {accel:.2f} g\n"
        f"Tilt X: {tilt_x:.1f} deg\n"
        f"Tilt Y: {tilt_y:.1f} deg\n"
        "Action: Contact the rider immediately and verify condition."
    )


def _send_iprog_sms(phone_number, message):
    """Send one SMS via iProg SMS API."""
    if not IPROG_SMS_ENABLED:
        return False, "disabled"
    if not IPROG_SMS_API_TOKEN:
        return False, "missing_token"
    endpoint = f"{IPROG_SMS_BASE_URL}/api/v1/sms_messages"
    payload = {
        "api_token": IPROG_SMS_API_TOKEN,
        "phone_number": _format_sms_phone(phone_number),
        "message": message,
    }
    body = json.dumps(payload).encode("utf-8")
    req = urlrequest.Request(
        endpoint,
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urlrequest.urlopen(req, timeout=10) as resp:
            resp_body = resp.read().decode("utf-8", errors="ignore")
            if 200 <= resp.status < 300:
                # iProg may return HTTP 200 even when provider-level status is failed.
                # Count as success only when body clearly indicates accepted/sent state.
                try:
                    data = json.loads(resp_body) if resp_body else {}
                except Exception:
                    data = {}
                status = str(data.get("status", "")).strip().lower()
                success_flag = data.get("success")
                has_message_id = bool(data.get("message_id") or data.get("id"))
                accepted_statuses = {"ok", "success", "sent", "queued", "accepted"}
                if success_flag is True or status in accepted_statuses or has_message_id:
                    return True, resp_body or "accepted"
                return False, f"provider_rejected:{(resp_body or 'empty response')[:240]}"
            return False, f"http_{resp.status}:{resp_body[:240]}"
    except urlerror.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="ignore") if hasattr(e, "read") else str(e)
        return False, f"http_{e.code}:{err_body[:240]}"
    except Exception as e:
        return False, str(e)


def _send_semaphore_sms(phone_number, message):
    """Send one SMS via Semaphore API."""
    if not SEMAPHORE_API_KEY:
        return False, "missing_semaphore_api_key"
    endpoint = f"{SEMAPHORE_SMS_BASE_URL}/api/v4/messages"
    payload = {
        "apikey": SEMAPHORE_API_KEY,
        "number": _format_sms_phone(phone_number),
        "message": message,
    }
    if SEMAPHORE_USE_PRIORITY:
        payload["priority"] = 1

    body = None
    request_url = endpoint
    if SEMAPHORE_QUERY_STRING:
        request_url = endpoint + "?" + urlparse.urlencode(payload)
    else:
        body = urlparse.urlencode(payload).encode("utf-8")

    req = urlrequest.Request(
        request_url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        method="POST",
    )
    ssl_context = None
    if not SEMAPHORE_SSL_VERIFY:
        ssl_context = ssl._create_unverified_context()
    try:
        with urlrequest.urlopen(req, timeout=10, context=ssl_context) as resp:
            resp_body = resp.read().decode("utf-8", errors="ignore")
            if not (200 <= resp.status < 300):
                return False, f"http_{resp.status}:{resp_body[:240]}"
            try:
                data = json.loads(resp_body) if resp_body else []
            except Exception:
                data = []
            # Typical Semaphore response is a list with status like "Queued"/"Pending"/"Sent".
            item = data[0] if isinstance(data, list) and data else {}
            status = str(item.get("status", "")).strip().lower()
            accepted_statuses = {"queued", "pending", "sent", "success"}
            if status in accepted_statuses or item.get("message_id") or item.get("id"):
                return True, resp_body or "accepted"
            return False, f"provider_rejected:{(resp_body or 'empty response')[:240]}"
    except urlerror.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="ignore") if hasattr(e, "read") else str(e)
        return False, f"http_{e.code}:{err_body[:240]}"
    except Exception as e:
        return False, str(e)


def _send_alert_sms_to_contacts(
    alert_id, lat, lon, accel, tilt_x, tilt_y, ts, vibration_triggered, gps_stale=False, req=None
):
    """Send accident SMS. Named area uses helmet connection IP + map; GPS is separate."""
    phones = _get_emergency_phones()
    if not phones:
        return 0, 0, "no_contacts"
    has_gps_fix = abs(float(lat)) >= 1e-9 or abs(float(lon)) >= 1e-9
    ip_data = _lookup_ip_geo(_get_client_ip(req)) if req is not None else None
    ip_lat = ip_lon = None
    device_area_text = None
    if ip_data:
        la, lo = ip_data.get("lat"), ip_data.get("lon")
        if la is not None and lo is not None:
            try:
                ip_lat, ip_lon = float(la), float(lo)
            except (TypeError, ValueError):
                ip_lat, ip_lon = None, None
        label = ip_data.get("label")
        wan_fb = bool(ip_data.get("wan_fallback"))
        pcli = bool(ip_data.get("private_client"))
        clip = ip_data.get("client_ip") or "?"
        if label:
            if wan_fb and pcli:
                device_area_text = f"Helmet IP is private (LAN {clip}). {label}"
            else:
                device_area_text = f"Device / network area (helmet connection): {label}"
        elif pcli and not wan_fb:
            device_area_text = (
                f"Helmet on private WiFi ({clip}); WAN lookup failed — check PC internet or use GPS outdoors."
            )

    sms_used_network_for_map = False
    if SMS_USE_HELMET_GPS_FOR_ADDRESS:
        if has_gps_fix:
            mla, mlo = float(lat), float(lon)
        elif ip_lat is not None and ip_lon is not None:
            mla, mlo = ip_lat, ip_lon
            sms_used_network_for_map = True
        else:
            mla, mlo = 0.0, 0.0
    else:
        # Address + map from device/network IP only (not helmet GPS), per product setting.
        if ip_lat is not None and ip_lon is not None:
            mla, mlo = ip_lat, ip_lon
            sms_used_network_for_map = True
        else:
            mla, mlo = 0.0, 0.0

    place_address = None
    if abs(mla) >= 1e-9 or abs(mlo) >= 1e-9:
        place_address = _reverse_geocode(mla, mlo)

    msg = _build_alert_sms_message(
        alert_id,
        lat,
        lon,
        accel,
        tilt_x,
        tilt_y,
        ts,
        vibration_triggered,
        place_address=place_address,
        gps_stale=gps_stale,
        device_area_text=device_area_text,
        map_lat=mla,
        map_lon=mlo,
        has_gps_fix=has_gps_fix,
        sms_used_network_for_map=sms_used_network_for_map,
    )
    ok_count = 0
    fail_count = 0
    last_error = ""
    for p in phones:
        ok, info = _send_semaphore_sms(p, msg) if SEMAPHORE_API_KEY else _send_iprog_sms(p, msg)
        if ok:
            ok_count += 1
        else:
            fail_count += 1
            last_error = info
    return ok_count, fail_count, last_error


@app.route("/api/ping", methods=["GET"])
def api_ping():
    """Lightweight heartbeat from ESP32; updates connection status for Live/Offline indicator."""
    _touch_esp32_seen()
    return jsonify({"status": "ok"})


@app.route("/api/status")
def api_status():
    """Dashboard polls this to show Live vs Offline and nav bell dot (unacknowledged alerts)."""
    global _last_esp32_seen
    now = datetime.now(timezone.utc)
    connected = False
    if _last_esp32_seen is not None:
        delta = (now - _last_esp32_seen).total_seconds()
        connected = delta <= ESP32_CONNECTED_SEC
    return jsonify({
        "connected": connected,
        "unacknowledged_alerts": _unacknowledged_alert_count(),
    })


@app.route("/alert", methods=["POST"])
def alert():
    """1. Receive accident alerts from ESP32 and optionally send guardian SMS via iProg API."""
    _touch_esp32_seen()
    try:
        data = request.get_json(force=True, silent=True) or {}
        lat = float(data.get("latitude", 0))
        lon = float(data.get("longitude", 0))
        accel = float(data.get("acceleration", 0))
        tilt_x = float(data.get("tilt_x", 0))
        tilt_y = float(data.get("tilt_y", 0))
        ts = data.get("timestamp", datetime.now(timezone.utc).isoformat())
        vibration_triggered = bool(data.get("vibration_triggered", False))
        gps_stale = bool(data.get("gps_stale", False))
    except (TypeError, ValueError):
        return jsonify({"status": "error", "message": "Invalid payload"}), 400
    conn = get_db()
    alert_id = None
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO alerts (latitude, longitude, acceleration, tilt_x, tilt_y, timestamp, vibration_triggered)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (lat, lon, accel, tilt_x, tilt_y, ts, 1 if vibration_triggered else 0),
            )
            alert_id = cur.lastrowid
        conn.commit()
    finally:
        conn.close()
    sent_ok, sent_fail, sms_error = _send_alert_sms_to_contacts(
        alert_id=alert_id,
        lat=lat,
        lon=lon,
        accel=accel,
        tilt_x=tilt_x,
        tilt_y=tilt_y,
        ts=ts,
        vibration_triggered=vibration_triggered,
        gps_stale=gps_stale,
        req=request,
    )
    if sent_ok > 0:
        conn = get_db()
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE alerts SET sos_sent_at = CURRENT_TIMESTAMP WHERE id = %s", (alert_id,))
            conn.commit()
        finally:
            conn.close()
    return jsonify({
        "status": "ok",
        "alert_id": alert_id,
        "sms_sent": sent_ok,
        "sms_failed": sent_fail,
        "sms_error": sms_error if sent_fail else "",
    })


@app.route("/api/emergency-phones")
def api_emergency_phones():
    """Return list of emergency contact phone numbers for ESP32 to send SOS SMS via SIM800L."""
    phones = _get_emergency_phones()
    return jsonify({"phones": phones})


@app.route("/api/alerts/<int:alert_id>/sos-sent", methods=["POST"])
def alert_sos_sent(alert_id):
    """Called by ESP32 after sending SOS SMS via SIM800L; marks alert sos_sent_at."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE alerts SET sos_sent_at = CURRENT_TIMESTAMP WHERE id = %s", (alert_id,))
        conn.commit()
    finally:
        conn.close()
    return jsonify({"status": "ok"})


@app.route("/api/alerts")
def api_alerts():
    """2. View Location & Time – return recent accident logs (JSON)."""
    limit = request.args.get("limit", 50, type=int)
    limit = min(max(limit, 1), 200)
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, latitude, longitude, acceleration, tilt_x, tilt_y, timestamp, created_at,
                          acknowledged_at, sos_sent_at, vibration_triggered FROM alerts ORDER BY id DESC LIMIT %s""",
                (limit,),
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    alerts = []
    for r in rows:
        alerts.append({
            "id": r["id"],
            "latitude": r["latitude"],
            "longitude": r["longitude"],
            "acceleration": r["acceleration"],
            "tilt_x": r["tilt_x"],
            "tilt_y": r["tilt_y"],
            "timestamp": r["timestamp"],
            "created_at": str(r["created_at"]) if r.get("created_at") else None,
            "acknowledged_at": str(r["acknowledged_at"]) if r.get("acknowledged_at") else None,
            "sos_sent_at": str(r["sos_sent_at"]) if r.get("sos_sent_at") else None,
            "vibration_triggered": bool(r.get("vibration_triggered")),
        })
    return jsonify(alerts)


@app.route("/api/alerts/<int:alert_id>/acknowledge", methods=["POST"])
def alert_acknowledge(alert_id):
    """3. Acknowledge Alerts."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE alerts SET acknowledged_at = CURRENT_TIMESTAMP WHERE id = %s", (alert_id,))
        conn.commit()
    finally:
        conn.close()
    return jsonify({"status": "ok"})


@app.route("/api/alerts/<int:alert_id>/location", methods=["PATCH", "POST"])
def alert_update_location(alert_id):
    """Update alert location only when GPS had no signal (current lat,lon are 0,0). Use viewer's device location as fallback."""
    try:
        data = request.get_json(force=True, silent=True) or request.form or {}
        lat = float(data.get("latitude"))
        lon = float(data.get("longitude"))
    except (TypeError, ValueError, KeyError):
        return jsonify({"status": "error", "message": "latitude and longitude required"}), 400
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT latitude, longitude FROM alerts WHERE id = %s", (alert_id,))
            row = cur.fetchone()
        if not row:
            return jsonify({"status": "error", "message": "Alert not found"}), 404
        curr_lat = row["latitude"] if row["latitude"] is not None else 0
        curr_lon = row["longitude"] if row["longitude"] is not None else 0
        if curr_lat != 0 or curr_lon != 0:
            return jsonify({"status": "error", "message": "Location can only be replaced when GPS had no signal (0,0)"}), 400
        with conn.cursor() as cur:
            cur.execute("UPDATE alerts SET latitude = %s, longitude = %s WHERE id = %s", (lat, lon, alert_id))
        conn.commit()
    finally:
        conn.close()
    return jsonify({"status": "ok"})


@app.route("/api/alerts/reset", methods=["POST"])
def alerts_reset():
    """4. Reset Alerts – clear all alerts from the list."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM alerts")
        conn.commit()
    finally:
        conn.close()
    return jsonify({"status": "ok"})


@app.route("/emergency")
def emergency_page():
    """Emergency contacts management (for 5. SOS Auto-Send)."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name, email, phone, created_at FROM emergency_contacts ORDER BY id")
            contacts = cur.fetchall()
        for c in contacts:
            c["created_at"] = str(c["created_at"]) if c.get("created_at") else ""
    finally:
        conn.close()
    return render_template("emergency.html", contacts=contacts)


@app.route("/emergency/contacts", methods=["POST"])
def emergency_contact_add():
    """Add emergency contact (phone required for SOS SMS via GSM)."""
    name = (request.form.get("name") or "").strip()
    email = (request.form.get("email") or "").strip()
    phone = (request.form.get("phone") or "").strip()
    if not name or not phone:
        return redirect(url_for("emergency_page") + "?error=Name and phone required for SMS")
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO emergency_contacts (name, email, phone) VALUES (%s, %s, %s)",
                (name, email or None, phone or None),
            )
        conn.commit()
    finally:
        conn.close()
    return redirect(url_for("emergency_page"))


@app.route("/emergency/contacts/<int:cid>/delete", methods=["POST"])
def emergency_contact_delete(cid):
    """Delete emergency contact."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM emergency_contacts WHERE id = %s", (cid,))
        conn.commit()
    finally:
        conn.close()
    return redirect(url_for("emergency_page"))


@app.route("/emergency/test-sms", methods=["POST"])
def emergency_test_sms():
    """Send a test SMS to all emergency contacts via iProg API."""
    now_ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    msg = (
        "Guardian Helmet TEST ALERT\n"
        f"Time: {now_ts}\n"
        "This is a test SMS from your Guardian Helmet system."
    )
    phones = _get_emergency_phones()
    if not phones:
        return redirect(url_for("emergency_page") + "?error=No emergency contact numbers found.")

    ok_count = 0
    fail_count = 0
    last_error = ""
    for p in phones:
        ok, info = _send_semaphore_sms(p, msg) if SEMAPHORE_API_KEY else _send_iprog_sms(p, msg)
        if ok:
            ok_count += 1
        else:
            fail_count += 1
            last_error = info

    if ok_count > 0 and fail_count == 0:
        return redirect(url_for("emergency_page") + f"?success=Test SMS sent to {ok_count} contact(s).")
    if ok_count > 0 and fail_count > 0:
        msg = f"Test SMS sent to {ok_count} contact(s), failed for {fail_count}."
        if last_error:
            msg += f" Last error: {last_error}"
        return redirect(url_for("emergency_page") + "?success=" + urlparse.quote(msg))
    err_msg = "Test SMS failed for all contacts."
    if last_error:
        err_msg = f"{err_msg} ({last_error})"
    return redirect(url_for("emergency_page") + "?error=" + urlparse.quote(err_msg))


# --- User Account Management ---

@app.route("/accounts")
def accounts_list():
    """1. View Account - list all users."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, username, email, created_at FROM users ORDER BY id")
            users = cur.fetchall()
        for u in users:
            u["created_at"] = str(u["created_at"]) if u.get("created_at") else ""
    finally:
        conn.close()
    return render_template("accounts.html", users=users)


@app.route("/accounts/create", methods=["GET", "POST"])
def account_create():
    """3. Create Account."""
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        email = (request.form.get("email") or "").strip()
        if not username or not password:
            return render_template("accounts.html", users=_get_users(), create_error="Username and password required.")
        conn = get_db()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM users WHERE username = %s", (username,))
                if cur.fetchone():
                    return render_template("accounts.html", users=_get_users(), create_error="Username already exists.")
                cur.execute(
                    "INSERT INTO users (username, password_hash, email) VALUES (%s, %s, %s)",
                    (username, generate_password_hash(password), email or None),
                )
            conn.commit()
            return redirect(url_for("accounts_list"))
        finally:
            conn.close()
    return redirect(url_for("accounts_list"))


@app.route("/accounts/<int:user_id>/edit")
def account_edit(user_id):
    """Show update form for one user (2. Update Account)."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, username, email, created_at FROM users WHERE id = %s", (user_id,))
            edit_user = cur.fetchone()
        if not edit_user:
            return redirect(url_for("accounts_list"))
        edit_user["created_at"] = str(edit_user["created_at"]) if edit_user.get("created_at") else ""
    finally:
        conn.close()
    return render_template("accounts.html", users=_get_users(), edit_user=edit_user)


@app.route("/accounts/<int:user_id>/update", methods=["POST"])
def account_update(user_id):
    """2. Update Account."""
    username = (request.form.get("username") or "").strip()
    email = (request.form.get("email") or "").strip()
    new_password = request.form.get("new_password") or ""
    if not username:
        return redirect(url_for("accounts_list"))
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE username = %s AND id != %s", (username, user_id))
            if cur.fetchone():
                edit_user = {"id": user_id, "username": username, "email": email}
                return render_template("accounts.html", users=_get_users(), edit_user=edit_user, update_error="Username already taken.")
            if new_password:
                cur.execute(
                    "UPDATE users SET username = %s, email = %s, password_hash = %s WHERE id = %s",
                    (username, email or None, generate_password_hash(new_password), user_id),
                )
            else:
                cur.execute(
                    "UPDATE users SET username = %s, email = %s WHERE id = %s",
                    (username, email or None, user_id),
                )
        conn.commit()
        return redirect(url_for("accounts_list"))
    finally:
        conn.close()


@app.route("/accounts/<int:user_id>/delete", methods=["POST"])
def account_delete(user_id):
    """4. Delete Account."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM users WHERE id = %s", (user_id,))
        conn.commit()
    finally:
        conn.close()
    return redirect(url_for("accounts_list"))


def _get_users():
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, username, email, created_at FROM users ORDER BY id")
            rows = cur.fetchall()
        for u in rows:
            u["created_at"] = str(u["created_at"]) if u.get("created_at") else ""
        return rows
    finally:
        conn.close()


# --- Access Data Logs ---

@app.route("/data-logs")
def data_logs():
    """Access Data Logs – view all accident alerts."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, latitude, longitude, acceleration, tilt_x, tilt_y, timestamp, created_at, acknowledged_at, sos_sent_at, vibration_triggered
                   FROM alerts ORDER BY id DESC LIMIT 500"""
            )
            logs = cur.fetchall()
        for r in logs:
            for k in ("created_at", "acknowledged_at", "sos_sent_at"):
                if r.get(k):
                    r[k] = str(r[k])
            r["vibration_triggered"] = bool(r.get("vibration_triggered"))
    finally:
        conn.close()
    return render_template("data_logs.html", logs=logs)


@app.route("/data-logs/export")
def data_logs_export():
    """Export accident logs as CSV."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, latitude, longitude, acceleration, tilt_x, tilt_y, timestamp, created_at, acknowledged_at, sos_sent_at, vibration_triggered
                   FROM alerts ORDER BY id DESC"""
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    import csv
    import io
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["id", "latitude", "longitude", "acceleration", "tilt_x", "tilt_y", "timestamp", "created_at", "acknowledged_at", "sos_sent_at", "vibration_triggered"])
    for r in rows:
        w.writerow([
            r.get("id"), r.get("latitude"), r.get("longitude"), r.get("acceleration"),
            r.get("tilt_x"), r.get("tilt_y"), r.get("timestamp"),
            str(r["created_at"]) if r.get("created_at") else "",
            str(r["acknowledged_at"]) if r.get("acknowledged_at") else "",
            str(r["sos_sent_at"]) if r.get("sos_sent_at") else "",
            "yes" if r.get("vibration_triggered") else "no",
        ])
    return Response(out.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=guardian_helmet_alerts.csv"})


# --- Backup & Security / Generate System Reports ---

@app.route("/reports")
def reports():
    """Backup & Security – Generate System Reports."""
    return render_template("reports.html")


@app.route("/reports/backup/alerts")
def reports_backup_alerts():
    """Download alerts as CSV backup."""
    return data_logs_export()


@app.route("/reports/backup/contacts")
def reports_backup_contacts():
    """Download emergency contacts as CSV backup."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name, email, phone, created_at FROM emergency_contacts ORDER BY id")
            rows = cur.fetchall()
    finally:
        conn.close()
    import csv
    import io
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["id", "name", "email", "phone", "created_at"])
    for r in rows:
        w.writerow([r.get("id"), r.get("name"), r.get("email"), r.get("phone"), str(r["created_at"]) if r.get("created_at") else ""])
    return Response(out.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=guardian_helmet_contacts.csv"})


@app.route("/reports/generate")
def reports_generate():
    """Generate system report (summary + alerts)."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) as n FROM alerts")
            total_alerts = cur.fetchone()["n"]
            cur.execute("SELECT COUNT(*) as n FROM emergency_contacts")
            total_contacts = cur.fetchone()["n"]
            cur.execute("SELECT MIN(created_at) as first_at, MAX(created_at) as last_at FROM alerts")
            span = cur.fetchone()
            cur.execute(
                """SELECT id, latitude, longitude, acceleration, tilt_x, tilt_y, timestamp, created_at
                   FROM alerts ORDER BY id DESC LIMIT 500"""
            )
            alerts = cur.fetchall()
    finally:
        conn.close()
    import io
    buf = io.StringIO()
    buf.write("Guardian Helmet – System Report\n")
    buf.write("Generated: %s\n\n" % datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"))
    buf.write("Summary\n")
    buf.write("-------\n")
    buf.write("Total accident alerts: %s\n" % total_alerts)
    buf.write("Emergency contacts: %s\n" % total_contacts)
    buf.write("First alert: %s\n" % (str(span["first_at"]) if span and span.get("first_at") else "—"))
    buf.write("Last alert: %s\n\n" % (str(span["last_at"]) if span and span.get("last_at") else "—"))
    buf.write("Recent Alerts (id, lat, lon, accel, tilt_x, tilt_y, timestamp, created_at)\n")
    buf.write("--------------------------------------------------------------------------------\n")
    for r in alerts:
        buf.write("%s,%s,%s,%s,%s,%s,%s,%s\n" % (
            r.get("id"), r.get("latitude"), r.get("longitude"), r.get("acceleration"),
            r.get("tilt_x"), r.get("tilt_y"), r.get("timestamp") or "",
            str(r["created_at"]) if r.get("created_at") else "",
        ))
    return Response(buf.getvalue(), mimetype="text/plain", headers={"Content-Disposition": "attachment; filename=guardian_helmet_report.txt"})


@app.route("/api/latest")
def api_latest():
    """Return latest alert (for map)."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, latitude, longitude, timestamp FROM alerts ORDER BY id DESC LIMIT 1"
            )
            row = cur.fetchone()
    finally:
        conn.close()
    if row is None:
        return jsonify({"id": None, "latitude": None, "longitude": None, "timestamp": None})
    return jsonify({
        "id": row["id"],
        "latitude": row["latitude"],
        "longitude": row["longitude"],
        "timestamp": row["timestamp"],
    })


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000, debug=True)
