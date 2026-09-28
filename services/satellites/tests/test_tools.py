"""Tools a language model word may call: the round trip, and the tools.

Every server is a fake on an httpx.MockTransport: the model on llm.test,
SearXNG on searx.test, Open-Meteo on its own hosts, Home Assistant on ha.test.
Nothing reaches the network.
"""

from __future__ import annotations

import datetime
import json

import httpx
import pytest
from test_llm import LLM, ask, delta, one_body, sent, sse
from test_router import Fake

from app import tools
from app.destinations import DestinationError, Llm

SEARX = {"results": [
    {"title": "Race report", "content": "Norris won the Singapore Grand Prix on Sunday.",
     "publishedDate": "2026-09-27T14:00:00"},
    {"title": "Standings", "content": "Piastri leads the championship."}],
    "answers": [], "infoboxes": []}


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("SATELLITES_SEARXNG_URL", "http://searx.test")
    for name in ("SATELLITES_HOME_LAT", "SATELLITES_HOME_LON", "SATELLITES_HOME_NAME",
                 "SATELLITES_TIMEZONE", "TZ", "SATELLITES_LLM_API_KEY", "SATELLITES_UNITS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(tools, "_home", None)
    monkeypatch.setattr(tools, "_home_at", None)
    monkeypatch.setattr(Llm, "limit_names", {})


@pytest.fixture
def fake() -> Fake:
    f = Fake()
    f.handlers["searx.test"] = lambda r: httpx.Response(200, json=SEARX)
    return f


@pytest.fixture
def client(fake) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(fake))


def has_tool_result(request: httpx.Request) -> bool:
    return any(m.get("role") == "tool" for m in json.loads(request.content)["messages"])


async def said(destination: dict, client: httpx.AsyncClient, text: str = "who won the race") -> str:
    return "".join([p async for p in Llm.model_validate(destination).answer(client, ask(text))])


SEARCHING = LLM | {"tools": ["web_search"]}


async def test_a_streamed_search_runs_and_the_answer_is_spoken_from_its_results(fake, client):
    """The call arrives in pieces (its id and name, then the arguments in two
    parts); the hub searches, sends the results back, and speaks the answer."""
    def model(r: httpx.Request) -> httpx.Response:
        if not has_tool_result(r):
            return sse(delta(tool_calls=[{"index": 0, "id": "call_1", "type": "function",
                                          "function": {"name": "web_search", "arguments": ""}}]),
                       delta(tool_calls=[{"index": 0, "function": {"arguments": '{"query": "singapore '}}]),
                       delta("tool_calls", tool_calls=[{"index": 0, "function": {"arguments": 'gp winner"}'}}]))
        return sse(delta(content="Lando Norris won "), delta("stop", content="in Singapore."))
    fake.handlers["llm.test"] = model

    assert await said(SEARCHING, client) == "Lando Norris won in Singapore."
    first, second = sent(fake)
    assert first["tools"] == [tools.SPECS["web_search"]] and "tool_choice" not in first
    [search] = [r for r in fake.seen if r.url.host == "searx.test"]
    assert (search.url.params["q"], search.url.params["format"]) == ("singapore gp winner", "json")
    asked, answered = second["messages"][-2:]
    assert asked["tool_calls"] == [{"id": "call_1", "type": "function",
                                    "function": {"name": "web_search", "arguments": '{"query": "singapore gp winner"}'}}]
    assert answered["role"] == "tool" and answered["tool_call_id"] == "call_1"
    assert "Norris won the Singapore Grand Prix on Sunday." in answered["content"]
    assert "(2026-09-27)" in answered["content"]


async def test_a_one_body_answer_asks_for_tools_the_same_way(fake, client):
    def model(r: httpx.Request) -> httpx.Response:
        if not has_tool_result(r):
            return one_body(None, "tool_calls", tool_calls=[{"id": "c", "type": "function", "function": {
                "name": "web_search", "arguments": '{"query": "f1"}'}}])
        return one_body("Norris won.")
    fake.handlers["llm.test"] = model
    assert await said(SEARCHING | {"stream": False}, client) == "Norris won."
    assert len(sent(fake)) == 2


