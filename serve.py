#!/usr/bin/env python3
"""Read-only trace viewer over OpenClaw trajectory files. Binds to 127.0.0.1 only."""
import hashlib, json, os, random, re, sqlite3, sys, threading, time, urllib.parse
from datetime import datetime, timezone

def iso_ms(ts):
    """'2026-08-29T10:46:09.724Z' -> epoch ms, or None."""
    if not isinstance(ts, str):
        return None
    try:
        return int(datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ")
                   .replace(tzinfo=timezone.utc).timestamp() * 1000)
    except Exception:
        return None
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.environ.get("TRACE_DB", os.path.join(HERE, "traces.db"))
# Loopback only, on purpose: reach it over an SSH tunnel, never expose it.
HOST = os.environ.get("TRACE_HOST", "127.0.0.1")
PORT = int(os.environ.get("TRACE_PORT", "8765"))
# Seconds between "did anything change?" checks. 0 turns the timer off and
# leaves only the refresh button.
REFRESH_SEC = int(os.environ.get("TRACE_REFRESH_SEC", "30"))

sys.path.insert(0, HERE)
try:
    import index_traces as indexer
except Exception as _ex:          # the viewer still serves the existing index
    indexer, _import_error = None, str(_ex)

def db():
    """Reader connection. busy_timeout so a page load waits out a concurrent
    index write instead of failing with 'database is locked'."""
    con = sqlite3.connect(DB, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=10000")
    return con

# ---------- keeping the index fresh ----------
# A trajectory file is only written when a run *finishes* — OpenClaw holds every
# event of a run in memory and rewrites the whole file during cleanup. So there
# is nothing to see between runs, and a timer misses nothing that an OS-level
# file watcher would catch. Checking timestamp+size for ~1,100 files costs about
# 10ms, which is what makes a 30s poll the boring right answer.
#
# DATA_VERSION is not a count of anything. It is a label the page compares
# against its own copy to decide whether it needs to re-fetch the list.
DATA_VERSION = 0
SWEEP = {"checked": None, "running": False, "error": None,
         "last_files": 0, "last_runs": 0}
_sweep_lock = threading.Lock()

def run_sweep():
    """One incremental index pass. Bumps DATA_VERSION only if a file moved."""
    global DATA_VERSION
    if indexer is None:
        SWEEP["error"] = f"indexer unavailable: {_import_error}"
        return 0
    with _sweep_lock:
        SWEEP["running"] = True
        try:
            con = indexer.connect(DB)
            con.executescript(indexer.SCHEMA)
            changed, rows, errs = indexer.sweep(con)
            con.close()
            SWEEP.update({"error": None, "last_files": changed, "last_runs": rows})
            if changed:
                DATA_VERSION += 1
            return changed
        except Exception as ex:
            SWEEP["error"] = str(ex)
            return 0
        finally:
            SWEEP["running"] = False
            SWEEP["checked"] = datetime.now(timezone.utc).strftime("%H:%M:%S")

def refresh_loop():
    while True:
        time.sleep(REFRESH_SEC)
        run_sweep()

# ---------- detail: re-parse one run out of its trajectory file ----------

def as_text(v):
    """Content fields are usually strings but not always; never trust the type."""
    if isinstance(v, str):
        return v
    if v is None:
        return ""
    try:
        return json.dumps(v, ensure_ascii=False, indent=2)
    except Exception:
        return str(v)

def blocks(content):
    """Flatten a message's content, recording each value's JSON path so the UI
    can always say which key it is showing."""
    if isinstance(content, str):
        return [{"kind": "text", "text": content, "path": "content"}]
    if isinstance(content, dict):
        content = [content]
    if not isinstance(content, list):
        return [{"kind": "text", "text": as_text(content), "path": "content"}]
    out = []
    for i, b in enumerate(content):
        pre = f"content[{i}]"
        if isinstance(b, str):
            out.append({"kind": "text", "text": b, "path": pre})
        elif isinstance(b, dict):
            t = b.get("type")
            if t == "text":
                out.append({"kind": "text", "text": as_text(b.get("text")), "path": f"{pre}.text"})
            elif t == "thinking":
                out.append({"kind": "thinking", "text": as_text(b.get("thinking")), "path": f"{pre}.thinking"})
            elif t == "toolCall":
                key = "partialArgs" if b.get("partialArgs") else "arguments"
                out.append({"kind": "toolCall", "id": b.get("id"), "name": b.get("name"),
                            "args": as_text(b.get("partialArgs") or b.get("arguments") or ""),
                            "path": f"{pre}.{key}", "namePath": f"{pre}.name"})
            else:
                out.append({"kind": "text", "text": as_text(b), "path": pre})
        else:
            out.append({"kind": "text", "text": as_text(b), "path": pre})
    return out

# ---------- redaction (TRACE_REDACT=1) ----------
# Replaces conversation content with same-shaped placeholder text so the viewer
# can be screenshotted or demoed without publishing anyone's messages. Timings,
# costs, token counts, tool names, roles and the call/result id wiring all
# survive untouched — those are what the UI is actually demonstrating.

REDACT = os.environ.get("TRACE_REDACT", "") not in ("", "0", "false", "no")

# Values kept verbatim: they are structure, not content.
_KEEP = {"role", "type", "toolName", "name", "api", "provider", "model",
         "stopReason", "thinkingSignature", "totalOrigin", "kind", "action",
         "status", "agent", "trigger", "finalStatus", "traceSchema"}
# Values replaced with a same-shaped fake, so ids still match across a run.
_IDS = {"id", "toolCallId", "responseId", "traceId", "sessionId", "runId",
        "listId", "jobId", "transcriptLeafId", "run_id", "session_id"}
_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
                   r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_WORDS = ("lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod "
          "tempor incididunt ut labore et dolore magna aliqua enim ad minim veniam "
          "quis nostrud exercitation ullamco laboris nisi aliquip ex ea commodo "
          "consequat duis aute irure in reprehenderit voluptate velit esse").split()

def _seed(s):
    return int(hashlib.md5(s.encode("utf-8")).hexdigest()[:8], 16)

def _fake_id(s):
    """Same shape, same length, stable — so a toolCallId still visibly pairs
    with its toolCall, which is half of what the trace view is for."""
    pre, h, i, out = "", hashlib.md5(s.encode("utf-8")).hexdigest() * 4, 0, []
    if "_" in s:
        pre, _, s = s.partition("_")
        pre += "_"
    for ch in s:
        if ch.isalnum():
            out.append(h[i]); i += 1
        else:
            out.append(ch)
    return pre + "".join(out)

def _fake_text(s):
    """Placeholder of the same length, line count and indentation, so the
    character counts shown in the UI stay honest."""
    if not s.strip():
        return s
    rnd = random.Random(_seed(s))
    lines = []
    for line in s.split("\n"):
        if not line.strip():
            lines.append(line); continue
        indent = line[:len(line) - len(line.lstrip())]
        target = max(len(line) - len(indent), 1)
        words, n = [], 0
        while n < target:
            w = rnd.choice(_WORDS); words.append(w); n += len(w) + 1
        lines.append(indent + " ".join(words)[:target])
    return "\n".join(lines)

def _fake_string(s):
    # Keep any embedded uuid recognisable as an id, fake the prose around it.
    parts, out = _UUID.split(s), []
    ids = _UUID.findall(s)
    for i, part in enumerate(parts):
        out.append(_fake_text(part))
        if i < len(ids):
            out.append(_fake_id(ids[i]))
    return "".join(out)

def scrub(x, key=None):
    """Walk any decoded JSON and redact content, leaving structure intact."""
    if not REDACT:
        return x
    if isinstance(x, dict):
        return {k: scrub(v, k) for k, v in x.items()}
    if isinstance(x, list):
        return [scrub(v, key) for v in x]
    if not isinstance(x, str):
        return x
    if key in _KEEP:
        return x
    if key in _IDS:
        return _fake_id(x)
    if key in ("sessionKey", "session_key"):
        # Only the numeric part identifies a person (a Telegram id); the rest
        # is routing and worth keeping legible.
        return re.sub(r"\d{5,}", lambda m: _fake_id(m.group()), x)
    stripped = x.strip()
    if stripped[:1] in "{[":
        # Tool results are usually JSON in a string. Redact inside it so the
        # rendered result still looks like JSON rather than a wall of lorem.
        try:
            return json.dumps(scrub(json.loads(stripped)), ensure_ascii=False, indent=2)
        except Exception:
            pass
    return _fake_string(x)

def load_run(path, run_id):
    system_prompt, tools, msgs, meta = "", [], [], {}
    if not os.path.exists(path):
        return {"error": f"trajectory file missing: {path}"}
    for line in open(path, encoding="utf-8", errors="replace"):
        line = line.strip()
        if not line or run_id not in line:
            continue
        try:
            e = json.loads(line)
        except Exception:
            continue
        if e.get("runId") != run_id:
            continue
        d = e.get("data") or {}
        if not isinstance(d, dict):
            continue
        t = e.get("type")
        if t == "context.compiled":
            system_prompt = scrub(d.get("systemPrompt") or "", "systemPrompt")
            for tl in (d.get("tools") or []):
                if isinstance(tl, dict):
                    tools.append(tl.get("name") or tl.get("function", {}).get("name") or "?")
        elif t == "session.started":
            meta["trigger"] = d.get("trigger")
            meta["toolCount"] = d.get("toolCount")
            meta["started"] = e.get("ts")
        elif t == "model.completed":
            msgs = scrub(d.get("messagesSnapshot") or [])
            meta["usage"] = d.get("usage")
        elif t == "trace.artifacts":
            meta["finalStatus"] = d.get("finalStatus")
            meta["compactionCount"] = d.get("compactionCount")
            for k in ("aborted", "timedOut", "idleTimedOut", "timedOutDuringToolExecution",
                      "timedOutDuringCompaction", "timedOutByRunBudget", "externalAbort"):
                if d.get(k):
                    meta.setdefault("flags", []).append(k)
        elif t == "session.ended":
            meta["ended"] = e.get("ts")

    # messagesSnapshot is CUMULATIVE: it carries the whole conversation, not just
    # this run's messages. Anchor on the run's own start so earlier turns are
    # marked as prior context rather than counted as this run's work.
    t0 = iso_ms(meta.get("started"))

    # A run is a sequence of MODEL CALLS. Each assistant message is one call's
    # output; everything before it in the array is that call's input. That is
    # the reconstruction openclaw-runtime-internals.md does by hand.
    messages, calls = [], []
    pending = {}
    for i, m in enumerate(msgs):
        if not isinstance(m, dict):
            continue
        ts = m.get("timestamp")
        offset = (ts - t0) if isinstance(ts, (int, float)) and t0 is not None else None
        role = m.get("role")
        bl = blocks(m.get("content"))
        for b in bl:
            if b["kind"] == "toolCall":
                pending[b.get("id")] = b
        entry = {
            "i": len(messages), "role": role, "ts": ts, "offset": offset,
            "prior": offset is not None and offset < 0,
            "blocks": bl, "raw": m,
        }
        if role == "toolResult":
            call = pending.get(m.get("toolCallId"))
            entry["toolName"] = m.get("toolName")
            entry["isError"] = bool(m.get("isError"))
            entry["args"] = (call or {}).get("args", "")
        elif role == "assistant":
            u = m.get("usage")
            entry["usage"] = u if isinstance(u, dict) else None
            entry["stopReason"] = m.get("stopReason")
            entry["model"] = m.get("model")
            calls.append({"n": len(calls) + 1, "i": entry["i"], "ts": ts,
                          "offset": offset, "prior": entry["prior"]})
        messages.append(entry)

    return {"systemPrompt": system_prompt, "tools": tools, "meta": meta,
            "messages": messages, "calls": calls}

# ---------- HTTP ----------

def hide_path(r):
    """Absolute paths name the deployment. Sweep every string field, not just
    file_path — workspace_dir carries one too."""
    if not REDACT:
        return r
    for k, v in list(r.items()):
        if isinstance(v, str) and v.startswith("/"):
            r[k] = "<redacted>/" + os.path.basename(v.rstrip("/"))
    return r

def scrub_row(r):
    """List rows come from the index, not the trajectory file, so they need
    their own pass. run_id / session_id stay real — the UI uses them to fetch."""
    if not REDACT:
        return r
    for k in ("user_text", "reply_text", "session_key"):
        if r.get(k):
            r[k] = scrub(r[k], k)
    return r

def query_runs(q):
    con = db()
    where, args = [], []
    if q.get("agent"):
        where.append("agent = ?"); args.append(q["agent"][0])
    if q.get("failed") == ["1"]:
        where.append("ok = 0")
    if q.get("q"):
        term = "%" + q["q"][0] + "%"
        where.append("(user_text LIKE ? OR reply_text LIKE ? OR tool_names LIKE ? "
                     "OR run_id LIKE ? OR session_id LIKE ?)")
        args += [term] * 5

    base = ("SELECT * FROM (SELECT *, "
            "ROW_NUMBER() OVER (PARTITION BY session_id ORDER BY started_ts) turn_n, "
            "COUNT(*) OVER (PARTITION BY session_id) turn_tot FROM runs)")
    if where:
        base += " WHERE " + " AND ".join(where)

    limit = int(q.get("limit", ["200"])[0])
    grouped = q.get("group", ["session"])[0] == "session"

    if grouped:
        # Group into sessions, newest activity first. A session's turns travel
        # with it, so expanding a row needs no second request.
        rows = [hide_path(scrub_row(dict(r))) for r in con.execute(base + " ORDER BY started_ts", args)]
        sess = {}
        for r in rows:
            g = sess.setdefault(r["session_id"], {
                "session_id": r["session_id"], "agent": r["agent"],
                "session_key": r["session_key"], "model": r["model"],
                "trigger": r["trigger"], "started_ts": r["started_ts"],
                "last_ts": r["started_ts"], "turns": 0, "duration_ms": 0,
                "cost_usd": 0.0, "tool_count": 0, "failed": 0,
                "total_tokens": 0, "user_text": "", "runs": [],
            })
            g["turns"] += 1
            g["last_ts"] = max(g["last_ts"] or "", r["started_ts"] or "")
            g["duration_ms"] += r["duration_ms"] or 0
            g["cost_usd"] += r["cost_usd"] or 0.0
            g["tool_count"] += r["tool_count"] or 0
            g["total_tokens"] = max(g["total_tokens"], r["total_tokens"] or 0)
            g["failed"] += 0 if r["ok"] else 1
            if not g["user_text"] and r["user_text"]:
                g["user_text"] = r["user_text"]
            g["runs"].append(r)
        out = sorted(sess.values(), key=lambda x: x["last_ts"] or "", reverse=True)[:limit]
        result = {"rows": out, "grouped": True}
    else:
        result = {"rows": [hide_path(scrub_row(dict(r))) for r in con.execute(
                      base + " ORDER BY started_ts DESC LIMIT ?", args + [limit])],
                  "grouped": False}

    tot = con.execute("SELECT COUNT(*), SUM(ok=0), ROUND(SUM(cost_usd),4), "
                      "COUNT(DISTINCT session_id) FROM runs").fetchone()
    result.update({"redact": REDACT,
                   "total": tot[0], "failed": tot[1], "cost": tot[2], "sessions": tot[3],
                   "agents": [r[0] for r in con.execute(
                       "SELECT DISTINCT agent FROM runs ORDER BY 1")]})
    con.close()
    return result

# A run can be judged on two independent questions, and merging them into one
# number would misrepresent both:
#
#   did it finish?          -> ok / failure_kind, set in index_traces.py
#   did anything go wrong   -> error_tool_count, counted per tool result
#   while it ran?
#
# A run can fail without a single tool error (it ran out of budget mid-thought),
# and a run can log a dozen tool errors and still end perfectly — the agent hit
# a denied command, worked around it, and answered. Both columns, never a sum.
LABEL = ("CASE WHEN failure_kind IS NOT NULL AND failure_kind != '' THEN failure_kind "
         "ELSE COALESCE(NULLIF(LOWER(status), ''), 'unknown') END")

def query_health(q):
    con = db()
    tot = con.execute(
        "SELECT COUNT(*), SUM(ok), SUM(1-ok), SUM(error_tool_count > 0), "
        "SUM(error_tool_count) FROM runs").fetchone()
    kinds = [{"kind": r[0], "n": r[1]} for r in con.execute(
        f"SELECT {LABEL}, COUNT(*) FROM runs WHERE ok = 0 GROUP BY 1 ORDER BY 2 DESC")]
    agents = [{"agent": r[0], "runs": r[1], "bad": r[2], "tool_err": r[3]}
              for r in con.execute(
                  "SELECT agent, COUNT(*), SUM(1-ok), SUM(error_tool_count > 0) "
                  "FROM runs GROUP BY 1 ORDER BY 2 DESC")]
    # Every failed run, newest first. 66 rows today; the cap is there so a bad
    # week can't turn this page into a multi-megabyte response.
    limit = int(q.get("limit", ["300"])[0])
    bad = [hide_path(scrub_row(dict(r))) for r in con.execute(
        f"SELECT run_id, session_id, agent, model, trigger, started_ts, duration_ms, "
        f"status, {LABEL} AS label, error_text, total_tokens, cost_usd, tool_count, "
        f"error_tool_count, msg_count, user_text, reply_text "
        f"FROM runs WHERE ok = 0 ORDER BY started_ts DESC LIMIT ?", (limit,))]
    con.close()
    return {"redact": REDACT, "total": tot[0], "ok": tot[1], "bad": tot[2],
            "tool_err_runs": tot[3], "tool_err_total": tot[4],
            "kinds": kinds, "agents": agents, "bad_runs": bad,
            "truncated": max(0, (tot[2] or 0) - len(bad))}

class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype):
        b = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _version(self):
        return {"v": DATA_VERSION, "every": REFRESH_SEC, "checked": SWEEP["checked"],
                "running": SWEEP["running"], "error": SWEEP["error"],
                "last_files": SWEEP["last_files"], "last_runs": SWEEP["last_runs"]}

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        try:
            if u.path == "/api/reindex":
                run_sweep()          # on demand; the timer keeps running too
                return self._send(200, json.dumps(self._version()), "application/json")
            self._send(404, "not found", "text/plain")
        except Exception as ex:
            self._send(500, json.dumps({"error": str(ex)}), "application/json")

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        try:
            if u.path == "/":
                return self._send(200, PAGE, "text/html; charset=utf-8")
            if u.path == "/health":
                return self._send(200, HEALTH_PAGE, "text/html; charset=utf-8")
            if u.path == "/api/health":
                return self._send(200, json.dumps(query_health(q), ensure_ascii=False),
                                  "application/json")
            if u.path == "/api/version":
                # ~60 bytes. The page asks this every few seconds and only
                # re-fetches the run list when the value differs from its own.
                return self._send(200, json.dumps(self._version()), "application/json")
            if u.path == "/api/runs":
                return self._send(200, json.dumps(query_runs(q)), "application/json")
            if u.path.startswith("/api/session/"):
                sid = urllib.parse.unquote(u.path[len("/api/session/"):])
                con = db()
                rows = [scrub_row(dict(x)) for x in con.execute(
                    "SELECT * FROM runs WHERE session_id = ? ORDER BY started_ts", (sid,))]
                con.close()
                if not rows:
                    return self._send(404, json.dumps({"error": "no such session"}),
                                      "application/json")
                CAP = 25   # a few sessions run to 43 turns; don't ship tens of MB
                shown, truncated = rows[:CAP], max(0, len(rows) - CAP)
                for r in shown:
                    r["detail"] = load_run(r["file_path"], r["run_id"])
                    hide_path(r)
                return self._send(200, json.dumps({
                    "session_id": sid, "agent": rows[0]["agent"],
                    "session_key": rows[0]["session_key"], "model": rows[0]["model"],
                    "turns": len(rows), "truncated": truncated,
                    "duration_ms": sum(r.get("duration_ms") or 0 for r in rows),
                    "cost_usd": sum(r.get("cost_usd") or 0 for r in rows),
                    "tool_count": sum(r.get("tool_count") or 0 for r in rows),
                    "failed": sum(0 if r["ok"] else 1 for r in rows),
                    "runs": shown,
                }, ensure_ascii=False), "application/json")
            if u.path.startswith("/api/run/"):
                rid = urllib.parse.unquote(u.path[len("/api/run/"):])
                con = db()
                row = con.execute("SELECT * FROM runs WHERE run_id = ?", (rid,)).fetchone()
                con.close()
                if not row:
                    return self._send(404, json.dumps({"error": "no such run"}), "application/json")
                row = dict(row)
                row["detail"] = load_run(row["file_path"], rid)
                hide_path(scrub_row(row))
                return self._send(200, json.dumps(row, ensure_ascii=False), "application/json")
            self._send(404, "not found", "text/plain")
        except Exception as ex:
            self._send(500, json.dumps({"error": str(ex)}), "application/json")

