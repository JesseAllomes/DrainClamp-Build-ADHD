# Gate 5 — Reset

Runs at a **real milestone boundary**, never mid-milestone.

## 1. Write state back first

```
dc_state.py --append-log "<one sentence: what the diff did>"
dc_state.py --set ROADMAP --file <updated roadmap>
```

State is written before any purge is recommended. A purge with unsaved state loses the work.

## 2. Ask for the verdict

```
dc_state.py --purge-check [--context-high]
```

`overlap = |intersection(completed, next)| / |next|` over the `DC:ROADMAP` file sets.

- `COMPLETE` when every roadmap row is `done`. Checked **before** the overlap calculus, which has no
  next milestone to compare against and would file a finished project as `HOLD (no next milestone)`.
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

Resume reads **only** `.agent/drainclamp-state.md`. Never auto-load
`.agent/drainclamp-log-archive.md`; it exists for human forensics, not for context.

## Failure interaction

A failed implementation or verification emits **no** Gate 5 terminator and does not mark the
milestone `done`. It also cannot retroactively unsay an earlier gate's success sentence — Gate 2
correctly reported what it did at the time.
