"""Generate independent mutations and compare selection with full pytest runs."""

import argparse
from difflib import unified_diff
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory

APP = Path(os.environ.get("BLAST_APPLICATION", "/app"))
sys.path.insert(0, str(APP))
from patches import apply_diff  # noqa: E402
from replay import Replay  # noqa: E402


def make_patch(source, relative, old, new):
    before = (source / relative).read_text(encoding="utf-8")
    if before.count(old) != 1:
        raise ValueError(f"mutation anchor is ambiguous in {relative}")
    after = before.replace(old, new)
    diff = unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True),
                        f"a/{relative}", f"b/{relative}")
    return (f"diff --git a/{relative} b/{relative}\n" + "".join(diff)).encode()


def run(source, inventory, verify):
    mutations = {
        "optimizer-body": (
            "sqlglot/optimizer/normalize_identifiers.py",
            "if isinstance(node, exp.Identifier):",
            "if isinstance(node, exp.Identifier) and node.quoted:",
        ),
        "numeric-config": (
            "sqlglot/dialects/bigquery.py",
            '"x": (16, 2, 2, 0xFF),',
            '"x": (10, 2, 2, 0xFF),',
        ),
        "collection-failure": (
            "sqlglot/optimizer/normalize_identifiers.py",
            "from __future__ import annotations\n",
            'from __future__ import annotations\n\nraise ImportError("held-out validation")\n',
        ),
    }
    names = {test["id"]: test["number"] for test in json.loads(inventory.read_text())["tests"]}
    with TemporaryDirectory(prefix="blast-holdout-") as temporary:
        root = Path(temporary)
        (root / "commits").mkdir()
        shutil.copyfile(inventory, root / "tests.json")
        for name, mutation in mutations.items():
            (root / "commits" / f"{name}.patch").write_bytes(make_patch(source, *mutation))
        command = [sys.executable, str(APP / "selector.py"), "--data", str(root), "--source", str(source)]
        if verify:
            command.append("--verify")
        selected = subprocess.run(command, check=True, capture_output=True, text=True, timeout=180)
        results = {
            line["commit"]: {number for worker in line["workers"] for number in worker}
            for line in map(json.loads, selected.stdout.splitlines())
        }
        replay = Replay(source)
        try:
            for name in mutations:
                patch = (root / "commits" / f"{name}.patch").read_bytes()
                with replay.tree(apply_diff(source, patch)) as cwd:
                    report = root / f"{name}.json"
                    request = {"names": sorted(names), "timeout": 100, "report": str(report)}
                    result = subprocess.run(
                        [sys.executable, str(APP / "verify.py")], cwd=cwd,
                        input=json.dumps(request), text=True, capture_output=True,
                        env={**os.environ, "PYTHONPATH": str(cwd)}, timeout=210,
                    )
                    if result.returncode or not report.is_file():
                        raise RuntimeError(f"full suite did not finish for {name}\n{result.stderr}")
                    status = json.loads(report.read_text())
                    if name == "collection-failure":
                        assert status["collection_error"], status
                        assert results[name] == set(names.values())
                        print(f"{name}: full test inventory retained for collection error", flush=True)
                    else:
                        assert not status["collection_error"] and status["status"] in (0, 1), status
                        failing = set(names) - set(status["passed"])
                        assert failing, f"mutation {name} did not break a test"
                        omitted = sorted(test for test in failing if names[test] not in results[name])
                        assert not omitted, f"{name} omitted {len(omitted)} failing tests: {omitted}"
                        print(f"{name}: {len(failing)} affected tests, {len(results[name])} selected, "
                              "zero omissions", flush=True)
        finally:
            replay.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("/opt/sqlglot"))
    parser.add_argument("--inventory", type=Path, default=Path("/data/tests.json"))
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    run(args.source, args.inventory, args.verify)
