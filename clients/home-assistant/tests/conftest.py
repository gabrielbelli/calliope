"""Fixtures: a fake Calliope gateway and a loaded entry pointed at it."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest
from homeassistant.const import CONF_URL, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.calliope.api import wav_bytes
from custom_components.calliope.const import DOMAIN

from .fake_calliope import FakeCalliope


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load custom_components/calliope in every test."""


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reconnect in milliseconds, not seconds."""
    monkeypatch.setattr("custom_components.calliope.coordinator.BACKOFF_MIN", 0.01)
    monkeypatch.setattr("custom_components.calliope.coordinator.BACKOFF_MAX", 0.05)


@pytest.fixture(autouse=True)
def tts_cache_in_tmp(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Home Assistant's TTS file cache in a fresh directory per test, not
    in the plugin's shared testing_config, where it outlives the run."""
    monkeypatch.setattr(
        "homeassistant.components.tts.DEFAULT_CACHE_DIR", str(tmp_path / "tts")
    )


@pytest.fixture
async def fake(
    hass: HomeAssistant, socket_enabled: None
) -> AsyncGenerator[FakeCalliope]:
    """A fake gateway on a free port of 127.0.0.1. Home Assistant's test
    harness blocks sockets; socket_enabled lets this one listen, and
    connections stay limited to 127.0.0.1."""
    server = FakeCalliope()
    await server.start()
    yield server
    await server.stop()


@pytest.fixture
async def entry(hass: HomeAssistant, fake: FakeCalliope) -> MockConfigEntry:
    """An entry pointed at the fake, not yet set up. Home Assistant's own
    core component is set up first, as it always is outside tests: the Assist
    satellite platform needs conversation, which needs its exposed entities."""
    assert await async_setup_component(hass, "homeassistant", {})
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        title="127.0.0.1",
        unique_id=fake.url.split("//", 1)[1],
        data={CONF_URL: fake.url, CONF_VERIFY_SSL: True},
    )
    config_entry.add_to_hass(hass)
    return config_entry


@pytest.fixture
async def loaded(
    hass: HomeAssistant, fake: FakeCalliope, entry: MockConfigEntry
) -> AsyncGenerator[MockConfigEntry]:
    """The entry set up, its event stream open and its first refresh done."""
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await fake.wait_streams(1)
    await until(hass, lambda: entry.runtime_data.coordinator.connected)
    await hass.async_block_till_done()
    yield entry
    # A test that failed while the fake held an upload's answer would
    # otherwise wait for it here for ever.
    fake.media_hold.set()
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def until(
    hass: HomeAssistant, condition: Callable[[], object], timeout: float = 5
) -> None:
    """Let the event loop run until `condition` holds."""
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.01)
            await hass.async_block_till_done()


@pytest.fixture
def converted(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """What ffmpeg was asked to convert, and in which format. No ffmpeg
    runs and nothing plays: each conversion is 20 ms of silence in the
    format asked for."""
    calls: list[dict[str, Any]] = []

    async def wav_chunks(
        hass: HomeAssistant,
        sources: list[str],
        rate: int,
        channels: int,
        *,
        satellite: str,
    ) -> AsyncIterator[bytes]:
        calls.append(
            {
                "sources": sources,
                "rate": rate,
                "channels": channels,
                "satellite": satellite,
            }
        )
        yield wav_bytes(b"\x00\x00" * channels * (rate // 50), rate, channels)

    monkeypatch.setattr("custom_components.calliope.media.wav_chunks", wav_chunks)
    return calls
