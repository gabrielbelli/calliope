#include "actions.h"

#include <string.h>

static const char *const NAMES[] = {"none", "mute", "volume_up", "volume_down",
                                    "lights", "dimmer", "brighter"};

Action action_parse(const char *name) {
  if (!name) return Action::None;
  for (uint8_t i = 1; i < sizeof(NAMES) / sizeof(NAMES[0]); i++)
    if (!strcmp(name, NAMES[i])) return (Action)i;
  return Action::None;
}

const char *action_name(Action a) {
  uint8_t i = (uint8_t)a;
  return i < sizeof(NAMES) / sizeof(NAMES[0]) ? NAMES[i] : "none";
}

// Roughly even steps to the eye, which judges light by ratios.
static const int LEVELS[] = {10, 20, 35, 60, 100};
static const int NLEVELS = sizeof(LEVELS) / sizeof(LEVELS[0]);

static int volume_at(int k) { return (k * 100 + VOLUME_STEPS / 2) / VOLUME_STEPS; }

int volume_step(int percent, bool up) {
  if (up) {
    for (int k = 0; k <= VOLUME_STEPS; k++)
      if (volume_at(k) > percent) return volume_at(k);
    return 100;
  }
  for (int k = VOLUME_STEPS; k >= 0; k--)
    if (volume_at(k) < percent) return volume_at(k);
  return 0;
}

int volume_level(int percent) { return (percent * VOLUME_STEPS + 50) / 100; }

int brightness_step(int percent, bool up) {
  if (up) {
    for (int i = 0; i < NLEVELS; i++)
      if (LEVELS[i] > percent) return LEVELS[i];
    return LEVELS[NLEVELS - 1];
  }
  for (int i = NLEVELS - 1; i >= 0; i--)
    if (LEVELS[i] < percent) return LEVELS[i];
  return LEVELS[0];
}
