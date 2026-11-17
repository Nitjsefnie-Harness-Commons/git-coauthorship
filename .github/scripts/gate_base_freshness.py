#!/usr/bin/env python3
"""Refuse a head lacking a commit main holds that touches a file a required
check reads: with strict required checks off, a green head vouches only for
the tree it checked out.

The set is derived from the workflows; only REQUIRED_JOBS is hand-held. Text
only, so unseen: config a tool reads unnamed (setup.cfg, pyrightconfig.json,
pyproject.toml), the merge-marker step's whole-tree `.` (counting it would be
strict checks by the back door, the maintainer's call), and what a script
reaches from inside itself.
"""

import os
import posixpath
import re
import subprocess
import sys
from pathlib import Path

# No ruleset exists yet, so these are the jobs every pull request runs: each
# workflow below has an unfiltered `pull_request:` trigger. pr-gate and
# pin-guard judge the base, claim runs on comments. A name with no job
# behind it refuses rather than shrinking the set.
REQUIRED_JOBS = ("actionlint", "suites", "wheel", "coverage", "pycodestyle",
                 "pylint", "pyright", "pip-audit", "analyze", "comment-share",
                 "size-baseline")

BASE_BRANCH = "main"

# Under ARG_MAX: a refusal by name, not E2BIG.
PATHSPECS_MAX_BYTES = 65536

WORKFLOW_DIR = ".github/workflows"


class GateError(Exception):
    """The answer could not be established."""


class WorkflowError(GateError):
    """A workflow shape this parser does not model."""


def git(root: Path, *arguments: str, what: str) -> str:
    command = ("git", "-C", str(root)) + arguments
    done = subprocess.run(command, capture_output=True, text=True, check=False)
    if done.returncode != 0:
        detail = done.stderr.strip() or "no output"
        raise GateError(
            f"cannot {what}: `{' '.join(command)}` exited {done.returncode}: {detail}")
    return done.stdout


# The YAML subset these workflows use; any other shape refuses, since a
# guessed shape is a smaller, green set.

MAPPING = re.compile(r"^(?P<key>[A-Za-z_][A-Za-z0-9_.-]*):(?:[ \t]+(?P<value>.*))?$")
SEQUENCE = re.compile(r"^- (?P<rest>.+)$")
BLOCK_SCALAR = re.compile(r"^[|>][0-9+-]*$")

Step = dict[str, str | None]


def indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def skippable(line: str) -> bool:
    stripped = line.strip()
    return not stripped or stripped.startswith("#")


def block_end(lines: list[str], start: int, indent: int) -> int:
    index = start + 1
    while index < len(lines):
        line = lines[index]
        if not skippable(line) and indent_of(line) <= indent:
            break
        index += 1
    return index


def block_scalar(lines: list[str], start: int, key_indent: int,
                 limit: int) -> tuple[str, int]:
    body = []
    index = start + 1
    while index < limit:
        line = lines[index]
        if line.strip() and indent_of(line) <= key_indent:
            break
        body.append(line)
        index += 1
    leads = [indent_of(line) for line in body if line.strip()]
    lead = min(leads) if leads else key_indent + 2
    return "\n".join(line[lead:] if len(line) > lead else "" for line in body), index


def top_level_keys(lines: list[str], key: str) -> list[tuple[int, re.Match[str]]]:
    found = []
    for index, line in enumerate(lines):
        if skippable(line) or indent_of(line) != 0:
            continue
        match = MAPPING.match(line)
        if match is not None and match["key"] == key:
            found.append((index, match))
    return found


