"""Compare the selector against real test results on self-made mutants."""
import json
import os
import resource
import shutil
import subprocess
import sys
import time

res_dir, commits_dir, mapf = sys.argv[1], sys.argv[2], sys.argv[3]
data = "/tmp/mutdata"
if os.path.isdir(data):
    shutil.rmtree(data)
os.makedirs(data + "/commits")
tests = json.load(open("/home/claude/br/sample/sample/tests.json"))["tests"]
num = {t["id"]: t["number"] for t in tests}
shutil.copy("/home/claude/br/sample/sample/tests.json", data + "/tests.json")
answers = {}
for fn in sorted(os.listdir(res_dir)):
    if not fn.endswith(".done"):
        continue
    cid = fn[:-5]
    lines = [l.strip() for l in open(os.path.join(res_dir, cid + ".txt")) if l.strip()]
    failing = sorted({num[l] for l in lines if l in num})
    other = [l for l in lines if l not in num]
    answers[cid] = {"failing": failing, "kind": "mut", "other": other}
    shutil.copy(os.path.join(commits_dir, cid + ".patch"), data + "/commits/")
json.dump({"commits": answers}, open(data + "/answers.json", "w"))
env = dict(os.environ, BR_DATA=data, BR_SRC="/home/claude/br/sqlglot", BR_MAP=mapf,
           PYTHONPYCACHEPREFIX=f"/tmp/pyc{time.time_ns()}")
t0 = time.time()
p = subprocess.run(["/home/claude/br/venv/bin/python", "-B", "/home/claude/br/entry/selector.py"],
                   env=env, capture_output=True, text=True)
ru = resource.getrusage(resource.RUSAGE_CHILDREN)
if os.environ.get("SHOWLOG"):
    sys.stderr.write(p.stderr)
dur = {t["number"]: round(t["seconds"] * 1000) for t in tests}
heavy_skip = {num["tests/test_parser.py::TestParser::test_max_nodes"],
              num["tests/test_generator.py::TestGenerator::test_generator_memory"]}
tot = 0
miss = 0
oracle = 0
for l in p.stdout.splitlines():
    o = json.loads(l)
    ws = [w for w in o["workers"] if w]
    c = (max(sum(dur[n] for n in w) for w in ws) + 2000 * len(ws)) if ws else 0
    sel = {n for w in ws for n in w}
    a = answers[o["commit"]]
    f = set(a["failing"])
    m = f - sel
    fo = sum(dur[n] for n in f)
    oracle += (max(dur[n] for n in f) + 2000) if f else 0
    if m:
        miss += 1
        print("MISS", o["commit"], [tests[n]["id"] for n in m][:4], "sel", len(sel))
    elif c > 2600:
        print(f"  {o['commit']} fail={len(f)} sel={len(sel)} cost={c / 1000:.2f}")
    if a["other"]:
        print("  OTHER", o["commit"], a["other"][:3])
    tot += c
print(f"n={len(answers)} simulated={tot / 1000:.1f} oracle~{oracle / 1000:.1f} misses={miss} "
      f"cpu={ru.ru_utime + ru.ru_stime:.1f}s (note: max_nodes/generator_memory not run in labels)")
