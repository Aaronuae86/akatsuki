"""Blast Radius container entrypoint."""

import argparse
import json
from math import floor
from pathlib import Path
import sys
import time

from dependencies import DependencyIndex
from index_store import load_index
from patches import apply_diff
from replay import Replay
from schedule import cost, pack


EXECUTOR_TEST = "tests/test_executor.py::TestExecutor::test_execute_tpcds"


def choose_query_jobs(profile, path, lines):
    if lines.intersection(profile["parent"]["files"].get(path, ())):
        return None
    jobs = [i for i, job in enumerate(profile["jobs"])
            if lines.intersection(job["files"].get(path, ()))]
    cpu = sum(profile["jobs"][i]["cpu"] for i in jobs)
    # CPU in the probe differs by host. Keep the replay to sparse jobs.
    if not jobs or len(jobs) > 8 or cpu > 4:
        return None
    return jobs


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


def cheap_verification_group(candidates, durations, unverified):
    if (candidates and not candidates.intersection(unverified)
        and len(candidates) <= 5 and sum(durations[n] for n in candidates) <= 650):
        return candidates
    return set()


def main(data, source, index_path, verify, verbose, executor_index=None, check_cheap=False):
    start = time.monotonic()
    tests = json.loads((data / "tests.json").read_text(encoding="utf-8"))["tests"]
    by_name = {test["id"]: test["number"] for test in tests}
    names = {test["number"]: test["id"] for test in tests}
    durations = {test["number"]: floor(test["seconds"] * 1000 + 0.5) for test in tests}
    all_workers = pack(durations, durations)
    paths = sorted((data / "commits").glob("*.patch"))
    index = None
    if index_path.is_file():
        index = DependencyIndex(load_index(index_path), tests)
    profile = json.loads(executor_index.read_bytes()) if executor_index and executor_index.is_file() else None
    replay = Replay(source) if verify or profile or check_cheap else None
    verification_left = 48.0
    cheap_budget = 14.0
    unverified = {by_name[name] for name in index.unverified} if index else set()
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
                        if (profile and impact.executor_lines and EXECUTOR_TEST in impact.names
                            and EXECUTOR_TEST not in index.unverified and verification_left >= 8
                            and deadline - time.monotonic() >= 8):
                            executor_number = by_name[EXECUTOR_TEST]
                            reduced = pack(candidates - {executor_number}, durations)
                            if before - cost(reduced, durations) >= 12_000:
                                jobs = choose_query_jobs(profile, changes[0].path, impact.executor_lines)
                                if jobs is not None:
                                    passed, _, spent = replay.check(
                                        changes, {EXECUTOR_TEST}, 8, query_jobs=jobs,
                                        query_count=len(profile["jobs"]),
                                    )
                                    verification_left -= spent
                                    verify_wall += spent
                                    if EXECUTOR_TEST in passed:
                                        candidates.remove(executor_number)
                                        workers = reduced
                                        before = cost(workers, durations)
                        if verify and verification_left >= 1.1 and time.monotonic() < deadline:
                            group = set()
                            if any(change.path.startswith("sqlglot/") and change.removed for change in changes):
                                group = verification_group(candidates, durations, before)
                                group -= unverified
                            if group:
                                budget = min(7.5, verification_left, deadline - time.monotonic())
                                if budget >= 1.1:
                                    selected = {names[n] for n in group}
                                    passed, collection_error, spent = replay.check(changes, selected, budget)
                                    verification_left -= spent
                                    verify_wall += spent
                                    if collection_error:
                                        workers = all_workers
                                        reason = "verification collection error"
                                    else:
                                        workers = pack(candidates - {by_name[name] for name in passed}, durations)
                        elif (check_cheap and cheap_budget >= 1.5 and deadline - time.monotonic() >= 1.5
                              and all(change.path.startswith("sqlglot/") for change in changes)
                              and cheap_verification_group(candidates, durations, unverified)):
                            passed, collection_error, spent = replay.check(
                                changes, {names[n] for n in candidates}, 1.5,
                            )
                            cheap_budget -= spent
                            verify_wall += spent
                            if collection_error:
                                workers = all_workers
                                reason = "verification collection error"
                            elif passed:
                                workers = pack(candidates - {by_name[name] for name in passed}, durations)
                        if verbose:
                            print(f"{patch.stem}: candidates={len(candidates)} {before/1000:.3f}s",
                                  file=sys.stderr)
                except (OSError, ValueError, IndexError, KeyError, SyntaxError, RecursionError, OverflowError) as error:
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
    parser.add_argument("--index", type=Path, default=Path("/opt/dependencies.sqlite"))
    parser.add_argument("--verify", action="store_true", help="try budgeted pytest reruns")
    parser.add_argument("--check-cheap", action="store_true", help="verify small candidate groups with pytest")
    parser.add_argument("--executor-index", type=Path)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    main(args.data.resolve(), args.source.resolve(), args.index.resolve(), args.verify, args.verbose,
         args.executor_index, args.check_cheap)
