"""Tools a language model word may call, chosen for a voice assistant's speed.

A spoken question is answered in one turn, so every tool here returns in a
second or two, and returns little: a few lines the model reads to write one
spoken sentence, never a page to read aloud.

    web_search   SearXNG's JSON API (SATELLITES_SEARXNG_URL): the top results'
                 titles and snippets, and any direct answer or infobox. No page
                 is fetched: snippets are what make it fast, and they hold the
                 answer to most of what a voice assistant is asked. SafeSearch
                 is the instance's own setting.
    weather      Open-Meteo (no key): now and the next three days, at home or
                 at a named place (its geocoder, in the question's language),
                 in the household's units (SATELLITES_UNITS, else Home
                 Assistant's unit system, else metric).

The date and time are not a tool. They go into every language model's
system prompt (now_line), so "what day is it" costs no round trip.

WHERE HOME IS. SATELLITES_HOME_LAT, SATELLITES_HOME_LON, SATELLITES_HOME_NAME
and SATELLITES_TIMEZONE, when set. Otherwise Home Assistant's own configuration
(GET /api/config: latitude, longitude, location name, time zone), asked through
the first Home Assistant action a wake word has, with that action's token, and
kept for HOME_TTL_S; a failed ask is not repeated for HOME_RETRY_S. Every
language model turn primes it, with tools or without, since every prompt
carries the time. Neither: the weather needs a place named, and the clock is
TZ's, else UTC.

A tool never raises into the turn. A search that fails, times out, finds
nothing or answers in a shape it does not expect returns a sentence saying so,
and the model answers without it.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import time
import zoneinfo

import httpx

log = logging.getLogger("voice-satellites.tools")

NAMES = ("web_search", "weather")
SEARCH_TIMEOUT_S = 4.0
WEATHER_TIMEOUT_S = 4.0
HOME_TIMEOUT_S = 3.0
HOME_TTL_S = 3600.0
HOME_RETRY_S = 300.0
RESULTS = 5
SNIPPET_CHARS = 280
OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
GEOCODER = "https://geocoding-api.open-meteo.com/v1/search"

SPECS = {
    "web_search": {"type": "function", "function": {
        "name": "web_search",
        "description": "Search the web for facts you do not know or that change: news, results, "
                       "prices, schedules, recent events, people, places. Returns the top results' "
                       "titles and snippets. Search once with a short, precise query, then answer "
                       "from the snippets.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "A short, precise search query."}},
            "required": ["query"]}}},
    "weather": {"type": "function", "function": {
        "name": "weather",
        "description": "The weather now and the forecast for the next three days. Without a "
                       "location it is the weather at home.",
        "parameters": {"type": "object", "properties": {
            "location": {"type": "string",
                         "description": "A city, only when the question names a place other than home."}},
            "required": []}}},
}

# WMO weather codes, as Open-Meteo reports them, in words a model can say.
WMO = {0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "freezing fog",
       51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "freezing drizzle", 57: "freezing drizzle",
       61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "freezing rain",
       71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains", 80: "light showers",
       81: "showers", 82: "heavy showers", 85: "snow showers", 86: "heavy snow showers",
       95: "thunderstorm", 96: "thunderstorm with hail", 99: "thunderstorm with hail"}

_home: dict | None = None
_home_at: float | None = None  # when Home Assistant was last asked, answered or not


def guidance(names) -> str:
    """What a model with tools is told about them, beside its own prompt."""
    have = ", ".join(n for n in NAMES if n in names)
    return (f"You can call these tools: {have}. Use one only when the answer needs fresh or local "
            "facts you do not have; one call is usually enough. Then answer in one or two spoken "
            "sentences from what it returned. Never read out links, sources or result numbers.")


def _zone() -> datetime.tzinfo:
    name = os.getenv("SATELLITES_TIMEZONE") or (_home or {}).get("time_zone") or os.getenv("TZ") or "UTC"
    try:
        return zoneinfo.ZoneInfo(name)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        return datetime.timezone.utc


def now_line(now: datetime.datetime | None = None) -> str:
    """The date and time where the satellites are, for the system prompt."""
    zone = _zone()
    t = (now or datetime.datetime.now(datetime.timezone.utc)).astimezone(zone)
    where = f" ({(_home or {}).get('name')})" if (_home or {}).get("name") else ""
    return f"Now it is {t.strftime('%A %d %B %Y, %H:%M')}, local time{where}, time zone {zone}."


def _from_env() -> dict | None:
    lat, lon = os.getenv("SATELLITES_HOME_LAT"), os.getenv("SATELLITES_HOME_LON")
    if not (lat and lon):
        return None
    try:
        return {"lat": float(lat), "lon": float(lon), "name": os.getenv("SATELLITES_HOME_NAME") or "home",
                "time_zone": os.getenv("SATELLITES_TIMEZONE")}
    except ValueError:
        return None


def _units() -> str:
    """"us" (°F, mph, inches) or "metric": SATELLITES_UNITS, else Home
    Assistant's unit system as /api/config gave it, else metric."""
    chosen = (os.getenv("SATELLITES_UNITS") or "").strip().lower()
    if chosen in ("us", "metric"):
        return chosen
    return (_home or {}).get("units") or "metric"


