"""Pin the release path's write-token split over every workflow, and run the
tag and wait steps' own `run:` text against stub commands."""
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


lists = load("test_gate_list")
publish = load("test_publish_release")
yml = lists.yml
WorkflowError = lists.WorkflowError

# Commands that run code from the tree; a write job may name none of them.
INTERPRETERS = {"python", "python3", "pip", "pip3", "bash", "sh", "node", "npm", "npx", "make", "source"}
Step = dict[str, str]


def permissions(lines: list[str], entry: tuple[str | None, int, int] | None, indent: int) -> dict[str, str] | None:
    """Scope -> level; None where no block is written."""
    if entry is None:
        return None
    value, start, end = entry
    if value is None:
        return {scope: lists.scalar(found[0] or "") for scope, found in lists.children(lines, start, end, indent).items()}
    value = lists.scalar(value) if value.strip() != "{}" else ""
    if value in ("", "read-all", "write-all"):
        return {} if not value else {"contents": value.split("-")[0]}
    raise WorkflowError(f"unread permissions {value!r}")


def text_of(lines: list[str], entry: tuple[str | None, int, int], indent: int) -> str:
    """A key's scalar; a folded or literal block joined by single spaces."""
    value, start, end = entry
    if value is not None and yml.BLOCK_SCALAR.match(value.strip()):
        return " ".join(yml.block_scalar(lines, start - 1, indent, end)[0].split())
    return lists.scalar(value or "")


def job_steps(lines: list[str], entry: tuple[str | None, int, int]) -> list[Step]:
    value, start, end = entry
    if value is not None:
        raise WorkflowError(f"`steps: {value}` is not a block list")
    steps = []
    for index in range(start, end):
        line = lines[index]
        if yml.skippable(line) or yml.indent_of(line) != 6:
            continue
        item = yml.SEQUENCE.match(line.strip())
        if item is None:
            raise WorkflowError(f"expected a step, found {line!r}")
        stop = yml.block_end(lines, index, 6)
        body = [" " * 8 + item["rest"]] + lines[index + 1:stop]
        step = {}
        for key, field in lists.children(body, 0, len(body), 8).items():
            if key == "run" and (field[0] is None or yml.BLOCK_SCALAR.match(field[0])):
                step[key] = yml.block_scalar(body, field[1] - 1, 8, field[2])[0]
            elif field[0] is not None:
                step[key] = text_of(body, field, 8)
        steps.append(step)
    return steps


def jobs(name: str) -> dict[str, dict]:
    """Each job's effective contents level, needs, `if:` and steps."""
    lines = lists.read(name).splitlines()
    top = yml.top_level_keys(lines, "permissions")
    if len(top) > 1:
        raise WorkflowError(f"{name}: {len(top)} top-level `permissions:` keys")
    level_entry = None
    if top:
        index, match = top[0]
        level_entry = (match["value"], index + 1, yml.block_end(lines, index, 0))
    workflow = permissions(lines, level_entry, 2)
    index = yml.top_level_keys(lines, "jobs")[0][0]
    found = {}
    for job, (_, start, end) in lists.children(lines, index + 1, yml.block_end(lines, index, 0), 2).items():
        keys = lists.children(lines, start, end, 4)
        own = permissions(lines, keys.get("permissions"), 6)
        granted = own if own is not None else workflow
        # No block anywhere leaves the repository default, which may be write.
        contents = "write" if granted is None else granted.get("contents", "none")
        needs = keys.get("needs", ("[]", 0, 0))
        if needs[0] is not None and not needs[0].lstrip().startswith("["):
            needs = (f"[{needs[0]}]", 0, 0)
        found[job] = {"contents": contents, "needs": lists.strings(lines, needs, 6) if needs[0] != "[]" else [],
                      "if": text_of(lines, keys["if"], 4) if "if" in keys else None,
                      "steps": job_steps(lines, keys["steps"]) if "steps" in keys else []}
    found["<workflow>"] = {"permissions": workflow}
    return found


def tracked() -> tuple[str, ...]:
    return tuple(subprocess.run(["git", "-C", str(ROOT), "ls-files"], check=True,
                                capture_output=True, text=True).stdout.split())


def repository_code(step: Step, files: tuple[str, ...]) -> list[str]:
    """What in a step runs or reads the tree: a checkout, a local action, a
    tracked path or an interpreter named in its `run:`."""
    uses = step.get("uses", "")
    reasons = [uses] if uses.startswith(("actions/checkout@", "./")) else []
    words = yml.candidates(step.get("run", ""))
    reasons += [word for word in words if word in INTERPRETERS or yml.resolve(word, files)]
    return reasons


