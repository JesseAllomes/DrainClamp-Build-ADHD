"""Domain skills (dc_skills.py): sources from config, read from the ref, marked per project."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "drainclamp-build-adhd" / "scripts"
os.environ.setdefault("DRAINCLAMP_HOME", tempfile.mkdtemp(prefix="dcreg-"))  # never the real registry
sys.path.insert(0, str(SCRIPTS))
import dc_project  # noqa: E402
import dc_skills  # noqa: E402
import dc_state  # noqa: E402

fails = []


def check(name, cond, detail=""):
    detail = str(detail) if detail else ""
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  ' + detail if detail else ''}")
    if not cond:
        fails.append(name)


tmp = Path(tempfile.mkdtemp(prefix="dcskills-"))
home = tmp / "home"
home.mkdir()


def git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(cwd), *args],
                   check=True, capture_output=True)


def skill_md(name, desc):
    return f'---\nname: {name}\ndescription: "{desc}\\n"\n---\n\n# {name}\n\nBody of {name}.\n'


# an upstream with two skills and a template, a clone of it left on another branch
upstream = tmp / "upstream.git"
subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(upstream)], check=True)
work = tmp / "work"
subprocess.run(["git", "clone", "-q", str(upstream), str(work)], check=True, capture_output=True)
git(work, "checkout", "-q", "-b", "main")
for rel, text in {
        "plugins/pay/skills/award-rules/SKILL.md": skill_md("award-rules", "Award rules. Use for pay."),
        "plugins/pay/skills/award-rules/references/rates.md": "rates table\n",
        "plugins/ros/skills/roster-rules/SKILL.md": skill_md("roster-rules", "Roster rules."),
        "templates/my-skill/SKILL.md": skill_md("my-skill", "Template."),
}.items():
    (work / rel).parent.mkdir(parents=True, exist_ok=True)
    (work / rel).write_text(text, encoding="utf-8")
git(work, "add", "-A")
git(work, "commit", "-q", "-m", "skills")
git(work, "push", "-q", "origin", "main")
clone = tmp / "clone"
subprocess.run(["git", "clone", "-q", str(upstream), str(clone)], check=True, capture_output=True)
git(clone, "checkout", "-q", "-b", "feature")
(clone / "plugins/pay/skills/award-rules/SKILL.md").write_text(
    skill_md("award-rules", "LOCAL EDIT"), encoding="utf-8")


def write_config(cfg):
    (home / "config.json").write_text(json.dumps(cfg), encoding="utf-8")


def make_repo(name) -> Path:
    root = tmp / name
    (root / ".agent").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    text = dc_state.template_text().replace(
        "<!-- DC:ROADMAP -->\n| id | goal | files | status |\n|---|---|---|---|\n",
        "<!-- DC:ROADMAP -->\n| id | goal | files | status |\n|---|---|---|---|\n"
        "| m1 | build | a.py | active |\n")
    (root / ".agent" / "drainclamp-state.md").write_text(text, encoding="utf-8")
    return root


def run(script, root, *args):
    proc = subprocess.run([sys.executable, "-B", str(SCRIPTS / script), "--root", str(root),
                           "--home", str(home), *args], capture_output=True, text=True,
                          encoding="utf-8")
    return proc.returncode, proc.stdout + proc.stderr


repo = make_repo("proj")

# 1. no sources: nothing offered, and next says nothing about skills
rc, out = run("dc_skills.py", repo, "list")
check("no skill_sources offers nothing", rc == 0 and "no skill_sources" in out, out)
rc, out = run("dc_project.py", repo, "next")
check("next is silent about skills with no source", rc == 0 and "Skills" not in out, out)

# 2. a source lists its skills from the ref, skipping templates
write_config({"skill_sources": [{"repo": str(clone)}, {"repo": 5}, "junk"]})
rc, out = run("dc_skills.py", repo, "list")
check("list shows both skills", rc == 0 and "award-rules - Award rules." in out
      and "roster-rules" in out, out)
check("templates are not offered", "my-skill" not in out, out)
check("list reads the ref, not the working tree", "LOCAL EDIT" not in out, out)
check("an unmarked project says so", "Marked: not yet" in out, out)
rc, out = run("dc_project.py", repo, "next")
check("next asks Gate 1 to mark skills", "Skills: not marked" in out, out)

# 3. set refuses unknown names and empty lists; marks known ones
rc, out = run("dc_skills.py", repo, "set", "--skills", "award-rules,nope")
check("set refuses a name in no source", rc != 0 and "nope" in out, out)
rc, out = run("dc_skills.py", repo, "set", "--skills", " , ")
check("set refuses an empty list", rc != 0 and "--none" in out, out)
rc, out = run("dc_skills.py", repo, "set", "--skills", "award-rules")
data = dc_project.load(repo / ".agent")
check("set stores the marked skill in the sidecar", rc == 0
      and data["skills"]["names"] == ["award-rules"], out)
rc, out = run("dc_project.py", repo, "next")
check("next names the marked skill and how to load it",
      "Skills: award-rules - load each with `dc_skills.py show <name>`" in out, out)
rc, out = run("dc_skills.py", repo, "list")
check("list flags the marked skill", "[x] award-rules" in out and "[ ] roster-rules" in out, out)

# 4. --none: marked as needing nothing, next stays quiet
other = make_repo("plain")
rc, out = run("dc_skills.py", other, "set", "--none")
rc2, nxt = run("dc_project.py", other, "next")
check("--none marks no skill and next stays quiet", rc == 0 and "none needed" in out
      and "Skills" not in nxt, (out, nxt))

# 5. show prints the ref's text and the skill's own files, never outside the folder
rc, out = run("dc_skills.py", repo, "show", "award-rules")
check("show prints the published SKILL.md", rc == 0 and "Body of award-rules" in out
      and "LOCAL EDIT" not in out, out)
rc, out = run("dc_skills.py", repo, "show", "award-rules", "--file", "references/rates.md")
check("show --file reads a file beside the skill", rc == 0 and "rates table" in out, out)
for bad in ("../../ros/skills/roster-rules/SKILL.md", "/etc/passwd"):
    rc, out = run("dc_skills.py", repo, "show", "award-rules", "--file", bad)
    check(f"show --file refuses {bad}", rc != 0 and "inside the skill folder" in out, out)
rc, out = run("dc_skills.py", repo, "show", "missing")
check("show names an unknown skill", rc != 0 and "missing" in out, out)

# 6. --fetch picks up a new upstream commit without touching the clone's branch
(work / "plugins/ros/skills/roster-rules/SKILL.md").write_text(
    skill_md("roster-rules", "Roster rules v2."), encoding="utf-8")
git(work, "commit", "-q", "-am", "v2")
git(work, "push", "-q", "origin", "main")
rc, out = run("dc_skills.py", repo, "list")
check("without --fetch the last fetched copy is used", "Roster rules v2" not in out, out)
rc, out = run("dc_skills.py", repo, "list", "--fetch")
branch = subprocess.run(["git", "-C", str(clone), "branch", "--show-current"],
                        capture_output=True, text=True).stdout.strip()
check("--fetch reads the newest published text", rc == 0 and "Roster rules v2" in out, out)
check("the clone stays on its own branch with its edit", branch == "feature"
      and "LOCAL EDIT" in (clone / "plugins/pay/skills/award-rules/SKILL.md").read_text())

# 7. an unreachable remote warns and falls back; a duplicate name keeps the first source
moved = tmp / "upstream-moved.git"
shutil.move(str(upstream), str(moved))
rc, out = run("dc_skills.py", repo, "list", "--fetch")
check("an offline fetch warns and still lists", rc == 0 and "could not fetch" in out
      and "roster-rules" in out, out)
dup = tmp / "dup"
subprocess.run(["git", "clone", "-q", str(moved), str(dup)], check=True, capture_output=True)
write_config({"skill_sources": [{"repo": str(clone)}, {"repo": str(dup), "ref": "origin/main"}]})
rc, out = run("dc_skills.py", repo, "list")
check("a duplicate name warns and the first source wins",
      rc == 0 and "first source wins" in out and out.count("award-rules -") == 1, out)

# 8. frontmatter parsing
fm = dc_skills.frontmatter('---\nname: x-y\ndescription: \'Quoted "inner" text\'\n---\nbody')
check("frontmatter strips matching quotes only", fm == {"name": "x-y",
      "description": 'Quoted "inner" text'}, fm)
check("no frontmatter gives nothing", dc_skills.frontmatter("# just text") == {})

# r84: a malformed or non-object config.json gives no sources
for bad_cfg in ("{not json", "[1, 2]"):
    (home / "config.json").write_text(bad_cfg, encoding="utf-8")
    check(f"sources() is empty for config {bad_cfg!r}", dc_skills.sources(str(home)) == [],
          dc_skills.sources(str(home)))
write_config({"skill_sources": [{"repo": str(clone)}]})

# r85: git launch failures and timeouts become DcError
real_run = dc_skills.subprocess.run
for exc in (OSError("no git"), subprocess.TimeoutExpired(cmd="git", timeout=1)):
    def boom(*a, _exc=exc, **k):
        raise _exc
    dc_skills.subprocess.run = boom
    msg = ""
    try:
        dc_skills._git(Path("."), "status")
    except dc_skills.DcError as err:
        msg = str(err)
    finally:
        dc_skills.subprocess.run = real_run
    check(f"_git turns {type(exc).__name__} into a DcError", "git status failed in" in msg, msg)

# r90: a non-ASCII skill folder is listed
cafe = tmp / "cafe-src"
cafe.mkdir()
subprocess.run(["git", "init", "-q", str(cafe)], check=True)
(cafe / "café-skill").mkdir()
(cafe / "café-skill" / "SKILL.md").write_text(skill_md("cafe-skill", "Cafe skill."), encoding="utf-8")
git(cafe, "add", "-A")
git(cafe, "commit", "-q", "-m", "cafe")
write_config({"skill_sources": [{"repo": str(cafe), "ref": "HEAD"}]})
rc, out = run("dc_skills.py", repo, "list")
check("a non-ASCII skill folder is listed", rc == 0 and "cafe-skill" in out, out)
write_config({"skill_sources": [{"repo": str(clone)}]})

shutil.rmtree(tmp, ignore_errors=True)
print("FAILURES: " + (", ".join(fails) if fails else "none"))
sys.exit(1 if fails else 0)
