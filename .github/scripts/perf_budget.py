#!/usr/bin/env python3
"""Count the instructions each declared workload retires under valgrind
callgrind, and hold the counts to the committed budget.

  measure     build the history, run every workload, write a measurement
  check       compare a measurement with the budget
  tighten     stage a copy of the budget lowered to every cheaper workload
  rebaseline  rewrite the budget from a measurement; prints its Budget-Raise lines

Exit 0 pass, 1 over budget (or missing, unknown, another toolchain), 2 an
unreadable budget or measurement, 3 a broken harness: never a budget verdict.
"""
import argparse
import json
import math
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
import perf_history  # noqa: E402  pylint: disable=wrong-import-position

DOCUMENT = ".github/perf-budgets.json"
COUNTER = "callgrind-ir"
SCHEMA_VERSION = 1
FIELDS = {"schema_version", "counter", "tolerance_pct", "toolchain", "workloads"}
TOOLCHAIN = ("python", "valgrind", "libc")
DEFAULT_TOLERANCE_PCT = 1.0
# name -> (argv after the interpreter, run in the history; exit status; stdout prefix). The history
# has gaps on purpose, so author-stats exits 1; perf_reauthor_mapping.py builds reauthor --older's
# mapping as its main() does and stops before the git-filter-repo rewrite.
WORKLOADS = {
    "author-stats": (("-m", "git_coauthorship.author_stats"), 1, f"{perf_history.COMMITS} commits, "),
    "reauthor-mapping": (("{code}/perf_reauthor_mapping.py",), 0, "mapping "),
}
PINNED_ENV = {"PYTHONHASHSEED": "0", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
              "LC_ALL": "C.UTF-8", "GIT_CONFIG_NOSYSTEM": "1"}
# git writes its whole output before the pipe sees any of it, so the reader's poll loop takes the same
# number of turns on every run: streamed, two runs of one tree differed by 1.3%.
GIT_SHIM = '#!/bin/sh\nout=$(mktemp) || exit 125\n"{git}" "$@" > "$out"\nstatus=$?\ncat "$out"\nrm -f "$out"\nexit $status\n'
REBASELINE = (f"to accept the new counts, run `perf_budget.py rebaseline --measurement <file> --budget {DOCUMENT}` "
              "on this run's perf-measurement artifact and commit the budget alone with the Budget-Raise lines it prints")


class BudgetError(ValueError):
    """A budget or measurement the gate cannot read."""


class HarnessError(Exception):
    """The measurement could not be made."""


def count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def validate(document: Any) -> dict:
    if not isinstance(document, dict) or set(document) != FIELDS:
        raise BudgetError(f"the budget's keys must be exactly {', '.join(sorted(FIELDS))}")
    if not count(document["schema_version"]) or document["schema_version"] != SCHEMA_VERSION:
        raise BudgetError(f"schema_version must be {SCHEMA_VERSION}")
    if document["counter"] != COUNTER:
        raise BudgetError(f"counter must be {COUNTER!r}")
    tolerance = document["tolerance_pct"]
    if type(tolerance) not in (int, float) or not math.isfinite(tolerance) or not 0 <= tolerance < 100:
        raise BudgetError("tolerance_pct must be a finite number in [0, 100)")
    toolchain(document)
    workloads = document["workloads"]
    if not isinstance(workloads, dict) or not workloads or not all(map(count, workloads.values())):
        raise BudgetError("workloads must map each name to a positive integer count")
    return document


def toolchain(document: dict) -> dict:
    chain = document["toolchain"]
    if not isinstance(chain, dict) or set(chain) != set(TOOLCHAIN) or not all(
            isinstance(value, str) and value for value in chain.values()):
        raise BudgetError(f"toolchain must map exactly {', '.join(TOOLCHAIN)} to non-empty strings")
    return chain


def validate_measurement(measurement: Any) -> dict:
    if not isinstance(measurement, dict) or not {"counter", "toolchain", "counts"} <= set(measurement):
        raise BudgetError("a measurement holds counter, toolchain and counts")
    if measurement["counter"] != COUNTER:
        raise BudgetError(f"the measurement's counter must be {COUNTER!r}")
    toolchain(measurement)
    counts = measurement["counts"]
    if not isinstance(counts, dict) or not all(map(count, counts.values())):
        raise BudgetError("counts must map each name to a positive integer count")
    return measurement


