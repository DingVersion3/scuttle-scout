# Infer player roles (top / jungle / mid / bot / support) from early-game positions.
# GRID data has no roles, so we use where each player spends 1:30-10:00:
#   - jungle and mid are picked by time spent in those map zones
#   - the other three are side laners; the two who stay closest together are the duo,
#     the third is top (works through lane swaps, where the duo goes top)
#   - in the duo, lower net worth at 10:00 = support
#
# Usage (from repo root, venv active):
#   python -m scuttle_scout.roles              # infer roles for every game + consistency report
#   python -m scuttle_scout.roles --game e144  # print roles for one game (id prefix)

import argparse
import itertools
import math
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

DEFAULT_DB = Path("data") / "scout.db"
WINDOW_START, WINDOW_END = 90, 600

SCHEMA = """
-- Lane each player actually played in each game (swaps show up here)
CREATE TABLE IF NOT EXISTS player_roles (
    game_id TEXT, player_id TEXT, role TEXT, confidence REAL,
    PRIMARY KEY (game_id, player_id)
);
-- Each player's usual position: their most common lane across all games
CREATE TABLE IF NOT EXISTS roster_roles (
    player_id TEXT PRIMARY KEY, name TEXT, role TEXT, games INTEGER, share REAL
);
"""


def zone(x, y):
    # Summoner's Rift is roughly 0..14800 on both axes.
    # Blue base is near (0, 0), red base near (14800, 14800).
    if x < 3000 and y < 3000:
        return "base"
    if x > 11800 and y > 11800:
        return "base"
    # Mid lane runs along the x == y diagonal
    if abs(x - y) < 1100:
        return "mid"
    # Standard Riot coordinates (confirmed: GRID's top scuttle kill is at ~(4341, 9392)).
    # Top lane hugs the left and top edges, bot lane the bottom and right edges.
    if x < 2500 or y > 12300:
        return "top"
    if y < 2500 or x > 12300:
        return "bot"
    return "jungle"


def zone_fractions(samples):
    # samples: list of (x, y). Returns {zone: fraction}, ignoring time spent in base.
    counts = Counter(zone(x, y) for x, y in samples)
    counts.pop("base", None)
    total = sum(counts.values())
    if not total:
        return {}
    return {z: c / total for z, c in counts.items()}


def side_fraction(fr):
    # Time in either side lane. Lane swaps put the duo top and the top laner bot,
    # so which side lane a player is in says nothing about their role.
    return fr.get("top", 0.0) + fr.get("bot", 0.0)


def mean_distance(track_a, track_b):
    # Average distance between two players at the timestamps both were sampled
    common = track_a.keys() & track_b.keys()
    if not common:
        return float("inf")
    return sum(math.dist(track_a[t], track_b[t]) for t in common) / len(common)


def find_jungler(players, jungle_cs):
    # The jungler kills far more neutral monsters than anyone else on the team.
    # Returns None when the data is missing or the gap isn't clear.
    counts = sorted(((jungle_cs.get(p) or 0, p) for p in players), reverse=True)
    top_count, top_player = counts[0]
    runner_up = counts[1][0]
    if top_count >= 30 and top_count >= 2 * max(runner_up, 1):
        return top_player
    return None


def assign_team(fractions, tracks, net_worth, jungle_cs):
    # fractions: {player_id: {zone: fraction}}
    # tracks:    {player_id: {t: (x, y)}}
    # net_worth: {player_id: value at ~10:00}
    # jungle_cs: {player_id: neutral monster kills at game end}
    players = list(fractions)
    roles = {}

    # Step 1: jungler by monster kills; fall back to most time in the jungle zone
    jungler = find_jungler(players, jungle_cs)
    if jungler:
        roles[jungler] = ("jungle", 1.0)
    else:
        jungler = max(players, key=lambda p: fractions[p].get("jungle", 0.0))
        roles[jungler] = ("jungle", fractions[jungler].get("jungle", 0.0))
    players.remove(jungler)

    # Step 2: mid laner is whoever spent the most time in mid; the other three are side laners
    mid = max(players, key=lambda p: fractions[p].get("mid", 0.0))
    roles[mid] = ("mid", fractions[mid].get("mid", 0.0))
    players.remove(mid)
    side_players = players

    # Step 3: the two side laners who stick together are the duo, the third is top
    duo = min(
        itertools.combinations(side_players, 2),
        key=lambda pair: mean_distance(tracks[pair[0]], tracks[pair[1]]),
    )
    solo = next(p for p in side_players if p not in duo)
    roles[solo] = ("top", side_fraction(fractions[solo]))

    # Step 4: in the duo, lower net worth is the support
    support = min(duo, key=lambda p: net_worth.get(p) or 0)
    adc = duo[0] if duo[1] == support else duo[1]
    roles[adc] = ("bot", side_fraction(fractions[adc]))
    roles[support] = ("support", side_fraction(fractions[support]))
    return roles


