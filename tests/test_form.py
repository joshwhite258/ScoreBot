"""/form: finding a player, reading his box-score lines, averages, and the command."""
import asyncio
import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("DISCORD_TOKEN", "x")

from scorebot import form as fm
from scorebot import live as lv
from scorebot import sports as sp
from scorebot.cogs import form as cf


def row(name, aid, team, *groups):
    return {"team": {"abbreviation": team, "displayName": team}, "statistics": [
        {"name": g[0], "keys": g[1], "labels": g[2], "athletes": [
            {"athlete": {"id": aid, "displayName": name}, "stats": g[3]}]} for g in groups]}


def box_of(*teams):
    return {"header": {"competitions": [{"status": {"type": {"completed": True}}}]},
            "boxscore": {"players": list(teams)}}


def player(summary, aid):
    return next(p for p in lv.parse_box(summary) if p["id"] == aid)


NBA = box_of(row("Jamal Murray", "1", "DEN", ("", ["minutes", "points", "rebounds", "assists", "threePointFieldGoalsMade-threePointFieldGoalsAttempted"],
                                                ["MIN", "PTS", "REB", "AST", "3PT"], ["34:30", "30", "3", "5", "4-9"])))
SKATER = box_of(row("Connor McDavid", "2", "EDM", ("forwards", ["goals", "assists", "shotsTotal", "timeOnIce"],
                                                    ["G", "A", "S", "TOI"], ["1", "2", "5", "21:18"])))
GOALIE = box_of(row("Stuart Skinner", "3", "EDM", ("goalies", ["saves", "shotsAgainst"], ["SV", "SA"], ["28", "30"])))
HITTER = box_of(row("Aaron Judge", "4", "NYY", ("batting", ["hits-atBats", "atBats", "hits", "RBIs", "homeRuns", "runs"],
                                                 ["H-AB", "AB", "H", "RBI", "HR", "R"], ["2-4", "4", "2", "3", "1", "2"])))
PITCHER = box_of(row("Gerrit Cole", "5", "NYY", ("pitching", ["fullInnings.partInnings", "strikeouts", "earnedRuns"],
                                                  ["IP", "K", "ER"], ["6.1", "9", "2"])))


def test_helpers():
    assert fm.clock("34:30") == 34.5 and fm.clock("31") == 31.0 and fm.clock(None) is None and fm.clock("x") is None
    assert fm.innings("6.1") == pytest.approx(6 + 1 / 3) and fm.innings("7") == 7 and fm.innings(None) is None
    assert fm.result_text({"home": True, "opp": "BOS", "team_score": 112.0, "opp_score": 104.0}) == "W 112–104 vs BOS"
    assert fm.result_text({"home": False, "opp": "NYR", "team_score": 2.0, "opp_score": 3.0}) == "L 2–3 @ NYR"
    assert fm.result_text({"home": False, "opp": "NYR", "team_score": None, "opp_score": None}) == "@ NYR"


def test_box_lines_per_sport():
    nba = fm.line_for("nba", player(NBA, "1"))
    assert nba["text"] == "30 pts, 3 reb, 5 ast, 4 3PM · 34 min"
    assert nba["vals"]["PTS"] == 30 and nba["kind"] == "nba"
    sk = fm.line_for("nhl", player(SKATER, "2"))
    assert sk["kind"] == "skater" and sk["text"].startswith("1 G, 2 A, 5 SOG")
    gl = fm.line_for("nhl", player(GOALIE, "3"))
    assert gl == {"kind": "goalie", "text": "28 saves on 30 shots", "vals": {"SV": 28, "SA": 30}}
    hit = fm.line_for("mlb", player(HITTER, "4"))
    assert hit["kind"] == "hitter" and hit["text"] == "2-4, 1 HR, 3 RBI, 2 R"
    pit = fm.line_for("mlb", player(PITCHER, "5"))
    assert pit["kind"] == "pitcher" and pit["text"] == "6.1 IP, 9 K, 2 ER"
    assert fm.line_for("tennis", player(NBA, "1")) is None


def test_did_not_play_is_skipped():
    dnp = box_of(row("Bench Guy", "9", "DEN", ("", ["minutes", "points"], ["MIN", "PTS"], ["0", "0"])))
    assert fm.line_for("nba", player(dnp, "9")) is None


