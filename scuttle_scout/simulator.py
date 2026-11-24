# Simulate a Bo3/Bo5 Fearless draft series.
#
# Draft order follows the standard pro format: first/second teams act by
# sequence, independently from the blue/red side assignment.

import re
from dataclasses import dataclass, field

from scuttle_scout.drafts import DEFAULT_DATA, pool_after_burns


DRAFT_ORDER = (
    ("ban", "first"), ("ban", "second"), ("ban", "first"),
    ("ban", "second"), ("ban", "first"), ("ban", "second"),
    ("pick", "first"), ("pick", "second"), ("pick", "second"),
    ("pick", "first"), ("pick", "first"), ("pick", "second"),
    ("ban", "second"), ("ban", "first"), ("ban", "second"),
    ("ban", "first"),
    ("pick", "second"), ("pick", "first"), ("pick", "first"),
    ("pick", "second"),
)


class IllegalAction(ValueError):
    pass


@dataclass
class GameState:
    number: int
    blue_team: str
    red_team: str
    first_pick_team: str
    actions: list = field(default_factory=list)
    bans: list = field(default_factory=list)
    picks: list = field(default_factory=list)
    winner: str | None = None


class SeriesState:
    def __init__(self, team_a, team_b, best_of, champions):
        if team_a == team_b:
            raise ValueError("a series requires two different teams")
        if best_of not in (3, 5):
            raise ValueError("best_of must be 3 or 5")
        self.teams = (team_a, team_b)
        self.best_of = best_of
        self.champions = self._canonical_champions(champions)
        self.games = []

    @staticmethod
    def _normalize_champion(champion):
        return re.sub(r"[^a-z0-9]", "", str(champion).lower())

    @classmethod
    def _canonical_champions(cls, champions):
        canonical = {}
        for champion in champions:
            name = str(champion).strip()
            key = cls._normalize_champion(name)
            if not key:
                raise ValueError("champion names must contain letters or numbers")
            existing = canonical.get(key)
            if existing is not None and existing != name:
                raise ValueError(f"ambiguous champion names normalize to the same value: {existing}, {name}")
            canonical[key] = name
        if not canonical:
            raise ValueError("at least one champion is required")
        return tuple(sorted(canonical.values(), key=str.lower))

    @property
    def wins_needed(self):
        return self.best_of // 2 + 1

    @property
    def score(self):
        wins = {team: 0 for team in self.teams}
        for game in self.games:
            if game.winner is not None:
                wins[game.winner] += 1
        return wins

    @property
    def is_over(self):
        return any(wins >= self.wins_needed for wins in self.score.values())

    @property
    def winner(self):
        return next((team for team, wins in self.score.items() if wins >= self.wins_needed), None)

    @property
    def current_game(self):
        return self.games[-1] if self.games else None

    def _other_team(self, team):
        return self.teams[1] if team == self.teams[0] else self.teams[0]

    def _validate_team(self, team):
        if team not in self.teams:
            raise IllegalAction(f"unknown team {team!r}; expected one of {self.teams!r}")

    def start_game(self, blue_team, first_pick_team):
        if self.is_over:
            raise IllegalAction(f"series is over; {self.winner} won")
        if self.current_game is not None and self.current_game.winner is None:
            raise IllegalAction(f"game {self.current_game.number} has not been decided")
        self._validate_team(blue_team)
        self._validate_team(first_pick_team)
        red_team = self._other_team(blue_team)
        game = GameState(
            number=len(self.games) + 1,
            blue_team=blue_team,
            red_team=red_team,
            first_pick_team=first_pick_team,
        )
        self.games.append(game)
        return game

    def _action_at(self, index, game):
        action, order = DRAFT_ORDER[index]
        team = game.first_pick_team if order == "first" else self._other_team(game.first_pick_team)
        return team, action

    def next_action(self):
        game = self.current_game
        if game is None or game.winner is not None or len(game.actions) >= len(DRAFT_ORDER):
            return None
        return self._action_at(len(game.actions), game)

    def burned_champions(self):
        return {champion for game in self.games for _, champion in game.picks}

    def available_champions(self):
        unavailable = {self._normalize_champion(champion) for champion in self.burned_champions()}
        game = self.current_game
        if game is not None and game.winner is None:
            unavailable.update(self._normalize_champion(champion) for _, champion in game.bans)
        return [
            champion for champion in self.champions
            if self._normalize_champion(champion) not in unavailable
        ]

    def _require_turn(self, team, action):
        self._validate_team(team)
        if self.is_over:
            raise IllegalAction(f"series is over; {self.winner} won")
        game = self.current_game
        if game is None:
            raise IllegalAction("start a game before drafting")
        if game.winner is not None:
            raise IllegalAction(f"game {game.number} already has a winner")
        expected = self.next_action()
        if expected is None:
            raise IllegalAction(f"game {game.number} draft is complete; set its winner")
        expected_team, expected_action = expected
        if expected_action != action:
            raise IllegalAction(f"draft expects a {expected_action}, not a {action}")
        if expected_team != team:
            raise IllegalAction(f"it is {expected_team}'s turn to {expected_action}, not {team}'s")
        return game

    def _canonical_action_champion(self, champion):
        key = self._normalize_champion(champion)
        for canonical in self.champions:
            if self._normalize_champion(canonical) == key:
                return canonical
        raise IllegalAction(f"unknown champion {champion!r}")

    def ban(self, team, champion):
        game = self._require_turn(team, "ban")
        champion = self._canonical_action_champion(champion)
        key = self._normalize_champion(champion)
        banned = {self._normalize_champion(name) for _, name in game.bans}
        if key in banned:
            raise IllegalAction(f"{champion} has already been banned in game {game.number}")
        picked_game = next(
            (picked.number for picked in self.games
             if any(self._normalize_champion(name) == key for _, name in picked.picks)),
            None,
        )
        if picked_game is not None:
            raise IllegalAction(f"{champion} was picked in game {picked_game} and is burned for the series")
        game.bans.append((team, champion))
        game.actions.append(("ban", team, champion))
        return champion

    def pick(self, team, champion):
        game = self._require_turn(team, "pick")
        champion = self._canonical_action_champion(champion)
        key = self._normalize_champion(champion)
        if any(self._normalize_champion(name) == key for _, name in game.bans):
            raise IllegalAction(f"{champion} was banned in game {game.number}")
        picked_game = next(
            (picked.number for picked in self.games
             if any(self._normalize_champion(name) == key for _, name in picked.picks)),
            None,
        )
        if picked_game is not None:
            if picked_game == game.number:
                raise IllegalAction(f"{champion} has already been picked in game {game.number}")
            raise IllegalAction(f"{champion} was picked in game {picked_game} and is burned for the series")
        game.picks.append((team, champion))
        game.actions.append(("pick", team, champion))
        return champion

    def set_winner(self, team):
        self._validate_team(team)
        if self.is_over:
            raise IllegalAction(f"series is over; {self.winner} won")
        game = self.current_game
        if game is None:
            raise IllegalAction("start a game before setting a winner")
        if game.winner is not None:
            raise IllegalAction(f"game {game.number} already has a winner")
        if len(game.actions) != len(DRAFT_ORDER):
            raise IllegalAction(f"game {game.number} draft is incomplete ({len(game.actions)}/{len(DRAFT_ORDER)} actions)")
        game.winner = team
        return self.winner

    def remaining_pools(self, conn=None, pools=None, data_dir=DEFAULT_DATA, strict_fearless=False):
        burned = self.burned_champions()
        if pools is None:
            return {
                team: pool_after_burns(
                    team, burned, conn=conn, data_dir=data_dir, strict_fearless=strict_fearless
                )
                for team in self.teams
            }
        return {
            team: pool_after_burns(pools[team], burned)
            for team in self.teams
        }


def load_champions(conn):
    rows = conn.execute(
        """
        SELECT champion FROM draft WHERE champion IS NOT NULL
        UNION
        SELECT champion FROM game_players WHERE champion IS NOT NULL
        ORDER BY champion
        """
    )
    return [row[0] for row in rows]