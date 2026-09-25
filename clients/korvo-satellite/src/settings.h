// Persistent settings in NVS. Wi-Fi credentials are kept by the Wi-Fi stack
// itself; everything the hub decides lives here.
#pragma once
#include <Arduino.h>

struct Settings {
  String hub;    // ws://host:port or wss://host:port, set in the setup portal
  String token;  // issued by the hub on adoption; empty = not adopted
  String name;   // label chosen on the hub
  int volume = 60;
  float mic_gain_db = 30;
  bool mic_enabled = true;
  bool speaker_enabled = true;
  bool local_volume_buttons = true;  // VOL+/- act locally as well as reporting
  bool lights_enabled = true;        // false: the ring stays dark, whatever happens
};

extern Settings settings;

void settings_load();
void settings_save();
void settings_wipe();  // forget adoption and hub; Wi-Fi is cleared separately
