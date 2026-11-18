"""Pin perf_budget.py's verdicts, its lowering and rebaseline, its refusals and
exit codes, and its measuring harness behind a stub valgrind."""
import copy
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
SCRIPT = HERE / "perf_budget.py"
# `valgrind --tool=callgrind --collect-atstart=no --toggle-collect=_dl_fini /usr/bin/true`, valgrind 3.24.0.
CAPTURE = HERE / "test_perf_budget_callgrind.out"
spec = importlib.util.spec_from_file_location("perf_budget", SCRIPT)
assert spec is not None and spec.loader is not None
pb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pb)

CHAIN = {"python": "3.13.14 (main) [GCC]", "valgrind": "valgrind-3.24.0", "libc": "glibc 2.39"}
# The harness pins its children's environment, so the stub takes its orders from files beside it.
STUB = """
import os, shutil, subprocess, sys
if sys.argv[1:] == ["--version"]:
    sys.exit(print("valgrind-stub"))
here = os.path.dirname(os.path.abspath(sys.argv[0]))
target = next(arg.split("=", 1)[1] for arg in sys.argv if arg.startswith("--callgrind-out-file="))
if os.path.exists(os.path.join(here, "capture.out")):
    shutil.copyfile(os.path.join(here, "capture.out"), target)
command = [arg for arg in sys.argv[1:] if not arg.startswith("--")]
if os.path.exists(os.path.join(here, "exit")):
    sys.exit(subprocess.run(command).returncode and int(open(os.path.join(here, "exit")).read()))
if os.path.exists(os.path.join(here, "mute")):
    sys.exit(subprocess.run(command, stdout=subprocess.DEVNULL).returncode)
os.execv(command[0], command)
"""


def budget(**workloads: int) -> dict:
    return {"schema_version": 1, "counter": "callgrind-ir", "tolerance_pct": 1.0, "toolchain": dict(CHAIN),
            "workloads": workloads or {"author-stats": 1000, "reauthor-mapping": 2000}}


def measurement(**counts: int) -> dict:
    return {"schema_version": 1, "counter": "callgrind-ir", "toolchain": dict(CHAIN),
            "counts": counts or {"author-stats": 1000, "reauthor-mapping": 2000}}


class Verdicts(unittest.TestCase):
    def verdicts(self, document: dict, measured: dict) -> tuple[bool, list[str]]:
        failed, lines = pb.check(document, measured)
        return failed, [line.split(" | ")[-1].rstrip(" |") for line in lines[2:] if line.startswith("| ")]

    def test_a_count_within_the_tolerance_passes_and_one_past_it_fails(self) -> None:
        for author, failed, verdict in ((1010, False, "ok"), (1011, True, "over budget"), (1, False, "ok")):
            with self.subTest(author=author):
                self.assertEqual(self.verdicts(budget(), measurement(**{"author-stats": author, "reauthor-mapping": 2000})),
                                 (failed, [verdict, "ok"]))

    def test_a_missing_or_undeclared_workload_fails(self) -> None:
        self.assertEqual(self.verdicts(budget(), measurement(**{"author-stats": 1000})),
                         (True, ["ok", "missing from the measurement"]))
        self.assertEqual(self.verdicts(budget(**{"author-stats": 1000}), measurement()),
                         (True, ["ok", "unknown: the budget does not declare it"]))

    def test_another_toolchain_compares_nothing_and_names_the_route(self) -> None:
        for field in pb.TOOLCHAIN:
            with self.subTest(field=field):
                measured = measurement()
                measured["toolchain"][field] = "other"
                failed, lines = pb.check(budget(), measured)
                self.assertTrue(failed)
                self.assertEqual(self.verdicts(budget(), measured)[1], ["not compared"] * 2)
                self.assertIn(f"{field}: {CHAIN[field]!r} -> 'other'", lines[-1])
                self.assertIn("perf_budget.py rebaseline", lines[-1])


