// The LED ring. A background task animates whichever layer is on top:
// privacy mute > OTA > identify > local status > whatever the hub asked for.
#pragma once
#include <stdint.h>

enum class Status : uint8_t {
  Booting,
  Portal,       // Wi-Fi setup access point is open
  Connecting,   // joining Wi-Fi or reaching the hub
  Pending,      // connected, waiting to be adopted
  Ready,        // adopted: the hub owns the ring
  HubLost,      // was adopted, hub unreachable
};

enum class Mode : uint8_t { Off, Solid, Pulse, Spin, Pixels };

void lights_begin();
void lights_status(Status s);
void lights_muted(bool muted);
void lights_identify(uint32_t ms);
void lights_ota(int percent);  // -1 clears
// Hub layer. pixels is LED_COUNT * 3 bytes when mode is Pixels.
void lights_hub(Mode mode, uint8_t r, uint8_t g, uint8_t b, uint8_t brightness, const uint8_t *pixels);
