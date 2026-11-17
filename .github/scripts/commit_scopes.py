#!/usr/bin/env python3
"""Refuse an outgoing commit whose scope names a workflow but whose type is
not ci: a workflow's `name:` is a scope only with type `ci`.

The names are DERIVED from the top-level `name:` of every workflow at HEAD,
so a renamed workflow cannot leave the rule in silence; a `name:` this reader
cannot read refuses. Only outgoing commits are judged, never merged history:
origin/main..HEAD, or on a push the pushed range before..HEAD, since main
already holds a pushed head. A subject that does not parse is listed.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

BASE_BRANCH = "main"

WORKFLOW_DIR = ".github/workflows"

# Set by the actionlint workflow's scope step to github.event.before.
PUSH_BEFORE = "GIT_COAUTHORSHIP_PUSH_BEFORE"


class GateError(Exception):
    """The answer could not be established."""


def git(root: Path, *arguments: str, what: str) -> str:
    command = ("git", "-C", str(root)) + arguments
    done = subprocess.run(command, capture_output=True, text=True, check=False)
    if done.returncode != 0:
        detail = done.stderr.strip() or "no output"
        raise GateError(
            f"cannot {what}: `{' '.join(command)}` exited {done.returncode}: {detail}")
    return done.stdout


MAPPING = re.compile(r"^(?P<key>[A-Za-z_][A-Za-z0-9_.-]*):(?:[ \t]+(?P<value>.*))?$")

# A quoted, anchored, aliased, tagged, block or flow value is not plain.
NOT_PLAIN = re.compile(r"""["'|>&*![\]{}]""")

# ` #` would end a plain scalar; refused, not stripped.
TRAILING_COMMENT = re.compile(r"\s#")


def indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def skippable(line: str) -> bool:
    stripped = line.strip()
    return not stripped or stripped.startswith("#")


def tracked_files(root: Path) -> tuple[str, ...]:
    return tuple(f for f in
                 git(root, "ls-tree", "-r", "--name-only", "--full-tree", "HEAD",
                     what="list the tracked tree").split("\n") if f)


def workflow_files(files: tuple[str, ...]) -> list[str]:
    return sorted(f for f in files
                  if f.startswith(WORKFLOW_DIR + "/")
                  and (f.endswith(".yml") or f.endswith(".yaml")))


def refuse_name(workflow: str, reason: str) -> GateError:
    return GateError(
        f"{workflow}: {reason} The workflow-name set would be incomplete: "
        f"spell the name as a plain top-level `name:` line, or teach this "
        f"check the shape.")


def workflow_name(workflow: str, text: str) -> str:
    """The single column-0 `name:`; a job's or step's is indented."""
    lines = text.splitlines()
    matches = [(index, match) for index, line in enumerate(lines)
               if not skippable(line) and indent_of(line) == 0
               and (match := MAPPING.match(line)) is not None and match["key"] == "name"]
    if not matches:
        raise refuse_name(workflow, "has no top-level `name:` this reader can read.")
    if len(matches) != 1:
        raise GateError(
            f"{workflow} has {len(matches)} top-level `name:` mappings, and which "
            f"one names the workflow cannot be established")
    at, matched = matches[0]
    value = (matched["value"] or "").strip()
    where = f"the top-level `name:` at line {at + 1}"
    if not value:
        raise refuse_name(workflow, f"{where} carries no value.")
    if NOT_PLAIN.match(value[0]):
        raise refuse_name(workflow, f"{where} is {value!r}, not a plain scalar.")
    if TRAILING_COMMENT.search(value):
        raise refuse_name(workflow, f"{where} carries a trailing comment.")
    following = next((line for line in lines[at + 1:] if not skippable(line)), "")
    if indent_of(following) > 0:
        raise refuse_name(workflow, f"{where} continues onto an indented line.")
    return value


def workflow_name_set(root: Path) -> set[str]:
    files = tracked_files(root)
    if not files:
        raise GateError("HEAD tracks no files, so there are no workflows to read")
    names = {workflow_name(workflow, git(root, "cat-file", "blob", f"HEAD:{workflow}",
                                         what=f"read {workflow}"))
             for workflow in workflow_files(files)}
    if not names:
        raise GateError(
            f"no workflow under {WORKFLOW_DIR}/ carries a readable `name:`, "
            f"so the workflow-name set is empty and no subject can be judged")
    return names


def fetch_base(root: Path) -> None:
    # Unshallowed, or the graft hides which of main's commits the head carries.
    arguments = ["fetch", "--no-tags", "--quiet"]
    if git(root, "rev-parse", "--is-shallow-repository",
           what="ask whether the checkout is shallow").strip() == "true":
        arguments.append("--unshallow")
    arguments += ["origin", f"+refs/heads/{BASE_BRANCH}:refs/remotes/origin/{BASE_BRANCH}"]
    git(root, *arguments, what=f"fetch origin/{BASE_BRANCH}")


