# Methodology

How Scuttle Scout gets from GRID feed files to scouting findings, how each step was
checked, and what the data cannot show.

All numbers below were computed from the local database built from the GRID starter
dataset: 728 series, 1,697 played games, 44 teams, LCK / LEC / LCS / LTA,
2025-01-15 to 2026-10-13.

## Rules every finding follows

- Each finding carries its sample size (n), a baseline to compare against, and the
  game ids it came from.
- When n is below 5 the tool prints "insufficient data" instead of a number. This also
  applies to baselines.
- Baselines are like-for-like: other teams in the same bucket (same side, same game
  number, same ban phase), other players on the same champion, or the team's own
  overall win rate for a side win rate.

## 1. Role inference

GRID data has no role or position field, so roles are inferred per game from where
players are between 1:30 and 10:00 (`scuttle_scout/roles.py`).

### Zones

Each position sample is put into a zone. Coordinates are standard Riot units: blue
base near (0, 0), red base near (14800, 14800).

| Zone | Rule |
| --- | --- |
| base | x < 3000 and y < 3000, or x > 11800 and y > 11800 (ignored) |
| mid | within 1100 units of the x = y diagonal |
| top | x < 2500 or y > 12300 (left and top edges) |
| bot | y < 2500 or x > 12300 (bottom and right edges) |
| jungle | everything else |

### Assigning the five roles of a team

1. **Jungle: jungle CS.** The jungler is the player with the most neutral monster kills
   at the end of the game, if that count is at least 30 and at least twice the
   runner-up's. If no player passes, the fallback is the player with the largest share
   of samples in the jungle zone. In the current data the jungle CS rule decided all
   3,394 team-games; the fallback was never needed.
2. **Mid: zone.** Of the remaining four, the player with the largest share of samples
   in the mid zone.
3. **Top versus duo: proximity.** The remaining three are side laners. The two with
   the smallest mean distance to each other (measured at the timestamps where both
   were sampled) are the bot-lane duo. The third is the top laner.
   This is what makes lane swaps work: in a swap the duo goes to the top lane and the
   top laner goes bot, so which side lane a player stands in says nothing about the
   role. Who they stand next to does.
4. **Bot versus support: net worth.** Within the duo, the player with the lower net
   worth at 10:00 is the support.

A team with fewer than five players with position samples is skipped rather than
guessed. All 1,697 played games received all ten roles.

### Per-game role versus player role

- `player_roles`: the role inferred in each single game.
- `roster_roles`: a player's role is the one inferred most often across all games.
  Everything that needs "this player's role" (first-pick roles, champion pools, the
  jungler in the flags) uses `roster_roles`.

### Agreement rate

**99.52%** (16,728 of 16,809 player-games; 251 players with 10 or more games).

What this measures: for each player, the share of games where the single-game
inference gives the same role as that player's most common role. Pro players almost
always play one role, so a game with a different role is treated as an inference error
until shown otherwise. It is an internal consistency check. There is no role label in
GRID to compare against, so it is not accuracy against ground truth.

The 81 disagreeing player-games belong to 52 players:

| Disagreement | Player-games |
| --- | --- |
| mid and bot mixed up | 30 |
| top and bot mixed up | 27 |
| usual jungler inferred as support | 10 |
| bot and support mixed up | 10 |
| mid and top mixed up | 4 |

We have not classified these one by one, so some may be real role swaps.

Reproduce: `python -m scuttle_scout.roles` prints the rate and the players with
off-role games.

## 2. Jungle openings

`scuttle_scout/paths.py` traces each jungler's first clear from position samples.

### Tracing

- The jungler of a game is the player with role `jungle` in `player_roles`.
- Samples from 1:00 to 5:00 are used. Tracing stops at the first recall (a sample in
  base after 1:30).
- Each sample is matched to the nearest of 12 jungle camps and 2 scuttle spots. It
  counts as "at the camp" within 800 units of the camp centre.
- A visit needs 2 or more consecutive samples at the same camp, so walking past a camp
  does not count.
- Camp centres were calibrated on the data: `--calibrate` prints the mean jungler
  position near each camp next to the centre we use.
- Camps are named from the jungler's point of view. "Red" is their own red buff,
  "enemy Red" means an invade.

### Why the first camp cannot be seen