def infer_game(conn, game_id):
    rows = conn.execute(
        """
        SELECT gp.team_id, p.player_id, p.t, p.x, p.y, p.net_worth
        FROM positions p
        JOIN game_players gp ON gp.game_id = p.game_id AND gp.player_id = p.player_id
        WHERE p.game_id = ? AND p.t BETWEEN ? AND ?
        ORDER BY p.t
        """,
        (game_id, WINDOW_START, WINDOW_END),
    ).fetchall()

    samples = defaultdict(list)            # player_id -> [(x, y)]
    tracks = defaultdict(dict)             # player_id -> {t: (x, y)}, last sample per second
    team_of = {}                           # player_id -> team_id
    net_worth = {}                         # player_id -> latest net worth in window
    for team_id, pid, t, x, y, nw in rows:
        samples[pid].append((x, y))
        tracks[pid][t] = (x, y)
        team_of[pid] = team_id
        if nw is not None:
            net_worth[pid] = nw

    by_team = defaultdict(dict)
    for pid, pts in samples.items():
        fr = zone_fractions(pts)
        if fr:
            by_team[team_of[pid]][pid] = fr

    jungle_cs = dict(conn.execute(
        "SELECT player_id, jungle_cs FROM game_players WHERE game_id = ?", (game_id,)
    ).fetchall())

    result = {}
    for team_id, fractions in by_team.items():
        if len(fractions) != 5:
            # Missing players (bad feed, substitutions): skip this team rather than guess
            continue
        result.update(assign_team(fractions, tracks, net_worth, jungle_cs))
    return result


def infer_all(conn):
    conn.executescript(SCHEMA)
    game_ids = [r[0] for r in conn.execute("SELECT game_id FROM games")]
    skipped = 0
    for i, gid in enumerate(game_ids, 1):
        roles = infer_game(conn, gid)
        if len(roles) != 10:
            skipped += 1
        conn.execute("DELETE FROM player_roles WHERE game_id = ?", (gid,))
        conn.executemany(
            "INSERT INTO player_roles VALUES (?, ?, ?, ?)",
            [(gid, pid, role, round(conf, 3)) for pid, (role, conf) in roles.items()],
        )
        if i % 200 == 0:
            print(f"  {i}/{len(game_ids)} games")
    conn.commit()
    print(f"roles inferred for {len(game_ids)} games ({skipped} incomplete)")
    build_roster_roles(conn)


def build_roster_roles(conn):
    # A player's position is the lane they play most often; single games in another
    # lane are swaps, not a change of role
    rows = conn.execute(
        """
        SELECT gp.player_id, gp.name, pr.role
        FROM player_roles pr
        JOIN game_players gp ON gp.game_id = pr.game_id AND gp.player_id = pr.player_id
        """
    ).fetchall()
    by_player = defaultdict(Counter)
    names = {}
    for pid, name, role in rows:
        by_player[pid][role] += 1
        names[pid] = name

    conn.execute("DELETE FROM roster_roles")
    conn.executemany(
        "INSERT INTO roster_roles VALUES (?, ?, ?, ?, ?)",
        [
            (pid, names[pid], counts.most_common(1)[0][0], sum(counts.values()),
             round(counts.most_common(1)[0][1] / sum(counts.values()), 3))
            for pid, counts in by_player.items()
        ],
    )
    conn.commit()


def consistency_report(conn, min_games=10):
    # Pro players play one role. Any game where a player gets a different role than
    # their usual one is treated as an inference bug until proven otherwise.
    rows = conn.execute(
        """
        SELECT gp.player_id, gp.name, pr.game_id, pr.role
        FROM player_roles pr
        JOIN game_players gp ON gp.game_id = pr.game_id AND gp.player_id = pr.player_id
        """
    ).fetchall()
    by_player = defaultdict(list)          # player_id -> [(game_id, role)]
    names = {}
    for pid, name, gid, role in rows:
        by_player[pid].append((gid, role))
        names[pid] = name

    agree = total = 0
    mismatched = []
    for pid, games in by_player.items():
        if len(games) < min_games:
            continue
        counts = Counter(role for _, role in games)
        main_role, main_count = counts.most_common(1)[0]
        agree += main_count
        total += len(games)
        off = [(gid, role) for gid, role in games if role != main_role]
        if off:
            mismatched.append((main_count / len(games), names[pid], main_role, len(games), off))

    if total:
        print(f"\nrole agreement (players with {min_games}+ games): {agree / total:.2%}")
    print(f"players with any off-lane game (swaps, or inference errors to check): {len(mismatched)}")
    for pct, name, main_role, n, off in sorted(mismatched)[:30]:
        examples = ", ".join(f"{gid[:8]}={role}" for gid, role in off[:3])
        print(f"  {pct:6.1%}  {name:<16} {main_role:<8} {n:>3} games  off: {examples}")


def print_game(conn, prefix):
    rows = conn.execute(
        """
        SELECT gt.side, gt.name, gp.name, gp.champion, pr.role, pr.confidence
        FROM player_roles pr
        JOIN game_players gp ON gp.game_id = pr.game_id AND gp.player_id = pr.player_id
        JOIN game_teams gt ON gt.game_id = gp.game_id AND gt.team_id = gp.team_id
        WHERE pr.game_id LIKE ?
        ORDER BY gt.side, CASE pr.role
            WHEN 'top' THEN 1 WHEN 'jungle' THEN 2 WHEN 'mid' THEN 3
            WHEN 'bot' THEN 4 ELSE 5 END
        """,
        (prefix + "%",),
    ).fetchall()
    for side, team, name, champ, role, conf in rows:
        print(f"  {side:<5} {team:<14} {role:<8} {name:<14} {champ:<14} {conf:.0%}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--game", help="print roles for one game (game_id prefix)")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    if args.game:
        print_game(conn, args.game)
    else:
        infer_all(conn)
        consistency_report(conn)
    conn.close()


if __name__ == "__main__":
    main()