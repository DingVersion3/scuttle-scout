# Generate opponent scouting flags for the current game in a Fearless series.

from collections import Counter

from scuttle_scout.drafts import (
    DEFAULT_DATA,
    DEFAULT_DB,
    MIN_SAMPLE,
    _game_numbers,
    _team_game_rows,
    _team_matches,
    _normalize_champion,
    fearless_series,
    pool_after_burns,
    team_champion_pools,
)


LOW_POOL = 1
FIRST_PICK_SHARE = 0.5
SIDE_DIFF = 15
SIDE_MIN_GAMES = 8
FULL_CLEAR_CAMPS = 6


def load_scouting(conn, team_query, data_dir=DEFAULT_DATA, strict_fearless=False):
    team_id, _ = _team_matches(conn, team_query)
    pools = team_champion_pools(conn, team_query, data_dir, strict_fearless)
    fearless, _, series_games = fearless_series(conn)
    included_series = set(fearless)
    if strict_fearless:
        included_series -= {
            series_id for series_id, games in series_games.items() if len(games) == 1
        }

    game_numbers = _game_numbers(conn)
    team_rows = _team_game_rows(conn, included_series, game_numbers, {})
    first_picks = [
        {
            "game_id": row["game_id"],
            "team_id": row["team_id"],
            "game_number": row["game_number"],
            "champion": row["first_pick_champion"],
        }
        for row in team_rows
        if row["first_pick_champion"]
    ]

    results = [
        {"game_id": game_id, "side": (side or "").lower(), "won": bool(won)}
        for series_id, game_id, side, won in conn.execute(
            """
            SELECT g.series_id, g.game_id, gt.side, g.winner_team_id = gt.team_id
            FROM games AS g
            JOIN game_teams AS gt ON gt.game_id = g.game_id
            WHERE g.winner_team_id IS NOT NULL AND gt.team_id = ?
            ORDER BY g.game_id
            """,
            (team_id,),
        )
        if game_id in game_numbers and str(series_id) in included_series
    ]

    openings_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'jungle_openings'"
    ).fetchone()
    if not openings_table:
        raise ValueError("jungle_openings is missing; run python -m scuttle_scout.paths first")
    openings = [
        {
            "game_id": game_id,
            "player_id": player_id,
            "team_id": opening_team_id,
            "champion": champion,
            "role": role,
            "start_side": start_side,
            "first_scuttle": first_scuttle,
            "camps_before_scuttle": camps,
        }
        for (
            series_id,
            game_id,
            player_id,
            opening_team_id,
            champion,
            role,
            start_side,
            first_scuttle,
            camps,
        ) in conn.execute(
            """
            SELECT g.series_id, jo.game_id, jo.player_id, jo.team_id, gp.champion, rr.role,
                   jo.start_side, jo.first_scuttle, jo.camps_before_scuttle
            FROM jungle_openings AS jo
            JOIN games AS g ON g.game_id = jo.game_id
            JOIN game_players AS gp
              ON gp.game_id = jo.game_id AND gp.player_id = jo.player_id
            JOIN roster_roles AS rr ON rr.player_id = jo.player_id
            WHERE g.winner_team_id IS NOT NULL AND gp.champion IS NOT NULL
            ORDER BY jo.game_id, jo.player_id
            """
        )
        if game_id in game_numbers and str(series_id) in included_series
    ]

    return {
        "team_id": team_id,
        "pools": pools,
        "first_picks": first_picks,
        "results": results,
        "openings": openings,
    }


def _sample(game_ids, value, n=None):
    ids = sorted(set(game_ids))
    count = len(ids) if n is None else n
    return {
        "value": value if count >= MIN_SAMPLE else None,
        "n": count,
        "game_ids": ids,
        "insufficient": count < MIN_SAMPLE,
    }


def _finding(kind, text, n, baseline, game_ids):
    return {
        "kind": kind,
        "text": text,
        "n": n,
        "baseline": baseline,
        "game_ids": sorted(set(game_ids)),
    }