def load(path: Path, judge) -> dict:
    try:
        return judge(json.loads(path.read_text(encoding="utf-8"), parse_constant=float))
    except (OSError, ValueError) as error:
        raise BudgetError(f"{path}: {error}") from None


def callgrind_total(path: Path) -> int:
    """The one Ir summary of a callgrind output; anything else is a broken run."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as error:
        raise HarnessError(f"cannot read {path}: {error}") from None
    events = [line for line in lines if line.startswith("events:")]
    summaries = [line.split(":", 1)[1].strip() for line in lines if line.startswith("summary:")]
    if events != ["events: Ir"] or len(summaries) != 1 or not summaries[0].isdigit() or not int(summaries[0]):
        raise HarnessError(f"{path} holds no single nonzero Ir summary")
    return int(summaries[0])


def current_toolchain() -> dict[str, str]:
    try:
        valgrind = subprocess.run(["valgrind", "--version"], capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError) as error:
        raise HarnessError(f"valgrind --version failed: {error}") from None
    return {"python": sys.version.splitlines()[0], "valgrind": valgrind.strip() + package("valgrind"),
            "libc": (" ".join(platform.libc_ver()).strip() or "unknown") + package("libc6")}


def package(name: str) -> str:
    """The installed package's revision, so a distribution patch to it reads as another toolchain."""
    try:
        done = subprocess.run(["dpkg-query", "-W", "-f", "${Version}", name], capture_output=True, text=True, check=False)
    except OSError:
        return ""
    return f", package {name} {done.stdout.strip()}" if done.returncode == 0 and done.stdout.strip() else ""