def workflow_steps(text: str, workflow: str) -> dict[str, list[Step]]:
    lines = text.splitlines()
    job_matches = top_level_keys(lines, "jobs")
    if len(job_matches) != 1:
        raise WorkflowError(
            f"{workflow} has {len(job_matches)} top-level `jobs:` mappings; this "
            f"reader models exactly one, so which jobs it defines cannot be "
            f"established")
    if top_level_keys(lines, "defaults"):
        raise WorkflowError(
            f"{workflow} sets top-level `defaults:`, which moves where steps run; "
            f"this reader models only a step's own `working-directory:`")
    if job_matches[0][1]["value"] is not None:
        raise WorkflowError(
            f"{workflow} writes `jobs:` as a flow mapping; this reader models "
            f"only the block form")

    jobs: dict[str, tuple[int, int]] = {}
    index = job_matches[0][0] + 1
    while index < len(lines):
        line = lines[index]
        if skippable(line):
            index += 1
            continue
        if indent_of(line) < 2:
            break
        if indent_of(line) != 2:
            raise WorkflowError(
                f"{workflow}: expected a job entry under `jobs:`, found {line!r}")
        match = MAPPING.match(line.strip())
        if not match or match["value"] is not None:
            raise WorkflowError(f"{workflow}: expected a job entry, found {line!r}")
        name = match["key"]
        if name in jobs:
            raise WorkflowError(f"{workflow}: duplicate job {name!r}")
        jobs[name] = (index + 1, block_end(lines, index, 2))
        index = jobs[name][1]

    return {name: job_steps(lines, name, span, workflow)
            for name, span in jobs.items()}


def job_steps(lines: list[str], name: str, span: tuple[int, int],
              workflow: str) -> list[Step]:
    start, end = span
    steps: list[Step] = []
    index = start
    while index < end:
        line = lines[index]
        if skippable(line):
            index += 1
            continue
        if indent_of(line) != 4:
            raise WorkflowError(
                f"{workflow}: expected a step list or a job key in job "
                f"`{name}`, found {line!r}")
        match = MAPPING.match(line.strip())
        if not match:
            raise WorkflowError(
                f"{workflow}: expected a job key in job `{name}`, found {line!r}")
        if match["key"] == "defaults":
            raise WorkflowError(
                f"{workflow}: job `{name}` sets `defaults:`, which moves where its "
                f"steps run; this reader models only a step's own `working-directory:`")
        if match["key"] == "steps" and match["value"] is not None:
            raise WorkflowError(
                f"{workflow}: `steps:` carries a value in job `{name}`; this "
                f"reader models only the block form")
        if match["key"] != "steps":
            # The whole block, so `permissions:`'s children are not read as steps.
            index = block_end(lines, index, 4)
            continue
        index = step_entries(lines, name, index + 1, block_end(lines, index, 4),
                             workflow, steps)
    return steps


def step_entries(lines: list[str], name: str, start: int, end: int,
                 workflow: str, steps: list[Step]) -> int:
    index = start
    while index < end:
        line = lines[index]
        if skippable(line):
            index += 1
            continue
        entry = SEQUENCE.match(line.strip()) if indent_of(line) == 6 else None
        if entry is None:
            raise WorkflowError(
                f"{workflow}: expected a step entry in job `{name}`, found {line!r}")
        stop = block_end(lines, index, 6)
        steps.append(step_fields(lines, name, index + 1, stop, workflow,
                                 entry["rest"]))
        index = stop
    return index


def apply_field(lines: list[str], end: int, key: str, value: str, at: int,
                step: Step, workflow: str) -> int:
    if key == "run":
        if not value or BLOCK_SCALAR.match(value):
            step["run"], after = block_scalar(lines, at, 8, end)
            return after
        step["run"] = value
    elif key == "uses":
        step["uses"] = value.split()[0] if value else None
    elif key == "with":
        if value:
            raise WorkflowError(
                f"{workflow}: `with: {value}` is a flow mapping; this reader models "
                f"only the block form, whose values may name files")
        after = block_end(lines, at, 8)
        # Values only: an input's name is not a path, its value may be.
        step["with"] = "\n".join(
            ((match["value"] or "") if (match := MAPPING.match(line.strip())) else "")
            if indent_of(line) == 10 else line for line in lines[at + 1:after])
        return after
    elif key == "working-directory":
        if not value or BLOCK_SCALAR.match(value) or "${{" in value:
            raise WorkflowError(
                f"{workflow}: `working-directory: {value}` is not a literal path, "
                f"so where the step's paths resolve cannot be established")
        step["dir"] = value
    elif not value or BLOCK_SCALAR.match(value):
        # A `with:`/`env:` block is a step's input, not a file it reads.
        return block_end(lines, at, 8)
    return at + 1


