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
