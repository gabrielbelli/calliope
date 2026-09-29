# ADR 0020 — A Home Assistant integration beside MQTT discovery, and both are optional

**Status:** accepted
**Date:** 2026-09-25

## Context

The satellite hub already published every adopted satellite to Home
Assistant over MQTT discovery (`SATELLITES_MQTT_URL`): switches, volume, Wi-Fi
signal, the audio output, the last wake word, and events for wake words and
buttons. MQTT needs no code in Home Assistant, only a broker.

Three things MQTT discovery cannot give:

- **Device triggers** for wake words, trigger words, buttons and
  conversations, offered by name in the automation editor.
- **Actions** on a satellite: say this, play a tone, start listening.
- **Calliope as Assist's speech engines**, so any Assist pipeline can use the
  stack's speech-to-text and Kokoro.

A trigger word has no command and does nothing on the hub: Home Assistant is
where its meaning is decided, so it has to arrive there as something an
automation can use without writing MQTT topics by hand.

## Decision

`clients/home-assistant` is a custom integration, `calliope`. It talks to the
gateway only, holds one connection to the hub's event stream and polls
nothing. It makes each adopted satellite a device with entities, device
triggers and actions, fires every hub event on the bus as `calliope_event`,
adds one speech-to-text entity per engine the stack serves and a Kokoro
text-to-speech entity, and keeps Home Assistant's names on the stack as a
glossary profile ([ADR 0017](0017-home-assistant-vocabulary.md)).

MQTT discovery stays. Neither needs the other, and the hub needs neither.

## What it costs

- **Two routes into Home Assistant.** With both on, each satellite appears
  twice, once per integration, with separate entities. The READMEs say to use
  one of them. The hub cannot tell which one an operator uses.
- **A second client to keep in step with the hub's events.** The integration
  reads the hub's event and field names; renaming one breaks it. It is tested
  against a fake gateway with the hub's shapes, not in CI.
- **HACS cannot install it from this repository**, because HACS reads a
  repository from its root. It installs by hand, or from a subtree split
  published on its own.
- **The satellite's room in Assist depends on it.** The hub's `ha_assist`
  action passes the satellite's device, which only this integration
  registers.

## Rejected

- **Only MQTT.** It gives no device triggers, no actions and no Assist
  engines.
- **Only the integration.** MQTT works for a Home Assistant that runs no
  custom code, and it was already built and checked against Home Assistant's
  own MQTT integration.
- **An Assist satellite entity.** The hub runs its own wake words,
  endpointing and routing, so a satellite is not an Assist satellite in Home
  Assistant's sense.
