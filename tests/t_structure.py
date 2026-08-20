"""Layout checks: the shape `dc_install.py` and the fork procedure assume.

`dc_install.py` finds its own skill with `Path(__file__).parent.parent` and the
scout with `source.parent.parent / "agents"`. Both are silent about a tree that
does not match: a second skill directory left behind by a copy, a scripts
directory nested one level too deep, an agent file that the plugin loader will
never read. The install still reports success, having installed the wrong
thing or no agent at all.

These checks are about arrangement, not content. Every other suite runs the
scripts; this one asserts the tree those scripts are reached through.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def tracked() -> set[str]:
    """Git-tracked paths. Disk state is not the question -- `.agent/` and
    `__pycache__` appear the moment the scripts are run, and both are ignored.
    What matters is what a clone receives."""
    done = subprocess.run(["git", "ls-files"], cwd=str(ROOT),
                          capture_output=True, text=True)
    return set(done.stdout.splitlines()) if done.returncode == 0 else set()


TRACKED = tracked()

fails = []


def check(name, cond, detail=""):
    detail = str(detail) if detail else ""
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  ' + detail if detail else ''}")
    if not cond:
        fails.append(name)


# --------------------------------------------------------------------------
# One skill, at the depth the installer expects
# --------------------------------------------------------------------------

skills_dir = ROOT / "skills"
check("skills/ exists", skills_dir.is_dir())

entries = sorted(p for p in skills_dir.iterdir() if p.is_dir()) if skills_dir.is_dir() else []
check("exactly one skill directory", len(entries) == 1,
      f"{len(entries)}: {[p.name for p in entries]}")

# A leftover copy is the failure this suite exists for: `cp -r` of a parent
# followed by a rename leaves two, and the installer silently takes one.
stray = [p for p in entries if not (p / "SKILL.md").is_file()]
check("no directory in skills/ without a SKILL.md", not stray, str([p.name for p in stray]))

if not entries or not (entries[0] / "SKILL.md").is_file():
    print()
    print("FAILURES: ['no usable skill directory']")
    sys.exit(1)

skill = entries[0]

# --------------------------------------------------------------------------
# The paths dc_install.py resolves by relative arithmetic
# --------------------------------------------------------------------------

check("scripts/ sits directly under the skill", (skill / "scripts").is_dir())
check("dc_install.py is where source_skill() expects it",
      (skill / "scripts" / "dc_install.py").is_file())
check("SKILL.md sits at the skill root", (skill / "SKILL.md").is_file())

# `source_agent()` looks at source.parent.parent / "agents" -- the repository
# root -- because Claude Code's plugin loader reads agents only from there and
# silently ignores a path inside skills/.
scouts = sorted(p.name for p in (ROOT / "agents").glob("*.md")) if (ROOT / "agents").is_dir() else []
check("a scout definition exists at the repository root", bool(scouts), str(scouts))

# Only the scout is constrained. `skills/*/agents/openai.yaml` is a Codex
# config, a different artifact that legitimately sits beside the skill.
nested_scout = sorted(p.name for p in (skill / "agents").glob("*.md")) if (skill / "agents").is_dir() else []
check("no scout definition nested inside the skill", not nested_scout, str(nested_scout))

# Grok catalogs plugins from `.grok-plugin/marketplace.json`. A Claude-style
# `"source": "./"` at marketplace root is registered as a source and then
# silently dropped from the catalog, so `grok plugin install <name>` cannot
# find the plugin. A URL source (or a subdirectory path) is what Grok indexes.
grok_market = ROOT / ".grok-plugin" / "marketplace.json"
check(".grok-plugin/marketplace.json exists", grok_market.is_file())
if grok_market.is_file():
    try:
        grok_data = json.loads(grok_market.read_text(encoding="utf-8"))
        grok_ok = True
    except json.JSONDecodeError as exc:
        grok_data = {}
        grok_ok = False
        check(".grok-plugin/marketplace.json is valid JSON", False, str(exc))
    if grok_ok:
        check(".grok-plugin/marketplace.json is valid JSON", True)

        def grok_source_indexable(src):
            if src in (None, "", ".", "./"):
                return False
            if isinstance(src, str):
                return src not in (".", "./")
            if isinstance(src, dict):
                if src.get("url"):
                    return True
                path = src.get("path")
                return bool(path) and path not in (".", "./")
            return False

        entries = grok_data.get("plugins") if isinstance(grok_data, dict) else None
        sources = [p.get("source") for p in entries] if isinstance(entries, list) else []
        bad = [s for s in sources if not grok_source_indexable(s)]
        check("Grok marketplace sources are indexable (not repo-root ./)",
              bool(sources) and not bad, str(bad if sources else "no plugins"))

# --------------------------------------------------------------------------
# Every reference the skill declares actually resolves
# --------------------------------------------------------------------------

text = (skill / "SKILL.md").read_text(encoding="utf-8")

# Named three ways: `references/x.md`, a bare `x.md` in the gate table, and
# the glob `references/phaseN-*.md` that stands for the whole phase set.
referenced = set(re.findall(r"references/([a-z0-9-]+\.md)", text))
referenced |= set(re.findall(r"`([a-z0-9-]+\.md)`", text))
if "phaseN-*.md" in text:
    referenced |= {p.name for p in (skill / "references").glob("phase*.md")}
referenced = sorted(referenced)
missing_refs = [r for r in referenced
                if r.startswith("phase") or r.startswith("platform")
                if not (skill / "references" / r).is_file()]
check("every reference named in SKILL.md exists", not missing_refs, str(missing_refs))

named_scripts = sorted(set(re.findall(r"\b(dc_[a-z]+\.py)\b", text)))
missing_scripts = [s for s in named_scripts if not (skill / "scripts" / s).is_file()]
check("every script named in SKILL.md exists", not missing_scripts, str(missing_scripts))

# The reverse: a reference file nothing points at is either dead weight or a
# rename that half-landed.
if (skill / "references").is_dir():
    on_disk = {p.name for p in (skill / "references").glob("*.md")}
    orphans = sorted(on_disk - set(referenced))
    check("no orphaned reference files", not orphans, str(orphans))

# --------------------------------------------------------------------------
# Nothing generated or private is committed
# --------------------------------------------------------------------------

check("no .agent/ state committed",
      not [f for f in TRACKED if f.startswith(".agent/")])
check("no __pycache__ committed",
      not [f for f in TRACKED if "__pycache__" in f])
check("no test fixtures committed",
      not [f for f in TRACKED if f.startswith("tests/fixtures/")])

# Scripts are stdlib-only by design; a requirements file would contradict the
# skill's own claim and change how every host must install it.
for forbidden in ("requirements.txt", "pyproject.toml", "setup.py"):
    check(f"no {forbidden} (scripts are stdlib-only)", forbidden not in TRACKED)

print()
print(f"FAILURES: {fails if fails else 'none'}")
sys.exit(1 if fails else 0)
