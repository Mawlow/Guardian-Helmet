# ESP32-CAM dash cam (Guardian Helmet)

Use an **ESP32-CAM** module as a helmet dash cam: live stream over WiFi. Code is in this folder.

## Quick setup (live stream)

1. **Arduino IDE**
   - Install **ESP32** board support: File → Preferences → Additional boards URL:  
     `https://raw.githubusercontent.com/espressif/arduino-esp32/gh-pages/package_esp32_index.json`  
     Then Tools → Board → Boards Manager → search “esp32” → Install.
   - Select board: Tools → Board → **ESP32 Arduino** → **AI Thinker ESP32-CAM**.

2. **Open this project’s sketch**
   - Open **`GuardianHelmet_Cam/GuardianHelmet_Cam.ino`** from the `esp32_cam` folder.

3. **Set your WiFi**
   - At the top of the sketch, set:
   ```cpp
   const char* WIFI_SSID     = "YourWiFiSSID";
   const char* WIFI_PASSWORD = "YourWiFiPassword";
   ```

4. **Upload**
   - Connect ESP32-CAM via USB‑TTL (or USB‑CAM board).  
   - Tools → Port → (your COM port) → Upload.

5. **Get the stream URL**
   - Open Serial Monitor (115200 baud). You’ll see:
   ```text
   Stream: http://192.168.x.x/stream
   ```
   - Use that URL (e.g. `http://192.168.1.100/stream`) in the server config below.

6. **Show it on the dashboard**
   - Set the stream URL when starting the Flask server, e.g.:  
     `ESP32_CAM_STREAM_URL=http://192.168.1.100/stream`  
   - The dashboard will show a “Helmet cam (live)” card.

## Wiring (ESP32-CAM AI-Thinker)

| ESP32-CAM pin | Connect to   |
|---------------|-------------|
| 5V            | 5 V (USB or external; 3.3 V may be too low) |
| GND           | GND         |
| (U0R / U0T)   | USB‑TTL for programming (RX ↔ TX crossed)   |

The camera and SD slot are on the board; no extra wiring needed for basic streaming. For **SD card recording** you can use the same CameraWebServer example’s “Start Recording” in the web UI (if your board has PSRAM/SD).

## Two devices: main ESP32 + ESP32-CAM

- **Main ESP32** (accident detection): runs `esp32/main.py` (MicroPython) – GPS, MPU6050, SW-420, GSM, POSTs alerts.
- **ESP32-CAM**: runs **`esp32_cam/GuardianHelmet_Cam/GuardianHelmet_Cam.ino`** (Arduino) – WiFi + camera; serves MJPEG at `http://<CAM_IP>/stream`.

Both join the same WiFi. The Flask server and dashboard run on your PC; the dashboard embeds the CAM stream URL you configure.

## Optional: record to SD card

If your ESP32-CAM has an SD card slot and you use a sketch that supports recording, you can save clips (e.g. loop overwriting the oldest). The official example has limited recording; for continuous dash-cam style recording you may need a third‑party sketch that writes MJPEG or AVI to SD.