class Tighten(unittest.TestCase):
    def test_only_a_count_below_half_the_tolerance_is_lowered(self) -> None:
        lowered = pb.tighten(budget(), measurement(**{"author-stats": 994, "reauthor-mapping": 1990}))
        self.assertEqual(lowered, budget(**{"author-stats": 994, "reauthor-mapping": 2000}))
        self.assertIsNone(pb.tighten(budget(), measurement(**{"author-stats": 995, "reauthor-mapping": 1990})))

    def test_a_far_cheaper_run_is_lowered_not_failed(self) -> None:
        self.assertEqual(pb.tighten(budget(), measurement(**{"author-stats": 3, "reauthor-mapping": 4})),
                         budget(**{"author-stats": 3, "reauthor-mapping": 4}))

    def test_a_failing_or_foreign_measurement_lowers_nothing(self) -> None:
        foreign = measurement(**{"author-stats": 3, "reauthor-mapping": 4})
        foreign["toolchain"]["libc"] = "other"
        for measured in (measurement(**{"author-stats": 3, "reauthor-mapping": 9000}),
                         measurement(**{"author-stats": 3}), foreign):
            with self.subTest(measured=measured):
                self.assertIsNone(pb.tighten(budget(), measured))


class Rebaseline(unittest.TestCase):
    def test_it_declares_each_raised_count_and_keeps_the_tolerance(self) -> None:
        document = {**budget(), "tolerance_pct": 0.5}
        measured = measurement(**{"author-stats": 1500, "reauthor-mapping": 1900})
        measured["toolchain"]["python"] = "next"
        new, raises = pb.rebaseline(document, measured)
        self.assertEqual(new, {**budget(**measured["counts"]), "tolerance_pct": 0.5, "toolchain": measured["toolchain"]})
        self.assertEqual(raises, ["Budget-Raise: .github/perf-budgets.json workloads.author-stats 1000 -> 1500"])

    def test_a_seed_declares_nothing_and_a_partial_measurement_is_refused(self) -> None:
        self.assertEqual(pb.rebaseline(None, measurement()), (budget(), []))
        with self.assertRaises(pb.BudgetError):
            pb.rebaseline(None, measurement(**{"author-stats": 1}))

    def test_the_committed_budget_is_valid_and_declares_every_workload(self) -> None:
        document = pb.load(HERE.parent / "perf-budgets.json", pb.validate)
        self.assertEqual(set(document["workloads"]), set(pb.WORKLOADS))


class Callgrind(unittest.TestCase):
    def test_the_captured_output_reads_as_its_summary(self) -> None:
        self.assertEqual(pb.callgrind_total(CAPTURE), 916)

    def test_an_output_without_one_nonzero_ir_summary_is_a_broken_run(self) -> None:
        text = CAPTURE.read_text(encoding="utf-8")
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        for broken in (text.replace("summary: 916", ""), text.replace("summary: 916", "summary: 916\nsummary: 916"),
                       text.replace("summary: 916", "summary: 0"), text.replace("summary: 916", "summary: 9x"),
                       text.replace("events: Ir", "events: Ir Dr"), ""):
            with self.subTest(broken=broken[:0] or len(broken)):
                (root / "out").write_text(broken, encoding="utf-8")
                with self.assertRaises(pb.HarnessError):
                    pb.callgrind_total(root / "out")
        with self.assertRaises(pb.HarnessError):
            pb.callgrind_total(root / "absent")


