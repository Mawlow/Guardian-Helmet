# sw420.py - SW-420 digital vibration/shock sensor (MicroPython, ESP32)
# Connect DO (digital out) to the GPIO below. VCC=3.3V, GND=GND.
# Some modules output HIGH when vibration detected, others LOW; set active_high accordingly.

import machine


class SW420:
    """Read SW-420 vibration sensor. value() returns 1 when vibration detected, 0 otherwise."""

    def __init__(self, pin_num=4, active_high=True, pull_down=True):
        """
        pin_num: GPIO connected to sensor DO.
        active_high: True if DO goes HIGH on vibration; False if DO goes LOW on vibration.
        pull_down: Use internal pull-down so idle state is 0 (recommended if active_high=True).
        """
        self._pin = machine.Pin(
            pin_num,
            machine.Pin.IN,
            machine.Pin.PULL_DOWN if pull_down else None,
        )
        self._active_high = active_high

    def value(self):
        """Return 1 if vibration detected, 0 otherwise."""
        raw = self._pin.value()
        return 1 if (raw == 1) == self._active_high else 0

    def vibration_detected(self):
        """Return True when vibration is detected."""
        return self.value() == 1
