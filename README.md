# DrainClamp & Build

A token-minimal architect and auditor skill for **Claude Code**, **Codex**, and **Grok**.

A session gate plus seven conditional gates over one repository change (the seventh an optional adversarial review), with durable state on disk so the work survives a
context reset. Stdlib-only Python 3. No third-party dependencies.

> **Status: feature complete.** Every gate has a script and a reference, `dc_install.py` installs the
> skill without ever clobbering what it does not own, and the read-only `dca-scout` subagent plus the
> Gate 6 review roster ship for hosts that expose subagents.

## Why

Token spend is **resident context × turns remaining**, discounted by cache hit rate — not bytes read
once. Three consequences drive the whole design:

| Naive rule | Why it backfires | What this does instead |
|---|---|---|
| Purge after every task | `/clear` and `/compact` destroy the cached prefix; every later task reloads state at full price | Purge only at a real milestone boundary with known overlap < 0.20 |
| Hard line cap on every read | A 300-line file in 8 chunks is 8 full context re-sends | Index first, then one contiguous range; full read when the file is small or mostly needed |
| Ban full-file reads | Blind spots, then re-reads | `dc_map.py` turns a large file into a symbol index, then you read the one range you need |

## The gates

| # | Gate | Runs when | Skipped when |
|---|---|---|---|
| S | Session | every invocation, before Gate 0 | never |
| 0 | Audit | `.agent/audit.json` fails the freshness test | cache is fresh |
| 1 | Grill | a choice would materially change implementation | fully specified |
| 2 | Externalise | state missing, stale, or roadmap changed | valid state exists |
| 3 | Select checks | verification config changed, or `DC:VERIFY` empty | fingerprint unchanged |
| 4 | Implement | always | — |
| 5 | Reset | at a real milestone boundary | mid-milestone |
| 6 | Review | review mode on, at milestone close (after 3, before 5), or on request | mode `off` (the default) |

Each gate loads only its own reference file. The skill body stays small; the detail is paged in on
demand.

Gate S exists because durable state is portable between agents, and that cuts both ways. A session
opened in a repository another agent had been working in used to resume it silently — the state was
found, the pipeline continued, and nothing said whose work it was. Gate S turns that into a
question:

```
DRAINCLAMP: what are you working on now?
  (1) - lightyear-qbo (last worked 2026-08-11, 4/7 done)
  (2) - drainclamp-build-adhd (last worked 2026-08-13, 6/6 done)
  (3) - a new project
  (4) - mark a project complete
  (5) - reopen a completed project
Reply with a number. No gate runs until you do.
```

The menu is printed by `dc_session.py`, not described in `SKILL.md`, so the always-loaded skill body
pays about 55 tokens for the gate and nothing for its output. It caps at ten projects and says
`SHOWING n/N`; `--all` lifts the cap, and `--forget` drops an index entry without touching the
repository it points at. Action numbers continue past the full count, never the shown count, so a
capped menu cannot offer "a new project" under a number the cap hid.

## Design commitments

These are the parts worth stealing even if you never install it.

**Never claim an event that did not happen.** No CLI exposes `/clear` or `/compact` to the model, so
Gate 5 *recommends* a purge and prints the resume instruction. Only a genuinely fresh session may
say a reset occurred. Nothing ever emits a `[SYSTEM: …]` prefix — that impersonates the host.

**Missing data never defaults to PURGE.** `overlap = |intersection(completed, next)| / |next|`. No
completed milestone means the overlap is *undefined*, not zero — otherwise every project's first
milestone would render a confident `0%` and recommend throwing away context.

**The repository is not an authority.** `DC:VERIFY` is command text stored in a repo file. Schema v1
stores no trust or verdict field, because a stored value is forgeable; both are derived in code.
Unknown keys are rejected rather than ignored. A command outside the runner allowlist is never
executed by the verifier: it prints an `APPROVAL-REQUIRED` record with a digest, the agent runs it
through the host's own approval path where the user can see it, and the outcome is recorded against
that digest — so editing the entry invalidates its approval.

