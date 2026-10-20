"""/live: ESPN parsing, box-score standouts, the card, the update loop, and college football."""
import asyncio
import os
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import discord
import pytest

os.environ.setdefault("DISCORD_TOKEN", "x")

from scorebot import live as lv
from scorebot import scores as sc
from scorebot import sports as sp
from scorebot.cogs import live as cl
from scorebot.cogs.live import Live, StopView, render
from scorebot.cogs.scores import SPORT_CHOICES

TZ = ZoneInfo("America/New_York")


def ev(eid, home, away, ha, aa, hs, as_, state="in", period=3, clock="8:12", detail="8:12 - 3rd",
       date="2026-10-18T17:00Z", name="STATUS_IN_PROGRESS"):
    return {"id": eid, "date": date,
            "status": {"period": period, "displayClock": clock,
                       "type": {"state": state, "shortDetail": detail, "name": name}},
            "competitions": [{"competitors": [
                {"homeAway": "home", "score": str(hs), "team": {"displayName": home, "abbreviation": ha}},
                {"homeAway": "away", "score": str(as_), "team": {"displayName": away, "abbreviation": aa}}]}]}


def game(state="in", hs=14, as_=17, **kw):
    detail = {"in": "8:12 - 3rd", "post": "Final", "pre": "1:00 PM EDT"}[state]
    [g] = lv.parse_events("nfl", {"events": [ev("1", "Buffalo Bills", "Kansas City Chiefs", "BUF", "KC", hs, as_,
                                                state=state, detail=detail, **kw)]}, "football/nfl", "NFL")
    return g


def athletes(*rows):
    return [{"athlete": {"id": str(abs(hash(n)) % 10**8), "displayName": n}, "stats": s} for n, s in rows]


def team_box(abbr, *groups):
    return {"team": {"abbreviation": abbr, "displayName": abbr}, "statistics": list(groups)}


FOOTBALL_BOX = {"boxscore": {"players": [
    team_box("KC",
             {"name": "passing", "keys": ["completions/passingAttempts", "passingYards", "passingTouchdowns",
                                          "interceptions"], "labels": ["C/ATT", "YDS", "TD", "INT"],
              "athletes": [{"athlete": {"id": "15", "displayName": "Patrick Mahomes", "jersey": "15"},
                            "stats": ["22/31", "254", "2", "1"]}]},
             {"name": "rushing", "keys": ["rushingAttempts", "rushingYards", "rushingTouchdowns"],
              "labels": ["CAR", "YDS", "TD"],
              "athletes": [{"athlete": {"id": "10", "displayName": "Isiah Pacheco"}, "stats": ["12", "61", "0"]},
                           {"athlete": {"id": "15", "displayName": "Patrick Mahomes"}, "stats": ["3", "14", "0"]}]},
             {"name": "receiving", "keys": ["receptions", "receivingYards", "receivingTouchdowns"],
              "labels": ["REC", "YDS", "TD"],
              "athletes": [{"athlete": {"id": "87", "displayName": "Travis Kelce"}, "stats": ["7", "83", "1"]}]}),
    team_box("BUF",
             {"name": "passing", "keys": ["completions/passingAttempts", "passingYards", "passingTouchdowns",
                                          "interceptions"], "labels": ["C/ATT", "YDS", "TD", "INT"],
              "athletes": [{"athlete": {"id": "17", "displayName": "Josh Allen"}, "stats": ["18/27", "201", "1", "0"]}]},
             {"name": "rushing", "keys": ["rushingAttempts", "rushingYards", "rushingTouchdowns"],
              "labels": ["CAR", "YDS", "TD"],
              "athletes": [{"athlete": {"id": "4", "displayName": "James Cook"}, "stats": ["15", "88", "1"]}]})]}}


# ------------------------------------------------------------- parsing ---
def test_elapsed():
    assert lv.elapsed("nfl", "in", 3, "7:30", "7:30 - 3rd") == pytest.approx(0.625)
    assert lv.elapsed("ncaaf", "in", 2, "0:00", "Halftime") == pytest.approx(0.5)
    assert lv.elapsed("nfl", "in", 5, "5:00", "5:00 - OT") == 1.0
    assert lv.elapsed("nhl", "in", 2, "10:00", "10:00 - 2nd") == pytest.approx(0.5)
    assert lv.elapsed("nba", "in", 3, "6:00", "6:00 - 3rd") == pytest.approx(0.625)
    assert lv.elapsed("mlb", "in", 7, "", "Top 7th") == pytest.approx(6.25 / 9)
    assert lv.elapsed("soccer", "in", 2, "67'", "67'") == pytest.approx(67 / 90)
    assert lv.elapsed("nfl", "pre", 0, "", "") == 0.0 and lv.elapsed("nfl", "post", 4, "0:00", "Final") == 1.0
    assert lv.elapsed("tennis", "in", 1, "", "") is None


