# Scuttle Scout

Pre-match League of Legends scouting and a Fearless draft simulator, built for the
Cloud9 x JetBrains hackathon (Category 2).

**Live demo: https://scuttlebuddy.lol/coach**

## What it does

Scuttle Scout uses GRID pro-match data to answer the questions a coach asks before
and during a Fearless series:

- **Draft tendencies.** For any team: most-picked and most-banned champions overall,
  by game number in the series (game 1 to 5) and by side; which champions and roles
  they take with first pick; what they ban in the first three bans versus the last two.
- **Champion pools.** For each player: champions played, games, win rate, last played,
  and a "comfort" flag (3+ games in the last 6 months of data). Given the champions
  already burned in a series, it shows which comfort picks each player has left.
- **Fearless series simulator.** A Bo3 or Bo5 where side choice and first-pick choice
  are separate decisions (2026 rules), with 10 bans and 10 picks in pro draft order.
  A champion picked in an earlier game cannot be picked again by either team; bans do
  not carry over. Illegal actions are rejected with the reason.
- **Flags during the series.** As the draft goes on, it reports things worth acting on
  about the opponent:
  - a player with one or zero comfort picks left
  - a champion they first-pick in 50%+ of their games at this game number
  - a side where their win rate is 15+ points away from their overall win rate
  - how their jungler opens on each remaining comfort champion (start side, first
    scuttle side, full clear rate)
- **Jungle openings.** Start side, camps cleared before the first scuttle and first
  scuttle side for every jungler in every game, traced from position data.

Every number comes with its sample size (n), a baseline to compare against, and the
game ids it was computed from. When n is below 5 the tool shows "insufficient data"
instead of a number.

The dataset behind it: 728 series and 1,697 played games from LCK, LEC, LCS and LTA
(January 2025 to October 2026), 44 teams.

How each part works, and what the data cannot show, is in
[docs/methodology.md](docs/methodology.md).

## Run it locally

Requirements: Python 3.12 and the GRID starter dataset.

GRID data is not in this repository and cannot be redistributed. The code expects it
at `~/data/grid-starter` (`lol/*.jsonl.zst`, `lol/series.csv`, `meta/games.csv`);
`--data` points the scripts somewhere else. Without the data you can still run the
tests, which use synthetic data only, and use the live demo.

```bash
git clone https://github.com/DingVersion3/scuttle-scout.git
cd scuttle-scout
python3.12 -m venv .venv
source .venv/bin/activate
pip install zstandard fastapi 'uvicorn[standard]' httpx pytest
```

Run the tests (no GRID data needed):

```bash
pytest
```

Build the local database (`data/scout.db`, gitignored), in this order:

```bash
python -m scuttle_scout.ingest --all   # load every LoL series into SQLite
python -m scuttle_scout.roles          # infer roles, print the agreement rate
python -m scuttle_scout.paths          # trace jungle openings
```

Reports on the command line:

```bash
python -m scuttle_scout.drafts --team "Cloud9"          # draft tendencies
python -m scuttle_scout.drafts --team "Cloud9" --pools  # champion pools + Bo5 burn example
python -m scuttle_scout.paths --team "Cloud9"           # jungle openings game by game
python scripts/check_fearless.py                        # which series follow Fearless rules
```

Start the API:

```bash
uvicorn scuttle_scout.api:app
```

Interactive API docs are then at http://127.0.0.1:8000/docs.

| Endpoint | What it returns |
| --- | --- |
| `GET /teams` | Teams with their names and number of games |
| `GET /teams/{name}/report` | Draft tendencies and champion pools for a team |
| `POST /series` | Creates a series: `{"team_a": ..., "team_b": ..., "best_of": 3 or 5}` |
| `POST /series/{id}/action` | One action: `side`, `first-pick`, `ban`, `pick` or `winner` |
| `GET /series/{id}` | Series state plus the flags for both teams (`?opponent=` for one) |

Example:

```bash
curl -X POST localhost:8000/series -H 'content-type: application/json' \
  -d '{"team_a": "Cloud9", "team_b": "Team Liquid", "best_of": 5}'
curl -X POST localhost:8000/series/<id>/action -H 'content-type: application/json' \
  -d '{"action": "side", "team": "Cloud9", "side": "blue"}'
curl -X POST localhost:8000/series/<id>/action -H 'content-type: application/json' \
  -d '{"action": "first-pick", "team": "Team Liquid"}'
curl localhost:8000/series/<id>
```

Series state is kept in memory and is lost when the server restarts. The database and
data locations can be set with `SCUTTLE_SCOUT_DB` and `SCUTTLE_SCOUT_DATA`. Behind a
proxy that serves the app under `/coach`, start it with `--root-path /coach`.

## Repository layout

| Path | Purpose |
| --- | --- |
| `scuttle_scout/ingest.py` | GRID feed files to SQLite; drops rolled-back games and invalid feed sessions |
| `scuttle_scout/roles.py` | Role inference per game and per player |
| `scuttle_scout/paths.py` | Jungle paths and openings |
| `scuttle_scout/drafts.py` | Draft tendencies, champion pools, pool after burns |
| `scuttle_scout/simulator.py` | Fearless Bo3/Bo5 series state |
| `scuttle_scout/flags.py` | Findings about the opponent for a series in progress |
| `scuttle_scout/api.py` | FastAPI app |
| `scripts/` | Data exploration and the Fearless check |
| `tests/` | pytest suite on synthetic data |
| `docs/methodology.md` | Methods, verification numbers and data limits |

## Data and privacy

The public site serves computed statistics only. It never returns raw GRID rows and
never serves the database or anything under `data/` as a file. `data/` is gitignored.

## License

MIT License, Copyright (c) 2026 Jake Wilkinson. See [LICENSE](LICENSE).

The license covers the code in this repository. GRID data is not part of it and stays
under GRID's terms.
