#!/bin/bash
# usage: runmuts.sh COMMITS_DIR RESULTS_DIR
cd /home/claude/br/mut/sqlglot
mkdir -p "$2"
for p in "$1"/*.patch; do
  id=$(basename "$p" .patch)
  [ -f "$2/$id.done" ] && continue
  git apply "$p" || { echo "apply failed $id"; continue; }
  : > "$2/$id.txt"
  SKIP_INTEGRATION=0 MUTREC_OUT="$2/$id.txt" PYTHONPATH=.:/home/claude/br/mutplug timeout 600 /home/claude/br/venv/bin/python -m pytest -q -p no:cacheprovider -p mutrec tests -n 4 --timeout 120 \
     --deselect tests/test_parser.py::TestParser::test_max_nodes --deselect tests/test_generator.py::TestGenerator::test_generator_memory > "$2/$id.log" 2>&1
  git checkout -q -- .
  touch "$2/$id.done"
done
echo ALLDONE
