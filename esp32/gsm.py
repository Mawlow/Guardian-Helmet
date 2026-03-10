# gsm.py - SIM800L (GSM) UART driver for MicroPython on ESP32
# Sends SMS via AT commands. Use UART1 so UART2 stays for GPS.

from machine import UART, Pin

# SIM800L on UART1: ESP32 TX -> SIM800L RX, ESP32 RX <- SIM800L TX
GSM_UART_ID = 1
GSM_BAUD = 9600


class GSM:
    """Simple SIM800L driver over UART. Sends SMS only."""

    def __init__(self, tx_pin=25, rx_pin=26, baud=GSM_BAUD):
        self.uart = UART(GSM_UART_ID, baudrate=baud, tx=Pin(tx_pin), rx=Pin(rx_pin))
        self._buf = b""

    def _read_line(self, timeout_ms=500):
        import time
        t = time.ticks_ms()
        while time.ticks_diff(time.ticks_ms(), t) < timeout_ms:
            if self.uart.any():
                self._buf += self.uart.read()
            if b"\r\n" in self._buf:
                idx = self._buf.index(b"\r\n")
                line, self._buf = self._buf[:idx], self._buf[idx + 2:]
                return line.strip()
            if b"\n" in self._buf:
                idx = self._buf.index(b"\n")
                line, self._buf = self._buf[:idx], self._buf[idx + 1:]
                return line.strip()
            time.sleep_ms(10)
        return None

    def _at(self, cmd, wait_ok=True):
        self.uart.write(cmd + b"\r\n")
        while True:
            line = self._read_line(1000)
            if line is None:
                break
            if wait_ok and line == b"OK":
                return True
            if b"ERROR" in line:
                return False
        return False

    def send_sms(self, phone, message):
        """Send one SMS. phone = international format e.g. +639171234567. Returns True if sent."""
        if len(message) > 160:
            message = message[:157] + "..."
        phone = str(phone).strip()
        if not phone.startswith("+"):
            phone = "+" + "".join(c for c in phone if c.isdigit())
        try:
            if not self._at(b"AT"):
                return False
            if not self._at(b"AT+CMGF=1"):  # text mode
                return False
            self.uart.write(b'AT+CMGS="' + phone.encode() + b'"\r\n')
            import time
            time.sleep_ms(500)
            self.uart.write(message.encode() + b"\x1a")  # Ctrl+Z
            time.sleep_ms(2000)
            self._read_line(5000)
            return True
        except Exception:
            return False
