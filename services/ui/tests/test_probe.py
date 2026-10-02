"""The probe resolves the host itself before yt-dlp runs, and gives it nothing of this service's.

/ui/resolve checks a URL before MeTube sees it; the probe runs after a MeTube
round trip, so it checks again, immediately before it spawns (D36). A name
that has started answering a private address in between is refused, and no
process is started. When one is, its environment is three fixed variables.
"""

from __future__ import annotations

import asyncio
import json
import socket

import pytest

from app import config, guard, probe


def answering(address):
    def resolve(host, port, **kwargs):
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, port))]
    return resolve


class Spawned(Exception):
    """Raised by the stand-in for create_subprocess_exec: a process was started."""


@pytest.fixture
def installed(monkeypatch):
    """A probe that is on and finds yt-dlp, and a record of every spawn attempt."""
    monkeypatch.setattr(config, "PROBE", True)
    monkeypatch.setattr(probe.shutil, "which", lambda name: "/opt/bin/yt-dlp")
    spawned: list[tuple[tuple, dict]] = []

    async def refuse(*args, **kwargs):
        spawned.append((args, kwargs))
        raise Spawned()
    monkeypatch.setattr(probe.asyncio, "create_subprocess_exec", refuse)
    return spawned


@pytest.mark.parametrize("url,address", [
    ("http://127.0.0.1/", None),
    ("http://192.168.0.1/", None),
    ("http://[::1]/", None),
    ("https://looks-public.example/watch", "10.0.0.5"),
    ("https://looks-public.example/watch", "100.64.0.9"),
    ("https://looks-public.example/watch", "169.254.169.254"),
    ("https://looks-public.example/watch", "fd00::1"),
    ("https://looks-public.example/watch", "0.0.0.0"),
])
def test_the_probe_refuses_a_private_destination_before_spawning(
        installed, monkeypatch, url, address):
    if address is not None:
        monkeypatch.setattr(guard.socket, "getaddrinfo", answering(address))
    with pytest.raises(probe.DestinationNotAllowed):
        asyncio.run(probe.run(url))
    assert installed == [], "yt-dlp was started on a private destination"


def test_the_probe_gives_yt_dlp_a_clean_environment(installed, monkeypatch):
    """Nothing this service was started with -- a proxy setting, the path to its
    credential volume -- reaches a process that runs on a user's URL."""
    monkeypatch.setattr(guard.socket, "getaddrinfo", answering("93.184.216.34"))
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.internal:3128")
    monkeypatch.setenv("CALLIOPE_RUN_DIR", "/run/calliope")
    with pytest.raises(Spawned):
        asyncio.run(probe.run("https://media.example/watch?v=x"))
    [(args, kwargs)] = installed
    assert args[0] == "/opt/bin/yt-dlp" and args[-2:] == ("--", "https://media.example/watch?v=x")
    assert kwargs["env"] == {"PATH": probe.os.defpath, "LANG": "C.UTF-8",
                             "PYTHONIOENCODING": "utf-8"}
    assert kwargs["cwd"] == "/"


def test_a_switched_off_probe_neither_resolves_nor_spawns(installed, monkeypatch):
    monkeypatch.setattr(config, "PROBE", False)

    def explode(*args, **kwargs):
        raise AssertionError("resolved a host for a probe that is off")
    monkeypatch.setattr(guard.socket, "getaddrinfo", explode)
    assert asyncio.run(probe.run("http://127.0.0.1/")) is None
    assert installed == []


def test_resolve_refuses_a_link_whose_host_moved_to_a_private_address(client, monkeypatch):
    """The first check passed; by the time the probe ran, the name answered
    10.0.0.5. MeTube's pending record goes, and the link is nobody's."""
    url = "https://rebinding.example/watch"
    api, _, tube = client(UI_PROBE="1")
    from app import ingest

    # The class the reloaded app catches: the conformance suite imports the
    # app afresh, so this module's own `probe` may be an older copy.
    async def moved(target):
        raise ingest.probe.DestinationNotAllowed(
            "refusing to fetch rebinding.example: 10.0.0.5 is a private address")
    monkeypatch.setattr(ingest.probe, "available", lambda: True)
    monkeypatch.setattr(ingest.probe, "run", moved)

    response = api.post("/ui/resolve", json={"url": url})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "destination_not_allowed"
    assert url not in tube.pending, "the refused link was left parked in MeTube"
    assert ingest.OWNERS.owner(url) is None


def test_a_probe_answer_is_five_scalars_whatever_the_page_said(installed, monkeypatch):
    """The info-dict comes from a page someone else wrote; only the numbers the
    confirm card needs are kept."""
    monkeypatch.setattr(guard.socket, "getaddrinfo", answering("93.184.216.34"))

    class Process:
        returncode = 0

        async def communicate(self):
            return json.dumps({"title": "T", "duration": 61.0, "uploader": "U",
                               "secret": "x" * 10, "formats": []}).encode(), b""

    async def spawn(*args, **kwargs):
        return Process()
    monkeypatch.setattr(probe.asyncio, "create_subprocess_exec", spawn)
    facts = asyncio.run(probe.run("https://media.example/watch"))
    assert set(facts) == {"title", "uploader", "duration", "bytes", "is_live",
                          "has_subtitles"}
