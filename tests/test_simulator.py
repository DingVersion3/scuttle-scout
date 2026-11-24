import pytest

from scuttle_scout.simulator import DRAFT_ORDER, IllegalAction, SeriesState


CHAMPIONS = [f"Champion{i:02}" for i in range(1, 101)]


def make_series(best_of=3):
    return SeriesState("Blue Team", "Red Team", best_of, CHAMPIONS)


def start(series, blue="Blue Team", first_pick="Blue Team"):
    return series.start_game(blue, first_pick)


def draft_next(series, champion=None):
    team, action = series.next_action()
    available = series.available_champions()
    chosen = champion if champion is not None else available[0]
    if action == "ban":
        series.ban(team, chosen)
    else:
        series.pick(team, chosen)
    return team, action, chosen


def finish_draft(series, first_pick=None):
    first_pick_done = False
    while series.next_action() is not None:
        team, action = series.next_action()
        if action == "pick" and not first_pick_done and first_pick is not None:
            selected = first_pick
            first_pick_done = True
        else:
            selected = None
        draft_next(series, selected)


def win_game(series, team="Blue Team", first_pick=None):
    finish_draft(series, first_pick)
    series.set_winner(team)


def advance_to_first_pick(series):
    for _ in range(6):
        draft_next(series)


def test_draft_order_slot_by_slot_and_red_side_first_pick():
    series = make_series()
    game = start(series, blue="Blue Team", first_pick="Red Team")
    assert (game.blue_team, game.red_team, game.first_pick_team) == (
        "Blue Team", "Red Team", "Red Team"
    )
    expected = [
        ("Red Team" if side == "first" else "Blue Team", action)
        for action, side in DRAFT_ORDER
    ]
    actual = []
    while series.next_action() is not None:
        actual.append(series.next_action())
        draft_next(series)
    assert actual == expected
    assert len(game.bans) == 10
    assert len(game.picks) == 10


def test_side_choice_and_first_pick_choice_are_independent():
    series = make_series()
    start(series, blue="Red Team", first_pick="Blue Team")
    assert series.current_game.blue_team == "Red Team"
    assert series.current_game.first_pick_team == "Blue Team"
    assert series.next_action() == ("Blue Team", "ban")


def test_fearless_pick_is_unavailable_to_both_teams_later():
    series = make_series()
    start(series)
    advance_to_first_pick(series)
    series.pick("Blue Team", CHAMPIONS[20])
    win_game(series)
    start(series, blue="Red Team", first_pick="Red Team")
    assert CHAMPIONS[20] not in series.available_champions()
    with pytest.raises(IllegalAction, match="burned for the series"):
        series.ban("Red Team", CHAMPIONS[20])
    advance_to_first_pick(series)
    with pytest.raises(IllegalAction, match="burned for the series"):
        series.pick("Red Team", CHAMPIONS[20])


def test_bans_do_not_carry_over_between_games():
    series = make_series()
    start(series)
    banned = CHAMPIONS[0]
    draft_next(series, banned)
    assert banned not in series.available_champions()
    win_game(series)
    assert banned in series.available_champions()
    start(series)
    for champion in CHAMPIONS[30:36]:
        draft_next(series, champion)
    series.pick("Blue Team", banned)
    assert banned in series.burned_champions()


def test_duplicate_ban_and_pick_within_game_are_rejected():
    series = make_series()
    start(series)
    first_ban = CHAMPIONS[0]
    draft_next(series, first_ban)
    with pytest.raises(IllegalAction, match="already been banned"):
        series.ban("Red Team", first_ban)
    while len(series.current_game.actions) < 6:
        draft_next(series)
    banned = series.current_game.bans[0][1]
    with pytest.raises(IllegalAction, match="was banned"):
        series.pick("Blue Team", banned)
    first_pick_team, _ = series.next_action()
    picked = CHAMPIONS[10]
    series.pick(first_pick_team, picked)
    next_team, _ = series.next_action()
    with pytest.raises(IllegalAction, match="already been picked"):
        series.pick(next_team, picked)


