# Stamp the firmware with `git describe`, so the hub shows which build a
# satellite runs.
import subprocess

Import("env")  # noqa: F821  (PlatformIO injects it)

try:
    version = subprocess.check_output(
        ["git", "describe", "--always", "--dirty", "--tags"], text=True
    ).strip()
except Exception:
    version = "unknown"

env.Append(CPPDEFINES=[("FW_VERSION", f'\\"{version}\\"')])  # noqa: F821
