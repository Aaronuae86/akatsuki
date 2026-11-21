"""Create and independently label deterministic, local-only mutations."""

import argparse
import ast
from concurrent.futures import ThreadPoolExecutor
from difflib import unified_diff
import hashlib
import json
import os
from pathlib import Path
from random import Random
import shutil
import subprocess
import sys

from holdout import Replay, apply_diff


def python_mutations(source):
    for file in sorted((source / "sqlglot").rglob("*.py")):
        path = file.relative_to(source).as_posix()
        text = file.read_text(encoding="utf-8")
        lines = text.splitlines(keepends=True)
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if isinstance(node, ast.If) and (path == "sqlglot/parser.py" or "/optimizer/" in path):
                test = node.test
                if test.lineno != test.end_lineno or not lines[test.lineno - 1].isascii():
                    continue
                line = test.lineno - 1
                old = lines[line]
                after = old[:test.col_offset] + "not (" + old[test.col_offset:test.end_col_offset] + ")" + old[test.end_col_offset:]
                family = "parser" if path == "sqlglot/parser.py" else "optimizer"
                yield family, path, line, 1, [after]
            if not isinstance(node, (ast.ClassDef, ast.Module)):
                continue
            for stmt in node.body:
                if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
                    target = stmt.targets[0]
                elif isinstance(stmt, ast.AnnAssign):
                    target = stmt.target
                else:
                    continue
                if not isinstance(target, ast.Name) or not target.id.isupper():
                    continue
                value = stmt.value
                if isinstance(value, ast.Constant) and type(value.value) is bool:
                    old = lines[value.lineno - 1]
                    if old.isascii():
                        after = old[:value.col_offset] + str(not value.value) + old[value.end_col_offset:]
                        yield "configuration", path, value.lineno - 1, 1, [after]
                if isinstance(value, ast.Dict) and target.id in {"TRANSFORMS", "TYPE_MAPPING", "FUNCTIONS"}:
                    for key, item in zip(value.keys, value.values):
                        if (key and key.lineno == item.end_lineno
                            and lines[key.lineno - 1].strip().endswith(",")
                            and lines[key.lineno - 1].lstrip().startswith(ast.unparse(key) + ":")):
                            yield "dispatch", path, key.lineno - 1, 1, []


def fixture_mutations(source):
    for file in sorted((source / "tests" / "fixtures" / "optimizer").glob("*.sql")):
        path = file.relative_to(source).as_posix()
        for i, line in enumerate(file.read_text(encoding="utf-8").splitlines(keepends=True)):
            if line.strip() in {"TRUE;", "FALSE;"}:
                after = line.replace("TRUE", "FALSE") if "TRUE" in line else line.replace("FALSE", "TRUE")
                yield "fixture", path, i, 1, [after]


def freeze(source, inventory, target):
    candidates = {}
    for item in (*python_mutations(source), *fixture_mutations(source)):
        candidates.setdefault(item[0], []).append(item)
    random = Random(417230)
    for split in ("development", "holdout"):
        (target / split / "commits").mkdir(parents=True, exist_ok=False)
        shutil.copyfile(inventory, target / split / "tests.json")
    manifest = []
    for family, choices in sorted(candidates.items()):
        random.shuffle(choices)
        accepted = 0
        for _, path, line, count, new in choices:
            before = (source / path).read_text(encoding="utf-8").splitlines(keepends=True)
            after = before[:line] + new + before[line + count:]
            if path.endswith(".py"):
                try:
                    compile("".join(after), path, "exec")
                except SyntaxError:
                    continue
            split = "development" if accepted < 2 else "holdout"
            name = f"{family}-{accepted + 1}"
            diff = unified_diff(before, after, f"a/{path}", f"b/{path}")
            patch = (f"diff --git a/{path} b/{path}\n" + "".join(diff)).encode()
            apply_diff(source, patch)
            (target / split / "commits" / f"{name}.patch").write_bytes(patch)
            manifest.append({"name": name, "kind": family, "split": split, "path": path,
                             "sha256": hashlib.sha256(patch).hexdigest()})
            accepted += 1
            if accepted == 3:
                break
        if accepted != 3:
            raise ValueError(f"Insufficient mutations for {family}")
    (target / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"Froze {len(manifest)} independent patches before labeling.", flush=True)


def label(source, target, workers):
    manifest = json.loads((target / "manifest.json").read_text())

    def run(case):
        split = target / case["split"]
        name = case["name"]
        patch = (split / "commits" / f"{name}.patch").read_bytes()
        if hashlib.sha256(patch).hexdigest() != case["sha256"]:
            raise ValueError(f"Frozen patch changed: {name}")
        names = {test["id"]: test["number"] for test in json.loads((split / "tests.json").read_text())["tests"]}
        replay = Replay(source)
        try:
            with replay.tree(apply_diff(source, patch)) as cwd:
                report = cwd.parent / "report.json"
                request = {"names": sorted(names), "timeout": 85, "report": str(report)}
                try:
                    run = subprocess.run(
                        [sys.executable, "/app/verify.py"], cwd=cwd, input=json.dumps(request),
                        env={**os.environ, "PYTHONPATH": str(cwd)}, capture_output=True, text=True, timeout=240,
                    )
                except subprocess.TimeoutExpired:
                    return case, {"status": "timeout"}
                if run.returncode or not report.is_file():
                    return case, {"status": "interrupted", "exit": run.returncode}
                result = json.loads(report.read_text())
                if result["collection_error"] or result["status"] not in (0, 1):
                    return case, {"status": "collection" if result["collection_error"] else "interrupted"}
                if "Timeout" in run.stdout or "Timeout" in run.stderr:
                    return case, {"status": "test_timeout"}
                return case, {"status": "labeled",
                              "failing": sorted(names[test] for test in names.keys() - set(result["passed"]))}
        finally:
            replay.close()

    statuses = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for case, result in pool.map(run, manifest):
            statuses[case["name"]] = result
            print(case["name"], result["status"], len(result.get("failing", [])), flush=True)
    (target / "statuses.json").write_text(json.dumps(statuses, indent=2))
    for group in ("development", "holdout"):
        folder = target / group
        (folder / "unscored").mkdir(exist_ok=True)
        answers = {}
        for case in manifest:
            if case["split"] != group:
                continue
            name = case["name"]
            result = statuses[name]
            if result["status"] == "labeled":
                answers[name] = {"failing": result["failing"], "kind": case["kind"]}
            else:
                (folder / "commits" / f"{name}.patch").replace(folder / "unscored" / f"{name}.patch")
        (folder / "answers.json").write_text(json.dumps({"commits": answers}, indent=2))


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("action", choices=("freeze", "label"))
    cli.add_argument("target", type=Path)
    cli.add_argument("--source", type=Path, default=Path("/opt/sqlglot"))
    cli.add_argument("--inventory", type=Path, default=Path("/data/tests.json"))
    cli.add_argument("--workers", type=int, default=2)
    args = cli.parse_args()
    if args.action == "freeze":
        freeze(args.source, args.inventory, args.target)
    else:
        label(args.source, args.target, args.workers)
