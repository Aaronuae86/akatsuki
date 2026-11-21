"""Score three container runs, including CPU charged to every process."""

import argparse
import json
from pathlib import Path
from statistics import median
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from score import score_run


CPU_WRAPPER = """
from pathlib import Path
import subprocess, sys

subprocess.run([sys.executable, '/app/selector.py', *sys.argv[1:]], check=True)
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
"""


def run(image, data, selector_flags, runs, wall_limit, target):
    tests = json.loads((data / "tests.json").read_text())["tests"]
    answers = json.loads((data / "answers.json").read_text())["commits"]
    command = [
        "docker", "run", "--rm", "--platform", "linux/amd64", "--network", "none",
        "--cpus", "2", "--memory", "4096m", "--memory-swap", "4096m", "--pids-limit", "256",
        "--read-only", "--tmpfs", "/tmp:rw,size=64m,mode=1777", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges", "--user", "65534:65534", "--env", "HOME=/tmp",
        "--volume", f"{data / 'commits'}:/data/commits:ro",
        "--volume", f"{data / 'tests.json'}:/data/tests.json:ro",
        "--entrypoint", "python", image, "-c", CPU_WRAPPER, *selector_flags,
    ]
    totals = []
    for i in range(runs):
        start = time.monotonic()
        output = subprocess.run(command, capture_output=True, timeout=wall_limit, check=False)
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
        result = score_run(output.stdout, tests, answers)
        if not result["ok"] or result["misses"]:
            raise RuntimeError(f"sample failed: {result}\n{logs}")
        simulated = result["simulated_milliseconds"]
        total = simulated * 1000 + cpu
        totals.append(total)
        usage = ""
        if "memory.peak" in peaks:
            usage += f", peak memory {peaks['memory.peak'] / (1024 * 1024):.1f} MiB"
        if "pids.peak" in peaks:
            usage += f", peak PIDs {peaks['pids.peak']}"
        print(f"Run {i + 1}: simulated {simulated / 1000:.3f} s, CPU {cpu / 1e6:.6f} s, "
              f"total {total / 1e6:.6f} s, wall {wall:.3f} s, "
              f"stdout {len(output.stdout)} bytes, 0 misses{usage}", flush=True)
    middle = int(median(totals))
    rounded = ((middle + 50_000) // 100_000) / 10
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
