#!/usr/bin/env python3
"""Advisory symbol index for DrainClamp & Build.

The index is a navigation aid. It is never proof that a symbol does not exist:
every response carries `COVERAGE: partial`.

Two engines, and the difference is stated rather than smoothed over: Python is
parsed with the stdlib `ast` module, JavaScript with a bounded line-oriented
regex scanner that is explicitly partial. Every other extension is reported
`UNSUPPORTED` so the caller reads it directly instead of assuming the file is
empty. Adding a grammar bumps `MAP_SCHEMA` — never an implicit parser
substitution over rows a different engine wrote.
"""

from __future__ import annotations

import argparse
import ast
import os
import re
import sys
from pathlib import Path

import _dcio
from _dcio import DcError

MAP_NAME = "map.tsv"
# Schema 2 added the JavaScript scanner. Bumping it invalidates every cache
# written by schema 1, which is the point: a grammar change must not be an
# implicit parser substitution over rows someone else's engine produced.
MAP_SCHEMA = 2
PAGE_SIZE = 120
PYTHON_EXT = {".py", ".pyi"}
JS_EXT = {".js", ".jsx", ".mjs", ".cjs"}
SUPPORTED = PYTHON_EXT | JS_EXT
SKIP_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", "dist", "build",
    ".tmp", ".python_packages", ".agent", ".mypy_cache", ".pytest_cache", ".ruff_cache",
}
# Anything with source in it that we cannot parse yet. Reported, not hidden.
SOURCEY = {
    ".ts", ".tsx", ".go", ".rs", ".java", ".rb",
    ".cs", ".c", ".h", ".cpp", ".hpp", ".php", ".swift", ".kt", ".scala", ".sh",
    ".ps1", ".sql", ".lua", ".pl", ".r", ".m",
}

OK, UNSUPPORTED, PARSE_FAILED = "OK", "UNSUPPORTED", "PARSE-FAILED"


def is_link(path: Path) -> bool:
    """Symlink, or a Windows junction — both leave the tree if followed.

    `Path.is_symlink()` is False for a junction, so a junction-only check would
    let a linked directory be walked as if it were part of the repository.
    """
    try:
        if path.is_symlink():
            return True
    except OSError:
        return False
    checker = getattr(os.path, "isjunction", None)
    if checker is None:
        return False
    try:
        return bool(checker(path))
    except OSError:
        return False


def walk(root: Path, skipped: list[str] | None = None):
    """Cheap metadata walk. Directory symlinks are not followed.

    A caller that reports coverage passes `skipped` to collect the linked
    directories that were not descended into — an unreported skip would let a
    bounded total look like a complete one.
    """
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            name = entry.name
            try:
                if entry.is_dir():
                    if name in SKIP_DIRS:
                        continue
                    if is_link(entry):
                        if skipped is not None:
                            skipped.append(entry.relative_to(root).as_posix())
                        continue
                    stack.append(entry)
                    continue
                if is_link(entry):
                    continue
                suffix = entry.suffix.lower()
                if suffix in SUPPORTED or suffix in SOURCEY:
                    stat = entry.stat()
                    yield entry, stat.st_mtime_ns, stat.st_size
            except OSError:
                continue


def parse_python(path: Path) -> tuple[str, list[tuple[str, int, int]]]:
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return PARSE_FAILED, [("<unreadable: %s>" % exc.strerror, 1, 1)]
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return PARSE_FAILED, []

    symbols: list[tuple[str, int, int]] = []

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = f"{prefix}{child.name}"
                end = getattr(child, "end_lineno", None) or child.lineno
                symbols.append((name, child.lineno, end))
                if isinstance(child, ast.ClassDef):
                    visit(child, f"{name}.")

    visit(tree, "")
    return OK, symbols


# --------------------------------------------------------------------------
# JavaScript — a bounded line scanner, and explicitly partial
#
# There is no stdlib JavaScript grammar and no new dependency is permitted, so
# this reads lines, not syntax. It finds declarations at the shapes below and
# brackets them by brace depth. A brace inside an unusual template literal or a
# regex literal can therefore push an end line out. That is why every response
# carries `COVERAGE: partial` and why the index is never proof a symbol is
# absent — it is a navigation aid, and for JavaScript a rougher one.
# --------------------------------------------------------------------------

_JS_FUNCTION = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*"
    r"([A-Za-z_$][\w$]*)\s*\(")
_JS_CLASS = re.compile(r"^\s*(?:export\s+)?(?:default\s+)?class\s+([A-Za-z_$][\w$]*)")
_JS_ASSIGNED = re.compile(
    r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*"
    r"(?:async\s*)?(?:function\b|\([^)]*\)\s*=>|[A-Za-z_$][\w$]*\s*=>)")
