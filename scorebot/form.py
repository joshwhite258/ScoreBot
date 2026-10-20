"""Player form: a player's last few games and his averages, from free ESPN box scores (NBA, NHL, MLB).

How it works: ESPN's search finds the player, his team's schedule gives the last finished games, and each
game's box score gives his line. Finished box scores never change, so they're kept in memory.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime

import aiohttp

from . import live as lv
from .espn import get_json
from .names import norm_name
from .sports import ESPN

log = logging.getLogger(__name__)

SEARCH = "https://site.web.api.espn.com/apis/common/v3/search"
PATH = {"nba": "basketball/nba", "nhl": "hockey/nhl", "mlb": "baseball/mlb"}
FORM_SPORTS = tuple(PATH)
SEASON_TYPES = (2, 3)                       # regular season, then playoffs
OUT = {"Out", "Injured Reserve", "Suspension"}
LOOKBACK = 5                                # extra team games to scan, since players miss some

_sched: dict[tuple[str, str], tuple[float, list[dict]]] = {}
_boxes: dict[tuple[str, str], list[dict]] = {}
_inj: dict[str, tuple[float, dict]] = {}
_found: dict[tuple[str, str], tuple[float, list[dict]]] = {}


def _trim(cache: dict, limit: int) -> None:
    while len(cache) > limit:
        cache.pop(next(iter(cache)))


# ------------------------------------------------------------ finding him ---
def parse_search(data: dict, sport: str) -> list[dict]:
    out = []
    for it in data.get("items") or []:
        if (it.get("league") or "").lower() != sport:
            continue
        tr = (it.get("teamRelationships") or [{}])[0].get("core") or {}
        out.append({"name": it.get("displayName", ""), "id": str(it.get("id")),
                    "team": (tr.get("abbreviation") or "").upper(), "team_name": tr.get("displayName", "")})
    return out


def match_name(cands: list[dict], name: str) -> list[dict]:
    """Candidates matching the typed name: the full name, or first initial + last name ('J. Murray'), or just a
    last name. An exact full-name match wins outright."""
    want = norm_name(name)
    parts = want.replace("-", " ").split()
    if not parts:
        return []

    def ok(c):
        n = norm_name(c["name"])
        if n == want:
            return True
        cp = n.replace("-", " ").split()
        return bool(cp) and cp[-1] == parts[-1] and (len(parts) == 1 or cp[0][:1] == parts[0][:1])
    hits = [c for c in cands if ok(c)]
    exact = [c for c in hits if norm_name(c["name"]) == want]
    return exact or hits


async def find_athletes(session: aiohttp.ClientSession, sport: str, name: str) -> list[dict]:
    """Players matching `name`, best first (usually one)."""
    parts = norm_name(name).replace("-", " ").split()
    if not parts:
        return []
    query = parts[-1] if len(parts[0]) <= 2 else norm_name(name)      # an initial alone is a useless query
    hit = _found.get((sport, query))
    if hit and time.monotonic() - hit[0] < 86400:
        cands = hit[1]
    else:
        data = await get_json(session, SEARCH, {"query": query, "limit": 25, "type": "player"})
        cands = parse_search(data, sport)
        _found[(sport, query)] = (time.monotonic(), cands)
        _trim(_found, 300)
    seen, out = set(), []
    for c in match_name(cands, name):
        if c["id"] not in seen:
            seen.add(c["id"])
            out.append(c)
    return out


# ---------------------------------------------------------------- games ---
def parse_schedule(data: dict, team: str) -> list[dict]:
    """[{id, date, completed, home, opp, team_score, opp_score}] for one team's schedule payload."""
    out, team = [], team.upper()
    for ev in data.get("events") or []:
        comp = (ev.get("competitions") or [{}])[0]
        cs = comp.get("competitors") or []
        me = next((c for c in cs if (c.get("team") or {}).get("abbreviation", "").upper() == team), None)
        opp = next((c for c in cs if c is not me), None)
        if not me or not opp:
            continue
        st = (comp.get("status") or {}).get("type") or {}

        def score(c):
            s = c.get("score")
            if isinstance(s, dict):
                s = s.get("value", s.get("displayValue"))
            try:
                return float(s)
            except (TypeError, ValueError):
                return None
        out.append({"id": str(ev.get("id")), "date": ev.get("date") or comp.get("date"),
                    "completed": bool(st.get("completed")), "home": me.get("homeAway") == "home",
                    "opp": (opp.get("team") or {}).get("abbreviation", "").upper(),
                    "team_score": score(me), "opp_score": score(opp)})
    return sorted(out, key=lambda g: g["date"] or "")


