#!/usr/bin/python3
"""OpenClip action: read the selection aloud with calliope-player.

Hands the text to ~/.local/share/calliope/calliope-player and returns at once, so OpenClip's
60-second script watchdog never cuts speech off. A new Speak replaces whatever is playing.
Installed into ~/.openclip/extensions/calliope.openclipext by ../install.sh.
"""
import json
import os
import signal
import subprocess
import sys
import tempfile

RUNTIME = os.path.expanduser("~/.local/share/calliope")
# Inside the application, which is where the code lives now. The runtime below
# is still the changing half -- the queue, the pid file, the logs.
# install.sh replaces __APP__ with wherever it put Calliope.app, so this file
# and the installer cannot disagree. Read straight out of the repository it is
# still the placeholder, hence the fallback -- and the check is on the variable
# rather than on a second literal, because a sed that replaced one occurrence
# and not the other left this silently pointing at /Applications.
APP = "__APP__"
if APP.startswith("__"):
    APP = "/Applications/Calliope.app"
PLAYER = os.path.join(APP, "Contents/Helpers/CalliopePlayer.app/Contents/MacOS/calliope-player")
PID_FILE = os.path.join(RUNTIME, "player.pid")
QUEUE_DIR = os.path.join(RUNTIME, "queue")
LOG_FILE = os.path.join(RUNTIME, "player.log")


def stop_current():
    try:
        with open(PID_FILE) as f:
            pid = int(f.read().strip())
    except (FileNotFoundError, ValueError):
        return
    # Guard against a stale pid file whose pid now belongs to another program.
    command = subprocess.run(["/bin/ps", "-o", "command=", "-p", str(pid)],
                             capture_output=True, text=True).stdout
    if PLAYER in command:
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


def main():
    stop_current()
    text = (os.environ.get("OPENCLIP_TEXT") or sys.stdin.read()).strip()
    if not text:
        sys.stdout.write(json.dumps({"type": "toast", "message": "No text to speak", "style": "error"}))
        return

    os.makedirs(QUEUE_DIR, exist_ok=True)
    fd, text_path = tempfile.mkstemp(dir=QUEUE_DIR, suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    with open(LOG_FILE, "w") as log:
        proc = subprocess.Popen([PLAYER, text_path], stdin=subprocess.DEVNULL,
                                stdout=log, stderr=log, start_new_session=True)
    # THE PLAYER WRITES PID_FILE ITSELF, after setsid(), because it is the
    # process that has to be named: killpg only reaches a group leader. Two
    # writers meant the daemon's hotkey and this action each knew only about
    # their own player, and a second Speak read over the first.
    sys.stdout.write(json.dumps({"type": "success"}))


if __name__ == "__main__":
    main()