def test_parse_events_and_matching():
    g = game()
    assert (g.home_abbr, g.away_abbr, g.home_score, g.away_score, g.state) == ("BUF", "KC", 14, 17, "in")
    assert g.frac == pytest.approx((2 * 15 + 15 - 8.2) / 60) and g.league == "NFL" and g.path == "football/nfl"
    assert g.involves("chiefs") and g.involves("buf") and g.involves("") and not g.involves("packers")
    postponed = lv.parse_events("nfl", {"events": [ev("2", "A", "B", "A", "B", 0, 0, state="pre",
                                                      name="STATUS_POSTPONED")]})
    assert postponed[0].canceled


def test_football_possession_and_down():
    data = ev("300", "Kansas City Chiefs", "Buffalo Bills", "KC", "BUF", 20, 10)
    comp = data["competitions"][0]
    comp["competitors"][0]["id"], comp["competitors"][1]["id"] = "12", "2"
    comp["situation"] = {"possession": "2", "downDistanceText": "2nd & 7 at KC 18", "isRedZone": True}
    g = lv.parse_events("nfl", {"events": [data]})[0]
    assert g.possession == "away" and lv.score_line(g) == "🏈 BUF 10 – 20 KC"
    assert lv.down_text(g) == "🚨 2nd & 7 at KC 18"

    comp["situation"] = {"possession": "12", "downDistanceText": "1st & 10 at KC 25"}
    g = lv.parse_events("nfl", {"events": [data]})[0]
    assert lv.score_line(g) == "BUF 10 – 20 KC 🏈" and lv.down_text(g) == "1st & 10 at KC 25"

    del comp["situation"]                                           # halftime / no data: plain score
    g = lv.parse_events("nfl", {"events": [data]})[0]
    assert g.possession is None and lv.score_line(g) == "BUF 10 – 20 KC" and lv.down_text(g) == ""


def test_status_text_and_dates():
    kickoff = int(datetime(2026, 10, 18, 17, 0, tzinfo=timezone.utc).timestamp())
    assert lv.status_text(game()) == "🔴 LIVE · 8:12 - 3rd"
    assert lv.status_text(game("post")) == "✅ Final"
    assert lv.status_text(game("pre")) == f"🕒 Starts <t:{kickoff}:t> (<t:{kickoff}:R>)"
    assert lv.score_line(game("pre")) == "KC @ BUF"
    assert lv.status_text(lv.parse_events("nfl", {"events": [ev("2", "A", "B", "A", "B", 0, 0, state="pre",
                                                               name="STATUS_CANCELED")]})[0]).startswith("🚫")
    # 8:20 PM Eastern on the 18th is already the 19th in UTC; ESPN files it under the 18th
    assert lv.espn_date("2026-10-19T00:20Z", datetime.now(TZ)) == "20261018"
    assert lv.espn_date("garbage", datetime(2026, 10, 20, 9, 0, tzinfo=TZ)) == "20261020"
    order = lv.sort_games([game("post"), game("pre"), game("in")])
    assert [g.state for g in order] == ["in", "pre", "post"]


# ------------------------------------------------------------- leaders ---
def leaders(sport, summary, team):
    return lv.leaders(sport, lv.parse_box(summary), team)


def test_football_leaders():
    assert leaders("nfl", FOOTBALL_BOX, "KC") == [
        "P. Mahomes 22/31, 254 yds, 2 TD, 1 INT", "I. Pacheco 12 car, 61 yds", "T. Kelce 7 rec, 83 yds, 1 TD"]
    assert leaders("ncaaf", FOOTBALL_BOX, "BUF") == ["J. Allen 18/27, 201 yds, 1 TD", "J. Cook 15 car, 88 yds, 1 TD"]
    assert leaders("nfl", FOOTBALL_BOX, "DET") == [] and leaders("nfl", {}, "KC") == []


