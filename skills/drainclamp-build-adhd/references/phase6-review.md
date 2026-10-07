# Gate 6 — Adversarial review (optional)

Independent agents attack the change; you adjudicate; the user decides; a fixer applies exactly
what was decided. `dc_review.py` does every step that needs no model: packet, citation check,
dedupe, suppression, agent cap, fixer scope, verdict. Run it, never re-implement it.

## When

| `dc_review.py config` mode | Runs |
|---|---|
| `off` (default) | only when the user asks |
| `milestone` | at every milestone close: after Gate 3's milestone tier passes, before Gate 5 |
| `final` | once, before the last milestone closes; it reviews the whole change |

The mode is enforced, not advisory: `dc_state.py --set ROADMAP` refuses to mark a row `done` while
findings block it (exit 2), when no finished round covers it (`NOT-RUN`, exit 6), or when its files
changed after the review (`STALE`, exit 5). Turning review on is the user's call:
`dc_review.py config --mode milestone`.

## Roster and models

| Role | Agent | Default model | Reads | Writes |
|---|---|---|---|---|
| orchestrator + adjudicator | you (main session) | `models.adjudicator` | tables, never the packet | sidecar via the script |
| `critic-a` correctness, `critic-b` safety | `dca-critic` | sonnet, effort high | packet + one range per lead | nothing |
| `checker` goal and test coverage | `dca-checker` | haiku, effort medium | packet + tests | nothing |
| `refuter`, `recheck` | `dca-refuter` | sonnet, effort high | candidates file + cited ranges | nothing |
| `fixer` | `dca-fixer` | sonnet, effort medium | tickets file + ticket ranges | ticket files only |

- **Adjudicator check, once per round.** State your model and effort in one line. If it is not
  `models.adjudicator` from the config, ask once: switch (`/model`, `/effort`) or continue with the
  verdict tagged `ADJUDICATOR: <model>`.
- Agent names: a plugin install lists `drainclamp-build-adhd:dca-critic`, a copied install
  `dca-critic`. Use whichever the host lists. Pass the role's model as the Agent tool's `model`.
- **Cap.** At most `max_agents` (≤6) runs per round. Register every run *before* spawning it:
  `dc_review.py run --round R<n> --role <role> --model <model>`. The script refuses the run over
  the cap, and that refusal is the cap.
- **No subagents on this host** (see `platform-adapters.md`): play each role yourself, in order,
  reading only what that role would read, and register it with `--model inline`. The round is then
  `COVERAGE: partial (inline, not independent)`. Never simulate agents with worktrees or nested CLIs.

## One round

1. **Gate 3 first.** `dc_verify.py --tier milestone` must pass on this tree. Tests are cheaper than
   reviewers; `packet` refuses with `GATE3-RED` otherwise.
2. **Packet.** `dc_review.py packet [--milestone m] [--depth quick|milestone|final]`. Do not read
   the packet yourself; the agents do. `quick` = critic-a + refuter, diff only. `milestone` and
   `final` = both critics + checker + refuter, with one-hop call sites.
3. **Finders, in parallel, in one message.** Register each run, then spawn. The prompt stays this
   short, because the agent definition carries the method and a stable prompt caches:
   `Gate 6 review. Packet: .agent/review/R1/packet.md. Lens: A (correctness). Output JSON lines only.`
4. **Ingest each result verbatim:** `dc_review.py ingest --round R1 --role critic-a --file -`
   (stdin heredoc). Never edit, merge or drop findings by hand. The script rejects fake citations,
   merges duplicates and suppresses what the user already waived or dismissed.
5. **Refute.** No candidates: skip to step 13. Otherwise register `refuter` and spawn it with
   `Refute mode. Packet: <path>. Candidates: .agent/review/R1/candidates.jsonl.`, then
   `dc_review.py adjudicate --round R1 --file -` with its output.
6. **Adjudicate.** You may overrule the refuter (`adjudicate --set r2=open --note "<why>"`), but only
   after reading the cited range yourself, and the note must say what you read.