**Exit 0 is not universally a pass.** `gofmt -l` exits 0 while listing the files it would rewrite,
so each allowlisted check declares how its verdict is read. A tier where nothing actually ran
reports `NO-CHECKS-RUN`, never green.

**A cache is only worth having if it knows when it is stale.** The audit re-runs unless all five
hold: schema and root, HEAD, the content fingerprint of dirty and untracked files, the
verification-config fingerprint, and the TTL. HEAD plus a TTL would sail through an entire working
session of uncommitted edits.

**Caps never masquerade as results.** Truncated output carries `SHOWING n/N`, `TRUNCATED n/N`, or
`COVERAGE: partial`. The symbol index is explicitly advisory — never proof a symbol does not exist.

**Ambiguity is refused, not guessed.** `dc_chunk.py --symbol handle` in a repo with two `handle`
functions lists both and exits non-zero. Confidently reading the wrong implementation is the failure
it exists to prevent.

**Instruction surfaces are never touched.** `AGENTS.md`, `CLAUDE.md`, `.gitignore` and
`.git/info/exclude` are only ever written on an explicit request. `.agent/` is created freely; if it
is unignored you get one warning and nothing else.

## Scripts

| Script | Does |
|---|---|
| `_dcio.py` | Atomic writes, PID-aware locking, generation checks, subprocess sandbox, output normalisation |
| `dc_map.py` | Symbol index: Python via stdlib `ast`, JavaScript via a bounded regex scanner; cached on `path + mtime_ns + size` |
| `dc_chunk.py` | Range reader; refuses ambiguous symbols; advises on read sizing |
| `dc_state.py` | Schema v1 state, crash-recoverable log rollover, purge calculus, narrow promotion |
| `dc_registry.py` | Cross-repository project index at `~/.drainclamp/projects.json`; prunes on read, never trusts a corrupt file |
| `dc_session.py` | Gate S: the project menu, plus `--complete` and `--reopen` |
| `dc_project.py` | Project sidecar `.agent/drainclamp-project.json`: chunks with targets, errors, to-dos, time, tokens; `next` resume capsule; `create` / `complete` / `reopen` from a charter |
| `dc_tokens.py` | On-demand report: none / base / adhd across Claude, Codex and Grok; `--project` attributes Claude tokens to chunks and milestones. Aggregates only, never transcript text |
| `dc_audit.py` | Gate 0: five-part freshness test, depth-2 rollups, stack, dead-code candidates |
| `dc_review.py` | Gate 6: review packet, citation check, dedupe, suppression, agent cap, fixer scope, roadmap guard |
| `dc_verify.py` | Tiered verification, runner allowlist, approval handoff with digests tied to a tree fingerprint |
| `dc_selftest.py` | Materialises `tests/fixtures/`, runs the whole suite matrix |
| `dc_install.py` | Links the skill into each host, installs `dca-scout` and the Gate 6 roster, refuses to touch anything it does not own |

### Measuring the overhead

No host exposes live context usage to a model — that is why Gate 5's `--context-high` is an
assertion rather than a measurement. Local transcripts are the one place the real numbers exist, so
`dc_tokens.py` reads them after the fact:

```
py -3 -B skills/drainclamp-build-adhd/scripts/dc_tokens.py
```

It reports three exclusive buckets (`none`, `base`, `adhd`) across Claude, Codex and Grok. Claude
uses session-median context/fresh values; Codex uses the final session token total; Grok remains
session-level and never invents a cache split. Thin samples are marked `n<3`.

Use `--claude`, `--codex`, or `--grok` to isolate a host; `--transcripts` aliases `--claude`.

`--project <root>` attributes Claude tokens to that project's chunks and milestones. A call belongs to
a chunk when it lands after the previous chunk was ticked and by the time this one was, clipped to the
start of the tracked time session; calls in a session but between chunks are reported as outside.
`--save` stores the totals in the sidecar, where project-board can show them. Claude only (Codex and Grok transcripts carry
no per-call timestamps), and untracked time is not counted, so totals are a floor. One API call is
streamed as several transcript records; they are counted once, by message and request id.

The static table estimates resident `SKILL.md` and paged references (bytes/4; not a host bill).

