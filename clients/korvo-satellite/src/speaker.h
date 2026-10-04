// The speaker path. One task owns the I2S port: it plays the hub's audio from a
// ring buffer, mixes any earcon over it, and applies ducking to the hub's
// audio only.
#pragma once
#include <stddef.h>
#include <stdint.h>

void speaker_begin();

// Hub audio, mono s16le at SPK_RATE. False when there is no room (a drop).
bool speaker_push(const uint8_t *pcm, size_t len);
void speaker_flush();  // drops queued hub audio (barge-in); an earcon plays on
uint32_t speaker_buffered_ms();

// Plays pcm at once, over whatever the hub is playing, and replaces an earcon
// that is still sounding. Takes ownership: the buffer must come from
// heap_caps_malloc and is freed when played, or at once if this returns false.
bool speaker_play(int16_t *pcm, size_t samples);

// Lowers the hub's audio to `level`, on the same 0-100 scale as the volume and
// never above it, for `ms` milliseconds, or until speaker_unduck() when ms is 0.
// Earcons are not ducked, so a chime over lowered speech is still heard. Nothing
// is written to NVS: a duck is short-lived, and flash wears.
void speaker_duck(int level, uint32_t ms);
void speaker_unduck();
int speaker_duck_level();  // -1 when not ducked
void speaker_loop();       // from loop(): ends a timed duck
