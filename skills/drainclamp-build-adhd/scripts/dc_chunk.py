#!/usr/bin/env python3
"""Bounded range reader for DrainClamp & Build.

Two jobs:

1. Resolve a symbol to one contiguous range, refusing to guess when the name is
   ambiguous. Confidently reading the wrong `handle()` is the failure this
   prevents.
2. Advise on read sizing, so a known span is read once rather than split into N
   micro-chunks, and a file that is small or mostly needed is read whole.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import _dcio
import dc_map
from _dcio import DcError

SMALL_FILE_LINES = 120
FULL_READ_FRACTION = 0.50
MAX_CANDIDATES = 20
MAX_GREP_HITS = 40


def line_count(path: Path) -> int:
    try:
        with open(path, "rb") as handle:
            return sum(1 for _ in handle)
    except OSError as exc:
        raise DcError(f"cannot read {path}: {exc.strerror or exc}") from exc


def emit(path: Path, start: int, end: int, root: Path) -> None:
    total = line_count(path)
    start = max(1, start)
    end = min(total, end)
    rel = path.relative_to(root).as_posix() if _dcio.is_within(path, root) else str(path)
    print(f"{rel}:{start}-{end}  ({end - start + 1} of {total} lines)")
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for number, text in enumerate(handle, 1):
            if number < start:
                continue
            if number > end:
                break
            print(f"{number}\t{text.rstrip(chr(10))}")


def advise(path: Path, start: int, end: int) -> str | None:
    """Return a sizing note when a full read would be the better call."""
    total = line_count(path)
    span = end - start + 1
    if total <= SMALL_FILE_LINES:
        return (f"ADVICE: {path.name} is {total} lines; read it in full rather than "
                "in ranges.")
    if span / total >= FULL_READ_FRACTION:
        return (f"ADVICE: requested span is {span}/{total} lines "
                f"({span / total:.0%}); read the file in full instead.")
    return None


def candidates(root: Path, symbol: str, path_filter: str | None) -> list[tuple[str, str, int, int]]:
    """Index rows matching `symbol`, as (rel, name, start, end)."""
    _, records = dc_map.load_cache(dc_map.cache_path(root))
    if not records:
        records = dc_map.refresh(root)
    wanted = symbol.lower()
    hits: list[tuple[str, str, int, int]] = []
    for rel in sorted(records):
        if path_filter and not rel.startswith(path_filter.replace("\\", "/").lstrip("./")):
            continue
        _, status, symbols = records[rel]
        if status != dc_map.OK:
            continue
        for name, start, end in symbols:
            leaf = name.rsplit(".", 1)[-1]
            if wanted in (name.lower(), leaf.lower()):
                hits.append((rel, name, int(start), int(end)))
    return hits


def match_id(rel: str, name: str) -> str:
    return f"{rel}::{name}"


def main() -> int:
    parser = argparse.ArgumentParser(prog="dc_chunk.py", description=__doc__)
    parser.add_argument("file", nargs="?")
    parser.add_argument("start", nargs="?", type=int)
    parser.add_argument("end", nargs="?", type=int)
    parser.add_argument("--root", default=None)
    parser.add_argument("--symbol")
    parser.add_argument("--path", dest="path_filter")
    parser.add_argument("--match", dest="match")
    parser.add_argument("--grep")
    parser.add_argument("--context", type=int, default=3)
    args = parser.parse_args()

    root = _dcio.repo_root(args.root)

    # ---- symbol resolution ------------------------------------------------
    if args.symbol:
        hits = candidates(root, args.symbol, args.path_filter)
        if args.match:
            hits = [h for h in hits if match_id(h[0], h[1]) == args.match]
        if not hits:
            raise DcError(
                f"no indexed symbol named '{args.symbol}'. The index is partial "
                "(COVERAGE: partial), so this is not proof it does not exist — "
                "read the file directly.",
                _dcio.EXIT_CHECK_FAILED,
            )
        if len(hits) > 1:
            print(f"AMBIGUOUS: {len(hits)} symbols named '{args.symbol}'")
            for rel, name, start, end in hits[:MAX_CANDIDATES]:
                print(f"  {match_id(rel, name)}\t{rel}:{start}-{end}")
            if len(hits) > MAX_CANDIDATES:
                print(f"SHOWING {MAX_CANDIDATES}/{len(hits)}")
            print("Re-run with --path <dir> or --match <id>. Refusing to guess.")
            return _dcio.EXIT_CHECK_FAILED
        rel, name, start, end = hits[0]
        target = root / rel
        note = advise(target, start, end)
        if note:
            print(note)
        emit(target, start, end, root)
        return _dcio.EXIT_OK

    if not args.file:
        parser.print_help()
        return _dcio.EXIT_OK

    target = Path(args.file)
    if not target.is_absolute():
        target = root / args.file
    if not target.is_file():
        raise DcError(f"not a file: {args.file}", _dcio.EXIT_CHECK_FAILED)

    # ---- grep -------------------------------------------------------------
    if args.grep:
        pattern = re.compile(args.grep)
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        hits = [i for i, text in enumerate(lines, 1) if pattern.search(text)]
        if not hits:
            print(f"no match for {args.grep!r} in {target.name}")
            return _dcio.EXIT_CHECK_FAILED
        shown = hits[:MAX_GREP_HITS]
        if len(hits) > len(shown):
            print(f"SHOWING {len(shown)}/{len(hits)} matches")
        for hit in shown:
            low = max(1, hit - args.context)
            high = min(len(lines), hit + args.context)
            print(f"--- {target.name}:{low}-{high}")
            for number in range(low, high + 1):
                mark = ">" if number == hit else " "
                print(f"{mark}{number}\t{lines[number - 1]}")
        return _dcio.EXIT_OK

    # ---- explicit range ---------------------------------------------------
    total = line_count(target)
    start = args.start or 1
    end = args.end or total
    if end < start:
        raise DcError("end line precedes start line")
    note = advise(target, start, end)
    if note:
        print(note)
    emit(target, start, end, root)
    return _dcio.EXIT_OK


if __name__ == "__main__":
    _dcio.run_cli(main)
