# Gate 5 — Reset

Runs at a **real milestone boundary**. Inside a milestone, the only boundary is a finished chunk
(see "Chunk boundaries").

## 1. Write state back first

```
dc_state.py --close <m> --log "<one sentence: what the diff did>"
```

`--close` flips only that row's status to `done`, appends the log line, and prints the section 2
verdict. `--mark <m>=<pending|active|done>` (repeatable) flips others. Both run the roadmap guard
and honour `--expect-generation`. Never hand-write a roadmap file for `--set ROADMAP`.

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
| `COMPLETE`, host documents a clear command | run `dc_project.py --root <root> complete --note "<one line>"` first, while context is loaded; then the script line and `Run /clear now.` |
| `COMPLETE`, host support unknown | run `dc_project.py --root <root> complete --note "<one line>"` first; then the script line and `Start a fresh session.` |
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

Resume is one call, `dc_session.py --resume <root>`: audit freshness, Done/In progress/Next, the
`dc_project.py next` capsule, then the `dc_state.py --resume-view` view.
The view keeps ARCH, the standing DECISIONS and VERIFY, all open roadmap rows and the last done row
(with files and dependencies), done ids, and the last five log lines; `SHOWING n/N` declares omissions.
When a new decision replaces an old one, mark it in the same step:
`dc_state.py --supersede <n> --by <m> --expect-generation <g>` (Gate 6 numbering, D1 = first line).
The full file keeps the line; the view leaves it out and counts it as superseded.
Read the full `.agent/drainclamp-state.md` when preparing Gate 2 edits; the view is not a state
replacement. Never auto-load `.agent/drainclamp-log-archive.md` or earlier chunk notes; they exist for
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
- Disjoint chunks retain context when their finished target files total fewer than
  `2 * SMALL_FILE_LINES` (currently 240 lines), counting each path once. Unknown read sizes
  keep the overlap verdict; `--context-high` overrides the small-chunk hold.
- A chunk `PURGE` is a recommendation.

## Failure interaction

A failed implementation or verification emits **no** Gate 5 terminator and does not mark the
milestone `done`. It also cannot retroactively unsay an earlier gate's success sentence — Gate 2
correctly reported what it did at the time.

## Compact advice

At the end of a **completed step**, print one line after the Done / In progress / Next block:
`Compact: safe (state saved)` or `Compact: not yet: <reason>`. A completed step is a chunk done, a
milestone closed, a publish, deploy or release finished, or a hand-off written. Never print it
mid-step, on a question, or on every reply. At a milestone boundary Gate 5's own purge lines
(section 2 and "Chunk boundaries") count as this line: do not print both.

Take the verdict from the script, not from memory:

```
dc_state.py --root R --compact-check
```

It prints `Compact: safe (state saved)` (exit 0) or `Compact: not yet: <first failing check>` (exit 2). Checks, in
order: state file saved at its current generation (no write or lock pending); the last `DC:LOG` entry
or chunk tick (`chunk done`) is newer than the last edit of any changed file outside `.agent/` (`2 edited files not logged`); no
open `dc_review.py` round and no uncommitted Gate 6 fix; no background work you started still
running; no question to the user whose answer is not on disk. Relay the line as printed; never
reword it or claim `safe` without it.

The script cannot see host processes or the conversation, so the last two checks read a small record
you keep in `.agent/drainclamp-compact.json`:

```
dc_state.py --root R --running add NAME     # before starting a background agent or long command
dc_state.py --root R --running done NAME    # when it has finished and its result is saved
dc_state.py --root R --pending-question set    # before asking the user something that blocks work
dc_state.py --root R --pending-question clear  # once the answer is written to state or a file
```

You never run `/compact` yourself and never say you did; the user runs it.

### Idle reminder (opt-in, off by default)

`dc_install.py --compact-reminder [on|off]` adds or removes a Claude Code Notification hook
(matcher `idle_prompt`) in `~/.claude/settings.json` (or `CLAUDE_CONFIG_DIR`). When the user has been
idle and `--compact-check` says safe, it shows "DrainClamp: safe to compact"; it never runs
`/compact`. Install and removal are idempotent and keep every other settings key.

Delay: Claude Code fires `idle_prompt` about 60 seconds after a reply and only if the user has not
typed (Claude Code hooks reference). No setting or variable for that delay is documented, so the
3 minute target cannot be set; the install output states the 60 seconds.