def measure(work: Path) -> dict:
    """One callgrind run per workload over a fresh history and a byte-compiled copy of the code, as a
    wheel install leaves it."""
    chain = current_toolchain()
    git = shutil.which("git")
    if git is None:
        raise HarnessError("git is not on PATH")
    code, history, home, shim, tmp, out = (work / name for name in ("code", "history", "home", "bin", "tmp", "callgrind"))
    try:
        work.mkdir(parents=True)
        shutil.copytree(ROOT / "git_coauthorship", code / "git_coauthorship",
                        ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy2(HERE / "perf_reauthor_mapping.py", code)
        subprocess.run([sys.executable, "-m", "compileall", "-q", str(code / "git_coauthorship")], check=True)
        for directory in (home, shim, tmp, out):
            directory.mkdir()
        (shim / "git").write_text(GIT_SHIM.format(git=git), encoding="utf-8")
        (shim / "git").chmod(0o755)
        perf_history.build(history)
    except (OSError, subprocess.CalledProcessError) as error:
        raise HarnessError(f"cannot prepare {work}: {error}") from None
    env = {"PATH": f"{shim}{os.pathsep}{os.environ.get('PATH', '')}", "HOME": str(home), "TMPDIR": str(tmp),
           "PYTHONPATH": str(code), **PINNED_ENV}
    counts, wall = {}, {}
    for name, (argv, status, prefix) in WORKLOADS.items():
        target = out / f"{name}.out"
        started = time.monotonic()
        done = subprocess.run(["valgrind", "--tool=callgrind", f"--callgrind-out-file={target}", sys.executable,
                               *(word.format(code=code) for word in argv)],
                              cwd=history, env=env, capture_output=True, text=True, check=False)
        wall[name] = round(time.monotonic() - started, 1)
        if done.returncode != status or not done.stdout.startswith(prefix):
            raise HarnessError(f"{name} exited {done.returncode} (expected {status}) printing "
                               f"{done.stdout[:200]!r}: {done.stderr[-500:]}")
        counts[name] = callgrind_total(target)
    return {"schema_version": SCHEMA_VERSION, "counter": COUNTER, "toolchain": chain,
            "counts": counts, "wall_seconds": wall}


def check(document: dict, measurement: dict) -> tuple[bool, list[str]]:
    """(failed, report lines): a toolchain change compares nothing and fails."""
    budgets, counts = document["workloads"], measurement["counts"]
    tolerance = document["tolerance_pct"]
    lines = ["| workload | count | budget | verdict |", "|---|---|---|---|"]
    moved = [f"{field}: {document['toolchain'][field]!r} -> {measurement['toolchain'][field]!r}"
             for field in TOOLCHAIN if document["toolchain"][field] != measurement["toolchain"][field]]
    failed = bool(moved)
    for name in sorted(set(budgets) | set(counts)):
        measured, budget = counts.get(name), budgets.get(name)
        if moved:
            verdict = "not compared"
        elif budget is None:
            verdict = "unknown: the budget does not declare it"
        elif measured is None:
            verdict = "missing from the measurement"
        else:
            verdict = "over budget" if measured > budget * (1 + tolerance / 100) else "ok"
        failed = failed or verdict != "ok"
        lines.append(f"| {name} | {measured or '-'} | {budget or '-'} | {verdict} |")
    if moved:
        lines.append(f"\nThe toolchain differs from the budget's ({'; '.join(moved)}), so no count was compared; "
                     f"{REBASELINE}.")
    elif failed:
        lines.append(f"\nTolerance {tolerance}%. A workload that must cost more is a declared raise: {REBASELINE}.")
    return failed, lines


def tighten(document: dict, measurement: dict) -> dict | None:
    """The budget with each count below budget * (1 - tolerance/2) lowered to it; None when nothing drops."""
    if check(document, measurement)[0]:
        return None
    margin = document["tolerance_pct"] / 200
    counts = measurement["counts"]
    lowered = {name: counts[name] for name, budget in document["workloads"].items() if counts[name] < budget * (1 - margin)}
    return {**document, "workloads": {**document["workloads"], **lowered}} if lowered else None


def rebaseline(document: dict | None, measurement: dict) -> tuple[dict, list[str]]:
    counts = measurement["counts"]
    if set(counts) != set(WORKLOADS):
        raise BudgetError(f"the measurement must count exactly {', '.join(WORKLOADS)}")
    tolerance = document["tolerance_pct"] if document else DEFAULT_TOLERANCE_PCT
    new = {"schema_version": SCHEMA_VERSION, "counter": COUNTER, "tolerance_pct": tolerance,
           "toolchain": measurement["toolchain"], "workloads": dict(sorted(counts.items()))}
    old = document["workloads"] if document else {}
    return new, [f"Budget-Raise: {DOCUMENT} workloads.{name} {old[name]} -> {value}"
                 for name, value in new["workloads"].items() if name in old and value > old[name]]


def dump(document: dict) -> str:
    return json.dumps(document, indent=2, allow_nan=False) + "\n"


def run(args: argparse.Namespace) -> int:
    if args.command == "measure":
        measurement = measure(args.work)
        args.out.write_text(json.dumps(measurement, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(measurement["counts"], indent=2))
        return 0
    measurement = load(args.measurement, validate_measurement)
    if args.command == "rebaseline":
        new, raises = rebaseline(load(args.budget, validate) if args.budget.exists() else None, measurement)
        args.budget.write_text(dump(new), encoding="utf-8")
        print("\n".join(raises or ["No count rose: the commit needs no Budget-Raise line."]))
        return 0
    document = load(args.budget, validate)
    if args.command == "check":
        failed, lines = check(document, measurement)
        report = "\n".join(["### Instruction counts (callgrind Ir)", "", *lines]) + "\n"
        print(report)
        if args.summary:
            with args.summary.open("a", encoding="utf-8") as summary:
                summary.write(report)
        return 1 if failed else 0
    lowered = tighten(document, measurement)
    if lowered is None:
        print("No workload fell below its budget by half the tolerance: nothing to lower.")
        return 0
    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / Path(DOCUMENT).name).write_text(dump(lowered), encoding="utf-8")
    print(f"Lowered: {json.dumps(document['workloads'])} -> {json.dumps(lowered['workloads'])}")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    measuring = commands.add_parser("measure")
    measuring.add_argument("--work", type=Path, required=True, help="a directory that does not exist yet")
    measuring.add_argument("--out", type=Path, required=True)
    for name in ("check", "tighten", "rebaseline"):
        command = commands.add_parser(name)
        command.add_argument("--budget", type=Path, required=True)
        command.add_argument("--measurement", type=Path, required=True)
    commands.choices["check"].add_argument("--summary", type=Path)
    commands.choices["tighten"].add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        return run(args)
    except BudgetError as error:
        print(f"perf budget: {error}", file=sys.stderr)
        return 2
    except Exception as error:
        # Whatever else escapes is the harness, and must never read as exit 1, over budget.
        print(f"perf budget: the harness broke, so nothing was judged: {error!r}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