def step_fields(lines: list[str], name: str, start: int, end: int,
                workflow: str, dash: str) -> Step:
    # `dash`, the text after the step's `- `, is its first key.
    step: Step = {"run": None, "uses": None, "dir": None, "with": None}
    index = start
    if dash:
        match = MAPPING.match(dash)
        if not match:
            raise WorkflowError(
                f"{workflow}: expected a key on the step entry in job `{name}`, "
                f"found {dash!r}")
        index = apply_field(lines, end, match["key"],
                            (match["value"] or "").strip(), start - 1, step, workflow)
    while index < end:
        line = lines[index]
        if skippable(line):
            index += 1
            continue
        match = MAPPING.match(line.strip()) if indent_of(line) == 8 else None
        if not match:
            raise WorkflowError(
                f"{workflow}: expected a key in a step of job `{name}`, found {line!r}")
        index = apply_field(lines, end, match["key"],
                            (match["value"] or "").strip(), index, step, workflow)
    return step


# `${{ }}` is expanded before bash sees it, so its text is not a path.
EXPRESSION = re.compile(r"\$\{\{.*?\}\}", re.DOTALL)
CANDIDATE = re.compile(r"[A-Za-z0-9._/-]+")


def candidates(text: str) -> list[str]:
    return CANDIDATE.findall(EXPRESSION.sub(" ", text))


def resolve(candidate: str, files: tuple[str, ...]) -> tuple[str, ...]:
    candidate = candidate[2:] if candidate.startswith("./") else candidate
    candidate = candidate.rstrip("/")
    if not candidate:
        return ()
    if candidate in files:
        return (candidate,)
    prefix = candidate + "/"
    return tuple(f for f in files if f.startswith(prefix))


def tracked_files(root: Path) -> tuple[str, ...]:
    return tuple(f for f in
                 git(root, "ls-tree", "-r", "--name-only", "--full-tree", "HEAD",
                     what="list the tracked tree").split("\n") if f)


def workflow_names(files: tuple[str, ...]) -> list[str]:
    return sorted(f for f in files
                  if f.startswith(WORKFLOW_DIR + "/")
                  and (f.endswith(".yml") or f.endswith(".yaml")))


def gate_paths(root: Path) -> list[str]:
    # Every workflow is parsed: one it cannot read hides steps.
    files = tracked_files(root)
    if not files:
        raise GateError("HEAD tracks no files, so the tree to compare is not there")
    parsed = {}
    for workflow in workflow_names(files):
        parsed[workflow] = workflow_steps(
            git(root, "cat-file", "blob", f"HEAD:{workflow}",
                what=f"read {workflow}"), workflow)
    derived: set[str] = set()
    for job in REQUIRED_JOBS:
        # Unioned over every workflow defining the id: ids are unique per file only.
        defining = sorted(name for name, jobs in parsed.items() if job in jobs)
        if not defining:
            raise GateError(
                f"no workflow under {WORKFLOW_DIR}/ defines the required job "
                f"`{job}`, so the files the required checks read cannot be built: "
                f"put the job back, or update REQUIRED_JOBS if it was renamed")
        for name in defining:
            for step in parsed[name][job]:
                uses = step["uses"] or ""
                if uses.startswith("./"):
                    # A local action reads its whole directory.
                    derived.update(files if uses == "./" else resolve(uses, files))
                for candidate in candidates(step["run"] or ""):
                    derived.update(resolve(candidate, files))
                    if step["dir"]:
                        derived.update(resolve(posixpath.normpath(
                            f"{step['dir']}/{candidate}"), files))
                for candidate in candidates(step["with"] or ""):
                    derived.update(resolve(candidate, files))
    if not derived:
        raise GateError(
            "the required checks name no file in this tree, so there is nothing "
            "to compare and this run can vouch for nothing")
    return sorted(derived)


