# Draft scouting reports for a team, using Fearless series only.
#
# Usage:
#   python -m scuttle_scout.drafts --team "Cloud9"
#   python -m scuttle_scout.drafts --team "Cloud9" --db data/scout.db --top 5
#   python -m scuttle_scout.drafts --team "Cloud9" --strict-fearless

import argparse
import calendar
import csv
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

DEFAULT_DB = Path("data") / "scout.db"
DEFAULT_DATA = Path.home() / "data" / "grid-starter"
MIN_SAMPLE = 5
GAME_NUMBERS = range(1, 6)
SIDES = ("blue", "red")
COMFORT_GAMES = 3
COMFORT_MONTHS = 6


def fearless_series(conn):
    games = conn.execute(
        "SELECT game_id, series_id FROM games WHERE winner_team_id IS NOT NULL"
    ).fetchall()
    series_games = defaultdict(set)
    for game_id, series_id in games:
        series_games[str(series_id)].add(game_id)

    picks = conn.execute(
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
        SELECT g.series_id, p.game_id, p.champion
        FROM games AS g
        JOIN picks AS p ON p.game_id = g.game_id
        WHERE g.winner_team_id IS NOT NULL
        """
    ).fetchall()
    champion_games = defaultdict(lambda: defaultdict(set))
    for series_id, game_id, champion in picks:
        champion_games[str(series_id)][champion].add(game_id)

    repeated = {
        series_id
        for series_id, champions in champion_games.items()
        if any(len(game_ids) > 1 for game_ids in champions.values())
    }
    played_series = set(series_games)
    return played_series - repeated, repeated, series_games


def _game_numbers(conn):
    rows = conn.execute(
        """
        SELECT game_id, series_id, game_order
        FROM games
        WHERE winner_team_id IS NOT NULL
        """
    ).fetchall()
    by_series = defaultdict(list)
    for game_id, series_id, game_order in rows:
        by_series[str(series_id)].append((game_id, game_order))

    numbers = {}
    for series_id, games in by_series.items():
        games.sort(key=lambda game: (game[1] is None, game[1] or 0, str(game[0])))
        for game_number, (game_id, _) in enumerate(games, 1):
            numbers[game_id] = game_number
    return numbers


class TeamNotFound(ValueError):
    pass


class AmbiguousTeam(ValueError):
    pass


def _team_matches(conn, team_query):
    query = str(team_query).strip()
    matches = conn.execute(
        "SELECT DISTINCT team_id, name FROM game_teams WHERE name LIKE ? ORDER BY name, team_id",
        (f"%{query}%",),
    ).fetchall()
    exact_ids = {
        team_id for team_id, name in matches
        if team_id is not None and (name or "").casefold() == query.casefold()
    }
    if len(exact_ids) == 1:
        matches = [row for row in matches if row[0] in exact_ids]
    team_ids = {team_id for team_id, _ in matches if team_id is not None}
    if not team_ids:
        raise TeamNotFound(f"no team matching {team_query!r}")
    if len(team_ids) > 1:
        labels = ", ".join(
            f"{name or '(unknown name)'} [{team_id}]" for team_id, name in matches
        )
        raise AmbiguousTeam(f"team query matched multiple team IDs; use a narrower name: {labels}")
    team_id = next(iter(team_ids))
    names = sorted({name for matched_id, name in matches if matched_id == team_id and name})
    return team_id, names or [team_query]


def _available_roles(conn):
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'roster_roles'"
    ).fetchone()
    if not exists:
        raise ValueError("roster_roles is missing; run python -m scuttle_scout.roles first")

    roles = defaultdict(set)
    for game_id, team_id, champion, role in conn.execute(
        """
        SELECT gp.game_id, gp.team_id, gp.champion, rr.role
        FROM game_players AS gp
        JOIN roster_roles AS rr ON rr.player_id = gp.player_id
        WHERE gp.champion IS NOT NULL AND rr.role IS NOT NULL
        """
    ):
        roles[(game_id, team_id, champion)].add(role)
    if not roles:
        raise ValueError("roster_roles has no role data; run python -m scuttle_scout.roles first")
    return roles


def _team_game_rows(conn, included_series, game_numbers, roles):
    game_teams = conn.execute(
        """
        SELECT g.game_id, g.series_id, gt.team_id, gt.name, gt.side
        FROM games AS g
        JOIN game_teams AS gt ON gt.game_id = g.game_id
        WHERE g.winner_team_id IS NOT NULL
        ORDER BY g.series_id, g.game_order, g.game_id
        """
    ).fetchall()
    rows = {}
    for game_id, series_id, team_id, name, side in game_teams:
        if str(series_id) not in included_series or team_id is None:
            continue
        normalized_side = (side or "").lower()
        rows[(game_id, team_id)] = {
            "game_id": game_id,
            "series_id": str(series_id),
            "game_number": game_numbers.get(game_id),
            "team_id": team_id,
            "team_name": name,
            "side": normalized_side,
            "picks": set(),
            "pick_available": False,
            "bans": {"first_3": set(), "last_2": set()},
            "ban_phase_available": set(),
            "first_pick_champion": None,
            "first_pick_role": None,
        }

    draft_by_game = defaultdict(list)
    for game_id, seq, action, team_id, champion in conn.execute(
        "SELECT game_id, seq, action, team_id, champion FROM draft ORDER BY game_id, seq"
    ):
        if game_id in game_numbers and champion:
            draft_by_game[game_id].append((seq, action, team_id, champion))

    fallback_picks = defaultdict(set)
    for game_id, team_id, champion in conn.execute(
        "SELECT game_id, team_id, champion FROM game_players WHERE champion IS NOT NULL"
    ):
        if game_id in game_numbers:
            fallback_picks[(game_id, team_id)].add(champion)

    drafts_by_game = defaultdict(list)
    for (game_id, _), _row in rows.items():
        drafts_by_game[game_id] = draft_by_game.get(game_id, [])

    for game_id, game_actions in drafts_by_game.items():
        picks = sorted(
            (action for action in game_actions if action[1] == "pick"),
            key=lambda action: (action[0] is None, action[0] or 0),
        )
        first_pick = picks[0] if picks else None
        first_pick_seq = first_pick[0] if first_pick else None
        for (row_game_id, team_id), row in rows.items():
            if row_game_id != game_id:
                continue
            team_picks = {champion for _, action, tid, champion in game_actions
                          if action == "pick" and tid == team_id}
            if team_picks:
                row["picks"] = team_picks
                row["pick_available"] = True
            elif fallback_picks.get((game_id, team_id)):
                row["picks"] = fallback_picks[(game_id, team_id)]
                row["pick_available"] = True

            if first_pick and first_pick[2] == team_id:
                row["first_pick_champion"] = first_pick[3]
                role_matches = roles.get((game_id, team_id, first_pick[3]), set())
                row["first_pick_role"] = sorted(role_matches)[0] if role_matches else None

            for seq, action, action_team_id, champion in game_actions:
                if action != "ban" or action_team_id != team_id or first_pick_seq is None:
                    continue
                phase = "first_3" if seq is not None and seq < first_pick_seq else "last_2"
                row["bans"][phase].add(champion)
                row["ban_phase_available"].add(phase)

    return list(rows.values())


def _event_values(row, field):
    if field == "all_bans":
        value = row["bans"]["first_3"] | row["bans"]["last_2"]
    elif field in ("first_3_bans", "last_2_bans"):
        value = row["bans"][field.removesuffix("_bans")]
    else:
        value = row.get(field)
    if value is None:
        return set()
    if isinstance(value, set):
        return value
    return {value}


def tally(records, baseline_records, field, eligible):
    samples = [row for row in records if eligible(row)]
    baseline_samples = [row for row in baseline_records if eligible(row)]
    game_ids = sorted({row["game_id"] for row in samples})
    baseline_game_ids = sorted({row["game_id"] for row in baseline_samples})
    counts = defaultdict(set)
    baseline_counts = defaultdict(set)
    for row in samples:
        for name in _event_values(row, field):
            counts[name].add((row["game_id"], row["team_id"]))
    for row in baseline_samples:
        for name in _event_values(row, field):
            baseline_counts[name].add((row["game_id"], row["team_id"]))

    rows = []
    for name in sorted(counts, key=lambda value: (-len(counts[value]), str(value).lower())):
        found_ids = sorted({game_id for game_id, _ in counts[name]})
        baseline_ids = sorted({game_id for game_id, _ in baseline_counts[name]})
        rows.append({
            "name": name,
            "count": len(counts[name]),
            "game_ids": found_ids,
            "share": len(counts[name]) / len(samples) if len(samples) >= MIN_SAMPLE else None,
            "baseline_count": len(baseline_counts[name]),
            "baseline_game_ids": baseline_ids,
            "baseline_share": (
                len(baseline_counts[name]) / len(baseline_samples)
                if len(baseline_samples) >= MIN_SAMPLE else None
            ),
            "baseline_n": len(baseline_samples),
        })

    return {
        "n": len(samples),
        "game_ids": game_ids,
        "insufficient": len(samples) < MIN_SAMPLE,
        "baseline_n": len(baseline_samples),
        "baseline_game_ids": baseline_game_ids,
        "baseline_insufficient": len(baseline_samples) < MIN_SAMPLE,
        "rows": rows,
    }


def _build_scope_stats(team_rows, baseline_rows):
    stats = {
        "games": len({row["game_id"] for row in team_rows}),
        "picked": {"overall": None, "by_game": {}},
        "banned": {"overall": None, "by_game": {}},
        "first_pick_champions": None,
        "first_pick_roles": None,
        "bans_by_phase": {},
    }
    stats["picked"]["overall"] = tally(
        team_rows, baseline_rows, "picks", lambda row: row["pick_available"]
    )
    stats["banned"]["overall"] = tally(
        team_rows, baseline_rows, "all_bans", lambda row: bool(row["bans"]["first_3"] | row["bans"]["last_2"])
    )
    for game_number in GAME_NUMBERS:
        team_games = [row for row in team_rows if row["game_number"] == game_number]
        baseline_games = [row for row in baseline_rows if row["game_number"] == game_number]
        stats["picked"]["by_game"][game_number] = tally(
            team_games, baseline_games, "picks", lambda row: row["pick_available"]
        )
        stats["banned"]["by_game"][game_number] = tally(
            team_games, baseline_games, "all_bans",
            lambda row: bool(row["bans"]["first_3"] | row["bans"]["last_2"]),
        )

    stats["first_pick_champions"] = tally(
        team_rows, baseline_rows, "first_pick_champion",
        lambda row: row["first_pick_champion"] is not None,
    )
    stats["first_pick_roles"] = tally(
        team_rows, baseline_rows, "first_pick_role",
        lambda row: row["first_pick_role"] is not None,
    )
    for phase in ("first_3", "last_2"):
        stats["bans_by_phase"][phase] = tally(
            team_rows, baseline_rows, f"{phase}_bans",
            lambda row, phase=phase: phase in row["ban_phase_available"],
        )
    return stats


def team_draft_stats(conn, team_query, strict_fearless=False):
    team_id, team_names = _team_matches(conn, team_query)
    roles = _available_roles(conn)
    fearless, repeated, series_games = fearless_series(conn)
    eligible_series = set(fearless)
    one_game_series = {series_id for series_id, games in series_games.items() if len(games) == 1}
    strict_excluded = set()
    if strict_fearless:
        strict_excluded = eligible_series & one_game_series
        eligible_series -= strict_excluded

    game_numbers = _game_numbers(conn)
    all_team_rows = _team_game_rows(conn, eligible_series, game_numbers, roles)
    team_rows = [row for row in all_team_rows if row["team_id"] == team_id]
    baseline_rows = [row for row in all_team_rows if row["team_id"] != team_id]
    scopes = {"overall": (team_rows, baseline_rows)}
    for side in SIDES:
        scopes[side] = (
            [row for row in team_rows if row["side"] == side],
            [row for row in baseline_rows if row["side"] == side],
        )

    return {
        "team_query": team_query,
        "team_id": team_id,
        "team_names": team_names,
        "strict_fearless": strict_fearless,
        "filter": {
            "played_series": len(series_games),
            "fearless_series": len(fearless),
            "repeated_series_excluded": len(repeated),
            "one_game_series": len(one_game_series),
            "strict_one_game_series_excluded": len(strict_excluded),
        },
        "games": len({row["game_id"] for row in team_rows}),
        "scopes": {
            name: _build_scope_stats(*scope_rows)
            for name, scope_rows in scopes.items()
        },
    }


def _short_ids(game_ids):
    return ", ".join(str(game_id)[:8] for game_id in game_ids) or "none"


def _sample_text(count, n, share):
    if n < MIN_SAMPLE or share is None:
        return f"insufficient data (n={n})"
    return f"{count}/{n} games ({share:.1%})"


def _print_stat(title, stat, top):
    print(f"\n{title} (n={stat['n']}; baseline n={stat['baseline_n']})")
    if not stat["rows"]:
        print(f"  no observations; game_ids: {_short_ids(stat['game_ids'])}")
        return
    for row in stat["rows"][:top]:
        team_value = _sample_text(row["count"], stat["n"], row["share"])
        baseline_value = _sample_text(
            row["baseline_count"], row["baseline_n"], row["baseline_share"]
        )
        print(
            f"  {row['name']}: {team_value}; baseline {baseline_value}; "
            f"game_ids: {_short_ids(row['game_ids'])}"
        )


def print_report(report, top=5):
    name = " / ".join(report["team_names"])
    print(f"Draft report: {name} [{report['team_id']}]")
    filters = report["filter"]
    print(
        f"Fearless filter: {filters['repeated_series_excluded']} repeat-pick series excluded; "
        f"{filters['fearless_series']} Fearless series remain from {filters['played_series']} played."
    )
    if report["strict_fearless"]:
        retained = filters["fearless_series"] - filters["strict_one_game_series_excluded"]
        print(
            f"Strict mode excluded {filters['strict_one_game_series_excluded']} one-game series; "
            f"{retained} series retained."
        )
    else:
        print(
            f"One-game series included: {filters['one_game_series']} (no later game can verify reuse)."
        )
    print(f"Team games in report: {report['games']}")

    for scope_name, stats in report["scopes"].items():
        heading = {"overall": "All sides", "blue": "Blue side", "red": "Red side"}[scope_name]
        print(f"\n=== {heading}: {stats['games']} team-games ===")
        _print_stat("Most picked champions | overall", stats["picked"]["overall"], top)
        for game_number, stat in stats["picked"]["by_game"].items():
            _print_stat(f"Most picked champions | game {game_number}", stat, top)
        _print_stat("Most banned champions | overall", stats["banned"]["overall"], top)
        for game_number, stat in stats["banned"]["by_game"].items():
            _print_stat(f"Most banned champions | game {game_number}", stat, top)
        _print_stat("First-pick champions", stats["first_pick_champions"], top)
        _print_stat("First-pick champion roles", stats["first_pick_roles"], top)
        _print_stat("Bans | first 3", stats["bans_by_phase"]["first_3"], top)
        _print_stat("Bans | last 2", stats["bans_by_phase"]["last_2"], top)


def _parse_game_date(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except (AttributeError, TypeError, ValueError):
        try:
            return date.fromisoformat(value[:10])
        except (AttributeError, TypeError, ValueError):
            return None


def _game_dates(conn, data_dir):
    data_dir = Path(data_dir).expanduser()
    dates = {}
    meta_path = data_dir / "meta" / "games.csv"
    if meta_path.exists():
        with open(meta_path, newline="", encoding="utf-8") as source:
            for row in csv.DictReader(source):
                game_date = _parse_game_date(row.get("started_at"))
                if row.get("game_id") and game_date:
                    dates[row["game_id"]] = game_date

    series_dates = {}
    series_path = data_dir / "lol" / "series.csv"
    if series_path.exists():
        with open(series_path, newline="", encoding="utf-8") as source:
            for row in csv.DictReader(source):
                scheduled = _parse_game_date(row.get("scheduled_start"))
                if row.get("series_id") and scheduled:
                    series_dates[str(row["series_id"])] = scheduled

    played_games = conn.execute(
        "SELECT game_id, series_id FROM games WHERE winner_team_id IS NOT NULL"
    ).fetchall()
    for game_id, series_id in played_games:
        if game_id not in dates and str(series_id) in series_dates:
            dates[game_id] = series_dates[str(series_id)]

    missing = [game_id for game_id, _ in played_games if game_id not in dates]
    if missing:
        expected = f"{meta_path} or {series_path}"
        raise ValueError(f"missing dates for {len(missing)} played games; expected started_at in {expected}")
    return dates


def _subtract_months(value, months):
    month_index = value.year * 12 + value.month - 1 - months
    year, month = divmod(month_index, 12)
    month += 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _roster_roles_by_player(conn):
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'roster_roles'"
    ).fetchone()
    if not exists:
        raise ValueError("roster_roles is missing; run python -m scuttle_scout.roles first")
    roles = dict(conn.execute("SELECT player_id, role FROM roster_roles WHERE role IS NOT NULL"))
    if not roles:
        raise ValueError("roster_roles has no role data; run python -m scuttle_scout.roles first")
    return roles


def team_champion_pools(conn, team_query, data_dir=DEFAULT_DATA, strict_fearless=False):
    team_id, team_names = _team_matches(conn, team_query)
    roles = _roster_roles_by_player(conn)
    fearless, repeated, series_games = fearless_series(conn)
    included_series = set(fearless)
    one_game_series = {series_id for series_id, games in series_games.items() if len(games) == 1}
    strict_excluded = set()
    if strict_fearless:
        strict_excluded = included_series & one_game_series
        included_series -= strict_excluded

    game_dates = _game_dates(conn, data_dir)
    latest_date = max(game_dates.values()) if game_dates else None
    window_start = _subtract_months(latest_date, COMFORT_MONTHS) if latest_date else None

    appearances = conn.execute(
        """
        SELECT gp.player_id, gp.name, gp.team_id, gt.name, g.game_id, g.series_id
        FROM games AS g
        JOIN game_players AS gp ON gp.game_id = g.game_id
        LEFT JOIN game_teams AS gt ON gt.game_id = gp.game_id AND gt.team_id = gp.team_id
        WHERE g.winner_team_id IS NOT NULL
        """
    ).fetchall()
    latest_appearance = {}
    team_players = set()
    team_game_counts = defaultdict(int)
    team_game_ids = set()
    for player_id, name, appearance_team_id, appearance_team_name, game_id, series_id in appearances:
        if player_id is None or game_id not in game_dates:
            continue
        if appearance_team_id == team_id:
            team_players.add(player_id)
            if str(series_id) in included_series:
                team_game_counts[player_id] += 1
                team_game_ids.add(game_id)
        key = (game_dates[game_id], str(game_id))
        current = latest_appearance.get(player_id)
        if current is None or key > current["key"]:
            latest_appearance[player_id] = {
                "key": key,
                "name": (name or "").strip(),
                "team_id": appearance_team_id,
                "team_name": appearance_team_name,
                "game_id": game_id,
            }

    current_players = {
        player_id for player_id in team_players
        if latest_appearance.get(player_id, {}).get("team_id") == team_id
    }
    former_players = []
    for player_id in team_players - current_players:
        latest = latest_appearance[player_id]
        former_players.append({
            "player_id": player_id,
            "name": latest["name"] or str(player_id),
            "role": roles.get(player_id) or "unknown",
            "last_team": latest["team_name"] or str(latest["team_id"] or "unknown"),
            "last_played": game_dates[latest["game_id"]].isoformat(),
            "game_ids": [latest["game_id"]],
        })

    player_rows = conn.execute(
        """
        SELECT gp.player_id, gp.name, gp.champion, gp.team_id, g.game_id, gt.won, g.series_id
        FROM games AS g
        JOIN game_players AS gp ON gp.game_id = g.game_id
        LEFT JOIN game_teams AS gt ON gt.game_id = gp.game_id AND gt.team_id = gp.team_id
        WHERE g.winner_team_id IS NOT NULL AND gp.champion IS NOT NULL
        """
    ).fetchall()
    games_by_player_champion = defaultdict(list)
    games_by_champion = defaultdict(list)
    team_champion_games = defaultdict(set)
    baseline_team_champion_games = defaultdict(set)
    for player_id, name, champion, appearance_team_id, game_id, won, series_id in player_rows:
        if game_id not in game_dates:
            continue
        if str(series_id) not in included_series:
            continue
        game_date = game_dates[game_id]
        row = (player_id, game_id, bool(won), game_date)
        games_by_champion[champion].append(row)
        if player_id in current_players:
            games_by_player_champion[(player_id, champion)].append((game_id, bool(won), game_date))
        occurrence = (game_id, appearance_team_id)
        if appearance_team_id == team_id:
            team_champion_games[champion].add(occurrence)
        else:
            baseline_team_champion_games[champion].add(occurrence)

    # Build player champion rows with an other-player baseline for win rate.
    player_records = []
    for player_id in current_players:
        latest = latest_appearance[player_id]
        champions = []
        for (pool_player_id, champion), games in games_by_player_champion.items():
            if pool_player_id != player_id:
                continue
            game_ids = sorted({game_id for game_id, _, _ in games})
            wins = sum(1 for _, won, _ in games if won)
            recent = [
                (game_id, won, game_date)
                for game_id, won, game_date in games
                if window_start is not None and window_start <= game_date <= latest_date
            ]
            baseline_games = [
                row for row in games_by_champion[champion] if row[0] != player_id
            ]
            baseline_game_ids = sorted({game_id for _, game_id, _, _ in baseline_games})
            baseline_wins = sum(1 for _, _, won, _ in baseline_games if won)
            n = len(games)
            baseline_n = len(baseline_games)
            champions.append({
                "champion": champion,
                "games": n,
                "n": n,
                "wins": wins,
                "win_rate": wins / n if n >= MIN_SAMPLE else None,
                "insufficient": n < MIN_SAMPLE,
                "game_ids": game_ids,
                "last_played": max(game_date for _, _, game_date in games).isoformat(),
                "recent_games": len(recent),
                "recent_game_ids": sorted({game_id for game_id, _, _ in recent}),
                "comfort": len(recent) >= COMFORT_GAMES,
                "comfort_baseline": {
                    "minimum_games": COMFORT_GAMES,
                    "window_start": window_start.isoformat() if window_start else None,
                    "window_end": latest_date.isoformat() if latest_date else None,
                },
                "baseline": {
                    "n": baseline_n,
                    "wins": baseline_wins,
                    "win_rate": baseline_wins / baseline_n if baseline_n >= MIN_SAMPLE else None,
                    "insufficient": baseline_n < MIN_SAMPLE,
                    "game_ids": baseline_game_ids,
                },
            })
        champions.sort(key=lambda row: (-row["games"], row["champion"].lower()))
        player_records.append({
            "player_id": player_id,
            "name": latest["name"] or str(player_id),
            "role": roles.get(player_id) or "unknown",
            "team_games": team_game_counts[player_id],
            "pool_games": sum(champion["games"] for champion in champions),
            "champions": champions,
        })

    role_order = {"top": 0, "jungle": 1, "mid": 2, "bot": 3, "support": 4}
    player_records.sort(key=lambda player: (
        role_order.get(player["role"], 5), player["name"].lower(), str(player["player_id"])
    ))

    team_champions = []
    for champion, occurrences in team_champion_games.items():
        baseline = baseline_team_champion_games[champion]
        game_ids = sorted({game_id for game_id, _ in occurrences})
        baseline_game_ids = sorted({game_id for game_id, _ in baseline})
        team_champions.append({
            "champion": champion,
            "games": len(occurrences),
            "n": len(occurrences),
            "game_ids": game_ids,
            "baseline": {"n": len(baseline), "game_ids": baseline_game_ids},
        })
    team_champions.sort(key=lambda row: (-row["games"], row["champion"].lower()))

    return {
        "team_query": team_query,
        "team_id": team_id,
        "team_names": team_names,
        "strict_fearless": strict_fearless,
        "filter": {
            "played_series": len(series_games),
            "fearless_series": len(fearless),
            "repeated_series_excluded": len(repeated),
            "one_game_series": len(one_game_series),
            "strict_one_game_series_excluded": len(strict_excluded),
        },
        "window": {
            "start": window_start.isoformat() if window_start else None,
            "end": latest_date.isoformat() if latest_date else None,
        },
        "games": len(team_game_ids),
        "players": player_records,
        "former_players": sorted(former_players, key=lambda player: player["name"].lower()),
        "team_champions": team_champions,
    }


def _normalize_champion(champion):
    return re.sub(r"[^a-z0-9]", "", champion.lower())


def pool_after_burns(team, burned_champions, conn=None, data_dir=DEFAULT_DATA, strict_fearless=False):
    owns_connection = False
    if isinstance(team, dict) and "players" in team:
        pools = team
    else:
        if conn is None:
            conn = _connect(DEFAULT_DB)
            owns_connection = True
        try:
            pools = team_champion_pools(conn, team, data_dir, strict_fearless)
        finally:
            if owns_connection:
                conn.close()

    burned = {str(champion) for champion in burned_champions if champion is not None}
    normalized_burns = {_normalize_champion(champion) for champion in burned}
    known_champions = {
        champion["champion"]
        for player in pools["players"]
        for champion in player["champions"]
    }
    known_by_normalized = {_normalize_champion(champion): champion for champion in known_champions}
    matched_burns = {known_by_normalized[key] for key in normalized_burns if key in known_by_normalized}
    unmatched_burns = sorted(
        (champion for champion in burned if _normalize_champion(champion) not in known_by_normalized),
        key=str.lower,
    )
    result = {
        "team_id": pools["team_id"],
        "team_names": pools["team_names"],
        "burned_champions": sorted(matched_burns, key=str.lower),
        "unmatched_burns": unmatched_burns,
        "players": [],
    }
    for player in pools["players"]:
        comfort = [champion for champion in player["champions"] if champion["comfort"]]
        result["players"].append({
            "player_id": player["player_id"],
            "name": player["name"],
            "role": player["role"],
            "remaining_comfort_picks": [
                champion for champion in comfort if champion["champion"] not in matched_burns
            ],
            "burned_comfort_picks": [
                champion for champion in comfort if champion["champion"] in matched_burns
            ],
            "comfort_game_ids": sorted({
                game_id for champion in comfort for game_id in champion["recent_game_ids"]
            }),
        })
    return result


def print_pools(report, after_burns, top=None):
    name = " / ".join(report["team_names"])
    filters = report["filter"]
    print(f"Champion pools: {name} [{report['team_id']}]")
    print(
        f"Fearless filter: {filters['repeated_series_excluded']} repeat-pick series excluded; "
        f"{filters['fearless_series']} Fearless series remain from {filters['played_series']} played."
    )
    print(f"Comfort window: {report['window']['start']} to {report['window']['end']} inclusive")
    print(f"Team played games: {report['games']}")
    if report["strict_fearless"]:
        print(f"Strict Fearless excluded {filters['strict_one_game_series_excluded']} one-game series.")
    else:
        print(f"One-game series included: {filters['one_game_series']}.")

    for player in report["players"]:
        print(
            f"\n{player['name']} ({player['role']}): {player['team_games']} games for this team, "
            f"{player['pool_games']} champion-games in full pool"
        )
        champions = player["champions"] if top is None else player["champions"][:top]
        if not champions:
            print("  no champion games in the Fearless sample")
            continue
        for champion in champions:
            if champion["win_rate"] is None:
                win_rate = f"insufficient data (n={champion['n']})"
            else:
                win_rate = f"{champion['win_rate']:.1%} ({champion['wins']}/{champion['n']})"
            baseline = champion["baseline"]
            if baseline["win_rate"] is None:
                baseline_rate = f"insufficient data (n={baseline['n']})"
            else:
                baseline_rate = f"{baseline['win_rate']:.1%} ({baseline['wins']}/{baseline['n']})"
            print(
                f"  {champion['champion']}: games={champion['games']}; win rate={win_rate}; "
                f"other-player baseline={baseline_rate}; last={champion['last_played']}; "
                f"recent={champion['recent_games']} games, comfort={'yes' if champion['comfort'] else 'no'} "
                f"(threshold {champion['comfort_baseline']['minimum_games']}); "
                f"game_ids: {_short_ids(champion['game_ids'])}; "
                f"comfort game_ids: {_short_ids(champion['recent_game_ids'])}; "
                f"baseline game_ids: {_short_ids(baseline['game_ids'])}"
            )
    if report["former_players"]:
        print("\nFormer players (most recent game was for another team):")
        for player in report["former_players"]:
            print(
                f"  {player['name']} ({player['role']}), now {player['last_team']}; "
                f"last played {player['last_played']}; game_ids: {_short_ids(player['game_ids'])}"
            )

    print("\n=== Example Bo5: burn the team's 3 most-played champions ===")
    top_three = report["team_champions"][:3]
    if not top_three:
        print("  no team champion picks available for the example")
    for champion in top_three:
        baseline = champion["baseline"]
        print(
            f"  burned {champion['champion']}: n={champion['n']}; "
            f"other-team baseline n={baseline['n']}; game_ids: {_short_ids(champion['game_ids'])}; "
            f"baseline game_ids: {_short_ids(baseline['game_ids'])}"
        )
    for player in after_burns["players"]:
        remaining = player["remaining_comfort_picks"]
        burned_comfort = player["burned_comfort_picks"]
        print(f"  {player['name']} ({player['role']}):")
        print("    remaining comfort picks: " + (
            ", ".join(f"{champion['champion']} ({champion['recent_games']}; {_short_ids(champion['recent_game_ids'])})"
                       for champion in remaining) or "none"
        ))
        print("    comfort picks burned: " + (
            ", ".join(f"{champion['champion']} ({champion['recent_games']}; {_short_ids(champion['recent_game_ids'])})"
                       for champion in burned_comfort) or "none"
        ))
    if after_burns["unmatched_burns"]:
        print("  burned champions not found in player pools: " + ", ".join(after_burns["unmatched_burns"]))


def _connect(db_path):
    db_path = Path(db_path).expanduser().resolve()
    return sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--team", required=True, help="team name contains")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA, help="GRID metadata directory, used by --pools")
    parser.add_argument("--top", type=int, help="rows per stat table (all champions by default with --pools)")
    parser.add_argument("--pools", action="store_true", help="print player champion pools and a Bo5 burn example")
    parser.add_argument(
        "--strict-fearless", action="store_true",
        help="exclude series with only one played game",
    )
    args = parser.parse_args()
    if args.top is not None and args.top < 1:
        parser.error("--top must be at least one")

    try:
        with _connect(args.db) as conn:
            if args.pools:
                report = team_champion_pools(conn, args.team, args.data, args.strict_fearless)
                burned = [row["champion"] for row in report["team_champions"][:3]]
                after_burns = pool_after_burns(report, burned)
            else:
                report = team_draft_stats(conn, args.team, args.strict_fearless)
    except (sqlite3.Error, ValueError) as error:
        sys.exit(str(error))
    if args.pools:
        print_pools(report, after_burns, args.top)
    else:
        print_report(report, args.top if args.top is not None else 5)


if __name__ == "__main__":
    main()