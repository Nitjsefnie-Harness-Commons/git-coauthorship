"""Drive commit_scopes.py against throwaway repositories with a local bare
origin, never the network and never this repository's history."""
import contextlib
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "commit_scopes.py"
ROOT = HERE.parents[1]

spec = importlib.util.spec_from_file_location("commit_scopes", SCRIPT)
assert spec is not None and spec.loader is not None
scopes = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scopes)

ACTIONLINT = "name: actionlint\non: push\njobs:\n  actionlint:\n    steps:\n      - run: echo\n"


def git(root: Path, *arguments: str) -> str:
    return subprocess.run(["git", "-C", str(root), *arguments], check=True,
                          capture_output=True, text=True).stdout


def commit(repo: Path, subject: str, files: dict[str, str]) -> str:
    for relative, text in files.items():
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    git(repo, "add", "--", *files)
    git(repo, "commit", "-m", subject)
    return git(repo, "rev-parse", "HEAD").strip()


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def published(self, head_subject: str, files: dict[str, str] | None = None,
                  name: str = "repo") -> Path:
        """origin's main is the seed; the head adds one commit, the subject under test."""
        repo = self.tmp / name
        git(self.tmp, "init", "-b", "main", str(repo))
        for key, value in (("user.name", "fixture"), ("user.email", "fixture@example.com"),
                           ("commit.gpgsign", "false")):
            git(repo, "config", key, value)
        commit(repo, "base: seed the fixture", {".github/workflows/actionlint.yml": ACTIONLINT,
                                                "README.md": "# fixture\n", **(files or {})})
        origin = self.tmp / f"{name}-origin.git"
        git(self.tmp, "clone", "--bare", str(repo), str(origin))
        git(repo, "remote", "add", "origin", str(origin))
        git(repo, "push", "-u", "origin", "main")
        commit(repo, head_subject, {"README.md": "# changed\n"})
        return repo

    def run_gate(self, repo: Path, *extra: str,
                 env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        clean = {k: v for k, v in os.environ.items()
                 if k not in ("GITHUB_EVENT_NAME", "GIT_COAUTHORSHIP_PUSH_BEFORE")}
        return subprocess.run([sys.executable, str(SCRIPT), "--root", str(repo), *extra],
                              check=False, capture_output=True, text=True,
                              env={**clean, **(env or {})})

    def assert_refuses(self, workflow_text: str, fragment: str) -> None:
        with self.assertRaises(scopes.GateError) as caught:
            scopes.workflow_name("w.yml", workflow_text)
        self.assertIn(fragment, str(caught.exception))


class ThisRepository(Fixture):
    def test_names_derived_from_this_repository(self) -> None:
        self.assertEqual(scopes.workflow_name_set(ROOT), {
            "actionlint", "audit", "claim", "codeql", "comment share", "lint",
            "pin test screen", "pr gate", "release", "size baseline", "tag", "tests", "types"})

    def test_the_workflow_hands_the_push_tip_under_the_name_the_check_reads(self) -> None:
        lines = git(ROOT, "show", "HEAD:.github/workflows/actionlint.yml").splitlines()
        run = [index for index, line in enumerate(lines)
               if line.strip() == "run: python3 .github/scripts/commit_scopes.py"]
        self.assertEqual(len(run), 1)
        step = lines[max(i for i in range(run[0]) if lines[i].startswith("      - ")):run[0]]
        env = dict(line.strip().split(": ", 1) for line in step[step.index("        env:") + 1:]
                   if line.startswith(" " * 10) and not line.strip().startswith("#"))
        self.assertEqual(env, {scopes.PUSH_BEFORE: "${{ github.event.before }}"})


class Subjects(Fixture):
    def test_workflow_scope_with_a_non_ci_type_is_red(self) -> None:
        done = self.run_gate(self.published("fix(actionlint): rewritten flag"))
        self.assertEqual(done.returncode, 1, (done.stdout, done.stderr))
        self.assertIn("1 commit in origin/main..HEAD pairs a workflow-name scope", done.stdout)
        self.assertIn("  scope `actionlint` is the name of a workflow under "
                      ".github/workflows/; type `fix` is not `ci`.", done.stdout)

    def test_ci_type_on_a_workflow_scope_passes(self) -> None:
        done = self.run_gate(self.published("ci(actionlint): the workflow moves"))
        self.assertEqual(done.returncode, 0, (done.stdout, done.stderr))
        self.assertIn("Examined 1 commit subject in origin/main..HEAD; 0 not examined.",
                      done.stdout)

    def test_a_spaced_workflow_name_is_compared_exactly(self) -> None:
        files = {".github/workflows/pr-gate.yml": ACTIONLINT.replace("actionlint", "pr gate")}
        for index, (subject, code, printed) in enumerate((
                ("fix(pr gate): x", 1, "scope `pr gate` is the name of a workflow"),
                ("ci(pr gate): x", 0, "Examined 1 commit subject in"),
                ("fix(pr gate ): x", 0, "; 1 not examined."),
                ("fix(pr-gate): x", 0, "Examined 1 commit subject in"))):
            done = self.run_gate(self.published(subject, files, name=f"r{index}"))
            self.assertEqual(done.returncode, code, (subject, done.stdout, done.stderr))
            self.assertIn(printed, done.stdout, subject)

    def test_a_non_workflow_scope_passes_with_any_type(self) -> None:
        done = self.run_gate(self.published("fix(ci): not a workflow name"))
        self.assertEqual(done.returncode, 0, (done.stdout, done.stderr))

    def test_a_subject_that_does_not_parse_is_listed_not_failed(self) -> None:
        repo = self.published("Fix round 1: honest comments")
        done = self.run_gate(repo)
        self.assertEqual(done.returncode, 0, (done.stdout, done.stderr))
        self.assertIn("Examined 0 commit subjects in origin/main..HEAD; 1 not examined.",
                      done.stdout)
        self.assertIn(f"    {git(repo, 'rev-parse', 'HEAD').strip()} Fix round 1", done.stdout)

    def test_only_the_outgoing_range_is_judged(self) -> None:
        repo = self.published("fix(actionlint): rewritten flag")
        git(repo, "push", "origin", "main")
        commit(repo, "fix(readme): a clean head commit", {"README.md": "# again\n"})
        done = self.run_gate(repo)
        self.assertEqual(done.returncode, 0, (done.stdout, done.stderr))
        self.assertIn("Examined 1 commit subject in origin/main..HEAD", done.stdout)

    def test_subject_grammar(self) -> None:
        for subject, kind, scope in (
                ("ci(tests)!: a breaking scope marker", "ci", "tests"),
                ("ci!(tests): a breaking type marker", "ci", "tests"),
                ("fix(pr\tgate): x", "fix", "pr\tgate"),
                ("ci(a): x", "ci", "a")):
            match = scopes.SUBJECT.match(subject)
            self.assertIsNotNone(match, subject)
            assert match is not None
            self.assertEqual((match["type"], match["scope"]), (kind, scope), subject)
        for subject in ("fix( pr gate): x", "fix(pr gate ): x", "fix(): x", "Fix(tests): x",
                        "fix: no scope"):
            self.assertIsNone(scopes.SUBJECT.match(subject), subject)


class PushRange(Fixture):
    """On a push origin/main..HEAD is empty, so the pushed range is judged."""

    def pushed(self, subject: str) -> tuple[Path, str]:
        repo = self.published(subject)
        before = git(repo, "rev-parse", "origin/main").strip()
        git(repo, "push", "origin", "main")
        return repo, before

    def push(self, repo: Path, before: str | None) -> subprocess.CompletedProcess[str]:
        env = {"GITHUB_EVENT_NAME": "push"}
        env.update({} if before is None else {"GIT_COAUTHORSHIP_PUSH_BEFORE": before})
        return self.run_gate(repo, env=env)

    def test_a_push_judges_the_commits_it_adds(self) -> None:
        repo, before = self.pushed("fix(actionlint): rewritten flag")
        done = self.push(repo, before)
        self.assertEqual(done.returncode, 1, (done.stdout, done.stderr))
        self.assertIn(f"1 commit in {before}..HEAD pairs a workflow-name scope", done.stdout)
        done = self.run_gate(repo, env={"GITHUB_EVENT_NAME": "pull_request",
                                        "GIT_COAUTHORSHIP_PUSH_BEFORE": before})
        self.assertEqual(done.returncode, 0, done.stdout)
        self.assertIn("Examined 0 commit subjects in origin/main..HEAD", done.stdout)

    def test_a_clean_push_passes_and_names_its_range(self) -> None:
        repo, before = self.pushed("ci(actionlint): the workflow moves")
        done = self.push(repo, before)
        self.assertEqual(done.returncode, 0, (done.stdout, done.stderr))
        self.assertIn(f"Examined 1 commit subject in {before}..HEAD; 0 not examined.",
                      done.stdout)

    def test_a_push_with_no_previous_tip_refuses(self) -> None:
        repo, _ = self.pushed("ci: clean")
        for before in (None, "", "0" * 40):
            done = self.push(repo, before)
            self.assertEqual(done.returncode, 1, (before, done.stdout))
            self.assertIn("names no previous tip", done.stderr, before)

    def test_a_before_that_is_not_a_commit_id_refuses(self) -> None:
        repo, _ = self.pushed("ci: clean")
        for before in ("HEAD~1", "--output=x", "abc123"):
            done = self.push(repo, before)
            self.assertEqual(done.returncode, 1, (before, done.stdout))
            self.assertIn("is not a commit id", done.stderr, before)

    def test_a_before_the_head_does_not_descend_from_refuses(self) -> None:
        repo, before = self.pushed("ci: clean")
        git(repo, "checkout", "-q", "-b", "side", before)
        rewritten = commit(repo, "ci: a tip a force push replaced", {"SIDE.md": "x\n"})
        git(repo, "checkout", "-q", "main")
        done = self.push(repo, rewritten)
        self.assertEqual(done.returncode, 1, done.stdout)
        self.assertIn("is not an ancestor of HEAD", done.stderr)
        done = self.push(repo, "1" * 40)
        self.assertEqual(done.returncode, 1, done.stdout)
        self.assertIn("cannot establish whether", done.stderr)


class Names(Fixture):
    def test_name_refusals(self) -> None:
        tail = "jobs:\n  suites:\n    steps: []\n"
        for text, fragment in (
                ('name: "tests"\n' + tail, "not a plain scalar"),
                ("name: >\n  folded\n" + tail, "not a plain scalar"),
                ("on: push\n" + tail, "no top-level `name:`"),
                ("name: tests # prose\n" + tail, "trailing comment"),
                ("name: tests\n  continued: yes\n" + tail, "continues onto an indented line"),
                ("name: a\nname: b\n" + tail, "cannot be established"),
                ("name: \n" + tail, "carries no value")):
            with self.subTest(fragment):
                self.assert_refuses(text, fragment)

    def test_a_job_level_name_is_not_collected(self) -> None:
        text = "name: pr gate\non: push\njobs:\n  suites:\n    name: scorecard\n    steps: []\n"
        self.assertEqual(scopes.workflow_name("w.yml", text), "pr gate")

    def test_a_tree_with_no_workflow_refuses(self) -> None:
        repo = self.published("ci: x")
        git(repo, "rm", "-q", "-r", ".github")
        git(repo, "commit", "-q", "-m", "ci: drop the workflows")
        done = self.run_gate(repo)
        self.assertEqual(done.returncode, 1, done.stdout)
        self.assertIn("workflow-name set is empty", done.stderr)


class Plumbing(Fixture):
    def test_fetch_base_deepens_a_shallow_clone(self) -> None:
        self.published("ci: clean")
        shallow = self.tmp / "shallow"
        git(self.tmp, "clone", "--depth", "1", (self.tmp / "repo-origin.git").as_uri(),
            str(shallow))
        scopes.fetch_base(shallow)
        self.assertEqual(git(shallow, "rev-parse", "--is-shallow-repository").strip(), "false")

    def test_exit_codes_and_handlers(self) -> None:
        repo = self.published("ci: clean")
        for extra in (("--nonsense",), ("--root",)):
            done = self.run_gate(repo, *extra)
            self.assertEqual(done.returncode, 2, extra)
            self.assertIn("usage", done.stderr)
        empty = self.tmp / "empty"
        git(self.tmp, "init", "-b", "main", str(empty))
        self.assertEqual(self.run_gate(empty).returncode, 1)
        saved = os.environ["PATH"]
        os.environ["PATH"] = ""
        try:
            with contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(scopes.main(["commit_scopes.py", "--root", str(ROOT)]), 1)
            self.assertIn("cannot run git", err.getvalue())
        finally:
            os.environ["PATH"] = saved


if __name__ == "__main__":
    unittest.main()
