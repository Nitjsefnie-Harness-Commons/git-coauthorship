"""Drive gate_base_freshness.py against throwaway repositories with a local
bare origin, never the network and never this repository's history."""
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
SCRIPT = HERE / "gate_base_freshness.py"
ROOT = HERE.parents[1]

spec = importlib.util.spec_from_file_location("gate_base_freshness", SCRIPT)
assert spec is not None and spec.loader is not None
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)

LINT = "./actionlint -color .github/workflows/*.yml"


def workflow(name: str, jobs: dict[str, str]) -> str:
    body = "".join(f"  {job}:\n    runs-on: ubuntu-latest\n    steps:\n      - run: {run}\n"
                   for job, run in jobs.items())
    return f"name: {name}\non: push\njobs:\n{body}"


def actionlint_yml(run: str = LINT) -> str:
    return workflow("actionlint", {"actionlint": run})


# Every required job but actionlint, so a fixture defines the whole set.
TESTS_YML = workflow("tests", {job: "python run_tests.py"
                               for job in gate.REQUIRED_JOBS if job != "actionlint"})


def git(root: Path, *arguments: str) -> str:
    return subprocess.run(["git", "-C", str(root), *arguments], check=True,
                          capture_output=True, text=True).stdout


def configure(repo: Path) -> None:
    git(repo, "config", "user.name", "fixture")
    git(repo, "config", "user.email", "fixture@example.com")
    git(repo, "config", "commit.gpgsign", "false")


