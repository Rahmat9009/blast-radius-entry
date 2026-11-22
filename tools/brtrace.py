"""pytest plugin: per-test line coverage (sys.monitoring), opened files, dialects used.

Writes one JSON file per process into $BRTRACE_OUT.
"""
import builtins
import io
import json
import os
import sys

import pytest

M = sys.monitoring
TOOL = 4
ROOT = os.path.abspath(os.environ.get("BRTRACE_ROOT", ".")) + os.sep
OUT = os.environ["BRTRACE_OUT"]

_cur = {"lines": set(), "files": set(), "dialects": set()}
_phase = "collect"
_data = {}
_files_cache = {}
_cur_types = set()


def _rel(fn):
    r = _files_cache.get(fn)
    if r is None:
        if fn.startswith(ROOT):
            r = fn[len(ROOT):]
        else:
            r = ""
        _files_cache[fn] = r
    return r


def _on_line(code, line):
    r = _rel(code.co_filename)
    if r:
        _cur["lines"].add((r, line))
    return M.DISABLE


M.use_tool_id(TOOL, "brtrace")
M.register_callback(TOOL, M.events.LINE, _on_line)
M.set_events(TOOL, M.events.LINE)

_orig_open = builtins.open


def _open(file, *a, **k):
    try:
        p = os.path.abspath(os.fspath(file))
        if p.startswith(ROOT):
            _cur["files"].add(p[len(ROOT):])
    except Exception:
        pass
    return _orig_open(file, *a, **k)


builtins.open = _open
io.open = _open


def _start(phase):
    global _phase
    _phase = phase
    _cur["lines"] = set()
    _cur["files"] = set()
    _cur["dialects"] = set()
    _cur_types.clear()
    M.restart_events()


def _flush(key):
    d = _data.setdefault(key, {"lines": set(), "files": set(), "dialects": set()})
    d["lines"] |= _cur["lines"]
    d["files"] |= _cur["files"]
    d["dialects"] |= _cur["dialects"]
    d["dialects"] |= {"E:" + t.__module__ + "." + t.__qualname__ for t in _cur_types}


def _hook_dialect():
    try:
        from sqlglot.dialects.dialect import Dialect
    except Exception:
        return
    orig = Dialect.__init__
    if getattr(orig, "_br", False):
        return

    def init(self, *a, **k):
        for c in type(self).__mro__:
            _cur["dialects"].add(c.__module__ + "." + c.__qualname__)
        return orig(self, *a, **k)

    init._br = True
    Dialect.__init__ = init
    try:
        from sqlglot.expressions.core import Expr
    except Exception:
        return
    eorig = Expr.__init__
    types = _cur_types

    def einit(self, *a, **k):
        types.add(type(self))
        return eorig(self, *a, **k)

    Expr.__init__ = einit


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    _flush("__collect__")
    _hook_dialect()
    _start("test")
    yield
    _flush(item.nodeid)
    _start("between")


def pytest_sessionfinish(session):
    _flush("__collect__")
    wid = os.environ.get("PYTEST_XDIST_WORKER", "main")
    out = {}
    for k, v in _data.items():
        lines = {}
        for f, l in v["lines"]:
            lines.setdefault(f, []).append(l)
        out[k] = {
            "lines": {f: sorted(ls) for f, ls in lines.items()},
            "files": sorted(v["files"]),
            "dialects": sorted(v["dialects"]),
        }
    os.makedirs(OUT, exist_ok=True)
    with _orig_open(os.path.join(OUT, f"trace-{wid}-{os.getpid()}.json"), "w") as fh:
        json.dump(out, fh)


class _SyncExecutor:
    """In-process stand-in for ProcessPoolExecutor so child work is traced."""

    def __init__(self, *a, initializer=None, initargs=(), **k):
        import pickle
        if initializer is not None:
            initializer(*pickle.loads(pickle.dumps(initargs)))

    def submit(self, fn, *a, **k):
        from concurrent.futures import Future

        import pickle
        f = Future()
        try:
            a, k = pickle.loads(pickle.dumps((a, k)))
            f.set_result(pickle.loads(pickle.dumps(fn(*a, **k))))
        except BaseException as e:  # noqa
            f.set_exception(e)
        return f

    def map(self, fn, *its, **k):
        return map(fn, *its)

    def shutdown(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def pytest_collection_finish(session):
    # import every sqlglot module now, so import-time lines are attributed to collection
    import importlib
    import pkgutil

    try:
        import sqlglot

        for info in pkgutil.walk_packages(sqlglot.__path__, prefix="sqlglot."):
            if info.name == "sqlglot.__main__":
                continue
            try:
                importlib.import_module(info.name)
            except Exception:
                pass
    except Exception:
        pass
    for name, mod in list(sys.modules.items()):
        if name.startswith("tests") and hasattr(mod, "ProcessPoolExecutor"):
            mod.ProcessPoolExecutor = _SyncExecutor
