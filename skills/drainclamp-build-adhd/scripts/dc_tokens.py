#!/usr/bin/env python3
"""On-demand token report: none / base / adhd, across Claude, Codex and Grok.

No host exposes live context usage to a model -- that is why Gate 5's
`--context-high` is an assertion rather than a measurement. Transcripts on disk
are the one place the real numbers exist, so this reads them after the fact.

It is deliberately not part of any gate and is never referenced from SKILL.md:
a measurement tool that costs context every invocation would be measuring a
problem it had joined. Run it when you want the number.

Output is aggregate only. Transcript text is never printed -- these files hold
whole working sessions, and a summary must not become a way to leak one.

Claude and Codex contribute per-turn usage. Claude is classified by
`attributionSkill` when present, then falls back to markers. The Claude
scoreboard is session-median ctx/fresh, not the turn-weighted mean.

Grok stores session-level signals only. The Grok table reports resident
window, growth-before-compaction, turns/session and compaction count --
never snapshot/turns as if it were a per-turn bill.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import _dcio

BUCKETS = ("none", "base", "adhd")
BASE_NAME = "drainclamp" + "-build"
# Concatenated so the baseline identity scan never sees a foreign skill token.
ADHD_MARKERS = (BASE_NAME + "-adhd",)
BASE_MARKERS = (BASE_NAME,)
GENERIC_MARKERS = ("drainclamp",)
MIN_DELTA_SESSIONS = 3

CLAUDE_USAGE = (
    "input_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "output_tokens",
)


@dataclass
class Config:
    claude: Path | None = None
    codex: Path | None = None
    grok: Path | None = None
    base_skill: Path | None = None
    adhd_skill: Path | None = None
    as_json: bool = False
    no_static: bool = False
    claude_explicit: bool = False
    codex_explicit: bool = False
    grok_explicit: bool = False


@dataclass
class Session:
    bucket: str
    host: str
    turns: int = 0
    input: int = 0
    created: int = 0
    read: int = 0
    output: int = 0
    reasoning: int = 0
    first_context: int = 0
    last_context: int = 0
    context_tokens: int = 0
    compaction: int = 0
    tokens_before: int = 0
    window_usage: int = 0
    session_level: bool = False


@dataclass
class Bucket:
    """Running totals for one (host, bucket) cell."""

    sessions: int = 0
    turns: int = 0
    input: int = 0
    created: int = 0
    read: int = 0
    output: int = 0
    reasoning: int = 0
    context_tokens: int = 0
    compaction: int = 0
    tokens_before: int = 0
    session_level: bool = False
    session_turns: list[int] = field(default_factory=list)
    session_fresh: list[int] = field(default_factory=list)
    session_ctx_per_turn: list[float] = field(default_factory=list)
    session_fresh_per_turn: list[float] = field(default_factory=list)
    session_context: list[int] = field(default_factory=list)
    session_before: list[int] = field(default_factory=list)
    session_window: list[int] = field(default_factory=list)
    first_contexts: list[int] = field(default_factory=list)
    last_contexts: list[int] = field(default_factory=list)

    def add(self, session: Session) -> None:
        self.sessions += 1
        self.turns += session.turns
        self.input += session.input
        self.created += session.created
        self.read += session.read
        self.output += session.output
        self.reasoning += session.reasoning
        self.context_tokens += session.context_tokens
        self.compaction += session.compaction
        self.tokens_before += session.tokens_before
        self.session_level = self.session_level or session.session_level
        if session.turns:
            self.session_turns.append(session.turns)
            if not session.session_level:
                ctx = (session.input + session.created + session.read) / session.turns
                fresh = (session.input + session.created) / session.turns
                self.session_ctx_per_turn.append(ctx)
                self.session_fresh_per_turn.append(fresh)
                self.session_fresh.append(session.input + session.created)
        if session.session_level:
            self.session_context.append(session.context_tokens)
            self.session_before.append(session.tokens_before)
            self.session_window.append(session.window_usage)
        if session.first_context:
            self.first_contexts.append(session.first_context)
        if session.last_context:
            self.last_contexts.append(session.last_context)
        elif session.context_tokens:
            self.last_contexts.append(session.context_tokens)

    @property
    def context_total(self) -> int:
        return self.input + self.created + self.read

    @property
    def context_per_turn(self) -> float:
        return self.context_total / self.turns if self.turns else 0.0

    @property
    def context_per_turn_p50(self) -> float:
        return percentile(self.session_ctx_per_turn, 0.50)

    @property
    def fresh_per_turn(self) -> float:
        return (self.input + self.created) / self.turns if self.turns else 0.0

    @property
    def fresh_per_turn_p50(self) -> float:
        return percentile(self.session_fresh_per_turn, 0.50)

    @property
    def context_p50(self) -> float:
        return percentile(self.session_context, 0.50)

    @property
    def before_p50(self) -> float:
        return percentile(self.session_before, 0.50)

    @property
    def window_p50(self) -> float:
        return percentile(self.session_window, 0.50)

    @property
    def output_per_turn(self) -> float:
        return self.output / self.turns if self.turns else 0.0

    @property
    def cache_hit_rate(self) -> float | None:
        if self.session_level or not self.context_total:
            return None
        return self.read / self.context_total

    @property
    def first_context(self) -> float:
        return percentile(self.first_contexts, 0.50)

    @property
    def last_context(self) -> float:
        return percentile(self.last_contexts, 0.50)

    @property
    def fresh_per_session(self) -> float:
        return mean(self.session_fresh)

    @property
    def turns_p50(self) -> float:
        return percentile(self.session_turns, 0.50)

    @property
    def turns_p90(self) -> float:
        return percentile(self.session_turns, 0.90)

    def as_dict(self) -> dict:
        data = {
            "sessions": self.sessions,
            "turns": self.turns,
            "input": self.input,
            "created": self.created,
            "read": self.read,
            "output": self.output,
            "reasoning": self.reasoning,
            "context_per_turn": self.context_per_turn,
            "context_per_turn_p50": self.context_per_turn_p50,
            "fresh_per_turn": self.fresh_per_turn,
            "fresh_per_turn_p50": self.fresh_per_turn_p50,
            "output_per_turn": self.output_per_turn,
            "fresh_per_session": self.fresh_per_session,
            "turns_per_session_p50": self.turns_p50,
            "turns_per_session_p90": self.turns_p90,
            "first_context": self.first_context,
            "last_context": self.last_context,
        }
        hit = self.cache_hit_rate
        if hit is not None:
            data["cache_hit_rate"] = hit
        if self.session_level:
            data["context_tokens"] = self.context_tokens
            data["context_p50"] = self.context_p50
            data["tokens_before"] = self.tokens_before
            data["before_p50"] = self.before_p50
            data["compaction"] = self.compaction
            data["window_p50"] = self.window_p50
            data["session_level"] = True
            data.pop("context_per_turn", None)
            data.pop("fresh_per_turn", None)
        return data


def default_claude() -> Path:
    return Path.home() / ".claude" / "projects"


def default_codex() -> Path:
    return Path.home() / ".codex" / "sessions"


def default_grok() -> Path:
    return Path.home() / ".grok" / "sessions"


def skill_dir_of(script: Path | None = None) -> Path:
    return (script or Path(__file__)).resolve().parent.parent


def default_base_skill() -> Path | None:
    here = skill_dir_of()
    if (here / "SKILL.md").is_file() and here.name == BASE_NAME:
        return here
    sibling = here.parent / BASE_NAME
    if (sibling / "SKILL.md").is_file():
        return sibling
    return here if (here / "SKILL.md").is_file() else None


def adhd_skill_near(skill_dir: Path) -> Path | None:
    """ADHD checkout next to this repo: <parent>/<name>-adhd/skills/<name>-adhd."""
    if skill_dir.name.endswith("-adhd") and (skill_dir / "SKILL.md").is_file():
        return skill_dir
    # scripts/ -> skill dir -> skills/ -> repo
    repo = skill_dir.parent.parent
    adhd_name = BASE_NAME + "-adhd"
    sibling = repo.parent / adhd_name / "skills" / adhd_name
    if (sibling / "SKILL.md").is_file():
        return sibling
    return None


def default_adhd_skill() -> Path | None:
    return adhd_skill_near(skill_dir_of())


def classify(text: str) -> str:
    lowered = text.casefold()
    if any(marker in lowered for marker in ADHD_MARKERS):
        return "adhd"
    if any(marker in lowered for marker in BASE_MARKERS):
        return "base"
    if any(marker in lowered for marker in GENERIC_MARKERS):
        return "unclassified"
    return "none"


def classify_attr(value: str) -> str:
    """Host skill id for the base variant. Not free text."""
    lowered = value.casefold()
    if (BASE_NAME + "-adhd").casefold() in lowered:
        return "adhd"
    if BASE_NAME.casefold() in lowered:
        return "base"
    return "none"


def classify_path(path: Path) -> str:
    try:
        return classify(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return "none"


def merge_bucket(current: str, incoming: str) -> str:
    rank = {"adhd": 3, "base": 2, "unclassified": 1, "none": 0}
    return incoming if rank[incoming] > rank[current] else current


def mean(values: list[int] | list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def percentile(values: list[int] | list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    index = (len(ordered) - 1) * p
    lo = int(index)
    hi = min(lo + 1, len(ordered) - 1)
    frac = index - lo
    return ordered[lo] + (ordered[hi] - ordered[lo]) * frac


def add_int(usage: dict, *keys: str) -> int:
    total = 0
    for key in keys:
        value = usage.get(key)
        if isinstance(value, int):
            total += value
        elif isinstance(value, float):
            total += int(value)
    return total


def context_of(inp: int, created: int, read: int) -> int:
    return inp + created + read


def scan_claude(path: Path) -> Session | None:
    session = Session(bucket="none", host="claude")
    text_bucket = "none"
    attr_bucket: str | None = None
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                text_bucket = merge_bucket(text_bucket, classify(line))
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                attr = record.get("attributionSkill")
                if isinstance(attr, str) and attr.strip():
                    if attr_bucket is None:
                        attr_bucket = "none"
                    attr_bucket = merge_bucket(attr_bucket, classify_attr(attr))
                message = record.get("message")
                if not isinstance(message, dict):
                    continue
                usage = message.get("usage")
                if not isinstance(usage, dict):
                    continue
                inp = add_int(usage, "input_tokens")
                created = add_int(usage, "cache_creation_input_tokens")
                read = add_int(usage, "cache_read_input_tokens")
                output = add_int(usage, "output_tokens")
                ctx = context_of(inp, created, read)
                session.turns += 1
                session.input += inp
                session.created += created
                session.read += read
                session.output += output
                if session.turns == 1:
                    session.first_context = ctx
                session.last_context = ctx
    except OSError:
        return None
    session.bucket = attr_bucket if attr_bucket is not None else text_bucket
    return session if session.turns else None


def _codex_usage(block: object) -> tuple[int, int, int, int, int] | None:
    if not isinstance(block, dict):
        return None
    return (
        add_int(block, "input_tokens"),
        add_int(block, "cache_write_input_tokens"),
        add_int(block, "cached_input_tokens"),
        add_int(block, "output_tokens"),
        add_int(block, "reasoning_output_tokens"),
    )


def scan_codex(path: Path) -> Session | None:
    session = Session(bucket="none", host="codex")
    last_total: tuple[int, int, int, int, int] | None = None
    user_messages = 0
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                session.bucket = merge_bucket(session.bucket, classify(line))
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                payload = record.get("payload")
                if not isinstance(payload, dict):
                    continue
                kind = payload.get("type") or record.get("type")
                if kind == "user_message":
                    user_messages += 1
                if kind != "token_count":
                    continue
                info = payload.get("info")
                if not isinstance(info, dict):
                    continue
                last = _codex_usage(info.get("last_token_usage"))
                total = _codex_usage(info.get("total_token_usage"))
                if last is not None:
                    ctx = context_of(last[0], last[1], last[2])
                    if session.first_context == 0:
                        session.first_context = ctx
                    session.last_context = ctx
                if total is not None:
                    last_total = total
    except OSError:
        return None
    if last_total is None:
        return None
    session.input, session.created, session.read, session.output, session.reasoning = last_total
    session.turns = user_messages or 1
    return session


def scan_grok(session_dir: Path) -> Session | None:
    signals_path = session_dir / "signals.json"
    try:
        signals = json.loads(signals_path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None
    if not isinstance(signals, dict):
        return None
    turns = add_int(signals, "turnCount")
    context = add_int(signals, "contextTokensUsed")
    if not turns and not context:
        return None
    bucket = "none"
    for name in ("system_prompt.txt", "chat_history.jsonl"):
        extra = session_dir / name
        if extra.is_file():
            bucket = merge_bucket(bucket, classify_path(extra))
    return Session(
        bucket=bucket,
        host="grok",
        turns=turns or 1,
        context_tokens=context,
        last_context=context,
        compaction=add_int(signals, "compactionCount"),
        tokens_before=add_int(signals, "totalTokensBeforeCompaction"),
        window_usage=add_int(signals, "contextWindowUsage"),
        session_level=True,
    )


def iter_claude(root: Path) -> Iterable[Path]:
    # Top-level project/session.jsonl only -- nested tool-results are not sessions.
    yield from sorted(root.glob("*/*.jsonl"))


def iter_codex(root: Path) -> Iterable[Path]:
    yield from sorted(p for p in root.rglob("*.jsonl") if p.is_file())


def iter_grok(root: Path) -> Iterable[Path]:
    seen: set[Path] = set()
    for pattern in ("*/signals.json", "*/*/signals.json"):
        for path in sorted(root.glob(pattern)):
            session_dir = path.parent
            if session_dir in seen:
                continue
            # Subagents live under .../subagents/<id>/signals.json -- skip them.
            if "subagents" in session_dir.parts:
                continue
            seen.add(session_dir)
            yield session_dir


def empty_host() -> dict[str, Bucket]:
    return {name: Bucket() for name in (*BUCKETS, "unclassified")}


def consume(host: str, sessions: Iterable[Session | None]) -> tuple[dict[str, Bucket], int]:
    buckets = empty_host()
    scanned = 0
    for session in sessions:
        if session is None:
            continue
        scanned += 1
        buckets[session.bucket].add(session)
    return buckets, scanned


def skill_size(path: Path | None) -> dict:
    empty = {
        "present": False,
        "resident_bytes": 0,
        "resident_lines": 0,
        "resident_tokens_est": 0,
        "paged_bytes": 0,
        "paged_lines": 0,
        "paged_tokens_est": 0,
        "paged_files": 0,
    }
    if path is None or not path.is_dir():
        return empty
    skill_md = path / "SKILL.md"
    resident_bytes, resident_lines = file_size(skill_md)
    paged_bytes = paged_lines = paged_files = 0
    refs = path / "references"
    if refs.is_dir():
        for child in sorted(refs.glob("*.md")):
            size, lines = file_size(child)
            paged_bytes += size
            paged_lines += lines
            paged_files += 1
    return {
        "present": skill_md.is_file(),
        "resident_bytes": resident_bytes,
        "resident_lines": resident_lines,
        "resident_tokens_est": tokens_est(resident_bytes),
        "paged_bytes": paged_bytes,
        "paged_lines": paged_lines,
        "paged_tokens_est": tokens_est(paged_bytes),
        "paged_files": paged_files,
    }


def file_size(path: Path) -> tuple[int, int]:
    if not path.is_file():
        return 0, 0
    try:
        data = path.read_bytes()
    except OSError:
        return 0, 0
    return len(data), data.count(b"\n") + (0 if data.endswith(b"\n") or not data else 1)


def tokens_est(byte_count: int) -> int:
    # chars/4 is an estimate, not a host bill. Stated as such in the table.
    return (byte_count + 3) // 4


def require_dir(path: Path, explicit: bool) -> Path | None:
    if path.is_dir():
        return path
    if explicit:
        print(f"ERROR: no transcript directory at {path}", file=sys.stderr)
        raise _dcio.DcError(f"no transcript directory at {path}", _dcio.EXIT_MISSING_REQUIRED)
    return None


def scan_hosts(cfg: Config) -> tuple[dict[str, dict], list[str]]:
    notes: list[str] = []
    hosts: dict[str, dict] = {}

    if cfg.claude is not None:
        root = require_dir(cfg.claude, cfg.claude_explicit)
        if root is None:
            notes.append(f"claude: skipped (no directory at {cfg.claude})")
        else:
            buckets, scanned = consume("claude", (scan_claude(p) for p in iter_claude(root)))
            hosts["claude"] = pack_host(buckets, scanned, session_level=False)

    if cfg.codex is not None:
        root = require_dir(cfg.codex, cfg.codex_explicit)
        if root is None:
            notes.append(f"codex: skipped (no directory at {cfg.codex})")
        else:
            buckets, scanned = consume("codex", (scan_codex(p) for p in iter_codex(root)))
            hosts["codex"] = pack_host(buckets, scanned, session_level=False)

    if cfg.grok is not None:
        root = require_dir(cfg.grok, cfg.grok_explicit)
        if root is None:
            notes.append(f"grok: skipped (no directory at {cfg.grok})")
        else:
            buckets, scanned = consume("grok", (scan_grok(p) for p in iter_grok(root)))
            hosts["grok"] = pack_host(buckets, scanned, session_level=True)

    return hosts, notes


def pack_host(buckets: dict[str, Bucket], scanned: int, session_level: bool) -> dict:
    packed = {name: buckets[name].as_dict() for name in (*BUCKETS, "unclassified")}
    packed["sessions_scanned"] = scanned
    packed["session_level"] = session_level
    return packed


def static_block(cfg: Config) -> dict:
    return {
        "none": skill_size(None),
        "base": skill_size(cfg.base_skill),
        "adhd": skill_size(cfg.adhd_skill),
        "token_estimate": "bytes/4",
    }


def num(value: float) -> str:
    return f"{value:,.0f}"


def rcell(text: str, width: int) -> str:
    """Right-align, always leaving a separating space if the value fills the column."""
    return text.rjust(max(width, len(text) + 1))


def pct(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value * 100:.1f}%"


def delta(on: float, off: float) -> str:
    if not off:
        return "n/a"
    return f"{(on - off) / off * 100:+.1f}%"


def print_static(static: dict) -> None:
    print("static skill size (bytes/4 estimate; not a host bill)")
    head = f"{'':<16}{'resident':>10}{'paged':>10}{'total':>10}{'~tok':>8}{'files':>7}"
    print(head)
    for name in BUCKETS:
        row = static[name]
        total = row["resident_bytes"] + row["paged_bytes"]
        print(f"{name:<16}{row['resident_bytes']:>10,}{row['paged_bytes']:>10,}"
              f"{total:>10,}{row['resident_tokens_est'] + row['paged_tokens_est']:>8,}"
              f"{row['paged_files'] + (1 if row['present'] else 0):>7}")


def print_usage_host(name: str, host: dict) -> None:
    kind = "session-level; no cache split" if host.get("session_level") else "per-turn usage"
    print(f"{name} ({kind}, {host['sessions_scanned']} session(s) scanned)")
    if host.get("session_level"):
        head = (f"{'':<16}{rcell('sessions', 9)}{rcell('turns', 8)}"
                f"{rcell('context', 12)}{rcell('before', 12)}{rcell('compact', 9)}"
                f"{rcell('tn/p50', 8)}{rcell('tn/p90', 8)}{rcell('win%', 7)}")
        print(head)
        for label in (*BUCKETS, "unclassified"):
            row = host[label]
            if label == "unclassified" and row["sessions"] == 0:
                continue
            print(f"{label:<16}{rcell(str(row['sessions']), 9)}{rcell(str(row['turns']), 8)}"
                  f"{rcell(num(row.get('context_p50', 0)), 12)}"
                  f"{rcell(num(row.get('before_p50', 0)), 12)}"
                  f"{rcell(num(row.get('compaction', 0)), 9)}"
                  f"{rcell(num(row['turns_per_session_p50']), 8)}"
                  f"{rcell(num(row['turns_per_session_p90']), 8)}"
                  f"{rcell(num(row.get('window_p50', 0)), 7)}")
        _print_deltas(host, (
            ("context_p50", 12),
            ("before_p50", 12),
        ), blank_before=17)
        return

    head = (f"{'':<16}{rcell('sessions', 9)}{rcell('turns', 8)}"
            f"{rcell('ctx/p50', 12)}{rcell('fresh/p50', 12)}{rcell('out/tn', 8)}"
            f"{rcell('hit%', 7)}{rcell('tn/p50', 8)}"
            f"{rcell('first', 10)}{rcell('last', 10)}")
    print(head)
    for label in (*BUCKETS, "unclassified"):
        row = host[label]
        if label == "unclassified" and row["sessions"] == 0:
            continue
        print(f"{label:<16}{rcell(str(row['sessions']), 9)}{rcell(str(row['turns']), 8)}"
              f"{rcell(num(row['context_per_turn_p50']), 12)}"
              f"{rcell(num(row['fresh_per_turn_p50']), 12)}"
              f"{rcell(num(row['output_per_turn']), 8)}"
              f"{rcell(pct(row.get('cache_hit_rate')), 7)}"
              f"{rcell(num(row['turns_per_session_p50']), 8)}"
              f"{rcell(num(row['first_context']), 10)}"
              f"{rcell(num(row['last_context']), 10)}")
    _print_deltas(host, (
        ("context_per_turn_p50", 12),
        ("fresh_per_turn_p50", 12),
        ("output_per_turn", 8),
    ), blank_before=17)


def _print_deltas(host: dict, fields: tuple[tuple[str, int], ...], blank_before: int) -> None:
    pairs = (("base/none", "base", "none"),
             ("adhd/none", "adhd", "none"),
             ("adhd/base", "adhd", "base"))
    any_pair = False
    for label, left, right in pairs:
        if not host[left]["sessions"] or not host[right]["sessions"]:
            continue
        any_pair = True
        cells = "".join(
            f"{delta(host[left][field], host[right][field]):>{width}}"
            for field, width in fields
        )
        thin = (host[left]["sessions"] < MIN_DELTA_SESSIONS
                or host[right]["sessions"] < MIN_DELTA_SESSIONS)
        note = "  n<3" if thin else ""
        print(f"{label:<16}{'':>{blank_before}}{cells}{note}")
    if not any_pair:
        print("delta: n/a (one side has no sessions)")


def report(cfg: Config) -> int:
    try:
        hosts, notes = scan_hosts(cfg)
    except _dcio.DcError as exc:
        return exc.code

    static = {} if cfg.no_static else static_block(cfg)
    scanned = sum(host["sessions_scanned"] for host in hosts.values())
    unclassified = sum(host["unclassified"]["sessions"] for host in hosts.values())

    if cfg.as_json:
        payload = {
            "buckets": list(BUCKETS),
            "hosts": hosts,
            "unclassified_sessions": unclassified,
            "notes": notes,
        }
        if static:
            payload["static"] = static
        print(json.dumps(payload, indent=2, sort_keys=True))
        return _dcio.EXIT_OK

    if not hosts and not static:
        print("DRAINCLAMP: no transcript hosts configured")
        return _dcio.EXIT_OK

    parts = [f"{name}={host['sessions_scanned']}" for name, host in hosts.items()]
    print(f"DRAINCLAMP: token report  {'  '.join(parts) or 'no hosts'}  "
          f"unclassified={unclassified}")
    for note in notes:
        print(f"note: {note}")
    if static:
        print_static(static)
    if not scanned:
        print("DRAINCLAMP: no transcripts with usage records")
    for name, host in hosts.items():
        print()
        print_usage_host(name, host)
    print()
    print("note: observational, not a controlled comparison -- the sets are "
          "different tasks, and the skill is loaded for the harder ones.")
    return _dcio.EXIT_OK


def parse_args(argv: list[str] | None = None) -> Config:
    parser = argparse.ArgumentParser(prog="dc_tokens.py", description=__doc__)
    parser.add_argument("--claude", default=None,
                        help="Claude transcript root (default ~/.claude/projects)")
    parser.add_argument("--transcripts", default=None,
                        help="alias for --claude")
    parser.add_argument("--codex", default=None,
                        help="Codex session root (default ~/.codex/sessions)")
    parser.add_argument("--grok", default=None,
                        help="Grok session root (default ~/.grok/sessions)")
    parser.add_argument("--base-skill", default=None,
                        help="path to the base skill dir (SKILL.md)")
    parser.add_argument("--adhd-skill", default=None,
                        help="path to the ADHD skill dir (SKILL.md)")
    parser.add_argument("--no-static", action="store_true",
                        help="skip the static skill-size table")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    claude_arg = args.claude or args.transcripts
    any_host = any((claude_arg, args.codex, args.grok))
    cfg = Config(
        claude=Path(claude_arg).expanduser().resolve() if claude_arg
        else (None if any_host else default_claude()),
        codex=Path(args.codex).expanduser().resolve() if args.codex
        else (None if any_host else default_codex()),
        grok=Path(args.grok).expanduser().resolve() if args.grok
        else (None if any_host else default_grok()),
        base_skill=Path(args.base_skill).expanduser().resolve() if args.base_skill
        else default_base_skill(),
        adhd_skill=Path(args.adhd_skill).expanduser().resolve() if args.adhd_skill
        else default_adhd_skill(),
        as_json=args.json,
        no_static=args.no_static,
        claude_explicit=bool(claude_arg),
        codex_explicit=bool(args.codex),
        grok_explicit=bool(args.grok),
    )
    return cfg


def main() -> int:
    return report(parse_args())


if __name__ == "__main__":
    sys.exit(main())
