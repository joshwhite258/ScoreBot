"""Live game state for /live, from ESPN's free scoreboard and box score (no API key).

  elapsed():      share of regulation played (0..1), None when unknown
  parse_events(): scoreboard JSON -> LiveGame: score, clock, football possession, down and distance
  parse_box():    summary JSON -> every player's stats, keyed by ESPN stat key and short label
  leaders():      each team's standout players, per sport
  score_line() / status_text() / down_text(): the text a live card shows
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import aiohttp

from . import sports as sp
from .espn import EspnBlocked, get_json

# Sports with a box score ESPN serves live (tennis has none).
LIVE_SPORTS = ("nfl", "ncaaf", "nba", "mlb", "nhl", "soccer")
# Count-down clocks: (regulation periods, minutes per period)
CLOCK = {"nfl": (4, 15), "ncaaf": (4, 15), "nhl": (3, 20), "nba": (4, 12)}
ET = ZoneInfo("America/New_York")      # ESPN files each game under its US Eastern date
FOOTBALL = ("nfl", "ncaaf")


@dataclass
class LiveGame:
    espn_id: str
    state: str                     # pre | in | post
    detail: str                    # "8:12 - 3rd", "Top 7th", "Final"
    home: str
    away: str
    home_abbr: str
    away_abbr: str
    home_score: int
    away_score: int
    date: str
    frac: float | None = None      # share of regulation played
    path: str = ""                 # ESPN path it came from, e.g. "soccer/eng.1"
    league: str = ""               # "NFL", "Premier League"
    canceled: bool = False
    possession: str | None = None  # "home" | "away" while a football team has the ball
    down: str = ""                 # "2nd & 7 at KC 35"
    red_zone: bool = False

    def involves(self, query: str) -> bool:
        q = (query or "").strip().lower()
        return not q or any(q in s.lower() for s in (self.home, self.away, self.home_abbr, self.away_abbr))


# -------------------------------------------------------------- parsing ---
def _clock_minutes(clock: str | None) -> float | None:
    if not clock:
        return None
    m = re.match(r"^\s*(\d+):(\d+(?:\.\d+)?)\s*$", str(clock))
    if m:
        return int(m.group(1)) + float(m.group(2)) / 60
    try:
        return float(clock) / 60          # "45.2" seconds near the end of a period
    except ValueError:
        return None


def elapsed(sport: str, state: str, period: int, clock: str | None, detail: str) -> float | None:
    """Share of regulation played (0..1). None when unknown."""
    if state == "pre":
        return 0.0
    if state == "post":
        return 1.0
    d = (detail or "").lower()
    if sport == "soccer":           # clock counts up: "67'", "45'+2'"; HT = half time
        if d in ("ht", "halftime", "half time"):
            return 0.5
        m = re.match(r"\s*(\d+)", str(clock or ""))
        if not m:
            return None
        return min(1.0, int(m.group(1)) / 90)
    if sport == "mlb":
        if period <= 0:
            return None
        half = 0.25 if d.startswith("top") else 0.5 if d.startswith("mid") else \
            0.75 if d.startswith(("bot", "bottom")) else 1.0 if d.startswith("end") else 0.5
        return min(1.0, ((period - 1) + half) / 9)
    if sport in CLOCK:
        n, per = CLOCK[sport]
        if "half" in d and sport in ("nfl", "ncaaf", "nba"):
            return 0.5
        if period > n:
            return 1.0
        left = _clock_minutes(clock)
        if period <= 0 or left is None:
            return None
        if "end" in d:
            left = 0.0
        return min(1.0, max(0.0, ((period - 1) * per + (per - left)) / (n * per)))
    return None


def parse_events(sport: str, data: dict, path: str = "", league: str = "") -> list[LiveGame]:
    out = []
    for ev in data.get("events", []):
        comp = (ev.get("competitions") or [{}])[0]
        teams = {c.get("homeAway"): c for c in comp.get("competitors", [])}
        if "home" not in teams or "away" not in teams:
            continue
        st = ev.get("status") or comp.get("status") or {}
        typ = st.get("type") or {}
        state = typ.get("state") or "pre"
        detail = typ.get("shortDetail") or typ.get("detail") or ""
        h, a = teams["home"], teams["away"]
        name = typ.get("name") or ""
        sit = comp.get("situation") or {}
        poss_id = str(sit.get("possession") or "")
        poss = None
        if poss_id and state == "in":
            for side, c in (("home", h), ("away", a)):
                if poss_id in (str(c.get("id") or ""), str((c.get("team") or {}).get("id") or "")):
                    poss = side
        out.append(LiveGame(
            espn_id=str(ev.get("id")), state=state, detail=detail,
            home=h["team"].get("displayName", ""), away=a["team"].get("displayName", ""),
            home_abbr=h["team"].get("abbreviation", ""), away_abbr=a["team"].get("abbreviation", ""),
            home_score=int(float(h.get("score") or 0)), away_score=int(float(a.get("score") or 0)),
            date=ev.get("date", ""),
            frac=elapsed(sport, state, int(st.get("period") or 0), st.get("displayClock"), detail),
            canceled=name in ("STATUS_POSTPONED", "STATUS_CANCELED", "STATUS_CANCELLED", "STATUS_FORFEIT"),
            path=path, league=league, possession=poss,
            down=(sit.get("downDistanceText") or sit.get("shortDownDistanceText") or "") if poss else "",
            red_zone=bool(sit.get("isRedZone")) and poss is not None))
    return out


def _num(v) -> float | None:
    s = str(v)
    if "-" in s[1:] or "/" in s:                  # "2-4" (H-AB), "23/33" -> first number
        s = re.split(r"[-/]", s.lstrip("-"), maxsplit=1)[0]
    try:
        return float(s)
    except ValueError:
        return None


def parse_box(summary: dict) -> list[dict]:
    """[{id, name, team, jersey, stats{group:{key:v}}, flat{key-or-label:v}, raw{label:text}}]
    from an ESPN game summary."""
    by_id: dict[str, dict] = {}
    for team in summary.get("boxscore", {}).get("players", []):
        tabbr = (team.get("team") or {}).get("abbreviation", "")
        tname = (team.get("team") or {}).get("displayName", "")
        for grp in team.get("statistics", []):
            gname = (grp.get("name") or grp.get("type") or "").lower()
            keys = grp.get("keys") or []
            labels = grp.get("labels") or []
            for ath in grp.get("athletes", []):
                a = ath.get("athlete", {})
                pid = str(a.get("id") or a.get("displayName"))
                p = by_id.setdefault(pid, {"id": pid, "name": a.get("displayName", ""), "team": tabbr,
                                           "team_name": tname, "jersey": str(a.get("jersey") or ""),
                                           "stats": {}, "flat": {}, "raw": {}})
                vals = {}
                for i, v in enumerate(ath.get("stats", [])):
                    if i < len(labels):           # as shown, e.g. innings "6.1" (not always a plain number)
                        p["raw"].setdefault(labels[i].lower(), str(v))
                    # ESPN packs two stats in one column: key "completions/passingAttempts", "23/33"
                    if i < len(keys) and "/" in keys[i] and "/" in str(v):
                        for k2, v2 in zip(keys[i].split("/"), str(v).split("/")):
                            y = _num(v2)
                            if y is not None:
                                vals[k2] = y
                                p["flat"].setdefault(k2.lower(), y)
                    x = _num(v)
                    if x is None:
                        continue
                    if i < len(keys):
                        vals[keys[i]] = x
                        p["flat"].setdefault(keys[i].lower(), x)
                    if i < len(labels):
                        vals.setdefault(labels[i].lower(), x)
                        p["flat"].setdefault(labels[i].lower(), x)
                p["stats"].setdefault(gname, {}).update(vals)
    return list(by_id.values())


# ------------------------------------------------------------- leaders ---
def short_name(name: str) -> str:
    """'Patrick Mahomes' -> 'P. Mahomes'."""
    parts = (name or "").split()
    return f"{parts[0][0]}. {' '.join(parts[1:])}" if len(parts) > 1 else (name or "?")


def _grp(p: dict, group: str, *names: str) -> float | None:
    g = p["stats"].get(group, {})
    for n in names:
        for k in (n, n.lower()):
            if k in g:
                return g[k]
    return None


def _any(p: dict, *names: str) -> float | None:
    for n in names:
        if n.lower() in p["flat"]:
            return p["flat"][n.lower()]
    return None


def _top(players: list[dict], team: str, score, n: int = 1) -> list[dict]:
    """The n players on a team with the highest positive score (None / 0 are skipped)."""
    pool = [(score(p), p) for p in players if p["team"] == team]
    pool = [(s, p) for s, p in pool if s]
    return [p for _, p in sorted(pool, key=lambda t: -t[0])[:n]]


def _pass_line(p: dict) -> str:
    g = lambda *k: _grp(p, "passing", *k)      # noqa: E731
    line = f"{short_name(p['name'])} {g('completions') or 0:g}/{g('passingAttempts') or 0:g}, " \
           f"{g('passingYards') or 0:g} yds"
    if g("passingTouchdowns"):
        line += f", {g('passingTouchdowns'):g} TD"
    if g("interceptions"):
        line += f", {g('interceptions'):g} INT"
    return line


def _rush_line(p: dict) -> str:
    g = lambda *k: _grp(p, "rushing", *k)      # noqa: E731
    line = f"{short_name(p['name'])} {g('rushingAttempts') or 0:g} car, {g('rushingYards') or 0:g} yds"
    return line + (f", {g('rushingTouchdowns'):g} TD" if g("rushingTouchdowns") else "")


def _rec_line(p: dict) -> str:
    g = lambda *k: _grp(p, "receiving", *k)    # noqa: E731
    line = f"{short_name(p['name'])} {g('receptions') or 0:g} rec, {g('receivingYards') or 0:g} yds"
    return line + (f", {g('receivingTouchdowns'):g} TD" if g("receivingTouchdowns") else "")


def _football(players: list[dict], team: str) -> list[str]:
    out = [_pass_line(p) for p in _top(players, team, lambda p: _grp(p, "passing", "passingYards"))]
    out += [_rush_line(p) for p in _top(players, team, lambda p: _grp(p, "rushing", "rushingYards"))]
    out += [_rec_line(p) for p in _top(players, team, lambda p: _grp(p, "receiving", "receivingYards"))]
    return out


def _basketball(players: list[dict], team: str) -> list[str]:
    out = []
    for p in _top(players, team, lambda p: _any(p, "points", "pts"), 2):
        out.append(f"{short_name(p['name'])} {_any(p, 'points', 'pts') or 0:g} pts, "
                   f"{_any(p, 'rebounds', 'reb') or 0:g} reb, {_any(p, 'assists', 'ast', 'a') or 0:g} ast")
    return out


def _hockey(players: list[dict], team: str) -> list[str]:
    pts = lambda p: (_any(p, "goals", "g") or 0) + (_any(p, "assists", "a") or 0)      # noqa: E731
    out = []
    for p in _top(players, team, pts, 2):
        out.append(f"{short_name(p['name'])} {_any(p, 'goals', 'g') or 0:g} G, {_any(p, 'assists', 'a') or 0:g} A, "
                   f"{_any(p, 'shotsTotal', 'shots', 'sog', 's') or 0:g} SOG")
    for p in _top(players, team, lambda p: _any(p, "saves", "sv")):
        out.append(f"{short_name(p['name'])} {_any(p, 'saves', 'sv'):g} saves")
    return out


def _baseball(players: list[dict], team: str) -> list[str]:
    def bat(p):
        h, hr, rbi = (_grp(p, "batting", *k) or 0 for k in (("hits", "h"), ("homeRuns", "hr"), ("RBIs", "rbi")))
        return h + 2 * hr + rbi

    out = []
    for p in _top(players, team, bat, 2):
        h, hr, rbi = (_grp(p, "batting", *k) or 0 for k in (("hits", "h"), ("homeRuns", "hr"), ("RBIs", "rbi")))
        line = f"{short_name(p['name'])} {h:g} H"
        line += f", {hr:g} HR" if hr else ""
        out.append(line + (f", {rbi:g} RBI" if rbi else ""))
    for p in _top(players, team, lambda p: _grp(p, "pitching", "strikeouts", "k"), 1):
        ip = p["raw"].get("ip")
        out.append(f"{short_name(p['name'])} {ip + ' IP, ' if ip else ''}{_grp(p, 'pitching', 'strikeouts', 'k'):g} K")
    return out


def _soccer(players: list[dict], team: str) -> list[str]:
    out = []
    for p in _top(players, team, lambda p: (_any(p, "goals", "g") or 0) + (_any(p, "assists", "a") or 0), 4):
        g, a = _any(p, "goals", "g") or 0, _any(p, "assists", "a") or 0
        out.append(f"{short_name(p['name'])} " + ", ".join(x for x in (f"{g:g} G" if g else "", f"{a:g} A" if a else "") if x))
    return out


LEADERS = {"nfl": _football, "ncaaf": _football, "nba": _basketball, "nhl": _hockey, "mlb": _baseball,
           "soccer": _soccer}


def leaders(sport: str, players: list[dict], team: str) -> list[str]:
    """Short standout lines for one team ('P. Mahomes 22/31, 254 yds, 2 TD'). Empty when there's no box yet."""
    fn = LEADERS.get(sport)
    return fn(players, team) if fn else []


