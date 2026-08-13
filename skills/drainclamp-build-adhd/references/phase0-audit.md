# Gate 0 — Audit

```
dc_audit.py [root] [--force]
```

Runs when `.agent/audit.json` fails the freshness test. Skipped when the cache is fresh — the script
decides that for you, so run it either way and read the first line.

## Freshness — all five, or it rescans

| # | Condition | Why it is not optional |
|---|---|---|
| 1 | schema and canonical root match | a different schema wrote different fields |
| 2 | HEAD unchanged | commits move the tree |
| 3 | fingerprint of eligible **dirty and untracked** files unchanged | HEAD sits still through a whole working session |
| 4 | **verification-config fingerprint** unchanged | it also decides whether Gate 3 must run |
| 5 | TTL valid (24h) | a long-lived session drifts from a cached picture |

Dirty and untracked files are content-hashed, so a byte-identical revert settles the cache again and
a `touch` alone never invalidates it. Paths are part of the input, so a rename does.

The verification-config fingerprint is SHA-256 over a schema tag plus the sorted
`path + SHA-256(bytes)` of every eligible, non-ignored config file: `package.json`, `pyproject.toml`,
`pytest.ini`, `setup.cfg`, `tox.ini`, `go.mod`, `Cargo.toml`, `ruff.toml`, `.ruff.toml`,
`eslint.config.*`, `.eslintrc*`, `tsconfig*.json`, `jest.config.*`, `vitest.config.*`. Presence is
part of the input, so creating, deleting or renaming one moves the fingerprint. **Lockfiles are never
inputs** — they churn on every install without changing what the repository declares.

All five conditions are evaluated, not short-circuited: a new `pyproject.toml` is both an untracked
file and a config change, and reporting only the first would hide the one that drives Gate 3.

## What it reports

- Totals **rolled up to depth two**, so a file buried five directories down is still counted — under
  its depth-2 ancestor, never dropped for being deep.
- Stack by **manifest presence, direct dependencies only**. No recognised manifest is reported as
  `unknown`, never guessed.
- Dead code as **candidates, unverified**. A name reached through `getattr`, an entry point, a
  plugin registry or a template is invisible to a reference count, so this list is a place to look,
  never a finding.
- Linked directories that were skipped rather than followed.
- `GATE3: required (<reason>)` or `GATE3: skip (fingerprint unchanged)`.

Output is capped at 15 lines; anything trimmed says `SHOWING n/N` or `TRUNCATED n/N`. The full
picture is in `.agent/audit.md`, the machine copy in `.agent/audit.json`.

## Indexing blind spots

`dc_map.py` backs the symbol side of the audit and is **advisory**:

- Python is parsed with `ast`; JavaScript with a bounded regex scanner that is explicitly partial;
  every other extension is `UNSUPPORTED` and must be read directly.
- `UNSUPPORTED` (no grammar) and `PARSE-FAILED` (supported but malformed) are different answers, and
  neither means "this file has no symbols".
- Generated code, vendored trees and `.venv` are skipped, so absence from the index is never
  evidence of absence from the repository.

Record what the audit found in `DC:ARCH` (Gate 2) as `module → responsibility → entrypoint`, plus
any optimisation vectors worth keeping. Do not paste the audit output into the conversation — it is
already on disk.
