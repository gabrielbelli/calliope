#include "speaker.h"

#include <Arduino.h>
#include <esp_heap_caps.h>
#include <freertos/queue.h>
#include <freertos/ringbuf.h>
#include <math.h>

#include "codec.h"
#include "settings.h"

static const size_t RB_PSRAM = 96 * 1024;  // about 1 s: the hub keeps 300 ms ahead
static const size_t RB_INTERNAL = 32 * 1024;
static const size_t CHUNK = 480;  // samples per write: 10 ms at 48 kHz
// A duck fades rather than steps, which clicks: full scale to silence in 50 ms.
static const float DUCK_STEP = 1.0f / (SPK_RATE / 20);

struct Clip {
  int16_t *pcm;
  size_t n;
};

static RingbufHandle_t rb;
static size_t rb_size;
static QueueHandle_t clips;  // from loop() to the task, which frees them

static volatile int duck_level = -1;
static bool duck_timed = false;  // loop() only
static uint32_t duck_until = 0;

// The gain the hub's audio should have now. The codec's volume is 0.5 dB per
// step (codec.cpp), so a duck to `level` sounds the same as setting the volume
// there, and it can only ever lower the output: the ceiling holds.
static float duck_target() {
  int level = duck_level, volume = settings.volume;
  if (level < 0 || level >= volume) return 1.0f;
  if (level <= 0) return 0.0f;
  return powf(10.0f, (level - volume) * 0.5f / 20.0f);
}

static void task(void *) {
  static int16_t out[CHUNK];
  bool amp = false, idle = true;
  uint32_t last_audio = 0;
  Clip clip = {nullptr, 0};
  size_t clip_pos = 0;
  float gain = 1.0f;
  for (;;) {
    Clip next;
    while (xQueueReceive(clips, &next, 0) == pdTRUE) {
      if (clip.pcm) heap_caps_free(clip.pcm);
      clip = next;
      clip_pos = 0;
    }
    size_t len = 0;
    // With an earcon sounding, do not wait for the hub: the earcon plays
    // whether or not speech is arriving.
    int16_t *pcm = (int16_t *)xRingbufferReceiveUpTo(rb, &len, clip.pcm ? 0 : pdMS_TO_TICKS(20),
                                                     CHUNK * sizeof(int16_t));
    size_t n = pcm ? len / 2 : 0;
    size_t e = clip.pcm ? min(CHUNK, clip.n - clip_pos) : 0;
    size_t total = max(n, e);
    if (!total) {
      if (pcm) vRingbufferReturnItem(rb, pcm);
      if (amp && millis() - last_audio > 3000) spk_amp(amp = false);  // amplifier hiss is audible
      idle = true;
      continue;
    }
    float target = duck_target();
    if (idle) gain = target;  // nothing was sounding, so nothing to fade from
    idle = false;
    for (size_t i = 0; i < total; i++) {
      gain = gain < target ? fminf(target, gain + DUCK_STEP) : fmaxf(target, gain - DUCK_STEP);
      int32_t s = i < n ? (int32_t)lrintf(pcm[i] * gain) : 0;
      if (i < e) s += clip.pcm[clip_pos + i];
      out[i] = s > 32767 ? 32767 : s < -32768 ? -32768 : s;
    }
    if (pcm) vRingbufferReturnItem(rb, pcm);  // before the write, which blocks
    clip_pos += e;
    if (clip.pcm && clip_pos >= clip.n) {
      heap_caps_free(clip.pcm);
      clip = {nullptr, 0};
    }
    if (!amp && settings.speaker_enabled) spk_amp(amp = true);
    spk_write(out, total, 100);
    last_audio = millis();
  }
}

void speaker_begin() {
  static StaticRingbuffer_t rb_struct;
  uint8_t *mem = (uint8_t *)heap_caps_malloc(RB_PSRAM, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
  rb_size = mem ? RB_PSRAM : RB_INTERNAL;
  rb = mem ? xRingbufferCreateStatic(RB_PSRAM, RINGBUF_TYPE_BYTEBUF, mem, &rb_struct)
           : xRingbufferCreate(RB_INTERNAL, RINGBUF_TYPE_BYTEBUF);
  clips = xQueueCreate(4, sizeof(Clip));
  xTaskCreatePinnedToCore(task, "spk", 4096, nullptr, 5, nullptr, 1);
}

bool speaker_push(const uint8_t *pcm, size_t len) {
  return xRingbufferSend(rb, pcm, len, 0) == pdTRUE;
}

void speaker_flush() {
  size_t len;
  void *item;
  while ((item = xRingbufferReceiveUpTo(rb, &len, 0, rb_size))) vRingbufferReturnItem(rb, item);
}

uint32_t speaker_buffered_ms() {
  return (rb_size - xRingbufferGetCurFreeSize(rb)) / (SPK_RATE * 2 / 1000);
}

bool speaker_play(int16_t *pcm, size_t samples) {
  Clip c = {pcm, samples};
  if (!pcm || !samples || xQueueSend(clips, &c, 0) != pdTRUE) {
    if (pcm) heap_caps_free(pcm);
    return false;
  }
  return true;
}

void speaker_duck(int level, uint32_t ms) {
  duck_level = level < 0 ? 0 : level > 100 ? 100 : level;
  duck_timed = ms > 0;
  duck_until = millis() + ms;
}

void speaker_unduck() {
  duck_level = -1;
  duck_timed = false;
}

int speaker_duck_level() { return duck_level; }

void speaker_loop() {
  if (duck_level >= 0 && duck_timed && (int32_t)(millis() - duck_until) >= 0) speaker_unduck();
}