def fetch_base(root: Path) -> None:
    # Unshallowed, or the graft hides which of main's commits head carries.
    arguments = ["fetch", "--no-tags", "--quiet"]
    if git(root, "rev-parse", "--is-shallow-repository",
           what="ask whether the checkout is shallow").strip() == "true":
        arguments.append("--unshallow")
    arguments += ["origin", f"+refs/heads/{BASE_BRANCH}:refs/remotes/origin/{BASE_BRANCH}"]
    git(root, *arguments, what=f"fetch origin/{BASE_BRANCH}")


def stale_commits(root: Path, head: str, base: str,
                  paths: list[str]) -> list[tuple[str, str, list[str]]]:
    # An empty `--` means every path.
    if not paths:
        raise GateError("the derived path set is empty, so there is nothing to compare")
    sized = sum(len(path) + 1 for path in paths)
    if sized > PATHSPECS_MAX_BYTES:
        raise GateError(
            f"the derived path set is {sized} bytes, past the {PATHSPECS_MAX_BYTES} "
            f"this run will put in an argument list")
    listing = git(root, "log", f"{head}..{base}", "--name-only",
                  "--format=%x00%H%x09%s", "--", *paths,
                  what=f"list what {BASE_BRANCH} holds that this head lacks")
    stale = []
    for chunk in listing.split("\0"):
        if not chunk.strip():
            continue
        header, _, body = chunk.partition("\n")
        sha, _, subject = header.partition("\t")
        stale.append((sha.strip(), subject.strip(),
                      [line for line in body.split("\n") if line.strip()]))
    return stale


def check(root: Path, event: str) -> int:
    head = git(root, "rev-parse", "--verify", "HEAD^{commit}",
               what="resolve the checked-out head").strip()
    fetch_base(root)
    base = git(root, "rev-parse", "--verify", f"refs/remotes/origin/{BASE_BRANCH}^{{commit}}",
               what=f"resolve origin/{BASE_BRANCH}").strip()
    paths = gate_paths(root)
    stale = stale_commits(root, head, base, paths)
    # A pushed head main already holds: main moved past it and no rebase answers.
    if stale and event == "push" and git(
            root, "merge-base", head, base, what="find the merge base").strip() == head:
        print(f"This pushed head is already on {BASE_BRANCH}, which has moved past it.")
        return 0
    if not stale:
        print(f"This head carries every commit on {BASE_BRANCH} that touches "
              f"the {len(paths)} file(s) a required check reads BY NAME.")
        print("  A step reading every tracked file — the `.` spelling, which the "
              "actionlint\n  job's merge-marker step uses — is a named reach "
              "limit and is not\n  counted above; see the module docstring.")
        return 0
    plural = "s" if len(stale) != 1 else ""
    print(f"{BASE_BRANCH} holds {len(stale)} commit{plural} this head does not:")
    for sha, subject, touched in stale:
        print(f"  {sha} {subject}")
        if not touched:
            print("    (a merge commit, which git names no file for; the "
                  "commits it brought in are listed as themselves)")
            continue
        for path in touched:
            print(f"    {path}")
    print(f"Rebase onto {BASE_BRANCH} and push again, so this run's checks read "
          f"the files {BASE_BRANCH} reads.")
    return 1


def main(argv: list[str]) -> int:
    # The repository root: pathspecs resolve against `git -C`'s directory.
    root = Path(__file__).resolve().parents[2]
    print_paths = False
    rest = list(argv[1:])
    while rest:
        argument = rest.pop(0)
        if argument == "--print-paths":
            print_paths = True
        elif argument == "--root" and rest:
            root = Path(rest.pop(0))
        else:
            print("usage: gate_base_freshness.py [--root DIR] [--print-paths]",
                  file=sys.stderr)
            return 2
    try:
        if print_paths:
            for path in gate_paths(root):
                print(path)
            return 0
        return check(root, os.environ.get("GITHUB_EVENT_NAME", ""))
    except GateError as refusal:
        print(f"head freshness: {refusal}", file=sys.stderr)
        return 1
    except OSError as failure:
        print(f"head freshness: cannot run git: {failure}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