async def test_after_two_rounds_the_model_must_answer(fake, client):
    """A model that keeps asking is asked a third time with tool_choice none."""
    def model(r: httpx.Request) -> httpx.Response:
        if json.loads(r.content).get("tool_choice") == "none":
            return sse(delta("stop", content="Norris won."))
        return sse(delta("tool_calls", tool_calls=[{"index": 0, "id": "x", "function": {
            "name": "web_search", "arguments": '{"query": "again"}'}}]))
    fake.handlers["llm.test"] = model
    assert await said(SEARCHING, client) == "Norris won."
    assert [b.get("tool_choice") for b in sent(fake)] == [None, None, "none"]
    assert len([r for r in fake.seen if r.url.host == "searx.test"]) == 2


async def test_a_tool_not_offered_or_failing_is_answered_in_words_not_raised(fake, client):
    fake.handlers["searx.test"] = lambda r: httpx.Response(500, text="boom")
    def model(r: httpx.Request) -> httpx.Response:
        if not has_tool_result(r):
            return sse(delta("tool_calls", tool_calls=[
                {"index": 0, "id": "a", "function": {"name": "web_search", "arguments": '{"query": "f1"}'}},
                {"index": 1, "id": "b", "function": {"name": "shell", "arguments": "{}"}}]))
        return sse(delta("stop", content="I could not look that up."))
    fake.handlers["llm.test"] = model
    assert await said(SEARCHING, client) == "I could not look that up."
    results = {m["tool_call_id"]: m["content"] for m in sent(fake)[1]["messages"] if m["role"] == "tool"}
    assert results["a"].startswith("The web search failed (HTTPStatusError)")
    assert results["b"] == "There is no tool called shell."


async def test_without_tools_nothing_about_tools_is_sent(fake, client):
    fake.handlers["llm.test"] = lambda r: sse(delta("stop", content="Hi."))
    await said(LLM, client)
    [body] = sent(fake)
    assert "tools" not in body and "tool_choice" not in body
    assert "You can call these tools" not in body["messages"][0]["content"]


async def test_every_prompt_has_the_local_date_and_time(fake, client, monkeypatch):
    monkeypatch.setenv("SATELLITES_TIMEZONE", "America/Sao_Paulo")
    line = tools.now_line(datetime.datetime(2026, 9, 28, 20, 30, tzinfo=datetime.timezone.utc))
    assert line == ("Now it is Monday 28 September 2026, 17:30, local time, "
                    "time zone America/Sao_Paulo.")
    fake.handlers["llm.test"] = lambda r: sse(delta("stop", content="Hi."))
    await said(LLM, client)
    assert "Now it is " in sent(fake)[0]["messages"][0]["content"]


OPEN_METEO = {"current": {"temperature_2m": 21.4, "apparent_temperature": 22.0, "relative_humidity_2m": 60,
                          "precipitation": 0.0, "weather_code": 2, "wind_speed_10m": 9.5},
              "daily": {"time": ["2026-09-28", "2026-09-29"], "weather_code": [2, 61],
                        "temperature_2m_max": [26.0, 23.0], "temperature_2m_min": [15.0, 16.0],
                        "precipitation_probability_max": [10, 80], "precipitation_sum": [0.0, 6.2]}}


async def test_the_weather_at_home_comes_from_the_environment(fake, client, monkeypatch):
    monkeypatch.setenv("SATELLITES_HOME_LAT", "-23.5")
    monkeypatch.setenv("SATELLITES_HOME_LON", "-46.6")
    monkeypatch.setenv("SATELLITES_HOME_NAME", "Home Town")
    fake.handlers["api.open-meteo.com"] = lambda r: httpx.Response(200, json=OPEN_METEO)
    text = await tools.run("weather", "{}", client, ["weather"], "en")
    assert text.startswith("Weather for Home Town. Now: partly cloudy, 21.4 °C")
    assert "2026-09-29: light rain, 16.0 to 23.0 °C, rain chance 80%, 6.2 mm" in text
    [forecast] = [r for r in fake.seen if r.url.host == "api.open-meteo.com"]
    assert (forecast.url.params["latitude"], forecast.url.params["longitude"]) == ("-23.5", "-46.6")


