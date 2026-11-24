# Run with: uvicorn scuttle_scout.api:app --root-path /coach
# Install API dependencies with: pip install fastapi 'uvicorn[standard]' httpx

from collections import Counter, defaultdict
from contextlib import closing
from copy import deepcopy
import logging
import os
from pathlib import Path
import sqlite3
from threading import RLock
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from scuttle_scout import drafts
from scuttle_scout.flags import load_scouting, series_flags
from scuttle_scout.simulator import IllegalAction, SeriesState, load_champions


logger = logging.getLogger(__name__)
MAX_SERIES = 500


class CreateSeriesRequest(BaseModel):
    team_a: str = Field(min_length=1, max_length=80)
    team_b: str = Field(min_length=1, max_length=80)
    best_of: int


class ActionRequest(BaseModel):
    action: str = Field(min_length=1, max_length=16)
    team: str = Field(min_length=1, max_length=80)
    champion: str | None = Field(default=None, max_length=48)
    side: str | None = Field(default=None, max_length=8)


def _safe_report_value(value, in_baseline=False):
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            if key in ("team_id", "player_id", "baseline_game_ids"):
                continue
            if in_baseline and key == "game_ids":
                continue
            cleaned[key] = _safe_report_value(item, in_baseline or key == "baseline")
        return cleaned
    if isinstance(value, list):
        return [_safe_report_value(item, in_baseline) for item in value]
    return value


def _limit_draft_rows(value, top):
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "rows" and isinstance(item, list):
                value[key] = item[:top]
            else:
                _limit_draft_rows(item, top)
    elif isinstance(value, list):
        for item in value:
            _limit_draft_rows(item, top)


def _series_response(series, pending):
    games = []
    for game in series.games:
        games.append({
            "number": game.number,
            "blue_team": game.blue_team,
            "red_team": game.red_team,
            "first_pick_team": game.first_pick_team,
            "actions": [
                {"action": action, "team": team, "champion": champion}
                for action, team, champion in game.actions
            ],
            "bans": [{"team": team, "champion": champion} for team, champion in game.bans],
            "picks": [{"team": team, "champion": champion} for team, champion in game.picks],
            "winner": game.winner,
        })
    action = series.next_action()
    return {
        "teams": list(series.teams),
        "best_of": series.best_of,
        "wins_needed": series.wins_needed,
        "score": series.score,
        "is_over": series.is_over,
        "winner": series.winner,
        "games": games,
        "pending": dict(pending),
        "next_action": {"team": action[0], "action": action[1]} if action else None,
        "burned_champions": sorted(series.burned_champions(), key=str.lower),
        "available_champions": series.available_champions(),
    }


