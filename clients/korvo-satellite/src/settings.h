// Persistent settings in NVS. Wi-Fi credentials are kept by the Wi-Fi stack
// itself; everything the hub decides lives here.
#pragma once
#include <Arduino.h>

#include "actions.h"
#include "buttons.h"

struct Settings {
  String hub;    // ws://host:port or wss://host:port, set in the setup portal
  String token;  // issued by the hub on adoption; empty = not adopted
  String name;   // label chosen on the hub
  int volume = 60;
  float mic_gain_db = 30;
  bool mic_enabled = true;
  bool speaker_enabled = true;
  bool lights_enabled = true;  // false: the ring stays dark, whatever happens
  int brightness = 100;        // percent, of whatever the ring shows
  // What each button does here, on press and on release (Action, indexed by
  // Button - 1). The hub sends its own; this is the board's until it does,
  // and what it was before there was a choice: Rec mutes, VOL+/- set volume.
  uint8_t actions[BUTTON_COUNT][2] = {
      {(uint8_t)Action::VolumeUp, 0}, {(uint8_t)Action::VolumeDown, 0}, {0, 0}, {0, 0},
      {0, 0}, {(uint8_t)Action::Mute, 0}, {0, 0}};
};

extern Settings settings;

void settings_load();
void settings_save();
void settings_wipe();  // forget adoption and hub; Wi-Fi is cleared separately
