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

## Python

The app runs on the macOS system Python 3.9 with **zero dependencies** — that is
deliberate and still true. Nothing below is required to use it.

Homebrew Python 3.14 is now installed and on PATH (`eval "$(brew shellenv)"` was
added to `~/.zshrc`; a backup of the original is at `~/.zshrc.bak.*`), plus a
`.venv` with the current `anthropic` SDK for the sit/start analyzer's AI layer.

### One local fix worth remembering

Homebrew's `python@3.14` bottle ships a `pyexpat` linked against
`/usr/lib/libexpat.1.dylib` at a newer version than macOS 26.2 provides. That
breaks `pyexpat` -> `plistlib` -> `platform.mac_ver()` -> pip, which fails with
`ValueError: invalid literal for int() with base 10: ''`. Repointed at
Homebrew's own expat:

    SO=/opt/homebrew/Cellar/python@3.14/3.14.7/Frameworks/Python.framework/Versions/3.14/lib/python3.14/lib-dynload/pyexpat.cpython-314-darwin.so
    install_name_tool -change /usr/lib/libexpat.1.dylib /opt/homebrew/opt/expat/lib/libexpat.1.dylib "$SO"
    codesign -f -s - "$SO"

A `brew upgrade python@3.14` will revert this until the bottle is rebuilt.
