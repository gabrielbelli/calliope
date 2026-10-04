"""The media player: AirPlay's state, cover and transport, the volume of the
satellite that plays, and streams from Home Assistant (play_media, tts.speak,
Stop, browse). No ffmpeg runs and nothing plays (conftest `converted`)."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

import pytest
from homeassistant.components.media_player import MediaPlayerEntityFeature as F
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from .conftest import until
from .fake_calliope import (
    COVER,
    COVER_SHA,
    KITCHEN_ID,
    LOUNGE_ID,
    FakeCalliope,
    airplay_playing,
)

TRANSPORT = F.PLAY | F.PAUSE | F.NEXT_TRACK | F.PREVIOUS_TRACK | F.STOP
MUSIC_URL = "http://192.0.2.10/music/So%20What.flac"


def _airplay(fake: FakeCalliope, airplay: dict[str, Any]) -> None:
    """The Pi's next status carries this AirPlay block."""
    status = fake.satellites[LOUNGE_ID]["status"]
    status["airplay"] = airplay
    fake.push({"type": "status", "satellite": LOUNGE_ID, "status": status})


def _features(hass: HomeAssistant, entity_id: str) -> F:
    return F(hass.states.get(entity_id).attributes["supported_features"])


async def _speak(hass: HomeAssistant, entity_id: str) -> None:
    await hass.services.async_call(
        "tts",
        "speak",
        {
            "entity_id": "tts.calliope_kokoro",
            "media_player_entity_id": entity_id,
            "message": "Dinner is ready.",
        },
        blocking=True,
    )


async def _play(hass: HomeAssistant, entity_id: str, **data: Any) -> None:
    await hass.services.async_call(
        "media_player",
        "play_media",
        {
            "entity_id": entity_id,
            "media_content_id": MUSIC_URL,
            "media_content_type": "music",
            **data,
        },
        blocking=True,
    )


async def test_state_and_metadata_come_from_airplay(
    hass: HomeAssistant, fake: FakeCalliope, loaded: MockConfigEntry
) -> None:
    """Playing, paused and idle from the phone's session; title, artist,
    album, duration and position; the position's time moves only with a new
    position; the cover through Home Assistant's proxy."""
    assert hass.states.get("media_player.lounge").state == "idle"
    _airplay(fake, airplay_playing(position=42.0))
    await until(hass, lambda: hass.states.get("media_player.lounge").state == "playing")
    lounge = hass.states.get("media_player.lounge")
    assert lounge.attributes["media_title"] == "So What"
    assert lounge.attributes["media_artist"] == "Miles Davis"
    assert lounge.attributes["media_album_name"] == "Kind of Blue"
    assert lounge.attributes["media_duration"] == 562
    assert lounge.attributes["media_position"] == 42
    assert lounge.attributes["app_name"] == "AirPlay"
    assert lounge.attributes["media_content_type"] == "music"
    assert lounge.attributes["entity_picture"].startswith(
        "/api/media_player_proxy/media_player.lounge?"
    )
    assert lounge.attributes["entity_picture"].endswith(f"&cache={COVER_SHA[:16]}")
    at = lounge.attributes["media_position_updated_at"]

    _airplay(fake, airplay_playing(position=42.0) | {"volume": 55})
    await hass.async_block_till_done()
    assert (
        hass.states.get("media_player.lounge").attributes["media_position_updated_at"]
        == at
    )
    _airplay(fake, airplay_playing(position=52.0))
    await until(
        hass,
        lambda: (
            hass.states.get("media_player.lounge").attributes["media_position"] == 52
        ),
    )
    assert (
        hass.states.get("media_player.lounge").attributes["media_position_updated_at"]
        > at
    )

    _airplay(fake, airplay_playing(paused=True))
    await until(hass, lambda: hass.states.get("media_player.lounge").state == "paused")
    _airplay(fake, airplay_playing() | {"session": False, "playing": False})
    await until(hass, lambda: hass.states.get("media_player.lounge").state == "idle")
    assert "media_title" not in hass.states.get("media_player.lounge").attributes
    assert "entity_picture" not in hass.states.get("media_player.lounge").attributes


