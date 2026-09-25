#include "hub.h"

#include <Arduino.h>
#include <ArduinoJson.h>
#include <Update.h>
#include <WebSocketsClient.h>
#include <WiFi.h>
#include <esp_heap_caps.h>
#include <esp_ota_ops.h>
#include <freertos/ringbuf.h>
#include <mbedtls/sha256.h>

#include "board.h"
#include "ca.h"
#include "codec.h"
#include "lights.h"
#include "settings.h"

#ifndef FW_VERSION
#define FW_VERSION "dev"
#endif

extern bool node_muted;  // main.cpp owns the privacy switch

static WebSocketsClient ws;
static bool connected = false;
static bool adopted = false;
static bool was_adopted = false;
static uint32_t last_status = 0;

// ---- audio ---------------------------------------------------------------

static const size_t MIC_FRAMES = MIC_RATE / 50;  // 20 ms per packet
static const size_t MIC_PACKET = FRAME_HEADER + MIC_FRAMES * MIC_CHANNELS * 2;
static RingbufHandle_t mic_rb;  // packets from the capture task to loop()
static RingbufHandle_t spk_rb;  // PCM from the hub to the playback task
static volatile uint32_t mic_dropped = 0, spk_dropped = 0;
static volatile uint32_t spk_last_audio = 0;

static bool mic_live() {
  return connected && adopted && settings.mic_enabled && !node_muted;
}

static void mic_task(void *) {
  static uint8_t pkt[MIC_PACKET];
  uint32_t seq = 0;
  for (;;) {
    size_t n = mic_read((int16_t *)(pkt + FRAME_HEADER), MIC_FRAMES);
    int64_t t_end = esp_timer_get_time();
    if (!mic_live()) continue;  // keep draining DMA so the clock stays steady
    int64_t t0 = t_end - (int64_t)n * 1000000 / MIC_RATE;
    pkt[0] = FRAME_MIC;
    pkt[1] = 0;
    pkt[2] = MIC_CHANNELS;
    pkt[3] = 0;
    memcpy(pkt + 4, &seq, 4);
    memcpy(pkt + 8, &t0, 8);
    seq++;
    if (xRingbufferSend(mic_rb, pkt, FRAME_HEADER + n * MIC_CHANNELS * 2, 0) != pdTRUE) mic_dropped++;
  }
}

static void spk_task(void *) {
  bool amp = false;
  for (;;) {
    size_t len = 0;
    int16_t *pcm = (int16_t *)xRingbufferReceiveUpTo(spk_rb, &len, pdMS_TO_TICKS(20), 960);
    if (pcm) {
      if (!amp && settings.speaker_enabled) spk_amp(amp = true);
      spk_write(pcm, len / 2, 100);
      vRingbufferReturnItem(spk_rb, pcm);
      spk_last_audio = millis();
    } else if (amp && millis() - spk_last_audio > 3000) {
      spk_amp(amp = false);  // amplifier hiss is audible; only power it while playing
    }
  }
}

// ---- firmware update over the socket ------------------------------------

static struct {
  bool active = false;
  uint32_t size = 0, offset = 0, last_pct = 0, last_rx = 0;
  String sha256, version;
  mbedtls_sha256_context sha;
} ota;

static void send_json(JsonDocument &doc) {
  String s;
  serializeJson(doc, s);
  ws.sendTXT(s);
}

static void ota_report(const char *state, const char *error = nullptr) {
  JsonDocument d;
  d["type"] = "ota";
  d["state"] = state;
  d["version"] = ota.version;
  if (ota.size) d["pct"] = ota.offset * 100 / ota.size;
  if (error) d["error"] = error;
  send_json(d);
}

static void ota_abort(const char *why) {
  ota_report("failed", why);
  Update.abort();
  mbedtls_sha256_free(&ota.sha);
  ota.active = false;
  lights_ota(-1);
}

static void ota_request_next() {
  JsonDocument d;
  d["type"] = "ota_next";
  d["offset"] = ota.offset;
  send_json(d);
  ota.last_rx = millis();
}