def commit(repo: Path, subject: str, files: dict[str, str]) -> None:
    for relative, text in files.items():
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    git(repo, "add", "--", *files)
    git(repo, "commit", "-m", subject)


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def repo(self, files: dict[str, str], name: str = "repo") -> Path:
        repo = self.tmp / name
        git(self.tmp, "init", "-b", "main", str(repo))
        configure(repo)
        commit(repo, "base: seed the fixture", files)
        return repo

    def published(self) -> tuple[Path, Path]:
        """A repo whose origin is a bare clone holding the same main."""
        repo = self.repo({".github/workflows/actionlint.yml": actionlint_yml(),
                          ".github/workflows/tests.yml": TESTS_YML,
                          "run_tests.py": 'print("suite runner")\n',
                          "README.md": "# fixture\n"})
        origin = self.tmp / "origin.git"
        git(self.tmp, "clone", "--bare", str(repo), str(origin))
        git(repo, "remote", "add", "origin", str(origin))
        git(repo, "push", "-u", "origin", "main")
        return repo, origin

    def clone(self, origin: Path, name: str) -> Path:
        other = self.tmp / name
        git(self.tmp, "clone", str(origin), str(other))
        configure(other)
        return other

    def advance(self, origin: Path, subject: str, files: dict[str, str]) -> None:
        other = self.clone(origin, "other")
        commit(other, subject, files)
        git(other, "push", "origin", "main")

    def merged_main(self, origin: Path) -> Path:
        """A clone taken before origin's main gains a real merge commit."""
        behind = self.clone(origin, "behind")
        side = self.clone(origin, "side")
        commit(side, "ci: the side parent", {".github/workflows/actionlint.yml":
                                             actionlint_yml(LINT + " -verbose")})
        adv = self.clone(origin, "adv")
        commit(adv, "ci: the second parent", {".github/workflows/tests.yml":
                                              TESTS_YML + "# moved\n"})
        git(side, "push", "origin", "main")
        git(adv, "fetch", "origin")
        git(adv, "merge", "--no-ff", "--no-edit", "origin/main")
        git(adv, "push", "origin", "main")
        return behind

    def run_gate(self, *arguments: str, event: str | None = "pull_request",
                 script: Path = SCRIPT, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        env = {k: v for k, v in os.environ.items() if k != "GITHUB_EVENT_NAME"}
        env.update({} if event is None else {"GITHUB_EVENT_NAME": event})
        return subprocess.run([sys.executable, str(script), *arguments], cwd=cwd,
                              check=False, capture_output=True, text=True, env=env)

    def in_process(self, call, *arguments) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = call(*arguments)
        return code, out.getvalue()

    def assert_refuses(self, call, *arguments, fragment: str, **keywords) -> None:
        with self.assertRaises(gate.GateError) as caught:
            call(*arguments, **keywords)
        self.assertIn(fragment, str(caught.exception))


class ThisRepository(Fixture):
    def test_required_jobs_are_the_jobs_every_pull_request_runs(self) -> None:
        self.assertEqual(gate.REQUIRED_JOBS, ("actionlint", "suites", "wheel", "coverage",
                                              "pycodestyle", "pylint", "pyright",
                                              "pip-audit", "analyze", "comment-share",
                                              "size-baseline"))
        files = gate.tracked_files(ROOT)
        for job in gate.REQUIRED_JOBS:
            defining = [name for name in gate.workflow_names(files)
                        if job in gate.workflow_steps(git(ROOT, "show", f"HEAD:{name}"), name)]
            self.assertEqual(len(defining), 1, job)
            lines = git(ROOT, "show", f"HEAD:{defining[0]}").splitlines()
            trigger = [i for i, line in enumerate(lines) if line == "  pull_request:"]
            self.assertEqual(len(trigger), 1, (job, defining))
            after = next(line for line in lines[trigger[0] + 1:] if not gate.skippable(line))
            self.assertLessEqual(gate.indent_of(after), 2, (job, "pull_request is filtered"))

    def test_gate_paths_on_this_repository(self) -> None:
        # A required job naming a new file moves this pin in the same commit.
        done = self.run_gate("--print-paths", cwd=ROOT)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.splitlines(), [
            ".github/comment-share.json", ".github/scripts/commit_scopes.py",
            ".github/scripts/conflict_markers.sh",
            ".github/scripts/gate_base_freshness.py", ".github/scripts/gate_wait.py",
            ".github/scripts/test_commit_scopes.py", ".github/scripts/test_conflict_markers.py",
            ".github/scripts/test_gate_base_freshness.py", ".github/scripts/test_gate_list.py",
            ".github/scripts/test_gate_wait.py",
            ".github/scripts/test_publish_release.py", ".github/size-baseline.json",
            ".github/workflows/actionlint.yml", ".github/workflows/audit.yml",
            ".github/workflows/claim.yml", ".github/workflows/codeql.yml",
            ".github/workflows/comment-share.yml", ".github/workflows/lint.yml",
            ".github/workflows/pin-guard.yml", ".github/workflows/pr-gate.yml",
            ".github/workflows/release.yml", ".github/workflows/scorecard.yml",
            ".github/workflows/secrets.yml",
            ".github/workflows/size-baseline.yml", ".github/workflows/tag.yml",
            ".github/workflows/tests.yml",
            ".github/workflows/types.yml", ".pylintrc", "git_coauthorship/__init__.py",
            "git_coauthorship/_scope.py", "git_coauthorship/author_stats.py",
            "git_coauthorship/reauthor.py",
            "requirements-audit.txt", "requirements-build.txt", "requirements-dev.txt",
            "requirements-test.txt", "requirements-zizmor.txt", "run_tests.py",
            "tests/_util.py",
            "tests/test_author_stats.py", "tests/test_package_contract.py",
            "tests/test_reauthor.py"])

    def test_every_tracked_workflow_parses(self) -> None:
        for name in gate.workflow_names(gate.tracked_files(ROOT)):
            self.assertTrue(gate.workflow_steps(git(ROOT, "show", f"HEAD:{name}"), name), name)


