"""Opt-in preference for non-extreme candidates with comparable validity."""
import json
import math


def load_policy(path):
    policy = json.loads(path.read_text(encoding="utf-8"))
    if (policy.get("version") != "soft_extremes_20261027_v1"
            or policy.get("maximum_mae_increase_days") != {"10": 3.0, "000": 2.0}
            or set(policy.get("excluded_alternatives", [])) != {"000", "10"}
            or policy.get("require_same_failed_cases") is not True):
        raise ValueError("Unexpected soft-extreme policy; create a new version for changes")
    return policy


def choose(ranked, scores, policy=None):
    """Use the original ranking unchanged; compare only equal-failure solutions."""
    baseline = ranked[0]
    result = dict(baseline=baseline, winner=baseline, alternative="", delta=None,
                  threshold=None, reason="pure_mae" if policy is None else "non_extreme_unchanged")
    if policy is None or baseline not in policy["maximum_mae_increase_days"]:
        return result
    result["threshold"] = policy["maximum_mae_increase_days"][baseline]
    alternatives = [g for g in ranked if g not in policy["excluded_alternatives"]
                    and scores[g]["failures"] == scores[baseline]["failures"]]
    if not alternatives:
        result["reason"] = "extreme_no_comparable_alternative"
        return result
    alternative = alternatives[0]
    delta = float(scores[alternative]["mat_mean"]) - float(scores[baseline]["mat_mean"])
    if not math.isfinite(delta) or delta < -1e-9:
        raise ValueError("Inconsistent original maturity-error ranking")
    result.update(alternative=alternative, delta=max(0.0, delta))
    # Numerical tolerance only; these values come from full annual diagnostics.
    if delta <= result["threshold"] + 1e-9:
        result.update(winner=alternative, reason="non_extreme_within_margin")
    else:
        result["reason"] = "extreme_clear_mae_advantage"
    return result
