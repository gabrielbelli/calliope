// Register sequences are ported from Espressif's esp_codec_dev drivers
// (components/esp_codec_dev/device/es7210/es7210.c and es8311/es8311.c in
// espressif/esp-adf), specialised for the Korvo: both codecs are I2S slaves,
// the ES7210 runs four mics in TDM, the ES8311 is DAC-only and takes its clock
// from BCLK. Changed from the original: the codec-interface layer is removed,
// the open/set_fs/enable sequences are inlined for one configuration, and the
// volume is mapped from a 0-100 scale capped at 0 dB.
//
//   SPDX-FileCopyrightText: 2023 Espressif Systems (Shanghai) CO LTD
//   SPDX-License-Identifier: Apache-2.0
//   Licensed under the Apache License, Version 2.0 (the "License"); you may
//   not use this file except in compliance with the License. You may obtain a
//   copy of the License at http://www.apache.org/licenses/LICENSE-2.0
#include "codec.h"

#include <Arduino.h>
#include <Wire.h>
#include <driver/i2s.h>

#include "board.h"

static const i2s_port_t MIC_PORT = I2S_NUM_1;
static const i2s_port_t SPK_PORT = I2S_NUM_0;

static void wr(uint8_t addr, uint8_t reg, uint8_t val) {
  Wire.beginTransmission(addr);
  Wire.write(reg);
  Wire.write(val);
  Wire.endTransmission();
}

static uint8_t rd(uint8_t addr, uint8_t reg) {
  Wire.beginTransmission(addr);
  Wire.write(reg);
  Wire.endTransmission(false);
  Wire.requestFrom(addr, (uint8_t)1);
  return Wire.available() ? Wire.read() : 0;
}

static void upd(uint8_t addr, uint8_t reg, uint8_t mask, uint8_t val) {
  wr(addr, reg, (rd(addr, reg) & ~mask) | (mask & val));
}

// ---- ES7210 --------------------------------------------------------------

static uint8_t mic_gain_idx = 10;  // 3 dB steps, 10 = 30 dB

static void es7210_mics_on() {
  const uint8_t A = ES7210_ADDR;
  for (uint8_t r = 0x43; r <= 0x46; r++) upd(A, r, 0x10, 0x00);
  wr(A, 0x4B, 0xff);
  wr(A, 0x4C, 0xff);
  upd(A, 0x01, 0x0b, 0x00);
  wr(A, 0x4B, 0x00);
  upd(A, 0x01, 0x15, 0x00);
  wr(A, 0x4C, 0x00);
  for (uint8_t r = 0x43; r <= 0x46; r++) {
    upd(A, r, 0x10, 0x10);
    upd(A, r, 0x0f, mic_gain_idx);
  }
  wr(A, 0x12, 0x02);  // TDM
}

static void es7210_begin() {
  const uint8_t A = ES7210_ADDR;
  wr(A, 0x00, 0xff);
  wr(A, 0x00, 0x41);
  wr(A, 0x01, 0x3f);
  wr(A, 0x09, 0x30);
  wr(A, 0x0A, 0x30);
  wr(A, 0x23, 0x2a);
  wr(A, 0x22, 0x0a);
  wr(A, 0x20, 0x0a);
  wr(A, 0x21, 0x2a);
  upd(A, 0x08, 0x01, 0x00);  // slave
  wr(A, 0x40, 0x43);
  wr(A, 0x41, 0x70);
  wr(A, 0x42, 0x70);
  wr(A, 0x07, 0x20);
  wr(A, 0x02, 0xc1);
  es7210_mics_on();
  uint8_t off_reg = rd(A, 0x01);
  wr(A, 0x11, (rd(A, 0x11) & 0x1c) | 0x60);  // 16-bit slots, standard I2S
  wr(A, 0x01, off_reg);
  wr(A, 0x06, 0x00);
  wr(A, 0x40, 0x43);
  for (uint8_t r = 0x47; r <= 0x4A; r++) wr(A, r, 0x08);
  es7210_mics_on();
  wr(A, 0x40, 0x43);
  wr(A, 0x00, 0x71);
  wr(A, 0x00, 0x41);
}

