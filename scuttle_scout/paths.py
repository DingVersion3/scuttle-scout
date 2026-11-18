# Jungle pathing: each jungler's start camp and first-clear route.
#
# Coordinates are Riot's standard Summoner's Rift units (blue base near (0, 0),
# red base near (14800, 14800)); GRID's top scuttle kills land at ~(4341, 9392).
# Camps are named from the jungler's point of view: "Red" is their own red buff,
# "enemy Red" means they invaded.
#
# Usage (from repo root, venv active, after ingest + roles):
#   python -m scuttle_scout.paths                  # trace paths for every game
#   python -m scuttle_scout.paths --calibrate      # check camp coordinates against real data
#   python -m scuttle_scout.paths --team Fnatic    # scouting view for one team

import argparse
import math
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

DEFAULT_DB = Path("data") / "scout.db"

# Camp centres by which side's jungle they belong to. Taken from --calibrate on the
# full dataset: the mean jungler position near each camp during the first clear.
CAMPS = {
    "blue": {
        "Blue": (3390, 8060),
        "Gromp": (2695, 8250),
        "Wolves": (3813, 6496),
        "Raptors": (6820, 5030),
        "Red": (7544, 3984),
        "Krugs": (8148, 2717),
    },
    "red": {
        "Blue": (11421, 6866),
        "Gromp": (12211, 6568),
        "Wolves": (11044, 8360),
        "Raptors": (8021, 9919),
        "Red": (7365, 10965),
        "Krugs": (6758, 12226),
    },
}
SCUTTLES = {
    "Scuttle top": (4466, 9260),
    "Scuttle bot": (10373, 5505),
}

CAMP_RADIUS = 800          # a sample this close to a camp centre counts as "at the camp"
CAMPS_SPAWN = 60           # jungle camps spawn at 1:00
CLEAR_END = 300            # stop tracing at 5:00 or at the first recall, whichever is first
OPENING_END = 225          # no scuttle by 3:45 -> count camps up to here instead

