"""Record pinned-tree test dependencies, including work done in spawned pools."""

import argparse
import atexit
from concurrent.futures import ProcessPoolExecutor
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

import pytest
import observations


TOOL = 3
SESSION = "<session>"
ROOT = None
DIRECTORY = None
owner = SESSION
active = False
by_owner = {}
paths = {}
original_submit = ProcessPoolExecutor.submit


def relative(filename):
    path = paths.get(filename)
    if path is None:
        path = Path(filename)
        if not path.is_absolute():
            path = ROOT / path
        try:
            path = os.path.relpath(path, ROOT).replace(os.sep, "/")
        except ValueError:
            path = ""
        paths[filename] = path
    return path


def current():
    return by_owner.setdefault(owner, {})


def attribute(path, marker):
    values = current().setdefault(path, set())
    if path.startswith("@int/") and len(values) >= 16:
        values.add(observations.UNKNOWN_OPERAND)
    else:
        values.add(marker)


def line(code, number):
    path = relative(code.co_filename)
    current().setdefault(path, set()).add(number)
    return sys.monitoring.DISABLE


def start_code(code, offset):
    path = relative(code.co_filename)
    if path.startswith(("sqlglot/", "tests/")):
        if code.co_flags & 1:  # CO_OPTIMIZED distinguishes function bodies from class/module suites.
            current().setdefault("@calls/" + path, set()).add(code.co_firstlineno)
        if origin := observations.ORIGINS.get(id(code)):
            current().setdefault("@factory-call/" + origin[1], set()).add(0)
        if code.co_name == "<lambda>":
            current().setdefault("@lambda/" + path, set()).add(code.co_firstlineno)
        sys.monitoring.set_local_events(TOOL, code, sys.monitoring.events.LINE)
    return sys.monitoring.DISABLE


def audit(event, args):
    if not active or event not in ("open", "os.listdir", "os.scandir"):
        return
    filename = args[0]
    if isinstance(filename, (str, bytes, os.PathLike)):
        path = relative(os.fsdecode(filename))
        if path.startswith("tests/fixtures"):
            current().setdefault(path, set()).add(0)
        elif event == "open" and path.startswith(("sqlglot/", "tests/")) and path.endswith(".py"):
            # Import source reads are already represented by module-body coverage.
            # Other callers can observe file contents without executing them.
            frame = sys._getframe(1)
            while frame:
                caller = frame.f_code.co_filename
                if caller.startswith(("<frozen importlib", "<frozen zipimport")):
                    break
                if relative(caller).startswith(("sqlglot/", "tests/")):
                    current().setdefault(path, set()).add(0)
                    break
                frame = frame.f_back


def setup(root, directory):
    global ROOT, DIRECTORY, active
    ROOT = Path(root)
    DIRECTORY = Path(directory)
    active = True
    observations.CALLBACK = attribute
    sys.monitoring.use_tool_id(TOOL, "blast-radius baseline")
    sys.monitoring.register_callback(TOOL, sys.monitoring.events.PY_START, start_code)
    sys.monitoring.register_callback(TOOL, sys.monitoring.events.LINE, line)
    sys.monitoring.set_events(TOOL, sys.monitoring.events.PY_START)
    sys.addaudithook(audit)


def set_owner(next_owner):
    global owner
    if next_owner != owner:
        owner = next_owner
        sys.monitoring.restart_events()


def flush_worker():
    data = {
        name: {path: sorted(lines) for path, lines in dependencies.items()}
        for name, dependencies in by_owner.items()
    }
    if data:
        (DIRECTORY / f"{os.getpid()}.json").write_text(json.dumps(data), encoding="utf-8")


def worker_call(fn, args, kwargs, node_id, root, directory):
    if not active:
        setup(root, directory)
        atexit.register(flush_worker)
    set_owner(node_id)
    return fn(*args, **kwargs)


def tracked_submit(pool, fn, /, *args, **kwargs):
    return original_submit(pool, worker_call, fn, args, kwargs, owner, str(ROOT), str(DIRECTORY))


class Coverage:
    def __init__(self):
        self.results = {}
        self.collected = []

    def pytest_collection_finish(self, session):
        self.collected = [item.nodeid for item in session.items]

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_protocol(self, item, nextitem):
        set_owner(item.nodeid)
        current()
        try:
            yield
        finally:
            set_owner(SESSION)

    @pytest.hookimpl(wrapper=True)
    def pytest_fixture_setup(self, fixturedef, request):
        preceding = owner
        if fixturedef.scope != "function":
            set_owner(request.node.nodeid or SESSION)
        try:
            return (yield)
        finally:
            set_owner(preceding)

    def pytest_runtest_logreport(self, report):
        if report.when == "call" and report.passed and not getattr(report, "context", None):
            self.results.setdefault(report.nodeid, True)
        if report.failed or report.skipped:
            self.results[report.nodeid] = False


def build(root, output):
    global active
    os.chdir(root)
    sys.path.insert(0, str(root))
    observations.install(root)
    profiler = Coverage()
    with TemporaryDirectory(prefix="blast-index-") as directory:
        setup(root, directory)
        ProcessPoolExecutor.submit = tracked_submit
        try:
            status = pytest.main(
                ["-q", "--assert=plain", "-o", "cache_dir=" + directory + "/cache", "tests"],
                plugins=[profiler],
            )
        finally:
            ProcessPoolExecutor.submit = original_submit
            active = False
            observations.CALLBACK = None
            sys.monitoring.set_events(TOOL, 0)
            sys.monitoring.free_tool_id(TOOL)
        for part in Path(directory).glob("*.json"):
            for name, dependencies in json.loads(part.read_text(encoding="utf-8")).items():
                target = by_owner.setdefault(name, {})
                for path, lines in dependencies.items():
                    target.setdefault(path, set()).update(lines)
    if status != 0 or not profiler.collected:
        raise RuntimeError(f"baseline profiling did not finish cleanly (pytest status {status})")
    index = {
        "schema": 1, "complete": status == 0, "tests": profiler.collected,
        "observed_attributes": True, "numeric_lookups": True, "call_coverage": True,
        "scalar_fields": sorted(observations.SCALAR_FIELDS),
        "map_fields": sorted(observations.MAP_FIELDS),
        "function_calls": True,
        "dispatch_exports": sorted(name for names in observations.DISPATCH_EXPORTS.values() for name in names),
        "dispatch_modules": sorted(
            name for name, module in sys.modules.copy().items()
            if name.startswith("sqlglot.") and module is not None
            and vars(module).get("exp") is sys.modules.get("sqlglot.expressions")
        ),
        "dtype_names": {
            name: member.name for name, member in sys.modules["sqlglot.expressions"].DType.__members__.items()
        },
    }
    index["unverified"] = [name for name in profiler.collected if not profiler.results.get(name)]
    # A file-first layout keeps runtime queries local to the changed files.
    files = {}
    for name, dependencies in by_owner.items():
        for path, lines in dependencies.items():
            files.setdefault(path, {})[name] = sorted(lines)
    index["files"] = files
    output.write_text(json.dumps(index, separators=(",", ":"), sort_keys=True), encoding="utf-8")
    print(f"Indexed {len(profiler.collected)} tests across {len(files)} files, pytest status {status}")


if __name__ == "__mp_main__" and "BLAST_PROFILE_SOURCE" in os.environ:
    observations.install(os.environ["BLAST_PROFILE_SOURCE"])

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    build(args.root.resolve(), args.output.resolve())
