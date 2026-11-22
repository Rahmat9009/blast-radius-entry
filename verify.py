"""Run candidate tests against a patched sqlglot, in a forked child.

The parent imports pristine sqlglot and the needed test modules once. For each commit a
child is forked; it swaps the changed functions' code objects for the patched ones (only
function-body changes qualify), runs the candidate tests with unittest, and reports the
numbers of the tests that failed.
"""

import gc
import io
import json
import os
import pickle
import signal
import sys
import time
import types
import unittest

CO_OPTIMIZED = 0x1


def code_sig(co):
    consts = []
    for c in co.co_consts:
        if isinstance(c, types.CodeType):
            consts.append(("<code>", c.co_qualname, code_sig(c)))
        else:
            consts.append((type(c).__name__, repr(c)))
    return (co.co_code, co.co_names, co.co_varnames, co.co_freevars, co.co_cellvars,
            co.co_flags, co.co_argcount, co.co_kwonlyargcount, tuple(consts))


def _norm_sig(co):
    """Signature without line numbers; a class body's __firstlineno__ const is normalised."""
    consts = []
    fl = co.co_firstlineno
    is_func = bool(co.co_flags & CO_OPTIMIZED)
    for c in co.co_consts:
        if isinstance(c, types.CodeType):
            consts.append(("<code>", c.co_qualname, _norm_sig(c)))
        elif not is_func and type(c) is int and c == fl:
            consts.append(("<firstlineno>",))
        else:
            consts.append((type(c).__name__, repr(c)))
    return (co.co_code, co.co_names, co.co_varnames, co.co_freevars, co.co_cellvars,
            co.co_flags, co.co_argcount, co.co_kwonlyargcount, tuple(consts))


_CC = {}


def ccompile(text, path):
    """compile() with a cache; raises SyntaxError like compile()."""
    key = (path, hash(text), len(text))
    got = _CC.get(key)
    if got is None:
        try:
            got = compile(text, path, "exec", dont_inherit=True)
        except SyntaxError as e:
            got = e
        _CC[key] = got
    if isinstance(got, SyntaxError):
        raise got
    return got


def same_code(path, old_text, new_text):
    """True if both sources compile to the same code apart from line numbers."""
    try:
        a = ccompile(old_text, path)
        b = ccompile(new_text, path)
    except SyntaxError:
        return False
    return _norm_sig(a) == _norm_sig(b)


def walk_codes(co, out):
    out.append(co)
    for c in co.co_consts:
        if isinstance(c, types.CodeType):
            walk_codes(c, out)
    return out


def code_changes(path, old_text, new_text):
    """Return {(qualname, firstlineno): new_code} for changed function bodies, or None if a
    module/class body changed or the shapes do not line up."""
    try:
        old_co = ccompile(old_text, path)
        new_co = ccompile(new_text, path)
    except SyntaxError:
        return None
    olds = walk_codes(old_co, [])
    news = walk_codes(new_co, [])
    if len(olds) != len(news):
        return None
    changes = {}
    for o, n in zip(olds, news):
        if o.co_qualname != n.co_qualname:
            return None
        if code_sig(o) == code_sig(n):
            continue
        if not (o.co_flags & CO_OPTIMIZED):
            # module or class body: only allowed to differ through nested code objects
            if strip_sig(o) != strip_sig(n):
                return None
            continue
        changes[(o.co_qualname, o.co_firstlineno)] = n
    return changes


def strip_sig(co):
    consts = []
    for c in co.co_consts:
        if isinstance(c, types.CodeType):
            consts.append(("<code>", c.co_qualname))
        else:
            consts.append((type(c).__name__, repr(c)))
    return (co.co_code, co.co_names, co.co_varnames, co.co_freevars, co.co_cellvars,
            co.co_flags, co.co_argcount, co.co_kwonlyargcount, tuple(consts))


class _SyncExecutor:
    def __init__(self, *a, initializer=None, initargs=(), **k):
        if initializer is not None:
            initializer(*pickle.loads(pickle.dumps(initargs)))

    def submit(self, fn, *a, **k):
        from concurrent.futures import Future

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


class _Result(unittest.TestResult):
    def __init__(self):
        super().__init__()
        self.bad = set()

    def _mark(self, test):
        self.bad.add(_tid(test))
        if getattr(self, "failfast", False):
            self.shouldStop = True

    def addError(self, test, err):
        self._mark(test)

    def addFailure(self, test, err):
        self._mark(test)

    def addSubTest(self, test, subtest, err):
        if err is not None:
            self._mark(test)

    def addUnexpectedSuccess(self, test):
        self.bad.add(_tid(test))

    def addSkip(self, test, reason):
        # a test that skips here may run (and fail) in the real environment
        self._mark(test)

    def addExpectedFailure(self, test, err):
        pass


