// Earcons: short feedback sounds kept in flash (LittleFS on the "storage"
// partition), so the satellite plays them from local storage, with no audio
// crossing the network. Files are raw mono s16le at SPK_RATE, uploaded by the
// hub.
#pragma once
#include <ArduinoJson.h>
#include <stddef.h>
#include <stdint.h>

#define EARCON_MAX_BYTES 192000  // 2 s of 48 kHz mono s16le
#define EARCON_MAX_COUNT 16
#define EARCON_ID_MAX 24  // [a-z0-9_-], so an id is always a safe file name

// Mounts in a background task: the first mount after the partition was blank
// formats it, and boot must not wait for that.
void earcons_begin();
bool earcons_ready();

// One upload at a time, pulled a chunk at a time like firmware.
const char *earcon_put_begin(const char *id, uint32_t size, const char *sha256);  // nullptr = started
enum class PutResult : uint8_t {
  Ignored,  // stale or duplicate chunk, or no upload active
  Next,     // accepted; ask for earcon_put_offset()
  Stored,   // complete, verified and in place
  Failed,   // *error says why; the upload is over
};
PutResult earcon_put_chunk(uint32_t offset, const uint8_t *data, size_t len, const char **error);
bool earcon_put_active();
const char *earcon_put_id();
uint32_t earcon_put_offset();
uint32_t earcon_put_size();
const char *earcon_put_sha256();
uint32_t earcon_put_idle_ms();
void earcon_put_abort();

void earcon_list(JsonArray items);
const char *earcon_delete(const char *id);  // nullptr = deleted
const char *earcon_play(const char *id);    // nullptr = playing
uint32_t earcon_last_load_us();             // flash to memory, for the last earcon played