static void ota_start(JsonDocument &msg) {
  if (ota.active) return;
  ota.size = msg["size"] | 0;
  ota.sha256 = (const char *)(msg["sha256"] | "");
  ota.version = (const char *)(msg["version"] | "");
  ota.offset = 0;
  ota.last_pct = 0;
  if (!ota.size || ota.sha256.length() != 64) {
    ota_report("failed", "bad request");
    return;
  }
  if (!Update.begin(ota.size, U_FLASH)) {
    ota_report("failed", Update.errorString());
    return;
  }
  mbedtls_sha256_init(&ota.sha);
  mbedtls_sha256_starts(&ota.sha, 0);
  ota.active = true;
  lights_ota(0);
  ota_report("started");
  ota_request_next();
}

static void ota_chunk(const uint8_t *data, size_t len) {
  if (!ota.active || len < 8) return;
  uint32_t offset;
  memcpy(&offset, data + 4, 4);
  if (offset != ota.offset) return;  // stale or duplicate
  data += 8;
  len -= 8;
  if (Update.write((uint8_t *)data, len) != len) return ota_abort(Update.errorString());
  mbedtls_sha256_update(&ota.sha, data, len);
  ota.offset += len;
  uint32_t pct = (uint64_t)ota.offset * 100 / ota.size;
  lights_ota(pct);
  if (pct >= ota.last_pct + 10) {
    ota.last_pct = pct;
    ota_report("progress");
  }
  if (ota.offset < ota.size) return ota_request_next();

  uint8_t digest[32];
  mbedtls_sha256_finish(&ota.sha, digest);
  mbedtls_sha256_free(&ota.sha);
  char hex[65];
  for (int i = 0; i < 32; i++) sprintf(hex + 2 * i, "%02x", digest[i]);
  if (ota.sha256 != hex) return ota_abort("sha256 mismatch");
  if (!Update.end(true)) return ota_abort(Update.errorString());
  ota_report("rebooting");
  ws.loop();
  delay(300);
  ESP.restart();  // boots the new image in PENDING_VERIFY; see verify_ota()
}

// The new image proves itself by reaching the hub. Until then the bootloader
// holds the old one, and a crash or a timeout rolls back to it.
static bool ota_pending_verify() {
  esp_ota_img_states_t st;
  return esp_ota_get_state_partition(esp_ota_get_running_partition(), &st) == ESP_OK &&
         st == ESP_OTA_IMG_PENDING_VERIFY;
}

static void verify_ota() {
  if (!ota_pending_verify()) return;
  esp_ota_mark_app_valid_cancel_rollback();
  ota.version = FW_VERSION;
  ota_report("verified");
}

// ---- messages ------------------------------------------------------------

static void send_hello() {
  JsonDocument d;
  d["type"] = "hello";
  d["id"] = WiFi.macAddress();
  d["model"] = BOARD_MODEL;
  d["fw"] = FW_VERSION;
  d["token"] = settings.token;
  d["name"] = settings.name;
  d["reset_reason"] = (int)esp_reset_reason();
  d["ota_pending"] = ota_pending_verify();
  JsonObject caps = d["caps"].to<JsonObject>();
  JsonObject mic = caps["mic"].to<JsonObject>();
  mic["rate"] = MIC_RATE;
  mic["channels"] = MIC_CHANNELS;
  mic["format"] = "s16le";
  JsonObject spk = caps["speaker"].to<JsonObject>();
  spk["rate"] = SPK_RATE;
  spk["channels"] = 1;
  spk["format"] = "s16le";
  caps["lights"] = LED_COUNT;
  JsonArray b = caps["buttons"].to<JsonArray>();
  for (const char *n : {"vol_up", "vol_down", "set", "play", "mode", "rec"}) b.add(n);
  send_json(d);
}

void hub_send_status() {
  if (!connected) return;
  JsonDocument d;
  d["type"] = "status";
  d["uptime_s"] = millis() / 1000;
  d["rssi"] = WiFi.RSSI();
  d["heap"] = ESP.getFreeHeap();
  d["psram"] = ESP.getFreePsram();
  d["muted"] = node_muted;
  d["volume"] = settings.volume;
  d["mic_enabled"] = settings.mic_enabled;
  d["mic_gain_db"] = settings.mic_gain_db;
  d["speaker_enabled"] = settings.speaker_enabled;
  d["lights_enabled"] = settings.lights_enabled;
  d["mic_dropped"] = mic_dropped;
  d["spk_dropped"] = spk_dropped;
  UBaseType_t free_bytes = xRingbufferGetCurFreeSize(spk_rb);
  d["spk_buffered_ms"] = (96 * 1024 - free_bytes) / (SPK_RATE * 2 / 1000);
  send_json(d);
  last_status = millis();
}

