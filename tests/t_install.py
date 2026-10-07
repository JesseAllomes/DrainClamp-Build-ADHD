"""dc_install.py checks: ownership, refusal, and never deleting the checkout.

The dangerous operation in an installer is removal, so most of this suite is
about what it *declines* to remove. The single worst outcome — `rmtree`
following a junction and emptying the checkout on the far side — is asserted
directly, by counting the source tree after every destructive path.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "drainclamp-build-adhd" / "scripts"
os.environ.setdefault("DRAINCLAMP_HOME", tempfile.mkdtemp(prefix="dcreg-"))  # never the real registry
SOURCE = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))
import dc_install  # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  ' + detail if detail else ''}")
    if not cond:
        fails.append(name)


def new_home():
    home = Path(tempfile.mkdtemp(prefix="dcinstall-"))
    (home / ".agents").mkdir()
    return home


def run(home, *args, script=None):
    # UTF-8 in and out: the installer prints paths, and a console codepage must
    # never be what decides whether a check passes.
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    return subprocess.run(
        [sys.executable, "-B", str(script or SCRIPTS / "dc_install.py"),
         "--home", str(home), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
    )


def source_files():
    return sum(1 for p in SOURCE.rglob("*") if p.is_file())


def skills(home):
    return home / ".agents" / "skills"


def installed(home):
    return skills(home) / dc_install.SKILL_NAME


def ledger(home):
    path = skills(home) / dc_install.LEDGER_NAME
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


BASELINE = source_files()

# --- fresh install ---------------------------------------------------------

home = new_home()
out = run(home, "--host", "agents")
check("a fresh install reports INSTALLED", out.returncode == 0 and "INSTALLED" in out.stdout,
      out.stdout.strip().splitlines()[0] if out.stdout else out.stderr.strip())

linked = installed(home)
linkable = linked.exists()
check("the install path resolves to this checkout",
      not linkable or Path(os.path.realpath(str(linked))) == Path(os.path.realpath(str(SOURCE))),
      os.path.realpath(str(linked)) if linkable else "no link created on this host")

entry = (ledger(home) or {}).get("entries", {}).get(dc_install.SKILL_NAME, {})
check("the ledger records the mechanism and source",
      entry.get("mechanism") in {"symlink", "junction"} and entry.get("source", "").endswith(
          dc_install.SKILL_NAME),
      f"{entry.get('mechanism')} -> {entry.get('source', '')[-40:]}")

# --- idempotency -----------------------------------------------------------

again = run(home, "--host", "agents")
check("installing twice is a no-op, not an error",
      again.returncode == 0 and "ALREADY" in again.stdout,
      again.stdout.strip().splitlines()[0] if again.stdout else again.stderr.strip())

# --- check -----------------------------------------------------------------

status = run(home, "--host", "agents", "--check")
check("--check is green on a healthy install",
      status.returncode == 0 and "OK  " in status.stdout,
      status.stdout.strip().splitlines()[0] if status.stdout else status.stderr.strip())

# A second name pointing at the same checkout means double discovery.
dup = skills(home) / "drainclamp-build-old"
duplicated = True
try:
    dc_install.make_link(dup, SOURCE)
except Exception:  # platform refused a second link; the case is untestable here
    duplicated = False
dupcheck = run(home, "--host", "agents", "--check")
check("a second link to the same checkout is reported as DUPLICATE",
      (not duplicated) or "DUPLICATE" in dupcheck.stdout,
      "SKIPPED: no second link on this host" if not duplicated else "reported")
if duplicated:
    dc_install.remove_link(dup)

# --- uninstall -------------------------------------------------------------

gone = run(home, "--host", "agents", "--uninstall")
check("uninstall removes the link it owns",
      gone.returncode == 0 and "REMOVED" in gone.stdout and not installed(home).exists(),
      gone.stdout.strip().splitlines()[0] if gone.stdout else gone.stderr.strip())
check("uninstalling a link never touches the checkout",
      source_files() == BASELINE, f"{source_files()} files, baseline {BASELINE}")
check("the ledger file is removed once it is empty",
      not (skills(home) / dc_install.LEDGER_NAME).exists())

empty = run(home, "--host", "agents", "--uninstall")
check("uninstalling nothing is a clean SKIP",
      empty.returncode == 0 and "SKIP" in empty.stdout,
      empty.stdout.strip().splitlines()[0])

# --- foreign directory -----------------------------------------------------

home = new_home()
foreign = installed(home)
foreign.mkdir(parents=True)
(foreign / "SOMEONE_ELSES.md").write_text("not ours\n", encoding="utf-8")

refused = run(home, "--host", "agents")
check("installing over an unowned directory is refused",
      refused.returncode == 2 and "REFUSED" in refused.stdout,
      refused.stdout.strip().splitlines()[0])
check("the refused directory is left exactly as it was",
      (foreign / "SOMEONE_ELSES.md").is_file())

refused_rm = run(home, "--host", "agents", "--uninstall")
check("uninstall refuses a directory it does not own",
      refused_rm.returncode == 2 and "not ours to remove" in refused_rm.stdout,
      refused_rm.stdout.strip().splitlines()[0])
check("--force does not extend to unowned directories",
      run(home, "--host", "agents", "--uninstall", "--force").returncode == 2
      and (foreign / "SOMEONE_ELSES.md").is_file())

# A corrupt ledger must not read as "we own everything in here".
(skills(home) / dc_install.LEDGER_NAME).write_text("{not json", encoding="utf-8")
check("a corrupt ledger claims no ownership",
      run(home, "--host", "agents", "--uninstall").returncode == 2
      and (foreign / "SOMEONE_ELSES.md").is_file())

# --- foreign link ----------------------------------------------------------

home = new_home()
decoy = home / "decoy"
(decoy / "inner").mkdir(parents=True)
(decoy / "inner" / "keep.txt").write_text("keep\n", encoding="utf-8")
skills(home).mkdir(parents=True, exist_ok=True)
foreign_link = True
try:
    dc_install.make_link(installed(home), decoy)
except Exception:
    foreign_link = False

if foreign_link:
    out = run(home, "--host", "agents", "--uninstall", "--force")
    check("a link pointing somewhere else is refused",
          out.returncode == 2 and "not ours to remove" in out.stdout,
          out.stdout.strip().splitlines()[0])
    check("the refused link's target keeps its contents",
          (decoy / "inner" / "keep.txt").is_file())
    dc_install.remove_link(installed(home))

    # A link whose target has been deleted is still not ours to clear away.
    stale = home / "stale"
    stale.mkdir()
    dc_install.make_link(installed(home), stale)
    stale.rmdir()
    out = run(home, "--host", "agents")
    check("a broken link is refused rather than replaced",
          out.returncode == 2 and "LINK-BROKEN" not in out.stdout
          and "REFUSED" in out.stdout,
          out.stdout.strip().splitlines()[0])
    dc_install.remove_link(installed(home))
else:
    check("a link pointing somewhere else is refused", True,
          "SKIPPED: this host refused to create a directory link")
    check("a broken link is refused rather than replaced", True,
          "SKIPPED: this host refused to create a directory link")

# --- copy mode -------------------------------------------------------------

home = new_home()
out = run(home, "--host", "agents", "--copy")
check("--copy installs a real directory, not a link",
      out.returncode == 0 and installed(home).is_dir()
      and not dc_install.leaves_tree(installed(home)),
      out.stdout.strip().splitlines()[0])
check("the ledger fingerprints the copied tree",
      len((ledger(home)["entries"][dc_install.SKILL_NAME].get("files") or {})) >= 10,
      f"{len(ledger(home)['entries'][dc_install.SKILL_NAME].get('files') or {})} files recorded")

edited = installed(home) / "SKILL.md"
edited.write_text(edited.read_text(encoding="utf-8") + "\nlocal edit\n", encoding="utf-8")

out = run(home, "--host", "agents", "--copy")
check("an edited copy is not silently overwritten",
      out.returncode == 2 and "was edited" in out.stdout,
      out.stdout.strip().splitlines()[0])
check("the edit survives the refusal", "local edit" in edited.read_text(encoding="utf-8"))

out = run(home, "--host", "agents", "--uninstall")
check("uninstall also refuses an edited copy without --force",
      out.returncode == 2 and installed(home).is_dir(),
      out.stdout.strip().splitlines()[0])

out = run(home, "--host", "agents", "--copy", "--force")
check("--force refreshes a copy we own",
      out.returncode == 0 and "local edit" not in edited.read_text(encoding="utf-8"),
      out.stdout.strip().splitlines()[0])

# Ours, unmodified, but written from an older checkout: `--copy` refreshes it,
# because a snapshot that silently goes stale is the whole risk of copy mode.
stale_file = installed(home) / "SKILL.md"
stale_file.write_text("older snapshot\n", encoding="utf-8")
dc_install.record(skills(home), dc_install.SKILL_NAME, "copy", SOURCE,
                  dc_install.file_digests(installed(home)))
out = run(home, "--host", "agents", "--copy")
check("--copy refreshes a snapshot that has fallen behind the checkout",
      out.returncode == 0 and "INSTALLED" in out.stdout
      and stale_file.read_bytes() == (SOURCE / "SKILL.md").read_bytes(),
      out.stdout.strip().splitlines()[0])

out = run(home, "--host", "agents", "--uninstall")
check("uninstall removes an untouched copy",
      out.returncode == 0 and not installed(home).exists(),
      out.stdout.strip().splitlines()[0])
check("removing a copy never touches the checkout",
      source_files() == BASELINE, f"{source_files()} files, baseline {BASELINE}")

# --- dry run ---------------------------------------------------------------

home = new_home()
out = run(home, "--host", "agents", "--dry-run")
check("--dry-run reports without installing",
      out.returncode == 0 and "DRY-RUN" in out.stdout and not installed(home).exists(),
      out.stdout.strip().splitlines()[0])

# --- host selection --------------------------------------------------------

home = new_home()  # only ~/.agents exists
out = run(home)
check("a host that is not installed here is skipped, not created",
      "SKIP" in out.stdout and not (home / ".codex").exists(),
      [ln for ln in out.stdout.splitlines() if "codex" in ln][0]
      if "codex" in out.stdout else out.stdout.strip())

out = run(home, "--host", "codex")
check("naming a host creates its directory",
      out.returncode == 0 and (home / ".codex" / "skills" / dc_install.SKILL_NAME).exists(),
      out.stdout.strip().splitlines()[0])

# --- the checkout living inside the skills directory -----------------------

home = new_home()
inside = installed(home)
inside.parent.mkdir(parents=True, exist_ok=True)
shutil.copytree(str(SOURCE), str(inside))
out = run(home, "--host", "agents", script=inside / "scripts" / "dc_install.py")
check("a checkout already inside the skills directory is not linked to itself",
      out.returncode == 0 and "already inside" in out.stdout,
      out.stdout.strip().splitlines()[0])

# --- the dc-scout subagent -------------------------------------------------

# Root `agents/` in a normal checkout, skill-local in an older one — ask the
# installer rather than hard-coding a layout it is allowed to move.
DEFINITION = dc_install.source_agent(SOURCE)


def agent_file(home):
    return home / ".claude" / "agents" / dc_install.AGENT_FILE


home = new_home()
(home / ".claude").mkdir()  # host present, but no ~/.claude/agents
out = run(home, "--host", "claude")
check("no agents directory means no scout, and no directory invented",
      out.returncode == 0 and not (home / ".claude" / "agents").exists(),
      [ln for ln in out.stdout.splitlines() if "dc-scout" in ln][0]
      if "dc-scout" in out.stdout else "not mentioned")

home = new_home()
(home / ".claude" / "agents").mkdir(parents=True)
out = run(home, "--host", "claude")
check("the scout is installed where the host reads agents",
      out.returncode == 0 and agent_file(home).is_file(),
      out.stdout.strip().splitlines()[-2])
check("the installed scout is byte-identical to the checkout's definition",
      agent_file(home).read_bytes() == DEFINITION.read_bytes())
roster = [n for n in dc_install.AGENT_FILES if n != dc_install.AGENT_FILE]
check("the Gate 6 roster is installed beside the scout",
      all((home / ".claude" / "agents" / n).read_bytes() == dc_install.source_agent(SOURCE, n).read_bytes()
          for n in roster), out.stdout)
chk = run(home, "--host", "claude", "--check")
check("--check reports every agent", all(f"OK       .claude/agents/{n}" in chk.stdout
                                         for n in dc_install.AGENT_FILES), chk.stdout)

out = run(home, "--host", "claude")
check("re-installing an up-to-date scout is a no-op",
      "ALREADY" in [ln.split(":")[0] for ln in out.stdout.splitlines()],
      out.stdout.strip().splitlines()[-2])

# Ours, unmodified, but older than the checkout: refresh without being asked.
agent_file(home).write_text("stale definition\n", encoding="utf-8")
dc_install.record(agent_file(home).parent, dc_install.AGENT_FILE, "file", DEFINITION,
                  {dc_install.AGENT_FILE: dc_install._hash_file(agent_file(home))})
out = run(home, "--host", "claude")
check("a scout we wrote and nobody edited is refreshed from the checkout",
      out.returncode == 0 and agent_file(home).read_bytes() == DEFINITION.read_bytes(),
      out.stdout.strip().splitlines()[-2])

agent_file(home).write_text(agent_file(home).read_text(encoding="utf-8") + "mine\n",
                            encoding="utf-8")
out = run(home, "--host", "claude")
check("an edited scout is refused, not overwritten",
      out.returncode == 2 and "mine" in agent_file(home).read_text(encoding="utf-8"),
      out.stdout.strip().splitlines()[-2])

out = run(home, "--host", "claude", "--force")
check("--force overwrites a scout we own",
      out.returncode == 0 and agent_file(home).read_bytes() == DEFINITION.read_bytes(),
      out.stdout.strip().splitlines()[-2])

out = run(home, "--host", "claude", "--uninstall")
check("uninstall removes the scout it owns",
      out.returncode == 0 and not agent_file(home).exists()
      and not any((home / ".claude" / "agents" / n).exists() for n in dc_install.AGENT_FILES),
      out.stdout.strip().splitlines()[-2])

# A definition somebody else wrote by hand is not ours to replace.
agent_file(home).write_text("---\nname: dc-scout\n---\nhand written\n", encoding="utf-8")
out = run(home, "--host", "claude")
check("a hand-written scout definition is refused",
      out.returncode == 2 and "hand written" in agent_file(home).read_text(encoding="utf-8"),
      out.stdout.strip().splitlines()[-2])

out = run(home, "--host", "claude", "--no-agent")
check("--no-agent leaves the agents directory alone",
      out.returncode == 0 and "dc-scout" not in out.stdout,
      out.stdout.strip().splitlines()[0])

# --- Codex agents: TOML generated from the same definitions ---------------
import tomllib  # noqa: E402

home = new_home()
(home / ".codex").mkdir()  # Codex present, no agents directory yet
out = run(home, "--host", "codex", "--agents-only")
codex_dir = home / ".codex" / "agents"
check("--agents-only installs Codex agents and no skill link",
      out.returncode == 0 and (codex_dir / "dca-critic.toml").is_file()
      and not (home / ".codex" / "skills").exists(), out.stdout)
critic = tomllib.loads((codex_dir / "dca-critic.toml").read_text(encoding="utf-8"))
meta, body = dc_install.split_definition(dc_install.source_agent(SOURCE, "dca-critic.md")
                                         .read_text(encoding="utf-8"))
check("the critic TOML carries the definition and the Codex roster's model",
      critic["name"] == "dca-critic" and critic["description"] == meta["description"]
      and critic["developer_instructions"] == body and critic["model"] == "gpt-6.1-sol"
      and critic["model_reasoning_effort"] == "high" and critic["sandbox_mode"] == "read-only",
      str({k: v for k, v in critic.items() if k != "developer_instructions"}))
fixer = tomllib.loads((codex_dir / "dca-fixer.toml").read_text(encoding="utf-8"))
checker = tomllib.loads((codex_dir / "dca-checker.toml").read_text(encoding="utf-8"))
scout = tomllib.loads((codex_dir / "dca-scout.toml").read_text(encoding="utf-8"))
check("the fixer may write; the checker runs on the light model; the scout inherits",
      fixer["sandbox_mode"] == "workspace-write" and checker["model"] == "gpt-6-luna"
      and "model" not in scout and scout["sandbox_mode"] == "read-only")
out = run(home, "--host", "codex", "--agents-only")
check("a second Codex install changes nothing", out.returncode == 0 and "INSTALLED" not in out.stdout,
      out.stdout)
out = run(home, "--host", "codex", "--agents-only", "--check")
check("--check --agents-only reports the Codex agents healthy",
      out.returncode == 0 and "OK       .codex/agents/dca-critic.toml" in out.stdout, out.stdout)

proj = Path(tempfile.mkdtemp(prefix="dcproj-"))
(proj / ".agent").mkdir()
(proj / ".agent" / "drainclamp-project.json").write_text(json.dumps(
    {"schema": 1, "rev": 1, "chunks": {}, "review": {"config": {"host_models": {
        "codex": {"checker": "gpt-6-sol/low"}}}}}), encoding="utf-8")
out = run(home, "--host", "codex", "--agents-only", "--project", str(proj))
checker = tomllib.loads((codex_dir / "dca-checker.toml").read_text(encoding="utf-8"))
check("--project takes the Codex roster from that project's review config",
      out.returncode == 0 and checker["model"] == "gpt-6-sol" and checker["model_reasoning_effort"] == "low",
      out.stdout + out.stderr)

(codex_dir / "dca-critic.toml").write_text("# mine\n", encoding="utf-8")
out = run(home, "--host", "codex", "--agents-only")
check("an edited Codex agent is refused", out.returncode == 2
      and (codex_dir / "dca-critic.toml").read_text(encoding="utf-8") == "# mine\n", out.stdout)
out = run(home, "--host", "codex", "--agents-only", "--uninstall", "--force")
check("uninstall removes every Codex agent it wrote",
      out.returncode == 0 and not list(codex_dir.glob("dca-*.toml")), out.stdout)

# --- Grok: per-type model pins in ~/.grok/config.toml ----------------------
home = new_home()
(home / ".grok").mkdir()
gcfg = home / ".grok" / "config.toml"
gcfg.write_text('[models]\ndefault = "grok-4.6"\n', encoding="utf-8")
out = run(home, "--host", "agents", "--agents-only")
pins = tomllib.loads(gcfg.read_text(encoding="utf-8"))
check("Grok pins land in config.toml beside the user's own tables",
      out.returncode == 0 and pins["models"]["default"] == "grok-4.6"
      and pins["subagents"]["models"]["drainclamp-build-adhd:dca-checker"] == "grok-4.7-build-fast"
      and pins["subagents"]["models"]["drainclamp-build-adhd:dca-critic"] == "grok-4.6"
      and "drainclamp-build-adhd:dca-scout" not in pins["subagents"]["models"], out.stdout)
out = run(home, "--host", "agents", "--agents-only")
check("a second run leaves the pins alone", out.returncode == 0 and "ALREADY" in out.stdout, out.stdout)
out = run(home, "--host", "agents", "--agents-only", "--check")
check("--check reports the Grok pins", "OK       .grok/config.toml -- Gate 6 model pins" in out.stdout, out.stdout)
out = run(home, "--host", "agents", "--agents-only", "--uninstall")
check("uninstall removes only our block",
      out.returncode == 0 and gcfg.read_text(encoding="utf-8") == '[models]\ndefault = "grok-4.6"\n',
      repr(gcfg.read_text(encoding="utf-8")))
gcfg.write_text('[subagents.models]\nexplore = "grok-4.6"\n', encoding="utf-8")
out = run(home, "--host", "agents", "--agents-only")
check("a user's own [subagents.models] is refused and left as it was",
      out.returncode == 2 and gcfg.read_text(encoding="utf-8") == '[subagents.models]\nexplore = "grok-4.6"\n',
      out.stdout)

check("the checkout is intact after the whole suite",
      source_files() == BASELINE, f"{source_files()} files, baseline {BASELINE}")

print()
print("FAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
