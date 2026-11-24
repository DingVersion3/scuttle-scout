import csv
import sqlite3

import pytest
from fastapi.testclient import TestClient
from starlette.routing import Mount

from scuttle_scout import api


CHAMPIONS = [f"C{i:02}" for i in range(60)]


def make_database(path):
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE games (
            game_id TEXT, series_id TEXT, game_order INTEGER, winner_team_id TEXT
        );
        CREATE TABLE game_teams (
            game_id TEXT, team_id TEXT, name TEXT, side TEXT, won INTEGER
        );
        CREATE TABLE game_players (game_id TEXT, team_id TEXT, champion TEXT);
        CREATE TABLE draft (
            game_id TEXT, seq INTEGER, action TEXT, team_id TEXT, champion TEXT
        );
    """)
    fixtures = [
        ("g1", "s1", "loud-id", "LOUD", "blue-id", "Blue Team"),
        ("g2", "s2", "cloud9-id", "Cloud9 Kia", "blue-id", "Blue renamed"),
        ("g3", "s3", "kc-id", "Karmine Corp", "kcb-id", "Karmine Corp Blue"),
        ("g4", "s4", "blue-id", "Blue Team", "red-id", "Red Team"),
    ]
    for game_id, series_id, winner, blue_name, red_id, red_name in fixtures:
        conn.execute("INSERT INTO games VALUES (?, ?, 1, ?)", (game_id, series_id, winner))
        conn.executemany("INSERT INTO game_teams VALUES (?, ?, ?, ?, ?)", [
            (game_id, winner, blue_name, "blue", 1),
            (game_id, red_id, red_name, "red", 0),
        ])
    conn.executemany(
        "INSERT INTO game_players VALUES ('g1', 'loud-id', ?)",
        [(champion,) for champion in CHAMPIONS],
    )
    conn.commit()
    conn.close()


def make_metadata(data_dir):
    meta = data_dir / "meta"
    meta.mkdir(parents=True)
    with (meta / "games.csv").open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=("game_id", "started_at"))
        writer.writeheader()
        writer.writerow({"game_id": "g1", "started_at": "2026-10-01T12:00:00Z"})


@pytest.fixture
def api_client(tmp_path):
    db_path = tmp_path / "scout.db"
    data_dir = tmp_path / "grid-data"
    make_database(db_path)
    make_metadata(data_dir)
    app = api.create_app(db_path, data_dir)
    with TestClient(app) as client:
        yield client, app


def create_series(client, best_of=3):
    response = client.post("/series", json={
        "team_a": "Blue Team", "team_b": "Red Team", "best_of": best_of,
    })
    assert response.status_code == 201, response.text
    return response.json()["id"]


def post_action(client, series_id, action, team, **extra):
    return client.post(
        f"/series/{series_id}/action",
        json={"action": action, "team": team, **extra},
    )


def choose_side_and_first_pick(client, series_id, first="Red Team", side_team="Blue Team"):
    side = post_action(client, series_id, "side", side_team, side="blue")
    assert side.status_code == 200, side.text
    first_pick = post_action(client, series_id, "first-pick", first)
    assert first_pick.status_code == 200, first_pick.text
    return first_pick.json()["state"]


def test_teams_are_fearless_filtered_merged_and_contain_no_database_ids(api_client):
    client, _ = api_client
    response = client.get("/teams")
    assert response.status_code == 200
    teams = response.json()
    blue = next(team for team in teams if team["name"] == "Blue Team")
    assert blue["names"] == ["Blue renamed", "Blue Team"]
    assert blue["games"] == 3
    assert all("team_id" not in team for team in teams)
    assert all("game_ids" not in team for team in teams)


def test_report_is_trimmed_and_exact_names_resolve_over_substring_matches(api_client, monkeypatch):
    client, _ = api_client
    seen = []

    def fake_draft(conn, name):
        team_id, names = api.drafts._team_matches(conn, name)
        seen.append((team_id, name))
        return {
            "team_id": team_id,
            "team_names": names,
            "team_query": name,
            "games": 7,
            "scopes": {"overall": {"picked": {"overall": {
                "n": 8,
                "game_ids": ["team-game"],
                "baseline_game_ids": ["baseline-game"],
                "rows": [
                    {"name": f"champion-{i}", "game_ids": [f"g{i}"],
                     "baseline_game_ids": [f"b{i}"], "baseline": {"n": 9, "game_ids": ["raw"]}}
                    for i in range(3)
                ],
            }}}},
        }

    def fake_pools(conn, name, data_dir):
        team_id, names = api.drafts._team_matches(conn, name)
        return {
            "team_id": team_id,
            "team_names": names,
            "players": [{"player_id": "private-player-id", "champions": [{
                "champion": "Ahri", "game_ids": ["pool-game"],
                "baseline": {"n": 8, "game_ids": ["baseline-pool-game"]},
            }]}],
        }

    monkeypatch.setattr(api.drafts, "team_draft_stats", fake_draft)
    monkeypatch.setattr(api.drafts, "team_champion_pools", fake_pools)
    response = client.get("/teams/LOUD/report?top=1")
    assert response.status_code == 200, response.text
    report = response.json()
    assert report["team"]["name"] == "LOUD"
    assert report["team"]["games"] == 7
    assert seen[0][0] == "loud-id"
    stat = report["draft"]["scopes"]["overall"]["picked"]["overall"]
    assert len(stat["rows"]) == 1
    assert stat["game_ids"] == ["team-game"]
    assert "baseline_game_ids" not in stat
    assert stat["rows"][0]["game_ids"] == ["g0"]
    assert "baseline_game_ids" not in stat["rows"][0]
    assert "game_ids" not in stat["rows"][0]["baseline"]
    assert "player_id" not in report["pools"]["players"][0]
    assert report["pools"]["players"][0]["champions"][0]["game_ids"] == ["pool-game"]
    assert "game_ids" not in report["pools"]["players"][0]["champions"][0]["baseline"]

    assert client.get("/teams/Corp/report").status_code == 409
    assert client.get("/teams/no-such-team/report").status_code == 404
    assert client.get("/teams/LOUD/report?top=0").status_code == 422


def test_create_series_validates_teams_and_best_of(api_client):
    client, _ = api_client
    assert client.post("/series", json={
        "team_a": "missing", "team_b": "Red Team", "best_of": 3,
    }).status_code == 404
    assert client.post("/series", json={
        "team_a": "Corp", "team_b": "Red Team", "best_of": 3,
    }).status_code == 409
    assert client.post("/series", json={
        "team_a": "Blue Team", "team_b": "Blue renamed", "best_of": 3,
    }).status_code == 400
    assert client.post("/series", json={
        "team_a": "Blue Team", "team_b": "Red Team", "best_of": 4,
    }).status_code == 422


def test_side_and_first_pick_are_separate_and_can_be_set_in_either_order(api_client):
    client, _ = api_client
    first_id = create_series(client)
    state = choose_side_and_first_pick(client, first_id)
    assert state["games"][0]["blue_team"] == "Blue Team"
    assert state["games"][0]["first_pick_team"] == "Red Team"
    assert state["next_action"] == {"team": "Red Team", "action": "ban"}

    second_id = create_series(client)
    first_pick = post_action(client, second_id, "first_pick", "Blue Team")
    assert first_pick.status_code == 200
    assert first_pick.json()["state"]["pending"]["blue_team"] is None
    side = post_action(client, second_id, "side", "Red Team", side="red")
    assert side.status_code == 200
    game = side.json()["state"]["games"][0]
    assert game["blue_team"] == "Blue Team"
    assert game["first_pick_team"] == "Blue Team"


def test_draft_actions_fearless_carryover_game_bans_and_best_of_end(api_client):
    client, _ = api_client
    series_id = create_series(client)
    state = choose_side_and_first_pick(client, series_id)

    wrong_action = post_action(client, series_id, "pick", "Red Team", champion="C00")
    assert wrong_action.status_code == 409
    assert "expects a ban" in wrong_action.json()["detail"]
    wrong_team = post_action(client, series_id, "ban", "Blue Team", champion="C00")
    assert wrong_team.status_code == 409
    assert "Red Team's turn" in wrong_team.json()["detail"]
    assert post_action(client, series_id, "ban", "Red Team").status_code == 422
    assert post_action(client, series_id, "winner", "Blue Team").status_code == 409

    # Game 1: the first ban is C00; picks are C06-C11 and C16-C19.
    for index in range(20):
        next_team = state["next_action"]["team"]
        next_action = state["next_action"]["action"]
        champion = f"C{index:02}"
        response = post_action(client, series_id, next_action, next_team, champion=champion)
        assert response.status_code == 200, response.text
        state = response.json()["state"]

    winner = post_action(client, series_id, "winner", "Blue Team")
    assert winner.status_code == 200
    assert winner.json()["state"]["score"] == {"Blue Team": 1, "Red Team": 0}

    # Game 2 starts with the opposite side choice. A champion banned in game 1 remains pickable.
    post_action(client, series_id, "first-pick", "Red Team")
    started = post_action(client, series_id, "side", "Red Team", side="red")
    assert started.status_code == 200
    state = started.json()["state"]
    for index in range(6):
        next_team = state["next_action"]["team"]
        action = state["next_action"]["action"]
        response = post_action(client, series_id, action, next_team, champion=f"C{20 + index:02}")
        assert response.status_code == 200, response.text
        state = response.json()["state"]

    burned = post_action(client, series_id, "pick", "Red Team", champion="C06")
    assert burned.status_code == 409
    assert "burned for the series" in burned.json()["detail"]
    allowed = post_action(client, series_id, "pick", "Red Team", champion="C00")
    assert allowed.status_code == 200
    state = allowed.json()["state"]

    # Finish game 2 with legal unique choices, then verify the Bo3 limit.
    while state["next_action"] is not None:
        next_team = state["next_action"]["team"]
        action = state["next_action"]["action"]
        count = len(state["games"][-1]["actions"])
        champion = f"C{19 + count:02}"
        response = post_action(client, series_id, action, next_team, champion=champion)
        assert response.status_code == 200, response.text
        state = response.json()["state"]
    second_winner = post_action(client, series_id, "winner", "Blue Team")
    assert second_winner.status_code == 200
    assert second_winner.json()["state"]["is_over"] is True
    assert second_winner.json()["state"]["winner"] == "Blue Team"
    assert post_action(client, series_id, "pick", "Blue Team", champion="C40").status_code == 409


def test_series_state_flags_opponent_filter_and_unknown_ids(api_client, monkeypatch):
    client, _ = api_client
    series_id = create_series(client)
    monkeypatch.setattr(api, "load_scouting", lambda conn, name, data_dir: {"team": name})

    def fake_flags(series, opponent, scouting=None):
        assert scouting["team"] == opponent
        return [{
            "text": "sample finding", "n": 5,
            "baseline": {"n": 9, "value": 0.4}, "game_ids": ["game-1"],
        }]

    monkeypatch.setattr(api, "series_flags", fake_flags)
    all_teams = client.get(f"/series/{series_id}")
    assert all_teams.status_code == 200, all_teams.text
    assert set(all_teams.json()["flags"]) == {"Blue Team", "Red Team"}
    assert all_teams.json()["flags"]["Blue Team"][0]["n"] == 5
    filtered = client.get(f"/series/{series_id}", params={"opponent": "Blue Team"})
    assert filtered.status_code == 200
    assert set(filtered.json()["flags"]) == {"Blue Team"}
    assert client.get(f"/series/{series_id}", params={"opponent": "LOUD"}).status_code == 400
    assert client.get("/series/not-a-series").status_code == 404


def test_no_data_files_are_served_and_missing_database_is_sanitized(tmp_path):
    db_path = tmp_path / "scout.db"
    make_database(db_path)
    app = api.create_app(db_path, tmp_path)
    with TestClient(app) as client:
        assert not any(isinstance(route, Mount) for route in app.routes)
        assert client.get("/data/scout.db").status_code == 404
        assert client.get("/scout.db").status_code == 404
        assert client.get("/data/").status_code == 404

    missing = tmp_path / "private" / "missing.db"
    broken = api.create_app(missing, tmp_path)
    with TestClient(broken) as client:
        response = client.get("/teams")
    assert response.status_code == 503
    assert str(missing) not in response.text