async def test_the_cover_is_fetched_with_the_integrations_key(
    hass: HomeAssistant,
    fake: FakeCalliope,
    entry: MockConfigEntry,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    """Home Assistant's own image fetch sends no key: the cover comes
    through the integration's client, with the key, by its SHA-256."""
    assert await hass.config_entries.async_setup(entry.entry_id)
    await until(hass, lambda: entry.runtime_data.coordinator.connected)
    _airplay(fake, airplay_playing())
    await until(hass, lambda: hass.states.get("media_player.lounge").state == "playing")

    client = await hass_client_no_auth()
    resp = await client.get(
        hass.states.get("media_player.lounge").attributes["entity_picture"]
    )
    assert resp.status == 200
    assert await resp.read() == COVER
    assert resp.content_type == "image/jpeg"
    [asked] = fake.calls("GET", f"/satellites/{LOUNGE_ID}/airplay/artwork")
    assert asked == {"v": COVER_SHA, "authorization": f"Bearer {fake.api_key}"}
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_transport_features_follow_what_the_phone_takes(
    hass: HomeAssistant, fake: FakeCalliope, loaded: MockConfigEntry
) -> None:
    """Nothing without a phone; nothing but Disconnect (which the page
    offers) when the phone takes no remote control; all of it when it
    does."""
    assert not _features(hass, "media_player.lounge") & TRANSPORT
    _airplay(fake, airplay_playing(controls=["disconnect"]))
    await until(hass, lambda: hass.states.get("media_player.lounge").state == "playing")
    assert not _features(hass, "media_player.lounge") & TRANSPORT
    _airplay(fake, airplay_playing())
    await until(
        hass, lambda: _features(hass, "media_player.lounge") & TRANSPORT == TRANSPORT
    )
    assert F.VOLUME_SET | F.VOLUME_STEP in _features(hass, "media_player.lounge")


async def test_next_track_asks_the_phone_through_the_hub(
    hass: HomeAssistant, fake: FakeCalliope, loaded: MockConfigEntry
) -> None:
    """POST /satellites/{id}/airplay/next, and pause likewise."""
    _airplay(fake, airplay_playing())
    await until(hass, lambda: F.NEXT_TRACK in _features(hass, "media_player.lounge"))
    for service in ("media_next_track", "media_pause"):
        await hass.services.async_call(
            "media_player", service, {"entity_id": "media_player.lounge"}, blocking=True
        )
    assert fake.calls("POST", f"/satellites/{LOUNGE_ID}/airplay/next") == [None]
    assert fake.calls("POST", f"/satellites/{LOUNGE_ID}/airplay/pause") == [None]


async def test_a_refused_airplay_command_is_an_error(
    hass: HomeAssistant, fake: FakeCalliope, loaded: MockConfigEntry
) -> None:
    """The phone's refusal, with Shairport's reason, reaches the person."""
    _airplay(fake, airplay_playing())
    await until(hass, lambda: F.NEXT_TRACK in _features(hass, "media_player.lounge"))
    fake.airplay_refusal = (
        502,
        "the phone did not take next (491: the phone refused the connection)",
        "airplay_refused",
    )
    with pytest.raises(HomeAssistantError, match="refused the connection") as err:
        await hass.services.async_call(
            "media_player",
            "media_next_track",
            {"entity_id": "media_player.lounge"},
            blocking=True,
        )
    assert err.value.translation_key == "satellite_refused"


async def test_volume_is_the_volume_of_the_satellite_that_plays(
    hass: HomeAssistant, fake: FakeCalliope, loaded: MockConfigEntry
) -> None:
    """The kitchen answers through the lounge's speaker: its volume is the
    lounge's, and setting it sets the lounge's, not its own, which does
    nothing meanwhile."""
    fake.satellites[KITCHEN_ID]["config"]["output_satellite"] = LOUNGE_ID
    fake.satellites[LOUNGE_ID]["config"]["volume"] = 30
    fake.push(
        {"type": "config", "satellite": KITCHEN_ID, "changed": ["output_satellite"]}
    )
    fake.push({"type": "config", "satellite": LOUNGE_ID, "changed": ["volume"]})
    await until(
        hass,
        lambda: (
            hass.states.get("media_player.kitchen").attributes["volume_level"] == 0.3
        ),
    )
    await hass.services.async_call(
        "media_player",
        "volume_set",
        {"entity_id": "media_player.kitchen", "volume_level": 0.5},
        blocking=True,
    )
    assert fake.calls("PATCH", f"/satellites/{LOUNGE_ID}") == [{"volume": 50}]
    assert fake.calls("PATCH", f"/satellites/{KITCHEN_ID}") == []
    assert hass.states.get("media_player.kitchen").attributes["volume_level"] == 0.5
    assert hass.states.get("media_player.lounge").attributes["volume_level"] == 0.5

    await hass.services.async_call(
        "media_player", "volume_up", {"entity_id": "media_player.lounge"}, blocking=True
    )
    assert fake.calls("PATCH", f"/satellites/{LOUNGE_ID}")[-1] == {"volume": 55}


async def test_a_volume_changed_on_the_phone_reaches_the_entity(
    hass: HomeAssistant, fake: FakeCalliope, loaded: MockConfigEntry
) -> None:
    """The Pi takes the phone's AirPlay slider as its own volume and says so
    (cause "local"); the hub publishes settings, and the record is read."""
    fake.satellites[LOUNGE_ID]["config"]["volume"] = 32
    fake.push({"type": "settings", "satellite": LOUNGE_ID, "volume": 32})
    await until(
        hass,
        lambda: (
            hass.states.get("media_player.lounge").attributes["volume_level"] == 0.32
        ),
    )


@pytest.mark.parametrize(
    ("entity_id", "sid", "rate", "channels"),
    [
        ("media_player.lounge", LOUNGE_ID, 44100, 2),
        ("media_player.kitchen", KITCHEN_ID, 48000, 1),
    ],
)
async def test_play_media_streams_a_wav_in_the_satellites_music_format(
    hass: HomeAssistant,
    fake: FakeCalliope,
    loaded: MockConfigEntry,
    converted: list[dict[str, Any]],
    entity_id: str,
    sid: str,
    rate: int,
    channels: int,
) -> None:
    """The Pi's media lane takes 44.1 kHz stereo, the Korvo 48 kHz mono.
    The action returns while the hub still holds its answer (the music
    plays), and the player shows the stream until the hub answers."""
    fake.media_hold.clear()
    await _play(hass, entity_id)
    await until(hass, lambda: fake.calls("POST", f"/satellites/{sid}/media"))
    [sent] = fake.calls("POST", f"/satellites/{sid}/media")
    assert sent["announce"] == "0"
    assert sent["content_type"] == "audio/wav"
    assert sent["bytes"] > 44
    assert converted == [
        {
            "sources": [MUSIC_URL],
            "rate": rate,
            "channels": channels,
            "satellite": entity_id.removeprefix("media_player."),
        }
    ]
    state = hass.states.get(entity_id)
    assert state.state == "playing"
    assert state.attributes["app_name"] == "Calliope"
    assert state.attributes["media_title"] == "So What"
    assert F.STOP in F(state.attributes["supported_features"])

    fake.media_hold.set()
    await until(hass, lambda: hass.states.get(entity_id).state == "idle")
    assert F.STOP not in _features(hass, entity_id)


async def test_tts_speak_is_an_announcement_in_the_voice_format(
    hass: HomeAssistant,
    fake: FakeCalliope,
    loaded: MockConfigEntry,
    converted: list[dict[str, Any]],
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    """tts.speak plays as an announcement: the voice lane's format (the Pi's
    speaker is 44.1 kHz mono), announce=1, from Home Assistant's own URL."""
    hass.config.internal_url = "http://192.0.2.10:8123"
    await _speak(hass, "media_player.lounge")
    await until(hass, lambda: fake.calls("POST", f"/satellites/{LOUNGE_ID}/media"))
    [sent] = fake.calls("POST", f"/satellites/{LOUNGE_ID}/media")
    assert sent["announce"] == "1"
    [call] = converted
    assert (call["rate"], call["channels"]) == (44100, 1)
    assert call["sources"][0].startswith("http://192.0.2.10:8123/api/tts_proxy/")
    # What ffmpeg does with it: fetch the speech, which Kokoro then makes.
    client = await hass_client_no_auth()
    resp = await client.get(urlsplit(call["sources"][0]).path)
    assert resp.status == 200
    assert fake.calls("POST", "/v1/audio/speech")[0]["input"] == "Dinner is ready."


async def test_an_announcement_plays_over_the_music_and_leaves_it_playing(
    hass: HomeAssistant,
    fake: FakeCalliope,
    loaded: MockConfigEntry,
    converted: list[dict[str, Any]],
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    """tts.speak while Home Assistant's music plays: the announcement goes
    up beside the music, whose upload is left alone (the hub pauses or ducks
    the music under it), and the player still shows the music."""
    hass.config.internal_url = "http://192.0.2.10:8123"
    fake.media_hold.clear()
    await _play(hass, "media_player.lounge")
    await until(hass, lambda: fake.calls("POST", f"/satellites/{LOUNGE_ID}/media"))
    await _speak(hass, "media_player.lounge")
    await until(
        hass, lambda: len(fake.calls("POST", f"/satellites/{LOUNGE_ID}/media")) == 2
    )
    music, spoken = fake.calls("POST", f"/satellites/{LOUNGE_ID}/media")
    assert (music["announce"], spoken["announce"]) == ("0", "1")
    lounge = hass.states.get("media_player.lounge")
    assert lounge.state == "playing"
    assert lounge.attributes["app_name"] == "Calliope"
    assert lounge.attributes["media_title"] == "So What"
    # ffmpeg would fetch the speech; the fetch waits for it to be made.
    client = await hass_client_no_auth()
    assert (await client.get(urlsplit(converted[-1]["sources"][0]).path)).status == 200

    fake.media_hold.set()
    await until(hass, lambda: "reason" in music and "reason" in spoken)
    assert (music["reason"], spoken["reason"]) == ("ended", "ended")
    await until(hass, lambda: hass.states.get("media_player.lounge").state == "idle")


async def test_play_media_refuses_what_is_not_a_web_url(
    hass: HomeAssistant,
    fake: FakeCalliope,
    loaded: MockConfigEntry,
    converted: list[dict[str, Any]],
) -> None:
    """Anyone may call play_media: a file: URL or a bare path would have
    ffmpeg read any file Home Assistant can, past My media's folders."""
    for media_id in ("file:///config/secrets.yaml", "secrets.yaml"):
        with pytest.raises(HomeAssistantError) as err:
            await _play(hass, "media_player.lounge", media_content_id=media_id)
        assert err.value.translation_key == "unsupported_url"
    assert converted == []
    assert not fake.calls("POST", f"/satellites/{LOUNGE_ID}/media")


async def test_media_stop_ends_the_stream_and_asks_the_hub_to_flush(
    hass: HomeAssistant,
    fake: FakeCalliope,
    loaded: MockConfigEntry,
    converted: list[dict[str, Any]],
) -> None:
    """Stop cancels the upload and asks the hub to stop what plays."""
    fake.media_hold.clear()
    await _play(hass, "media_player.lounge")
    await until(hass, lambda: fake.calls("POST", f"/satellites/{LOUNGE_ID}/media"))
    await hass.services.async_call(
        "media_player",
        "media_stop",
        {"entity_id": "media_player.lounge"},
        blocking=True,
    )
    assert fake.calls("POST", f"/satellites/{LOUNGE_ID}/media/stop") == [None]
    assert hass.states.get("media_player.lounge").state == "idle"
    fake.media_hold.set()
    [sent] = fake.calls("POST", f"/satellites/{LOUNGE_ID}/media")
    await until(hass, lambda: "reason" in sent)
    assert sent["reason"] == "cancelled"


async def test_browse_media_offers_audio_only(
    hass: HomeAssistant, fake: FakeCalliope, loaded: MockConfigEntry, tmp_path
) -> None:
    """My media, without its pictures."""
    media = tmp_path / "media"
    media.mkdir()
    (media / "song.mp3").write_bytes(b"ID3")
    (media / "cover.jpg").write_bytes(b"\xff\xd8")
    hass.config.media_dirs["local"] = str(media)
    result = await hass.services.async_call(
        "media_player",
        "browse_media",
        {
            "entity_id": "media_player.lounge",
            "media_content_id": "media-source://media_source/local/.",
        },
        blocking=True,
        return_response=True,
    )
    assert [c.title for c in result["media_player.lounge"].children] == ["song.mp3"]


async def test_no_play_media_against_a_hub_without_the_media_route(
    hass: HomeAssistant, fake: FakeCalliope, entry: MockConfigEntry
) -> None:
    """A hub from before media names no format: nothing is offered, and
    play_media is refused rather than sent where it would 404."""
    fake.media_route = False
    assert await hass.config_entries.async_setup(entry.entry_id)
    await until(hass, lambda: entry.runtime_data.coordinator.connected)
    await hass.async_block_till_done()
    features = _features(hass, "media_player.lounge")
    assert not features & (F.PLAY_MEDIA | F.BROWSE_MEDIA | F.MEDIA_ANNOUNCE)
    assert F.VOLUME_SET in features
    with pytest.raises(HomeAssistantError):
        await _play(hass, "media_player.lounge")
    assert not fake.calls("POST", f"/satellites/{LOUNGE_ID}/media")
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_play_media_on_a_satellite_with_its_speaker_off_is_refused_at_once(
    hass: HomeAssistant,
    fake: FakeCalliope,
    loaded: MockConfigEntry,
    converted: list[dict[str, Any]],
) -> None:
    """Refused before anything is converted or sent, naming the satellite."""
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": "switch.kitchen_speaker"}, blocking=True
    )
    with pytest.raises(
        HomeAssistantError, match="kitchen has its speaker turned off"
    ) as err:
        await _play(hass, "media_player.kitchen")
    assert err.value.translation_key == "speaker_off"
    assert converted == []
    assert not fake.calls("POST", f"/satellites/{KITCHEN_ID}/media")
