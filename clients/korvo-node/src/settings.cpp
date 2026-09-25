#include "settings.h"

#include <Preferences.h>

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

void settings_load() {
  prefs.begin("node", true);
  settings.hub = prefs.getString("hub", DEFAULT_HUB);
  settings.token = prefs.getString("token", "");
  settings.name = prefs.getString("name", "");
  settings.volume = prefs.getInt("volume", 60);
  settings.mic_gain_db = prefs.getFloat("mic_gain", 30);
  settings.mic_enabled = prefs.getBool("mic_on", true);
  settings.speaker_enabled = prefs.getBool("spk_on", true);
  settings.local_volume_buttons = prefs.getBool("local_vol", true);
  settings.lights_enabled = prefs.getBool("lights_on", true);
  prefs.end();
}

void settings_save() {
  prefs.begin("node", false);
  prefs.putString("hub", settings.hub);
  prefs.putString("token", settings.token);
  prefs.putString("name", settings.name);
  prefs.putInt("volume", settings.volume);
  prefs.putFloat("mic_gain", settings.mic_gain_db);
  prefs.putBool("mic_on", settings.mic_enabled);
  prefs.putBool("spk_on", settings.speaker_enabled);
  prefs.putBool("local_vol", settings.local_volume_buttons);
  prefs.putBool("lights_on", settings.lights_enabled);
  prefs.end();
}

void settings_wipe() {
  prefs.begin("node", false);
  prefs.clear();
  prefs.end();
  settings = Settings();
  settings.hub = DEFAULT_HUB;
}
