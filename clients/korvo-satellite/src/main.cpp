// Calliope satellite firmware for the ESP32-Korvo v1.1.
//
// The device is a thin client: it streams its microphones to the hub and
// plays, lights and reports whatever the hub decides. Only four things stay
// local, because they must work whatever the hub does: the privacy mute, the
// volume ceiling (codec.cpp), recovery (Wi-Fi setup, factory reset, firmware
// rollback), and, in a build with a public key, the firmware signature check
// (ota_sig.cpp).
//
// Buttons: every press and release is reported to the hub, and each may also
// do one thing here (actions.h), which the hub chooses: the privacy mute,
// volume, the lights on or off, or their brightness. Out of the box Rec mutes
// and VOL+/- set the volume. Whatever the choice, two holds are recovery:
//   SET held 5 s   reopens the Wi-Fi setup portal
//   MODE held 10 s factory reset: forgets Wi-Fi, hub and adoption
#include <Arduino.h>
#include <WiFi.h>
#include <WiFiManager.h>
#include <esp_ota_ops.h>
#include <esp_timer.h>

#include "board.h"
#include "boot.h"
#include "actions.h"
#include "buttons.h"
#include "codec.h"
#include "earcons.h"
#include "hub.h"
#include "lights.h"
#include "settings.h"

bool satellite_muted = false;

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

static String satellite_ap_name() {
  uint8_t mac[6];
  WiFi.macAddress(mac);
  char buf[32];
  snprintf(buf, sizeof(buf), "calliope-sat-%02x%02x", mac[4], mac[5]);
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
  String ap = satellite_ap_name();
  bool ok = force_portal ? wm.startConfigPortal(ap.c_str()) : wm.autoConnect(ap.c_str());
  settings.hub = hub_param.getValue();
  settings_save();
  if (!ok) ESP.restart();  // portal timed out: try again from the top
  WiFi.setSleep(false);    // modem sleep adds tens of ms of jitter to audio
}

static void set_muted(bool m) {
  satellite_muted = m;
  mic_power(!m);
  lights_muted(m);
  hub_send_status();
}

// A button's action here. A setting it changes is saved and reported at once,
// marked as the button's, so the hub takes it as its own.
static void run_action(Action a) {
  switch (a) {
    case Action::Mute:
      set_muted(!satellite_muted);
      return;
    case Action::VolumeUp:
    case Action::VolumeDown:
      settings.volume = volume_step(settings.volume, a == Action::VolumeUp);
      spk_set_volume(settings.volume);
      lights_level(volume_level(settings.volume), VOLUME_SHOW_MS);
      break;
    case Action::Lights:
      settings.lights_enabled = !settings.lights_enabled;
      lights_dark(!settings.lights_enabled);
      break;
    case Action::Dimmer:
    case Action::Brighter:
      settings.brightness = brightness_step(settings.brightness, a == Action::Brighter);
      lights_brightness(settings.brightness);
      break;
    default:
      return;
  }
  settings_save();
  hub_send_status("button");
}

static Action action_for(Button b, int edge) {
  return b == Button::None ? Action::None : (Action)settings.actions[(int)b - 1][edge];
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
    if (released != Button::None) {
      hub_send_button(button_name(released), "release", held);
      run_action(action_for(released, 1));
    }
    if (pressed != Button::None) {
      hub_send_button(button_name(pressed), "press", 0);
      run_action(action_for(pressed, 0));
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
  Serial.begin(115200);
  boot_mark(BOOT_START);
  arm_rollback_timer();
  settings_load();
  boot_mark(BOOT_SETTINGS);
  lights_dark(!settings.lights_enabled);  // before the first frame is drawn
  lights_brightness(settings.brightness);
  lights_begin();
  boot_mark(BOOT_LIGHTS);
  codec_begin();
  boot_mark(BOOT_CODEC);
  mic_set_gain_db(settings.mic_gain_db);
  boot_mark(BOOT_GAIN);
  spk_set_volume(settings.volume);
  boot_mark(BOOT_VOLUME);
  earcons_begin();  // in the background, while Wi-Fi comes up
  boot_mark(BOOT_EARCONS);
  // A release build that inherited a ws:// hub from a development one reopens
  // the portal rather than connecting in clear (see hub_begin).
#ifndef DEV_HUB
  bool plain = settings.hub.startsWith("ws://");
#else
  bool plain = false;
#endif
  boot_mark(BOOT_WIFI);
  run_wifi(settings.hub.isEmpty() || plain);
  boot_mark(BOOT_HUB);
  hub_begin();
}

void loop() {
  hub_loop();
  handle_buttons();
  delay(1);
}