def _tid(test):
    # _ErrorHolder for setUpClass failures: mark the whole class
    if isinstance(test, unittest.suite._ErrorHolder):
        return ("class", test.description)
    return (type(test).__module__, type(test).__name__, test._testMethodName)


class Runner:
    def __init__(self, src, tests):
        self.src = src
        self.tests = tests
        self.mods = {}

    def prepare(self, files):
        os.environ["SKIP_INTEGRATION"] = "0"
        if self.src not in sys.path:
            sys.path.insert(0, self.src)
        os.chdir(self.src)
        import importlib

        for f in sorted(files):
            name = f[:-3].replace("/", ".")
            try:
                mod = importlib.import_module(name)
            except Exception as e:
                print("import failed", name, repr(e), file=sys.stderr)
                continue
            if hasattr(mod, "ProcessPoolExecutor"):
                mod.ProcessPoolExecutor = _SyncExecutor
            self.mods[f] = mod
        gc.collect()

    def can_run(self, nums):
        return all(self.tests[n]["file"] in self.mods for n in nums)

    def start(self, changes, nums, fail_fast=False):
        """Fork a child. Returns (pid, read_fd)."""
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(r)
            code = 1
            try:
                out = self._child(changes, nums, fail_fast)
                os.write(w, json.dumps(out).encode())
                code = 0
            except BaseException as e:  # noqa
                try:
                    os.write(w, json.dumps({"error": repr(e)}).encode())
                except Exception:
                    pass
            finally:
                os._exit(code)
        os.close(w)
        return pid, r

    def _child(self, changes, nums, fail_fast=False):
        sys.stdout = io.StringIO()  # never let a test print to our stdout
        import importlib

        for path in changes:
            mod = path[:-3].replace("/", ".")
            if mod.endswith(".__init__"):
                mod = mod[: -len(".__init__")]
            importlib.import_module(mod)
        # swap code objects
        wanted = {}
        for path, ch in changes.items():
            full = os.path.join(self.src, path)
            for (qn, fl), co in ch.items():
                wanted[(full, qn, fl)] = co
        found = set()
        for obj in gc.get_objects():
            if type(obj) is types.FunctionType:
                c = obj.__code__
                key = (c.co_filename, c.co_qualname, c.co_firstlineno)
                co = wanted.get(key)
                if co is not None:
                    obj.__code__ = co.replace(co_filename=c.co_filename)
                    found.add(key)
        # functions never created (e.g. not yet called factories' inner functions) are
        # fine: their parent code was swapped and creates them from the new consts.
        # Anything not found is reported so the caller can decide.
        missing = [list(k[1:]) for k in wanted if k not in found and not _nested_ok(k, wanted)]
        stems = {os.path.basename(p)[:-3] for p in changes}
        setup_heavy = {"tests/test_executor.py"}

        def order(n):
            t = self.tests[n]
            meth = t["id"].rsplit("::", 1)[-1]
            hit = any(st and st in meth for st in stems)
            # likely failures first (fail-fast), expensive class setups last; keep classes together
            return (not hit, t["file"] in setup_heavy, t["file"], t["id"])

        suite = unittest.TestSuite()
        for n in sorted(nums, key=order if fail_fast else (lambda n: self.tests[n]["id"])):
            t = self.tests[n]
            mod = self.mods[t["file"]]
            _, cls, meth = t["id"].split("::")
            suite.addTest(getattr(mod, cls)(meth))
        res = _Result()
        if fail_fast:
            res.failfast = True
        suite.run(res)
        ran = res.testsRun
        bad = []
        bad_classes = {d for d in res.bad if d[0] == "class"}
        idx = {}
        for n in nums:
            t = self.tests[n]
            _, cls, meth = t["id"].split("::")
            idx[(self.mods[t["file"]].__name__, cls, meth)] = n
        for d in res.bad:
            if d[0] == "class":
                continue
            n = idx.get(d)
            if n is not None:
                bad.append(n)
        if bad_classes:
            # setUpClass / tearDownClass error: count every test in that class as failing
            for d in bad_classes:
                for (m, cls, meth), n in idx.items():
                    if cls in d[1]:
                        bad.append(n)
        return {"bad": sorted(set(bad)), "missing": missing}


def _nested_ok(key, wanted):
    # a changed function that is nested inside another changed function needs no live object
    full, qn, fl = key
    for (f2, q2, l2) in wanted:
        if f2 == full and q2 != qn and qn.startswith(q2 + ".<locals>."):
            return True
    return False