async def schedule(session: aiohttp.ClientSession, sport: str, team: str) -> list[dict]:
    hit = _sched.get((sport, team))
    if hit and time.monotonic() - hit[0] < 3 * 3600:
        return hit[1]
    games: dict[str, dict] = {}
    for st in SEASON_TYPES:
        try:
            data = await get_json(session, f"{ESPN}/{PATH[sport]}/teams/{team.lower()}/schedule",
                                  {"seasontype": st})
        except Exception as e:          # no playoffs yet, ...
            log.debug("schedule %s %s %s: %s", sport, team, st, e)
            if st == SEASON_TYPES[0]:
                raise
            continue
        for g in parse_schedule(data, team):
            games[g["id"]] = g
    out = sorted(games.values(), key=lambda g: g["date"] or "")
    _sched[(sport, team)] = (time.monotonic(), out)
    _trim(_sched, 200)
    return out


async def box(session: aiohttp.ClientSession, sport: str, event_id: str) -> list[dict]:
    hit = _boxes.get((sport, event_id))
    if hit is not None:
        return hit
    data = await get_json(session, f"{ESPN}/{PATH[sport]}/summary", {"event": event_id})
    players = lv.parse_box(data)
    st = (((data.get("header") or {}).get("competitions") or [{}])[0].get("status") or {}).get("type") or {}
    if st.get("completed"):
        _boxes[(sport, event_id)] = players
        _trim(_boxes, 400)
    return players


def find_row(players: list[dict], athlete: dict) -> dict | None:
    for p in players:
        if p["id"] == athlete["id"]:
            return p
    want = norm_name(athlete["name"])
    return next((p for p in players if norm_name(p["name"]) == want), None)


# ---------------------------------------------------------- his box line ---
def clock(v: str | None) -> float | None:
    """'34:12' -> 34.2 minutes; '31' -> 31.0."""
    if not v:
        return None
    try:
        if ":" in v:
            m, s = v.split(":", 1)
            return int(m) + int(s) / 60
        return float(v)
    except ValueError:
        return None


def innings(v: str | None) -> float | None:
    """Baseball '6.1' means six and a third."""
    if not v:
        return None
    try:
        whole, _, outs = v.partition(".")
        return int(whole) + (int(outs) / 3 if outs else 0)
    except ValueError:
        return None


def _made(raw: str | None) -> float | None:
    """'3-7' -> 3 (the made half of a made-attempted column)."""
    try:
        return float((raw or "").split("-")[0])
    except ValueError:
        return None


def _n(x) -> float:
    return x or 0


def line_for(sport: str, p: dict) -> dict | None:
    """{'kind', 'text', 'vals'} for one player's box row; vals are what gets averaged. None if he didn't play."""
    raw = p.get("raw") or {}
    if sport == "nba":
        mins = clock(raw.get("min"))
        pts, reb, ast = (lv._any(p, *k) for k in (("points", "pts"), ("rebounds", "reb"), ("assists", "ast", "a")))
        if mins is None and pts is None:
            return None
        if mins == 0 and not pts:
            return None
        threes = _made(raw.get("3pt"))
        text = f"{_n(pts):g} pts, {_n(reb):g} reb, {_n(ast):g} ast"
        if threes:
            text += f", {threes:g} 3PM"
        if mins:
            text += f" · {mins:.0f} min"
        vals = {"PTS": _n(pts), "REB": _n(reb), "AST": _n(ast)}
        if mins:
            vals["MIN"] = mins
        return {"kind": "nba", "text": text, "vals": vals}
    if sport == "nhl":
        sv = lv._any(p, "saves", "sv")
        if sv is not None and lv._any(p, "goals", "g") is None:
            sa = lv._any(p, "shotsAgainst", "sa")
            text = f"{sv:g} saves" + (f" on {sa:g} shots" if sa else "")
            return {"kind": "goalie", "text": text, "vals": {"SV": sv, **({"SA": sa} if sa else {})}}
        g, a = _n(lv._any(p, "goals", "g")), _n(lv._any(p, "assists", "a"))
        s, toi = lv._any(p, "shotsTotal", "shots", "sog", "s"), clock(raw.get("toi"))
        if toi is None and s is None and not g and not a:
            return None
        text = f"{g:g} G, {a:g} A, {_n(s):g} SOG" + (f" · {toi:.0f} TOI" if toi else "")
        vals = {"G": g, "A": a, "SOG": _n(s)}
        if toi:
            vals["TOI"] = toi
        return {"kind": "skater", "text": text, "vals": vals}
    if sport == "mlb":
        if "pitching" in p["stats"]:
            ip, k = innings(raw.get("ip")), lv._grp(p, "pitching", "strikeouts", "k")
            er = lv._grp(p, "pitching", "earnedRuns", "er")
            if ip is None:
                return None
            text = f"{raw.get('ip')} IP, {_n(k):g} K" + (f", {er:g} ER" if er is not None else "")
            vals = {"IP": ip, "K": _n(k)}
            if er is not None:
                vals["ER"] = er
            return {"kind": "pitcher", "text": text, "vals": vals}
        h, hr, rbi, r = (_n(lv._grp(p, "batting", *k)) for k in
                         (("hits", "h"), ("homeRuns", "hr"), ("RBIs", "rbi"), ("runs", "r")))
        ab = lv._grp(p, "batting", "atBats", "ab")
        if ab is None and not h:
            return None
        text = f"{h:g}-{_n(ab):g}" + "".join(f", {v:g} {n}" for v, n in ((hr, "HR"), (rbi, "RBI"), (r, "R")) if v)
        return {"kind": "hitter", "text": text, "vals": {"H": h, "AB": _n(ab), "HR": hr, "RBI": rbi, "R": r}}
    return None