PAGE = r"""<!doctype html><meta charset=utf-8><title>OpenClaw Traces</title>
<style>
:root{--bg:#fff;--fg:#111;--dim:#666;--line:#e3e3e3;--card:#fafafa;--accent:#2b6cb0;--bad:#c53030;--warn:#b7791f}
@media(prefers-color-scheme:dark){:root{--bg:#15171a;--fg:#e8e8e8;--dim:#9aa0a6;--line:#2c3036;--card:#1c1f23;--accent:#7aa7d9;--bad:#f28b82;--warn:#e0b95d}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:13px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
header{padding:10px 16px;border-bottom:1px solid var(--line);display:flex;gap:12px;align-items:center;flex-wrap:wrap;position:sticky;top:0;background:var(--bg);z-index:5}
h1{font-size:14px;margin:0;font-weight:600}
input,select,button{font:inherit;padding:4px 8px;border:1px solid var(--line);border-radius:5px;background:var(--card);color:var(--fg)}
button{cursor:pointer}
.stat{color:var(--dim);font-size:12px}
main{display:grid;grid-template-columns:minmax(420px,1fr) 1.3fr;height:calc(100vh - 47px)}
main.nodetail{grid-template-columns:1fr}
main.nodetail>#detail{display:none}
main.nodetail #list{border-right:none}
#togdetail{white-space:nowrap}
.redbadge{background:var(--warn);color:#111;font-weight:700;padding:1px 6px;border-radius:4px;font-size:10px;letter-spacing:.06em}
#list{overflow:auto;border-right:1px solid var(--line)}
table{width:100%;border-collapse:collapse}
th{position:sticky;top:0;background:var(--bg);text-align:left;font-weight:600;font-size:11px;color:var(--dim);padding:6px 8px;border-bottom:1px solid var(--line);text-transform:uppercase;letter-spacing:.04em}
td{padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}
tr[data-id]{cursor:pointer}tr[data-id]:hover td{background:var(--card)}
/* The selected row has to be findable at a glance in a list of 200. --card alone
   is a couple of percent off the page background and disappears in dark mode. */
tr.sel td{background:color-mix(in srgb,var(--accent) 15%,transparent);box-shadow:inset 4px 0 var(--accent)}
tr.sel td:first-child{font-weight:600}
tr.sel .dim,tr.sel .msg{color:var(--fg)}
.msg{color:var(--dim);max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.warnchip{color:var(--warn);font-size:11px}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11px}
.bad{color:var(--bad);font-weight:600}.dim{color:var(--dim)}
#detail{overflow:auto;padding:16px}
.step{border-left:2px solid var(--line);padding:0 0 12px 12px;margin-left:6px;position:relative}
.step:before{content:"";position:absolute;left:-5px;top:5px;width:8px;height:8px;border-radius:50%;background:var(--line)}
.step.tool:before{background:var(--accent)}.step.err:before{background:var(--bad)}
.lbl{font-size:11px;color:var(--dim);display:flex;gap:8px;align-items:baseline}
.lbl b{color:var(--fg);font-size:12px}
pre{background:var(--card);border:1px solid var(--line);border-radius:6px;padding:8px;overflow:auto;max-height:280px;white-space:pre-wrap;word-break:break-word;font-family:ui-monospace,Menlo,monospace;font-size:11px;margin:5px 0}
details>summary{cursor:pointer;color:var(--accent);font-size:12px;padding:3px 0}
.empty{color:var(--dim);padding:40px;text-align:center}
.call.prior{opacity:.55}
.callhead{font-weight:600;font-size:13px;margin-bottom:6px;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.outlbl{font-size:10px;letter-spacing:.08em;color:var(--dim);margin:8px 0 3px;text-transform:uppercase}
.sub{margin:0 0 6px 10px;border-left:2px solid var(--line);padding-left:8px}
.sub.tool{border-left-color:var(--accent)}.sub.err{border-left-color:var(--bad)}
details.inp>summary{color:var(--warn);font-weight:600}
.usermsg{border-left:3px solid var(--accent);padding-left:8px;margin:0 0 10px}
button.more{display:block;margin:-2px 0 6px;padding:2px 8px;font-size:11px;border:1px solid var(--line);border-radius:5px;background:var(--card);color:var(--accent);cursor:pointer}
button.more:hover{border-color:var(--accent)}
pre.expanded{max-height:70vh}
tr.sessrow{cursor:pointer;font-weight:600}
tr.sessrow:hover td{background:var(--card)}
tr.sessrow.open td{background:var(--card)}
td.twist{width:16px;color:var(--dim);text-align:center;user-select:none;font-size:9px}
tr.child td{background:color-mix(in srgb,var(--card) 60%,transparent);font-weight:400}
tr.child td:first-child{border-left:3px solid var(--accent)}
td.sess{cursor:pointer;font-size:11px}td.sess:hover{color:var(--accent);text-decoration:underline}
.priorwrap{margin:0 0 12px;border:1px dashed var(--line);border-radius:8px;padding:6px 10px}
.priorwrap>summary{color:var(--dim);font-size:12px}
.ctx{border:1px solid var(--warn);border-left-width:4px;border-radius:8px;padding:8px 10px;margin:0 0 6px;background:color-mix(in srgb,var(--warn) 6%,transparent)}
.ctxhead{font-size:10px;letter-spacing:.09em;text-transform:uppercase;color:var(--warn);font-weight:700}
.ctxhead.fin{color:var(--ok,#7ec699)}
.ctx:has(>.ctxhead.fin){border-color:var(--ok,#7ec699)}
.ctxsum{font-size:12px;margin:2px 0 6px}
.grow{color:var(--warn);font-weight:600}
.added{margin:4px 0 6px}
.addedlbl{font-size:10px;letter-spacing:.06em;text-transform:uppercase;color:var(--dim);margin-bottom:3px}
.addrow{margin:0 0 5px}
.ctxmsg{border-top:1px solid var(--line);padding:5px 0}
.ctxmsg.new{background:color-mix(in srgb,var(--warn) 9%,transparent);border-radius:5px;padding:5px 6px}
.ctxmsghead{display:flex;gap:6px;align-items:center;margin-bottom:2px}
.newbadge{font-size:9px;letter-spacing:.08em;text-transform:uppercase;background:var(--warn);color:#1a1a1a;border-radius:8px;padding:0 6px;font-weight:700}
.spwrap>summary{color:var(--dim);font-size:11px}
/* A failed run's own explanation. Loud, because the whole point of the page is
   answering "what went wrong" without opening a trajectory file by hand. */
.errbar{background:color-mix(in srgb,var(--bad) 14%,transparent);border:1px solid var(--bad);
  border-left-width:4px;border-radius:6px;padding:8px 10px;margin:8px 0 12px;font-size:12px}
.errbar b{color:var(--bad);text-transform:uppercase;font-size:11px;letter-spacing:.04em;
  display:block;margin-bottom:3px}
.errbar .msg{color:var(--fg);max-width:none;white-space:normal;font-family:ui-monospace,Menlo,monospace}
.errbar.recov{background:color-mix(in srgb,var(--warn) 12%,transparent);border-color:var(--warn)}
.errbar.recov b{color:var(--warn)}
.turnbar{position:sticky;top:0;z-index:2;background:var(--accent);color:#fff;font-weight:700;font-size:12px;padding:5px 10px;border-radius:6px;margin:20px 0 8px;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.turnbar .dim{color:rgba(255,255,255,.8);font-weight:400}
.recovchip{background:var(--warn);color:#000;font-weight:700;font-size:11px;padding:1px 7px;border-radius:5px}
/* The turn bar doubles as the fold handle, so it needs room for the marker and
   a marker that reads against the accent fill rather than the page. */
summary.cardsum.turnbar{padding-left:24px}
summary.cardsum.turnbar::before{left:9px;top:6px;color:rgba(255,255,255,.85)}
summary.cardsum.turnbar:hover::before{color:#fff}
/* Closed turns stack tightly — that compact list is the point of the view. An
   open one gets air and a rule under it so its contents read as belonging to it. */
details.turn:not([open])>summary.turnbar{margin:6px 0}
details.turn[open]{border-bottom:1px solid var(--line);padding-bottom:14px;margin-bottom:6px}
.warnbar{background:color-mix(in srgb,var(--warn) 18%,transparent);border:1px solid var(--warn);border-radius:6px;padding:5px 9px;margin-bottom:10px;font-size:12px}
.tracebtn{cursor:pointer;border:1px solid var(--accent);color:var(--accent);border-radius:9px;padding:1px 7px;font-size:11px;white-space:nowrap}
.tracebtn:hover{background:var(--accent);color:#fff}
.pill.act{border-color:var(--accent);color:var(--accent);font-weight:600}
nav{display:flex;gap:6px}
nav a.pill{text-decoration:none;color:var(--dim);margin:0;font-size:13px;padding:5px 14px;border-radius:8px}
nav a.pill:hover{border-color:var(--accent);color:var(--accent)}
/* Card headers double as fold handles. The global details>summary rule above
   paints summaries accent-blue at 12px, which would flatten every card title,
   so these opt out of it and keep the styling their own head class gives them. */
summary.cardsum{cursor:pointer;list-style:none;position:relative;padding-left:17px;
  color:inherit;font-size:inherit}
summary.cardsum::-webkit-details-marker{display:none}
/* Same triangle the browser draws on a plain <details>, rotated the same way.
   The first version used ▾/▸ — the "small triangle" glyphs — at 10px, which is
   a speck next to the native marker sitting a few lines below it. ▶ is drawn at
   full size, and rotating one glyph beats swapping two: nothing shifts by a
   pixel when a card opens. */
summary.cardsum::before{content:"▶";position:absolute;left:1px;top:.15em;font-size:11px;
  line-height:1.2;color:var(--dim);transform-origin:45% 55%;transition:transform .12s ease}
details[open]>summary.cardsum::before{transform:rotate(90deg)}
summary.cardsum:hover::before{color:var(--accent)}
details.ctx>summary.cardsum,details.call>summary.cardsum{margin-bottom:2px}
details.ctxmsg[open]>summary.cardsum{margin-bottom:3px}
details.outsec>summary.cardsum{margin:8px 0 3px}
.keypath{font-family:ui-monospace,Menlo,monospace;font-size:10px;color:var(--warn);opacity:.85;margin:1px 0 2px}
.jk{color:#79b8ff}.js{color:#e2a06a}
@media(prefers-color-scheme:light){.jk{color:#0550ae}.js{color:#a15c00}}
.rolechip{display:inline-block;font-size:10px;background:var(--card);border:1px solid var(--line);border-radius:9px;padding:0 6px;margin-bottom:2px;font-family:ui-monospace,Menlo,monospace}
.call{border:1px solid var(--line);border-left:4px solid var(--accent);border-radius:8px;padding:10px;margin:0 0 16px}
.pill{display:inline-block;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:1px 7px;font-size:11px;margin-right:4px}
.newpill{background:var(--warn);color:#1a1a1a;border-color:var(--warn);font-weight:700}
#fresh{font-variant-numeric:tabular-nums}
</style>
<header>
  <h1>OpenClaw Traces</h1>
  <nav><a class="pill act" href="/">Traces</a><a class=pill href="/health">Reliability</a></nav>
  <select id=agent></select>
  <label class=stat><input type=checkbox id=failed> failures only</label>
  <input id=q placeholder="search text / tool / id" size=24>
  <label class=stat>group <select id=grp>
    <option value=session selected>by session</option><option value=run>by run</option>
  </select></label>
  <label class=stat>view <select id=mode>
    <option value=readable selected>readable</option><option value=raw>raw JSON</option>
  </select></label>
  <label class=stat>show <select id=lim>
    <option value=500>500</option><option value=2000 selected>2,000</option>
    <option value=10000>10,000</option><option value=0>all</option>
  </select> chars</label>
  <button id=togdetail title="collapse the trace pane so the list gets the full width (\ toggles)">hide trace</button>
  <button id=foldall title="collapse every card in the open trace">fold all</button>
  <button id=refresh title="check for new runs right now">refresh</button>
  <button id=newpill class=newpill hidden>▲ new runs — click to load</button>
  <span class=stat id=stats></span>
  <span class=stat id=fresh></span>
</header>
<main>
  <div id=list></div>
  <div id=detail><div class=empty>Select a run</div></div>
</main>
<script>
const $=s=>document.querySelector(s);

// Collapse the trace pane. Persisted, because whoever wants the wide list
// usually wants it on the next page load too.
function setDetail(hidden){
  document.querySelector("main").classList.toggle("nodetail",hidden);
  $("#togdetail").textContent=hidden?"show trace":"hide trace";
  try{localStorage.setItem("hideDetail",hidden?"1":"")}catch(e){}
}
function detailHidden(){return document.querySelector("main").classList.contains("nodetail")}
const esc=s=>(s??"").toString().replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const dur=ms=>ms==null?"—":ms<1000?ms+"ms":ms<60000?(ms/1000).toFixed(1)+"s":Math.floor(ms/60000)+"m"+Math.round(ms%60000/1000)+"s";
const when=t=>t?t.replace("T"," ").replace(/\..*/,""):"—";
let FULL=[], LIMIT=2000, MODE="readable";
// The agent filter is held here rather than read off the <select>, because the
// dropdown's options only exist after the first response — a restored filter has
// to be applied to that first request, before there is an option to select.
let AGENT="";

// What is open in the trace pane, kept as ids rather than as an element. The
// list is rebuilt from scratch on every refresh, so an element reference goes
// stale and the highlight vanishes while the trace beside it is still open.
// One of these two is set, never both.
let SELID=null, SELSESS=null;

// ---------- remembering where you were ----------
// Traces and Reliability are separate URLs, so switching between them is a full
// page load: filters, the open trace and the scroll position would all come back
// empty. Everything needed to restore the view is small, so it is written on
// every change and re-applied on the next load. Per-browser by design — this is
// a convenience, not data, and localStorage can legitimately come back empty.
const STATE="traceState";
function saveState(){
  try{localStorage.setItem(STATE,JSON.stringify({
    agent:AGENT, failed:$("#failed").checked, q:$("#q").value,
    grp:$("#grp").value, mode:MODE, lim:$("#lim").value,
    selid:SELID, selsess:SELSESS, foldpref:FOLDPREF,
    scroll:$("#list").scrollTop, dscroll:$("#detail").scrollTop,
    folds:FOLDS}))}catch(e){}
}
function readState(){
  try{return JSON.parse(localStorage.getItem(STATE)||"{}")||{}}catch(e){return{}}
}
const S0=readState();
// How a trace you have never opened before should arrive. The per-run fold bits
// further down only fit the trace they were taken from; this is the standing
// preference behind them, and it survives every navigation, because "closed by
// default" is a way of working, not a property of one run. Closed is the default.
let FOLDPREF=S0.foldpref===0?0:1;

// Re-apply the highlight to whatever row now represents the open trace. Safe to
// call when nothing matches — a run can be open that the current filter or limit
// leaves out of the list entirely.
function markSel(){
  const l=$("#list");
  l.querySelectorAll("tr.sel").forEach(t=>t.classList.remove("sel"));
  const row=SELID?l.querySelector(`tr[data-id="${CSS.escape(SELID)}"]`)
          :SELSESS?l.querySelector(`tr.sessrow[data-sess="${CSS.escape(SELSESS)}"]`):null;
  if(row)row.classList.add("sel");
}

// Long values are truncated for readability only — never dropped. The full
// text stays in memory and "show all" reveals it, so nothing here is hidden.
function cut(t,fmt){
  t=(t??"").toString();
  const paint=x=>fmt?fmt(x):esc(x);
  if(!LIMIT||t.length<=LIMIT)return `<pre>${paint(t)}</pre>`;
  const i=FULL.push({t,fmt})-1;
  return `<pre data-slot="${i}">${paint(t.slice(0,LIMIT))}</pre>`+
    `<button class=more data-full="${i}">show all ${t.length.toLocaleString()} chars `+
    `<span class=dim>(+${(t.length-LIMIT).toLocaleString()} hidden)</span></button>`;
}
document.addEventListener("click",e=>{
  const b=e.target.closest("button.more"); if(!b)return;
  const i=b.dataset.full, pre=b.parentNode.querySelector(`pre[data-slot="${i}"]`);
  const f=FULL[i];
  if(pre){pre.innerHTML=f.fmt?f.fmt(f.t):esc(f.t);pre.classList.add("expanded")}
  b.remove();
});

// Minimal JSON colouring: keys one colour, string values another — enough to
// see which key holds what, which is the whole point of the raw view.
function hl(src){
  return esc(src).replace(/"(?:[^"\\]|\\.)*"(\s*:)?/g,(m,colon)=>
    colon?`<span class=jk>${m.slice(0,m.length-colon.length)}</span>${colon}`
         :`<span class=js>${m}</span>`);
}
const J=o=>JSON.stringify(o,null,2);

async function load(){
  const p=new URLSearchParams();
  if(AGENT)p.set("agent",AGENT);
  if($("#failed").checked)p.set("failed","1");
  if($("#q").value.trim())p.set("q",$("#q").value.trim());
  p.set("group",$("#grp").value);
  const d=await (await fetch("/api/runs?"+p)).json();
  if(!$("#agent").dataset.done){
    $("#agent").innerHTML='<option value="">all agents</option>'+d.agents.map(a=>`<option>${a}</option>`).join("");
    $("#agent").dataset.done=1;
    $("#agent").value=AGENT;   // the restored filter finally has an option to point at
  }
  $("#stats").innerHTML=(d.redact?`<span class=redbadge>REDACTED — placeholder content</span> `:"")
    +esc(`${d.total} runs in ${d.sessions} sessions · ${d.failed} failed · $${d.cost} total · showing ${d.rows.length}`);

  const head=d.grouped
    ? `<tr><th></th><th>last activity</th><th>agent</th><th>session</th><th>first message</th><th>turns</th><th>total</th><th>tools</th><th>cost</th></tr>`
    : `<tr><th>when</th><th>agent</th><th>session</th><th>message</th><th>took</th><th>tools</th><th>cost</th></tr>`;

  const runRow=(r,child)=>`<tr data-id="${r.run_id}" class="${child?'child':''}">
      ${child?`<td class=dim></td><td class="mono dim">${when(r.started_ts).slice(11)}</td><td class=dim>turn ${r.turn_n}</td><td></td>`
             :`<td class="mono dim">${when(r.started_ts)}</td>
               <td>${esc(r.agent)}${r.ok
                 ?(r.error_text?` <span class=warnchip title="${esc(r.error_text)}">↻ recovered</span>`:"")
                 :` <span class=bad>✕ ${esc(r.failure_kind||"failed")}</span>`}</td>
               <td class="mono sess" data-sess="${r.session_id}">${r.session_id.slice(0,8)}${r.turn_tot>1?`<br><span class=dim>turn ${r.turn_n}/${r.turn_tot}</span>`:""}</td>`}
      <td class=msg title="${esc(r.user_text)}">${child&&!r.ok?`<span class=bad>✕ ${esc(r.failure_kind||"failed")}</span> `:""}${esc(r.user_text)||'<span class=dim>—</span>'}</td>
      <td class=mono>${dur(r.duration_ms)}</td>
      <td class=mono>${r.tool_count||0}${r.error_tool_count?` <span class=bad>${r.error_tool_count}✕</span>`:""}</td>
      <td class=mono>${r.cost_usd?("$"+r.cost_usd.toFixed(4)):"—"}</td></tr>`;

  const sessRow=g=>`<tr class=sessrow data-sess="${g.session_id}">
      <td class=twist>${g.turns>1?"▶":""}</td>
      <td class="mono dim">${when(g.last_ts)}</td>
      <td>${esc(g.agent)}${g.failed?` <span class=bad>✕${g.failed>1?" "+g.failed:""}</span>`:""}</td>
      <td class="mono dim">${g.session_id.slice(0,8)}<br><span class=dim>${esc(g.trigger||"")}</span></td>
      <td class=msg title="${esc(g.user_text)}">${esc(g.user_text)||'<span class=dim>—</span>'}</td>
      <td class=mono><span class=tracebtn data-sess="${g.session_id}" title="open the whole session as one trace">▶ ${g.turns}</span></td>
      <td class=mono>${dur(g.duration_ms)}</td>
      <td class=mono>${g.tool_count||0}</td>
      <td class=mono>${g.cost_usd?("$"+g.cost_usd.toFixed(4)):"—"}</td></tr>`;

  const bodyRows=d.grouped
    ? d.rows.map(g=>g.turns===1?sessRow(g).replace("<tr class=sessrow","<tr data-id=\""+g.runs[0].run_id+"\" class=\"sessrow single\"")
                              :sessRow(g)+g.runs.map(r=>runRow(r,true)).join("")).join("")
    : d.rows.map(r=>runRow(r,false)).join("");

  $("#list").innerHTML=`<table><thead>${head}</thead><tbody>${bodyRows}</tbody></table>`;
  $("#list").querySelectorAll("tr[data-id]").forEach(tr=>tr.onclick=()=>open(tr));
  $("#list").querySelectorAll("tr.sessrow:not(.single)").forEach(tr=>tr.onclick=()=>{
    const on=tr.classList.toggle("open");
    tr.querySelector(".twist").textContent=on?"▼":"▶";
    let n=tr.nextElementSibling;
    while(n&&n.classList.contains("child")){n.style.display=on?"":"none";n=n.nextElementSibling}
  });
  $("#list").querySelectorAll("tr.child").forEach(tr=>tr.style.display="none");
  $("#list").querySelectorAll(".tracebtn").forEach(el=>el.onclick=e=>{
    e.stopPropagation();
    openSession(el.dataset.sess);
  });
  $("#list").querySelectorAll("td.sess").forEach(td=>td.onclick=e=>{
    e.stopPropagation(); $("#q").value=td.dataset.sess; $("#grp").value="run"; load();
  });
  markSel();   // the table was just rebuilt; put the highlight back
}

// ---------------------------------------------------------------- rendering
const off=x=>x.offset==null?"":(x.offset<0?`−${(-x.offset/1000).toFixed(1)}s before`:`+${(x.offset/1000).toFixed(1)}s`);
const money=u=>{const c=u&&u.cost;if(!c)return"";const t=typeof c==="object"?(c.total??Object.values(c).reduce((a,b)=>a+(+b||0),0)):c;return t?` · $${(+t).toFixed(6)}`:""};
const tok=u=>u?`${u.input??0} in / ${u.output??0} out${u.cacheRead?` / ${u.cacheRead} cached`:""}${money(u)}`:"";
const size=a=>JSON.stringify(a.map(m=>m.raw)).length;
const mlabel=m=>m.role==="toolResult"?`toolResult · ${m.toolName||"?"}${m.isError?" (error)":""}`:m.role;
const mchars=m=>JSON.stringify(m.raw).length;

function msgBody(m){
  if(MODE==="raw")return cut(J(m.raw),hl);
  return (m.blocks||[]).map(x=>
    `<div class=keypath>${esc(x.path)}</div>`+
    cut(x.kind==="toolCall"?`→ ${x.name}(${x.args})`:x.text)).join("")
    ||`<div class="dim sub">(no content)</div>`;
}

// The full conversation as it stands going into a call — every message, with
// the ones added since the previous call marked and open by default.
function contextBlock(M,SP,c,prev,n,final,fold){
  const ctx=M.slice(0,c.i), from=prev==null?0:prev.i;
  const added=ctx.length-from;
  // The counts live inside the summary so a folded card still says what it holds.
  return `<details class=ctx${fold?"":" open"}>
    <summary class=cardsum>
      <div class="ctxhead${final?' fin':''}">${final?`FINAL CONTEXT · after model call #${n}`:`CONTEXT → MODEL CALL #${n}`}</div>
      <div class=ctxsum>${ctx.length} message${ctx.length===1?"":"s"} · ${size(ctx).toLocaleString()} chars
        · system prompt ${SP.length.toLocaleString()} chars
        ${added>0?`· <span class=grow>${added} new since ${prev==null?"the run started":"model call #"+(final?n:n-1)}</span>`:""}</div>
    </summary>
    <details class=spwrap><summary>system prompt (${SP.length.toLocaleString()} chars)</summary>${cut(SP)}</details>
    ${ctx.map((m,i)=>{
      const isNew=i>=from;
      // Messages carried over from an earlier call start folded; the ones added
      // since start open. Either way the handle is the same.
      return `<details class="ctxmsg${isNew?" new":""}"${isNew&&!fold?" open":""}>
        <summary class=cardsum><div class=ctxmsghead><span class=rolechip>#${i+1} ${esc(mlabel(m))}</span>
          ${isNew?'<span class=newbadge>new</span>':''}
          <span class=dim>${mchars(m).toLocaleString()} chars</span></div></summary>
        ${msgBody(m)}
      </details>`;
    }).join("")||'<div class=dim>(empty context)</div>'}
    <details><summary>whole context as one JSON array</summary>${cut(J(ctx.map(m=>m.raw)),hl)}</details>
  </details>`;
}

function callBlock(M,C,c,n,fold){
  const m=M[c.i], ci=C.indexOf(c), nextI=ci+1<C.length?C[ci+1].i:M.length;
  const results=M.slice(c.i+1,nextI).filter(x=>x.role==="toolResult");
  const out = MODE==="raw"
    ? `<div class=sub><div class=keypath>the assistant message, verbatim</div>${cut(J(m.raw),hl)}</div>`
    : ((m.blocks||[]).map(b=>b.kind==="toolCall"
        ? `<div class=sub><div class=lbl><b>→ ${esc(b.name)}</b><span class=dim>tool call</span></div><div class=keypath>${esc(b.path)}</div>${cut(b.args)}</div>`
        : `<div class=sub><div class=lbl><b>${b.kind}</b></div><div class=keypath>${esc(b.path)}</div>${cut(b.text)}</div>`).join("")
      ||`<div class="dim sub">(no content)</div>`);
  return `<details class="call ${c.prior?'prior':''}"${fold?"":" open"}>
    <summary class=cardsum><div class=callhead>Model call #${n} <span class=dim>${off(c)}</span>
      ${m.stopReason?`<span class=pill>${esc(m.stopReason)}</span>`:''}
      ${m.usage?`<span class="pill mono">${esc(tok(m.usage))}</span>`:''}
      ${results.length?`<span class=dim>${results.length} tool result${results.length>1?"s":""}</span>`:''}</div></summary>
    <details class=outsec${fold?"":" open"}><summary class=cardsum><span class=outlbl>model output</span></summary>${out}</details>
    ${results.map(t=>`<details class="sub tool ${t.isError?'err':''}"${fold?"":" open"}>
      <summary class=cardsum><span class=lbl><b>${esc(t.toolName)}</b><span class=dim>${off(t)}</span>${t.isError?'<span class=bad>error</span>':''}</span></summary>
      ${MODE==="raw"?cut(J(t.raw),hl):`
      ${t.args?`<details><summary>arguments</summary>${cut(t.args)}</details>`:""}
      <details><summary>result (${(t.blocks||[]).reduce((n,b)=>n+(b.text||"").length,0).toLocaleString()} chars)</summary>${cut((t.blocks||[]).map(b=>b.text).join("\n"))}</details>`}
    </details>`).join("")}
  </details>`;
}

function renderRun(r,d,opts){
  const M=d.messages||[], C=d.calls||[], SP=d.systemPrompt||"";
  const fold=(opts||{}).folded;
  const own=C.filter(c=>!c.prior), pri=C.filter(c=>c.prior);
  const body=own.map((c,i)=>{
    const prev=i===0?(pri.length?pri[pri.length-1]:null):own[i-1];
    return contextBlock(M,SP,c,prev,i+1,false,fold)+callBlock(M,C,c,i+1,fold);
  }).join("")+(own.length
    ? contextBlock(M,SP,{i:M.length},own[own.length-1],own.length,true,fold)
    : "");
  const priorBody=(pri.length&&!(opts||{}).hidePrior)
    ? `<details class=priorwrap><summary>${pri.length} model call${pri.length>1?"s":""} inherited from earlier turns in this session</summary>${pri.map((c,i)=>callBlock(M,C,c,"P"+(i+1),fold)).join("")}</details>`:"";
  const users=M.filter(m=>m.role==="user"&&!m.prior);
  // Same fact, two very different stories. A run that hit this and stopped is a
  // failure; one that hit it, was restarted by OpenClaw and finished is a run
  // that worked — and saying so is the difference between a useful dashboard and
  // one that cries wolf.
  const err=r.error_text
    ? (r.ok===0
        ? `<div class=errbar><b>${esc(r.failure_kind||"failed")}</b><div class=msg>${esc(r.error_text)}</div></div>`
        : `<div class="errbar recov"><b>recovered${r.attempts>1?` · OpenClaw restarted this run (${r.attempts} attempts)`:""}</b><div class=msg>${esc(r.error_text)}</div></div>`)
    : "";
  return `${err}${users.map(u=>`<div class=usermsg><div class=lbl><b>user message</b><span class=dim>${off(u)}</span></div>${cut((u.blocks||[]).map(b=>b.text).join("\n"))}</div>`).join("")}
    <div class="mono dim" style="margin:6px 0">${own.length} model call${own.length===1?"":"s"}${pri.length?` · ${pri.length} inherited`:""} · context ${size(M).toLocaleString()} chars by the end</div>
    ${priorBody}${body||`<div class=empty>${r.error_text
      ? "the run ended before any model call — the reason is above"
      : "no model call recorded, and the run left no reason behind"}</div>`}`;
}

const hdr=(r,extra)=>`<div class=lbl style="margin-bottom:8px;flex-wrap:wrap">
    <span class=pill>${esc(r.agent)}</span><span class=pill>${esc(r.model||"")}</span>
    <span class=pill>${dur(r.duration_ms)}</span><span class=pill>$${(r.cost_usd||0).toFixed(4)}</span>
    ${extra||""}
    ${r.failed?`<span class="pill bad">${r.failed} failed turn${r.failed>1?"s":""}</span>`:""}
    ${r.ok===0?`<span class="pill bad">${esc(r.failure_kind||"failed")}</span>`:""}
  </div>`;

async function open(tr){
  if(detailHidden())setDetail(false);
  FULL=[];
  $("#detail").innerHTML="<div class=empty>loading…</div>";
  $("#detail").scrollTop=0;
  SELID=tr.dataset.id; SELSESS=null; markSel(); saveState();
  const r=await (await fetch("/api/run/"+encodeURIComponent(tr.dataset.id))).json();
  const d=r.detail||{};
  if(d.error){$("#detail").innerHTML=`<div class=empty>${esc(d.error)}</div>`;return}
  $("#detail").innerHTML=
    hdr(r,`<span class=pill>${(r.total_tokens||0).toLocaleString()} tok</span>
      <span class="pill act" data-sess="${r.session_id}">▶ full session trace (${r.turn_tot||1} turns)</span>`)+
    `<div class="mono dim" style="margin-bottom:10px">${esc(r.session_key||"")}<br>run ${esc(r.run_id)}</div>`+
    `<details><summary>${(d.tools||[]).length} tools available to the model</summary>${cut((d.tools||[]).join("\n"))}</details>`+
    renderRun(r,d,{folded:!!FOLDPREF});
  wireActions(); applyFolds();
}

async function openSession(sid){
  if(detailHidden())setDetail(false);
  FULL=[];
  $("#detail").innerHTML="<div class=empty>loading whole session…</div>";
  $("#detail").scrollTop=0;
  SELSESS=sid; SELID=null; markSel(); saveState();
  const s=await (await fetch("/api/session/"+encodeURIComponent(sid))).json();
  if(s.error){$("#detail").innerHTML=`<div class=empty>${esc(s.error)}</div>`;return}
  // A session is the view you open to find one turn, not to read all of them.
  // Every turn starts closed, so what you land on is a short stack of turn bars
  // you can click — and what is inside a turn you open starts closed too, or the
  // scrolling problem just moves one level down.
  const turns=s.runs.map((r,i)=>{
    const calls=((r.detail||{}).calls||[]).filter(c=>!c.prior).length;
    return `<details class=turn>
      <summary class="cardsum turnbar">TURN ${i+1} of ${s.turns}
        <span class=dim>${when(r.started_ts)} · ${dur(r.duration_ms)} · $${(r.cost_usd||0).toFixed(4)}
          · ${calls} model call${calls===1?"":"s"}${r.tool_count?` · ${r.tool_count} tool${r.tool_count===1?"":"s"}`:""}</span>
        ${r.ok
          ?(r.error_text?`<span class=recovchip title="${esc(r.error_text)}">↻ recovered</span>`:"")
          :`<span class=bad>✕ ${esc(r.failure_kind||"failed")}</span>`}</summary>
      ${renderRun(r,r.detail||{},{hidePrior:true,folded:!!FOLDPREF})}
    </details>`;
  }).join("");
  $("#detail").innerHTML=
    hdr(s,`<span class=pill>${s.turns} turns</span><span class=pill>${s.tool_count} tool calls</span>`)+
    `<div class="mono dim" style="margin-bottom:10px">${esc(s.session_key||"")}<br>session ${esc(s.session_id)}</div>`+
    (s.truncated?`<div class=warnbar>showing the first 25 of ${s.turns} turns</div>`:"")+
    turns;
  wireActions(); applyFolds();
}

function wireActions(){
  $("#detail").querySelectorAll(".act[data-sess]").forEach(el=>{
    el.style.cursor="pointer"; el.onclick=()=>openSession(el.dataset.sess);
  });
}

$("#agent").onchange=()=>{AGENT=$("#agent").value;saveState();load()};
$("#failed").onchange=()=>{saveState();load()};
// Redraw whatever is open, run or whole session, under the new setting.
function reopen(){
  if(SELID)open(stubRow(SELID));
  else if(SELSESS)openSession(SELSESS);
}
$("#lim").onchange=()=>{LIMIT=+$("#lim").value;saveState();reopen()};
$("#mode").onchange=()=>{MODE=$("#mode").value;saveState();reopen()};
$("#grp").onchange=()=>{saveState();load()};
let t;$("#q").oninput=()=>{clearTimeout(t);t=setTimeout(()=>{saveState();load()},250)};
// Where you had scrolled to, in both panes, debounced so a flick of the wheel
// is not a hundred writes. A long session trace is the one you are most likely
// to be deep inside when you click away to Reliability.
let st;const onscroll=()=>{clearTimeout(st);st=setTimeout(saveState,300)};
$("#list").addEventListener("scroll",onscroll,{passive:true});
$("#detail").addEventListener("scroll",onscroll,{passive:true});

$("#togdetail").onclick=()=>setDetail(!detailHidden());
// Backslash toggles it, as long as you are not typing in the search box.
document.addEventListener("keydown",e=>{
  if(e.key!=="\\"||e.metaKey||e.ctrlKey||e.altKey)return;
  const t=e.target.tagName;
  if(t==="INPUT"||t==="SELECT"||t==="TEXTAREA")return;
  e.preventDefault(); setDetail(!detailHidden());
});
// Every card is its own <details>, so one button has to mean something sensible
// for a mixed state: if anything is open, close everything; otherwise open it.
const CARDS="details.turn,details.ctx,details.call,details.ctxmsg,details.outsec,details.sub";
// Two different sets. "fold all" acts on the cards, because folding the little
// "arguments"/"result" disclosures with them would be noise. Remembering, on the
// other hand, covers every disclosure in the pane — opening the system prompt is
// a choice too, and losing it on a page switch is the thing being fixed.
const foldCards=()=>[...$("#detail").querySelectorAll(CARDS)];
const allCards=()=>[...$("#detail").querySelectorAll("details")];

// Which cards you left open is part of where you were, so it is remembered with
// the rest. Cards are identified by their position in the rendered trace rather
// than by an id: the render is deterministic for a given trace at a given mode
// and limit, so position is stable, and all three go into the key. Anything that
// does not match — a different trace, a re-indexed run that now renders more
// cards — falls back to the defaults instead of opening the wrong ones.
let FOLDS=S0.folds||null;
const foldKey=()=>`${SELID?"r:"+SELID:SELSESS?"s:"+SELSESS:"-"}|${MODE}|${LIMIT}`;
function foldSnap(){
  const els=allCards();
  // Called while the pane is showing "loading…" too; there is nothing to read
  // then, and the previous snapshot is the honest answer.
  return els.length?{key:foldKey(),bits:els.map(e=>e.open?"1":"0").join("")}:FOLDS;
}
function applyFolds(){
  const els=allCards();
  if(FOLDS&&FOLDS.key===foldKey()&&FOLDS.bits.length===els.length)
    els.forEach((e,i)=>e.open=FOLDS.bits[i]==="1");
  FOLDS=foldSnap(); saveState();
  syncFoldBtn();
}
function syncFoldBtn(){
  const els=foldCards();
  if(!els.length)return;   // nothing open in the pane; leave the label alone
  $("#foldall").textContent=els.some(x=>x.open)?"fold all":"unfold all";
}
// toggle does not bubble, so it is caught on the way down.
let ft;$("#detail").addEventListener("toggle",()=>{
  clearTimeout(ft);
  ft=setTimeout(()=>{FOLDS=foldSnap();saveState();syncFoldBtn()},200);
},true);

$("#foldall").onclick=()=>{
  const els=foldCards();
  if(!els.length)return;
  const anyOpen=els.some(x=>x.open);
  els.forEach(x=>x.open=!anyOpen);
  // Pressing this is the clearest statement the page ever gets about how much it
  // should be showing, so it sets the standing preference, not just this trace.
  FOLDPREF=anyOpen?1:0;
  FOLDS=foldSnap(); saveState(); syncFoldBtn();
};

// ---------- staying up to date ----------
// The server re-checks the trajectory files on a timer and gives its data a new
// version label whenever one of them moved. We hold onto the last label we saw
// and ask for it every few seconds; 60 bytes, no work, until it differs.
//
// Differs, not "is bigger": the label lives in the server's memory, so a restart
// sends it back to 0. Comparing for "bigger" would leave the page stale forever.
let DV=null;

async function reloadKeepingPlace(){
  const el=$("#list"), top=el.scrollTop;
  await load();
  el.scrollTop=top;              // never move what someone is reading
}

function showFresh(v){
  if(!v.checked){$("#fresh").textContent="";return}
  $("#fresh").textContent=v.error?("refresh failing: "+v.error)
    :(v.running?"checking…":"checked "+v.checked+" UTC");
}

async function checkVersion(){
  let v;
  try{v=await (await fetch("/api/version")).json()}catch(e){return}
  showFresh(v);
  if(DV===null){DV=v.v;return}
  if(v.v===DV)return;
  DV=v.v;
  // A trace is open — someone is reading. Offer the update, don't impose it.
  // A whole-session trace counts: it used to fall through to a silent reload,
  // which rebuilt the list and dropped the highlight out from under the reader.
  if(SELID||SELSESS){$("#newpill").hidden=false}
  else await reloadKeepingPlace();
}

$("#newpill").onclick=async()=>{$("#newpill").hidden=true;await reloadKeepingPlace()};

$("#refresh").onclick=async()=>{
  const b=$("#refresh"); b.disabled=true; b.textContent="checking…";
  try{
    const v=await (await fetch("/api/reindex",{method:"POST"})).json();
    DV=v.v; showFresh(v); $("#newpill").hidden=true;
    await reloadKeepingPlace();
  }catch(e){}
  b.disabled=false; b.textContent="refresh";
};

setInterval(checkVersion,5000);
checkVersion();

// ---------- arriving from somewhere else ----------
// /health links to #run=<id>. That run is usually not in the list on screen —
// it may be older than the limit, or filtered out — so we don't hunt for a row
// to click. open() only ever needs an id and something to mark selected, and a
// stub supplies both.
const stubRow=id=>({dataset:{id},classList:{add(){},remove(){}}});
async function openHash(){
  const m=/^#run=(.+)$/.exec(location.hash);
  if(!m)return false;
  await open(stubRow(decodeURIComponent(m[1])));
  return true;
}
addEventListener("hashchange",openHash);

try{if(localStorage.getItem("hideDetail"))setDetail(true)}catch(e){}

// Put the controls back before the first request, so it asks the server for the
// rows you were looking at instead of fetching the default set and refetching.
// The open trace and the scroll position need the list to exist, so they follow.
if(S0.agent)AGENT=S0.agent;
if(S0.q)$("#q").value=S0.q;
if(S0.failed)$("#failed").checked=true;
if(S0.grp)$("#grp").value=S0.grp;
if(S0.mode){MODE=S0.mode;$("#mode").value=S0.mode}
if(S0.lim!=null){LIMIT=+S0.lim;$("#lim").value=S0.lim}

(async()=>{
  await load();
  // An explicit #run= link is someone asking for that run by name, so it wins
  // over whatever happened to be open here last.
  if(!await openHash()){
    if(S0.selid)await open(stubRow(S0.selid));
    else if(S0.selsess)await openSession(S0.selsess);
  }
  if(S0.scroll)$("#list").scrollTop=S0.scroll;
  if(S0.dscroll)$("#detail").scrollTop=S0.dscroll;
  saveState();
})();
</script>"""


