#!/usr/bin/env python3
"""
mutexcc — a local mutex for Claude Code agents.

Coordinates concurrent agents so they don't edit the same file / folder / git
tree at the same time. One small SQLite-backed lock authority, reachable three
ways:

  1. PreToolUse / PostToolUse hooks  -> automatic, ENFORCED per-edit locking
  2. MCP server (stdio)              -> cooperative, explicit lock tools
  3. plain CLI                       -> manual acquire/release/status

Locks are path-scoped and hierarchical: locking a folder conflicts with any
lock on a file beneath it, and vice-versa. No external dependencies; stdlib
only. Python 3.8+.
"""

import argparse
import hashlib
import json
import os
import socket
import sqlite3
import sys
import time

# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #

def home_dir():
    return os.environ.get("MUTEXCC_HOME") or os.path.join(
        os.path.expanduser("~"), ".mutexcc"
    )


def db_path():
    d = home_dir()
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "locks.db")


def connect():
    conn = sqlite3.connect(db_path(), timeout=30, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=30000;")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS locks (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            path      TEXT NOT NULL,
            agent     TEXT NOT NULL,
            host      TEXT NOT NULL,
            pid       INTEGER NOT NULL,
            mode      TEXT NOT NULL DEFAULT 'exclusive',
            acquired  REAL NOT NULL,
            expires   REAL,
            note      TEXT
        );
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_locks_path ON locks(path);")
    return conn


# --------------------------------------------------------------------------- #
# Identity & helpers
# --------------------------------------------------------------------------- #

def this_host():
    return socket.gethostname()


def agent_id(explicit=None):
    """Resolve the lock holder identity. Stable per agent/session if possible."""
    if explicit:
        return explicit
    for var in ("MUTEXCC_AGENT", "CLAUDE_SESSION_ID", "CLAUDE_AGENT_ID"):
        v = os.environ.get(var)
        if v:
            return v
    # Fall back to the parent process so sequential CLI calls in one shell
    # share an identity; still unique per host.
    return "pid-%d" % os.getppid()


def norm(path):
    """Absolute, symlink-resolved, no trailing slash."""
    p = os.path.abspath(os.path.expanduser(path))
    p = os.path.realpath(p)
    if len(p) > 1 and p.endswith(os.sep):
        p = p.rstrip(os.sep)
    return p


def paths_conflict(a, b):
    """True if locking a and b would interfere (equal or ancestor/descendant)."""
    if a == b:
        return True
    return a.startswith(b + os.sep) or b.startswith(a + os.sep)


# --------------------------------------------------------------------------- #
# Core lock operations
# --------------------------------------------------------------------------- #

def prune(conn):
    """Drop expired locks.

    Staleness is TTL-based, not pid-based: a lock's holder is the agent
    (session), which outlives any single CLI/hook process, so we must NOT
    tie liveness to the process that happened to create the row. Hooks set a
    safety TTL so crashed agents auto-release; explicit locks persist until
    released (or their optional TTL elapses).
    """
    now = time.time()
    cur = conn.execute("DELETE FROM locks WHERE expires IS NOT NULL AND expires < ?",
                       (now,))
    return cur.rowcount


def conflicting(conn, path, agent):
    """Return a conflicting lock row held by a DIFFERENT agent, or None."""
    for row in conn.execute(
        "SELECT id, path, agent, host, pid, acquired, expires, note FROM locks"
    ):
        lid, lpath, lagent, host, pid, acq, exp, note = row
        if lagent == agent:
            continue  # reentrant: we already own an overlapping scope
        if paths_conflict(path, lpath):
            return {
                "id": lid, "path": lpath, "agent": lagent, "host": host,
                "pid": pid, "acquired": acq, "expires": exp, "note": note,
            }
    return None


def try_acquire(conn, path, agent, ttl, note):
    """Single atomic attempt. Returns (ok, conflict_or_lockid)."""
    conn.execute("BEGIN IMMEDIATE;")
    try:
        prune(conn)
        clash = conflicting(conn, path, agent)
        if clash:
            conn.execute("ROLLBACK;")
            return False, clash
        # Reentrant refresh: if we already hold this exact path, update it.
        existing = conn.execute(
            "SELECT id FROM locks WHERE path=? AND agent=?", (path, agent)
        ).fetchone()
        now = time.time()
        expires = now + ttl if ttl and ttl > 0 else None
        if existing:
            conn.execute(
                "UPDATE locks SET acquired=?, expires=?, pid=?, host=?, note=? WHERE id=?",
                (now, expires, os.getpid(), this_host(), note, existing[0]),
            )
            lid = existing[0]
        else:
            cur = conn.execute(
                "INSERT INTO locks(path, agent, host, pid, mode, acquired, expires, note) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (path, agent, this_host(), os.getpid(), "exclusive", now, expires, note),
            )
            lid = cur.lastrowid
        conn.execute("COMMIT;")
        return True, lid
    except Exception:
        conn.execute("ROLLBACK;")
        raise


