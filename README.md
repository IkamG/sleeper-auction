# Sleeper auction draft board

Live value board for a Sleeper **auction** draft. Half-PPR. Python 3.9 stdlib only, no installs.

## Run

    ./run.sh --draft 1389690785410064385 --me 5

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

## Layout

    sleeper_auction/
      board.py        HTTP server, source aggregation, live auction values
      analysis.py     post-draft grading vs market and room clearing price
      sitstart.py     weekly start/sit: Vegas, matchup, usage, AI reasoning
      valuation.py    VORP -> auction dollars, tiers, confidence
      sleeper.py      Sleeper API client (drafts, picks, budgets, user lookup)
      sources/        ranking-source adapters (base, cbs, yahoo, fantasypros,
                      espn, ffc, sleeper_src)
    run.sh            launcher; picks the best available interpreter
    cache/            on-disk HTTP cache (gitignored)

Every module runs standalone: `python3 -m sleeper_auction.sitstart --help`.

## Sources

Sleeper projections+ADP (native half-PPR, backbone), FantasyFootballCalculator
ADP (real drafts), ESPN auction values, Yahoo, CBS, FantasyPros ECR+auction.
Aggregated by **median**, so one bad scraper cannot move the board. Every source
is optional; the board degrades to whatever is reachable.

## Status

`sleeper_auction/board.py` is the app's entry point and HTTP server.

`sleeper_auction/sources/` holds the ranking-source adapters. `cbs`, `yahoo` and `fantasypros`
are the only way those sources are read. `sleeper_src` and `espn` are preferred
over the inline fetchers in `draftboard.py` when importable, and the inline
versions remain as a fallback so a bare checkout of `quick/draftboard.py` still
runs on its own.

`sleeper_src` matters most: Sleeper is the one source the board cannot do
without, and the adapter retries with backoff, falls back to a stale cache when
the API is unreachable, re-requests per position if the combined call fails,
and drops the ~2,400 teamless, unprojected rows Sleeper keeps in its database.
The inline version does none of that and simply exits if the call fails.

`ffc` stays inline: the adapter returns the same ~250 players, so there is
nothing to gain.

`sleeper_auction/valuation.py` owns the auction math when importable, with the inline version
in `board.py` as fallback. It fixes two real bugs the inline math has:
tiers cap at `MAX_TIER_SIZE` so a flat position cannot collapse into one
27-player tier, and its normalisation lands on exactly `teams * budget` instead
of leaking a few dollars. It also fills players with no published projection
from an isotonic points-vs-rank curve (318 of them, previously stranded at $1)
and returns a 0-1 `value_conf` from source coverage and disagreement.

`sleeper_auction/sleeper.py` backs `GET /api/user/<username>/drafts`, so a draft can be
found by Sleeper username instead of requiring the id up front.

`value_conf` and `proj_source` flow through to both the analysis page and the
sit/start payload, so neither the grade view nor the model treats an inferred
projection as a published one.

## Python

The app is stdlib-only and still runs on Apple's system Python 3.9 with **zero
dependencies** — that fallback is deliberate and intact. Nothing below is
required to use it.

Default interpreter is now **pyenv 3.14.7** (`pyenv global 3.14.7`), with
Homebrew 3.14.7 behind it and Apple's 3.9.6 last. `~/.zshrc` wires Homebrew,
pyenv and nvm, in that order; backups at `~/.zshrc.bak.*`.

`.venv` is built on the pyenv Python and carries the `anthropic` SDK for the
sit/start AI layer. `run.sh` picks `.venv` -> `python3` on PATH ->
`/usr/bin/python3`, so a fresh clone with no setup still works.

macOS 26.6 fixed the underlying system `libexpat`, verified by testing the
module against `/usr/lib` directly. The patch below is now redundant but is
left in place, since it points at Homebrew's own expat and is insulated from
future system changes. The pyenv build compiles its own `pyexpat` and was never
affected.

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
