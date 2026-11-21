"""Measure an optimizer-only replay on the executor fixture inputs."""

import argparse
import json
from pathlib import Path
import subprocess
import sys

from holdout import Replay, apply_diff


PROBE = """
import json
import time
from sqlglot import parse_one
from sqlglot.optimizer import optimize
from sqlglot.schema import ensure_schema
from tests.helpers import TPCDS_SCHEMA, load_sql_fixture_pairs

start = time.process_time()
results = []
for meta, sql, _ in load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql"):
    if not meta.get("execute"):
        continue
    parsed = parse_one(sql)
    schema = ensure_schema(TPCDS_SCHEMA)
    try:
        optimized = optimize(parsed, schema, leave_tables_isolated=True)
        results.append({"output": optimized.dump(), "input_after": parsed.dump()})
    except Exception as error:
        results.append({"exception": type(error).__name__, "message": str(error)})
print(json.dumps({"queries": results, "cpu": time.process_time() - start}, default=repr))
"""


def run(root):
    result = subprocess.run([sys.executable, "-B", "-c", PROBE], cwd=root,
                            capture_output=True, check=True, text=True, timeout=90)
    return json.loads(result.stdout)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("patch", type=Path)
    parser.add_argument("--source", type=Path, default=Path("/opt/sqlglot"))
    args = parser.parse_args()
    changes = apply_diff(args.source, args.patch.read_bytes())
    if not changes or any(not change.path.startswith("sqlglot/optimizer/") for change in changes):
        parser.error("probe requires an optimizer-only patch")
    replay = Replay(args.source)
    try:
        before = run(args.source)
        with replay.tree(changes) as patched:
            after = run(patched)
    finally:
        replay.close()
    print(json.dumps({
        "queries": len(before["queries"]),
        "changed": [i for i, (a, b) in enumerate(zip(before["queries"], after["queries"], strict=True)) if a != b],
        "baseline_cpu_seconds": before["cpu"], "patched_cpu_seconds": after["cpu"],
    }, indent=2))
