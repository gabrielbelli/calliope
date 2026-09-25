#include "lights.h"

#include <Adafruit_NeoPixel.h>
#include <Arduino.h>
#include <math.h>
#include <string.h>

#include "board.h"

static Adafruit_NeoPixel strip(LED_COUNT, PIN_LEDS, NEO_GRB + NEO_KHZ800);
static portMUX_TYPE mux = portMUX_INITIALIZER_UNLOCKED;

static volatile Status status = Status::Booting;
static volatile bool muted = false;
static volatile uint32_t identify_until = 0;
static volatile int ota_pct = -1;

struct HubLayer {
  Mode mode = Mode::Off;
  uint8_t r = 0, g = 0, b = 0, brightness = 64;
  uint8_t pixels[LED_COUNT * 3] = {};
};
static HubLayer hub;

static void fill(uint8_t r, uint8_t g, uint8_t b) {
  for (int i = 0; i < LED_COUNT; i++) strip.setPixelColor(i, r, g, b);
}

// 0..1 triangle-ish breathing curve with the given period.
static float breathe(uint32_t t, uint32_t period) {
  return 0.5f - 0.5f * cosf(2 * PI * (t % period) / period);
}

static void spin(uint32_t t, uint8_t r, uint8_t g, uint8_t b, uint32_t period) {
  int head = (t % period) * LED_COUNT / period;
  for (int i = 0; i < LED_COUNT; i++) {
    int d = (head - i + LED_COUNT) % LED_COUNT;
    float k = d == 0 ? 1.0f : d == 1 ? 0.35f : d == 2 ? 0.1f : 0.0f;
    strip.setPixelColor(i, r * k, g * k, b * k);
  }
}

static void render(uint32_t t) {
  if (muted) {
    fill(80, 0, 0);
    return;
  }
  if (ota_pct >= 0) {
    int lit = (ota_pct * LED_COUNT + 99) / 100;
    for (int i = 0; i < LED_COUNT; i++) strip.setPixelColor(i, 0, i < lit ? 60 : 4, 0);
    return;
  }
  if (t < identify_until) {
    uint8_t v = ((t / 150) % 2) ? 120 : 0;
    fill(v, v, v);
    return;
  }
  switch (status) {
    case Status::Booting: fill(10, 10, 10); return;
    case Status::Portal: {
      float k = breathe(t, 2000);
      fill(90 * k, 35 * k, 0);
      return;
    }
    case Status::Connecting: spin(t, 0, 20, 90, 1200); return;
    case Status::Pending: {
      float k = 0.1f + 0.9f * breathe(t, 3000);
      fill(40 * k, 40 * k, 40 * k);
      return;
    }
    case Status::HubLost: {
      float k = breathe(t, 4000);
      fill(40 * k, 15 * k, 0);
      return;
    }
    case Status::Ready: break;
  }

  HubLayer h;
  portENTER_CRITICAL(&mux);
  h = hub;
  portEXIT_CRITICAL(&mux);
  float s = h.brightness / 255.0f;
  switch (h.mode) {
    case Mode::Off: fill(0, 0, 0); break;
    case Mode::Solid: fill(h.r * s, h.g * s, h.b * s); break;
    case Mode::Pulse: {
      float k = s * breathe(t, 2000);
      fill(h.r * k, h.g * k, h.b * k);
      break;
    }
    case Mode::Spin: spin(t, h.r * s, h.g * s, h.b * s, 1000); break;
    case Mode::Pixels:
      for (int i = 0; i < LED_COUNT; i++)
        strip.setPixelColor(i, h.pixels[3 * i] * s, h.pixels[3 * i + 1] * s, h.pixels[3 * i + 2] * s);
      break;
  }
}

static void task(void *) {
  for (;;) {
    render(millis());
    strip.show();
    vTaskDelay(pdMS_TO_TICKS(30));
  }
}

void lights_begin() {
  strip.begin();
  strip.clear();
  strip.show();
  xTaskCreatePinnedToCore(task, "lights", 3072, nullptr, 1, nullptr, 0);
}

void lights_status(Status s) { status = s; }
void lights_muted(bool m) { muted = m; }
void lights_identify(uint32_t ms) { identify_until = millis() + ms; }
void lights_ota(int percent) { ota_pct = percent; }

void lights_hub(Mode mode, uint8_t r, uint8_t g, uint8_t b, uint8_t brightness, const uint8_t *pixels) {
  portENTER_CRITICAL(&mux);
  hub.mode = mode;
  hub.r = r;
  hub.g = g;
  hub.b = b;
  hub.brightness = brightness;
  if (pixels) memcpy(hub.pixels, pixels, sizeof(hub.pixels));
  portEXIT_CRITICAL(&mux);
}
