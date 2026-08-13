#!/usr/bin/env python3
"""On-demand token report: sessions with the skill loaded vs sessions without.

No host exposes live context usage to a model -- that is why Gate 5's
`--context-high` is an assertion rather than a measurement. Transcripts on disk
are the one place the real numbers exist, so this reads them after the fact.

It is deliberately not part of any gate and is never referenced from SKILL.md:
a measurement tool that costs context every invocation would be measuring a
problem it had joined. Run it when you want the number.

Output is aggregate only. Transcript text is never printed -- these files hold
whole working sessions, and a summary must not become a way to leak one.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import _dcio

DEFAULT_MARKER = "drainclamp"
USAGE_FIELDS = (
    "input_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "output_tokens",
)


def transcript_root(override: str | None = None) -> Path:
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / ".claude" / "projects"


class Bucket:
    """Running totals for one side of the comparison."""

    def __init__(self) -> None:
        self.sessions = 0
        self.turns = 0
        self.input = 0
        self.created = 0
        self.read = 0
        self.output = 0

    def add(self, stats: dict) -> None:
        self.sessions += 1
        self.turns += stats["turns"]
        self.input += stats["input_tokens"]
        self.created += stats["cache_creation_input_tokens"]
        self.read += stats["cache_read_input_tokens"]
        self.output += stats["output_tokens"]

    @property
    def context_per_turn(self) -> float:
        """Whole prompt seen per turn, cached or not -- the context bill."""
        return (self.input + self.created + self.read) / self.turns if self.turns else 0.0

    @property
    def fresh_per_turn(self) -> float:
        """Tokens that were not a cache hit -- the part that costs full price."""
        return (self.input + self.created) / self.turns if self.turns else 0.0

    @property
    def output_per_turn(self) -> float:
        return self.output / self.turns if self.turns else 0.0


def scan(path: Path, marker: str) -> dict | None:
    """Totals for one transcript, or None when it holds no usage records."""
    stats = {k: 0 for k in USAGE_FIELDS}
    stats["turns"] = 0
    stats["marked"] = False
    lowered = marker.casefold()
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if lowered in line.casefold():
                    stats["marked"] = True
                try:
                    record = json.loads(line)
                except ValueError:
                    continue  # a partial trailing line in a live session
                message = record.get("message")
                if not isinstance(message, dict):
                    continue
                usage = message.get("usage")
                if not isinstance(usage, dict):
                    continue
                stats["turns"] += 1
                for field in USAGE_FIELDS:
                    value = usage.get(field)
                    if isinstance(value, int):
                        stats[field] += value
    except OSError:
        return None
    return stats if stats["turns"] else None


def num(value: float) -> str:
    return f"{value:,.0f}"


def delta(on: float, off: float) -> str:
    if not off:
        return "n/a"
    return f"{(on - off) / off * 100:+.1f}%"


def report(root: Path, marker: str, as_json: bool) -> int:
    if not root.is_dir():
        print(f"ERROR: no transcript directory at {root}", file=sys.stderr)
        return _dcio.EXIT_MISSING_REQUIRED

    on, off = Bucket(), Bucket()
    scanned = 0
    for path in sorted(root.glob("*/*.jsonl")):
        stats = scan(path, marker)
        if stats is None:
            continue
        scanned += 1
        (on if stats["marked"] else off).add(stats)

    if as_json:
        print(json.dumps({
            "marker": marker,
            "sessions_scanned": scanned,
            "with_skill": vars(on),
            "without_skill": vars(off),
        }, indent=2, sort_keys=True))
        return _dcio.EXIT_OK

    if not scanned:
        print(f"DRAINCLAMP: no transcripts with usage records under {root}")
        return _dcio.EXIT_OK

    print(f"DRAINCLAMP: token usage by session, marker={marker!r}, "
          f"{scanned} session(s) scanned")
    head = f"{'':<16}{'sessions':>9}{'turns':>8}{'context/turn':>14}" \
           f"{'fresh/turn':>12}{'output/turn':>13}"
    print(head)
    for label, bucket in (("with skill", on), ("without skill", off)):
        print(f"{label:<16}{bucket.sessions:>9}{bucket.turns:>8}"
              f"{num(bucket.context_per_turn):>14}"
              f"{num(bucket.fresh_per_turn):>12}"
              f"{num(bucket.output_per_turn):>13}")
    if on.turns and off.turns:
        print(f"{'delta':<16}{'':>9}{'':>8}"
              f"{delta(on.context_per_turn, off.context_per_turn):>14}"
              f"{delta(on.fresh_per_turn, off.fresh_per_turn):>12}"
              f"{delta(on.output_per_turn, off.output_per_turn):>13}")
    else:
        print("delta: n/a (one side has no sessions)")

    print("note: observational, not a controlled comparison -- the two sets are "
          "different tasks, and the skill is loaded for the harder ones.")
    return _dcio.EXIT_OK


def main() -> int:
    parser = argparse.ArgumentParser(prog="dc_tokens.py", description=__doc__)
    parser.add_argument("--transcripts", default=None,
                        help="transcript directory (default ~/.claude/projects)")
    parser.add_argument("--marker", default=DEFAULT_MARKER,
                        help=f"substring marking a skill-loaded session "
                             f"(default {DEFAULT_MARKER!r})")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    return report(transcript_root(args.transcripts), args.marker, args.json)


if __name__ == "__main__":
    sys.exit(main())
