#!/usr/bin/env python3
"""Refuse an undeclared raise of .github/perf-budgets.json. A workload count or
tolerance_pct may rise only on a commit that touches nothing but the budget and
carries, for each raised key, the line

    Budget-Raise: .github/perf-budgets.json <key> <from> -> <to>

The window is merge-base(base, head)..head on a pull request and before..head
on a push; a budget absent at the window's base is a seed. Exit 0 clean or
skipped, 1 a finding, 2 the window could not be read.
"""
import argparse
import json
import re
import subprocess
import sys
from decimal import Decimal, InvalidOperation

DOCUMENT = ".github/perf-budgets.json"
PREFIX = "Budget-Raise:"
DECLARATION = re.compile(r"Budget-Raise: (\S+) (\S+) (\S+) -> (\S+)")
ZERO = "0" * 40


class WindowError(Exception):
    """The history could not be read."""


def git(*args: str) -> str:
    done = subprocess.run(["git", *args], capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise WindowError(f"git {' '.join(args)}: {done.stderr.strip() or done.returncode}")
    return done.stdout


def numbers(commit: str) -> dict[str, Decimal] | None:
    """The budget's raisable values at commit, keyed workloads.<name> and tolerance_pct; None when absent."""
    if not git("ls-tree", "--name-only", commit, "--", DOCUMENT).strip():
        return None
    try:
        document = json.loads(git("show", f"{commit}:{DOCUMENT}"))
    except ValueError:
        return {}
    if not isinstance(document, dict):
        return {}
    workloads = document.get("workloads")
    pairs = [(f"workloads.{name}", value) for name, value in (workloads.items() if isinstance(workloads, dict) else ())]
    pairs.append(("tolerance_pct", document.get("tolerance_pct")))
    return {key: Decimal(repr(value)) for key, value in pairs
            if type(value) in (int, float) and Decimal(repr(value)).is_finite()}


def judge(commit: str, declared: set[str]) -> list[str]:
    """Findings for one commit's own raises and declarations; records each declared key."""
    parents = git("rev-list", "--parents", "-n", "1", commit).split()[1:]
    before = [values for values in map(numbers, parents) if values is not None]
    after = numbers(commit) or {}
    raises = {}
    for key, value in after.items():
        olds = [values[key] for values in before if key in values]
        if olds and all(value > old for old in olds):
            raises[key] = (olds, value)
    lines = [line.strip() for line in git("log", "-1", "--format=%B", commit).splitlines()
             if line.strip().startswith(PREFIX)]
    if not raises and not lines:
        return []
    short, findings, pool = commit[:12], [], []
    for line in lines:
        match = DECLARATION.fullmatch(line)
        try:
            pool.append((match[1], match[2], Decimal(match[3]), Decimal(match[4])) if match else None)
        except InvalidOperation:
            pool.append(None)
        if pool[-1] is None:
            findings.append(f"{short}: {line!r} does not parse: write '{PREFIX} {DOCUMENT} <key> <from> -> <to>'")
    pool = [entry for entry in pool if entry]
    others = sorted(set(git("diff-tree", "--cc", "--name-only", "-r", "--no-commit-id", commit).splitlines()) - {DOCUMENT})
    for key, (olds, value) in sorted(raises.items()):
        if others:
            findings.append(f"{short}: {DOCUMENT}: {key} rose {olds[0]} -> {value} on a commit that also touches "
                            f"{', '.join(others)}; a raise lands as its own commit touching only {DOCUMENT}")
            continue
        entry = next((entry for entry in pool if entry[:2] == (DOCUMENT, key) and entry[3] == value
                      and entry[2] in olds), None)
        if entry is None:
            findings.append(f"{short}: {DOCUMENT}: {key} rose {olds[0]} -> {value} without a Budget-Raise line naming it")
            continue
        pool.remove(entry)
        declared.add(key)
    findings += [f"{short}: '{PREFIX} {' '.join(map(str, entry[:2]))} {entry[2]} -> {entry[3]}' declares a raise "
                 "the commit does not make" for entry in pool]
    return findings


def findings(base: str, head: str) -> list[str]:
    start, end = numbers(base), numbers(head)
    if start is None:
        return []
    if end is None:
        return [f"{DOCUMENT} was deleted"]
    declared: set[str] = set()
    found = [line for commit in git("rev-list", "--reverse", f"{base}..{head}").split() for line in judge(commit, declared)]
    return found + [f"{DOCUMENT}: {key} net raised {start[key]} -> {value} with no declared step"
                    for key, value in sorted(end.items())
                    if key in start and value > start[key] and key not in declared]


def window(event: str, base: str | None, before: str | None, head: str | None) -> tuple[str, str] | None:
    if event not in ("pull_request", "push"):
        return None
    if not head:
        raise WindowError(f"a {event} run needs --head")
    if git("rev-parse", "--is-shallow-repository").strip() == "true":
        raise WindowError("the clone is shallow, so the window's commits are not all here: fetch the full history")
    head = git("rev-parse", "--verify", "--end-of-options", f"{head}^{{commit}}").strip()
    if event == "pull_request":
        if not base:
            raise WindowError("a pull_request run needs --base")
        return git("merge-base", base, head).strip(), head
    if not before:
        raise WindowError("a push run needs --before")
    if before == ZERO:
        return None
    if subprocess.run(["git", "merge-base", "--is-ancestor", before, head], capture_output=True, check=False).returncode:
        raise WindowError(f"{before} is not an ancestor of {head}: the push window is not its own commits")
    return before, head


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    for name in ("event", "base", "before", "head"):
        parser.add_argument(f"--{name}", required=name == "event")
    args = parser.parse_args(argv)
    try:
        span = window(args.event, args.base, args.before, args.head)
        if span is None:
            print(f"budget raise: a {args.event} run with this window has nothing to judge; skipping")
            return 0
        found = findings(*span)
    except WindowError as error:
        print(f"budget raise: {error}", file=sys.stderr)
        return 2
    print("\n".join(found or [f"budget raise: {DOCUMENT} not raised without a declaration in {span[0][:12]}..{span[1][:12]}"]))
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
