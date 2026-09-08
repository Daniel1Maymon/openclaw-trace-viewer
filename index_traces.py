#!/usr/bin/env python3
"""Build a SQLite index over OpenClaw trajectory files. Read-only on the source.

The index is a growing archive, never a mirror. A trajectory file is a rolling
10 MB window: OpenClaw rewrites the whole file at the end of every run and drops
the oldest lines to stay under the cap (`trimJsonlWindow` in its source). So a
run that has aged out of its file exists nowhere but here. Two rules follow, and
both are load-bearing:

  * never DROP the runs table and rebuild — that would delete trimmed-away runs
    from the only copy that still has them;
  * never let a re-read downgrade a complete row to an empty one, which is what
    a half-trimmed run parses as (see the WHERE clause in `upsert_sql`).
"""
import json, glob, os, sqlite3, sys, time
from datetime import datetime, timezone

def iso_ms(ts):
    if not isinstance(ts, str):
        return None
    try:
        return int(datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ")
                   .replace(tzinfo=timezone.utc).timestamp() * 1000)
    except Exception:
        return None

# Where OpenClaw keeps its per-agent session data. Override for a local copy
# pulled off the server, or to point at a different install.
AGENTS_DIR = os.environ.get("OPENCLAW_AGENTS_DIR", "/root/.openclaw/agents")
DB = os.environ.get("TRACE_DB",
                    os.path.join(os.path.dirname(os.path.abspath(__file__)), "traces.db"))
# Agents to leave out of the index (throwaway test agents, by default none).
def _skip_agents():
    """Agents to leave out of the index.

    `TRACE_SKIP_AGENTS` wins when set, but it is trivially forgotten: the cron
    line carries it and a hand-started viewer does not, which is how two dead
    test agents got indexed once already. So when the variable is absent the
    list comes from `skip_agents.txt` beside this script — one agent per line,
    `#` comments allowed. That way the setting belongs to the deployment
    instead of to whoever happens to type the start command.
    """
    raw = os.environ.get("TRACE_SKIP_AGENTS")
    if raw is None:
        try:
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "skip_agents.txt")) as fh:
                raw = ",".join(line.split("#")[0] for line in fh)
        except OSError:
            raw = ""
    return {a.strip() for a in raw.split(",") if a.strip()}

SKIP_AGENTS = _skip_agents()

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY, session_id TEXT, agent TEXT, session_key TEXT,
  provider TEXT, model TEXT, trigger TEXT, workspace_dir TEXT,
  started_ts TEXT, ended_ts TEXT, duration_ms INTEGER,
  status TEXT, ok INTEGER, failure_kind TEXT,
  input_tokens INTEGER, output_tokens INTEGER, cache_read INTEGER,
  reasoning_tokens INTEGER, total_tokens INTEGER, cost_usd REAL,
  tool_count INTEGER, tool_names TEXT, error_tool_count INTEGER,
  msg_count INTEGER, compaction_count INTEGER,
  user_text TEXT, reply_text TEXT,
  file_path TEXT, has_transcript INTEGER
);
CREATE INDEX IF NOT EXISTS idx_started ON runs(started_ts DESC);
CREATE INDEX IF NOT EXISTS idx_agent ON runs(agent);
CREATE INDEX IF NOT EXISTS idx_ok ON runs(ok);