# ---------------------------------------------------------- card text ---
def score_line(g: LiveGame) -> str:
    """'🏈 KC 17 – 21 BUF', the ball next to the team that has it. Before kickoff: 'KC @ BUF'."""
    if g.state == "pre":
        return f"{g.away_abbr} @ {g.home_abbr}"
    away, home = f"{g.away_abbr} {g.away_score}", f"{g.home_score} {g.home_abbr}"
    if g.possession == "away":
        away = f"🏈 {away}"
    elif g.possession == "home":
        home = f"{home} 🏈"
    return f"{away} – {home}"


def epoch(iso: str) -> int | None:
    try:
        return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())
    except (ValueError, AttributeError):
        return None


def status_text(g: LiveGame) -> str:
    if g.canceled:
        return "🚫 Postponed or canceled"
    if g.state == "in":
        return f"🔴 LIVE · {g.detail}"
    if g.state == "post":
        return f"✅ {g.detail or 'Final'}"
    ts = epoch(g.date)
    return f"🕒 Starts <t:{ts}:t> (<t:{ts}:R>)" if ts else f"🕒 {g.detail}"


def down_text(g: LiveGame) -> str:
    """'🚨 2nd & 7 at KC 18' (red zone) or '2nd & 7 at KC 35'; empty when nobody has the ball."""
    if not g.possession or not g.down:
        return ""
    return f"🚨 {g.down}" if g.red_zone else g.down


