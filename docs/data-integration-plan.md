# Data integration plan: advanced stats, consensus projections, player props, news and ROS values

Written 2026-09-25, during week 3 of the 2026 season: Thursday night is final, and Sunday is still to come.
This document is a hand-off to the agent implementing it. It records what was
researched, what was verified and how, and exactly where each new signal lands
in the existing code.

---

## Decisions (from the user, 2026-09-25)

- **Scope:** implement every free source in this plan. That includes all of section 2.7, with
  PFR advanced stats and Next Gen Stats now in scope (Phase 1b), plus RotoWire RSS as a
  second news feed.
- **The Odds API: yes.** The user has a key. Read it **only** from the `ODDS_API_KEY`
  environment variable. Never write it into the repo, a fixture, a log line or the cache
  directory. It could not be tested from the research sandbox (the proxy blocks
  `api.the-odds-api.com`), so the first step of Phase 2 is `--probe` with the key set. The
  probe settles whether this plan includes player props.
- **Blend weight: start at 0.55 props** (full coverage), as specified in Phase 3. Phase 5
  calibration can still move it once there are 4 weeks of data.
- **`/trades`: yes, as a separate feature** (section 10). It depends on Phase 4's value feeds
  but ships on its own branch/PR, after the data phases.
- **KeepTradeCut: yes, alongside FantasyCalc.** They are different datasets: FantasyCalc
  values come from real completed trades, KTC's from crowdsourced keep/trade/cut votes. KTC
  has no API and its terms prohibit scrapers, so its adapter is **opt-in**
  (`KTC_ENABLED=1`) and off by default. See Phase 4.

## 0. Summary

| Requested feature | Obtainable? | Best source | Cost | Verified | Lands in |
|---|---|---|---|---|---|
| Target share, air yards, WOPR, RACR, aDOT | **Yes** | nflverse `stats_player_week_2026` | Free, keyless | Downloaded and parsed 2026-09-25 | sit/start, waivers, look-ahead |
| Practice reports (DNP/LP/FP) | **Yes**, latest status only | nflverse `injuries_2026` + Sleeper `practice_participation` | Free | Downloaded 2026-09-25 | sit/start, waivers |
| Expected fantasy points (luck / regression) | **Yes** (not requested; recommended) | ffverse `ep_weekly_2026` | Free | Downloaded 2026-09-25 | waivers, look-ahead |
| Weekly projections from several sites | **Yes**, 6 independent sources | Sleeper (RotoWire), ESPN (Mike Clay), CBS, NFL.com, FanDuel/numberFire, FFToday, FantasySharks | Free, scraping | Endpoints from ffanalytics (fixed Sep 9 2026); **probe first** | new consensus layer |
| Player props (yards, TDs) | **Yes** | **Kalshi** public market data (keyless); The Odds API (key) as an option | Kalshi free; Odds API 500 credits/mo free, props may need a paid plan | Kalshi via official SDK 3.30.0; **probe first** | sit/start projection blend, floor/ceiling |
| News | **Yes** | ESPN per-player fantasy news (RotoWire blurbs), Sleeper `news_updated` | Free | Endpoint shape from two public integrations; **probe first** | sit/start, waivers |
| Trade values | **Yes** | FantasyCalc redraft values (`sleeperId` included) | Free | Params/fields from public integrations; **probe first** | waivers quality term, look-ahead, optional `/trades` |
| ROS rankings | **Yes** | FantasyPros ROS ECR (same `ecrData` parser already in repo), FanDuel `REMAINING` projections | Free | FP pages not fenced (repo notes); **probe first** | waivers, look-ahead |

Also recommended, all free: Sleeper `waiver_budget_used` and transaction bids (removes the
"FAAB spent is unknown" limitation and lets bids be calibrated against this room),
nflverse schedules (kickoff time, roof and next week's lines, which fixes the weather
limitation), and red-zone / goal-line usage plus team pass rate over expected (PROE) from the
ffopportunity play-by-play files.

**How things were verified.** The research sandbox could reach GitHub (nflverse and ffverse
release assets, raw files) and PyPI, but not Sleeper, ESPN, CBS, NFL.com, FantasyPros,
FantasyCalc, Kalshi or The Odds API. The nflverse, ffopportunity, schedule and crosswalk files
were downloaded and parsed. The other endpoints were confirmed from maintained open-source
clients (ffanalytics as of 2026-09-09, `kalshi_python_sync` 3.30.0, oddsapiR, a Go Sleeper
client) and public integrations. Those rows say **probe first**: run the module's
`--probe` on an unrestricted network before building on it.

**Recommended order:** Phase 0 foundation → Phase 1 nflverse → Phase 1b PFR/NGS → Phase 2
props → Phase 3 consensus projections → Phase 4 news, ROS/trade values and league FAAB →
Phase 5 calibration. After that, the separate `/trades` feature (section 10). Each phase is
independently shippable and leaves the app working if its feeds are down.

---

## 1. Rules for the implementing agent

These restate the conventions of this codebase. Breaking any of them is a regression.

1. **Standard library only, Python 3.9 compatible.** No `pandas` or `requests`, and no `X | Y`
   type unions or `match`. Parquet is off the table, so use `.csv.gz` / `.csv`. `gzip`, `csv`,
   `concurrent.futures`, `statistics.NormalDist`, `zoneinfo` and `html.parser` are all
   available in 3.9.
2. **Every feed is optional and wrapped.** A failed fetch degrades that one signal. Pages must
   render every existing number with every new feed down. Follow `board.src_extra()` and
   the `try/except` around role data in `waivers.build_board()`.
3. **Probe before you trust.** Every adapter module starts with a docstring recording
   what was verified, when, and the traps (see `sources/fantasypros.py` and
   `sources/yahoo.py`). Endpoints marked **probe first** below were confirmed through
   maintained third-party code, not a live request. Run the module's `--probe` and write
   what you saw into the docstring before building on it.
4. **Claude is never handed a number it could be given, and never asked to invent one.**
   Every new field goes into the payload with provenance (source, season, weeks, sample
   size, as-of time), and gets a paragraph in the relevant `SYSTEM` prompt saying what it
   means and how much to trust it.
5. **Sample-size honesty.** Gate rate stats on volume, as `waivers.score_candidate`
   already does. A target share over one game is flagged, not sold.
6. **Joins are the highest-risk step.** Join on IDs (section 3.3), never on names alone, and
   surface unmatched counts in the status endpoint.
7. **Nothing slow on the request path.** Multi-page scrapes (CBS, NFL.com, FFToday,
   FantasySharks, ESPN news) run in a background prefetcher. Pages read cache.
8. **Update README.md** in the same commit as each phase: signals table, sources table,
   layout, known limitations.

---

## 2. Research findings

### 2.1 nflverse: advanced stats, snaps and practice reports (verified)

All nflverse release assets download as
`https://github.com/<org>/<repo>/releases/download/<tag>/<file>`. That URL 302-redirects to
`release-assets.githubusercontent.com`, and `urllib` follows the redirect. Formats are
`.csv`, `.csv.gz`, `.parquet`, `.rds` and `.qs`. Use `.csv.gz` and fall back to `.csv`.

| Dataset | URL (tag / file) | Key | Updated | What it gives |
|---|---|---|---|---|
| Weekly player stats | `nflverse-data` / `stats_player/stats_player_week_2026.csv` | `player_id` (GSIS, `00-00xxxxx`) | Nightly. On 2026-09-25 it held weeks 1–2 plus TNF of week 3 (2,294 rows) | `targets`, `receptions`, `receiving_yards`, `receiving_air_yards`, `receiving_yards_after_catch`, `target_share`, `air_yards_share`, `wopr`, `racr`, `carries`, `rushing_yards`, `passing_*`, `passing_cpoe`, `*_epa`, `fantasy_points_ppr`, `opponent_team`, `game_id` |
| Snap counts | `nflverse-data` / `snap_counts/snap_counts_2026.csv` | `pfr_player_id` | Daily (2026-09-25 11:01 UTC) | `offense_snaps`, `offense_pct` (0–1), `team`, `opponent`, `week` |
| Injuries / practice | `nflverse-data` / `injuries/injuries_2026.csv` | `gsis_id` | Daily (2026-09-25) | `report_status` (Out/Doubtful/Questionable/blank), `practice_status` (Full/Limited/Did Not Participate), `practice_primary_injury`, `report_primary_injury`, `week`, `team`, `position` |
| Expected fantasy points | `ffverse/ffopportunity` / `latest-data/ep_weekly_2026.csv` | `player_id` (GSIS) | Daily (2026-09-25) | `total_fantasy_points_exp`, `receptions_exp`, `rec_yards_gained_exp`, `*_touchdown_exp`, actuals, `_diff`, team totals |
| xFP play-by-play | `ffverse/ffopportunity` / `latest-data/ep_pbp_rush_2026.csv`, `ep_pbp_pass_2026.csv` | `rusher_player_id` (pass file: probe) | Daily | `yardline_100`, `goal_to_go`, `xpass`, `down`, `score_differential`, `vegas_wp`, `implied_total`: enough for red-zone share and PROE |
| Schedules and lines | `https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv` | `game_id`, teams | Several times a week. Week 3 and week 4 lines already present | `gameday`, `gametime` (ET), `spread_line` (**home** margin, positive = home favoured), `total_line`, moneylines, `roof`, `surface`, `away_rest`/`home_rest`, `stadium_id` |
| PFR advanced | `nflverse-data` / `pfr_advstats/advstats_week_{rec,rush,pass}_2026.csv` | `pfr_player_id` | 2026-09-24 | Drops, broken tackles, yards after contact |
| Next Gen Stats | `nflverse-data` / `nextgen_stats/ngs_{receiving,rushing,passing}.csv.gz` | GSIS | Weekly | Separation, cushion, rush yards over expected. Use the **combined** files: the per-season 2024+ files are ~600-byte stubs |

