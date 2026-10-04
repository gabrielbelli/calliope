"""Each satellite with a speaker as a media player.

It is the satellite's one volume control, and it shows what plays there: the
phone's AirPlay music (title, artist, cover, position, and the transport the
phone takes), or a stream Home Assistant sent. play_media, tts.speak and
announcements are converted by Home Assistant's ffmpeg to the WAV the hub
names and uploaded to the hub (media.py).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit

from homeassistant.components import media_source
from homeassistant.components.media_player import (
    ATTR_MEDIA_ANNOUNCE,
    BrowseMedia,
    MediaPlayerDeviceClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
)
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import media
from .api import CalliopeError
from .const import APP_AIRPLAY, APP_CALLIOPE, DOMAIN, VOLUME_STEP
from .coordinator import CalliopeConfigEntry, CalliopeCoordinator, device_name
from .entity import CalliopeSatelliteEntity, add_for_caps

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0

# What each AirPlay command the phone takes (status.airplay.remote.controls)
# offers here. The phone decides what it takes, per app and per moment.
AIRPLAY_FEATURES = {
    "play": MediaPlayerEntityFeature.PLAY,
    "pause": MediaPlayerEntityFeature.PAUSE,
    "next": MediaPlayerEntityFeature.NEXT_TRACK,
    "previous": MediaPlayerEntityFeature.PREVIOUS_TRACK,
    "stop": MediaPlayerEntityFeature.STOP,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CalliopeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """One media player per satellite with a speaker."""

    def build(
        coordinator: CalliopeCoordinator, sid: str, key: str
    ) -> CalliopeMediaPlayer:
        return CalliopeMediaPlayer(coordinator, sid, key)

    add_for_caps(entry, async_add_entities, Platform.MEDIA_PLAYER, build)


def _title_of(media_id: str) -> str | None:
    """A stream's title: the last part of its path, without its extension.
    Only a file's extension is one: in media-source://tts/tts.calliope_kokoro
    the dot is part of an entity id."""
    name = PurePosixPath(
        unquote(urlsplit(media_id).path.rstrip("/").rpartition("/")[2])
    )
    return (name.stem if 1 < len(name.suffix) <= 5 else name.name) or None


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


class CalliopeMediaPlayer(CalliopeSatelliteEntity, MediaPlayerEntity):
    """The satellite's speaker."""

    _attr_name = None
    _attr_device_class = MediaPlayerDeviceClass.SPEAKER
    _attr_volume_step = VOLUME_STEP
    # The cover is fetched with the integration's key (async_get_media_image):
    # browsers get it through Home Assistant's proxy, never from the hub.
    _attr_media_image_remotely_accessible = False
    _attr_media_image_hash = None

    def __init__(self, coordinator: CalliopeCoordinator, sid: str, key: str) -> None:
        """No stream of its own yet."""
        super().__init__(coordinator, sid, key)
        # The music this entity sent: what it shows, and what Stop ends.
        self._task: asyncio.Task[None] | None = None
        # Its announcements, each until the hub says it has played. They
        # play over the music and never replace it, nor each other: the hub
        # queues them on the voice lane.
        self._announcements: set[asyncio.Task[None]] = set()
        self._title: str | None = None
        self._position: float | None = None

    # -- what the hub says -------------------------------------------------

    @property
    def _media(self) -> dict[str, Any]:
        """GET /satellites/{id} "media": the formats to send and through
        whom; {} on a hub that cannot play media."""
        return _dict((self.satellite or {}).get("media"))

    @property
    def _through(self) -> str:
        """The satellite that plays for this one: itself, or its Output."""
        through = self._media.get("through")
        return through if isinstance(through, str) and through else self.satellite_id

    @property
    def _through_sat(self) -> dict[str, Any]:
        return (self.coordinator.data or {}).get(self._through) or self.satellite or {}

    @property
    def _airplay(self) -> dict[str, Any]:
        return _dict(self.status.get("airplay"))

    @property
    def _controls(self) -> list[str]:
        """What the phone takes now; [] without a session."""
        controls = _dict(self._airplay.get("remote")).get("controls")
        return (
            [c for c in controls if isinstance(c, str)]
            if isinstance(controls, list)
            else []
        )

    @property
    def _own(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def _streaming(self) -> bool:
        """A stream from Home Assistant plays: this entity's own, or one the
        hub says plays here (started by another entity, or before a restart)."""
        return self._own or bool(self._media.get("playing"))

    def _marks(self) -> tuple[Any, ...]:
        """The satellite it plays through changes its volume."""
        return (*super()._marks(), self.coordinator.revision.get(self._through))

    # -- state -----------------------------------------------------------------

    def _on_update(self) -> None:
        """Features, state and attributes from the hub's record and this
        entity's own stream."""
        features = (
            MediaPlayerEntityFeature.VOLUME_SET | MediaPlayerEntityFeature.VOLUME_STEP
        )
        if self._media:
            features |= (
                MediaPlayerEntityFeature.PLAY_MEDIA
                | MediaPlayerEntityFeature.BROWSE_MEDIA
                | MediaPlayerEntityFeature.MEDIA_ANNOUNCE
            )
        if self._streaming:
            features |= MediaPlayerEntityFeature.STOP
        for command in self._controls:
            features |= AIRPLAY_FEATURES.get(command, MediaPlayerEntityFeature(0))
        self._attr_supported_features = features

        ap = self._airplay
        if self._streaming or ap.get("playing"):
            self._attr_state = MediaPlayerState.PLAYING
        elif ap.get("session"):
            self._attr_state = MediaPlayerState.PAUSED
        else:
            self._attr_state = MediaPlayerState.IDLE

        title = artist = album = duration = position = image = app = None
        kind: MediaType | None = None
        if self._streaming:
            app, kind = APP_CALLIOPE, MediaType.MUSIC
            title = self._title if self._own else None
        elif ap.get("session"):
            app, kind = APP_AIRPLAY, MediaType.MUSIC
            title, artist, album = ap.get("title"), ap.get("artist"), ap.get("album")
            progress = _dict(ap.get("progress"))
            if isinstance(progress.get("duration_s"), (int, float)):
                duration = int(progress["duration_s"])
            if isinstance(progress.get("position_s"), (int, float)):
                position = int(progress["position_s"])
                if progress["position_s"] != self._position:
                    # Only a new position is news: the frontend runs the clock
                    # from when it was valid.
                    self._position = progress["position_s"]
                    self._attr_media_position_updated_at = dt_util.utcnow()
            sha = _dict(ap.get("artwork")).get("sha256")
            image = sha[:16] if isinstance(sha, str) and sha else None
        if position is None:
            self._position = None
            self._attr_media_position_updated_at = None
        self._attr_app_name = app
        self._attr_media_content_type = kind
        self._attr_media_title = title
        self._attr_media_artist = artist
        self._attr_media_album_name = album
        self._attr_media_duration = duration
        self._attr_media_position = position
        self._attr_media_image_hash = image

    @property
    def volume_level(self) -> float | None:
        """The volume of the satellite that plays: this one's does nothing
        while another plays for it."""
        volume = _dict(self._through_sat.get("config")).get("volume")
        if not isinstance(volume, (int, float)):
            volume = self.config.get("volume")
        return volume / 100 if isinstance(volume, (int, float)) else None

    async def async_set_volume_level(self, volume: float) -> None:
        """PATCH the volume of the satellite that plays."""
        try:
            sat = await self.coordinator.client.configure(
                self._through, volume=round(volume * 100)
            )
        except CalliopeError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="hub_refused",
                translation_placeholders={"error": str(err)},
            ) from err
        self.coordinator.async_set_satellite(sat)

    async def async_get_media_image(self) -> tuple[bytes | None, str | None]:
        """The AirPlay cover, asked for by its full SHA-256 so that an older
        picture the hub still holds is never shown for this one."""
        sha = _dict(self._airplay.get("artwork")).get("sha256")
        if not isinstance(sha, str) or not sha:
            return None, None
        try:
            return await self.coordinator.client.airplay_artwork(self.satellite_id, sha)
        except CalliopeError as err:
            _LOGGER.debug("No cover for %s: %s", self.entity_id, err)
            return None, None

    # -- playing -----------------------------------------------------------

    def _name(self, sat: dict[str, Any] | None = None) -> str:
        return device_name(self.satellite_id, sat or self.satellite or {})

    async def async_play_media(
        self, media_type: MediaType | str, media_id: str, **kwargs: Any
    ) -> None:
        """Start playing and return: the upload lasts as long as the music,
        and tts.speak waits for this call. Music replaces this entity's
        music. An announcement (tts.speak, announce: true) plays over it on
        the hub's voice lane, the music pausing or ducking meanwhile, and
        leaves the player showing the music."""
        fmt = self._media
        if not fmt:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="hub_cannot_play"
            )
        through = self._through_sat
        if _dict(through.get("config")).get("speaker_enabled") is False:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="speaker_off",
                translation_placeholders={
                    "satellite": device_name(self._through, through)
                },
            )
        source = await media.resolve(self.hass, media_id, self.entity_id)
        announce = bool(kwargs.get(ATTR_MEDIA_ANNOUNCE))
        wav = _dict(fmt.get("announce" if announce else "music"))
        play = self._play(source, int(wav["rate"]), int(wav["channels"]), announce)
        create = self.coordinator.config_entry.async_create_background_task
        if announce:
            task = create(
                self.hass, play, f"{DOMAIN} announcement for {self.satellite_id}"
            )
            self._announcements.add(task)
            task.add_done_callback(self._announcements.discard)
            return
        if self._task is not None:
            self._task.cancel()
        self._title = _title_of(media_id)
        self._task = create(self.hass, play, f"{DOMAIN} media for {self.satellite_id}")
        self._on_update()
        self.async_write_ha_state()

    async def _play(
        self, source: str | Path, rate: int, channels: int, announce: bool
    ) -> None:
        name = self._name()
        try:
            result = await self.coordinator.client.media(
                self.satellite_id,
                media.wav_chunks(self.hass, [source], rate, channels, satellite=name),
                announce=announce,
            )
        except (CalliopeError, HomeAssistantError) as err:
            _LOGGER.warning("Calliope could not play on %s: %s", name, err)
        else:
            media.log_end(name, result)
        finally:
            if self._task is asyncio.current_task():
                self._task = None
                self._on_update()
                self.async_write_ha_state()

    async def async_media_stop(self) -> None:
        """Stop Home Assistant's stream, or ask the phone to stop. An
        announcement already sent plays on: the hub has it on the voice
        lane, which Stop does not touch."""
        if self._streaming:
            if self._task is not None:
                self._task.cancel()
                self._task = None
            await self._ask(self.coordinator.client.media_stop(self.satellite_id))
            self._on_update()
            self.async_write_ha_state()
        elif "stop" in self._controls:
            await self._airplay_command("stop")

    async def async_media_play(self) -> None:
        """Ask the phone to play."""
        await self._airplay_command("play")

    async def async_media_pause(self) -> None:
        """Ask the phone to pause."""
        await self._airplay_command("pause")

    async def async_media_next_track(self) -> None:
        """Ask the phone for the next track."""
        await self._airplay_command("next")

    async def async_media_previous_track(self) -> None:
        """Ask the phone for the previous track."""
        await self._airplay_command("previous")

    async def _airplay_command(self, command: str) -> None:
        """POST /satellites/{id}/airplay/{command}. The satellite's next
        status shows what the phone did."""
        await self._ask(self.coordinator.client.airplay(self.satellite_id, command))

    async def _ask(self, call: Any) -> Any:
        try:
            return await call
        except CalliopeError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="satellite_refused",
                translation_placeholders={"satellite": self._name(), "error": str(err)},
            ) from err

    async def async_browse_media(
        self,
        media_content_type: MediaType | str | None = None,
        media_content_id: str | None = None,
    ) -> BrowseMedia:
        """Home Assistant's media, audio only."""
        return await media_source.async_browse_media(
            self.hass,
            media_content_id,
            content_filter=lambda item: item.media_content_type.startswith("audio/"),
        )

    async def async_will_remove_from_hass(self) -> None:
        """A stream of a removed entity ends with it, as do the uploads of
        its announcements."""
        if self._task is not None:
            self._task.cancel()
        for task in self._announcements:
            task.cancel()
        await super().async_will_remove_from_hass()
