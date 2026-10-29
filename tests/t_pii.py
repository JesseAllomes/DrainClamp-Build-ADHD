"""PII push gate (dc_pii.py): pii-redact parity, the smart name rule, the pre-push scan.

Sample PII is assembled at run time (J(...)) so that no line of this file matches a
detector and trips the gate on its own repository.
"""
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "drainclamp-build-adhd" / "scripts"
REPO = SCRIPTS.parent.parent.parent
os.environ.setdefault("DRAINCLAMP_HOME", tempfile.mkdtemp(prefix="dcreg-"))  # never the real config
sys.path.insert(0, str(SCRIPTS))
import dc_pii  # noqa: E402

fails = []


def check(name, cond, detail=""):
    detail = str(detail) if detail else ""
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  ' + detail if detail else ''}")
    if not cond:
        fails.append(name)


def J(*parts):
    return "".join(parts)


def kinds(text, **kw):
    return [f["kind"] for f in dc_pii.scan(text, **kw)]


def values(text, **kw):
    return [f["value"] for f in dc_pii.scan(text, **kw)]


JANE = J("Ja", "ne")
SMITH = J("Smi", "th")
EMAIL = J("jane", "@", "acme.com")
MOBILE = J("0412", " 345", " 678")

# 1. parity with pii-redact's own test cases (hooks/pii-redact.test.ts), its default modes
check("email and phone", kinds(J("email ", EMAIL, " or ring ", MOBILE), name_mode="all")
      == ["EMAIL", "PHONE"])
check("TFN with separators", kinds(J("TFN 123", " 456", " 782 for the file"), name_mode="all") == ["TFN"])
mixed = J("Participant ", JANE, " ", SMITH, " and D", "r Robert O'", "Brien met; roster: Ngu", "yen, Van.")
check("names in all mode, title and Last, First",
      [dc_pii.short_name(v) for v in values(mixed, name_mode="all")]
      == [J(JANE, " S."), "Robert O.", "Van N."], values(mixed, name_mode="all"))
ctx = J(JANE, " ", SMITH, " rang. Client: ", JANE, " ", SMITH, ".")
found = dc_pii.scan(ctx, name_mode="context")
check("context mode leaves the bare name alone", len(found) == 1 and found[0]["start"] > 10, found)
gov = J("CRN 123", " 456 789A, My Aged Care A", "C1234", "5678, TAC claim 12", "345678, DVA NX12",
        "3456, ABN 51 824", " 753 556, passport PA123", "4567")
check("government identifiers", kinds(gov, name_mode="all")
      == ["CENTRELINK", "AGEDCARE", "TAC", "DVA", "ABN", "PASSPORT"], kinds(gov, name_mode="all"))
listed = J(JANE, " ", SMITH, ", Van Ngu", "yen and Bob Jo", "nes at the Mil", "dura office")
terms = [J(JANE, " ", SMITH), J("Ngu", "yen, Van"), J("Mil", "dura")]
check("listed mode masks only the list", values(listed, name_mode="listed", terms=terms)
      == [J(JANE, " ", SMITH), J("Van Ngu", "yen"), J("Mil", "dura")])
check("allowlist domain and extra terms", kinds(J("Mil", "dura office <", EMAIL, "> rang"),
      terms=[J("Mil", "dura")], allowlist=["@acme.com"], name_mode="all") == ["NAME"])
check("a landline in a page", kinds(J("<p>Call us on (03) 5021", " 1234</p>")) == ["PHONE"])
key = J("sk-", "ant-api03-", "abcdefghijklmnopqrstuvwxyz0123456789")
sec = J("ANTHROPIC_API_KEY=", key, "; refresh_", "token: \"rt_9f8e7d6c5b4a3210zz\"; const token = request.headers.token")
check("secrets, and a code reference left alone", kinds(sec) == ["SECRET", "SECRET"], kinds(sec))
check("a reserved test domain is not an email", kinds(J("a", "@b.example.invalid")) == [])
check("a no-reply mailbox is no person's address", kinds(J("Co-Authored-By: C <noreply", "@anthropic.com>"))
      == [] and kinds(J("1+u@users", ".noreply.github.com")) == [])
check("a Luhn-valid run not starting 2-6 is not a card", kinds(J("1761", "234567890", "12")) == []
      and kinds(J("4111 1111", " 1111 1111")) == ["CARD"])

