# app.py - Flask server for Smart Helmet accident alerts (MySQL backend)

import json
import os
from datetime import datetime

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
    "/login", "/logout", "/register",
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
    if session.get("user_id"):
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
    conn.commit()
    conn.close()


def _current_user():
    """Return dict with id, username for the logged-in user, or None."""
    uid = session.get("user_id")
    if not uid:
        return None
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, username FROM users WHERE id = %s", (uid,))
            return cur.fetchone()
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
    """Log in with username and password."""
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        if not username or not password:
            return render_template("login.html", error="Username and password required.", has_users=_user_count() > 0)
        conn = get_db()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT id, password_hash FROM users WHERE username = %s", (username,))
                row = cur.fetchone()
            if not row or not check_password_hash(row["password_hash"], password):
                return render_template("login.html", error="Invalid username or password.", has_users=_user_count() > 0)
            session["user_id"] = row["id"]
            session.permanent = True
        finally:
            conn.close()
        next_url = request.args.get("next") or url_for("index")
        return redirect(next_url)
    return render_template("login.html", error=request.args.get("error"), has_users=_user_count() > 0)


@app.route("/logout")
def logout_page():
    """Log out and redirect to login."""
    session.pop("user_id", None)
    return redirect(url_for("login_page"))


@app.route("/register", methods=["GET", "POST"])
def register_page():
    """Allow any visitor to create an account (open registration)."""
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        email = (request.form.get("email") or "").strip()
        if not username or not password:
            return render_template("login.html", register=True, has_users=_user_count() > 0, error="Username and password required.")
        conn = get_db()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM users WHERE username = %s", (username,))
                if cur.fetchone():
                    return render_template("login.html", register=True, has_users=_user_count() > 0, error="Username already taken. Choose another.")
                cur.execute("INSERT INTO users (username, password_hash, email) VALUES (%s, %s, %s)",
                            (username, generate_password_hash(password), email or None))
                uid = cur.lastrowid
            conn.commit()
            session["user_id"] = uid
            session.permanent = True
        finally:
            conn.close()
        return redirect(url_for("index"))
    return render_template("login.html", register=True, has_users=_user_count() > 0)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/camera")
def camera_page():
    """Dash cam / helmet camera: live stream and status."""
    return render_template("camera.html")


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
    _last_esp32_seen = datetime.utcnow()


@app.route("/api/ping", methods=["GET"])
def api_ping():
    """Lightweight heartbeat from ESP32; updates connection status for Live/Offline indicator."""
    _touch_esp32_seen()
    return jsonify({"status": "ok"})


@app.route("/api/status")
def api_status():
    """Dashboard polls this to show Live vs Offline and nav bell dot (unacknowledged alerts)."""
    global _last_esp32_seen
    now = datetime.utcnow()
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
    """1. Receive Accident Alerts from ESP32. SOS SMS is sent by ESP32 via SIM800L (see /api/emergency-phones)."""
    _touch_esp32_seen()
    try:
        data = request.get_json(force=True, silent=True) or {}
        lat = float(data.get("latitude", 0))
        lon = float(data.get("longitude", 0))
        accel = float(data.get("acceleration", 0))
        tilt_x = float(data.get("tilt_x", 0))
        tilt_y = float(data.get("tilt_y", 0))
        ts = data.get("timestamp", datetime.utcnow().isoformat())
        vibration_triggered = bool(data.get("vibration_triggered", False))
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
    return jsonify({"status": "ok", "alert_id": alert_id})


@app.route("/api/emergency-phones")
def api_emergency_phones():
    """Return list of emergency contact phone numbers for ESP32 to send SOS SMS via SIM800L."""
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
