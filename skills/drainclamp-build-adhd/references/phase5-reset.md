# Gate 5 — Reset

Runs at a **real milestone boundary**. Inside a milestone, the only boundary is a finished chunk
(see "Chunk boundaries").

## 1. Write state back first

```
dc_state.py --append-log "<one sentence: what the diff did>"
dc_state.py --set ROADMAP --file <updated roadmap>
```

State is written before any purge is recommended. A purge with unsaved state loses the work.

With Gate 6 review mode on, the roadmap write refuses `done` until the review clears (exit 2 findings,
5 stale, 6 not run). That refusal is the gate working: run `phase6-review.md`, do not edit around it.

## 2. Ask for the verdict

```
dc_state.py --purge-check [--context-high]
```

`overlap = |intersection(completed, next)| / |next|` over the `DC:ROADMAP` file sets.

- `COMPLETE` when every roadmap row is `done`. Checked **before** the overlap calculus, which has no
  next milestone to compare against and would file a finished project as `HOLD (no next milestone)`.
- `HOLD (review open: n)` while Gate 6 findings wait on a decision: the context holding them is
  the cheapest place to resolve them. Checked after `COMPLETE`, before the overlap calculus.
- `PURGE` iff `overlap < 0.20`. Exactly 0.20 is `HOLD`.
- Missing or unresolved data always yields `HOLD (overlap unknown: <reason>)`. It never defaults to
  `PURGE` — purging on an unknown is how work gets lost. In particular, **no completed milestone
  means the overlap is undefined, not zero**; an empty comparison set would otherwise render as a
  confident `0%` and trigger a purge on the first milestone of every project.
- Reasons: `no next milestone` · `next milestone empty` · `multiple eligible next milestones` ·
  `no completed milestone` · `completed milestone empty` · `unresolved path pattern: <pattern>`.
- Explicitly named repo-relative files count even when they do not exist yet. Only unresolvable
  directories and globs force `unknown`.

`--context-high` is **model-observed**: pass it when you judge context to be over ~50% of the
window. No script can read the host's context usage, so this is your assertion, not a measurement.
It forces `PURGE` but never manufactures an overlap figure — an unknown overlap stays unknown.

## 3. Report honestly

You cannot invoke `/clear` or `/compact`. No host exposes them to the model. Gate 5 therefore
**recommends** a purge; it never announces one.

| Verdict | Output |
|---|---|
| `COMPLETE`, host documents a clear command | script line, then `Run /clear now, then mark the project complete with dc_session.py --complete <n>.` |
| `COMPLETE`, host support unknown | script line, then `Start a fresh session, then mark the project complete with dc_session.py --complete <n>.` |
| `PURGE`, host documents a compaction command | `DRAINCLAMP: State saved to .agent/drainclamp-state.md. Context purge recommended.`<br>`Run /compact now, then resume from .agent/drainclamp-state.md.` |
| `PURGE`, host support unknown | first line, then `Start a fresh session and resume from .agent/drainclamp-state.md.` |
| `HOLD` | `DRAINCLAMP: State saved to .agent/drainclamp-state.md. Context retained (overlap NN%).` |
| `HOLD`, overlap unresolved | `DRAINCLAMP: State saved to .agent/drainclamp-state.md. Context retained (overlap unknown: <reason>).` |

Print the `/compact` line **only** when the host documents that command. Claude Code does; check
`platform-adapters.md` before assuming any other host does.

Never use a `[SYSTEM: …]` prefix. That impersonates the host, and this is ordinary skill output.

## 4. Resume

Only a genuinely fresh session may say a reset happened:

```
DRAINCLAMP: Resumed from .agent/drainclamp-state.md. Context reset confirmed.
```

This requires host evidence or an explicit fresh-session signal — never self-assertion in the same
session that recommended the purge.

Resume reads **only** `.agent/drainclamp-state.md`, then `dc_project.py next` for the current
chunk. Never auto-load `.agent/drainclamp-log-archive.md` or earlier chunk notes; they exist for
human forensics, not for context.

## Chunk boundaries

`dc_project.py chunk done` runs the same calculus between the finished chunk and the next open
chunk in the milestone. It compares their **targets** by path, so two symbols of one file overlap.

| Output | Say |
|---|---|
| `PURGE (chunk overlap NN%)` or `PURGE (chunk context-high; ...)` | its `DRAINCLAMP:` line, then `Run /compact now, then resume with dc_project.py next.` (host support unknown: `Start a fresh session and resume with dc_project.py next.`) |
| `HOLD (chunk ...)` | its `DRAINCLAMP:` line; continue with the next chunk |
| `all chunks done` | close the milestone: section 1, then `--purge-check`. When the line names Gate 6, run Gate 3 milestone tier and Gate 6 first |

- The chunk tick is the save: the sidecar already holds the note, and `next` rebuilds the resume
  view from it. No state-file write is needed mid-milestone.
- The same rules hold: unknown is never `PURGE`, and `--context-high` is your judgement, not a
  measurement.
- A chunk `PURGE` is a recommendation. Several small chunks in a row over different files will
  each say `PURGE`; judge whether reloading context costs more than it saves.

## Failure interaction

A failed implementation or verification emits **no** Gate 5 terminator and does not mark the
milestone `done`. It also cannot retroactively unsay an earlier gate's success sentence — Gate 2
correctly reported what it did at the time.
