# test_sensors.py - Console-only test for MPU6050, GPS, and SW-420
# Run in Thonny; same pins as main: MPU SDA=21, SCL=22 | GPS TX=17, RX=16 | SW-420 DO=4
# When tilt + accel (or SW-420 vibration + accel) exceed thresholds, prints "ACCIDENT DETECTED!"

import time
from mpu6050 import MPU6050
from gps import GPS

# Same pins as main project
MPU_SDA = 21
MPU_SCL = 22
GPS_TX = 17   # ESP32 TX -> GPS RX
GPS_RX = 16   # ESP32 RX <- GPS TX
SW420_PIN = 4   # SW-420 DO (digital out)

# Accident detection (same as main.py)
ACCEL_THRESHOLD_G = 2.0
TILT_THRESHOLD_DEG = 60.0
DEBOUNCE_SEC = 15
SW420_ACCEL_THRESHOLD_G = 1.5  # trigger also when SW-420 vibration + accel >= this
# If True: trigger on TILT ONLY (so tilting helmet >60 deg shows "ACCIDENT DETECTED")
# If False: need accel+tilt OR (SW-420 vibration + accel)
TEST_MODE_TILT_ONLY = True


def max_tilt_deg(tilt_x, tilt_y):
    return max(abs(tilt_x), abs(tilt_y))


def main():
    print("Guardian Helmet - Sensor test (console only)")
    print("Pins: MPU6050 SDA=%d SCL=%d | GPS TX=%d RX=%d | SW-420 DO=%d" % (MPU_SDA, MPU_SCL, GPS_TX, GPS_RX, SW420_PIN))
    print("-" * 50)

    try:
        mpu = MPU6050(sda_pin=MPU_SDA, scl_pin=MPU_SCL)
        print("MPU6050 OK")
    except Exception as e:
        print("MPU6050 init failed:", e)
        mpu = None

    try:
        gps = GPS(tx_pin=GPS_TX, rx_pin=GPS_RX)
        print("GPS OK")
    except Exception as e:
        print("GPS init failed:", e)
        gps = None

    try:
        from sw420 import SW420
        sw420 = SW420(pin_num=SW420_PIN, active_high=True)
        print("SW-420 OK (pin %d)" % SW420_PIN)
    except Exception as e:
        print("SW-420 init failed:", e)
        sw420 = None

    print("Thresholds: accel>=%.1fg  tilt>=%.0f deg  debounce=%ds" % (ACCEL_THRESHOLD_G, TILT_THRESHOLD_DEG, DEBOUNCE_SEC))
    print("Test mode (tilt-only): %s" % ("ON - tilt >60 deg will trigger" if TEST_MODE_TILT_ONLY else "OFF - need accel+tilt"))
    print("-" * 50)
    print("Reading... (Ctrl+C to stop)\n")
    last_trigger_time = 0

    while True:
        # MPU6050
        accel_g = 0.0
        tilt_x = tilt_y = 0.0
        if mpu:
            try:
                ax, ay, az = mpu.read_accel()
                gx, gy, gz = mpu.read_gyro()
                tilt_x, tilt_y = mpu.get_tilt_angles()
                mag = (ax*ax + ay*ay + az*az) ** 0.5
                accel_g = mag
                print("MPU6050 | Accel(g): X=%.2f Y=%.2f Z=%.2f | Mag=%.2f" % (ax, ay, az, mag))
                print("         | Gyro(deg/s): X=%.1f Y=%.1f Z=%.1f" % (gx, gy, gz))
                print("         | Tilt: X=%.1f deg  Y=%.1f deg" % (tilt_x, tilt_y))
                # Accident check: (accel+tilt) or (SW-420 vibration + accel); or tilt-only in test mode
                tilt_deg = max_tilt_deg(tilt_x, tilt_y)
                vibration = sw420.value() if sw420 else 0
                now = time.time()
                if TEST_MODE_TILT_ONLY:
                    trigger = tilt_deg >= TILT_THRESHOLD_DEG
                else:
                    trigger = (
                        (accel_g >= ACCEL_THRESHOLD_G and tilt_deg >= TILT_THRESHOLD_DEG)
                        or (vibration and accel_g >= SW420_ACCEL_THRESHOLD_G)
                    )
                if trigger:
                    if now - last_trigger_time >= DEBOUNCE_SEC:
                        last_trigger_time = now
                        print("")
                        print(">>> ACCIDENT DETECTED! <<<")
                        print("    Accel=%.2f g  Tilt=%.1f deg  Vibration=%s" % (accel_g, tilt_deg, "YES" if vibration else "NO"))
                        if gps:
                            try:
                                lat, lon, fix = gps.get_location()
                                if fix:
                                    print("    GPS: Lat=%.6f Lon=%.6f" % (lat, lon))
                                else:
                                    print("    GPS: No fix")
                            except Exception:
                                print("    GPS: read error")
                        print("")
            except Exception as e:
                print("MPU6050 read error:", e)
        else:
            print("MPU6050 not available")

        # SW-420
        if sw420 is not None:
            try:
                vib = sw420.value()
                print("SW-420  | Vibration: %s (raw=%d)" % ("YES" if vib else "NO", vib))
            except Exception as e:
                print("SW-420 read error:", e)
        else:
            print("SW-420  | not available")

        # GPS
        if gps:
            try:
                lat, lon, fix = gps.get_location()
                if fix:
                    print("GPS     | Fix=YES  Lat=%.6f  Lon=%.6f" % (lat, lon))
                else:
                    print("GPS     | Fix=NO   (waiting for satellites)")
            except Exception as e:
                print("GPS read error:", e)
        else:
            print("GPS not available")

        print("-" * 50)
        time.sleep(2)


if __name__ == "__main__":
    main()
