# Check whether series reuse a champion across multiple games.
#
# Usage:
#   python scripts/check_fearless.py
#   python scripts/check_fearless.py --db data/scout.db --data ~/data/grid-starter
#   python scripts/check_fearless.py --examples 20

import argparse
import csv
import sqlite3
from collections import defaultdict
from pathlib import Path

DEFAULT_DATA = Path.home() / "data" / "grid-starter"
DEFAULT_DB = Path("data") / "scout.db"


def load_series(path):
    series = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            series[str(row["series_id"])] = {
                "league": row.get("event") or "(unknown league)",
                "teams": row.get("teams") or "(teams unknown)",
            }
    return series


def read_games(db_path):
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as db:
        return db.execute(
            """
            WITH picks AS (
                SELECT game_id, team_id, champion
                FROM game_players
                WHERE champion IS NOT NULL
                UNION
                SELECT game_id, team_id, champion
                FROM draft
                WHERE action = 'pick' AND champion IS NOT NULL
            )
            SELECT g.series_id, g.game_id, g.game_order, g.winner_team_id,
                   p.champion, p.team_id, COALESCE(t.name, p.team_id, '?')
            FROM games AS g
            LEFT JOIN picks AS p ON p.game_id = g.game_id
            LEFT JOIN game_teams AS t
                ON t.game_id = p.game_id AND t.team_id = p.team_id
            ORDER BY g.series_id, g.game_order, g.game_id, p.champion
            """
        ).fetchall()


def choose_examples(by_league, limit):
    selected = []
    league_names = sorted(by_league)
    indexes = {league: 0 for league in league_names}
    while len(selected) < limit:
        added = False
        for league in league_names:
            index = indexes[league]
            if index < len(by_league[league]):
                selected.append(by_league[league][index])
                indexes[league] += 1
                added = True
                if len(selected) == limit:
                    break
        if not added:
            break
    return selected


def format_game(game_id, game_order, teams):
    order = f"G{game_order}" if game_order is not None else f"game {game_id}"
    return f"{order} ({', '.join(sorted(teams))})"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--examples", type=int, default=10)
    args = parser.parse_args()
    if args.examples < 0:
        parser.error("--examples must be zero or greater")

    metadata = load_series(args.data.expanduser() / "lol" / "series.csv")
    rows = read_games(args.db.expanduser())

    series_by_league = defaultdict(set)
    picked_games = defaultdict(set)
    champion_games = defaultdict(lambda: defaultdict(lambda: defaultdict(set)))
    game_orders = defaultdict(dict)
    skipped_games = set()

    for series_id, game_id, game_order, winner_team_id, champion, _team_id, team_name in rows:
        series_id = str(series_id)
        league = metadata.get(series_id, {}).get("league", "(not in series.csv)")
        series_by_league[league].add(series_id)
        if winner_team_id is None:
            skipped_games.add(game_id)
            continue
        if champion is None:
            continue
        picked_games[series_id].add(game_id)
        game_orders[series_id][game_id] = game_order
        champion_games[series_id][champion][game_id].add(team_name)

    league_totals = defaultdict(lambda: {"series": 0, "multi_game": 0, "repeats": 0})
    examples_by_league = defaultdict(list)
    for league, series_ids in series_by_league.items():
        league_totals[league]["series"] = len(series_ids)

    for series_id, games in picked_games.items():
        league = metadata.get(series_id, {}).get("league", "(not in series.csv)")
        if len(games) >= 2:
            league_totals[league]["multi_game"] += 1

        repeats = {
            champion: game_map
            for champion, game_map in champion_games[series_id].items()
            if len(game_map) >= 2
        }
        if repeats:
            league_totals[league]["repeats"] += 1
            examples_by_league[league].append((series_id, repeats))

    print(f"Games skipped (no winner): {len(skipped_games)}")
    print("\nPer league:")
    print(f"  {'League':<40} {'Series':>8} {'2+ games with picks':>20} {'Repeats':>9} {'Share':>9}")
    for league in sorted(league_totals):
        totals = league_totals[league]
        share = (
            f"{totals['repeats'] / totals['multi_game']:.1%}"
            if totals["multi_game"]
            else "n/a"
        )
        print(
            f"  {league:<40.40} {totals['series']:>8} "
            f"{totals['multi_game']:>20} {totals['repeats']:>9} {share:>9}"
        )

    examples = choose_examples(examples_by_league, args.examples)
    print(f"\nExample violating series (up to {args.examples}):")
    if not examples:
        print("  None")
    for series_id, repeats in examples:
        league = metadata.get(series_id, {}).get("league", "(not in series.csv)")
        teams = metadata.get(series_id, {}).get("teams", "(teams unknown)")
        details = []
        for champion in sorted(repeats):
            game_map = repeats[champion]
            ordered_games = sorted(
                game_map,
                key=lambda game_id: (
                    game_orders[series_id][game_id] is None,
                    game_orders[series_id][game_id] or 0,
                    str(game_id),
                ),
            )
            appearances = [
                format_game(game_id, game_orders[series_id][game_id], game_map[game_id])
                for game_id in ordered_games
            ]
            details.append(f"{champion}: " + " -> ".join(appearances))
        print(f"  {league} | series {series_id} | {teams} | " + "; ".join(details))


if __name__ == "__main__":
    main()