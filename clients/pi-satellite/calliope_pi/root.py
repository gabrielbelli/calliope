"""calliope-root: the few things the satellite does as root, and nothing else.

The agent runs as the `calliope` user. sudoers lets that user run this
command, and only this one, without a password (bundle/sudoers.d/calliope):

  calliope-root install FILE SIGNATURE   verify and install a bundle the agent received
  calliope-root restart-agent            start the agent again, on the release now current
  calliope-root rollback-check           put the previous release back if the new one is overdue
  calliope-root reboot
  calliope-root wifi-setup               open the Wi-Fi setup network (portal.py)

`install` checks the signature itself, on its own copy of the file: the agent
is not trusted to have done it, so a compromised agent still cannot run code
as root. The file must be in the agent's incoming directory."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from . import bundle, paths


def _systemctl(*args: str) -> None:
    subprocess.run(["systemctl", *args], check=False)


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    cmd, args = argv[0], argv[1:]
    try:
        if cmd == "install" and len(args) == 2:
            path = Path(args[0]).resolve()
            if path.parent != paths.incoming().resolve():
                raise bundle.BundleError(f"{path} is not in {paths.incoming()}")
            data = path.read_bytes()  # one read: what is verified is what is unpacked
            man = bundle.install(data, args[1])
            bundle.prune()
            print(json.dumps({"installed": man["version"]}))
            return 0
        if cmd == "restart-agent" and not args:
            _systemctl("--no-block", "restart", "calliope-agent.service")
            return 0
        if cmd == "rollback-check" and not args:
            back = bundle.rollback_if_due()
            if back:
                print(json.dumps({"rolled_back_to": back}))
                _systemctl("--no-block", "restart", "calliope-agent.service")
            return 0
        if cmd == "reboot" and not args:
            _systemctl("--no-block", "reboot")
            return 0
        if cmd == "wifi-setup" and not args:
            _systemctl("--no-block", "start", "calliope-portal.service")
            return 0
    except (bundle.BundleError, OSError) as e:
        print(json.dumps({"error": str(e)}))
        return 1
    print(f"calliope-root: unknown command {' '.join(argv)!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
