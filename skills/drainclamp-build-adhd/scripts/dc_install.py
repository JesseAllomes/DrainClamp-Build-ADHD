"""Install this skill into each host's skill directory, and never clobber.

A skill directory is shared ground: other people's skills live beside ours, and
a directory named `drainclamp-build-adhd` is not proof that we put it there. So the
installer only ever removes or replaces what it can *prove* it owns:

  * a link is ours when it resolves to this checkout — the strongest evidence
    there is, and it survives the ledger being lost;
  * a copy is ours when the ledger records it and the tree still fingerprints
    to what we wrote. A drifted copy is somebody's edit, so it is refused.

Everything else is reported and left alone. `--force` re-copies a drifted copy
we already own; nothing at all removes a directory we do not own.

Linking is the default because a link tracks the checkout. `--copy` writes a
snapshot instead, and a snapshot goes stale silently, so it is never a fallback
the installer chooses on your behalf.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _dcio  # noqa: E402
import dc_map  # noqa: E402
from _dcio import DcError  # noqa: E402

INSTALL_SCHEMA = 1
# Derived, not hardcoded: this repository is a fork installed alongside the
# baseline, and a constant name would make the two overwrite each other.
SKILL_NAME = Path(__file__).resolve().parent.parent.name
LEDGER_NAME = ".drainclamp-install.json"

MAX_DRIFT_SHOWN = 3


@dataclass(frozen=True)
class Target:
    """One skill directory, and the hosts that read it."""

    key: str
    rel: str
    hosts: tuple[str, ...]
    invocation: str
    agent_rel: str | None = None

    def path(self, home: Path) -> Path:
        return home.joinpath(*self.rel.split("/"))

    def root(self, home: Path) -> Path:
        """The host's own directory — its existence is how we detect the host."""
        return home / self.rel.split("/")[0]

    def agent_path(self, home: Path) -> Path | None:
        if not self.agent_rel:
            return None
        return home.joinpath(*self.agent_rel.split("/"))


# Three directories, three hosts. Claude Code reads `~/.claude/skills/` and
# nothing else — a skill placed only in `~/.agents/skills/` never appears in its
# skill list, which is a silent failure: install reports success and the slash
# command simply does not exist. Grok reads `~/.agents/skills/`; Codex reads
# `~/.codex/skills/`.
#
# Subagents are a separate directory and a separate host feature: Claude Code
# reads `~/.claude/agents/`. No `agent_rel` means the host has no documented
# subagent adapter, and the scout is simply not installed there — inventing a
# path would produce a file no host ever reads.
TARGETS: tuple[Target, ...] = (
    Target("claude", ".claude/skills", ("Claude Code",), "/drainclamp-build-adhd",
           agent_rel=".claude/agents"),
    Target("agents", ".agents/skills", ("Grok",), "/drainclamp-build-adhd"),
    Target("codex", ".codex/skills", ("Codex",), "$drainclamp-build-adhd"),
)

AGENT_FILE = "dca-scout.md"
# Every subagent definition this checkout ships: the scout, plus Gate 6's
# review roster. Each is installed, checked and removed on its own, so one
# hand-edited definition never blocks the others.
AGENT_FILES = (AGENT_FILE, "dca-critic.md", "dca-checker.md", "dca-refuter.md", "dca-fixer.md")

# Classification of whatever currently sits at the install path.
ABSENT = "ABSENT"
LINK_OURS = "LINK-OURS"
LINK_FOREIGN = "LINK-FOREIGN"
LINK_BROKEN = "LINK-BROKEN"
COPY_OURS = "COPY-OURS"
COPY_DRIFTED = "COPY-DRIFTED"
DIR_FOREIGN = "DIR-FOREIGN"
FILE_FOREIGN = "FILE-FOREIGN"

OWNED = {LINK_OURS, COPY_OURS, COPY_DRIFTED}


# --------------------------------------------------------------------------
# Source
# --------------------------------------------------------------------------


def source_skill() -> Path:
    """The skill directory this script lives in, verified to be one."""
    candidate = Path(__file__).resolve().parent.parent
    if not (candidate / "SKILL.md").is_file():
        raise DcError(
            f"{candidate} has no SKILL.md; run dc_install.py from a checkout "
            "of the skill, not from a copy of the script alone",
            _dcio.EXIT_MISSING_REQUIRED,
        )
    return candidate


