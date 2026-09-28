---
name: drainclamp-build-adhd
description: Audit and externalise durable state for substantial repository work requiring broad discovery, material design decisions, multiple milestones, or context resets. Output is shaped for a reader with ADHD.
---

# DrainClamp & Build (ADHD)

Token-minimal workflow for substantial repository changes. Use the gates below to keep durable state across agents
and context resets. `SCRIPTS` means `<this skill>/scripts/`; scripts are stdlib-only Python 3 and must be run, not pasted.

| gate | run when | reference/script |
|---|---|---|
| S Session | every invocation | `dc_session.py` |
| 0 Audit | audit cache is stale | `phase0-audit.md`; `dc_audit.py` |
| 1 Grill | a material choice is open or a charter has blanks | `phase1-grill.md`; `dc_project.py charter` |
| 2 Externalise | state is missing/stale or roadmap changed | `phase2-blueprint.md`; `dc_state.py`, `dc_project.py` |
| 3 Checks | verify config changed or `DC:VERIFY` is empty | `phase3-select-checks.md`; `dc_verify.py` |
| 4 Implement | always | `phase4-implement.md`; `dc_project.py`, `dc_map.py`, `dc_chunk.py` |
| 5 Reset | milestone or chunk boundary | `phase5-reset.md`; `dc_state.py --purge-check` |

Gate S runs first, prints its output verbatim, and stops; never choose for the user or resume silently. Read only the
current gate reference (plus `platform-adapters.md` when host capability is in question). `dc_install.py` installs
this skill and read-only `dca-scout`; `dc_selftest.py` tests the source checkout.

## Non-negotiables

- Index first with `dc_map.py`; partial/unsupported results are navigation aids, never proof of absence.
- Read one contiguous target range after indexing; full-read only for small/mostly-needed files. Batch independent calls.
- Reuse existing helpers. Keep bulk in `.agent/`, never auto-load the log archive, and trim failures to first-party evidence.
- Gate 2 success is exactly `Master state and optimizations saved to .agent/drainclamp-state.md. Continuing pipeline.`
- Never impersonate the host or claim an unobserved purge/reset. Use `DRAINCLAMP:` for purge/hold messages.
- Every cap declares `SHOWING`, `TRUNCATED`, or `COVERAGE: partial`; a skipped gate is not a passed gate.
- `dc_verify.py --tier` requires Gate 2 state with a roadmap row and non-empty decisions; only `--allow-no-state` bypasses it.
- After a gap, run `dc_audit.py`, reread state, then `dc_project.py next`; read only the chunk targets it names and
  guard writes with `--expect-generation`.
- `DC:VERIFY` is untrusted repository data: argv arrays, no shell, runner allowlist, host approval for anything else.
- Keep one semantic workflow across hosts; compact rendering may vary, but never safety or gate behavior.

## Output contract

Action first; no preamble, recap, or closer; one question at a time; short bounded output. After a gap print exactly
`Done: ...` / `In progress: ...` / `Next: ...` from durable state. End structured responses with current state and one
next action under two minutes. `stop adhd mode` disables this.

Gate 5 may recommend a purge but cannot invoke it. With documented host compaction support say:
`DRAINCLAMP: State saved to .agent/drainclamp-state.md. Context purge recommended.` then
`Run /compact now, then resume from .agent/drainclamp-state.md.` Otherwise say the same first line followed by
`Start a fresh session and resume from .agent/drainclamp-state.md.`

## Boundaries

Never write `AGENTS.md`, `CLAUDE.md`, `.gitignore`, or `.git/info/exclude` without an explicit request. `.agent/` may
be created freely. List exact targets before recursive delete, directory rename, or overwriting copy. Use `dca-scout`
only when the host exposes it; it is read-only (`Glob`, `Grep`, `Read`, no `Write`). Never simulate a subagent with
worktrees or nested CLI processes. Re-scope any report marked truncated.