# 2. the smart name rule (the gate default): people, not Title Case phrases
for text, want in [
        (J(JANE, " ", SMITH, " rang about the roster."), [J(JANE, " ", SMITH)]),
        (J(JANE, " ", SMITH, " Support Worker"), [J(JANE, " ", SMITH)]),
        (J("Participant: Ju", "ne Taylor"), [J("Ju", "ne Taylor")]),
        (J("Dear M", "r Ngu", "yen"), [J("M", "r Ngu", "yen")]),
        (J("D", "r Jo", "nes"), [J("D", "r Jo", "nes")]),
        (J("Jos", "é Garc", "ía joined"), [J("Jos", "é Garc", "ía")]),
        (J(SMITH, ", ", JANE, ";Le", "e, Kim"), [J(SMITH, ", ", JANE), J("Le", "e, Kim")]),
        (J(SMITH.upper(), ", ", JANE.upper(), "\t38h"), [J(SMITH.upper(), ", ", JANE.upper())]),
        ("Payment for Weekday Day Rate", []), ("Name: Base Rate", []),
        ("Rate, Hours, Allowance", []), (J("Yesterday, ", JANE, " called"), []),
        ("Mon Tue Wed", []), ("Run Gate 4 then Context Purged", []), ("Mark Complete", []),
        ("NDIS Worker Screening Check", []), ("Claude Code session", []), ("Grant Access", []),
        ("License\n\nCopyright", [])]:
    check(f"smart: {text!r}", values(text) == want, values(text))

# 3. the pre-push scan over a real repository and remote
tmp = Path(tempfile.mkdtemp(prefix="dcpii-"))
home = tmp / "home"
home.mkdir()
(home / "config.json").write_text(json.dumps({"pii_names_file": str(tmp / "no-names.txt")}),
                                  encoding="utf-8")
remote = tmp / "remote.git"
subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
repo = tmp / "repo"
subprocess.run(["git", "clone", "-q", str(remote), str(repo)], check=True, capture_output=True)


def git(*args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(repo), *args],
                          check=True, capture_output=True, text=True).stdout.strip()


def commit(files, msg="change"):
    for rel, body in files.items():
        p = repo / rel
        if body is None:
            p.unlink()
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        (p.write_bytes if isinstance(body, bytes) else lambda b: p.write_text(b, encoding="utf-8"))(body)
    git("add", "-A")
    git("commit", "-q", "-m", msg)
    return git("rev-parse", "HEAD")


def pii(*args, stdin=""):
    proc = subprocess.run([sys.executable, "-B", str(SCRIPTS / "dc_pii.py"), "--root", str(repo),
                           "--home", str(home), *args], input=stdin, capture_output=True,
                          text=True, encoding="utf-8")
    return proc.returncode, proc.stdout + proc.stderr


def prepush():
    """The ref line git hands a pre-push hook for `git push origin HEAD:main`."""
    try:
        rsha = git("rev-parse", "origin/main")
    except subprocess.CalledProcessError:
        rsha = dc_pii.ZERO
    return pii("prepush", stdin=f"refs/heads/main {git('rev-parse', 'HEAD')} refs/heads/main {rsha}\n")


def push():
    git("push", "-q", "origin", "HEAD:main")
    git("fetch", "-q")


commit({"README.md": "Nothing personal here.\n"}, "start")
rc, out = prepush()
check("a clean first push passes", rc == 0 and "clean" in out, out)
push()

c1 = commit({"owner.txt": J("owner ", EMAIL, "\n")}, "add owner")
commit({"owner.txt": None}, "drop owner")
rc, out = prepush()
check("PII added then deleted is still refused (every commit scanned)",
      rc != 0 and f"{c1[:8]} owner.txt:1  EMAIL" in out, out)
check("the refusal never prints the value", EMAIL not in out, out)
git("reset", "-q", "--hard", "origin/main")

commit({"notes.txt": "ok\n"}, J("call ", MOBILE))
rc, out = prepush()
check("a commit message is scanned", rc != 0 and "(commit message):1  PHONE" in out, out)
git("reset", "-q", "--hard", "origin/main")

commit({J(JANE, "_", SMITH, "_payslip.txt"): "x\n"}, "file")
rc, out = prepush()
check("a file name is scanned", rc != 0 and "(file name)" in out and "NAME" in out, out)
git("reset", "-q", "--hard", "origin/main")

buf = io.BytesIO()
with zipfile.ZipFile(buf, "w") as z:
    z.writestr("[Content_Types].xml", "<Types/>")
    z.writestr("xl/sharedStrings.xml", J("<sst><si><t>TFN 123", " 456 782</t></si></sst>"))
commit({"rates.xlsx": buf.getvalue()}, "sheet")
rc, out = prepush()
check("text inside an .xlsx is scanned", rc != 0 and "rates.xlsx" in out and "TFN" in out, out)
git("reset", "-q", "--hard", "origin/main")

commit({"logo.png": b"\x89PNG\r\n\x1a\n\0\0\0binary"}, "logo")
rc, out = prepush()
check("an unreadable binary is refused", rc != 0 and "logo.png  UNSCANNABLE" in out, out)
commit({dc_pii.ALLOW_FILE: "path *.png  logo, no people\n"}, "allow logo")
rc, out = prepush()
check("a path rule in the pushed allow file lets it through", rc == 0, out)
push()

