// ES7210 (microphones) and ES8311 (speaker) over I2C, with their I2S ports.
#pragma once
#include <stddef.h>
#include <stdint.h>

#define MIC_RATE 16000
#define MIC_CHANNELS 4
#define SPK_RATE 48000

void codec_begin();

// Microphones: reads interleaved s16le frames (MIC_CHANNELS per frame).
size_t mic_read(int16_t *frames, size_t max_frames);
void mic_set_gain_db(float db);
void mic_power(bool on);  // privacy mute powers the mic front-end down

// Speaker: mono s16le at SPK_RATE, duplicated to both DAC slots.
size_t spk_write(const int16_t *mono, size_t samples, uint32_t timeout_ms);
void spk_set_volume(int percent);  // 0..100, 100 = 0 dB, never above
void spk_amp(bool on);
