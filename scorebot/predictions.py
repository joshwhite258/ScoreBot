"""Live track record: freeze each NFL prediction before kickoff, grade it once the game is final.

Backtests can flatter a model; a log of calls made in advance can't. /nfl report shows both.
"""
from __future__ import annotations

import math
import time

from .db import DB


async def record(db: DB, preds: list[dict]) -> int:
    """Store predictions for games not yet logged. Re-running before kickoff refreshes the
    numbers (new QB news, rebuilt ratings); once a game has a score it never changes."""
    n = 0
    for p in preds:
        n += await db.execute(
            "INSERT INTO predictions(game_id, season, week, home, away, p_home, margin, total, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(game_id) DO UPDATE SET "
            "p_home=excluded.p_home, margin=excluded.margin, total=excluded.total, created_at=excluded.created_at "
            "WHERE predictions.home_score IS NULL",
            (p["game_id"], p["season"], p["week"], p["home"], p["away"],
             p["p_home"], p["margin"], p["total"], time.time()))
    return n


async def grade(db: DB, games: list[dict]) -> int:
    """Copy final scores from the schedule onto logged predictions. Returns games graded."""
    open_ids = {r["game_id"] for r in await db.fetchall("SELECT game_id FROM predictions WHERE home_score IS NULL")}
    n = 0
    for g in games:
        if g["game_id"] in open_ids and g["home_score"] is not None:
            n += await db.execute("UPDATE predictions SET home_score=?, away_score=? WHERE game_id=?",
                                  (g["home_score"], g["away_score"], g["game_id"]))
    return n


def summarize(rows: list) -> dict:
    """Accuracy, Brier score and average miss for graded predictions (ties excluded from W/L)."""
    graded = [r for r in rows if r["home_score"] is not None]
    decided = [r for r in graded if r["home_score"] != r["away_score"]]
    if not graded:
        return {"games": 0}
    hits = sum((r["p_home"] >= 0.5) == (r["home_score"] > r["away_score"]) for r in decided)
    brier = sum((r["p_home"] - (r["home_score"] > r["away_score"])) ** 2 for r in decided) / max(len(decided), 1)
    margin_err = [abs(r["margin"] - (r["home_score"] - r["away_score"])) for r in graded]
    total_err = [abs(r["total"] - (r["home_score"] + r["away_score"])) for r in graded]
    return {"games": len(graded), "correct": hits, "decided": len(decided),
            "accuracy": hits / len(decided) if decided else math.nan, "brier": brier,
            "margin_mae": sum(margin_err) / len(margin_err), "total_mae": sum(total_err) / len(total_err)}


async def track_record(db: DB, season: int | None = None) -> dict:
    sql, args = "SELECT * FROM predictions", ()
    if season is not None:
        sql, args = sql + " WHERE season=?", (season,)
    return summarize(await db.fetchall(sql, args))
