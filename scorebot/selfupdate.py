"""/scorebot update plumbing.

The bot runs as the unprivileged `scorebot` user, so it can't pull or restart itself. Instead it
drops a request file in its data dir; a root systemd path unit (deploy/scorebot-update.path) sees it
and runs deploy/discord-update.sh, which runs the normal auto-update and writes a result file.
The bot picks the result up (right away if nothing changed, or after its restart) and posts it.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

REQUEST = "update-request.json"
RUNNING = "update-running.json"
RESULT = "update-result.json"
STALE_S = 15 * 60      # a request nobody picked up in this long = the updater isn't installed


def write_request(data_dir: Path, channel_id: int, user_id: int) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    tmp = data_dir / (REQUEST + ".tmp")
    tmp.write_text(json.dumps({"channel_id": channel_id, "user_id": user_id, "ts": time.time()}))
    tmp.replace(data_dir / REQUEST)      # atomic: the path unit never sees a half-written file


def pending(data_dir: Path) -> str | None:
    """'queued' (waiting for the updater), 'running', or None."""
    if (data_dir / RUNNING).exists():
        return "running"
    if (data_dir / REQUEST).exists():
        return "queued"
    return None


def stale_request(data_dir: Path, now: float | None = None) -> bool:
    """True when a request has sat unclaimed for STALE_S (updater units not installed/running)."""
    f = data_dir / REQUEST
    if not f.exists():
        return False
    try:
        ts = json.loads(f.read_text()).get("ts", 0)
    except (OSError, ValueError):
        ts = f.stat().st_mtime
    return (now or time.time()) - ts > STALE_S


def clear_request(data_dir: Path) -> None:
    (data_dir / REQUEST).unlink(missing_ok=True)


def take_result(data_dir: Path) -> dict | None:
    """Read and delete the updater's result. None if there isn't one (or it's unreadable)."""
    f = data_dir / RESULT
    if not f.exists():
        return None
    try:
        out = json.loads(f.read_text())
    except (OSError, ValueError):
        out = {"status": "failed", "log": "Updater wrote an unreadable result file."}
    f.unlink(missing_ok=True)
    return out


def result_message(r: dict) -> str:
    st = r.get("status")
    old, new = (r.get("old") or "")[:7], (r.get("new") or "")[:7]
    if st == "uptodate":
        return f"✅ Already up to date (`{new or old}`). Nothing to install."
    if st == "updated":
        commits = [c for c in (r.get("commits") or "").splitlines() if c.strip()][:10]
        body = "\n".join(f"• {c.split(' ', 1)[1] if ' ' in c else c}" for c in commits)
        return f"🚀 Updated `{old}` → `{new}` and restarted.\n{body}".rstrip()
    tail = "\n".join((r.get("log") or "").strip().splitlines()[-15:])[-1500:]
    return f"❌ Update failed — the bot is still on the old version.\n```\n{tail or 'no output'}\n```"
