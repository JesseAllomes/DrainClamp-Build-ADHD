"""Identity checks: does this checkout agree with itself about its own name?

A fork is made by copying the tree and renaming the skill directory. Every
other name in the repository -- the `name:` field, the plugin manifests, the
agent definition, the install target, the README's paths -- is then a separate
edit that can be missed, and missing one produces a plugin that installs over
the plugin it was forked from. Nothing in the other suites notices: they all
test the scripts, and the scripts do not read `SKILL.md`'s front matter.

So these checks derive the expected name from the directory and assert that
everything else already agrees. They are deliberately name-agnostic -- this
file is identical in the baseline and in any fork of it.
"""
import json
import os
import re
import sys
import tempfile
os.environ.setdefault("DRAINCLAMP_HOME", tempfile.mkdtemp(prefix="dcreg-"))  # never the real registry
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

fails = []


def check(name, cond, detail=""):
    detail = str(detail) if detail else ""
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  ' + detail if detail else ''}")
    if not cond:
        fails.append(name)


# --------------------------------------------------------------------------
# The skill directory is the single source of truth for the name.
# --------------------------------------------------------------------------

skill_dirs = sorted(p.parent for p in ROOT.glob("skills/*/SKILL.md"))
check("exactly one skill directory", len(skill_dirs) == 1,
      f"{len(skill_dirs)}: {[d.name for d in skill_dirs]}")

if not skill_dirs:
    print()
    print("FAILURES: ['no skill directory found']")
    sys.exit(1)

skill = skill_dirs[0]
NAME = skill.name
print(f"       (derived name: {NAME})")

# --------------------------------------------------------------------------
# Front matter
# --------------------------------------------------------------------------

text = (skill / "SKILL.md").read_text(encoding="utf-8")
front = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
check("SKILL.md has front matter", front is not None)

declared = ""
if front:
    m = re.search(r"^name:\s*(\S+)\s*$", front.group(1), re.MULTILINE)
    declared = m.group(1) if m else ""
check("SKILL.md declares a name", bool(declared))
check("SKILL.md name matches its directory", declared == NAME,
      f"declared {declared!r}, directory {NAME!r}")

# --------------------------------------------------------------------------
# No stale references to a name this checkout no longer uses.
#
# A fork keeps the prefix, so `drainclamp-build` is a live substring of
# `drainclamp-build-adhd`. The check has to be for the bare name as a whole
# token, not as a substring, or it can never fail in the baseline and always
# fails in a fork.
# --------------------------------------------------------------------------

SKIP_DIRS = {".git", ".agent", "__pycache__", "fixtures", ".venv", "venv"}
SUFFIXED = re.compile(re.escape(NAME) + r"-[a-z0-9]")

# Only files that carry install identity are scanned. `tests/` is excluded on
# purpose: a suite that checks duplicate detection has to name a second,
# non-existent skill to detect, and that name is data, not an identity claim.
SCANNED = ("skills", "agents", ".claude-plugin", ".grok-plugin", "README.md")


def carries_identity(path: Path) -> bool:
    rel = path.relative_to(ROOT)
    return rel.parts[0] in SCANNED

# Format markers are a file-format tag, not an install identity. They stay
# fixed across forks on purpose: every variant writes `.agent/drainclamp-state.md`
# with the same header, so a project started under one plugin can be resumed
# under another. Renaming the marker would fork the state format and break that.
# Matched in both spellings -- as written into a file, and as the regex a script
# uses to parse it back.
FORMAT_MARKER = re.compile(
    r"<!--\s*(\\s\*)?[a-z0-9-]+:\s*(\\s\*)?(state|audit|log archive|fixtures)")

others = set()
for path in sorted(ROOT.rglob("*")):
    if not path.is_file() or path.suffix not in {".md", ".json", ".py"}:
        continue
    if SKIP_DIRS & set(path.parts):
        continue
    if not carries_identity(path):
        continue
    body = path.read_text(encoding="utf-8", errors="replace")
    for i, line in enumerate(body.splitlines(), start=1):
        if FORMAT_MARKER.search(line) or f"# drainclamp-build fixtures" in line:
            continue
        for hit in re.finditer(r"\bdrainclamp-build[a-z0-9-]*\b", line):
            found = hit.group(0)
            if found == NAME:
                continue
            # a longer name that merely starts with ours is a different plugin
            if found.startswith(NAME) and SUFFIXED.match(found):
                others.add(f"{path.relative_to(ROOT)}:{i}: {found}")
            elif not NAME.startswith(found):
                others.add(f"{path.relative_to(ROOT)}:{i}: {found}")
            elif found != NAME:
                others.add(f"{path.relative_to(ROOT)}:{i}: {found}")

check("no references to a foreign skill name", not others,
      "; ".join(sorted(others)[:3]) + (f" (+{len(others) - 3} more)" if len(others) > 3 else ""))

# --------------------------------------------------------------------------
# Manifests agree
# --------------------------------------------------------------------------

for plugin_dir_name in (".claude-plugin", ".grok-plugin"):
    plugin_dir = ROOT / plugin_dir_name
    rel = plugin_dir_name
    if not plugin_dir.is_dir():
        check(f"no {rel} directory to check", True, "skipped")
        continue
    for manifest in sorted(plugin_dir.glob("*.json")):
        label = f"{rel}/{manifest.name}"
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            check(f"{label} is valid JSON", False, str(exc))
            continue
        check(f"{label} is valid JSON", True)
        if "plugins" in data:
            plugins = data["plugins"]
            if isinstance(plugins, list):
                names = [p.get("name") for p in plugins]
            elif isinstance(plugins, dict):
                names = list(plugins)
            else:
                names = []
            check(f"{label} lists this plugin", NAME in names, str(names))
        else:
            check(f"{label} name matches the skill",
                  data.get("name") == NAME, str(data.get("name")))

# --------------------------------------------------------------------------
# The agent definition, where one ships
# --------------------------------------------------------------------------

agents = sorted((ROOT / "agents").glob("*.md")) if (ROOT / "agents").is_dir() else []
# Exactly the definitions the installer ships: a stray file would sit in the plugin's
# agents/ unseen by dc_install, and a missing one would install nothing for its role.
_install = (ROOT / "skills" / NAME / "scripts" / "dc_install.py").read_text(encoding="utf-8")
_m = re.search(r"^AGENT_FILES = \((.*?)\)$", _install, re.MULTILINE | re.DOTALL)
shipped = sorted(re.findall(r'"([\w.-]+\.md)"', _m.group(1)) + (["dca-scout.md"] if "AGENT_FILE," in _m.group(1) else [])) if _m else []
check("agent definitions are exactly the installer's list", [a.name for a in agents] == shipped,
      f"{[a.name for a in agents]} vs {shipped}")
for agent in agents:
    body = agent.read_text(encoding="utf-8")
    m = re.search(r"^name:\s*(\S+)\s*$", body, re.MULTILINE)
    check("agent declares a name", m is not None)
    if m:
        check("agent file name matches its declared name",
              agent.stem == m.group(1), f"{agent.stem} vs {m.group(1)}")

print()
print(f"FAILURES: {fails if fails else 'none'}")
sys.exit(1 if fails else 0)
