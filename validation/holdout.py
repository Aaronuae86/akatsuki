"""Generate independent mutations and compare selection with full pytest runs."""

import argparse
import ast
from difflib import unified_diff
import json
import os
from pathlib import Path
from random import Random
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


def blind_mutation(source, family):
    """Draw a mutation from the pinned tree without consulting coverage or labels."""
    candidates = []
    for path in sorted((source / "sqlglot").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines(keepends=True)
        for node in ast.walk(ast.parse(text)):
            if family == "blind-flag" and isinstance(node, ast.ClassDef):
                for stmt in node.body:
                    if (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1
                        and isinstance(stmt.targets[0], ast.Name) and stmt.targets[0].id.isupper()
                        and isinstance(stmt.value, ast.Constant) and type(stmt.value.value) is bool):
                        old = lines[stmt.lineno - 1]
                        new = old.replace(str(stmt.value.value), str(not stmt.value.value), 1)
                        if new != old and text.count(old) == 1:
                            candidates.append((path.relative_to(source).as_posix(), old, new))
            elif family == "blind-threshold" and isinstance(node, ast.Compare) and len(node.ops) == 1:
                rhs = node.comparators[0]
                if isinstance(rhs, ast.Constant) and type(rhs.value) is int and 0 <= rhs.value <= 32:
                    old = lines[rhs.lineno - 1]
                    if old.isascii() and rhs.lineno == rhs.end_lineno and text.count(old) == 1:
                        new = old[:rhs.col_offset] + str(rhs.value + 1) + old[rhs.end_col_offset:]
                        candidates.append((path.relative_to(source).as_posix(), old, new))
    return Random(20261121).choice(candidates)


def run(source, inventory, verify, case=None, export=None):
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
    if case:
        extra = {
            "new-scalar": ("sqlglot/dialects/dremio.py", "SUPPORTS_USER_DEFINED_TYPES = False",
                           "SUPPORTS_USER_DEFINED_TYPES = True"),
            "sort-order": ("sqlglot/dialects/dremio.py", 'NULL_ORDERING = "nulls_are_last"',
                           'NULL_ORDERING = "nulls_are_small"'),
            "one-line-lambda": ("sqlglot/generators/sqlite.py", 'self.func("RANDOM", e.this)',
                                'self.func("RANDOM2", e.this)'),
            "factory-string": ("sqlglot/generators/sqlite.py", 'exp.If: rename_func("IIF")',
                               'exp.If: rename_func("IF")'),
            "nested-factory": ("sqlglot/generators/dremio.py", '_date_delta_sql("DATE_SUB")',
                               '_date_delta_sql("DATE_ADD")'),
            "test-body": ("tests/dialects/test_dremio.py",
                          'self.validate_identity("CAST(x AS SMALLINT)", "CAST(x AS INT)")',
                          'self.validate_identity("CAST(x AS SMALLINT)", "CAST(x AS FLOAT)")'),
            "threshold": ("sqlglot/parsers/bigquery.py", "len(table_parts) > 1 and table_parts[-2]",
                          "len(table_parts) > 2 and table_parts[-2]"),
            "type-table": ("sqlglot/generators/dremio.py", 'exp.DType.BINARY: "VARBINARY"',
                           'exp.DType.BINARY: "VARCHAR"'),
            "new-local": ("sqlglot/generators/dremio.py", 'if expression.text("kind") == "AS OF":',
                          'kind = expression.text("kind")\n        if kind == "AT":'),
        }
        mutations = {case: blind_mutation(source, case) if case.startswith("blind-") else (mutations | extra)[case]}
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
                        if case is None:
                            assert failing, f"mutation {name} did not break a test"
                        omitted = sorted(test for test in failing if names[test] not in results[name])
                        assert not omitted, f"{name} omitted {len(omitted)} failing tests: {omitted}"
                        print(f"{name}: {len(failing)} affected tests, {len(results[name])} selected, "
                              "zero omissions", flush=True)
                        if export:
                            (export / "commits").mkdir(parents=True, exist_ok=True)
                            (export / "commits" / f"{name}.patch").write_bytes(patch)
                            (export / f"{name}.json").write_text(json.dumps(sorted(names[n] for n in failing)))
        finally:
            replay.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("/opt/sqlglot"))
    parser.add_argument("--inventory", type=Path, default=Path("/data/tests.json"))
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--case")
    parser.add_argument("--export", type=Path)
    args = parser.parse_args()
    run(args.source, args.inventory, args.verify, args.case, args.export)
