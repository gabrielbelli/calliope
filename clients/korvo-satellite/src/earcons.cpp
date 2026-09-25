#include "earcons.h"

#include <Arduino.h>
#include <LittleFS.h>
#include <esp_heap_caps.h>
#include <mbedtls/sha256.h>

#include "ota_sig.h"  // sig_hex32
#include "speaker.h"

// Each earcon is <id>.pcm with its SHA-256 beside it in <id>.sha, so listing
// never reads audio back to hash it. An upload is written to <id>.part and
// renamed only once its hash matches.
static const char *DIR = "/e";
static volatile bool ready = false;
static uint32_t last_load_us = 0;

static struct {
  bool active = false;
  char id[EARCON_ID_MAX + 1] = "";
  char sha[65] = "";
  uint32_t size = 0, offset = 0, last_rx = 0;
  File f;
  mbedtls_sha256_context ctx;
} put;

static bool valid_id(const char *id) {
  size_t n = id ? strlen(id) : 0;
  if (!n || n > EARCON_ID_MAX) return false;
  for (size_t i = 0; i < n; i++) {
    char c = id[i];
    if (!((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '_' || c == '-')) return false;
  }
  return true;
}

static String path(const char *id, const char *ext) { return String(DIR) + "/" + id + ext; }

static String read_sha(const String &id) {
  File f = LittleFS.open(path(id.c_str(), ".sha"), "r");
  String s = f ? f.readString() : String();
  s.trim();
  return s.length() == 64 ? s : String();  // missing or torn: the hub sees a mismatch and re-sends
}

// Names in DIR ending in ext, without it.
static int names(const char *ext, String *out, int cap) {
  File d = LittleFS.open(DIR);
  int n = 0;
  if (!d || !d.isDirectory()) return 0;
  size_t el = strlen(ext);
  for (File f = d.openNextFile(); f; f = d.openNextFile()) {
    String name = f.name();
    if (name.endsWith(ext) && n < cap) out[n++] = name.substring(0, name.length() - el);
  }
  return n;
}

static void mount_task(void *) {
  if (LittleFS.begin(true, "/littlefs", 4, "storage")) {
    if (!LittleFS.exists(DIR)) LittleFS.mkdir(DIR);
    String parts[EARCON_MAX_COUNT];  // uploads cut off by a reboot
    int n = names(".part", parts, EARCON_MAX_COUNT);
    for (int i = 0; i < n; i++) LittleFS.remove(path(parts[i].c_str(), ".part"));
    ready = true;
  }
  vTaskDelete(nullptr);
}

void earcons_begin() {
  // Core 0 beside the lights: the format disables core 0's watchdog while it
  // runs (LittleFS.cpp), and the audio tasks live on core 1.
  xTaskCreatePinnedToCore(mount_task, "earcons", 8192, nullptr, 1, nullptr, 0);
}

bool earcons_ready() { return ready; }

static bool stored(const char *id) { return LittleFS.exists(path(id, ".pcm")); }

static int count() {
  String ids[EARCON_MAX_COUNT + 1];
  return names(".pcm", ids, EARCON_MAX_COUNT + 1);
}

const char *earcon_put_begin(const char *id, uint32_t size, const char *sha256) {
  if (!ready) return "storage not ready";
  if (put.active) return "busy";
  if (!valid_id(id)) return "bad id";
  if (!size || size > EARCON_MAX_BYTES || size % 2) return "bad size";
  uint8_t unused[32];
  if (!sig_hex32(sha256, unused)) return "bad sha256";
  if (!stored(id) && count() >= EARCON_MAX_COUNT) return "full";
  put.f = LittleFS.open(path(id, ".part"), "w");
  if (!put.f) return "cannot write";
  strlcpy(put.id, id, sizeof(put.id));
  for (int i = 0; i < 65; i++) put.sha[i] = tolower(sha256[i]);
  put.size = size;
  put.offset = 0;
  put.last_rx = millis();
  mbedtls_sha256_init(&put.ctx);
  mbedtls_sha256_starts(&put.ctx, 0);
  put.active = true;
  return nullptr;
}

static void put_end(bool keep_part) {
  if (put.f) put.f.close();
  if (!keep_part) LittleFS.remove(path(put.id, ".part"));
  mbedtls_sha256_free(&put.ctx);
  put.active = false;
}

static PutResult put_fail(const char *why, const char **error) {
  put_end(false);
  *error = why;
  return PutResult::Failed;
}

PutResult earcon_put_chunk(uint32_t offset, const uint8_t *data, size_t len, const char **error) {
  if (!put.active || offset != put.offset || !len) return PutResult::Ignored;
  if (len > put.size - put.offset) return put_fail("longer than announced", error);
  put.last_rx = millis();
  if (put.f.write(data, len) != len) return put_fail("write failed", error);
  mbedtls_sha256_update(&put.ctx, data, len);
  put.offset += len;
  if (put.offset < put.size) return PutResult::Next;

  uint8_t digest[32];
  char hex[65];
  mbedtls_sha256_finish(&put.ctx, digest);
  for (int i = 0; i < 32; i++) sprintf(hex + 2 * i, "%02x", digest[i]);
  if (strcmp(hex, put.sha)) return put_fail("sha256 mismatch", error);
  put_end(true);
  // The old hash goes first: a reboot between these steps leaves an earcon
  // with no hash, which the hub replaces, and never a hash that lies.
  LittleFS.remove(path(put.id, ".sha"));
  LittleFS.remove(path(put.id, ".pcm"));
  if (!LittleFS.rename(path(put.id, ".part"), path(put.id, ".pcm"))) {
    LittleFS.remove(path(put.id, ".part"));
    *error = "rename failed";
    return PutResult::Failed;
  }
  File s = LittleFS.open(path(put.id, ".sha"), "w");
  if (!s || s.print(put.sha) != 64) {
    *error = "write failed";
    return PutResult::Failed;
  }
  return PutResult::Stored;
}

bool earcon_put_active() { return put.active; }
const char *earcon_put_id() { return put.id; }
uint32_t earcon_put_offset() { return put.offset; }
uint32_t earcon_put_size() { return put.size; }
const char *earcon_put_sha256() { return put.sha; }
uint32_t earcon_put_idle_ms() { return millis() - put.last_rx; }

void earcon_put_abort() {
  if (put.active) put_end(false);
}

void earcon_list(JsonArray items) {
  if (!ready) return;
  String ids[EARCON_MAX_COUNT + 1];
  int n = names(".pcm", ids, EARCON_MAX_COUNT + 1);
  for (int i = 0; i < n; i++) {
    File f = LittleFS.open(path(ids[i].c_str(), ".pcm"), "r");
    JsonObject o = items.add<JsonObject>();
    o["id"] = ids[i];
    o["size"] = f ? f.size() : 0;
    o["sha256"] = read_sha(ids[i]);
  }
}

const char *earcon_delete(const char *id) {
  if (!ready) return "storage not ready";
  if (!valid_id(id)) return "bad id";
  if (put.active && !strcmp(put.id, id)) put_end(false);
  if (!stored(id)) return "unknown earcon";
  LittleFS.remove(path(id, ".sha"));
  return LittleFS.remove(path(id, ".pcm")) ? nullptr : "delete failed";
}

const char *earcon_play(const char *id) {
  if (!ready) return "storage not ready";
  if (!valid_id(id)) return "bad id";
  uint32_t t0 = micros();
  File f = LittleFS.open(path(id, ".pcm"), "r");
  if (!f) return "unknown earcon";
  size_t n = f.size();
  if (!n || n > EARCON_MAX_BYTES || n % 2) return "bad file";
  int16_t *pcm = (int16_t *)heap_caps_malloc(n, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
  if (!pcm) pcm = (int16_t *)heap_caps_malloc(n, MALLOC_CAP_8BIT);
  if (!pcm) return "out of memory";
  if (f.read((uint8_t *)pcm, n) != n) {
    heap_caps_free(pcm);
    return "read failed";
  }
  last_load_us = micros() - t0;
  return speaker_play(pcm, n / 2) ? nullptr : "speaker busy";
}

uint32_t earcon_last_load_us() { return last_load_us; }
