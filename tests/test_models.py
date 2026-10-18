"""Team ratings, the blended win-probability model, and player projections."""
import random

import pytest

from scorebot.models.players import PlayerModel, PlayerParams
from scorebot.models.ratings import Elo, build, calibration, fit_logistic, game_prediction, sigmoid

CODES = ["BUF", "KC", "BAL", "PHI", "DET", "SF", "NYJ", "CAR"]


def synthetic_history(seed=2, seasons=range(2000, 2024)):
    """Fake league where each team has a fixed true strength; scores and EPA follow it."""
    rnd = random.Random(seed)
    strength = {t: rnd.gauss(0, 6) for t in CODES}
    games, tw, gid = [], {}, 0
    for season in seasons:
        for week in range(1, 15):
            teams = list(strength)
            rnd.shuffle(teams)
            for h, a in zip(teams[::2], teams[1::2]):
                gid += 1
                m = strength[h] - strength[a] + 1.5 + rnd.gauss(0, 13)
                tot = max(abs(m) + 3, 44 + (strength[h] + strength[a]) / 2 + rnd.gauss(0, 9))
                games.append({"game_id": str(gid), "season": season, "week": week, "home": h, "away": a,
                              "home_score": round((tot + m) / 2), "away_score": round((tot - m) / 2),
                              "neutral": False, "gameday": f"{season}-09-{week:02d}"})
                tw[(str(gid), h)] = {"epa": strength[h] * 2 + rnd.gauss(0, 5), "plays": 60}
                tw[(str(gid), a)] = {"epa": strength[a] * 2 + rnd.gauss(0, 5), "plays": 60}
    return games, tw, strength


def test_elo_update_and_prob():
    e = Elo()
    assert 0.55 < e.prob("A", "B", False) < 0.56        # home field only
    assert e.prob("A", "B", True) == pytest.approx(0.5)
    e.update("A", "B", False, 31, 3)
    assert e.rating("A") > 1505 > e.rating("B")
    assert e.rating("A") + e.rating("B") == pytest.approx(3010)   # zero-sum


def test_logistic_recovers_weights():
    rnd = random.Random(0)
    rows = []
    for _ in range(4000):
        x = rnd.gauss(0, 1)
        rows.append(({"elo": x}, int(rnd.random() < sigmoid(0.3 + 1.5 * x))))
    w = fit_logistic(rows, ("elo",), l2=0.0)
    assert w["elo"] == pytest.approx(1.5, abs=0.15) and w["bias"] == pytest.approx(0.3, abs=0.12)


def test_build_and_predict_synthetic():
    games, tw, strength = synthetic_history()
    st = build(games, tw)
    best, worst = max(strength, key=strength.get), min(strength, key=strength.get)
    assert st.elo.rating(best) > st.elo.rating(worst)
    assert st.epa.net(best) > st.epa.net(worst)
    pred = game_prediction(st, best, worst, False)
    assert pred["p_home"] > 0.6 and pred["margin"] > 0
    assert pred["home_pts"] > pred["away_pts"]
    assert pred["home_pts"] + pred["away_pts"] == pytest.approx(pred["total"])
    bt = st.backtest
    assert bt["games"] > 0 and set(bt["accuracy"]) == {"model", "elo", "home"}
    assert bt["log_loss"]["model"] < bt["log_loss"]["home"]          # beats "home team wins"
    assert "totals" in bt and bt["totals"]["rmse_model"] > 0


def test_calibration_buckets():
    rows = calibration([(0.9, 1), (0.1, 0), (0.6, 0), (0.4, 1)])
    assert sum(r["n"] for r in rows) == 4
    assert all(0.5 <= r["lo"] < r["hi"] <= 1.0 for r in rows)


def _row(pid, name, team, opp, wk, pos="WR", **st):
    base = {k: 0.0 for k in ("passing_yards", "passing_tds", "rushing_yards", "rushing_tds", "receptions",
                             "receiving_yards", "receiving_tds", "attempts", "carries", "targets")}
    base.update(st)
    return {"player_id": pid, "player_display_name": name, "position": pos, "team": team,
            "opponent_team": opp, "season": 2026, "week": wk, "game_id": f"g{wk}", **base}


def test_player_model_learns_and_adjusts():
    pm = PlayerModel(PlayerParams())
    pm._new_season(2026)
    for wk in range(1, 9):
        # star WR ~100 yds; TEN's defense allows a lot to everyone, SF's very little
        pm.update_game([_row("p1", "Star Guy", "BUF", "KC", wk, receiving_yards=100, receptions=7, targets=9),
                        _row("p2", "Filler", "NYJ", "TEN", wk, receiving_yards=90, receptions=6, targets=8),
                        _row("p3", "Filler Two", "MIA", "SF", wk, receiving_yards=30, receptions=3, targets=5)])
    base = pm.project("p1", "rec_yds", None, None)
    assert 90 < base < 105
    assert pm.project("p1", "rec_yds", "TEN", None) > base > pm.project("p1", "rec_yds", "SF", None)
    assert pm.project("p1", "rec_yds", None, 28) > base         # team projected to score a lot
    assert pm.find("star guy") == "p1" and pm.find("STAR") == "p1"
    lo, hi = pm.interval("rec_yds", base)
    assert lo < base < hi
    rlo, rhi = pm.interval("receptions", 7.0)
    assert rlo < 7 < rhi and rlo == int(rlo)
