# sleeper-auction

Six tools for a Sleeper **auction** league, half-PPR by default:

| | |
|---|---|
| **Sit / start** | Weekly start-or-sit calls from Vegas lines, matchups, usage and AI reasoning |
| **Waivers** | Who to add, what to drop, and how much FAAB to bid |
| **Look ahead** | Long-term roster weak points, and stashes that pay off later |
| **Trades** | Offers that improve both lineups and are fair by the trade market |
| **Draft board** | Live auction values during the draft, inflation-adjusted as the room spends |
| **Analysis** | Post-draft grading of every team against market value and the room's own clearing price |

Python 3.9+, **standard library only**. No install step, no dependencies, no build.
One optional extra unlocks the AI layer — see [AI analysis](#ai-analysis).

---

## Quick start

    git clone https://github.com/IkamG/sleeper-auction && cd sleeper-auction
    ./run.sh --draft <draft_id> --me <roster_id>

Then open the URL it prints — it lands on **sit/start**, the page you want most
weeks. The draft board lives at `/board`. `run.sh` also prints a LAN address so a phone on
the same wifi can load it during a draft.

**Don't know your draft id?** Start the server with no arguments and look
yourself up by Sleeper username:

    ./run.sh
    curl localhost:8778/api/user/<your_username>/drafts

`--me` is your `roster_id` (1-12), which that same endpoint returns.

---

## The tools

All of them are served by the same process and share a tab bar.

### Sit / start — `/` or `/sitstart?week=N`

A deterministic layer computes every number; Claude then argues both sides of
each call. **Claude is never asked to estimate a number it could be handed** —
it is asked for the judgment the numbers do not settle.

The page answers a different question depending on **phase**, detected from
real game states:

| Phase | Page reads | What it does |
|---|---|---|
| **pre** | `sit / start` | Every player is a decision. Argue both sides, commit. |
| **mid** | `live` | Started games are results, not decisions. Verdicts are given only for players who have not kicked off, weighed against the **live** margin. Banked points are shown for both sides. |
| **post** | `results` | No verdicts. A retrospective: who beat their projection, who missed, which pre-game signals actually predicted it, and which were variance. |

Both totals are computed the same way — actual points for finished games,
projections for the rest — so the margin never compares a live number against a
pre-game one.

The risk posture is arithmetic, not preference. Projected as a heavy favourite,
you take the floor, because a zero is the only way to lose. As a heavy
underdog, you start the boom/bust player *deliberately*, because a median week
loses anyway. **The same player gets opposite calls depending on the opponent.**

Signals fed to the model:

| Signal | Source | Why it matters |
|---|---|---|
| Implied team total | Vegas spread + O/U (ESPN; nflverse schedule fills games ESPN has no line for) | The market's own forecast of how much offense exists to share |
| Weekly projection | Consensus of six projection sources, blended with player props | The baseline. The consensus median, with props weighted 0.55 when they price 70%+ of the line (0.25 at 30–70%). The number of sources and their range show under it; `proj_sleeper` keeps Sleeper's own number |
| Expert rank | FantasyPros weekly ECR | A rank and start/sit grade, not points, so it sits beside the projection rather than in it |
| Player props | Kalshi ladders (keyless) + The Odds API over/unders (`ODDS_API_KEY`) | The market's own view of this week, converted to league points. Moves on news faster than projections; a missing line on a starter is itself a signal. Also gives **this week's** floor and ceiling (p10/p90, marked *mkt*) |
| Boom/bust profile | Last season's actual weekly scores | A projection is a mean; volatility says whether it's reliable |
| Defense vs position | Computed from real results | A team total can't say "bad for a WR *specifically*" |
| Snap share + trend | nflverse, joined by PFR id | The earliest sign a projection has gone stale |
| Target share, air-yards share, WOPR | nflverse weekly stats, recomputed from summed targets | For receivers a better usage read than snaps: on the field is not the same as targeted |
| Practice trajectory | nflverse injury report + Sleeper, snapshotted through the week | DNP → LP → FP is on track; FP → LP late in the week is the worst sign in the data. Veteran rest days are labelled, not flagged |
| Expected points (xFP) | ffopportunity | Points his opportunities were worth. Far above means touchdown luck due to regress; far below means the role is better than the results |
| Red-zone and goal-line share | ffopportunity play-by-play | Where touchdowns come from |
| Efficiency context | Next Gen Stats + PFR advanced | Only when a rate is in the best or worst 10% at the position. Explains *why* a rate is high or low; never a projection |
| Depth chart | Sleeper, live | Current team, current season — never stale |
| Injuries | ESPN | Status, body part, expected return |
| Weather | Open-Meteo, at kickoff (kickoff to +3 h) | Wind above ~15 mph suppresses passing and kicking. Roof comes from the nflverse schedule; neutral-site games (Rio, London, …) use the venue's own coordinates |
| News | ESPN per-player fantasy news (RotoWire blurbs) + RotoWire's NFL feed | Reported, dated news, two items a player, last seven days. Shown as a 📰 flag with the text on hover |
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

Ranks every unrostered player on three forces that pull against each other:

- **Need** — weekly points above the player he would actually replace *in your
  lineup*. A fact about your roster, not about him. It is **negative for
  almost every add worth making in September**: a good claim usually does not
  crack your lineup the week you make it.
- **Upside** — role evidence a weekly projection cannot see. Per-touch
  production against usage within the position (YPC for backs, YPT for
  receivers), whether a backfield has an owner, whether a rookie is already
  top-2 on the depth chart, snap share, and nflverse usage: a target share
  up 8+ points over the last three games, a WOPR of 0.45+, expected points
  3+ a game above what he has scored, goal-line work on a light role. Each
  needs two games or more. This is the half that wins leagues.
- **Quality** — his season-long auction value in the abstract. A genuinely
  valuable player is worth rostering even without a need.

A pure-need model misses league-winners at positions you happen to be set at; a
pure-quality model tells a team with two elite QBs to bid on a third. Each
position carries a **hurdle** — the weekly edge required before an upgrade is
worth a roster spot. It is high at single-slot positions (QB, TE, K, DEF)
because the backup never plays, and low at RB/WR where a spare slots into the
flex. A player below his hurdle is still shown, but discounted and capped at
token FAAB.

**Kickers and defences are streamed, not added**, so they rank in their own
strip and never compete for a place on the board. The best available defence
is worth about a point a week over the one you already have, that edge does not
persist because you stream again next week, and no kicker has ever been the
pickup that changed a season. Quarterbacks join them unless one clears the
hurdle over your starter. Ranking these alongside skill players on this week's
projection alone is how a waiver board ends up with no running backs on it —
the positions where claims are actually won produce negative need every time.
Tight ends are capped at four seats for the same reason.

A gap is weighted by the volume behind it. Ten yards per target on 26 targets
is arithmetic on a tiny denominator, not a breakout, and it is scored as such.

**FAAB pricing is anchored on measured demand.** Sleeper publishes how many
leagues added each player in the last 24 hours, across millions of leagues —
crowd-sourced waiver demand measured rather than opined. High demand on a
player who does not help your roster is a reason to let him go, not to chase
him: demand sets his *price*, not his value to you.

**FAAB is real.** Sleeper rosters carry `waiver_budget_used`, so the header
shows what you actually have left, and what every opponent has left. No bid
is ever suggested above the richest opponent's remaining budget plus a dollar:
nobody can outbid that. The league's completed waiver claims give **this
room's clearing prices** (median winning bid as a share of the budget,
contested vs uncontested, by position); once there are eight or more, bids are
scaled by what this room pays for a contested add against the 5% community
convention (clamped to 0.6–1.6×).

