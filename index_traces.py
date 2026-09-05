#!/usr/bin/env python3
"""Build a SQLite index over OpenClaw trajectory files. Read-only on the source."""
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
SKIP_AGENTS = {a for a in os.environ.get("TRACE_SKIP_AGENTS", "").split(",") if a}

SCHEMA = """
DROP TABLE IF EXISTS runs;
CREATE TABLE runs (
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
CREATE INDEX idx_started ON runs(started_ts DESC);
CREATE INDEX idx_agent ON runs(agent);
CREATE INDEX idx_ok ON runs(ok);
"""

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

def main():
    t0 = time.time()
    con = sqlite3.connect(DB)
    con.executescript(SCHEMA)
    cols = [c[1] for c in con.execute("PRAGMA table_info(runs)")]
    ins = f"INSERT OR REPLACE INTO runs ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})"

    files, n_rows, n_files, errs = [], 0, 0, 0
    for adir in sorted(glob.glob(os.path.join(AGENTS_DIR, "*"))):
        agent = os.path.basename(adir)
        if agent in SKIP_AGENTS:
            continue
        files += [(p, agent) for p in sorted(glob.glob(os.path.join(adir, "sessions", "*.trajectory.jsonl")))]

    for path, agent in files:
        base = path[: -len(".trajectory.jsonl")]
        has_tr = 1 if os.path.exists(base + ".jsonl") else 0
        try:
            rows = parse_file(path, agent, has_tr)
        except Exception as ex:
            errs += 1
            print(f"  ERROR {path}: {ex}", file=sys.stderr)
            continue
        n_files += 1
        for r in rows:
            con.execute(ins, [r.get(c) for c in cols])
            n_rows += 1
        if n_files % 200 == 0:
            con.commit()
            print(f"  ... {n_files}/{len(files)} files, {n_rows} runs", flush=True)
    con.commit()

    print(f"\nindexed {n_files} files -> {n_rows} runs in {time.time()-t0:.1f}s ({errs} file errors)")
    for row in con.execute("SELECT agent, COUNT(*), SUM(ok=0), ROUND(SUM(cost_usd),4) FROM runs GROUP BY agent"):
        print(f"  {row[0]:<12} runs={row[1]:<5} failed={row[2]:<5} cost=${row[3]}")
    con.close()

if __name__ == "__main__":
    main()