c2 = commit({"team.md": J("Contact ", JANE, " ", SMITH, " for help\n")}, "team")
rc, out = prepush()
check("a person's name is refused", rc != 0 and "team.md:1  NAME" in out, out)
rc, out = pii("allow", "--at", f"{c2}:team.md:1", "--kind", "NAME", "--reason", "public author")
allow_text = (repo / dc_pii.ALLOW_FILE).read_text(encoding="utf-8")
check("allow writes a hashed rule, never the value", rc == 0 and SMITH not in allow_text
      and dc_pii.value_hash(J(JANE, " ", SMITH)) in allow_text, (out, allow_text))
rc, out = prepush()
check("an allow rule not yet committed does not count", rc != 0, out)
git("add", "-A")
git("commit", "-q", "-m", "allow author")
rc, out = prepush()
check("the committed allow rule lets the name through", rc == 0, out)
push()

commit({dc_pii.ALLOW_FILE: "nonsense line\n"}, "break allow")
rc, out = prepush()
check("a malformed allow file refuses (fail closed)", rc != 0 and "fail closed" in out, out)
git("reset", "-q", "--hard", "origin/main")

head = git("rev-parse", "HEAD")
rc, out = pii("prepush", stdin=f"refs/heads/dc/proj-c1 {head} refs/heads/dc/proj-c1 {dc_pii.ZERO}\n")
check("a builder branch dc/* is never pushed", rc != 0 and "stays local" in out, out)
rc, out = pii("prepush", stdin=f"refs/heads/main {head} refs/heads/dc/x {dc_pii.ZERO}\n")
check("nor is anything pushed onto a dc/* remote branch", rc != 0 and "stays local" in out, out)
rc, out = pii("prepush", stdin=f"(delete) {dc_pii.ZERO} refs/heads/old {git('rev-parse', 'HEAD')}\n")
check("deleting a remote branch publishes nothing", rc == 0, out)
rc, out = prepush()
check("commits already on the remote are not scanned again", rc == 0 and "0 commit(s)" in out, out)

(home / "config.json").write_text("{not json", encoding="utf-8")
commit({"a.txt": "fine\n"}, "fine")
rc, out = prepush()
check("an unreadable config refuses (fail closed)", rc != 0 and "fail closed" in out, out)
(home / "config.json").write_text("{}", encoding="utf-8")

# history audit: --remote sees only what the remote holds; --full lists everything
commit({"local.txt": J("ring ", MOBILE, "\n")}, "local only")
rc, out = pii("scan", "--history", "--remote", "origin")
check("a remote history audit ignores unpushed commits", rc == 0 and "clean" in out, out)
rc, out = pii("scan", "--history", "--full")
check("a full-history audit sees local commits too", rc != 0 and "local.txt:1  PHONE" in out, out)
git("reset", "-q", "--hard", "origin/main")

# 4. drift against a pii-redact checkout
src = tmp / "pii-redact"
subprocess.run(["git", "init", "-q", "-b", "main", str(src)], check=True)
(src / "hooks").mkdir()
(src / "hooks" / "redact.ts").write_text("// detectors v2\n", encoding="utf-8")
subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(src), "add", "-A"], check=True)
subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(src), "commit", "-q",
                "-m", "v2"], check=True)
(home / "config.json").write_text(json.dumps({"pii_redact_repo": str(src), "pii_redact_ref": "main"}),
                                  encoding="utf-8")
rc, out = pii("drift")
check("a changed pii-redact detector file warns", rc != 0 and "changed since the port" in out, out)
saved = dict(dc_pii.PORTED_FROM)
dc_pii.PORTED_FROM["sha256"] = hashlib.sha256(b"// detectors v2\n").hexdigest()
check("a matching hash is quiet", dc_pii.cmd_drift(str(home)) == 0)
dc_pii.PORTED_FROM.update(saved)
(home / "config.json").write_text("{}", encoding="utf-8")
rc, out = pii("drift")
check("drift with no pii_redact_repo says it was not checked", rc == 0 and "not checked" in out, out)

# 5. this repository's own files pass its gate (with its committed allow file)
rules = dc_pii.parse_allow((REPO / dc_pii.ALLOW_FILE).read_text(encoding="utf-8")
                           if (REPO / dc_pii.ALLOW_FILE).exists() else "")
listed_files = subprocess.run(["git", "-C", str(REPO), "ls-files", "-co", "--exclude-standard"],
                              capture_output=True, text=True).stdout.splitlines()
hits = []
for rel in listed_files:
    p = REPO / rel
    if not p.is_file() or rel.startswith("tests/fixtures/"):
        continue
    try:
        blob = p.read_bytes()
    except OSError:
        continue
    dc_pii.scan_blob(REPO, hits, {"name_mode": "smart", "terms": [], "allowlist": []}, rules,
                     "WORKTREE", rel, blob)
check("the repository's own files scan clean", not hits,
      [f"{h['where']}:{h['line']} {h['kind']}" for h in hits[:10]])

shutil.rmtree(tmp, ignore_errors=True)
print("FAILURES: " + (", ".join(fails) if fails else "none"))
sys.exit(1 if fails else 0)