From week 3, **quality** is rest-of-season value, not the preseason auction
price: FantasyCalc trade value where the player has one, otherwise his
rest-of-season projected points, both mapped onto the draft's dollar curve so
the units do not change. Candidates carry reported news from the last week. Bids price **need plus upside**, not need
alone — pricing off this week's lineup help put every September add at zero,
which is how you lose the back you needed in October because you would not bid
on him in week 2. A player nobody else is adding is capped near the minimum:
FAAB is a sealed auction against the room, so there is no sense paying for
competition that does not exist.

Recent r/fantasyfootball posts are pulled from the Atom feed for injury and
role leads. They are passed to the model as explicitly unverified chatter —
useful for a lead, never asserted as fact.

### Look ahead — `/lookahead`

Answers the question that decides a season rather than a week: who is being
under-used relative to how well he performs, and where does this roster break
in six weeks.

The core signal is the **opportunity gap** — efficiency percentile minus usage
percentile, within position. Backs are judged on yards per carry, receivers on
yards per target.

> A back at 5.4 yards a carry on eight touches a game is not a good player
> being wasted by accident; he is a coaching decision, and those reverse. A
> back at 3.6 on twenty touches has his job because nobody better is there —
> there is no gap to close, and the job can be taken from him.

Your own starters carry the same number, so a negative gap flags a job at risk
even while the player is still producing.

