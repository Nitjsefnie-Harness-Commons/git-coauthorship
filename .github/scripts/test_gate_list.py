"""Hold gate_wait.py's required list and each waiter's own-job exclusions to
the workflow files, through a minimal reader that refuses what it cannot read."""
import importlib.util
import itertools
import re
import shlex
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORKFLOWS = HERE.parent / "workflows"


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


wait = load("gate_wait")
yml = load("gate_base_freshness")
WorkflowError = yml.WorkflowError

# Push-on-main jobs whose main run records rather than judges: each commits
# its own tightened records to main (README, CI).
NOT_REQUIRED = {"comment-share", "size-baseline"}
WAITERS = {"release.yml", "tag.yml"}
MATRIX_REFERENCE = re.compile(r"\$\{\{\s*matrix\.([A-Za-z0-9_-]+)\s*\}\}")


def scalar(value: str) -> str:
    value = re.sub(r"\s+#.*$", "", value).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    if value and value[0] in "'\"[{&*|>!%@`":
        raise WorkflowError(f"unread scalar {value!r}")
    return value


def flow_list(value: str) -> list[str]:
    value = re.sub(r"\s+#.*$", "", value).strip()
    if not (value.startswith("[") and value.endswith("]")) or "[" in value[1:-1]:
        raise WorkflowError(f"expected a flow list, found {value!r}")
    return [scalar(item) for item in value[1:-1].split(",") if item.strip()]


def children(lines: list[str], start: int, end: int, indent: int) -> dict[str, tuple[str | None, int, int]]:
    """The mapping keys at `indent` in lines[start:end]: value, block span."""
    found: dict[str, tuple[str | None, int, int]] = {}
    index = start
    while index < end:
        line = lines[index]
        if yml.skippable(line):
            index += 1
            continue
        match = yml.MAPPING.match(line.strip()) if yml.indent_of(line) == indent else None
        if match is None or match["key"] in found:
            raise WorkflowError(f"expected one key at indent {indent}, found {line!r}")
        value = match["value"]
        value = None if value is None or value.lstrip().startswith("#") else value
        stop = min(yml.block_end(lines, index, indent), end)
        found[match["key"]] = (value, index + 1, stop)
        index = stop
    return found


def block_list(lines: list[str], start: int, end: int, indent: int) -> list[str]:
    items = []
    for line in lines[start:end]:
        if yml.skippable(line):
            continue
        match = yml.SEQUENCE.match(line.strip()) if yml.indent_of(line) == indent else None
        if match is None:
            raise WorkflowError(f"expected a list item at indent {indent}, found {line!r}")
        items.append(scalar(match["rest"]))
    return items


def strings(lines: list[str], entry: tuple[str | None, int, int], indent: int) -> list[str]:
    value, start, end = entry
    items = flow_list(value) if value is not None else block_list(lines, start, end, indent)
    if value is None and not items:
        raise WorkflowError("a key with neither a value nor list items")
    return items


def push_trigger(text: str) -> dict[str, list[str]] | None:
    lines = text.splitlines()
    found = yml.top_level_keys(lines, "on")
    if len(found) != 1:
        raise WorkflowError(f"{len(found)} top-level `on:` keys")
    index, match = found[0]
    value = match["value"]
    if value is not None and not value.lstrip().startswith("#"):
        events = flow_list(value) if value.lstrip().startswith("[") else [scalar(value)]
        return {} if "push" in events else None
    events = children(lines, index + 1, yml.block_end(lines, index, 0), 2)
    if "push" not in events:
        return None
    push_value, start, end = events["push"]
    if push_value is not None:
        raise WorkflowError(f"`push: {push_value}` is not a block mapping")
    filters = children(lines, start, end, 4)
    unknown = set(filters) - {"branches", "tags", "paths", "paths-ignore"}
    if unknown:
        raise WorkflowError(f"unread push filters {sorted(unknown)}")
    return {key: strings(lines, entry, 6) for key, entry in filters.items()}