class WriteJobs(unittest.TestCase):
    def test_every_write_job_runs_no_repository_code(self) -> None:
        files = tracked()
        for name in lists.workflow_files():
            writers = {job: spec for job, spec in jobs(name).items() if spec.get("contents") == "write"}
            with self.subTest(workflow=name):
                self.assertLessEqual(len(writers), 1, sorted(writers))
                for job, spec in writers.items():
                    for step in spec["steps"]:
                        self.assertEqual(repository_code(step, files), [], (job, step.get("name")))

    def test_each_waiter_has_one_named_write_job_and_reads_by_default(self) -> None:
        for name, writer in (("release.yml", "release-publish"), ("tag.yml", "tag")):
            with self.subTest(workflow=name):
                found = jobs(name)
                self.assertEqual(found.pop("<workflow>")["permissions"], {"contents": "read"})
                self.assertEqual([job for job, spec in found.items() if spec["contents"] == "write"], [writer])

    def test_publish_needs_the_gates_and_the_build_and_verifies_before_publishing(self) -> None:
        found = jobs("release.yml")
        self.assertLessEqual({"release-gates", "release-build"}, set(found["release-publish"]["needs"]))
        steps = found["release-publish"]["steps"]
        verify = [i for i, step in enumerate(steps) if re.search(r"\bsha256sum -c\b", step.get("run", ""))]
        release = [i for i, step in enumerate(steps) if "gh release" in step.get("run", "")]
        self.assertEqual(len(verify), 1)
        self.assertTrue(release and verify[0] < min(release), (verify, release))
        self.assertNotIn("if", steps[verify[0]])

    def test_each_wait_runs_whenever_its_writer_does(self) -> None:
        release = jobs("release.yml")
        self.assertEqual([step.get("if") for step in release["release-gates"]["steps"]
                          if "gate_wait.py" in step.get("run", "")], [None])
        self.assertIsNone(release["release-gates"]["if"])
        tag = jobs("tag.yml")
        self.assertEqual(tag["tag"]["needs"], ["release-readiness"])
        waits = [step for step in tag["release-readiness"]["steps"] if "gate_wait.py" in step.get("run", "")]
        self.assertEqual(len(waits), 1)
        self.assertEqual(waits[0]["if"].replace("steps.existing.outputs.", "needs.release-readiness.outputs."),
                         tag["tag"]["if"])

    def test_repository_code_is_recognised(self) -> None:
        files = ("git_coauthorship/__init__.py", ".github/scripts/gate_wait.py")
        for step in ({"uses": "actions/checkout@abc"}, {"uses": "./.github/actions/x"},
                     {"run": "python3 -c 'import git_coauthorship'"}, {"run": "x .github/scripts/gate_wait.py"},
                     {"run": "bash step.sh"}):
            with self.subTest(step=step):
                self.assertNotEqual(repository_code(step, files), [])
        self.assertEqual(repository_code({"run": "gh release upload v1 dist/*.whl"}, files), [])

    def test_permission_blocks_are_read_in_every_form(self) -> None:
        text = ("permissions: write-all\njobs:\n  a:\n    steps:\n      - run: x\n"
                "  b:\n    permissions: {}\n  c:\n    permissions:\n      issues: write\n"
                "  d:\n    permissions: read-all\n")
        with unittest.mock.patch.object(lists, "read", lambda name: text):
            found = jobs("x.yml")
        self.assertEqual({job: found[job]["contents"] for job in "abcd"},
                         {"a": "write", "b": "none", "c": "none", "d": "read"})
        with unittest.mock.patch.object(lists, "read", lambda name: "jobs:\n  a:\n    steps:\n      - run: x\n"):
            self.assertEqual(jobs("x.yml")["a"]["contents"], "write")
        twice = "permissions: {}\npermissions: write-all\njobs:\n  a:\n    steps:\n      - run: x\n"
        with unittest.mock.patch.object(lists, "read", lambda name: twice), self.assertRaises(WorkflowError):
            jobs("x.yml")


STUB = """
import json, os, sys
name = os.path.basename(sys.argv[0])
with open(os.environ["STUB_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps([name] + sys.argv[1:]) + "\\n")
if os.environ.get("STUB_FAIL") and os.environ["STUB_FAIL"] in " ".join(sys.argv[1:]):
    sys.stderr.write("stub: HTTP 422\\n")
    sys.exit(1)
if name == "gh" and sys.argv[1:5] == ["api", "--method", "POST", "repos/owner/repo/git/tags"]:
    print("feed" * 10)
if name == "git":
    print("cafe" * 10)
"""


