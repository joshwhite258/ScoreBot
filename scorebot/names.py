"""Name normalisation and NFL team lookups shared by the feeds and commands."""
from __future__ import annotations

import re
import unicodedata

TEAMS = {
    "Arizona Cardinals": "ARI", "Atlanta Falcons": "ATL", "Baltimore Ravens": "BAL",
    "Buffalo Bills": "BUF", "Carolina Panthers": "CAR", "Chicago Bears": "CHI",
    "Cincinnati Bengals": "CIN", "Cleveland Browns": "CLE", "Dallas Cowboys": "DAL",
    "Denver Broncos": "DEN", "Detroit Lions": "DET", "Green Bay Packers": "GB",
    "Houston Texans": "HOU", "Indianapolis Colts": "IND", "Jacksonville Jaguars": "JAX",
    "Kansas City Chiefs": "KC", "Las Vegas Raiders": "LV", "Los Angeles Chargers": "LAC",
    "Los Angeles Rams": "LAR", "Miami Dolphins": "MIA", "Minnesota Vikings": "MIN",
    "New England Patriots": "NE", "New Orleans Saints": "NO", "New York Giants": "NYG",
    "New York Jets": "NYJ", "Philadelphia Eagles": "PHI", "Pittsburgh Steelers": "PIT",
    "San Francisco 49ers": "SF", "Seattle Seahawks": "SEA", "Tampa Bay Buccaneers": "TB",
    "Tennessee Titans": "TEN", "Washington Commanders": "WAS",
}
ABBR_TO_NAME = {v: k for k, v in TEAMS.items()}


def norm_name(name: str) -> str:
    """'Kenneth Walker III' -> 'kenneth walker'; 'D.J. Moore' -> 'dj moore'."""
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[.'’]", "", s)
    s = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b", "", s)
    return re.sub(r"\s+", " ", s).strip()


def resolve(query: str) -> str | None:
    """Team text -> full NFL team name. Accepts 'BUF', 'LA'/'LAR', 'bills', 'buffalo', 'niners'."""
    q = (query or "").strip().lower()
    if not q:
        return None
    for code, name in ABBR_TO_NAME.items():
        if q == code.lower():
            return name
    if q in ("la", "rams"):
        return "Los Angeles Rams"
    if q == "niners":
        return "San Francisco 49ers"
    for name in TEAMS:
        if q == name.lower() or q == name.split()[-1].lower():
            return name
    starts = [n for n in TEAMS if any(w.lower().startswith(q) for w in n.split())] if len(q) >= 3 else []
    return starts[0] if len(starts) == 1 else None
