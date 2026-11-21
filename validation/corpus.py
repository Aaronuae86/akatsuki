"""Create and independently label deterministic, local-only mutations."""

import argparse
import ast
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import unified_diff
import hashlib
import json
import os
from pathlib import Path
from random import Random
import shutil
import signal
import subprocess
import sys
import time

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


def freeze(source, inventory, target, seed=417230, per_family=3, families=None):
    candidates = {}
    for item in (*python_mutations(source), *fixture_mutations(source)):
        if families is None or item[0] in families:
            candidates.setdefault(item[0], []).append(item)
    random = Random(seed)
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
            split = "development" if accepted < (per_family + 1) // 2 else "holdout"
            name = f"{family}-{accepted + 1}"
            diff = unified_diff(before, after, f"a/{path}", f"b/{path}")
            patch = (f"diff --git a/{path} b/{path}\n" + "".join(diff)).encode()
            apply_diff(source, patch)
            (target / split / "commits" / f"{name}.patch").write_bytes(patch)
            manifest.append({"name": name, "kind": family, "split": split, "path": path,
                             "sha256": hashlib.sha256(patch).hexdigest()})
            accepted += 1
            if accepted == per_family:
                break
        if accepted != per_family:
            raise ValueError(f"Insufficient mutations for {family}")
    (target / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"Froze {len(manifest)} independent patches before labeling.", flush=True)


def collect_outcomes(cwd, names, timeout):
    report = cwd.parent / "report.json"
    report.unlink(missing_ok=True)
    request = {"names": sorted(names), "timeout": 85, "report": str(report)}
    verifier = Path(__file__).resolve().parents[1] / "verify.py"
    if not verifier.is_file():
        verifier = Path("/app/verify.py")
    process = subprocess.Popen(
        [sys.executable, str(verifier)], cwd=cwd, stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env={**os.environ, "PYTHONPATH": str(cwd)}, start_new_session=os.name == "posix",
    )
    try:
        process.communicate(json.dumps(request).encode(), timeout=timeout)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            process.kill()
        process.wait()
        return set(), set()
    if process.returncode or not report.is_file():
        return set(), set()
    result = json.loads(report.read_text())
    failed = set(result["failed"])
    failed_files = set(result["failed_collectors"])
    failed.update(node for node in names if node.split("::", 1)[0] in failed_files)
    terminal = failed | set(result["completed"]) | set(result["skipped"])
    return failed & names, terminal & names


def label(source, target, workers, retry=False):
    manifest = json.loads((target / "manifest.json").read_text())
    status_file = target / "statuses.json"
    statuses = json.loads(status_file.read_text()) if retry and status_file.exists() else {}

    def run(case):
        split = target / case["split"]
        name = case["name"]
        location = split / "commits" / f"{name}.patch"
        if not location.exists():
            location = split / "unscored" / location.name
        patch = location.read_bytes()
        if hashlib.sha256(patch).hexdigest() != case["sha256"]:
            raise ValueError(f"Frozen patch changed: {name}")
        names = {test["id"]: test["number"] for test in json.loads((split / "tests.json").read_text())["tests"]}
        replay = Replay(source)
        try:
            with replay.tree(apply_diff(source, patch)) as cwd:
                prior = statuses.get(name, {})
                if retry and "unresolved" in prior:
                    terminal = names.keys() - set(prior["unresolved"])
                    previous_failures = set(prior["failing"])
                    failed = {node for node in terminal if names[node] in previous_failures}
                    shard_size = 20
                elif retry:
                    failed, terminal = set(), set()
                    shard_size = 120
                else:
                    failed, terminal = collect_outcomes(cwd, names.keys(), 240)
                    shard_size = 120
                remaining = sorted(names.keys() - terminal)
                retry_deadline = time.monotonic() + 360
                for start in range(0, len(remaining), shard_size):
                    budget = min(130, retry_deadline - time.monotonic())
                    if budget < 10:
                        break
                    group = set(remaining[start:start + shard_size])
                    errors, finished = collect_outcomes(cwd, group, budget)
                    failed.update(errors)
                    terminal.update(finished)
                return case, {
                    "status": "labeled" if names.keys() <= terminal else "unresolved",
                    "failing": sorted(names[test] for test in failed),
                    "unresolved": sorted(names.keys() - terminal),
                }
        finally:
            replay.close()

    pending = [case for case in manifest if statuses.get(case["name"], {}).get("status") != "labeled"]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for future in as_completed([pool.submit(run, case) for case in pending]):
            case, result = future.result()
            statuses[case["name"]] = result
            temporary = status_file.with_suffix(".tmp")
            temporary.write_text(json.dumps(statuses, indent=2))
            temporary.replace(status_file)
            print(case["name"], result["status"], len(result.get("failing", [])), flush=True)
    for group in ("development", "holdout"):
        folder = target / group
        (folder / "unscored").mkdir(exist_ok=True)
        answers = {}
        for case in manifest:
            if case["split"] != group:
                continue
            name = case["name"]
            result = statuses[name]
            active = folder / "commits" / f"{name}.patch"
            unscored = folder / "unscored" / active.name
            if result["status"] == "labeled":
                if unscored.exists():
                    unscored.replace(active)
                answers[name] = {"failing": result["failing"], "kind": case["kind"]}
            elif active.exists():
                active.replace(unscored)
        (folder / "answers.json").write_text(json.dumps({"commits": answers}, indent=2))


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("action", choices=("freeze", "label"))
    cli.add_argument("target", type=Path)
    cli.add_argument("--source", type=Path, default=Path("/opt/sqlglot"))
    cli.add_argument("--inventory", type=Path, default=Path("/data/tests.json"))
    cli.add_argument("--workers", type=int, default=2)
    cli.add_argument("--retry-unscored", action="store_true")
    cli.add_argument("--seed", type=int, default=417230)
    cli.add_argument("--per-family", type=int, default=3)
    cli.add_argument("--family", action="append", choices=("dispatch", "parser", "optimizer", "configuration", "fixture"))
    args = cli.parse_args()
    if args.action == "freeze":
        if args.per_family < 2:
            cli.error("--per-family must be at least 2")
        freeze(args.source, args.inventory, args.target, args.seed, args.per_family, args.family)
    else:
        label(args.source, args.target, args.workers, args.retry_unscored)
