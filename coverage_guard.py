"""Select and schedule SQLGlot tests using coverage on a pinned baseline."""

import argparse
import ast
from dataclasses import dataclass, field
from functools import lru_cache
import gzip
import json
import os
import pickle

import selector as published
from pathlib import Path, PurePosixPath
import re


HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
SOURCE_ROOT = Path("/opt/sqlglot")
PACKAGE_ROOT = Path(__file__).resolve().parent


@dataclass
class Hunk:
    start: int
    old_count: int
    lines: list[str] = field(default_factory=list)


@dataclass
class Change:
    old_path: str | None
    new_path: str | None
    hunks: list[Hunk] = field(default_factory=list)

    @property
    def path(self):
        return self.old_path or self.new_path


def patch_path(raw, prefix):
    path = raw.split("\t", 1)[0]
    if path == "/dev/null":
        return None
    if not path.startswith(prefix):
        raise ValueError("unsupported patch path")
    path = path[len(prefix):]
    parsed = PurePosixPath(path)
    if parsed.is_absolute() or ".." in parsed.parts or not parsed.parts:
        raise ValueError("invalid patch path")
    return parsed.as_posix()


def read_changes(text):
    changes = []
    change = hunk = last_hunk = None
    pending_path = None
    for line in text.splitlines(keepends=True):
        if line.startswith("\\ No newline at end of file"):
            if last_hunk is None or not last_hunk.lines:
                raise ValueError("misplaced end-of-file marker")
            last_hunk.lines[-1] = last_hunk.lines[-1].removesuffix("\n")
            continue
        if hunk:
            marker = line[:1]
            if marker in (" ", "+", "-"):
                hunk.lines.append(line)
                remaining_old -= marker != "+"
                remaining_new -= marker != "-"
                if remaining_old == remaining_new == 0:
                    hunk = None
                elif min(remaining_old, remaining_new) < 0:
                    raise ValueError("incorrect hunk length")
                continue
            raise ValueError("incomplete patch hunk")
        if line.startswith("diff --git "):
            change = None
        elif line.startswith("--- "):
            pending_path = patch_path(line[4:].rstrip("\r\n"), "a/")
        elif line.startswith("+++ "):
            change = Change(pending_path, patch_path(line[4:].rstrip("\r\n"), "b/"))
            changes.append(change)
        elif (match := HUNK_HEADER.match(line)) and change:
            start, old, _, new = match.groups()
            remaining_old, remaining_new = int(old or 1), int(new or 1)
            hunk = last_hunk = Hunk(int(start), remaining_old)
            change.hunks.append(hunk)
        elif line.startswith(("Binary files ", "GIT binary patch", "rename ", "copy ")):
            raise ValueError("unsupported file operation")
    if hunk or not changes or any(not change.hunks for change in changes):
        raise ValueError("empty or incomplete diff")
    return changes


def apply_change(original, change):
    source = original.splitlines(keepends=True)
    result = []
    locations = set()
    cursor = 0
    for hunk in change.hunks:
        start = hunk.start - bool(hunk.old_count)
        if start < cursor:
            raise ValueError("overlapping hunks")
        result.extend(source[cursor:start])
        cursor = start
        removed, inserted = set(), set()
        for line in hunk.lines:
            marker, content = line[0], line[1:]
            if marker != "+":
                if cursor >= len(source) or source[cursor].rstrip("\r\n") != content.rstrip("\r\n"):
                    raise ValueError("diff context does not match baseline")
                cursor += 1
            if marker != "-":
                result.append(content)
            if marker == "-":
                removed.add(cursor)
            elif marker == "+":
                inserted.add(cursor + 1)
            else:
                locations.update(removed or inserted)
                removed.clear()
                inserted.clear()
        locations.update(removed or inserted)
    result.extend(source[cursor:])
    return "".join(result), locations


@dataclass
class Location:
    anchors: set[int] = field(default_factory=set)
    fallback: list[int] = field(default_factory=list)
    symbols: set[str] = field(default_factory=set)


def locate(tree, line):
    location = Location({line})

    def visit(node, inside_function=False):
        start = getattr(node, "lineno", None)
        end = getattr(node, "end_lineno", None)
        if start is not None and not start <= line <= (end or start):
            return
        if isinstance(node, (ast.expr, ast.stmt, ast.ExceptHandler)):
            # A change to a later operand can affect earlier short-circuit paths.
            if isinstance(node, ast.expr) or not any(
                getattr(child, "lineno", 0) <= line <= (getattr(child, "end_lineno", 0) or 0)
                for child in getattr(node, "body", [])
                if isinstance(child, ast.stmt)
            ):
                location.anchors.add(start)
            elif inside_function:
                location.fallback.append(start)
        if not inside_function:
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                location.symbols.update(
                    target.id for target in targets if isinstance(target, ast.Name)
                )
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                location.symbols.update(alias.asname or alias.name for alias in node.names)
        nested_function = inside_function or isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        for child in ast.iter_child_nodes(node):
            visit(child, nested_function)

    visit(tree)
    return location


