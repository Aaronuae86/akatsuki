"""Budget and isolate pytest verification jobs."""

from contextlib import contextmanager
from importlib.util import cache_from_source
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
from tempfile import TemporaryDirectory
import time


IGNORE = shutil.ignore_patterns(".git", ".pytest_cache")
VERIFIER = Path(__file__).with_name("verify.py").resolve()


def symlink_file(source, destination):
    os.symlink(source, destination)


class Replay:
    def __init__(self, root):
        self.root = root
        self.workspace = None

    @contextmanager
    def tree(self, changes):
        if self.workspace is None:
            self.workspace = TemporaryDirectory(prefix="blast-replay-", ignore_cleanup_errors=True)
            self.worktree = Path(self.workspace.name) / "repo"
            copier = symlink_file if os.name == "posix" else shutil.copyfile
            shutil.copytree(self.root, self.worktree, ignore=IGNORE, copy_function=copier)
        written = []
        cached = []
        try:
            for change in changes:
                if change.original == change.updated:
                    continue
                target = self.worktree / change.path
                written.append(change)
                if target.suffix == ".py":
                    bytecode = Path(cache_from_source(str(target)))
                    if bytecode.exists():
                        cached.append(bytecode)
                        bytecode.unlink()
                if target.exists() or target.is_symlink():
                    target.unlink()
                if change.updated is not None:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(change.updated)
            yield self.worktree
        finally:
            for change in reversed(written):
                target = self.worktree / change.path
                if target.exists() or target.is_symlink():
                    target.unlink()
                if change.original is not None:
                    if os.name == "posix":
                        symlink_file(self.root / change.path, target)
                    else:
                        target.write_bytes(change.original)
            for bytecode in cached:
                source = self.root / bytecode.relative_to(self.worktree)
                (symlink_file if os.name == "posix" else shutil.copyfile)(source, bytecode)

    def close(self):
        if self.workspace is not None:
            self.workspace.cleanup()

    def check(self, changes, names, budget, **options):
        report = None
        start = time.monotonic()
        try:
            with self.tree(changes) as cwd:
                report = cwd.parent / "result.json"
                report.unlink(missing_ok=True)
                request = {"names": sorted(names), "report": str(report),
                           "timeout": max(1.0, budget - 0.4), **options}
                env = {
                    **os.environ,
                    "PYTHONPATH": str(cwd),
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "PYTHON_CPU_COUNT": "2",
                    "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
                    "SKIP_INTEGRATION": "0",
                    "OPENBLAS_NUM_THREADS": "1",
                    "OMP_NUM_THREADS": "1",
                    "MKL_NUM_THREADS": "1",
                }
                process = subprocess.Popen(
                    [sys.executable, "-B", str(VERIFIER)], cwd=cwd, env=env,
                    stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=os.name == "posix",
                )
                try:
                    process.communicate(json.dumps(request).encode("utf-8"), timeout=budget)
                except subprocess.TimeoutExpired:
                    if os.name == "posix":
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    else:
                        subprocess.run(
                            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
                        )
                        if process.poll() is None:
                            process.kill()
                    process.communicate()
                    return set(), False, time.monotonic() - start
                if process.returncode or not report.is_file():
                    return set(), False, time.monotonic() - start
                data = json.loads(report.read_text(encoding="utf-8"))
                return names & set(data["passed"]), data["collection_error"], time.monotonic() - start
        except (OSError, ValueError, KeyError):
            return set(), False, time.monotonic() - start
        finally:
            if report is not None:
                report.unlink(missing_ok=True)
