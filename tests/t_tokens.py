"""Aggregation and leak checks for the three-host, three-bucket token report."""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "drainclamp-build-adhd" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import dc_tokens  # noqa: E402

fails = []


def check(name, cond, detail=""):
    detail = str(detail) if detail else ""
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  ' + detail if detail else ''}")
    if not cond:
        fails.append(name)


def run_cli(*args):
    return subprocess.run(
        [sys.executable, "-B", str(SCRIPTS / "dc_tokens.py"), *args],
        capture_output=True, text=True)


tmp = Path(tempfile.mkdtemp(prefix="dctokens-"))
claude = tmp / "claude"
codex = tmp / "codex"
grok = tmp / "grok"
skills = tmp / "skills"
SECRET = "correct-horse-battery-staple"

(claude / "proj-a").mkdir(parents=True)
(codex / "2026" / "08").mkdir(parents=True)
(grok / "ws" / "sess-adhd").mkdir(parents=True)
(grok / "ws" / "sess-none").mkdir(parents=True)
(grok / "ws" / "sess-adhd" / "subagents" / "child").mkdir(parents=True)
(skills / "base" / "references").mkdir(parents=True)
(skills / "adhd" / "references").mkdir(parents=True)


def claude_turn(inp, created, read, out):
    return json.dumps({"type": "assistant", "message": {"usage": {
        "input_tokens": inp, "cache_creation_input_tokens": created,
        "cache_read_input_tokens": read, "output_tokens": out}}})