def test_averages():
    nba = [fm.line_for("nba", player(NBA, "1"))] * 2
    assert fm.averages(nba).startswith("30.0 pts, 3.0 reb, 5.0 ast")
    hit = [fm.line_for("mlb", player(HITTER, "4"))] * 2
    assert fm.averages(hit) == "4-for-8 (.500), 2 HR, 6 RBI"
    pit = [fm.line_for("mlb", player(PITCHER, "5"))]
    assert fm.averages(pit) == "6.3 IP, 9.0 K, 2.0 ER"
    assert fm.averages([]) == ""


def test_name_matching():
    cands = [{"name": "Jamal Murray", "id": "1", "team": "DEN", "team_name": "Denver Nuggets"},
             {"name": "Keegan Murray", "id": "2", "team": "SAC", "team_name": "Sacramento Kings"},
             {"name": "Jamal Murray Jr.", "id": "3", "team": "XXX", "team_name": ""}]
    assert [c["id"] for c in fm.match_name(cands, "Jamal Murray")] == ["1", "3"]     # suffix ignored, so both are exact
    assert [c["id"] for c in fm.match_name(cands, "J. Murray")] == ["1", "3"]
    assert {c["id"] for c in fm.match_name(cands, "Murray")} == {"1", "2", "3"}
    assert fm.match_name(cands, "Nobody") == [] and fm.match_name(cands, "") == []


def test_parse_search_filters_by_sport():
    data = {"items": [
        {"displayName": "Jamal Murray", "id": 1, "league": "nba",
         "teamRelationships": [{"core": {"abbreviation": "DEN", "displayName": "Denver Nuggets"}}]},
        {"displayName": "Jamal Murray", "id": 2, "league": "mens-college-basketball"}]}
    assert fm.parse_search(data, "nba") == [{"name": "Jamal Murray", "id": "1", "team": "DEN",
                                             "team_name": "Denver Nuggets"}]


def test_parse_schedule_and_injuries():
    def ev(i, date, home_abbr, away_abbr, hs, as_, done=True):
        return {"id": i, "date": date, "competitions": [{"status": {"type": {"completed": done}}, "competitors": [
            {"homeAway": "home", "score": {"value": hs}, "team": {"abbreviation": home_abbr}},
            {"homeAway": "away", "score": {"value": as_}, "team": {"abbreviation": away_abbr}}]}]}
    games = fm.parse_schedule({"events": [ev("2", "2026-10-20T00:00Z", "DEN", "LAL", 100, 90),
                                          ev("1", "2026-10-18T00:00Z", "BOS", "DEN", 110, 105)]}, "den")
    assert [g["id"] for g in games] == ["1", "2"]
    assert games[0]["home"] is False and games[0]["opp"] == "BOS" and games[0]["team_score"] == 105
    inj = fm.parse_injuries({"injuries": [{"injuries": [{"athlete": {"displayName": "Jamal Murray"},
                                                         "status": "Questionable", "shortComment": "Knee"}]}]})
    assert inj["jamal murray"] == ("Questionable", "Knee")


# ---- recent_form, end to end with ESPN faked ----
@pytest.fixture(autouse=True)
def clear_caches():
    for c in (fm._sched, fm._boxes, fm._inj, fm._found):
        c.clear()


def fake_espn(monkeypatch, games, boxes, injuries=None):
    async def get(session, url, params=None, timeout=20):
        if url.endswith("/schedule"):
            if params["seasontype"] == 3:
                return {"events": []}
            return {"events": [{"id": gid, "date": f"2026-10-{10 + i:02d}T00:00Z", "competitions": [{
                "status": {"type": {"completed": done}}, "competitors": [
                    {"homeAway": "home", "score": {"value": 100}, "team": {"abbreviation": "DEN"}},
                    {"homeAway": "away", "score": {"value": 90}, "team": {"abbreviation": "LAL"}}]}]}
                for i, (gid, done) in enumerate(games)]}
        if url.endswith("/summary"):
            return boxes[params["event"]]
        if url.endswith("/injuries"):
            return injuries or {"injuries": []}
        raise AssertionError(url)
    monkeypatch.setattr(fm, "get_json", get)


