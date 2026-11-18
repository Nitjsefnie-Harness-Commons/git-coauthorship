"""Run ratchet-push.yml's own `run:` text against a stub `gh`, stub SSH
tools and a local bare repository standing in for GitHub."""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("test_publish_release", HERE / "test_publish_release.py")
assert spec is not None and spec.loader is not None
publish = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publish)

WORKFLOW = (HERE.parent / "workflows" / "ratchet-push.yml").read_text(encoding="utf-8")
LIST = "List the triggering run's ratchet artifacts"
PUSH = "Push the raised records to main"
RECORD = ".github/ci-thresholds.json"
MESSAGE = "chore(ratchet): raise the coverage floor (automated)"
STUB = """
import os, sys
with open(os.environ["STUB_LOG"], "a", encoding="utf-8") as log:
    log.write(" ".join([os.path.basename(sys.argv[0])] + sys.argv[1:]) + "\\n")
if os.path.basename(sys.argv[0]) == "gh":
    if os.environ.get("STUB_FAIL"):
        sys.exit("stub: HTTP 502")
    print(os.environ["STUB_NAMES"])
if os.path.basename(sys.argv[0]) == "ssh-agent":
    print("SSH_AGENT_PID=; export SSH_AGENT_PID;")
"""


def document(floor: float, measured: float) -> str:
    return json.dumps({"schema_version": 1, "coverage": {"python": {"floor": floor, "measured": measured}}},
                      indent=2) + "\n"