void hub_send_button(const char *name, const char *action, uint32_t held_ms) {
  if (!connected) return;
  JsonDocument d;
  d["type"] = "button";
  d["button"] = name;
  d["action"] = action;
  if (held_ms) d["held_ms"] = held_ms;
  send_json(d);
}

static void apply_config(JsonVariantConst c) {
  if (c["volume"].is<int>()) spk_set_volume(settings.volume = c["volume"]);
  if (c["mic_gain_db"].is<float>()) mic_set_gain_db(settings.mic_gain_db = c["mic_gain_db"]);
  if (c["mic_enabled"].is<bool>()) settings.mic_enabled = c["mic_enabled"];
  if (c["speaker_enabled"].is<bool>()) {
    settings.speaker_enabled = c["speaker_enabled"];
    if (!settings.speaker_enabled) spk_amp(false);
  }
  if (c["local_volume_buttons"].is<bool>()) settings.local_volume_buttons = c["local_volume_buttons"];
  if (c["lights_enabled"].is<bool>()) lights_dark(!(settings.lights_enabled = c["lights_enabled"]));
  if (c["name"].is<const char *>()) settings.name = (const char *)c["name"];
  settings_save();
  hub_send_status();
}

static Mode parse_mode(const char *m) {
  if (!m) return Mode::Off;
  if (!strcmp(m, "solid")) return Mode::Solid;
  if (!strcmp(m, "pulse")) return Mode::Pulse;
  if (!strcmp(m, "spin")) return Mode::Spin;
  if (!strcmp(m, "pixels")) return Mode::Pixels;
  return Mode::Off;
}

static void on_text(const char *text, size_t len) {
  JsonDocument msg;
  if (deserializeJson(msg, text, len)) return;
  const char *type = msg["type"] | "";

  if (!strcmp(type, "welcome")) {
    adopted = was_adopted = true;
    if (msg["name"].is<const char *>()) settings.name = (const char *)msg["name"];
    apply_config(msg["config"]);
    lights_status(Status::Ready);
    verify_ota();
  } else if (!strcmp(type, "pending")) {
    adopted = false;
    lights_status(Status::Pending);
    verify_ota();
  } else if (!strcmp(type, "adopt")) {
    settings.token = (const char *)(msg["token"] | "");
    if (msg["name"].is<const char *>()) settings.name = (const char *)msg["name"];
    settings_save();
    send_hello();  // the hub answers with welcome and the config
  } else if (!strcmp(type, "config")) {
    apply_config(msg.as<JsonVariantConst>());
  } else if (!strcmp(type, "lights")) {
    uint8_t px[LED_COUNT * 3] = {};
    JsonArrayConst p = msg["pixels"];
    for (size_t i = 0; i < LED_COUNT && i < p.size(); i++)
      for (int k = 0; k < 3; k++) px[3 * i + k] = p[i][k] | 0;
    lights_hub(parse_mode(msg["mode"]), msg["color"][0] | 0, msg["color"][1] | 0, msg["color"][2] | 0,
               msg["brightness"] | 64, p.size() ? px : nullptr);
  } else if (!strcmp(type, "identify")) {
    lights_identify((msg["seconds"] | 5) * 1000);
  } else if (!strcmp(type, "reboot")) {
    ESP.restart();
  } else if (!strcmp(type, "forget")) {
    settings.token = "";
    settings.name = "";
    settings_save();
    adopted = was_adopted = false;
    lights_status(Status::Pending);
    send_hello();
  } else if (!strcmp(type, "set_hub")) {  // move to another hub, like UniFi's set-inform
    const char *url = msg["url"] | "";
    if (!strncmp(url, "ws://", 5) || !strncmp(url, "wss://", 6)) {
      settings.hub = url;
      settings_save();
      delay(200);
      ESP.restart();
    }
  } else if (!strcmp(type, "ota")) {
    ota_start(msg);
  } else if (!strcmp(type, "flush")) {  // drop queued speaker audio (barge-in)
    size_t len;
    void *item;
    while ((item = xRingbufferReceiveUpTo(spk_rb, &len, 0, 96 * 1024))) vRingbufferReturnItem(spk_rb, item);
  }
}

