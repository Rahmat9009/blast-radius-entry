# blast-radius-entry

Builders Buildathon, RWX Blast Radius track.

For each patch the selector picks only the tests that can reach the changed code, then packs
them onto workers to minimise `max load + 2 s × workers`.

How it decides:

- **Function-body lines**: tests whose per-test line coverage (at the pinned commit) touches the
  changed statement.
- **Signature / decorator lines**: tests that executed the function's body.
- **Class-level lines** (dicts, constants on dialects, parsers, expressions): tests that used that
  class, i.e. instantiated a dialect whose dialect/parser/generator/tokenizer classes include it,
  or created an expression of that class, or ran any code inside it.
- **Module-level lines**: tests that ran code in that module or in modules that mention the name.
- **Non-Python files (fixtures)**: tests that opened that file.
- **No-op edits** (comments, formatting): if the old and new AST are identical, run nothing.

## Maps

`map.pkl` is built from the pinned sqlglot commit only (coverage, opened files, classes used):
`tools/brtrace.py` is a pytest plugin that records per-test line coverage with `sys.monitoring`,
runs process-pool work in-process (with pickling) so it is traced too, and records files opened,
dialects and expression classes used. `tools/buildmap.py` merges those traces into `map.pkl`.

`tools/mergeidx.py` also merges in `coverage.json.gz` (an independent coverage index traced
in real pool worker processes, built by `build_index.py`) so the map is the union of both.

At run time, when a commit's candidate tests are fast, they are run against the patched code
(changed function bodies swapped in, in a forked child) and only the tests that really fail are
listed. Slow behaviour tests are dropped only when every fast test covering the same code passes;
memory and node-limit tests are never dropped.

## Open-source code used

- `src/` is an unmodified copy of the `.py` files of [sqlglot](https://github.com/tobymao/sqlglot)
  at commit `eb11aec4be8cb2122afe713a4816662c3f0fb2fa` (MIT, see `src/LICENSE-sqlglot`). It is only
  read, to find which statement a changed line belongs to.
- Python standard library only at run time.

## License

MIT

## Coverage guard (ChatGPT, not used by the Dockerfile right now)

The guard combines the original selector with a separately collected baseline line and fixture index. The second index traces tests in actual worker processes and gives the selector another source of evidence for short-circuit expressions and class settings. The runtime emits the union of both candidate sets and schedules it using integer-millisecond test durations. It uses the Python standard library.

The source archive and test coverage both come from the pinned, unmodified SQLGlot checkout. Published sample answers are used for local evaluation.
