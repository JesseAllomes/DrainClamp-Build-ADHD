#!/usr/bin/env python3
"""PII push gate: nothing pushed to a remote may carry personal data or secrets.

Local files may hold PII; what leaves the machine must not. `dc_pii.py` scans
exactly what a push would publish and refuses when it finds anything:

- every commit being pushed, not just the tip (PII added in one commit and
  deleted in the next is still published), as added lines only;
- each commit's message and the names of files it adds or renames;
- the text inside .xlsx/.xlsm/.docx/.pptx files;
- other binary files (PDF, images, archives) cannot be read, so they are
  refused unless a path rule in the allow file covers them.

A refusal names the commit, file, line and kind, never the value. Any error,
timeout or unreadable object refuses the push (fail closed).

The detectors are a port of the pii-redact mod's `hooks/redact.ts` (see
PORTED_FROM). `drift` compares that file's current hash with the one ported,
so an update to pii-redact is flagged rather than silently missed.

False positives are allowed in `.drainclamp-pii-allow` at the repository root,
read from the commit being pushed so the allow list is itself reviewed:

    value <kind> sha256:<16 hex of the lower-cased value>  <reason>
    path <glob>  <reason>

`allow --at <rev>:<file>:<line> --kind K --reason R` writes a value entry from
a reported location, so the value itself is never typed or printed. Personal
allowances (your own name and address) go in `pii_allow` in
~/.drainclamp/config.json instead and never reach the repository.

Commands: `prepush` (git pre-push hook: reads the ref lines on stdin), `scan`
(--range A..B | --history | --tree), `allow`, `drift`.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import unicodedata
import zipfile
from pathlib import Path

import _dcio
import dc_registry
from _dcio import DcError

# Where the detectors came from. `drift` hashes this file at the configured
# pii-redact checkout's ref and warns when it no longer matches.
PORTED_FROM = {"file": "hooks/redact.ts", "commit": "abe73d3",
               "sha256": "51d86f4de31c916670611e9a01fc4596d73d156a788ef101b11337350d835ddb"}

ALLOW_FILE = ".drainclamp-pii-allow"
CONFIG_NAME = "config.json"
NAMES_FILE = Path(os.environ.get("ProgramData", "C:/ProgramData")) / "pii-redact" / "names.txt"
ZERO = "0" * 40
GIT_TIMEOUT = 120
MAX_BLOB = 20 * 1024 * 1024
REPORT_CAP = 25
OFFICE = (".xlsx", ".xlsm", ".docx", ".docm", ".pptx", ".pptm")
NAME_MODES = ("smart", "listed", "context", "all")
F = re.ASCII  # JavaScript's \w, \d, \s and \b are ASCII; keep parity


# -- detectors (ported from pii-redact hooks/redact.ts) ----------------------

def _digits(s: str) -> str:
    return re.sub(r"\D", "", s)


def _all_same(d: str) -> bool:
    return bool(re.fullmatch(r"(\d)\1+", d))


def _luhn(d: str) -> bool:
    total, double = 0, False
    for ch in reversed(d):
        n = ord(ch) - 48
        if double:
            n *= 2
            n -= 9 if n > 9 else 0
        total += n
        double = not double
    return total % 10 == 0


def _tfn_ok(d: str) -> bool:
    w = ([1, 4, 3, 7, 5, 8, 6, 9, 10] if len(d) == 9 else
         [10, 7, 8, 4, 6, 3, 5, 1] if len(d) == 8 else None)
    if not w or _all_same(d):
        return False
    return sum((ord(c) - 48) * w[i] for i, c in enumerate(d)) % 11 == 0


def _abn_ok(d: str) -> bool:
    if len(d) != 11 or _all_same(d):
        return False
    w = [10, 1, 3, 5, 7, 9, 11, 13, 15, 17, 19]
    return sum((ord(c) - 48 - (1 if i == 0 else 0)) * w[i] for i, c in enumerate(d)) % 89 == 0


def _acn_ok(d: str) -> bool:
    if len(d) != 9 or _all_same(d):
        return False
    s = sum((ord(d[i]) - 48) * w for i, w in enumerate([8, 7, 6, 5, 4, 3, 2, 1]))
    return (10 - s % 10) % 10 == ord(d[8]) - 48


def _medicare_ok(d: str) -> bool:
    if not 10 <= len(d) <= 11 or _all_same(d) or not 2 <= ord(d[0]) - 48 <= 6:
        return False
    s = sum((ord(d[i]) - 48) * w for i, w in enumerate([1, 3, 7, 9, 1, 3, 7, 9]))
    return s % 10 == ord(d[8]) - 48


_HINT = re.compile(r"(tfn|tax file|abn|acn|medicare|card|ndis|participant|member|reference|ref|"
                   r"number|no\.?|#|id)\s*:?\s*$", re.I | F)


def _hint(text: str, i: int) -> bool:
    return bool(_HINT.search(text[max(0, i - 32):i]))


def _sep(s: str) -> bool:
    return bool(re.search(r"[ -]", s))


_PRIVATE_IP = re.compile(r"^(?:10\.|127\.|0\.|169\.254\.|192\.168\.|172\.(?:1[6-9]|2\d|3[01])\.|"
                         r"255\.255\.255\.255$|224\.|239\.)", F)
# Gate deviation: any reserved test domain (RFC 2606/6761: .example .invalid .localhost .test).
_DUMMY_EMAIL = re.compile(r"@(?:example\.(?:com|org|net)|localhost|test|invalid|email\.com|"
                          r"domain\.com|acme\.test|(?:[A-Za-z0-9-]+\.)+(?:example|invalid|"
                          r"localhost|test))$", re.I | F)
# Gate deviations: a no-reply mailbox (commit trailers, GitHub's noreply addresses) is no
# person's address, and a card number starts with an issuer digit 2-6 (an epoch-ms timestamp or
# a token count in JSON starts with 1 and only passes Luhn by chance).
_ROBOT_EMAIL = re.compile(r"(?:^no-?reply@|@users\.noreply\.github\.com$)", re.I | F)
_STREET = ("Street|St|Road|Rd|Avenue|Ave|Av|Drive|Dr|Court|Ct|Crescent|Cres|Cr|Place|Pl|Lane|Ln|"
           "Way|Parade|Pde|Highway|Hwy|Terrace|Tce|Close|Cl|Boulevard|Blvd|Bvd|Circuit|Cct|"
           "Esplanade|Esp|Grove|Gr|Rise|Square|Sq|Track|Trk|Walk|Mews|Loop|Row|Vista|View|Chase|"
           "Circle|Cir|Promenade|Prom|Quay|Mall|Retreat|Rtt")
_ID = r"((?:[A-Z]{1,3}[ -]?)?\d(?:[ -]?\d){3,15}(?:[ -]?[A-Z]{1,2})?)"
_GAP = r"\s*(?:no\.?|number|num|#|id|ref(?:erence)?)?\s*[:#=-]?\s*"


def _id_like(v, *_):
    return len(_digits(v)) >= 4


def _d(kind, pattern, flags=0, group=None, keep=None, replace=None):
    return {"kind": kind, "re": re.compile(pattern, flags | F), "group": group,
            "keep": keep, "replace": replace}


DETECTORS = [
    _d("EMAIL", r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
                r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)*\.[A-Za-z]{2,24}",
       keep=lambda v, t, i: not _DUMMY_EMAIL.search(v) and not _ROBOT_EMAIL.search(v)
       and not re.search(r"://[^\s/:@]+:$", t[max(0, i - 80):i], F)),
    _d("CARD", r"(?<![\d-])(?:\d[ -]?){12,18}\d(?![\d-])",
       keep=lambda v, t, i: 13 <= len(_digits(v)) <= 19 and not _all_same(_digits(v))
       and _digits(v)[0] in "23456" and _luhn(_digits(v))),
    _d("DOB", r"\b(?:DOB|D\.O\.B\.?|date of birth|birth ?date|born(?: on)?)\s*[:=-]?\s*"
              r"(\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}|\d{4}-\d{2}-\d{2}|\d{1,2}(?:st|nd|rd|th)?\s+"
              r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s+\d{4})",
       re.I, group=1),
    _d("BANK", r"\bBSB\s*(?:no\.?|number|#)?\s*:?\s*(\d{3}[ -]?\d{3})\b", re.I, group=1),
    _d("BANK", r"\b(?:acc(?:oun)?t\.?|account)\s*(?:no\.?|number|#)?\s*:?\s*(\d{5,10})\b",
       re.I, group=1),
    _d("CENTRELINK", r"\b(\d{3}[ -]?\d{3}[ -]?\d{3}[A-Za-z])\b", group=1),
    _d("AGEDCARE", r"\bAC[ -]?\d{8}\b"),
    _d("ABN", r"\b(\d{2}[ -]?\d{3}[ -]?\d{3}[ -]?\d{3})\b", group=1,
       keep=lambda v, t, i: _abn_ok(_digits(v))),
    _d("MEDICARE", r"\b([2-6]\d{3}[ -]?\d{5}[ -]?\d(?:[ /-]?\d)?)\b", group=1,
       keep=lambda v, t, i: _medicare_ok(_digits(v)) and (_sep(v) or _hint(t, i))),
    _d("TFN", r"\b(\d{3}[ -]?\d{3}[ -]?\d{2,3})\b", group=1,
       keep=lambda v, t, i: _tfn_ok(_digits(v)) and (_sep(v) or _hint(t, i))),
    _d("ACN", r"\b(\d{3}[ -]?\d{3}[ -]?\d{3})\b", group=1,
       keep=lambda v, t, i: _acn_ok(_digits(v)) and (_sep(v) or _hint(t, i))),
    _d("NDIS", r"\b43\d{7}\b"),
    _d("CENTRELINK", rf"\b(?:Centrelink|CRN|customer reference(?: number)?){_GAP}{_ID}\b",
       re.I, group=1, keep=_id_like),
    _d("AGEDCARE", rf"\b(?:My Aged Care|aged care(?: client| ID| number| no\.?)?|MAC(?: ID)?)"
                   rf"{_GAP}{_ID}\b", re.I, group=1, keep=_id_like),
    _d("TAC", rf"\b(?:TAC|Transport Accident Commission)(?:\s+claim)?{_GAP}{_ID}\b",
       re.I, group=1, keep=_id_like),
    _d("DVA", rf"\b(?:DVA|Veterans'? Affairs|veteran|gold card|white card)(?:\s+(?:file|card))?"
              rf"{_GAP}([A-Z]{{1,4}}\s?\d{{3,8}}[A-Z]?)\b", re.I, group=1),
    _d("PASSPORT", rf"\bpassport{_GAP}([A-Z]{{1,2}}\s?\d{{6,8}}|\d{{8,9}})\b", re.I, group=1),
    _d("LICENCE", rf"\b(?:driver'?s?\s+)?licen[cs]e{_GAP}([A-Z0-9]{{6,12}})\b", re.I, group=1,
       keep=_id_like),
    _d("CLAIM", rf"\b(?:WorkCover|WorkSafe|iCare|icare|Comcare|claim|compensation|"
                rf"insurance policy|policy){_GAP}{_ID}\b", re.I, group=1, keep=_id_like),
    _d("GOVID", rf"\b(?:case|client|customer|member|participant|patient|resident|pension|"
                rf"concession|health ?care card|HCC|PCC|SRSS|ImmiCard|visa grant|visa|CHSP|HCP|"
                rf"home care package|SIL|SDA|plan){_GAP}{_ID}\b", re.I, group=1, keep=_id_like),
    _d("PHONE", r"(?<![\d.\w])(?:\+61[ -]?(?:\(0\)[ -]?)?[2-478](?:[ -]?\d){8}|\(0[2-478]\)[ -]?"
                r"\d{4}[ -]?\d{4}|0[2-478](?:[ -]?\d){8}|\+(?!61\b)\d{1,3}(?:[ .-]?\(?\d{1,4}\)?)"
                r"{2,5})(?![\d\w])",
       keep=lambda v, t, i: 8 <= len(_digits(v)) <= 15 and not _all_same(_digits(v))),
    _d("ADDRESS", r"\b(?:(?:Unit|Apt|Apartment|Flat|Suite|Lot|Level)\s*\d+[A-Za-z]?[,/ ]+)?"
                  r"\d{1,5}[A-Za-z]?(?:\s*[/-]\s*\d{1,5}[A-Za-z]?)?\s+(?:[A-Z][A-Za-z'’-]+\s+)"
                  rf"{{1,3}}(?:{_STREET})\b\.?(?:,?\s+(?:[A-Z][A-Za-z'’-]+\s?){{1,3}},?\s+"
                  r"(?:NSW|VIC|QLD|SA|WA|TAS|NT|ACT)\s+\d{4})?"),
    _d("IP", r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b",
       keep=lambda v, t, i: not _PRIVATE_IP.search(v)),
]


def _credential_like(v: str, *_) -> bool:
    v = re.sub(r"^[\"'`]|[\"'`]$", "", v)
    if len(v) < 12 or not re.search(r"\d", v) or not re.search(r"[A-Za-z]", v):
        return False
    if re.match(r"(?:process\.env|os\.environ|env|settings|config|secrets|this|self|req|request|"
                r"res|response|ctx|options|params|args)\b", v, re.I | F):
        return False
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+", v):
        return False
    if re.search(r"[(){}<>$\s]", v):
        return False
    return not re.fullmatch(r"(?:x+|\*+|\.+|changeme|example|placeholder|your[-_a-z]*|redacted|"
                            r"null|undefined|true|false)", v, re.I)


SECRET_DETECTORS = [
    _d("SECRET", r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY(?: BLOCK)?-----[\s\S]*?"
                 r"-----END (?:[A-Z]+ )?PRIVATE KEY(?: BLOCK)?-----"),
    _d("SECRET", r"\b(?:Bearer|Basic|Token)\s+([A-Za-z0-9\-._~+/]{16,}=*)", group=1,
       keep=lambda v, t, i: bool(re.search(r"[\dA-Z]", v))),
    _d("SECRET", r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    _d("SECRET", r"(?<![A-Za-z0-9_-])(?:sk-ant-[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9_-]{20,}|"
                 r"gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{22,}|glpat-[A-Za-z0-9_-]{20,}|"
                 r"xox[abprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}|"
                 r"AIza[0-9A-Za-z_-]{35,}|(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{16,}|"
                 r"SG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}|npm_[A-Za-z0-9]{30,}|"
                 r"pypi-AgEI[A-Za-z0-9_-]{30,}|SK[0-9a-f]{32}|hf_[A-Za-z0-9]{30,}|"
                 r"vercel_[A-Za-z0-9]{20,}|sbp_[A-Za-z0-9]{30,})(?![A-Za-z0-9_-])"),
    _d("SECRET", r"\b(?:api[_-]?key|apikey|api[_-]?secret|secret[_-]?key|client[_-]?secret|secret|"
                 r"access[_-]?token|refresh[_-]?token|id[_-]?token|auth[_-]?token|"
                 r"session[_-]?token|bearer[_-]?token|token|password|passwd|pwd|private[_-]?key|"
                 r"signing[_-]?key|encryption[_-]?key|connection[_-]?string|authorization)\b"
                 r"[\"']?\s*[:=]\s*[\"'`]?([^\s\"'`,;]{12,})", re.I, group=1,
       keep=_credential_like),
    _d("SECRET", r"\b[a-z][a-z0-9+.-]*://[^\s/:@]+:([^\s/@]{6,})@", re.I, group=1),
]

# Gate deviation from pii-redact: a name never spans a line break ([ \t] for \s), so a heading
# followed by a Title Case paragraph is not read as a name in a file.
_TITLE = r"(?:(?:Mr|Mrs|Ms|Miss|Mx|Dr|Prof|Sir|Dame|Rev)\.?[ \t]+)?"
_PARTICLE = r"(?:(?:van|von|de|der|den|da|di|la|le|del|du|st)[ \t]+){0,2}"
_WORD = r"[A-Z][a-z]+(?:[-'’][A-Z]?[a-z]+)*"
_SURNAME = rf"{_PARTICLE}(?:Mc|Mac|O'|O’|D'|D’)?[A-Z][a-z]+(?:[-'’][A-Z]?[a-z]+)*"
_FULL_NAME = rf"(?<![\w/\\.@-]){_TITLE}({_WORD}(?:[ \t]+{_WORD})?[ \t]+{_SURNAME})(?![\w/\\@])"
_LAST_FIRST = rf"(?<![\w/\\.@-])({_SURNAME},[ \t]+{_WORD})(?![\w/\\@])"
_PEOPLE = re.compile(
    r"(?:participant|client|resident|carer|worker|staff|employee|contact|name|dear|attn|attention|"
    r"re|for|from|to|with|by|cc|signed|guardian|nominee|parent|mother|father|son|daughter|partner|"
    r"spouse|coordinator|manager|applicant|patient|customer|tenant|nurse|doctor|gp|hi|hello|thanks|"
    r"regards)\s*[:\-–]?\s*$", re.I | F)
_TITLE_BEFORE = re.compile(r"\b(?:Mr|Mrs|Ms|Miss|Mx|Dr|Prof|Sir|Dame|Rev)\.?\s+$", F)
NOT_A_NAME = set((
    "january february march april may june july august september october november december "
    "monday tuesday wednesday thursday friday saturday sunday "
    "australia australian victoria queensland tasmania sydney melbourne brisbane perth adelaide "
    "hobart darwin canberra mildura geelong bendigo ballarat "
    "new south north east west wales western northern southern eastern central territory capital "
    "the this that these those with from into onto over under about after before while when where "
    "which what who "
    "street road avenue drive court crescent place lane way parade highway terrace close boulevard "
    "circuit "
    "claude code anthropic opus sonnet haiku fable mythos openai codex gpt google microsoft apple "
    "amazon meta github gitlab "
    "visual studio react native node type script java python ruby rust swift kotlin windows linux "
    "macos android ios chrome edge firefox safari "
    "git hub pull request merge commit branch error warning info debug trace read write edit bash "
    "grep glob tool agent plugin hook session settings config model project file path line "
    "sunraysia residential services support worker workers plan manager provider participant "
    "client invoice payment total amount date time name first last email phone address status "
    "active inactive pending complete completed cancelled "
    "national disability insurance scheme aged care home community access daily living core "
    "capacity building capital group centre program service agreement policy procedure privacy "
    "terms conditions "
    "table column row index key value object array string number boolean function class interface "
    "module import export default public private static return true false null undefined "
    "dear hi hello regards kind thanks thank best yours sincerely faithfully team board committee "
    "department government commission transport accident centrelink medicare medibank bupa "
    "pty ltd inc co limited company corporation trust fund bank credit union health hospital "
    "school university college council shire city town state federal "
    "ndis ndia tac dva workcover worksafe ato asic abn acn tfn crn "
    "excel word outlook teams sharepoint onedrive dropbox slack notion jira confluence xero myob "
    "quickbooks keypay paycat employment hero shiftcare brevity lumary "
    "am pm").split())


def _name_word(w: str) -> bool:
    return re.sub(r"[^a-z]", "", w.lower()) not in NOT_A_NAME


def _name_parts(name: str):
    if "," in name:
        bits = re.split(r",\s*", name)
        last, first = (bits + ["", ""])[:2]
        return (first, last) if first and last else None
    words = re.split(r"\s+", name)
    return (words[0], words[-1]) if len(words) >= 2 else None


def short_name(name: str) -> str:
    """A full name (`First Last` or `Last, First`) -> `First L.`"""
    parts = _name_parts(name)
    if not parts:
        return name
    first, last = parts
    surname = re.sub(r"^(?:(?:van|von|de|der|den|da|di|la|le|del|du|st)\s+){0,2}", "", last,
                     flags=re.I)
    initial = (surname[:1] or last[:1])
    return f"{first} {initial.upper()}."


def _looks_like_name(v: str) -> bool:
    return bool(_name_parts(v)) and all(_name_word(w) for w in re.split(r"[\s,]+", v) if w)


def _name_detectors(mode: str) -> list[dict]:
    if mode == "listed":
        return []

    def keep(v, t, i):
        if not _looks_like_name(v):
            return False
        if mode == "all":
            return True
        before = t[max(0, i - 40):i]
        return bool(_TITLE_BEFORE.search(before) or _PEOPLE.search(before))
    if mode == "smart":
        return _smart_detectors()
    return [_d("NAME", _FULL_NAME, group=1, keep=keep, replace=short_name),
            _d("NAME", _LAST_FIRST, group=1, keep=keep, replace=short_name)]


# -- smart names: the gate's default (pii-redact review r24-r28 and its "recommended direction") --
# A Title Case pair is a person only when its first word is a known given name, or a title or a
# people label comes first. The name stops at the first word that cannot be a name, so
# a name followed by a job title is caught without the title. `Last, First` needs a label or a roster
# cell. Payroll vocabulary and day/month abbreviations are never names. Unicode letters,
# capitalised particles and internal capitals (DiMarco) are words; ALL-CAPS needs a roster cell.

_U, _L = "A-ZÀ-ÖØ-Þ", "a-zß-öø-ÿ"
_SW = rf"[{_U}][{_L}]+(?:[{_U}][{_L}]+)?(?:[-'’][{_U}]?[{_L}]+)*"
_SPART = r"(?:(?i:van|von|de|der|den|da|di|la|le|del|du|st|bin|binti|al|el)[ \t]+){0,2}"
_SSUR = rf"{_SPART}(?:Mc|Mac|O'|O’|D'|D’)?{_SW}"
_STITLE = r"((?:Mr|Mrs|Ms|Miss|Mx|Dr|Prof|Sir|Dame|Rev)\.?[ \t]+)?"
_CELL_START = r"(?:^|(?<=[\t;|\"]))[ \t]*"
_CELL_END = r"(?=[ \t]*(?:$|[\t;|\"]))"
SMART_FULL = re.compile(rf"(?<![\w/\\.@-]){_STITLE}({_SW}(?:[ \t]+{_SW})?[ \t]+{_SSUR})(?![\w/\\@])")
SMART_LAST_FIRST = re.compile(rf"(?<![\w/\\.@-])({_SSUR}),[ \t]+({_SW})(?![\w/\\@])")
SMART_TITLED = re.compile(rf"(?<![\w/\\.@-])(?:Mr|Mrs|Ms|Miss|Mx|Dr|Prof|Sir|Dame|Rev)\.?[ \t]+"
                          rf"({_SSUR})(?![\w/\\@])")
SMART_CAPS = re.compile(rf"{_CELL_START}([A-Z][A-Z'’-]+),?[ \t]+([A-Z][A-Z'’-]+){_CELL_END}", re.M)
_PEOPLE_WORDS = (r"participant|client|resident|carer|worker|staff|employee|contact|name|dear|attn|"
                 r"attention|signed|guardian|nominee|parent|mother|father|son|daughter|partner|"
                 r"spouse|coordinator|applicant|patient|customer|tenant|nurse|doctor|gp|hi|hello|"
                 r"regards|thanks")
# Strong: a title, or a label with a colon or dash ("Client: ", "From: "). Weak: a bare label word
# ("Worker Screening Check"), which counts only when a given name follows it.
_LABEL_STRONG = re.compile(
    rf"(?:\b(?:Mr|Mrs|Ms|Miss|Mx|Dr|Prof|Sir|Dame|Rev)\.?[ \t]+|\b(?:{_PEOPLE_WORDS}|from|to|cc|"
    r"bcc|by|for|with)[ \t]*[:\-–][ \t]*)$", re.I)
_LABEL_WEAK = re.compile(rf"\b(?:{_PEOPLE_WORDS})[ \t]+$", re.I)


def _labelled(before: str, first: str) -> bool:
    """A title or people label precedes the name (r27: it overrides the word lists)."""
    return bool(_LABEL_STRONG.search(before)) or (
        bool(_LABEL_WEAK.search(before)) and _plain(first) in given_names())
PAYROLL = set((
    "mon tue tues wed thu thur thurs fri sat sun jan feb mar apr jun jul aug sep sept oct nov dec "
    "rate rates penalty penalties loading allowance allowances overtime ordinary hours hour shift "
    "shifts broken sleepover night weekday weekend day days holiday holidays leave annual personal "
    "carers long award level pay payroll payrun run slip gross net super superannuation tax "
    "deduction deductions balance timesheet roster rosters casual permanent part full base "
    "registered officer chief executive accounts payable receivable purchase order trial summary "
    "report reports week weekly fortnight fortnightly monthly yearly public afternoon morning "
    "evening total totals gate gates release notes step steps").split())
# Given names that are also everyday words: they count only after a title or a label.
AMBIGUOUS = set((
    "may june april august will mark bill grace hope joy faith rose dawn summer ray art jack chase "
    "guy pat sue rob frank don max hunter harper mason cooper carter taylor jordan morgan bailey "
    "parker paige pearl ivy lily violet hazel holly ruby amber jade crystal destiny trinity aurora "
    "angel sky river reed miles lance clay drew dean dale glen cliff grant gene penny victoria "
    "sydney austin florence georgia brooklyn chelsea kit eve ken jean bob nell pia mai tate wade "
    "ted rex val win hank bea").split())
# Denylisted words that are also surnames: allowed as a surname after a label (r27).
SURNAME_OK = set("west lane best darwin north south east may street ford hill wood".split())
_GIVEN: set[str] | None = None


def given_names() -> set[str]:
    global _GIVEN
    if _GIVEN is None:
        path = Path(__file__).resolve().parent.parent / "assets" / "given-names.txt"
        text = _dcio.read_text(path)
        if text is None:
            raise DcError(f"{path} is missing; refusing (fail closed)")
        _GIVEN = {w for line in text.splitlines() for w in re.sub(r"#.*$", "", line).split()}
    return _GIVEN


def _plain(w: str) -> str:
    """Lower case letters only, accents folded (José -> jose) for the word lists."""
    folded = unicodedata.normalize("NFKD", w.lower())
    return re.sub(r"[^a-z]", "", "".join(c for c in folded if not unicodedata.combining(c)))


def _not_name(w: str) -> bool:
    p = _plain(w)
    return p in NOT_A_NAME or p in PAYROLL


def _smart_full(m, text: str):
    """(value, start) of the person in a SMART_FULL match, or None."""
    words = re.split(r"[ \t]+", m.group(2))
    labelled = bool(m.group(1)) or _labelled(text[max(0, m.start() - 40):m.start()], words[0])
    first = _plain(words[0])
    if not labelled and (first not in given_names() or first in AMBIGUOUS):
        return None
    if _not_name(words[0]) and not (labelled and first in given_names()):
        return None
    keep = [words[0]]
    for w in words[1:]:  # stop at the first word that cannot be a name (r24)
        if _not_name(w) and not (labelled and _plain(w) in SURNAME_OK):
            break
        keep.append(w)
    if len(keep) < 2:
        return None
    value = m.group(2)[:m.group(2).index(keep[-1]) + len(keep[-1])]
    return value, m.start(2)


def _smart_detectors() -> list[dict]:
    return [{"kind": "NAME", "re": SMART_FULL, "smart": "full"},
            {"kind": "NAME", "re": SMART_LAST_FIRST, "smart": "last_first"},
            {"kind": "NAME", "re": SMART_TITLED, "smart": "titled"},
            {"kind": "NAME", "re": SMART_CAPS, "smart": "caps"}]


def _smart_match(kind: str, m, text: str):
    """(value, start) for a smart detector's match, or None to drop it."""
    if kind == "full":
        return _smart_full(m, text)
    if kind == "titled":  # a title makes a lone surname a person
        sur = re.split(r"[ 	]+", m.group(1))[-1]
        return None if _not_name(sur) else (m.group(0), m.start())
    before = text[max(0, m.start() - 40):m.start()]
    line_start = text.rfind("\n", 0, m.start()) + 1
    cell = re.fullmatch(r"[ \t]*|.*[\t;|\"][ \t]*", text[line_start:m.start()], re.S) is not None
    after = text[m.end():text.find("\n", m.end()) if "\n" in text[m.end():] else len(text)]
    cell = cell and re.match(r"[ \t]*(?:$|[\t;|\"])", after) is not None
    if kind == "last_first":
        last, first = m.group(1), m.group(2)
        labelled = _labelled(before, first)
        if not (labelled or cell) or _not_name(first):
            return None
        if _not_name(last.split()[-1]) and not (labelled and _plain(last) in SURNAME_OK):
            return None
        if not labelled and (_plain(first) not in given_names() or _plain(first) in AMBIGUOUS):
            return None
        return m.group(0), m.start()
    a, b = m.group(1), m.group(2)  # caps: SMITH, JANE or JANE SMITH in a roster cell
    first = _plain(b) if "," in m.group(0) else _plain(a)
    other = a if "," in m.group(0) else b
    if first not in given_names() or first in AMBIGUOUS or _not_name(other):
        return None
    return m.group(0).strip(), m.start() + (len(m.group(0)) - len(m.group(0).lstrip()))