-- What each trajectory file looked like the last time we read it. A file whose
-- timestamp and size both match is untouched and gets skipped: that check is
-- what makes a refresh cost milliseconds instead of seconds.
CREATE TABLE IF NOT EXISTS seen (
  path TEXT PRIMARY KEY, mtime REAL, size INTEGER
);
"""


def connect(db=None):
    """A connection safe to use while another thread is writing.

    WAL lets the viewer keep serving pages during a re-index instead of erroring
    with 'database is locked'; busy_timeout makes the rare genuine collision wait
    rather than fail."""
    con = sqlite3.connect(db or DB, timeout=10)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=10000")
    return con

def num(x):
    return x if isinstance(x, (int, float)) and not isinstance(x, bool) else 0

def cost_of(usage):
    if not isinstance(usage, dict):
        return 0.0
    c = usage.get("cost")
    if isinstance(c, dict):
        if isinstance(c.get("total"), (int, float)):
            return float(c["total"])
        return float(sum(v for v in c.values() if isinstance(v, (int, float))))
    return float(c) if isinstance(c, (int, float)) else 0.0

def text_of(content, limit=400):
    """messagesSnapshot content is a string or a list of typed blocks."""
    if isinstance(content, str):
        return content[:limit]
    if isinstance(content, list):
        out = []
        for b in content:
            if isinstance(b, dict):
                if b.get("type") == "text" and isinstance(b.get("text"), str):
                    out.append(b["text"])
                elif isinstance(b.get("text"), str):
                    out.append(b["text"])
            elif isinstance(b, str):
                out.append(b)
        return " ".join(out)[:limit]
    return ""

def failure_kind(a):
    for k, label in (("timedOutByRunBudget", "run budget"),
                     ("timedOutDuringToolExecution", "tool timeout"),
                     ("timedOutDuringCompaction", "compaction timeout"),
                     ("idleTimedOut", "idle timeout"),
                     ("timedOut", "timeout"),
                     ("externalAbort", "external abort"),
                     ("aborted", "aborted")):
        if a.get(k):
            return label
    return ""

def parse_file(path, agent, has_transcript):
    runs = {}
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return []
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except Exception:
                continue
            rid = e.get("runId") or e.get("sessionId")
            if not rid:
                continue
            r = runs.setdefault(rid, {
                "run_id": rid, "session_id": e.get("sessionId"), "agent": agent,
                "session_key": e.get("sessionKey"), "provider": e.get("provider"),
                "model": e.get("modelId"), "workspace_dir": e.get("workspaceDir"),
                "trigger": "", "started_ts": None, "ended_ts": None, "status": "",
                "ok": 0, "failure_kind": "", "input_tokens": 0, "output_tokens": 0,
                "cache_read": 0, "reasoning_tokens": 0, "total_tokens": 0,
                "cost_usd": 0.0, "tool_count": 0, "tool_names": "",
                "error_tool_count": 0, "msg_count": 0, "compaction_count": 0,
                "user_text": "", "reply_text": "",
                "file_path": path, "has_transcript": has_transcript,
            })
            t, ts, d = e.get("type"), e.get("ts"), (e.get("data") or {})
            if not isinstance(d, dict):
                d = {}
            if t == "session.started":
                r["started_ts"] = ts
                r["trigger"] = d.get("trigger") or ""
            elif t == "session.ended":
                r["ended_ts"] = ts
                r["status"] = d.get("status") or r["status"]
            elif t == "model.completed":
                snap = d.get("messagesSnapshot") or []
                # CUMULATIVE snapshot: it holds the whole conversation. Anything
                # stamped before this run started belongs to an earlier turn.
                t0 = iso_ms(r["started_ts"])
                tools, users, replies, cost = [], [], [], 0.0
                for m in snap:
                    if not isinstance(m, dict):
                        continue
                    mts = m.get("timestamp")
                    if t0 is not None and isinstance(mts, (int, float)) and mts < t0:
                        continue
                    r["msg_count"] += 1
                    role = m.get("role")
                    if role == "toolResult":
                        tools.append(m.get("toolName") or "?")
                        if m.get("isError"):
                            r["error_tool_count"] += 1
                    elif role == "user":
                        users.append(text_of(m.get("content")))
                    elif role == "assistant":
                        txt = text_of(m.get("content"))
                        if txt.strip():
                            replies.append(txt)
                        cost += cost_of(m.get("usage"))
                r["cost_usd"] = r["cost_usd"] or cost
                if tools:
                    r["tool_names"] = ",".join(sorted(set(tools)))
                    r["tool_count"] = len(tools)
                if users and not r["user_text"]:
                    r["user_text"] = users[0]
                if replies:
                    r["reply_text"] = replies[-1]
            elif t == "trace.artifacts":
                u = d.get("usage") or {}
                r["input_tokens"] = num(u.get("input"))
                r["output_tokens"] = num(u.get("output"))
                r["cache_read"] = num(u.get("cacheRead"))
                r["reasoning_tokens"] = num(u.get("reasoningTokens"))
                r["total_tokens"] = num(u.get("total"))
                r["compaction_count"] = num(d.get("compactionCount"))
                r["status"] = d.get("finalStatus") or r["status"]
                r["failure_kind"] = failure_kind(d)
                metas = d.get("toolMetas")
                if isinstance(metas, list) and metas and not r["tool_names"]:
                    names = [m.get("toolName", "?") for m in metas if isinstance(m, dict)]
                    r["tool_names"] = ",".join(sorted(set(names)))
                    r["tool_count"] = len(names)
                if not r["reply_text"]:
                    at = d.get("assistantTexts")
                    if isinstance(at, list) and at:
                        r["reply_text"] = str(at[-1])[:400]

    out = []
    for r in runs.values():
        if r["started_ts"] and r["ended_ts"]:
            try:
                from datetime import datetime
                f = "%Y-%m-%dT%H:%M:%S.%fZ"
                a = datetime.strptime(r["started_ts"], f)
                b = datetime.strptime(r["ended_ts"], f)
                r["duration_ms"] = int((b - a).total_seconds() * 1000)
            except Exception:
                r["duration_ms"] = None
        else:
            r["duration_ms"] = None
        bad = r["failure_kind"] or (r["status"] or "").lower() in ("error", "failed", "aborted")
        # a run with no model.completed never finished
        incomplete = r["msg_count"] == 0 and r["total_tokens"] == 0
        r["ok"] = 0 if (bad or incomplete) else 1
        if incomplete and not r["failure_kind"]:
            r["failure_kind"] = "incomplete"
        out.append(r)
    return out

def list_files():
    """Every trajectory file worth indexing, as (path, agent)."""
    out = []
    for adir in sorted(glob.glob(os.path.join(AGENTS_DIR, "*"))):
        agent = os.path.basename(adir)
        if agent in SKIP_AGENTS:
            continue
        out += [(p, agent) for p in
                sorted(glob.glob(os.path.join(adir, "sessions", "*.trajectory.jsonl")))]
    return out

def upsert_sql(cols):
    """Add or update a run, but never blank out one we already have in full.

    A file that has been trimmed can hand us a run whose `session.started` and
    `model.completed` lines are gone; that parses as a row with no messages and
    no tokens. Without the WHERE, re-reading such a file would overwrite the
    good stored row with the stub."""
    setters = ", ".join(f"{c}=excluded.{c}" for c in cols if c != "run_id")
    return (f"INSERT INTO runs ({','.join(cols)}) VALUES ({','.join('?' * len(cols))}) "
            f"ON CONFLICT(run_id) DO UPDATE SET {setters} "
            f"WHERE excluded.msg_count > 0 OR excluded.total_tokens > 0 "
            f"   OR (runs.msg_count = 0 AND runs.total_tokens = 0)")

def sweep(con, verbose=False):
    """One incremental pass. Returns (files_reread, rows_written, errors).

    Reads the timestamp and size of every trajectory file — about 10ms for a
    thousand of them — and opens only the ones that moved. A run is only written
    to disk when it finishes, so a file whose stats changed means exactly one
    thing: a turn completed in that session."""
    cols = [c[1] for c in con.execute("PRAGMA table_info(runs)")]
    sql = upsert_sql(cols)
    seen = {p: (m, s) for p, m, s in con.execute("SELECT path, mtime, size FROM seen")}
    files = list_files()
    changed, n_rows, errs = 0, 0, 0

    for path, agent in files:
        try:
            st = os.stat(path)
        except OSError:
            continue
        prev = seen.get(path)
        if prev and prev[0] == st.st_mtime and prev[1] == st.st_size:
            continue
        base = path[: -len(".trajectory.jsonl")]
        has_tr = 1 if os.path.exists(base + ".jsonl") else 0
        try:
            rows = parse_file(path, agent, has_tr)
        except Exception as ex:
            errs += 1
            print(f"  ERROR {path}: {ex}", file=sys.stderr)
            continue
        for r in rows:
            con.execute(sql, [r.get(c) for c in cols])
            n_rows += 1
        con.execute("INSERT INTO seen (path, mtime, size) VALUES (?,?,?) "
                    "ON CONFLICT(path) DO UPDATE SET mtime=excluded.mtime, size=excluded.size",
                    (path, st.st_mtime, st.st_size))
        changed += 1
        if verbose and changed % 200 == 0:
            con.commit()
            print(f"  ... {changed} files re-read, {n_rows} runs", flush=True)

    con.commit()
    return changed, n_rows, errs

def main():
    t0 = time.time()
    con = connect()
    con.executescript(SCHEMA)
    changed, n_rows, errs = sweep(con, verbose=True)
    held = con.execute("SELECT COUNT(*) FROM runs").fetchone()[0]

    print(f"\nre-read {changed} changed files -> {n_rows} runs written "
          f"in {time.time()-t0:.1f}s ({errs} file errors)")
    print(f"index holds {held} runs")
    for row in con.execute("SELECT agent, COUNT(*), SUM(ok=0), ROUND(SUM(cost_usd),4) "
                           "FROM runs GROUP BY agent"):
        print(f"  {row[0]:<12} runs={row[1]:<5} failed={row[2]:<5} cost=${row[3]}")
    con.close()

if __name__ == "__main__":
    main()
