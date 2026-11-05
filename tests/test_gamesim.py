"""The NFL game simulator: calibration to the model, reproducibility, player ranges and the /nfl simulate output."""
import pytest

from scorebot.models import gamesim as gs


def players(side_qb="QB A", rb="RB A", wr1="WR A", te="TE A"):
    return [
        {"name": side_qb, "pos": "QB", "pass_yds": 250.0},
        {"name": rb, "pos": "RB", "rush_yds": 70.0, "td": 0.5},
        {"name": wr1, "pos": "WR", "rec_yds": 75.0, "rec": 5.5, "td": 0.45},
        {"name": te, "pos": "TE", "rec_yds": 40.0, "rec": 3.5, "td": 0.3},
    ]


def game(hp=27.0, ap=20.0):
    return gs.SimGame(home="BUF", away="MIA", home_pts=hp, away_pts=ap,
                      home_players=players("Home QB", "Home RB", "Home WR", "Home TE"),
                      away_players=players("Away QB", "Away RB", "Away WR", "Away TE"))


def test_average_points_match_the_model():
    s = gs.simulate(game(), 6000, seed=7)
    assert s["avg_home"] == pytest.approx(27.0, abs=1.5)
    assert s["avg_away"] == pytest.approx(20.0, abs=1.5)
    assert 0.6 < s["home_win"] < 0.8
    assert s["home_win"] + s["away_win"] + s["tie"] == pytest.approx(1.0)
    assert 10 < s["margin_sd"] < 17                       # real NFL margins: about 13.5


def test_even_matchup_is_a_coin_flip():
    s = gs.simulate(game(23.0, 23.0), 6000, seed=3)
    assert s["home_win"] == pytest.approx(0.5, abs=0.04)


def test_same_seed_same_result_and_replay_matches():
    g = game()
    assert gs.simulate(g, 500, seed=11) == gs.simulate(g, 500, seed=11)
    assert gs.simulate(g, 500, seed=11) != gs.simulate(g, 500, seed=12)
    r = gs.replay(g, 11, 42)
    again = gs.play_game(gs.Prepared(g), gs._game_rng(11, 42))
    assert (r["home"], r["away"]) == (again["home"], again["away"])
    assert r["drives"] and r["drives"][-1]["home"] == r["home"]


def test_player_ranges():
    s = gs.simulate(game(), 4000, seed=5)
    qb = s["players"]["Home QB"]
    assert qb["team"] == "home" and qb["pos"] == "QB"
    assert qb["pass_yds"]["mean"] == pytest.approx(250, rel=0.15)
    q = qb["pass_yds"]["q"]
    assert q == sorted(q) and q[0] < q[2] < q[4]
    wr = s["players"]["Home WR"]
    assert 0 < wr["td"]["p_any"] < 1 and wr["rec"]["mean"] > 2


def test_at_least_one_game():
    assert gs.simulate(game(), 0, seed=1)["n"] == 1


def test_margin_buckets_sum_to_one():
    s = gs.simulate(game(), 2000, seed=2)
    assert sum(gs.margin_buckets(s).values()) == pytest.approx(1.0)


class FakePlayers:
    def __init__(self):
        self.info = {
            "q1": {"name": "Starter QB", "team": "BUF", "pos": "QB"},
            "r1": {"name": "Lead Back", "team": "BUF", "pos": "RB"},
            "r2": {"name": "Hurt Back", "team": "BUF", "pos": "RB"},
            "w1": {"name": "Top Receiver", "team": "BUF", "pos": "WR"},
            "w2": {"name": "Other Team WR", "team": "MIA", "pos": "WR"},
        }
        self.proj = {("q1", "pass_yds"): 260, ("r1", "rush_yds"): 75, ("r2", "rush_yds"): 80,
                     ("w1", "rec_yds"): 70, ("w1", "receptions"): 5, ("w1", "total_tds"): 0.4,
                     ("w2", "rec_yds"): 90}

    def project(self, pid, stat, opp, team_pts):
        return self.proj.get((pid, stat), 0.0)

    def find(self, name, team=None):
        return next((p for p, i in self.info.items() if i["name"] == name and (not team or i["team"] == team)), None)


def test_roster_skips_injured_players_and_other_teams():
    notes = []
    out = gs.roster(FakePlayers(), "BUF", "MIA", 24.0, "Starter QB",
                    lambda n: "Out" if n == "Hurt Back" else None, notes)
    names = [p["name"] for p in out]
    assert names[0] == "Starter QB" and "Lead Back" in names and "Top Receiver" in names
    assert "Hurt Back" not in names and "Other Team WR" not in names
    assert notes == ["BUF left out: Hurt Back (out)"]


def test_roster_backup_qb_without_history():
    out = gs.roster(FakePlayers(), "BUF", "MIA", 20.0, "replacement-level backup")
    assert out[0]["name"] == "Backup QB" and out[0]["pass_yds"] == 190.0


class FakeService:
    players = FakePlayers()
    espn_injuries = {}

    def predict_game(self, g):
        return {"home": "BUF", "away": "MIA", "total": 46.0, "margin": 4.0, "home_qb_name": "Starter QB",
                "away_qb_name": "replacement-level backup", "home_qb_adj": 0.0, "away_qb_adj": -3.0,
                "home_qb_src": "usual starter", "away_qb_src": "injury report: starter out"}


def test_build_game_from_the_model():
    sg = gs.build_game(FakeService(), {"week": 10})
    assert sg.home_pts + sg.away_pts == pytest.approx(46.0)
    assert sg.home_pts - sg.away_pts == pytest.approx(4.0 * gs.SIM_STRETCH)
    assert sg.home_players and sg.away_players[0]["name"] == "Backup QB"
    assert any("MIA QB" in n for n in sg.notes)


def test_simulate_embed_and_player_menu():
    from scorebot.cogs.nfl import SimView, player_embed, sim_embed
    sg = game()
    s = gs.simulate(sg, 1000, seed=4)
    e = sim_embed(sg, s, {"week": 10, "gameday": "2026-11-08"})
    names = [f.name for f in e.fields]
    assert names[:3] == ["Win chance", "Typical final", "How it ends"]
    assert any(n.startswith("BUF players") for n in names) and any(n.startswith("MIA players") for n in names)
    assert all(len(f.value) <= 1024 for f in e.fields)
    pe = player_embed("Home QB", s["players"]["Home QB"], s["n"])
    assert pe.fields[0].name == "pass yds"