def _home_assistant() -> tuple[str, str] | None:
    """The first Home Assistant action's address and token variable."""
    from . import (
        router as routing,  # noqa: PLC0415 - the router imports destinations, which imports this
    )
    listing = getattr(routing.current().rules, "listing", None)
    for item in listing() if callable(listing) else []:
        d = (item.get("action") or {}).get("destination") or {}
        if d.get("type") in ("ha_assist", "ha_conversation") and d.get("url"):
            return d["url"].rstrip("/"), d.get("token_env") or "SATELLITES_HA_TOKEN"
    return None


async def prime(client: httpx.AsyncClient) -> None:
    """Know where home is: from the environment, or asked of Home Assistant
    once per HOME_TTL_S, and after a failure not again for HOME_RETRY_S, so a
    Home Assistant that is down or refuses the token costs one turn a round
    trip, not every turn. Quiet on failure, a bad token included: the clock
    falls back to TZ or UTC, and the turn goes on."""
    global _home, _home_at
    env = _from_env()
    if env:
        _home = env
        return
    if _home_at is not None and time.monotonic() - _home_at < (HOME_TTL_S if _home is not None
                                                                  else HOME_RETRY_S):
        return
    _home_at = time.monotonic()
    found = _home_assistant()
    if not found:
        return
    from .destinations import (  # noqa: PLC0415 - one place turns a name into a value
        DestinationError,
        _secret,
    )
    url, token_env = found
    try:
        token = _secret(token_env)
        if not token:
            return
        r = await client.get(f"{url}/api/config", headers={"Authorization": f"Bearer {token}"},
                             timeout=HOME_TIMEOUT_S)
        c = r.json() if r.status_code == 200 else {}
        units = c.get("unit_system") if isinstance(c.get("unit_system"), dict) else {}
        # A Home Assistant with no location set still knows its time zone.
        home = {"name": c.get("location_name") or None, "time_zone": c.get("time_zone"),
                "units": "us" if units.get("temperature") == "°F" else "metric"}
        if isinstance(c.get("latitude"), (int, float)) and isinstance(c.get("longitude"), (int, float)):
            home |= {"lat": c["latitude"], "lon": c["longitude"], "name": home["name"] or "home"}
        if home["time_zone"] or "lat" in home:
            _home = home
    except (httpx.HTTPError, ValueError, AttributeError, DestinationError) as e:
        log.info("tools: Home Assistant's location was not read (%s)", type(e).__name__)


async def run(name: str, arguments: str, client: httpx.AsyncClient, allowed, language: str | None) -> str:
    """One tool call's result, as text for the model. Never raises."""
    if name not in allowed:
        return f"There is no tool called {name}."
    try:
        args = json.loads(arguments or "{}")
    except ValueError:
        args = None
    if not isinstance(args, dict):
        return f"The arguments for {name} were not a JSON object."
    try:
        if name == "web_search":
            return await web_search(client, str(args.get("query") or "").strip(), language)
        return await weather(client, str(args.get("location") or "").strip(), language)
    except Exception as e:  # noqa: BLE001 - any answer in a shape not expected; CancelledError is not one
        log.info("tools: %s failed: %s", name, type(e).__name__)
        return f"The {name.replace('_', ' ')} failed ({type(e).__name__}); answer without it."