def _term_detectors(terms: list[str]) -> list[dict]:
    clean = list(dict.fromkeys(t.strip() for t in terms if len(t.strip()) >= 2))
    full = [t for t in clean if re.search(r"\s", t) and _name_parts(t)]
    single = [t for t in clean if t not in full]
    out = []
    if full:
        forms = []
        for t in full:
            first, last = _name_parts(t)
            last_re = re.sub(r"\\?\s+", r"\\s+", re.escape(last))
            middle = "" if "," in t else "".join(rf"\s+{re.escape(w)}" for w in re.split(r"\s+", t)[1:-1])
            forms += [rf"{re.escape(first)}{middle}\s+{last_re}", rf"{last_re},\s*{re.escape(first)}"]
        out.append(_d("NAME", rf"(?<![\w'’])(?:{'|'.join(forms)})(?![\w'’])", re.I,
                      replace=lambda v: short_name(re.sub(r"\s+", " ", v))))
    if single:
        out.append(_d("NAME", rf"(?<![\w'’])(?:{'|'.join(map(re.escape, single))})(?![\w'’])", re.I))
    return out


def parse_name_list(text: str) -> list[str]:
    out = []
    for raw in text.splitlines():
        line = re.sub(r"#.*$", "", raw).strip()
        if len(line) >= 2:
            out.append(line)
    return out


