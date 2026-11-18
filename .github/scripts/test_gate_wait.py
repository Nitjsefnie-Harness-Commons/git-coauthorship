"""Drive gate_wait.py against a fake GitHub API and a fake clock: no network,
no real waiting."""
import contextlib
import importlib.util
import io
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from typing import Callable

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "gate_wait.py"

spec = importlib.util.spec_from_file_location("gate_wait", SCRIPT)
assert spec is not None and spec.loader is not None
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)

REPO = "owner/repo"
SHA = "f00d" * 10
REQUIRED = ("lint", "suite")
OWN = ("tag", "release-readiness")
PAGE = re.compile(r"^(?P<path>[^?]+)\?(?:head_sha=(?P<sha>[0-9a-f]+)&)?per_page=100&page=(?P<page>[0-9]+)$")


def check(name: str, conclusion: str | None = "success", suite: int = 1,
          status: str = "completed") -> dict:
    return {"name": name, "status": status, "conclusion": conclusion,
            "check_suite": {"id": suite}}


def run(suite: int = 1, event: str = "push") -> dict:
    return {"check_suite_id": suite, "event": event}


GREEN = [check("lint"), check("suite")]


Api = Callable[[str], dict]


def api(*polls: tuple[list[dict], list[dict]]) -> Api:
    """Answers the two listings gate_wait reads, one poll per check-runs read
    (the last repeating); any other request fails the test."""
    index = [-1]

    def fetch(path: str) -> dict:
        match = PAGE.match(path)
        if match is None:
            raise AssertionError(f"unmodeled request {path!r}")
        page = int(match["page"])
        if match["path"] == f"repos/{REPO}/commits/{SHA}/check-runs" and match["sha"] is None:
            if page == 1:
                index[0] = min(index[0] + 1, len(polls) - 1)
            key, items = "check_runs", polls[index[0]][0]
        elif match["path"] == f"repos/{REPO}/actions/runs" and match["sha"] == SHA:
            key, items = "workflow_runs", polls[index[0]][1]
        else:
            raise AssertionError(f"unmodeled request {path!r}")
        return {key: items[(page - 1) * 100:page * 100]}
    return fetch


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class Wait(unittest.TestCase):
    def wait(self, api: Api, required: tuple[str, ...] = REQUIRED,
             exclude: tuple[str, ...] = OWN, **keywords) -> tuple[int, str, Clock]:
        clock = Clock()
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = gate.wait(api, REPO, SHA, exclude, required=required,
                             clock=clock, sleep=clock.sleep, **keywords)
        return code, out.getvalue(), clock

    def assert_passes(self, api: Api, **keywords) -> Clock:
        code, out, clock = self.wait(api, **keywords)
        self.assertEqual(code, 0, out)
        return clock

    def assert_refuses(self, api: Api, fragment: str, **keywords) -> Clock:
        code, out, clock = self.wait(api, **keywords)
        self.assertEqual(code, 1, out)
        self.assertIn(fragment, out)
        return clock

    def test_every_gate_and_check_green_passes_without_waiting(self) -> None:
        clock = self.assert_passes(api((GREEN + [check("other")], [run()])))
        self.assertEqual(clock.sleeps, [])

    def test_a_pending_check_is_waited_for(self) -> None:
        pending = [check("lint"), check("suite", None, status="in_progress")]
        clock = self.assert_passes(api((pending, [run()]), (GREEN, [run()])))
        self.assertEqual(clock.sleeps, [20])

    def test_a_pending_check_that_then_fails_is_refused(self) -> None:
        pending = GREEN + [check("other", None, status="queued")]
        self.assert_refuses(api((pending, [run()]), (GREEN + [check("other", "failure")], [run()])),
                            "other")

    def test_its_own_jobs_are_excluded_by_exact_name(self) -> None:
        own = [check(name, "failure") for name in OWN] + [check("release-readiness", None, status="in_progress")]
        self.assert_passes(api((GREEN + own, [run()])))

    def test_a_name_only_sharing_a_prefix_or_suffix_with_its_own_is_judged(self) -> None:
        for name in ("tag-x", "tagx", "xtag", "release-readinessx", "release-readiness ",
                     "release-buildx"):
            with self.subTest(name=name):
                self.assert_refuses(api((GREEN + [check(name, "failure")], [run()])), name,
                                    exclude=OWN + ("release-build",))

    def test_a_required_gate_never_reported_is_refused_at_the_deadline(self) -> None:
        clock = self.assert_refuses(api(([check("lint")], [run()])), "suite", deadline_seconds=60)
        self.assertEqual(clock.sleeps, [20, 20, 20])

    def test_a_required_gate_reported_just_before_the_deadline_passes(self) -> None:
        late = api(([check("lint")], [run()]), ([check("lint")], [run()]), ([check("lint")], [run()]),
                   (GREEN, [run()]))
        clock = self.assert_passes(late, deadline_seconds=60)
        self.assertEqual(clock.sleeps, [20, 20, 20])

    def test_a_check_still_pending_at_the_deadline_is_refused(self) -> None:
        pending = GREEN + [check("other", None, status="in_progress")]
        self.assert_refuses(api((pending, [run()])), "other", deadline_seconds=40)

    def test_the_deadline_defaults_to_45_minutes(self) -> None:
        clock = self.assert_refuses(api(([check("lint")], [run()])), "suite")
        self.assertEqual(clock.now - 1000.0, 45 * 60)

    def test_a_commit_with_none_of_the_required_gates_is_refused(self) -> None:
        for checks in ([], [check("other"), check("tag")]):
            with self.subTest(checks=checks):
                self.assert_refuses(api((checks, [run()])), "lint", deadline_seconds=0)

    def test_any_non_passing_conclusion_on_another_check_is_refused(self) -> None:
        for conclusion in ("failure", "cancelled", "timed_out", "action_required", "stale", None):
            with self.subTest(conclusion=conclusion):
                self.assert_refuses(api((GREEN + [check("other", conclusion)], [run()])), "other")

    def test_another_check_may_pass_as_success_skipped_or_neutral(self) -> None:
        others = [check("a"), check("b", "skipped"), check("c", "neutral")]
        self.assert_passes(api((GREEN + others, [run()])))

    def test_a_required_gate_must_succeed_not_skip(self) -> None:
        for conclusion in ("skipped", "neutral", "failure", "cancelled"):
            with self.subTest(conclusion=conclusion):
                self.assert_refuses(api(([check("lint"), check("suite", conclusion)], [run()])), "suite")

    def test_one_failed_run_of_a_required_gate_refuses_beside_a_green_one(self) -> None:
        checks = GREEN + [check("suite", "failure", suite=2)]
        self.assert_refuses(api((checks, [run(), run(2)])), "suite")

    def test_check_runs_are_read_past_the_first_page(self) -> None:
        filler = [check(f"other-{index}") for index in range(100)]
        self.assert_refuses(api((filler + GREEN + [check("late", "failure")], [run()])), "late")
        self.assert_passes(api((filler + GREEN, [run()])))

    def test_workflow_runs_are_read_past_the_first_page(self) -> None:
        runs = [run(suite) for suite in range(10, 110)] + [run(2, "schedule")]
        self.assert_passes(api((GREEN + [check("scorecard", "failure", suite=2)], [run()] + runs)))

    def test_checks_of_runs_from_other_events_are_ignored(self) -> None:
        for event in ("schedule", "pull_request", "pull_request_target", "workflow_dispatch"):
            with self.subTest(event=event):
                ignored = [check("lint", "skipped", suite=2), check("other", "failure", suite=2),
                           check("other", None, suite=2, status="in_progress")]
                self.assert_passes(api((GREEN + ignored, [run(), run(2, event)])))

    def test_a_required_gate_from_another_event_does_not_count(self) -> None:
        checks = [check("lint"), check("suite", suite=2)]
        self.assert_refuses(api((checks, [run(), run(2, "schedule")])), "suite", deadline_seconds=0)

    def test_a_check_with_no_workflow_run_behind_it_is_judged(self) -> None:
        self.assert_refuses(api((GREEN + [check("CodeQL", "failure", suite=9)], [run()])), "CodeQL")


