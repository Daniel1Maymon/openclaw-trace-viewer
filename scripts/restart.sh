#!/usr/bin/env bash
# Restart the viewer, detached from whatever shell asked for it.
#
# setsid with closed stdio is deliberate. Without it the server dies with the
# ssh session that started it, or holds that connection open until it times
# out. `nohup ... &` is the simplification that did not work here.
set -uo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIR"

# Settings install.sh wrote, so a hand restart matches the cron job.
[ -f trace-viewer.env ] && set -a && . ./trace-viewer.env && set +a

PORT="${TRACE_PORT:-8765}"
LOG_DIR="${TRACE_LOG_DIR:-$DIR}"
mkdir -p "$LOG_DIR"

# Match the absolute path only. A broad `pkill -f serve.py` will happily kill
# somebody else's viewer on the same box — it did, once.
pkill -f "python3 $DIR/serve.py" 2>/dev/null || true
sleep 1

setsid python3 "$DIR/serve.py" >> "$LOG_DIR/trace-viewer.log" 2>&1 </dev/null &
sleep 4

# Exiting 0 is not evidence that anything is serving. Ask the port.
CODE=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/" || echo 000)
echo "HTTP $CODE  http://127.0.0.1:$PORT/"
[ "$CODE" = "200" ] || { echo "not serving — tail -50 $LOG_DIR/trace-viewer.log" >&2; exit 1; }
