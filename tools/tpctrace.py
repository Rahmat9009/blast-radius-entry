"""Per-query line coverage for the TPC executor tests (test_execute_tpch / test_execute_tpcds).

usage (from the sqlglot checkout): python tpctrace.py OUT.pkl
Records the lines run by the class setup + initializer ("setup") and, for every query i,
the lines run to parse it, pickle it, execute it and compare it (the test's own steps).
"""
import os
import pickle
import sys

os.environ["SKIP_INTEGRATION"] = "0"
sys.path.insert(0, os.getcwd())
ROOT = os.getcwd() + os.sep
M = sys.monitoring
TOOL = 4
cur = set()


def on_line(code, line):
    fn = code.co_filename
    if fn.startswith(ROOT):
        cur.add((fn[len(ROOT):], line))
    return M.DISABLE


M.use_tool_id(TOOL, "tpc")
M.register_callback(TOOL, M.events.LINE, on_line)


def start():
    global cur
    cur = set()
    M.set_events(TOOL, M.events.LINE)
    M.restart_events()


def stop():
    M.set_events(TOOL, 0)
    return cur


import tests.test_executor as te  # noqa: E402

out = {}
start()
te.TestExecutor.setUpClass()
setup = stop()
for kind, schema, tables, sqls in (
    ("tpch", te.TPCH_SCHEMA, te.TestExecutor.tpch_tables, te.TestExecutor.tpch_sqls),
    ("tpcds", te.TPCDS_SCHEMA, te.TestExecutor.tpcds_tables, te.TestExecutor.tpcds_sqls),
):
    start()
    te.initializer(*pickle.loads(pickle.dumps((schema, tables))))
    init = stop()
    inst = te.TestExecutor("test_execute_" + kind)
    per = []
    for i, (args, sql, _) in enumerate(sqls):
        start()
        e = pickle.loads(pickle.dumps(te.parse_one(sql)))
        table = te.mp_execute(e, args)
        table = pickle.loads(pickle.dumps(table))
        if table is not None:
            inst.subtestHelper(i, table, tpch=(kind == "tpch"))
        per.append(stop())
    out[kind] = {"setup": setup | init, "queries": per}
    print(kind, len(per), "queries", sum(len(p) for p in per), file=sys.stderr)
with open(sys.argv[1], "wb") as fh:
    pickle.dump(out, fh)
