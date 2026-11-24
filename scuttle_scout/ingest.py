# Ingest GRID LoL series files into a local SQLite database (data/scout.db).
# The database lives in data/, which is gitignored: GRID data must never be committed.
#
# Usage (from repo root, venv active):
#   python -m scuttle_scout.ingest --series 2748702     # one series + early-game density report
#   python -m scuttle_scout.ingest --all --limit 20     # first 20 series, quick test
#   python -m scuttle_scout.ingest --all                # every LoL series in series.csv
#
# Tables:
#   games         one row per game (series, order, duration, winner)
#   game_teams    team name/side/won per game
#   game_players  player name/team/champion per game
#   positions     player position samples (only when the position changed)
#   events        kills, objectives, structures with game time and actor position
#   draft         ordered picks/bans per game

import argparse
import csv
import io
import json
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

DEFAULT_DATA = Path.home() / "data" / "grid-starter"
DEFAULT_DB = Path("data") / "scout.db"

# Event types kept as discrete map events (prefix match)
KEEP_EVENT_PREFIXES = (
    "player-killed-player",
    "player-completed-slay",
    "player-completed-destroy",
    "team-completed-destroy",
    "team-won-game",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
    game_id TEXT PRIMARY KEY,
    series_id TEXT NOT NULL,
    game_order INTEGER,
    duration_s INTEGER,
    winner_team_id TEXT
);
CREATE TABLE IF NOT EXISTS game_teams (
    game_id TEXT, team_id TEXT, name TEXT, side TEXT, won INTEGER,
    PRIMARY KEY (game_id, team_id)
);
CREATE TABLE IF NOT EXISTS game_players (
    game_id TEXT, player_id TEXT, team_id TEXT, name TEXT, champion TEXT, jungle_cs INTEGER,
    PRIMARY KEY (game_id, player_id)
);
CREATE TABLE IF NOT EXISTS positions (
    game_id TEXT, player_id TEXT, t INTEGER, x REAL, y REAL, net_worth INTEGER
);
CREATE TABLE IF NOT EXISTS events (
    game_id TEXT, t INTEGER, type TEXT,
    actor_type TEXT, actor_id TEXT, actor_team_id TEXT,
    target_type TEXT, target_id TEXT,
    x REAL, y REAL
);
CREATE TABLE IF NOT EXISTS draft (
    game_id TEXT, seq INTEGER, action TEXT, team_id TEXT, champion TEXT,
    PRIMARY KEY (game_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_positions_game_player_t ON positions (game_id, player_id, t);
CREATE INDEX IF NOT EXISTS idx_events_game_t ON events (game_id, t);
CREATE INDEX IF NOT EXISTS idx_games_series ON games (series_id);
"""

PER_GAME_TABLES = ("games", "game_teams", "game_players", "positions", "events", "draft")


def iter_lines(path):
    # Python 3.14+ has zstd in the stdlib; older versions use the zstandard package
    try:
        from compression import zstd  # pyright: ignore[reportMissingImports]
        with zstd.open(path, "rt", encoding="utf-8") as f:
            yield from f
    except ImportError:
        import zstandard
        with open(path, "rb") as fh:
            reader = zstandard.ZstdDecompressor().stream_reader(fh)
            yield from io.TextIOWrapper(reader, encoding="utf-8")


def parse_series(path, series_id, session_valid=None):
    games = {}          # game_id -> game row dict
    teams = {}          # (game_id, team_id) -> team row dict
    players = {}        # (game_id, player_id) -> player row dict
    drafts = {}         # game_id -> list of draft actions
    positions = []
    events = []
    last_xy = {}        # (game_id, player_id) -> (x, y), to skip unchanged positions
    game_sessions = {}  # game_id -> feed session where it first appeared
    feed_session_valid = {}
    rolled_back = set()
    session = 1
    previous_sequence = None

    for line in iter_lines(path):
        line = line.strip()
        if not line:
            continue
        envelope = json.loads(line)
        try:
            sequence = int(envelope["sequenceNumber"])
        except (KeyError, TypeError, ValueError):
            sequence = None
        if sequence is not None:
            if previous_sequence is not None and sequence < previous_sequence:
                session += 1
            previous_sequence = sequence

        for event in envelope.get("events") or []:
            state = event.get("seriesState") or {}
            if "valid" in state:
                feed_session_valid[session] = state["valid"] is True

            if event.get("type") == "series-!started-game":
                target = event.get("target") or {}
                if target.get("id"):
                    rolled_back.add(target["id"])

            current_gid = None
            current_t = None

            for game in state.get("games") or []:
                gid = game.get("id")
                if not gid:
                    continue
                game_sessions.setdefault(gid, session)
                t = (game.get("clock") or {}).get("currentSeconds")

                g = games.setdefault(gid, {
                    "game_id": gid,
                    "series_id": series_id,
                    "game_order": len(games) + 1,
                    "duration_s": None,
                    "winner_team_id": None,
                })
                if t is not None:
                    g["duration_s"] = max(g["duration_s"] or 0, t)
                    # Last game in the list with a clock is the one being played
                    current_gid, current_t = gid, t

                # Keep the most complete draft seen for this game
                actions = game.get("draftActions") or []
                if len(actions) > len(drafts.get(gid, [])):
                    drafts[gid] = actions

                for team in game.get("teams") or []:
                    tid = team.get("id")
                    if not tid:
                        continue
                    team_row = teams.setdefault((gid, tid), {
                        "game_id": gid, "team_id": tid, "name": None, "side": None, "won": None,
                    })
                    for key in ("name", "side", "won"):
                        if team.get(key) is not None:
                            team_row[key] = team[key]
                    if team.get("won") is True:
                        g["winner_team_id"] = tid

                    for p in team.get("players") or []:
                        pid = p.get("id")
                        if not pid:
                            continue
                        player_row = players.setdefault((gid, pid), {
                            "game_id": gid, "player_id": pid, "team_id": tid,
                            "name": None, "champion": None, "jungle_cs": None,
                        })
                        if p.get("name"):
                            player_row["name"] = p["name"]
                        character = p.get("character")
                        if isinstance(character, dict) and character.get("name"):
                            player_row["champion"] = character["name"]

                        # Neutral monster kills (only in full-state snapshots); the jungler
                        # ends with far more than anyone else, which pins down the jungle role
                        for unit in p.get("unitKills") or []:
                            if unit.get("unitName") == "neutralMinion":
                                count = unit.get("count") or 0
                                player_row["jungle_cs"] = max(player_row["jungle_cs"] or 0, count)

                        pos = p.get("position")
                        if t is None or not isinstance(pos, dict):
                            continue
                        xy = (pos.get("x"), pos.get("y"))
                        if xy[0] is None or last_xy.get((gid, pid)) == xy:
                            continue
                        last_xy[(gid, pid)] = xy
                        positions.append((gid, pid, t, xy[0], xy[1], p.get("netWorth")))

            # Discrete map events (kills, objectives, structures)
            etype = event.get("type") or ""
            if current_gid and etype.startswith(KEEP_EVENT_PREFIXES):
                actor = event.get("actor") or {}
                target = event.get("target") or {}
                actor_state = actor.get("state") or {}
                actor_pos = (actor_state.get("game") or {}).get("position") or {}
                events.append((
                    current_gid, current_t, etype,
                    actor.get("type"), actor.get("id"), actor_state.get("teamId"),
                    target.get("type"), target.get("id"),
                    actor_pos.get("x"), actor_pos.get("y"),
                ))

    metadata_validity = (session_valid or {}).get(str(series_id), {})
    drop_reasons = {}
    for gid in games:
        if gid in rolled_back:
            drop_reasons[gid] = "rolled back"
            continue
        game_session = game_sessions[gid]
        valid = metadata_validity.get(game_session, feed_session_valid.get(game_session))
        if valid is not True:
            drop_reasons[gid] = "invalid session" if valid is False else "session validity unknown"
    for gid in rolled_back:
        drop_reasons[gid] = "rolled back"

    kept_game_ids = [gid for gid in games if gid not in drop_reasons]
    kept = set(kept_game_ids)
    for game_order, gid in enumerate(kept_game_ids, 1):
        games[gid]["game_order"] = game_order

    draft_rows = []
    for gid, actions in drafts.items():
        if gid not in kept:
            continue
        for a in actions:
            draft_rows.append((
                gid,
                a.get("sequenceNumber"),
                a.get("type"),
                (a.get("drafter") or {}).get("id"),
                (a.get("draftable") or {}).get("name"),
            ))

    return {
        "series_id": str(series_id),
        "games": [games[gid] for gid in kept_game_ids],
        "teams": [row for (gid, _), row in teams.items() if gid in kept],
        "players": [row for (gid, _), row in players.items() if gid in kept],
        "positions": [row for row in positions if row[0] in kept],
        "events": [row for row in events if row[0] in kept],
        "draft": draft_rows,
        "dropped": [
            {"game_id": gid, "session": game_sessions.get(gid), "reason": reason}
            for gid, reason in sorted(drop_reasons.items())
        ],
    }


def write_series(conn, parsed):
    series_id = parsed["series_id"]
    stored_game_ids = {
        row[0] for row in conn.execute("SELECT game_id FROM games WHERE series_id = ?", (series_id,))
    }
    current_game_ids = {g["game_id"] for g in parsed["games"]}
    dropped_game_ids = {game["game_id"] for game in parsed["dropped"]}
    # Keep old rows for games absent from the current feed; replace parsed games
    # and explicitly dropped games, including games that are now all-filtered.
    replaced_game_ids = current_game_ids | dropped_game_ids
    missing_game_ids = stored_game_ids - replaced_game_ids
    game_ids = [(game_id,) for game_id in sorted(replaced_game_ids)]
    for table in PER_GAME_TABLES:
        conn.executemany(f"DELETE FROM {table} WHERE game_id = ?", game_ids)

    conn.executemany(
        "INSERT INTO games VALUES (:game_id, :series_id, :game_order, :duration_s, :winner_team_id)",
        parsed["games"],
    )
    conn.executemany(
        "INSERT INTO game_teams VALUES (:game_id, :team_id, :name, :side, :won)",
        parsed["teams"],
    )
    conn.executemany(
        "INSERT INTO game_players VALUES (:game_id, :player_id, :team_id, :name, :champion, :jungle_cs)",
        parsed["players"],
    )
    conn.executemany("INSERT INTO positions VALUES (?, ?, ?, ?, ?, ?)", parsed["positions"])
    conn.executemany("INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", parsed["events"])
    conn.executemany("INSERT OR REPLACE INTO draft VALUES (?, ?, ?, ?, ?)", parsed["draft"])
    conn.commit()
    return missing_game_ids


def early_density_report(conn, series_id, window_s=240):
    # How well can we see the early jungle clear? Count position samples per player
    # in the first `window_s` seconds and measure the gaps between them.
    games = conn.execute(
        "SELECT game_id, game_order, duration_s FROM games WHERE series_id = ? ORDER BY game_order",
        (series_id,),
    ).fetchall()
    for game_id, order, duration in games:
        print(f"\n--- game {order} ({game_id[:8]}), {duration // 60 if duration else '?'} min ---")
        rows = conn.execute(
            """
            SELECT gt.side, gp.name, gp.champion, p.t
            FROM positions p
            JOIN game_players gp ON gp.game_id = p.game_id AND gp.player_id = p.player_id
            LEFT JOIN game_teams gt ON gt.game_id = gp.game_id AND gt.team_id = gp.team_id
            WHERE p.game_id = ? AND p.t BETWEEN 0 AND ?
            ORDER BY gt.side, gp.player_id, p.t
            """,
            (game_id, window_s),
        ).fetchall()

        by_player = {}
        for side, name, champ, t in rows:
            by_player.setdefault((side, name, champ), []).append(t)

        print(f"  first {window_s}s: samples / median gap / max gap")
        for (side, name, champ), times in by_player.items():
            gaps = [b - a for a, b in zip(times, times[1:]) if b > a]
            med = f"{statistics.median(gaps):.1f}s" if gaps else "-"
            mx = f"{max(gaps)}s" if gaps else "-"
            print(f"  {str(side):<5} {str(name):<14} {str(champ):<14} {len(times):>4}  {med:>6}  {mx:>6}")

        scuttles = conn.execute(
            """
            SELECT e.t, e.type, gp.name, gt.side
            FROM events e
            LEFT JOIN game_players gp ON gp.game_id = e.game_id AND gp.player_id = e.actor_id
            LEFT JOIN game_teams gt ON gt.game_id = e.game_id AND gt.team_id = e.actor_team_id
            WHERE e.game_id = ? AND e.type LIKE '%slayRiftScuttler%'
            ORDER BY e.t LIMIT 2
            """,
            (game_id,),
        ).fetchall()
        for t, etype, name, side in scuttles:
            print(f"  scuttle: {etype.split('slay')[-1]} at {t // 60}:{t % 60:02d} by {name} ({side})")


def migrate(conn):
    # Add columns introduced after a database was first created
    cols = {row[1] for row in conn.execute("PRAGMA table_info(game_players)")}
    if "jungle_cs" not in cols:
        conn.execute("ALTER TABLE game_players ADD COLUMN jungle_cs INTEGER")
        conn.commit()


def load_series_index(data_dir):
    index_path = data_dir / "lol" / "series.csv"
    if not index_path.exists():
        sys.exit(f"Missing {index_path}. Is the sparse checkout of lol/ done?")
    with open(index_path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_session_validity(data_dir):
    games_path = data_dir / "meta" / "games.csv"
    if not games_path.exists():
        return None
    with open(games_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if "session_valid" not in (reader.fieldnames or []):
            return None
        validity = defaultdict(dict)
        for row in reader:
            if row.get("title") != "lol":
                continue
            try:
                session = int(row["session"])
            except (KeyError, TypeError, ValueError):
                continue
            validity[str(row["series_id"])][session] = row.get("session_valid", "").strip().lower() == "true"
    return dict(validity)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--series", help="ingest one series by id and print a density report")
    group.add_argument("--all", action="store_true", help="ingest every LoL series")
    parser.add_argument("--limit", type=int, help="with --all, only ingest the first N series")
    args = parser.parse_args()

    data_dir = args.data.expanduser()
    index = load_series_index(data_dir)
    session_validity = load_session_validity(data_dir)
    if args.series:
        index = [row for row in index if row["series_id"] == args.series]
        if not index:
            sys.exit(f"Series {args.series} not found in series.csv")
    elif args.limit:
        index = index[: args.limit]

    args.db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(args.db)
    conn.executescript(SCHEMA)
    migrate(conn)

    dropped_by_reason = Counter()
    dropped_series_by_reason = defaultdict(set)
    missing_feed_series = set()
    missing_feed_games = 0
    for i, row in enumerate(index, 1):
        path = data_dir / row["file"]
        if not path.exists():
            print(f"[{i}/{len(index)}] missing file {path}, skipping")
            continue
        parsed = parse_series(path, row["series_id"], session_validity)
        missing_game_ids = write_series(conn, parsed)
        missing_feed_games += len(missing_game_ids)
        if missing_game_ids:
            missing_feed_series.add(row["series_id"])
        series_drops = Counter(game["reason"] for game in parsed["dropped"])
        for game in parsed["dropped"]:
            dropped_by_reason[game["reason"]] += 1
            dropped_series_by_reason[game["reason"]].add(row["series_id"])
        drop_summary = ", ".join(f"{reason}: {count}" for reason, count in sorted(series_drops.items()))
        print(
            f"[{i}/{len(index)}] {row['series_id']} {row['teams']}: "
            f"{len(parsed['games'])} games, {len(parsed['positions'])} positions, "
            f"{len(parsed['events'])} events, {len(parsed['dropped'])} dropped"
            f"{f' ({drop_summary})' if drop_summary else ''}"
        )

    print(f"\nDropped games: {sum(dropped_by_reason.values())}")
    for reason in sorted(dropped_by_reason):
        series_ids = ", ".join(sorted(dropped_series_by_reason[reason]))
        print(f"  {reason}: {dropped_by_reason[reason]} (series: {series_ids})")
    print(
        f"Games absent from current feeds retained unchanged: {missing_feed_games} "
        f"(series: {', '.join(sorted(missing_feed_series)) or 'none'})"
    )

    if args.series:
        early_density_report(conn, args.series)

    conn.close()


if __name__ == "__main__":
    main()