Candidates are tagged by *why* they are worth a bench spot:

| Tag | Means |
|---|---|
| `efficient-unused` | Produces per touch, isn't given touches. The strongest standalone signal. |
| `handcuff` | Directly behind a workhorse. Insurance — pays only on an injury. |
| `open-committee` | No back on that team owns the job. Upside needs no injury. |
| `rookie-in-line` | Rookie already first or second on the depth chart. |
| `red-zone role` | Scoring chances without volume. Touchdown-dependent. |
| `efficient-unused` + NGS | When rush yards over expected or separation agrees with the gap, the why says so: independent evidence the efficiency is the player's, not the scheme's. |
| `rising-share` | Target share up 8+ points over the last three games, on 4+ targets a game. Usage leads production. |
| `buy-low` | Expected points from his opportunities run 3+ a game ahead of what he has scored. |
| ROS $ / FP ROS | Rest-of-season value on the draft dollar curve and FantasyPros' rest-of-season rank, with the 30-day trade-value trend and strength of the remaining schedule in the payload. A starter whose trade value is falling is named as a weak point. |
| `sell-high` | Your roster only: scoring 4+ a game above expected over three games, driven by touchdowns. Named as a weak point while he is still producing. |

Age is a direct discount — a stash is a bet on future value, and a 31-year-old
career backup with a great per-touch rate has no career arc left for the role
to arrive in. Players on IR or PUP are excluded.

The rail shows **thin positions** (no bench cover) and **bye clusters**
(several starters idle the same week). Every page's rail also has a small
**Data** panel giving each feed's freshness.

### Trades — `/trades`

Finds trades another manager might actually accept. A package is proposed only
when it passes two tests:

- **Both lineups improve.** Each side's starting lineup is rebuilt with the
  league's own slots (QB, RB×2, WR×2, TE, FLEX, K, DEF) on rest-of-season
  projected points, before and after. `My +` and `Their +` must both be
  positive. In a 2-for-1 the side receiving two players cuts its worst bench
  player, and the page names him.
- **Fair by the market.** The two sides' consensus trade values (FantasyCalc,
  plus KeepTradeCut when enabled) are within 10% of each other.

The search is deterministic: 1-for-1, 2-for-1 and 1-for-2 against every other
roster, using players worth $3 or more in trade value (kickers and defences
are never traded). Results are ranked by what they add to your lineup, at most
three per partner, so the list is not one roster permuted. Up to three
**value asks** lean your way by up to 20% and are labelled as asks the other
manager may decline. The AI then argues both sides of each offer, writes the
pitch from the other manager's point of view (what their lineup gains), and
flags players whose two value sources disagree.

The rail shows where you sit against the league-median starter at each
position, your bench players who would start elsewhere, which opponents need
what, and your sell-high candidates (scoring well above expected points on
touchdowns).

An empty list is a real answer: if your starters are strong and your bench
has no trade value, no offer helps both sides, and the page says so.

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

**Analyses are cached permanently**, keyed by week *and phase*, so a pre-week
read, a Sunday-evening read and the post-week retrospective are three separate
stored answers rather than one overwriting the next. Nothing re-runs on its
own; append `?refresh=1` or click **re-run** on a cached panel to rebuild it.

The JSON endpoints (`/api/sitstart`, `/api/waivers`) stay synchronous, since a
scripted caller wants the whole answer in one response.

---