def source_agent(source: Path, name: str = AGENT_FILE) -> Path:
    """The scout definition, wherever this checkout keeps it.

    Claude Code's plugin loader only reads agents from `agents/` at the plugin
    root, and a path pointing into `skills/` is silently ignored — the plugin
    installs reporting success with zero agents. So the definition lives at the
    repository root. A checkout that predates the move, or a skill directory
    copied out on its own, still has it beside the skill; both are accepted
    rather than making one layout an error.
    """
    root = source.parent.parent / "agents" / name
    return root if root.is_file() else source / "agents" / name


# --------------------------------------------------------------------------
# Links
# --------------------------------------------------------------------------


def real(path: Path) -> Path:
    """Resolve `path` without letting a linked *ancestor* look like a link.

    `realpath` follows every component, so under a symlinked home directory
    every path would appear to point elsewhere. Resolving the parent and then
    appending the name isolates the last component, which is the only one whose
    linkness we are judging.
    """
    return Path(os.path.realpath(str(path.parent))) / path.name


def leaves_tree(path: Path) -> bool:
    """True when following `path` leaves where it appears to be.

    `dc_map.is_link` needs `os.path.isjunction`, which only exists on 3.12+, so
    a junction on an older Python would read as a plain directory — and a plain
    directory is a thing `shutil.rmtree` will happily walk *through*, deleting
    the checkout on the far side. The realpath comparison catches a junction on
    every version, and when it is wrong it is wrong towards refusing.
    """
    if dc_map.is_link(path):
        return True
    try:
        return os.path.realpath(str(path)) != str(real(path))
    except OSError:
        return True


def make_link(link: Path, target: Path) -> str:
    """Create a directory link. Returns the mechanism, or raises DcError.

    Junctions come first on Windows: a symlink there needs developer mode or an
    elevated prompt, a junction needs neither.
    """
    link.parent.mkdir(parents=True, exist_ok=True)
    attempts: list[str] = []

    if os.name == "nt":
        done = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True, text=True,
        )
        if done.returncode == 0 and link.exists():
            return "junction"
        attempts.append(f"junction: {_dcio.collapse(done.stderr or done.stdout, 100)}")

    try:
        os.symlink(str(target), str(link), target_is_directory=True)
        return "symlink"
    except (OSError, NotImplementedError, AttributeError) as exc:
        attempts.append(f"symlink: {exc}")

    raise DcError(
        f"could not link {link}: {'; '.join(attempts)}. "
        "Pass --copy to install a snapshot instead -- it will not track this "
        "checkout, so you must re-run the installer after every update.",
        _dcio.EXIT_CHECK_FAILED,
    )


def remove_link(link: Path) -> None:
    """Unlink a directory link without touching what it points at.

    Windows removes a directory symlink or junction with `rmdir`; POSIX removes
    a symlink with `unlink`. Try both, never `rmtree` — `rmtree` follows a
    junction and deletes the target's contents.
    """
    try:
        os.unlink(str(link))
        return
    except (IsADirectoryError, PermissionError, OSError):
        pass
    os.rmdir(str(link))


# --------------------------------------------------------------------------
# Copies
# --------------------------------------------------------------------------


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def file_digests(root: Path) -> dict[str, str]:
    """`{relative posix path: sha256}` for every file under `root`.

    Links are recorded by where they point rather than followed, so a link
    dropped into an installed copy is drift rather than an invisible no-op.
    """
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if leaves_tree(path):
            out[rel] = "link:" + os.path.realpath(str(path))
            continue
        if path.is_file():
            try:
                out[rel] = _hash_file(path)
            except OSError as exc:
                out[rel] = f"unreadable:{exc.errno}"
    return out


def fingerprint(digests: dict[str, str]) -> str:
    digest = hashlib.sha256()
    for rel, value in sorted(digests.items()):
        digest.update(f"{rel}\0{value}\0".encode("utf-8"))
    return digest.hexdigest()


def drift(installed: Path, recorded: dict[str, str]) -> list[str]:
    """Which paths differ from what we wrote. Empty means untouched."""
    current = file_digests(installed)
    changed = [f"{rel} (changed)" for rel in sorted(current)
               if rel in recorded and current[rel] != recorded[rel]]
    changed += [f"{rel} (added)" for rel in sorted(current) if rel not in recorded]
    changed += [f"{rel} (removed)" for rel in sorted(recorded) if rel not in current]
    return changed