7. **Consult the user. One table, one question.** Print `dc_review.py status` and add your
   recommendation per row:

   ```
   r1 [critical] open  src/a.py:42  loop drops the last element         -> fix
   r4 [low]      open  src/b.py:9   caller hard-codes input (vs D2)      -> dismiss?
   Reply per id: fix | waive: <reason> | dismiss: <reason>
   ```

   `vs D<n>` means the finding touches recorded decision `D<n>`: either the code breaks it, or the
   finding argues against it. Say which, and ask whether the decision still stands; do not assume. Waive = real, but accepted. Dismiss = not a defect. Only the user decides
   either; a subagent never does.
8. **Record the reply as given:** `dc_review.py decide --set r1=fix --set "r4=dismiss:<reason>"`.
9. **Tickets.** For each `fix`, write the ticket and store it:
   `dc_review.py ticket --round R1 --id r1 --allow "src/a.py;tests/test_a.py" --file -`.
   `--allow` is exactly the ticket's `FILES`.

   ```
   TICKET r1  (severity critical)
   FILES:     src/a.py lines 40-48 (parse); tests/test_a.py (append only)
   DEFECT:    range(len(items) - 1) skips the last item
   CHANGE:    1. line 42: range(len(items) - 1) -> range(len(items))
   TEST:      test_parse_sums_all_items: parse([1, 2, 3]) == 6; fails before CHANGE
   DO NOT:    touch other files, rename, refactor, reformat, change signatures
   DONE WHEN: parse sums every item and the new test passes
   IF BLOCKED: stop, leave the ticket's files untouched, return "BLOCKED r1: <reason>"
   ```

   A ticket the fixer cannot misread: exact lines, exact edits, the test, the limits.
10. **Fix.** Register `fixer`, spawn once for every ticket:
    `Tickets: .agent/review/R1/tickets.md. Apply r3 r1.`
    List the ids bottom-of-file first, so each ticket's line numbers are still true when it is
    applied. Two tickets editing the same lines belong in one ticket.
11. **Scope, then tests.** `dc_review.py scope --round R1`. `SCOPE-BREACH` (exit 4): stop and show the
    user the stray files. Never revert them yourself, because they may hold the user's own work. Then
    `dc_verify.py --tier fast`.
12. **Re-check.** Register `recheck`, spawn `dca-refuter` with
    `Re-check mode. Tickets: .agent/review/R1/tickets.md. Ids: r1 r3.` Then for each id:
    `RESOLVED` → `dc_review.py resolve --id r1 --fixed --note "<what changed>"`;
    `NOT-RESOLVED` / `REGRESSION` → `resolve --id r1 --reopen --note "<why>"`, back to step 7.
13. **Finish.** `dc_review.py finish --round R1` stamps the reviewed files and prints the verdict.
    `REVIEW-PASS` → close the milestone (Gate 4 log line, roadmap `done`, Gate 5).
    `FINDINGS n` → the milestone stays open, and `dc_project.py next` shows it as waiting on the user.

**Cap reached mid-round** (the fix loop needs a 7th run): finish what can be finished, then say
`Round cap reached. Reply go for a fix-diff round.` On go:
`dc_review.py packet --only "<fixed files>"` starts a small round over just those files.

## Output contract

Each step prints one script line; relay only verdicts and the step-7 table. The user sees one
question per round unless a breach or a regression needs a second. After a gap, `dc_review.py
status` plus `dc_project.py next` is the whole resume: everything else is in the sidecar.

## Never

- Never let Gate 6 stand in for Gate 3, or report `NOT-RUN` / `GATE3-RED` as a pass.
- Never write, paraphrase or soften a finding on a subagent's behalf (inline mode is labelled).
- Never mark a finding fixed without a passing `scope` and a `RESOLVED` re-check.
- Never decide waive or dismiss for the user, and never take one without the user's reason.
- Never spawn a run you did not register, or exceed the cap by any route.