def _allowed(value: str, kind: str, allowlist: list[str]) -> bool:
    v = value.lower()
    for raw in allowlist:
        a = raw.strip().lower()
        if a and (a == v or (kind == "EMAIL" and a.startswith("@") and v.endswith(a))):
            return True
    return False


def scan(text: str, terms: list[str] | None = None, allowlist: list[str] | None = None,
         name_mode: str = "smart", addresses: bool = True, ip: bool = False,
         secrets: bool = True) -> list[dict]:
    """Every PII span in `text`: [{kind, value, start, end}], non-overlapping, by position."""
    if not text:
        return []
    dets = [d for d in DETECTORS if (d["kind"] != "ADDRESS" or addresses) and (d["kind"] != "IP" or ip)]
    if secrets:
        dets = SECRET_DETECTORS + dets
    dets += _term_detectors(terms or []) + _name_detectors(name_mode)
    found = []
    for d in dets:
        pos = 0
        while pos <= len(text):
            m = d["re"].search(text, pos)
            if not m:
                break
            if not m.group(0):
                pos = m.start() + 1
                continue
            if d.get("smart"):
                hit = _smart_match(d["smart"], m, text)
                pos = hit[1] + len(hit[0]) if hit else m.start() + 1
                if hit and not _allowed(hit[0], "NAME", allowlist or []):
                    found.append({"kind": "NAME", "value": hit[0], "start": hit[1],
                                  "end": hit[1] + len(hit[0])})
                continue
            g = d["group"]
            value = m.group(0) if g is None else m.group(g)
            pos = m.end()
            if not value:
                continue
            start = m.start() if g is None else m.start(g)
            if d["keep"] and not d["keep"](value, text, start):
                pos = m.start() + 1  # a dropped candidate may hide a shorter one inside it
                continue
            if _allowed(value, d["kind"], allowlist or []):
                continue
            found.append({"kind": d["kind"], "value": value, "start": start,
                          "end": start + len(value)})
    found.sort(key=lambda f: (f["start"], -f["end"], f["kind"] == "NAME"))
    kept, cursor = [], 0
    for f in found:
        if f["start"] >= cursor:
            kept.append(f)
            cursor = f["end"]
    return kept