class Comparison(Fixture):
    def test_green_when_the_head_carries_every_gate_commit(self) -> None:
        repo, _ = self.published()
        done = self.run_gate("--root", str(repo))
        self.assertEqual(done.returncode, 0, (done.stdout, done.stderr))
        self.assertIn("carries every commit on main", done.stdout)
        self.assertIn("named reach limit", done.stdout)

    def test_red_when_main_advances_a_gate_file(self) -> None:
        repo, origin = self.published()
        self.advance(origin, "ci: move the gate the head already read",
                     {".github/workflows/actionlint.yml": actionlint_yml(LINT + " -verbose")})
        done = self.run_gate("--root", str(repo))
        self.assertEqual(done.returncode, 1, (done.stdout, done.stderr))
        self.assertIn("main holds 1 commit this head does not", done.stdout)
        self.assertIn("ci: move the gate the head already read", done.stdout)
        self.assertIn("    .github/workflows/actionlint.yml", done.stdout)
        self.assertIn("Rebase onto main", done.stdout)

    def test_green_when_main_advances_a_non_gate_file(self) -> None:
        repo, origin = self.published()
        self.advance(origin, "fix: a file no required check reads by name",
                     {"setup.cfg": "[pycodestyle]\n"})
        done = self.run_gate("--root", str(repo))
        self.assertEqual(done.returncode, 0, (done.stdout, done.stderr))

    def test_a_push_passes_only_a_head_main_already_holds(self) -> None:
        repo, origin = self.published()
        self.advance(origin, "ci: main moves past the pushed head",
                     {".github/workflows/actionlint.yml": actionlint_yml(LINT + " -verbose")})
        done = self.run_gate("--root", str(repo), event="push")
        self.assertEqual(done.returncode, 0, (done.stdout, done.stderr))
        self.assertIn("This pushed head is already on main", done.stdout)
        for event in ("workflow_dispatch", None):
            done = self.run_gate("--root", str(repo), event=event)
            self.assertEqual(done.returncode, 1, (event, done.stdout))
        commit(repo, "work main does not hold", {"BRANCH.md": "branch work\n"})
        done = self.run_gate("--root", str(repo), event="push")
        self.assertEqual(done.returncode, 1, done.stdout)

    def test_default_root_from_the_scripts_own_depth(self) -> None:
        # Copied to .github/scripts/ in the fixture: a wrong default root
        # empties the pathspec listing and greens a stale head.
        repo, origin = self.published()
        self.advance(origin, "ci: main moves a gate file the head has not read",
                     {".github/workflows/actionlint.yml": actionlint_yml(LINT + " -verbose")})
        script = repo / ".github/scripts/gate_base_freshness.py"
        script.parent.mkdir(parents=True)
        shutil.copyfile(SCRIPT, script)
        done = self.run_gate(script=script, cwd=repo)
        self.assertEqual(done.returncode, 1, (done.stdout, done.stderr))
        self.assertIn("ci: main moves a gate file the head has not read", done.stdout)

    def test_red_names_a_merge_commit(self) -> None:
        _, origin = self.published()
        behind = self.merged_main(origin)
        code, out = self.in_process(gate.check, behind, "pull_request")
        self.assertEqual(code, 1, out)
        self.assertIn("(a merge commit, which git names no file for", out)
        self.assertIn("ci: the side parent", out)
        self.assertIn("ci: the second parent", out)

    def test_a_shallow_clone_is_unshallowed_before_comparing(self) -> None:
        _, origin = self.published()
        self.advance(origin, "ci: main moved", {".github/workflows/tests.yml": TESTS_YML + "#\n"})
        behind = self.tmp / "shallow"
        # file:// so --depth is honoured.
        git(self.tmp, "clone", "--depth", "1", origin.as_uri(), str(behind))
        self.assertEqual(git(behind, "rev-parse", "--is-shallow-repository").strip(), "true")
        gate.fetch_base(behind)
        self.assertEqual(git(behind, "rev-parse", "--is-shallow-repository").strip(), "false")

    def test_exit_codes_and_argv(self) -> None:
        repo, _ = self.published()
        for arguments in (("--root", str(repo), "--nonsense"), ("--root",)):
            done = self.run_gate(*arguments)
            self.assertEqual(done.returncode, 2, arguments)
            self.assertIn("usage", done.stderr)
        code, out = self.in_process(gate.main, ["gate_base_freshness.py", "--root", str(repo)])
        self.assertEqual(code, 0, out)

    def test_main_refuses_a_repository_with_no_commit_and_a_missing_git(self) -> None:
        empty = self.tmp / "empty"
        git(self.tmp, "init", "-b", "main", str(empty))
        self.assertEqual(self.in_process(gate.main, ["g", "--root", str(empty)])[0], 1)
        saved = os.environ["PATH"]
        os.environ["PATH"] = ""
        try:
            self.assertEqual(self.in_process(gate.main, ["g", "--root", str(ROOT)])[0], 1)
        finally:
            os.environ["PATH"] = saved


