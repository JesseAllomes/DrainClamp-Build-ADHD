# Platform adapters

Read this **only when host capability is in question**.

## Invocation

| Host | Invocation |
|---|---|
| Claude Code | `/drainclamp-build-adhd` |
| Grok | `/drainclamp-build-adhd` |
| Codex | `$drainclamp-build-adhd`, or via `/skills` |

## What no host can do

**No CLI exposes `/clear` or `/compact` to the model.** Context compaction is a user action. Gate 5
recommends it and prints the resume instruction; it never claims a purge occurred. Only a genuinely
fresh session may say `Context reset confirmed`.

Print the literal `/compact` instruction only for a host that documents that command — Claude Code
does. For any host where you lack that evidence, recommend starting a fresh session instead. Naming
a command the host does not have is worse than the generic instruction.

## Subagents

Detect at runtime; do not infer from the host's name. Subagent support changes between releases, and
a hardcoded "Claude only" rule ages badly.

> Use a native subagent only when the current host exposes one. Otherwise work inline. **Never**
> simulate one with git worktrees or nested CLI processes — that spends more than it saves and adds
> a failure mode nobody is watching.

The definition lives at `agents/dca-scout.md` at the repository root, because Claude Code's plugin
loader reads agents only from there and silently ignores a path inside `skills/`. Claude Code reads
installed agent definitions from `~/.claude/agents/`, so `dc_install.py` copies it there when that
directory already exists — it never creates it, because a definition in a directory the host does not
read is install theatre. No other host documents a subagent directory, so no other host gets a copy.

When a subagent is available, `dca-scout` is read-only: `Glob`, `Grep`, `Read`. It has no `Write`,
because a `tools:` list cannot confine writes to `.agent/` — that would need host permissions or
hooks, which this skill does not install. The parent persists whatever the scout returns. Over-budget
results come back with a `TRUNCATED n/N` header and a narrowing hint (which path or symbol to
re-query), and the parent re-scopes rather than accepting a silent truncation.

## Skill discovery

Canonical copy: `~/.agents/skills/drainclamp-build-adhd/`.

Claude and Grok discover skills through junctions or symlinks into that directory; Grok also reads
`~/.agents/skills/` directly, so it may see the skill twice. Duplicate discovery is harmless but is
reported by `dc_install.py --check`.

Codex discovers user skills under `~/.codex/skills/`. There is no `~/.codex/prompts/` adapter — a
skill directory is the supported mechanism.

## Python

All scripts are stdlib-only Python 3 and are meant to be **run, not read**. On Windows invoke them
with `py -3 -B`; elsewhere `python3`. No third-party package is required, and none should be
introduced without a schema change.
