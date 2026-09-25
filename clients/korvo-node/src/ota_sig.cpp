#include "ota_sig.h"

#include <mbedtls/base64.h>
#include <mbedtls/pk.h>
#include <string.h>

#ifdef FIRMWARE_SIGNING
#include "firmware_key.h"  // generated into the build directory
#endif

bool sig_required() {
#ifdef FIRMWARE_SIGNING
  return true;
#else
  return false;
#endif
}

const char *sig_key_id() {
#ifdef FIRMWARE_SIGNING
  return FIRMWARE_KEY_ID;
#else
  return "";
#endif
}

size_t sig_decode(const char *text, uint8_t *out, size_t cap) {
  // mbedTLS reads only the standard alphabet with padding. The upload script
  // sends base64url, and a hub may pass it on unchanged, so accept both.
  char buf[128];
  size_t n = 0;
  bool padding = false;
  for (const char *p = text; *p; p++) {
    char c = *p;
    if (c == '=') {
      padding = true;
      continue;
    }
    if (padding) return 0;  // data after padding
    if (n + 4 > sizeof(buf)) return 0;
    buf[n++] = c == '-' ? '+' : c == '_' ? '/' : c;
  }
  if (!n || n % 4 == 1) return 0;
  while (n % 4) buf[n++] = '=';
  size_t olen = 0;
  if (mbedtls_base64_decode(out, cap, &olen, (const unsigned char *)buf, n) != 0) return 0;
  return olen;
}

bool sig_hex32(const char *hex, uint8_t out[32]) {
  if (!hex || strlen(hex) != 64) return false;
  for (int i = 0; i < 64; i++) {
    char c = hex[i];
    int v = c >= '0' && c <= '9' ? c - '0' : c >= 'a' && c <= 'f' ? c - 'a' + 10
          : c >= 'A' && c <= 'F' ? c - 'A' + 10 : -1;
    if (v < 0) return false;
    if (i % 2) out[i / 2] |= v;
    else out[i / 2] = v << 4;
  }
  return true;
}

bool sig_verify(const uint8_t digest[32], const uint8_t *sig, size_t len) {
#ifdef FIRMWARE_SIGNING
  if (!sig || !len || len > SIG_MAX_DER) return false;
  mbedtls_pk_context pk;
  mbedtls_pk_init(&pk);
  // Parsed on every call rather than kept: it runs once or twice per update,
  // and a context held for the node's lifetime is heap for nothing.
  bool ok = mbedtls_pk_parse_public_key(&pk, FIRMWARE_KEY_DER, sizeof(FIRMWARE_KEY_DER)) == 0 &&
            mbedtls_pk_can_do(&pk, MBEDTLS_PK_ECDSA) &&
            mbedtls_pk_verify(&pk, MBEDTLS_MD_SHA256, digest, 32, sig, len) == 0;
  mbedtls_pk_free(&pk);
  return ok;
#else
  (void)digest;
  (void)sig;
  (void)len;
  return false;
#endif
}
