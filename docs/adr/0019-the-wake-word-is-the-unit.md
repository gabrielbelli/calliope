# ADR 0019 — The wake word is the unit of configuration, and it has one of three modes

**Status:** accepted
**Date:** 2026-09-25

## Context

The satellite hub began with two settings that described one thing.
`SATELLITES_WAKE_WORDS` named the words every satellite listened for, and
`rules.json` said where the command after each word went: a rule named a
wake word or `*`, optionally some satellites, and a destination. The page
had a Routing card that edited the rules. To make "hey jarvis" do something
new, an operator changed two places, and could not tell from either which
satellite would do what.

Every word also did the same kind of thing: hear one command, send it once,
speak the answer. Some words want a conversation that goes on without the
wake word, and some want no command at all ("lumos" is the whole request).

## Decision

**Each entry in `wake_words.json` is a whole behaviour**: the model, its
threshold, the satellites that hear it, its mode, a language hint, the pause
that ends a command, the ring colour, and its action (destination,
`reply_to`, voice, and for a command a `fallback`). Push-to-talk has an entry
of its own. The Satellites tab edits these entries, and only these.

**Three modes:**

| Mode | After the word |
|---|---|
| `command` | The command goes to the action once, and the reply plays |
| `conversation` | The same, then the satellite listens without the wake word for the next turn, and the action gets the turns so far |
| `trigger` | Nothing. The hub publishes `triggered`, and Home Assistant decides what it does. No speech-to-text runs, so the default threshold is stricter (0.7) and a cooldown stops one utterance firing twice |

**A command can hand over to a conversation.** Its `fallback` names a
conversation word, which takes the same transcript when the command's
destination fails or does not understand (Home Assistant's
`response_type: "error"`), and the satellite is then in that conversation.

**Saving merges by name.** `PUT /satellites/wake-words` replaces the set, and
a field an entry leaves out keeps its saved value. A client that knows only
`name`, `threshold` and `satellites` therefore cannot wipe an action by
moving a word to another room.

**The migration is one way.** A `wake_words.json` without `version` is from
before this decision. On its first start each word, and push-to-talk, gets
the rule it would have taken: the first rule in file order that names the
word or `*` and names no satellites. The hub writes version 2, leaves
`rules.json` untouched and does not read it again. `PUT /satellites/routing`
answers 409 `routing_per_wake_word`.

## What it costs

- **Rules that named satellites are left behind.** One entry per word cannot
  say "in the kitchen do this, in the bedroom that". The log names each rule
  it left behind at migration.
- **A word does the same thing on every satellite that hears it.** Two rooms
  that want different actions need two words.
- **`SATELLITES_WAKE_WORDS` only seeds the file** on a volume that has none.
  After that, changing the variable does nothing.

## Rejected

- **Keeping `rules.json` beside the words.** It was the two-places problem
  this decision removes.
