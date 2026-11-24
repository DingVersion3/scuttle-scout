import csv
import sqlite3

import pytest

from scuttle_scout.flags import load_scouting, series_flags
from scuttle_scout.simulator import GameState, SeriesState


def make_series(game_number=1, burned=(), current_bans=(), first_pick="Red Team"):
    champions = ["Nidalee", "Lee Sin", "Elise", "Aatrox", "Jinx", "Orianna"]
    series = SeriesState("Blue Team", "Red Team", 5, champions)
    if game_number > 1:
        series.games.append(GameState(
            number=1,
            blue_team="Blue Team",
            red_team="Red Team",
            first_pick_team="Blue Team",
            picks=[("Blue Team", champion) for champion in burned],
            winner="Blue Team",
        ))
    game = series.start_game("Blue Team", first_pick)
    game.bans.extend(("Blue Team", champion) for champion in current_bans)
    return series


def player_pool(champions, recent_game_ids=None, role="top", player_id="red-player"):
    recent_game_ids = recent_game_ids or [f"pool-{i}" for i in range(6)]
    return {
        "team_id": "red-id",
        "team_names": ["Red Team"],
        "players": [{
            "player_id": player_id,
            "name": "Red Player",
            "role": role,
            "champions": [
                {
                    "champion": champion,
                    "comfort": comfort,
                    "recent_game_ids": list(recent_game_ids),
                }
                for champion, comfort in champions
            ],
        }],
    }


def make_scouting(pools=None, **extra):
    return {
        "team_id": "red-id",
        "pools": pools or player_pool([]),
        "first_picks": [],
        "results": [],
        "openings": [],
        **extra,
    }


def test_low_comfort_pool_flags_zero_or_one_but_not_two_remaining():
    pools = player_pool([("Nidalee", True), ("Lee Sin", True), ("Elise", True)])
    one_left = series_flags(
        make_series(game_number=2, burned=("Nidalee", "Lee Sin")), "Red Team",
        scouting=make_scouting(pools),
    )
    zero_left = series_flags(
        make_series(game_number=2, burned=("Nidalee", "Lee Sin", "Elise")), "Red Team",
        scouting=make_scouting(pools),
    )
    two_left = series_flags(
        make_series(game_number=2, burned=("Nidalee",)), "Red Team",
        scouting=make_scouting(pools),
    )
    assert [row["kind"] for row in one_left] == ["low_comfort_pool"]
    assert [row["kind"] for row in zero_left] == ["low_comfort_pool"]
    assert not two_left
    assert "1 remaining comfort pick" in one_left[0]["text"]
    assert one_left[0]["baseline"]["value"] == 3


def test_pool_flags_include_current_bans_but_not_bans_from_finished_games():
    pools = player_pool([("Nidalee", True), ("Lee Sin", True)])
    current = make_series(game_number=2, current_bans=("Nidalee",))
    current_flag = series_flags(current, "Red Team", scouting=make_scouting(pools))
    assert current_flag[0]["kind"] == "low_comfort_pool"
    assert "Current-game comfort bans: Nidalee" in current_flag[0]["text"]

    finished = make_series(game_number=2)
    finished.games[0].bans.append(("Blue Team", "Nidalee"))
    finished_flag = series_flags(finished, "Red Team", scouting=make_scouting(pools))
    assert not finished_flag


def test_pool_flag_suppressed_when_player_has_fewer_than_five_evidence_games():
    pools = player_pool([("Nidalee", True), ("Lee Sin", True)], ["g1", "g2", "g3", "g4"])
    findings = series_flags(
        make_series(game_number=2, burned=("Nidalee",)), "Red Team",
        scouting=make_scouting(pools),
    )
    assert not findings


def first_pick_rows(game_number, team_id, champions):
    return [
        {"game_id": f"{team_id}-{game_number}-{i}", "team_id": team_id,
         "game_number": game_number, "champion": champion}
        for i, champion in enumerate(champions)
    ]


