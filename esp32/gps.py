# gps.py - NEO-6M (GY-GPS6MV2) UART driver for MicroPython on ESP32
# Parses NMEA sentences for latitude and longitude

from machine import UART, Pin

# UART2 on ESP32: TX=17, RX=16 (GPS TX -> ESP32 RX16, GPS RX -> ESP32 TX17)
GPS_UART_ID = 2
GPS_BAUD = 9600


class GPS:
    """Simple NEO-6M GPS parser over UART. Reads GGA for lat/lon."""

    def __init__(self, tx_pin=17, rx_pin=16, baud=GPS_BAUD):
        self.uart = UART(GPS_UART_ID, baudrate=baud, tx=Pin(tx_pin), rx=Pin(rx_pin))

    def read_line(self):
        """Read one line from GPS (NMEA sentence)."""
        if self.uart.any():
            return self.uart.readline()
        return None

    def _parse_gga(self, line):
        """
        Parse $GPGGA sentence. Returns (lat, lon, fix_ok) or (None, None, False).
        Lat/lon in decimal degrees.
        """
        try:
            line = line.decode("utf-8").strip() if isinstance(line, bytes) else line.strip()
        except Exception:
            return None, None, False
        if not line.startswith("$GPGGA"):
            return None, None, False
        parts = line.split(",")
        if len(parts) < 10:
            return None, None, False
        fix = parts[6]  # 0=no fix, 1=GPS, 2=DGPS
        if fix == "0" or fix == "":
            return None, None, False
        lat_str = parts[2]   # e.g. 1234.5678
        lat_ns = parts[3]    # N or S
        lon_str = parts[4]
        lon_ew = parts[5]
        if not lat_str or not lon_str:
            return None, None, False
        # Convert DDMM.MMMM to decimal degrees
        lat = self._nmea_to_decimal(lat_str, lat_ns == "S")
        lon = self._nmea_to_decimal(lon_str, lon_ew == "W")
        return lat, lon, True

    def _nmea_to_decimal(self, nmea_val, negate=False):
        """Convert NMEA DDMM.MMMM to decimal degrees."""
        try:
            dot = nmea_val.find(".")
            if dot < 0:
                return 0.0
            deg = int(nmea_val[: dot - 2]) if dot >= 2 else 0
            min_val = float(nmea_val[dot - 2 :])
            dec = deg + min_val / 60.0
            return -dec if negate else dec
        except Exception:
            return 0.0

    def get_location(self):
        """
        Read available UART data and try to get latest valid lat/lon from GGA.
        Returns (latitude, longitude, has_fix).
        """
        lat, lon, fix = None, None, False
        for _ in range(20):  # read up to 20 lines
            line = self.read_line()
            if line is None:
                break
            la, lo, ok = self._parse_gga(line)
            if ok:
                lat, lon, fix = la, lo, True
        return (lat, lon, fix)