def _pool_flags(series, opponent, scouting, unavailable):
    pools = scouting["pools"]
    after_burns = pool_after_burns(pools, unavailable)
    players = {player["player_id"]: player for player in pools["players"]}
    findings = []
    for remaining in after_burns["players"]:
        source = players[remaining["player_id"]]
        recent_ids = sorted({
            game_id
            for champion in source["champions"]
            for game_id in champion.get("recent_game_ids", [])
        })
        n = len(recent_ids)
        if n < MIN_SAMPLE:
            continue
        remaining_count = len(remaining["remaining_comfort_picks"])
        if remaining_count > LOW_POOL:
            continue
        before_count = sum(1 for champion in source["champions"] if champion["comfort"])
        baseline = _sample(recent_ids, before_count)
        burned_keys = {
            _normalize_champion(champion) for champion in series.burned_champions()
        }
        banned_keys = {
            _normalize_champion(champion)
            for _, champion in series.current_game.bans
        } if series.current_game is not None and series.current_game.winner is None else set()
        burned = [
            champion["champion"] for champion in source["champions"]
            if champion["comfort"] and _normalize_champion(champion["champion"]) in burned_keys
        ]
        bans = ", ".join(
            champion["champion"]
            for champion in source["champions"]
            if champion["comfort"] and _normalize_champion(champion["champion"]) in banned_keys
        )
        text = (
            f"{remaining['name']} ({remaining['role']}) has {remaining_count} remaining comfort "
            f"pick{'s' if remaining_count != 1 else ''} after {len(burned)} Fearless burn(s); "
            f"{before_count} before this series."
        )
        if burned:
            text += f" Burned comfort picks: {', '.join(burned)}."
        if bans:
            text += f" Current-game comfort bans: {bans}."
        findings.append(_finding(
            "low_comfort_pool", text, n, baseline, recent_ids
        ))
    return findings


def _first_pick_flags(series, opponent, scouting, team_id, game_number, unavailable):
    current = series.current_game
    if current is not None and current.winner is None:
        if current.first_pick_team != opponent:
            return []
        if any(action[0] == "pick" for action in current.actions):
            return []

    target_rows = [
        row for row in scouting["first_picks"]
        if row["team_id"] == team_id and row["game_number"] == game_number and row["champion"]
    ]
    n = len(target_rows)
    if n < MIN_SAMPLE:
        return []
    counts = Counter(row["champion"] for row in target_rows)
    baseline_rows = [
        row for row in scouting["first_picks"]
        if row["team_id"] != team_id and row["game_number"] == game_number and row["champion"]
    ]
    baseline_n = len(baseline_rows)
    baseline_ids = [row["game_id"] for row in baseline_rows]
    burned = {_normalize_champion(champion) for champion in unavailable}
    findings = []
    for champion, count in sorted(counts.items(), key=lambda item: (-item[1], item[0].lower())):
        if count * 2 < n or _normalize_champion(champion) in burned:
            continue
        share = count / n
        base_count = sum(row["champion"] == champion for row in baseline_rows)
        baseline = _sample(baseline_ids, base_count / baseline_n if baseline_n else None, baseline_n)
        baseline_text = (
            f"{baseline['value']:.0%} among other teams" if baseline["value"] is not None
            else "insufficient baseline data"
        )
        findings.append(_finding(
            "first_pick",
            f"{opponent} first-picks {champion} in {share:.0%} of its game-{game_number} "
            f"first picks ({count}/{n}); baseline: {baseline_text}.",
            n,
            baseline,
            [row["game_id"] for row in target_rows],
        ))
    return findings


def _side_flags(opponent, scouting):
    results = [row for row in scouting["results"] if row["side"] in ("blue", "red")]
    if not results:
        return []
    overall_wins = sum(bool(row["won"]) for row in results)
    overall_n = len(results)
    overall_rate = overall_wins / overall_n
    overall_ids = [row["game_id"] for row in results]
    findings = []
    for side in ("blue", "red"):
        rows = [row for row in results if row["side"] == side]
        n = len(rows)
        if n < SIDE_MIN_GAMES:
            continue
        wins = sum(bool(row["won"]) for row in rows)
        if abs(wins * overall_n - overall_wins * n) * 100 < SIDE_DIFF * n * overall_n:
            continue
        rate = wins / n
        baseline = _sample(overall_ids, overall_rate, overall_n)
        findings.append(_finding(
            "side_win_rate",
            f"{opponent} wins {rate:.0%} on {side} ({wins}/{n}) versus {overall_rate:.0%} "
            f"overall ({overall_wins}/{overall_n}).",
            n,
            baseline,
            [row["game_id"] for row in rows],
        ))
    return findings


