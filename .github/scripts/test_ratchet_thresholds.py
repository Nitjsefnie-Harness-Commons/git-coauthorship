"""Run ratchet_thresholds.py on throwaway records and pin its floor reads,
its raises and its refusals."""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "ratchet_thresholds.py"


def record(floor: object = 80.0, measured: object = 81.5) -> dict:
    return {"schema_version": 1, "coverage": {"python": {"floor": floor, "measured": measured}}}


class Ratchet(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        self.records = self.root / "ci-thresholds.json"
        self.out = self.root / "out"
        self.write(record())

    def write(self, data: object) -> None:
        text = data if isinstance(data, str) else json.dumps(data)
        self.records.write_text(text, encoding="utf-8")

    def run_script(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, "-I", str(SCRIPT), *args], capture_output=True,
                              text=True, check=False)

    def raise_to(self, measured: str) -> subprocess.CompletedProcess[str]:
        return self.run_script("raise", str(self.records), f"--measured={measured}", "--out-dir", str(self.out))

    def staged(self) -> dict:
        return json.loads((self.out / "ci-thresholds.json").read_text(encoding="utf-8"))

    def test_the_floor_is_printed_as_recorded(self) -> None:
        for floor, printed in ((80.0, "80.0"), (83.6, "83.6"), (33, "33")):
            with self.subTest(floor=floor):
                self.write(record(floor, 90))
                done = self.run_script("floor", str(self.records))
                self.assertEqual((done.returncode, done.stdout), (0, printed + "\n"), done.stderr)

    def test_a_measurement_above_floor_plus_gap_raises_floor_and_measured(self) -> None:
        done = self.raise_to("85.1")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.staged(), record(83.6, 85.1))
        self.assertEqual(json.loads(self.records.read_text(encoding="utf-8")), record())

    def test_a_raise_by_the_smallest_step_is_still_a_raise(self) -> None:
        done = self.raise_to("81.6")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.staged()["coverage"]["python"], {"floor": 80.1, "measured": 81.6})

    def test_a_measurement_at_or_below_floor_plus_gap_stages_nothing(self) -> None:
        for measured in ("81.5", "81.0", "40", "0"):
            with self.subTest(measured=measured):
                done = self.raise_to(measured)
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertIn("No raise", done.stdout)
                self.assertFalse(self.out.exists())

    def test_the_staged_document_is_valid_strict_json(self) -> None:
        self.assertEqual(self.raise_to("99.9").returncode, 0)
        text = (self.out / "ci-thresholds.json").read_text(encoding="utf-8")
        self.assertTrue(text.endswith("}\n"))
        json.loads(text, parse_constant=self.fail)

    def test_an_invalid_measurement_is_refused(self) -> None:
        for measured in ("", "nan", "inf", "-inf", "85.1%", "x", "100.1", "-0.1", "True"):
            with self.subTest(measured=measured):
                done = self.raise_to(measured)
                self.assertEqual(done.returncode, 1, done.stdout)
                self.assertIn("measured", done.stderr)
                self.assertFalse(self.out.exists())

    def test_an_invalid_record_is_refused_by_both_commands(self) -> None:
        python = record()["coverage"]["python"]
        cases = ("", "{", "[]", "null", "NaN",
                 {**record(), "extra": 1},
                 {"schema_version": 2, "coverage": {"python": python}},
                 {"schema_version": True, "coverage": {"python": python}},
                 {"schema_version": "1", "coverage": {"python": python}},
                 {"coverage": {"python": python}},
                 {"schema_version": 1},
                 {"schema_version": 1, "coverage": []},
                 {"schema_version": 1, "coverage": {"python": python, "go": python}},
                 {"schema_version": 1, "coverage": {"python": {**python, "cell": "x"}}},
                 {"schema_version": 1, "coverage": {"python": {"floor": 80}}},
                 record("80", 81.5), record(True, 81.5), record(None, 81.5), record(80, 101),
                 record(-1, 81.5), '{"schema_version": 1, "coverage": {"python": '
                                   '{"floor": NaN, "measured": 81.5}}}')
        for case in cases:
            for command in (("floor", str(self.records)),
                            ("raise", str(self.records), "--measured", "99", "--out-dir", str(self.out))):
                with self.subTest(case=case, command=command[0]):
                    self.write(case)
                    done = self.run_script(*command)
                    self.assertEqual(done.returncode, 1, done.stdout)
                    self.assertIn(str(self.records), done.stderr)
                    self.assertEqual(done.stdout, "")
                    self.assertFalse(self.out.exists())

    def test_a_missing_record_is_refused(self) -> None:
        self.records.unlink()
        done = self.run_script("floor", str(self.records))
        self.assertEqual((done.returncode, done.stdout), (1, ""))


if __name__ == "__main__":
    unittest.main()
