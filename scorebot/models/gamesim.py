"""NFL game simulator: play one matchup thousands of times, drive by drive, from ScoreBot's own model.

Each simulated game is a sequence of drives. A drive ends in a touchdown, a made or missed field goal, a
turnover (sometimes returned for a score), a safety, or a punt / turnover on downs. The scoring chances are
scaled so that, averaged over many games, each team scores what the team model projects (its total and margin),
and the spread of results matches real NFL margins. Yards and touchdowns go to the quarterback, runners and
receivers in proportion to the player model's projections, so every simulated game has a full box score.

Reproducible: game number i of a simulation uses random.Random(seed * 1_000_003 + i), so `replay(g, seed, i)`
always gives the same game i, however many games were played in total.

What it is not: new information. It spreads out what the model already believes, so you can see the whole range
of outcomes (how often it's a blowout, how often it goes to overtime, each player's realistic range) instead of a
single projected score.

Pure Python, standard library only.
"""
from __future__ import annotations

import math
import random
from collections import Counter
from dataclasses import asdict, dataclass, field
from statistics import mean, pstdev

# --------------------------------------------------------------------------- tuning (league-average NFL)
DRIVES_MEAN, DRIVES_SD = 22.6, 1.0          # drives per game, both teams
BASE_TD, BASE_FG, MISS_FG, BASE_TO, SAFETY = 0.205, 0.13, 0.012, 0.115, 0.002
DEF_TD_SHARE = 0.07                         # share of turnovers returned for a touchdown
PTS_TD = 6.96                               # six plus the extra point (95.5%) or a two-point try
PASS_SHARE, PASS_TD_SHARE = 0.62, 0.60
OT_MAX_DRIVES = 8
SIM_STRETCH = 1.03                          # the drive model's margins come out ~3% narrower than the model's
MAX_RUNS = 100_000

# Player stats the summary reports, with display labels.
STATS = ("pass_yds", "pass_td", "rush_yds", "rec_yds", "rec", "td")
STAT_LABEL = {"pass_yds": "pass yds", "pass_td": "pass TDs", "rush_yds": "rush yds", "rec_yds": "rec yds",
              "rec": "receptions", "td": "TDs"}


@dataclass
class PlayerSpec:
    name: str
    pos: str
    pass_yds: float = 0.0       # this matchup's projections (QB)
    rush_yds: float = 0.0
    rec_yds: float = 0.0
    rec: float = 0.0
    td: float = 0.0             # expected rushing + receiving touchdowns, used to weight who scores