## Layout

    sleeper_auction/
      board.py        HTTP server, source aggregation, live auction values
      analysis.py     post-draft grading vs market and room clearing price
      sitstart.py     weekly start/sit
      waivers.py      waiver wire: pickups, drops, FAAB bids
      lookahead.py    long-term weak points and stash candidates
      trades.py       two-way trade finder
      valuation.py    VORP -> auction dollars, tiers, confidence
      sleeper.py      Sleeper API client (drafts, picks, budgets, user lookup)
      sources/        ranking-source adapters
      feeds/          in-season data feeds (see "In-season data feeds")
        common.py     cached fetch (gunzip, stale-if-error), status, parallel, snapshots
        ids.py        ID crosswalk: gsis/espn/pfr/cbs/... -> Sleeper id
        scoring.py    league scoring_settings -> points(stat line)
        nflverse.py   usage, snaps, practice, xFP, red zone, schedule (nflverse/ffverse)
        props/        kalshi.py, oddsapi.py, implied.py (markets -> points), merge
        projections/  one adapter per projection site + consensus.py
        blend.py      final weekly number: props over the consensus
        prefetch.py   background refresh of the slow scrapers
        news.py       ESPN per-player news (RotoWire), RotoWire RSS
        values.py     FantasyCalc, KeepTradeCut (opt-in), FantasyPros ROS, ROS points, SOS
        league.py     real FAAB remaining, waiver history, the room's prices
        calibrate.py  scores every source against actual points; fits the blend
    tests/            unittest, network-free (fixtures in tests/fixtures)
    run.sh            launcher; picks the best available interpreter
    cache/            on-disk HTTP cache (gitignored)

Every module runs standalone:

    python3 -m sleeper_auction.board    --draft <id> --me <n> [--port 8778]
    python3 -m sleeper_auction.analysis --draft <id> [--team <owner>] [--json]
    python3 -m sleeper_auction.sitstart --draft <id> --me <n> --week <n> [--no-ai]
    python3 -m sleeper_auction.waivers  --draft <id> --me <n> --week <n> [--limit 25]
    python3 -m sleeper_auction.lookahead --draft <id> --me <n> [--limit 20]
    python3 -m sleeper_auction.trades   --draft <id> --me <n> [--limit 15] [--no-ai] [--json]
    python3 -m sleeper_auction.feeds.ids --probe     # crosswalk sizes per id kind
    python3 -m sleeper_auction.feeds.nflverse --probe --season 2026
    python3 -m sleeper_auction.feeds.props --probe --week N  # series, joins, coverage
    python3 -m sleeper_auction.feeds.props.implied --fit     # refit prop distribution constants
    python3 -m sleeper_auction.feeds.projections --probe --week N  # rows, join rate, fetch time
    python3 -m sleeper_auction.feeds.calibrate --season 2026       # MAE by source, fit weights

Tests (offline, stdlib `unittest`, run on Python 3.9 and 3.14):

    python3 -m unittest discover -s tests

### HTTP API

    GET  /                                 sit/start (falls back to the board
                                           when no draft is set)
    GET  /board                            draft board
    GET  /analysis                         grades      (?text=1 for plain text)
    GET  /sitstart?week=N                  sit/start   (?text=1, ?noai=1)
    GET  /api/board?draft=&me=             live board JSON, polled every 5s
    GET  /api/analysis?draft=              grades JSON
    GET  /waivers?week=N                   waiver wire (?text=1, ?noai=1)
    GET  /lookahead                        long-term outlook and stashes
    GET  /api/lookahead?draft=&me=         look-ahead JSON
    GET  /trades                           trade finder (?text=1, ?noai=1, ?refresh=1)
    GET  /api/trades?draft=&me=            trade finder JSON
    GET  /api/sitstart?draft=&me=&week=    sit/start JSON
    GET  /api/waivers?draft=&me=&week=     waiver wire JSON
    GET  /api/user/<username>/drafts       find drafts by Sleeper username
    GET  /api/values                       the valued player pool
    GET  /api/feeds                        freshness, errors and ID-join rates
                                           for every in-season feed
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

## In-season data feeds

