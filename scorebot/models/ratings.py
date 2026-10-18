"""NFL team ratings: Elo + EPA + quarterback value, blended by logistic regression.
Pure Python, no pandas.

Inputs are plain dicts parsed from nflverse CSVs (see nflverse.py):
  games: game_id, season, week, game_type, home, away, home_score, away_score, neutral
         (scores are None for unplayed games)
  team_weeks: {(game_id, team): {"epa": float, "plays": int, "pass_epa": float, "dropbacks": int}}
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from statistics import NormalDist


MARGIN_SD = 13.5            # NFL final margins vs expectation: ~13.5 pt std dev
_N = NormalDist()


def logit(p: float) -> float:
    p = min(max(p, 1e-6), 1 - 1e-6)
    return math.log(p / (1 - p))


def sigmoid(x: float) -> float:
    return 1 / (1 + math.exp(-x))


def prob_to_margin(p: float) -> float:
    p = min(max(p, 1e-4), 1 - 1e-4)
    return MARGIN_SD * _N.inv_cdf(p)


def margin_to_prob(m: float) -> float:
    return _N.cdf(m / MARGIN_SD)


# ===================================================================== Elo ===
@dataclass
class EloParams:
    k: float = 20.0
    hfa: float = 40.0           # home-field advantage in Elo points (~1.6 pts); tuned 2012-24
    revert: float = 0.4         # pull toward mean between seasons; tuned 2012-24
    mean: float = 1505.0


class Elo:
    def __init__(self, params: EloParams | None = None):
        self.p = params or EloParams()
        self.r: dict[str, float] = {}
        self.season: int | None = None

    def rating(self, team: str) -> float:
        return self.r.get(team, self.p.mean)

    def diff(self, home: str, away: str, neutral: bool) -> float:
        return self.rating(home) - self.rating(away) + (0 if neutral else self.p.hfa)

    def prob(self, home: str, away: str, neutral: bool) -> float:
        return 1 / (1 + 10 ** (-self.diff(home, away, neutral) / 400))

    def new_season(self, season: int) -> None:
        if self.season is not None and season != self.season:
            for t in self.r:
                self.r[t] = self.r[t] * (1 - self.p.revert) + self.p.mean * self.p.revert
        self.season = season

    def update(self, home: str, away: str, neutral: bool, hs: int, as_: int) -> None:
        d = self.diff(home, away, neutral)
        exp_home = 1 / (1 + 10 ** (-d / 400))
        actual = 1.0 if hs > as_ else 0.0 if hs < as_ else 0.5
        mov = abs(hs - as_)
        winner_diff = d if hs > as_ else -d
        mult = math.log(mov + 1) * (2.2 / (winner_diff * 0.001 + 2.2)) if mov else 1.0
        shift = self.p.k * mult * (actual - exp_home)
        self.r[home] = self.rating(home) + shift
        self.r[away] = self.rating(away) - shift


# ===================================================================== EPA ===
@dataclass
class EPAParams:
    decay: float = 0.94          # per-game weight decay (~11 game half-life); tuned 2012-24
    season_carry: float = 0.5    # how much of last season survives the offseason
    prior_plays: float = 250.0   # shrink early-season ratings toward 0


@dataclass
class _Acc:
    off: float = 0.0
    off_n: float = 0.0
    dfn: float = 0.0
    dfn_n: float = 0.0


class EPA:
    """Exponentially weighted offense / defense EPA per play."""

    def __init__(self, params: EPAParams | None = None):
        self.p = params or EPAParams()
        self.acc: dict[str, _Acc] = {}
        self.season: int | None = None

    def new_season(self, season: int) -> None:
        if self.season is not None and season != self.season:
            c = self.p.season_carry
            for a in self.acc.values():
                a.off, a.off_n, a.dfn, a.dfn_n = a.off * c, a.off_n * c, a.dfn * c, a.dfn_n * c
        self.season = season

    def team(self, t: str) -> tuple[float, float]:
        """(offense EPA/play, defense EPA/play allowed). Higher off / lower def = better."""
        a = self.acc.get(t, _Acc())
        pp = self.p.prior_plays
        return a.off / (a.off_n + pp), a.dfn / (a.dfn_n + pp)

    def net(self, t: str) -> float:
        o, d = self.team(t)
        return o - d

    def update(self, team: str, opp: str, off_epa: float, off_plays: int) -> None:
        """Record one team's offensive game (and the opponent's defensive game)."""
        k = self.p.decay
        a = self.acc.setdefault(team, _Acc())
        b = self.acc.setdefault(opp, _Acc())
        a.off, a.off_n = a.off * k + off_epa, a.off_n * k + off_plays
        b.dfn, b.dfn_n = b.dfn * k + off_epa, b.dfn_n * k + off_plays


# ====================================================================== QB ===
@dataclass
class QBParams:
    decay: float = 0.95          # per-start weight decay
    season_carry: float = 0.8    # QB skill is sticky year to year
    prior_dropbacks: float = 250.0
    replacement: float = -0.12   # pass EPA/dropback of an unknown / fringe QB
    dropbacks_per_game: float = 36.0
    share_decay: float = 0.8     # how fast "who's the usual starter" updates


REPLACEMENT = "REPLACEMENT"      # sentinel id: an unknown / fringe backup


class QB:
    """QB value = team pass EPA per dropback in his starts (shrunk toward replacement).

    The adjustment for a game is (starter value - usual starter value) x dropbacks,
    i.e. roughly how many points better/worse the offense is with today's QB.
    """

    def __init__(self, params: QBParams | None = None):
        self.p = params or QBParams()
        self.val: dict[str, list[float]] = {}           # qb_id -> [epa_sum, dropbacks]
        self.share: dict[str, dict[str, float]] = {}    # team -> {qb_id: start weight}
        self.names: dict[str, str] = {}
        self.last_team: dict[str, str] = {}
        self.season: int | None = None

    def new_season(self, season: int) -> None:
        if self.season is not None and season != self.season:
            for v in self.val.values():
                v[0] *= self.p.season_carry
                v[1] *= self.p.season_carry
        self.season = season

    def value(self, qb: str | None) -> float:
        if not qb:
            return self.p.replacement
        s, n = self.val.get(qb, (0.0, 0.0))
        pr = self.p.prior_dropbacks
        return (s + pr * self.p.replacement) / (n + pr)

    def usual(self, team: str) -> str | None:
        sh = self.share.get(team)
        return max(sh, key=sh.get) if sh else None

    def backup(self, team: str) -> str | None:
        sh = self.share.get(team, {})
        ranked = sorted(sh, key=sh.get, reverse=True)
        return ranked[1] if len(ranked) > 1 else None

    def adjustment(self, team: str, starter: str | None) -> float:
        """Points vs. the team's usual QB (negative = downgrade)."""
        usual = self.usual(team)
        if not starter or not usual or starter == usual:
            return 0.0
        return (self.value(starter) - self.value(usual)) * self.p.dropbacks_per_game

    def update(self, team: str, qb: str | None, pass_epa: float, dropbacks: int,
               name: str | None = None) -> None:
        if not qb:
            return
        if name:
            self.names[qb] = name
        self.last_team[qb] = team
        sh = self.share.setdefault(team, {})
        for k in list(sh):
            sh[k] *= self.p.share_decay
            if sh[k] < 0.001:
                del sh[k]
        sh[qb] = sh.get(qb, 0.0) + 1.0
        if dropbacks:
            v = self.val.setdefault(qb, [0.0, 0.0])
            v[0] = v[0] * self.p.decay + pass_epa
            v[1] = v[1] * self.p.decay + dropbacks