def averages(rows: list[dict]) -> str:
    """'24.2 pts, 6.0 reb, 7.4 ast · 33 min' style, over the games shown."""
    if not rows:
        return ""
    kind = rows[0]["kind"]
    keys = [k for k in rows[0]["vals"]]
    avg = {k: sum(r["vals"].get(k, 0) for r in rows) / len(rows) for k in keys}
    if kind == "nba":
        s = f"{avg['PTS']:.1f} pts, {avg['REB']:.1f} reb, {avg['AST']:.1f} ast"
        return s + (f" · {avg['MIN']:.0f} min" if "MIN" in avg else "")
    if kind == "skater":
        s = f"{avg['G']:.2f} G, {avg['A']:.2f} A, {avg['SOG']:.1f} SOG"
        return s + (f" · {avg['TOI']:.0f} TOI" if "TOI" in avg else "")
    if kind == "goalie":
        return f"{avg['SV']:.1f} saves" + (f" on {avg['SA']:.1f} shots" if "SA" in avg else "")
    if kind == "pitcher":
        return f"{avg['IP']:.1f} IP, {avg['K']:.1f} K" + (f", {avg['ER']:.1f} ER" if "ER" in avg else "")
    hits, ab = sum(r["vals"]["H"] for r in rows), sum(r["vals"]["AB"] for r in rows)
    return (f"{hits:g}-for-{ab:g}" + (f" ({hits / ab:.3f})".replace("0.", ".", 1) if ab else "")
            + f", {sum(r['vals']['HR'] for r in rows):g} HR, {sum(r['vals']['RBI'] for r in rows):g} RBI")


def result_text(g: dict) -> str:
    """'W 112–104 vs BOS' / 'L 2–3 @ NYR'."""
    where = f"{'vs' if g['home'] else '@'} {g['opp']}"
    a, b = g.get("team_score"), g.get("opp_score")
    if a is None or b is None:
        return where
    return f"{'W' if a > b else 'L' if a < b else 'T'} {a:g}–{b:g} {where}"


# ---------------------------------------------------------------- injury ---
def parse_injuries(data: dict) -> dict[str, tuple[str, str]]:
    """{normalized name: (status, comment)}."""
    out = {}
    for t in data.get("injuries") or []:
        for i in t.get("injuries") or []:
            a = i.get("athlete") or {}
            if a.get("displayName"):
                out[norm_name(a["displayName"])] = (i.get("status", ""), i.get("shortComment", ""))
    return out


async def injury(session: aiohttp.ClientSession, sport: str, name: str) -> tuple[str, str] | None:
    hit = _inj.get(sport)
    if not hit or time.monotonic() - hit[0] > 3600:
        try:
            hit = (time.monotonic(), parse_injuries(await get_json(session, f"{ESPN}/{PATH[sport]}/injuries")))
        except Exception as e:
            log.info("injuries %s unavailable: %s", sport, e)
            return None
        _inj[sport] = hit
    return hit[1].get(norm_name(name))


# ------------------------------------------------------------------ main ---
async def recent_form(session: aiohttp.ClientSession, sport: str, athlete: dict, n: int = 5) -> dict:
    """{'games': [{date, text, result, kind, vals}] newest first, 'average': str, 'injury': (status, note) | None}.
    Games he didn't play are skipped. Looks back at most n + LOOKBACK team games."""
    done = [g for g in await schedule(session, sport, athlete["team"]) if g["completed"]]
    rows = []
    for g in reversed(done[-(n + LOOKBACK):]):
        try:
            p = find_row(await box(session, sport, g["id"]), athlete)
        except Exception as e:
            log.info("box %s unavailable: %s", g["id"], e)
            continue
        ln = p and line_for(sport, p)
        if not ln:
            continue
        rows.append({"date": g["date"], "result": result_text(g), **ln})
        if len(rows) == n:
            break
    return {"games": rows, "average": averages(rows), "injury": await injury(session, sport, athlete["name"])}


def short_date(iso: str | None) -> str:
    try:
        return datetime.fromisoformat((iso or "").replace("Z", "+00:00")).astimezone(lv.ET).strftime("%b %-d")
    except ValueError:
        return "?"
