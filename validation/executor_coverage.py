"""Measure which optimizer lines each real executor subtask reaches on the pinned tree."""

import argparse
from concurrent.futures import ProcessPoolExecutor
import json
import os
from pathlib import Path
import sys
import time
from tempfile import TemporaryDirectory


TOOL = 3
EXECUTOR_TEST = "tests/test_executor.py::TestExecutor::test_execute_tpcds"


def start_trace(root):
    files = {}
    prefix = str(root / "sqlglot" / "optimizer") + os.sep

    def line(code, number):
        files[code.co_filename].add(number)
        return sys.monitoring.DISABLE

    def enter(code, offset):
        if code.co_filename.startswith(prefix):
            files.setdefault(code.co_filename, set())
            sys.monitoring.set_local_events(TOOL, code, sys.monitoring.events.LINE)
        return sys.monitoring.DISABLE

    sys.monitoring.use_tool_id(TOOL, "executor query coverage")
    sys.monitoring.register_callback(TOOL, sys.monitoring.events.PY_START, enter)
    sys.monitoring.register_callback(TOOL, sys.monitoring.events.LINE, line)
    sys.monitoring.set_events(TOOL, sys.monitoring.events.PY_START)
    sys.monitoring.restart_events()
    return files


def finish_trace(root, files, output, elapsed):
    sys.monitoring.set_events(TOOL, 0)
    sys.monitoring.free_tool_id(TOOL)
    output.write_text(json.dumps({
        "cpu": elapsed,
        "files": {Path(path).relative_to(root).as_posix(): sorted(lines) for path, lines in files.items()},
    }))


def run_query(root, output, fn, args, kwargs):
    files = start_trace(root)
    start = time.process_time()
    try:
        return fn(*args, **kwargs)
    finally:
        finish_trace(root, files, output, time.process_time() - start)


def collect(root, target):
    import pytest

    target.mkdir(parents=True, exist_ok=False)
    os.chdir(root)
    sys.path.insert(0, str(root))
    original = ProcessPoolExecutor.submit
    count = 0

    def submit(pool, fn, /, *args, **kwargs):
        nonlocal count
        if fn.__module__ != "tests.test_executor" or fn.__name__ != "mp_execute":
            raise RuntimeError(f"Unexpected pool function {fn}")
        output = target / f"query-{count}.json"
        count += 1
        return original(pool, run_query, root, output, fn, args, kwargs)

    ProcessPoolExecutor.submit = submit
    files = start_trace(root)
    start = time.process_time()
    try:
        status = pytest.main(["-q", "-p", "no:cacheprovider", EXECUTOR_TEST])
    finally:
        ProcessPoolExecutor.submit = original
        finish_trace(root, files, target / "parent.json", time.process_time() - start)
    if status or len(list(target.glob("query-*.json"))) != count or not count:
        raise RuntimeError(f"Incomplete query coverage, pytest status {status}")


def index(root, output):
    with TemporaryDirectory() as temporary:
        target = Path(temporary) / "trace"
        collect(root, target)
        parent = json.loads((target / "parent.json").read_bytes())
        jobs = [json.loads(p.read_bytes()) for p in sorted(
            target.glob("query-*.json"), key=lambda p: int(p.stem.split("-")[-1]),
        )]
        output.write_text(json.dumps({"parent": parent, "jobs": jobs}, separators=(",", ":")))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", type=Path)
    parser.add_argument("--source", type=Path, default=Path("/opt/sqlglot"))
    parser.add_argument("--index", action="store_true")
    opts = parser.parse_args()
    action = index if opts.index else collect
    action(opts.source.resolve(), opts.target.resolve())