void mic_set_gain_db(float db) {
  int idx = db < 0 ? 0 : (int)((db + 0.5f) / 3.0f);
  if (idx > 14) idx = 14;
  mic_gain_idx = idx;
  for (uint8_t r = 0x43; r <= 0x46; r++) upd(ES7210_ADDR, r, 0x0f, mic_gain_idx);
}

void mic_power(bool on) {
  if (on) {
    es7210_mics_on();
  } else {
    wr(ES7210_ADDR, 0x4B, 0xff);  // MIC1/2 bias, ADC and PGA off
    wr(ES7210_ADDR, 0x4C, 0xff);  // MIC3/4
  }
}

// ---- ES8311 --------------------------------------------------------------

static void es8311_begin() {
  const uint8_t A = ES8311_ADDR;
  // open(): slave, clock from BCLK, no internal DAC reference.
  wr(A, 0x44, 0x08);
  wr(A, 0x44, 0x08);
  wr(A, 0x01, 0x30);
  wr(A, 0x02, 0x00);
  wr(A, 0x03, 0x10);
  wr(A, 0x16, 0x24);
  wr(A, 0x04, 0x10);
  wr(A, 0x05, 0x00);
  wr(A, 0x0B, 0x00);
  wr(A, 0x0C, 0x00);
  wr(A, 0x10, 0x1F);
  wr(A, 0x11, 0x7F);
  wr(A, 0x00, 0x80);
  wr(A, 0x00, rd(A, 0x00) & 0xBF);
  wr(A, 0x01, 0xBF);
  wr(A, 0x06, rd(A, 0x06) & ~0x20);
  wr(A, 0x13, 0x10);
  wr(A, 0x1B, 0x0A);
  wr(A, 0x1C, 0x6A);
  wr(A, 0x44, 0x08);

  // set_fs(): 16-bit, standard I2S, 48 kHz. Internal MCLK = BCLK x 8 = 256 fs.
  wr(A, 0x09, (rd(A, 0x09) | 0x0c) & 0xFC);
  wr(A, 0x0A, (rd(A, 0x0A) | 0x0c) & 0xFC);
  wr(A, 0x02, (rd(A, 0x02) & 0x07) | (3 << 3));
  wr(A, 0x05, 0x00);
  wr(A, 0x03, (rd(A, 0x03) & 0x80) | 0x10);
  wr(A, 0x04, (rd(A, 0x04) & 0x80) | 0x10);
  wr(A, 0x07, rd(A, 0x07) & 0xC0);
  wr(A, 0x08, 0xff);
  wr(A, 0x06, (rd(A, 0x06) & 0xE0) | (4 - 1));

  // start(), DAC only.
  wr(A, 0x00, 0x80);
  wr(A, 0x01, 0xBF);
  wr(A, 0x09, rd(A, 0x09) & 0xBF);
  wr(A, 0x0A, rd(A, 0x0A) & 0xBF);
  wr(A, 0x17, 0xBF);
  wr(A, 0x0E, 0x02);
  wr(A, 0x12, 0x00);
  wr(A, 0x14, 0x1A);
  wr(A, 0x14, rd(A, 0x14) & ~0x40);
  wr(A, 0x0D, 0x01);
  wr(A, 0x15, 0x40);
  wr(A, 0x37, 0x08);
  wr(A, 0x45, 0x00);
  wr(A, 0x31, rd(A, 0x31) & 0x9f);  // unmute
}

void spk_set_volume(int percent) {
  if (percent < 0) percent = 0;
  if (percent > 100) percent = 100;
  // 0.5 dB per register step, 0xBF = 0 dB. 100 % is the ceiling: never boost.
  uint8_t reg = percent == 0 ? 0 : 0xBF - (100 - percent);
  wr(ES8311_ADDR, 0x32, reg);
}

void spk_amp(bool on) { digitalWrite(PIN_PA_CTRL, on ? HIGH : LOW); }

