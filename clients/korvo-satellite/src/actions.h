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
