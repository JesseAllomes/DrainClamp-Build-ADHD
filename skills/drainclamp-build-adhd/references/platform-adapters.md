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
read is install theatre.

Grok loads the plugin's `agents/` directory itself and lists each as
`drainclamp-build-adhd:dca-*`. Its remote settings can hide `spawn_subagent`'s `model` argument
(subagent model inheritance), so `dc_install.py --host agents` pins each role's model as a marked
block under `[subagents.models]` in `~/.grok/config.toml`, which that setting does not affect. It
refuses, and prints the lines, when the config already has its own `[subagents.models]`. Codex reads custom agents
as TOML from `~/.codex/agents/`, and its plugins cannot ship them, so `dc_install.py --host codex`
generates `dca-*.toml` from the same definitions: `name`, `description`, `developer_instructions`,
the role's `model` and `model_reasoning_effort` from the Codex roster, and `sandbox_mode`
(`read-only`, or `workspace-write` for the fixer). It creates `~/.codex/agents/` when Codex is
present, because Codex documents that directory. With the skill installed as a plugin, run it from
the plugin's copy with `--agents-only` so no skill link is made, and again after each upgrade.

When a subagent is available, `dca-scout` is read-only: `Glob`, `Grep`, `Read`. It has no `Write`,
because a `tools:` list cannot confine writes to `.agent/` — that would need host permissions or
hooks, which this skill does not install. The parent persists whatever the scout returns. Over-budget
results come back with a `TRUNCATED n/N` header and a narrowing hint (which path or symbol to
re-query), and the parent re-scopes rather than accepting a silent truncation.

Gate 6 adds five definitions beside the scout, installed the same way: `dca-critic`, `dca-checker`, `dca-advisor`
and `dca-refuter` (read-only like the scout) and `dca-fixer` (Read, Edit, Write; no shell; on Codex the
sandbox allows workspace writes and cannot withhold the shell, and the hash scope check still holds). None
has an agent-spawning tool, so the per-round cap in `dc_review.py run` cannot be exceeded from inside
a subagent. Their frontmatter sets `model` and `effort`; the orchestrator may override `model` per
call from `dc_review.py config`. Subagents cannot ask the user anything, so every decision stays
with the main session. A host without subagents runs Gate 6 inline and the round is labelled
`COVERAGE: partial (inline, not independent)`. Inline review is weaker, so the label stays visible.

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