class Derivation(Fixture):
    def test_refuses_when_a_required_job_is_missing(self) -> None:
        repo = self.repo({".github/workflows/actionlint.yml": actionlint_yml("cat README.md"),
                          "README.md": "# fixture\n"})
        self.assert_refuses(gate.gate_paths, repo, fragment="defines the required job `suites`")

    def test_expressions_are_stripped_before_paths_are_taken(self) -> None:
        repo = self.repo({".github/workflows/actionlint.yml":
                          actionlint_yml("python -m pytest ${{ matrix.python }}"),
                          ".github/workflows/tests.yml": TESTS_YML,
                          "run_tests.py": "\n", "matrix.python": "\n"})
        self.assertEqual(gate.gate_paths(repo), ["run_tests.py"])

    def test_whole_tree_spelling_adds_no_paths(self) -> None:
        marker = "git grep -nI -E \"^(<{7}( |$)|>{7}( |$)|={7}$)\" -- ."
        repo = self.repo({".github/workflows/actionlint.yml": actionlint_yml(marker),
                          ".github/workflows/tests.yml": workflow(
                              "tests", {job: marker for job in gate.REQUIRED_JOBS[1:]})})
        self.assert_refuses(gate.gate_paths, repo, fragment="name no file in this tree")

    def test_a_local_action_takes_its_directory_whole(self) -> None:
        repo = self.repo({"action.yml": "name: a\n", "README.md": "#\n",
                          ".github/workflows/actionlint.yml": actionlint_yml().replace(
                              f"- run: {LINT}", "- uses: ./"),
                          ".github/workflows/tests.yml": TESTS_YML})
        self.assertIn("README.md", gate.gate_paths(repo))

    def test_a_working_directory_resolves_the_steps_paths_under_it(self) -> None:
        moved = TESTS_YML.replace("      - run: python run_tests.py\n",
                                  "      - run: python run_tests.py ../setup.cfg\n"
                                  "        working-directory: sub\n", 1)
        repo = self.repo({".github/workflows/actionlint.yml": actionlint_yml("cat README.md"),
                          ".github/workflows/tests.yml": moved, "setup.cfg": "\n",
                          "sub/run_tests.py": "\n", "README.md": "#\n"})
        self.assertEqual(gate.gate_paths(repo), ["README.md", "setup.cfg", "sub/run_tests.py"])

    def test_a_steps_with_values_name_paths_and_its_keys_do_not(self) -> None:
        tests = TESTS_YML.replace("      - run: python run_tests.py\n",
                                  "      - uses: owner/action@ref\n        with:\n"
                                  "          # a comment\n"
                                  "          records: '.github/r.json'\n          notes: |\n"
                                  "            see docs/n.md\n", 1)
        repo = self.repo({".github/workflows/actionlint.yml": actionlint_yml("echo"),
                          ".github/workflows/tests.yml": tests, ".github/r.json": "{}\n",
                          "docs/n.md": "\n", "records": "\n", "run_tests.py": "\n"})
        self.assertEqual(gate.gate_paths(repo), [".github/r.json", "docs/n.md", "run_tests.py"])

    def test_refuses_a_run_directory_it_cannot_resolve(self) -> None:
        head = "name: w\njobs:\n  actionlint:\n"
        for why, text in {
                "expression": head + "    steps:\n      - run: echo\n"
                                     "        working-directory: ${{ inputs.dir }}\n",
                "job defaults": head + "    defaults:\n      run:\n"
                                       "        working-directory: sub\n",
                "flow with": head + "    steps:\n      - uses: a@b\n"
                                    "        with: {records: r.json}\n",
                "workflow defaults": "defaults:\n  run:\n    working-directory: sub\n"
                                     + head + "    steps:\n      - run: echo\n"}.items():
            with self.subTest(why), self.assertRaises(gate.WorkflowError):
                gate.workflow_steps(text, "w.yml")

    def test_gate_paths_refuses_a_tree_with_no_files(self) -> None:
        repo = self.tmp / "repo"
        git(self.tmp, "init", "-b", "main", str(repo))
        configure(repo)
        git(repo, "commit", "--allow-empty", "-m", "base: an empty tree")
        self.assert_refuses(gate.gate_paths, repo, fragment="tracks no files")

    def test_git_refusal_names_what_was_attempted(self) -> None:
        self.assert_refuses(gate.git, ROOT, "rev-parse", "--verify", "no-such-ref^{commit}",
                            what="x", fragment="cannot x: `git -C")

    def test_stale_commits_refuses_empty_and_oversized_paths(self) -> None:
        self.assert_refuses(gate.stale_commits, ROOT, "HEAD", "HEAD", [], fragment="empty")
        oversized = ["a" * (gate.PATHSPECS_MAX_BYTES + 1)]
        self.assert_refuses(gate.stale_commits, ROOT, "HEAD", "HEAD", oversized,
                            fragment="bytes")

    def test_resolve_and_candidates(self) -> None:
        files = ("action.yml", ".github/workflows/tests.yml", ".github/workflows/a.yml")
        self.assertEqual(gate.resolve("action.yml", files), ("action.yml",))
        self.assertEqual(gate.resolve("./action.yml", files), ("action.yml",))
        self.assertEqual(gate.resolve("action.yml/", files), ("action.yml",))
        self.assertEqual(gate.resolve(".github/workflows", files), files[1:])
        self.assertEqual(gate.resolve("absent.yml", files), ())
        self.assertEqual(gate.resolve(".", files), ())
        self.assertEqual(gate.candidates("cat ${{ matrix.python }}/x.yml"), ["cat", "/x.yml"])


