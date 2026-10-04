#!/usr/bin/env bash
# Makes a local code-signing identity, "Calliope Local Signing", in the login
# Keychain, which install.sh then signs with instead of ad hoc.
#
# WHY IT EXISTS: PERMISSIONS THAT SURVIVE A REBUILD. macOS grants Accessibility
# -- which the hotkey needs to read a selection -- to a program as it is signed.
# An ad-hoc signature is a hash of the build itself, so every install from
# source is a different program to macOS: the hotkey stops working, and the
# entry in System Settings has to be removed and added again. A certificate
# signs every build the same way, so the grant is made once and kept.
#
# It is local and self-signed: enough for this Mac to recognise its own builds,
# and nothing more. It cannot make a build Gatekeeper accepts elsewhere -- that
# still takes a Developer ID and notarisation.
#
# macOS asks for your password once, to trust the certificate for code signing.
# Run it once; it does nothing when the identity is already there.
set -euo pipefail

name="Calliope Local Signing"
keychain="$HOME/Library/Keychains/login.keychain-db"

if security find-identity -v -p codesigning | grep -q "\"$name\""; then
    echo "$name is already in the login Keychain."
    exit 0
fi

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
chmod 700 "$work"

cat > "$work/openssl.cnf" <<EOF
[req]
distinguished_name = dn
x509_extensions = ext
prompt = no
[dn]
CN = $name
[ext]
basicConstraints = critical, CA:false
keyUsage = critical, digitalSignature
extendedKeyUsage = critical, codeSigning
EOF

# The private key never leaves this machine and is never written anywhere but
# this temporary directory and the Keychain; it is unencrypted only for the
# moment between the two.
openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -sha256 \
    -config "$work/openssl.cnf" -keyout "$work/key.pem" -out "$work/cert.pem" 2>/dev/null

# THE CLASSIC RSA FORM, because security import does not read the PKCS #8
# form openssl writes by default: measured, "Unknown format in import".
openssl rsa -in "$work/key.pem" -out "$work/rsa.pem" -traditional 2>/dev/null \
    || openssl rsa -in "$work/key.pem" -out "$work/rsa.pem" 2>/dev/null

# codesign is allowed to use the key without asking each time.
security import "$work/rsa.pem" -k "$keychain" -t priv -f openssl -T /usr/bin/codesign >/dev/null
security import "$work/cert.pem" -k "$keychain" -t cert >/dev/null

echo "macOS will ask for your password to trust \"$name\" for code signing."
security add-trusted-cert -r trustRoot -p codeSign -k "$keychain" "$work/cert.pem"

security find-identity -v -p codesigning | grep -q "\"$name\"" \
    && echo "Done: install.sh now signs with \"$name\"." \
    || { echo "The identity is not valid for code signing; nothing else was changed." >&2; exit 1; }