static void on_event(WStype_t type, uint8_t *payload, size_t len) {
  switch (type) {
    case WStype_CONNECTED:
      connected = true;
      lights_status(Status::Connecting);
      send_hello();
      break;
    case WStype_DISCONNECTED:
      connected = adopted = false;
      if (ota.active) ota_abort("disconnected");
      lights_status(was_adopted ? Status::HubLost : Status::Connecting);
      break;
    case WStype_TEXT:
      on_text((const char *)payload, len);
      break;
    case WStype_BIN:
      if (!len) break;
      if (payload[0] == FRAME_SPEAKER && len > FRAME_HEADER && adopted) {
        if (xRingbufferSend(spk_rb, payload + FRAME_HEADER, len - FRAME_HEADER, 0) != pdTRUE) spk_dropped++;
      } else if (payload[0] == FRAME_FIRMWARE && adopted) {
        ota_chunk(payload, len);
      }
      break;
    default:
      break;
  }
}

// ---- lifecycle -----------------------------------------------------------

static bool parse_hub(const String &url, bool *tls, String *host, uint16_t *port) {
  String u = url;
  u.trim();
  *tls = u.startsWith("wss://");
  if (*tls) u = u.substring(6);
  else if (u.startsWith("ws://")) u = u.substring(5);
  int slash = u.indexOf('/');
  if (slash >= 0) u = u.substring(0, slash);
  int colon = u.lastIndexOf(':');
  *host = colon >= 0 ? u.substring(0, colon) : u;
  *port = colon >= 0 ? u.substring(colon + 1).toInt() : (*tls ? 443 : 80);
  return host->length() > 0 && *port > 0;
}

void hub_begin() {
  mic_rb = xRingbufferCreate(16 * 1024, RINGBUF_TYPE_NOSPLIT);
  static StaticRingbuffer_t spk_rb_struct;
  uint8_t *spk_mem = (uint8_t *)heap_caps_malloc(96 * 1024, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
  spk_rb = spk_mem ? xRingbufferCreateStatic(96 * 1024, RINGBUF_TYPE_BYTEBUF, spk_mem, &spk_rb_struct)
                   : xRingbufferCreate(32 * 1024, RINGBUF_TYPE_BYTEBUF);
  xTaskCreatePinnedToCore(mic_task, "mic", 4096, nullptr, 5, nullptr, 1);
  xTaskCreatePinnedToCore(spk_task, "spk", 4096, nullptr, 5, nullptr, 1);

  bool tls;
  String host;
  uint16_t port;
  if (!parse_hub(settings.hub, &tls, &host, &port)) return;
#ifndef DEV_HUB
  // Release builds speak TLS only: the adoption token and the microphones
  // never cross the network in clear. A plain ws:// hub is for development
  // builds (DEV_HUB), on a machine the developer controls.
  if (!tls) {
    lights_status(Status::Portal);
    return;
  }
#endif
  if (tls) ws.beginSslWithCA(host.c_str(), port, "/nodes/ws", CA_BUNDLE, "");
  else ws.begin(host.c_str(), port, "/nodes/ws", "");
  ws.onEvent(on_event);
  ws.setReconnectInterval(3000);
  ws.enableHeartbeat(15000, 5000, 2);
  lights_status(Status::Connecting);
}

void hub_loop() {
  ws.loop();
  size_t len;
  while (connected) {
    void *pkt = xRingbufferReceive(mic_rb, &len, 0);
    if (!pkt) break;
    ws.sendBIN((uint8_t *)pkt, len);
    vRingbufferReturnItem(mic_rb, pkt);
  }
  if (ota.active && millis() - ota.last_rx > 5000) ota_abort("timeout");
  if (connected && millis() - last_status > 10000) hub_send_status();
}

bool hub_adopted() { return adopted; }
bool hub_connected() { return connected; }