# Own camps on the top-lane half vs the bot-lane half of each team's jungle
TOP_SIDE_CAMPS = {
    "blue": {"Blue", "Gromp", "Wolves"},
    "red": {"Red", "Krugs", "Raptors"},
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS jungle_paths (
    game_id TEXT, player_id TEXT, team_id TEXT, side TEXT,
    seq INTEGER, t INTEGER, camp TEXT,
    PRIMARY KEY (game_id, player_id, seq)
);
-- One row per jungler per game: the parts of the opening the data shows reliably
CREATE TABLE IF NOT EXISTS jungle_openings (
    game_id TEXT, player_id TEXT, team_id TEXT, side TEXT,
    start_side TEXT,          -- 'top' / 'bot' half of own jungle, from the first camps seen
    camps_before_scuttle INTEGER,
    first_scuttle TEXT,       -- 'top' / 'bot' / NULL
    first_scuttle_t INTEGER,
    PRIMARY KEY (game_id, player_id)
);
"""


def in_base(x, y):
    return (x < 3000 and y < 3000) or (x > 11800 and y > 11800)


def nearest_camp(x, y, team_side):
    # Returns (distance, label) for the closest camp, labelled from the jungler's view
    best = None
    for owner, camps in CAMPS.items():
        for name, centre in camps.items():
            d = math.dist((x, y), centre)
            label = name if owner == team_side else f"enemy {name}"
            if best is None or d < best[0]:
                best = (d, label)
    for name, centre in SCUTTLES.items():
        d = math.dist((x, y), centre)
        if d < best[0]:
            best = (d, name)
    return best


def trace_path(samples, team_side):
    # samples: [(t, x, y)] sorted by t. Returns [(t, camp_label)] in visit order.
    path = []

    # The first camp is usually killed during a stretch with no events, so it's not in
    # the path; the first camp we see is normally the second one of the clear.
    # Clear: group consecutive samples at the same camp; 2+ samples = a real visit,
    # so walking past a camp doesn't count
    current_label, first_t, count = None, None, 0

    def close_visit():
        if current_label and count >= 2 and (not path or path[-1][1] != current_label):
            path.append((first_t, current_label))

    for t, x, y in samples:
        if t <= CAMPS_SPAWN:
            continue
        if t > CLEAR_END or (in_base(x, y) and t > 90):
            break
        d, label = nearest_camp(x, y, team_side)
        label = label if d <= CAMP_RADIUS else None
        if label == current_label:
            count += 1
        else:
            close_visit()
            current_label, first_t, count = label, t, 1
    close_visit()
    return path


def summarize_opening(path, team_side, scuttle):
    # path: [(t, camp)]; scuttle: (t, 'top'|'bot') of the jungler's first scuttle, or None
    own_top = TOP_SIDE_CAMPS[team_side]

    # Start side: the half of their own jungle where the first own camp shows up
    start_side = None
    for t, camp in path:
        if camp in CAMPS[team_side]:
            start_side = "top" if camp in own_top else "bot"
            break

    # Own camps cleared before the first scuttle (+1 for the unseen first camp)
    cutoff = scuttle[0] if scuttle else OPENING_END
    seen = {camp for t, camp in path if t < cutoff and camp in CAMPS[team_side]}
    camps = min(len(seen) + 1, 6) if seen else None
    return start_side, camps


def first_scuttle(conn, game_id, player_id):
    row = conn.execute(
        """
        SELECT t, type FROM events
        WHERE game_id = ? AND actor_id = ? AND type LIKE '%slayRiftScuttler%'
        ORDER BY t LIMIT 1
        """,
        (game_id, player_id),
    ).fetchone()
    if not row:
        return None
    t, etype = row
    return t, "top" if etype.endswith("Top") else "bot"


def junglers(conn):
    return conn.execute(
        """
        SELECT pr.game_id, pr.player_id, gp.team_id, gt.side
        FROM player_roles pr
        JOIN game_players gp ON gp.game_id = pr.game_id AND gp.player_id = pr.player_id
        JOIN game_teams gt ON gt.game_id = gp.game_id AND gt.team_id = gp.team_id
        WHERE pr.role = 'jungle'
        """
    ).fetchall()


def samples_for(conn, game_id, player_id):
    return conn.execute(
        "SELECT t, x, y FROM positions WHERE game_id = ? AND player_id = ? AND t <= ? ORDER BY t",
        (game_id, player_id, CLEAR_END),
    ).fetchall()


def trace_all(conn):
    conn.executescript(SCHEMA)
    conn.execute("DELETE FROM jungle_paths")
    conn.execute("DELETE FROM jungle_openings")
    rows = junglers(conn)
    for i, (gid, pid, tid, side) in enumerate(rows, 1):
        path = trace_path(samples_for(conn, gid, pid), side)
        conn.executemany(
            "INSERT INTO jungle_paths VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(gid, pid, tid, side, seq, t, camp) for seq, (t, camp) in enumerate(path)],
        )
        scuttle = first_scuttle(conn, gid, pid)
        start_side, camps = summarize_opening(path, side, scuttle)
        conn.execute(
            "INSERT INTO jungle_openings VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (gid, pid, tid, side, start_side, camps,
             scuttle[1] if scuttle else None, scuttle[0] if scuttle else None),
        )
        if i % 500 == 0:
            print(f"  {i}/{len(rows)} junglers")
    conn.commit()
    print(f"traced {len(rows)} jungler paths")


def calibrate(conn):
    # For each camp, average the jungler samples near it during the first clear.
    # If the average sits within a few hundred units of our centre, the coordinates are right.
    near = defaultdict(list)
    for gid, pid, tid, side in junglers(conn):
        for t, x, y in samples_for(conn, gid, pid):
            if t < CAMPS_SPAWN:
                continue
            for owner, camps in CAMPS.items():
                for name, centre in camps.items():
                    if math.dist((x, y), centre) <= 1200:
                        near[(owner, name)].append((x, y))
            for name, centre in SCUTTLES.items():
                if math.dist((x, y), centre) <= 1200:
                    near[("river", name)].append((x, y))

    print("camp                 our centre       data mean        offset  samples")
    all_camps = [(o, n, c) for o, camps in CAMPS.items() for n, c in camps.items()]
    all_camps += [("river", n, c) for n, c in SCUTTLES.items()]
    for owner, name, centre in all_camps:
        pts = near.get((owner, name), [])
        if not pts:
            print(f"{owner:<5} {name:<14} {str(centre):<16} no samples")
            continue
        mx = sum(p[0] for p in pts) / len(pts)
        my = sum(p[1] for p in pts) / len(pts)
        offset = math.dist((mx, my), centre)
        print(f"{owner:<5} {name:<14} {str(centre):<16} ({mx:6.0f}, {my:6.0f})  {offset:6.0f}  {len(pts):>7}")


def fmt_time(t):
    return f"{t // 60}:{t % 60:02d}"


def team_report(conn, team_query):
    teams = conn.execute(
        "SELECT DISTINCT team_id, name FROM game_teams WHERE name LIKE ?", (f"%{team_query}%",)
    ).fetchall()
    if not teams:
        print(f"no team matching '{team_query}'")
        return
    team_ids = [t[0] for t in teams]
    print("teams:", ", ".join(sorted({t[1] for t in teams})))

    marks = ",".join("?" * len(team_ids))
    # One entry per game the team played, even if no camps were seen
    games = {}
    openings = {}
    for gid, side, name, champ, series_id, order, start_side, camps, scuttle, scuttle_t in conn.execute(
        f"""
        SELECT jo.game_id, jo.side, gp.name, gp.champion, g.series_id, g.game_order,
               jo.start_side, jo.camps_before_scuttle, jo.first_scuttle, jo.first_scuttle_t
        FROM jungle_openings jo
        JOIN game_players gp ON gp.game_id = jo.game_id AND gp.player_id = jo.player_id
        JOIN games g ON g.game_id = jo.game_id
        WHERE jo.team_id IN ({marks})
        ORDER BY g.series_id, g.game_order
        """,
        team_ids,
    ):
        games[gid] = {"side": side, "name": name, "champ": champ,
                      "series": series_id, "order": order, "path": []}
        openings[gid] = (start_side, camps, scuttle, scuttle_t)

    for gid, t, camp in conn.execute(
        f"SELECT game_id, t, camp FROM jungle_paths WHERE team_id IN ({marks}) ORDER BY game_id, seq",
        team_ids,
    ):
        if gid in games:
            games[gid]["path"].append((t, camp))

    start_sides = defaultdict(Counter)       # map side -> Counter(start side)
    scuttles = defaultdict(Counter)          # map side -> Counter(scuttle side)
    scuttle_times = defaultdict(list)
    camp_counts = defaultdict(list)
    for gid, g in games.items():
        start_side, camps, scuttle, scuttle_t = openings.get(gid, (None, None, None, None))
        path_str = " -> ".join(f"{camp} {fmt_time(t)}" for t, camp in g["path"])
        scuttle_str = f"{scuttle} scuttle {fmt_time(scuttle_t)}" if scuttle else "no scuttle"
        print(f"\n{g['series']} g{g['order']}  {g['side']:<4}  {g['name']} ({g['champ']})")
        print(f"  starts {start_side or '?'} side, {camps or '?'} camps, {scuttle_str}")
        print(f"  {path_str}")
        if start_side:
            start_sides[g["side"]][start_side] += 1
        if scuttle:
            scuttles[g["side"]][scuttle] += 1
            scuttle_times[g["side"]].append(scuttle_t)
        if camps:
            camp_counts[g["side"]].append(camps)

    print(f"\n=== summary over {len(games)} games ===")
    for side in ("blue", "red"):
        starts = start_sides[side]
        total = sum(starts.values())
        if not total:
            continue
        sc = scuttles[side]
        sc_total = sum(sc.values())
        times = sorted(scuttle_times[side])
        median_t = times[len(times) // 2] if times else None
        full = sum(1 for c in camp_counts[side] if c >= 6)
        print(f"{side} side ({total} games):")
        print(f"  starts top side {starts['top'] / total:.0%}, bot side {starts['bot'] / total:.0%}")
        if sc_total:
            print(f"  first scuttle top {sc['top'] / sc_total:.0%}, bot {sc['bot'] / sc_total:.0%}"
                  f" (median {fmt_time(median_t)})")
        if camp_counts[side]:
            print(f"  full clear before scuttle {full / len(camp_counts[side]):.0%}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--calibrate", action="store_true", help="check camp coordinates")
    parser.add_argument("--team", help="scouting view for a team (name contains)")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    if args.calibrate:
        calibrate(conn)
    elif args.team:
        team_report(conn, args.team)
    else:
        trace_all(conn)
    conn.close()


if __name__ == "__main__":
    main()