Positions are not sampled on a clock. A position only arrives as part of the state
attached to a feed event, and at the start of the game there are almost no events.
For junglers the feed is close to silent from about **0:53 to 1:43**:

- For the median jungler, the gap between samples around 1:30 starts at 0:52 and ends
  at 1:44 (median length 53 s; 30 s or longer for 2,146 of 3,394 junglers).
- 1,392 of 3,394 junglers (41%) have no position sample at all between 0:53 and 1:43.
  2,124 (63%) have two or fewer, and a visit needs two at the same camp.
- The first camp that shows up in a traced path has a median time of 1:46
  (10th percentile 1:12, 90th percentile 1:57).

The first camp is killed inside that gap. The first camp we see is normally the
second one of the clear.

### What we report instead

Only the parts of the opening the data shows reliably (`jungle_openings`, one row per
jungler per game):

| Field | Definition | Missing |
| --- | --- | --- |
| Start side | Half of their own jungle (top or bot) where the first own camp is seen | 40 of 3,394 |
| Camps before scuttle | Distinct own camps seen before the first scuttle kill (before 3:45 if there is none), plus 1 for the unseen first camp, capped at 6 | 52 of 3,394 |
| First scuttle | Top or bot, taken from the scuttle kill event of that player, not from positions | 16 of 3,394 |

A "full clear" is 6 camps before the first scuttle: 1,935 of 3,342 openings.

Known weaknesses:

- Start side assumes the second camp is in the same half as the unseen first camp.
  That holds for a standard clear but cannot be checked in the data.
- The "+1" for the unseen first camp is always added. When the first camp was in fact
  sampled, the count is one too high (never above 6).
- Start side is relative to the team's own jungle, so it can be compared across blue
  and red side. It is not split by map side because that halves n.

### Use in the flags

For the opponent jungler's remaining comfort champions, the flags report the majority
start side, the majority first scuttle side and the full clear rate, each with its own
denominator, against other junglers on the same champion.

## 3. Fearless rules

2026 rules as implemented:

- A champion picked by either team in a series cannot be picked again by either team
  for the rest of that series.
- Bans are per game and do not carry over.
- Side choice and first-pick choice are separate decisions.

### Which series in the data are Fearless

`scripts/check_fearless.py` counts, per league, the series where a champion was picked
in more than one game. Picks are taken from both the draft and the player rows; games
without a winner are skipped.

Result: 0 of the 556 series with two or more played games contain a repeated pick,
across all 20 events in the dataset. So the whole dataset is used. The other 172
series have a single played game and cannot show a repeat; they are kept by default
(a single game has the same draft constraints as a game 1) and `--strict-fearless`
leaves them out.

### Enforcement in the simulator

`scuttle_scout/simulator.py` (`SeriesState`) holds a Bo3 or Bo5:

- **Per game:** which team is on blue side and which team has first pick, as two
  independent inputs. In the data the red-side team has first pick in 238 of the
  1,682 games with a standard draft, so first pick cannot be derived from side.
- **Draft order:** 20 actions in the standard pro order, with F the first-pick team
  and S the other team: bans F S F S F S, picks F S S F F S, bans S F S F, picks
  S F F S. 1,682 of the 1,697 played games in the data follow exactly this order; 13
  have fewer than 20 recorded actions (missed bans or no draft in the feed) and 2 have
  bans in other slots.
- **Burned champions:** every champion picked so far in the series, by either team. A
  pick is burned the moment it is locked.
- **A pick is rejected** when the champion was picked in an earlier game (the error
  names the game), was already picked in this game, or is banned in this game.
- **A ban is rejected** when the champion is already banned in this game or is burned.
  Banning a burned champion is our own restriction: it would waste a ban, and it never
  happens in the data (0 of 16,952 bans).
- **Bans do not carry over:** the available champions are all champions minus the
  burned ones minus the bans of the current game only.
- Also rejected: acting out of turn, a ban when a pick is due (and the reverse), an
  unknown champion, a winner before the draft is complete, any action after the series
  ended.
- **Series end:** 2 wins in a Bo3, 3 wins in a Bo5.
- Champion names are matched ignoring case and punctuation ("ksante" is "K'Sante").
  The champion list is the 156 champions that appear in the data.