def test_basketball_leaders():
    box = {"boxscore": {"players": [team_box("DEN", {
        "keys": ["minutes", "points", "rebounds", "assists"], "labels": ["MIN", "PTS", "REB", "AST"],
        "athletes": athletes(("Nikola Jokić", ["30", "24", "11", "8"]), ("Jamal Murray", ["31", "30", "3", "5"]),
                             ("Bench Guy", ["4", "2", "0", "0"]))})]}}
    assert leaders("nba", box, "DEN") == ["J. Murray 30 pts, 3 reb, 5 ast", "N. Jokić 24 pts, 11 reb, 8 ast"]


def test_hockey_leaders():
    box = {"boxscore": {"players": [team_box(
        "EDM",
        {"name": "forwards", "keys": ["goals", "assists", "shotsTotal"], "labels": ["G", "A", "S"],
         "athletes": athletes(("Connor McDavid", ["1", "2", "5"]), ("Zach Hyman", ["0", "0", "2"]))},
        {"name": "goalies", "keys": ["saves", "shotsAgainst"], "labels": ["SV", "SA"],
         "athletes": athletes(("Stuart Skinner", ["28", "30"]))})]}}
    assert leaders("nhl", box, "EDM") == ["C. McDavid 1 G, 2 A, 5 SOG", "S. Skinner 28 saves"]


def test_baseball_leaders():
    box = {"boxscore": {"players": [team_box(
        "NYY",
        {"type": "batting", "keys": ["hits-atBats", "atBats", "hits", "RBIs", "homeRuns"],
         "labels": ["H-AB", "AB", "H", "RBI", "HR"],
         "athletes": athletes(("Aaron Judge", ["2-4", "4", "2", "3", "1"]), ("Pinch Hitter", ["0-1", "1", "0", "0", "0"]))},
        {"type": "pitching", "keys": ["fullInnings.partInnings", "strikeouts"], "labels": ["IP", "K"],
         "athletes": athletes(("Gerrit Cole", ["6.1", "9"]))})]}}
    assert leaders("mlb", box, "NYY") == ["A. Judge 2 H, 1 HR, 3 RBI", "G. Cole 6.1 IP, 9 K"]


def test_soccer_leaders():
    box = {"boxscore": {"players": [team_box("ARS", {
        "keys": ["totalGoals", "goalAssists"], "labels": ["G", "A"],
        "athletes": athletes(("Bukayo Saka", ["1", "1"]), ("Martin Odegaard", ["0", "1"]),
                             ("Declan Rice", ["0", "0"]))})]}}
    assert leaders("soccer", box, "ARS") == ["B. Saka 1 G, 1 A", "M. Odegaard 1 A"]
    assert lv.leaders("tennis", lv.parse_box(box), "ARS") == []          # no box score for tennis


# ------------------------------------------------------- the card (embed) ---
def test_render_live_card_with_leaders():
    emb = render(sp.NFL, game(), lv.parse_box(FOOTBALL_BOX))
    assert emb.title == "🏈 Kansas City Chiefs @ Buffalo Bills · NFL"
    assert emb.description == "## KC 17 – 14 BUF\n🔴 LIVE · 8:12 - 3rd"
    assert [f.name for f in emb.fields] == ["KC", "BUF"] and "P. Mahomes" in emb.fields[0].value
    assert emb.footer.text == "Updates every 30s · source: ESPN" and emb.color.value == cl.COLORS["in"]


def test_render_other_states():
    pre = render(sp.NFL, game("pre", 0, 0), [])
    assert pre.fields == [] and "Starts" in pre.description and pre.color.value == cl.COLORS["pre"]
    nobox = render(sp.NFL, game(), [])
    assert nobox.fields[0].name == "Box score" and "haven't posted" in nobox.fields[0].value
    final = render(sp.NFL, game("post"), lv.parse_box(FOOTBALL_BOX))
    assert final.footer.text == "Final" and final.color.value == cl.COLORS["post"]
    assert render(sp.NFL, game(), [], stale=True).footer.text.startswith("⚠️")
    assert render(sp.NFL, game(), [], footer="Stopped · no longer updating").footer.text.startswith("Stopped")


# ----------------------------------------------------- college football ---
def test_college_football_is_registered_everywhere():
    assert sp.get("ncaaf") is sp.NCAAF and sp.NCAAF.emoji == "🏈"
    assert "ncaaf" in [c.value for c in SPORT_CHOICES]
    assert [c.value for c in cl.LIVE_CHOICES] == ["nfl", "ncaaf", "nba", "mlb", "nhl", "soccer"]   # no tennis


