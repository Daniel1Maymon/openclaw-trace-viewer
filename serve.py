#!/usr/bin/env python3
"""Read-only trace viewer over OpenClaw trajectory files. Binds to 127.0.0.1 only."""
import hashlib, json, os, random, re, sqlite3, urllib.parse
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
    con = sqlite3.connect(DB); con.row_factory = sqlite3.Row
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

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        try:
            if u.path == "/":
                return self._send(200, PAGE, "text/html; charset=utf-8")
            if u.path == "/api/runs":
                return self._send(200, json.dumps(query_runs(q)), "application/json")
            if u.path.startswith("/api/session/"):
                sid = urllib.parse.unquote(u.path[len("/api/session/"):])
                con = sqlite3.connect(DB); con.row_factory = sqlite3.Row
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
                con = sqlite3.connect(DB); con.row_factory = sqlite3.Row
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
tr[data-id]{cursor:pointer}tr[data-id]:hover td{background:var(--card)}tr.sel td{background:var(--card);box-shadow:inset 3px 0 var(--accent)}
.msg{color:var(--dim);max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
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
td.twist{width:16px;color:var(--dim);text-align:center;user-select:none}
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
.turnbar{position:sticky;top:0;z-index:2;background:var(--accent);color:#fff;font-weight:700;font-size:12px;padding:5px 10px;border-radius:6px;margin:20px 0 8px;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.turnbar .dim{color:rgba(255,255,255,.8);font-weight:400}
.warnbar{background:color-mix(in srgb,var(--warn) 18%,transparent);border:1px solid var(--warn);border-radius:6px;padding:5px 9px;margin-bottom:10px;font-size:12px}
.tracebtn{cursor:pointer;border:1px solid var(--accent);color:var(--accent);border-radius:9px;padding:1px 7px;font-size:11px;white-space:nowrap}
.tracebtn:hover{background:var(--accent);color:#fff}
.pill.act{border-color:var(--accent);color:var(--accent);font-weight:600}
.keypath{font-family:ui-monospace,Menlo,monospace;font-size:10px;color:var(--warn);opacity:.85;margin:1px 0 2px}
.jk{color:#79b8ff}.js{color:#e2a06a}
@media(prefers-color-scheme:light){.jk{color:#0550ae}.js{color:#a15c00}}
.rolechip{display:inline-block;font-size:10px;background:var(--card);border:1px solid var(--line);border-radius:9px;padding:0 6px;margin-bottom:2px;font-family:ui-monospace,Menlo,monospace}
.call{border:1px solid var(--line);border-left:4px solid var(--accent);border-radius:8px;padding:10px;margin:0 0 16px}
.pill{display:inline-block;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:1px 7px;font-size:11px;margin-right:4px}
</style>
<header>
  <h1>OpenClaw Traces</h1>
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
  <span class=stat id=stats></span>
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
let sel=null, FULL=[], LIMIT=2000, MODE="readable";

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
  if($("#agent").value)p.set("agent",$("#agent").value);
  if($("#failed").checked)p.set("failed","1");
  if($("#q").value.trim())p.set("q",$("#q").value.trim());
  p.set("group",$("#grp").value);
  const d=await (await fetch("/api/runs?"+p)).json();
  if(!$("#agent").dataset.done){
    $("#agent").innerHTML='<option value="">all agents</option>'+d.agents.map(a=>`<option>${a}</option>`).join("");
    $("#agent").dataset.done=1;
  }
  $("#stats").innerHTML=(d.redact?`<span class=redbadge>REDACTED — placeholder content</span> `:"")
    +esc(`${d.total} runs in ${d.sessions} sessions · ${d.failed} failed · $${d.cost} total · showing ${d.rows.length}`);

  const head=d.grouped
    ? `<tr><th></th><th>last activity</th><th>agent</th><th>session</th><th>first message</th><th>turns</th><th>total</th><th>tools</th><th>cost</th></tr>`
    : `<tr><th>when</th><th>agent</th><th>session</th><th>message</th><th>took</th><th>tools</th><th>cost</th></tr>`;

  const runRow=(r,child)=>`<tr data-id="${r.run_id}" class="${child?'child':''}">
      ${child?`<td class=dim></td><td class="mono dim">${when(r.started_ts).slice(11)}</td><td class=dim>turn ${r.turn_n}</td><td></td>`
             :`<td class="mono dim">${when(r.started_ts)}</td>
               <td>${esc(r.agent)}${r.ok?"":` <span class=bad>✕ ${esc(r.failure_kind||"failed")}</span>`}</td>
               <td class="mono sess" data-sess="${r.session_id}">${r.session_id.slice(0,8)}${r.turn_tot>1?`<br><span class=dim>turn ${r.turn_n}/${r.turn_tot}</span>`:""}</td>`}
      <td class=msg title="${esc(r.user_text)}">${child&&!r.ok?`<span class=bad>✕ ${esc(r.failure_kind||"failed")}</span> `:""}${esc(r.user_text)||'<span class=dim>—</span>'}</td>
      <td class=mono>${dur(r.duration_ms)}</td>
      <td class=mono>${r.tool_count||0}${r.error_tool_count?` <span class=bad>${r.error_tool_count}✕</span>`:""}</td>
      <td class=mono>${r.cost_usd?("$"+r.cost_usd.toFixed(4)):"—"}</td></tr>`;

  const sessRow=g=>`<tr class=sessrow data-sess="${g.session_id}">
      <td class=twist>${g.turns>1?"▸":""}</td>
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
    tr.querySelector(".twist").textContent=on?"▾":"▸";
    let n=tr.nextElementSibling;
    while(n&&n.classList.contains("child")){n.style.display=on?"":"none";n=n.nextElementSibling}
  });
  $("#list").querySelectorAll("tr.child").forEach(tr=>tr.style.display="none");
  $("#list").querySelectorAll(".tracebtn").forEach(el=>el.onclick=e=>{
    e.stopPropagation();
    $("#list").querySelectorAll("tr").forEach(t=>t.classList.remove("sel"));
    el.closest("tr").classList.add("sel"); sel=null;
    openSession(el.dataset.sess);
  });
  $("#list").querySelectorAll("td.sess").forEach(td=>td.onclick=e=>{
    e.stopPropagation(); $("#q").value=td.dataset.sess; $("#grp").value="run"; load();
  });
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
function contextBlock(M,SP,c,prev,n,final){
  const ctx=M.slice(0,c.i), from=prev==null?0:prev.i;
  const added=ctx.length-from;
  return `<div class=ctx>
    <div class="ctxhead${final?' fin':''}">${final?`FINAL CONTEXT · after Call #${n}`:`CONTEXT → Call #${n}`}</div>
    <div class=ctxsum>${ctx.length} message${ctx.length===1?"":"s"} · ${size(ctx).toLocaleString()} chars
      · system prompt ${SP.length.toLocaleString()} chars
      ${added>0?`· <span class=grow>${added} new since ${prev==null?"the run started":"Call #"+(final?n:n-1)}</span>`:""}</div>
    <details class=spwrap><summary>system prompt (${SP.length.toLocaleString()} chars)</summary>${cut(SP)}</details>
    ${ctx.map((m,i)=>{
      const isNew=i>=from;
      return `<div class="ctxmsg${isNew?" new":""}">
        <div class=ctxmsghead><span class=rolechip>#${i+1} ${esc(mlabel(m))}</span>
          ${isNew?'<span class=newbadge>new</span>':''}
          <span class=dim>${mchars(m).toLocaleString()} chars</span></div>
        ${isNew?msgBody(m):`<details><summary>show</summary>${msgBody(m)}</details>`}
      </div>`;
    }).join("")||'<div class=dim>(empty context)</div>'}
    <details><summary>whole context as one JSON array</summary>${cut(J(ctx.map(m=>m.raw)),hl)}</details>
  </div>`;
}

function callBlock(M,C,c,n){
  const m=M[c.i], ci=C.indexOf(c), nextI=ci+1<C.length?C[ci+1].i:M.length;
  const results=M.slice(c.i+1,nextI).filter(x=>x.role==="toolResult");
  const out = MODE==="raw"
    ? `<div class=sub><div class=keypath>the assistant message, verbatim</div>${cut(J(m.raw),hl)}</div>`
    : ((m.blocks||[]).map(b=>b.kind==="toolCall"
        ? `<div class=sub><div class=lbl><b>→ ${esc(b.name)}</b><span class=dim>tool call</span></div><div class=keypath>${esc(b.path)}</div>${cut(b.args)}</div>`
        : `<div class=sub><div class=lbl><b>${b.kind}</b></div><div class=keypath>${esc(b.path)}</div>${cut(b.text)}</div>`).join("")
      ||`<div class="dim sub">(no content)</div>`);
  return `<div class="call ${c.prior?'prior':''}">
    <div class=callhead>Call #${n} <span class=dim>${off(c)}</span>
      ${m.stopReason?`<span class=pill>${esc(m.stopReason)}</span>`:''}
      ${m.usage?`<span class="pill mono">${esc(tok(m.usage))}</span>`:''}</div>
    <div class=outlbl>model output</div>${out}
    ${results.map(t=>`<div class="sub tool ${t.isError?'err':''}">
      <div class=lbl><b>${esc(t.toolName)}</b><span class=dim>${off(t)}</span>${t.isError?'<span class=bad>error</span>':''}</div>
      ${MODE==="raw"?cut(J(t.raw),hl):`
      ${t.args?`<details><summary>arguments</summary>${cut(t.args)}</details>`:""}
      <details><summary>result (${(t.blocks||[]).reduce((n,b)=>n+(b.text||"").length,0).toLocaleString()} chars)</summary>${cut((t.blocks||[]).map(b=>b.text).join("\n"))}</details>`}
    </div>`).join("")}
  </div>`;
}

function renderRun(r,d,opts){
  const M=d.messages||[], C=d.calls||[], SP=d.systemPrompt||"";
  const own=C.filter(c=>!c.prior), pri=C.filter(c=>c.prior);
  const body=own.map((c,i)=>{
    const prev=i===0?(pri.length?pri[pri.length-1]:null):own[i-1];
    return contextBlock(M,SP,c,prev,i+1)+callBlock(M,C,c,i+1);
  }).join("")+(own.length
    ? contextBlock(M,SP,{i:M.length},own[own.length-1],own.length,true)
    : "");
  const priorBody=(pri.length&&!(opts||{}).hidePrior)
    ? `<details class=priorwrap><summary>${pri.length} call${pri.length>1?"s":""} inherited from earlier turns in this session</summary>${pri.map((c,i)=>callBlock(M,C,c,"P"+(i+1))).join("")}</details>`:"";
  const users=M.filter(m=>m.role==="user"&&!m.prior);
  return `${users.map(u=>`<div class=usermsg><div class=lbl><b>user message</b><span class=dim>${off(u)}</span></div>${cut((u.blocks||[]).map(b=>b.text).join("\n"))}</div>`).join("")}
    <div class="mono dim" style="margin:6px 0">${own.length} model call${own.length===1?"":"s"}${pri.length?` · ${pri.length} inherited`:""} · context ${size(M).toLocaleString()} chars by the end</div>
    ${priorBody}${body||"<div class=empty>no model call recorded (run never reached the model)</div>"}`;
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
  if(sel)sel.classList.remove("sel");
  sel=tr;tr.classList.add("sel");
  FULL=[];
  $("#detail").innerHTML="<div class=empty>loading…</div>";
  const r=await (await fetch("/api/run/"+encodeURIComponent(tr.dataset.id))).json();
  const d=r.detail||{};
  if(d.error){$("#detail").innerHTML=`<div class=empty>${esc(d.error)}</div>`;return}
  $("#detail").innerHTML=
    hdr(r,`<span class=pill>${(r.total_tokens||0).toLocaleString()} tok</span>
      <span class="pill act" data-sess="${r.session_id}">▶ full session trace (${r.turn_tot||1} turns)</span>`)+
    `<div class="mono dim" style="margin-bottom:10px">${esc(r.session_key||"")}<br>run ${esc(r.run_id)}</div>`+
    `<details><summary>${(d.tools||[]).length} tools available to the model</summary>${cut((d.tools||[]).join("\n"))}</details>`+
    renderRun(r,d);
  wireActions();
}

async function openSession(sid){
  if(detailHidden())setDetail(false);
  FULL=[];
  $("#detail").innerHTML="<div class=empty>loading whole session…</div>";
  const s=await (await fetch("/api/session/"+encodeURIComponent(sid))).json();
  if(s.error){$("#detail").innerHTML=`<div class=empty>${esc(s.error)}</div>`;return}
  const turns=s.runs.map((r,i)=>`
    <div class=turnbar>TURN ${i+1} of ${s.turns} <span class=dim>${when(r.started_ts)} · ${dur(r.duration_ms)} · $${(r.cost_usd||0).toFixed(4)}</span>
      ${r.ok?"":`<span class=bad>✕ ${esc(r.failure_kind||"failed")}</span>`}</div>
    ${renderRun(r,r.detail||{},{hidePrior:true})}`).join("");
  $("#detail").innerHTML=
    hdr(s,`<span class=pill>${s.turns} turns</span><span class=pill>${s.tool_count} tool calls</span>`)+
    `<div class="mono dim" style="margin-bottom:10px">${esc(s.session_key||"")}<br>session ${esc(s.session_id)}</div>`+
    (s.truncated?`<div class=warnbar>showing the first 25 of ${s.turns} turns</div>`:"")+
    turns;
  wireActions();
}

function wireActions(){
  $("#detail").querySelectorAll(".act[data-sess]").forEach(el=>{
    el.style.cursor="pointer"; el.onclick=()=>openSession(el.dataset.sess);
  });
}

$("#agent").onchange=load;$("#failed").onchange=load;
$("#lim").onchange=()=>{LIMIT=+$("#lim").value;if(sel)open(sel)};
$("#mode").onchange=()=>{MODE=$("#mode").value;if(sel)open(sel)};
$("#grp").onchange=load;
let t;$("#q").oninput=()=>{clearTimeout(t);t=setTimeout(load,250)};

$("#togdetail").onclick=()=>setDetail(!detailHidden());
// Backslash toggles it, as long as you are not typing in the search box.
document.addEventListener("keydown",e=>{
  if(e.key!=="\\"||e.metaKey||e.ctrlKey||e.altKey)return;
  const t=e.target.tagName;
  if(t==="INPUT"||t==="SELECT"||t==="TEXTAREA")return;
  e.preventDefault(); setDetail(!detailHidden());
});
try{if(localStorage.getItem("hideDetail"))setDetail(true)}catch(e){}

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
    print("stop with Ctrl+C", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