def sort_games(games: list[LiveGame]) -> list[LiveGame]:
    far = datetime.max.replace(tzinfo=timezone.utc)

    def start(g):
        try:
            return datetime.fromisoformat(g.date.replace("Z", "+00:00"))
        except ValueError:
            return far
    return sorted(games, key=lambda g: ({"in": 0, "pre": 1, "post": 2}.get(g.state, 3), start(g)))


def espn_date(iso: str, fallback: datetime) -> str:
    """The YYYYMMDD ESPN files a game under (its US Eastern start date)."""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        dt = fallback
    return dt.astimezone(ET).strftime("%Y%m%d")


# ------------------------------------------------------------- fetching ---
def _paths(sport: sp.Sport) -> list[tuple[str, str]]:
    """[(ESPN path like 'football/nfl', league label)] for a sport's scoreboards (several for soccer)."""
    return [(url.split("/sports/", 1)[1].rsplit("/scoreboard", 1)[0], label) for url, label in sport.scoreboards]


async def scoreboard(session: aiohttp.ClientSession, sport: sp.Sport, date: str) -> list[LiveGame]:
    """Every league's scoreboard for that sport on one date (YYYYMMDD)."""
    paths = _paths(sport)
    out: list[LiveGame] = []
    for path, label in paths:
        try:
            data = await get_json(session, f"{sp.ESPN}/{path}/scoreboard",
                                  {"dates": date, **dict(sport.params)}, timeout=15)
        except EspnBlocked:
            raise
        except Exception:
            if len(paths) == 1:
                raise
            continue                      # one soccer league failing shouldn't hide the others
        out += parse_events(sport.key, data, path, label)
    return out


async def box(session: aiohttp.ClientSession, path: str, espn_id: str) -> list[dict]:
    """Every player's stats in one game."""
    return parse_box(await get_json(session, f"{sp.ESPN}/{path}/summary", {"event": espn_id}, timeout=15))