def _jungle_flags(opponent, scouting, pool_after):
    openings = [row for row in scouting["openings"] if row.get("role") == "jungle"]
    findings = []
    for player in pool_after["players"]:
        if player["role"] != "jungle":
            continue
        for comfort in player["remaining_comfort_picks"]:
            champion = comfort["champion"]
            champion_key = _normalize_champion(champion)
            player_rows = [
                row for row in openings
                if row["player_id"] == player["player_id"]
                and _normalize_champion(row["champion"]) == champion_key
            ]
            baseline_rows = [
                row for row in openings
                if row["player_id"] != player["player_id"]
                and _normalize_champion(row["champion"]) == champion_key
            ]
            metrics = (
                ("start_side", "start-side tendency", lambda value: value),
                ("first_scuttle", "first-scuttle tendency", lambda value: value),
                (
                    "camps_before_scuttle",
                    "full-clear rate",
                    lambda value: value >= FULL_CLEAR_CAMPS,
                ),
            )
            for field, label, normalize in metrics:
                rows = [row for row in player_rows if row.get(field) is not None]
                n = len(rows)
                if n < MIN_SAMPLE:
                    continue
                values = [normalize(row[field]) for row in rows]
                counts = Counter(values)
                value = sorted(counts, key=lambda item: (-counts[item], str(item)))[0]
                count = counts[value]
                rate = count / n
                comparison_rows = [
                    row for row in baseline_rows if row.get(field) is not None
                ]
                baseline_n = len(comparison_rows)
                baseline_count = sum(normalize(row[field]) == value for row in comparison_rows)
                baseline_rate = baseline_count / baseline_n if baseline_n else None
                baseline = _sample(
                    [row["game_id"] for row in comparison_rows], baseline_rate, baseline_n
                )
                if field == "camps_before_scuttle":
                    tendency = "full clear" if value else "not full clear"
                else:
                    tendency = str(value)
                baseline_text = (
                    f"{baseline['value']:.0%}" if baseline["value"] is not None
                    else "insufficient baseline data"
                )
                findings.append(_finding(
                    "jungle_opening",
                    f"{player['name']} on {champion}: {label} is {tendency} in {rate:.0%} "
                    f"of {n} observed openings; same-champion baseline: {baseline_text}.",
                    n,
                    baseline,
                    [row["game_id"] for row in rows],
                ))
    return findings


def series_flags(
    series,
    opponent,
    conn=None,
    scouting=None,
    data_dir=DEFAULT_DATA,
    strict_fearless=False,
):
    if opponent not in series.teams:
        raise ValueError(f"opponent must be one of the series teams: {series.teams!r}")
    if series.is_over:
        return []

    owns_connection = False
    if scouting is None:
        if conn is None:
            from scuttle_scout.drafts import _connect

            conn = _connect(DEFAULT_DB)
            owns_connection = True
        try:
            scouting = load_scouting(conn, opponent, data_dir, strict_fearless)
        finally:
            if owns_connection:
                conn.close()

    team_id = scouting.get("team_id", scouting["pools"].get("team_id"))
    current = series.current_game
    game_number = current.number if current is not None and current.winner is None else len(series.games) + 1
    unavailable = set(series.burned_champions())
    if current is not None and current.winner is None:
        unavailable.update(champion for _, champion in current.bans)

    pool_after = pool_after_burns(scouting["pools"], unavailable)
    findings = _pool_flags(series, opponent, scouting, unavailable)
    findings.extend(_first_pick_flags(
        series, opponent, scouting, team_id, game_number, unavailable
    ))
    findings.extend(_side_flags(opponent, scouting))
    findings.extend(_jungle_flags(opponent, scouting, pool_after))
    return [finding for finding in findings if finding["n"] >= MIN_SAMPLE]