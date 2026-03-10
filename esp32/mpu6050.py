# mpu6050.py - MPU6050 (GY-521) I2C driver for MicroPython on ESP32
# Reads accelerometer and gyroscope; computes tilt angle (X, Y axis)

from machine import I2C, Pin

# MPU6050 I2C address: 0x68 when AD0 is GND/unconnected; use 0x69 if AD0 is tied to 3.3V
MPU6050_ADDR = 0x68

# Registers
REG_PWR_MGMT_1 = 0x6B
REG_ACCEL_XOUT_H = 0x3B
REG_GYRO_XOUT_H = 0x43

# Scale factors (raw to g and deg/s)
ACCEL_SCALE = 16384.0   # ±2g
GYRO_SCALE = 131.0     # ±250 deg/s


class MPU6050:
    """Driver for MPU6050 accelerometer/gyroscope over I2C."""

    def __init__(self, sda_pin=21, scl_pin=22, i2c_id=0):
        self.i2c = I2C(i2c_id, sda=Pin(sda_pin), scl=Pin(scl_pin), freq=400000)
        self._wake()

    def _wake(self):
        """Wake up MPU6050 (exit sleep mode)."""
        self.i2c.writeto_mem(MPU6050_ADDR, REG_PWR_MGMT_1, bytes([0]))

    def _read_raw(self, reg, length=6):
        """Read raw bytes from register."""
        return self.i2c.readfrom_mem(MPU6050_ADDR, reg, length)

    def _to_signed16(self, high, low):
        """Convert high/low bytes to signed 16-bit."""
        val = (high << 8) | low
        return val if val < 32768 else val - 65536

    def read_accel(self):
        """Return acceleration in g: (ax, ay, az)."""
        data = self._read_raw(REG_ACCEL_XOUT_H, 6)
        ax = self._to_signed16(data[0], data[1]) / ACCEL_SCALE
        ay = self._to_signed16(data[2], data[3]) / ACCEL_SCALE
        az = self._to_signed16(data[4], data[5]) / ACCEL_SCALE
        return (ax, ay, az)

    def read_gyro(self):
        """Return angular velocity in deg/s: (gx, gy, gz)."""
        data = self._read_raw(REG_GYRO_XOUT_H, 6)
        gx = self._to_signed16(data[0], data[1]) / GYRO_SCALE
        gy = self._to_signed16(data[2], data[3]) / GYRO_SCALE
        gz = self._to_signed16(data[4], data[5]) / GYRO_SCALE
        return (gx, gy, gz)

    def read_all(self):
        """Return (accel_g, gyro_deg_s)."""
        return self.read_accel(), self.read_gyro()

    def get_tilt_angles(self):
        """
        Compute tilt angles in degrees from accelerometer (X and Y axis).
        Returns (tilt_x_deg, tilt_y_deg).
        """
        ax, ay, az = self.read_accel()
        import math
        # Tilt X: rotation around X (pitch); tilt Y: rotation around Y (roll)
        tilt_x = math.atan2(ay, (ax * ax + az * az) ** 0.5)
        tilt_y = math.atan2(-ax, (ay * ay + az * az) ** 0.5)
        tilt_x_deg = math.degrees(tilt_x)
        tilt_y_deg = math.degrees(tilt_y)
        return (tilt_x_deg, tilt_y_deg)

    def get_magnitude_accel(self):
        """Return magnitude of acceleration in g (for sudden impact)."""
        ax, ay, az = self.read_accel()
        import math
        return math.sqrt(ax * ax + ay * ay + az * az)