class TestIndex:
    def __init__(self, manifest, index, source):
        self.tests = manifest
        self.all = {test["number"] for test in manifest}
        self.source = source
        self.by_name = {test["id"]: test["number"] for test in manifest}
        self.by_file = {}
        for test in manifest:
            self.by_file.setdefault(test["file"], set()).add(test["number"])
        self.number = [self.by_name.get(name, -1 if not name else None) for name in index["node_ids"]]
        self.lines = index["lines"]
        self.fixtures = index["fixtures"]

    @lru_cache(maxsize=None)
    def baseline(self, path):
        return (self.source / path).read_text(encoding="utf-8")

    def resolve(self, internal_ids):
        return {number for index in internal_ids if (number := self.number[index]) is not None}

    @lru_cache(maxsize=None)
    def file_tests(self, path):
        selected = self.resolve({index for ids in self.lines[path].values() for index in ids})
        selected.discard(-1)
        return selected

    @lru_cache(maxsize=256)
    def symbol_tests(self, symbol, definition_path):
        selected = set()
        for path in self.lines:
            if path == definition_path:
                continue
            source = self.baseline(path)
            if symbol not in source:
                continue
            for node in ast.walk(parse_source(source)):
                if (
                    isinstance(node, ast.Attribute) and node.attr == symbol
                    or isinstance(node, ast.Name) and node.id == symbol
                ):
                    selected.update(self.resolve(self.lines[path].get(str(node.lineno), ())))
        selected.discard(-1)
        return selected

    def for_python(self, change):
        path = change.path
        if (change.old_path != change.new_path) or path not in self.lines:
            return self.all
        original = self.baseline(path)
        updated, locations = apply_change(original, change)
        old_tree = parse_source(original)
        new_tree = parse_source(updated)
        if ast.dump(old_tree) == ast.dump(new_tree):
            return set()

        covered = self.lines[path]
        selected = set()
        for line in locations:
            location = locate(old_tree, line)
            hits = self.resolve(
                {node for anchor in location.anchors for node in covered.get(str(anchor), ())}
            )
            if not hits:
                for anchor in reversed(location.fallback):
                    hits = self.resolve(covered.get(str(anchor), ()))
                    if hits:
                        break
            if -1 in hits:
                for symbol in location.symbols:
                    hits.update(self.symbol_tests(symbol, path))
            selected.update(hits or self.file_tests(path))

        if -1 in selected or not selected:
            selected.discard(-1)
            selected.update(self.file_tests(path))
            if not selected:
                return self.all
        return selected

    def for_diff(self, text):
        selected = set()
        try:
            for change in read_changes(text):
                path = change.path
                if path in self.by_file:
                    selected.update(self.by_file[path])
                elif path in self.fixtures:
                    readers = self.resolve(self.fixtures[path])
                    selected.update(self.all if -1 in readers else readers)
                elif path.startswith("sqlglot/") and path.endswith(".py"):
                    selected.update(self.for_python(change))
                else:
                    return self.all.copy()
        except (ValueError, OSError, SyntaxError, RecursionError, UnicodeError):
            return self.all.copy()
        return selected


def parse_source(source):
    return ast.parse(source, type_comments=True)


def schedule(tests):
    if not tests:
        return []
    durations = sorted(
        ((round(test["seconds"] * 1000), test["number"]) for test in tests),
        reverse=True,
    )
    total_ms = sum(duration for duration, _ in durations)
    best_cost = float("inf")
    best_workers = []
    for count in range(1, min(len(tests), 8) + 1):
        bound = max(durations[0][0], (total_ms + count - 1) // count)
        if bound + 2000 * count >= best_cost:
            continue
        loads = [0] * count
        workers = [[] for _ in loads]
        for duration, test in durations:
            worker = loads.index(min(loads))
            loads[worker] += duration
            workers[worker].append(test)
        cost = max(loads) + 2000 * count
        if cost < best_cost:
            best_cost, best_workers = cost, workers
    return best_workers


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, default=Path("/data"))
    parser.add_argument("--source", type=Path, default=SOURCE_ROOT)
    parser.add_argument("--index", type=Path, default=PACKAGE_ROOT / "coverage.json.gz")
    args = parser.parse_args()

    tests = json.loads((args.data / "tests.json").read_text())["tests"]
    with gzip.open(args.index, "rt", encoding="utf-8") as file:
        index = TestIndex(tests, json.load(file), args.source)
    published.SRC = args.source
    published_map = Path(os.environ.get("BR_MAP", "/app/map.pkl"))
    with published_map.open("rb") as file:
        reference = published.Selector(pickle.load(file), tests)
    cache = {}
    for patch in sorted((args.data / "commits").glob("*.patch")):
        patch_text = patch.read_text(encoding="utf-8", errors="replace")
        selected = index.for_diff(patch_text)
        try:
            selected.update(reference.select(patch_text))
        except (ValueError, OSError, SyntaxError, RecursionError, UnicodeError):
            selected = index.all
        selection = frozenset(selected & index.all)
        if selection not in cache:
            cache[selection] = schedule([test for test in tests if test["number"] in selection])
        workers = cache[selection]
        print(json.dumps({"commit": patch.stem, "workers": workers}, separators=(",", ":")))


if __name__ == "__main__":
    main()