def write(path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# Claude: ADHD session carrying a secret that must never reach the report.
write(claude / "proj-a" / "adhd.jsonl", [
    json.dumps({"type": "user", "message": {"content": f"run drainclamp-build-adhd on {SECRET}"}}),
    claude_turn(10, 100, 1000, 50),
    claude_turn(10, 100, 1000, 50),
])
# Claude: base plugin, twice the context per turn.
write(claude / "proj-a" / "base.jsonl", [
    json.dumps({"type": "user", "message": {"content": "invoke /drainclamp-build"}}),
    claude_turn(20, 200, 2000, 100),
])
# Claude: no plugin.
write(claude / "proj-a" / "none.jsonl", [
    json.dumps({"type": "user", "message": {"content": "unrelated work"}}),
    claude_turn(40, 400, 4000, 200),
])
# Claude: generic "drainclamp" only — unclassified, not a fake bucket.
write(claude / "proj-a" / "generic.jsonl", [
    json.dumps({"type": "user", "message": {"content": "talking about drainclamp"}}),
    claude_turn(5, 5, 5, 5),
])
# No usage records at all -- skipped, not a zero-turn session.
write(claude / "proj-a" / "empty.jsonl", [json.dumps({"type": "summary"})])
# Truncated trailing line, as a live session leaves behind.
write(claude / "proj-a" / "partial.jsonl", [claude_turn(1, 1, 1, 1), '{"type": "assis'])
# attributionSkill wins over unmarked user text.
write(claude / "proj-a" / "attr-base.jsonl", [
    json.dumps({"type": "user", "message": {"content": "unrelated work"}}),
    json.dumps({"type": "assistant",
                "attributionSkill": "drainclamp-build:drainclamp-build",
                "message": {"usage": {
                    "input_tokens": 20, "cache_creation_input_tokens": 200,
                    "cache_read_input_tokens": 2000, "output_tokens": 100}}}),
])
# attributionSkill of another product wins over a drainclamp-build mention.
write(claude / "proj-a" / "attr-other.jsonl", [
    json.dumps({"type": "user", "message": {"content": "please run drainclamp-build"}}),
    json.dumps({"type": "assistant", "attributionSkill": "doctor",
                "message": {"usage": {
                    "input_tokens": 8, "cache_creation_input_tokens": 8,
                    "cache_read_input_tokens": 80, "output_tokens": 8}}}),
])
# One API call streamed as three records repeating its usage, plus a second call.
def streamed(mid, rid, inp, stamp=""):
    return json.dumps({"type": "assistant", "requestId": rid, "timestamp": stamp,
                       "message": {"id": mid, "usage": {
                           "input_tokens": inp, "cache_creation_input_tokens": 0,
                           "cache_read_input_tokens": 0, "output_tokens": 1}}})


write(tmp / "dedupe" / "s.jsonl", [streamed("msg1", "req1", 7), streamed("msg1", "req1", 7),
                                   streamed("msg1", "req1", 7), streamed("msg2", "req2", 3)])
dup = dc_tokens.scan_claude(tmp / "dedupe" / "s.jsonl")
check("claude streamed records count once per API call",
      dup is not None and (dup.turns, dup.input) == (2, 10), dup and (dup.turns, dup.input))
# A large none session so the turn-weighted mean parts from the session median.
write(claude / "proj-a" / "none-big.jsonl", [
    json.dumps({"type": "user", "message": {"content": "unrelated work"}}),
    claude_turn(1000, 10000, 80000, 200),
])


def codex_event(payload):
    return json.dumps({"type": "event_msg", "payload": payload})


def codex_usage(inp, cached, write_n, out, reasoning=0):
    return {
        "input_tokens": inp,
        "cached_input_tokens": cached,
        "cache_write_input_tokens": write_n,
        "output_tokens": out,
        "reasoning_output_tokens": reasoning,
        "total_tokens": inp + cached + write_n + out + reasoning,
    }


# Codex: two streaming token_count snapshots, then a later turn.
# Session bill is the LAST total_token_usage, not the sum of snapshots.
write(codex / "2026" / "08" / "rollout-base.jsonl", [
    json.dumps({"type": "session_meta", "payload": {
        "type": "session_meta", "base_instructions": "use drainclamp-build gates"}}),
    codex_event({"type": "user_message", "content": "go"}),
    codex_event({"type": "token_count", "info": {
        "last_token_usage": codex_usage(10, 10, 10, 5),
        "total_token_usage": codex_usage(10, 10, 10, 5),
        "model_context_window": 100000}}),
    codex_event({"type": "token_count", "info": {
        "last_token_usage": codex_usage(10, 90, 10, 5),
        "total_token_usage": codex_usage(10, 90, 10, 5),
        "model_context_window": 100000}}),
    codex_event({"type": "user_message", "content": "again"}),
    codex_event({"type": "token_count", "info": {
        "last_token_usage": codex_usage(20, 200, 20, 10, 3),
        "total_token_usage": codex_usage(30, 290, 30, 15, 3),
        "model_context_window": 100000}}),
])

# Grok ADHD: session-level signals only.
(grok / "ws" / "sess-adhd" / "system_prompt.txt").write_text(
    "skill drainclamp-build-adhd loaded\n", encoding="utf-8")
write(grok / "ws" / "sess-adhd" / "chat_history.jsonl", [
    json.dumps({"type": "user", "content": f"secret {SECRET}"}),
])
(grok / "ws" / "sess-adhd" / "signals.json").write_text(json.dumps({
    "turnCount": 4,
    "contextTokensUsed": 8000,
    "contextWindowTokens": 128000,
    "compactionCount": 1,
    "totalTokensBeforeCompaction": 12000,
}), encoding="utf-8")
# Grok none.
(grok / "ws" / "sess-none" / "system_prompt.txt").write_text(
    "plain assistant\n", encoding="utf-8")
write(grok / "ws" / "sess-none" / "chat_history.jsonl", [
    json.dumps({"type": "user", "content": "hello"}),
])
(grok / "ws" / "sess-none" / "signals.json").write_text(json.dumps({
    "turnCount": 2,
    "contextTokensUsed": 16000,
    "contextWindowTokens": 128000,
    "compactionCount": 0,
    "totalTokensBeforeCompaction": 0,
}), encoding="utf-8")
# Subagent must not be counted as its own session.
(grok / "ws" / "sess-adhd" / "subagents" / "child" / "signals.json").write_text(
    json.dumps({"turnCount": 99, "contextTokensUsed": 1}), encoding="utf-8")

# Static skill trees.
(skills / "base" / "SKILL.md").write_text("x" * 400, encoding="utf-8")
(skills / "base" / "references" / "phase0-audit.md").write_text("y" * 200, encoding="utf-8")
(skills / "adhd" / "SKILL.md").write_text("x" * 800, encoding="utf-8")
(skills / "adhd" / "references" / "phase0-audit.md").write_text("y" * 200, encoding="utf-8")

common = [
    "--claude", str(claude),
    "--codex", str(codex),
    "--grok", str(grok),
    "--base-skill", str(skills / "base"),
    "--adhd-skill", str(skills / "adhd"),
]

rc = dc_tokens.report(dc_tokens.Config(
    claude=claude, codex=codex, grok=grok,
    base_skill=skills / "base", adhd_skill=skills / "adhd",
    as_json=False))
check("report exits clean", rc == 0, rc)

proc = run_cli(*common, "--json")
check("json mode exits clean", proc.returncode == 0, proc.stderr.strip())
data = json.loads(proc.stdout)

claude_h = data["hosts"]["claude"]
check("claude adhd sessions", claude_h["adhd"]["sessions"] == 1, claude_h["adhd"]["sessions"])
check("claude adhd turns", claude_h["adhd"]["turns"] == 2, claude_h["adhd"]["turns"])
check("claude adhd input summed", claude_h["adhd"]["input"] == 20, claude_h["adhd"]["input"])
check("claude adhd cache reads summed", claude_h["adhd"]["read"] == 2000, claude_h["adhd"]["read"])
check("claude base not swallowed by adhd prefix", claude_h["base"]["sessions"] == 2,
      claude_h["base"]["sessions"])
check("claude attributionSkill beats a text mention",
      claude_h["none"]["sessions"] == 4, claude_h["none"]["sessions"])
check("claude unclassified generic marker",
      claude_h["unclassified"]["sessions"] == 1, claude_h["unclassified"]["sessions"])
check("usage-free transcript skipped",
      data["hosts"]["claude"]["sessions_scanned"] == 8,
      data["hosts"]["claude"]["sessions_scanned"])
check("claude scoreboard is session median",
      claude_h["none"]["context_per_turn_p50"] < claude_h["none"]["context_per_turn"],
      (claude_h["none"]["context_per_turn_p50"], claude_h["none"]["context_per_turn"]))

codex_h = data["hosts"]["codex"]
check("codex base classified from session_meta", codex_h["base"]["sessions"] == 1)
check("codex uses last total, not streamed snapshots",
      codex_h["base"]["input"] == 30, codex_h["base"]["input"])
check("codex cached input mapped to read",
      codex_h["base"]["read"] == 290, codex_h["base"]["read"])
check("codex reasoning kept separate",
      codex_h["base"]["reasoning"] == 3, codex_h["base"]["reasoning"])
check("codex turns from user_message count",
      codex_h["base"]["turns"] == 2, codex_h["base"]["turns"])

grok_h = data["hosts"]["grok"]
check("grok adhd from system_prompt", grok_h["adhd"]["sessions"] == 1)
check("grok none counted", grok_h["none"]["sessions"] == 1)
check("grok ignores subagent signals", grok_h["adhd"]["turns"] == 4, grok_h["adhd"]["turns"])
check("grok context is session-level",
      grok_h["adhd"]["context_tokens"] == 8000, grok_h["adhd"]["context_tokens"])
check("grok records growth-before-compaction",
      grok_h["adhd"]["tokens_before"] == 12000, grok_h["adhd"]["tokens_before"])
check("grok has no invented per-turn bill",
      "context_per_turn" not in grok_h["adhd"])
check("grok has no invented cache split",
      grok_h["adhd"]["read"] == 0 and "cache_hit_rate" not in grok_h["adhd"])

static = data["static"]
check("static none is zero", static["none"]["resident_bytes"] == 0)
check("static base resident bytes", static["base"]["resident_bytes"] == 400)
check("static adhd larger resident", static["adhd"]["resident_bytes"] == 800)
check("static paged counted", static["base"]["paged_bytes"] == 200)

check("json names the three buckets",
      set(data["buckets"]) == {"none", "base", "adhd"})

plain = run_cli(*common).stdout
check("json output leaks no transcript text", SECRET not in proc.stdout)
check("table output leaks no transcript text", SECRET not in plain)
check("table reports three buckets",
      "none" in plain and "base" in plain and "adhd" in plain)
check("table reports three hosts",
      "claude" in plain and "codex" in plain and "grok" in plain)
check("table carries the observational caveat", "observational" in plain)
check("table states grok is session-level", "session-level" in plain)
grok_section = plain.split("grok (")[-1]
check("grok table has no ctx/turn", "ctx/turn" not in grok_section)
check("grok table reports before", "before" in grok_section)
check("claude table uses session median", "ctx/p50" in plain)
check("claude table shows first-last", "first" in plain and "last" in plain)
check("adhd delta prints even at n=1", "adhd/none" in plain)
check("thin-n deltas are marked n<3", "n<3" in plain)
check("table does not use old binary labels",
      "with skill" not in plain and "without skill" not in plain)

# An explicit missing directory is a stated error, not an empty green table.
proc = run_cli("--claude", str(tmp / "nope"))
check("missing explicit transcript directory fails loudly",
      proc.returncode != 0 and "no transcript directory" in proc.stderr)

# Host isolation: passing only --claude must not scan default Codex/Grok homes.
isolated = run_cli("--claude", str(claude), "--json")
check("explicit claude-only still exits clean", isolated.returncode == 0, isolated.stderr.strip())
iso = json.loads(isolated.stdout)
check("unrequested hosts are absent, not default-scanned",
      "codex" not in iso["hosts"] and "grok" not in iso["hosts"])

# Cache-hit and first/last context are computed, not guessed.
adhd_m = claude_h["adhd"]
# (10+100+1000)*2 = 2220 context, read=2000, hit = 2000/2220
check("claude cache-hit rate present", 0.8 < adhd_m["cache_hit_rate"] < 1.0,
      adhd_m["cache_hit_rate"])
check("claude first-last context present",
      adhd_m["first_context"] > 0 and adhd_m["last_context"] > 0)

joined = dc_tokens.rcell("661", 8) + dc_tokens.rcell("2,427,693", 9)
check("rcell keeps p90 and fresh/s as two tokens",
      joined.split()[-2:] == ["661", "2,427,693"], joined)

personal = tmp / "personal"
base_skill = personal / "drainclamp-build" / "skills" / "drainclamp-build"
adhd_name = "drainclamp-build" + "-adhd"
adhd_skill = personal / adhd_name / "skills" / adhd_name
adhd_skill.mkdir(parents=True)
(adhd_skill / "SKILL.md").write_text("x", encoding="utf-8")
found = dc_tokens.adhd_skill_near(base_skill)
check("adhd locator finds sibling checkout", found == adhd_skill, found)
check("adhd locator misses a tree with no sibling",
      dc_tokens.adhd_skill_near(tmp / "orphan" / "skills" / "drainclamp-build") is None)

# --- project mode: chunk windows from done_at, clipped to time sessions --------
proj = (tmp / "proj-root").resolve()
(proj / ".agent").mkdir(parents=True)
D = "2026-01-05T"


def chunk(cid, done_at):
    return {"id": cid, "goal": "g", "targets": [], "est_min": None, "done": True,
            "done_at": D + done_at, "note": ""}


(proj / ".agent" / "drainclamp-project.json").write_text(json.dumps({
    "schema": 1, "rev": 1,
    "time": [{"id": "s1", "start": D + "10:00:00+00:00", "end": D + "11:00:00+00:00",
              "source": "auto", "note": ""}],
    "chunks": {"m1": [chunk("c0", "09:00:00+00:00"), chunk("c1", "10:20:00+00:00"),
                      chunk("c2", "10:40:00+00:00")],
               "m2": [chunk("c1", "10:45:00+00:00"),
                      dict(chunk("c2", "10:50:00+00:00"), done=False)]}}), encoding="utf-8")
pclaude = tmp / "pclaude"
named = json.dumps({"type": "user", "cwd": str(proj), "message": {"content": SECRET}})
write(pclaude / "p" / "a.jsonl", [
    named,
    streamed("x0", "r0", 1, D + "09:30:00Z"),       # before any window, outside the session
    streamed("x1", "r1", 5, D + "10:10:00Z"),       # m1/c1
    streamed("x2", "r2", 7, D + "10:30:00Z"),       # m1/c2, streamed twice
    streamed("x2", "r2", 7, D + "10:30:00Z"),
    streamed("x3", "r3", 11, D + "10:52:00Z"),      # in the session, in no chunk
    streamed("x4", "r4", 13, D + "13:00:00Z"),      # after the session
])
write(pclaude / "p" / "a" / "subagents" / "agent-1.jsonl", [
    json.dumps({"type": "user", "message": {"content": "work in " + proj.as_posix()}}),
    streamed("y1", "q1", 17, D + "10:15:00Z"),      # m1/c1, from a subagent
])
write(pclaude / "p" / "other.jsonl", [
    json.dumps({"type": "user", "message": {"content": "some other repo"}}),
    streamed("z1", "w1", 100, D + "10:10:00Z"),
])
got = dc_tokens.project_tokens(proj, pclaude)
check("project: only transcripts naming the root count", got["transcripts"] == 2, got["transcripts"])
check("project: chunk window sums main + subagent calls",
      (got["chunks"]["m1/c1"]["calls"], got["chunks"]["m1/c1"]["fresh"]) == (2, 22),
      got["chunks"].get("m1/c1"))
check("project: streamed call counted once", got["chunks"]["m1/c2"]["calls"] == 1,
      got["chunks"].get("m1/c2"))
check("project: milestone totals its chunks",
      got["milestones"]["m1"]["fresh"] == 29 and got["milestones"]["m2"]["calls"] == 0,
      got["milestones"])
check("project: session time outside chunks is `outside`",
      got["outside"]["calls"] == 1 and got["outside"]["fresh"] == 11, got["outside"])
check("project: chunk done outside every session has no window",
      got["unwindowed"] == ["m1/c0"], got["unwindowed"])
check("project: open chunk has no row", "m2/c2" not in got["chunks"], sorted(got["chunks"]))
cli = run_cli("--project", str(proj), "--claude", str(pclaude))
check("project CLI exits clean", cli.returncode == 0, cli.stderr.strip())
check("project CLI declares coverage", "COVERAGE: partial" in cli.stdout, cli.stdout[:200])
check("project CLI never prints transcript text", SECRET not in cli.stdout + cli.stderr)
check("project CLI prints milestone total", "m1 total" in cli.stdout, cli.stdout)
side = proj / ".agent" / "drainclamp-project.json"
saved = run_cli("--project", str(proj), "--claude", str(pclaude), "--save")
stored = json.loads(side.read_text(encoding="utf-8"))
check("save exits clean", saved.returncode == 0, saved.stderr.strip())
check("save stores totals in the sidecar and bumps rev",
      stored["rev"] == 2 and stored["tokens"]["chunks"]["m1/c1"]["calls"] == 2
      and stored["tokens"]["at"], stored.get("tokens"))
check("save never stores transcript text or time", SECRET not in side.read_text(encoding="utf-8")
      and len(stored["time"]) == 1, stored["time"])
import dc_project  # noqa: E402
check("sidecar with tokens still validates",
      dc_project.load(proj / ".agent")["tokens"]["transcripts"] == 2)
stored["tokens"] = ["not", "a", "dict"]
side.write_text(json.dumps(stored), encoding="utf-8")
wrong = run_cli("--project", str(proj), "--claude", str(pclaude), "--save")
check("save refuses a sidecar with a malformed tokens field",
      wrong.returncode != 0 and json.loads(side.read_text(encoding="utf-8"))["tokens"] == ["not", "a", "dict"],
      wrong.stderr.strip())
bad = tmp / "no-sidecar"
bad.mkdir()
nosave = run_cli("--project", str(bad), "--claude", str(pclaude), "--save")
check("save with no sidecar refuses and creates nothing",
      nosave.returncode != 0 and not (bad / ".agent").exists(), nosave.stderr.strip())
check("--save without --project is a usage error",
      run_cli("--save", "--claude", str(pclaude)).returncode == 2)
empty_run = dc_tokens.project_tokens(bad, pclaude)
check("project with no sidecar reports nothing",
      empty_run["chunks"] == {} and empty_run["transcripts"] == 0, empty_run)

print()
print("FAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