async def test_the_weather_somewhere_named_is_geocoded_in_the_questions_language(fake, client):
    fake.handlers["geocoding-api.open-meteo.com"] = lambda r: httpx.Response(200, json={"results": [
        {"name": "Lisboa", "admin1": "Lisboa", "country": "Portugal", "latitude": 38.7, "longitude": -9.1}]})
    fake.handlers["api.open-meteo.com"] = lambda r: httpx.Response(200, json=OPEN_METEO)
    text = await tools.run("weather", '{"location": "Lisboa"}', client, ["weather"], "pt-BR")
    assert text.startswith("Weather for Lisboa, Lisboa, Portugal.")
    [geo] = [r for r in fake.seen if r.url.host == "geocoding-api.open-meteo.com"]
    assert (geo.url.params["name"], geo.url.params["language"]) == ("Lisboa", "pt")


async def test_home_and_its_time_zone_come_from_home_assistant_when_not_configured(fake, client, monkeypatch):
    monkeypatch.setattr(tools, "_home_assistant", lambda: ("http://ha.test:8123", "SATELLITES_HA_TOKEN"))
    monkeypatch.setenv("SATELLITES_HA_TOKEN", "ha-token-do-not-leak")
    fake.handlers["ha.test"] = lambda r: httpx.Response(200, json={
        "latitude": -23.5, "longitude": -46.6, "location_name": "Casa", "time_zone": "America/Sao_Paulo"})
    await tools.prime(client)
    [config] = [r for r in fake.seen if r.url.host == "ha.test"]
    assert config.url.path == "/api/config"
    assert config.headers["authorization"] == "Bearer ha-token-do-not-leak"
    line = tools.now_line(datetime.datetime(2026, 9, 28, 20, 30, tzinfo=datetime.timezone.utc))
    assert line.endswith("17:30, local time (Casa), time zone America/Sao_Paulo.")
    await tools.prime(client)  # kept, not asked again
    assert len([r for r in fake.seen if r.url.host == "ha.test"]) == 1


async def test_a_word_without_tools_tells_the_time_in_home_assistants_zone(fake, client, monkeypatch):
    """Home was primed only for a word with tools, so the default language
    model word, with none, told the time in UTC although the README promised
    Home Assistant's zone. A Home Assistant with no location set still gives
    its zone, and its unit system."""
    monkeypatch.setattr(tools, "_home_assistant", lambda: ("http://ha.test:8123", "SATELLITES_HA_TOKEN"))
    monkeypatch.setenv("SATELLITES_HA_TOKEN", "ha-token-do-not-leak")
    fake.handlers["ha.test"] = lambda r: httpx.Response(200, json={
        "time_zone": "America/New_York", "unit_system": {"temperature": "°F", "length": "mi"}})
    fake.handlers["llm.test"] = lambda r: sse(delta("stop", content="Hi."))
    await said(LLM, client)
    assert "time zone America/New_York." in sent(fake)[0]["messages"][0]["content"]
    assert tools._units() == "us"
    assert await tools.run("weather", "{}", client, ["weather"], "en") == (
        "Where home is is not known here; ask for a named place.")


