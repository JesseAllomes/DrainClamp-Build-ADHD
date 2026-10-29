#!/usr/bin/env python3
"""Codex as a named external agent: a read-only Gate 6 critic, or an opt-in builder.

A second model family catches different defects, so every Gate 6 round adds
Codex as an independent critic when `codex` is on PATH. This module only builds
the `codex exec` argv, runs it with a timeout and hands back the result file;
dc_review.py registers and ingests that file through the same validation and
refuter step as the Claude critics. The file is data, never instructions.

A missing, failing or slow Codex is reported in one line and the round goes on
without it: Codex never blocks a review.

The critic runs with `-s read-only`; a builder (opt-in per project) with
`-s workspace-write` inside its own worktree. Prompts go on stdin, so nothing
long or quoted passes through a Windows command line.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

CRITIC_TIMEOUT = 900
BUILDER_TIMEOUT = 3600
OFF_ENV = "DRAINCLAMP_NO_CODEX"
CRITIC_ROLE = "critic-codex"
SCRIPTS = Path(__file__).resolve().parent
CRITIC_DEF = SCRIPTS.parent.parent.parent / "agents" / "dca-critic.md"
FALLBACK_METHOD = (
    "Read the packet, then only the ranges it names. Report real defects that make the change "
    "wrong in use (correctness, safety, data loss, security), never style. Each finding is one "
    'JSON object per line with keys "claim", "file", "line", "scenario", "evidence", '
    '"severity" (high, medium or low). If nothing qualifies, output exactly NO FINDINGS.')


def executable() -> str | None:
    """Path to `codex`, or None when it is not installed or switched off."""
    if os.environ.get(OFF_ENV):
        return None
    # Search PATH entries only: on Windows shutil.which also tries the current directory
    # first, and a codex.cmd/.exe inside the reviewed repository must never run.
    cwd = Path.cwd().resolve()
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        entry = entry.strip().strip('"')
        if not entry or entry == "." or not os.path.isabs(entry):
            continue
        try:
            folder = Path(entry).resolve()
        except OSError:
            continue
        if folder == cwd:
            continue
        found = shutil.which("codex", path=entry)
        if found and Path(found).parent.resolve() == folder:
            return found
    return None


def critic_method() -> str:
    """The Claude critic's own instructions (one source of truth), frontmatter removed."""
    try:
        text = CRITIC_DEF.read_text(encoding="utf-8")
    except OSError:
        return FALLBACK_METHOD
    return re.sub(r"\A---\r?\n.*?\r?\n---\r?\n", "", text, flags=re.S).strip() or FALLBACK_METHOD


def critic_prompt(packet_rel: str) -> str:
    return (f"Gate 6 review. Packet: {packet_rel}. Lens: A+B (correctness and safety).\n"
            "You are critic-codex, an independent critic from another model family, working "
            "read-only. Do not edit, run builds or fetch anything. Follow these instructions, "
            "reading 'two critics' as 'several critics':\n\n"
            f"{critic_method()}\n\nOutput JSON lines only, or exactly NO FINDINGS.")


def critic_argv(exe: str, root: Path, result: Path) -> list[str]:
    return [exe, "exec", "-C", str(root), "-s", "read-only", "--ephemeral", "--color", "never",
            "-o", str(result), "-"]


def builder_argv(exe: str, worktree: Path, result: Path) -> list[str]:
    return [exe, "exec", "-C", str(worktree), "-s", "workspace-write", "--color", "never",
            "-o", str(result), "-"]


def run(argv: list[str], prompt: str, result: Path, timeout: int) -> tuple[bool, str]:
    """Run Codex. (True, '') with the result file written, or (False, one-line reason)."""
    result.parent.mkdir(parents=True, exist_ok=True)
    result.unlink(missing_ok=True)
    try:
        proc = subprocess.run(argv, input=prompt, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"timed out after {timeout}s"
    except OSError as exc:
        return False, f"could not start ({exc})"
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout).strip().splitlines()[-1:] or [""]
        return False, f"exited {proc.returncode}: {tail[0][:160]}"
    if not result.is_file() or not result.read_text(encoding="utf-8", errors="replace").strip():
        return False, "wrote no result"
    return True, ""