def acquire(path, agent, blocking, timeout, ttl, note, poll=0.25):
    """Acquire a lock. Returns dict with result."""
    conn = connect()
    path = norm(path)
    deadline = time.time() + timeout if timeout and timeout > 0 else None
    while True:
        ok, info = try_acquire(conn, path, agent, ttl, note)
        if ok:
            return {"ok": True, "path": path, "agent": agent, "lock_id": info}
        if not blocking:
            return {"ok": False, "path": path, "agent": agent, "blocked_by": info}
        if deadline is not None and time.time() >= deadline:
            return {"ok": False, "timeout": True, "path": path,
                    "agent": agent, "blocked_by": info}
        time.sleep(poll)


def release(path, agent, all_for_agent=False):
    conn = connect()
    if all_for_agent:
        cur = conn.execute("DELETE FROM locks WHERE agent=?", (agent,))
        return {"ok": True, "released": cur.rowcount, "agent": agent}
    path = norm(path)
    cur = conn.execute(
        "DELETE FROM locks WHERE path=? AND agent=?", (path, agent)
    )
    released = cur.rowcount
    return {"ok": released > 0, "released": released, "path": path, "agent": agent}


def list_locks():
    conn = connect()
    prune(conn)
    rows = conn.execute(
        "SELECT id, path, agent, host, pid, acquired, expires, note "
        "FROM locks ORDER BY acquired"
    ).fetchall()
    now = time.time()
    out = []
    for lid, path, agent, host, pid, acq, exp, note in rows:
        out.append({
            "id": lid, "path": path, "agent": agent, "host": host, "pid": pid,
            "held_for_s": round(now - acq, 1),
            "expires_in_s": round(exp - now, 1) if exp else None,
            "note": note,
        })
    return out


def check(path, agent):
    conn = connect()
    prune(conn)
    clash = conflicting(conn, norm(path), agent)
    return {"path": norm(path), "free": clash is None, "blocked_by": clash}


# --------------------------------------------------------------------------- #
# Hook handlers (Claude Code PreToolUse / PostToolUse)
# --------------------------------------------------------------------------- #

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}


def retry_hint(expires):
    """A concrete 'check back in ~X' phrase Claude can act on.

    If the blocking lock has a TTL we know exactly when it lapses; otherwise
    it's an explicit (no-expiry) lock, so we can only advise re-checking.
    """
    if not expires:
        return ("It has no expiry (an explicit lock), so re-check with "
                "`mutexcc check <path>` before retrying.")
    remaining = expires - time.time()
    if remaining <= 0:
        return "It should be free now — retry."
    if remaining < 90:
        return "It expires in ~%ds; check back then." % int(remaining)
    return "It expires in ~%d minute(s); check back then." % round(remaining / 60)


def extract_paths(payload):
    """Pull the target path(s) out of a tool-call payload."""
    tool = payload.get("tool_name") or payload.get("tool") or ""
    ti = payload.get("tool_input") or payload.get("input") or {}
    paths = []
    for key in ("file_path", "notebook_path", "path"):
        if ti.get(key):
            paths.append(ti[key])
    # MultiEdit nests edits but shares one file_path; covered above.
    return tool, paths


def hook_pre():
    """
    Read a PreToolUse payload from stdin, block until the lock is free (or
    timeout), then allow/deny. exit 0 = allow, exit 2 = block (reason on stderr).
    """
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)  # malformed input: don't get in the way
    tool, paths = extract_paths(payload)
    if tool not in EDIT_TOOLS or not paths:
        sys.exit(0)
    agent = agent_id(payload.get("session_id"))
    timeout = float(os.environ.get("MUTEXCC_HOOK_TIMEOUT", "120"))
    ttl = float(os.environ.get("MUTEXCC_HOOK_TTL", "300"))
    waited = False
    for p in paths:
        # Non-blocking probe first so we only print a wait notice when needed.
        res = acquire(p, agent, blocking=False, timeout=0, ttl=ttl,
                      note="auto:%s" % tool)
        if not res["ok"]:
            waited = True
            b = res["blocked_by"]
            sys.stderr.write(
                "mutexcc: waiting for %s (held by agent %s) ...\n"
                % (p, b["agent"])
            )
            res = acquire(p, agent, blocking=True, timeout=timeout, ttl=ttl,
                          note="auto:%s" % tool)
        if not res["ok"]:
            b = res.get("blocked_by", {})
            sys.stderr.write(
                "mutexcc: BLOCKED — %s is locked by agent %s; edit denied to "
                "avoid a concurrent-write conflict. %s Or coordinate with the "
                "other agent / lock a different file in the meantime.\n"
                % (p, b.get("agent", "?"), retry_hint(b.get("expires")))
            )
            sys.exit(2)
    if waited:
        sys.stderr.write("mutexcc: lock acquired, proceeding.\n")
    sys.exit(0)


