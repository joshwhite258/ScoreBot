"""Prediction log, model service queries, and the Discord command tree."""
import asyncio
import os

import pytest

from scorebot import predictions
from scorebot.db import DB
from scorebot.models import nflverse as nv
from scorebot.models.ratings import build
from scorebot.models.service import ModelService

from .test_models import synthetic_history


def _service(tmp_path):
    games, tw, strength = synthetic_history()
    season = nv.current_season()
    upcoming = [{"game_id": f"{season}_09_KC_BUF", "season": season, "week": 9, "home": "BUF", "away": "KC",
                 "home_score": None, "away_score": None, "neutral": False, "gameday": f"{season}-11-01"},
                {"game_id": f"{season}_10_BUF_DET", "season": season, "week": 10, "home": "DET", "away": "BUF",
                 "home_score": None, "away_score": None, "neutral": False, "gameday": f"{season}-11-08"}]
    svc = ModelService(tmp_path, None)
    svc.state, svc.games, svc.built_at = build(games, tw), games + upcoming, 1.0
    return svc, strength


def test_service_upcoming_predict_and_ratings(tmp_path):
    svc, strength = _service(tmp_path)
    up = svc.upcoming()
    assert [g["week"] for g in up] == [9]                      # only the next week
    p = svc.predict_game(up[0])
    assert p["home_name"] == "Buffalo Bills" and p["away_name"] == "Kansas City Chiefs"
    assert 0 < p["p_home"] < 1 and p["home_qb_src"] == "usual starter"
    assert svc.next_game("DET")["week"] == 10
    ranks = svc.ratings()
    assert len(ranks) == 32 and ranks[0]["code"] == max(strength, key=strength.get)
    assert svc.player_projection("anyone") is None             # no player model loaded


def test_prediction_log_record_grade_summary(tmp_path):
    async def run():
        db = DB(tmp_path / "s.db")
        await db.connect()
        preds = [{"game_id": "g1", "season": 2026, "week": 7, "home": "BUF", "away": "KC",
                  "p_home": 0.65, "margin": 3.0, "total": 48.0},
                 {"game_id": "g2", "season": 2026, "week": 7, "home": "DET", "away": "GB",
                  "p_home": 0.40, "margin": -2.0, "total": 45.0}]
        await predictions.record(db, preds)
        preds[0]["p_home"] = 0.70                                # refresh before kickoff is allowed
        await predictions.record(db, preds)
        n = await predictions.grade(db, [{"game_id": "g1", "home_score": 27, "away_score": 20},
                                         {"game_id": "g2", "home_score": None, "away_score": None}])
        assert n == 1
        preds[0]["p_home"] = 0.10                                # ...but never after the result is in
        await predictions.record(db, preds)
        row = await db.fetchone("SELECT p_home FROM predictions WHERE game_id='g1'")
        rec = await predictions.track_record(db, 2026)
        await db.set_meta("k", "v")
        meta = await db.get_meta("k")
        await db.set_channel(1, 99)
        ch = await db.channel(1)
        await db.close()
        return row["p_home"], rec, meta, ch

    p, rec, meta, ch = asyncio.run(run())
    assert p == pytest.approx(0.70)
    assert rec["games"] == 1 and rec["correct"] == 1 and rec["margin_mae"] == pytest.approx(4.0)
    assert rec["total_mae"] == pytest.approx(1.0)
    assert meta == "v" and ch == 99


def test_cogs_load_and_commands_register(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "test-token")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "s.db"))
    from scorebot.bot import COGS, ScoreBot
    from scorebot.config import Config

    async def run():
        bot = ScoreBot(Config.load())
        for ext in COGS:
            await bot.load_extension(ext)
        names = sorted(c.qualified_name for c in bot.tree.walk_commands())
        await bot.close()
        return names

    names = asyncio.run(run())
    for want in ["scores", "live", "form", "injuries", "nfl predict", "nfl power", "nfl player", "nfl report",
                 "scorebot help", "scorebot status", "scorebot setchannel", "scorebot digest", "scorebot update"]:
        assert want in names, names


def test_env_inline_comments(monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "abc  # my token")
    monkeypatch.setenv("DIGEST_HOUR", "-1 # off")
    from scorebot.config import Config
    cfg = Config.load()
    assert cfg.discord_token == "abc" and cfg.digest_hour == -1
