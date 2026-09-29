# ADR 0018 — A language model word may call two tools, and the date is not one of them

**Status:** accepted
**Date:** 2026-09-28

## Context

A satellite's language model word answers from what the model knows. Asked
"what day is it", "will it rain tomorrow" or "who won last night", a model
without fresh facts guesses, or says it cannot know. A voice assistant is
asked exactly those questions, and it has one spoken turn to answer them in,
so whatever fetches the facts has to return within a second or two and return
little.

## Decision

A word's `llm` destination has `tools`, empty by default, which may hold
`web_search` and `weather` (`services/satellites/app/tools.py`):

- **`web_search`** asks a SearXNG instance that the operator runs
  (`SATELLITES_SEARXNG_URL`) through its JSON API, and gives the model the top
  five results' titles and snippets, 280 characters each, and any direct
  answer or infobox. No page is fetched.
- **`weather`** asks Open-Meteo, with no key, for now and the next three days,
  at home or at a place the question names (through Open-Meteo's geocoder),
  in the household's units.

The date and time are **not** a tool. Every language model prompt carries the
local date and time, with tools or without, so "what day is it" costs no
round trip.

The hub collects the calls the model asks for while the reply streams, runs
them together, sends the results back and asks again. It does this at most
twice, then asks with `tool_choice` `none`, so the model must answer. Each
tool has 4 s. A tool that fails, times out, finds nothing or gets an answer
in a shape it does not expect returns a sentence saying so, and the turn goes
on.

With no tool ticked, the request has no `tools` field, and is exactly what it
was before tools existed.

**Home** is `SATELLITES_HOME_LAT`, `SATELLITES_HOME_LON` and
`SATELLITES_HOME_NAME` when they are set. Otherwise the hub asks Home
Assistant's `GET /api/config` through the first Home Assistant action a wake
word has, with that action's token, and keeps the answer for an hour. The
time zone is `SATELLITES_TIMEZONE`, else Home Assistant's, else `TZ`, else
UTC.

## Why these two, and not more

- **Snippets, not pages.** Fetching and reading a page costs seconds, and a
  page is far more text than a spoken sentence needs. The snippets hold the
  answer to most of what a voice assistant is asked.
- **SearXNG, not a hosted search API.** An instance the operator runs needs
  no account and no key on the hub, and which engines it asks, and its
  SafeSearch, stay the operator's settings.
- **Open-Meteo** needs no key, answers in one request, and covers any place
  the geocoder knows.
- **Two rounds.** A spoken answer waits for one search, perhaps two. A model
  that keeps calling tools is an agent, and a satellite user cannot see what
  it is doing.

## What leaves the house, and to whom

- **`web_search`:** the model's search query goes to the operator's SearXNG,
  and from there to every search engine that instance uses.
- **`weather`:** the home's coordinates, or the place named, go to
  `api.open-meteo.com` and `geocoding-api.open-meteo.com` over the internet.
  The hub makes that call itself, so it happens even when the language model
  runs on the local network.
- **The home lookup** reads Home Assistant's location with a token that the
  operator gave for a Home Assistant action.

Open-Meteo's free API is for non-commercial use, at most 10,000 calls a day,
and its data is CC BY 4.0 (THIRD-PARTY-NOTICES.md).

## What a provider must support

The model and the server must support OpenAI's tool calling. Ticked tools go
with every question, and a server that cannot take them refuses every
request, not only the ones that need a tool. A server of your own may need
tool calling switched on: llama.cpp with `--jinja`, vLLM with automatic tool
choice, or an Ollama model that has tools. Before this was built, Claude and
Grok models reached through an OpenAI-compatible proxy were checked to call
tools.

## Rejected

- **The date as a tool.** It would cost a round trip on the one question
  that every prompt can answer for nothing.
- **Tools on by default.** A server without tool calling would refuse every
  question from a word that worked before.
