"""Report selection and scheduling costs for a labeled, local-only batch."""

import argparse
from collections import defaultdict
import json
from math import floor
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from schedule import WORKER_COST, cost, pack  # noqa: E402
from score import evaluate_run  # noqa: E402


def lower_bound(failing, durations):
    if not failing:
        return 0
    largest = max(durations[number] for number in failing)
    total = sum(durations[number] for number in failing)
    return min(
        k * WORKER_COST + max(largest, (total + k - 1) // k)
        for k in range(1, min(8, len(failing)) + 1)
    )


def audit(data, output):
    tests = json.loads((data / "tests.json").read_text())["tests"]
    answers = json.loads((data / "answers.json").read_text())["commits"]
    stdout = output.read_bytes()
    result = evaluate_run(stdout, tests, answers)
    if not result["ok"]:
        raise ValueError(f"Invalid selector output: {result}")
    durations = {test["number"]: floor(test["seconds"] * 1000 + 0.5) for test in tests}
    names = {test["number"]: test["id"] for test in tests}
    details = []
    totals = defaultdict(lambda: [0, 0, 0])
    for raw in stdout.splitlines():
        row = json.loads(raw)
        commit = row["commit"]
        answer = answers[commit]
        family = answer.get("kind", "unknown")
        workers = row["workers"]
        selected = {number for worker in workers for number in worker}
        failing = set(answer["failing"])
        configured = cost([worker for worker in workers if worker], durations)
        repacked = cost(pack(selected, durations), durations)
        oracle = cost(pack(failing, durations), durations)
        bound = lower_bound(failing, durations)
        totals[family][0] += configured
        totals[family][1] += oracle
        totals[family][2] += bound
        expensive = sorted(selected - failing, key=lambda n: (-durations[n], n))[:3]
        details.append({
            "commit": commit, "family": family, "selected": len(selected),
            "failing": len(failing), "missing": sorted(failing - selected),
            "scheduled_ms": configured, "repacked_ms": repacked,
            "failing_only_ms": oracle, "lower_bound_ms": bound,
            "passing_tests": [{"id": names[n], "ms": durations[n]} for n in expensive],
        })
    return {
        "misses": result["missed_commits"],
        "families": {
            family: dict(zip(("scheduled_ms", "failing_only_ms", "lower_bound_ms"), values))
            for family, values in sorted(totals.items())
        },
        "patches": sorted(details, key=lambda row: (
            row["failing_only_ms"] - row["scheduled_ms"], row["commit"],
        )),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("data", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    print(json.dumps(audit(args.data, args.output), indent=2))