def test_college_football_asks_for_every_fbs_game(monkeypatch):
    seen = []

    async def fake(session, url, params=None, timeout=20):
        seen.append((url, params))
        return {"events": []}
    monkeypatch.setattr(sc, "get_json", fake)
    asyncio.run(sc.fetch_day(None, sp.NCAAF, TZ))
    url, params = seen[0]
    assert url.endswith("/football/college-football/scoreboard")
    assert params["groups"] == "80" and params["limit"] == "400" and "dates" in params
    asyncio.run(sc.fetch_day(None, sp.NFL, TZ))
    assert set(seen[1][1]) == {"dates"}                                   # other sports are unchanged


def test_live_scoreboard_paths_and_soccer_fallback(monkeypatch):
    calls = []

    async def fake(session, url, params=None, timeout=20):
        calls.append((url, params))
        if "uefa.champions" in url:
            raise RuntimeError("boom")
        return {"events": [ev(url[-30:], "Home", "Away", "HOM", "AWY", 1, 0)]}
    monkeypatch.setattr(lv, "get_json", fake)
    games = asyncio.run(lv.scoreboard(None, sp.SOCCER, "20261018"))
    assert len(games) == 2 and {g.path for g in games} == {"soccer/eng.1", "soccer/usa.1"}   # one league failed
    assert {g.league for g in games} == {"Premier League", "MLS"}
    cfb = asyncio.run(lv.scoreboard(None, sp.NCAAF, "20261018"))
    assert cfb[0].path == "football/college-football" and calls[-1][1]["groups"] == "80"
    with pytest.raises(RuntimeError):                                      # a single-league sport doesn't hide errors
        async def bad(session, url, params=None, timeout=20):
            raise RuntimeError("down")
        monkeypatch.setattr(lv, "get_json", bad)
        asyncio.run(lv.scoreboard(None, sp.NFL, "20261018"))


# ---------------------------------------------------------- the cog ---
class FakeMsg:
    def __init__(self, fail=None):
        self.id, self.edits, self.fail = 77, [], fail

    async def edit(self, **kw):
        if self.fail:
            raise self.fail
        self.edits.append(kw)


def make_cog(monkeypatch, boards, box=None, fast=True):
    """A Live cog whose scoreboard pops the next list in `boards` (a list item that is an Exception is raised)."""
    if fast:
        for name in ("REFRESH_S", "PRE_REFRESH_S", "BOARD_TTL_S"):
            monkeypatch.setattr(cl, name, 0)

    async def fake_board(session, sport, date):
        nxt = boards.pop(0) if len(boards) > 1 else boards[0]
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    async def fake_box(session, path, espn_id):
        return lv.parse_box(box if box is not None else FOOTBALL_BOX)
    monkeypatch.setattr(lv, "scoreboard", fake_board)
    monkeypatch.setattr(lv, "box", fake_box)
    return Live(SimpleNamespace(cfg=SimpleNamespace(tz=TZ), session=object()))


def test_card_updates_until_final_then_removes_the_button(monkeypatch):
    async def go():
        cog = make_cog(monkeypatch, [[game(hs=14, as_=17)], [game(hs=14, as_=24)], [game("post", 14, 24)]])
        msg, view = FakeMsg(), StopView(cl.Live(SimpleNamespace()), 1)
        cog.cards[msg.id] = asyncio.current_task()
        await cog.run(msg, sp.NFL, game(), [], view)
        assert len(msg.edits) == 3
        assert msg.edits[0]["view"] is view and msg.edits[1]["view"] is view
        assert "24" in msg.edits[1]["embed"].description
        assert msg.edits[2]["view"] is None and msg.edits[2]["embed"].footer.text == "Final"
        assert msg.id not in cog.cards                                     # the loop cleans up after itself
    asyncio.run(go())


def test_deleted_card_stops_the_loop(monkeypatch):
    async def go():
        boards = [[game()]]
        cog = make_cog(monkeypatch, boards)
        msg = FakeMsg(fail=discord.NotFound(MagicMock(status=404, reason="gone"), "Unknown Message"))
        await cog.run(msg, sp.NFL, game(), [], StopView(cog, 1))          # returns instead of looping forever
        assert msg.edits == [] and msg.id not in cog.cards
    asyncio.run(go())


