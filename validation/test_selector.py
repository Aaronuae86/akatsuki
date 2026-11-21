import ast
from difflib import unified_diff
from enum import Enum
import json
import os
from pathlib import Path
import subprocess
import sys
from types import ModuleType

import pytest
import observations
import verify

from dependencies import DependencyIndex, comparison_site, guard_edit
from observations import GUARD_NAME, Reads, guard_value
from patches import apply_diff
from schedule import cost, pack


SOURCE = "sqlglot/mod.py"
PASSED = "tests/test_mod.py::test_passed"
OTHER = "tests/test_other.py::test_other"


def patch(path, old, new):
    lines = unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                         f"a/{path}", f"b/{path}")
    return (f"diff --git a/{path} b/{path}\n" + "".join(lines)).encode()


def impact(tmp_path, before, after, dependencies):
    source = tmp_path / SOURCE
    source.parent.mkdir()
    source.write_bytes(before.encode())
    diff = apply_diff(tmp_path, patch(SOURCE, before, after))
    index = DependencyIndex(
        {"tests": [PASSED, OTHER], "files": {SOURCE: dependencies}},
        [{"id": PASSED}, {"id": OTHER}],
    )
    return index.candidates(diff)


def test_tuple_boundary_uses_exact_operands(tmp_path):
    before = "def f(version):\n    return version < (23, 12)\n"
    after = before.replace("<", "<=")
    tag = f"@tuple/{SOURCE}/2:11"
    source = tmp_path / SOURCE
    source.parent.mkdir()
    source.write_bytes(before.encode())
    changes = apply_diff(tmp_path, patch(SOURCE, before, after))
    tests = [{"id": PASSED}, {"id": OTHER}]
    files = {SOURCE: {PASSED: [2], OTHER: [2]}, tag: {PASSED: ["[23, 12]"], OTHER: ["[23, 11]", "[24]"]}}
    index = DependencyIndex({"tests": [PASSED, OTHER], "files": files}, tests)
    assert index.candidates(changes).names == {PASSED}
    files[tag][OTHER].append("?")
    assert index.candidates(changes).names == {PASSED, OTHER}


def test_version_observations_include_default_maxsize(monkeypatch):
    calls = {}
    monkeypatch.setattr(observations, "CALLBACK", calls.__setitem__)
    tag = "@tuple/sqlglot/mod.py/1:0"
    observations.integer_value((sys.maxsize,), tag)
    assert json.loads(calls[tag]) == [sys.maxsize]
    observations.integer_value(("23",), tag)
    assert calls[tag] == "?"