# ================================================================== Totals ===
@dataclass
class PointsParams:
    decay: float = 0.92
    season_carry: float = 0.5
    prior_games: float = 3.0
    league_avg: float = 22.0


class Points:
    """Exponentially weighted points scored / allowed -> expected game total."""

    def __init__(self, params: PointsParams | None = None):
        self.p = params or PointsParams()
        self.acc: dict[str, list[float]] = {}   # team -> [scored, allowed, weight]
        self.season: int | None = None

    def new_season(self, season: int) -> None:
        if self.season is not None and season != self.season:
            for a in self.acc.values():
                a[0] *= self.p.season_carry
                a[1] *= self.p.season_carry
                a[2] *= self.p.season_carry
        self.season = season

    def rates(self, t: str) -> tuple[float, float]:
        sc, al, w = self.acc.get(t, (0.0, 0.0, 0.0))
        pg, avg = self.p.prior_games, self.p.league_avg
        return (sc + pg * avg) / (w + pg), (al + pg * avg) / (w + pg)

    def expected_total(self, home: str, away: str) -> float:
        hs, ha = self.rates(home)
        as_, aa = self.rates(away)
        return (hs + aa) / 2 + (as_ + ha) / 2

    def update(self, home: str, away: str, hs: int, as_: int) -> None:
        k = self.p.decay
        for t, sc, al in ((home, hs, as_), (away, as_, hs)):
            a = self.acc.setdefault(t, [0.0, 0.0, 0.0])
            a[0], a[1], a[2] = a[0] * k + sc, a[1] * k + al, a[2] * k + 1


