"""The brand images: Calliope's mark, which Home Assistant shows for the
integration from its own brand folder."""

from __future__ import annotations

import struct
from pathlib import Path

from homeassistant.components.brands.const import ALLOWED_IMAGES
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.calliope.const import DOMAIN

BRAND = Path(__file__).parents[1] / "custom_components" / DOMAIN / "brand"
# The mark is a dark rounded plate, so it reads on the light and the dark
# theme alike: no dark_ variants, and the logo falls back to the icon.
SIDES = {"icon.png": 256, "icon@2x.png": 512}
PNG = b"\x89PNG\r\n\x1a\n"


def _size(path: Path) -> tuple[int, int]:
    """Width and height from the PNG's IHDR chunk."""
    head = path.read_bytes()[:24]
    assert head[:8] == PNG, path.name
    assert head[12:16] == b"IHDR", path.name
    return struct.unpack(">II", head[16:24])


def test_the_brand_folder_holds_only_images_home_assistant_serves() -> None:
    """Exactly the icon and its 2x, square, at the sizes Home Assistant's
    brands expect. A name it does not allow would never be served."""
    names = {path.name for path in BRAND.iterdir()}
    assert names == set(SIDES)
    assert names <= ALLOWED_IMAGES
    for name, side in SIDES.items():
        assert _size(BRAND / name) == (side, side), name


async def test_home_assistant_serves_the_mark_for_every_brand_image(
    hass: HomeAssistant, hass_client: ClientSessionGenerator
) -> None:
    """The loader sees the folder, and the brands API answers every image it
    allows from it, without asking the brands CDN: the icon at 1x, its 2x
    where a 2x is asked for and found, and the icon in place of a logo or a
    dark variant."""
    integration = await async_get_integration(hass, DOMAIN)
    assert integration.has_branding
    assert await async_setup_component(hass, "brands", {})
    client = await hass_client()
    icon = (BRAND / "icon.png").read_bytes()
    icon_2x = (BRAND / "icon@2x.png").read_bytes()
    for image in sorted(ALLOWED_IMAGES):
        resp = await client.get(f"/api/brands/integration/{DOMAIN}/{image}")
        assert resp.status == 200, image
        assert resp.content_type == "image/png"
        expected = icon_2x if image in ("icon@2x.png", "dark_icon@2x.png") else icon
        assert await resp.read() == expected, image
