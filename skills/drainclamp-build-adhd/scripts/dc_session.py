#!/usr/bin/env python3
"""Session entry: which project is this, before any gate runs.

Invoking the skill inside a repository that already holds DrainClamp state used
to resume it silently, so a session begun for one purpose continued another
agent's work without ever saying so. This script makes that a question.

It prints a menu and exits. It selects nothing, runs no gate, and writes
nothing unless asked to change a project's status -- the choice is the user's,
and a script that guessed it would reintroduce the bug it exists to fix.

One exception, a cache: for a group repo it rebuilds `.agent/drainclamp-group.md` at the git
top level (one line per project) so a cold start can read one file.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

import _dcio
import dc_registry
import dc_state
from _dcio import DcError


def progress(root: str) -> str:
    """`n/N done` from the project's roadmap, or '' when it cannot be read."""
    try:
        text = _dcio.read_text(dc_registry.state_file(root))
    except OSError:
        # The registry is only a cache. A project can outlive the permissions
        # of the host rendering this menu (notably a Windows sandbox ACL), so
        # inaccessible progress must not make the session gate unusable.
        return ""
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


def review(root: str) -> str:
    """`review: n blocking` while Gate 6 findings hold the project, else ''."""
    try:
        n = dc_state.review_blocking(Path(root))
    except OSError:
        # Same tolerance as progress(): the flag is a hint, never a gate.
        return ""
    return f"review: {n} blocking" if n else ""


def row(index: int, entry: dict) -> str:
    detail = ", ".join(d for d in (progress(entry["root"]), review(entry["root"])) if d)
    suffix = f", {detail}" if detail else ""
    name = entry.get("name")
    # The full path tells two same-named checkouts apart and says where the
    # session will resume, so it is printed even when the name is present.
    label = f"{name} — {entry['root']}" if name else entry["root"]
    return f"({index}) - {label} (last worked {day(entry)}{suffix})"


def print_rows(rows: list[dict]) -> None:
    """Numbered rows, with a group heading above each run of one group's projects.

    Numbers run straight through, so a heading never changes what `--complete N`
    resolves to. Entries with no group print exactly as before.
    """
    group = None
    for i, entry in enumerate(rows, start=1):
        if entry.get("group") != group:
            group = entry.get("group")
            if group:
                print(f"  [{group}]")
        print("  " + ("  " if group else "") + row(i, entry))


GROUP_FILE = "drainclamp-group.md"


def project_line(entry: dict) -> str:
    """`name | status | done/total | last log day` from the project's own state."""
    root = entry["root"]
    done = total = 0
    last = "none"
    try:
        text = _dcio.read_text(dc_registry.state_file(root))
        if text is not None:
            state = dc_state.State.parse(text)
            rows = dc_state.parse_roadmap(state.sections["ROADMAP"])
            done, total = sum(1 for r in rows if r.get("status") == "done"), len(rows)
            logs = dc_state.log_lines(state.sections["LOG"])
            if logs:
                found = re.match(r"-\s+(\d{4}-\d{2}-\d{2})", logs[-1].strip())
                last = found.group(1) if found else "unknown"
    except (OSError, DcError, KeyError, ValueError):
        pass  # a cache line is a hint; an unreadable state shows as empty
    return f"- {entry.get('name') or root} | {entry.get('status', 'active')} | {done}/{total} | last log {last}"


def write_group_overviews(entries: list[dict]) -> list[Path]:
    """Rebuild `<top level>/.agent/drainclamp-group.md` for each group repo in `entries`.

    One line per registered project, so a cold start reads one file instead of every
    project's state. Read-only cache, never an authority; rewritten only when the
    text changes, and a repo that cannot be written is skipped.
    """
    by_top: dict[Path, list[dict]] = {}
    for entry in entries:
        if entry.get("group"):
            by_top.setdefault(_dcio.repo_root(entry["root"]), []).append(entry)
    written: list[Path] = []
    for top, rows in sorted(by_top.items(), key=lambda kv: str(kv[0])):
        lines = ["# DrainClamp group overview",
                 "Cache rebuilt on Gate S from each project's state; the state files are the authority.",
                 ""]
        lines += [project_line(e) for e in sorted(rows, key=lambda e: e.get("name") or "")]
        text = "\n".join(lines) + "\n"
        path = top / _dcio.AGENT_DIR_NAME / GROUP_FILE
        try:
            if _dcio.read_text(path) == text:
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            _dcio.atomic_write(path, text)
            written.append(path)
        except OSError:
            continue
    return written


def menu(home: str | None) -> int:
    active = dc_registry.listing("active", home)
    complete = dc_registry.listing("complete", home)
    write_group_overviews(active + complete)

    # Every active project is listed: a capped menu made the user re-run with
    # --all to reach a project the cap hid, which is a second question.
    print("DRAINCLAMP: what are you working on now?")
    print_rows(active)
    if not active:
        print("  (no active projects)")

    # Action numbers continue past the projects, so they never collide with one.
    n = len(active)
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