# -- configuration and allow rules -------------------------------------------

def value_hash(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.strip().lower().encode("utf-8")).hexdigest()[:16]


def config(home: str | None = None) -> dict:
    text = _dcio.read_text(dc_registry.home_dir(home) / CONFIG_NAME)
    try:
        data = json.loads(text) if text else {}
    except ValueError:
        raise DcError(f"{CONFIG_NAME} is not valid JSON; refusing (fail closed)") from None
    return data if isinstance(data, dict) else {}


def options(cfg: dict) -> dict:
    mode = cfg.get("pii_name_mode", "smart")
    if mode not in NAME_MODES:
        raise DcError(f"pii_name_mode must be one of {', '.join(NAME_MODES)}")
    terms = []
    names_file = cfg.get("pii_names_file") or str(NAMES_FILE)
    text = _dcio.read_text(Path(names_file))
    if text:
        terms = parse_name_list(text)
    allow = [a for a in cfg.get("pii_allow") or [] if isinstance(a, str)]
    return {"name_mode": mode, "terms": terms, "allowlist": allow}


def parse_allow(text: str | None) -> dict:
    """{'values': {(kind, hash)}, 'paths': [glob]} from an allow file."""
    rules = {"values": set(), "paths": []}
    for n, raw in enumerate((text or "").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        bits = line.split(None, 3)
        if bits[0] == "value" and len(bits) >= 3 and re.fullmatch(r"sha256:[0-9a-f]{16}", bits[2]):
            rules["values"].add((bits[1].upper(), bits[2]))
        elif bits[0] == "path" and len(bits) >= 2:
            rules["paths"].append(bits[1])
        else:
            raise DcError(f"{ALLOW_FILE} line {n} is not `value KIND sha256:<16 hex> reason` "
                          "or `path GLOB reason`; refusing (fail closed)")
    return rules


def _path_allowed(path: str, rules: dict) -> bool:
    return any(fnmatch.fnmatchcase(path, g) for g in rules["paths"])


# -- git ----------------------------------------------------------------------

def git(root: Path, *args: str, binary: bool = False):
    try:
        proc = subprocess.run(["git", "-C", str(root), "-c", "core.quotepath=false", *args],
                              capture_output=True, timeout=GIT_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DcError(f"git {args[0]} failed: {exc}; refusing (fail closed)") from None
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip()
        raise DcError(f"git {args[0]} failed: {_dcio.collapse(err)}; refusing (fail closed)")
    return proc.stdout if binary else proc.stdout.decode("utf-8", "replace")


def office_text(blob: bytes) -> str:
    """The text inside an Office Open XML file: every XML part, tags stripped."""
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            parts = [n for n in z.namelist() if n.endswith(".xml") or n.endswith(".rels")]
            text = []
            for n in parts:
                xml = z.read(n).decode("utf-8", "replace")
                text.append(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", xml)))
            return "\n".join(text)
    except (zipfile.BadZipFile, OSError, ValueError, RuntimeError):
        raise DcError("unreadable Office file") from None


def is_binary(blob: bytes) -> bool:
    return b"\0" in blob[:8000]


def added_by_file(diff: str) -> tuple[dict, list[str]]:
    """{path: [(line, text)]} of added lines, and the paths git called binary."""
    files, binaries, current, line = {}, [], None, 0
    for raw in diff.split("\n"):
        if raw.startswith("+++ "):
            p = raw[4:].rstrip("\t")
            current = None if p == "/dev/null" else re.sub(r"^b/", "", p)
            continue
        if raw.startswith("Binary files ") and raw.endswith(" differ"):
            m = re.search(r" and (?:b/)?(.+) differ$", raw)
            if m and m.group(1) != "/dev/null":
                binaries.append(m.group(1))
            continue
        m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", raw)
        if m:
            line = int(m.group(1))
            continue
        if current and raw.startswith("+"):
            files.setdefault(current, []).append((line, raw[1:]))
            line += 1
    return files, binaries


def _locate(rows: list[tuple[int, str]], findings: list[dict]) -> list[tuple[int, dict]]:
    """Map findings in the joined text back to their source line numbers."""
    starts, off = [], 0
    for n, text in rows:
        starts.append((off, n))
        off += len(text) + 1
    out = []
    for f in findings:
        line = next((n for o, n in reversed(starts) if o <= f["start"]), rows[0][0] if rows else 0)
        out.append((line, f))
    return out


# -- the scans ----------------------------------------------------------------

def _hit(hits, rules, rev, where, line, f):
    if (f["kind"], value_hash(f["value"])) in rules["values"]:
        return
    hits.append({"rev": rev[:8], "where": where, "line": line, "kind": f["kind"]})


def scan_text(hits, opts, rules, rev, where, rows):
    if not rows:
        return
    text = "\n".join(t for _, t in rows)
    found = scan(text, opts["terms"], opts["allowlist"], opts["name_mode"])
    for line, f in _locate(rows, found):
        _hit(hits, rules, rev, where, line, f)


def scan_blob(root, hits, opts, rules, rev, path, blob: bytes | None = None):
    """A whole file at `rev`: office text, plain text, or refused as unreadable."""
    if _path_allowed(path, rules):
        return
    if blob is None:
        size = int(git(root, "cat-file", "-s", f"{rev}:{path}").strip())
        if size > MAX_BLOB:
            hits.append({"rev": rev[:8], "where": path, "line": 0, "kind": "TOO-LARGE"})
            return
        blob = git(root, "cat-file", "blob", f"{rev}:{path}", binary=True)
    if path.lower().endswith(OFFICE):
        try:
            text = office_text(blob)
        except DcError:
            hits.append({"rev": rev[:8], "where": path, "line": 0, "kind": "UNREADABLE"})
            return
        scan_text(hits, opts, rules, rev, path, [(0, text)])
    elif is_binary(blob):
        hits.append({"rev": rev[:8], "where": path, "line": 0, "kind": "UNSCANNABLE"})
    else:
        text = blob.decode("utf-8", "replace")
        scan_text(hits, opts, rules, rev, path, list(enumerate(text.split("\n"), 1)))


def scan_commit(root: Path, sha: str, opts: dict, rules: dict, hits: list) -> None:
    parents = git(root, "rev-list", "--parents", "-n", "1", sha).split()[1:]
    base = ["diff", "-U0", "--no-color", "--no-ext-diff", "--no-textconv", "-M"]
    diff = git(root, *base, parents[0], sha) if parents else \
        git(root, "diff-tree", "-p", "--root", *base[1:], sha)
    files, binaries = added_by_file(diff)
    for path, rows in files.items():
        if not _path_allowed(path, rules):
            scan_text(hits, opts, rules, sha, path, rows)
    for path in binaries:
        scan_blob(root, hits, opts, rules, sha, path)
    names_cmd = ["diff", "--name-only", "--diff-filter=AR", "-M"]
    names = git(root, *names_cmd, parents[0], sha) if parents else \
        git(root, "diff-tree", "--root", "-r", "--name-only", "--diff-filter=AR", sha)
    for path in names.splitlines():
        if path.strip() and path != sha:
            scan_text(hits, opts, rules, sha, f"{path} (file name)",
                      [(0, re.sub(r"[/\\_.]+", " ", path))])
    scan_text(hits, opts, rules, sha, "(commit message)",
              list(enumerate(git(root, "log", "-1", "--format=%B", sha).split("\n"), 1)))


def push_commits(root: Path, ref_lines: list[str]) -> list[tuple[str, str]]:
    """(commit, tip) pairs a push would publish, oldest first, each once."""
    seen, out = set(), []
    for raw in ref_lines:
        bits = raw.split()
        if len(bits) != 4:
            continue
        _lref, lsha, _rref, rsha = bits
        if set(lsha) == {"0"}:
            continue  # deleting a remote branch publishes nothing
        rng = [lsha, "--not", "--remotes"] if set(rsha) == {"0"} else [f"{rsha}..{lsha}"]
        for sha in git(root, "rev-list", "--reverse", *rng).split():
            if sha not in seen:
                seen.add(sha)
                out.append((sha, lsha))
    return out


def allow_rules(root: Path, rev: str) -> dict:
    try:
        text = git(root, "show", f"{rev}:{ALLOW_FILE}")
    except DcError:
        text = None  # no allow file at that commit
    return parse_allow(text)


def report(hits: list[dict], head: str, cap: int | None = REPORT_CAP) -> list[str]:
    if not hits:
        return []
    lines = [f"{head}: {len(hits)} finding(s). Values are never printed."]
    for h in hits[:cap]:
        at = f":{h['line']}" if h["line"] else ""
        lines.append(f"  {h['rev']} {h['where']}{at}  {h['kind']}")
    if cap is not None and len(hits) > cap:
        lines.append(f"  TRUNCATED: SHOWING {cap}/{len(hits)}")
    lines.append("Remove the data (rewrite the commits if it is in history), or if it is not "
                 f"PII: dc_pii.py allow --at <rev>:<file>:<line> --kind <KIND> --reason \"...\" "
                 f"(path rules for binaries go in {ALLOW_FILE} by hand).")
    return lines


# -- commands -----------------------------------------------------------------

def cmd_prepush(root: Path, stdin_text: str, home: str | None) -> int:
    opts = options(config(home))
    hits = []
    commits = push_commits(root, stdin_text.splitlines())
    rules_by_tip = {}
    for sha, tip in commits:
        rules = rules_by_tip.setdefault(tip, allow_rules(root, tip))
        scan_commit(root, sha, opts, rules, hits)
    lines = report(hits, "DRAINCLAMP PII: push refused")
    if lines:
        print("\n".join(lines), file=sys.stderr)
        return _dcio.EXIT_CHECK_FAILED
    print(f"DRAINCLAMP PII: {len(commits)} commit(s) clean.", file=sys.stderr)
    return _dcio.EXIT_OK


def cmd_scan(root: Path, args, home: str | None) -> int:
    opts = options(config(home))
    hits = []
    if args.range:
        rules = allow_rules(root, args.range.split("..")[-1] or "HEAD")
        for sha in git(root, "rev-list", "--reverse", args.range).split():
            scan_commit(root, sha, opts, rules, hits)
    elif args.tree:
        rules = allow_rules(root, "HEAD")
        for path in git(root, "ls-tree", "-r", "--name-only", "HEAD").splitlines():
            scan_blob(root, hits, opts, rules, "HEAD", path)
    else:  # --history: every blob ever committed on any ref (or one remote's), and every message
        refs = [f"--remotes={args.remote}"] if args.remote else ["--all"]
        rules = allow_rules(root, "HEAD")
        seen = set()
        for row in git(root, "rev-list", "--objects", *refs).splitlines():
            sha, _, path = row.partition(" ")
            if not path or sha in seen:
                continue
            kind = git(root, "cat-file", "-t", sha).strip()
            if kind != "blob":
                continue
            seen.add(sha)
            size = int(git(root, "cat-file", "-s", sha).strip())
            if size > MAX_BLOB:
                hits.append({"rev": sha[:8], "where": path, "line": 0, "kind": "TOO-LARGE"})
                continue
            scan_blob(root, hits, opts, rules, sha, path,
                      git(root, "cat-file", "blob", sha, binary=True))
        for block in git(root, "log", *refs, "--format=%H%x00%B%x01").split("\x01"):
            sha, _, body = block.strip("\n").partition("\x00")
            if sha:
                scan_text(hits, opts, rules, sha, "(commit message)",
                          list(enumerate(body.split("\n"), 1)))
    lines = report(hits, f"DRAINCLAMP PII: {root.name}", None if args.full else REPORT_CAP)
    print("\n".join(lines) if lines else f"DRAINCLAMP PII: {root.name} clean.")
    return _dcio.EXIT_CHECK_FAILED if hits else _dcio.EXIT_OK


def cmd_allow(root: Path, args, home: str | None) -> int:
    m = re.fullmatch(r"([^:]+):(.+):(\d+)", args.at)
    if not m:
        raise DcError("--at must be <rev>:<file>:<line>")
    rev, path, line = m.group(1), m.group(2), int(m.group(3))
    if not args.reason.strip():
        raise DcError("--reason is required")
    text = git(root, "show", f"{rev}:{path}")
    rows = text.split("\n")
    if not 1 <= line <= len(rows):
        raise DcError(f"{path} has no line {line} at {rev}")
    opts = options(config(home))
    found = [f for f in scan(rows[line - 1], opts["terms"], opts["allowlist"], opts["name_mode"])
             if f["kind"] == args.kind.upper()]
    if not found:
        raise DcError(f"no {args.kind.upper()} finding on {path}:{line} at {rev}")
    allow = root / ALLOW_FILE
    existing = _dcio.read_text(allow) or ""
    parse_allow(existing)
    reason = re.sub(r"\s+", " ", args.reason.strip())
    new = [f"value {f['kind']} {value_hash(f['value'])}  {reason}" for f in found]
    new = [n for n in new if n.split("  ")[0] not in existing]
    if new:
        body = existing if not existing or existing.endswith("\n") else existing + "\n"
        _dcio.atomic_write(allow, body + "\n".join(new) + "\n")
    print(f"DRAINCLAMP PII: {len(new)} allow rule(s) added to {ALLOW_FILE}; commit it with the change.")
    return _dcio.EXIT_OK


def cmd_drift(home: str | None) -> int:
    cfg = config(home)
    src = cfg.get("pii_redact_repo")
    if not isinstance(src, str) or not src.strip():
        print("DRAINCLAMP PII: drift not checked (no pii_redact_repo in config.json).")
        return _dcio.EXIT_OK
    ref = cfg.get("pii_redact_ref") or "origin/main"
    try:
        blob = git(Path(src), "show", f"{ref}:{PORTED_FROM['file']}", binary=True)
    except DcError as exc:
        print(f"WARN: PII drift not checked: {exc}")
        return _dcio.EXIT_OK
    now = hashlib.sha256(blob.replace(b"\r\n", b"\n")).hexdigest()
    if now != PORTED_FROM["sha256"]:
        print(f"WARN: pii-redact {PORTED_FROM['file']} changed since the port "
              f"({PORTED_FROM['commit']}); update dc_pii.py detectors to match.")
        return _dcio.EXIT_CHECK_FAILED
    print("DRAINCLAMP PII: detectors match pii-redact.")
    return _dcio.EXIT_OK


def main() -> int:
    p = argparse.ArgumentParser(prog="dc_pii.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--root", default=None)
    p.add_argument("--home", default=None, help="config directory override")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("prepush", help="git pre-push hook body: ref lines on stdin")
    sc = sub.add_parser("scan")
    g = sc.add_mutually_exclusive_group(required=True)
    g.add_argument("--range", help="commits A..B (as a push would publish them)")
    g.add_argument("--history", action="store_true", help="every blob and message on any ref")
    sc.add_argument("--full", action="store_true", help="list every finding (no cap)")
    sc.add_argument("--remote", help="with --history: only what that remote holds (e.g. origin)")
    g.add_argument("--tree", action="store_true", help="every file at HEAD, whole contents")
    al = sub.add_parser("allow", help="allow a reported finding by location; value never shown")
    al.add_argument("--at", required=True, help="<rev>:<file>:<line> from a report")
    al.add_argument("--kind", required=True)
    al.add_argument("--reason", required=True)
    sub.add_parser("drift", help="warn when pii-redact's detectors changed since the port")
    args = p.parse_args()
    if args.cmd == "drift":
        return cmd_drift(args.home)
    root = Path(git(Path(args.root or "."), "rev-parse", "--show-toplevel").strip())
    if args.cmd == "prepush":
        return cmd_prepush(root, sys.stdin.read(), args.home)
    if args.cmd == "scan":
        return cmd_scan(root, args, args.home)
    return cmd_allow(root, args, args.home)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except DcError as exc:
        print(f"DRAINCLAMP PII: {exc}", file=sys.stderr)
        sys.exit(_dcio.EXIT_INTERNAL)
    except Exception as exc:  # fail closed: any crash refuses the push
        print(f"DRAINCLAMP PII: scanner error ({type(exc).__name__}: {exc}); refusing (fail closed)",
              file=sys.stderr)
        sys.exit(_dcio.EXIT_INTERNAL)
