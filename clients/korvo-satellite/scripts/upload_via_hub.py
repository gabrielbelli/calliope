# `pio run -e ota -t upload`: instead of esptool, hand the image to the hub and
# ask it to update a satellite. The satellite pulls the image over its own
# connection and only keeps it once it has reached the hub again (rollback
# otherwise).
#
# The image is signed here, on the developer's machine, with the private key at
# CALLIOPE_SIGNING_KEY (default ~/.config/calliope/firmware-signing.pem). The
# hub only carries the signature. A satellite built with a public key checks it
# itself, so whoever controls the hub still cannot install their own firmware.
import json
import os
import ssl
import sys
import urllib.parse
import urllib.request

Import("env")  # noqa: F821

project = env.subst("$PROJECT_DIR")  # noqa: F821
sys.path.insert(0, os.path.join(project, "scripts"))  # SCons scripts have no __file__
import firmware_signing  # noqa: E402


def call(url, data, key, content_type):
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", content_type)
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    with urllib.request.urlopen(req, context=ssl.create_default_context(), timeout=60) as r:
        return json.loads(r.read() or b"{}")


def fw_version(env):
    # A define is a (name, value) pair or a bare name, depending on how it was
    # added, so unpacking every entry as a pair breaks on the first bare one.
    for d in env["CPPDEFINES"]:
        if isinstance(d, (tuple, list)) and len(d) == 2 and d[0] == "FW_VERSION":
            return str(d[1]).strip('\\"')
    return "unknown"


def upload(source, target, env):
    base = os.environ.get("CALLIOPE_URL", "https://orko.gabrielbelli.com:30080").rstrip("/")
    satellite = os.environ.get("CALLIOPE_SATELLITE")
    key = os.environ.get("CALLIOPE_API_KEY")
    if not satellite:
        sys.exit("CALLIOPE_SATELLITE is not set: a satellite id, its name, or 'all'")
    version = fw_version(env)
    with open(str(source[0]), "rb") as f:
        image = f.read()
    query = {"model": "esp32-korvo-v1.1", "version": version}
    try:
        sig = firmware_signing.sign_for_upload(
            image,
            os.environ.get("CALLIOPE_SIGNING_KEY") or firmware_signing.DEFAULT_PRIVATE_KEY,
            os.environ.get("CALLIOPE_FIRMWARE_PUBKEY")
            or os.path.join(project, "keys", "firmware-signing.pub.pem"),
        )
    except firmware_signing.SigningError as e:
        sys.exit(str(e))
    if sig:
        query["signature"] = sig
    fw = call(f"{base}/satellites/firmware?{urllib.parse.urlencode(query)}",
              image, key, "application/octet-stream")
    print(f"hub stored firmware {fw['sha256'][:12]} ({fw['size']} bytes, {version})")
    res = call(f"{base}/satellites/ota",
               json.dumps({"satellite": satellite, "sha256": fw["sha256"]}).encode(),
               key, "application/json")
    for n in res.get("started", []):
        print(f"updating {n}")
    for n, why in res.get("skipped", {}).items():
        print(f"skipped {n}: {why}")


env.Replace(UPLOADCMD=upload)  # noqa: F821