def copy_tree(source: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(str(source), str(dest), symlinks=True)


# --------------------------------------------------------------------------
# Ledger
# --------------------------------------------------------------------------


def ledger_path(target_dir: Path) -> Path:
    return target_dir / LEDGER_NAME


def read_ledger(target_dir: Path) -> dict:
    raw = _dcio.read_text(ledger_path(target_dir))
    if not raw:
        return {"schema": INSTALL_SCHEMA, "entries": {}}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # An unreadable ledger must not read as "we own everything here", so it
        # degrades to "we own nothing", and ownership falls back to link proof.
        return {"schema": INSTALL_SCHEMA, "entries": {}}
    if data.get("schema") != INSTALL_SCHEMA or not isinstance(data.get("entries"), dict):
        return {"schema": INSTALL_SCHEMA, "entries": {}}
    return data


def write_ledger(target_dir: Path, data: dict) -> None:
    path = ledger_path(target_dir)
    if not data["entries"]:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    _dcio.atomic_write(path, json.dumps(data, indent=2, sort_keys=True) + "\n")


def entry_for(target_dir: Path, name: str) -> dict | None:
    return read_ledger(target_dir)["entries"].get(name)


def record(target_dir: Path, name: str, mechanism: str, source: Path,
           digests: dict[str, str] | None) -> None:
    data = read_ledger(target_dir)
    entry = {
        "mechanism": mechanism,
        "source": source.as_posix(),
        "installed": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }
    if digests is not None:
        entry["fingerprint"] = fingerprint(digests)
        entry["files"] = digests
    data["entries"][name] = entry
    write_ledger(target_dir, data)


def forget(target_dir: Path, name: str) -> None:
    data = read_ledger(target_dir)
    data["entries"].pop(name, None)
    write_ledger(target_dir, data)


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------


def classify(path: Path, entry: dict | None, source: Path) -> tuple[str, str]:
    """What is at `path`, and the detail worth printing about it."""
    if leaves_tree(path):
        pointed = Path(os.path.realpath(str(path)))
        if not path.exists():
            return LINK_BROKEN, f"link to missing {pointed}"
        if pointed == Path(os.path.realpath(str(source))):
            return LINK_OURS, "link to this checkout"
        return LINK_FOREIGN, f"link to {pointed}"

    if not path.exists():
        return ABSENT, ""

    if path.is_dir():
        if entry and entry.get("mechanism") == "copy":
            changed = drift(path, entry.get("files") or {})
            if not changed:
                return COPY_OURS, f"copy of {entry.get('source', '?')}"
            shown = ", ".join(changed[:MAX_DRIFT_SHOWN])
            if len(changed) > MAX_DRIFT_SHOWN:
                shown += f" (SHOWING {MAX_DRIFT_SHOWN}/{len(changed)})"
            return COPY_DRIFTED, shown
        return DIR_FOREIGN, "directory not recorded by this installer"

    # A single installed file — the scout agent definition.
    if entry and entry.get("mechanism") == "file":
        recorded = (entry.get("files") or {}).get(path.name)
        if recorded and recorded == _hash_file(path):
            return COPY_OURS, f"file from {entry.get('source', '?')}"
        return COPY_DRIFTED, f"{path.name} (changed since it was installed)"

    return FILE_FOREIGN, "a file this installer did not write"


def duplicates(target_dir: Path, source: Path) -> list[str]:
    """Other names in this directory that resolve to the same checkout.

    Grok reads `~/.agents/skills/` directly as well as through links, so a
    second name pointing at the same source means the skill is discovered
    twice. Harmless, but it is not something to leave unsaid.
    """
    if not target_dir.is_dir():
        return []
    want = os.path.realpath(str(source))
    found = []
    for child in sorted(target_dir.iterdir()):
        if child.name in (SKILL_NAME, LEDGER_NAME):
            continue
        try:
            if os.path.realpath(str(child)) == want:
                found.append(child.name)
        except OSError:
            continue
    return found


# --------------------------------------------------------------------------
# Operations
# --------------------------------------------------------------------------


def selected(args) -> list[Target]:
    if args.host == "all":
        return list(TARGETS)
    chosen = [t for t in TARGETS if t.key == args.host]
    if not chosen:
        raise DcError(f"unknown host {args.host!r}", _dcio.EXIT_MISSING_REQUIRED)
    return chosen


def install_one(target: Target, home: Path, source: Path, args) -> tuple[str, str]:
    target_dir = target.path(home)
    path = target_dir / SKILL_NAME
    hosts = ", ".join(target.hosts)

    if args.host == "all" and not target.root(home).is_dir() and not path.exists():
        return "SKIP", (f"{target.rel} -- {hosts} not installed here "
                        f"(pass --host {target.key} to create it anyway)")

    # `is_within` resolves links, so this must be asked of the skills directory
    # rather than the install path — once a link exists, the install path
    # resolves to the checkout and every run would look like a self-install.
    if _dcio.is_within(source, target_dir):
        return "SKIP", (f"{target.rel} -- the checkout is already inside this "
                        "directory; the host discovers it without a link")

    entry = entry_for(target_dir, SKILL_NAME)
    state, detail = classify(path, entry, source)

    if state == LINK_OURS:
        return "ALREADY", f"{target.rel}/{SKILL_NAME} -- {detail} ({hosts})"
    if state == COPY_OURS and not args.force:
        # A copy that no longer matches the checkout is exactly the failure mode
        # `--copy` warns about, so re-running `--copy` refreshes it. Doing so
        # only ever overwrites a tree we wrote and nobody has edited since;
        # anything edited classified as COPY_DRIFTED and never reaches here.
        stale = args.copy and (entry or {}).get("files") != file_digests(source)
        if not stale:
            return "ALREADY", f"{target.rel}/{SKILL_NAME} -- {detail} ({hosts})"
    if state in (LINK_FOREIGN, LINK_BROKEN, DIR_FOREIGN, FILE_FOREIGN):
        return "REFUSED", (f"{target.rel}/{SKILL_NAME} -- {detail}; "
                           "this installer did not create it, so it will not "
                           "remove it. Move it aside and re-run.")
    if state == COPY_DRIFTED and not args.force:
        return "REFUSED", (f"{target.rel}/{SKILL_NAME} -- installed copy was "
                           f"edited: {detail}. Re-run with --force to overwrite "
                           "your changes.")

    if args.dry_run:
        verb = "would replace" if state in OWNED else "would install"
        how = "copy" if args.copy else "link"
        return "DRY-RUN", f"{target.rel}/{SKILL_NAME} -- {verb} as {how} ({hosts})"

    if state in OWNED:  # our own copy, being refreshed under --force
        if leaves_tree(path):
            remove_link(path)
        else:
            shutil.rmtree(str(path))

    if args.copy:
        copy_tree(source, path)
        record(target_dir, SKILL_NAME, "copy", source, file_digests(path))
        return "INSTALLED", f"{target.rel}/{SKILL_NAME} -- copy ({hosts})"

    mechanism = make_link(path, source)
    record(target_dir, SKILL_NAME, mechanism, source, None)
    return "INSTALLED", f"{target.rel}/{SKILL_NAME} -- {mechanism} ({hosts})"


def agent_paths(target: Target, home: Path, source: Path,
                name: str = AGENT_FILE) -> tuple[Path, Path] | None:
    """(source definition, install path) for one agent, or None if N/A here."""
    directory = target.agent_path(home)
    if directory is None:
        return None
    return source_agent(source, name), directory / name


def _label(name: str) -> str:
    return "dc-scout" if name == AGENT_FILE else name[:-3]


def install_agent(target: Target, home: Path, source: Path, args,
                  name: str = AGENT_FILE) -> tuple[str, str] | None:
    """Copy the scout definition next to the host's other agents.

    Always a copy, never a link: an agent definition is one small file, and a
    host that scans this directory should not have to follow links out of it.
    The directory is never created — its absence is how we know the host has no
    agents configured, and dropping a lone file into a directory the host does
    not read would be install theatre.
    """
    paths = agent_paths(target, home, source, name)
    if paths is None:
        return None
    definition, path = paths
    if not definition.is_file():
        return "SKIP", f"{target.agent_rel}/{name} -- not in this checkout"
    if not path.parent.is_dir():
        if name != AGENT_FILE:
            return None  # one absence line per host, said by the scout
        return "SKIP", (f"{target.agent_rel} -- directory absent; subagents not "
                        "installed (create it to enable dc-scout and the Gate 6 roster)")

    entry = entry_for(path.parent, name)
    state, detail = classify(path, entry, definition)

    # Ours and identical to the definition in this checkout: nothing to do.
    # Ours but *older* than the checkout gets refreshed without asking — we are
    # only overwriting a file we wrote and nobody has touched since.
    if state == COPY_OURS and _hash_file(path) == _hash_file(definition):
        return "ALREADY", f"{target.agent_rel}/{name} -- {detail}"
    if state == COPY_DRIFTED and not args.force:
        return "REFUSED", (f"{target.agent_rel}/{name} -- {detail}. "
                           "Re-run with --force to overwrite your changes.")
    if state not in (ABSENT, COPY_OURS, COPY_DRIFTED):
        return "REFUSED", (f"{target.agent_rel}/{name} -- {detail}, so it "
                           "will not be replaced. Move it aside and re-run.")
    if args.dry_run:
        verb = "would refresh" if state != ABSENT else "would install"
        return "DRY-RUN", f"{target.agent_rel}/{name} -- {verb}"

    shutil.copyfile(str(definition), str(path))
    record(path.parent, name, "file", definition,
           {name: _hash_file(path)})
    return "INSTALLED", f"{target.agent_rel}/{name} -- {_label(name)}"


def uninstall_agent(target: Target, home: Path, source: Path, args,
                    name: str = AGENT_FILE) -> tuple[str, str] | None:
    paths = agent_paths(target, home, source, name)
    if paths is None:
        return None
    definition, path = paths
    entry = entry_for(path.parent, name)
    if not path.parent.is_dir():
        return None
    state, detail = classify(path, entry, definition)

    if state == ABSENT:
        if entry:
            forget(path.parent, name)
            return "CLEANED", f"{target.agent_rel} -- ledger entry for a file that is gone"
        return None
    if state not in (COPY_OURS, COPY_DRIFTED):
        return "REFUSED", f"{target.agent_rel}/{name} -- {detail}; not ours to remove"
    if state == COPY_DRIFTED and not args.force:
        return "REFUSED", (f"{target.agent_rel}/{name} -- {detail}. "
                           "Re-run with --force to delete it anyway.")
    if args.dry_run:
        return "DRY-RUN", f"{target.agent_rel}/{name} -- would remove"

    path.unlink()
    forget(path.parent, name)
    return "REMOVED", f"{target.agent_rel}/{name}"


def uninstall_one(target: Target, home: Path, source: Path, args) -> tuple[str, str]:
    target_dir = target.path(home)
    path = target_dir / SKILL_NAME
    entry = entry_for(target_dir, SKILL_NAME)
    state, detail = classify(path, entry, source)

    if state == ABSENT:
        if entry:
            forget(target_dir, SKILL_NAME)
            return "CLEANED", f"{target.rel} -- ledger entry for a path that is gone"
        return "SKIP", f"{target.rel} -- nothing installed"

    if state not in OWNED:
        return "REFUSED", (f"{target.rel}/{SKILL_NAME} -- {detail}; "
                           "not ours to remove")

    if state == COPY_DRIFTED and not args.force:
        return "REFUSED", (f"{target.rel}/{SKILL_NAME} -- installed copy was "
                           f"edited: {detail}. Re-run with --force to delete it "
                           "anyway.")

    if args.dry_run:
        return "DRY-RUN", f"{target.rel}/{SKILL_NAME} -- would remove ({state})"

    if leaves_tree(path):
        remove_link(path)
    else:
        # Only reachable for a copy we wrote, and only after `leaves_tree` has
        # said this is not a link. rmtree never runs on anything else.
        shutil.rmtree(str(path))
    forget(target_dir, SKILL_NAME)
    return "REMOVED", f"{target.rel}/{SKILL_NAME} -- {state.lower()}"


def check_one(target: Target, home: Path, source: Path) -> tuple[bool, list[str]]:
    """(healthy, lines) for one target directory."""
    target_dir = target.path(home)
    path = target_dir / SKILL_NAME
    entry = entry_for(target_dir, SKILL_NAME)
    state, detail = classify(path, entry, source)
    hosts = ", ".join(target.hosts)
    lines: list[str] = []
    healthy = True

    if state == ABSENT:
        present = target.root(home).is_dir()
        lines.append(f"{'MISSING' if present else 'ABSENT '}  {target.rel}/{SKILL_NAME} "
                     f"-- {hosts} " + ("host present, skill not installed"
                                      if present else "host not installed here"))
        healthy = not present
    elif state in (LINK_OURS, COPY_OURS):
        lines.append(f"OK       {target.rel}/{SKILL_NAME} -- {detail} "
                     f"({hosts}; invoke {target.invocation})")
    else:
        lines.append(f"PROBLEM  {target.rel}/{SKILL_NAME} -- {state}: {detail}")
        healthy = False

    for name in AGENT_FILES:
        paths = agent_paths(target, home, source, name)
        if paths is None:
            break
        definition, agent = paths
        label = _label(name)
        if not definition.is_file():
            continue
        if not agent.parent.is_dir():
            lines.append(f"ABSENT   {target.agent_rel}/{name} -- host has no "
                         f"agents directory; {label} not installed")
            continue
        astate, adetail = classify(agent, entry_for(agent.parent, name), definition)
        if astate == ABSENT:
            lines.append(f"MISSING  {target.agent_rel}/{name} -- {label} "
                         "not installed")
        elif astate == COPY_OURS:
            current = _hash_file(agent) == _hash_file(definition)
            lines.append(f"OK       {target.agent_rel}/{name} -- {label}"
                         + ("" if current else " (older than this checkout; "
                                               "re-run the installer)"))
        else:
            lines.append(f"PROBLEM  {target.agent_rel}/{name} -- "
                         f"{astate}: {adetail}")
            healthy = False

    for name in duplicates(target_dir, source):
        lines.append(f"DUPLICATE {target.rel}/{name} resolves to the same checkout; "
                     "the skill is discovered twice")

    if _dcio.is_within(source, target_dir):
        lines.append(f"DUPLICATE the checkout itself is inside {target.rel}; "
                     "the host discovers it directly as well")

    return healthy, lines


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(prog="dc_install.py", description=__doc__)
    parser.add_argument("--host", default="all",
                        choices=[t.key for t in TARGETS] + ["all"],
                        help="which skill directory to act on (default: all "
                             "hosts already present)")
    parser.add_argument("--home", default=None,
                        help="treat this directory as home (for tests)")
    parser.add_argument("--check", action="store_true",
                        help="report what is installed and exit")
    parser.add_argument("--uninstall", action="store_true",
                        help="remove installs this installer owns")
    parser.add_argument("--copy", action="store_true",
                        help="install a snapshot copy instead of a link; it "
                             "will not track this checkout")
    parser.add_argument("--force", action="store_true",
                        help="overwrite or delete an edited copy we own; never "
                             "touches anything we do not own")
    parser.add_argument("--no-agent", action="store_true",
                        help="skip the subagent definitions (scout and Gate 6 roster)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print what would happen, change nothing")
    args = parser.parse_args()

    if args.check and args.uninstall:
        raise DcError("--check and --uninstall are mutually exclusive",
                      _dcio.EXIT_MISSING_REQUIRED)

    source = source_skill()
    home = Path(args.home).expanduser().resolve() if args.home else Path.home()
    targets = selected(args)

    if args.check:
        problems = 0
        for target in targets:
            healthy, lines = check_one(target, home, source)
            problems += 0 if healthy else 1
            for line in lines:
                print(line)
        print(f"source: {source}")
        print(f"RESULT: {'PROBLEMS' if problems else 'OK'} "
              f"({len(targets) - problems}/{len(targets)} targets healthy)")
        return _dcio.EXIT_CHECK_FAILED if problems else _dcio.EXIT_OK

    act = uninstall_one if args.uninstall else install_one
    act_agent = uninstall_agent if args.uninstall else install_agent
    refused = 0
    for target in targets:
        outcomes = [act(target, home, source, args)]
        if not args.no_agent:
            outcomes.extend(act_agent(target, home, source, args, name) for name in AGENT_FILES)
        for outcome in outcomes:
            if outcome is None:
                continue
            status, message = outcome
            if status == "REFUSED":
                refused += 1
            print(f"{status}: {message}")

    verb = "uninstall" if args.uninstall else "install"
    print(f"RESULT: {verb} {'INCOMPLETE' if refused else 'OK'} "
          f"({refused} refused of {len(targets)} targets)")
    return _dcio.EXIT_CHECK_FAILED if refused else _dcio.EXIT_OK


if __name__ == "__main__":
    _dcio.run_cli(main)
