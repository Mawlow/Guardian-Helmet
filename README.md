# Smart Helmet Accident Detection System

ESP32 + MicroPython (Thonny) + MPU6050 + NEO-6M GPS + Flask dashboard.

---

## How to set up and run

### 1. Server (Flask + MySQL)

1. **Install MySQL** (e.g. [XAMPP](https://www.apachefriends.org/) – start MySQL from the control panel). Defaults: `localhost`, user `root`, no password. The app will create the database and tables on first run.
2. **Install Python 3.8+** and create a virtual environment (optional but recommended):
   ```bash
   cd "Guardian Helmet"
   python -m venv venv
   venv\Scripts\activate    # Windows
   # source venv/bin/activate   # macOS/Linux
   ```
3. **Install dependencies and run the server:**
   ```bash
   pip install -r requirements.txt
   cd server
   python app.py
   ```
4. Open **http://localhost:5000** in your browser. On first run you’ll need to **register** an account (first user only); then log in.
5. **(Optional)** Copy `server/.env.example` to `server/.env` and set:
   - `ESP32_CAM_STREAM_URL=http://YOUR_CAM_IP/stream` if you use the ESP32-CAM dash cam.
   - `MYSQL_*` only if you’re not using XAMPP defaults.
6. Note your PC’s IP (e.g. `192.168.1.100`) – the ESP32 will need it for `SERVER_BASE`.

### 2. ESP32 (helmet – accident detection)

1. **Install [Thonny](https://thonny.org/)** and flash **MicroPython** for ESP32 (Tools → Options → Interpreter → MicroPython (ESP32)).
2. **Wire the hardware** (see [Hardware wiring](#hardware-wiring-esp32) below): MPU6050 (I2C), optional NEO-6M GPS (UART2), optional SW-420 (vibration), optional SIM800L (UART1 for SMS).
3. **Copy these files onto the ESP32** (Thonny: open file → Save copy to device):
   - `esp32/mpu6050.py` → device
   - `esp32/gps.py` → device (if using GPS)
   - `esp32/gsm.py` → device (if using SIM800L for SOS SMS)
   - `esp32/main.py` → device
4. **Edit `main.py` on the device:**
   - `WIFI_SSID` = your Wi‑Fi name  
   - `WIFI_PASS` = your Wi‑Fi password  
   - `SERVER_BASE` = `http://YOUR_PC_IP:5000` (e.g. `http://192.168.1.100:5000`)
5. **Run** `main.py` from Thonny (or let it run on boot). In Serial/Thonny you’ll see “WiFi OK” and **ESP32 IP**. Optionally enter that IP in the web app **Settings** so the dashboard knows which device is linked.
6. **(Optional) SOS SMS:** Add emergency contacts with **phone numbers** on the web **Emergency** page. When an accident is detected, the ESP32 fetches that list and sends SMS via SIM800L.

### 3. ESP32-CAM dash cam (optional)

1. Flash the camera sketch: see **[esp32_cam/README.md](esp32_cam/README.md)** (Arduino IDE, ESP32-CAM board, WiFi settings).
2. Note the ESP32-CAM IP from Serial. Set in `server/.env`:  
   `ESP32_CAM_STREAM_URL=http://CAM_IP/stream`
3. Restart the Flask server. The **Dash cam** page in the web app will show the live stream.

---

## File structure

```
project/
├── esp32/
│   ├── main.py      # Main loop: sensors, accident, POST /alert, SOS SMS via GSM
│   ├── mpu6050.py   # MPU6050 I2C driver
│   ├── gps.py       # NEO-6M UART / NMEA parser
│   └── gsm.py       # SIM800L UART driver (SMS)
├── esp32_cam/
│   └── README.md   # ESP32-CAM dash cam setup (Arduino CameraWebServer)
├── server/
│   ├── app.py
│   ├── templates/
│   │   └── index.html
│   └── static/
├── requirements.txt
└── README.md
```

---

## Hardware wiring (ESP32)

### MPU6050 (GY-521)

| MPU6050 | ESP32   |
|---------|--------|
| VCC     | 3.3V    |
| GND     | GND     |
| SDA     | GPIO 21 |
| SCL     | GPIO 22 |

**Extra pins (GY-521 has 4 more):**

| Pin | Name | Use in this project |
|-----|------|----------------------|
| **AD0** (ADD) | I2C address select | Leave **unconnected** or GND → address 0x68 (default). Connect to 3.3V for 0x69. |
| **XDA** | Aux I2C data | Leave **unconnected** (used for external I2C device e.g. magnetometer). |
| **XCL** | Aux I2C clock | Leave **unconnected**. |
| **INT** | Interrupt output | Leave **unconnected**. Optional: connect to a GPIO for motion-interrupt instead of polling. |

### GPS (GY-GPS6MV2 / NEO-6M)

| GPS  | ESP32    |
|------|----------|
| VCC  | 3.3V or 5V |
| GND  | GND      |
| TX   | GPIO 16 (RX2) |
| RX   | GPIO 17 (TX2) |

**Note:** GPS TX outputs data → connect to ESP32 **RX** (GPIO16). GPS RX receives → connect to ESP32 **TX** (GPIO17).

### SW-420 (vibration / shock sensor)

| SW-420 module | ESP32     |
|---------------|-----------|
| VCC           | 3.3V      |
| GND           | GND       |
| DO (digital out) | GPIO 4 (or set `SW420_PIN` in `main.py`) |

The SW-420 adds shock/vibration detection: an alert can trigger when **(high accel + high tilt)** from the MPU6050, or when **vibration is detected** and acceleration is above a threshold. If the module is not connected, the code skips it without crashing.

---

## Wiring diagram (text)

```
                    ESP32 DevKit
        ┌─────────────────────────────────┐
        │  3.3V ─────┬────────────────────┤
        │            │   GND ───┬──────────┤
        │            │          │          │
        │  GPIO21 ───┼── SDA    │   SCL ───┼── GPIO22
        │  (I2C)     │   (MPU6050 GY-521)  │  (I2C)
        │            │          │          │
        │  GPIO16 (RX2) ◄── TX  (GPS NEO-6M)
        │  GPIO17 (TX2) ──► RX  (GPS)
        │  GPIO 4 ───────── DO   (SW-420 vibration)
        └─────────────────────────────────┘

  MPU6050 GY-521          GY-GPS6MV2 (NEO-6M)    SW-420
  ┌──────────┐            ┌──────────┐            ┌──────┐
  │ VCC SDA  │            │ VCC  TX  │            │ VCC  │
  │ GND SCL  │            │ GND  RX  │            │ GND DO│
  └──────────┘            └──────────┘            └──────┘
```

---

## Step 1: MySQL and Flask server (PC)

**Using XAMPP:** Start **MySQL** from the XAMPP Control Panel. The app uses default XAMPP MySQL: `localhost`, user `root`, no password. No extra setup needed.

1. Install Python 3.8+ and MySQL (XAMPP, MySQL Server, or MariaDB).
2. The app creates the database `guardian_helmet` and the `alerts` table on first run.
3. (Optional) Override via environment variables: `MYSQL_HOST`, `MYSQL_PORT` (3306), `MYSQL_USER`, `MYSQL_PASSWORD`, `MYSQL_DATABASE`.
4. In project folder:
   ```bash
   pip install -r requirements.txt
   cd server
   python app.py
   ```
5. Server runs at `http://0.0.0.0:5000`. Open `http://localhost:5000` in browser.
6. Note your PC’s IP (e.g. `192.168.1.100`) for the ESP32.
7. **Connect the web app to the ESP32:** In the dashboard, go to **Settings** and enter the **Helmet ESP32 IP address** (the IP shown in Serial/Thonny when the ESP32 connects to WiFi). The dashboard will show this IP so you know which device is linked. The ESP32 must have `SERVER_URL` in `main.py` set to this server’s address (e.g. `http://YOUR_PC_IP:5000/alert`).

---

## Step 2: ESP32 MicroPython (Thonny)

1. Install [Thonny](https://thonny.org/).
2. Install MicroPython for ESP32:
   - Tools → Options → Interpreter → select “MicroPython (ESP32)”.
   - Install or pick correct firmware/port.
3. Copy to ESP32 (in Thonny):
   - `esp32/mpu6050.py` → save as `mpu6050.py` on device.
   - `esp32/gps.py` → save as `gps.py` on device.
   - `esp32/main.py` → save as `main.py` on device.
4. In `main.py` on the ESP32 set:
   - `WIFI_SSID` = your Wi‑Fi name
   - `WIFI_PASS` = your Wi‑Fi password
   - `SERVER_URL` = `http://YOUR_PC_IP:5000/alert` (e.g. `http://192.168.1.100:5000/alert`)
5. Optional: install `urequests` on ESP32 (MicroPython) if not in firmware:
   - [micropython-urequests](https://github.com/micropython/micropython-lib/tree/master/micropython/urequests) or use Thonny’s package manager.
6. Run `main.py` from Thonny (or restart ESP32 so it runs on boot).

---

## Accident detection logic

- **Trigger:** `acceleration magnitude > 2g` **and** `max(|tilt_x|, |tilt_y|) > 60°`.
- **Debounce:** 15 seconds after a trigger before the next one.
- On trigger: read GPS (lat/lon), then HTTP POST to `/alert` with latitude, longitude, acceleration, tilt_x, tilt_y, timestamp.

---

## Dashboard

- **POST /alert** — receives JSON from ESP32, stores in MySQL, triggers SOS SMS via GSM (if configured).
- **GET /** — dashboard: latest location (Leaflet map), accident logs, acknowledge/reset, emergency contacts.
- **Settings** — set the **Helmet ESP32 IP** so the web app is linked to your device; the IP is shown in the nav and on the dashboard when set.

---

## SOS SMS via GSM (SIM800L) on ESP32

SOS is sent as **SMS only** from the **ESP32** using a **SIM800L** connected to the helmet (Thonny/MicroPython).
 
1. **Hardware:** Connect SIM800L to the ESP32 on **UART1** (see wiring below). Insert SIM with SMS credit.
2. **ESP32:** Upload `gsm.py` and use the pins in `main.py` (default TX=25, RX=26). On accident, the ESP32 POSTs to the server, then GETs `/api/emergency-phones`, then sends one SMS per number via the SIM800L.
3. **Emergency contacts:** Add contacts with **phone numbers** on the web Emergency page. The ESP32 fetches this list when an accident is triggered.

### SIM800L wiring (ESP32)

| SIM800L | ESP32   |
|---------|--------|
| VCC     | 3.3V–4.2V (check module; some need 4V) |
| GND     | GND    |
| TX      | GPIO 26 (RX1) |
| RX      | GPIO 25 (TX1) |

---

## Troubleshooting

- **GPS no fix:** Ensure antenna has sky view; wait 1–2 minutes for cold start.
- **No alerts in dashboard:** Check SERVER_URL, WiFi, and that Flask is running and reachable from the ESP32 (firewall/port 5000).
- **MPU6050 errors:** Check I2C wiring (SDA/SCL, 3.3V, GND) and that only one I2C device or correct address (0x68) is used.
- **SOS SMS not sent:** SIM800L must be on the ESP32 (UART1: TX=25, RX=26). Check SIM has signal and SMS credit; ensure emergency contacts have phone numbers on the web; ESP32 must have WiFi to fetch `/api/emergency-phones`.

---

## ESP32-CAM dash cam (optional)

You can add an **ESP32-CAM** as a helmet dash cam for live view and recording.

1. **Setup the ESP32-CAM** with the project sketch: see **[esp32_cam/README.md](esp32_cam/README.md)**. In short: Arduino IDE → ESP32 board support → open **`esp32_cam/GuardianHelmet_Cam/GuardianHelmet_Cam.ino`** → set WiFi SSID/password at the top → upload to **AI Thinker ESP32-CAM** → note the device IP from Serial Monitor.
2. **Stream URL:** The MJPEG stream is at `http://<CAM_IP>/stream` (e.g. `http://192.168.1.100/stream`).
3. **Show on dashboard:** Set the stream URL so the Flask dashboard can show the live feed:
   - **Option A (recommended):** In the `server` folder, copy `.env.example` to `.env` and set:
     ```bash
     ESP32_CAM_STREAM_URL=http://192.168.254.184/stream
     ```
     (Use your ESP32-CAM’s actual IP from Serial.) Then run `python app.py` from `server` — no need to set the variable each time.
   - **Option B:** Set the env var when starting the server, e.g. PowerShell:
     ```powershell
     $env:ESP32_CAM_STREAM_URL="http://192.168.254.184/stream"; python app.py
     ```
4. The **Dashboard** (home) and **Dash cam** page will show the live stream. Main ESP32 (accident detection) and ESP32-CAM are separate devices on the same WiFi.