`tests/test_simulator.py` covers each rule, the draft order slot by slot, and the
series end. The full suite (simulator, flags, API) is 30 tests on synthetic data.

### Fearless in the scouting

- **Comfort pick:** a champion a player has played 3 or more times in the last 6
  months of data. The window is anchored on the latest game in the data, not on
  today's date.
- **Pool after burns:** each player's comfort picks minus the burned champions. During
  a game in progress the flags also remove that game's bans.
- A player's pool includes his games for previous teams.

## 4. Data limits

- **No ward placement data.** GRID does not provide it, so there are no warding or
  vision features.
- **Event-driven position sampling.** Positions arrive with feed events, and we store
  a sample only when the position changed. Over a 100-game sample the time between
  two samples of a player has a median of 3 s, a 90th percentile of 8 s and a 99th
  percentile of 20 s. In the first five minutes the 99th percentile is 53 s and the
  longest gap 98 s. Consequences:
  - the first jungle camp is not visible (section 2)
  - zone shares are shares of samples, not of seconds
  - a camp visit needs two consecutive samples
  - distance between two players is only measured at timestamps both were sampled
- **Rolled-back games.** A `series-!started-game` event means the game was rolled
  back. `ingest.py` drops the game and all its rows. This removed 1 game.
- **Multi-session feeds.** A feed can contain two sessions: the envelope
  `sequenceNumber` restarts at 1 and the second session starts with an empty games
  list. `ingest.py` detects the restart, assigns each game to the session where it
  first appeared, and keeps only games from valid sessions. Validity comes from the
  `session_valid` column of `meta/games.csv` when it exists, otherwise from the last
  `valid` flag the feed gave for that session. This removed 45 games. Game numbers
  are renumbered after a drop.
- **Games without a winner.** 40 games in the database are shells with a draft but no
  result. They are excluded from every statistic.
- **Game number.** The game number in a series is the rank among played games, not the
  position in the feed, because those shells still occupy a slot.
- **No dates in the feed tables.** Game dates come from `meta/games.csv`.
- **Game duration** in the feed is unreliable and is not used.
- **Team names.** Some teams appear under two or three names; statistics are grouped
  by team id.
- **Small samples.** Most splits by champion, side and game number are small. That is
  why n and the game ids are always shown and n < 5 gives no number.
- **Publication.** GRID data is not in the repository. The public site returns
  computed statistics only.

## 5. JetBrains tools and Junie

What can be stated from the repository and from the Junie session of 2026-11-24:

- The project is a JetBrains IDE Python project (`.idea/` is committed; the
  interpreter is the project `.venv`).
- `.junie/guidelines.md` holds the project rules Junie reads on every task: never
  commit or serve GRID data, `#` comments instead of docstrings, every finding needs
  n / baseline / game ids, "insufficient data" below n = 5, the verified data facts,
  and the 2026 Fearless rules.
- In that session Junie wrote, each as a separate task:
  - `scripts/check_fearless.py`
  - the rollback and multi-session fix in `scuttle_scout/ingest.py`, followed by a
    full re-ingest
  - `scuttle_scout/drafts.py` (draft tendencies, then champion pools and
    `pool_after_burns`)
  - `scuttle_scout/simulator.py` and `tests/test_simulator.py`
  - `scuttle_scout/flags.py` and `tests/test_flags.py`
  - `scuttle_scout/api.py` and `tests/test_api.py`
  - this document and the README
- For each task Junie first wrote a plan based on queries against the real database,
  then checked the result against independent SQL. Data problems found this way that
  changed the design:
  - a 4-second remake with the same draft as the next game, which would have counted
    as a Fearless violation; games without a winner are now skipped
  - first pick is not tied to side (red side has it in 238 games), so the simulator
    takes it as a separate input
  - missed bans are absent from the feed, so ban phases are split by position in the
    draft, not by counting bans per team
  - the team name "LOUD" also matches "Cloud9 Kia" in a substring search; an exact
    name now wins

> **TODO (author):** which JetBrains IDE and which of its features were used by hand
> (debugger, database tools, test runner, ...).

> **TODO (author):** how `roles.py`, `paths.py`, `scripts/explore.py` and the first
> version of `ingest.py` were written (commits of 2026-11-18). That work is outside
> the session this section is based on.

> **TODO (author):** anything from your notes that is missing here. The notes were
> not available when this document was written.