Traps found while probing:

- **Scoring.** `ep_weekly` is **full PPR**. Verified: Trey McBride with 9 receptions, 95
  yards and 1 TD has `rec_fantasy_points = 24.5`. For league scoring use
  `x = total_fantasy_points_exp + (rec_value − 1.0) × receptions_exp`. For half-PPR that
  is `− 0.5 × receptions_exp`.
- **Practice reports keep only the latest status.** `injuries_2026` has exactly **one row per
  player-week** (0 duplicates). The Wednesday → Thursday → Friday trajectory that
  matters is not in the file. It must be built by snapshotting daily (section 3.4).
- **`practice_primary_injury = "Not injury related - resting player"`** is a veteran rest
  day, not an injury. It must never read as an injury flag.
- A blank `report_status` means no game designation, which is the normal state. A player
  missing from the file entirely is not on the report.
- `stats_player_week` has rows only for players who recorded a stat. Team totals (for
  share denominators) are sums over the rows of each `(game_id, team)`.
- nflverse writes the Rams as `LA`. `board.nteam()` already maps `LA → LAR`.
- There is no routes-run data here (no TPRR/YPRR). Don't promise it. Snap share and target
  share are the proxies.

### 2.2 ID crosswalk (verified)

- **Sleeper's player DB** (`/v1/players/nfl`, already cached as `slp-players`) carries
  `gsis_id`, `espn_id`, `yahoo_id`, `rotowire_id`, `rotoworld_id`, `sportradar_id`,
  `stats_id`, `fantasy_data_id`, `swish_id`, `opta_id`, `pandascore_id`, `oddsjam_id`,
  `kalshi_id`, and also `practice_participation`, `practice_description`, `injury_status`,
  `injury_body_part`, `injury_notes`, `news_updated` (epoch ms) and `team_changed_at`.
  These come from a maintained Go client's `Player` struct. Probe one record and
  confirm. **Strip whitespace from `gsis_id`**: older Sleeper records carry a leading space.
- **DynastyProcess `db_playerids.csv`**
  (`https://raw.githubusercontent.com/dynastyprocess/data/master/files/db_playerids.csv`,
  12,508 rows, current as of today) maps `sleeper_id`, `gsis_id`, `pfr_id`, `espn_id`,
  `yahoo_id`, `fantasypros_id`, `cbs_id`, `nfl_id`, `rotowire_id`, `ktc_id`,
  `fleaflicker_id` and more. It covers 2026 rookies: 146 skill-position rookies, 135 with a
  `sleeper_id` and 131 with a `gsis_id`. This is the only source of `pfr_id` (for snaps),
  `cbs_id` and `nfl_id`.

### 2.3 Weekly projection sources

Status comes from `ffanalytics` (R), which scrapes all of these, was last fixed on
2026-09-09 ("fix fanduel season scrape", "transitioning css selector to xpath", July 2026),
and has one open source bug: FantasyPros projections return only 10 rows per position.
That matches the fence this repo already documents in `sources/fantasypros.py`.

| Source | Who produces it | Endpoint | Format | IDs | Notes |
|---|---|---|---|---|---|
| Sleeper | **RotoWire** | `api.sleeper.com/projections/nfl/{season}/{week}?season_type=regular&position[]=…` (already used) | JSON with a full `stats` dict | Sleeper | Keep the whole `stats` dict, not just `pts_half_ppr`. It is the filler for props. |
| ESPN | Mike Clay | `lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{season}/segments/0/leaguedefaults/3?view=kona_player_info` with an `X-Fantasy-Filter` header | JSON | ESPN | Weekly line = entry in `player.stats[]` with `statSourceId==1`, `statSplitTypeId==1`, `scoringPeriodId==week`. Stat IDs: 0 pass_att, 1 pass_cmp, 3 pass_yd, 4 pass_td, 19 pass_2pt, 20 pass_int, 23 rush_att, 24 rush_yd, 25 rush_td, 26 rush_2pt, 53 rec (41 alias), 42 rec_yd, 43 rec_td, 44 rec_2pt, 58 targets, 72 fum_lost. Filter template in 4.3. |
| CBS | CBS staff | `www.cbssports.com/fantasy/football/stats/{POS}/{season}/{week}/projections/{ppr\|nonppr}/` | HTML table | CBS id in player link | Stat lines are the same under either scoring slug. 2 s between pages. |
| NFL.com | NFL | `fantasy.nfl.com/research/projections?position={1\|2\|3\|4\|7\|8}&statCategory=projectedStats&statSeason={season}&statType=weekProjectedStats&statWeek={week}&count=…` | HTML table | NFL id | Position codes: QB 1, RB 2, WR 3, TE 4, K 7, DST 8. Paginated by `offset`. |
| FanDuel / numberFire | numberFire | `POST https://fdresearch-api.fanduel.com/graphql`, `operationName: GetProjections`, `input: {type: WEEKLY\|REMAINING, position: NFL_SKILL\|NFL_KICKER\|NFL_D_ST, sport: NFL}`, header `Origin: https://www.fanduel.com` | GraphQL JSON | numberFire id | **Current week only** (no week argument). Check `gameInfo.gameTime` falls in the requested week. `REMAINING` gives ROS projections. Field list in 4.3. |
| FFToday | FFToday | `www.fftoday.com/rankings/playerwkproj.php?Season={s}&GameWeek={w}&PosID={10\|20\|30\|40\|80\|99}&LeagueID=1&cur_page={n}` | HTML | name only | PosID: QB 10, RB 20, WR 30, TE 40, K 80, DST 99, but **weekly pages have no DST**. RB 2 pages, WR 3. Column names changed in Sep 2025, so parse headers, don't index columns. |
| FantasySharks | FantasySharks | `www.fantasysharks.com/apps/bert/forecasts/projections.php?csv=1&Sort=&League=-1&Position={1,2,4,5,7,6}&scoring=1&Segment={874+week+8}&uid=4` | **CSV** | name | Segment base for 2026 is 874 (2025 was 842): week N → `882 + N`. Two receiving columns share a name; rename the duplicates. |
| FantasyPros weekly ECR | 100+ experts, **rank** consensus | `www.fantasypros.com/nfl/rankings/half-point-ppr-{rb,wr,te,flex}.php`, `qb.php`, `k.php`, `dst.php` | `var ecrData = {...}` in the page | FP id | Not fenced, served cookieless via CloudFront. Carries a start/sit grade and an opponent string per player. Ranks, not points. robots.txt crawl-delay is 5 s. |
| Yahoo | **FantasyPros** | n/a | n/a | n/a | **Skip.** ffanalytics dropped it because Yahoo now shows FantasyPros projections. It would double-count. |

### 2.4 Player props

**Primary: Kalshi public market data (keyless, official).**

- Base `https://external-api.kalshi.com/trade-api/v2`, with `https://api.elections.kalshi.com/trade-api/v2`
  as the documented alternate. Both come from the official `kalshi_python_sync` 3.30.0
  configuration. Read endpoints need **no auth**.
