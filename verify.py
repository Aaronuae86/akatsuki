"""Run selected pytest tests on the complete, independently patched tree."""

from collections import defaultdict
import json
from pathlib import Path
import sys
import time

import pytest


class Verification:
    def __init__(self, names):
        self.names = set(names)
        self.phases = defaultdict(set)
        self.failed = set()
        self.collection_error = False

    def pytest_collection_modifyitems(self, config, items):
        rejected = [item for item in items if item.nodeid not in self.names]
        items[:] = [item for item in items if item.nodeid in self.names]
        if rejected:
            config.hook.pytest_deselected(items=rejected)

    def pytest_collectreport(self, report):
        if report.failed:
            self.collection_error = True

    def pytest_runtest_logreport(self, report):
        if report.failed or report.skipped:
            self.failed.add(report.nodeid)
        if report.passed and not hasattr(report, "context"):
            self.phases[report.nodeid].add(report.when)

    def proven_passing(self):
        required = {"setup", "call", "teardown"}
        return sorted(
            name for name in self.names - self.failed
            if self.phases[name] >= required
        )


def run(names, timeout):
    start = time.process_time()
    checks = Verification(names)
    files = sorted({name.split("::", 1)[0] for name in names})
    status = pytest.main(
        ["-q", "--tb=no", "-p", "no:cacheprovider", "-p", "pytest_timeout",
         "-o", f"timeout={timeout}", "--continue-on-collection-errors", *files],
        plugins=[checks],
    )
    result = {
        "passed": checks.proven_passing() if status in (0, 1) and not checks.collection_error else [],
        "collection_error": checks.collection_error,
        "status": int(status),
        "cpu": time.process_time() - start,
    }
    return result


if __name__ == "__main__":
    request = json.load(sys.stdin)
    response = run(request["names"], request["timeout"])
    Path(request["report"]).write_text(json.dumps(response), encoding="utf-8")
