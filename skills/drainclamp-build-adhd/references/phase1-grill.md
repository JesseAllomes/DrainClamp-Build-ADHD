# Gate 1 — Grill

## The rule

Ask only when **two plausible answers would change** public behaviour, data shape, a security
boundary, a dependency, a migration, or a destructive action.

Otherwise: follow the repository's established convention, record the choice in `DC:DECISIONS`, and
move on. A question that costs a turn and changes nothing is pure loss.

When nothing qualifies, write the single line `no open questions` into `DC:DECISIONS` and continue.
Skipping Gate 1 is the normal case for a well-specified task.

## Examples

- **Ask**: must this existing API stay backward compatible? Two answers, two different
  implementations, and one of them breaks callers.
- **Ask**: may stored data be migrated destructively? The answer changes the data and the rollback
  story.
- **Do not ask**: which naming or test-layout convention to follow. Inspect the repository and reuse
  what is there.

## Form

At most **2–3 questions**, in one message, each answerable in a sentence. Keep the whole message
under 80 words. No preamble, no restating the task back.

Ask before implementing, not during — a question mid-diff has already cost the work it was meant to
prevent.

## Recording

Every material decision goes into `DC:DECISIONS`, one per line, in the form
`<question> -> <answer> (<source: user | repo convention>)`.

This section is what makes a context purge recoverable: after a reset, the decisions survive even
though the conversation does not. A decision not written down will be re-litigated.
