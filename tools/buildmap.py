"""Merge trace files into a compact map used by the selector.

usage: buildmap.py TRACE_DIR TESTS_JSON SQLGLOT_DIR OUT_PICKLE
"""
import glob
import json
import os
import pickle
import sys

trace_dir, tests_json, src, out = sys.argv[1:5]
sys.path.insert(0, src)

tests = json.load(open(tests_json))["tests"]
num = {t["id"]: t["number"] for t in tests}

merged = {}
import_lines = {}
for p in glob.glob(os.path.join(trace_dir, "trace-*.json")):
    d = json.load(open(p))
    for k, v in d.items():
        if k == "__collect__":
            for f, ls in v["lines"].items():
                import_lines.setdefault(f, set()).update(ls)
            continue
        n = num.get(k)
        if n is None:
            # parametrised ids may differ; try prefix match later
            continue
        m = merged.setdefault(n, {"lines": {}, "files": set(), "dialects": set()})
        for f, ls in v["lines"].items():
            m["lines"].setdefault(f, set()).update(ls)
        m["files"].update(v["files"])
        m["dialects"].update(v["dialects"])

missing = [t["id"] for t in tests if t["number"] not in merged]
print("tests mapped", len(merged), "missing", len(missing), missing[:5], file=sys.stderr)

# line -> tests
line_tests = {}
for n, m in merged.items():
    for f, ls in m["lines"].items():
        fl = line_tests.setdefault(f, {})
        for l in ls:
            fl.setdefault(l, []).append(n)
# intern sets
set_ids = {}
sets = []
line_map = {}
for f, fl in line_tests.items():
    lm = {}
    for l, ts in fl.items():
        key = tuple(sorted(ts))
        i = set_ids.get(key)
        if i is None:
            i = set_ids[key] = len(sets)
            sets.append(key)
        lm[l] = i
    line_map[f] = lm

file_tests = {}
for n, m in merged.items():
    for f in m["files"]:
        file_tests.setdefault(f, set()).add(n)

# class usage: dialect classes (with their parser/generator/tokenizer/typing classes) and
# expression classes (with MRO)
import importlib

cls_tests = {}


def resolve(name):
    mod, _, qn = name.rpartition(".")
    # qualname may be nested: find the module part
    parts = name.split(".")
    for i in range(len(parts) - 1, 0, -1):
        try:
            obj = importlib.import_module(".".join(parts[:i]))
        except Exception:
            continue
        try:
            for a in parts[i:]:
                obj = getattr(obj, a)
            return obj
        except AttributeError:
            return None
    return None


def cname(c):
    return c.__module__ + "." + c.__qualname__


cache = {}


def expand(name):
    if name in cache:
        return cache[name]
    res = set()
    isexp = name.startswith("E:")
    obj = resolve(name[2:] if isexp else name)
    if obj is None:
        res.add(name[2:] if isexp else name)
    else:
        for c in obj.__mro__:
            res.add(cname(c))
        if not isexp:
            for attr in ("parser_class", "generator_class", "tokenizer_class", "jsonpath_tokenizer_class"):
                k = getattr(obj, attr, None)
                if isinstance(k, type):
                    for c in k.__mro__:
                        res.add(cname(c))
                    for inner in ("Tokenizer", "Parser", "Generator"):
                        pass
            for inner in ("Tokenizer", "Parser", "Generator", "JSONPathTokenizer"):
                k = getattr(obj, inner, None)
                if isinstance(k, type):
                    for c in k.__mro__:
                        res.add(cname(c))
    cache[name] = res
    return res


for n, m in merged.items():
    for d in m["dialects"]:
        for c in expand(d):
            cls_tests.setdefault(c, set()).add(n)

# modules: tests that executed any runtime line in a module
mod_tests = {}
for n, m in merged.items():
    for f in m["lines"]:
        mod_tests.setdefault(f, set()).add(n)

data = {
    "sets": sets,
    "line_map": line_map,
    "file_tests": {f: sorted(s) for f, s in file_tests.items()},
    "cls_tests": {c: sorted(s) for c, s in cls_tests.items()},
    "mod_tests": {f: sorted(s) for f, s in mod_tests.items()},
}
with open(out, "wb") as fh:
    pickle.dump(data, fh, protocol=5)
print("sets", len(sets), "files", len(line_map), "classes", len(cls_tests), file=sys.stderr)
print("size", os.path.getsize(out), file=sys.stderr)

# function spans for every python file: (start, end, import_time)
import ast as _ast

spans = {}
for base in ("sqlglot", "tests"):
    for dp, dn, fns in os.walk(os.path.join(src, base)):
        if "__pycache__" in dp:
            continue
        for fn in fns:
            if not fn.endswith(".py"):
                continue
            full = os.path.join(dp, fn)
            rel = os.path.relpath(full, src)
            try:
                tree = _ast.parse(open(full).read())
            except SyntaxError:
                continue
            out_sp = []

            def visit(node, meta=False, cls=None):
                for ch in _ast.iter_child_nodes(node):
                    if isinstance(ch, _ast.ClassDef):
                        is_meta = any(isinstance(b, _ast.Name) and b.id == "type" for b in ch.bases)
                        visit(ch, is_meta, ch)
                    elif isinstance(ch, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                        imp = ch.name == "__init_subclass__" or (meta and ch.name in ("__new__", "__init__"))
                        out_sp.append((ch.body[0].lineno, ch.end_lineno, imp))
                        visit(ch, False, None)
                    elif isinstance(ch, _ast.Lambda):
                        out_sp.append((ch.lineno, ch.end_lineno, False))
                        visit(ch, False, None)
                    else:
                        visit(ch, meta, cls)

            visit(tree)
            spans[rel] = sorted(out_sp)

data["spans"] = spans
data["import_lines"] = {f: sorted(v) for f, v in import_lines.items()}
with open(out, "wb") as fh:
    pickle.dump(data, fh, protocol=5)
print("spans files", len(spans), "size", os.path.getsize(out), file=sys.stderr)
