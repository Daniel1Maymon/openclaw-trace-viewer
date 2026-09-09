#!/usr/bin/env bash
# Install the OpenClaw trace viewer.
#
# Idempotent: running it again is the normal way to upgrade. Every step either
# checks its own work or refuses to continue, because a start command exiting 0
# is not evidence that anything is serving.
set -euo pipefail

DIR="${TRACE_INSTALL_DIR:-$HOME/trace-viewer}"
PORT="${TRACE_PORT:-8765}"
AGENTS_DIR="${OPENCLAW_AGENTS_DIR:-}"
SKIP="${TRACE_SKIP_AGENTS:-}"
LOG_DIR="${TRACE_LOG_DIR:-}"
DO_CRON=1
DO_START=1
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
  cat <<EOF
usage: ./install.sh [options]

  --dir PATH          install location            (default: \$HOME/trace-viewer)
  --port N            viewer port                 (default: 8765)
  --agents-dir PATH   OpenClaw agents directory   (default: autodetected)
  --skip-agents A,B   agents to leave out of the index
  --no-cron           skip the 4-hourly index refresh
  --no-start          install and index, but do not start the viewer
  -h, --help          this

All options also read from the matching environment variable; see CLAUDE.md.
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --dir)         DIR="$2"; shift 2 ;;
    --port)        PORT="$2"; shift 2 ;;
    --agents-dir)  AGENTS_DIR="$2"; shift 2 ;;
    --skip-agents) SKIP="$2"; shift 2 ;;
    --no-cron)     DO_CRON=0; shift ;;
    --no-start)    DO_START=0; shift ;;
    -h|--help)     usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

