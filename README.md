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

`sources/` holds ranking-source adapters from a larger parallel build whose
remaining stages were cut off by a session limit. The board imports three of
them opportunistically (`cbs`, `yahoo`, `fantasypros`, plus the shared
`base`); `espn`, `ffc` and `sleeper_src` are unused, since `draftboard.py`
carries its own fetchers for those three. They are kept because they are
finished and tested, and are the natural place to extend if a source needs
more depth than the inline version provides.

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
