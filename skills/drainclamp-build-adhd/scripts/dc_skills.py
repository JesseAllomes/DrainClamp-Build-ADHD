#!/usr/bin/env python3
"""Domain skills: load a skill only for the projects that need it.

Skill sources are git repositories named in `~/.drainclamp/config.json`:

    "skill_sources": [{"repo": "C:/path/to/skills-repo", "ref": "origin/main"}]

Skills are read straight from the ref with `git show`, never from the working
tree, so the newest published text is used whatever branch the local clone has
checked out, and the clone is never modified. `--fetch` updates the ref first;
when the network is down the last fetched copy is used and said so.

Gate 1 marks which skills a project needs (`set`), stored in the project
sidecar under `skills`. `dc_project.py next` then names them, and Gate 4 loads
each with `show` before working on code in its domain. A project with none
marked never loads one. Nothing domain-specific ships in this file.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import _dcio
import dc_project
import dc_registry
from _dcio import DcError

DEFAULT_REF = "origin/main"
FETCH_TIMEOUT = 30
GIT_TIMEOUT = 15
DESC_CAP = 110
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
SKIP_DIRS = ("templates/", "template/", "examples/")


def _git(repo: Path, *args: str, timeout: int = GIT_TIMEOUT) -> str:
    try:
        proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DcError(f"git {args[0]} failed in {repo}: {exc}") from None
    if proc.returncode != 0:
        raise DcError(f"git {args[0]} failed in {repo}: {_dcio.collapse(proc.stderr.strip())}")
    return proc.stdout


def sources(home: str | None) -> list[dict]:
    """`skill_sources` from config.json; malformed entries are skipped."""
    text = _dcio.read_text(dc_registry.home_dir(home) / dc_project.CONFIG_NAME)
    try:
        raw = json.loads(text).get("skill_sources") if text else None
    except (ValueError, AttributeError):
        return []
    out = []
    for s in raw if isinstance(raw, list) else []:
        if isinstance(s, dict) and isinstance(s.get("repo"), str) and s["repo"].strip():
            ref = s.get("ref") if isinstance(s.get("ref"), str) and s["ref"].strip() else DEFAULT_REF
            out.append({"repo": Path(s["repo"]).expanduser(), "ref": ref})
    return out


def fetch(src: dict) -> str:
    """Update the source's ref. Returns a warning line, or '' when it worked."""
    remote = src["ref"].split("/", 1)[0] if "/" in src["ref"] else "origin"
    try:
        _git(src["repo"], "fetch", "--quiet", remote, timeout=FETCH_TIMEOUT)
    except DcError as exc:
        return f"WARN: could not fetch {src['repo'].name} ({exc}); using the last fetched {src['ref']}"
    return ""


def frontmatter(text: str) -> dict:
    """name and description from a SKILL.md YAML header (simple scalars only)."""
    m = re.match(r"^---\r?\n(.*?)\r?\n---", text, re.S)
    out = {}
    if not m:
        return out
    for key in ("name", "description"):
        km = re.search(rf"^{key}:\s*(.+)$", m.group(1), re.M)
        if km:
            val = km.group(1).strip()
            if len(val) > 1 and val[0] in "\"'" and val[-1] == val[0]:
                val = val[1:-1]
            out[key] = val.replace("\\n", " ").replace('\\"', '"').strip()
    return out


def _short(desc: str) -> str:
    first = re.split(r"(?<=[.!?])\s", desc, maxsplit=1)[0]
    return first if len(first) <= DESC_CAP else first[:DESC_CAP - 3].rstrip() + "..."


def available(home: str | None, do_fetch: bool = False) -> tuple[list[dict], list[str]]:
    """Every skill in every source: [{name, description, repo, ref, path}], warnings."""
    skills, warns, seen = [], [], {}
    for src in sources(home):
        if do_fetch and (w := fetch(src)):
            warns.append(w)
        try:
            names = [p for p in _git(src["repo"], "ls-tree", "-r", "-z", "--name-only",
                                     src["ref"]).split("\0") if p]
        except DcError as exc:
            warns.append(f"WARN: skipped source {src['repo']}: {exc}")
            continue
        for path in sorted(p for p in names if p.endswith("/SKILL.md") or p == "SKILL.md"):
            if any(path.startswith(d) or f"/{d}" in path for d in SKIP_DIRS):
                continue
            try:
                meta = frontmatter(_git(src["repo"], "show", f"{src['ref']}:{path}"))
            except DcError as exc:
                warns.append(f"WARN: {path}: {exc}")
                continue
            name = meta.get("name", "")
            if not NAME_RE.match(name):
                warns.append(f"WARN: {src['repo'].name}:{path} has no valid name; skipped")
                continue
            if name in seen:
                warns.append(f"WARN: {name} is in {seen[name]} and {src['repo'].name}; "
                             f"the first source wins")
                continue
            seen[name] = src["repo"].name
            skills.append({"name": name, "description": meta.get("description", ""),
                           "repo": src["repo"], "ref": src["ref"], "path": path})
    return skills, warns