def create_app(db_path=None, data_dir=None):
    app = FastAPI(title="Scuttle Scout API")
    app.state.db_path = Path(
        db_path or os.environ.get("SCUTTLE_SCOUT_DB") or drafts.DEFAULT_DB
    )
    app.state.data_dir = Path(
        data_dir or os.environ.get("SCUTTLE_SCOUT_DATA") or drafts.DEFAULT_DATA
    )
    app.state.series = {}
    app.state.report_cache = {}
    app.state.scouting_cache = {}
    app.state.champions = None
    app.state.teams_cache = None
    app.state.lock = RLock()

    def connect():
        return drafts._connect(app.state.db_path)

    def resolve_team(conn, name):
        try:
            return drafts._team_matches(conn, name)
        except drafts.TeamNotFound as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except drafts.AmbiguousTeam as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    def unavailable(error, message="Scouting data is unavailable."):
        logger.exception("Scuttle Scout API could not load scouting data", exc_info=error)
        raise HTTPException(status_code=503, detail=message) from error

    def load_team_report(conn, name, team_id):
        cached = app.state.report_cache.get(team_id)
        if cached is None:
            draft_report = drafts.team_draft_stats(conn, name)
            pools_report = drafts.team_champion_pools(conn, name, app.state.data_dir)
            cached = {
                "draft": _safe_report_value(draft_report),
                "pools": _safe_report_value(pools_report),
            }
            app.state.report_cache[team_id] = cached
        return deepcopy(cached)

    def get_scouting(conn, name, team_id):
        if team_id not in app.state.scouting_cache:
            app.state.scouting_cache[team_id] = load_scouting(
                conn, name, app.state.data_dir
            )
        return app.state.scouting_cache[team_id]

    @app.get("/teams")
    def get_teams():
        with app.state.lock:
            if app.state.teams_cache is not None:
                return deepcopy(app.state.teams_cache)
            try:
                with closing(connect()) as conn:
                    fearless, _, _ = drafts.fearless_series(conn)
                    team_names = defaultdict(Counter)
                    team_games = defaultdict(set)
                    rows = conn.execute(
                        """
                        SELECT g.series_id, g.game_id, gt.team_id, gt.name
                        FROM games AS g
                        JOIN game_teams AS gt ON gt.game_id = g.game_id
                        WHERE g.winner_team_id IS NOT NULL AND gt.team_id IS NOT NULL
                        """
                    )
                    for series_id, game_id, team_id, name in rows:
                        if str(series_id) not in fearless:
                            continue
                        team_names[team_id][name or "Unknown team"] += 1
                        team_games[team_id].add(game_id)
                    teams = []
                    for team_id, counts in team_names.items():
                        names = sorted(counts, key=lambda value: value.casefold())
                        name = min(names, key=lambda value: (-counts[value], value.casefold()))
                        teams.append({"name": name, "names": names, "games": len(team_games[team_id])})
                    teams.sort(key=lambda team: team["name"].casefold())
                    app.state.teams_cache = teams
                    return deepcopy(teams)
            except HTTPException:
                raise
            except (sqlite3.Error, OSError, ValueError) as error:
                unavailable(error)

    @app.get("/teams/{name}/report")
    def get_team_report(name: str, top: int = Query(default=10, ge=1, le=50)):
        with app.state.lock:
            try:
                with closing(connect()) as conn:
                    team_id, names = resolve_team(conn, name)
                    report = load_team_report(conn, name, team_id)
                    report["team"] = {
                        "name": names[0],
                        "names": names,
                        "games": report["draft"].get("games", 0),
                    }
                    _limit_draft_rows(report["draft"], top)
                    return report
            except HTTPException:
                raise
            except (sqlite3.Error, OSError, ValueError) as error:
                unavailable(error)

    @app.post("/series", status_code=201)
    def create_series(request: CreateSeriesRequest):
        with app.state.lock:
            try:
                with closing(connect()) as conn:
                    team_a = request.team_a.strip()
                    team_b = request.team_b.strip()
                    team_a_id, _ = resolve_team(conn, team_a)
                    team_b_id, _ = resolve_team(conn, team_b)
                    if team_a_id == team_b_id:
                        raise HTTPException(status_code=400, detail="a series requires two different teams")
                    if app.state.champions is None:
                        app.state.champions = load_champions(conn)
                    try:
                        state = SeriesState(team_a, team_b, request.best_of, app.state.champions)
                    except ValueError as error:
                        raise HTTPException(status_code=422, detail=str(error)) from error
                    series_id = uuid4().hex
                    if len(app.state.series) >= MAX_SERIES:
                        oldest = next(iter(app.state.series))
                        del app.state.series[oldest]
                    pending = {"blue_team": None, "first_pick_team": None}
                    app.state.series[series_id] = {
                        "state": state,
                        "pending": pending,
                        "team_ids": {team_a: team_a_id, team_b: team_b_id},
                        "queries": {team_a: team_a, team_b: team_b},
                    }
                    return {"id": series_id, "state": _series_response(state, pending)}
            except HTTPException:
                raise
            except (sqlite3.Error, OSError, ValueError) as error:
                unavailable(error)

    @app.post("/series/{series_id}/action")
    def series_action(series_id: str, request: ActionRequest):
        with app.state.lock:
            entry = app.state.series.get(series_id)
            if entry is None:
                raise HTTPException(status_code=404, detail="series not found")
            series = entry["state"]
            action = request.action.lower().replace("_", "-")
            try:
                if action == "side":
                    if request.side is None:
                        raise HTTPException(status_code=422, detail="side is required for a side action")
                    if request.side.lower() not in ("blue", "red"):
                        raise HTTPException(status_code=422, detail="side must be blue or red")
                    if series.is_over:
                        raise IllegalAction(f"series is over; {series.winner} won")
                    series._validate_team(request.team)
                    if series.current_game is not None and series.current_game.winner is None:
                        raise IllegalAction(f"game {series.current_game.number} has not been decided")
                    blue_team = request.team if request.side.lower() == "blue" else series._other_team(request.team)
                    entry["pending"]["blue_team"] = blue_team
                elif action in ("first-pick", "firstpick"):
                    if series.is_over:
                        raise IllegalAction(f"series is over; {series.winner} won")
                    series._validate_team(request.team)
                    if series.current_game is not None and series.current_game.winner is None:
                        raise IllegalAction(f"game {series.current_game.number} has not been decided")
                    entry["pending"]["first_pick_team"] = request.team
                elif action in ("pick", "ban"):
                    if request.champion is None:
                        raise HTTPException(status_code=422, detail=f"champion is required for a {action} action")
                    getattr(series, action)(request.team, request.champion)
                elif action == "winner":
                    series.set_winner(request.team)
                else:
                    raise IllegalAction("action must be side, first-pick, ban, pick, or winner")

                pending = entry["pending"]
                if pending["blue_team"] is not None and pending["first_pick_team"] is not None:
                    series.start_game(pending["blue_team"], pending["first_pick_team"])
                    entry["pending"] = {"blue_team": None, "first_pick_team": None}
                return {"id": series_id, "state": _series_response(series, entry["pending"])}
            except HTTPException:
                raise
            except IllegalAction as error:
                raise HTTPException(status_code=409, detail=str(error)) from error

    @app.get("/series/{series_id}")
    def get_series(series_id: str, opponent: str | None = None):
        with app.state.lock:
            entry = app.state.series.get(series_id)
            if entry is None:
                raise HTTPException(status_code=404, detail="series not found")
            series = entry["state"]
            try:
                with closing(connect()) as conn:
                    if opponent is None:
                        selected_teams = list(series.teams)
                    else:
                        opponent_id, _ = resolve_team(conn, opponent)
                        selected = next(
                            (team for team, team_id in entry["team_ids"].items() if team_id == opponent_id),
                            None,
                        )
                        if selected is None:
                            raise HTTPException(status_code=400, detail="opponent must be one of the series teams")
                        selected_teams = [selected]
                    flags = {
                        team: series_flags(
                            series,
                            team,
                            scouting=get_scouting(conn, entry["queries"][team], entry["team_ids"][team]),
                        )
                        for team in selected_teams
                    }
                    return {
                        "id": series_id,
                        "state": _series_response(series, entry["pending"]),
                        "flags": flags,
                    }
            except HTTPException:
                raise
            except (sqlite3.Error, OSError, ValueError) as error:
                unavailable(error)

    return app


app = create_app()