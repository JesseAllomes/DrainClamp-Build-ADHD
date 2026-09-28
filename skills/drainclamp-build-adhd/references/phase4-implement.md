# Gate 4 — Implement

Always runs. This is the work; everything else exists to make this cheap and recoverable.

## Start

```
dc_project.py next        # <=15 lines: current chunk, its targets, how to read them, last note
dc_project.py time touch  # opens or extends the build-time session
```

The capsule replaces rereading the log or earlier chunk notes. When it says `No chunks yet`, split
the milestone first (Gate 2, "Chunking the active milestone").

## Loop (once per chunk)

1. **Read the chunk's targets only.** Use the read hints the capsule prints. Anything outside the
   targets needs a reason; add it with `chunk add` if it becomes a step of its own.
2. **Reuse before writing.** Index first (`dc_map.py`), then look for the helper that already does
   this. A second implementation of an existing behaviour is a defect the tests will not catch.
3. **Red.** Write or extend the test so it fails for the intended reason. A test that passes before
   the change proves nothing about the change.
4. **Green.** Smallest change that passes it.
5. **Refine.** Mutating formatters belong here, not in Gate 3 — a check-only refiner reports, this
   step rewrites.
6. **Tick it.** `dc_project.py chunk done --milestone <m> --id <c> --note "<what was done>"`.
   This extends build time and prints the next chunk plus a chunk-boundary verdict
   (Gate 5, "Chunk boundaries"). Add `--context-high` when you judge context to be over ~50%.
7. **Log problems where they happen.** `dc_project.py error add --summary ... --milestone <m>
   --chunk <c>` for a defect found, `error fix` when it is fixed, `todo add` for later work.

Chunk notes stay in the sidecar. `DC:LOG` gets **one** line per milestone, at close:
`dc_state.py --append-log "<one sentence>"`.

## Reading

Rules 1 and 3 of `SKILL.md` in one line: index, then read the one range you need. A chunk's
`path::symbol` target is that range.

| Situation | Read |
|---|---|
| file ≤120 lines | the whole file |
| needed span ≥50% of the file | the whole file |
| known target under 50% | one contiguous range (`dc_chunk.py --symbol`, or a plain ranged read) |
| target still unknown after indexing | the whole file, then narrow |

Never split one known range into several micro-chunks: each chunk is another turn, and every turn
re-sends the whole context. `dc_chunk.py` refuses an ambiguous symbol rather than guessing — supply
`--path` or `--match`, because confidently reading the wrong `handle()` is worse than an error.

## Verification tiers

```
dc_verify.py --tier fast        # during the loop: diff-scoped tests + check-only refiners
dc_verify.py --tier milestone   # at milestone close: the DC:VERIFY list
dc_verify.py --tier final       # full suite and full lint
```

The changed-file set is staged + unstaged + renamed + untracked, respecting ignore rules;
`--base REF` makes it branch-relative. When no usable change set exists, configured checks run
unscoped rather than an empty set being reported as a pass.

| Verdict | Meaning | Exit |
|---|---|---|
| `PASS` | ran and passed under its own verdict rule | 0 |
| `FAIL` | ran and failed | 2 |
| `MISSING-REQUIRED` | configured runner is not installed | 3 |
| `UNSAFE-COMMAND` / `APPROVAL-REQUIRED` | nothing was executed | 4 |
| `TIMEOUT` | killed with its process tree | 5 |
| `NO-CHECKS-RUN` | nothing ran — never green | 6 |
| `NO-STATE` | no state, no roadmap row, or no decisions; nothing executed — never green | 6 |

## Reporting a failure

Keep the first failing assertion plus first-party frames. Drop library frames, full logs and
unchanged diff context — the full log is already at `.agent/verify.log`.

A failure here emits **no** Gate 5 terminator and does not mark the milestone `done`. It also cannot
retroactively unsay Gate 2's success sentence: that gate reported what it did at the time.
