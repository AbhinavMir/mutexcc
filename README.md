# mutexcc — a local mutex for Claude Code agents

When you run several Claude Code agents at once (parallel sessions, subagents,
multiple CLIs on the same repo), nothing stops two of them from editing the
same file at the same moment and clobbering each other. `mutexcc` is a small
local lock authority that makes agents **wait their turn** on a shared
file / folder / subtree.

Single file, stdlib-only Python 3.8+, SQLite-backed. No daemon to run.

## Install

It's a single stdlib-only file, so installing is light.

**Works today (straight from the repo):**

```sh
# one-line installer — drops `mutexcc` onto ~/.local/bin
curl -fsSL https://raw.githubusercontent.com/AbhinavMir/mutex-claude-code/main/install.sh | sh

# or just grab the one file and run it
curl -fsSL https://raw.githubusercontent.com/AbhinavMir/mutex-claude-code/main/mutexcc.py \
  -o mutexcc && chmod +x mutexcc && ./mutexcc status

# or install the checkout with pip
git clone https://github.com/AbhinavMir/mutex-claude-code && pip install ./mutex-claude-code
```

**Once published to PyPI** (`python3 -m build && twine upload dist/*`):

```sh
pipx install mutexcc     # isolated, recommended
uvx mutexcc status       # run without installing
pip install mutexcc
```

Any route gives you a `mutexcc` command. Then `cd` into a repo and run
`mutexcc install-hooks`.

## Does Claude Code already do this?

No. Claude Code has **no built-in cross-agent mutex**. Git worktrees give you
*isolation* (each agent on its own copy) but that's avoidance, not a lock —
nobody waits, and you still have to merge. `mutexcc` provides actual waiting.

## How it locks

- Locks are **path-scoped and hierarchical**. Locking a folder conflicts with
  any lock on a file beneath it, and vice-versa. So you can lock a single file,
  a folder, or the whole repo root ("git tree") — coarseness is your choice.
- Locks are **exclusive** and **reentrant** (an agent re-acquiring a scope it
  already holds always succeeds).
- The lock holder is the **agent/session**, identified by `CLAUDE_SESSION_ID`
  (or `MUTEXCC_AGENT`), not the short-lived CLI process.
- Staleness is **TTL-based**: hooks set a safety TTL so a crashed agent
  auto-releases; explicit locks persist until released.

## Three ways to use it

### 1. Hooks — automatic & enforced (recommended)

```sh
./mutexcc install-hooks --scope project   # writes .claude/settings.json
```

This wires a **`PreToolUse`** hook on `Edit|Write|MultiEdit|NotebookEdit` that
acquires a lock on the target path before the edit runs — **blocking the agent
until the lock is free** — and a `PostToolUse` hook that releases it. Because
hooks fire on every matching tool call, the agent *cannot skip them*. This is
the one to use if you want a mutex the model can't forget about.

If the wait exceeds `MUTEXCC_HOOK_TIMEOUT` (default 120s) the edit is denied
(exit 2) with a message that tells Claude **how long to wait** — e.g. "expires
in ~5 minutes; check back then" — derived from the blocking lock's remaining
TTL, so the agent has a concrete ETA rather than spinning.

### 2. MCP server — cooperative

```sh
claude mcp add mutexcc -- "$PWD/mutexcc" mcp
```

Exposes `acquire_lock`, `release_lock`, `check_lock`, `list_locks` so an agent
can grab a **long-lived** lock on a folder/subtree while it works on a feature.
Note: MCP tools are opt-in — the model has to choose to call them — so use this
for cooperative coordination, and the hooks above for hard enforcement.

### 3. Plain CLI — manual / scripting

```sh
mutexcc acquire path/to/dir          # blocks until free (default 120s timeout)
mutexcc acquire path --no-block      # fail immediately if held (exit 1)
mutexcc acquire path --ttl 600       # auto-expire after 10 min
mutexcc release path                 # release one lock
mutexcc release --all                # release all of my locks
mutexcc check path                   # is it free? who holds it?
mutexcc status                       # list all held locks
mutexcc gc                           # prune expired locks
```

Add `--json` to any command for machine-readable output.

## Agent identity

Each agent must have a distinct id so they can be told apart. `mutexcc` reads,
in order: `MUTEXCC_AGENT`, `CLAUDE_SESSION_ID`, `CLAUDE_AGENT_ID`, then falls
back to the parent process id. Claude Code passes `session_id` into hook
payloads, which the hooks use automatically. For manual CLI use across
agents, set `MUTEXCC_AGENT`.

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `MUTEXCC_HOME` | `~/.mutexcc` | where the lock database lives |
| `MUTEXCC_AGENT` | — | override the holder identity |
| `MUTEXCC_HOOK_TIMEOUT` | `120` | seconds a `PreToolUse` hook waits before denying |
| `MUTEXCC_HOOK_TTL` | `300` | safety TTL on per-edit hook locks |

## Limitations

- Coordinates agents on **one machine** (shared `MUTEXCC_HOME`). Not networked.
- The hook locks one path per edit; it doesn't analyze `Bash` commands, so
  `git` operations or shell-driven writes aren't auto-locked — grab those
  explicitly via the CLI/MCP (`mutexcc acquire <repo-root>`).