def marked(data: dict) -> list[str] | None:
    """The project's marked skills; None when Gate 1 has not marked any yet."""
    block = data.get("skills")
    if not isinstance(block, dict) or not isinstance(block.get("names"), list):
        return None
    return [n for n in block["names"] if isinstance(n, str)]


def capsule_line(data: dict, home: str | None) -> str:
    """One `next` line, or '' when no source is configured and nothing is marked."""
    names = marked(data)
    if names:
        return f"Skills: {', '.join(names)} - load each with `dc_skills.py show <name>`"
    if names is None and sources(home):
        return "Skills: not marked for this project - Gate 1 asks (`dc_skills.py list`)"
    return ""


def _find(home: str | None, name: str, do_fetch: bool) -> tuple[dict, list[str]]:
    skills, warns = available(home, do_fetch)
    for s in skills:
        if s["name"] == name:
            return s, warns
    raise DcError(f"no skill named {name!r} in the configured skill_sources")


def main() -> int:
    p = argparse.ArgumentParser(prog="dc_skills.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--root", default=None)
    p.add_argument("--home", default=None, help="registry/config directory override")
    sub = p.add_subparsers(dest="cmd", required=True)
    ls = sub.add_parser("list", help="skills in every source, and the ones this project marked")
    ls.add_argument("--fetch", action="store_true")
    st = sub.add_parser("set", help="mark the skills this project needs (Gate 1)")
    g = st.add_mutually_exclusive_group(required=True)
    g.add_argument("--skills", help="comma-separated skill names")
    g.add_argument("--none", action="store_true", help="this project needs no domain skill")
    sh = sub.add_parser("show", help="print a skill (or one of its files) from the source ref")
    sh.add_argument("name")
    sh.add_argument("--file", default="SKILL.md", help="path relative to the skill folder")
    sh.add_argument("--fetch", action="store_true")
    args = p.parse_args()

    if args.cmd == "show":
        skill, warns = _find(args.home, args.name, args.fetch)
        rel = Path(args.file).as_posix()
        if rel.startswith("/") or ".." in rel.split("/"):
            raise DcError("--file must stay inside the skill folder")
        base = skill["path"].rsplit("/", 1)[0] if "/" in skill["path"] else ""
        path = f"{base}/{rel}" if base else rel
        for w in warns:
            print(w, file=sys.stderr)
        sys.stdout.write(_git(skill["repo"], "show", f"{skill['ref']}:{path}"))
        return _dcio.EXIT_OK

    root = _dcio.project_root(args.root)
    agent = root / _dcio.AGENT_DIR_NAME
    if args.cmd == "list":
        skills, warns = available(args.home, args.fetch)
        for w in warns:
            print(w)
        if not sources(args.home):
            print("DRAINCLAMP: no skill_sources in config.json; no domain skills to offer.")
            return _dcio.EXIT_OK
        names = marked(dc_project.load(agent))
        for i, s in enumerate(skills, 1):
            flag = "x" if names and s["name"] in names else " "
            print(f"  ({i}) [{flag}] {s['name']} - {_short(s['description'])}")
        print("Marked: " + ("not yet" if names is None else ", ".join(names) or "none needed"))
        return _dcio.EXIT_OK

    names = [] if args.none else [n.strip() for n in args.skills.split(",") if n.strip()]
    if not args.none and not names:
        raise DcError("--skills names no skill; use --none when the project needs none")
    if names:
        known = {s["name"] for s in available(args.home)[0]}
        unknown = [n for n in names if n not in known]
        if unknown:
            raise DcError(f"not in any skill source: {', '.join(unknown)}")

    def apply(data: dict) -> dict:
        data["skills"] = {"names": names,
                          "set_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        return data

    dc_project.mutate(agent, apply)
    print("DRAINCLAMP: skills " + (", ".join(names) if names else "none needed") + " marked.")
    return _dcio.EXIT_OK


if __name__ == "__main__":
    try:
        sys.exit(main())
    except DcError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(_dcio.EXIT_INTERNAL)
