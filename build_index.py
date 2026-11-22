"""Index coverage and fixture reads on the unmodified SQLGlot checkout."""

import builtins
import collections
from concurrent.futures import ProcessPoolExecutor
import gzip
import io
import json
from multiprocessing.util import Finalize
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

import coverage
import pytest


worker_coverage = None


def trace_worker(test_name, function, args, kwargs):
    global worker_coverage
    if worker_coverage is None:
        data_file = os.environ["TEST_SCOPE_COVERAGE"]
        worker_coverage = coverage.Coverage(
            source=["sqlglot"], config_file=False, data_file=data_file, data_suffix=True
        )
        worker_coverage.start()
        Finalize(None, worker_coverage.save, exitpriority=10)
    worker_coverage.switch_context(test_name)
    return function(*args, **kwargs)


def main():
    root = Path.cwd().resolve()
    fixtures = root / "tests" / "fixtures"
    output = Path(sys.argv[1]).resolve()
    reads = collections.defaultdict(set)
    temporary = TemporaryDirectory(prefix="test-scope-coverage-")
    data_file = str(Path(temporary.name) / ".coverage")
    os.environ["TEST_SCOPE_COVERAGE"] = data_file
    measured = coverage.Coverage(source=["sqlglot"], config_file=False, data_file=data_file)

    class ObserveTests:
        current = ""

        @pytest.hookimpl(hookwrapper=True, tryfirst=True)
        def pytest_runtest_protocol(self, item, nextitem):
            self.current = item.nodeid
            measured.switch_context(item.nodeid)
            try:
                yield
            finally:
                self.current = ""

    hooks = ObserveTests()
    submit = ProcessPoolExecutor.submit

    def submit_with_context(pool, function, /, *args, **kwargs):
        return submit(pool, trace_worker, hooks.current, function, args, kwargs)

    def record_reads(original):
        def open_file(file, *args, **kwargs):
            if isinstance(file, (str, os.PathLike)) and "fixtures" in os.fspath(file):
                path = Path(file).resolve()
                if path.is_relative_to(fixtures):
                    reads[path.relative_to(root).as_posix()].add(hooks.current)
            return original(file, *args, **kwargs)
        return open_file

    builtin_open, io_open = builtins.open, io.open
    builtins.open = record_reads(builtin_open)
    io.open = record_reads(io_open)
    ProcessPoolExecutor.submit = submit_with_context
    measured.start()
    try:
        status = pytest.main(["-q", "--tb=short", "-p", "no:cacheprovider", "tests"], plugins=[hooks])
    finally:
        measured.stop()
        builtins.open, io.open = builtin_open, io_open
        ProcessPoolExecutor.submit = submit
    if status not in {pytest.ExitCode.OK, pytest.ExitCode.TESTS_FAILED}:
        raise SystemExit(f"SQLGlot test collection failed: {status}")

    measured.combine()
    data = measured.get_data()
    node_ids = sorted(data.measured_contexts() | {name for names in reads.values() for name in names})
    ids = {name: index for index, name in enumerate(node_ids)}
    lines = {}
    for path in sorted(data.measured_files()):
        relpath = Path(path).resolve().relative_to(root).as_posix()
        lines[relpath] = {
            str(line): sorted(ids[name] for name in names)
            for line, names in data.contexts_by_lineno(path).items()
        }
    index = {
        "node_ids": node_ids,
        "lines": lines,
        "fixtures": {path: sorted(ids[name] for name in names) for path, names in reads.items()},
    }
    output.write_bytes(gzip.compress(json.dumps(index, separators=(",", ":")).encode(), mtime=0))


if __name__ == "__main__":
    main()
