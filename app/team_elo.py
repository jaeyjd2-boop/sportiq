"""Pure Elo helpers for pre-game win probabilities in basketball, hockey and
American football.

Why Elo: it needs only what SportIQ stores for these sports (final scores,
home/away, kickoff time), it is easy to explain ("each result moves both
teams' ratings by how surprising it was"), and its parameters follow the
published FiveThirtyEight-style values rather than numbers fitted here.

The probability is for the final winner including overtime / shootout, which
is how SportIQ stores these results (status 'FT', detail 'Final/OT'). Ties are
practically absent (two in an NFL season), so the draw probability is 0.
"""
from dataclasses import dataclass
from math import log


@dataclass(frozen=True)
class EloParams:
    k: float              # rating change per game, before the margin multiplier
    home_advantage: float # Elo points added to the home side
    revert: float         # share pulled back toward the mean between seasons
    mov: str              # margin-of-victory multiplier style: "nba" or "log"
    min_games: int        # prior games each team needs before we publish


MEAN = 1505.0
SEASON_GAP_DAYS = 60   # a longer pause between two games of a sport = new season

# K, margin multipliers and season reversion follow the published
# FiveThirtyEight-style values. Home advantage was chosen from a small grid
# (0/25/50/75/100) on the 2025-26 stored season only and then checked on
# 2026-27 games it never saw; stored home win rates are 52-55%, lower than the
# older published values assume.
PARAMS = {
    "basketball": EloParams(k=20, home_advantage=50, revert=0.25, mov="nba", min_games=8),
    "hockey": EloParams(k=6, home_advantage=25, revert=0.3, mov="log", min_games=8),
    "american_football": EloParams(k=20, home_advantage=25, revert=1 / 3, mov="log", min_games=4),
}

MODEL_VERSION = {
    "basketball": "elo-basketball-v0.1",
    "hockey": "elo-hockey-v0.1",
    "american_football": "elo-american-football-v0.1",
}


def expected(diff):
    """Probability the side with +diff Elo points wins."""
    return 1.0 / (1.0 + 10 ** (-diff / 400.0))


def _mov_multiplier(style, margin, winner_diff):
    margin = abs(margin)
    if margin == 0:
        return 1.0
    if style == "nba":
        return (margin + 3) ** 0.8 / (7.5 + 0.006 * winner_diff)
    return log(margin + 1) * 2.2 / (winner_diff * 0.001 + 2.2)


def _revert(ratings, params):
    return {t: MEAN + (r - MEAN) * (1 - params.revert) for t, r in ratings.items()}


def run_ratings(games, params, as_of=None):
    """Replay ``games`` in time order; return (ratings, games_played).

    ``games``: iterable of dicts with home, away, home_score, away_score and
    start_time (datetime). Teams may be any hashable identifier. When
    ``as_of`` (the kickoff being predicted) starts a new season, the
    between-season pull toward the mean is applied to the result as well.
    """
    ratings, played = {}, {}
    last_time = None
    for g in sorted(games, key=lambda g: g["start_time"]):
        if last_time is not None and (g["start_time"] - last_time).days > SEASON_GAP_DAYS:
            ratings = _revert(ratings, params)
        last_time = g["start_time"]
        h, a = g["home"], g["away"]
        rh, ra = ratings.get(h, MEAN), ratings.get(a, MEAN)
        diff = rh + params.home_advantage - ra
        p_home = expected(diff)
        hs, as_ = float(g["home_score"]), float(g["away_score"])
        actual = 1.0 if hs > as_ else 0.0 if hs < as_ else 0.5
        winner_diff = diff if hs > as_ else -diff if hs < as_ else 0.0
        delta = params.k * _mov_multiplier(params.mov, hs - as_, winner_diff) * (actual - p_home)
        ratings[h], ratings[a] = rh + delta, ra - delta
        played[h], played[a] = played.get(h, 0) + 1, played.get(a, 0) + 1
    if as_of is not None and last_time is not None and (as_of - last_time).days > SEASON_GAP_DAYS:
        ratings = _revert(ratings, params)
    return ratings, played


def predict(ratings, home, away, params):
    """(p_home, p_away, elo_home, elo_away) for a game not yet played."""
    rh, ra = ratings.get(home, MEAN), ratings.get(away, MEAN)
    p_home = expected(rh + params.home_advantage - ra)
    return p_home, 1.0 - p_home, rh, ra
