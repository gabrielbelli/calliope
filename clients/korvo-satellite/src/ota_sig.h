// Firmware signatures: ECDSA P-256 over the image's SHA-256, checked against
// the public key scripts/firmware_key.py compiles in from
// keys/firmware-signing.pub.pem. A build without that file defines no
// FIRMWARE_SIGNING and accepts unsigned images (development only).
//
// Nothing here uses Arduino, so the file also builds on a desktop against the
// same mbedTLS release (2.28) to test it without a board.
#pragma once
#include <stddef.h>
#include <stdint.h>

// A DER ECDSA P-256 signature: a SEQUENCE of two INTEGERs of up to 33 bytes.
#define SIG_MAX_DER 72

bool sig_required();       // true when built with a public key
const char *sig_key_id();  // first 16 hex of the key's SHA-256, "" when unsigned

// base64 or base64url, padded or not. Returns the byte count, or 0 when the
// text is not base64 or does not fit in cap.
size_t sig_decode(const char *text, uint8_t *out, size_t cap);

// Parses 64 hex digits into 32 bytes.
bool sig_hex32(const char *hex, uint8_t out[32]);

// True only when sig is a valid signature of digest by the compiled-in key.
// Always false in a build without one.
bool sig_verify(const uint8_t digest[32], const uint8_t *sig, size_t len);