class RatchetPush(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        (self.root / "bin").mkdir()
        for name in ("gh", "ssh-agent", "ssh-add"):
            stub = self.root / "bin" / name
            stub.write_text(f"#!{sys.executable}\n{STUB}", encoding="utf-8")
            stub.chmod(0o755)
        (self.root / "gitconfig").write_text("", encoding="utf-8")
        self.remote = self.root / "remote.git"
        self.env = dict(os.environ, PATH=f"{self.root / 'bin'}{os.pathsep}{os.environ['PATH']}",
                        STUB_LOG=str(self.root / "calls.log"), GITHUB_STEP_SUMMARY=str(self.root / "summary"),
                        GITHUB_OUTPUT=str(self.root / "output"), GIT_CONFIG_GLOBAL=str(self.root / "gitconfig"),
                        GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_COUNT="1",
                        GIT_CONFIG_KEY_0=f"url.{self.remote}.insteadOf",
                        GIT_CONFIG_VALUE_0="git@github.com:owner/repo.git",
                        GIT_COAUTHORSHIP_REPO="owner/repo", GIT_COAUTHORSHIP_RUN_ID="7",
                        GIT_COAUTHORSHIP_LEGS="coverage", GIT_COAUTHORSHIP_DEPLOY_KEY="key material")
        self.git("init", "-q", "--bare", "-b", "main", str(self.remote))
        self.work = self.root / "clone"
        self.git("clone", "-q", str(self.remote), str(self.work))
        self.base = self.commit({RECORD: document(80.0, 81.5), "tool.py": "x = 1\n"})
        self.stage(document(83.7, 85.2), self.base)

    def git(self, *args: str, cwd: Path | None = None) -> str:
        identity = {f"GIT_{role}_{field}": value for role in ("AUTHOR", "COMMITTER")
                    for field, value in (("NAME", "Dev"), ("EMAIL", "dev@example.invalid"))}
        return subprocess.run(["git", *args], cwd=cwd, env={**self.env, **identity}, check=True,
                              capture_output=True, text=True).stdout.strip()

    def commit(self, files: dict[str, str]) -> str:
        for name, text in files.items():
            (self.work / name).parent.mkdir(parents=True, exist_ok=True)
            (self.work / name).write_text(text, encoding="utf-8")
            self.git("add", name, cwd=self.work)
        self.git("commit", "-q", "-m", "change", cwd=self.work)
        self.git("push", "-q", "origin", "HEAD:main", cwd=self.work)
        return self.git("rev-parse", "HEAD", cwd=self.work)

    def stage(self, text: str, base: str, extra: str = "") -> None:
        leg = self.root / "temp" / "ratchet-data" / "coverage"
        leg.mkdir(parents=True, exist_ok=True)
        (leg / "ci-thresholds.json").write_text(text, encoding="utf-8")
        (leg / "base.txt").write_text(base + "\n", encoding="utf-8")
        if extra:
            (leg / extra).write_text("", encoding="utf-8")

    def run_step(self, name: str, **environ: str) -> subprocess.CompletedProcess[str]:
        for fresh in ("ratchet-work", "ratchet-repo"):
            shutil.rmtree(self.root / "temp" / fresh, ignore_errors=True)
        script = self.root / "step.sh"
        script.write_text(publish.step_run(WORKFLOW, name), encoding="utf-8")
        return subprocess.run(["bash", "-e", str(script)], cwd=self.root,
                              env={**self.env, "RUNNER_TEMP": str(self.root / "temp"), **environ},
                              capture_output=True, text=True, check=False)

    def main_tip(self) -> str:
        return self.git("--git-dir", str(self.remote), "rev-parse", "main")

    def assert_dropped(self, done: subprocess.CompletedProcess[str], tip: str, reason: str) -> None:
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn(f"::notice::Dropped: {reason}", done.stdout)
        self.assertEqual(self.main_tip(), tip)

    def test_the_listing_selects_each_present_leg(self) -> None:
        for names, output in (("wheel\nratchet-coverage\ncoverage-xml", "coverage=true\nlegs=coverage\n"),
                              ("wheel\nratchet-coverage-old\nratchet-", "legs=\n"), ("", "legs=\n")):
            with self.subTest(names=names):
                (self.root / "output").write_text("", encoding="utf-8")
                done = self.run_step(LIST, STUB_NAMES=names)
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertEqual((self.root / "output").read_text(encoding="utf-8"), output)
        self.assertIn("gh api -H Cache-Control: no-cache --paginate repos/owner/repo/actions/runs/7/artifacts",
                      (self.root / "calls.log").read_text(encoding="utf-8"))

    def test_a_failed_listing_fails_and_selects_nothing(self) -> None:
        done = self.run_step(LIST, STUB_NAMES="ratchet-coverage", STUB_FAIL="1")
        self.assertNotEqual(done.returncode, 0)
        self.assertFalse((self.root / "output").exists())

    def test_the_raise_lands_on_main_as_one_bot_commit(self) -> None:
        done = self.run_step(PUSH)
        self.assertEqual(done.returncode, 0, done.stderr)
        tip = self.main_tip()
        show = ["--git-dir", str(self.remote), "log", "-1", "--format=%P%n%an <%ae>%n%s", tip]
        self.assertEqual(self.git(*show).splitlines(), [
            self.base, "github-actions[bot] <41898282+github-actions[bot]@users.noreply.github.com>", MESSAGE])
        self.assertEqual(self.git("--git-dir", str(self.remote), "diff", "--name-only", self.base, tip), RECORD)
        self.assertEqual(self.git("--git-dir", str(self.remote), "show", f"{tip}:{RECORD}") + "\n",
                         document(83.7, 85.2))
        self.assertIn("ssh-add", (self.root / "calls.log").read_text(encoding="utf-8"))

    def test_main_moving_under_the_measured_tree_drops_the_raise(self) -> None:
        tip = self.commit({"tool.py": "x = 2\n"})
        self.assert_dropped(self.run_step(PUSH), tip, "main moved under the measured tree")

    def test_a_records_only_move_merges_or_waits(self) -> None:
        tip = self.commit({RECORD: document(83.7, 85.2)})
        done = self.run_step(PUSH)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("nothing to commit", done.stdout)
        self.assertEqual(self.main_tip(), tip)
        tip = self.commit({RECORD: document(84.0, 85.2)})
        self.assert_dropped(self.run_step(PUSH), tip, f"{RECORD} changed on main since the measure")

    def test_a_broken_handoff_fails_before_any_fetch(self) -> None:
        for text, base, extra in ((document(83.7, 85.2), "-x", ""), (document(83.7, 85.2), "ab" * 19, ""),
                                  (document(83.7, 85.2), "AB" * 20, ""), (document(83.7, 85.2), "", "x.json")):
            with self.subTest(base=base, extra=extra):
                shutil.rmtree(self.root / "temp")
                self.stage(text, base or self.base, extra)
                done = self.run_step(PUSH)
                self.assertEqual(done.returncode, 1, done.stdout)
                self.assertIn("artifact ratchet-coverage", done.stderr)
                self.assertEqual(self.main_tip(), self.base)
        (self.root / "temp" / "ratchet-data" / "coverage" / "x.json").unlink()
        (self.root / "temp" / "ratchet-data" / "coverage" / "ci-thresholds.json").unlink()
        self.assertEqual(self.run_step(PUSH).returncode, 1)

    def test_an_empty_deploy_key_fails_closed(self) -> None:
        done = self.run_step(PUSH, GIT_COAUTHORSHIP_DEPLOY_KEY="")
        self.assertEqual(done.returncode, 1, done.stdout)
        self.assertIn("missing its secret", done.stderr)
        self.assertFalse((self.root / "calls.log").exists())

    def test_staged_data_that_is_not_json_is_dropped(self) -> None:
        self.stage("{", self.base)
        self.assert_dropped(self.run_step(PUSH), self.base, f"the merged {RECORD} is not JSON")

    def test_a_rejected_push_fails_unless_main_moved(self) -> None:
        hook = self.remote / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        hook.chmod(0o755)
        done = self.run_step(PUSH)
        self.assertEqual(done.returncode, 1, done.stdout)
        self.assertIn("rejected while main stood still", done.stderr)
        moved = self.git("--git-dir", str(self.remote), "commit-tree", "-p", self.base, "-m", "moved",
                         f"{self.base}^{{tree}}")
        # Git refuses ref updates from a quarantined pre-receive; main moves as if by another pusher.
        hook.write_text("#!/bin/sh\nunset GIT_QUARANTINE_PATH GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES\n"
                        f"git update-ref refs/heads/main {moved}\nexit 1\n", encoding="utf-8")
        self.assert_dropped(self.run_step(PUSH), moved, "main moved before the push landed")


if __name__ == "__main__":
    unittest.main()