// ---- I2S -----------------------------------------------------------------

static void mic_i2s_begin() {
  i2s_config_t cfg = {};
  cfg.mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX);
  cfg.sample_rate = MIC_RATE;
  cfg.bits_per_sample = I2S_BITS_PER_SAMPLE_32BIT;
  cfg.channel_format = I2S_CHANNEL_FMT_RIGHT_LEFT;
  cfg.communication_format = I2S_COMM_FORMAT_STAND_I2S;
  cfg.intr_alloc_flags = ESP_INTR_FLAG_LEVEL1;
  cfg.dma_buf_count = 8;
  cfg.dma_buf_len = 320;
  cfg.use_apll = true;
  cfg.fixed_mclk = MIC_RATE * 256;
  cfg.mclk_multiple = I2S_MCLK_MULTIPLE_256;
  cfg.bits_per_chan = I2S_BITS_PER_CHAN_32BIT;
  i2s_driver_install(MIC_PORT, &cfg, 0, NULL);
  i2s_pin_config_t pins = {};
  pins.mck_io_num = PIN_MIC_MCLK;
  pins.bck_io_num = PIN_MIC_BCLK;
  pins.ws_io_num = PIN_MIC_WS;
  pins.data_out_num = I2S_PIN_NO_CHANGE;
  pins.data_in_num = PIN_MIC_DIN;
  i2s_set_pin(MIC_PORT, &pins);
}

static void spk_i2s_begin() {
  i2s_config_t cfg = {};
  cfg.mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_TX);
  cfg.sample_rate = SPK_RATE;
  cfg.bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT;
  cfg.channel_format = I2S_CHANNEL_FMT_RIGHT_LEFT;
  cfg.communication_format = I2S_COMM_FORMAT_STAND_I2S;
  cfg.intr_alloc_flags = ESP_INTR_FLAG_LEVEL1;
  cfg.dma_buf_count = 8;
  cfg.dma_buf_len = 480;
  cfg.use_apll = false;  // the one APLL belongs to the mic clock
  cfg.tx_desc_auto_clear = true;  // underrun plays silence, not the last buffer
  cfg.bits_per_chan = I2S_BITS_PER_CHAN_16BIT;
  i2s_driver_install(SPK_PORT, &cfg, 0, NULL);
  i2s_pin_config_t pins = {};
  pins.mck_io_num = I2S_PIN_NO_CHANGE;
  pins.bck_io_num = PIN_SPK_BCLK;
  pins.ws_io_num = PIN_SPK_WS;
  pins.data_out_num = PIN_SPK_DOUT;
  pins.data_in_num = I2S_PIN_NO_CHANGE;
  i2s_set_pin(SPK_PORT, &pins);
}

void codec_begin() {
  pinMode(PIN_PA_CTRL, OUTPUT);
  spk_amp(false);
  mic_i2s_begin();  // both codecs are slaves: clocks must run before setup
  spk_i2s_begin();
  delay(10);
  Wire.begin(PIN_I2C_SDA, PIN_I2C_SCL, 100000);
  es7210_begin();
  es8311_begin();
  spk_set_volume(60);
}

size_t mic_read(int16_t *frames, size_t max_frames) {
  size_t got = 0;
  i2s_read(MIC_PORT, frames, max_frames * MIC_CHANNELS * sizeof(int16_t), &got, portMAX_DELAY);
  return got / (MIC_CHANNELS * sizeof(int16_t));
}

size_t spk_write(const int16_t *mono, size_t samples, uint32_t timeout_ms) {
  static int16_t stereo[480 * 2];
  size_t done = 0;
  while (done < samples) {
    size_t n = samples - done;
    if (n > 480) n = 480;
    for (size_t i = 0; i < n; i++) stereo[2 * i] = stereo[2 * i + 1] = mono[done + i];
    size_t wrote = 0;
    i2s_write(SPK_PORT, stereo, n * 4, &wrote, pdMS_TO_TICKS(timeout_ms));
    if (wrote == 0) break;
    done += wrote / 4;
  }
  return done;
}
