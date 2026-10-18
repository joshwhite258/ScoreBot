"""Download + parse nflverse data (free, public, updated during the season)."""
from __future__ import annotations

import csv
import logging
import time
from datetime import date
from pathlib import Path

import aiohttp

log = logging.getLogger(__name__)

GAMES_URL = "https://github.com/nflverse/nfldata/raw/master/data/games.csv"
TEAM_STATS_URL = ("https://github.com/nflverse/nflverse-data/releases/download/"
                  "stats_team/stats_team_week_{year}.csv")
PLAYER_STATS_URL = ("https://github.com/nflverse/nflverse-data/releases/download/"
                    "stats_player/stats_player_week_{year}.csv")
INJURIES_URL = ("https://github.com/nflverse/nflverse-data/releases/download/"
                "injuries/injuries_{year}.csv")
FIRST_SEASON = 1999

# Relocated franchises -> current nflverse code
FRANCHISE = {"OAK": "LV", "SD": "LAC", "STL": "LA"}

# nflverse code -> full team name (matches ESPN)
CODE_TO_NAME = {
    "ARI": "Arizona Cardinals", "ATL": "Atlanta Falcons", "BAL": "Baltimore Ravens",
    "BUF": "Buffalo Bills", "CAR": "Carolina Panthers", "CHI": "Chicago Bears",
    "CIN": "Cincinnati Bengals", "CLE": "Cleveland Browns", "DAL": "Dallas Cowboys",
    "DEN": "Denver Broncos", "DET": "Detroit Lions", "GB": "Green Bay Packers",
    "HOU": "Houston Texans", "IND": "Indianapolis Colts", "JAX": "Jacksonville Jaguars",
    "KC": "Kansas City Chiefs", "LV": "Las Vegas Raiders", "LAC": "Los Angeles Chargers",
    "LA": "Los Angeles Rams", "MIA": "Miami Dolphins", "MIN": "Minnesota Vikings",
    "NE": "New England Patriots", "NO": "New Orleans Saints", "NYG": "New York Giants",
    "NYJ": "New York Jets", "PHI": "Philadelphia Eagles", "PIT": "Pittsburgh Steelers",
    "SF": "San Francisco 49ers", "SEA": "Seattle Seahawks", "TB": "Tampa Bay Buccaneers",
    "TEN": "Tennessee Titans", "WAS": "Washington Commanders",
}
NAME_TO_CODE = {v: k for k, v in CODE_TO_NAME.items()}


def norm(code: str) -> str:
    return FRANCHISE.get(code, code)


def current_season(today: date | None = None) -> int:
    d = today or date.today()
    return d.year if d.month >= 3 else d.year - 1


# ----------------------------------------------------------------- download ---
async def refresh(session: aiohttp.ClientSession, folder: Path, max_age_h: float = 12) -> None:
    """Fetch games.csv and team-week stats. Past seasons download once; the current
    season and games.csv refresh when older than max_age_h."""
    folder.mkdir(parents=True, exist_ok=True)
    season = current_season()
    targets = [(GAMES_URL, folder / "games.csv", True),
               (INJURIES_URL.format(year=season), folder / f"injuries_{season}.csv", True)]
    for y in (season - 1, season):          # player stats: ~9 MB per season, keep two
        targets.append((PLAYER_STATS_URL.format(year=y), folder / f"stats_player_week_{y}.csv", y == season))
    for y in range(FIRST_SEASON, season + 1):
        targets.append((TEAM_STATS_URL.format(year=y), folder / f"stats_team_week_{y}.csv", y == season))
    for url, path, volatile in targets:
        if path.exists() and (not volatile or time.time() - path.stat().st_mtime < max_age_h * 3600):
            continue
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as r:
                if r.status == 404:
                    continue          # e.g. current-season file before week 1
                r.raise_for_status()
                body = await r.read()
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(body)
            tmp.replace(path)
            log.info("nflverse: downloaded %s (%d KB)", path.name, len(body) // 1024)
        except Exception as e:
            log.warning("nflverse: failed %s: %s", url, e)


# -------------------------------------------------------------------- parse ---
def _int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def load_games(folder: Path) -> list[dict]:
    out = []
    with open(folder / "games.csv", newline="") as f:
        for r in csv.DictReader(f):
            out.append({
                "game_id": r["game_id"], "season": int(r["season"]), "week": int(r["week"]),
                "game_type": r["game_type"], "gameday": r["gameday"],
                "home": norm(r["home_team"]), "away": norm(r["away_team"]),
                "home_score": _int(r["home_score"]), "away_score": _int(r["away_score"]),
                "neutral": r.get("location") == "Neutral", "espn": r.get("espn") or None,
                "home_qb": r.get("home_qb_id") or None, "away_qb": r.get("away_qb_id") or None,
                "home_qb_name": r.get("home_qb_name") or None, "away_qb_name": r.get("away_qb_name") or None,
            })
    return out


def load_team_weeks(folder: Path) -> dict[tuple[str, str], dict]:
    out = {}
    for path in sorted(folder.glob("stats_team_week_*.csv")):
        with open(path, newline="") as f:
            for r in csv.DictReader(f):
                plays = (_int(r.get("attempts")) or 0) + (_int(r.get("sacks_suffered")) or 0) \
                    + (_int(r.get("carries")) or 0)
                pass_epa = _float(r.get("passing_epa"))
                dropbacks = (_int(r.get("attempts")) or 0) + (_int(r.get("sacks_suffered")) or 0)
                out[(r["game_id"], norm(r["team"]))] = {
                    "epa": pass_epa + _float(r.get("rushing_epa")), "plays": plays,
                    "pass_epa": pass_epa, "dropbacks": dropbacks}
    return out


def load_injuries(folder: Path, season: int) -> dict[tuple[str, int], dict[str, str]]:
    """{(team, week): {gsis_id: report_status}} for QBs on the official injury report."""
    path = folder / f"injuries_{season}.csv"
    out: dict = {}
    if not path.exists():
        return out
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            if r.get("position") != "QB" or not r.get("report_status"):
                continue
            wk = _int(r.get("week"))
            if wk is None:
                continue
            out.setdefault((norm(r["team"]), wk), {})[r["gsis_id"]] = r["report_status"]
    return out