class Parser(Fixture):
    def steps(self, text: str) -> list[dict[str, str | None]]:
        return gate.workflow_steps(text, "w.yml")["actionlint"]

    def test_block_scalar_run_text_is_read_verbatim(self) -> None:
        text = ("name: w\njobs:\n  actionlint:\n    # a comment\n\n    steps:\n"
                "      - name: a step\n        run: |\n          echo one\n"
                "          # a comment bash receives\n          grep README.md\n"
                "        if: ${{ !cancelled() }}\npermissions:\n  contents: read\n")
        self.assertEqual(self.steps(text), [{"run": "echo one\n# a comment bash receives\n"
                                             "grep README.md", "uses": None, "dir": None,
                                             "with": None}])

    def test_the_shapes_this_repositorys_workflows_use(self) -> None:
        text = ("name: w\njobs:\n  actionlint:\n    name: lint (${{ matrix.os }})\n"
                "    strategy:\n      matrix:\n        os: [a, b]\n"
                "    runs-on: ${{ matrix.os }}\n    env:\n      A: b\n    permissions:\n"
                "      contents: read\n    steps:\n"
                "      - uses: owner/action@ref # v1\n"
                "        with:\n          fetch-depth: 0\n"
                "          cache-dependency-path: requirements-*.txt\n"
                "      - name: s\n        env:\n          X: ${{ github.sha }}\n"
                "        working-directory: dist\n        run: python run_tests.py\n")
        self.assertEqual(self.steps(text), [
            {"run": None, "uses": "owner/action@ref", "dir": None,
             "with": "0\nrequirements-*.txt"},
            {"run": "python run_tests.py", "uses": None, "dir": "dist", "with": None}])

    def test_refuses_shapes_it_cannot_read(self) -> None:
        head = "name: w\njobs:\n  actionlint:\n"
        cases = {
            "flow jobs": "name: w\njobs: {actionlint: {runs-on: x}}\n",
            "two jobs keys": head + "    steps: []\njobs:\n",
            "duplicate job": head + "    runs-on: x\n  actionlint:\n    runs-on: x\n",
            "flow steps": head + "    steps: [{run: cat README.md}]\n",
            "job at the wrong indent": "name: w\njobs:\n   actionlint:\n",
            "job with an inline value": "name: w\njobs:\n  actionlint: {runs-on: x}\n",
            "job key at the wrong indent": head + "   runs-on: x\n",
            "job key that is no key": head + "    [1, 2]\n",
            "step without a dash": head + "    steps:\n        run: echo\n",
            "dash with no key": head + "    steps:\n      - 123\n",
            "step key at the wrong indent": head + "    steps:\n      - run: echo\n         x: 1\n",
            "step key that is no key": head + "    steps:\n      - run: echo\n        [1, 2]\n",
        }
        for why, text in cases.items():
            with self.subTest(why), self.assertRaises(gate.WorkflowError):
                gate.workflow_steps(text, "w.yml")


if __name__ == "__main__":
    unittest.main()
