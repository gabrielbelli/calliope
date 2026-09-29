"""AirPlay: Shairport Sync as the calliope user, playing through PipeWire.

The satellite is an AirPlay receiver under its own name, on the output the
hub chose, beside everything else it plays: PipeWire mixes them. The hub
turns it on and off and names it (airplay_enabled, airplay_name); the agent
writes Shairport Sync's configuration and runs it as a user service
(calliope-airplay.service), so no root is needed after install.sh.

This is classic AirPlay, from Debian's shairport-sync (4.3.7 in trixie, built
without AirPlay 2): iPhones, iPads and Macs list it; multi-room and the Home
app need AirPlay 2.

DUCKING. While the satellite speaks, or while the hub holds a duck (someone
is talking to it, or to a satellite that plays through it), every other
stream (AirPlay, and later Bluetooth) goes down to a fraction of its volume,
and back when it is over. Through pipewire-pulse's pactl, which reads and
sets each stream's own volume."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from pathlib import Path

log = logging.getLogger("calliope.airplay")

UNIT = "calliope-airplay.service"
CONF = Path.home() / ".config" / "calliope" / "shairport-sync.conf"
OURS = "Assistant"        # media.role of the agent's own streams (pipewire.Player)


def available() -> bool:
    return shutil.which("shairport-sync") is not None


def _quote(text: str) -> str:
    """A libconfig string: backslashes and quotes escaped, controls dropped."""
    clean = "".join(ch for ch in text if ch.isprintable())
    return '"' + clean.replace("\\", "\\\\").replace('"', '\\"') + '"'


def config(name: str) -> str:
    """Shairport Sync's configuration for this satellite."""
    return "\n".join([
        "// Written by the Calliope satellite agent (calliope_pi/airplay.py).",
        "general = {",
        f"  name = {_quote(name[:50] or 'Calliope')};",
        '  output_backend = "pa";',
        '  interpolation = "soxr";',
        "};",
        "pa = {",
        '  application_name = "AirPlay";',
        "};",
        "sessioncontrol = {",
        "  session_timeout = 20;",
        "};",
        ""])


async def _run(*argv: str, timeout: float = 15.0) -> tuple[int, str]:
    if shutil.which(argv[0]) is None:
        return 127, f"{argv[0]} is not installed"
    proc = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        return 124, f"{argv[0]} did not answer within {timeout:g} s"
    return proc.returncode or 0, out.decode(errors="replace")


class AirPlay:
    def __init__(self, conf: Path = CONF):
        self.conf = conf
        self.error: str | None = None

    async def apply(self, enabled: bool, name: str) -> None:
        """Running under `name` when enabled, stopped when not. Restarted
        only when its configuration changed, so a settings message that
        changes nothing else does not cut the music."""
        if not available():
            self.error = "shairport-sync is not installed"
            return
        if not enabled:
            await _run("systemctl", "--user", "disable", "--now", UNIT)
            self.error = None
            return
        text = config(name)
        changed = not self.conf.exists() or self.conf.read_text() != text
        if changed:
            self.conf.parent.mkdir(parents=True, exist_ok=True)
            self.conf.write_text(text)
        code, out = await _run("systemctl", "--user", "enable", UNIT)
        verb = "restart" if changed else "start"
        code, out = await _run("systemctl", "--user", verb, UNIT)
        self.error = None if code == 0 else out.strip()[-200:] or f"systemctl {verb} failed"
        if changed:
            log.info("AirPlay as %r (%s)", name, "running" if code == 0 else self.error)

    async def running(self) -> bool:
        code, out = await _run("systemctl", "--user", "is-active", UNIT)
        return out.strip() == "active"


# ---- ducking --------------------------------------------------------------------


def others(sink_inputs: list) -> list[dict]:
    """The streams that are not the agent's own: {index, volume (0-1 of the
    first channel), app}."""
    out = []
    for si in sink_inputs if isinstance(sink_inputs, list) else []:
        props = si.get("properties") or {}
        if props.get("media.role") == OURS:
            continue
        vol = (si.get("volume") or {})
        first = next(iter(vol.values()), {}) if isinstance(vol, dict) else {}
        try:
            fraction = int(first.get("value")) / 65536
        except (TypeError, ValueError, AttributeError):
            continue
        out.append({"index": si.get("index"), "volume": fraction,
                    "app": props.get("application.name") or props.get("media.name")})
    return out


class Ducker:
    """Lowers every other stream while `want` is set, and gives each back the
    volume it had. A stream that starts while ducked is lowered too."""

    def __init__(self) -> None:
        self.saved: dict[int, float] = {}
        self.level: float | None = None
        self._task: asyncio.Task | None = None

    async def _inputs(self) -> list:
        code, out = await _run("pactl", "-f", "json", "list", "sink-inputs")
        if code:
            return []
        try:
            return json.loads(out)
        except ValueError:
            return []

    async def set(self, level: float | None) -> None:
        """Duck to `level` (0-1 of each stream's own volume), or None to lift."""
        if level == self.level:
            return
        self.level = level
        if level is None:
            if self._task is not None:
                self._task.cancel()
                self._task = None
            await self._restore()
        elif self._task is None or self._task.done():
            self._task = asyncio.create_task(self._hold())

    async def _hold(self) -> None:
        while self.level is not None:
            for s in others(await self._inputs()):
                idx = s["index"]
                if idx not in self.saved:
                    self.saved[idx] = s["volume"]
                    target = self.saved[idx] * self.level
                    await _run("pactl", "set-sink-input-volume", str(idx), f"{target:.3f}")
            await asyncio.sleep(0.5)

    async def _restore(self) -> None:
        saved, self.saved = self.saved, {}
        present = {s["index"] for s in others(await self._inputs())}
        for idx, volume in saved.items():
            if idx in present:
                await _run("pactl", "set-sink-input-volume", str(idx), f"{volume:.3f}")