class CommandLine(unittest.TestCase):
    """main() through a stub `gh`, so the real fetch, argument parsing and the
    shipped gate list run."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        self.log = self.root / "calls.jsonl"
        stub = self.root / "bin" / "gh"
        stub.parent.mkdir()
        stub.write_text(f"#!{sys.executable}\n" + """
import json, os, re, sys
args = sys.argv[1:]
with open(os.environ["STUB_GH_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps(args) + "\\n")
scenario = json.loads(os.environ["STUB_GH_SCENARIO"])
if scenario.get("fail") or len(args) != 4 or args[:3] != ["api", "-H", "Cache-Control: no-cache"]:
    sys.stderr.write("stub gh: HTTP 502\\n")
    sys.exit(1)
page = int(re.search(r"[?&]page=([0-9]+)$", args[3]).group(1))
key = "check_runs" if "/check-runs?" in args[3] else "workflow_runs"
sys.stdout.write(json.dumps({key: scenario[key] if page == 1 else []}))
""", encoding="utf-8")
        stub.chmod(0o755)

    def main(self, scenario: dict, *arguments: str) -> tuple[int, str]:
        environ = {"PATH": f"{self.root / 'bin'}{os.pathsep}{os.environ['PATH']}",
                   "STUB_GH_LOG": str(self.log), "STUB_GH_SCENARIO": json.dumps(scenario)}
        out = io.StringIO()
        with unittest.mock.patch.dict(os.environ, environ), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = gate.main(["--repo", REPO, "--sha", SHA, "--exclude", "tag", *arguments])
        return code, out.getvalue()

    def calls(self) -> list[list[str]]:
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    def test_the_shipped_gate_list_passes_through_real_gh_calls(self) -> None:
        scenario = {"check_runs": [check(name) for name in gate.REQUIRED_GATES] + [check("tag", "failure")],
                    "workflow_runs": [run()]}
        code, out = self.main(scenario)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.calls(), [
            ["api", "-H", "Cache-Control: no-cache",
             f"repos/{REPO}/commits/{SHA}/check-runs?per_page=100&page=1"],
            ["api", "-H", "Cache-Control: no-cache",
             f"repos/{REPO}/actions/runs?head_sha={SHA}&per_page=100&page=1"]])

    def test_the_shipped_gate_list_refuses_a_missing_gate(self) -> None:
        scenario = {"check_runs": [check(name) for name in gate.REQUIRED_GATES[1:]],
                    "workflow_runs": [run()]}
        code, out = self.main(scenario, "--deadline-minutes", "0")
        self.assertEqual(code, 1, out)
        self.assertIn(gate.REQUIRED_GATES[0], out)

    def test_a_failed_api_call_is_refused_not_read_as_empty(self) -> None:
        code, out = self.main({"fail": True})
        self.assertEqual(code, 1, out)
        self.assertIn("HTTP 502", out)


if __name__ == "__main__":
    unittest.main()