class Cli(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        self.budget, self.measurement, self.out = self.root / "b.json", self.root / "m.json", self.root / "staged"
        self.write(self.budget, budget())
        self.write(self.measurement, measurement())

    @staticmethod
    def write(path: Path, data: object) -> None:
        path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")

    def run_script(self, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, "-I", str(SCRIPT), *args], capture_output=True, text=True,
                              check=False, env={**os.environ, **env})

    def judge(self, command: str, *extra: str) -> subprocess.CompletedProcess[str]:
        return self.run_script(command, "--budget", str(self.budget), "--measurement", str(self.measurement), *extra)

    def test_check_exits_0_within_budget_and_1_over_it_writing_the_summary(self) -> None:
        summary = self.root / "summary.md"
        self.assertEqual(self.judge("check", "--summary", str(summary)).returncode, 0)
        self.write(self.measurement, measurement(**{"author-stats": 1011, "reauthor-mapping": 2000}))
        done = self.judge("check", "--summary", str(summary))
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertEqual(summary.read_text(encoding="utf-8").count("### Instruction counts"), 2)
        self.assertIn("| author-stats | 1011 | 1000 | over budget |", summary.read_text(encoding="utf-8"))

    def test_an_unreadable_budget_or_measurement_exits_2(self) -> None:
        valid = budget()
        cases = ["{", "[]", {**valid, "extra": 1}, {k: v for k, v in valid.items() if k != "toolchain"},
                 {**valid, "schema_version": 2}, {**valid, "schema_version": True}, {**valid, "counter": "wall"},
                 {**valid, "tolerance_pct": -1}, {**valid, "tolerance_pct": "1"}, {**valid, "tolerance_pct": True},
                 json.dumps(valid).replace('"tolerance_pct": 1.0', '"tolerance_pct": NaN'),
                 {**valid, "toolchain": {**CHAIN, "libc": ""}},
                 {**valid, "toolchain": {"python": "p", "valgrind": "v"}}, {**valid, "workloads": {}},
                 {**valid, "workloads": {"author-stats": 0}}, {**valid, "workloads": {"author-stats": 1.5}},
                 {**valid, "workloads": {"author-stats": True}}]
        for case in cases:
            for command in ("check", "tighten", "rebaseline"):
                with self.subTest(case=case, command=command):
                    self.write(self.budget, case)
                    done = self.judge(command, *(("--out-dir", str(self.out)) if command == "tighten" else ()))
                    self.assertEqual((done.returncode, done.stdout), (2, ""), done.stderr)
                    self.assertIn(str(self.budget), done.stderr)
                    self.assertFalse(self.out.exists())
        self.write(self.budget, budget())
        for case in ("", {**measurement(), "counter": "x"}, {**measurement(), "counts": {"author-stats": -1}},
                     {k: v for k, v in measurement().items() if k != "toolchain"}):
            with self.subTest(measurement=case):
                self.write(self.measurement, case)
                self.assertEqual(self.judge("check").returncode, 2)

    def test_tighten_stages_only_a_lowered_copy(self) -> None:
        self.assertEqual(self.judge("tighten", "--out-dir", str(self.out)).returncode, 0)
        self.assertFalse(self.out.exists())
        self.write(self.measurement, measurement(**{"author-stats": 900, "reauthor-mapping": 2000}))
        done = self.judge("tighten", "--out-dir", str(self.out))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads((self.out / "perf-budgets.json").read_text(encoding="utf-8")),
                         budget(**{"author-stats": 900, "reauthor-mapping": 2000}))
        self.assertEqual(json.loads(self.budget.read_text(encoding="utf-8")), budget())

    def test_rebaseline_writes_the_budget_and_prints_its_declarations(self) -> None:
        self.write(self.measurement, measurement(**{"author-stats": 1200, "reauthor-mapping": 2000}))
        done = self.judge("rebaseline")
        self.assertEqual((done.returncode, done.stdout),
                         (0, "Budget-Raise: .github/perf-budgets.json workloads.author-stats 1000 -> 1200\n"))
        self.assertEqual(json.loads(self.budget.read_text(encoding="utf-8")),
                         budget(**{"author-stats": 1200, "reauthor-mapping": 2000}))
        self.budget.unlink()
        self.assertEqual(self.judge("rebaseline").returncode, 0)
        self.assertEqual(json.loads(self.budget.read_text(encoding="utf-8"))["tolerance_pct"], pb.DEFAULT_TOLERANCE_PCT)


