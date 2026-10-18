"""/scorebot update request/result files."""
import json
import os
import subprocess
import time
from pathlib import Path

from scorebot import selfupdate as su


def test_request_lifecycle(tmp_path):
    assert su.pending(tmp_path) is None
    su.write_request(tmp_path, 123, 456)
    assert su.pending(tmp_path) == "queued"
    req = json.loads((tmp_path / su.REQUEST).read_text())
    assert req["channel_id"] == 123 and req["user_id"] == 456
    assert not su.stale_request(tmp_path)
    assert su.stale_request(tmp_path, now=time.time() + su.STALE_S + 1)
    (tmp_path / su.REQUEST).rename(tmp_path / su.RUNNING)
    assert su.pending(tmp_path) == "running"
    su.clear_request(tmp_path)


def test_take_result_and_messages(tmp_path):
    assert su.take_result(tmp_path) is None
    (tmp_path / su.RESULT).write_text(json.dumps({
        "channel_id": 1, "status": "updated", "old": "a" * 40, "new": "b" * 40,
        "commits": "bbbbbbb Add power rankings\ncccccc Fix thing"}))
    r = su.take_result(tmp_path)
    assert not (tmp_path / su.RESULT).exists()
    msg = su.result_message(r)
    assert "`aaaaaaa` → `bbbbbbb`" in msg and "• Add power rankings" in msg
    assert "up to date" in su.result_message({"status": "uptodate", "new": "abc1234"})
    fail = su.result_message({"status": "failed", "log": "line1\nfatal: boom"})
    assert "failed" in fail and "fatal: boom" in fail
    (tmp_path / su.RESULT).write_text("{not json")
    assert su.take_result(tmp_path)["status"] == "failed"


def test_discord_update_script_end_to_end(tmp_path):
    """Run deploy/discord-update.sh against a fake repo/data dir (no root, no systemd)."""
    repo = Path(__file__).resolve().parents[1]
    script = (repo / "deploy" / "discord-update.sh").read_text()
    data = tmp_path / "data"
    data.mkdir()
    fake = tmp_path / "repo" / "deploy"
    fake.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(fake.parent)], check=True)
    (fake / "auto-update.sh").write_text('echo "up to date"\n')
    py = os.popen("command -v python3").read().strip()
    (fake / "discord-update.sh").write_text(
        script.replace("DATA=/opt/scorebot/data", f"DATA={data}")
              .replace("/opt/scorebot/venv/bin/python", py))
    su.write_request(data, 42, 7)
    subprocess.run(["bash", str(fake / "discord-update.sh")], check=True, capture_output=True)
    r = su.take_result(data)
    assert r["status"] == "uptodate" and r["channel_id"] == 42
    assert su.pending(data) is None
