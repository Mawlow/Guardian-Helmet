# main.py - Smart Helmet Accident Detection (ESP32 + MicroPython)
# Uses MPU6050, GPS, SIM800L (GSM), SW-420 vibration. POST to Flask on accident; send SOS SMS from ESP32.
print("Guardian Helmet: start")
import time
import math
import machine

# --- Config ---
ACCEL_THRESHOLD_G = 2.0
TILT_THRESHOLD_DEG = 60.0
DEBOUNCE_SEC = 15
SERVER_BASE = "http://192.168.254.124:5000"  # no trailing slash
SERVER_URL = SERVER_BASE + "/alert"
WIFI_SSID = "GlobeAtHome_2C66D_2.4"
WIFI_PASS = "hzBWU7xP"
GSM_TX = 25
GSM_RX = 26
# SW-420 vibration sensor: DO → GPIO (use a free pin; 4 is common)
SW420_PIN = 4
# Trigger also when SW-420 detects vibration and accel is at least this (g)
SW420_ACCEL_THRESHOLD_G = 1.5

# --- Init sensors (fail gracefully so one missing part doesn't crash the board) ---
print("Guardian Helmet: init...")
mpu = None
gps = None
gsm = None
try:
    from mpu6050 import MPU6050
    mpu = MPU6050(sda_pin=21, scl_pin=22)
    print("  MPU6050 OK")
except Exception as e:
    print("  MPU6050 fail:", e)

try:
    from gps import GPS
    gps = GPS(tx_pin=17, rx_pin=16)
    print("  GPS OK")
except Exception as e:
    print("  GPS fail:", e)

try:
    from gsm import GSM
    gsm = GSM(tx_pin=GSM_TX, rx_pin=GSM_RX)
    print("  GSM OK")
except Exception as e:
    print("  GSM skip (no SIM800L?):", e)

sw420 = None
try:
    from sw420 import SW420
    sw420 = SW420(pin_num=SW420_PIN, active_high=True)
    print("  SW-420 OK")
except Exception as e:
    print("  SW-420 skip:", e)

if mpu is None:
    print("ERROR: MPU6050 required. Check wiring (SDA=21, SCL=22).")
    raise SystemExit(1)

last_trigger_time = 0
last_ping_time = 0
PING_INTERVAL_SEC = 30  # heartbeat so dashboard shows Live when ESP32 is connected


def connect_wifi():
    """Connect to WiFi using network module."""
    import network
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    if not wlan.isconnected():
        wlan.connect(WIFI_SSID, WIFI_PASS)
        for _ in range(30):
            if wlan.isconnected():
                break
            time.sleep(0.5)
    return wlan.isconnected()


def send_alert(lat, lon, accel_g, tilt_x, tilt_y, timestamp_str, vibration_triggered=False):
    """POST to server; then fetch emergency phones and send SOS SMS via SIM800L. Returns alert_id or None."""
    import urequests
    try:
        payload = {
            "latitude": lat,
            "longitude": lon,
            "acceleration": round(accel_g, 2),
            "tilt_x": round(tilt_x, 2),
            "tilt_y": round(tilt_y, 2),
            "timestamp": timestamp_str,
            "vibration_triggered": bool(vibration_triggered),
        }
        r = urequests.post(SERVER_URL, json=payload, timeout=5)
        data = r.json() if hasattr(r, "json") and r.json else {}
        r.close()
        alert_id = data.get("alert_id")
        # SOS: get emergency phone numbers from server and send SMS via GSM (SIM800L)
        try:
            r2 = urequests.get(SERVER_BASE + "/api/emergency-phones", timeout=4)
            if r2.status_code == 200:
                phones = (r2.json() or {}).get("phones") or []
                r2.close()
                msg = "SOS Guardian Helmet. Time: %s. Location: %.6f, %.6f" % (timestamp_str, lat, lon)
                if len(msg) > 160:
                    msg = msg[:157] + "..."
                if gsm is not None:
                    for phone in phones:
                        if phone:
                            try:
                                gsm.send_sms(phone, msg)
                                time.sleep(1)
                            except Exception:
                                pass
                if phones and alert_id is not None:
                    urequests.post(SERVER_BASE + "/api/alerts/%s/sos-sent" % alert_id, timeout=3)
            else:
                r2.close()
        except Exception:
            pass
        return alert_id
    except Exception:
        return None


def get_timestamp():
    """Simple timestamp string (RTC not set = 1970; use server time if needed)."""
    import time
    t = time.localtime()
    return "{:04d}-{:02d}-{:02d} {:02d}:{:02d}:{:02d}".format(
        t[0], t[1], t[2], t[3], t[4], t[5]
    )


def max_tilt_deg(tilt_x, tilt_y):
    """Max absolute tilt (degrees) from X and Y."""
    return max(abs(tilt_x), abs(tilt_y))


def send_ping():
    """Lightweight GET so server marks ESP32 as connected (dashboard Live indicator)."""
    try:
        import urequests
        r = urequests.get(SERVER_BASE + "/api/ping", timeout=3)
        r.close()
    except Exception:
        pass


def main():
    global last_trigger_time, last_ping_time
    print("WiFi...")
    if not connect_wifi():
        print("WiFi failed; check SSID/password and SERVER_URL")
    else:
        import network
        wlan = network.WLAN(network.STA_IF)
        ip = wlan.ifconfig()[0] if wlan.isconnected() else ""
        print("WiFi OK  |  ESP32 IP: %s  (use this in dashboard Settings)" % ip)
    print("Monitoring (accel>=%sg + tilt>=%s deg" % (ACCEL_THRESHOLD_G, TILT_THRESHOLD_DEG) + (" + SW-420)" if sw420 else ")") + ". Ctrl+C to stop.")
    import network
    wlan = network.WLAN(network.STA_IF)
    while True:
        try:
            accel_g = mpu.get_magnitude_accel()
            tilt_x, tilt_y = mpu.get_tilt_angles()
            tilt_deg = max_tilt_deg(tilt_x, tilt_y)
            vibration = sw420.value() if sw420 is not None else 0
            now = time.time()
            # Accident: BOTH tilt (MPU) AND vibration (SW-420) must agree (two sensors)
            tilt_trigger = accel_g >= ACCEL_THRESHOLD_G and tilt_deg >= TILT_THRESHOLD_DEG
            vib_trigger = vibration and accel_g >= SW420_ACCEL_THRESHOLD_G
            if sw420 is not None:
                is_accident = tilt_trigger and vib_trigger  # both sensors
            else:
                is_accident = tilt_trigger  # no SW-420: tilt only
            if is_accident:
                if now - last_trigger_time >= DEBOUNCE_SEC:
                    last_trigger_time = now
                    lat, lon, has_fix = (0.0, 0.0, False)
                    if gps is not None:
                        lat, lon, has_fix = gps.get_location()
                    if not has_fix:
                        lat, lon = 0.0, 0.0
                    ts = get_timestamp()
                    send_alert(lat, lon, accel_g, tilt_x, tilt_y, ts, vibration_triggered=(sw420 is not None))
            if wlan.isconnected() and (now - last_ping_time) >= PING_INTERVAL_SEC:
                last_ping_time = now
                send_ping()
        except OSError:
            pass
        except Exception as e:
            pass
        time.sleep(0.1)


if __name__ == "__main__":
    main()
