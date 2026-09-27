// Six buttons on one ADC resistor ladder. Only one reads at a time.
#pragma once
#include <stdint.h>

enum class Button : uint8_t { None, VolUp, VolDown, Set, Play, Mode, Rec };

const char *button_name(Button b);

// Call every ~10 ms. Returns true on a debounced change; *pressed is the new
// button (None on release) and *released/*held_ms describe what was let go.
bool buttons_poll(Button *pressed, Button *released, uint32_t *held_ms);
Button buttons_current();
uint32_t buttons_held_ms();

// What the ladder has read since the last call: the latest, lowest and highest
// millivolts and how many polls, for the status. A press that never reaches
// an event shows here as a dip, or does not.
void buttons_window(uint32_t *now_mv, uint32_t *min_mv, uint32_t *max_mv, uint32_t *polls);