def test_espn_failures_show_stale_then_recover(monkeypatch):
    async def go():
        cog = make_cog(monkeypatch, [RuntimeError("down"), [game("post", 14, 24)]])
        msg = FakeMsg()
        await cog.run(msg, sp.NFL, game(), [], StopView(cog, 1))
        assert msg.edits[0]["embed"].footer.text.startswith("⚠️") and msg.edits[0]["view"] is not None
        assert msg.edits[1]["embed"].footer.text == "Final"
    asyncio.run(go())


def test_card_gives_up_after_repeated_failures(monkeypatch):
    async def go():
        monkeypatch.setattr(cl, "MAX_FAILS", 3)
        cog = make_cog(monkeypatch, [RuntimeError("down")])
        msg = FakeMsg()
        await cog.run(msg, sp.NFL, game(), [], StopView(cog, 1))
        assert len(msg.edits) == 3                                         # two stale edits, then the stop notice
        assert msg.edits[-1]["embed"].footer.text == "Stopped · no longer updating" and msg.edits[-1]["view"] is None
    asyncio.run(go())


def test_find_games_filters_by_team_and_drops_yesterdays_finals(monkeypatch):
    from datetime import timedelta
    today = datetime.now(TZ)
    y = (today - timedelta(days=1)).strftime("%Y%m%d")
    t = today.strftime("%Y%m%d")
    by_date = {
        y: [game("post"), lv.parse_events("nfl", {"events": [ev("9", "Lions", "Packers", "DET", "GB", 3, 0,
                                                                date="2026-10-18T17:00Z")]}, "football/nfl")[0]],
        t: [game("pre", 0, 0), lv.parse_events("nfl", {"events": [ev("5", "Rams", "49ers", "LAR", "SF", 0, 0,
                                                                     state="pre", date="2026-10-20T00:20Z")]},
                                               "football/nfl")[0]],
    }

    async def fake_board(session, sport, date):
        return by_date[date]
    monkeypatch.setattr(lv, "scoreboard", fake_board)
    cog = Live(SimpleNamespace(cfg=SimpleNamespace(tz=TZ), session=object()))
    everything = asyncio.run(cog.find_games(sp.NFL, None))
    assert [g.espn_id for g in everything] == ["9", "1", "5"]            # live first; yesterday's final is gone
    assert [g.espn_id for g in asyncio.run(cog.find_games(sp.NFL, "rams"))] == ["5"]
    assert asyncio.run(cog.find_games(sp.NFL, "jaguars")) == []


def test_game_picker_lists_games_with_status():
    async def go():
        cog = Live(SimpleNamespace(cfg=SimpleNamespace(tz=TZ), session=object()))
        games = [game(), game("pre", 0, 0), game("post", 1, 2)]
        for i, g in enumerate(games):
            g.espn_id = str(i)
        picker = cl.GamePicker(cog, sp.NFL, games)
        labels = [o.label for o in picker.select.options]
        assert labels[0] == "KC @ BUF · 8:12 - 3rd" and labels[2] == "KC @ BUF · Final" and "KC @ BUF · " in labels[1]
        assert [o.value for o in picker.select.options] == ["football/nfl|0", "football/nfl|1", "football/nfl|2"]
        assert [o.description for o in picker.select.options] == ["NFL"] * 3
    asyncio.run(go())


def test_stop_button_only_for_starter_or_moderators():
    async def go():
        cog = Live(SimpleNamespace(cfg=SimpleNamespace(tz=TZ), session=object()))
        view = StopView(cog, starter_id=5)
        sent, edited = [], []

        async def send_message(*a, **k):
            sent.append((a, k))

        async def edit_message(**k):
            edited.append(k)

        def inter(uid, manage):
            return SimpleNamespace(user=SimpleNamespace(id=uid), permissions=SimpleNamespace(manage_messages=manage),
                                   message=SimpleNamespace(id=99, embeds=[discord.Embed(description="x")]),
                                   response=SimpleNamespace(send_message=send_message, edit_message=edit_message))
        task = asyncio.create_task(asyncio.sleep(60))
        cog.cards[99] = task
        await view.stop_button.callback(inter(6, False))                   # a stranger
        assert sent and not edited and not task.cancelled() and 99 in cog.cards
        await view.stop_button.callback(inter(5, False))                   # whoever started it
        await asyncio.sleep(0)
        assert task.cancelled() and 99 not in cog.cards
        assert edited[0]["view"] is None and edited[0]["embed"].footer.text == "Stopped · no longer updating"
    asyncio.run(go())