@dataclass
class SimGame:
    home: str
    away: str
    home_pts: float             # expected points for each side
    away_pts: float
    home_qb: str | None = None
    away_qb: str | None = None
    notes: list = field(default_factory=list)
    home_players: list = field(default_factory=list)      # PlayerSpec dicts
    away_players: list = field(default_factory=list)

    def to_json(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_json(d: dict) -> "SimGame":
        return SimGame(**d)


# --------------------------------------------------------------------------- drive probabilities
def _p_td(s: float) -> float:
    return min(0.5, BASE_TD * s)


def _p_fg(s: float) -> float:
    return min(0.2, max(0.05, BASE_FG * (1 - 0.25 * (s - 1))))


def _p_to(s: float) -> float:
    return min(0.2, max(0.05, BASE_TO / s ** 0.6))


def _drive_probs(s: float) -> tuple[float, float, float, float, float]:
    """Cumulative thresholds: TD, FG made, FG missed, turnover, safety (the rest is a punt / downs)."""
    t = _p_td(s)
    f = t + _p_fg(s)
    m = f + MISS_FG
    o = m + _p_to(s)
    return t, f, m, o, o + SAFETY


def _calibrate(home_pts: float, away_pts: float, n_home: float, n_away: float) -> tuple[float, float]:
    """Scoring scales (s_home, s_away) so expected points match the targets, counting the points the other
    side's turnovers hand over as defensive touchdowns."""
    def own(s: float) -> float:
        return _p_td(s) * PTS_TD + _p_fg(s) * 3

    sh = sa = 1.0
    for _ in range(8):
        for side in ("h", "a"):
            target, n_own, s_opp, n_opp = ((home_pts, n_home, sa, n_away) if side == "h"
                                           else (away_pts, n_away, sh, n_home))
            gift = n_opp * _p_to(s_opp) * DEF_TD_SHARE * PTS_TD + n_opp * SAFETY * 2
            need = max(target - gift, 3.0) / max(n_own, 1.0)
            lo, hi = 0.05, 4.0
            for _ in range(40):
                mid = (lo + hi) / 2
                if own(mid) < need:
                    lo = mid
                else:
                    hi = mid
            if side == "h":
                sh = (lo + hi) / 2
            else:
                sa = (lo + hi) / 2
    return sh, sa


def _exp_drive_yards(s: float) -> float:
    t, f, m, o, sf = _drive_probs(s)
    return t * 64 + (f - t) * 46 + (m - f) * 42 + (o - m) * 28 + (sf - o) * 10 + (1 - sf) * 24


class Prepared:
    """A SimGame with its drive probabilities worked out once, shared by every simulated game."""

    def __init__(self, g: SimGame):
        self.g = g
        self.s_home, self.s_away = _calibrate(g.home_pts, g.away_pts, DRIVES_MEAN / 2, DRIVES_MEAN / 2)
        self.home_players = [PlayerSpec(**p) if isinstance(p, dict) else p for p in g.home_players]
        self.away_players = [PlayerSpec(**p) if isinstance(p, dict) else p for p in g.away_players]
        self.w = {"home": self._weights(self.home_players), "away": self._weights(self.away_players)}
        self.exp_yards = {"home": DRIVES_MEAN / 2 * _exp_drive_yards(self.s_home),
                          "away": DRIVES_MEAN / 2 * _exp_drive_yards(self.s_away)}

    @staticmethod
    def _weights(players: list[PlayerSpec]) -> dict:
        qb = next((p for p in players if p.pos == "QB"), None)
        rbs = [p for p in players if p.pos in ("RB", "FB") or (p.pos != "QB" and p.rush_yds > p.rec_yds)]
        recs = [p for p in players if p.pos in ("WR", "TE") or (p.pos != "QB" and p.rec_yds >= p.rush_yds)]
        return {"qb": qb, "rbs": rbs, "recs": recs}


# --------------------------------------------------------------------------- one game
def _alloc(rng: random.Random, total: float, weights: list[float], conc: float = 10.0) -> list[float]:
    """Split `total` across players with Dirichlet-style randomness (gamma draws), so shares vary game to game."""
    ws = [max(w, 1e-6) for w in weights]
    draws = [rng.gammavariate(conc * w / sum(ws) + 0.05, 1.0) for w in ws]
    s = sum(draws)
    return [total * d / s for d in draws]


def _pick(rng: random.Random, items: list, weights: list[float]):
    return rng.choices(items, weights=[max(w, 1e-6) for w in weights])[0] if items else None


def play_game(pg: Prepared, rng: random.Random, detail: bool = False) -> dict:
    """Play one game. Returns the score and box score; with detail=True also the drive log."""
    n = max(16, min(30, round(rng.gauss(DRIVES_MEAN, DRIVES_SD))))
    probs = {"home": _drive_probs(pg.s_home), "away": _drive_probs(pg.s_away)}
    score = {"home": 0, "away": 0}
    yards = {"home": 0.0, "away": 0.0}
    tds = {"home": 0, "away": 0}
    turnovers = {"home": 0, "away": 0}
    drives: list[dict] = []
    team = "home" if rng.random() < 0.5 else "away"

    def one_drive(side: str, idx: int, quarter: str) -> None:
        other = "away" if side == "home" else "home"
        t, f, m, o, sf = probs[side]
        r = rng.random()
        pts_off = pts_def = 0
        if r < t:
            res, y = "TD", min(99.0, max(8.0, rng.gauss(64, 22)))
            conv = rng.random()
            pts_off = 7 if conv < 0.955 else (6 + (2 if rng.random() < 0.48 else 0)) if conv < 0.995 else 6
            tds[side] += 1
        elif r < f:
            res, y = "FG", min(80.0, max(6.0, rng.gauss(46, 14)))
            pts_off = 3
        elif r < m:
            res, y = "missed FG", min(70.0, max(6.0, rng.gauss(42, 14)))
        elif r < o:
            turnovers[side] += 1
            y = max(0.0, rng.gauss(28, 16))
            if rng.random() < DEF_TD_SHARE:
                res, pts_def = "turnover, returned for a TD", 7
                tds[other] += 1
            else:
                res = "turnover"
        elif r < sf:
            res, y = "safety", max(0.0, rng.gauss(10, 8))
            pts_def = 2
        else:
            res, y = "punt", max(0.0, rng.gauss(24, 14))
        yards[side] += y
        score[side] += pts_off
        score[other] += pts_def
        plays_noise = rng.gauss(0, 1.2)           # always drawn, so a replay with detail is the same game
        if detail:
            drives.append(dict(n=idx, quarter=quarter, team=side, result=res, yards=round(y),
                               plays=max(1, round(y / 5.4 + plays_noise)), home=score["home"], away=score["away"]))

    for i in range(n):
        one_drive(team, i + 1, f"Q{min(3, i * 4 // n) + 1}")
        team = "away" if team == "home" else "home"

    ot = False
    if score["home"] == score["away"]:
        ot = True
        first = "home" if rng.random() < 0.5 else "away"
        second = "away" if first == "home" else "home"
        one_drive(first, n + 1, "OT")
        one_drive(second, n + 2, "OT")
        k = 2
        while score["home"] == score["away"] and k < OT_MAX_DRIVES:
            for side in (first, second):
                k += 1
                one_drive(side, n + k, "OT")
                if score["home"] != score["away"]:
                    break
    out = dict(home=score["home"], away=score["away"], ot=ot,
               turnovers_home=turnovers["home"], turnovers_away=turnovers["away"],
               yards_home=round(yards["home"]), yards_away=round(yards["away"]))
    out["stats"] = _box(pg, rng, yards, tds)
    if detail:
        out["drives"] = drives
    return out


def _box(pg: Prepared, rng: random.Random, yards: dict, tds: dict) -> dict:
    """Player lines for one game. Yardage follows the projections, scaled by how well the offence moved the ball
    in THIS game (a high-scoring game is also a big yardage game); touchdowns are split pass/rush and handed to
    receivers and runners by their touchdown weights."""
    stats: dict[str, dict] = {}
    for side in ("home", "away"):
        w = pg.w[side]
        qb, rbs, recs = w["qb"], w["rbs"], w["recs"]
        script = max(0.3, yards[side] / max(pg.exp_yards[side], 1.0))      # 1.0 = a typical day
        share = min(0.8, max(0.45, rng.gauss(PASS_SHARE, 0.05)))
        pass_y = (qb.pass_yds * script ** 0.9 * rng.gauss(1.0, 0.08)) if qb is not None and qb.pass_yds > 0 \
            else yards[side] * share
        rush_proj = sum(r.rush_yds for r in rbs) / 0.85 if rbs else 0.0
        rush_y = rush_proj * script ** 0.6 * rng.gauss(1.0, 0.08) if rush_proj > 0 else yards[side] * (1 - share)
        n_td = tds[side]
        pass_tds = sum(1 for _ in range(n_td) if rng.random() < PASS_TD_SHARE) if recs else 0
        rush_tds = n_td - pass_tds
        if qb is not None:
            stats[qb.name] = dict(team=side, pos="QB", pass_yds=max(0, round(pass_y)), pass_td=pass_tds,
                                  rush_yds=round(rush_y * 0.07))
        if recs:
            ws = [r.rec_yds for r in recs] + [sum(r.rec_yds for r in recs) * 0.25]
            for r, ry in zip(recs, _alloc(rng, max(pass_y, 0.0), ws, 12.0)):
                ypr = max(6.0, r.rec_yds / r.rec) if r.rec > 0 else 11.0
                stats[r.name] = dict(team=side, pos=r.pos, rec_yds=round(ry), td=0,
                                     rec=max(0, round(ry / ypr + rng.gauss(0, 0.9))))
        if rbs:
            ws = [r.rush_yds for r in rbs] + [sum(r.rush_yds for r in rbs) * 0.2]
            for r, ry in zip(rbs, _alloc(rng, max(rush_y * 0.85, 0.0), ws, 10.0)):
                stats.setdefault(r.name, dict(team=side, pos=r.pos, td=0))["rush_yds"] = round(ry)
        for _ in range(pass_tds):
            who = _pick(rng, recs, [max(r.td, 0.02) for r in recs])
            stats[who.name]["td"] += 1
        for _ in range(rush_tds):
            pool = rbs or recs
            if not pool:
                break
            who = _pick(rng, pool, [max(r.td, 0.02) for r in pool])
            stats[who.name]["td"] = stats[who.name].get("td", 0) + 1
    return stats


# --------------------------------------------------------------------------- many games
def _game_rng(seed: int, i: int) -> random.Random:
    return random.Random(seed * 1_000_003 + i)


def _quantiles(counts: Counter, n: int, qs=(0.1, 0.25, 0.5, 0.75, 0.9)) -> list[float]:
    """Quantiles of a value distribution stored as counts (memory stays flat however many games are played)."""
    out, items, acc, k = [], sorted(counts.items()), 0, 0
    targets = [min(n - 1, int(q * n)) for q in qs]
    for v, c in items:
        acc += c
        while k < len(targets) and targets[k] < acc:
            out.append(v)
            k += 1
    while len(out) < len(qs):
        out.append(items[-1][0] if items else 0)
    return out


def simulate(g: SimGame, n: int = 20000, seed: int = 1) -> dict:
    """Play `n` games and summarise them as plain JSON-able data."""
    n = max(1, min(int(n), MAX_RUNS))
    pg = Prepared(g)
    margins: Counter = Counter()
    totals: Counter = Counter()
    scores: Counter = Counter()
    per: dict[str, dict[str, Counter]] = {}
    side_of: dict[str, tuple[str, str]] = {}
    hw = aw = ot = 0
    sum_h = sum_a = 0
    for i in range(n):
        r = play_game(pg, _game_rng(seed, i))
        m = r["home"] - r["away"]
        hw += m > 0
        aw += m < 0
        ot += r["ot"]
        sum_h += r["home"]
        sum_a += r["away"]
        margins[m] += 1
        totals[r["home"] + r["away"]] += 1
        scores[(r["away"], r["home"])] += 1
        for name, st in r["stats"].items():
            side_of.setdefault(name, (st["team"], st["pos"]))
            d = per.setdefault(name, {})
            for k in STATS:
                if k in st:
                    d.setdefault(k, Counter())[st[k]] += 1

    def mean_sd(c: Counter) -> tuple[float, float]:
        mu = sum(v * k for v, k in c.items()) / n
        var = sum(k * (v - mu) ** 2 for v, k in c.items()) / n
        return mu, math.sqrt(var)

    m_mu, m_sd = mean_sd(margins)
    t_mu, t_sd = mean_sd(totals)
    players = {}
    for name, d in per.items():
        team, pos = side_of[name]
        row: dict = dict(team=team, pos=pos)
        for k, c in d.items():
            c = Counter(c)
            c[0] += n - sum(c.values())          # games where the player had no line count as zero
            mu = sum(v * cnt for v, cnt in c.items()) / n
            row[k] = dict(mean=mu, q=_quantiles(c, n), p_any=sum(cnt for v, cnt in c.items() if v >= 1) / n)
        players[name] = row
    return dict(n=n, seed=seed, home=g.home, away=g.away, home_win=hw / n, away_win=aw / n,
                tie=(n - hw - aw) / n, ot=ot / n, avg_home=sum_h / n, avg_away=sum_a / n,
                margin_mean=m_mu, margin_sd=m_sd, total_mean=t_mu, total_sd=t_sd,
                margin_counts={str(k): v for k, v in margins.items()},
                top_scores=[(a, h, c / n) for (a, h), c in scores.most_common(5)],
                players=players)


def replay(g: SimGame, seed: int, i: int) -> dict:
    """Game number i of a simulation with this seed, with its drive log and box score."""
    return play_game(Prepared(g), _game_rng(seed, i), detail=True)


def margin_buckets(summary: dict) -> dict[str, float]:
    """Share of simulated games by margin band, from the home side (negative = away won)."""
    b = {"away by 15+": 0, "away by 8-14": 0, "away by 1-7": 0, "tie": 0,
         "home by 1-7": 0, "home by 8-14": 0, "home by 15+": 0}
    for k, v in summary["margin_counts"].items():
        m = int(k)
        key = ("away by 15+" if m <= -15 else "away by 8-14" if m <= -8 else "away by 1-7" if m < 0 else
               "tie" if m == 0 else "home by 1-7" if m <= 7 else "home by 8-14" if m <= 14 else "home by 15+")
        b[key] += v
    return {k: v / summary["n"] for k, v in b.items()}


# --------------------------------------------------------------------------- from ScoreBot's models
SIDELINED = ("Out", "Doubtful", "Injured Reserve", "Suspension")


def roster(pm, code: str, opp: str | None, team_pts: float | None, qb_name: str | None,
           sidelined=None, notes: list | None = None) -> list[dict]:
    """The QB, top two runners and top four receivers of a team with this matchup's projections.
    `sidelined(name)` returns an injury status (Out, Doubtful, IR...) for players to leave out."""
    def proj(pid: str, stat: str) -> float:
        try:
            return float(pm.project(pid, stat, opp, team_pts) or 0.0)
        except Exception:
            return 0.0

    def why_out(pid: str) -> str | None:
        st = sidelined(pm.info[pid]["name"]) if sidelined else None
        return st if st in SIDELINED else None

    mine = [pid for pid, i in pm.info.items() if i.get("team") == code]
    out: list[dict] = []
    dropped: list[str] = []
    qb_pid = pm.find(qb_name, code) if qb_name and "backup" not in qb_name and qb_name != "unknown QB" else None
    if qb_pid is None and not qb_name:
        best = sorted(((proj(pid, "pass_yds"), pid) for pid in mine
                       if pm.info[pid].get("pos") == "QB" and not why_out(pid)), reverse=True)
        qb_pid = best[0][1] if best and best[0][0] > 0 else None
    if qb_pid is not None:
        out.append(asdict(PlayerSpec(pm.info[qb_pid]["name"], "QB", pass_yds=proj(qb_pid, "pass_yds") or 200.0)))
    elif qb_name:                                   # a backup the player model has no history for
        out.append(asdict(PlayerSpec("Backup QB" if "backup" in qb_name else qb_name, "QB", pass_yds=190.0)))

    def top(positions: tuple, stat: str, k: int) -> list[tuple[float, str]]:
        ranked = sorted(((proj(pid, stat), pid) for pid in mine if pm.info[pid].get("pos") in positions),
                        reverse=True)
        keep = []
        for y, pid in ranked:
            if len(keep) >= k:
                break
            why = why_out(pid)
            if why:
                if y > 8:
                    dropped.append(f"{pm.info[pid]['name']} ({why.lower()})")
                continue
            keep.append((y, pid))
        return keep

    for y, pid in top(("RB",), "rush_yds", 2):
        if y > 8:
            out.append(asdict(PlayerSpec(pm.info[pid]["name"], "RB", rush_yds=y, td=proj(pid, "total_tds"))))
    for y, pid in top(("WR", "TE"), "rec_yds", 4):
        if y > 8:
            out.append(asdict(PlayerSpec(pm.info[pid]["name"], pm.info[pid]["pos"], rec_yds=y,
                                         rec=proj(pid, "receptions"), td=proj(pid, "total_tds"))))
    if notes is not None and dropped:
        notes.append(f"{code} left out: " + ", ".join(dropped))
    return out


def build_game(svc, game: dict) -> SimGame | None:
    """A SimGame for one schedule row from the team model (expected points, QBs) and the player model."""
    pred = svc.predict_game(game)
    if not pred:
        return None
    total, margin = pred["total"], pred["margin"] * SIM_STRETCH
    hp, ap = (total + margin) / 2, (total - margin) / 2
    notes: list[str] = []
    for side in ("away", "home"):
        if abs(pred.get(f"{side}_qb_adj", 0.0)) >= 0.5:
            notes.append(f"{pred[side]} QB: {pred[f'{side}_qb_name']} ({pred[f'{side}_qb_src']})")
    sg = SimGame(home=pred["home"], away=pred["away"], home_pts=hp, away_pts=ap,
                 home_qb=pred.get("home_qb_name"), away_qb=pred.get("away_qb_name"), notes=notes)
    pm = getattr(svc, "players", None)
    if pm is not None:
        from ..names import norm_name
        live = getattr(svc, "espn_injuries", {}) or {}

        def sidelined(name: str) -> str | None:
            return (live.get(norm_name(name)) or {}).get("status")
        sg.home_players = roster(pm, sg.home, sg.away, hp, sg.home_qb, sidelined, notes)
        sg.away_players = roster(pm, sg.away, sg.home, ap, sg.away_qb, sidelined, notes)
    return sg
