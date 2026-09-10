# sleeper-auction

Four tools for a Sleeper **auction** league, half-PPR by default:

| | |
|---|---|
| **Draft board** | Live auction values during the draft, inflation-adjusted as the room spends |
| **Analysis** | Post-draft grading of every team against market value and the room's own clearing price |
| **Sit / start** | Weekly start-or-sit calls from Vegas lines, matchups, usage and AI reasoning |
| **Waivers** | Who to add, what to drop, and how much FAAB to bid |

Python 3.9+, **standard library only**. No install step, no dependencies, no build.
One optional extra unlocks the AI layer — see [AI analysis](#ai-analysis).

---

## Quick start

    git clone https://github.com/IkamG/sleeper-auction && cd sleeper-auction
    ./run.sh --draft <draft_id> --me <roster_id>

Then open the URL it prints. `run.sh` also prints a LAN address so a phone on
the same wifi can load it during a draft.

**Don't know your draft id?** Start the server with no arguments and look
yourself up by Sleeper username:

    ./run.sh
    curl localhost:8778/api/user/<your_username>/drafts

`--me` is your `roster_id` (1-12), which that same endpoint returns.

---

## The tools

All four are served by the same process and share a tab bar.

### Draft board — `/board`

Polls Sleeper every 5 seconds, drops drafted players, and re-prices everyone
left.

- **Live $** — inflation-adjusted value. Inflation is the discretionary money
  left league-wide (after reserving $1 for every open roster slot) divided by
  the expected cost of the players still needed to fill those slots. It is
  computed on dollars *above* the $1 minimum, which is the part most auction
  calculators get wrong: dividing raw values systematically overprices the
  middle of the board.
- **Base $** — 55% VORP model on half-PPR projections, 45% real market auction
  dollars, both rescaled onto this league's pool.
- **Edge** — model minus market. Positive means the room may be sleeping on him.
- **'25 paid** — what *this exact room* paid for that player last season, which
  no public ranking source knows.

Sleeper's public API exposes **completed picks only** — there is no public
endpoint for the player currently on the block, and the live nomination flows
over their private app websocket. The nomination lookup box covers that gap:
type the name being auctioned and get Live $, your max bid, tier context, and a
bid-or-pass verdict.

### Analysis — `/analysis`

Grades every team once the draft is done.

- **vs Market** — value acquired minus price paid.
- **vs Room** — the same, against a least-squares fit of what this league
  actually paid onto model value. This matters because the model runs rich at
  the top of the board, so grading purely against it hands free credit to
  whoever bought the studs. The room line asks *did you beat these twelve
  people*, which is the question that decides a league.
- Grades are on the **league curve**, not an absolute scale — a 12-team auction
  is zero-sum, and absolute grading buries half the league in Ds including
  teams with positive surplus.

Final score weights value captured (45%) and best-lineup projected points
(55%): surplus you cannot start is worth less than surplus you can.

Click any team for a full roster breakdown. Also available as
`/analysis?text=1` and `/api/analysis`.

### Sit / start — `/` or `/sitstart?week=N`

A deterministic layer computes every number; Claude then argues both sides of
each call. **Claude is never asked to estimate a number it could be handed** —
it is asked for the judgment the numbers do not settle.

The risk posture is arithmetic, not preference. Projected as a heavy favourite,
you take the floor, because a zero is the only way to lose. As a heavy
underdog, you start the boom/bust player *deliberately*, because a median week
loses anyway. **The same player gets opposite calls depending on the opponent.**

Signals fed to the model:

| Signal | Source | Why it matters |
|---|---|---|
| Implied team total | Vegas spread + O/U | The market's own forecast of how much offense exists to share |
| Weekly projection | Sleeper, native half-PPR | The baseline |
| Boom/bust profile | Last season's actual weekly scores | A projection is a mean; volatility says whether it's reliable |
| Defense vs position | Computed from real results | A team total can't say "bad for a WR *specifically*" |
| Snap share + trend | nflverse | The earliest sign a projection has gone stale |
| Depth chart | Sleeper, live | Current team, current season — never stale |
| Injuries | ESPN | Status, body part, expected return |
| Weather | Open-Meteo | Wind above ~15 mph suppresses passing and kicking; domes short-circuit |
| WR/CB matchup | Optional chart image | Per-receiver, against the cornerback projected to cover him |

Defense-vs-position is **computed, not scraped**: every weekly score is
attributed to the defense it was scored against and aggregated by position, so
it is what actually happened.

Snap history records the team it was earned on and flags `team_changed`, so a
role a player no longer has is never presented as current.

#### WR/CB matchup charts

Pass a chart image, or the article URL containing them — every image on the
page is collapsed to its full-size original and filtered to table-shaped
images:

    ./run.sh --draft <id> --me <n> --wrcb "https://example.com/wr-cb-matchups-week-1"

The model reads the table directly, so there is no parser to maintain. It is
told the charts are partial and to report an unlisted receiver as unknown
rather than guessing.

### Waivers — `/waivers?week=N`

Ranks every unrostered player on two forces that pull against each other:

- **Need** — weekly points above the player he would actually replace *in your
  lineup*. A fact about your roster, not about him.
- **Quality** — his season-long auction value in the abstract. A genuinely
  valuable player is worth rostering even without a need.

A pure-need model misses league-winners at positions you happen to be set at; a
pure-quality model tells a team with two elite QBs to bid on a third. Each
position carries a **hurdle** — the weekly edge required before an upgrade is
worth a roster spot. It is high at single-slot positions (QB, TE, K, DEF)
because the backup never plays, and low at RB/WR where a spare slots into the
flex. A player below his hurdle is still shown, but discounted and capped at
token FAAB.

**FAAB pricing is anchored on measured demand.** Sleeper publishes how many
leagues added each player in the last 24 hours, across millions of leagues —
crowd-sourced waiver demand measured rather than opined. High demand on a
player who does not help your roster is a reason to let him go, not to chase
him: demand sets his *price*, not his value to you.

The league's FAAB budget is read from Sleeper, so bids come back in real
dollars as well as percentages.

Recent r/fantasyfootball posts are pulled from the Atom feed for injury and
role leads. They are passed to the model as explicitly unverified chatter —
useful for a lead, never asserted as fact.

---

## AI analysis

The sit/start and waiver narratives need a Claude backend. Three are supported, in
priority order:

1. **`ANTHROPIC_API_KEY` set** → official `anthropic` SDK if installed, else
   raw HTTP. Billed as API credits.
2. **Claude Code installed** → runs through `claude -p` on your existing
   subscription. **No API key needed.**
3. **Neither** → every computed number still renders; only the narrative is
   skipped.

Option 2 is the default path for most users and costs nothing extra.

**Pages never block on it.** `/sitstart` and `/waivers` render every computed
number immediately — typically in about a second — and the analysis is
generated on a background thread, polled by the page, and dropped in when
ready. A reload while it is thinking reuses the running job rather than paying
for the same analysis twice, and a finished one is served straight from cache.

    GET /api/ai/<job_id>    -> {status: pending|done|error, html, result}

The JSON endpoints (`/api/sitstart`, `/api/waivers`) stay synchronous, since a
scripted caller wants the whole answer in one response.

---

## Layout

    sleeper_auction/
      board.py        HTTP server, source aggregation, live auction values
      analysis.py     post-draft grading vs market and room clearing price
      sitstart.py     weekly start/sit
      waivers.py      waiver wire: pickups, drops, FAAB bids
      valuation.py    VORP -> auction dollars, tiers, confidence
      sleeper.py      Sleeper API client (drafts, picks, budgets, user lookup)
      sources/        ranking-source adapters
    run.sh            launcher; picks the best available interpreter
    cache/            on-disk HTTP cache (gitignored)

Every module runs standalone:

    python3 -m sleeper_auction.board    --draft <id> --me <n> [--port 8778]
    python3 -m sleeper_auction.analysis --draft <id> [--team <owner>] [--json]
    python3 -m sleeper_auction.sitstart --draft <id> --me <n> --week <n> [--no-ai]
    python3 -m sleeper_auction.waivers  --draft <id> --me <n> --week <n> [--limit 25]

### HTTP API

    GET  /                                 sit/start (falls back to the board
                                           when no draft is set)
    GET  /board                            draft board
    GET  /analysis                         grades      (?text=1 for plain text)
    GET  /sitstart?week=N                  sit/start   (?text=1, ?noai=1)
    GET  /api/board?draft=&me=             live board JSON, polled every 5s
    GET  /api/analysis?draft=              grades JSON
    GET  /waivers?week=N                   waiver wire (?text=1, ?noai=1)
    GET  /api/sitstart?draft=&me=&week=    sit/start JSON
    GET  /api/waivers?draft=&me=&week=     waiver wire JSON
    GET  /api/user/<username>/drafts       find drafts by Sleeper username
    GET  /api/values                       the valued player pool
    POST /api/refresh                      rebuild the source pool

---

## Ranking sources

| Source | Contributes |
|---|---|
| Sleeper | Native half-PPR projections + ADP, and the only source with Sleeper player ids |
| FantasyFootballCalculator | Consensus ADP from real half-PPR drafts |
| ESPN | Real auction dollars |
| Yahoo | Real auction dollars + draft analysis |
| CBS | Expert consensus rankings |
| FantasyPros | ECR + auction calculator |

Aggregated by **median**, so one bad scraper cannot move the board. Every
source is optional and wrapped — the board degrades to whatever is reachable.

Cross-source identity is a normalized `POS|name` key that folds accents, strips
generational suffixes, resolves nickname variants, and keys defenses by team.
This is the highest-risk part of the pipeline: a failed join silently drops a
source's opinion of a player.

`sleeper_src` and `espn` prefer the adapters in `sources/`, falling back to
inline fetchers in `board.py`. `sleeper_src` matters most — Sleeper is the one
source the board cannot run without, and the adapter retries with backoff,
serves a stale cache when the API is unreachable, and re-requests per position
if the combined call fails. `ffc` stays inline; the adapter returns the same
players.

---

## Valuation

`valuation.py` owns the auction math, with a simpler implementation in
`board.py` as fallback.

- Replacement level per position, with flex slots allocated by which positions
  actually own the best flex-eligible players rather than an even split.
- `dollars_per_vorp = (pool - roster_spots) / total_vorp`, so every rostered
  player costs at least $1 and the board sums to `teams * budget`.
- Tiers cut at genuine gaps, capped at `MAX_TIER_SIZE` so a flat position
  cannot collapse into one enormous tier.
- Players with no published projection are filled from an isotonic
  points-vs-rank curve instead of being stranded at $1.
- `value_conf` (0-1) from source coverage and disagreement, and `proj_source`
  marking whether a projection was published or inferred. Both flow through to
  the analysis page and the sit/start payload, so an inferred number is never
  presented as a published one.

K and DEF are held at $1. The K1-vs-K12 gap is real in projection but not
predictable, and pricing it taxes the RB/WR pool that actually decides a
league.

---

## Known limitations

- **The model runs rich at the top.** Backtested against a completed 12-team
  auction: r = 0.95 and ~$5 mean absolute error on contested players, but a
  consistent **−$4** bias in the $40+ tier. Bid accordingly.
- Defense-vs-position and snap trends fall back to last season until enough
  weeks of the current one exist. The payload always records which season was
  used.
- Rookies have no volatility history, so floor/ceiling read as unknown rather
  than being invented.
- Weather uses the maximum wind across the forecast window rather than the wind
  at kickoff, so it over-flags.
- WR/CB charts are partial by nature; unlisted receivers are reported unknown.
- FAAB bids are derived from need, demand and positional scarcity, not from a
  published expert consensus. Two dedicated FAAB sites were evaluated:
  [faabtastic](https://www.faabtastic.com/) is sign-in gated with no current
  season data, and [faablab](https://www.faablab.app/) is a client-rendered app
  with nothing server-side to read. Sleeper's trending-adds counts are used
  instead, which are measured rather than opined.
- Sleeper does not expose FAAB spent to date, so budgets shown are the season
  budget, not what remains.

---

## Local environment notes

Not required to run the app — it works on Apple's system Python 3.9 with no
setup. These record how this particular machine is configured.

Default interpreter is pyenv 3.14.7, with Homebrew's 3.14.7 behind it and
Apple's 3.9.6 last. `.venv` is built on the pyenv Python and carries the
`anthropic` SDK. `run.sh` prefers `.venv`, then `python3` on PATH, then
`/usr/bin/python3`.

<details>
<summary>Homebrew python@3.14 + macOS 26.2 pyexpat breakage</summary>

The `python@3.14` bottle shipped a `pyexpat` linked against
`/usr/lib/libexpat.1.dylib` at a newer version than macOS 26.2 provided,
breaking `pyexpat` → `plistlib` → `platform.mac_ver()` → pip, which failed with
`ValueError: invalid literal for int() with base 10: ''`. Repointed at
Homebrew's own expat:

    SO=/opt/homebrew/Cellar/python@3.14/3.14.7/Frameworks/Python.framework/Versions/3.14/lib/python3.14/lib-dynload/pyexpat.cpython-314-darwin.so
    install_name_tool -change /usr/lib/libexpat.1.dylib /opt/homebrew/opt/expat/lib/libexpat.1.dylib "$SO"
    codesign -f -s - "$SO"

macOS 26.6 fixed the underlying system library, verified by testing the module
against `/usr/lib` directly, so this is now redundant. Left in place because it
points at Homebrew's expat and is insulated from future system changes. A
`brew upgrade python@3.14` reverts it.
</details>
