#include "buttons.h"

#include <Arduino.h>

#include "board.h"

// Ladder voltages from the mic board sheet: VOL+ 0.38, VOL- 0.82, SET 1.11,
// PLAY 1.65, MODE 1.98, REC 2.41 V; idle is the 3.3 V pull-up. Thresholds are
// the midpoints. KEY1, once wired, shorts the pin to ground: the ADC's floor,
// below VOL+'s 0.38 V (measured 0.41).
static Button classify(uint32_t mv) {
  if (mv < 250) return Button::Key1;
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
    case Button::Key1: return "key1";
    default: return "none";
  }
}

static Button stable = Button::None, candidate = Button::None;
static uint8_t count = 0;
static uint32_t since = 0;

static uint32_t last_mv = 0, min_mv = UINT32_MAX, max_mv = 0, polls = 0;

bool buttons_poll(Button *pressed, Button *released, uint32_t *held_ms) {
  uint32_t mv = analogReadMilliVolts(PIN_BUTTONS);
  last_mv = mv;
  if (mv < min_mv) min_mv = mv;
  if (mv > max_mv) max_mv = mv;
  polls++;
  Button now = classify(mv);
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

void buttons_window(uint32_t *now_mv, uint32_t *lo, uint32_t *hi, uint32_t *n) {
  *now_mv = last_mv;
  *lo = min_mv == UINT32_MAX ? last_mv : min_mv;
  *hi = max_mv;
  *n = polls;
  min_mv = UINT32_MAX;
  max_mv = 0;
  polls = 0;
}
uint32_t buttons_held_ms() { return stable == Button::None ? 0 : millis() - since; }