@pytest.mark.skipif(os.name != "posix", reason="pytest-timeout uses process termination on Windows")
def test_verifier_reports_timeout_and_failed_import(tmp_path):
    (tmp_path / "test_broken.py").write_text("raise ImportError('fixture import')\n")
    (tmp_path / "test_checked.py").write_text(
        "import time\ndef test_timeout():\n    time.sleep(2)\ndef test_ok():\n    pass\n",
    )
    report = tmp_path / "result.json"
    names = ["test_broken.py::test_example", "test_checked.py::test_timeout", "test_checked.py::test_ok"]
    subprocess.run(
        [sys.executable, str(Path(verify.__file__).resolve())], cwd=tmp_path,
        input=json.dumps({"report": str(report), "names": names, "timeout": 0.1}),
        env={**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
        text=True, check=True, capture_output=True,
    )
    data = json.loads(report.read_text())
    assert data["failed"] == [names[1]]
    assert data["completed"] == [names[2]]
    assert data["failed_collectors"] == ["test_broken.py"]
    assert data["passed"] == []


def test_single_function_body_edit(tmp_path):
    original = "def f(x):\n    return x + 1\n\ndef g():\n    return 7\n"
    result = impact(tmp_path, original, original.replace("+ 1", "+ 2"), {PASSED: [2], OTHER: [5]})
    assert result.names == {PASSED}
    assert not result.fallback


@pytest.mark.parametrize("edited", [
    'def f(x):\n    """Changed docstring."""\n    return x + 1\n',
    "def f(x):\n    yield x + 1\n",
    "def f(x):\n    return x + 1\n    x = 3\n",
    'raise ImportError("simulated import failure")\ndef f(x):\n    return x + 1\n',
])
def test_structural_edits_retain_suite(tmp_path, edited):
    result = impact(tmp_path, "def f(x):\n    return x + 1\n", edited, {PASSED: [2]})
    assert result.names == {PASSED, OTHER}
    assert result.fallback


def test_comments_keep_raw_source_consumers(tmp_path):
    original = "def f(x):\n    return x + 1\n"
    result = impact(tmp_path, original, "# explanatory comment\n" + original, {PASSED: [0], OTHER: [2]})
    assert result.names == {PASSED}


def test_equivalence_requires_a_sole_operator_edit():
    old = ast.parse("x.a is None")
    assert comparison_site(old, ast.parse("x.a == None")) == "1:0"
    assert comparison_site(old, ast.parse("x.b == None")) is None
    assert comparison_site(old, ast.parse("x.a == None\ny()")) is None


def test_integer_observations_keep_changed_and_unknown_operands(tmp_path):
    from dependencies import UNKNOWN_OPERAND

    original = "def f(x):\n    return x > 3\n"
    source = tmp_path / SOURCE
    source.parent.mkdir()
    source.write_bytes(original.encode())
    diff = apply_diff(tmp_path, patch(SOURCE, original, original.replace("> 3", ">= 3")))
    tests = [{"id": name} for name in (PASSED, OTHER, "unknown")]
    index = DependencyIndex({
        "tests": [t["id"] for t in tests],
        "files": {SOURCE: {t["id"]: [2] for t in tests},
                  f"@int/{SOURCE}/2:11": {PASSED: [1, 9], OTHER: [3], "unknown": [UNKNOWN_OPERAND]}},
    }, tests)
    assert index.candidates(diff).names == {OTHER, "unknown"}


@pytest.mark.parametrize(("operator", "marker"), [("and", 0), ("or", 1)])
def test_guard_excludes_only_scopes_with_a_proven_short_circuit(tmp_path, operator, marker):
    original = "def f(x):\n    if x:\n        return 1\n    return 0\n"
    edited = original.replace("if x:", f"if x {operator} extra():")
    before, after = ast.parse(original), ast.parse(edited)
    assert guard_edit(before, after).unchanged == marker
    assert guard_edit(after, before).unchanged == marker
    assert guard_edit(after, ast.parse(edited.replace("extra()", "other()"))).unchanged == marker
    assert guard_edit(before, ast.parse(edited.replace("return 0", "return 2"))) is None
    source = tmp_path / SOURCE
    source.parent.mkdir()
    source.write_bytes(original.encode())
    diff = apply_diff(tmp_path, patch(SOURCE, original, edited))
    names = (PASSED, OTHER, "unknown", "mixed", "unobserved", "source-reader")
    index = DependencyIndex({
        "tests": names,
        "files": {
            SOURCE: {name: [2] if name != "source-reader" else [0, 2] for name in names},
            f"@guard/{SOURCE}/2:7": {
                PASSED: [marker], OTHER: [1 - marker], "unknown": [2], "mixed": [0, 1],
                "source-reader": [marker],
            },
        },
    }, [{"id": name} for name in names])
    assert index.candidates(diff).names == set(names) - {PASSED}


def test_appended_block_statement_uses_unchanged_guard_and_entry_coverage(tmp_path):
    before = "def f(x):\n    if x:\n        g(\n            x\n        )\n    return 0\n"
    after = before.replace("    return 0", "        h()\n    return 0")
    edit = guard_edit(ast.parse(before), ast.parse(after))
    assert isinstance(edit.anchor, ast.Name) and edit.anchor.id == "x" and edit.unchanged == 0
    assert impact(tmp_path, before, after, {PASSED: [2, 6], OTHER: [2, 4, 6]}).names == {OTHER}


def test_instrumented_nested_guard_preserves_custom_truthiness():
    class Truth:
        calls = 0

        def __bool__(self):
            self.calls += 1
            return True

    code = "def f(x):\n    if (x or False) and True:\n        return True\n"
    original, instrumented = {}, {GUARD_NAME: guard_value}
    exec(compile(code, SOURCE, "exec"), original)
    tree = Reads(SOURCE).visit(ast.parse(code))
    exec(compile(ast.fix_missing_locations(tree), SOURCE, "exec"), instrumented)
    a, b = Truth(), Truth()
    assert original["f"](a) == instrumented["f"](b)
    assert a.calls == b.calls == 1


def test_map_edit_keeps_whole_table_readers_and_matching_values(tmp_path):
    original = 'class Dialect:\n    TYPE_MAPPING = {"text": "TEXT", "n": "INT"}\n'
    source = tmp_path / SOURCE
    source.parent.mkdir()
    source.write_bytes(original.encode())
    diff = apply_diff(tmp_path, patch(SOURCE, original, original.replace('"TEXT"', '"VARCHAR"')))
    tag = "@attr/sqlglot.mod/Dialect/TYPE_MAPPING"
    files = {SOURCE: {"<session>": [1, 2]}, tag: {PASSED: [0]},
             tag + "/value/'TEXT'": {OTHER: [0]}, tag + "/value/'INT'": {"unaffected": [0]}}
    tests = [{"id": name} for name in (PASSED, OTHER, "unaffected")]
    index = DependencyIndex({"tests": [t["id"] for t in tests], "files": files,
                             "observed_attributes": True, "map_fields": ["TYPE_MAPPING"]}, tests)
    assert index.candidates(diff).names == {PASSED, OTHER}
    files["@eager/" + tag] = {"<session>": [0]}
    assert index.candidates(diff).names == {t["id"] for t in tests}


def test_removed_dispatch_entry_keeps_key_consumers_and_eager_fallback(tmp_path):
    original = ("class Generator:\n    TRANSFORMS = {\n"
                "        **Parent.TRANSFORMS,\n"
                "        exp.Int: lambda self, e: e,\n"
                "        exp.String: lambda self, e: e,\n    }\n")
    updated = original.replace("        exp.Int: lambda self, e: e,\n", "")
    source = tmp_path / SOURCE
    source.parent.mkdir()
    source.write_bytes(original.encode())
    diff = apply_diff(tmp_path, patch(SOURCE, original, updated))
    tag = "@attr/sqlglot.mod/Generator/TRANSFORMS"
    names = [PASSED, OTHER, "whole-table"]
    files = {SOURCE: {"<session>": [1, 2, 3]}, tag: {"whole-table": [0]},
             tag + "/key/Int": {PASSED: [0]}, tag + "/key/String": {OTHER: [0]}}
    index = DependencyIndex({
        "tests": names, "files": files, "observed_attributes": True,
        "dispatch_exports": ["Int", "String"], "dispatch_modules": ["sqlglot.mod"],
    }, [{"id": name} for name in names])
    assert index.candidates(diff).names == {PASSED, "whole-table"}
    files["@eager/" + tag] = {"<session>": [0]}
    assert index.candidates(diff).names == set(names)


def test_copied_dispatch_preserves_transitive_consumers_and_unrecognized_eager_reads(tmp_path):
    original = "class Base:\n    TRANSFORMS = {exp.Int: handler, exp.Str: other}\n"
    source = tmp_path / SOURCE
    source.parent.mkdir()
    source.write_bytes(original.encode())
    changes = apply_diff(tmp_path, patch(SOURCE, original, original.replace("exp.Int: handler, ", "")))
    base, child, leaf = [f"@attr/sqlglot.mod/{name}/TRANSFORMS" for name in ("Base", "Child", "Leaf")]
    files = {
        SOURCE: {"<session>": [1, 2]},
        "@dispatch-copy/" + base: {"<session>": [child]},
        "@dispatch-copy/" + child: {"<session>": [leaf]},
        leaf + "/key/Int": {PASSED: [0]},
        leaf + "/key/Str": {OTHER: [0]},
    }
    names = [PASSED, OTHER, "whole-table"]
    index = DependencyIndex({
        "tests": names, "files": files, "observed_attributes": True,
        "dispatch_modules": ["sqlglot.mod"], "dispatch_exports": ["Int", "Str"],
    }, [{"id": name} for name in names])
    assert index.candidates(changes).names == {PASSED}
    files[child] = {"whole-table": [0]}
    assert index.candidates(changes).names == {PASSED, "whole-table"}
    files["@eager/" + leaf] = {"<session>": [0]}
    assert index.candidates(changes).names == set(names)


def test_dispatch_spread_recognizes_only_key_preserving_copies():
    def copied(expression):
        return observations.copy_source(ast.parse(expression, mode="eval").body, "TRANSFORMS")
    assert copied("Base.TRANSFORMS") is not None
    assert copied("{k: v for k, v in Base.TRANSFORMS.items() if k not in {exp.Add}}") is not None
    assert copied("{k: wrap(v) for k, v in Base.TRANSFORMS.items()}") is None
    assert copied("{k: v for k, v in Base.TRANSFORMS.items() if v()}") is None
    inner = "{**Base.TRANSFORMS, exp.Add: handler}"
    filtered = "{k: v for k, v in " + inner + ".items() if v is not None}"
    assert isinstance(observations.filtered_dispatch_table(ast.parse(filtered, mode="eval").body), ast.Dict)
    changed_keys = filtered.replace("k: v for", "v: k for")
    assert observations.filtered_dispatch_table(ast.parse(changed_keys, mode="eval").body) is None
    changed_filter = filtered.replace("v is not None", "keep(v)")
    assert observations.filtered_dispatch_table(ast.parse(changed_filter, mode="eval").body) is None


def test_added_type_key_keeps_existing_misses_and_whole_table_readers(tmp_path):
    original = ('class Generator:\n    TYPE_MAPPING = {\n'
                '        **Parent.TYPE_MAPPING,\n        exp.DType.INT: "INT",\n    }\n')
    updated = original.replace('        exp.DType.INT', '        exp.DType.TEXT: "VARCHAR",\n        exp.DType.INT')
    source = tmp_path / SOURCE
    source.parent.mkdir()
    source.write_bytes(original.encode())
    diff = apply_diff(tmp_path, patch(SOURCE, original, updated))
    names = [PASSED, OTHER, "whole-table"]
    files = {SOURCE: {"<session>": [1, 2, 3]}, "@type-map/whole": {"whole-table": [0]},
             "@type-map/key/TEXT": {PASSED: [0]}, "@type-map/key/INT": {OTHER: [0]}}
    index = DependencyIndex({
        "tests": names, "files": files, "observed_attributes": True, "map_fields": ["TYPE_MAPPING"],
        "dtype_names": {"INT": "INT", "TEXT": "TEXT"}, "dispatch_modules": ["sqlglot.mod"],
    }, [{"id": name} for name in names])
    assert index.candidates(diff).names == {PASSED, "whole-table"}
    files["@type-map/eager/key/TEXT"] = {"<session>": [0]}
    assert index.candidates(diff).names == set(names)


def test_type_lookup_records_both_value_and_requested_key(monkeypatch):
    class DType(Enum):
        INT = "INT"
        TEXT = "TEXT"

    class Generator:
        TYPE_MAPPING = {DType.INT: "NUMBER"}

    Generator.__module__ = "sqlglot.test"
    module = ModuleType("sqlglot.expressions")
    module.DType = DType
    monkeypatch.setitem(sys.modules, module.__name__, module)
    visits = {}
    monkeypatch.setattr(observations, "CALLBACK", visits.__setitem__)
    lookup = observations.map_getter(Generator(), "TYPE_MAPPING")
    assert lookup(DType.INT) == "NUMBER"
    assert lookup(DType.TEXT, "TEXT") == "TEXT"
    assert "@type-map/key/INT" in visits and "@type-map/key/TEXT" in visits
    assert any(path.endswith("/TYPE_MAPPING/value/'NUMBER'") for path in visits)


def test_lambda_definition_and_invocation_have_separate_coverage(tmp_path):
    original = 'class Dialect:\n    table = {"fn": lambda x: x + 1}\n'
    source = tmp_path / SOURCE
    source.parent.mkdir()
    source.write_bytes(original.encode())
    diff = apply_diff(tmp_path, patch(SOURCE, original, original.replace("+ 1", "+ 2")))
    tests = [{"id": PASSED}, {"id": OTHER}]
    index = DependencyIndex({"tests": [PASSED, OTHER], "call_coverage": True,
                             "files": {SOURCE: {"<session>": [1, 2]},
                                       "@lambda/" + SOURCE: {PASSED: [2]}}}, tests)
    assert index.candidates(diff).names == {PASSED}


def test_offset_hunk_uses_pinned_lines(tmp_path):
    source = tmp_path / SOURCE
    source.parent.mkdir()
    original = "a = 1\nb = 2\nc = 3\n"
    source.write_bytes(("# inserted in pinned source\n" + original).encode())
    [changed] = apply_diff(tmp_path, patch(SOURCE, original, original.replace("b = 2", "b = 9")))
    assert changed.removed == {3}
    assert changed.updated == b"# inserted in pinned source\na = 1\nb = 9\nc = 3\n"


def test_rejects_unmatched_context(tmp_path):
    source = tmp_path / SOURCE
    source.parent.mkdir()
    source.write_bytes(b"different\n")
    with pytest.raises(ValueError):
        apply_diff(tmp_path, patch(SOURCE, "a = 1\n", "a = 2\n"))


def test_scheduler_accounts_for_worker_startup():
    durations = {0: 28_817, 1: 2_200, 2: 1_500, 3: 0}
    workers = pack(durations, durations)
    assert len(workers) == 2
    assert cost(workers, durations) == 32_817
    assert sorted(number for worker in workers for number in worker) == list(durations)
    assert pack(set(), durations) == []
    assert pack({1, 2}, durations) == [[1, 2]]


def test_verifier_keeps_subtest_and_teardown_failures(tmp_path):
    (tmp_path / "test_report.py").write_text(
        "import unittest\n"
        "class TestSubtests(unittest.TestCase):\n"
        "    def test_subtest_failure(self):\n"
        "        with self.subTest(case=1):\n"
        "            self.assertEqual(1, 2)\n"
        "    def test_pass(self):\n"
        "        pass\n"
        "class TestTeardown(unittest.TestCase):\n"
        "    def test_teardown_failure(self):\n"
        "        pass\n"
        "    def tearDown(self):\n"
        "        self.fail('teardown')\n",
        encoding="utf-8",
    )
    report = tmp_path / "report.json"
    passed = "test_report.py::TestSubtests::test_pass"
    names = [
        passed, "test_report.py::TestSubtests::test_subtest_failure",
        "test_report.py::TestTeardown::test_teardown_failure",
    ]
    subprocess.run(
        [sys.executable, str(Path(verify.__file__).resolve())], cwd=tmp_path,
        input=json.dumps({"report": str(report), "names": names, "timeout": 10}),
        env={**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
        text=True, check=True, capture_output=True,
    )
    assert json.loads(report.read_text())["passed"] == [passed]
