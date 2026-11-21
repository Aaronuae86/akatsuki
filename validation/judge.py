"""Compare two Docker images using the contest scorer and labeled local batches."""

import argparse
import json
from pathlib import Path
import subprocess
import sys

from audit import audit_stdout
from benchmark import capture, rounded_score


def trial_summary(results, data):
    middle = sorted(results, key=lambda row: row["total_us"])[len(results) // 2]
    audit = audit_stdout(data, middle["stdout"])
    audit["missing_by_run"] = []
    tests = json.loads((data / "tests.json").read_text())["tests"]
    names = {test["number"]: test["id"] for test in tests}
    for index, result in enumerate(results, 1):
        if not result["missed_commits"]:
            continue
        report = audit if result is middle else audit_stdout(data, result["stdout"])
        audit["missing_by_run"].append({
            "run": index,
            "commits": {
                row["commit"]: [names[number] for number in row["missing"]]
                for row in report["patches"] if row["missing"]
            },
        })
    return {
        "score": rounded_score(middle["total_us"]),
        "trials": [{key: value for key, value in row.items() if key != "stdout"} for row in results],
        "median": {key: value for key, value in middle.items() if key != "stdout"},
        "audit": audit,
    }


def compare(data, images, runs, wall):
    output = {"data": str(data), "images": images, "results": {}, "errors": {}}
    results = {name: [] for name in images}
    roles = tuple(images)
    for index in range(runs):
        for role in roles if index % 2 == 0 else reversed(roles):
            if role in output["errors"]:
                continue
            try:
                image = results[role][0]["image_id"] if results[role] else images[role]
                results[role].extend(capture(image, data, [], 1, wall))
            except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as error:
                output["errors"][role] = str(error)
    for role in roles:
        if role not in output["errors"]:
            output["results"][role] = trial_summary(results[role], data)
    return output


def print_comparison(report, target):
    print(f"\n{report['data']}")
    for role, error in report["errors"].items():
        print(f"  {role}: rejected or unavailable — {error}", file=sys.stderr)
    failed = bool(report["errors"])
    for role, result in report["results"].items():
        row = result["median"]
        misses = [len(trial["missed_commits"]) for trial in result["trials"]]
        print(f"  {role:9} {result['score']:7.1f} s | simulated {row['simulated_ms']/1000:.3f} s"
              f" | CPU {row['cpu_us']/1e6:.3f} s | misses by run {misses}")
        for failure in result["audit"]["missing_by_run"]:
            for commit, tests in failure["commits"].items():
                print(f"    Run {failure['run']}, missed commit {commit}: {', '.join(tests)}")
        failed |= role == "candidate" and any(misses)
    base = report["results"].get("baseline")
    candidate = report["results"].get("candidate")
    if not candidate:
        return failed
    patches = candidate["audit"]["patches"]
    bound = sum(row["lower_bound_ms"] for row in patches) / 1000
    print(f"  Zero-miss lower bound before CPU: {bound:.3f} s across {len(patches)} patches")
    if base:
        delta = (base["median"]["total_us"] - candidate["median"]["total_us"]) / 1e6
        before = {row["commit"]: row["scheduled_ms"] for row in base["audit"]["patches"]}
        changes = [(row["commit"], before[row["commit"]] - row["scheduled_ms"]) for row in patches]
        print(f"  Improvement including CPU: {delta:+.3f} s; schedule savings: {sum(x[1] for x in changes)/1000:+.3f} s")
        for commit, change in sorted(changes, key=lambda row: (-abs(row[1]), row[0]))[:5]:
            if change:
                print(f"    {commit}: {change/1000:+.3f} s schedule savings")
    if target is not None:
        failed |= candidate["score"] >= target
        print(f"  Strict local target < {target:g} s: {'FAIL' if failed else 'PASS'}")
    return failed


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--batch", action="append", required=True, type=Path)
    parser.add_argument("--mode", choices=("practice", "final"), default="practice")
    parser.add_argument("--runs", type=int, choices=(1, 3, 5))
    parser.add_argument("--target", type=float, help="strict score threshold for one labeled batch")
    parser.add_argument("--report", type=Path, help="save all trial summaries and per-patch audits as JSON")
    args = parser.parse_args()
    if args.target is not None and len(args.batch) != 1:
        parser.error("--target requires exactly one --batch")
    runs = args.runs or (3 if args.mode == "practice" else 5)
    wall = 180 if args.mode == "practice" else 270
    images = {"baseline": args.baseline, "candidate": args.candidate}
    reports = []
    failed = False
    for batch in args.batch:
        report = compare(batch.resolve(), images, runs, wall)
        reports.append(report)
        failed |= print_comparison(report, args.target)
        sys.stdout.flush()
    if args.report:
        args.report.write_text(json.dumps(reports, indent=2), encoding="utf-8")
    sys.exit(int(failed))