def matrix_rows(lines: list[str], entry: tuple[str | None, int, int]) -> list[dict[str, str]]:
    value, start, end = entry
    if value is not None:
        raise WorkflowError(f"`matrix: {value}` is not a block mapping")
    axes = children(lines, start, end, 8)
    if "exclude" in axes:
        raise WorkflowError("a matrix `exclude:`")
    if "include" in axes:
        if len(axes) != 1:
            raise WorkflowError("an `include:` beside other matrix keys")
        rows: list[dict[str, str]] = []
        include_value, start, end = axes["include"]
        if include_value is not None:
            raise WorkflowError("a flow `include:`")
        for line_index in range(start, end):
            line = lines[line_index]
            if yml.skippable(line) or yml.indent_of(line) != 10:
                continue
            item = yml.SEQUENCE.match(line.strip())
            if item is None:
                raise WorkflowError(f"expected an include entry, found {line!r}")
            stop = yml.block_end(lines, line_index, 10)
            lines_of_entry = [" " * 12 + item["rest"]] + lines[line_index + 1:stop]
            rows.append({key: scalar(found[0] or "")
                         for key, found in children(lines_of_entry, 0, len(lines_of_entry), 12).items()})
        return rows
    lists = {key: strings(lines, axis, 10) for key, axis in axes.items()}
    return [dict(zip(lists, values)) for values in itertools.product(*lists.values())]


def check_names(text: str) -> dict[str, list[str]]:
    """Each job id's check runs, named as GitHub names a job that runs."""
    lines = text.splitlines()
    found = yml.top_level_keys(lines, "jobs")
    if len(found) != 1:
        raise WorkflowError(f"{len(found)} top-level `jobs:` keys")
    index = found[0][0]
    names = {}
    for job, (value, start, end) in children(lines, index + 1, yml.block_end(lines, index, 0), 2).items():
        if value is not None:
            raise WorkflowError(f"job `{job}` is not a block mapping")
        keys = children(lines, start, end, 4)
        if "uses" in keys:
            raise WorkflowError(f"job `{job}` calls a reusable workflow, whose checks are `{job} / <inner>`")
        rows: list[dict[str, str]] = [{}]
        if "strategy" in keys:
            strategy = children(lines, keys["strategy"][1], keys["strategy"][2], 6)
            if "matrix" in strategy:
                rows = matrix_rows(lines, strategy["matrix"])
        template = scalar(keys["name"][0] or "") if "name" in keys else None
        if template is not None:
            if "${{" in MATRIX_REFERENCE.sub("", template):
                raise WorkflowError(f"job `{job}` name {template!r} uses more than the matrix")
            names[job] = [MATRIX_REFERENCE.sub(lambda m, row=row: row[m[1]], template) for row in rows]
        elif rows == [{}]:
            names[job] = [job]
        else:
            names[job] = [f"{job} ({', '.join(row.values())})" for row in rows]
    return names


def read(name: str) -> str:
    return (WORKFLOWS / name).read_text(encoding="utf-8")


def workflow_files() -> list[str]:
    return sorted(path.name for path in WORKFLOWS.iterdir() if path.suffix in (".yml", ".yaml"))


def published() -> dict[str, list[str]]:
    return {name: [check for checks in check_names(read(name)).values() for check in checks]
            for name in workflow_files()}


def gate_workflows() -> list[str]:
    """Pushes the tag's own commits reach: main, no path or tag filter, and
    ignoring no path tag.yml does not."""
    ignored = set(push_trigger(read("tag.yml"))["paths-ignore"])
    gates = []
    for name in workflow_files():
        push = push_trigger(read(name))
        if name in WAITERS or push is None or "paths" in push or "tags" in push:
            continue
        if any(re.search(r"[*?\[!+]", branch) for branch in push.get("branches", [])):
            raise WorkflowError(f"{name}: a branch pattern")
        if ("branches" not in push or "main" in push["branches"]) \
                and set(push.get("paths-ignore", [])) <= ignored:
            gates.append(name)
    return gates