The comparison is observational, not controlled, and is never a benchmark. The script is not wired into any gate and is not referenced from `SKILL.md`: a measurement tool that costs context on every invocation would be measuring a problem it had joined.

### The project sidecar

State schema v1 is frozen, so what the workflow tracks beyond the roadmap lives in
`.agent/drainclamp-project.json`: chunks under each milestone (each naming the files it touches, so
Gate 4 reads only those), errors, to-dos, build time and tokens. Every write bumps `rev`; a writer
holding an old revision is refused, never merged. `dc_project.py next` prints a dozen-line resume
capsule instead of the whole state file, and ticking a chunk runs a chunk-boundary purge check.

Dashboards are not DrainClamp's job. project-board is a separate tool that reads
DrainClamp's project list and these files (never writing them) and adds status, % complete, value and
your time across every project, DrainClamp or not. When it creates, completes or reopens a DrainClamp
project it calls `dc_project.py create` / `complete` / `reopen`, the same commands you can run yourself.

### Gate 6: adversarial review

Optional, off by default (`dc_review.py config --mode milestone|final`). With it on, a milestone
cannot be marked `done` until a review round clears it. Per round, at most six subagent runs:

| Role | Agent | Model | Job |
|---|---|---|---|
| critic A, critic B | `dca-critic` | sonnet, high | correctness lens; safety lens |
| checker | `dca-checker` | haiku, medium | goal met, every new branch tested |
| refuter, re-check | `dca-refuter` | sonnet, high | disprove each finding; confirm each fix |
| fixer | `dca-fixer` | sonnet, medium | apply the user-approved ticket exactly, nothing else |
| advisor | `dca-advisor` | opus, high | after a pass: up to 5 grounded improvements against the build's purpose |

The main session (Opus, high effort, by default) orchestrates and adjudicates, and the user makes
every fix / waive / dismiss call. Accuracy comes from independence: two lenses, then a refuter that
defaults to `REFUTED`. Cost stays low because:

- Gate 3 must be green first.
- The packet is built by a script, so agents read one file rather than the conversation.
- Each finding's quoted evidence is checked against the file before any model reads it.
- Duplicates merge, and waived or dismissed findings never come back.
- The fixer's file scope is checked by hash, not trust.

Findings live in the sidecar under `review`, which project-board shows as an "Adversarial review"
section.

The advisor is a separate, non-blocking step. After a review passes (by default only the final one),
one fresh Opus agent weighs the build against its stated purpose and proposes up to five
improvements. Each must cite the purpose line it serves and evidence the script can check. You
accept each one (a small one becomes a to-do, a bigger one a roadmap row), deny it (with a reason,
and it never comes back) or shelve it (it comes back next time). The board lists them under
"Improvement ideas".

Use the real `dca-*` agent types, not a general-purpose stand-in. Their short tool lists cut each
run's fixed overhead: a stand-in measured about 45k tokens before reading anything.

### The sandbox

Never a shell. argv arrays only, `cwd` resolved and required to sit at or beneath the repo root,
per-command timeout with **process-tree** termination, UTF-8 decoded with replacement, byte-capped,
ANSI and control characters stripped, line lengths capped — every cap explicitly marked.

Two different argv checks, because they solve different problems. `check_argv` guards our own calls
and only constrains the executable: without a shell, `python -c "import sys; sys.exit(3)"` is
perfectly safe and blocking it would buy nothing. `check_repo_argv` guards entries read out of a
repository file, where a metacharacter means the author expected shell parsing they will never get,
so the command would run as something other than it reads as.

Neither is the security boundary. Safety comes from never using a shell and from the runner
allowlist — `python -c` can do anything at all without a single metacharacter in sight.

### Crash-recoverable log rollover

`DC:LOG` caps at 20 entries; the rest move to `.agent/drainclamp-log-archive.md`. Two files cannot
be updated atomically together, so the order is chosen so a crash **duplicates rather than loses**:
archive first and fsync, then replace the state file. A crash in between leaves entries in both, and
the next mutation reconciles them by id. There is no ordering that loses an entry.

### Windows note