`feeds/` holds the data the weekly tools use beyond Sleeper's own projections.
The draft-ranking `sources/` keep their one-shape contract; feeds have
different shapes and cadences. Rules every feed follows:

- **Optional and wrapped.** A failed feed degrades that one signal. Every page
  renders all of its existing numbers with every feed down. A feed whose live
  fetch fails serves its last cached copy, whatever its age, and is marked
  stale in `/api/feeds`.
- **IDs over names.** `feeds/ids.py` maps every external ID to a Sleeper ID,
  built from Sleeper's player DB and filled from the
  [DynastyProcess](https://github.com/dynastyprocess/data) ID table (the only
  source of PFR, CBS, NFL.com and KeepTradeCut IDs, and of GSIS IDs Sleeper
  leaves blank). Names are a last resort, and an ambiguous name is dropped
  rather than guessed. Hit, fallback and miss counts show in `/api/feeds`.
- **League scoring.** `feeds/scoring.py` scores any stat line with the
  league's own `scoring_settings` (TE premium and bonuses included), falling
  back to standard half-PPR.
- `FEEDS_DISABLED=props,news,...` switches feeds off (useful for checking the
  degraded path). There is no config file.

| Feed | Source | Cache | Used for |
|---|---|---|---|
| Weekly player stats | nflverse `stats_player_week_<season>` | 6 h | target share, air-yards share, WOPR, aDOT, carry share |
| Snap counts | nflverse `snap_counts_<season>` | 24 h | snap share by Sleeper id |
| Injury report | nflverse `injuries_<season>` + Sleeper practice fields | 2 h Wed–Sat, else 12 h | practice trajectory, game designation |
| Expected points | ffopportunity `ep_weekly_<season>` (full PPR, converted to league scoring) | 6 h | xFP, buy-low / sell-high |
| Play-by-play | ffopportunity `ep_pbp_rush/pass_<season>` | 6 h | red-zone and goal-line share, team pass rate over expected |
| PFR advanced | nflverse `advstats_week_{rec,rush}_<season>` | 12 h | drops, yards after contact, broken tackles (joined by PFR id) |
| Next Gen Stats | nflverse `ngs_{receiving,rushing}` (season-total rows) | 12 h | separation, cushion, YAC over expected, rush yards over expected |
| News | ESPN per-player news, RotoWire RSS, Sleeper `news_updated` | 30 min | reported news on sit/start and waivers (fetched in the background) |
| Trade values | FantasyCalc (real trades; KeepTradeCut opt-in) | 12 h | rest-of-season quality, trend |
| ROS rank / points | FantasyPros ROS ECR; Sleeper future-week projections, FanDuel REMAINING | 12 h | look-ahead, waiver quality |
| League FAAB | Sleeper rosters and transactions | 10 min | FAAB left, opponent budgets, room prices |
| Schedule | nflverse `games.csv` | 2 h | kickoff time, roof, neutral venues, fallback lines, bye weeks |

### Player props

