// What a button does on the satellite itself. The hub decides which button
// does what (its "buttons" config) and sends the part the satellite acts on
// as "button_actions"; the rest (talk, stop, webhooks) the hub does itself
// when it hears of the press. These run here so that they work with the hub
// down, and so that only a button, never the hub, can undo the privacy mute.
#pragma once
#include <stdint.h>

enum class Action : uint8_t { None, Mute, VolumeUp, VolumeDown, Lights, Dimmer, Brighter };

Action action_parse(const char *name);  // None for anything else, hub actions included
const char *action_name(Action a);

// Brightness steps for Dimmer and Brighter, in percent.
int brightness_step(int percent, bool up);

// The volume has VOLUME_STEPS steps, one to an LED of the ring: step k is
// k * 100 / VOLUME_STEPS percent, about 4 dB apart (the codec's volume is 0.5
// dB a percent). A volume between steps, set on the hub, moves to the next one.
static const int VOLUME_STEPS = 12;
int volume_step(int percent, bool up);
int volume_level(int percent);  // 0..VOLUME_STEPS, the LEDs that show it
static const uint32_t VOLUME_SHOW_MS = 1500;  // a change of volume shows on the ring this long
