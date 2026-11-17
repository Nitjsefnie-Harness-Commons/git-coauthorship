"""Drive publish_release.sh against a stub `gh` and pin every call it makes."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "publish_release.sh"
REPO = "owner/repo"
TAG = "v1.2.0"
WHEEL = "dist/git_coauthorship-1.2.0-py3-none-any.whl"

# Records each call, then answers only the four commands the script may run;
# anything else exits 97, so an unexpected call cannot pass silently.
STUB = """
import json, os, sys
args = sys.argv[1:]
with open(os.environ["STUB_GH_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps(args) + "\\n")
scenario = json.loads(os.environ["STUB_GH_SCENARIO"])
kind = "list" if args[:1] == ["api"] else args[1] if args[:1] == ["release"] and len(args) > 1 else None
if kind not in ("list", "create", "upload", "edit"):
    sys.stderr.write("stub gh: unmodeled command %r\\n" % (args,))
    sys.exit(97)
if kind == scenario.get("fail"):
    sys.stderr.write("stub gh: HTTP 502 on %s\\n" % kind)
    sys.exit(1)
if kind == "list":
    sys.stdout.write(scenario.get("releases", ""))
"""

LIST = ["api", "--paginate", f"repos/{REPO}/releases?per_page=100",
        "--jq", ".[] | [.tag_name, .draft] | @tsv"]
CREATE = ["release", "create", TAG, "--repo", REPO, "--draft",
          "--title", TAG, "--generate-notes"]
UPLOAD = ["release", "upload", TAG, "--repo", REPO, WHEEL,
          "dist/SHA256SUMS", "--clobber"]
PUBLISH = ["release", "edit", TAG, "--repo", REPO, "--draft=false"]


class PublishRelease(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        stub = self.root / "bin" / "gh"
        stub.parent.mkdir()
        stub.write_text(f"#!{sys.executable}\n{STUB}", encoding="utf-8")
        stub.chmod(0o755)
        (self.root / "dist").mkdir()
        (self.root / WHEEL).write_bytes(b"wheel")
        (self.root / "dist" / "SHA256SUMS").write_text("sums\n", encoding="utf-8")
        self.log = self.root / "calls.jsonl"

    def run_script(self, releases: str = "", fail: str = "",
                   ref: str = f"refs/tags/{TAG}") -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        env.update(PATH=f"{self.root / 'bin'}{os.pathsep}{env['PATH']}",
                   GITHUB_REF=ref, GITHUB_REPOSITORY=REPO,
                   STUB_GH_LOG=str(self.log),
                   STUB_GH_SCENARIO=json.dumps({"releases": releases, "fail": fail}))
        return subprocess.run(["bash", str(SCRIPT)], cwd=self.root, env=env,
                              capture_output=True, text=True, check=False)

    def calls(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    def assert_refused(self, result: subprocess.CompletedProcess[str],
                       calls: list[list[str]], message: str) -> None:
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.calls(), calls)
        self.assertIn(message, result.stderr)
        self.assertNotIn("Published", result.stdout)

    def assert_published(self, result: subprocess.CompletedProcess[str],
                         calls: list[list[str]]) -> None:
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), calls)
        self.assertIn(f"Published {TAG}.", result.stdout)

    def test_a_published_release_is_refused_without_any_write(self) -> None:
        result = self.run_script(f"v1.1.0\tfalse\n{TAG}\tfalse\n")
        self.assert_refused(result, [LIST], f"{TAG} was already published")

    def test_a_published_release_wins_over_a_draft_for_the_same_tag(self) -> None:
        result = self.run_script(f"{TAG}\ttrue\n{TAG}\tfalse\n")
        self.assert_refused(result, [LIST], f"{TAG} was already published")

    def test_a_draft_is_completed_by_uploading_into_it_then_publishing(self) -> None:
        result = self.run_script(f"{TAG}\ttrue\nv1.1.0\tfalse\n")
        self.assert_published(result, [LIST, UPLOAD, PUBLISH])

    def test_no_release_creates_a_draft_uploads_then_publishes(self) -> None:
        self.assert_published(self.run_script(""), [LIST, CREATE, UPLOAD, PUBLISH])

    def test_only_an_exact_tag_match_counts_as_this_release(self) -> None:
        result = self.run_script(f"{TAG}-rc1\tfalse\nv1.2.0.1\ttrue\nv1.1.0\tfalse\n")
        self.assert_published(result, [LIST, CREATE, UPLOAD, PUBLISH])

    def test_two_drafts_for_the_tag_are_refused(self) -> None:
        result = self.run_script(f"{TAG}\ttrue\n{TAG}\ttrue\n")
        self.assert_refused(result, [LIST], f"2 draft releases for {TAG}")

    def test_an_unreadable_draft_flag_is_refused(self) -> None:
        result = self.run_script(f"{TAG}\tnull\n")
        self.assert_refused(result, [LIST], "unexpected draft flag")

    def test_a_failed_listing_is_never_read_as_no_release(self) -> None:
        result = self.run_script("", fail="list")
        self.assert_refused(result, [LIST], "HTTP 502 on list")

    def test_a_failed_create_stops_before_any_upload(self) -> None:
        result = self.run_script("", fail="create")
        self.assert_refused(result, [LIST, CREATE], "HTTP 502 on create")

    def test_a_failed_upload_leaves_the_new_draft_unpublished(self) -> None:
        result = self.run_script("", fail="upload")
        self.assert_refused(result, [LIST, CREATE, UPLOAD], "HTTP 502 on upload")

    def test_a_failed_upload_leaves_the_found_draft_unpublished(self) -> None:
        result = self.run_script(f"{TAG}\ttrue\n", fail="upload")
        self.assert_refused(result, [LIST, UPLOAD], "HTTP 502 on upload")

    def test_a_failed_publish_is_never_reported_as_published(self) -> None:
        result = self.run_script(f"{TAG}\ttrue\n", fail="edit")
        self.assert_refused(result, [LIST, UPLOAD, PUBLISH], "HTTP 502 on edit")

    def test_a_non_tag_ref_is_refused_before_any_call(self) -> None:
        result = self.run_script("", ref="refs/heads/main")
        self.assert_refused(result, [], "Not a tag ref (refs/heads/main); nothing to publish.")


if __name__ == "__main__":
    unittest.main()
