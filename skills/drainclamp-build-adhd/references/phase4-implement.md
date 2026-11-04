# Gate 4 — Implement

Always runs. This is the work; everything else exists to make this cheap and recoverable.

## Start

```
dc_project.py next        # <=15 lines: current chunk, its targets, how to read them, last note
dc_project.py time touch  # opens or extends the build-time session
```

The capsule replaces rereading the log or earlier chunk notes. When it says `No chunks yet`, split
the milestone first (Gate 2, "Chunking the active milestone").

When the capsule names `Skills:`, load each one once per session with `dc_skills.py show <name>`
(add `--fetch` on the first load of the day) before editing code in its domain, and follow it over
your own reading of that domain. Its other files load with `--file <path>` only when it points to
them. Never load a skill the project has not marked.

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
6. **Tick it.** `dc_project.py chunk done --milestone <m> --id <c> --commit --note "<what was done>"`.
   One call: auto-commit (below), tick, then the next chunk plus a chunk-boundary verdict
   (Gate 5, "Chunk boundaries"). Add `--context-high` when you judge context to be over ~50%.
7. **Log problems where they happen.** `dc_project.py error add --summary ... --milestone <m>
   --chunk <c>` for a defect found, `error fix` when it is fixed, `todo add` for later work.

Chunk notes stay in the sidecar. `DC:LOG` gets **one** line per milestone, at close, with the status flip in one guarded call:
`dc_state.py --close <m> --log "<one sentence>"` (`--mark <m>=active|pending|done` for other flips).

## Auto-commit

Each completed step is committed on its own, so work is never lost and never mixed.

```
dc_project.py chunk start  --milestone <m> --id <c>   # before the first edit: snapshots files already edited
dc_project.py commit --milestone <m> --chunk <c> [--verified] [--paths "x;y"] [--trailer "<line>"]
dc_project.py config [--autocommit on|off] [--clean-tree on|off] [--verify-cmd "<cmd>"] [--review-required on|off]
```

Order at the end of a step: **verify** (the `DC:VERIFY` tier; a passing `dc_verify.py` record for the
current files counts, and `--verified` is the override when the check ran outside `dc_verify.py`),
**scan** (PII/secrets on the exact staged diff, built in), **commit**, **log** the hash, then the compact
line. `chunk done --commit [--verified] [--paths/--trailer/--subject]` does all of it in one call and
prints, in order: `COMMITTED: <hash> <subject> (<n> file(s))` or `NOT COMMITTED: <reason>`, the tick
(the hash is prefixed to `--note`), the boundary lines, then the `Compact:` line, left out when the
boundary already said PURGE or closed the milestone. A refused commit leaves the chunk unticked; fix
the reason and rerun. With auto-commit off it reports that and still ticks. `commit` alone stays for
a milestone with no chunks.

- Committed: files changed since `chunk start` inside the chunk's targets (plus `--paths`). A file
  already edited at `chunk start` is never committed; another session's staged files never ride along.
- Not committed, with a one-line reason: not verified (no record, it failed, or files changed since the check), PII hit (kinds and paths only),
  merge, rebase, cherry-pick or bisect in progress, detached HEAD, locked index, a file over 5 MB or a
  new binary (needs the user's go), opt-out (`--autocommit off`), no `chunk start` snapshot, a step file edited after the PII scan.
- CODEOWNERS (root, `.github/`, `docs/`) or `--review-required on`, on the default branch: the commit
  goes to `dc/<project>-<milestone>` and the output says `PR needed`. If that branch already exists at another commit, nothing is committed: switch to it first.
- `--clean-tree on`: the fast check runs in a temporary worktree at the candidate commit first, so a
  pass cannot depend on an uncommitted file. The command is `--verify-cmd` (approved on this machine when set; a sidecar-only value is refused) or the first fast-tier `DC:VERIFY` entry: an allowlisted runner outside the repo, run in its `cwd`.
- Never pushes, tags, deploys, resets or skips hooks. A hook refusal is reported as is. Undo is `git revert`.

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

## Conductor build

Independent chunks build in parallel on their own worktrees. You plan, brief, merge, review.

1. Before dispatch: `dc_project.py fanout --milestone <m>` (`--no-subagents` on a host without them).
2. Go parallel only on `PARALLEL:`. A `SEQUENTIAL: <reason>` line means build in order, as before.
   Chunks under `then in order after the merge:` wait and are built in order once the batch is merged.
3. At most 4 builders. Brief each with `dc_project.py brief --milestone <m> --chunk <c>`.
4. Use host subagents with worktree isolation. A repo outside the host folder: make
   `<repo>/.worktrees/<project>-<chunk>` by hand and remove it after merge.
5. Builder commits stay local on `dc/*`; the push gate refuses them. Over budget: stop, report.
6. Merge in `dc_project.py merge-order` order. Run the full `DC:VERIFY` once on the merged
   result, then one Gate 6 review of the combined diff.
7. Fix tickets go back to the same builder. After merge, remove the worktrees and `dc/*` branches.
8. No subagents: `SEQUENTIAL: host has no subagents`; build in order.
9. The Workflow tool only when the user explicitly asks.
