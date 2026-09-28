#include "lights.h"

#include <Adafruit_NeoPixel.h>
#include <Arduino.h>
#include <driver/rmt.h>
#include <math.h>
#include <string.h>

#include "board.h"

// The frame is composed in the NeoPixel buffer and sent by send_frame(), never
// by strip.show(). On this core, show() drives the LEDs from a single 64-pulse
// RMT block, installed afresh for every frame and refilled by an interrupt
// every 32 pulses; a frame is 288. Whenever that interrupt ran late (Wi-Fi on
// this core, or the flash writes of the earcon store, which stall a non-IRAM
// interrupt) bits went to the wrong LED, and random LEDs lit up dimly.
// send_frame() gives the channel enough blocks to hold the whole frame, so
// nothing has to be refilled while it is sent.
static Adafruit_NeoPixel strip(LED_COUNT, PIN_LEDS, NEO_GRB + NEO_KHZ800);
static constexpr rmt_channel_t LED_RMT = RMT_CHANNEL_0;
static constexpr int LED_BITS = LED_COUNT * 24;
static constexpr int LED_RMT_BLOCKS = (LED_BITS + 1 + 63) / 64;  // + the end marker
static rmt_item32_t items[LED_BITS];
static uint8_t sent[LED_COUNT * 3];
static uint32_t sent_at = 0;
static bool sent_once = false;
// Resent this often although unchanged, so that a frame an LED misread
// (a glitch on the line) does not stay up.
static constexpr uint32_t RESEND_MS = 1000;
static portMUX_TYPE mux = portMUX_INITIALIZER_UNLOCKED;

static volatile Status status = Status::Booting;
static volatile bool muted = false;
static volatile bool dark = false;
static volatile int brightness_pct = 100;
static volatile uint32_t identify_until = 0;
static volatile int ota_pct = -1;
static volatile int level_lit = 0;
static volatile int top_led = 0;
static volatile int ring_dir = 1;
static volatile uint32_t level_until = 0;

struct HubLayer {
  Mode mode = Mode::Off;
  uint8_t r = 0, g = 0, b = 0, brightness = 64;
  uint8_t pixels[LED_COUNT * 3] = {};
  float direction = -1;
};
static HubLayer hub;
// Where the listening arc is drawn, in LEDs (fractional); it moves towards the
// talker rather than jumping. Negative: not drawn yet.
static float arc_at = -1;

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

// Listening: a soft breathing glow on the whole ring and, once the talker is
// located, a brighter arc about two LEDs wide that glides the shortest way
// round towards them (a quarter LED per frame, about eight LEDs a second).
static void listen(uint32_t t, const HubLayer &h, float s) {
  if (h.direction < 0) {
    arc_at = -1;
    float k = s * (0.25f + 0.75f * breathe(t, 1600));
    fill(h.r * k, h.g * k, h.b * k);
    return;
  }
  float target = fmodf(h.direction, 360.0f) / 360.0f * LED_COUNT;
  if (arc_at < 0) {
    arc_at = target;
  } else {
    float d = fmodf(target - arc_at + 1.5f * LED_COUNT, (float)LED_COUNT) - 0.5f * LED_COUNT;
    const float step = 0.25f;
    arc_at = fmodf(arc_at + (d > step ? step : d < -step ? -step : d) + LED_COUNT, (float)LED_COUNT);
  }
  float glow = 0.10f + 0.06f * breathe(t, 2400);
  float peak = 0.7f + 0.3f * breathe(t, 1200);
  for (int i = 0; i < LED_COUNT; i++) {
    float d = fabsf(i - arc_at);
    if (d > LED_COUNT / 2.0f) d = LED_COUNT - d;
    float arc = d < 2.5f ? 1.0f - d / 2.5f : 0.0f;
    float k = s * (glow + (1.0f - glow) * arc * arc * peak);
    strip.setPixelColor(i, h.r * k, h.g * k, h.b * k);
  }
}

