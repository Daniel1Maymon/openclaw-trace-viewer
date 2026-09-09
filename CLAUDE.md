# Installing the OpenClaw trace viewer

You are an agent installing this tool for someone. This file tells you what it
is, what to ask, and what not to touch. Read it before running anything.

## What this is

A dashboard for OpenClaw trajectory files. OpenClaw tells you what your agents
cost; this tells you whether they worked. Two files do the work:

- `index_traces.py` — reads OpenClaw's `*.jsonl` trajectory files and builds a
  SQLite index (`traces.db`). Read-only on the source, always.
- `serve.py` — a `http.server` dashboard over that index. Binds to loopback.

Python 3.8+. No pip install, no dependencies, no virtualenv. If you find
yourself writing a `requirements.txt`, you have misread the project.

## Install it

```bash
./install.sh
```

It is idempotent — running it twice is safe and is the normal way to upgrade.
It preflights, indexes, installs the refresh cron, starts the viewer, and then
curls the result to prove it works. If it prints `HTTP 200` at the end, you are
done. If it exits non-zero, read the message; it says which check failed and
what to do about it.

Useful flags: `--dir` (install location), `--port`, `--agents-dir`,
`--no-cron`, `--no-start`. `./install.sh --help` lists them.

## What to ask the user, and when

Ask only what you cannot detect:

- **Where OpenClaw keeps its agents.** `install.sh` probes `/root/.openclaw/agents`
  and `~/.openclaw/agents` and uses one if it exists. If neither does, ask —
  do not guess, and do not create the directory.
- **Whether to install the cron entry.** Default is yes (every 4 hours). It
  matters because the index must stay complete even when the viewer is not
  running; OpenClaw rotates trajectory files and an unindexed one is a gap you
  cannot recover later.

Do not ask about the port, the database path, or the bind address unless the
preflight reports a conflict. The defaults are right.

## Reaching the dashboard

`serve.py` binds to `127.0.0.1` on purpose: the pages show real conversation
content, including system prompts and the model's private reasoning. On a
remote box, forward the port rather than changing the bind address:

```bash
ssh -L 8765:127.0.0.1:8765 user@server
```

Then open `http://localhost:8765`. **Never** set `TRACE_HOST=0.0.0.0` to make
this easier. If the user asks you to, tell them what it exposes first.

## Rules

**`traces.db` is an archive, not a cache.** It holds rows for trajectory files
that OpenClaw has since rotated away, and those rows cannot be rebuilt from
disk. Never `DROP TABLE runs`, never delete the file to "start clean", and
never let a re-index downgrade a complete row to an empty one. `install.sh`
backs it up before touching it; keep that habit.

**Never write into the OpenClaw runtime.** The indexer opens trajectory files
read-only and the viewer never opens them at all. Anything under
`~/.openclaw/agents/` is somebody's live agent state.

**Never commit `traces.db` or a screenshot of real traces.** `.gitignore`
covers the database. Screenshots are the trap: a picture of the dashboard is a
picture of real conversations, real session ids and real schedules. Use
`TRACE_REDACT=1` for anything anyone else will see:

```bash
TRACE_REDACT=1 python3 serve.py
```

That replaces conversation content with same-shaped placeholder text and puts a
`REDACTED` badge in the header. Note what it does *not* mask: session ids and
job uuids stay real, because the UI needs them to fetch. Check the frame before
you share it.

**Do not paste secrets into chat.** If a step needs a token or a credential,
give the user a command to run themselves.

## Configuration

All optional, all environment variables. `install.sh` writes the ones you
choose into the cron entry and the start script so they survive a reboot.

| Variable | Default | Meaning |
|---|---|---|
| `OPENCLAW_AGENTS_DIR` | `/root/.openclaw/agents` | Where OpenClaw keeps per-agent session data |
| `TRACE_DB` | `./traces.db` | Index location |
| `TRACE_SKIP_AGENTS` | *(none)* | Comma-separated agents to exclude |
| `TRACE_REDACT` | *(off)* | Placeholder conversation content |
| `TRACE_HOST` | `127.0.0.1` | Bind address — changing this exposes content |
| `TRACE_PORT` | `8765` | Port |

`skip_agents.txt` is read when `TRACE_SKIP_AGENTS` is unset, so a hand-started
viewer skips the same agents the cron job does. Put test agents there.

## Operating it

```bash
./scripts/restart.sh          # restart the viewer, detached, and health-check it
python3 index_traces.py       # refresh the index by hand
```

`restart.sh` uses `setsid` with closed stdio deliberately. Without that the
viewer dies with the SSH session that started it, or holds the connection open
until it times out. If you are tempted to simplify it to `nohup ... &`, that is
the thing that did not work.

## Verifying, not assuming

A start command exiting 0 does not mean the viewer is up. Prove it:

```bash
curl -s -o /dev/null -w 'HTTP %{http_code}\n' http://127.0.0.1:8765/
curl -s 'http://127.0.0.1:8765/api/runs?limit=1' | head -c 200
```

If you changed anything on a remote host, compare checksums rather than
trusting `scp`:

```bash
md5sum serve.py   # both ends, must match
```
