#!/usr/bin/python3
"""OpenClip action: read the selection aloud with Calliope.

A thin caller of the `calliope` command inside Calliope.app: the text goes to `calliope speak -`
on standard input, which queues it, replaces whatever is playing, starts the player and returns
at once -- so OpenClip's 60-second script watchdog never cuts speech off.
Installed into ~/.openclip/extensions/calliope.openclipext by ../install.sh.

ONE WAY TO START THE PLAYER, NOT TWO. This file used to carry its own copy of the queue, the pid
file and the stale-pid guard, and the command line would have needed a second copy of the same.
Two copies of "stop the player that is speaking" is how a second Speak ends up reading over the
first, so the command owns it and this only translates its answer into OpenClip's JSON.
"""
import json
import os
import subprocess
import sys

# install.sh replaces __APP__ with wherever it put Calliope.app, so this file
# and the installer cannot disagree. Read straight out of the repository it is
# still the placeholder, hence the fallback -- and the check is on the variable
# rather than on a second literal, because a sed that replaced one occurrence
# and not the other left this silently pointing at /Applications.
APP = "__APP__"
if APP.startswith("__"):
    APP = "/Applications/Calliope.app"
CLI = os.path.join(APP, "Contents/Resources/cli/calliope")


def reply(message=None):
    if message is None:
        sys.stdout.write(json.dumps({"type": "success"}))
    else:
        sys.stdout.write(json.dumps({"type": "toast", "message": message, "style": "error"}))


def main():
    text = os.environ.get("OPENCLIP_TEXT") or sys.stdin.read()
    # Empty text is still handed over: the command stops what is playing before it says there
    # is nothing to speak, which is what Speak with nothing selected has always done.
    try:
        result = subprocess.run([CLI, "speak", "-"], input=text, capture_output=True,
                                text=True, timeout=30)
    except FileNotFoundError:
        return reply("Calliope is not installed at %s" % APP)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return reply("Calliope did not start: %s" % exc)
    if result.returncode != 0:
        lines = [line for line in result.stderr.splitlines() if line.strip()]
        message = lines[-1] if lines else "Speak failed"
        return reply(message[len("calliope: "):] if message.startswith("calliope: ") else message)
    return reply()


if __name__ == "__main__":
    main()
