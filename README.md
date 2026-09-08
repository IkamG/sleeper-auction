# Sleeper auction draft board

Live value board for a Sleeper **auction** draft. Half-PPR. Python 3.9 stdlib only, no installs.

## Run

    python3 quick/draftboard.py --draft 1389690785410064385 --me 5

Then open http://localhost:8778/?draft=1389690785410064385&me=5
(the banner also prints a LAN URL so a phone on the same wifi can load it).

`--me` is your roster_id — 5 for IkamG in KHALISTAN FOOTBALL.

## What it does

Polls Sleeper every 5s, removes drafted players, and re-prices everyone left.

- **Live $** — inflation-adjusted value. Inflation = discretionary money left
  (after reserving $1 per open roster slot league-wide) divided by the expected
  cost of the players still needed to fill those slots. Computed on dollars
  *above* the $1 minimum, which is the part most calculators get wrong.
- **Base $** — 55% VORP model on Sleeper's half-PPR projections, 45% real market
  auction dollars, rescaled onto this league's $2400 pool.
- **'25 paid** — what this exact room paid for that player last season.
- **Edge** — model minus market. Positive means the room may let them go cheap.

Sleeper's public API exposes completed picks only; there is no public endpoint
for the player currently on the block. The nomination lookup box covers that —
type the nominated name, get Live $, your max bid, and a bid/pass verdict.

## Sources

Sleeper projections+ADP (native half-PPR, backbone), FantasyFootballCalculator
ADP (real drafts), ESPN auction values, Yahoo, CBS, FantasyPros ECR+auction.
Aggregated by **median**, so one bad scraper cannot move the board. Every source
is optional; the board degrades to whatever is reachable.

## Status

`quick/draftboard.py` is the working app and is self-contained.

`sources/`, `sleeper_draft.py` are from a larger parallel build whose remaining
stages (merge/valuation/server/UI) were cut off by a session limit. The six
adapters in `sources/` are finished and tested, and the board imports three of
them (cbs, yahoo, fantasypros) opportunistically.