_JS_METHOD = re.compile(
    r"^\s*(?:static\s+)?(?:async\s+)?(?:get\s+|set\s+)?\*?\s*"
    r"([A-Za-z_$][\w$]*)\s*\([^;]*\)\s*\{")
_JS_KEYWORDS = {"if", "for", "while", "switch", "catch", "return", "function",
                "constructor", "do", "else", "try"}
_JS_STRINGS = re.compile(r"'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"|`(?:\\.|[^`\\])*`")


def _js_code_only(line: str, in_block: bool) -> tuple[str, bool]:
    """Strip strings and comments so brace counting sees code only."""
    if in_block:
        end = line.find("*/")
        if end == -1:
            return "", True
        line, in_block = line[end + 2:], False
    line = _JS_STRINGS.sub('""', line)
    start = line.find("/*")
    while start != -1:
        end = line.find("*/", start + 2)
        if end == -1:
            return line[:start], True
        line = line[:start] + " " + line[end + 2:]
        start = line.find("/*")
    slashes = line.find("//")
    if slashes != -1:
        line = line[:slashes]
    return line, in_block


def parse_javascript(path: Path) -> tuple[str, list[tuple[str, int, int]]]:
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return PARSE_FAILED, [(f"<unreadable: {exc.strerror}>", 1, 1)]

    lines = source.splitlines()
    symbols: list[tuple[str, int, int]] = []
    open_frames: list[dict] = []  # declarations whose closing brace is pending
    class_stack: list[dict] = []
    depth = 0
    in_block = False

    for number, raw in enumerate(lines, 1):
        code, in_block = _js_code_only(raw, in_block)
        name: str | None = None
        is_class = False

        match = _JS_CLASS.search(code)
        if match:
            name, is_class = match.group(1), True
        else:
            match = _JS_FUNCTION.search(code) or _JS_ASSIGNED.search(code)
            if match:
                name = match.group(1)
            elif class_stack and class_stack[-1]["depth"] < depth:
                match = _JS_METHOD.search(code)
                if match and match.group(1) not in _JS_KEYWORDS:
                    name = f"{class_stack[-1]['name']}.{match.group(1)}"

        pushed = False
        if name:
            open_frames.append({"name": name, "start": number, "depth": depth})
            pushed = True
            if is_class:
                class_stack.append({"name": name, "depth": depth})

        opened, closed = code.count("{"), code.count("}")
        depth += opened
        # An expression body (`const add = (a, b) => a + b;`) opens no block, so
        # it closes on its own line rather than waiting for someone else's brace.
        if pushed and opened == 0 and (";" in code or "=>" in code):
            frame = open_frames.pop()
            symbols.append((frame["name"], frame["start"], number))
            if is_class:
                class_stack.pop()
        depth -= closed
        while open_frames and depth <= open_frames[-1]["depth"]:
            frame = open_frames.pop()
            symbols.append((frame["name"], frame["start"], number))
        while class_stack and depth <= class_stack[-1]["depth"]:
            class_stack.pop()

    last = len(lines)
    while open_frames:  # unterminated: report to the end rather than drop it
        frame = open_frames.pop()
        symbols.append((frame["name"], frame["start"], last))

    symbols.sort(key=lambda row: (row[1], row[0]))
    return OK, symbols


def classify(path: Path) -> tuple[str, list[tuple[str, int, int]]]:
    suffix = path.suffix.lower()
    if suffix in PYTHON_EXT:
        return parse_python(path)
    if suffix in JS_EXT:
        return parse_javascript(path)
    return UNSUPPORTED, []


# --------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------


def cache_path(root: Path) -> Path:
    return _dcio.agent_dir(root) / MAP_NAME


def load_cache(path: Path) -> tuple[dict, dict[str, list[list]]]:
    """Return (header, {relpath: [key, status, [symbol rows]]})."""
    text = _dcio.read_text(path)
    if text is None:
        return {}, {}
    header: dict[str, str] = {}
    records: dict[str, list] = {}
    for line in text.splitlines():
        if line.startswith("#"):
            if "=" in line:
                key, _, value = line[1:].strip().partition("=")
                header[key.strip()] = value.strip()
            continue
        # A cache written by a different grammar is not ours to reinterpret.
        if header.get("schema") != str(MAP_SCHEMA):
            return header, {}
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        rel, key, status = parts[0], parts[1], parts[2]
        entry = records.setdefault(rel, [key, status, []])
        if len(parts) >= 6 and parts[3]:
            entry[2].append([parts[3], int(parts[4]), int(parts[5])])
    return header, records


def render_cache(root: Path, records: dict[str, list], generated: str) -> str:
    lines = [
        f"# schema={MAP_SCHEMA}",
        f"# root={root.as_posix()}",
        f"# generated={generated}",
        "# COVERAGE: partial",
        "# engine=python:ast; javascript:regex(partial); others=UNSUPPORTED",
    ]
    for rel in sorted(records):
        key, status, symbols = records[rel]
        if not symbols:
            lines.append(f"{rel}\t{key}\t{status}\t\t\t")
            continue
        for name, start, end in symbols:
            lines.append(f"{rel}\t{key}\t{status}\t{name}\t{start}\t{end}")
    return "\n".join(lines) + "\n"


def refresh(root: Path) -> dict[str, list]:
    """Metadata walk; reparse only changed or new files, drop deleted ones."""
    path = cache_path(root)
    _, cached = load_cache(path)
    fresh: dict[str, list] = {}
    for file_path, mtime_ns, size in walk(root):
        rel = file_path.relative_to(root).as_posix()
        key = f"{mtime_ns}:{size}"
        previous = cached.get(rel)
        if previous and previous[0] == key:
            fresh[rel] = previous
            continue
        status, symbols = classify(file_path)
        fresh[rel] = [key, status, [[n, s, e] for n, s, e in symbols]]
    from datetime import datetime, timezone

    stamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    _dcio.atomic_write(path, render_cache(root, fresh, stamp))
    return fresh


# --------------------------------------------------------------------------
# Query
# --------------------------------------------------------------------------


def rows_for(records: dict[str, list], path_filter: str | None,
             symbol_filter: str | None) -> tuple[list[str], list[str]]:
    """Return (symbol lines, note lines)."""
    lines: list[str] = []
    notes: list[str] = []
    for rel in sorted(records):
        if path_filter and not rel.startswith(path_filter.replace("\\", "/").lstrip("./")):
            continue
        _, status, symbols = records[rel]
        if status == UNSUPPORTED:
            if not symbol_filter:
                notes.append(f"{rel}\tUNSUPPORTED\tno grammar for this extension; read directly")
            continue
        if status == PARSE_FAILED:
            if not symbol_filter:
                notes.append(f"{rel}\tPARSE-FAILED\tsupported syntax but malformed; read directly")
            continue
        for name, start, end in symbols:
            if symbol_filter and symbol_filter.lower() not in name.lower():
                continue
            lines.append(f"{rel}:{start}-{end}\t{name}")
    return lines, notes


def main() -> int:
    parser = argparse.ArgumentParser(prog="dc_map.py", description=__doc__)
    parser.add_argument("root", nargs="?", default=None)
    parser.add_argument("--root", dest="root_flag", default=None,
                        help="same as the positional root; accepted so every "
                             "dc_*.py script takes --root")
    parser.add_argument("--path", dest="path_filter")
    parser.add_argument("--symbol", dest="symbol_filter")
    parser.add_argument("--page", type=int, default=1)
    parser.add_argument("--no-refresh", action="store_true")
    parser.add_argument("--lang", default="auto")
    args = parser.parse_args()

    if args.lang != "auto":
        raise DcError("only --lang auto is supported at schema 1")
    if args.page < 1:
        raise DcError("--page is 1-based")
    if args.root_flag is not None and args.root is not None:
        raise DcError("give the root once: positionally or as --root, not both")

    root = _dcio.repo_root(args.root_flag or args.root)

    if args.no_refresh:
        header, records = load_cache(cache_path(root))
        if not records:
            raise DcError("no cached index; run dc_map.py without --no-refresh first")
        print("CACHE: unvalidated")
    else:
        records = refresh(root)

    print("COVERAGE: partial")

    lines, notes = rows_for(records, args.path_filter, args.symbol_filter)
    total = len(lines)
    start = (args.page - 1) * PAGE_SIZE
    page = lines[start:start + PAGE_SIZE]

    if total > PAGE_SIZE:
        print(f"SHOWING {len(page)}/{total} (page {args.page})")
    for line in page:
        print(line)

    if notes:
        shown = notes[:20]
        print(f"NOT INDEXED: {len(notes)} file(s)")
        for note in shown:
            print(note)
        if len(notes) > len(shown):
            print(f"SHOWING {len(shown)}/{len(notes)} not-indexed files")

    if total == 0 and not notes:
        print("no symbols matched; the index is partial, so read directly to confirm")
    return _dcio.EXIT_OK


if __name__ == "__main__":
    _dcio.run_cli(main)
