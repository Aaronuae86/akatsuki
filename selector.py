"""Blast Radius container entrypoint."""

import argparse
import json
from math import floor
from pathlib import Path
import sys
import time

from dependencies import DependencyIndex
from patches import apply_diff
from replay import Replay
from schedule import cost, pack


def verification_group(numbers, durations, cost_ms):
    if not numbers or len(numbers) > 350:
        return set()
    total = sum(durations[n] for n in numbers)
    if total <= 3_200 and len(numbers) <= 130:
        return numbers
    # Leave expensive tests selected and check tests whose removal could
    # shorten the schedule or let it use fewer workers.
    short = {n for n in numbers if durations[n] <= 2_600}
    if not short or len(short) > 130 or sum(durations[n] for n in short) > 5_000:
        return set()
    remain = numbers - short
    optimistic = cost(pack(remain, durations), durations)
    if cost_ms - optimistic >= 1_700:
        return short
    return set()


def main(data, source, index_path, verify, verbose):
    start = time.monotonic()
    tests = json.loads((data / "tests.json").read_text(encoding="utf-8"))["tests"]
    by_name = {test["id"]: test["number"] for test in tests}
    names = {test["number"]: test["id"] for test in tests}
    durations = {test["number"]: floor(test["seconds"] * 1000 + 0.5) for test in tests}
    all_workers = pack(durations, durations)
    paths = sorted((data / "commits").glob("*.patch"))
    index = None
    if index_path.is_file():
        index = DependencyIndex(json.loads(index_path.read_text(encoding="utf-8")), tests)
    replay = Replay(source) if verify else None
    verification_left = 48.0
    deadline = start + 150.0
    try:
        for patch in paths:
            workers = all_workers
            reason = "no index"
            verify_wall = 0.0
            if index:
                try:
                    changes = apply_diff(source, patch.read_bytes())
                    impact = index.candidates(changes)
                    reason = impact.fallback
                    if not reason:
                        candidates = {by_name[name] for name in impact.names}
                        workers = pack(candidates, durations)
                        before = cost(workers, durations)
                        if replay and verification_left >= 1.1 and time.monotonic() < deadline:
                            group = set()
                            if any(change.path.startswith("sqlglot/") and change.removed for change in changes):
                                group = verification_group(candidates, durations, before)
                                group -= {by_name[name] for name in index.unverified}
                            if group:
                                budget = min(7.5, verification_left, deadline - time.monotonic())
                                if budget >= 1.1:
                                    selected = {names[n] for n in group}
                                    passed, collection_error, verify_wall = replay.check(changes, selected, budget)
                                    verification_left -= verify_wall
                                    if collection_error:
                                        workers = all_workers
                                        reason = "verification collection error"
                                    else:
                                        workers = pack(candidates - {by_name[name] for name in passed}, durations)
                        if verbose:
                            print(f"{patch.stem}: candidates={len(candidates)} {before/1000:.3f}s",
                                  file=sys.stderr)
                except (OSError, ValueError, IndexError, KeyError, SyntaxError, UnicodeError) as error:
                    workers = all_workers
                    reason = f"{type(error).__name__}: {error}"
            if verbose or reason:
                print(
                    f"{patch.stem}: selected={sum(map(len, workers))} "
                    f"simulated={cost(workers, durations)/1000:.3f}s "
                    f"verify_wall={verify_wall:.3f}s {reason}",
                    file=sys.stderr,
                )
            print(json.dumps({"commit": patch.stem, "workers": workers}, separators=(",", ":")), flush=True)
    finally:
        if replay:
            replay.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path("/data"))
    parser.add_argument("--source", type=Path, default=Path("/opt/sqlglot"))
    parser.add_argument("--index", type=Path, default=Path("/opt/dependencies.json"))
    parser.add_argument("--verify", action="store_true", help="try budgeted pytest reruns")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    main(args.data.resolve(), args.source.resolve(), args.index.resolve(), args.verify, args.verbose)
