"""ESPN scoreboard and injury parsing, team name lookups."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from scorebot import injuries as inj
from scorebot import scores as sc
from scorebot.names import norm_name, resolve


def _team(abbr, name, home_away, score):
    return {"homeAway": home_away, "score": score,
            "team": {"abbreviation": abbr, "displayName": name, "shortDisplayName": name.split()[-1]}}


TEAM_BOARD = {"events": [
    {"id": "1", "date": "2026-10-18T17:00Z", "status": {"type": {"state": "post", "shortDetail": "Final"}},
     "competitions": [{"id": "1", "competitors": [_team("BUF", "Buffalo Bills", "home", "31"),
                                                  _team("KC", "Kansas City Chiefs", "away", "28")]}]},
    {"id": "2", "date": "2026-10-18T20:25Z", "status": {"type": {"state": "in", "shortDetail": "Q3 5:12"}},
     "competitions": [{"id": "2", "competitors": [_team("DET", "Detroit Lions", "home", "14"),
                                                  _team("GB", "Green Bay Packers", "away", "10")]}]},
    {"id": "3", "date": "2026-10-19T00:20Z", "status": {"type": {"state": "pre", "shortDetail": "8:20 PM"}},
     "competitions": [{"id": "3", "competitors": [_team("SF", "San Francisco 49ers", "home", "0"),
                                                  _team("LAR", "Los Angeles Rams", "away", "0")]}]},
]}

TENNIS_BOARD = {"events": [{"id": "t", "groupings": [{"competitions": [
    {"id": "m1", "date": "2026-10-18T12:00Z", "status": {"type": {"state": "in", "shortDetail": "2nd set"}},
     "competitors": [{"athlete": {"displayName": "Jannik Sinner", "shortName": "J. Sinner"}, "score": "1"},
                     {"athlete": {"displayName": "Carlos Alcaraz", "shortName": "C. Alcaraz"}, "score": "0"}]}]}]}]}


def test_parse_team_scoreboard_and_sort():
    games = sc.sort_games(sc.parse_scoreboard(TEAM_BOARD, "NFL"))
    assert [g.state for g in games] == ["in", "pre", "post"]          # live first, finals last
    final = games[-1]
    assert (final.home_short, final.away_short, final.home_score, final.away_score) == ("BUF", "KC", "31", "28")
    pre = games[1]
    assert pre.home_score is None and pre.start == datetime(2026, 10, 19, 0, 20, tzinfo=timezone.utc)
    assert games[0].involves("packers") and games[0].involves("det") and not games[0].involves("bills")


def test_parse_tennis_groupings():
    (m,) = sc.parse_scoreboard(TENNIS_BOARD, "ATP")
    assert {m.home, m.away} == {"Jannik Sinner", "Carlos Alcaraz"} and m.state == "in"
    assert m.involves("sinner")
    alt = {"events": [{"groupings": [{"competitions": [{"id": "m2", "status": {"type": {"state": "post"}},
           "competitors": [{"athlete": {"displayName": "A"}, "linescores": [{"winner": True}, {"winner": True}]},
                           {"athlete": {"displayName": "B"}, "linescores": [{"winner": False}, {}]}]}]}]}]}
    (m2,) = sc.parse_scoreboard(alt, "WTA")
    assert (m2.away_score, m2.home_score) == ("2", "0")          # sets won when there's no score field


def test_fmt_score_lines():
    tz = ZoneInfo("America/New_York")
    live, pre, final = sc.sort_games(sc.parse_scoreboard(TEAM_BOARD, "NFL"))
    assert sc.fmt_score(live, tz) == "🔴 GB 10 – 14 DET · Q3 5:12"
    assert sc.fmt_score(final, tz) == "✅ KC 28 – 31 BUF · Final"
    assert sc.fmt_score(pre, tz) == "🕒 LAR @ SF · 8:20 PM"


def test_injury_parse_and_team_report():
    data = {"injuries": [{"displayName": "Buffalo Bills", "injuries": [
        {"status": "Questionable", "shortComment": "hamstring", "date": "x",
         "details": {"type": "Hamstring", "side": "Left"},
         "athlete": {"displayName": "Josh Allen", "position": {"abbreviation": "QB"},
                     "team": {"displayName": "Buffalo Bills"}}},
        {"status": "Out", "details": {"type": "Knee", "side": "Not Specified"},
         "athlete": {"displayName": "Some Guard", "position": {"abbreviation": "G"}}}]}]}
    rep = inj.parse(data)
    assert rep["josh allen"]["detail"] == "Left Hamstring" and rep["some guard"]["detail"] == "Knee"
    rows = inj.team_report(rep, "Buffalo Bills")
    assert [r["status"] for r in rows] == ["Out", "Questionable"]


def test_names():
    assert norm_name("D.J. Moore Jr.") == "dj moore"
    assert resolve("BUF") == resolve("bills") == resolve("buffalo") == "Buffalo Bills"
    assert resolve("niners") == "San Francisco 49ers" and resolve("la") == "Los Angeles Rams"
    assert resolve("new") is None                       # ambiguous


def test_chunk_lines_keeps_lines_whole():
    from scorebot.digest import chunk_lines
    lines = [f"line {i} " + "x" * 90 for i in range(30)]
    chunks = chunk_lines(lines)
    assert all(len(c) <= 1024 for c in chunks) and len(chunks) > 1
    assert "\n".join(chunks).split("\n") == lines
