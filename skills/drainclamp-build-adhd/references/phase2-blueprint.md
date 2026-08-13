# Gate 2 — Externalise: state schema v1

Durable state lives at `.agent/drainclamp-state.md` in the repository being worked on. It is **not**
an instruction surface: `AGENTS.md` and `CLAUDE.md` are never written automatically.

Schema 1 is frozen. Adding a field requires schema 2 — never a silent extension.

## File header

Line 1 of the file, exactly:

```
<!-- drainclamp-build: state; schema=1; generation=<int> -->
```

`generation` starts at 1 and increments on every committed mutation. `dc_state.py` re-reads and
validates it inside the lock immediately before replacing the file; a mismatch aborts the
transaction rather than overwriting another writer's work.

## Sections

Exactly these five, in this order, each a matched marker pair:

```
<!-- DC:ARCH -->      … <!-- /DC:ARCH -->
<!-- DC:DECISIONS --> … <!-- /DC:DECISIONS -->
<!-- DC:ROADMAP -->   … <!-- /DC:ROADMAP -->
<!-- DC:VERIFY -->    … <!-- /DC:VERIFY -->
<!-- DC:LOG -->       … <!-- /DC:LOG -->
```

Unmatched, duplicated, or out-of-order markers are a **hard parse error**. The file is never
silently repaired — a malformed state file means something else wrote it, and guessing would lose
data.

### DC:ARCH

Free-form condensed architecture: module → responsibility → entrypoint, plus optimisation vectors
found during the audit. No prose paragraphs.

### DC:DECISIONS

Constraints and rulings from Gate 1 — the section that makes a context purge recoverable. One
decision per line. When Gate 1 is skipped, write the single line `no open questions`.

### DC:ROADMAP

A markdown table. Row shape v1:

```
| id | goal | files | status |
```

- `id` — short slug, unique within the file.
- `goal` — one line.
- `files` — semicolon-separated repo-relative paths or patterns. **This is what the purge calculus
  consumes**, so it is required, not decorative.
- `status` — `pending` | `active` | `done`.

Explicitly named repo-relative files count as members even when they do not exist yet; milestones
routinely create files. Only unresolvable directories or globs force `overlap unknown`.

### DC:VERIFY

A JSON array inside a fenced ```json block. Entry v1:

```json
{"id": "pytest-unit", "argv": ["pytest", "tests/"], "cwd": ".", "tier": "milestone"}
```

- Required: `id`, `argv` (array, never a string), `cwd` (repo-relative), `tier`
  (`fast` | `milestone` | `final`).
- Optional: `timeout_s`.
- **Never stored**: any trust or provenance field, and the pass/fail rule. Both are derived in code.
  A value stored in a repository file is forgeable, so believing one would defeat the check.
- **Unknown keys are rejected**, not ignored — including `source`. Schema drift fails loudly.

### DC:LOG

Append-only, one line per completed diff:

```
- <ISO-8601> <id8> <one sentence>
```

`id8` is a stable 8-character unique ID. The active log holds at most **20 entries**; older ones
move to `.agent/drainclamp-log-archive.md`, which is **never auto-loaded into context** and is not
part of the resume protocol. Resume reads only `.agent/drainclamp-state.md`.

Two files cannot be updated atomically together, so the order is chosen so a crash **duplicates
rather than loses**: the archive is appended and fsynced first, then the state file is replaced
without those entries. A crash between the two leaves the entries in both files; the next mutation
notices the overlap by `id8` and drops them from the active log. Archive appends are idempotent by
id, so replaying one changes nothing.

## Promoting constraints

`dc_state.py --promote-to-agents` copies selected `DC:DECISIONS` lines into `AGENTS.md`. It is
deliberately narrow — explicit permission makes the write allowable, it does not make wholesale
copying appropriate:

- With no `--select`, it lists the numbered constraints and writes nothing.
- With `--select` but no `--confirm`, it prints the exact block and writes nothing.
- `ROADMAP`, `VERIFY` and `LOG` are task state, not instructions, and are **never** promoted.
- The block sits between `<!-- DRAINCLAMP:CONSTRAINTS -->` markers, so hand-written content around
  it survives and a re-promote replaces rather than duplicates.
- When several `AGENTS.md` files apply it refuses and names them; choose one with `--dest`.
- `CLAUDE.md` is never created.

## Gate 2 output

On success, emit exactly:

```
Master state and optimizations saved to .agent/drainclamp-state.md. Continuing pipeline.
```

Gate 3 may still run, so this sentence does not claim the code loop has started. On a persistence
failure, report the failure — never this sentence.

## Ordering milestones

`DC:ROADMAP` takes an optional fifth column, semicolon-separated milestone ids:

```
| id | goal | files | status | deps |
| m1 | extract the parser | src/parse.py | done | |
| m2 | reuse it in the loader | src/load.py | pending | m1 |
| m3 | delete the old path | src/legacy.py | pending | m2 |
```

The column is optional and a four-column table means what it always did. What it buys is a
decision at Gate 5. With several rows pending and nothing to order them, the purge calculus
can only report `multiple eligible next milestones` and hold — it cannot tell which milestone's
files to compare against. Dependencies narrow the set to what is startable, and where that
leaves exactly one row, the overlap is computed and the context is released or kept on
evidence rather than withheld for want of an ordering.

A dependency naming a milestone that does not exist, a self-dependency, and a cycle are all
parse errors. None is repaired: a roadmap that silently drops an edge would have the calculus
order work by a graph nobody wrote.

Dependencies do not disambiguate several *active* milestones. Concurrent agents holding more
than one milestone in flight is a real state rather than a missing edge, and holding the
context is the right answer there.
