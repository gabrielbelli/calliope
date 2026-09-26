#include "boot.h"

#include <Arduino.h>
#include <esp_attr.h>
#include <esp_system.h>
#include <esp_timer.h>

static const char *NAMES[BOOT_STAGES] = {"start", "settings", "lights", "codec", "gain",
                                         "volume", "earcons", "wifi", "hub"};
static uint32_t stamps[BOOT_STAGES];
static volatile uint8_t current = BOOT_START;

// RTC_NOINIT survives a software restart but not a power cut, which is exactly
// the span we need: "the last start-up stalled in <step>".
static RTC_NOINIT_ATTR uint32_t rtc_magic;
static RTC_NOINIT_ATTR uint8_t rtc_stuck;
static RTC_NOINIT_ATTR uint32_t rtc_restarts;
static const uint32_t MAGIC = 0x5A7E1173;

// Longer than a healthy start-up takes to reach Wi-Fi (well under 2 s when it
// works), far shorter than an evening offline.
static const uint64_t STALL_US = 20ULL * 1000000;

static void on_stall(void *) {
  if (current >= BOOT_WIFI) return;
  rtc_magic = MAGIC;
  rtc_stuck = current;
  rtc_restarts++;
  esp_restart();
}

const char *boot_stage_name(uint8_t s) { return s < BOOT_STAGES ? NAMES[s] : "none"; }
uint32_t boot_ms(uint8_t s) { return s < BOOT_STAGES ? stamps[s] : 0; }
uint8_t boot_stuck_stage() { return rtc_magic == MAGIC ? rtc_stuck : 255; }
uint32_t boot_stuck_restarts() { return rtc_magic == MAGIC ? rtc_restarts : 0; }

void boot_mark(BootStage s) {
  if (s == BOOT_START) {
    if (esp_reset_reason() == ESP_RST_POWERON || rtc_magic != MAGIC) {
      rtc_magic = MAGIC;
      rtc_stuck = 255;
      rtc_restarts = 0;
    }
    static esp_timer_handle_t t;
    esp_timer_create_args_t a = {};
    a.callback = on_stall;
    a.name = "boot-stall";
    esp_timer_create(&a, &t);
    esp_timer_start_once(t, STALL_US);
  }
  stamps[s] = millis();
  current = s;
  Serial.printf("[boot] %-8s %6lu ms\n", NAMES[s], (unsigned long)stamps[s]);
}