def fit_ols(X: list[list[float]], y: list[float], l2: float = 0.0) -> list[float]:
    """Ordinary least squares with an intercept column already in X."""
    n = len(X[0])
    A = [[sum(r[i] * r[j] for r in X) + (l2 if i == j and i > 0 else 0) for j in range(n)] for i in range(n)]
    b = [sum(r[i] * t for r, t in zip(X, y)) for i in range(n)]
    return _solve(A, b)


def calibration(pairs: list[tuple[float, int]]) -> list[dict]:
    """Fold to the favourite's side, then bucket. Each row: range, n, predicted, actual."""
    edges = [0.5, 0.55, 0.6, 0.65, 0.7, 0.8, 1.0001]
    out = []
    for lo, hi in zip(edges, edges[1:]):
        b = [(p if p >= .5 else 1 - p, y if p >= .5 else 1 - y) for p, y in pairs
             if lo <= (p if p >= .5 else 1 - p) < hi]
        if b:
            out.append({"lo": lo, "hi": min(hi, 1.0), "n": len(b),
                        "pred": sum(p for p, _ in b) / len(b), "actual": sum(y for _, y in b) / len(b)})
    return out


# =================================================================== Blend ===
FEATURES = ("elo", "epa", "qb")


def features(elo_diff: float, epa_diff: float, qb_pts: float = 0.0) -> dict[str, float]:
    """Scaled inputs. qb_pts = home QB adjustment minus away QB adjustment, in points."""
    return {"elo": elo_diff / 100.0, "epa": epa_diff * 10.0, "qb": qb_pts / 10.0}


def fit_logistic(rows: list[tuple[dict, int]], use: tuple[str, ...], l2: float = 1.0,
                 iters: int = 30) -> dict[str, float]:
    """Newton-Raphson logistic regression with a light L2 penalty. Returns weights incl 'bias'."""
    names = ("bias",) + use
    w = [0.0] * len(names)
    X = [[1.0] + [f[n] for n in use] for f, _ in rows]
    y = [t for _, t in rows]
    n = len(names)
    for _ in range(iters):
        g = [0.0] * n
        H = [[0.0] * n for _ in range(n)]
        for xi, yi in zip(X, y):
            p = sigmoid(sum(a * b for a, b in zip(w, xi)))
            r = p - yi
            s = p * (1 - p)
            for j in range(n):
                g[j] += r * xi[j]
                for k in range(j, n):
                    H[j][k] += s * xi[j] * xi[k]
        for j in range(1, n):            # don't penalise bias
            g[j] += l2 * w[j]
            H[j][j] += l2
        for j in range(n):
            for k in range(j):
                H[j][k] = H[k][j]
        step = _solve(H, g)
        w = [a - b for a, b in zip(w, step)]
        if max(abs(s) for s in step) < 1e-7:
            break
    return dict(zip(names, w))


