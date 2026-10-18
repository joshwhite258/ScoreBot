"""Keeps the NFL models fresh and answers prediction questions for the bot."""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

import aiohttp

from ..names import norm_name
from . import nflverse as nv
from . import ratings as R
from .players import POSITION_STATS, PlayerModel, build_players, load_player_weeks

log = logging.getLogger(__name__)

SIDELINED = ("Out", "Doubtful", "Injured Reserve", "Suspension")


class ModelService:
    def __init__(self, data_dir: Path, session: aiohttp.ClientSession | None):
        self.dir = Path(data_dir)
        self.session = session
        self.state: R.ModelState | None = None
        self.built_at: float = 0.0
        self.games: list[dict] = []
        self.injuries: dict = {}                       # nflverse official report: (team, week) -> {qb: status}
        self.espn_injuries: dict[str, dict] = {}       # live ESPN feed: norm name -> status info
        self.players: PlayerModel | None = None
        self._lock = asyncio.Lock()

    @property
    def ready(self) -> bool:
        return self.state is not None

    async def refresh(self, force: bool = False) -> bool:
        """Download fresh data and rebuild. Returns True when a rebuild happened."""
        async with self._lock:
            if not force and self.ready and time.time() - self.built_at < 6 * 3600:
                return False
            await nv.refresh(self.session, self.dir)
            if not (self.dir / "games.csv").exists():
                log.warning("Model: no games.csv yet, skipping build")
                return False
            self.state, self.games, self.injuries, self.players = await asyncio.to_thread(self._build)
            self.built_at = time.time()
            bt = self.state.backtest
            if bt.get("accuracy"):
                log.info("Model rebuilt through %s. Holdout accuracy %.1f%% (Elo alone %.1f%%)",
                         self.state.last_completed, bt["accuracy"]["model"] * 100, bt["accuracy"]["elo"] * 100)
            return True

    def _build(self):
        games = nv.load_games(self.dir)
        state = R.build(games, nv.load_team_weeks(self.dir))
        season = nv.current_season()
        try:
            players = build_players(load_player_weeks(self.dir, [season - 1, season]), test_season=season - 1)
        except Exception:
            log.exception("Player model build failed")
            players = None
        return state, games, nv.load_injuries(self.dir, season), players

    # ------------------------------------------------------------- schedule
    def upcoming(self) -> list[dict]:
        """Unplayed games of the next week on the schedule, in kickoff order."""
        todo = [g for g in self.games if g["home_score"] is None and g["season"] == nv.current_season()]
        if not todo:
            return []
        wk = min(g["week"] for g in todo)
        return sorted((g for g in todo if g["week"] == wk), key=lambda g: (g["gameday"], g["game_id"]))

    def next_game(self, team_code: str) -> dict | None:
        for g in sorted((g for g in self.games if g["home_score"] is None),
                        key=lambda g: (g["season"], g["week"])):
            if team_code in (g["home"], g["away"]):
                return g
        return None

    # ------------------------------------------------------------------- QBs
    def starter(self, team: str, game: dict | None) -> tuple[str | None, str]:
        """(qb_id, source) for a team's upcoming game."""
        qb = self.state.qb
        if game:
            side = "home" if game["home"] == team else "away"
            if game.get(f"{side}_qb"):
                return game[f"{side}_qb"], "projected"
            usual = qb.usual(team)
            live = self.espn_injuries.get(norm_name(qb.names.get(usual, "") or "-"))
            if live and live.get("status") in SIDELINED:
                return (qb.backup(team) or R.REPLACEMENT), \
                    f"ESPN: {qb.names.get(usual, 'starter')} {live['status'].lower()}"
            status = self.injuries.get((team, game["week"]), {}).get(usual)
            if status in ("Out", "Doubtful"):
                return (qb.backup(team) or R.REPLACEMENT), \
                    f"injury report: {qb.names.get(usual, 'starter')} {status.lower()}"
        return qb.usual(team), "usual starter"

    def qb_name(self, qid: str | None) -> str:
        if not qid or qid == R.REPLACEMENT:
            return "replacement-level backup"
        return self.state.qb.names.get(qid, "unknown QB")

    # ---------------------------------------------------------------- queries
    def predict_game(self, game: dict) -> dict | None:
        """Full prediction for one nflverse schedule row."""
        if not self.ready:
            return None
        h, a = game["home"], game["away"]
        hq, hsrc = self.starter(h, game)
        aq, asrc = self.starter(a, game)
        pred = R.game_prediction(self.state, h, a, bool(game.get("neutral")), hq, aq)
        pred.update({"game_id": game["game_id"], "season": game["season"], "week": game["week"],
                     "gameday": game.get("gameday"), "home": h, "away": a,
                     "home_name": nv.CODE_TO_NAME.get(h, h), "away_name": nv.CODE_TO_NAME.get(a, a),
                     "home_qb_src": hsrc, "away_qb_src": asrc,
                     "home_qb_name": self.qb_name(hq), "away_qb_name": self.qb_name(aq),
                     "home_usual_name": self.qb_name(pred["home_usual_qb"]),
                     "away_usual_name": self.qb_name(pred["away_usual_qb"])})
        return pred

    def player_projection(self, name: str, stat: str | None = None) -> dict | None:
        """Next-game projection (with 80% range) for every relevant stat, or just `stat`."""
        if not self.players or not self.ready:
            return None
        pid = self.players.find(name)
        if not pid:
            return None
        info = self.players.info[pid]
        game = self.next_game(info["team"])
        opp, team_pts = None, None
        if game:
            opp = game["away"] if game["home"] == info["team"] else game["home"]
            pred = self.predict_game(game)
            team_pts = pred["home_pts"] if game["home"] == info["team"] else pred["away_pts"]
        stats = (stat,) if stat else POSITION_STATS.get(info["pos"], ("rec_yds", "receptions", "total_tds"))
        rows = []
        for s in stats:
            proj = self.players.project(pid, s, opp, team_pts)
            if proj is not None:
                lo, hi = self.players.interval(s, proj)
                rows.append({"stat": s, "proj": proj, "lo": lo, "hi": hi,
                             "opp_factor": self.players.opp_factor(opp, s) if opp else 1.0})
        return {"name": info["name"], "team": info["team"], "pos": info["pos"], "opp": opp,
                "home": bool(game and game["home"] == info["team"]), "week": game["week"] if game else None,
                "team_pts": team_pts, "rows": rows}

    def ratings(self) -> list[dict]:
        if not self.ready:
            return []
        out = []
        for code, name in nv.CODE_TO_NAME.items():
            o, d = self.state.epa.team(code)
            elo = self.state.elo.rating(code)
            # Power score: Elo + net EPA scaled to Elo points (0.1 EPA/play ~ 60 Elo)
            out.append({"code": code, "name": name, "elo": elo, "epa_off": o, "epa_def": d,
                        "epa_net": o - d, "power": elo + (o - d) * 600})
        return sorted(out, key=lambda t: -t["power"])
