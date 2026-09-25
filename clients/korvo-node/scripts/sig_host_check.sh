#!/usr/bin/env bash
# Builds the node's signature check (src/ota_sig.cpp) on this machine against
# mbedTLS 2.28.7, the release in the ESP32 Arduino core 2.0.17, and runs it on
# good and bad signatures made with a throwaway key. No board is involved.
#
#   clients/korvo-node/scripts/sig_host_check.sh
#
# Needs a C++ compiler, make, curl, openssl and python3. MBEDTLS_DIR reuses an
# unpacked mbedtls-2.28.7 source tree instead of downloading one.
set -euo pipefail

node=$(cd "$(dirname "$0")/.." && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

mbedtls=${MBEDTLS_DIR:-}
if [ -z "$mbedtls" ]; then
  curl -fsSL https://github.com/Mbed-TLS/mbedtls/archive/refs/tags/v2.28.7.tar.gz | tar xz -C "$work"
  mbedtls=$work/mbedtls-2.28.7
fi
[ -f "$mbedtls/library/libmbedcrypto.a" ] ||
  make -s -C "$mbedtls/library" -j8 libmbedcrypto.a CFLAGS="-O2 -w"

# Throwaway keys, only in the temporary directory.
openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 -out "$work/key.pem" 2>/dev/null
openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 -out "$work/other.pem" 2>/dev/null
openssl pkey -in "$work/key.pem" -pubout -out "$work/key.pub.pem"

# The same header scripts/firmware_key.py generates for a firmware build.
mkdir -p "$work/gen"
python3 - "$node/scripts" "$work/key.pub.pem" "$work/gen/firmware_key.h" <<'EOF'
import sys
sys.path.insert(0, sys.argv[1])
import firmware_signing as fs
der = fs.pem_to_der(open(sys.argv[2]).read())
fs.check_p256_public_key(der)
body = ", ".join(f"0x{b:02x}" for b in der)
open(sys.argv[3], "w").write(
    "#pragma once\n"
    f'#define FIRMWARE_KEY_ID "{fs.key_id(der)}"\n'
    f"static const unsigned char FIRMWARE_KEY_DER[{len(der)}] = {{{body}}};\n")
EOF

c++ -std=c++17 -Wall -Wextra -Werror -DFIRMWARE_SIGNING -I"$work/gen" -I"$node/src" \
  -I"$mbedtls/include" "$node/src/ota_sig.cpp" "$node/scripts/sig_host_check.cpp" \
  "$mbedtls/library/libmbedcrypto.a" -o "$work/check"

head -c 1100000 /dev/urandom > "$work/image.bin"
openssl dgst -sha256 -sign "$work/key.pem" -out "$work/good.sig" "$work/image.bin"
openssl dgst -sha256 -sign "$work/other.pem" -out "$work/other.sig" "$work/image.bin"

python3 - "$work" <<'EOF'
import base64, hashlib, subprocess, sys
work = sys.argv[1]
image = open(f"{work}/image.bin", "rb").read()
good, other = open(f"{work}/good.sig", "rb").read(), open(f"{work}/other.sig", "rb").read()
digest = hashlib.sha256(image).hexdigest()
url = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()
std = lambda b: base64.b64encode(b).decode()
flipped = bytearray(good); flipped[-1] ^= 1
cases = [
    (1, "good, base64url unpadded (the upload script)", digest, url(good)),
    (1, "good, padded base64 (the ota message)", digest, std(good)),
    (1, "good, upper-case digest", digest.upper(), url(good)),
    (0, "signature of another image", hashlib.sha256(image + b"x").hexdigest(), url(good)),
    (0, "one bit flipped", digest, url(bytes(flipped))),
    (0, "signed by another key", digest, url(other)),
    (0, "truncated DER", digest, url(good[:-3])),
    (0, "a byte after the DER", digest, url(good + b"\0")),
    (0, "not base64", digest, "!!not base64!!"),
    (0, "empty", digest, ""),
    (0, "data after the padding", digest, std(good) + "=AAAA"),
]
failed = 0
for want, name, d, sig in cases:
    got = int(subprocess.run([f"{work}/check", d, sig], capture_output=True, text=True,
                             check=True).stdout.strip())
    ok = got == want
    failed += not ok
    print(f"{'ok  ' if ok else 'FAIL'} {'accept' if got else 'refuse'}  {name}")
sys.exit(1 if failed else 0)
EOF
