// Runs the node's own signature check (src/ota_sig.cpp) on a desktop.
// Usage: sig_host_check <sha256 hex> <signature base64 or base64url>
// Prints "1" when the node would accept the signature and "0" when it would not.
// Driven by sig_host_check.sh; not part of the firmware build.
#include <stdio.h>

#include "ota_sig.h"

int main(int argc, char **argv) {
  if (argc != 3) {
    fprintf(stderr, "usage: %s <sha256 hex> <signature>\n", argv[0]);
    return 2;
  }
  uint8_t digest[32], sig[SIG_MAX_DER];
  if (!sig_hex32(argv[1], digest)) return 2;
  size_t n = sig_decode(argv[2], sig, sizeof(sig));
  puts(sig_verify(digest, sig, n) ? "1" : "0");
  return 0;
}