# The colour tokens and base rules are repeated here rather than shared. The
# alternative is a /style.css route, which would mean the trace page can render
# unstyled if one request fails — not worth it for six lines.
HEALTH_PAGE = r"""<!doctype html><meta charset=utf-8><title>OpenClaw Reliability</title>
<style>
:root{--bg:#fff;--fg:#111;--dim:#666;--line:#e3e3e3;--card:#fafafa;--accent:#2b6cb0;--bad:#c53030;--warn:#b7791f;--ok:#2f7d51}
@media(prefers-color-scheme:dark){:root{--bg:#15171a;--fg:#e8e8e8;--dim:#9aa0a6;--line:#2c3036;--card:#1c1f23;--accent:#7aa7d9;--bad:#f28b82;--warn:#e0b95d;--ok:#7ec699}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:13px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
header{padding:10px 16px;border-bottom:1px solid var(--line);display:flex;gap:12px;align-items:center;flex-wrap:wrap;position:sticky;top:0;background:var(--bg);z-index:5}
h1{font-size:14px;margin:0;font-weight:600}
h2{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--dim);margin:26px 0 6px;font-weight:600}
.stat{color:var(--dim);font-size:12px}
.pill{display:inline-block;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:1px 7px;font-size:11px}
.pill.act{border-color:var(--accent);color:var(--accent);font-weight:600}
nav{display:flex;gap:6px}
nav a.pill{text-decoration:none;color:var(--dim);font-size:13px;padding:5px 14px;border-radius:8px}
nav a.pill:hover{border-color:var(--accent);color:var(--accent)}
main{padding:16px 16px 60px;max-width:1180px}
.cards{display:flex;gap:10px;flex-wrap:wrap}
.card{border:1px solid var(--line);border-radius:10px;padding:11px 15px;background:var(--card);min-width:132px}
.big{font-size:25px;font-weight:600;font-variant-numeric:tabular-nums;line-height:1.15}
.cap{font-size:10px;text-transform:uppercase;letter-spacing:.07em;color:var(--dim)}
.sub{font-size:11px;color:var(--dim)}
.note{color:var(--dim);font-size:12px;margin:4px 0 0;max-width:78ch}
table{width:100%;border-collapse:collapse}
th{text-align:left;font-weight:600;font-size:11px;color:var(--dim);padding:6px 8px;border-bottom:1px solid var(--line);text-transform:uppercase;letter-spacing:.04em}
td{padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11px}
.bad{color:var(--bad);font-weight:600}.dim{color:var(--dim)}.ok{color:var(--ok);font-weight:600}
.num{text-align:right;font-variant-numeric:tabular-nums}
.bar{display:inline-block;height:7px;border-radius:4px;background:var(--bad);vertical-align:middle;min-width:2px}
.barwrap{width:150px}
tr.run{cursor:pointer}tr.run:hover td{background:var(--card)}
.msg{color:var(--dim);max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
/* The run's own words under the short label, so the table can be scanned by
   category and still read for detail without opening anything. */
.why{color:var(--dim);font-weight:400;font-size:11px;font-family:ui-monospace,Menlo,monospace;
  max-width:320px;white-space:normal;margin-top:2px}
.empty{color:var(--dim);padding:40px;text-align:center}
.offline{background:var(--bad);color:#fff;padding:6px 16px;font-size:12px;font-weight:600}
.redbadge{background:var(--warn);color:#111;font-weight:700;padding:1px 6px;border-radius:4px;font-size:10px;letter-spacing:.06em}
.two{display:flex;gap:26px;flex-wrap:wrap;align-items:flex-start}
.two>section{flex:1 1 380px;min-width:340px}
</style>
<header>
  <h1>OpenClaw Traces</h1>
  <nav><a class=pill href="/">Traces</a><a class="pill act" href="/health">Reliability</a></nav>
  <span class=stat id=stats></span>
</header>
<div id=offline class=offline hidden></div>
<main id=main><div class=empty>loading…</div></main>
<script>
const $=s=>document.querySelector(s);
const esc=s=>(s??"").toString().replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const dur=ms=>ms==null?"—":ms<1000?ms+"ms":ms<60000?(ms/1000).toFixed(1)+"s":Math.floor(ms/60000)+"m"+Math.round(ms%60000/1000)+"s";
const when=t=>t?t.replace("T"," ").replace(/\..*/,""):"—";
const pct=(a,b)=>b?(100*a/b).toFixed(1)+"%":"—";

function render(d){
  const bad=d.bad||0, tot=d.total||0;
  const worst=Math.max(1,...d.kinds.map(k=>k.n));
  const cards=`<div class=cards>
    <div class=card><div class=cap>runs indexed</div><div class=big>${tot.toLocaleString()}</div></div>
    <div class=card><div class=cap>finished ok</div><div class="big ok">${(d.ok||0).toLocaleString()}</div>
      <div class=sub>${pct(d.ok||0,tot)} of all runs</div></div>
    <div class=card><div class=cap>did not finish</div><div class="big bad">${bad.toLocaleString()}</div>
      <div class=sub>${pct(bad,tot)} of all runs</div></div>
    <div class=card><div class=cap>runs with a tool error</div><div class=big style="color:var(--warn)">${(d.tool_err_runs||0).toLocaleString()}</div>
      <div class=sub>${(d.tool_err_total||0).toLocaleString()} errors, ${pct(d.tool_err_runs||0,tot)} of runs</div></div>
  </div>
  <p class=note><b>These are two different questions.</b> "Did not finish" means the run
  ended badly — it was aborted, ran out of budget, or never completed. "Tool error" means
  something failed <i>inside</i> a run that may well have ended fine: a denied command, a
  missing file, an API refusing. A run can be in either column, both, or neither.</p>`;

  const kinds=`<section><h2>Why runs did not finish</h2><table><thead>
    <tr><th>reason</th><th class=num>runs</th><th></th></tr></thead><tbody>
    ${d.kinds.map(k=>`<tr><td>${esc(k.kind)}</td><td class="num mono">${k.n}</td>
      <td class=barwrap><span class=bar style="width:${Math.round(100*k.n/worst)}%"></span></td></tr>`).join("")
      ||`<tr><td colspan=3 class=dim>none</td></tr>`}
    </tbody></table></section>`;

  const agents=`<section><h2>Per agent</h2><table><thead>
    <tr><th>agent</th><th class=num>runs</th><th class=num>failed</th><th class=num>fail rate</th>
    <th class=num>runs w/ tool error</th></tr></thead><tbody>
    ${d.agents.map(a=>`<tr><td>${esc(a.agent)}</td>
      <td class="num mono">${a.runs}</td>
      <td class="num mono ${a.bad?"bad":"dim"}">${a.bad}</td>
      <td class="num mono ${a.bad?"bad":"dim"}">${pct(a.bad,a.runs)}</td>
      <td class="num mono ${a.tool_err?"":"dim"}" style="${a.tool_err?"color:var(--warn)":""}">${a.tool_err}</td></tr>`).join("")}
    </tbody></table></section>`;

  const runs=`<h2>Every run that did not finish${d.truncated?` <span class=dim>(newest ${d.bad_runs.length}, ${d.truncated} older not shown)</span>`:""}</h2>
    <table><thead><tr><th>when</th><th>agent</th><th>reason</th><th>trigger</th>
    <th>first message</th><th class=num>took</th><th class=num>tools</th><th class=num>tokens</th></tr></thead><tbody>
    ${d.bad_runs.map(r=>`<tr class=run data-id="${esc(r.run_id)}" title="open this run in the trace view">
      <td class="mono dim">${when(r.started_ts)}</td>
      <td>${esc(r.agent)}</td>
      <td class=bad title="${esc(r.error_text||"")}">${esc(r.label||"failed")}${
        r.error_text?`<div class=why>${esc(r.error_text)}</div>`:""}</td>
      <td class="mono dim">${esc(r.trigger||"—")}</td>
      <td class=msg title="${esc(r.user_text)}">${esc(r.user_text)||'<span class=dim>—</span>'}</td>
      <td class="num mono">${dur(r.duration_ms)}</td>
      <td class="num mono">${r.tool_count||0}${r.error_tool_count?` <span class=bad>${r.error_tool_count}✕</span>`:""}</td>
      <td class="num mono">${(r.total_tokens||0).toLocaleString()}</td></tr>`).join("")
      ||`<tr><td colspan=8 class=dim>nothing failed — every indexed run finished</td></tr>`}
    </tbody></table>`;

  $("#main").innerHTML=cards+`<div class=two>${kinds}${agents}</div>`+runs;
  $("#stats").innerHTML=(d.redact?`<span class=redbadge>REDACTED — placeholder content</span> `:"")
    +esc(`${tot.toLocaleString()} runs · ${bad} did not finish · ${d.tool_err_runs} hit a tool error`);
  // Straight into the existing trace view, which opens the run from the hash.
  $("#main").querySelectorAll("tr.run").forEach(tr=>tr.onclick=()=>{
    location.href="/#run="+encodeURIComponent(tr.dataset.id);
  });
}

// A failed fetch used to look exactly like a quiet day: nothing changed on
// screen. It says so now — a dead ssh tunnel is the usual cause.
function offline(on,msg){
  const el=$("#offline");
  el.hidden=!on;
  if(on)el.textContent="Disconnected — "+msg+". The server is not answering; check the ssh tunnel on port 8765.";
}

let DV=null;
async function load(){
  try{
    const d=await (await fetch("/api/health")).json();
    offline(false); render(d);
  }catch(e){ offline(true,e.message||"fetch failed") }
}
async function poll(){
  try{
    const v=await (await fetch("/api/version")).json();
    offline(false);
    if(DV!==null&&v.v!==DV)await load();
    DV=v.v;
  }catch(e){ offline(true,e.message||"fetch failed") }
}
setInterval(poll,5000);
load();
</script>"""

