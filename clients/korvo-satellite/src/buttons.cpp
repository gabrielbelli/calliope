#include "buttons.h"

#include <Arduino.h>

#include "board.h"

// Ladder voltages from the mic board sheet: VOL+ 0.38, VOL- 0.82, SET 1.11,
// PLAY 1.65, MODE 1.98, REC 2.41 V; idle is the 3.3 V pull-up. Thresholds are
// the midpoints.
static Button classify(uint32_t mv) {
  if (mv < 600) return Button::VolUp;
  if (mv < 965) return Button::VolDown;
  if (mv < 1380) return Button::Set;
  if (mv < 1815) return Button::Play;
  if (mv < 2195) return Button::Mode;
  if (mv < 2850) return Button::Rec;
  return Button::None;
}

const char *button_name(Button b) {
  switch (b) {
    case Button::VolUp: return "vol_up";
    case Button::VolDown: return "vol_down";
    case Button::Set: return "set";
    case Button::Play: return "play";
    case Button::Mode: return "mode";
    case Button::Rec: return "rec";
    default: return "none";
  }
}

static Button stable = Button::None, candidate = Button::None;
static uint8_t count = 0;
static uint32_t since = 0;

bool buttons_poll(Button *pressed, Button *released, uint32_t *held_ms) {
  Button now = classify(analogReadMilliVolts(PIN_BUTTONS));
  if (now != candidate) {
    candidate = now;
    count = 0;
    return false;
  }
  if (now == stable || ++count < 3) return false;
  *released = stable;
  *held_ms = stable == Button::None ? 0 : millis() - since;
  *pressed = now;
  stable = now;
  since = millis();
  return true;
}

Button buttons_current() { return stable; }
uint32_t buttons_held_ms() { return stable == Button::None ? 0 : millis() - since; }
