# ADR 0012 — One branch: `main` is the branch, a `v*` tag is a release

**Status:** accepted
**Date:** 2026-09-19
**Supersedes:** [ADR 0004](0004-branching.md)

## Decision

One long-lived branch, `main`. A release is a `v*` tag cut from it.

| ref | publishes | deploy from |
|---|---|---|
| `main` | `:latest`, `:main-<sha>` | no |
| `v*` | `:<version>`, `:latest` | **yes** |

`prerelease` is deleted. A topic branch is still available when a change
genuinely needs isolating; that rule is unchanged from ADR 0004 and so is the
habit that came with it — check the working tree is clean before starting and
before committing.

## Why the old shape expired

ADR 0004's two branches existed to do one job: keep an unvalidated build out of
`:latest`, so that pulling the default tag could not pick up something that had
never run on hardware. `prerelease` published `:pre`, `main` published
`:latest`, and the deployment pulled `:pre`.

That job is gone, because **the deployment no longer pulls a moving tag at
all.** compose.yaml pins `:v0.1.0`. A version tag cannot pick up an
unvalidated build by definition: it points at one manifest for ever.

So the protection ADR 0004 bought is now bought by the pin, and the second
branch was paying for it twice.

## What made this urgent rather than tidy

The floating tag was not merely redundant, it was actively wrong, and it cost a
release.

On 2026-09-19 the phonemiser lock, the `/health` redaction, the innerHTML fix
and the upload ceiling were all merged, and all of them built. The release
published `:v0.1.0` and `:latest`. It did not move `:pre` — nothing does, except
a push to `prerelease`, and by then the work had moved to `main`. The NAS was
pulling `:pre`, so it kept serving a four-week-old build while every fix sat in
a registry it was not looking at. The deploy reported success and changed
nothing.

Two branches with two moving tags is two things that can drift apart. One
branch and an immutable version tag is one thing that cannot.

## Consequences

- `.github/workflows/build.yml` builds on `main` and on `v*`. The
  `refs/heads/prerelease` case and the `:pre` tags are gone.
- `:pre` images already on ghcr are left alone. They are the record of what was
  deployed before this, and deleting them would make the history of the
  deployment unreadable.
- Deploying means editing the pinned version in compose.yaml. That is a
  deliberate edit, which is the point: `git checkout <tag>` gives you the stack
  that is running.
- The validation rule from ADR 0004 survives the branch it was attached to. A
  `v*` tag still means *exercised on orko and seen to work* — it is now the tag
  that carries the promise rather than the branch.