def test_first_pick_flag_uses_current_game_number_and_exact_fifty_percent():
    rows = first_pick_rows(1, "red-id", ["Aatrox"] * 5 + ["Jinx"] * 4 + ["Orianna"])
    rows += first_pick_rows(1, "other-id", ["Aatrox"] * 2 + ["Jinx"] * 8)
    rows += first_pick_rows(2, "red-id", ["Orianna"] * 5 + ["Aatrox"] * 5)
    scouting = make_scouting(first_picks=rows)

    game_one = series_flags(make_series(), "Red Team", scouting=scouting)
    assert [(row["kind"], "Aatrox" in row["text"]) for row in game_one] == [("first_pick", True)]
    assert game_one[0]["n"] == 10
    assert game_one[0]["baseline"]["value"] == 0.2

    game_two = series_flags(make_series(game_number=2), "Red Team", scouting=scouting)
    assert len(game_two) == 2
    assert any("Orianna" in row["text"] for row in game_two)
    assert all("game-2" in row["text"] for row in game_two)


def test_first_pick_flag_suppresses_small_samples_burned_champions_and_locked_picks():
    rows = first_pick_rows(1, "red-id", ["Aatrox"] * 2 + ["Jinx"] * 2)
    assert not series_flags(make_series(), "Red Team", scouting=make_scouting(first_picks=rows))

    rows = first_pick_rows(2, "red-id", ["Aatrox"] * 3 + ["Jinx"] * 2)
    burned = series_flags(
        make_series(game_number=2, burned=("Aatrox",)), "Red Team",
        scouting=make_scouting(first_picks=rows),
    )
    assert not burned

    locked = make_series()
    locked.current_game.actions.extend([("ban", "Blue Team", "Nidalee")] * 6)
    locked.current_game.actions.append(("pick", "Red Team", "Aatrox"))
    locked.current_game.picks.append(("Red Team", "Aatrox"))
    rows = first_pick_rows(1, "red-id", ["Aatrox"] * 5 + ["Jinx"] * 5)
    assert not series_flags(locked, "Red Team", scouting=make_scouting(first_picks=rows))


def result_rows(side, wins, losses, prefix):
    return [
        {"game_id": f"{prefix}-w{i}", "side": side, "won": True}
        for i in range(wins)
    ] + [
        {"game_id": f"{prefix}-l{i}", "side": side, "won": False}
        for i in range(losses)
    ]


def test_side_flags_at_fifteen_points_and_eight_games_minimum():
    exact = result_rows("blue", 65, 35, "blue") + result_rows("red", 35, 65, "red")
    findings = series_flags(make_series(), "Red Team", scouting=make_scouting(results=exact))
    blue = next(row for row in findings if "on blue" in row["text"])
    assert blue["n"] == 100
    assert blue["baseline"]["value"] == 0.5

    fourteen = result_rows("blue", 64, 36, "blue") + result_rows("red", 36, 64, "red")
    assert not series_flags(make_series(), "Red Team", scouting=make_scouting(results=fourteen))

    seven = result_rows("blue", 7, 0, "blue") + result_rows("red", 0, 7, "red")
    assert not series_flags(make_series(), "Red Team", scouting=make_scouting(results=seven))


def test_jungle_flags_use_remaining_comfort_picks_and_metric_denominators():
    pools = player_pool(
        [("Nidalee", True), ("Lee Sin", True), ("Elise", True)], role="jungle", player_id="j1"
    )
    target = [
        {"game_id": f"j1-{i}", "player_id": "j1", "champion": "Nidalee", "role": "jungle",
         "start_side": side, "first_scuttle": scuttle, "camps_before_scuttle": camps}
        for i, (side, scuttle, camps) in enumerate([
            ("top", "top", 6), ("top", "bot", 6), ("top", "top", 5),
            ("top", "bot", 6), ("bot", "top", 4), ("bot", None, 6),
            (None, "bot", None),
        ])
    ]
    baseline = [
        {"game_id": f"j2-{i}", "player_id": "j2", "champion": "Nidalee", "role": "jungle",
         "start_side": side, "first_scuttle": scuttle, "camps_before_scuttle": camps}
        for i, (side, scuttle, camps) in enumerate([
            ("top", "top", 6), ("top", "top", 5), ("top", "bot", 6),
            ("bot", "top", 4), ("bot", "top", 6), ("bot", "bot", 5),
        ])
    ]
    ignored = [
        {**row, "champion": "Lee Sin"} for row in target
    ]
    findings = series_flags(
        make_series(game_number=2, burned=("Lee Sin",)), "Red Team",
        scouting=make_scouting(pools, openings=target + baseline + ignored),
    )
    jungle = [row for row in findings if row["kind"] == "jungle_opening"]
    assert len(jungle) == 3
    by_label = {label: next(row for row in jungle if label in row["text"]) for label in (
        "start-side tendency", "first-scuttle tendency", "full-clear rate"
    )}
    assert by_label["start-side tendency"]["n"] == 6
    assert by_label["start-side tendency"]["baseline"]["value"] == 0.5
    assert by_label["first-scuttle tendency"]["n"] == 6
    assert by_label["full-clear rate"]["n"] == 6
    assert all("Lee Sin" not in row["text"] for row in jungle)

    short = [row for row in target[:4]]
    suppressed = series_flags(
        make_series(), "Red Team",
        scouting=make_scouting(pools, openings=short),
    )
    assert not any(row["kind"] == "jungle_opening" for row in suppressed)


