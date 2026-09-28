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

// Listen: the ring breathes softly and a brighter arc glides towards the
// talker (direction in degrees, as the hub's direction of arrival; negative
// until there is one), animated here so it never looks like a stalled frame.
// LED 0 is taken to sit at 0 degrees (microphone 1), with the LEDs counting
// the same way round as the microphones. Neither has been checked on a board,
// so the arc may be rotated or mirrored.
enum class Mode : uint8_t { Off, Solid, Pulse, Spin, Pixels, Listen };

void lights_begin();
// Dark overrides every layer, status included: a bedroom satellite stays unlit
// through reboots, reconnects and updates.
void lights_dark(bool dark);
// For ms, the ring shows a level as a clock bar: `lit` LEDs on from the top,
// clockwise as the board is mounted, the rest faint. Over everything but the
// dark, which a bedroom satellite keeps.
void lights_level(int lit, uint32_t ms);
// Which LED is at 12 o'clock as the board is mounted, and whether the LEDs'
// order runs anticlockwise as seen (upside down).
void lights_ring(int top, bool upside_down);
// Percent of whatever is shown, the privacy mute's red kept at a quarter or
// more so that it is never too dim to see.
void lights_brightness(int percent);
void lights_status(Status s);
void lights_muted(bool muted);
void lights_identify(uint32_t ms);
void lights_ota(int percent);  // -1 clears
// Hub layer. pixels is LED_COUNT * 3 bytes when mode is Pixels; direction is
// read in Listen only.
void lights_hub(Mode mode, uint8_t r, uint8_t g, uint8_t b, uint8_t brightness, const uint8_t *pixels,
                float direction = -1);