def test_rejects_wrong_action_wrong_turn_and_unknown_champions():
    series = make_series()
    start(series)
    with pytest.raises(IllegalAction, match="expects a ban"):
        series.pick("Blue Team", CHAMPIONS[0])
    with pytest.raises(IllegalAction, match="it is Blue Team's turn"):
        series.ban("Red Team", CHAMPIONS[0])
    with pytest.raises(IllegalAction, match="unknown champion"):
        series.ban("Blue Team", "NotAChampion")


def test_champion_names_are_matched_without_case_or_punctuation():
    series = SeriesState("A", "B", 3, ["K'Sante", "Aatrox"])
    start(series, blue="A", first_pick="A")
    series.ban("A", "k-sAnTe")
    assert series.current_game.bans == [("A", "K'Sante")]
    assert "K'Sante" not in series.available_champions()


def test_burned_champions_only_contains_picks_and_available_excludes_bans():
    series = make_series()
    start(series)
    banned = CHAMPIONS[0]
    advance_to_first_pick(series)
    assert banned not in series.available_champions()
    assert series.burned_champions() == set()
    picked = series.available_champions()[0]
    team, _ = series.next_action()
    series.pick(team, picked)
    assert series.burned_champions() == {picked}
    assert picked not in series.available_champions()


def test_remaining_pools_applies_series_burns_to_both_teams():
    series = make_series()
    start(series)
    advance_to_first_pick(series)
    series.pick("Blue Team", CHAMPIONS[20])

    def pool(team_id):
        return {
            "team_id": team_id,
            "team_names": [team_id],
            "players": [{
                "player_id": team_id,
                "name": team_id,
                "role": "mid",
                "champions": [
                    {"champion": CHAMPIONS[20], "comfort": True, "recent_game_ids": ["game-1"]},
                    {"champion": CHAMPIONS[1], "comfort": True, "recent_game_ids": ["game-2"]},
                ],
            }],
        }

    remaining = series.remaining_pools(pools={"Blue Team": pool("blue"), "Red Team": pool("red")})
    for team in series.teams:
        player = remaining[team]["players"][0]
        assert [row["champion"] for row in player["remaining_comfort_picks"]] == [CHAMPIONS[1]]
        assert [row["champion"] for row in player["burned_comfort_picks"]] == [CHAMPIONS[20]]


@pytest.mark.parametrize(
    ("best_of", "winners", "expected"),
    [
        (3, ["Blue Team", "Blue Team"], "Blue Team"),
        (5, ["Blue Team", "Blue Team", "Blue Team"], "Blue Team"),
        (5, ["Blue Team", "Red Team", "Red Team", "Blue Team", "Blue Team"], "Blue Team"),
    ],
)
def test_series_ends_at_required_win_count(best_of, winners, expected):
    series = make_series(best_of)
    assert series.wins_needed == (2 if best_of == 3 else 3)
    for winner in winners:
        start(series)
        win_game(series, winner)
    assert series.is_over
    assert series.winner == expected
    assert series.score[expected] == series.wins_needed
    with pytest.raises(IllegalAction, match="series is over"):
        start(series)
    with pytest.raises(IllegalAction, match="series is over"):
        series.ban(expected, CHAMPIONS[0])


def test_winner_requires_complete_draft_and_cannot_be_set_twice():
    series = make_series()
    start(series)
    with pytest.raises(IllegalAction, match="draft is incomplete"):
        series.set_winner("Blue Team")
    win_game(series)
    with pytest.raises(IllegalAction, match="already has a winner"):
        series.set_winner("Blue Team")


def test_series_configuration_validation():
    with pytest.raises(ValueError, match="different teams"):
        SeriesState("A", "A", 3, CHAMPIONS)
    with pytest.raises(ValueError, match="best_of must be 3 or 5"):
        SeriesState("A", "B", 7, CHAMPIONS)
    with pytest.raises(IllegalAction, match="unknown team"):
        make_series().start_game("Other", "Blue Team")