class Server(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

def free_the_port():
    """Ctrl+C over ssh often doesn't reach this process, so an old instance can
    still hold the port. Find it and stop it rather than making the user do it."""
    import re, signal, subprocess, time
    try:
        out = subprocess.run(["ss", "-tlnp"], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return False
    for line in out.splitlines():
        if f":{PORT} " not in line:
            continue
        for pid in re.findall(r"pid=(\d+)", line):
            pid = int(pid)
            if pid == os.getpid():
                continue
            print(f"stopping previous instance (pid {pid})", flush=True)
            try:
                os.kill(pid, signal.SIGTERM)
                for _ in range(20):
                    time.sleep(0.1)
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        return True
                os.kill(pid, signal.SIGKILL)
                time.sleep(0.3)
            except (ProcessLookupError, PermissionError) as ex:
                print(f"  could not stop it: {ex}")
                return False
            return True
    return False

if __name__ == "__main__":
    srv = None
    for attempt in (1, 2):
        try:
            srv = Server((HOST, PORT), H)
            break
        except OSError as ex:
            if attempt == 1 and getattr(ex, "errno", None) == 98 and free_the_port():
                continue
            raise
    print(f"trace viewer on http://{HOST}:{PORT}  (db: {DB})", flush=True)
    if indexer is None:
        print(f"auto-refresh OFF — {_import_error}", flush=True)
    elif REFRESH_SEC > 0:
        print(f"auto-refresh every {REFRESH_SEC}s (TRACE_REFRESH_SEC=0 to disable)", flush=True)
        threading.Thread(target=refresh_loop, daemon=True).start()
    else:
        print("auto-refresh off; use the refresh button", flush=True)
    print("stop with Ctrl+C", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
