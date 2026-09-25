// Calliope node firmware for the ESP32-Korvo v1.1.
//
// The device is a thin client: it streams its microphones to the hub and
// plays, lights and reports whatever the hub decides. Only three things stay
// local, because they must work whatever the hub does: the privacy mute, the
// volume ceiling (codec.cpp), and recovery (Wi-Fi setup, factory reset,
// firmware rollback).
//
// Buttons, locally:
//   REC            toggles the privacy mute (mic powered down, ring red)
//   VOL+ / VOL-    change the volume while local_volume_buttons is on
//   SET held 5 s   reopens the Wi-Fi setup portal
//   MODE held 10 s factory reset: forgets Wi-Fi, hub and adoption
// Every press and release is also reported to the hub.
#include <Arduino.h>
#include <WiFi.h>
#include <WiFiManager.h>
#include <esp_ota_ops.h>
#include <esp_timer.h>

#include "board.h"
#include "buttons.h"
#include "codec.h"
#include "hub.h"
#include "lights.h"
#include "settings.h"

bool node_muted = false;

// Keep the Arduino core from marking a freshly updated image valid at boot:
// the image has to reach the hub first (hub.cpp, verify_ota).
extern "C" bool verifyRollbackLater() { return true; }

static void rollback_if_unverified(void *) {
  esp_ota_img_states_t st;
  if (esp_ota_get_state_partition(esp_ota_get_running_partition(), &st) == ESP_OK &&
      st == ESP_OTA_IMG_PENDING_VERIFY)
    esp_ota_mark_app_invalid_rollback_and_reboot();
}

static void arm_rollback_timer() {
  static esp_timer_handle_t t;
  esp_timer_create_args_t args = {};
  args.callback = rollback_if_unverified;
  args.name = "ota-verify";
  esp_timer_create(&args, &t);
  esp_timer_start_once(t, 180ULL * 1000000);  // time to join Wi-Fi and reach the hub
}

static String node_ap_name() {
  uint8_t mac[6];
  WiFi.macAddress(mac);
  char buf[32];
  snprintf(buf, sizeof(buf), "calliope-node-%02x%02x", mac[4], mac[5]);
  return buf;
}

static void run_wifi(bool force_portal) {
  WiFiManager wm;
  WiFiManagerParameter hub_param("hub", "Hub address (wss://host:port)", settings.hub.c_str(), 120);
  wm.addParameter(&hub_param);
  wm.setConfigPortalTimeout(600);
  wm.setConnectTimeout(20);
  wm.setAPCallback([](WiFiManager *) { lights_status(Status::Portal); });
  wm.setSaveParamsCallback([&]() {
    settings.hub = hub_param.getValue();
    settings_save();
  });
  wm.setBreakAfterConfig(true);
  lights_status(Status::Connecting);
  String ap = node_ap_name();
  bool ok = force_portal ? wm.startConfigPortal(ap.c_str()) : wm.autoConnect(ap.c_str());
  settings.hub = hub_param.getValue();
  settings_save();
  if (!ok) ESP.restart();  // portal timed out: try again from the top
  WiFi.setSleep(false);    // modem sleep adds tens of ms of jitter to audio
}

static void set_muted(bool m) {
  node_muted = m;
  mic_power(!m);
  lights_muted(m);
  hub_send_status();
}

static void factory_reset() {
  lights_identify(1500);
  settings_wipe();
  WiFiManager().resetSettings();
  delay(1500);
  ESP.restart();
}

static void handle_buttons() {
  static uint32_t last = 0;
  if (millis() - last < 10) return;
  last = millis();

  Button pressed, released;
  uint32_t held;
  if (buttons_poll(&pressed, &released, &held)) {
    if (released != Button::None) hub_send_button(button_name(released), "release", held);
    if (pressed != Button::None) {
      hub_send_button(button_name(pressed), "press", 0);
      if (pressed == Button::Rec) set_muted(!node_muted);
      if (settings.local_volume_buttons && (pressed == Button::VolUp || pressed == Button::VolDown)) {
        settings.volume = constrain(settings.volume + (pressed == Button::VolUp ? 10 : -10), 0, 100);
        spk_set_volume(settings.volume);
        settings_save();
        hub_send_status();
      }
    }
  }
  Button cur = buttons_current();
  uint32_t ms = buttons_held_ms();
  if (cur == Button::Mode && ms > 10000) factory_reset();
  if (cur == Button::Set && ms > 5000) {
    lights_identify(1000);
    run_wifi(true);
    ESP.restart();
  }
}

void setup() {
  arm_rollback_timer();
  settings_load();
  lights_begin();
  codec_begin();
  mic_set_gain_db(settings.mic_gain_db);
  spk_set_volume(settings.volume);
  run_wifi(settings.hub.isEmpty());
  hub_begin();
}

void loop() {
  hub_loop();
  handle_buttons();
  delay(1);
}
