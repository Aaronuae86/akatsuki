"""Score three container runs, including CPU charged to every process."""

import argparse
import json
from pathlib import Path
from statistics import median
import subprocess
import sys
import time
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from score import evaluate_run  # noqa: E402


CPU_WRAPPER = """
from pathlib import Path
import subprocess, sys

status = subprocess.run(sys.argv[1:], check=False).returncode
stat = Path('/sys/fs/cgroup/cpu.stat')
if stat.is_file():
    usage = int(dict(line.split() for line in stat.read_text().splitlines())['usage_usec'])
else:
    usage = int(Path('/sys/fs/cgroup/cpuacct/cpuacct.usage').read_text()) // 1000
print(f'blast_benchmark_cpu_usec={usage}', file=sys.stderr)
for metric in ('memory.peak', 'pids.peak'):
    path = stat.parent / metric
    if path.is_file():
        print(f'blast_benchmark_{metric}={path.read_text().strip()}', file=sys.stderr)
sys.exit(status)
"""


def image_command(image):
    inspection = subprocess.run(["docker", "image", "inspect", image], check=True, capture_output=True)
    details = json.loads(inspection.stdout)[0]
    config = details["Config"]
    entry = (config["Entrypoint"] or []) + (config["Cmd"] or [])
    if not entry:
        raise RuntimeError(f"{image} has no Docker command")
    return details["Id"], entry


def capture(image, data, selector_flags, runs, wall_limit):
    tests = json.loads((data / "tests.json").read_text())["tests"]
    answers = json.loads((data / "answers.json").read_text())["commits"]
    patches = {path.stem for path in (data / "commits").glob("*.patch")}
    if not patches or patches != answers.keys():
        raise ValueError(f"{data} must contain a nonempty batch and exactly one label per patch")
    image_id, entry = image_command(image)
    container = f"blast-radius-local-{uuid4().hex}"
    command = [
        "docker", "run", "--rm", "--name", container, "--platform", "linux/amd64", "--network", "none",
        "--cpus", "2", "--memory", "4096m", "--memory-swap", "4096m", "--pids-limit", "256",
        "--read-only", "--tmpfs", "/tmp:rw,size=64m,mode=1777", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges", "--user", "65534:65534", "--env", "HOME=/tmp",
        "--volume", f"{data / 'commits'}:/data/commits:ro",
        "--volume", f"{data / 'tests.json'}:/data/tests.json:ro",
        "--entrypoint", "python", image_id, "-c", CPU_WRAPPER, *entry, *selector_flags,
    ]
    for _ in range(runs):
        start = time.monotonic()
        try:
            output = subprocess.run(command, capture_output=True, timeout=wall_limit, check=False)
        except subprocess.TimeoutExpired:
            subprocess.run(["docker", "rm", "-f", container], capture_output=True, timeout=10, check=False)
            raise RuntimeError(f"{image} exceeded the {wall_limit:g} s wall limit") from None
        wall = time.monotonic() - start
        logs = output.stderr.decode(errors="replace")
        if output.returncode or len(output.stdout) > 2_000_000:
            raise RuntimeError(f"invalid container exit or stdout size\n{logs}")
        prefix = "blast_benchmark_cpu_usec="
        [cpu] = [int(line.removeprefix(prefix)) for line in logs.splitlines() if line.startswith(prefix)]
        peaks = {}
        for metric in ("memory.peak", "pids.peak"):
            prefix = f"blast_benchmark_{metric}="
            peaks.update({metric: int(line.removeprefix(prefix))
                          for line in logs.splitlines() if line.startswith(prefix)})
        if peaks.get("memory.peak", 0) > 4096 * 1024 * 1024 or peaks.get("pids.peak", 0) > 256:
            raise RuntimeError(f"container exceeded a resource limit: {peaks}")
        result = evaluate_run(output.stdout, tests, answers)
        if not result["ok"]:
            raise RuntimeError(f"scorer rejected {image}: {result}\n{logs}")
        simulated = result["simulated_milliseconds"]
        total = simulated * 1000 + cpu
        yield {
            "image_id": image_id,
            "simulated_ms": simulated, "cpu_us": cpu, "total_us": total,
            "wall_seconds": wall, "peaks": peaks, "stdout": output.stdout,
            "missed_commits": result["missed_commits"],
        }


def rounded_score(microseconds):
    return ((microseconds + 50_000) // 100_000) / 10


def run(image, data, selector_flags, runs, wall_limit, target):
    totals = []
    for i, result in enumerate(capture(image, data, selector_flags, runs, wall_limit), 1):
        if result["missed_commits"]:
            raise RuntimeError(f"sample failed: {result['missed_commits']}")
        peaks = result["peaks"]
        totals.append(result["total_us"])
        usage = ""
        if "memory.peak" in peaks:
            usage += f", peak memory {peaks['memory.peak'] / (1024 * 1024):.1f} MiB"
        if "pids.peak" in peaks:
            usage += f", peak PIDs {peaks['pids.peak']}"
        print(f"Run {i}: simulated {result['simulated_ms'] / 1000:.3f} s, CPU {result['cpu_us'] / 1e6:.6f} s, "
              f"total {result['total_us'] / 1e6:.6f} s, wall {result['wall_seconds']:.3f} s, "
              f"stdout {len(result['stdout'])} bytes, 0 misses{usage}", flush=True)
    rounded = rounded_score(median(totals))
    print(f"Median score: {rounded:.1f} s")
    if target is not None and rounded >= target:
        raise RuntimeError(f"median is above the strict {target:g} s target")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="blast-radius-entry")
    parser.add_argument("--data", type=Path, default=Path("sample"))
    parser.add_argument("--runs", type=int, choices=(1, 3, 5), default=3)
    parser.add_argument("--wall", type=float, default=180)
    parser.add_argument("--target", type=float)
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    flags = ["--verify"] if args.verify else []
    run(args.image, args.data.resolve(), flags, args.runs, args.wall, args.target)