def _solve(A: list[list[float]], b: list[float]) -> list[float]:
    n = len(b)
    M = [row[:] + [bv] for row, bv in zip(A, b)]
    for i in range(n):
        piv = max(range(i, n), key=lambda r: abs(M[r][i]))
        M[i], M[piv] = M[piv], M[i]
        for r in range(n):
            if r != i and M[i][i]:
                f = M[r][i] / M[i][i]
                M[r] = [a - f * c for a, c in zip(M[r], M[i])]
    return [M[i][n] / M[i][i] if M[i][i] else 0.0 for i in range(n)]


def predict(weights: dict[str, float], feats: dict[str, float]) -> float:
    return sigmoid(weights.get("bias", 0.0) + sum(weights.get(k, 0.0) * v for k, v in feats.items()))


def log_loss(pairs: list[tuple[float, int]]) -> float:
    eps = 1e-9
    return -sum(y * math.log(max(p, eps)) + (1 - y) * math.log(max(1 - p, eps))
                for p, y in pairs) / max(len(pairs), 1)


def brier(pairs: list[tuple[float, int]]) -> float:
    return sum((p - y) ** 2 for p, y in pairs) / max(len(pairs), 1)


# ============================================================ Full rebuild ===
@dataclass
class ModelState:
    elo: Elo
    epa: EPA
    weights: dict[str, float]          # logistic blend of FEATURES
    backtest: dict = field(default_factory=dict)
    last_completed: tuple[int, int] | None = None   # (season, week)
    qb: QB = field(default_factory=QB)
    points: Points = field(default_factory=Points)
    w_total: list[float] = field(default_factory=lambda: [0.0, 1.0])   # bias, raw model total
    total_sd: float = 13.5


TRAIN_FROM = 2002            # a few seasons of EPA history before rows count


