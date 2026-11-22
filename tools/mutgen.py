"""Generate random single-token mutants of sqlglot as unified diffs (for self-testing)."""
import difflib
import os
import pickle
import random
import re
import sys

src, mapf, outdir, n, seed = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4]), int(sys.argv[5])
random.seed(seed)
d = pickle.load(open(mapf, "rb"))
lm = d["line_map"]
files = [f for f in lm if f.startswith("sqlglot/") and f.endswith(".py")]

OPS = [
    (r"\band\b", "or"), (r"\bor\b", "and"), (r" == ", " != "), (r" != ", " == "),
    (r" < ", " <= "), (r" <= ", " < "), (r" > ", " >= "), (r" >= ", " > "),
    (r"\bTrue\b", "False"), (r"\bFalse\b", "True"), (r" is not ", " is "), (r" is ", " == "),
    (r"\bnot ", ""), (r"\b1\b", "2"), (r"\b0\b", "1"), (r"\b2\b", "3"), (r"\+ 1\b", "- 1"),
    (r"\bif \b", "if not "),
]
os.makedirs(outdir, exist_ok=True)
made = 0
tries = 0
while made < n and tries < 100000:
    tries += 1
    f = random.choice(files)
    text = open(os.path.join(src, f)).read()
    lines = text.split("\n")
    kind = random.random()
    if kind < 0.75:
        cand = list(lm[f].keys())  # covered runtime lines
    else:
        cand = list(range(1, len(lines) + 1))  # anywhere (class-level constants etc.)
    if not cand:
        continue
    ln = random.choice(cand)
    if ln > len(lines):
        continue
    line = lines[ln - 1]
    s = line.strip()
    if not s or s.startswith("#") or s.startswith(("def ", "class ", "import ", "from ", "@", '"""', "'")):
        continue
    ops = [(p, r) for p, r in OPS if re.search(p, line)]
    if not ops:
        continue
    p, r = random.choice(ops)
    ms = list(re.finditer(p, line))
    m = random.choice(ms)
    new = line[: m.start()] + re.sub(p, r, m.group(0)) + line[m.end():]
    if new == line:
        continue
    new_lines = lines[:]
    new_lines[ln - 1] = new
    new_text = "\n".join(new_lines)
    try:
        compile(new_text, f, "exec")
    except SyntaxError:
        continue
    diff = difflib.unified_diff(
        text.splitlines(keepends=True), new_text.splitlines(keepends=True), f"a/{f}", f"b/{f}", n=3
    )
    body = "".join(diff)
    cid = f"m{seed}{made:03d}"
    with open(os.path.join(outdir, cid + ".patch"), "w") as fh:
        fh.write(f"diff --git a/{f} b/{f}\n" + body)
    made += 1
print("made", made)
