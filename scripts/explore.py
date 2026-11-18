# Data spike: inspect one LoL series from the GRID starter repo.
# Answers: what event types exist, where player positions live, how often
# positions update, and what draft data looks like.
#
# Usage:
#   python scripts/explore.py                      # first LoL series found
#   python scripts/explore.py --series <file.zst>  # a specific series file
#   python scripts/explore.py --data ~/data/grid-starter
#
# Samples are written to ./data/spike/ (gitignored) so no GRID data gets committed.

import argparse
import csv
import io
import json
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

DEFAULT_DATA = Path.home() / "data" / "grid-starter"
SAMPLE_DIR = Path("data") / "spike"


def open_zst_lines(path):
    try:
        from compression import zstd # type: ignore
        return zstd.open(path, "rt", encoding="utf-8")
    except ImportError:
        import zstandard
        fh = open(path, "rb")
        reader = zstandard.ZstdDecompressor().stream_reader(fh)
        return io.TextIOWrapper(reader, encoding="utf-8")


def parse_ts(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def walk(node, path=""):
    if isinstance(node, dict):
        yield path, node
        for key, value in node.items():
            yield from walk(value, f"{path}.{key}" if path else key)
    elif isinstance(node, list):
        for i, item in enumerate(node):
            yield from walk(item, f"{path}[]")

def entity_label(d):
    name = d.get("name") or d.get("nickname")
    ident = d.get("id")
    char = d.get("character")
    if isinstance(char, dict):
        char = char.get("name") or char.get("id")
    parts = [str(p) for p in (name, char, ident) if p]
    return " / ".join(parts) or "unknown"

def show_csv_head(csv_path, rows=3):
    if not csv_path.exists():
        print(f"  (missing: {csv_path})")
        return
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        print(f"  columns: {reader.fieldnames}")
        for i, row in enumerate(reader):
            if i >= rows:
                break
            print(f"  {dict(row)}")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--series", type=Path, help="specific .zst series file")
    args = parser.parse_args()

    lol_dir = args.data.expanduser() / "lol"
    meta_dir = args.data.expanduser() / "meta"

    print("=== lol/series.csv ===")
    show_csv_head(lol_dir / "series.csv")
    for name in ("games.csv", "players.csv", "teams.csv"):
        print(f"=== meta/{name} ===")
        show_csv_head(meta_dir / name, rows=2)

    series_file = args.series
    if series_file is None:
        files = sorted(lol_dir.rglob("*.zst"))
        if not files:
            sys.exit(f"No .zst files under {lol_dir}. Did the sparse checkout pull lol/?")
        series_file = files[0]
    print(f"\n=== series file: {series_file} ===")

    envelope_keys = Counter()
    event_types = Counter()
    first_of_type = {}
    position_paths = Counter()
    draft_samples = []
    last_pos = {}
    change_times = defaultdict(list)

    envelopes = 0
    with open_zst_lines(series_file) as lines:
        for line in lines:
            line = line.strip()
            if not line:
                continue
            envelope = json.loads(line)
            envelopes += 1
            envelope_keys.update(envelope.keys())
            env_ts = parse_ts(envelope.get("occurredAt"))

            events = envelope.get("events") or []
            for event in events:
                etype = event.get("type", "<no type>")
                event_types[etype] += 1
                if etype not in first_of_type:
                    first_of_type[etype] = event

                ts = parse_ts(event.get("occurredAt")) or env_ts

                for path, d in walk(event):
                    if "draftActions" in d and len(draft_samples) < 1:
                        draft_samples.append(d["draftActions"])

                    pos = d.get("position")
                    if isinstance(pos, dict) and "x" in pos and "y" in pos:
                        position_paths[path] += 1
                        label = entity_label(d)
                        xy = (pos["x"], pos["y"])
                        if last_pos.get(label) != xy:
                            last_pos[label] = xy
                            if ts:
                                change_times[label].append(ts)

    print(f"\nenvelopes: {envelopes}")
    print(f"envelope keys: {dict(envelope_keys)}")

    print(f"\n=== event types ({len(event_types)}) ===")
    for etype, count in event_types.most_common():
        print(f"  {count:>7}  {etype}")

    print("\n=== where positions live (JSON path -> count) ===")
    for path, count in position_paths.most_common(15):
        print(f"  {count:>7}  {path}")

    print("\n=== position update interval per entity (seconds between changes) ===")
    for label, times in sorted(change_times.items(), key=lambda kv: -len(kv[1]))[:12]:
        times.sort()
        gaps = [(b - a).total_seconds() for a, b in zip(times, times[1:]) if b > a]
        if len(gaps) < 2:
            continue
        print(
            f"  {label[:40]:<40} changes={len(times):>6} "
            f"median={statistics.median(gaps):.2f}s p90={sorted(gaps)[int(len(gaps) * 0.9)]:.2f}s"
        )

    SAMPLE_DIR.mkdir(parents=True, exist_ok=True)
    out = SAMPLE_DIR / f"{series_file.name}.samples.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(
            {"first_of_each_type": first_of_type, "draftActions": draft_samples},
            f,
            indent=2,
            default=str,
        )
    print(f"\nfull example of each event type written to {out}")


if __name__ == "__main__":
    main()