- Kalshi lists weekly NFL player ladders: passing yards, passing TDs, rushing yards,
  receiving yards, receptions and anytime TD, **one market per threshold** ("Player: N+
  receiving yards"). Series seen in public code include `KXNFLPASSATT`, `KXNFLPASSCOMP`,
  `KXNFLREC`, `KXNFLRSHATT` and `KXNFLPASSINT`. Discover the rest; don't hardcode
  (section 4.2).
- Market fields (from SDK `models/market.py`): `ticker`, `event_ticker`, `title`,
  `yes_sub_title`, `status`, **`yes_bid_dollars` / `yes_ask_dollars` / `last_price_dollars`
  as fixed-point dollar strings** (for example `"0.5600"`, meaning P = 0.56), `volume_fp`,
  `open_interest_fp`, `floor_strike`, `cap_strike`, `strike_type`, `custom_strike`,
  `primary_participant_key`, `expected_expiration_time`.
- A ladder is a whole survival curve P(X ≥ k), not one line. That gives a proper mean *and*
  this week's floor and ceiling. It is a better input than a single over/under.

**Optional: The Odds API (official, keyed).**

- `GET https://api.the-odds-api.com/v4/sports/americanfootball_nfl/events?apiKey=…` is **free**
  (does not count against quota).
- `GET /v4/sports/americanfootball_nfl/events/{id}/odds?apiKey=…&regions=us&markets=player_pass_yds,player_pass_tds,player_rush_yds,player_reception_yds,player_receptions,player_anytime_td&oddsFormat=american&bookmakers=draftkings,fanduel`
  costs **(unique markets returned) × (regions)** credits. Up to 10 bookmakers count as one
  region. Outcome fields are `name` (Over/Under/Yes), `description` (player), `price` and
  `point`. Remaining quota comes back in the `x-requests-remaining` header.
- Free plan: 500 credits a month. **Whether the free plan includes player props is
  unverified.** Third-party comparisons say props need a paid plan. The first probe call
  settles it. A full slate is about 16 games × 6 markets = ~96 credits, so the free tier
  allows roughly one full pull a week.

**Not recommended:** Underdog, PrizePicks and Sleeper Picks. They have no public API
(Underdog's backend is explicitly non-public) and their terms restrict it. ESPN core
`/odds/{provider}` gives game lines only. No public prop endpoint was found.

### 2.5 News

- **ESPN per-player fantasy news** (RotoWire blurbs):
  `GET https://site.api.espn.com/apis/fantasy/v2/games/ffl/news/players?playerId={espn_id}&limit=20`.
  It returns `feed[]` with `headline`, `description`, `story` (HTML), `published`,
  `lastModified`, `type` (mostly `"Rotowire"`, also `"Story"` and `"Media"`) and `playerId`,
  with 11–20 items per player going back months. **League-wide requests (no `playerId`)
  return HTTP 500**, so it is per player only. Use the plain User-Agent: `sitstart.py`
  documents that `site.api.espn.com` 403s a browser UA.
- **Sleeper** `news_updated` (epoch ms) in the players DB is a cheap "something changed"
  flag across every player. Use it to choose whom to fetch ESPN news for.
- Keep the existing r/fantasyfootball Atom feed as explicitly unverified chatter.
- RotoWire publishes RSS (`rotowire.com/rss/`). Optional; probe for the NFL player-news feed
  URL before using it.

### 2.6 Trade values and ROS rankings

- **FantasyCalc**: `https://api.fantasycalc.com/values/current?isDynasty=false&numQbs=1&numTeams=12&ppr=0.5`.
  It returns a list whose entries carry `player.sleeperId`, `value`, `redraftValue`,
  `overallRank`, `positionRank` and `trend30Day`. Values come from real fantasy trades, and
  FantasyCalc labels its redraft values "Rest of Season Rankings". Always send
  `isDynasty=false`: one public integration shipped a bug by deriving it from league type.
  Derive `numQbs`, `numTeams` and `ppr` from the league.
- **FantasyPros ROS ECR**: `https://www.fantasypros.com/nfl/rankings/ros-half-point-ppr-overall.php`
  (plus the per-position `ros-*` pages). Same `ecrData` blob `sources/fantasypros.py`
  already parses: `rank_ecr`, `rank_ave`, `rank_std`, `rank_min`, `rank_max`, `tier`,
  `player_id`, and cross IDs such as `sportsdata_id`, `player_yahoo_id` and `cbs_player_id`.
- **FanDuel `REMAINING`** projections: numberFire's ROS points (same GraphQL call as 2.3).
- **KeepTradeCut** (`keeptradecut.com/fantasy-rankings`, the redraft page): values come from
  crowdsourced keep/trade/cut votes, a different method from FantasyCalc's completed
  trades, so the two disagree usefully. There is no API. Player data is embedded in the page
  as a JS array (`playersArray` on the dynasty pages; **probe** the redraft page for the
  variable name and value keys). **Its terms prohibit robots, spiders and scrapers**, so the
  adapter is opt-in (`KTC_ENABLED=1`), fetches at most once every 24 h, and ships off by
  default. The site was blocked from the research sandbox.

### 2.7 Other sources worth adding

| Source | Why | Effort |
|---|---|---|
| Sleeper roster `settings.waiver_budget_used` | Real FAAB remaining for you **and every opponent**. Removes a documented limitation. | Tiny |
| Sleeper `league/{id}/transactions/{week}` (`settings.waiver_bid`, `status` complete/failed) | What **this room** actually pays on waivers, the in-season equivalent of the board's "'25 paid" column | Small |
| Sleeper `league/{id}` `scoring_settings` | Exact league scoring for every source and for props (TE premium and bonuses included) instead of a `"half_ppr"` label | Small |
| nflverse schedules | Kickoff time for weather, roof, rest days (short week), next week's lines for look-ahead | Small |
| ffopportunity xFP | Buy-low / sell-high: production against the quality of opportunity. Separates a bad week from a bad role. | Small |
| ffopportunity pbp | Red-zone and goal-line share, which drive TDs. Team PROE (pass rate over expected in neutral script). | Medium |
| Open-Meteo at kickoff hour | Fixes "weather uses max wind across the forecast window" | Tiny |
| PFR advanced, NGS | Separation, yards after contact, RYOE, drops. Context for the model's narrative, and a second read on efficiency for look-ahead. | Small (Phase 1b) |
| RotoWire RSS | A bulk news feed alongside the per-player ESPN calls | Small (Phase 4) |

---

## 3. Architecture

### 3.1 Layout

`sources/` keeps its contract: draft-ranking adapters with `fetch(season, scoring)`
returning `record()` rows keyed by `POS|name`. The new data has different shapes and
cadences, so it gets its own package:

    sleeper_auction/
      feeds/
        __init__.py        registry + status() for /api/feeds
        common.py          fetch helpers (csv.gz, stale-if-error), parallel runner, snapshot store
        ids.py             crosswalk: any external id / name -> sleeper_id
        scoring.py         league scoring_settings -> points(stats)
        nflverse.py        usage, snaps-by-id, practice, xFP, red-zone/PROE, schedule
        props/
          __init__.py      week_props(): merge providers per player
          kalshi.py        ladder markets (keyless)
          oddsapi.py       O/U markets (ODDS_API_KEY, optional)
          implied.py       markets -> expected stat lines -> fantasy points + distribution
        projections/
          __init__.py      registry, fetch_all(season, week) via prefetcher
          sleeper_rw.py    wraps sitstart.week_projections, keeping full stat lines
          espn.py  cbs.py  nfl.py  fanduel.py  fftoday.py  fantasysharks.py
          consensus.py     per-player aggregation
        blend.py           final weekly projection from consensus + props (+ calibration)
        news.py            ESPN per-player news, Sleeper news_updated
        values.py          FantasyCalc, KeepTradeCut (opt-in), FantasyPros ROS, ROS projection, SOS
        league.py          FAAB remaining, transaction history, room clearing prices
        prefetch.py        background refresh thread for slow feeds
        calibrate.py       Phase 5: per-source error, blend weights
      sources/fantasypros.py   + fetch_week_ecr(), fetch_ros()
    tests/
      fixtures/            small recorded payloads (CSV/JSON/HTML), a few KB each
      test_*.py            unittest, network-free

### 3.2 Shared infrastructure (`feeds/common.py`)

- `fetch_text(url, key, ttl, headers=None, stale_ok=True)`: same cache directory and key
  rules as `board.get()`, plus two additions:
  1. **gunzip the body** when it starts with `\x1f\x8b` (`.csv.gz` assets are gzip *content*,
     not `Content-Encoding`), and cache the decompressed text;
  2. **stale-if-error**: on any exception, return the cached file whatever its age, and
     record `stale=True` in the feed's status. `sources/sleeper_src.py` already does this for
     the pool, so match its behaviour.
- `fetch_csv(urls, key, ttl)` takes a list of candidate URLs (`.csv.gz` first, then `.csv`)
  and returns `list[dict]`.
- `fetch_json(...)`, `post_json(url, body, key, ttl, headers)` (for FanDuel GraphQL and
  similar).
- `nflverse_url(tag, file)` and `ffopp_url(file)` constants.
- `parallel(tasks, max_workers=8, timeout=60)`: `ThreadPoolExecutor`, returns
  `(results, errors)` dicts. Use it in `build_slate` / `build_board` so the new feeds do not
  add their latencies together.
- `num(v)`: the tolerant float parser, the same as `sources/base.record()`'s.

### 3.3 Crosswalk (`feeds/ids.py`)

    crosswalk() -> {"by": {"gsis": {...}, "espn": {...}, "yahoo": {...}, "pfr": {...},
                           "fantasypros": {...}, "cbs": {...}, "nfl": {...},
                           "rotowire": {...}, "kalshi": {...}, "fantasycalc": {...}},
                    "by_name": {pkey: sleeper_id}}      # only keys that are unique
    to_sleeper(kind, value, name=None, pos=None, team=None) -> sleeper_id | None

- Build from the Sleeper players DB first; on conflict Sleeper wins. Fill gaps from
  DynastyProcess. Normalise every ID to a stripped `str`.
- The name fallback uses `board.pkey(name, pos, team)`, so it inherits `NAME_FIXES`. Drop
  ambiguous names (two active players with the same key) rather than guessing.
- Keep per-`kind` counters `{hit, name_fallback, miss}`. `status()` reports them, and a
  `miss` rate above 3% on fantasy-relevant players (any target, carry or projection) is a
  bug to fix before shipping.
- **Replace the name join in `sitstart.snap_trend`** with `pfr_player_id → sleeper_id`.
  Keep the output shape (`games`, `season_pct`, `recent_pct`, `trend`, `earned_on`) but key
  it by Sleeper ID. `_snaps_for()` looks up by ID first and name second. This fixes a latent
  bug class (two "Mike Williams" rows) at no cost.

### 3.4 Snapshot store

    snapshot(kind, season, week, payload)             # cache/history/<season>/w<WW>/<kind>/<UTC yyyymmddHH>.json
    snapshots(kind, season, week) -> [(ts, payload)]  # sorted

Used for:

- the **practice trajectory**: snapshot nflverse injuries and Sleeper practice fields on every
  fetch, deduplicated by content hash, so Wed/Thu/Fri accumulate;
- **calibration** in Phase 5: snapshot the final pre-kickoff consensus, props, Sleeper
  projection and blend per player, then score them against actuals after the week;
- **line movement**: the first and latest prop means for the week.

`cache/` is gitignored, so this history lives only on the user's machine. Say so in the
README. Never write snapshots on a request that failed.

### 3.5 League scoring (`feeds/scoring.py`)

    league_scoring(league_id) -> dict        # GET /v1/league/<id> -> scoring_settings; ttl 1 day
    points(stats, sc) -> float               # dot product over canonical keys

The canonical stat vocabulary is **Sleeper's stat keys**: `pass_yd`, `pass_td`,
`pass_int`, `pass_2pt`, `rush_yd`, `rush_td`, `rush_2pt`, `rec`, `rec_yd`, `rec_td`,
`rec_2pt`, `fum_lost`, plus bonus keys such as `bonus_rec_te`. They are the same keys
`scoring_settings` uses, so `points()` is a plain dot product and TE premium falls out for
free. Every adapter maps its columns to these keys. Default when the league is
unreachable: standard half-PPR (`rec 0.5`, `pass_yd 0.04`, `pass_td 4`, `pass_int -1`,
`rush_yd 0.1`, `rec_yd 0.1`, TDs 6, `fum_lost -2`, 2pt 2). K and DEF: use the source's own
points or Sleeper's. Do not rebuild kicker and defence scoring.

### 3.6 Feed status and prefetch

- Each feed module keeps `LAST_STATUS = {"ok", "rows", "as_of", "cache_age_s", "stale",
  "error", "join": {...}}`, like `LAST_NOTES` in the source adapters.
- `GET /api/feeds` (add to `board.H.do_GET`) returns `feeds.status()`. Also add a small
  "Data" panel to each page's rail listing each feed's freshness in one line.
- `feeds/prefetch.py` starts a daemon thread from `board.main` that refreshes slow feeds on
  a schedule (TTL table, section 6) and never runs on the request path. On a cold start,
  pages show whatever is cached and say which sources were missing. Guard every refresh with
  a lock so a manual `?refresh=` cannot double-fetch.
- Env switches: `FEEDS_DISABLED=props,news,...` turns feeds off; `ODDS_API_KEY` turns on The
  Odds API. There is no config file. That matches the app's zero-setup property.

---

## 4. Implementation by phase

Each phase lists files, logic, integration points, prompt changes, UI, tests and acceptance
criteria. Line references are to the current `main`.

### Phase 0: Foundation

**Files:** `feeds/__init__.py`, `feeds/common.py`, `feeds/ids.py`, `feeds/scoring.py`,
`tests/`, the `/api/feeds` route in `board.py`.

1. Build `common.py`, `ids.py` and `scoring.py` as specified in section 3.
2. `python3 -m sleeper_auction.feeds.ids --probe` prints crosswalk sizes per ID kind, the
   number of 2026 rookies covered, and a sample of 5 mappings per kind.
3. Add `tests/` with `unittest` (stdlib):
   - `test_common.py`: gunzip detection; stale-if-error (monkeypatch `urlopen` to raise
     with an old cache file present).
   - `test_ids.py`: fixture of 20 Sleeper players and 20 DP rows, including a leading-space
     `gsis_id`, a name collision and a rookie with no GSIS.
   - `test_scoring.py`: half-PPR, full PPR and TE-premium dot products.
   - Run with `python3 -m unittest discover -s tests`. Add that line to the README.

**Acceptance:** the tests pass offline; `/api/feeds` returns JSON; no existing page changes.

### Phase 1: nflverse usage, practice, xFP, red zone and schedule

**File:** `feeds/nflverse.py`

    player_weeks(season) -> rows                       # stats_player_week, QB/RB/WR/TE only
    usage(season=None, recent=3, min_weeks=2) -> {"season", "weeks", "players": {sid: {...}}}
    snaps(season=None) -> same shape as sitstart.snap_trend, keyed by sleeper id
    practice(season, week) -> {sid: {...}}
    expected_points(season=None, recent=3, rec_value=0.5) -> {"season", "players": {sid: {...}}}
    redzone(season=None) -> {"players": {sid: {...}}, "teams": {team: {"proe": ..}}}
    schedule(season) -> {week: {team: {...}}}

`usage()` per player, over the games he actually played:

- `target_share` = Σ player targets / Σ team targets in those games, for both the season and
  the last `recent` games. **Recompute from sums.** Don't average the per-game shares: a
  2-target blowout game should not weigh the same as a 12-target game.
- `air_yards_share`, and `wopr = 1.5 × target_share + 0.7 × air_yards_share` (the standard
  definition that nflverse uses).
- `adot = receiving_air_yards / targets` (only if targets ≥ 10), `racr`, `yac_per_rec`.
- `carry_share` = player carries / team carries, and `rb_target_share` for backs.
- `trend_*` = recent − season, in percentage points.
- `games`, `team` (most recent), and `team_changed` against the current Sleeper team, as in
  `_snaps_for`.
- Season fallback: current season when ≥ `min_weeks`, else the prior one. The return value
  records which, like `def_vs_position()` does.

`practice(season, week)`:

- Current rows from `injuries_{season}` for `week`, merged with Sleeper
  `practice_participation`, `practice_description` and `injury_status`.
- Normalise statuses to `DNP` / `LP` / `FP`. Set `rest = "Not injury related" in practice_primary_injury`.
- `trajectory`: from snapshots for that week, ordered by time and deduplicated, for example
  `["DNP", "LP", "FP"]`, with `as_of` per step.
- `report_status` (the game designation; usually published Friday).
- ESPN `injuries()` stays for the comment and return date. Join all three by Sleeper ID
  (ESPN via `espn_id`, **not** by name).

`expected_points()`:

- Per game: `xfp = total_fantasy_points_exp + (rec_value − 1) × receptions_exp`, and the same
  for actual points.
- Season and recent: `xfp_pg`, `fp_pg`, `diff_pg = fp_pg − xfp_pg`, `xtd_pg`, `td_pg`.
- Needs ≥ 2 games. `diff_pg` over one game is noise.

`redzone()` (from `ep_pbp_rush` / `ep_pbp_pass`):

- Per player: `rz_carries` (`yardline_100 ≤ 20`), `i10_carries` (≤ 10), `gl_carries` (≤ 5),
  `rz_targets`, and shares of team totals.
- **Probe the pass file's receiver column name before coding it.** Only the rush header was
  verified (`rusher_player_id`).
- Team PROE: in neutral script (`down ∈ {1,2}`, `|score_differential| ≤ 7`,
  `0.2 ≤ vegas_wp ≤ 0.8`, `qtr ≤ 3`), actual pass rate − mean `xpass`.

`schedule()`:

- Per team per week: `opp`, `home`, `total`, `spread`, `implied`, `kickoff_utc`
  (`gameday` + `gametime` in `America/New_York` → UTC via `zoneinfo`), `roof`, `rest_days`.
- **Spread sign must match `sitstart.vegas()`**, which uses the betting convention
  (favourite negative). nflverse's `spread_line` is the *home margin* (positive = home
  favoured), so home `spread = −spread_line` and away `spread = +spread_line`. Then
  `implied = total/2 − spread/2`.
- It is a **fallback** for `sitstart.vegas()` (ESPN) and the source of kickoff times. When
  both exist and differ by more than 1.5 points, keep ESPN (it is live) and record
  `line_sources_disagree`.

**Weather fix (`sitstart.weather`)**: request Open-Meteo with `&timezone=UTC`, find the hourly
index of `kickoff_utc`, and use max wind and total precipitation probability over
`[kickoff, kickoff + 3h]`. Take `roof` from the schedule where `STADIUM` is wrong or
missing. Keep `STADIUM` for coordinates. Delete the matching line from README "Known
limitations".

**Integration**

`sitstart.build_slate` (lines 485–534): fetch `usage`, `practice`, `expected_points` and
`redzone` in `common.parallel()` alongside the existing calls, then add per player:

    "usage":    {"tgt_share", "tgt_share_recent", "air_share", "wopr", "adot",
                 "carry_share", "trend_tgt", "games", "season"},
    "practice": {"trajectory": ["DNP","LP"], "report_status", "rest", "injury"},
    "xfp":      {"xfp_pg", "fp_pg", "diff_pg", "games"},
    "redzone":  {"rz_share", "gl_carries", "rz_targets"}

Add to `sitstart.SYSTEM`, after the snaps paragraph (proposed wording; tune it):

> - "usage" is target share (the fraction of the team's targets), air-yards share and WOPR
>   (1.5 × target share + 0.7 × air-yards share), season and last three games. For receivers
>   it is a better usage read than snap share, because a receiver can be on the field and
>   not be targeted. A rising target share ahead of a soft matchup is a start signal.
>   Weigh it against "games". Two games is a hint, not a role.
> - "practice.trajectory" is the week's practice participation in order. DNP → LP → FP is
>   a player on track. FP → LP or LP → DNP late in the week is the worst sign in the data.
>   "rest": true is a scheduled veteran rest day and means nothing. "report_status" is the
>   official game designation.
> - "xfp" is expected half-PPR points per game from the quality of the opportunities he got
>   (targets by depth, carries by field position). "diff_pg" well above zero means he has
>   been scoring above his opportunity, usually through touchdowns, and should be expected
>   to regress. Well below zero means the role is better than the results.
> - "redzone" is his share of the team's red-zone and goal-line work, where touchdowns come
>   from.

`sitstart.html_report`: add a `Tgt%` column (recent target share; carry share for RBs,
labelled). In Flags add practice glyphs (`DNP·LP·FP`, colour-coded) and `xFP ±x.x` when
`|diff_pg| ≥ 3` with ≥ 2 games. Update the footnote.

`waivers.score_candidate` (lines 225–268): add upside drivers, each gated on sample size,
with the constants at the top of the module and a comment on each:

| Driver | Condition | Upside | `upside_why` text |
|---|---|---|---|
| Rising target share | `trend_tgt ≥ +8pp`, games ≥ 2, recent targets ≥ 4/g | +1.5 | "target share up N pts over last 3" |
| Real receiving role | `wopr ≥ 0.45` | +1.0 | "WOPR 0.xx" |
| Unlucky | `xfp.diff_pg ≤ −3`, games ≥ 2 | +1.0 | "xFP says the role is N pts/g better than results" |
| Goal-line role | `gl_carries ≥ 2` or `rz_share ≥ 0.30` on `carry_share < 0.5` | +0.8 | "goal-line work on a light role" |

Update `waivers.SYSTEM` to match.

`lookahead`:

- `classify()` gets new tags: `rising-share`; `buy-low` (xFP well above actual); and, for your own
  roster only, `sell-high` (actual ≥ xFP + 4/g on ≥ 3 games, TD-driven).
- `stash_board` score: +0.8 for `rising-share`, +0.6 for `buy-low`.
- `my_roster` rows carry `usage` and `xfp` so the model can name TD-regression risk as a
  weak point.
- `efficiency()` keeps Sleeper season stats for YPC/YPT but adds `tgt_share` and `wopr`
  from nflverse.
- Update `lookahead.SYSTEM` for the new tags.

**Tests:** fixture CSVs trimmed to about 30 rows each (two teams, two games):
target-share recomputation from sums, the half-PPR xFP conversion (assert the McBride
example), practice normalisation including the "resting player" row, trajectory from three
snapshots, and schedule spread orientation (assert KC@MIA week 3: KC `spread −10.5`,
MIA `+10.5`).

**Acceptance:**

- `python3 -m sleeper_auction.feeds.nflverse --probe --season 2026` prints row counts,
  weeks present and join rates. At least 97% of players with ≥ 1 target or carry map to
  a Sleeper ID.
- `/sitstart?noai=1` renders the new column and flags.
- With the network off and no cache, every existing number still renders.

### Phase 1b: PFR advanced stats and Next Gen Stats

**File:** `feeds/nflverse.py` (same module)

    pfr_adv(season=None, recent=3) -> {"season", "players": {sid: {...}}}   # via pfr_id
    ngs(season=None) -> {"season", "players": {sid: {...}}}                  # via gsis_id

- PFR: `advstats_week_rec_{season}` for drops, drop %, broken tackles, yards after contact
  per reception and ADOT; `advstats_week_rush_{season}` for yards before/after contact and
  broken tackles per attempt. **Probe the column names** before mapping. Aggregate over the
  season and the last 3 games from sums, not averages of rates.
- NGS: the combined `ngs_receiving.csv.gz` / `ngs_rushing.csv.gz`, filtered to `season` and
  `week > 0` (NGS files also carry season-total rows with `week == 0`; probe that). Receiving:
  average separation, cushion, YAC over expected. Rushing: rush yards over expected per
  attempt, and efficiency. Minimum volume: 10 targets / 20 carries.
- **Integration:** an `efficiency_ctx` object on look-ahead candidates and `my_roster`, and on
  sit/start players only when a value is extreme (top or bottom 10% at the position), so the
  payload stays small. In `lookahead.classify()`, add a supporting line to `efficient-unused`
  when RYOE or separation agree with the YPC/YPT gap. That is independent confirmation that
  the efficiency is the player's rather than the scheme's. Add a `SYSTEM` paragraph:
  "efficiency_ctx is descriptive context, not a projection. It explains *why* a rate is high
  or low."
- **Tests:** fixtures for both files; the season-total-row filter; the volume gate.

### Phase 2: Player props

**Files:** `feeds/props/__init__.py`, `kalshi.py`, `oddsapi.py`, `implied.py`

Canonical market keys: `pass_yd`, `pass_td`, `pass_int`, `pass_att`, `pass_cmp`, `rush_yd`,
`rush_att`, `rec`, `rec_yd`, `anytime_td`.

**`kalshi.py`**

1. Discovery (cache 24 h): `GET /series?category=Sports`, keep tickers starting `KXNFL`.
   Map each to a canonical market key **by title keywords** ("receiving yards" → `rec_yd`,
   "receptions" → `rec`, "rushing yards" → `rush_yd`, "passing yards" → `pass_yd`,
   "passing touchdowns" → `pass_td`, "touchdown" + "anytime"/"score" → `anytime_td`, and so
   on). Keep a small override dict for the tickers you verify. Log unmapped `KXNFL` series
   in status so new ones get noticed.
2. Markets (cache 15 min, 5 min from 2 h before the first kickoff):
   `GET /markets?series_ticker=X&status=open&limit=1000`, following `cursor` until empty.
3. Parse each market:
   - player from `yes_sub_title` or `title` ("Player: N+ …");
   - threshold = `floor_strike` (fallback: parse `N` from the title);
   - game from `event_ticker`;
   - `bid`/`ask` = `float(yes_bid_dollars)` / `float(yes_ask_dollars)`;
   - `last` = `float(last_price_dollars)`;
   - `oi` = `float(open_interest_fp)`.
   - **Probe whether `custom_strike` or `primary_participant_key` carries an ID that
     matches Sleeper's `kalshi_id`.** If so, join on it; otherwise use name + team through
     the crosswalk.
4. Price of a rung: mid `(bid+ask)/2` when both > 0 and `ask − bid ≤ 0.10`; else `last`
   when `oi ≥ 50`; else drop the rung.
5. Ladder per (player, market): sort by threshold and **enforce non-increasing
   probabilities** with `valuation._pava_decreasing`. Drop ladders with fewer than 2 rungs.
6. Respect rate limits: small sequential requests with a 0.1 s sleep. There are about 10
   series; it is cheap.

**`oddsapi.py`** (enabled whenever `ODDS_API_KEY` is set; the user has a key):

0. **Probe first:** `ODDS_API_KEY=… python3 -m sleeper_auction.feeds.props.oddsapi --probe`
   calls `/events` (free), then **one** event with `markets=player_reception_yds` (1–2
   credits). Record in the docstring whether props came back on this plan, and the
   `x-requests-remaining` value. If the plan rejects props, the provider disables itself
   and Kalshi carries props alone. Never log the key or include it in cache keys; strip
   `apiKey` from any URL that ends up in a status message or exception.

1. `GET /events` (free) and keep games whose `commence_time` is in the requested week and not
   started.
2. Per event, `GET /events/{id}/odds` with `regions=us`, `bookmakers=draftkings,fanduel`
   (≤ 10 books = 1 region), the six markets, and `oddsFormat=american`.
3. **Budget guard:** persist `x-requests-remaining` to `cache/oddsapi-quota.json`, refuse
   to fetch below a reserve (default 60), and fetch at most once per event per 24 h unless
   `?refresh=props`. A 401/403/422 response naming the plan disables the provider for the
   day and says why in status.
4. Normalise to `{player, market, line, over_price, under_price, book}`. Take the median
   line across books and de-vig each book's pair (appendix A).

**`implied.py`**: see appendix A for the maths.

    expected_from_ladder(ladder) -> {"mean", "median", "p10", "p90"}
    expected_from_ou(market, line, p_over) -> {"mean", "median"}
    td_lambda(p_anytime) -> float
    fantasy_from_markets(markets, filler_stats, scoring) ->
        {"mean", "p10", "p50", "p90", "coverage", "components": {...}}

- `filler_stats` is the player's Sleeper projected stat line (Phase 3: the consensus line).
  It supplies every component without a market, for example receptions when only a
  yardage ladder exists.
- `coverage` = the share of the projected fantasy mean that came from markets.
- `--fit`: reads `stats_player_week_2024` and `_2025`, computes per-market
  `MEAN_OVER_MEDIAN` and `CV` constants, and prints them. Paste them into the module with
  a comment recording the derivation and the date.

**`props/__init__.py`**: `week_props(season, week)` merges providers per Sleeper ID and
prefers Kalshi ladders when `oi` is healthy, else Odds API lines. It records `sources` and
`as_of`, and snapshots the result.

**Integration (Phase 2 alone, before consensus exists):**

- `sitstart.build_slate`: per player
  `"props": {"mean", "p10", "p90", "anytime_td", "lines": {...}, "coverage", "source", "as_of"}`.
- Provisional blend: `proj_final = 0.5·props.mean + 0.5·sleeper` when `coverage ≥ 0.7`, else
  Sleeper. `my_projected`, `opp_projected` and `live_projected` use `proj_final`. Keep
  `proj` (Sleeper) in the payload as `proj_sleeper` so nothing downstream silently changes
  meaning, and add `proj_basis`.
- Floor / ceiling: when `props` exists, the `Floor / Ceil` column shows **this week's**
  market p10/p90, marked `mkt`. Otherwise it keeps last season's volatility. `posture()`
  stays as it is but runs on the new totals.
- Add to `sitstart.SYSTEM`:

  > - "props" is what the betting market implies for this player this week, converted to
  >   half-PPR points from yardage, reception and touchdown markets. Markets move on news
  >   faster than projections do. A prop mean well below the projection usually means the
  >   market has priced in an injury or a role change, so treat that gap as information.
  >   "coverage" is how much of the number came from markets rather than the projection.
  >   "anytime_td" is the market's probability that he scores. A starter with **no** props
  >   posted can itself be a signal: books pull lines when a player's status is uncertain.
  >   Say so, don't assume.

- `html_report`: a `Mkt` column (props mean, with coverage shown as ● full, ◐ partial).
- Waivers and look-ahead: add props only where present. Fringe players rarely have
  markets, so they are not a ranking input there.

**Tests:** ladder → mean and median on a synthetic ladder with a known answer (for
example an exponential survival curve); a PAVA fix of a non-monotone ladder; de-vig
(−110/−110 → 0.5; −150/+130 → ≈0.58); Poisson λ for p = 0.40 (≈0.511); pass-TD O/U 1.5 at
p_over = 0.45 → λ solved by bisection; coverage arithmetic; a Kalshi fixture JSON (5
markets, one wide-spread rung dropped); an Odds API fixture.

**Acceptance:**

- `python3 -m sleeper_auction.feeds.props --probe --week N` lists series found, markets
  parsed, players joined and unmatched names.
- On a normal Thursday or later, at least 80% of projected fantasy starters (top 24
  QB/TE, top 36 RB, top 48 WR by Sleeper projection) have a props mean.
- The page renders with Kalshi unreachable.

### Phase 3: Consensus projections

**Files:** `feeds/projections/*.py`, `feeds/blend.py`, `feeds/prefetch.py`

Adapter contract, one module per source:

    NAME = "cbs"; LABEL = "CBS Sports"; PRODUCER = "cbs"   # independence key
    def fetch_week(season, week) -> list of {"name", "pos", "team", "src_id",
                                             "id_kind", "stats": {canonical}, "pts": float|None}

Adapter notes:

- **sleeper_rw.py**: change `sitstart.week_projections` to also return `stats` (the full
  dict), keeping its current keys for existing callers.
- **espn.py**: reuse `sources/espn.py`'s host and UA handling. The filter follows
  ffanalytics' `scrape_espn` (same keys; values filled per call):

      {"players":{"filterSlotIds":{"value":[<0|2|4|6|17|16>]},
       "filterStatsForSourceIds":{"value":[1]},
       "filterStatsForSplitTypeIds":{"value":[1]},
       "sortAppliedStatTotal":{"sortAsc":false,"sortPriority":3,"value":"11<season><week>"},
       "sortDraftRanks":{"sortPriority":2,"sortAsc":true,"value":"PPR"},
       "limit":<42|100|150|60|35|32>,"offset":0,
       "filterStatsForTopScoringPeriodIds":{"value":2,
         "additionalValue":["00<season>","10<season>","11<season><week>","02<season>"]}}}

  Pick the stat entry with `statSourceId==1 && statSplitTypeId==1 && scoringPeriodId==week`
  and map stat IDs as in 2.3.
- **cbs.py / nfl.py / fftoday.py**: HTML parsing with `html.parser.HTMLParser` (stdlib).
  Parse header labels into canonical keys, not column positions. CBS and NFL.com expose a
  player ID in the link: crosswalk via DP `cbs_id` / `nfl_id`. FFToday is name only.
- **fanduel.py**: POST the GraphQL query (field list: `player{numberFireId,name,position}`,
  `team{abbreviation}`, `gameInfo{homeTeam{abbreviation},awayTeam{abbreviation},gameTime}`,
  `completionsAttempts` (the string `"c/a"`), `passingYards`, `passingTouchdowns`,
  `interceptionsThrown`, `rushingAttempts`, `rushingYards`, `rushingTouchdowns`,
  `receptions`, `targets`, `receivingYards`, `receivingTouchdowns`, `fantasy`). Drop rows
  whose `gameTime` is outside the requested week. Name + team join.
- **fantasysharks.py**: CSV and `csv.DictReader`. Dedupe repeated header names before
  mapping.
- **FantasyPros weekly ECR**: add `fetch_week_ecr(pos)` to `sources/fantasypros.py`,
  reusing its `ecrData` extractor. Store `rank_ecr`, `rank_ave`, `rank_std`, `tier` and the
  start/sit grade (probe the exact key names). It is a rank signal: show it in the UI and
  the payload, but it does not enter the points consensus.
- 2 s between pages for all scrapers (5 s for FantasyPros), and a real UA except where a host
  demands otherwise (see the `ESPN_HDRS` comment).

`consensus.py`:

    build(season, week, scoring) -> {sid: {"median", "mean", "lo", "hi", "sd", "n",
                                            "by_source": {name: pts}, "stats": {median per key}}}

- Score **every** source's stat line with `scoring.points()` so all sources use this league's
  rules. Use the source's own `pts` only when it gives no stat line (K/DEF).
- Median when `n ≥ 3`, mean otherwise. `sd` and range travel with the number.
- One value per `PRODUCER` (Yahoo would duplicate FantasyPros).
- A source projecting ~0 while the others project > 3 is kept but flagged
  `source_thinks_out`. That is useful news, not noise.
- `stats` (the per-key median line) becomes `filler_stats` for the props conversion.
- Snapshot per week.

`blend.py`:

    final(p) -> {"value", "basis": {"props": w, "consensus": w, "sleeper": w}, "range": (lo, hi)}

With module constants documented like `valuation.W_MODEL`:

    W_PROPS_FULL = 0.55      # coverage >= 0.7
    W_PROPS_PARTIAL = 0.25   # 0.3 <= coverage < 0.7
    # else consensus only; else Sleeper only

0.55 is the user's chosen starting weight. Phase 5 calibration replaces these once
`cache/calibration.json` exists and holds ≥ 4 weeks. Say in the comment that the weights
were chosen, not fitted.

`prefetch.py`: refresh the consensus inputs every 3 h from Tuesday 12:00 ET to kickoff,
and hourly on Sunday morning. Everything goes through `common.fetch_*` so the cache is the
contract.

**Integration**

- `sitstart`: `proj` shown in the table = `blend.final()`, with a small `n src · lo–hi` under
  it and `proj_sleeper` kept. `SYSTEM` gets:

  > - "consensus" is the median of up to six independent projection sources scored with
  >   this league's rules. "sd", "lo" and "hi" say how much they disagree. A wide range
  >   means the projection itself is uncertain, which is different from the player being
  >   volatile. "proj_basis" says how the final number weighs market props against
  >   consensus.

- `waivers`: `wk_proj` = consensus (props are rare for free agents). The flex bar and need
  deltas recompute on it. `_starters()` keeps working because it reads `wk_proj`.
- `README`: add a "Weekly projection sources" table next to "Ranking sources".

**Tests:** one fixture per adapter (a trimmed HTML/CSV/JSON page) → expected canonical
lines; consensus median, producer dedupe and `source_thinks_out`; blend weights by coverage.

**Acceptance:**

- `python3 -m sleeper_auction.feeds.projections --probe --week N` shows rows per source, join
  rate per source (≥ 95% for top-200 players) and fetch time.
- A cold page load does not wait on any scraper.
- At least 4 sources are live for the current week.

### Phase 4: News, ROS values and league FAAB

**`feeds/news.py`**

    player_news(sleeper_ids, max_age_days=7, per_player=3) -> {sid: [{"headline", "text",
                                                                    "published", "type"}]}

- Whom to fetch: your roster, the opponent's starters, the top 25 waiver candidates, and any
  player whose Sleeper `news_updated` is within 48 h. That is at most about 60 requests,
  with `parallel(max_workers=6)` and a per-player cache of 30 min.
- ESPN ID via the crosswalk. Strip `story` HTML with `html.parser`, collapse whitespace and
  truncate to 400 characters. Prefer `type == "Rotowire"`.
- Payload cap: 2 items per player, newest first, last 7 days only. Token cost matters.
- Prompts: news is **reported** (RotoWire), a different class from the unverified Reddit
  chatter, and every item carries its date. "A report from Tuesday is superseded by
  Friday's practice status."
- **RotoWire RSS** (`rotowire_rss()`): probe `rotowire.com/rss/` for the NFL player-news feed
  URL. Parse it with `xml.etree` the way `waivers.reddit_posts()` does, cache it for 30 min,
  and map titles to players through the crosswalk name index. Use it to (a) pick whom to
  fetch ESPN news for, alongside Sleeper `news_updated`, and (b) cover players ESPN has no
  ID for. Deduplicate against ESPN items by headline.

**`feeds/values.py`**

    fantasycalc(league) -> {sid: {"value", "redraft_value", "rank", "pos_rank", "trend30"}}
    fp_ros() -> {sid: {"rank_ecr", "tier", "rank_std"}}
    ros_points(season, from_week) -> {sid: {"pts", "source"}}
    ros_dollars(league, pool) -> {sid: $}
    sos(season, from_week) -> {team: {pos: {"avg_dvp_rank", "games"}}}

- FantasyCalc params come from the league: `numQbs` = 2 if the league has a `SUPER_FLEX`
  slot, `numTeams` = teams, `ppr` = `scoring_settings.rec`. Cache 12 h.
- `ros_points`, in order of preference:
  1. the sum of Sleeper future-week projections, **if Sleeper publishes weeks beyond the
     next one** (probe `week+3`);
  2. FanDuel `REMAINING`;
  3. `season_proj × remaining_games / 17`.
- `ros_dollars`: rescale FantasyCalc value onto this league's dollar pool with the same
  approach as `valuation._scale_to_pool` (monotone map from value rank to the current
  `base` dollar curve). That keeps "quality" in the units the waiver model already uses.
- `sos`: remaining opponents from `nflverse.schedule()` × `def_vs_position()` ranks.

**KeepTradeCut** (`ktc()`, opt-in via `KTC_ENABLED=1`):

    ktc(league) -> {sid: {"value", "rank", "pos_rank", "trend"}}
    trade_values(league) -> {sid: {"fc", "ktc", "consensus", "disagree"}}

- Fetch the redraft rankings page with a 24 h TTL and parse the embedded player array
  (probe the variable name and keys; pick 1QB vs superflex values from the league). Join on
  `ktc_id` from the DynastyProcess crosswalk, falling back to name and position.
- `trade_values`: the two sources use different scales, so **do not average raw values**.
  Convert each to a percentile within position, average the percentiles (FantasyCalc alone
  when KTC is off or missing), and map back to dollars through `ros_dollars`. Set
  `disagree = |pct_fc − pct_ktc| ≥ 0.15`. That is a real signal: the market's votes and its
  completed trades have diverged. Pass it to the model.
- Every consumer (waivers quality, look-ahead, `/trades`) reads `trade_values()`, never one
  source directly, so KTC can be switched off without code changes.

**`feeds/league.py`**

    faab(league_id) -> {roster_id: {"budget", "used", "left"}}     # rosters[].settings.waiver_budget_used
    faab_history(league_id, through_week) -> [{"week", "player", "bid", "won", "roster_id"}]
    clearing_curve(history, trend_snapshots) -> {"median_pct_by_band": {...}, "n"}

**Integration**

- `waivers.build_board`:
  - `budget_left` = **your real remaining FAAB**, with an `opp_budgets` summary (max and
    median remaining across the other 11 rosters).
  - The quality term switches from the preseason `season_value` to `ros_dollars` from week 3
    onward. The preseason auction value goes stale as roles change, and this is the
    single biggest accuracy fix available to the waiver board.
  - `faab_bid` caps at the maximum opponent budget (you never need to bid more than anyone
    can) and scales by `clearing_curve` once there are ≥ 8 completed claims.
  - The payload gets `news` per candidate. Keep Reddit, labelled unverified.
- `lookahead`: add `ros_dollars`, `trend30` and `sos` to the candidates and `my_roster`; a
  starter with falling FantasyCalc value is a weak point. Show FP ROS tier in the table.
- `sitstart`: `news` per player (2 items) in the payload and a news icon in Flags with the
  headline as its `title` tooltip.
- `README`: remove "Sleeper does not expose FAAB spent to date" from Known limitations and
  describe the room clearing curve.

**Tests:** ESPN news fixture (HTML stripping, date filter), FantasyCalc fixture (`sleeperId`
join, param derivation incl. super-flex), transactions fixture (complete vs failed, bids),
ROS fallback chain.

**Acceptance:**

- The waiver header shows real FAAB left.
- `/api/waivers` candidates carry `ros_dollars` and `news`.
- FantasyCalc join ≥ 98% of rostered players.

### Phase 5: Calibration

**`feeds/calibrate.py`**: `python3 -m sleeper_auction.feeds.calibrate --season 2026`

- For each completed week with snapshots, compare every source (Sleeper, ESPN, CBS, NFL,
  FanDuel, FFToday, Sharks, consensus, props, final) with actual league-scored points
  (`sitstart.actuals`, or nflverse weekly re-scored). Report MAE and bias by position,
  restricted to players who played.
- Fit blend weights: a 2-variable constrained least squares (props, consensus) by grid
  search over `w ∈ [0,1]` in 0.05 steps. Write `cache/calibration.json` with `n_weeks`,
  `n_players` and the MAE table.
- The post-week retrospective (`phase == "post"`) payload gets this week's per-source error
  for my players, so "which signals actually predicted it" is answered with data. Add a line
  to the `SYSTEM` phase notes.
- This is how the claim "props predict better than projections" gets tested for this
  league rather than assumed. The initial weights are deliberately moderate.

The `/trades` page is a separate feature: see section 10.

---

## 5. Appendix A: prop maths

**American odds → probability:** `p = 100/(o+100)` for `o > 0`, `p = −o/(−o+100)` for `o < 0`.
**De-vig a pair:** `p_over = p_o / (p_o + p_u)`. For a one-sided market (anytime TD "Yes"
only), apply a fixed haircut `p = p_yes × (1 − VIG_ONE_SIDED)` with `VIG_ONE_SIDED = 0.07`
until calibration.

**Yardage / receptions O/U** (`line L`, de-vigged `p_over`):

- median `m = L + σ·Φ⁻¹(p_over)`, where `σ = CV_market × L` and `Φ⁻¹ = statistics.NormalDist().inv_cdf`.
  When `p_over == 0.5`, `m = L`.
- mean `= m × MEAN_OVER_MEDIAN[market]` (yardage is right-skewed; fit it with `--fit`).
- p10 and p90 from a log-normal with that median and CV (for floor and ceiling only).

**Kalshi ladder** (thresholds `k₁ < … < kₙ`, survival `S(kᵢ) = P(X ≥ kᵢ)`, PAVA-enforced):

- `S(0) = 1`. Linear interpolation between rungs. Above `kₙ`, an exponential tail with the
  rate fitted from the last two rungs:
  `λ = ln(S(kₙ₋₁)/S(kₙ)) / (kₙ − kₙ₋₁)`, tail area `S(kₙ)/λ`.
- mean `E[X] = ∫₀^∞ S(x) dx` = trapezoids over the rungs + the tail area.
- median / p10 / p90: invert `S` at 0.5 / 0.9 / 0.1.
- Receptions and TDs are discrete: rungs at `N+` already mean `P(X ≥ N)`, so the same formula
  holds with unit steps.

**Touchdowns:**

- Anytime TD: `λ = −ln(1 − p)` (Poisson). Expected rush + receiving TD points = `6λ` (use the
  league's TD values).
- Passing TDs O/U `L` (for example 1.5): solve `P(N ≥ ⌈L⌉; λ) = p_over` for `λ` by
  bisection on `[0, 6]`. Interceptions the same way.

**Fantasy mean:** `Σ coefᵢ × E[statᵢ]` over components. By linearity of expectation this
holds whatever the correlation. Components without a market come from `filler_stats`.
`coverage = Σ market components / total`.

**Fantasy p10 / p90** (optional): 2,000-draw Monte Carlo (`random.Random(seed)`) sampling
yards by inverse survival and TDs as Poisson, with a Gaussian copula at `ρ = 0.5` between
yards and TDs. State in the payload that it is approximate. It is only for floor and
ceiling display.

**Blend:** `final = w·props + (1−w)·consensus` with `w` from coverage (section 4, Phase 3),
or from `calibration.json` when available.

---

## 6. Caching, cadence and request budget

| Feed | TTL | Cadence and notes |
|---|---|---|
| nflverse stats / xFP / pbp | 6 h | Nightly upstream. Mon–Tue after MNF is when it moves. |
| nflverse injuries + Sleeper practice | 2 h Wed–Sat, 12 h otherwise | Every fetch snapshots for the trajectory |
| nflverse snaps | 6 h | Daily upstream |
| Schedule (`games.csv`) | 2 h | Next week's lines appear mid-week |
| DP crosswalk / Sleeper players | 24 h | Already 24 h for `slp-players` |
| Kalshi | 15 min; 5 min within 2 h of first kickoff | ~10 series, paginated |
| The Odds API | 24 h per event unless `?refresh=props` | Quota guard (reserve 60 credits) |
| Projection scrapers | 3 h via prefetcher | 2 s between pages (5 s FantasyPros) |
| ESPN news | 30 min per player | ≤ 60 players per refresh |
| FantasyCalc | 12 h | |
| KeepTradeCut (opt-in) | 24 h | One page fetch a day; off unless `KTC_ENABLED=1` |
| RotoWire RSS | 30 min | |
| PFR advanced / NGS | 12 h | Weekly-ish upstream |
| FP ROS ECR | 12 h | |
| Sleeper transactions / rosters | 10 min | |
| League scoring | 24 h | |

---

## 7. Testing and verification

- `tests/` with `unittest` and fixtures only. **No network in tests.** Every parser gets a
  fixture, and every piece of maths gets a known-answer test.
- Each feed module has `--probe` that hits the live endpoint and prints counts, join rates
  and 3 sample rows. Run it before building on a source and paste the date and findings into
  the module docstring.
- Before each phase's commit:
  1. `python3 -m unittest discover -s tests`;
  2. the probe for every new feed;
  3. `./run.sh --draft <id> --me <n>` and load `/sitstart?noai=1`, `/waivers?noai=1` and
     `/lookahead?noai=1`; check that the JSON endpoints still return and that
     `?text=1` output is aligned;
  4. repeat step 3 with the new feeds forced to fail (`FEEDS_DISABLED=...`): no exceptions,
     and every old number is present.
- Keep `sleeper.py --selftest` passing.

---

## 8. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Scraped sites change markup (CBS, NFL.com, FFToday, FantasyPros) | Parse by header label; `--probe`; stale-if-error; consensus degrades to fewer sources and shows `n` |
| Name-join errors | ID crosswalk first; ambiguous names dropped; miss counters in `/api/feeds` |
| Thin Kalshi markets | Spread and OI filters; PAVA; `coverage` lowers the blend weight; the Odds API as second provider |
| Odds API free tier lacks props | Detected on the first call; provider disabled with the reason shown; Kalshi is primary anyway |
| Payload bloat raising AI cost and latency | Caps on news items, rounded floats, per-player fields only when present |
| Terms of use | Personal, non-commercial use; cache aggressively; honour crawl delays; the Kalshi and Odds API official APIs preferred for props; pick'em apps excluded |
| nflverse outage | Stale-if-error. The previous season's usage falls back as today, with the season recorded |
| Request-path latency | Prefetcher; `parallel()`; pages read cache |

---

## 9. Resolved questions and remaining notes

All four open questions were answered on 2026-09-25 (see "Decisions" at the top). Two notes
remain:

- The Odds API key was shared in a chat transcript. It is a free-tier key, but regenerate it
  from the Odds API dashboard if that matters to you. The implementation reads whatever
  `ODDS_API_KEY` holds, so rotating it needs no code change.
- KeepTradeCut's terms prohibit scrapers. It stays opt-in until you decide the one daily
  fetch is acceptable for personal use.

---

## 10. Separate feature: `/trades`

This is a new tool, not an enrichment. It ships on its own branch and PR **after** Phases 0–4,
because it needs `trade_values()`, `ros_points()`, `league.faab()` and the crosswalk. It
follows the shape of the other pages: a deterministic board, then an AI narrative streamed in
through `board.start_job`.

**Module:** `sleeper_auction/trades.py`, standalone like the others:
`python3 -m sleeper_auction.trades --draft <id> --me <n> [--limit 15] [--no-ai] [--json]`.

**Routes:** `GET /trades` (`?text=1`, `?noai=1`, `?refresh=1`) and `GET /api/trades`. Add a
"Trades" tab to every page's nav bar.

**Inputs, per roster in the league:**

- players, with `trade_values()` (FantasyCalc + KTC consensus, `disagree` flag),
  `ros_points` and bye week;
- starting lineup by ROS points, built with the same slot logic as `waivers._starters()` /
  `_flex_bar()` (generalise them to take any roster), using the league's slots;
- `need` per position = the ROS points lost between this roster's starter and the league
  median starter at that slot;
- surplus = bench players whose ROS points would start for another roster.

**Search (deterministic):**

1. Candidate partners: every other roster.
2. Packages: 1-for-1, 2-for-1 and 1-for-2, drawn from my surplus against their need, and the
   reverse. Prune to players with trade value above replacement (≥ $3 after `ros_dollars`)
   so the search stays at a few thousand pairs.
3. Score each package by:
   - `my_gain` = change in my ROS starting-lineup points;
   - `their_gain` = the same for them;
   - `value_gap` = the difference in consensus trade value, as a percentage of the larger
     side.
4. Keep packages where **both gains > 0** and `|value_gap| ≤ 10%`: fair by the market and
   useful to both, which is what gets accepted. Separately, list up to 3
   "value" packages where `my_gain > 0` and the value gap favours me by up to 20%. Label
   them as asks the other manager may decline.
5. The roster-size check accounts for 2-for-1s (the receiving side must drop someone: name
   their worst bench player by ROS points).
6. Rank by `my_gain`, break ties by the smaller value gap, and show the top `--limit`.

**Payload per proposal:** give, get, partner, `my_gain`, `their_gain`, `value_gap`,
per-player `fc` / `ktc` / `disagree`, `ros_points`, bye weeks, injury and practice status,
and the latest news headline. The AI is asked to argue both sides, name the pitch to the
other manager (what their roster gains), and flag when a `disagree` player makes the value
read unreliable. Schema: `proposals[{give, get, partner, verdict: PROPOSE|CONSIDER|AVOID,
pitch, risk}]`, `roster_read`.

**Also on the page (rail):** my positional surplus and need vs the league; each opponent's
need, which is who needs what I have; and a "sell-high" list reusing the Phase 1 `sell-high`
tag with trade values attached.

**Tests:** a synthetic 4-team league fixture with a known best 1-for-1, the
both-gain filter, the value-gap filter, 2-for-1 drop handling, and KTC off (FantasyCalc-only
values).

**Acceptance:** `/trades?noai=1` renders in under 2 s from a warm cache; every proposal has
`my_gain > 0` and `their_gain > 0` (or sits in the labelled "value" list); the page works
with KTC disabled; README gains a "Trades" section and the tool table goes from four tools to
five.