async def web_search(client: httpx.AsyncClient, query: str, language: str | None) -> str:
    base = os.getenv("SATELLITES_SEARXNG_URL", "").rstrip("/")
    if not base:
        return "Web search is not set up on this hub (SATELLITES_SEARXNG_URL); answer without it."
    if not query:
        return "The search had no query."
    # No safesearch: the instance's own setting stands. Forced off, what it
    # found was read aloud in a home, to whoever asked.
    params = {"q": query, "format": "json"}
    if language and language != "auto":
        params["language"] = language
    r = await client.get(f"{base}/search", params=params, timeout=SEARCH_TIMEOUT_S)
    r.raise_for_status()
    data = r.json()
    lines: list[str] = []
    for answer in (data.get("answers") or [])[:2]:
        text = answer.get("answer") if isinstance(answer, dict) else answer
        if text:
            lines.append(f"Direct answer: {str(text)[:SNIPPET_CHARS]}")
    for box in (data.get("infoboxes") or [])[:1]:
        if isinstance(box, dict) and box.get("content"):
            lines.append(f"{box.get('infobox', '')}: {str(box['content'])[:SNIPPET_CHARS * 2]}")
    for n, res in enumerate((data.get("results") or [])[:RESULTS], 1):
        title = str(res.get("title") or "").strip()
        snippet = " ".join(str(res.get("content") or "").split())[:SNIPPET_CHARS]
        when = f" ({res['publishedDate'][:10]})" if res.get("publishedDate") else ""
        lines.append(f"{n}. {title}{when}: {snippet}")
    if not lines:
        return f"The search for {query!r} found nothing."
    return f"Web results for {query!r}:\n" + "\n".join(lines)


async def weather(client: httpx.AsyncClient, location: str, language: str | None) -> str:
    if location:
        g = await client.get(GEOCODER, params={"name": location, "count": 1,
                                               "language": (language or "en").split("-")[0]},
                             timeout=WEATHER_TIMEOUT_S)
        g.raise_for_status()
        hits = g.json().get("results") or []
        if not hits:
            return f"No place called {location!r} was found."
        h = hits[0]
        place = {"lat": h["latitude"], "lon": h["longitude"],
                 "name": ", ".join(p for p in (h.get("name"), h.get("admin1"), h.get("country")) if p)}
    else:
        await prime(client)
        if not _home or "lat" not in _home:
            return "Where home is is not known here; ask for a named place."
        place = _home
    us = _units() == "us"
    deg, speed, depth = ("°F", "mph", "in") if us else ("°C", "km/h", "mm")
    r = await client.get(OPEN_METEO, params={
        "latitude": place["lat"], "longitude": place["lon"], "timezone": "auto", "forecast_days": 3,
        "current": "temperature_2m,apparent_temperature,relative_humidity_2m,precipitation,weather_code,wind_speed_10m",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,precipitation_sum",
    } | ({"temperature_unit": "fahrenheit", "wind_speed_unit": "mph", "precipitation_unit": "inch"}
         if us else {}), timeout=WEATHER_TIMEOUT_S)
    r.raise_for_status()
    d = r.json()
    now, daily = d.get("current") or {}, d.get("daily") or {}
    days = []
    for i, day in enumerate(daily.get("time") or []):
        days.append(f"{day}: {WMO.get(daily['weather_code'][i], 'unknown')}, "
                    f"{daily['temperature_2m_min'][i]} to {daily['temperature_2m_max'][i]} {deg}, "
                    f"rain chance {daily['precipitation_probability_max'][i]}%, "
                    f"{daily['precipitation_sum'][i]} {depth}")
    return (f"Weather for {place['name']}. Now: {WMO.get(now.get('weather_code'), 'unknown')}, "
            f"{now.get('temperature_2m')} {deg} (feels like {now.get('apparent_temperature')} {deg}), "
            f"humidity {now.get('relative_humidity_2m')}%, wind {now.get('wind_speed_10m')} {speed}, "
            f"precipitation {now.get('precipitation')} {depth}.\nNext days:\n" + "\n".join(days))
