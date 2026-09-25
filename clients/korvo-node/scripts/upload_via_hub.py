# `pio run -e ota -t upload`: instead of esptool, hand the image to the hub and
# ask it to update a node. The node pulls the image over its own connection and
# only keeps it once it has reached the hub again (rollback otherwise).
import json
import os
import ssl
import sys
import urllib.request

Import("env")  # noqa: F821


def call(url, data, key, content_type):
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", content_type)
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    with urllib.request.urlopen(req, context=ssl.create_default_context(), timeout=60) as r:
        return json.loads(r.read() or b"{}")


def upload(source, target, env):
    base = os.environ.get("CALLIOPE_URL", "https://orko.gabrielbelli.com:30080").rstrip("/")
    node = os.environ.get("CALLIOPE_NODE")
    key = os.environ.get("CALLIOPE_API_KEY")
    if not node:
        sys.exit("CALLIOPE_NODE is not set: a node id, its name, or 'all'")
    image = str(source[0])
    version = next(
        (v for k, v in env["CPPDEFINES"] if isinstance(k, str) and k == "FW_VERSION"), "unknown"
    ).strip('\\"')
    with open(image, "rb") as f:
        fw = call(
            f"{base}/nodes/firmware?model=esp32-korvo-v1.1&version={version}",
            f.read(), key, "application/octet-stream",
        )
    print(f"hub stored firmware {fw['sha256'][:12]} ({fw['size']} bytes, {version})")
    res = call(f"{base}/nodes/ota", json.dumps({"node": node, "sha256": fw["sha256"]}).encode(),
               key, "application/json")
    for n in res.get("started", []):
        print(f"updating {n}")
    for n, why in res.get("skipped", {}).items():
        print(f"skipped {n}: {why}")


env.Replace(UPLOADCMD=upload)  # noqa: F821
