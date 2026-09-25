// ESP32-Korvo v1.1 wiring, from Espressif's schematics
// (ESP32-KORVO_V1.1 and ESP32-KORVO-MIC_V1.1, sheets 2-4).
#pragma once

#define BOARD_MODEL "esp32-korvo-v1.1"

// I2C bus shared by both codecs.
#define PIN_I2C_SDA 19
#define PIN_I2C_SCL 32
#define ES7210_ADDR 0x40  // 4-channel ADC: three mics and the speaker loopback
#define ES8311_ADDR 0x18  // DAC for the speaker and the headphone jack

// I2S1, microphones. The ES7210 is the clock slave and packs four 16-bit slots
// into each 64-bit stereo frame (TDM).
#define PIN_MIC_MCLK 0
#define PIN_MIC_BCLK 27
#define PIN_MIC_WS 26
#define PIN_MIC_DIN 36

// I2S0, speaker. No MCLK: the ES8311 derives its clock from BCLK.
#define PIN_SPK_BCLK 25
#define PIN_SPK_WS 22
#define PIN_SPK_DOUT 13

// Speaker amplifier enable. Plugging in headphones gates it off in hardware
// (Q16 + U19), and the jack detect is not wired to any GPIO.
#define PIN_PA_CTRL 12

// Twelve WS2812C on the mic board, daisy-chained.
#define PIN_LEDS 33
#define LED_COUNT 12

// Six buttons on one resistor ladder. Voltages from the mic board sheet.
#define PIN_BUTTONS 39