def murray():
    return {"name": "Jamal Murray", "id": "1", "team": "DEN", "team_name": "Denver Nuggets"}


def test_recent_form_skips_missed_games_and_unfinished(monkeypatch):
    dnp = box_of(row("Someone Else", "7", "DEN", ("", ["minutes", "points"], ["MIN", "PTS"], ["20", "4"])))
    fake_espn(monkeypatch, [("a", True), ("b", True), ("c", True), ("d", False)],
              {"a": NBA, "b": dnp, "c": NBA},
              {"injuries": [{"injuries": [{"athlete": {"displayName": "Jamal Murray"}, "status": "Out",
                                           "shortComment": "Ankle"}]}]})
    data = asyncio.run(fm.recent_form(None, "nba", murray(), 5))
    assert len(data["games"]) == 2 and data["games"][0]["result"] == "W 100–90 vs LAL"
    assert data["games"][0]["date"] > data["games"][1]["date"]        # newest first
    assert data["average"].startswith("30.0 pts") and data["injury"] == ("Out", "Ankle")
    assert ("nba", "a") in fm._boxes                                  # finished boxes are kept


def test_recent_form_stops_after_n_games(monkeypatch):
    fake_espn(monkeypatch, [(c, True) for c in "abcdefg"], {c: NBA for c in "abcdefg"})
    assert len(asyncio.run(fm.recent_form(None, "nba", murray(), 3))["games"]) == 3


# ---- the command ----
def test_command_choices_exclude_football_and_soccer():
    assert [c.value for c in cf.FORM_CHOICES] == ["nba", "mlb", "nhl"]


def test_render_card():
    s = sp.get("nba")
    data = {"games": [{"date": "2026-10-18T23:00Z", "result": "W 100–90 vs LAL", "text": "30 pts, 3 reb, 5 ast"}],
            "average": "30.0 pts, 3.0 reb, 5.0 ast", "injury": ("Questionable", "Knee")}
    emb = cf.render(s, murray(), data, 5)
    assert emb.title == "🏀 Jamal Murray · DEN" and emb.description == "🟡 **Questionable** · Knee"
    assert emb.fields[0].name == "Last 1 game" and "W 100–90 vs LAL — 30 pts" in emb.fields[0].value
    assert emb.fields[1].value == "30.0 pts, 3.0 reb, 5.0 ast"
    out = cf.render(s, murray(), {"games": [], "average": "", "injury": ("Out", "")}, 5)
    assert out.description == "🔴 **Out**" and "No finished games" in out.fields[0].value


def test_command_flow(monkeypatch):
    async def go():
        sent = []

        async def send(*a, **k):
            sent.append((a, k))

        async def defer(**k):
            pass
        inter = SimpleNamespace(response=SimpleNamespace(defer=defer), followup=SimpleNamespace(send=send))
        cog = cf.Form(SimpleNamespace(session=None))
        cmd = cf.Form.form.callback
        choice = SimpleNamespace(value="nba")

        async def none(*a, **k):
            return []
        monkeypatch.setattr(fm, "find_athletes", none)
        await cmd(cog, inter, choice, "zzz", 5)
        assert "Couldn't find" in sent[-1][0][0]

        async def one(*a, **k):
            return [murray()]

        async def form(session, sport, athlete, n):
            return {"games": [], "average": "", "injury": None}
        monkeypatch.setattr(fm, "find_athletes", one)
        monkeypatch.setattr(fm, "recent_form", form)
        await cmd(cog, inter, choice, "Jamal Murray", 5)
        assert sent[-1][1]["embed"].title == "🏀 Jamal Murray · DEN"

        async def two(*a, **k):
            return [murray(), {"name": "Keegan Murray", "id": "2", "team": "SAC", "team_name": "Kings"}]
        monkeypatch.setattr(fm, "find_athletes", two)
        await cmd(cog, inter, choice, "Murray", 5)
        assert isinstance(sent[-1][1]["view"], cf.AthletePicker) and len(sent[-1][1]["view"].select.options) == 2

        async def boom(*a, **k):
            raise RuntimeError("down")
        monkeypatch.setattr(fm, "find_athletes", boom)
        await cmd(cog, inter, choice, "Murray", 5)
        assert "isn't answering" in sent[-1][0][0]
    asyncio.run(go())