| Provider | Access | What it gives |
|---|---|---|
| [Kalshi](https://kalshi.com) | Public market data, no key | Per-game ladders, one market per threshold ("70+ receiving yards"): passing yards/TDs/INTs/attempts/completions, rushing yards/attempts, receptions, receiving yards, and a 1+/2+/3+ touchdown ladder. A ladder is a whole survival curve, so it gives a mean *and* a floor and ceiling. Players join by id: Kalshi's player UUID is Sleeper's `kalshi_id` |
| [The Odds API](https://the-odds-api.com) | `ODDS_API_KEY` environment variable | DraftKings and FanDuel over/unders for passing yards and TDs, rushing yards, receptions, receiving yards and anytime TD. The free plan (500 credits a month) includes player props; a full slate costs about 90 credits, so each event is fetched at most once a day and fetching stops at a 60-credit reserve (`ODDS_API_RESERVE`) |

Per player and market, a healthy Kalshi ladder (3+ priced rungs, quoted
within 5 cents on average, or real open interest) wins; otherwise the
sportsbook line; otherwise whatever Kalshi has, marked `kalshi-thin`. Each
book's line is de-vigged and converted separately, then averaged, because
books often hang different numbers. Markets are converted to expected stats
(ladder integration, or a skewed distribution around an over/under line with
constants fitted on 2024–25 weekly data), then to points with the league's
scoring. Anything without a market comes from Sleeper's projected stat line;
`coverage` says how much of the number the markets supplied. Games that have
kicked off are dropped: a live market prices the game state, not the player.

Put the key in a `.env` file at the repo root (git-ignored; the app reads it
at startup, and an exported variable overrides it):

    ODDS_API_KEY=...               # optional; Kalshi works without it

nflverse publishes only the **latest** practice status per player-week, so
the Wednesday → Friday trajectory is built by snapshotting each fetch into
`cache/history/`. That history lives only on your machine, starts when the app
first runs in a week, and is gone if `cache/` is cleared. Rates fall back to
last season until the current one has two weeks, and every payload records
which season it used.

### Calibration

The blend weights are a starting guess, so the app keeps score. Through the
week the background thread snapshots every player's pre-kickoff lines
(Sleeper, each consensus source, the consensus, the props mean and the final
number) into `cache/history/`. Once a week is complete,

    python3 -m sleeper_auction.feeds.calibrate --season 2026

compares each against what players actually scored (league scoring, players
who played), prints mean absolute error and bias by source and position, fits
the props weight by grid search, and writes `cache/calibration.json`. With
four or more weeks in it, the blend uses the fitted weight instead of the
chosen 0.55, and the page says which it is using. The post-week retrospective
also receives each source's error for your players, so "which signal was
right" is answered from data. The history is local to your machine: clear
`cache/` and it starts again.

### Weekly projection sources

| Source | Produced by | Format | Joins on |
|---|---|---|---|
| Sleeper | RotoWire | JSON | Sleeper id |
| ESPN | Mike Clay | JSON | ESPN id |
| CBS Sports | CBS | HTML table | CBS id |
| FanDuel Research | numberFire | GraphQL (current week only; no K/DEF) | name + team |
| FFToday | FFToday | HTML table | name + team |
| FantasySharks | FantasySharks | CSV | name + team |

Every source's **stat line** is scored with the league's own scoring, so they
are on one scale; a source's own points are used only for K and DEF. One value
per producer (Yahoo is skipped: it shows FantasyPros' numbers). Median when
three or more sources have a player, mean otherwise, with the range and spread
alongside. A source projecting a player near zero while the others do not is
kept and flagged `source_thinks_out`: that site usually has news the others
have not priced in. The consensus stat line also fills the components props do
not cover. Tables are parsed by header label, never by column position.

The scrapers never run while a page loads. A background thread refreshes them
(hourly on Sunday morning, every 3 hours from Tuesday noon, else every 12) and
pages read the cache; a page that finds a source missing starts a refresh and
says so in the Data panel. NFL.com is not a source: its fantasy projections
were retired (the pages now redirect to nfl.com news, and the API says ESPN is
the NFL's official fantasy game).

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
- Kalshi's per-player markets are thin (open interest is often zero), so a
  ladder is only trusted when it is quoted tightly. The market/Sleeper blend
  weight (0.5) is chosen, not fitted, until there are weeks of results to
  calibrate against.
- On a cold start the consensus has only Sleeper until the background refresh
  finishes (about 30 seconds per week, because the scrapers pause between
  pages). The Data panel shows which sources are missing.
- There is no routes-run data (no TPRR/YPRR) in any free feed. Snap share and
  target share are the proxies.
- WR/CB charts are partial by nature; unlisted receivers are reported unknown.
- FAAB bids are derived from need, demand and positional scarcity, not from a
  published expert consensus. Two dedicated FAAB sites were evaluated:
  [faabtastic](https://www.faabtastic.com/) is sign-in gated with no current
  season data, and [faablab](https://www.faablab.app/) is a client-rendered app
  with nothing server-side to read. Sleeper's trending-adds counts are used
  instead, which are measured rather than opined, alongside this league's own
  clearing prices.
- KeepTradeCut is opt-in (`KTC_ENABLED=1`) because its terms prohibit
  scrapers, and as of the last probe its redraft page embeds only three
  players (the rest load in the browser), so it contributes almost nothing.
  Every consumer works on FantasyCalc alone.

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
