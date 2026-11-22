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
        for tag, text in h["lines"]:
            if tag == " ":
                out.append(old[pos] if pos < len(old) else text)
                pos += 1
            elif tag == "-":
                changed.add(pos + 1)
                pos += 1
            else:
                out.append(text)
                # insertion between old line pos and pos+1 (1-based)
                changed.add(-(pos))  # marker: inserted after old line `pos`
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
        self.tests = tests
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
        src = SRC / path
        if f["old"] is None or not src.exists():
            # new python file: tests importing it would be listed by module; be broad
            self.notes.append(("newfile", path))
            return set()
        old_text = src.read_text()
        new_text, changed = apply_and_changes(old_text, f["hunks"])
        try:
            old_tree = ast.parse(old_text)
        except SyntaxError:
            return set()
        try:
            new_tree = ast.parse(new_text)
        except SyntaxError:
            # file cannot be imported any more: every test that imports it fails
            self.notes.append(("syntax", path))
            return self.importers(path)
        if ast.dump(old_tree) == ast.dump(new_tree):
            return set()
        info = self.info_cache.get(path)
        if info is None:
            info = self.info_cache[path] = Info(old_tree)
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
            return self.lines_tests(path, lo, hi)
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
        classes = [p for p in parents if isinstance(p, ast.ClassDef)]
        if isinstance(node, ast.ClassDef):
            classes = classes + [node]
        if classes:
            r = self.class_users(path, classes)
            # lambdas or runtime code on these lines
            r |= self.lines_tests(path, lo, hi)
            return r
        # module level
        r = set(self.mod_tests.get(path, ()))
        for name in assigned_names(node):
            r |= self.name_users(path, name)
        return r

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
        # tests that ran any code inside the outermost class
        outer = classes[0]
        res |= self.lines_tests(path, outer.lineno, outer.end_lineno)
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


def main():
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    dur = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    with open(MAP, "rb") as fh:
        data = pickle.load(fh)
    s = Selector(data, tests)
    out = []
    for patch in sorted((DATA / "commits").glob("*.patch")):
        try:
            sel = s.select(patch.read_text())
        except Exception as e:  # never crash: fall back to everything
            log("error", patch.stem, repr(e))
            sel = set(dur)
        sel = {n for n in sel if n in dur}
        out.append(json.dumps({"commit": patch.stem, "workers": pack(sel, dur)}))
    sys.stdout.write("\n".join(out) + ("\n" if out else ""))


if __name__ == "__main__":
    main()