def test_findings_have_required_evidence_and_unknown_opponent_is_rejected():
    pools = player_pool([("Nidalee", True), ("Lee Sin", True)])
    findings = series_flags(
        make_series(game_number=2, burned=("Nidalee",)), "Red Team",
        scouting=make_scouting(pools),
    )
    for finding in findings:
        assert {"text", "n", "baseline", "game_ids"} <= finding.keys()
        assert finding["n"] >= 5
        assert finding["game_ids"]
        assert "n" in finding["baseline"]
        assert "game_ids" in finding["baseline"]
    with pytest.raises(ValueError, match="one of the series teams"):
        series_flags(make_series(), "Other Team", scouting=make_scouting())


def test_load_scouting_reads_fearless_team_draft_results_roles_and_openings(tmp_path):
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE games (game_id TEXT, series_id TEXT, game_order INTEGER, winner_team_id TEXT);
        CREATE TABLE game_teams (game_id TEXT, team_id TEXT, name TEXT, side TEXT, won INTEGER);
        CREATE TABLE game_players (game_id TEXT, player_id TEXT, team_id TEXT, name TEXT, champion TEXT);
        CREATE TABLE draft (game_id TEXT, seq INTEGER, action TEXT, team_id TEXT, champion TEXT);
        CREATE TABLE roster_roles (player_id TEXT, name TEXT, role TEXT, games INTEGER, share REAL);
        CREATE TABLE jungle_openings (
            game_id TEXT, player_id TEXT, team_id TEXT, side TEXT, start_side TEXT,
            camps_before_scuttle INTEGER, first_scuttle TEXT, first_scuttle_t INTEGER
        );
    """)
    conn.execute("INSERT INTO roster_roles VALUES ('red-jg', 'Red Jungle', 'jungle', 5, 1.0)")
    for i in range(5):
        game_id = f"g{i}"
        series_id = f"s{i}"
        conn.execute("INSERT INTO games VALUES (?, ?, 1, 'red-id')", (game_id, series_id))
        conn.executemany("INSERT INTO game_teams VALUES (?, ?, ?, ?, ?)", [
            (game_id, "red-id", "Red Team", "blue", 1),
            (game_id, "blue-id", "Blue Team", "red", 0),
        ])
        conn.executemany("INSERT INTO game_players VALUES (?, ?, ?, ?, ?)", [
            (game_id, "red-jg", "red-id", "Red Jungle", "Nidalee"),
            (game_id, "blue-jg", "blue-id", "Blue Jungle", "Aatrox"),
        ])
        conn.execute("INSERT INTO draft VALUES (?, 1, 'pick', 'red-id', 'Nidalee')", (game_id,))
        conn.execute(
            "INSERT INTO jungle_openings VALUES (?, 'red-jg', 'red-id', 'blue', 'top', 6, 'top', 120)",
            (game_id,),
        )

    meta = tmp_path / "meta"
    meta.mkdir()
    with (meta / "games.csv").open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=("game_id", "started_at"))
        writer.writeheader()
        for i in range(5):
            writer.writerow({"game_id": f"g{i}", "started_at": f"2026-10-{i + 1:02d}T12:00:00Z"})

    scouting = load_scouting(conn, "Red Team", data_dir=tmp_path)
    assert scouting["team_id"] == "red-id"
    assert scouting["pools"]["players"][0]["role"] == "jungle"
    assert len(scouting["first_picks"]) == 5
    assert all(row["champion"] == "Nidalee" for row in scouting["first_picks"])
    assert len(scouting["results"]) == 5
    assert len(scouting["openings"]) == 5
    assert all(row["role"] == "jungle" for row in scouting["openings"])
    conn.close()