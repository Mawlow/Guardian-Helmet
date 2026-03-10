/*
 * Guardian Helmet – ESP32-CAM Dash Cam Recorder Only
 * Records JPEG frames to SD card in a loop (overwrites oldest when full).
 * Optional: live stream at http://<IP>/stream
 * Board: AI Thinker ESP32-CAM. Insert SD card for recording.
 */

#include "esp_camera.h"
#include "esp_http_server.h"
#include "WiFi.h"
#include "SD_MMC.h"
#include "FS.h"

// ============ WiFi ============
const char* WIFI_SSID     = "YourWiFiSSID";
const char* WIFI_PASSWORD = "YourWiFiPassword";

// ============ Recording ============
#define RECORD_FOLDER    "/dashcam"
#define RECORD_INTERVAL_MS 500    // ms between frames (~2 fps)
#define MAX_FILES        600     // keep last ~10 min at 2 fps; delete oldest
#define FRAME_NAME_FORMAT "/dashcam/IMG_%04d.jpg"

// ============ AI Thinker ESP32-CAM pins (OV2640) ============
#define PWDN_GPIO_NUM  32
#define RESET_GPIO_NUM -1
#define XCLK_GPIO_NUM   0
#define SIOD_GPIO_NUM  26
#define SIOC_GPIO_NUM  27
#define Y9_GPIO_NUM    35
#define Y8_GPIO_NUM    34
#define Y7_GPIO_NUM    39
#define Y6_GPIO_NUM    36
#define Y5_GPIO_NUM    21
#define Y4_GPIO_NUM    19
#define Y3_GPIO_NUM    18
#define Y2_GPIO_NUM     5
#define VSYNC_GPIO_NUM 25
#define HREF_GPIO_NUM  23
#define PCLK_GPIO_NUM  22

static httpd_handle_t stream_httpd = NULL;
#define STREAM_BOUNDARY "frame"
static const char* STREAM_CONTENT_TYPE = "multipart/x-mixed-replace; boundary=" STREAM_BOUNDARY;
static const char* STREAM_PART_HEADER = "\r\n--" STREAM_BOUNDARY "\r\nContent-Type: image/jpeg\r\nContent-Length: %u\r\n\r\n";
#define CHUNK_SIZE 1024   // send JPEG in small chunks (some browsers/httpd need this)

static bool sdOk = false;
static uint32_t frameCount = 0;
static unsigned long lastRecordTime = 0;

// ----- Stream handler (optional live view) -----
static esp_err_t stream_handler(httpd_req_t* req) {
  camera_fb_t* fb = NULL;
  char part_buf[72];
  bool firstFrame = true;
  Serial.println("Stream client connected");
  httpd_resp_set_type(req, STREAM_CONTENT_TYPE);
  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
  httpd_resp_set_hdr(req, "Cache-Control", "no-store, no-cache, must-revalidate");
  int retries = 0;
  while (true) {
    fb = esp_camera_fb_get();
    if (!fb) {
      delay(20);
      if (++retries > 250) { Serial.println("Stream: no frame, timeout"); break; }
      continue;
    }
    retries = 0;
    if (fb->len == 0) { esp_camera_fb_return(fb); continue; }
    // First frame: send leading boundary
    if (firstFrame) {
      snprintf(part_buf, sizeof(part_buf), "--" STREAM_BOUNDARY "\r\nContent-Type: image/jpeg\r\nContent-Length: %u\r\n\r\n", (unsigned int)fb->len);
      firstFrame = false;
      Serial.printf("Stream: first frame %u bytes\n", (unsigned int)fb->len);
    } else {
      snprintf(part_buf, sizeof(part_buf), STREAM_PART_HEADER, (unsigned int)fb->len);
    }
    if (httpd_resp_send_chunk(req, part_buf, strlen(part_buf)) != ESP_OK) { esp_camera_fb_return(fb); break; }
    // Send JPEG in small chunks so httpd/browser don't drop data
    size_t sent = 0;
    while (sent < fb->len) {
      size_t toSend = (fb->len - sent) < CHUNK_SIZE ? (fb->len - sent) : CHUNK_SIZE;
      if (httpd_resp_send_chunk(req, (const char*)(fb->buf + sent), toSend) != ESP_OK) {
        esp_camera_fb_return(fb);
        Serial.println("Stream: send chunk failed");
        return ESP_FAIL;
      }
      sent += toSend;
    }
    esp_camera_fb_return(fb);
    delay(66);  // ~15 fps so browser can keep up
  }
  Serial.println("Stream client disconnected");
  return ESP_OK;
}

static esp_err_t index_handler(httpd_req_t* req) {
  const char* html = "<!DOCTYPE html><html><head><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"/>"
    "<title>Dash Cam</title></head><body style=\"margin:0;background:#000;\">"
    "<img src=\"/stream\" style=\"display:block;width:100%;height:auto;\"/></body></html>";
  httpd_resp_set_type(req, "text/html");
  httpd_resp_send(req, html, strlen(html));
  return ESP_OK;
}

