"""Run perf_budget_raise.py over throwaway histories: a raise passes only on a
budget-only commit declaring it, and every other route is refused."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "perf_budget_raise.py"
BUDGET = ".github/perf-budgets.json"
ZERO = "0" * 40


def budget(author: int = 500, mapping: int = 400, tolerance: float = 1.0, **extra: int) -> str:
    return json.dumps({"schema_version": 1, "counter": "callgrind-ir", "tolerance_pct": tolerance,
                       "toolchain": {"python": "p", "valgrind": "v", "libc": "l"},
                       "workloads": {"author-stats": author, "reauthor-mapping": mapping, **extra}}, indent=2) + "\n"


def declare(key: str, was: object, to: object) -> str:
    return f"Budget-Raise: {BUDGET} {key} {was} -> {to}"


class Raise(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        self.repo = self.root / "repo"
        identity = {f"GIT_{role}_{field}": value for role in ("AUTHOR", "COMMITTER")
                    for field, value in (("NAME", "Dev"), ("EMAIL", "dev@example.invalid"))}
        self.env = {**os.environ, **identity, "GIT_CONFIG_GLOBAL": str(self.root / "gitconfig"), "GIT_CONFIG_NOSYSTEM": "1"}
        (self.root / "gitconfig").write_text("", encoding="utf-8")
        self.git("init", "-q", "-b", "main", str(self.repo), cwd=self.root)
        self.base = self.commit({"tool.py": "x = 1\n", BUDGET: budget()})

    def git(self, *args: str, cwd: Path | None = None) -> str:
        return subprocess.run(["git", *args], cwd=cwd or self.repo, env=self.env, check=True,
                              capture_output=True, text=True).stdout.strip()

    def commit(self, files: dict[str, str | None], *lines: str) -> str:
        for name, text in files.items():
            path = self.repo / name
            if text is None:
                self.git("rm", "-q", name)
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            self.git("add", name)
        self.git("commit", "-q", "--allow-empty", "-m", "\n\n".join(["change", *lines]) if lines else "change")
        return self.git("rev-parse", "HEAD")

    def judge(self, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, "-I", str(SCRIPT), *args], cwd=cwd or self.repo, env=self.env,
                              capture_output=True, text=True, check=False)

    def pull(self, head: str, base: str | None = None) -> subprocess.CompletedProcess[str]:
        return self.judge("--event", "pull_request", "--base", base or self.base, "--head", head)

    def assert_clean(self, done: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

    def assert_refused(self, done: subprocess.CompletedProcess[str], *reasons: str) -> None:
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        for reason in reasons:
            self.assertIn(reason, done.stdout)

    def test_a_lowering_or_a_new_workload_needs_no_declaration(self) -> None:
        head = self.commit({"tool.py": "x = 2\n", BUDGET: budget(450, 300, 0.5, extra=9)})
        self.assert_clean(self.pull(head))

    def test_a_budget_absent_at_the_base_is_a_seed(self) -> None:
        self.base = self.commit({BUDGET: None})
        self.assert_clean(self.pull(self.commit({"tool.py": "x = 2\n", BUDGET: budget(900, 900, 50.0)})))

    def test_a_declared_raise_on_a_budget_only_commit_passes(self) -> None:
        self.commit({"tool.py": "x = 2\n"})
        head = self.commit({BUDGET: budget(600, 400, 2.0)}, declare("workloads.author-stats", 500, 600),
                           declare("tolerance_pct", "1.0", "2"))
        self.assert_clean(self.pull(head))

    def test_an_undeclared_raise_is_refused(self) -> None:
        for key, document in (("workloads.author-stats", budget(501)), ("tolerance_pct", budget(tolerance=1.5))):
            with self.subTest(key=key):
                self.git("reset", "-q", "--hard", self.base)
                self.assert_refused(self.pull(self.commit({BUDGET: document})), f"{BUDGET}: {key}", "without a Budget-Raise")

    def test_a_declared_raise_beside_other_files_is_refused(self) -> None:
        head = self.commit({BUDGET: budget(600), "tool.py": "x = 2\n"}, declare("workloads.author-stats", 500, 600))
        self.assert_refused(self.pull(head), "also touches tool.py")

    def test_a_declaration_that_does_not_name_the_raise_exactly_is_refused(self) -> None:
        for line in (declare("workloads.author-stats", 500, 601), declare("workloads.author-stats", 499, 600),
                     declare("workloads.reauthor-mapping", 500, 600), "Budget-Raise: other.json workloads.author-stats 500 -> 600"):
            with self.subTest(line=line):
                self.git("reset", "-q", "--hard", self.base)
                self.assert_refused(self.pull(self.commit({BUDGET: budget(600)}, line)),
                                    "without a Budget-Raise", "declares a raise the commit does not make")

    def test_a_declaration_without_a_raise_or_off_format_is_refused(self) -> None:
        for line, reason in ((declare("workloads.author-stats", 500, 600), "declares a raise the commit does not make"),
                             (f"Budget-Raise: {BUDGET} workloads.author-stats 500 to 600", "does not parse")):
            with self.subTest(line=line):
                self.git("reset", "-q", "--hard", self.base)
                self.assert_refused(self.pull(self.commit({BUDGET: budget(450)}, line)), reason)

    def test_a_raise_through_a_removed_and_readded_key_is_refused(self) -> None:
        document = json.loads(budget())
        del document["workloads"]["author-stats"]
        self.commit({BUDGET: json.dumps(document), "tool.py": "x = 2\n"})
        head = self.commit({BUDGET: budget(900), "tool.py": "x = 3\n"})
        self.assert_refused(self.pull(head), "net raised")

    def test_a_declared_step_licenses_nothing_past_its_own_value(self) -> None:
        document = json.loads(budget())
        del document["workloads"]["author-stats"]
        self.commit({BUDGET: budget(501)}, declare("workloads.author-stats", 500, 501))
        self.commit({BUDGET: json.dumps(document), "tool.py": "x = 2\n"})
        head = self.commit({BUDGET: budget(900000), "tool.py": "x = 3\n"})
        for done in (self.pull(head), self.judge("--event", "push", "--before", self.base, "--head", head)):
            self.assert_refused(done, "workloads.author-stats net raised 500 -> 900000 past every declared step")

    def test_chained_declared_steps_reach_their_last_value(self) -> None:
        self.commit({BUDGET: budget(600)}, declare("workloads.author-stats", 500, 600))
        self.commit({BUDGET: budget(550), "tool.py": "x = 2\n"})
        head = self.commit({BUDGET: budget(700)}, declare("workloads.author-stats", 550, 700))
        self.assert_clean(self.pull(head))

    def test_a_budget_unreadable_at_any_commit_cannot_be_judged(self) -> None:
        self.commit({BUDGET: budget(501)}, declare("workloads.author-stats", 500, 501))
        for broken in ("{", "[]", budget().replace("500", '"500"'), budget().replace("500", "NaN")):
            with self.subTest(broken=broken):
                self.commit({BUDGET: broken, "tool.py": "x = 2\n"})
                head = self.commit({BUDGET: budget(900000), "tool.py": "x = 3\n"})
                done = self.pull(head)
                self.assertEqual(done.returncode, 2, done.stdout)
                self.assertIn("is not a readable budget", done.stderr)
                self.git("reset", "-q", "--hard", "HEAD~2")

    def test_a_crash_exits_2_not_1(self) -> None:
        done = subprocess.run([sys.executable, "-I", str(SCRIPT), "--event", "push", "--before", self.base,
                               "--head", self.base], cwd=self.repo, env={**self.env, "PATH": ""},
                              capture_output=True, text=True, check=False)
        self.assertEqual(done.returncode, 2, done.stdout)
        self.assertIn("could not be judged", done.stderr)

    def test_a_non_finite_declaration_does_not_parse(self) -> None:
        head = self.commit({BUDGET: budget(600)}, f"Budget-Raise: {BUDGET} workloads.author-stats sNaN -> 600")
        self.assert_refused(self.pull(head), "does not parse")

    def test_deleting_the_budget_is_refused(self) -> None:
        self.assert_refused(self.pull(self.commit({BUDGET: None})), "deleted")

    def test_the_window_starts_at_the_merge_base(self) -> None:
        branch = self.commit({"tool.py": "x = 2\n"})
        self.git("reset", "-q", "--hard", self.base)
        tightened = self.commit({BUDGET: budget(450)})
        self.assert_clean(self.pull(branch, tightened))

    def test_a_merge_judges_only_what_it_adds(self) -> None:
        raised = self.commit({BUDGET: budget(600)}, declare("workloads.author-stats", 500, 600))
        self.git("reset", "-q", "--hard", self.base)
        self.commit({"tool.py": "x = 2\n"})
        self.git("merge", "-q", "--no-edit", raised)
        self.assert_clean(self.pull(self.git("rev-parse", "HEAD")))
        self.git("reset", "-q", "--hard", "HEAD^1")
        self.git("merge", "-q", "--no-commit", raised)
        (self.repo / BUDGET).write_text(budget(700), encoding="utf-8")
        self.git("add", BUDGET)
        self.git("commit", "-q", "--no-edit")
        self.assert_refused(self.pull(self.git("rev-parse", "HEAD")), "without a Budget-Raise")

    def test_a_push_judges_before_to_head(self) -> None:
        undeclared = self.commit({BUDGET: budget(600)})
        self.assert_refused(self.judge("--event", "push", "--before", self.base, "--head", undeclared), "without a Budget-Raise")
        self.assert_clean(self.judge("--event", "push", "--before", undeclared, "--head", self.commit({"tool.py": "x = 2\n"})))
        done = self.judge("--event", "push", "--before", ZERO, "--head", undeclared)
        self.assert_clean(done)
        self.assertIn("skipping", done.stdout)

    def test_a_push_from_a_commit_off_the_heads_history_cannot_be_judged(self) -> None:
        side = self.commit({"tool.py": "x = 2\n"})
        self.git("reset", "-q", "--hard", self.base)
        done = self.judge("--event", "push", "--before", side, "--head", self.commit({"tool.py": "x = 3\n"}))
        self.assertEqual(done.returncode, 2, done.stdout)
        self.assertIn("not an ancestor", done.stderr)

    def test_another_event_is_skipped_and_missing_input_is_an_error(self) -> None:
        done = self.judge("--event", "workflow_dispatch", "--head", self.base)
        self.assert_clean(done)
        self.assertIn("skipping", done.stdout)
        for args in (("--event", "pull_request", "--head", self.base), ("--event", "push", "--head", self.base),
                     ("--event", "pull_request", "--base", self.base, "--head", "no-such-revision")):
            with self.subTest(args=args):
                self.assertEqual(self.judge(*args).returncode, 2)

    def test_a_shallow_clone_cannot_be_judged(self) -> None:
        head = self.commit({"tool.py": "x = 2\n"})
        shallow = self.root / "shallow"
        self.git("clone", "-q", "--depth", "1", f"file://{self.repo}", str(shallow), cwd=self.root)
        done = self.judge("--event", "push", "--before", self.base, "--head", head, cwd=shallow)
        self.assertEqual(done.returncode, 2, done.stdout)
        self.assertIn("shallow", done.stderr)


if __name__ == "__main__":
    unittest.main()
