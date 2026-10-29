# Gate P — Push

Nothing pushed to a remote may carry PII or a secret. Local files may; what leaves the machine
must not. Gate P runs before every `git push`, compulsory, on every host.

## Two layers

1. **Machine-wide hook.** `dc_pushgate.py install` (once per machine) points
   `git config --global core.hooksPath` at DrainClamp's dispatchers, so every push from any tool
   (Claude, Codex, Grok, an editor, the user's terminal) runs `dc_pii.py prepush` first. The
   repository's own hooks still run after it. Pushes are refused when the scanner finds anything,
   is missing, or crashes.
2. **The gate.** Before you push, run:

```
dc_pushgate.py --root <repo> check
```

It scans what pushing the current branch would publish (every unpushed commit: added lines,
commit messages, added file names, Office text; unreadable binaries refused), then prints the
hook status and any pii-redact drift. Push only on exit 0 and `PUSH GATE: active.`

## On a refusal

The report names commit, file, line and kind. It never prints the value, and neither do you: do
not open the flagged line into the conversation to look. Tell the user what was found (file and
kind) and stop. They decide:

- **It is PII.** Remove it. If it is in an earlier commit, the commit must be rewritten before
  the push (amend, or an interactive rebase the user runs); deleting it in a new commit does not
  help, because the old commit is still published.
- **It is not PII.** `dc_pii.py allow --at <rev>:<file>:<line> --kind <KIND> --reason "..."`
  writes a hashed rule to `.drainclamp-pii-allow`; commit that file with the change. Binary path
  rules (`path assets/*.png  icons, no people`) are added by hand.
- **The user's own name or address.** `pii_allow` in `~/.drainclamp/config.json`, never the repo.

## Never

- `git push --no-verify`, unsetting or overriding `core.hooksPath`, or pushing through another
  tool to get around a refusal. A refusal is the gate working.
- Printing, echoing or quoting a flagged value.
- Allowing a finding the user has not confirmed is not PII.

## Status lines

- `PUSH GATE: not installed` — ask the user to approve `dc_pushgate.py install`.
- `PUSH GATE: INACTIVE for this repository` — the repo has its own `core.hooksPath` (husky and
  similar). The gate's `check` still covers your push; tell the user their manual pushes from
  this repo are not covered.
- `installed scanner differs` — rerun `dc_pushgate.py install` (after a plugin update).
- `WARN: pii-redact ... changed since the port` — the push may go ahead; tell the user the
  detectors need porting again (`dc_pii.py drift`).
