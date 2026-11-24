# Scuttle Scout: project rules

## What this is
Pre-match League of Legends scouting + Fearless draft simulator for the
Cloud9 x JetBrains hackathon (Category 2). Python 3.12, SQLite, FastAPI.
The demo runs on scuttlebuddy.lol/coach.

## Hard rules
- NEVER commit GRID data, data/scout.db, or anything under data/. GRID terms forbid it.
- scuttlebuddy.lol is public: serve computed stats only. Never raw GRID rows,
  never data/ or scout.db as files.
- Use # comments, not docstrings.
- Prefer small targeted changes over large refactors.
- Every scouting finding must carry: sample size (n), a baseline to compare against,
  and the game_ids it came from. No finding without a denominator.
- Show "insufficient data" instead of a number when n < 5.

## Data facts (verified)
- Raw data: ~/data/grid-starter/lol/*.jsonl.zst, index at lol/series.csv.
- scuttle_scout/ingest.py loads it into data/scout.db.
  Tables: games, game_teams, game_players (incl. jungle_cs), positions, events, draft.
- scuttle_scout/roles.py: player_roles (lane played per game), roster_roles (player's real role).
  Always use roster_roles for a player's role.
- scuttle_scout/paths.py: jungle_paths, jungle_openings.
- draft table: (game_id, seq, action 'pick'/'ban', team_id, champion), ordered by seq.
- games.game_order = game number within the series. meta/games.csv also has patch.
- meta/rosters.csv: per game per player: team, side, player, champion,
  participation_status, session. It has NO role/position column, so roles
  still come from roster_roles.
- A 'series-!started-game' event means that game was rolled back: exclude it.
- Some series have two feed sessions (sequenceNumber restarts at 1); only keep
  valid sessions. Known LoL case: 2885343.
- Coordinates are standard Riot units: blue base ~(0,0), red base ~(14800,14800).
- No ward placement data exists in GRID. Don't build warding features.
- Positions are event-driven snapshots (~2-4s apart, gaps up to ~50s early game).

## Fearless rules (2026)
- A champion picked by either team in a series can't be picked again by either team
  for the rest of that series. Bans are per game and don't carry over.
- Side choice and first-pick choice are separate decisions in 2026.