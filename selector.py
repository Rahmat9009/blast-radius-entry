"""Blast Radius selector.

For each patch, find the tests that can reach the changed code, using maps built from
the pinned sqlglot commit (per-test line coverage, files each test opens, and which
dialect / expression classes each test uses). Then split them over workers.
"""

import ast
import json
import os
import pickle
import re
import sys
import time
from pathlib import Path

DATA = Path(os.environ.get("BR_DATA", "/data"))
SRC = Path(os.environ.get("BR_SRC", "/app/sqlglot"))
MAP = Path(os.environ.get("BR_MAP", "/app/map.pkl"))
WORKER_COST = 2000  # ms


def log(*a):
    print(*a, file=sys.stderr)


# ---------------------------------------------------------------- patch parsing

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def parse_patch(text):
    """Return list of (old_path, new_path, hunks); hunk = (old_start, [(tag, line)])."""
    files = []
    cur = None
    hunk = None
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("diff --git "):
            cur = {"old": None, "new": None, "hunks": []}
            files.append(cur)
            hunk = None
        elif ln.startswith("--- ") and (hunk is None or hunk_done(hunk)):
            if cur is None:
                cur = {"old": None, "new": None, "hunks": []}
                files.append(cur)
            p = ln[4:].split("\t")[0]
            cur["old"] = None if p == "/dev/null" else p[2:] if p.startswith("a/") else p
            hunk = None
        elif ln.startswith("+++ ") and (hunk is None or hunk_done(hunk)):
            p = ln[4:].split("\t")[0]
            cur["new"] = None if p == "/dev/null" else p[2:] if p.startswith("b/") else p
        elif ln.startswith("@@"):
            m = HUNK.match(ln)
            if m and cur is not None:
                hunk = {
                    "old_start": int(m.group(1)),
                    "old_len": int(m.group(2)) if m.group(2) is not None else 1,
                    "new_len": int(m.group(4)) if m.group(4) is not None else 1,
                    "lines": [],
                    "o": 0,
                    "n": 0,
                }
                cur["hunks"].append(hunk)
        elif hunk is not None and not hunk_done(hunk):
            if ln.startswith("\\"):
                pass
            elif ln.startswith("+"):
                hunk["lines"].append(("+", ln[1:]))
                hunk["n"] += 1
            elif ln.startswith("-"):
                hunk["lines"].append(("-", ln[1:]))
                hunk["o"] += 1
            else:
                hunk["lines"].append((" ", ln[1:] if ln.startswith(" ") else ln))
                hunk["o"] += 1
                hunk["n"] += 1
        elif hunk is not None and ln.startswith("\\"):
            pass
        i += 1
    return files


def hunk_done(h):
    return h["o"] >= h["old_len"] and h["n"] >= h["new_len"]


def apply_and_changes(old_text, hunks):
    """Apply hunks to old text. Return (new_text, changed_old_lines, insert_points)."""
    old = old_text.split("\n")
    out = []
    pos = 0  # 0-based index into old
    changed = set()
    for h in hunks:
        start = h["old_start"] - 1 if h["old_len"] > 0 else h["old_start"]
        out.extend(old[pos:start])
        pos = start
        removed_run = False
        for tag, text in h["lines"]:
            if tag == " ":
                out.append(old[pos] if pos < len(old) else text)
                pos += 1
                removed_run = False
            elif tag == "-":
                changed.add(pos + 1)
                pos += 1
                removed_run = True
            else:
                out.append(text)
                if not removed_run:
                    # pure insertion between old line pos and pos+1 (1-based)
                    changed.add(-(pos))
    out.extend(old[pos:])
    return "\n".join(out), changed


# ---------------------------------------------------------------- AST helpers

class Info:
    def __init__(self, tree):
        self.tree = tree
        # list of (node, parents)
        self.stmts = []
        self._walk(tree, [])

    def _walk(self, node, parents):
        for field in ("body", "orelse", "finalbody", "handlers", "cases"):
            for child in getattr(node, field, []) or []:
                if isinstance(child, (ast.stmt, ast.excepthandler, ast.match_case)):
                    if hasattr(child, "lineno"):
                        self.stmts.append((child, parents))
                    self._walk(child, parents + [child])

    def innermost(self, line):
        best = None
        for node, parents in self.stmts:
            s = node.lineno
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.decorator_list:
                s = min(d.lineno for d in node.decorator_list)
            e = getattr(node, "end_lineno", node.lineno)
            if s <= line <= e:
                if best is None or (e - s) <= (best[2] - best[1]):
                    best = (node, s, e, parents)
        return best


