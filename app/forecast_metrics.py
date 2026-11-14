"""Pure scoring helpers for three-way football outcomes."""
from math import isfinite, log

OUTCOMES = ("home", "draw", "away")

def score_probabilities(probabilities, actual):
    """Return normalized multiclass Brier (0..2/3) and log loss."""
    if actual not in OUTCOMES:
        raise ValueError("invalid actual outcome")
    try:
        probs = {key: float(probabilities[key]) for key in OUTCOMES}
    except (KeyError, ValueError, TypeError) as exc:
        raise ValueError("missing or invalid probabilities") from exc
    if not all(isfinite(v) and 0 <= v <= 1 for v in probs.values()):
        raise ValueError("probabilities must be finite and between 0 and 1")
    if abs(sum(probs.values()) - 1) > 0.01:
        raise ValueError("probabilities must sum to one")
    brier = sum((probs[key] - int(key == actual)) ** 2 for key in OUTCOMES) / 3
    loss = -log(max(probs[actual], 1e-15))
    return brier, loss
