"""Run selected pytest tests on the complete, independently patched tree."""

from collections import defaultdict
from concurrent.futures import Future, ProcessPoolExecutor
import json
from multiprocessing import get_context
from pathlib import Path
import sys
import time

import pytest


EXECUTOR_TEST = "tests/test_executor.py::TestExecutor::test_execute_tpcds"


class QueryReplay:
    def __init__(self, jobs, count):
        self.jobs = set(jobs)
        self.count = count
        self.seen = 0
        self.original = ProcessPoolExecutor.submit
        self.module = None

    def __enter__(self):
        def submit(pool, fn, /, *args, **kwargs):
            if (fn.__module__, fn.__name__) != ("tests.test_executor", "mp_execute"):
                raise RuntimeError("Unexpected executor task")
            index = self.seen
            self.seen += 1
            if index >= self.count:
                raise RuntimeError("Executor submitted more queries than the baseline")
            if index in self.jobs:
                return self.original(pool, fn, *args, **kwargs)
            future = Future()
            future.set_result(None)
            return future

        ProcessPoolExecutor.submit = submit
        return self

    def __exit__(self, *error):
        ProcessPoolExecutor.submit = self.original
        if self.module is not None:
            self.module.get_context = self.get_context


class Verification:
    def __init__(self, names, query_replay=None):
        self.names = set(names)
        self.phases = defaultdict(set)
        self.failed = set()
        self.errors = set()
        self.skipped = set()
        self.failed_collectors = set()
        self.collection_error = False
        self.query_replay = query_replay

    def pytest_collection_modifyitems(self, config, items):
        rejected = [item for item in items if item.nodeid not in self.names]
        items[:] = [item for item in items if item.nodeid in self.names]
        if rejected:
            config.hook.pytest_deselected(items=rejected)

    def pytest_runtestloop(self, session):
        if self.query_replay and sys.platform == "linux":
            module = sys.modules.get("tests.test_executor")
            if module is not None:
                self.query_replay.module = module
                self.query_replay.get_context = module.get_context
                module.get_context = lambda method: get_context("fork")

    def pytest_collectreport(self, report):
        if report.failed:
            self.collection_error = True
            self.failed_collectors.add(report.nodeid)

    def pytest_runtest_logreport(self, report):
        if report.failed or report.skipped:
            self.failed.add(report.nodeid)
        if report.failed:
            self.errors.add(report.nodeid)
        if report.skipped:
            self.skipped.add(report.nodeid)
        if report.passed and not hasattr(report, "context"):
            self.phases[report.nodeid].add(report.when)

    def proven_passing(self):
        required = {"setup", "call", "teardown"}
        return sorted(
            name for name in self.names - self.failed
            if self.phases[name] >= required
        )


def run(names, timeout, query_replay=None):
    start = time.process_time()
    checks = Verification(names, query_replay)
    files = sorted({name.split("::", 1)[0] for name in names})
    status = pytest.main(
        ["-q", "--tb=no", "-p", "no:cacheprovider", "-p", "pytest_timeout",
         "-o", f"timeout={timeout}", "--continue-on-collection-errors", *files],
        plugins=[checks],
    )
    result = {
        "passed": checks.proven_passing() if status in (0, 1) and not checks.collection_error else [],
        "collection_error": checks.collection_error,
        "completed": checks.proven_passing(),
        "failed": sorted(checks.errors),
        "skipped": sorted(checks.skipped - checks.errors),
        "failed_collectors": sorted(checks.failed_collectors),
        "status": int(status),
        "cpu": time.process_time() - start,
    }
    return result


if __name__ == "__main__":
    request = json.load(sys.stdin)
    if "query_jobs" in request:
        with QueryReplay(request["query_jobs"], request["query_count"]) as replay:
            response = run([EXECUTOR_TEST], request["timeout"], replay)
        if replay.seen != replay.count:
            response["passed"] = []
    else:
        response = run(request["names"], request["timeout"])
    Path(request["report"]).write_text(json.dumps(response), encoding="utf-8")