class Steps(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        (self.root / "bin").mkdir()
        for name in ("gh", "git", "python3"):
            stub = self.root / "bin" / name
            stub.write_text(f"#!{sys.executable}\n{STUB}", encoding="utf-8")
            stub.chmod(0o755)
        self.log = self.root / "calls.jsonl"

    def run_step(self, workflow: str, step: str, **environ: str) -> subprocess.CompletedProcess[str]:
        script = self.root / "step.sh"
        script.write_text(publish.step_run(lists.read(workflow), step), encoding="utf-8")
        env = dict(os.environ, PATH=f"{self.root / 'bin'}{os.pathsep}{os.environ['PATH']}",
                   STUB_LOG=str(self.log), GITHUB_STEP_SUMMARY=str(self.root / "summary"),
                   REPO="owner/repo", **environ)
        return subprocess.run(["bash", "-e", str(script)], cwd=self.root, env=env,
                              capture_output=True, text=True, check=False)

    def calls(self) -> list[list[str]]:
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()] \
            if self.log.exists() else []

    def test_the_tag_is_created_as_a_tag_object_then_its_ref(self) -> None:
        done = self.run_step("tag.yml", "Create the tag", TAG="v1.2.0", SHA="ab" * 20)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.calls(), [
            ["gh", "api", "--method", "POST", "repos/owner/repo/git/tags", "-f", "tag=v1.2.0",
             "-f", "message=v1.2.0", "-f", "object=" + "ab" * 20, "-f", "type=commit",
             "-f", "tagger[name]=github-actions[bot]",
             "-f", "tagger[email]=41898282+github-actions[bot]@users.noreply.github.com", "--jq", ".sha"],
            ["gh", "api", "--method", "POST", "repos/owner/repo/git/refs", "-f", "ref=refs/tags/v1.2.0",
             "-f", "sha=" + "feed" * 10, "--silent"]])

    def test_a_malformed_tag_or_commit_is_refused_before_any_call(self) -> None:
        for tag, sha in (("v1.2.0\nx", "ab" * 20), ("v1.2.0;x", "ab" * 20), ("1.2.0", "ab" * 20),
                         ("v1.2.0", "ab" * 19), ("v1.2.0", "ab" * 20 + "\n"), ("v1.2.0", "AB" * 20)):
            with self.subTest(tag=tag, sha=sha):
                done = self.run_step("tag.yml", "Create the tag", TAG=tag, SHA=sha)
                self.assertEqual(done.returncode, 1, done.stdout)
                self.assertIn("Refusing to tag", done.stderr)
                self.assertEqual(self.calls(), [])

    def test_a_failed_tag_object_creates_no_ref(self) -> None:
        done = self.run_step("tag.yml", "Create the tag", TAG="v1.2.0", SHA="ab" * 20, STUB_FAIL="git/tags")
        self.assertEqual(done.returncode, 1, done.stdout)
        self.assertEqual([call[4] for call in self.calls()], ["repos/owner/repo/git/tags"])

    def test_a_tag_dispatch_refuses_a_red_tag_run_and_a_push_does_not(self) -> None:
        own = ["--exclude", "release-readiness", "--exclude", "tag"]
        refuse = ["--refuse-failed", "release-readiness", "--refuse-failed", "tag"]
        for event, extra in (("push", []), ("workflow_dispatch", refuse)):
            with self.subTest(event=event):
                self.log.unlink(missing_ok=True)
                done = self.run_step("tag.yml", "Wait for the gates on this commit",
                                     GITHUB_EVENT_NAME=event, SHA="ab" * 20)
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertEqual(self.calls(), [["python3", ".github/scripts/gate_wait.py", "--repo", "owner/repo",
                                                 "--sha", "ab" * 20, *own, *extra]])

    def test_a_release_on_a_non_tag_ref_is_refused_before_the_wait(self) -> None:
        for ref in ("refs/heads/main", "", "refs/pull/1/merge"):
            with self.subTest(ref=ref):
                done = self.run_step("release.yml", "Wait for the gates on the tagged commit", GITHUB_REF=ref)
                self.assertEqual(done.returncode, 1, done.stdout)
                self.assertIn("Not a tag ref", done.stderr)
                self.assertEqual(self.calls(), [])
        done = self.run_step("release.yml", "Wait for the gates on the tagged commit", GITHUB_REF="refs/tags/v1.2.0")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.calls()[-1], ["python3", ".github/scripts/gate_wait.py", "--repo", "owner/repo",
                                            "--sha", "cafe" * 10, "--exclude", "release-gates",
                                            "--exclude", "release-build", "--exclude", "release-publish"])


if __name__ == "__main__":
    unittest.main()