def gate0_lines(root: str) -> list[str]:
    """Gate 0 freshness for `root`: dc_audit's own first line and its GATE3 line."""
    try:
        proc = subprocess.run(
            [sys.executable, "-B", str(Path(__file__).resolve().parent / "dc_audit.py"), "--root", root],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
    except subprocess.TimeoutExpired:
        return ["AUDIT: failed (timed out after 300s)"]
    except OSError as exc:
        return [f"AUDIT: failed ({str(exc)[:120]})"]
    out = [ln for ln in proc.stdout.splitlines() if ln.startswith(("AUDIT:", "CACHE:", "GATE3:"))]
    if proc.returncode != 0 or not out:
        tail = (proc.stderr.strip().splitlines() or ["no output"])[-1]
        return [f"AUDIT: failed ({tail[:120]})"]
    return out


def three_lines(root: Path, data: dict) -> list[str]:
    """`Done:` / `In progress:` / `Next:` from the roadmap and the sidecar chunks."""
    import dc_project  # deferred: it imports this package's siblings
    try:
        rows = dc_project.roadmap(root)
    except (OSError, DcError, KeyError, ValueError):
        rows = []
    done = [r["id"] for r in rows if r["status"] == "done"]
    active = next((r for r in rows if r["status"] == "active"), None)
    ms = dc_project.current_milestone(rows)
    chunks = data["chunks"].get(ms["id"], []) if ms else []
    nxt = next((c for c in chunks if not c.get("done")), None)
    if ms is None:
        nxt_text = "no startable milestone; check the roadmap" if rows else "no roadmap yet; run Gate 2"
    elif nxt is not None:
        nxt_text = f"{ms['id']}/{nxt['id']} {dc_project._clip(nxt['goal'])}"
    elif chunks:
        nxt_text = f"close {ms['id']}"
    else:
        nxt_text = f"split {ms['id']} into chunks ({dc_project._clip(ms['goal'])})"
    prog = f"{active['id']}: {dc_project._clip(active['goal'])}" if active else "none"
    return [f"Done: {', '.join(done) if done else 'nothing yet'}",
            f"In progress: {prog}",
            f"Next: {nxt_text}"]


def resume(home: str | None, choice: str) -> int:
    """Gate S choice -> one command: audit freshness, ROOT line, 3 lines, capsule, time, registry."""
    import dc_project
    entry = resolve(dc_registry.listing("active", home), choice, "active")
    root = Path(entry["root"])
    if not dc_registry.state_file(root).is_file() and not (root / _dcio.PROJECT_MARKER).is_file():
        raise DcError(f"not a DrainClamp project (no state or marker): {root}")
    for line in gate0_lines(str(root)):
        print(line)
    print(f"ROOT: {entry['root']}")
    print(f"Pass --root {entry['root']} to every DrainClamp script this session.")
    agent = root / _dcio.AGENT_DIR_NAME
    data = dc_project.load(agent)
    print("\n".join(three_lines(root, data)))
    print("\n".join(dc_project.capsule(root, data, home) + dc_project.sibling_lines(root)))
    dc_project.mutate(agent, dc_project.touch_time)
    dc_registry.touch(root, home)
    return _dcio.EXIT_OK


def main() -> int:
    parser = argparse.ArgumentParser(prog="dc_session.py", description=__doc__)
    parser.add_argument("--home", default=None, help="registry directory override")
    parser.add_argument("--complete", help="menu number or path to mark complete")
    parser.add_argument("--resume", help="menu number or path: audit, ROOT, Done/In progress/Next, capsule")
    parser.add_argument("--reopen", help="menu number or path to reopen")
    parser.add_argument("--list-complete", action="store_true",
                        help="numbered list of completed projects")
    parser.add_argument("--register", help="add a repository to the registry")
    parser.add_argument("--forget", help="menu number or path to drop from the index")
    parser.add_argument("--all", action="store_true",
                        help="accepted for old callers; every project is always listed")
    parser.add_argument("--json", action="store_true", help="machine-readable listing")
    args = parser.parse_args()

    if args.register:
        target = Path(args.register).expanduser()
        if dc_registry.throwaway(target, args.home):
            print(f"DRAINCLAMP: not registered: {target.resolve()} is in the temp directory.")
            return _dcio.EXIT_OK
        entry = dc_registry.touch(target, args.home)
        print(f"DRAINCLAMP: registered {entry['name']} ({entry['root']})")
        return _dcio.EXIT_OK

    if args.forget:
        entry = resolve(dc_registry.listing("active", args.home), args.forget, "active")
        dc_registry.forget(entry["root"], args.home)
        print(f"DRAINCLAMP: {entry['name']} dropped from the index. "
              "Its repository and state file are untouched.")
        return _dcio.EXIT_OK

    if args.resume:
        return resume(args.home, args.resume)

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
        print_rows(rows)
        return _dcio.EXIT_OK

    if args.json:
        print(json.dumps({
            "active": dc_registry.listing("active", args.home),
            "complete": dc_registry.listing("complete", args.home),
        }, indent=2, sort_keys=True))
        return _dcio.EXIT_OK

    return menu(args.home)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except DcError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(_dcio.EXIT_INTERNAL)