def build(games: list[dict], team_weeks: dict, holdout_seasons: int = 2,
          elo_params: EloParams | None = None, epa_params: EPAParams | None = None,
          qb_params: QBParams | None = None) -> ModelState:
    """Walk history in order; record pre-game features; update ratings after each game.

    Blend weights are fit on everything except the last `holdout_seasons` completed
    seasons, evaluated there against simpler baselines (honest out-of-sample numbers),
    then refit on all data for live predictions.
    """
    elo, epa, qb, pts = Elo(elo_params), EPA(epa_params), QB(qb_params), Points()
    rows = []    # (season, feats, home_won)
    trows = []   # (season, raw model total, actual total)
    last = None
    games = sorted(games, key=lambda g: (g["season"], g["week"], g["game_id"]))
    for g in games:
        if g["home_score"] is None:
            continue
        s = g["season"]
        for m in (elo, epa, qb, pts):
            m.new_season(s)
        h, a, neu = g["home"], g["away"], g["neutral"]
        hq, aq = g.get("home_qb"), g.get("away_qb")
        qb_pts = qb.adjustment(h, hq) - qb.adjustment(a, aq)
        f = features(elo.diff(h, a, neu), epa.net(h) - epa.net(a), qb_pts)
        if g["home_score"] != g["away_score"]:
            rows.append((s, f, int(g["home_score"] > g["away_score"])))
        trows.append((s, pts.expected_total(h, a), g["home_score"] + g["away_score"]))

        elo.update(h, a, neu, g["home_score"], g["away_score"])
        pts.update(h, a, g["home_score"], g["away_score"])
        for team, opp, q, qn in ((h, a, hq, g.get("home_qb_name")), (a, h, aq, g.get("away_qb_name"))):
            tw = team_weeks.get((g["game_id"], team))
            if tw and tw["plays"]:
                epa.update(team, opp, tw["epa"], tw["plays"])
            qb.update(team, q, tw.get("pass_epa", 0.0) if tw else 0.0, tw.get("dropbacks", 0) if tw else 0, qn)
        last = (s, g["week"])

    train_rows = [(s, f, y) for s, f, y in rows if s >= TRAIN_FROM]
    seasons = sorted({s for s, *_ in train_rows})
    test_seasons = set(seasons[-holdout_seasons:]) if len(seasons) > holdout_seasons else set()
    tr = [(f, y) for s, f, y in train_rows if s not in test_seasons]
    te = [(f, y) for s, f, y in train_rows if s in test_seasons]

    bt = {}
    if tr and te:
        w = fit_logistic(tr, FEATURES)
        w_e = fit_logistic(tr, ("elo",))
        home_rate = sum(y for _, y in tr) / len(tr)
        model = [(predict(w, f), y) for f, y in te]
        elo_only = [(predict(w_e, {"elo": f["elo"]}), y) for f, y in te]
        home_only = [(home_rate, y) for _, y in te]
        bt = {
            "seasons": sorted(test_seasons), "games": len(te),
            "log_loss": {"model": log_loss(model), "elo": log_loss(elo_only), "home": log_loss(home_only)},
            "brier": {"model": brier(model), "elo": brier(elo_only), "home": brier(home_only)},
            "accuracy": {"model": sum((p > .5) == y for p, y in model) / len(model),
                         "elo": sum((p > .5) == y for p, y in elo_only) / len(elo_only),
                         "home": sum(y for _, y in te) / len(te)},
            "calibration": calibration(model),
        }

    # ---- totals: actual ~ a + b*raw_model_total, scored against a league-average guess
    ttr = [r for r in trows if r[0] >= TRAIN_FROM and r[0] not in test_seasons]
    tte = [r for r in trows if r[0] in test_seasons]
    w_total, total_sd = [0.0, 1.0], 13.5
    if len(ttr) > 200:
        wt = fit_ols([[1.0, x] for _, x, _ in ttr], [t for *_, t in ttr], l2=1.0)
        if tte:
            avg = sum(t for *_, t in ttr) / len(ttr)
            rm = math.sqrt(sum((t - (wt[0] + wt[1] * x)) ** 2 for _, x, t in tte) / len(tte))
            ra = math.sqrt(sum((t - avg) ** 2 for *_, t in tte) / len(tte))
            bt["totals"] = {"rmse_model": rm, "rmse_average": ra, "games": len(tte)}
        allt = [r for r in trows if r[0] >= TRAIN_FROM]
        w_total = fit_ols([[1.0, x] for _, x, _ in allt], [t for *_, t in allt], l2=1.0)
        res = [t - (w_total[0] + w_total[1] * x) for _, x, t in allt]
        total_sd = math.sqrt(sum(r * r for r in res) / len(res))

    all_rows = [(f, y) for _, f, y in train_rows]
    weights = fit_logistic(all_rows, FEATURES) if all_rows else {"bias": 0.0}
    bt["weights"] = {k: round(v, 3) for k, v in weights.items()}
    return ModelState(elo, epa, weights, bt, last, qb, pts, w_total, total_sd)


def game_prediction(state: ModelState, home: str, away: str, neutral: bool,
                    home_qb: str | None = None, away_qb: str | None = None) -> dict:
    """P(home wins) from the blend plus each component, projected margin, total and score."""
    ed = state.elo.diff(home, away, neutral)
    pd = state.epa.net(home) - state.epa.net(away)
    hq_adj = state.qb.adjustment(home, home_qb)
    aq_adj = state.qb.adjustment(away, away_qb)
    p = predict(state.weights, features(ed, pd, hq_adj - aq_adj))
    elo_p = 1 / (1 + 10 ** (-ed / 400))
    raw_total = state.points.expected_total(home, away)
    total = state.w_total[0] + state.w_total[1] * raw_total
    margin = prob_to_margin(p)
    return {"p_home": p, "p_elo": elo_p, "margin": margin,
            "elo_diff": ed, "epa_diff": pd,
            "home_qb_adj": hq_adj, "away_qb_adj": aq_adj,
            "home_qb": home_qb, "away_qb": away_qb,
            "home_usual_qb": state.qb.usual(home), "away_usual_qb": state.qb.usual(away),
            "total": total, "total_sd": state.total_sd,
            "home_pts": (total + margin) / 2, "away_pts": (total - margin) / 2}
