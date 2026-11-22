import gzip, json, pickle, sys
src_map, idx_path, tests_json, out = sys.argv[1:5]
d = pickle.load(open(src_map, "rb"))
idx = json.load(gzip.open(idx_path, "rt"))
t = json.load(open(tests_json))["tests"]
by = {x["id"]: x["number"] for x in t}
num = [by.get(n) for n in idx["node_ids"]]
sets = [tuple(s) for s in d["sets"]]
ids = {s: i for i, s in enumerate(sets)}
lm = d["line_map"]
import ast, os
SRC = os.environ.get("SRC", "/home/claude/br/sqlglot")


def def_body_lines(f):
    try:
        tree = ast.parse(open(os.path.join(SRC, f)).read())
    except Exception:
        return set()
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.update(range(n.body[0].lineno, n.end_lineno + 1))
    return out


added = 0
for f, lines in idx["lines"].items():
    fl = lm.setdefault(f, {})
    body = def_body_lines(f)
    for l, tids in lines.items():
        l = int(l)
        if l not in body:
            continue  # module/class level and lambda lines: their attribution is import-time noise
        theirs = {num[i] for i in tids if num[i] is not None}
        cur = set(sets[fl[l]]) if l in fl else set()
        if theirs - cur:
            added += 1
            key = tuple(sorted(cur | theirs))
            i = ids.get(key)
            if i is None:
                i = ids[key] = len(sets)
                sets.append(key)
            fl[l] = i
        # mod_tests too
    mt = set(d["mod_tests"].get(f, ()))
    for l, tids in lines.items():
        mt.update(num[i] for i in tids if num[i] is not None)
    d["mod_tests"][f] = sorted(mt)
for f, tids in idx["fixtures"].items():
    cur = set(d["file_tests"].get(f, ()))
    cur.update(num[i] for i in tids if num[i] is not None)
    d["file_tests"][f] = sorted(cur)
d["sets"] = sets
pickle.dump(d, open(out, "wb"), protocol=5)
print("lines widened", added)