`os.replace()` fails with `PermissionError` while *any* process holds the destination open —
including read-only holders like OneDrive, an editor, or the search indexer. `atomic_write` retries
briefly, then fails loudly. A partial file is never written.

## Install

### Claude Code plugin (recommended)

The repository is its own marketplace. Two commands, no clone:

```
/plugin marketplace add JesseAllomes/DrainClamp-Build-ADHD
/plugin install drainclamp-build-adhd@drainclamp-adhd
```

That installs the skill and the `dca-*` subagents together, and `/plugin update drainclamp-build-adhd`
pulls later versions. Claude Code copies the plugin into its own cache, so the install does not
track a checkout.

Use this **or** `dc_install.py` for Claude Code, not both — two definitions named
`drainclamp-build-adhd` is one too many. `dc_install.py --uninstall --host claude` removes the link
install if you are switching.

### Grok plugin

Grok reads `.grok-plugin/marketplace.json`, not Claude's `"source": "./"` catalog. From
PowerShell, no clone:

```powershell
grok plugin marketplace add JesseAllomes/DrainClamp-Build-ADHD
grok plugin install drainclamp-build-adhd --trust
grok plugin enable drainclamp-build-adhd
```

If the source is already added, refresh it first with
`grok plugin marketplace update DrainClamp-Build-ADHD`. Grok pins as
`drainclamp-build-adhd@DrainClamp-Build-ADHD` or
`drainclamp-build-adhd@jesseallomes/drainclamp-build-adhd` — not `@drainclamp-adhd`
(that qualifier is Claude Code's marketplace id).

Use this **or** `dc_install.py` for Grok, not both. `dc_install.py --uninstall --host agents`
removes the skill-link install if you are switching. Reload plugins (`r` in the Plugins
tab) or start a new session after install. The plugin brings the `dca-*` subagents too. To run
each Gate 6 role on its own Grok model (`dc_review.py config` shows the roster), pin them once from
the plugin's copy; this adds one marked block to `~/.grok/config.toml` and makes no skill link:

```powershell
py -3 -B (Get-ChildItem "$HOME\.grok\installed-plugins\drainclamp-build-adhd-*\skills\drainclamp-build-adhd\scripts\dc_install.py").FullName --host agents --agents-only
```

Re-run it after `grok plugin update` if the roster changed.

### Codex plugin

Codex reads the Claude catalog straight from git:

```powershell
codex plugin marketplace add https://github.com/JesseAllomes/DrainClamp-Build-ADHD.git
codex plugin add drainclamp-build-adhd@drainclamp-adhd
```

Codex plugins cannot ship subagents, so install Gate 6's roster once from the plugin's own copy.
It writes `~/.codex/agents/dca-*.toml`, each with its role's Codex model (`dc_review.py config`
shows the roster; `--project <repo>` uses that repo's override), and makes no skill link:

```powershell
py -3 -B "$HOME\.codex\plugins\cache\drainclamp-adhd\drainclamp-build-adhd\<version>\skills\drainclamp-build-adhd\scripts\dc_install.py" --host codex --agents-only
```

Update with `codex plugin marketplace upgrade drainclamp-adhd`, then re-run that line for the new
`<version>`: the agent files are copies, so they refresh only when the installer runs.

### Direct install (Claude Code, Codex, Grok)

Clone, then:

```powershell
py -3 -B skills/drainclamp-build-adhd/scripts/dc_install.py            # every host present
py -3 -B skills/drainclamp-build-adhd/scripts/dc_install.py --check    # what is installed, and is it healthy
py -3 -B skills/drainclamp-build-adhd/scripts/dc_install.py --uninstall
```

A link, so the install tracks the checkout: a junction on Windows (no admin needed), a symlink
elsewhere. Claude Code discovers skills through `~/.claude/skills/`, Grok through
`~/.agents/skills/`, and Codex reads `~/.codex/skills/`. A host with no directory of its own is
skipped rather than created — pass `--host codex` to install where the host is not set up yet.
Invoke with `/drainclamp-build-adhd` (Claude, Grok) or `$drainclamp-build-adhd` (Codex).

Claude Code reads only `~/.claude/skills/`, and a skill it cannot see fails silently: the install
reports success and the slash command simply never appears. `--check` is what catches that, so run
it after installing.

Where the host reads agent definitions — `~/.claude/agents/` for Claude Code — the read-only
`dca-scout` subagent is copied in alongside. `--no-agent` skips it.

The scout pins `model: haiku` rather than inheriting the parent's. It has three read-only tools and
a 50-line report cap, so the expensive judgement stays with the parent and the sweep does not pay
parent-model rates. Delegating a sweep is only a saving if the delegate is cheaper.

`--copy` installs a snapshot instead of a link, for a host that cannot link at all. It is never
chosen automatically: a copy goes stale silently, so re-running the installer is how it updates.

### Ownership

The installer removes or replaces only what it can prove it wrote — a link is ours when it resolves
to this checkout, a copy when the ledger records it and the tree still fingerprints to what we wrote.
Anything else is reported and left alone, and `--force` extends only to a copy we already own. A
directory somebody else put there is never overwritten, and never deleted, whatever flags you pass.

Removal never uses `shutil.rmtree` on a link. On Windows `rmtree` follows a junction and empties the
directory on the far side, which here would be the checkout itself; links are unlinked with `rmdir`
or `unlink`, and the test suite counts the source tree after every destructive path.

## Tests

```powershell
py -3 -B skills/drainclamp-build-adhd/scripts/dc_selftest.py               # the whole matrix
py -3 -B skills/drainclamp-build-adhd/scripts/dc_selftest.py --materialise # fixtures only
```

Or one suite at a time:

```powershell
py -3 -B tests/t_dcio.py       # atomicity, locking, PID liveness
py -3 -B tests/t_sandbox.py    # argv, cwd containment, timeouts, normalisation
py -3 -B tests/t_state.py      # schema v1, purge calculus
py -3 -B tests/t_archive.py    # log rollover, crash recovery, promotion
py -3 -B tests/t_map.py        # ast + regex index, cache, verdicts, paging
py -3 -B tests/t_chunk.py      # ambiguity refusal, read sizing
py -3 -B tests/t_audit.py      # freshness test, rollups, dead-code candidates
py -3 -B tests/t_verify.py     # allowlist, verdict rules, approval handoff, stale records
py -3 -B tests/t_gatestate.py  # verification refuses to run against no plan
py -3 -B tests/t_deps.py       # milestone dependencies pick the next row
py -3 -B tests/t_stale.py      # a returning writer cannot erase what landed
py -3 -B tests/t_session.py    # registry and the Gate S menu
py -3 -B tests/t_project.py    # sidecar: chunks, errors, to-dos, time, capsule
py -3 -B tests/t_lifecycle.py  # create from a charter, complete, reopen (functions and CLI)
py -3 -B tests/t_tokens.py     # token buckets, dedupe, chunk attribution, no text leaks
py -3 -B tests/t_identity.py   # the checkout agrees about its own name
py -3 -B tests/t_structure.py  # the layout install and forks assume
py -3 -B tests/t_install.py    # ownership, refusal, never deleting the checkout
py -3 -B tests/t_selftest.py   # the fixtures themselves
py -3 -B tests/t_review.py     # Gate 6: packet, citations, merge, suppression, cap, scope, guard
py -3 -B tests/t_review_feed.py # the review block project-board reads
py -3 -B tests/t_review_e2e.py  # a live Gate 6 run (real agents) replayed: 3 planted defects, 1 decoy
py -3 -B tests/t_skeleton.py   # one change through the core gates
```

825 checks across 23 suites, no third-party runner. Fixtures are generated, never hand-edited:
`dc_selftest.py --materialise` writes Python, JavaScript, an unsupported extension, malformed
source, paths with spaces, a Unicode filename, CRLF, a directory link, and a git repository with an
untracked file. The git fixture commits with a pinned identity and timestamp, so the same tree hashes
to the same HEAD on every run and every host — materialising twice is asserted to be a no-op. Anything
the platform refuses to create is reported as `SKIPPED` — a missing fixture that looks like a passing
test is the failure that guards against.

## License

MIT.
