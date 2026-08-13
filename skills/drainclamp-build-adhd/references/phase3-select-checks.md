# Gate 3 — Select checks

Runs when the verification-config fingerprint changed, or `DC:VERIFY` is empty. This gate
**registers** checks; it does not run them. Mutating formatters belong to Gate 4.

```
dc_verify.py --discover                     # what this repo actually configures
dc_state.py --set VERIFY --file <json>      # register it
```

`--discover` prints a `DC:VERIFY`-shaped array. Read it, drop what does not belong, add anything the
repository needs that discovery cannot see, then register the result.

## Entry schema v1

```json
{"id": "pytest-unit", "argv": ["pytest", "tests/"], "cwd": ".", "tier": "milestone"}
```

- Required: `id`, `argv` (array, never a string), `cwd` (repo-relative, at or beneath the root),
  `tier` (`fast` | `milestone` | `final`).
- Optional: `timeout_s`.
- **Never stored**: any trust or provenance field, and the pass/fail rule. Both are derived in code.
- **Unknown keys are rejected**, including `source`. Schema drift fails loudly rather than silently.

## Configured, not merely present

A runner is required only when the repository says so. Otherwise it is `UNDISCOVERED` — not
"required but missing".

| Required when | Evidence |
|---|---|
| npm/yarn/pnpm test | `scripts.test`, or jest/vitest/mocha in declared deps |
| pytest | `[tool.pytest.ini_options]`, `pytest.ini`, `setup.cfg [tool:pytest]`, `tox.ini [pytest]`, or a declared pytest dep |
| `go test` / `cargo test` | `go.mod` / `Cargo.toml` present |
| anything else | an explicit `DC:VERIFY` entry |

## The allowlist, and its verdict rules

Auto-discovered commands execute only when they match a known shape. Each shape declares how its
verdict is read, because **exit 0 is not universally a pass**:

| Command | Verdict rule |
|---|---|
| `pytest`, `python -m pytest` | exit code |
| `go test`, `cargo test` | exit code |
| package-manager `test` script | exit code |
| `ruff check` (no `--fix`) | exit code |
| `black --check` | exit code |
| `eslint` (no `--fix`) | exit code |
| `tsc --noEmit` | exit code |
| `gofmt -l` | **non-empty stdout is a failure** — it exits 0 while listing the files it would rewrite |

Refiners registered here are **check-only**. A command that would rewrite the tree (`ruff check
--fix`, `eslint --fix`, `black` without `--check`) is not a check and is not allowlisted.

## Anything else needs the host, not a trust field

The repository is not an authority. `shell=False` prevents shell *parsing*; it does not make a
command safe, because `python -c` needs no metacharacter to do anything at all. So a non-allowlisted
entry is never executed by `dc_verify.py`. It prints one bounded record instead:

```
APPROVAL-REQUIRED  id=<entry id>
  argv:   ["…"]
  cwd:    <repo-relative>
  digest: sha256:<12 chars>
```

Run that command through the **host's own tool and approval path**, where the user sees and permits
it, then record the outcome:

```
dc_verify.py --record <id> --digest <digest> --status pass|fail --log <path>
```

The record is accepted only when the digest matches the entry it was issued for, so an approved
command cannot be swapped for another — editing the entry invalidates its record. An entry that is
neither allowlisted nor recorded **has not run**, and a tier made only of those reports
`NO-CHECKS-RUN`, never green.

The same refusal applies one level up: a tier reports `NO-STATE` and executes nothing
unless Gate 2 left a state file carrying at least one `DC:ROADMAP` row and a non-empty
`DC:DECISIONS`. A file written from the template satisfies neither — it parses, and
records nothing. Checks are registered against a plan; an empty plan is not one.
