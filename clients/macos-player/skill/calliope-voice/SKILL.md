---
name: calliope-voice
description: Say an explanation out loud through Calliope, the Mac text-to-speech app, instead of only writing it. Use when the user wants to HEAR something - "read it to me", "say it", "explain it out loud", "talk me through it", a spoken, vocal or narrated explanation, or an audio version of an answer, summary or document. Writes the text for the ear, plays it with the `calliope` command, and can save it as an audio file.
---

# Speak it with Calliope

## 1. Check that Calliope is there

```bash
calliope status
```

If the command is missing, tell the user to install Calliope.app: its installer puts `calliope`
in `~/.local/bin`. If `status` says Calliope is not answering, ask them to open Calliope.app.

## 2. Write it for the ear

Write the explanation as a Markdown file. Somebody will listen to it, maybe without the
screen, so:

- Open with one sentence that says what the explanation covers.
- Use short sentences, and one idea per paragraph.
- Use headings and short lists if they help. Do not use tables or code blocks: they are not
  read aloud. Describe the code in words instead.
- Spell out symbols and abbreviations the first time ("the Domain Name System, DNS").
- Write numbers as words where they could be read two ways ("ten thousand", not "10k";
  "the third of October", not "3/10").
- Say file paths, commands and URLs only when the user needs them, and then in words.

Save the file where the user asked. Otherwise, save it in the system temporary directory
(`$TMPDIR`, or `/tmp`). Tell the user the path.

## 3. Play it

```bash
calliope speak --reader -f /path/to/explanation.md
```

The command returns at once and a player reads the text, with the reader window showing it.
A `.md` file is converted to speakable prose: code, links and markup are not read.
A new `speak` replaces what is playing.

## Options

- An audio file instead of, or as well as, playback. Offer it:

  ```bash
  calliope save -f /path/to/explanation.md -o /path/to/explanation.wav
  ```

  `.wav` always works. `.mp3` and other formats need a voice from a Calliope server.
- Other voices and languages: `calliope voices` lists them. Give one with `--voice`, for
  example `--voice mac/bf_emma`. Without `--voice`, the user's own choice for the language is
  used.
- `calliope --help` and `calliope <command> --help` list everything else.
