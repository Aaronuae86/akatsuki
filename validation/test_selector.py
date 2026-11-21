import ast
from difflib import unified_diff
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from dependencies import DependencyIndex, comparison_site
from patches import apply_diff
from schedule import cost, pack


SOURCE = "sqlglot/mod.py"
PASSED = "tests/test_mod.py::test_passed"
OTHER = "tests/test_other.py::test_other"
ROOT = Path(__file__).resolve().parents[1]


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
        [sys.executable, str(ROOT / "verify.py")], cwd=tmp_path,
        input=json.dumps({"report": str(report), "names": names, "timeout": 10}),
        env={**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
        text=True, check=True, capture_output=True,
    )
    assert json.loads(report.read_text())["passed"] == [passed]