def body_start(node):
    for field in ("body", "handlers", "orelse", "finalbody", "cases"):
        b = getattr(node, field, None)
        if b:
            return min(getattr(x, "lineno", 10**9) for x in b)
    return None


# ---------------------------------------------------------------- selection


class Selector:
    def __init__(self, data, tests):
        self.sets = data["sets"]
        self.line_map = data["line_map"]
        self.file_tests = data["file_tests"]
        self.cls_tests = data["cls_tests"]
        self.mod_tests = data["mod_tests"]
        self.spans = data.get("spans", {})
        self.import_lines = {f: set(v) for f, v in data.get("import_lines", {}).items()}
        self.tests = tests
        self._mention_cache = {}
        self.info_cache = {}
        self.by_file = {}
        for t in tests:
            self.by_file.setdefault(t["file"], []).append(t["number"])
        self.notes = []

    def lines_tests(self, path, lo, hi):
        lm = self.line_map.get(path)
        res = set()
        if not lm:
            return res
        for l in range(lo, hi + 1):
            i = lm.get(l)
            if i is not None:
                res.update(self.sets[i])
        return res

    def select(self, patch_text):
        sel = set()
        for f in parse_patch(patch_text):
            sel |= self.select_file(f)
        return sel

    def select_file(self, f):
        path = f["old"] or f["new"]
        if path is None:
            return set()
        if not path.endswith(".py"):
            return set(self.file_tests.get(path, ())) | self.dir_readers(path)
        readers = set(self.file_tests.get(path, ())) | set(self.file_tests.get(f["new"] or "", ()))
        r = self._select_py(f, path)
        return r | readers

    def _select_py(self, f, path):
        src = SRC / path
        if f["old"] is None or not src.exists():
            # new python file: tests importing it would be listed by module; be broad
            self.notes.append(("newfile", path))
            return set()
        old_text = self.read_src(path)
        new_text, changed = apply_and_changes(old_text, f["hunks"])
        from verify import same_code

        if same_code(path, old_text, new_text):
            return set()
        from verify import ccompile

        try:
            ccompile(new_text, path)
        except SyntaxError:
            # file cannot be imported any more: every test that imports it fails
            self.notes.append(("syntax", path))
            return self.importers(path)
        try:
            info = self.file_info(path)
        except SyntaxError:
            return set()
        res = set()
        nlines = old_text.count("\n") + 1
        for c in changed:
            if c > 0:
                pts = [c]
            else:
                # insertion after old line -c: look at both neighbours
                pts = [x for x in (-c, -c + 1) if 1 <= x <= nlines]
            for line in pts:
                res |= self.for_line(path, info, line, insertion=c <= 0)
        return res

    def dir_readers(self, path):
        return set()

    def importers(self, path):
        return set(self.mod_tests.get(path, ())) | set(self.by_file.get(path, ()))

    def for_line(self, path, info, line, insertion=False):
        hit = info.innermost(line)
        if hit is None:
            # outside any statement (blank/comment at module level)
            return set()
        node, s, e, parents = hit
        bs = body_start(node)
        if bs is not None and line >= bs:
            # inside a compound statement but not in a child statement (e.g. else:)
            lo, hi = s, e
        elif bs is not None:
            lo, hi = s, bs - 1
        else:
            lo, hi = s, e
        chain = parents + [node]
        func = None
        for p in reversed(parents):
            if isinstance(p, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                func = p
                break
        if func is not None:
            # runtime code inside a function body
            r = self.lines_tests(path, lo, hi)
            il = self.import_lines.get(path)
            if il and any(l in il for l in range(lo, hi + 1)):
                # this code also runs at import time (factories, metaclasses): anything built
                # from it then is used by tests that never run this line themselves
                outer_i = next(
                    i for i, p in enumerate(chain)
                    if isinstance(p, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
                )
                outer = chain[outer_i]
                cls_chain = [p for p in chain[:outer_i] if isinstance(p, ast.ClassDef)]
                if cls_chain:
                    r |= self.class_users(path, cls_chain)
                elif isinstance(outer, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    r |= self.names_users(path, [outer.name], [], (outer.lineno, outer.end_lineno))
                self.notes.append(("importfunc", path, line))
            return r
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # signature / decorator: tests that call the function
            fb = node.body[0].lineno
            r = self.lines_tests(path, fb, node.end_lineno)
            if any(isinstance(p, ast.ClassDef) for p in parents) or node.decorator_list:
                r |= self.class_users(path, parents) if parents else set()
            if not parents:
                r |= self.name_users(path, node.name)
            return r
        # import-time statement at module or class level
        lam = self.lambda_span(path, line)
        if lam is not None:
            return self.lines_tests(path, lam[0], lam[1])
        classes = [p for p in parents if isinstance(p, ast.ClassDef)]
        if isinstance(node, ast.ClassDef):
            return self.class_users(path, classes + [node])
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            return self.attr_users(path, classes, node)
        if classes:
            return self.class_users(path, classes)
        # module level import or other statement
        r = set(self.mod_tests.get(path, ()))
        for name in assigned_names(node):
            r |= self.name_users(path, name)
        return r

    def lambda_span(self, path, line):
        best = None
        for a, b, imp in self.spans.get(path, ()):
            if a <= line <= b and not imp:
                if best is None or b - a < best[1] - best[0]:
                    best = (a, b)
        return best

    def span_kind(self, f, line):
        best = None
        for a, b, imp in self.spans.get(f, ()):
            if a <= line <= b:
                if best is None or b - a < best[1] - best[0]:
                    best = (a, b, imp)
        if best is None:
            return "body"
        return "importfunc" if best[2] else "runtime"

    def mentions(self, name):
        """(runtime_tests, test_file_tests, import_time_mentions) for an identifier."""
        got = self._mention_cache.get(name)
        if got is not None:
            return got
        pat = re.compile(r"\b" + re.escape(name) + r"\b")
        rt = set()
        rt_tests_dir = set()
        imp = []
        for f in self.spans:
            try:
                txt = self.read_src(f)
            except OSError:
                continue
            if name not in txt:
                continue
            for m in pat.finditer(txt):
                line = txt.count("\n", 0, m.start()) + 1
                k = self.span_kind(f, line)
                if k == "runtime":
                    ts = self.lines_tests(f, line, line)
                    rt |= ts
                    if f.startswith("tests/"):
                        rt_tests_dir |= ts
                else:
                    imp.append((f, line, k))
        got = (rt, rt_tests_dir, imp)
        self._mention_cache[name] = got
        return got

    def attr_users(self, path, classes, node):
        return self.names_users(path, assigned_names(node), classes, (node.lineno, node.end_lineno))

    def names_users(self, path, names, classes, own):
        res = set()
        for name in names:
            rt, rt_tests, imp = self.mentions(name)
            base = set(rt)
            extra = set(rt_tests)
            for f, line, kind in imp:
                if f == path and own[0] <= line <= own[1]:
                    continue
                if kind == "importfunc":
                    base |= set(self.mod_tests.get(f, ()))
                    continue
                try:
                    finfo = self.file_info(f)
                except Exception:
                    extra |= set(self.mod_tests.get(f, ()))
                    continue
                hit = finfo.innermost(line)
                if hit is None:
                    continue
                st, s0, e0, par = hit
                if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    extra |= self.lines_tests(f, st.body[0].lineno, st.end_lineno)
                    continue
                tnames = assigned_names(st)
                value = getattr(st, "value", None)
                mine = {c.name for c in classes}
                reads = value is not None and any(
                    (isinstance(n, ast.Name) and n.id == name)
                    or (isinstance(n, ast.Attribute) and n.attr == name and _qual_ok(n.value, mine))
                    for n in ast.walk(value)
                )
                if not reads:
                    continue  # defines its own value, or reads an unrelated class's attribute
                cls2 = [p for p in par if isinstance(p, ast.ClassDef)]
                if isinstance(st, ast.ClassDef):
                    extra |= self.class_users(f, cls2 + [st])
                    continue
                derived = set()
                for m2 in tnames:
                    if m2 != name:
                        derived |= self.mentions(m2)[0]
                if cls2:
                    derived &= self.class_users(f, cls2)
                extra |= derived
            if classes:
                res |= base & self.class_users(path, classes)
            else:
                res |= base
            res |= extra
        return res

    def file_info(self, f):
        info = self.info_cache.get(f)
        if info is None:
            info = self.info_cache[f] = Info(ast.parse(self.read_src(f)))
        return info

    def class_users(self, path, classes):
        mod = path[:-3].replace("/", ".")
        if mod.endswith(".__init__"):
            mod = mod[: -len(".__init__")]
        res = set()
        for depth in range(len(classes), 0, -1):
            qn = ".".join(c.name for c in classes[:depth])
            key = mod + "." + qn
            got = self.cls_tests.get(key)
            if got:
                res.update(got)
                break
        # tests that ran any runtime code (function bodies, lambdas) inside the outermost class
        outer = classes[0]
        for a, b, imp in self.spans.get(path, ()):
            if not imp and outer.lineno <= a and b <= outer.end_lineno:
                res |= self.lines_tests(path, a, b)
        return res

    def name_users(self, path, name):
        # modules whose runtime code may read this module-level name
        res = set()
        pat = re.compile(r"\b" + re.escape(name) + r"\b")
        for f, ts in self.mod_tests.items():
            if f == path or not f.endswith(".py"):
                continue
            try:
                txt = self.read_src(f)
            except OSError:
                continue
            if pat.search(txt):
                res.update(ts)
        return res

    _src_cache = {}

    def read_src(self, f):
        t = self._src_cache.get(f)
        if t is None:
            t = self._src_cache[f] = (SRC / f).read_text()
        return t


def _qual_ok(q, mine):
    """Could `q.NAME` read the attribute of one of our classes? `Other.NAME` with an
    unrelated CamelCase class name cannot (unless inherited, which we accept)."""
    last = q.attr if isinstance(q, ast.Attribute) else q.id if isinstance(q, ast.Name) else None
    if last is None or not last[:1].isupper() or not mine:
        return True
    return last in mine


def assigned_names(node):
    names = []
    if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for t in targets:
            for n in ast.walk(t):
                if isinstance(n, ast.Name):
                    names.append(n.id)
    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        names.append(node.name)
    elif isinstance(node, (ast.Import, ast.ImportFrom)):
        for a in node.names:
            names.append((a.asname or a.name).split(".")[0])
    return names


# ---------------------------------------------------------------- packing


def pack_cost(nums, dur):
    ws = pack(nums, dur)
    return (max(sum(dur[n] for n in w) for w in ws) + WORKER_COST * len(ws)) if ws else 0


def pack(nums, dur):
    """Split tests over workers minimising max load + WORKER_COST per worker."""
    if not nums:
        return []
    items = sorted(nums, key=lambda n: -dur[n])
    best = None
    for k in range(1, 9):
        loads = [0] * k
        ws = [[] for _ in range(k)]
        for n in items:
            i = loads.index(min(loads))
            ws[i].append(n)
            loads[i] += dur[n]
        ws = [w for w in ws if w]
        cost = max(loads) + WORKER_COST * len(ws)
        if best is None or cost < best[0]:
            best = (cost, ws)
    return best[1]


HEAVY_MS = 1000  # tests slower than this are never run here, only listed
LIGHT_LIMIT_MS = int(os.environ.get("BR_LIGHT_LIMIT", "4000"))  # verify only if light candidates run this fast
DROP_HEAVY = os.environ.get("BR_DROP_HEAVY", "1") == "1"
MIN_LIGHT_FOR_DROP = int(os.environ.get("BR_MIN_LIGHT", "3"))
NEVER_DROP = {
    "tests/test_parser.py::TestParser::test_max_nodes",
    "tests/test_generator.py::TestGenerator::test_generator_memory",
    "tests/test_schema.py::TestSchema::test_cache_does_not_retain_queries",
}
PARALLEL = 2
IMPORT_LINES = {}
SELECTOR = None
CPU_FACTOR = float(os.environ.get("BR_CPU_FACTOR", "2.0"))
SETUP_COST_MS = {"tests/test_executor.py": 1400, "tests/test_optimizer.py": 150}


def main():
    t0 = time.time()
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    dur = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    with open(MAP, "rb") as fh:
        data = pickle.load(fh)
    s = Selector(data, tests)
    global SELECTOR
    SELECTOR = s
    IMPORT_LINES.update(s.import_lines)
    results = {}
    plans = []
    for patch in sorted((DATA / "commits").glob("*.patch")):
        cid = patch.stem
        try:
            text = patch.read_text()
            sel = s.select(text)
            sel = {n for n in sel if n in dur}
        except Exception as e:  # never crash: fall back to everything
            log("error", cid, repr(e))
            results[cid] = set(dur)
            continue
        results[cid] = sel
        if not sel:
            continue
        light = {n for n in sel if dur[n] <= HEAVY_MS}
        heavy = sel - light
        if not light or sum(dur[n] for n in light) > LIGHT_LIMIT_MS:
            continue
        if heavy and not DROP_HEAVY:
            continue
        est_cpu = sum(dur[n] for n in light) + sum(
            SETUP_COST_MS.get(f, 0) for f in {tests[n]["file"] for n in light}
        ) + 100
        gain = pack_cost(sel, dur)
        if CPU_FACTOR * est_cpu > (0.6 * gain if heavy else gain):
            continue
        try:
            changes = verifiable_changes(text)
        except Exception as e:
            log("verify-plan error", cid, repr(e))
            changes = None
        if changes:
            plans.append((cid, changes, light, heavy))
    if plans and os.environ.get("BR_VERIFY", "1") == "1":
        try:
            run_plans(plans, tests, dur, results)
        except Exception as e:
            log("verify error", repr(e))
    out = []
    for cid in sorted(results):
        out.append(json.dumps({"commit": cid, "workers": pack(results[cid], dur)}))
    sys.stdout.write("\n".join(out) + ("\n" if out else ""))
    sys.stdout.flush()
    log(f"done in {time.time() - t0:.2f}s, verified {len(plans)}")


def verifiable_changes(text):
    from verify import code_changes

    changes = {}
    for f in parse_patch(text):
        path = f["old"]
        if path is None or f["new"] != path or not path.endswith(".py"):
            return None
        src = SRC / path
        if not src.exists():
            return None
        old_text = SELECTOR.read_src(path)
        new_text, changed_lines = apply_and_changes(old_text, f["hunks"])
        from verify import same_code

        if same_code(path, old_text, new_text):
            continue
        il = IMPORT_LINES.get(path, ())
        if il and any((c if c > 0 else -c) in il or (c <= 0 and -c + 1 in il) for c in changed_lines):
            return None  # this code runs at import time; swapping it later proves nothing
        ch = code_changes(path, old_text, new_text)
        if ch is None:
            return None
        if ch:
            changes[path] = ch
    return changes or None


def run_plans(plans, tests, dur, results):
    import select
    import signal

    from verify import Runner

    files = {tests[n]["file"] for _, _, sel, _ in plans for n in sel}
    r = Runner(str(SRC), tests)
    t1 = time.time()
    r.prepare(files)
    import resource
    _ru = resource.getrusage(resource.RUSAGE_SELF)
    log(f"parent cpu {_ru.ru_utime + _ru.ru_stime:.2f}s")
    log(f"prepared {len(r.mods)} test modules in {time.time() - t1:.2f}s")
    queue = [p for p in plans if r.can_run(p[2])]
    queue.sort(key=lambda p: -sum(dur[n] for n in p[3]))
    running = {}
    while queue or running:
        while queue and len(running) < PARALLEL:
            cid, changes, sel, heavy = queue.pop(0)
            pid, fd = r.start(changes, sel, fail_fast=bool(heavy))
            limit = 8 + 8 * sum(dur[n] for n in sel) / 1000
            running[fd] = (pid, cid, sel, time.time() + limit, [], heavy)
        now = time.time()
        timeout = max(0.01, min(v[3] for v in running.values()) - now)
        ready, _, _ = select.select(list(running), [], [], timeout)
        for fd in ready:
            pid, cid, sel, dl, buf, heavy = running[fd]
            chunk = os.read(fd, 1 << 16)
            if chunk:
                buf.append(chunk)
                continue
            os.close(fd)
            _, _, ru = os.wait4(pid, 0)
            del running[fd]
            log(cid, f"child cpu {ru.ru_utime + ru.ru_stime:.2f}s")
            try:
                res = json.loads(b"".join(buf) or b"{}")
            except ValueError:
                res = {}
            if "bad" in res and not res.get("missing"):
                bad = set(res["bad"]) & sel
                if heavy and (bad or len(sel) < MIN_LIGHT_FOR_DROP):
                    # something broke (or too little evidence): keep the slow tests, and with
                    # fail-fast the remaining fast ones are unverified, so keep them all
                    bad = sel | heavy if bad else heavy
                elif heavy:
                    # fast tests on the same code all pass: drop slow behaviour tests, but keep
                    # the resource-limit tests (memory, node limits) that fast tests cannot stand in for
                    bad = {h for h in heavy if tests[h]["id"] in NEVER_DROP}
                results[cid] = bad
                log(cid, "verified", len(sel), "+", len(heavy), "->", len(results[cid]))
            else:
                log(cid, "unverified", res.get("error"), res.get("missing"))
        now = time.time()
        for fd in list(running):
            pid, cid, sel, dl, buf, heavy = running[fd]
            if now > dl:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
                os.close(fd)
                os.waitpid(pid, 0)
                del running[fd]
                log(cid, "timeout: keeping all", len(sel))


if __name__ == "__main__":
    main()
