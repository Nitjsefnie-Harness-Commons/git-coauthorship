#!/usr/bin/env python3
"""Wait until a commit's gate checks have succeeded and every other check
from its push runs has passed; refuse otherwise."""
import argparse
import json
import subprocess
import sys
import time
from typing import Any, Callable

# Check-run names exactly as GitHub publishes them. test_gate_list.py derives
# the set from the workflows and fails when the two differ.
REQUIRED_GATES = (
    "actionlint", "pip-audit", "analyze (actions)", "analyze (python)",
    "pycodestyle", "pylint", "pyright", "wheel", "coverage",
    *(f"suites ({os}, {python})" for os in ("ubuntu-latest", "windows-latest", "macos-latest")
      for python in ("3.11", "3.12", "3.13", "3.14")),
)

PASSING = ("success", "skipped", "neutral")
DEADLINE_SECONDS = 45 * 60
POLL_SECONDS = 20
PAGE = 100

Fetch = Callable[[str], dict[str, Any]]
Row = tuple[str, str, str]


class GateWaitError(Exception):
    """The checks could not be read."""


def gh_get(path: str) -> dict[str, Any]:
    done = subprocess.run(["gh", "api", "-H", "Cache-Control: no-cache", path],
                          capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise GateWaitError(f"`gh api {path}` exited {done.returncode}: "
                            f"{done.stderr.strip() or 'no output'}")
    try:
        return json.loads(done.stdout)
    except ValueError as error:
        raise GateWaitError(f"`gh api {path}` printed no JSON: {error}") from error


def listing(fetch: Fetch, path: str, key: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    separator = "&" if "?" in path else "?"
    page = 1
    while True:
        batch = fetch(f"{path}{separator}per_page={PAGE}&page={page}")[key]
        items.extend(batch)
        if len(batch) < PAGE:
            return items
        page += 1


def judged_rows(fetch: Fetch, repo: str, sha: str, exclude: tuple[str, ...]) -> list[Row]:
    # Check runs first: a run exists before its checks, so every Actions suite
    # read here is in the run listing read next.
    checks = listing(fetch, f"repos/{repo}/commits/{sha}/check-runs", "check_runs")
    runs = listing(fetch, f"repos/{repo}/actions/runs?head_sha={sha}", "workflow_runs")
    event_of = {run["check_suite_id"]: run["event"] for run in runs}
    # A check with no workflow run behind it (another app's) is judged too.
    return [(check["status"], check["conclusion"] or "", check["name"]) for check in checks
            if check["name"] not in exclude
            and event_of.get(check["check_suite"]["id"], "push") == "push"]


def show(rows: list[Row], heading: str) -> None:
    print(heading)
    for row in rows:
        print("    " + "\t".join(row))


def wait(fetch: Fetch, repo: str, sha: str, exclude: tuple[str, ...],
         required: tuple[str, ...] = REQUIRED_GATES, deadline_seconds: float = DEADLINE_SECONDS,
         clock: Callable[[], float] = time.monotonic,
         sleep: Callable[[float], None] = time.sleep) -> int:
    end = clock() + deadline_seconds
    while True:
        rows = judged_rows(fetch, repo, sha, exclude)
        pending = [row for row in rows if row[0] != "completed"]
        reported = {row[2] for row in rows}
        missing = [("not reported", "", name) for name in required if name not in reported]
        if not pending and not missing:
            show(rows, f"Every check on {sha} completed:")
            bad = [row for row in rows if row[1] not in PASSING
                   or (row[2] in required and row[1] != "success")]
            if bad:
                show(bad, "Refusing: these did not pass (a required gate must succeed):")
                return 1
            print("Every required gate succeeded and every other check passed.")
            return 0
        if clock() >= end:
            show(pending + missing, f"Timed out after {deadline_seconds:g} seconds on {sha}; not finished:")
            return 1
        show(pending + missing, "Waiting for:")
        sleep(POLL_SECONDS)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--exclude", action="append", required=True,
                        help="this workflow's own job name, matched exactly; repeat per job")
    parser.add_argument("--deadline-minutes", type=float, default=DEADLINE_SECONDS / 60)
    arguments = parser.parse_args(argv)
    try:
        return wait(gh_get, arguments.repo, arguments.sha, tuple(arguments.exclude),
                    deadline_seconds=arguments.deadline_minutes * 60)
    except (GateWaitError, KeyError, TypeError) as error:
        print(f"gate wait: cannot read the checks: {error!r}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
