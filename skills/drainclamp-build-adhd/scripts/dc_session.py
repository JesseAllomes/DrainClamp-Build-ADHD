#!/usr/bin/env python3
"""Session entry: which project is this, before any gate runs.

Invoking the skill inside a repository that already holds DrainClamp state used
to resume it silently, so a session begun for one purpose continued another
agent's work without ever saying so. This script makes that a question.

It prints a menu and exits. It selects nothing, runs no gate, and writes
nothing unless asked to change a project's status -- the choice is the user's,
and a script that guessed it would reintroduce the bug it exists to fix.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import _dcio
import dc_registry
import dc_state
from _dcio import DcError


def progress(root: str) -> str:
    """`n/N done` from the project's roadmap, or '' when it cannot be read."""
    text = _dcio.read_text(dc_registry.state_file(root))
    if text is None:
        return ""
    try:
        rows = dc_state.parse_roadmap(dc_state.State.parse(text).sections["ROADMAP"])
    except (DcError, KeyError, ValueError):
        return ""
    if not rows:
        return "no roadmap"
    done = sum(1 for r in rows if r.get("status") == "done")
    return f"{done}/{len(rows)} done"


def day(entry: dict) -> str:
    return (entry.get("last") or "unknown")[:10]


def row(index: int, entry: dict) -> str:
    detail = progress(entry["root"])
    suffix = f", {detail}" if detail else ""
    return f"({index}) - {entry.get('name') or entry['root']} (last worked {day(entry)}{suffix})"


MENU_CAP = 10


def menu(home: str | None, show_all: bool) -> int:
    active = dc_registry.listing("active", home)
    complete = dc_registry.listing("complete", home)

    total = len(active)
    shown = active if show_all else active[:MENU_CAP]

    print("DRAINCLAMP: what are you working on now?")
    for i, entry in enumerate(shown, start=1):
        print("  " + row(i, entry))
    if not active:
        print("  (no active projects)")
    if len(shown) < total:
        # Rule 10: a cap must never read as the whole list. The numbering below
        # continues past the cap, so --all is the only way to reach the rest.
        print(f"  SHOWING {len(shown)}/{total} — re-run with --all for the rest")

    # Action numbers continue past the full count, not the shown count, so a
    # capped menu can never offer "a new project" under a number that already
    # belongs to a project the cap hid.
    n = total
    print(f"  ({n + 1}) - a new project")
    if active:
        print(f"  ({n + 2}) - mark a project complete")
    if complete:
        offset = 3 if active else 2
        print(f"  ({n + offset}) - reopen a completed project ({len(complete)} available)")
    print("Reply with a number. No gate runs until you do.")
    return _dcio.EXIT_OK


def resolve(rows: list[dict], choice: str, status: str) -> dict:
    """A menu index or a path. Indexes are 1-based against the printed order."""
    if choice.isdigit():
        i = int(choice)
        if not 1 <= i <= len(rows):
            raise DcError(
                f"no {status} project numbered {i} (there are {len(rows)}); "
                "re-run the menu, the list may have changed")
        return rows[i - 1]
    entry = dc_registry.find({"projects": rows}, Path(choice).expanduser().resolve())
    if entry is None:
        raise DcError(f"not a {status} project: {choice}")
    return entry


def main() -> int:
    parser = argparse.ArgumentParser(prog="dc_session.py", description=__doc__)
    parser.add_argument("--home", default=None, help="registry directory override")
    parser.add_argument("--complete", help="menu number or path to mark complete")
    parser.add_argument("--reopen", help="menu number or path to reopen")
    parser.add_argument("--list-complete", action="store_true",
                        help="numbered list of completed projects")
    parser.add_argument("--register", help="add a repository to the registry")
    parser.add_argument("--forget", help="menu number or path to drop from the index")
    parser.add_argument("--all", action="store_true", help="show every project, uncapped")
    parser.add_argument("--json", action="store_true", help="machine-readable listing")
    args = parser.parse_args()

    if args.register:
        entry = dc_registry.touch(Path(args.register).expanduser(), args.home)
        print(f"DRAINCLAMP: registered {entry['name']} ({entry['root']})")
        return _dcio.EXIT_OK

    if args.forget:
        entry = resolve(dc_registry.listing("active", args.home), args.forget, "active")
        dc_registry.forget(entry["root"], args.home)
        print(f"DRAINCLAMP: {entry['name']} dropped from the index. "
              "Its repository and state file are untouched.")
        return _dcio.EXIT_OK

    if args.complete:
        entry = resolve(dc_registry.listing("active", args.home), args.complete, "active")
        dc_registry.set_status(entry["root"], "complete", args.home)
        print(f"DRAINCLAMP: {entry['name']} marked complete. "
              "Its state file is untouched and it can be reopened.")
        return _dcio.EXIT_OK

    if args.reopen:
        entry = resolve(dc_registry.listing("complete", args.home), args.reopen, "complete")
        dc_registry.set_status(entry["root"], "active", args.home)
        print(f"DRAINCLAMP: {entry['name']} reopened. Resume from {entry['root']}.")
        return _dcio.EXIT_OK

    if args.list_complete:
        rows = dc_registry.listing("complete", args.home)
        if args.json:
            print(json.dumps(rows, indent=2, sort_keys=True))
            return _dcio.EXIT_OK
        if not rows:
            print("DRAINCLAMP: no completed projects.")
            return _dcio.EXIT_OK
        print("DRAINCLAMP: completed projects")
        for i, entry in enumerate(rows, start=1):
            print("  " + row(i, entry))
        return _dcio.EXIT_OK

    if args.json:
        print(json.dumps({
            "active": dc_registry.listing("active", args.home),
            "complete": dc_registry.listing("complete", args.home),
        }, indent=2, sort_keys=True))
        return _dcio.EXIT_OK

    return menu(args.home, args.all)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except DcError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(_dcio.EXIT_INTERNAL)