SUBJECT = re.compile(
    r"^(?P<type>[a-z]+)!?\((?P<scope>[^()\s](?:[^()]*[^()\s])?)\)!?:"
    r"\s?(?P<summary>\S.*)?$")


COMMIT_ID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


def outgoing_base(root: Path, event: str, before: str) -> tuple[str, str]:
    """(base, range label) for the commits this run judges."""
    if event != "push":
        fetch_base(root)
        return (git(root, "rev-parse", "--verify",
                    f"refs/remotes/origin/{BASE_BRANCH}^{{commit}}",
                    what=f"resolve origin/{BASE_BRANCH}").strip(),
                f"origin/{BASE_BRANCH}..HEAD")
    if not before.strip("0"):
        raise GateError(
            "this push names no previous tip (a new branch, or a first push), so "
            "which commits it adds cannot be established; refusing to judge none")
    if not COMMIT_ID.fullmatch(before):
        raise GateError(f"the pushed range's base {before!r} is not a commit id")
    ancestry = subprocess.run(
        ("git", "-C", str(root), "merge-base", "--is-ancestor", before, "HEAD"),
        capture_output=True, text=True, check=False)
    if ancestry.returncode == 1:
        raise GateError(
            f"the pushed range's base {before} is not an ancestor of HEAD (a force "
            f"push?), so which commits this push adds cannot be established")
    if ancestry.returncode != 0:
        raise GateError(
            f"cannot establish whether {before} is an ancestor of HEAD: git exited "
            f"{ancestry.returncode}: {ancestry.stderr.strip() or 'no output'}")
    return before, f"{before}..HEAD"


def outgoing_commits(root: Path, base: str) -> list[tuple[str, str]]:
    listing = git(root, "log", "--format=%H%x09%s", f"{base}..HEAD",
                  what="list the outgoing commits")
    commits = []
    for line in listing.split("\n"):
        if not line.strip():
            continue
        sha, _, subject = line.partition("\t")
        commits.append((sha.strip(), subject.strip()))
    return commits


def check(root: Path, event: str, before: str) -> int:
    git(root, "rev-parse", "--verify", "HEAD^{commit}",
        what="resolve the checked-out head")
    base, span = outgoing_base(root, event, before)
    names = workflow_name_set(root)
    unexamined = []
    violations = []
    commits = outgoing_commits(root, base)
    for sha, subject in commits:
        match = SUBJECT.match(subject)
        if match is None:
            unexamined.append((sha, subject))
        elif match["scope"] in names and match["type"] != "ci":
            violations.append((sha, subject, match["scope"], match["type"]))
    examined = len(commits) - len(unexamined)

    if violations:
        plural = "s" if len(violations) != 1 else ""
        verb = "pair" if len(violations) != 1 else "pairs"
        print(f"{len(violations)} commit{plural} in {span} {verb} a "
              f"workflow-name scope with a type other than `ci`:")
        for sha, subject, scope, commit_type in violations:
            print(f"  {sha} {subject}")
            print(f"    scope `{scope}` is the name of a workflow under "
                  f"{WORKFLOW_DIR}/; type `{commit_type}` is not `ci`.")
        print("A workflow's `name:` is a scope only with the `ci` type: use "
              "the `ci` type on that scope, or take a scope that names what "
              "the commit is actually about.")
        return 1

    plural = "s" if examined != 1 else ""
    print(f"Examined {examined} commit subject{plural} in {span}; "
          f"{len(unexamined)} not examined.")
    print("  Only the OUTGOING range is examined, never merged history; a")
    print("  subject that does not parse (a merge commit, a non-conforming")
    print("  subject) is listed below and is never a failure:")
    for sha, subject in unexamined:
        print(f"    {sha} {subject}")
    print("No commit pairs a workflow-name scope with a type other than `ci`.")
    return 0


def main(argv: list[str]) -> int:
    root = Path(__file__).resolve().parents[2]
    rest = list(argv[1:])
    while rest:
        argument = rest.pop(0)
        if argument == "--root" and rest:
            root = Path(rest.pop(0))
        else:
            print("usage: commit_scopes.py [--root DIR]", file=sys.stderr)
            return 2
    try:
        return check(root, os.environ.get("GITHUB_EVENT_NAME", ""),
                     os.environ.get(PUSH_BEFORE, ""))
    except GateError as refusal:
        print(f"commit scopes: {refusal}", file=sys.stderr)
        return 1
    except OSError as failure:
        print(f"commit scopes: cannot run git: {failure}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
