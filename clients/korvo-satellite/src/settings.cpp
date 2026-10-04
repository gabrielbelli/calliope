#include "settings.h"

#include <Preferences.h>

#include "board.h"

Settings settings;
static Preferences prefs;

// DEV_HUB (set through PLATFORMIO_BUILD_FLAGS) points a development build at a
// hub on the developer's machine without editing platformio.ini.
#ifdef DEV_HUB
#undef DEFAULT_HUB
#define DEFAULT_HUB DEV_HUB
#endif
#ifndef DEFAULT_HUB
#define DEFAULT_HUB ""
#endif

// The NVS namespace keeps the feature's name from before 2026-09-25, when
// "nodes" became "satellites". Renamed, it would make every board updated over
// the air start again from the defaults: its hub address and adoption token
// gone, so it comes back pending, and its lights on in a room where they were
// off.
static const char *NVS_NAMESPACE = "node";

void settings_load() {
  prefs.begin(NVS_NAMESPACE, true);
  settings.hub = prefs.getString("hub", DEFAULT_HUB);
  settings.token = prefs.getString("token", "");
  settings.name = prefs.getString("name", "");
  settings.volume = prefs.getInt("volume", 60);
  settings.mic_gain_db = prefs.getFloat("mic_gain", 30);
  settings.mic_enabled = prefs.getBool("mic_on", true);
  settings.speaker_enabled = prefs.getBool("spk_on", true);
  settings.lights_enabled = prefs.getBool("lights_on", true);
  settings.muted = prefs.getBool("muted", false);
  settings.brightness = prefs.getInt("bright", 100);
  // ring_bottom, the setting for an hour on 27 Sep 2026, named the opposite LED.
  settings.ring_top = prefs.isKey("ring_top") ? prefs.getInt("ring_top", 0)
                                              : (prefs.getInt("ring_bottom", 6) + 6) % LED_COUNT;
  settings.ring_upside_down = prefs.getBool("ring_flip", false);
  if (prefs.getBytesLength("actions") == sizeof(settings.actions))
    prefs.getBytes("actions", settings.actions, sizeof(settings.actions));
  else if (!prefs.getBool("local_vol", true))
    // Saved by firmware from before the choice, with the volume buttons
    // handed to the hub: they do nothing here.
    settings.actions[0][0] = settings.actions[1][0] = (uint8_t)Action::None;
  prefs.end();
}

void settings_save() {
  prefs.begin(NVS_NAMESPACE, false);
  prefs.putString("hub", settings.hub);
  prefs.putString("token", settings.token);
  prefs.putString("name", settings.name);
  prefs.putInt("volume", settings.volume);
  prefs.putFloat("mic_gain", settings.mic_gain_db);
  prefs.putBool("mic_on", settings.mic_enabled);
  prefs.putBool("spk_on", settings.speaker_enabled);
  prefs.putBool("lights_on", settings.lights_enabled);
  prefs.putBool("muted", settings.muted);
  prefs.putInt("bright", settings.brightness);
  prefs.putInt("ring_top", settings.ring_top);
  prefs.putBool("ring_flip", settings.ring_upside_down);
  prefs.putBytes("actions", settings.actions, sizeof(settings.actions));
  prefs.end();
}

void settings_wipe() {
  prefs.begin(NVS_NAMESPACE, false);
  prefs.clear();
  prefs.end();
  settings = Settings();
  settings.hub = DEFAULT_HUB;
}
