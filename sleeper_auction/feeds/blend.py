"""Final weekly projection: market props blended with the consensus.

    final = w * props_mean + (1 - w) * base,  base = consensus median, else Sleeper

The weights are CHOSEN, not fitted (the user picked 0.55 at full coverage).
feeds/calibrate.py replaces them once cache/calibration.json holds four or
more weeks of scored results.
"""
import json
import os

from sleeper_auction.feeds import common

W_PROPS_FULL = 0.55      # props coverage >= 0.7
W_PROPS_PARTIAL = 0.25   # 0.3 <= coverage < 0.7
FULL_COV = 0.7
PARTIAL_COV = 0.3
MIN_CAL_WEEKS = 4


def weights():
    """(full, partial, source) -- calibration.json when it has enough weeks."""
    try:
        with open(common.cache_path("calibration.json"), encoding="utf-8") as f:
            cal = json.load(f)
        if cal.get("n_weeks", 0) >= MIN_CAL_WEEKS and cal.get("w_props") is not None:
            w = float(cal["w_props"])
            return w, w * W_PROPS_PARTIAL / W_PROPS_FULL, "calibrated (%d weeks)" % cal["n_weeks"]
    except (OSError, ValueError):
        pass
    return W_PROPS_FULL, W_PROPS_PARTIAL, "chosen"


def final(sleeper=None, consensus=None, props=None):
    """{"value", "basis": {props, consensus, sleeper}, "range", "weights"} or None."""
    base, basis = None, {}
    if consensus and consensus.get("median") is not None:
        base, basis = consensus["median"], {"consensus": 1.0}
    elif sleeper is not None:
        base, basis = sleeper, {"sleeper": 1.0}
    if base is None:
        return None
    full, partial, how = weights()
    w = 0.0
    if props and props.get("mean") is not None:
        cov = props.get("coverage") or 0.0
        w = full if cov >= FULL_COV else partial if cov >= PARTIAL_COV else 0.0
    val = w * props["mean"] + (1 - w) * base if w else base
    if w:
        basis = {k: round(v * (1 - w), 2) for k, v in basis.items()}
        basis["props"] = round(w, 2)
    rng = None
    if consensus and consensus.get("lo") is not None:
        rng = (consensus["lo"], consensus["hi"])
    return {"value": round(val, 2), "basis": basis, "range": rng, "weights": how}