def hook_post():
    """Release the per-edit locks this agent grabbed for the tool's path(s)."""
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)
    tool, paths = extract_paths(payload)
    if tool not in EDIT_TOOLS or not paths:
        sys.exit(0)
    agent = agent_id(payload.get("session_id"))
    for p in paths:
        release(p, agent)
    sys.exit(0)


# --------------------------------------------------------------------------- #
# MCP server (JSON-RPC 2.0 over stdio) — no SDK, stdlib only
# --------------------------------------------------------------------------- #

MCP_TOOLS = [
    {
        "name": "acquire_lock",
        "description": "Acquire an exclusive lock on a file or folder before "
                       "editing it, so other agents wait. Blocks until free or "
                       "timeout. Locking a folder also blocks files beneath it.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File or folder path."},
                "timeout_s": {"type": "number", "default": 120},
                "ttl_s": {"type": "number", "default": 0,
                          "description": "Auto-expire after N seconds (0 = never)."},
                "note": {"type": "string"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "release_lock",
        "description": "Release a lock you hold on a path.",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "check_lock",
        "description": "Check whether a path is free to edit (and who holds it).",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "list_locks",
        "description": "List all currently held locks across agents.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def mcp_call(name, args):
    agent = agent_id()
    if name == "acquire_lock":
        return acquire(args["path"], agent, blocking=True,
                       timeout=args.get("timeout_s", 120),
                       ttl=args.get("ttl_s", 0), note=args.get("note"))
    if name == "release_lock":
        return release(args["path"], agent)
    if name == "check_lock":
        return check(args["path"], agent)
    if name == "list_locks":
        return {"locks": list_locks()}
    raise ValueError("unknown tool: %s" % name)


def mcp_serve():
    def send(obj):
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            continue
        rid = req.get("id")
        method = req.get("method")
        try:
            if method == "initialize":
                send({"jsonrpc": "2.0", "id": rid, "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "mutexcc", "version": "1.0.0"},
                }})
            elif method in ("notifications/initialized", "initialized"):
                continue  # notification, no response
            elif method == "tools/list":
                send({"jsonrpc": "2.0", "id": rid, "result": {"tools": MCP_TOOLS}})
            elif method == "tools/call":
                params = req.get("params", {})
                result = mcp_call(params["name"], params.get("arguments", {}))
                send({"jsonrpc": "2.0", "id": rid, "result": {
                    "content": [{"type": "text",
                                 "text": json.dumps(result, indent=2)}],
                    "isError": not result.get("ok", True),
                }})
            elif rid is not None:
                send({"jsonrpc": "2.0", "id": rid,
                      "error": {"code": -32601, "message": "method not found"}})
        except Exception as e:
            if rid is not None:
                send({"jsonrpc": "2.0", "id": rid,
                      "error": {"code": -32603, "message": str(e)}})


# --------------------------------------------------------------------------- #
# Install helper — wire hooks + print MCP config
# --------------------------------------------------------------------------- #

def invocation():
    """How to invoke this tool from a settings.json hook / shell.

    Prefer the installed `mutexcc` console script if it's on PATH (pipx / uvx /
    pip installs); otherwise fall back to running this file with its
    interpreter so a loose checkout still works.
    """
    import shutil
    found = shutil.which("mutexcc")
    if found:
        return '"%s"' % found
    return '"%s" "%s"' % (sys.executable, os.path.abspath(__file__))


def install_hooks(scope):
    self_cmd = invocation()
    if scope == "project":
        settings = os.path.join(os.getcwd(), ".claude", "settings.json")
    else:
        settings = os.path.join(os.path.expanduser("~"), ".claude", "settings.json")
    os.makedirs(os.path.dirname(settings), exist_ok=True)
    data = {}
    if os.path.exists(settings):
        with open(settings) as f:
            try:
                data = json.load(f)
            except Exception:
                data = {}
    hooks = data.setdefault("hooks", {})
    matcher = "Edit|Write|MultiEdit|NotebookEdit"

    def entry(sub):
        return {"matcher": matcher, "hooks": [
            {"type": "command", "command": "%s hook %s" % (self_cmd, sub)}
        ]}

    def add(event, sub):
        lst = hooks.setdefault(event, [])
        cmd = "%s hook %s" % (self_cmd, sub)
        for grp in lst:
            for h in grp.get("hooks", []):
                if h.get("command") == cmd:
                    return
        lst.append(entry(sub))

    add("PreToolUse", "pre")
    add("PostToolUse", "post")
    with open(settings, "w") as f:
        json.dump(data, f, indent=2)
    print("Hooks installed in %s" % settings)
    print()
    print("To enable the cooperative MCP server, run:")
    print("  claude mcp add mutexcc -- %s mcp" % self_cmd)
    print()
    print("Set MUTEXCC_AGENT to a stable id per agent if CLAUDE_SESSION_ID")
    print("is unavailable, so each agent is distinguished.")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main():
    ap = argparse.ArgumentParser(prog="mutexcc", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    sub = ap.add_subparsers(dest="cmd", required=True)

    pa = sub.add_parser("acquire", help="acquire a lock (blocks by default)")
    pa.add_argument("path")
    pa.add_argument("--agent")
    pa.add_argument("--no-block", action="store_true")
    pa.add_argument("--timeout", type=float, default=120)
    pa.add_argument("--ttl", type=float, default=0, help="auto-expire seconds")
    pa.add_argument("--note")

    pr = sub.add_parser("release", help="release a lock")
    pr.add_argument("path", nargs="?")
    pr.add_argument("--agent")
    pr.add_argument("--all", action="store_true", help="release all my locks")

    pc = sub.add_parser("check", help="is a path free?")
    pc.add_argument("path")
    pc.add_argument("--agent")

    sub.add_parser("status", help="list all held locks")
    sub.add_parser("gc", help="prune expired / dead-process locks")

    ph = sub.add_parser("hook", help="(internal) Claude Code hook handler")
    ph.add_argument("phase", choices=["pre", "post"])

    sub.add_parser("mcp", help="run the MCP server on stdio")

    pi = sub.add_parser("install-hooks", help="wire hooks into settings.json")
    pi.add_argument("--scope", choices=["project", "user"], default="project")

    args = ap.parse_args()

    def emit(obj, human):
        if args.json:
            print(json.dumps(obj, indent=2))
        else:
            print(human)

    if args.cmd == "hook":
        return hook_pre() if args.phase == "pre" else hook_post()
    if args.cmd == "mcp":
        return mcp_serve()
    if args.cmd == "install-hooks":
        return install_hooks(args.scope)

    if args.cmd == "acquire":
        res = acquire(args.path, agent_id(args.agent),
                      blocking=not args.no_block, timeout=args.timeout,
                      ttl=args.ttl, note=args.note)
        if res["ok"]:
            emit(res, "locked: %s (lock #%s)" % (res["path"], res["lock_id"]))
        else:
            b = res.get("blocked_by", {})
            emit(res, "FAILED: %s held by agent %s%s"
                 % (res["path"], b.get("agent", "?"),
                    " (timeout)" if res.get("timeout") else ""))
            sys.exit(1)

    elif args.cmd == "release":
        res = release(args.path, agent_id(args.agent), all_for_agent=args.all)
        emit(res, "released %d lock(s)" % res["released"])

    elif args.cmd == "check":
        res = check(args.path, agent_id(args.agent))
        if res["free"]:
            emit(res, "FREE: %s" % res["path"])
        else:
            b = res["blocked_by"]
            emit(res, "LOCKED: %s by agent %s" % (res["path"], b["agent"]))
            sys.exit(1)

    elif args.cmd == "status":
        locks = list_locks()
        if args.json:
            print(json.dumps(locks, indent=2))
        elif not locks:
            print("no locks held")
        else:
            for l in locks:
                exp = (" exp:%ss" % int(l["expires_in_s"])) if l["expires_in_s"] else ""
                print("#%-3d %-8s held:%ss%s  %s"
                      % (l["id"], l["agent"][:8], int(l["held_for_s"]), exp, l["path"]))

    elif args.cmd == "gc":
        conn = connect()
        n = prune(conn)
        emit({"pruned": n}, "pruned %d stale lock(s)" % n)


if __name__ == "__main__":
    main()