def exclusions(name: str) -> list[list[str]]:
    calls = []
    for steps in yml.workflow_steps(read(name), name).values():
        for step in steps:
            for command in re.split(r"(?<!\\)\n", step["run"] or ""):
                if "gate_wait.py" in command:
                    words = shlex.split(command.replace("\\\n", " "), comments=True)
                    calls.append([words[i + 1] for i, word in enumerate(words) if word == "--exclude"])
    return calls


class GateList(unittest.TestCase):
    def test_the_required_list_is_every_gate_check_but_the_recorders(self) -> None:
        derived = {check for name in gate_workflows() for check in published()[name]}
        self.assertLessEqual(NOT_REQUIRED, derived)
        self.assertEqual(len(wait.REQUIRED_GATES), len(set(wait.REQUIRED_GATES)))
        self.assertEqual(set(wait.REQUIRED_GATES), derived - NOT_REQUIRED)

    def test_each_waiter_excludes_exactly_its_own_jobs(self) -> None:
        for name in sorted(WAITERS):
            with self.subTest(workflow=name):
                calls = exclusions(name)
                self.assertEqual(len(calls), 1, calls)
                self.assertEqual(sorted(calls[0]), sorted(published()[name]))

    def test_no_other_workflow_publishes_a_waiters_own_name(self) -> None:
        names = published()
        for waiter in sorted(WAITERS):
            for other, checks in names.items():
                if other != waiter:
                    with self.subTest(waiter=waiter, other=other):
                        self.assertFalse(set(names[waiter]) & set(checks))


class Reader(unittest.TestCase):
    def test_check_names_follow_github_naming(self) -> None:
        text = ("jobs:\n  plain:\n    runs-on: x\n"
                "  grid:\n    strategy:\n      matrix:\n        os: [a, 'b']\n        py: [\"1\", \"2\"]\n"
                "  named:\n    name: lang (${{ matrix.language }})\n    strategy:\n      matrix:\n"
                "        include:\n          - language: c\n            mode: none\n          - language: go\n")
        self.assertEqual(check_names(text), {
            "plain": ["plain"], "grid": ["grid (a, 1)", "grid (a, 2)", "grid (b, 1)", "grid (b, 2)"],
            "named": ["lang (c)", "lang (go)"]})

    def test_push_filters_are_read_in_both_list_forms(self) -> None:
        text = "on: # note\n  push:\n    branches: [main, next]\n    paths-ignore:\n      - '**/*.md'\n  pull_request:\n"
        self.assertEqual(push_trigger(text), {"branches": ["main", "next"], "paths-ignore": ["**/*.md"]})
        self.assertEqual(push_trigger("on: [push, pull_request]\n"), {})
        self.assertIsNone(push_trigger("on:\n  schedule:\n    - cron: '1 1 * * *'\n"))

    def test_shapes_it_cannot_read_are_refused(self) -> None:
        for text in ("on:\n  push:\n    branches-ignore: [x]\n",
                     "on:\n  push: {branches: [main]}\n",
                     "on:\n  push:\n    branches: ${{ fromJSON('[]') }}\n",
                     "jobs:\n  j:\n    strategy:\n      matrix: ${{ fromJSON(x) }}\n",
                     "jobs:\n  j:\n    name: j ${{ github.ref }}\n",
                     "jobs:\n  j:\n    strategy:\n      matrix:\n        os: [a]\n        include:\n          - os: b\n",
                     "jobs:\n  s:\n    strategy:\n      matrix:\n        os: [a, b]\n        exclude:\n          - os: a\n",
                     "jobs:\n  r:\n    uses: ./.github/workflows/other.yml\n",
                     "on:\n  push:\n    branches:\n  pull_request:\n"):
            with self.subTest(text=text), self.assertRaises(WorkflowError):
                (push_trigger if text.startswith("on:") else check_names)(text)


if __name__ == "__main__":
    unittest.main()
