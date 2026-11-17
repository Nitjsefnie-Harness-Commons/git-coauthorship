"""Run conflict_markers.sh in throwaway repositories and pin its exit codes."""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "conflict_markers.sh"


class ConflictMarkers(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.repo = self.tmp / "repo"
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.repo)], check=True)

    def track(self, name: str, data: bytes) -> None:
        (self.repo / name).write_bytes(data)
        subprocess.run(["git", "-C", str(self.repo), "add", "--", name], check=True)

    def run_script(self, cwd: Path) -> subprocess.CompletedProcess[str]:
        # The ceiling keeps git from finding a repository above the temp dir.
        env = {**os.environ, "GIT_CEILING_DIRECTORIES": str(self.tmp)}
        return subprocess.run(["bash", str(SCRIPT)], cwd=cwd, env=env,
                              capture_output=True, text=True, check=False)

    def test_each_marker_git_writes_is_refused(self) -> None:
        for marker in (b"<<<<<<< HEAD", b"<<<<<<<", b"=======", b">>>>>>> topic", b">>>>>>>"):
            with self.subTest(marker):
                self.track("README.md", b"# doc\n\n" + marker + b"\nprose\n")
                done = self.run_script(self.repo)
                self.assertEqual(done.returncode, 1, done.stdout)
                self.assertIn("README.md:3:" + marker.decode(), done.stdout)
                self.assertIn("A merge-conflict marker is committed.", done.stdout)

    def test_a_marker_in_a_nested_file_is_refused(self) -> None:
        (self.repo / "docs").mkdir()
        self.track("docs/x.md", b"# doc\n\n=======\nprose\n")
        done = self.run_script(self.repo)
        self.assertEqual(done.returncode, 1, done.stdout)
        self.assertIn("docs/x.md:3:=======", done.stdout)

    def test_a_tree_without_a_marker_passes(self) -> None:
        near_misses = b"<<<<<<<x\n========\n>>>>>>>>\n ======= \n<<<<<< HEAD\n"
        self.track("README.md", b"# doc\n" + near_misses)
        self.track("blob.bin", b"\0binary\n=======\n")
        (self.repo / "untracked.md").write_bytes(b"=======\n")
        done = self.run_script(self.repo)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(done.stdout, "")

    def test_a_git_error_is_never_a_clean_tree(self) -> None:
        outside = self.tmp / "outside"
        outside.mkdir()
        done = self.run_script(outside)
        self.assertEqual(done.returncode, 128, done.stdout + done.stderr)
        self.assertIn("git grep exited 128, so no marker was looked for.", done.stdout)


if __name__ == "__main__":
    unittest.main()