say()  { printf '  %s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }
die()  { printf '\nFAILED: %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- preflight
# Everything that can be known before we write anything is checked here, so a
# failure leaves the machine as it was found.
step "preflight"

command -v python3 >/dev/null 2>&1 || die "python3 not found."
PYV=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
python3 - <<'EOF' || die "python3 $PYV is too old; this needs 3.8 or newer."
import sys
sys.exit(0 if sys.version_info[:2] >= (3, 8) else 1)
EOF
say "python3 $PYV"

python3 -c 'import sqlite3' 2>/dev/null || die "python3 is missing the sqlite3 module."
say "sqlite3 module present"

for f in serve.py index_traces.py; do
  [ -f "$SRC/$f" ] || die "$f not found next to install.sh — run this from a checkout."
done
say "sources found in $SRC"

# The agents directory is the one thing worth guessing at, because it is nearly
# always in one of two places and asking about it is friction. Guessing WRONG,
# though, produces an empty index that looks like a working install, so an
# autodetected path is only accepted if it actually holds trajectory files.
if [ -z "$AGENTS_DIR" ]; then
  for c in /root/.openclaw/agents "$HOME/.openclaw/agents"; do
    [ -d "$c" ] && { AGENTS_DIR="$c"; break; }
  done
fi
[ -n "$AGENTS_DIR" ] || die "could not find an OpenClaw agents directory.
Looked in /root/.openclaw/agents and \$HOME/.openclaw/agents.
Pass it explicitly:  ./install.sh --agents-dir /path/to/.openclaw/agents"
[ -d "$AGENTS_DIR" ] || die "agents directory does not exist: $AGENTS_DIR"
say "agents directory: $AGENTS_DIR"

TRAJ=$(find "$AGENTS_DIR" -name '*.jsonl' -type f 2>/dev/null | head -1 || true)
[ -n "$TRAJ" ] || die "no .jsonl trajectory files under $AGENTS_DIR.
That is either the wrong directory, or OpenClaw has not run yet."
say "trajectory files present"

# A port already in use is usually our own previous instance. serve.py handles
# that case itself; anything else is somebody else's process and we stop.
if command -v lsof >/dev/null 2>&1 && lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | grep -q 'serve.py\|python'; then
    say "port $PORT held by an existing viewer — it will be replaced"
  else
    die "port $PORT is in use by something that is not this viewer.
Pick another:  ./install.sh --port 8766"
  fi
else
  say "port $PORT free"
fi

if [ -z "$LOG_DIR" ]; then
  PARENT=$(dirname "$AGENTS_DIR")
  LOG_DIR="$PARENT/logs"
fi

# ---------------------------------------------------------------- install
step "installing to $DIR"
mkdir -p "$DIR" "$DIR/scripts" "$LOG_DIR"

# An existing index is an ARCHIVE: it holds rows for trajectory files OpenClaw
# has since rotated away, which cannot be rebuilt from disk. Back it up before
# a new version of the indexer ever touches it.
if [ -f "$DIR/traces.db" ]; then
  BK="$DIR/traces.db.bak-$(date +%Y%m%d-%H%M%S)"
  cp "$DIR/traces.db" "$BK"
  say "existing index backed up to $(basename "$BK")"
fi

for f in serve.py index_traces.py; do
  cp "$SRC/$f" "$DIR/$f"
done
[ -f "$SRC/skip_agents.txt" ] && cp "$SRC/skip_agents.txt" "$DIR/skip_agents.txt"
[ -f "$SRC/scripts/restart.sh" ] && cp "$SRC/scripts/restart.sh" "$DIR/scripts/restart.sh"
chmod +x "$DIR/scripts/restart.sh" 2>/dev/null || true
say "copied serve.py, index_traces.py$( [ -f "$DIR/skip_agents.txt" ] && printf ', skip_agents.txt')"

# The environment the viewer and the cron job must agree on. Written to disk so
# a reboot, a cron run and a hand restart all use the same settings.
ENVF="$DIR/trace-viewer.env"
{
  echo "# written by install.sh on $(date -u +%FT%TZ)"
  echo "OPENCLAW_AGENTS_DIR=$AGENTS_DIR"
  echo "TRACE_PORT=$PORT"
  echo "TRACE_LOG_DIR=$LOG_DIR"
  [ -n "$SKIP" ] && echo "TRACE_SKIP_AGENTS=$SKIP"
} > "$ENVF"
say "settings written to $(basename "$ENVF")"

# ---------------------------------------------------------------- index
step "building the index"
cd "$DIR"
set +e
OPENCLAW_AGENTS_DIR="$AGENTS_DIR" ${SKIP:+TRACE_SKIP_AGENTS="$SKIP"} \
  python3 index_traces.py
RC=$?
set -e
[ $RC -eq 0 ] || die "index_traces.py exited $RC (see the output above)."

RUNS=$(python3 - "$DIR/traces.db" <<'EOF'
import sqlite3, sys
try:
    print(sqlite3.connect(sys.argv[1]).execute("select count(*) from runs").fetchone()[0])
except Exception:
    print(0)
EOF
)
[ "$RUNS" -gt 0 ] || die "the index built but contains 0 runs.
$AGENTS_DIR has trajectory files, so this is likely the wrong agents directory
or every agent is excluded by TRACE_SKIP_AGENTS ($SKIP)."
say "$RUNS runs indexed"

# ---------------------------------------------------------------- cron
if [ "$DO_CRON" = 1 ]; then
  step "index refresh"
  if ! command -v crontab >/dev/null 2>&1; then
    say "crontab not available — skipping. Refresh by hand: python3 index_traces.py"
  else
    LINE="0 */4 * * * cd $DIR && ${SKIP:+TRACE_SKIP_AGENTS=$SKIP }OPENCLAW_AGENTS_DIR=$AGENTS_DIR $(command -v python3) index_traces.py >> $LOG_DIR/trace-index-cron.log 2>&1"
    CUR=$(crontab -l 2>/dev/null || true)
    # Match on the install dir, so re-running replaces our entry instead of
    # stacking a second one, and leaves everybody else's crontab alone.
    NEW=$(printf '%s\n' "$CUR" | grep -v "cd $DIR && .*index_traces.py" || true)
    printf '%s\n%s\n%s\n' \
      "$NEW" \
      "# Keep the trace index complete even when the viewer is not running." \
      "$LINE" | grep -v '^$' | crontab -
    crontab -l 2>/dev/null | grep -qF "$DIR" \
      && say "cron entry installed (every 4 hours)" \
      || die "cron entry did not take."
  fi
fi

# ---------------------------------------------------------------- start
if [ "$DO_START" = 1 ]; then
  step "starting the viewer"
  if [ -x "$DIR/scripts/restart.sh" ]; then
    "$DIR/scripts/restart.sh" || die "restart.sh reported a problem."
  else
    pkill -f "python3 $DIR/serve.py" 2>/dev/null || true
    sleep 1
    setsid env OPENCLAW_AGENTS_DIR="$AGENTS_DIR" TRACE_PORT="$PORT" \
      ${SKIP:+TRACE_SKIP_AGENTS="$SKIP"} \
      python3 "$DIR/serve.py" >> "$LOG_DIR/trace-viewer.log" 2>&1 </dev/null &
    sleep 4
  fi

  CODE=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/" || echo 000)
  [ "$CODE" = "200" ] || die "the viewer is not answering on port $PORT (got HTTP $CODE).
Check the log:  tail -50 $LOG_DIR/trace-viewer.log"
  say "HTTP 200 from http://127.0.0.1:$PORT/"

  API=$(curl -s "http://127.0.0.1:$PORT/api/runs?limit=1" || true)
  case "$API" in
    *'"rows"'*) say "the API is serving indexed runs" ;;
    *) die "the page answered but /api/runs did not return rows." ;;
  esac
fi

cat <<EOF

Done. $RUNS runs indexed, viewer on http://127.0.0.1:$PORT/

It binds to loopback on purpose — the pages show real conversation content.
To reach it from your laptop, forward the port instead of changing the bind
address:

    ssh -L $PORT:127.0.0.1:$PORT $(whoami)@$(hostname)

Restart it later with:   $DIR/scripts/restart.sh
Before sharing a screenshot, read the redaction note in CLAUDE.md.
EOF