void startStreamServer() {
  httpd_config_t config = HTTPD_DEFAULT_CONFIG();
  config.server_port = 80;
  config.stack_size = 8192;  // larger stack for stream task
  if (httpd_start(&stream_httpd, &config) == ESP_OK) {
    httpd_uri_t stream_uri = { .uri = "/stream", .method = HTTP_GET, .handler = stream_handler };
    httpd_uri_t index_uri  = { .uri = "/",      .method = HTTP_GET, .handler = index_handler  };
    httpd_register_uri_handler(stream_httpd, &stream_uri);
    httpd_register_uri_handler(stream_httpd, &index_uri);
  }
}

// ----- Save one frame to SD (round-robin: overwrite oldest) -----
void saveFrame() {
  if (!sdOk) return;
  camera_fb_t* fb = esp_camera_fb_get();
  if (!fb || fb->len == 0) {
    if (fb) esp_camera_fb_return(fb);
    return;
  }
  int index = frameCount % MAX_FILES;  // 0..MAX_FILES-1, then overwrite
  char path[32];
  snprintf(path, sizeof(path), FRAME_NAME_FORMAT, index);
  File file = SD_MMC.open(path, FILE_WRITE);
  if (file) {
    file.write(fb->buf, fb->len);
    file.close();
    frameCount++;
    if (frameCount % 60 == 0)
      Serial.printf("Recording frame %lu -> %s\n", (unsigned long)frameCount, path);
  }
  esp_camera_fb_return(fb);
}

void setup() {
  Serial.begin(115200);
  delay(2000);  // wait for Serial Monitor (set to 115200 baud)
  Serial.println();
  Serial.println(">>> DASHCAM START <<<");
  Serial.println("Guardian Helmet - Dash Cam Recorder");
  Serial.println("Starting...");

  camera_config_t config = {};
  config.ledc_channel = LEDC_CHANNEL_0;
  config.ledc_timer   = LEDC_TIMER_0;
  config.pin_d0       = Y2_GPIO_NUM;
  config.pin_d1       = Y3_GPIO_NUM;
  config.pin_d2       = Y4_GPIO_NUM;
  config.pin_d3       = Y5_GPIO_NUM;
  config.pin_d4       = Y6_GPIO_NUM;
  config.pin_d5       = Y7_GPIO_NUM;
  config.pin_d6       = Y8_GPIO_NUM;
  config.pin_d7       = Y9_GPIO_NUM;
  config.pin_xclk     = XCLK_GPIO_NUM;
  config.pin_pclk     = PCLK_GPIO_NUM;
  config.pin_vsync    = VSYNC_GPIO_NUM;
  config.pin_href     = HREF_GPIO_NUM;
  config.pin_sccb_sda = SIOD_GPIO_NUM;
  config.pin_sccb_scl = SIOC_GPIO_NUM;
  config.pin_pwdn     = PWDN_GPIO_NUM;
  config.pin_reset    = RESET_GPIO_NUM;
  config.xclk_freq_hz = 20000000;
  config.frame_size   = FRAMESIZE_VGA;   // 640x480 - more reliable for streaming
  config.pixel_format = PIXFORMAT_JPEG;
  config.grab_mode    = CAMERA_GRAB_LATEST;
  config.fb_location  = CAMERA_FB_IN_PSRAM;
  config.jpeg_quality = 14;             // slightly lower = smaller frames, less memory
  config.fb_count     = 2;
  if (!psramFound()) {
    config.frame_size   = FRAMESIZE_VGA;
    config.fb_location  = CAMERA_FB_IN_DRAM;
    config.jpeg_quality = 16;
  }

  if (esp_camera_init(&config) != ESP_OK) {
    Serial.println("Camera init failed");
    return;
  }
  Serial.println("Camera OK");
  // Warm up camera so first stream frame is ready quickly
  for (int i = 0; i < 5; i++) {
    camera_fb_t* f = esp_camera_fb_get();
    if (f) esp_camera_fb_return(f);
    delay(80);
  }
  Serial.println("Camera warmed up");

  if (SD_MMC.begin("/sdcard", true)) {
    if (!SD_MMC.exists(RECORD_FOLDER)) {
      SD_MMC.mkdir(RECORD_FOLDER);
    }
    sdOk = true;
    Serial.println("SD OK – recording to " RECORD_FOLDER);
  } else {
    Serial.println("SD init failed – no recording (stream only)");
  }

  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  WiFi.setSleep(false);
  Serial.print("WiFi");
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }
  Serial.println("\nWiFi OK");
  startStreamServer();
  Serial.printf("Stream: http://%s/stream\n", WiFi.localIP().toString().c_str());
  Serial.println(">>> In browser use: http:// (type it) then the IP and /stream <<<");
  Serial.println("Dash cam recording started.\n");
}

void loop() {
  unsigned long now = millis();
  if (now - lastRecordTime >= (unsigned long)RECORD_INTERVAL_MS) {
    lastRecordTime = now;
    saveFrame();
  }
  delay(50);
}
