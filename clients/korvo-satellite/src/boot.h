// Where start-up got to, and how long each step took.
//
// WHY THIS EXISTS: on one power source the board stopped between lighting its
// ring and starting Wi-Fi, for hours, until its BOOT button (GPIO0, which is
// also the microphone codec's clock) was pressed. Nothing reached the network,
// so nothing could say where. Each step now stamps its time, the stamps go to
// the hub in hello, and a watchdog restarts a start-up that stalls, noting the
// step in RTC memory so the next boot can report it.
#pragma once
#include <stdint.h>

enum BootStage : uint8_t {
  BOOT_START, BOOT_SETTINGS, BOOT_LIGHTS, BOOT_CODEC, BOOT_GAIN, BOOT_VOLUME,
  BOOT_EARCONS, BOOT_WIFI, BOOT_HUB, BOOT_STAGES
};

const char *boot_stage_name(uint8_t stage);
void boot_mark(BootStage stage);        // stamps the end of the step before
uint32_t boot_ms(uint8_t stage);        // ms from power-on to the end of `stage`, 0 if not reached
uint8_t boot_stuck_stage();             // the step a previous stalled start-up was in, or 255
uint32_t boot_stuck_restarts();         // stalled start-ups since power was last applied