class Measure(unittest.TestCase):
    """The harness end to end, valgrind replaced by a stub that writes the captured output and runs the rest natively."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        (self.root / "bin").mkdir()
        stub = self.root / "bin" / "valgrind"
        stub.write_text(f"#!{sys.executable}\n{STUB}", encoding="utf-8")
        stub.chmod(0o755)
        dpkg = self.root / "bin" / "dpkg-query"
        # As dpkg-query does, the format repeats per installed architecture; libc6 has two here.
        dpkg.write_text(f"#!{sys.executable}\nimport sys\nname, form = sys.argv[-1], sys.argv[-2].replace('\\\\n', '\\n')\n"
                        "print(''.join(form.replace('${Version}', name + arch) for arch in "
                        "(['-rev', '-i386'] if name == 'libc6' else ['-rev'])), end='')\n", encoding="utf-8")
        dpkg.chmod(0o755)
        shutil.copyfile(CAPTURE, self.root / "bin" / "capture.out")
        self.env = {**os.environ, "PATH": f"{self.root / 'bin'}{os.pathsep}{os.environ['PATH']}"}

    def measure(self, **env: str) -> subprocess.CompletedProcess[str]:
        shutil.rmtree(self.root / "work", ignore_errors=True)
        return subprocess.run([sys.executable, "-I", str(SCRIPT), "measure", "--work", str(self.root / "work"),
                               "--out", str(self.root / "m.json")], capture_output=True, text=True, check=False,
                              env={**self.env, **env})

    def test_every_workload_runs_and_is_counted(self) -> None:
        done = self.measure()
        self.assertEqual(done.returncode, 0, done.stderr)
        result = json.loads((self.root / "m.json").read_text(encoding="utf-8"))
        self.assertEqual(result["counts"], {name: 916 for name in pb.WORKLOADS})
        self.assertEqual((result["counter"], result["toolchain"]["valgrind"]),
                         ("callgrind-ir", "valgrind-stub, package valgrind valgrind-rev"))
        self.assertTrue(result["toolchain"]["libc"].endswith(", package libc6 libc6-rev libc6-i386"), result["toolchain"])
        self.assertEqual(set(result["wall_seconds"]), set(pb.WORKLOADS))
        self.assertEqual(pb.validate_measurement(copy.deepcopy(result)), result)

    def test_a_broken_harness_exits_3(self) -> None:
        capture, stop, mute = (self.root / "bin" / name for name in ("capture.out", "exit", "mute"))
        for orders, env, reason in (({capture: ""}, {}, "no single nonzero Ir summary"),
                                    ({capture: None}, {}, "cannot read"),
                                    ({stop: "7"}, {}, f"author-stats exited 7 (expected 1) printing '{pb.WORKLOADS['author-stats'][2]}"),
                                    ({mute: ""}, {}, "author-stats exited 1 (expected 1) printing ''"),
                                    ({}, {"PATH": f"{self.root / 'bin'}{os.pathsep}/nonexistent"}, "git is not on PATH")):
            with self.subTest(reason=reason):
                for path in (stop, mute):
                    path.unlink(missing_ok=True)
                shutil.copyfile(CAPTURE, capture)
                for path, text in orders.items():
                    _ = path.write_text(text, encoding="utf-8") if text is not None else path.unlink()
                done = self.measure(**env)
                self.assertEqual(done.returncode, 3, done.stdout)
                self.assertIn(reason, done.stderr)
        shutil.copyfile(CAPTURE, capture)
        (self.root / "work").mkdir()
        done = subprocess.run([sys.executable, "-I", str(SCRIPT), "measure", "--work", str(self.root / "work"),
                               "--out", str(self.root / "m.json")], capture_output=True, text=True, check=False, env=self.env)
        self.assertEqual(done.returncode, 3, done.stdout)
        self.assertIn("File exists", done.stderr)

    def test_the_history_is_the_same_on_every_build(self) -> None:
        heads = []
        for name in ("one", "two"):
            pb.perf_history.build(self.root / name, 40)
            heads.append([subprocess.run(["git", "-C", str(self.root / name), *args], capture_output=True, text=True,
                                         check=True).stdout for args in (("rev-list", "--count", "HEAD"), ("rev-parse", "HEAD"))])
        self.assertEqual(heads[0], heads[1])
        self.assertEqual(heads[0][0], "40\n")


if __name__ == "__main__":
    unittest.main()