static void render(uint32_t t) {
  if (dark) {
    fill(0, 0, 0);
    return;
  }
  if ((int32_t)(level_until - t) > 0) {
    fill(3, 3, 3);
    for (int k = 0; k < level_lit; k++)
      strip.setPixelColor(((top_led + ring_dir * k) % LED_COUNT + LED_COUNT) % LED_COUNT, 90, 90, 90);
    return;
  }
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
    case Mode::Listen: listen(t, h, s); break;
  }
}

static void rmt_begin() {
  rmt_config_t c = RMT_DEFAULT_CONFIG_TX((gpio_num_t)PIN_LEDS, LED_RMT);
  c.clk_div = 2;  // 40 MHz: 25 ns a tick
  c.mem_block_num = LED_RMT_BLOCKS;
  rmt_config(&c);
  rmt_driver_install(LED_RMT, 0, 0);
}

// WS2812 at 800 kHz, in 25 ns ticks: a 0 is 400 ns high then 850 low, a 1 is
// 800 high then 450 low. Sent only when the frame changed, or RESEND_MS on.
static void send_frame(uint32_t t) {
  const uint8_t *grb = strip.getPixels();
  if (sent_once && !memcmp(grb, sent, sizeof(sent)) && t - sent_at < RESEND_MS) return;
  int k = 0;
  for (int i = 0; i < LED_COUNT * 3; i++)
    for (int bit = 7; bit >= 0; bit--) {
      bool one = (grb[i] >> bit) & 1;
      items[k].level0 = 1;
      items[k].duration0 = one ? 32 : 16;
      items[k].level1 = 0;
      items[k].duration1 = one ? 18 : 34;
      k++;
    }
  rmt_write_items(LED_RMT, items, k, true);
  memcpy(sent, grb, sizeof(sent));
  sent_at = t;
  sent_once = true;
}

// The whole frame at brightness_pct, drawn at full scale by render().
static void dim_frame() {
  int pct = brightness_pct;
  if (muted && pct < 25) pct = 25;
  if (pct >= 100) return;
  for (int i = 0; i < LED_COUNT; i++) {
    uint32_t c = strip.getPixelColor(i);
    strip.setPixelColor(i, ((c >> 16) & 0xff) * pct / 100, ((c >> 8) & 0xff) * pct / 100,
                        (c & 0xff) * pct / 100);
  }
}

static void task(void *) {
  for (;;) {
    uint32_t t = millis();
    render(t);
    dim_frame();
    send_frame(t);
    vTaskDelay(pdMS_TO_TICKS(30));
  }
}

void lights_begin() {
  rmt_begin();
  strip.clear();
  send_frame(millis());
  xTaskCreatePinnedToCore(task, "lights", 3072, nullptr, 1, nullptr, 0);
}

void lights_dark(bool d) { dark = d; }
void lights_ring(int top, bool upside_down) {
  top_led = ((top % LED_COUNT) + LED_COUNT) % LED_COUNT;
  ring_dir = upside_down ? -1 : 1;
}

void lights_level(int lit, uint32_t ms) {
  level_lit = lit < 0 ? 0 : lit > LED_COUNT ? LED_COUNT : lit;
  level_until = millis() + ms;
}
void lights_brightness(int percent) { brightness_pct = percent < 1 ? 1 : percent > 100 ? 100 : percent; }
void lights_status(Status s) { status = s; }
void lights_muted(bool m) { muted = m; }
void lights_identify(uint32_t ms) { identify_until = millis() + ms; }
void lights_ota(int percent) { ota_pct = percent; }

void lights_hub(Mode mode, uint8_t r, uint8_t g, uint8_t b, uint8_t brightness, const uint8_t *pixels,
                float direction) {
  portENTER_CRITICAL(&mux);
  hub.mode = mode;
  hub.direction = direction;
  hub.r = r;
  hub.g = g;
  hub.b = b;
  hub.brightness = brightness;
  if (pixels) memcpy(hub.pixels, pixels, sizeof(hub.pixels));
  portEXIT_CRITICAL(&mux);
}
