#!/usr/bin/env bash
# Run by scorebot-update.service (root) when the bot drops data/update-request.json via /scorebot update.
# Runs the normal auto-update and leaves data/update-result.json for the bot to post.
set -uo pipefail

DATA=/opt/scorebot/data
REPO="$(cd "$(dirname "$0")/.." && pwd)"
REQ="$DATA/update-request.json"
RUN="$DATA/update-running.json"
RES="$DATA/update-result.json"

[[ -f "$REQ" ]] || exit 0
mv -f "$REQ" "$RUN"          # stop the path unit re-triggering while we work

OLD=$(git -C "$REPO" rev-parse HEAD 2>/dev/null || echo "")
LOG=$(bash "$REPO/deploy/auto-update.sh" 2>&1)
CODE=$?
NEW=$(git -C "$REPO" rev-parse HEAD 2>/dev/null || echo "")
echo "$LOG"

if [[ $CODE -ne 0 ]]; then
  STATUS=failed
elif [[ "$OLD" == "$NEW" ]]; then
  STATUS=uptodate
else
  STATUS=updated
fi
COMMITS=""
[[ "$STATUS" == updated ]] && COMMITS=$(git -C "$REPO" log --format='%h %s' "$OLD..$NEW" 2>/dev/null)

# The venv python is always there; use it to write JSON safely.
STATUS="$STATUS" OLD="$OLD" NEW="$NEW" COMMITS="$COMMITS" LOG="$LOG" \
  /opt/scorebot/venv/bin/python - "$RUN" "$RES" <<'PY'
import json, os, sys
run, res = sys.argv[1], sys.argv[2]
try:
    req = json.load(open(run))
except Exception:
    req = {}
out = dict(req, status=os.environ["STATUS"], old=os.environ["OLD"], new=os.environ["NEW"],
           commits=os.environ["COMMITS"], log=os.environ["LOG"][-4000:])
tmp = res + ".tmp"
with open(tmp, "w") as f:
    json.dump(out, f)
os.replace(tmp, res)
PY
chown scorebot:scorebot "$RES" 2>/dev/null || true
rm -f "$RUN"
exit 0
