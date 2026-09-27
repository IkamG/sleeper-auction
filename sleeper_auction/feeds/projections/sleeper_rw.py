"""Sleeper's weekly projections (produced by RotoWire). Already on the request
path as sitstart.week_projections, which now keeps the full stat line."""
from sleeper_auction.feeds.projections import base

NAME = "sleeper"
LABEL = "Sleeper (RotoWire)"
PRODUCER = "rotowire"


def fetch_week(season, week):
    from sleeper_auction import sitstart
    from sleeper_auction.feeds import ids
    cw = ids.crosswalk()
    out = []
    for pid, v in sitstart.week_projections(week, season).items():
        info = cw["info"].get(pid) or {}
        r = base.row(info.get("name") or pid, info.get("pos"), info.get("team"),
                     {k: v["stats"].get(k) for k in v.get("stats") or {}},
                     pts=v.get("proj"), src_id=pid, id_kind="sleeper")
        out.append(r)
    return out