async def test_a_home_assistant_that_fails_is_not_asked_again_every_turn(fake, client, monkeypatch):
    """A failed /api/config was asked again on every turn, each a round trip
    (up to 3 s on a Home Assistant that times out) before the model."""
    monkeypatch.setattr(tools, "_home_assistant", lambda: ("http://ha.test:8123", "SATELLITES_HA_TOKEN"))
    monkeypatch.setenv("SATELLITES_HA_TOKEN", "ha-token-do-not-leak")
    fake.handlers["ha.test"] = lambda r: httpx.Response(502, text="bad gateway")
    await tools.prime(client)
    await tools.prime(client)
    assert len([r for r in fake.seen if r.url.host == "ha.test"]) == 1
    monkeypatch.setattr(tools, "_home_at", tools._home_at - tools.HOME_RETRY_S - 1)
    await tools.prime(client)
    assert len([r for r in fake.seen if r.url.host == "ha.test"]) == 2, "never asked again"


async def test_a_bad_home_assistant_token_does_not_fail_the_turn(fake, client, monkeypatch):
    """A token with a line break raised out of prime, so every language
    model turn failed with Home Assistant's token error."""
    monkeypatch.setattr(tools, "_home_assistant", lambda: ("http://ha.test:8123", "SATELLITES_HA_TOKEN"))
    monkeypatch.setenv("SATELLITES_HA_TOKEN", "abc\r")
    fake.handlers["llm.test"] = lambda r: sse(delta("stop", content="Hi."))
    assert await said(SEARCHING, client) == "Hi."
    assert [r for r in fake.seen if r.url.host == "ha.test"] == []


@pytest.mark.parametrize("body", [{"results": ["text"]}, ["a", "b"]])
async def test_a_search_answer_in_another_shape_is_a_failed_search_not_a_failed_turn(fake, client, body):
    fake.handlers["searx.test"] = lambda r: httpx.Response(200, json=body)
    assert await tools.run("web_search", '{"query": "f1"}', client, ["web_search"], "en") == (
        "The web search failed (AttributeError); answer without it.")


async def test_a_search_leaves_safesearch_to_the_instance(fake, client):
    await tools.run("web_search", '{"query": "f1"}', client, ["web_search"], "en")
    [search] = [r for r in fake.seen if r.url.host == "searx.test"]
    assert "safesearch" not in search.url.params


async def test_the_weather_is_in_the_households_units(fake, client, monkeypatch):
    monkeypatch.setenv("SATELLITES_HOME_LAT", "40.7")
    monkeypatch.setenv("SATELLITES_HOME_LON", "-74.0")
    monkeypatch.setenv("SATELLITES_UNITS", "us")
    fake.handlers["api.open-meteo.com"] = lambda r: httpx.Response(200, json=OPEN_METEO)
    text = await tools.run("weather", "{}", client, ["weather"], "en")
    [forecast] = [r for r in fake.seen if r.url.host == "api.open-meteo.com"]
    assert (forecast.url.params["temperature_unit"], forecast.url.params["wind_speed_unit"],
            forecast.url.params["precipitation_unit"]) == ("fahrenheit", "mph", "inch")
    assert "21.4 °F" in text and "9.5 mph" in text and "6.2 in" in text and "°C" not in text


async def test_a_model_that_ignores_tool_choice_none_fails_in_words_not_silence(fake, client):
    """Asked with tool_choice none, a server that ignores it asks for tools
    again: the turn ended with nothing said and no error."""
    fake.handlers["llm.test"] = lambda r: sse(delta("tool_calls", tool_calls=[
        {"index": 0, "id": "x", "function": {"name": "web_search", "arguments": '{"query": "again"}'}}]))
    with pytest.raises(DestinationError, match="still asked for tools after 2 rounds"):
        await said(SEARCHING, client)


async def test_what_a_one_body_answer_says_beside_its_tool_calls_is_spoken(fake, client):
    def model(r: httpx.Request) -> httpx.Response:
        if not has_tool_result(r):
            return one_body("Let me check.", "tool_calls", tool_calls=[{"id": "c", "type": "function",
                            "function": {"name": "web_search", "arguments": '{"query": "f1"}'}}])
        return one_body("Norris won.")
    fake.handlers["llm.test"] = model
    assert await said(SEARCHING | {"stream": False}, client) == "Let me check. Norris won."
