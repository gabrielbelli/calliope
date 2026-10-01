"""The one way a process of the local stack is started.

    python launch.py --marker M --parent PID --deadline S --violations FILE \
        (--app app.main:app --app-dir DIR --port N | --fakes ARGS...)

Every child of the harness is this script, so three guarantees hold for all of
them without each service knowing about any of it.

IT CANNOT REACH ANYTHING BUT THIS MACHINE'S LOOPBACK. The page's server, the
gateway and the hub all make outbound calls in production -- to backends, to
Home Assistant, to a weather service, to GitHub for a wake word model -- and a
test run on the user's Mac sits on the same LAN as the live hub. An environment
variable pointing each service at a fake is a convention; this is a wall. Name
resolution answers only for loopback names (and for the IANA documentation
domains, which the page server's URL guard insists on resolving to a public
address before it will hand a link to MeTube), and a connect to any address
outside 127.0.0.0/8 or ::1 is refused before a packet leaves. Each refusal is
appended to FILE, so the session can say what the stack tried to reach.

The wall only holds on Python's own socket module, which is why uvicorn is run
with loop="asyncio": uvloop connects in C, under libuv, where none of this is
seen.

IT DIES WITH ITS PARENT. Every child is started in a session of its own so the
harness can kill its whole process group, and the price of that is that
nothing kills it for the harness: a SIGKILLed pytest would leave a hub, a
gateway and a page server listening for ever. A thread here watches the parent
pid and exits when it goes; another exits at the deadline whatever happens.

IT IS FINDABLE. --marker is on the command line so `ps` can name every process
a session started, which is what the orphan sweep and the leak check look for.
"""

from __future__ import annotations

import argparse
import datetime
import ipaddress
import os
import signal
import socket
import sys
import threading
import time

# The answer for a documentation name (example.com and friends): a public
# address the page server's URL guard accepts. Nothing ever connects to it --
# the connect guard below refuses it like any other non-loopback address -- so
# it only has to look public to ipaddress.
DOCUMENTATION_ADDRESS = "93.184.215.14"
DOCUMENTATION_DOMAINS = ("example.com", "example.org", "example.net")
LOOPBACK_NAMES = frozenset({"localhost", "localhost.localdomain", "ip6-localhost"})

_violations_path: str | None = None
_violations_lock = threading.Lock()


def _violation(what: str) -> None:
    line = f"{datetime.datetime.now().isoformat(timespec='seconds')} pid={os.getpid()} {what}\n"
    sys.stderr.write("e2e network guard refused: " + line)
    if _violations_path:
        with _violations_lock, open(_violations_path, "a", encoding="utf-8") as out:
            out.write(line)


def _loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def _literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.split("%", 1)[0])
        return True
    except ValueError:
        return False


def install_network_guard(violations: str | None) -> None:
    """Loopback only, for every socket this interpreter opens from here on."""
    global _violations_path
    _violations_path = violations
    real_getaddrinfo = socket.getaddrinfo
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        name = host.decode() if isinstance(host, bytes) else host
        if name is None or _literal(name) or name.lower() in LOOPBACK_NAMES:
            if name is not None and name.lower() in LOOPBACK_NAMES:
                name = "127.0.0.1"
            return real_getaddrinfo(name, port, family, type, proto, flags)
        lowered = name.lower().rstrip(".")
        if any(lowered == d or lowered.endswith("." + d) for d in DOCUMENTATION_DOMAINS):
            number = int(port) if isinstance(port, (int, str)) and str(port).isdigit() else 0
            return [(socket.AF_INET, type or socket.SOCK_STREAM, proto or socket.IPPROTO_TCP, "",
                     (DOCUMENTATION_ADDRESS, number))]
        _violation(f"resolve {name}:{port}")
        raise socket.gaierror(socket.EAI_NONAME, f"the e2e stack resolves no name but loopback ({name})")

    def allowed(sock: socket.socket, address) -> bool:
        if sock.family not in (socket.AF_INET, socket.AF_INET6):
            return True  # a unix socket or a socketpair: never the network
        host = address[0] if isinstance(address, tuple) else str(address)
        return _loopback(str(host))

    def connect(self, address):
        if not allowed(self, address):
            _violation(f"connect {address!r}")
            raise ConnectionRefusedError(f"the e2e stack connects to loopback only, not {address!r}")
        return real_connect(self, address)

    def connect_ex(self, address):
        if not allowed(self, address):
            _violation(f"connect {address!r}")
            return 111  # ECONNREFUSED, as the real call reports it
        return real_connect_ex(self, address)

    socket.getaddrinfo = getaddrinfo
    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex


def install_lifetime(parent: int, deadline: float) -> None:
    """Exit when the parent goes, or at the deadline, whichever is first.

    SIGTERM first so uvicorn runs its shutdown (the hub writes its store on the
    way out), and _exit three seconds later if that shutdown hangs on a
    streaming response that never ends -- /satellites/events is one.
    """
    ends = time.monotonic() + deadline

    def watch() -> None:
        while True:
            time.sleep(1.0)
            if os.getppid() != parent or time.monotonic() > ends:
                why = "parent gone" if os.getppid() != parent else "deadline reached"
                sys.stderr.write(f"e2e child {os.getpid()} exiting: {why}\n")
                os.kill(os.getpid(), signal.SIGTERM)
                time.sleep(3.0)
                os._exit(3)

    threading.Thread(target=watch, name="e2e-lifetime", daemon=True).start()


def main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--marker", required=True)
    parser.add_argument("--parent", type=int, required=True)
    parser.add_argument("--deadline", type=float, required=True)
    parser.add_argument("--violations")
    parser.add_argument("--app")
    parser.add_argument("--app-dir")
    parser.add_argument("--port", type=int)
    parser.add_argument("--fakes", action="store_true")
    args, rest = parser.parse_known_args(argv)

    install_network_guard(args.violations)
    install_lifetime(args.parent, args.deadline)

    if args.fakes:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import fakes
        fakes.main(rest)
        return

    import uvicorn
    sys.path.insert(0, args.app_dir)
    uvicorn.run(args.app, host="127.0.0.1", port=args.port, loop="asyncio",
                log_level=os.environ.get("E2E_UVICORN_LOG_LEVEL", "warning"),
                timeout_graceful_shutdown=3)


if __name__ == "__main__":
    main(sys.argv[1:])
