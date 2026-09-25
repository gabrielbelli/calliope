// The one connection to the hub: control as JSON text frames, audio and
// firmware as binary frames. Protocol: services/satellites/README.md.
#pragma once
#include <stdint.h>

// Binary frame kinds (first byte).
#define FRAME_MIC 1       // device -> hub: 16-byte header + s16le frames
#define FRAME_SPEAKER 2   // hub -> device: 16-byte header + mono s16le
#define FRAME_FIRMWARE 3  // hub -> device: 8-byte header (kind, pad, offset) + image bytes
#define FRAME_EARCON 4    // hub -> device: same 8-byte header + earcon bytes (earcon_put)
#define FRAME_HEADER 16

void hub_begin();  // after Wi-Fi is up
void hub_loop();   // from loop()
bool hub_adopted();
bool hub_connected();
void hub_send_button(const char *name, const char *action, uint32_t held_ms);
void hub_send_status();
