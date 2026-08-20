"""Behavior and command-line coverage for :mod:`author_stats`."""
import contextlib
import io
import os
import shutil
import subprocess
import sys

import _util

_AS = _util.load(os.path.join(_util.SCRIPTS, "author_stats.py"))


def _require_git():
    if not shutil.which("git"):
        _util.skip("git is not available on PATH")


def _tally(pairs):
    """pairs -> records shaped like git_log_records output."""
    return [("h" * 40, name, email, "2026-08-03", "subj", "body")
            for name, email in pairs]


def _git(d, *args):
    return subprocess.run(["git", "-C", d, *args], capture_output=True,
                          text=True, check=True, timeout=60)


def _repo(tmp):
    """Create a local-configured repo with missing, single, and multi trailers."""
    _require_git()
    d = os.path.join(tmp, "repo")
    os.makedirs(d)
    _git(d, "init", "-q")
    _git(d, "config", "user.name", "Tester")
    _git(d, "config", "user.email", "tester@example.com")
    _git(d, "config", "commit.gpgsign", "false")

    path = os.path.join(d, "f.txt")
    messages = (
        "first missing",
        "second lower-case trailer\n\nco-authored-by: Bot <bot@example.com>",
        "third multi trailer\n\nCo-Authored-By: Bot <bot@example.com>\n"
        "Co-Authored-By: Helper <helper@example.com>",
    )
    hashes = []
    for index, message in enumerate(messages):
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(str(index))
        _git(d, "add", "f.txt")
        _git(d, "commit", "-q", "-m", message)
        hashes.append(_git(d, "rev-parse", "HEAD").stdout.strip())
    return d, hashes


def _invoke(args):
    old_argv = sys.argv
    stdout = io.StringIO()
    stderr = io.StringIO()
    try:
        sys.argv = ["author-stats", *args]
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = _AS.main()
    finally:
        sys.argv = old_argv
    return result, stdout.getvalue(), stderr.getvalue()


def test_tally_authors_counts_per_identity(_tmp):
    got = _AS.tally_authors(_tally([
        ("Nitjsefnie", "z@example.com"),
        ("Nitjsefnie", "z@example.com"),
        ("Someone Else", "s@example.com"),
    ]))
    assert got == {"Nitjsefnie <z@example.com>": 2,
                   "Someone Else <s@example.com>": 1}


def test_same_name_different_email_stays_separate(_tmp):
    """Keying on name alone would merge two distinct identities."""
    got = _AS.tally_authors(_tally([
        ("Alex", "alex@work.com"),
        ("Alex", "alex@personal.com"),
    ]))
    assert got == {"Alex <alex@work.com>": 1,
                   "Alex <alex@personal.com>": 1}


def test_empty_history_is_empty_tally(_tmp):
    assert _AS.tally_authors([]) == {}


def test_records_carry_six_fields_from_real_git(tmp):
    """The author email must not shift date, subject, or body columns."""
    d, _hashes = _repo(tmp)
    recs = _AS.git_log_records(d, None)
    assert len(recs) == 3, recs
    h, name, email, date, subject, body = recs[-1]
    assert len(h) == 40
    assert name == "Tester"
    assert email == "tester@example.com"
    assert date.startswith("20")
    assert subject == "first missing"
    assert "Co-Authored-By" not in body
    assert _AS.tally_authors(recs) == {"Tester <tester@example.com>": 3}


def test_main_summary_reports_tallies_and_missing_status(tmp):
    d, _hashes = _repo(tmp)
    result, output, error = _invoke(["-C", d])
    assert result == 1
    assert not error
    assert "3 commits, 2 co-authored, 1 missing" in output
    assert "By author (1 distinct; the git author field):" in output
    assert "By trailer (commits; a commit may carry more than one):" in output
    assert "(none)" in output
    assert "Bot <bot@example.com>" in output
    assert "Helper <helper@example.com>" in output


def test_main_list_reports_every_commit_and_multi_trailer_count(tmp):
    d, _hashes = _repo(tmp)
    result, output, error = _invoke(["--list", "-C", d])
    assert result == 1
    assert not error
    assert "ALL COMMITS:" in output
    assert "MISSING" in output
    assert "Bot <bot@example.com> (+1)" in output
    assert output.count("  ") > 3


def test_main_missing_reports_only_missing_commits(tmp):
    d, hashes = _repo(tmp)
    result, output, error = _invoke(["--missing", "-C", d])
    assert result == 1
    assert not error
    section = output.split("MISSING TRAILER:", 1)[1]
    assert hashes[0][:9] in section
    assert "first missing" in section
    assert hashes[1][:9] not in section
    assert "second lower-case trailer" not in section


def test_main_list_and_missing_modes_are_independent(tmp):
    d, _hashes = _repo(tmp)
    result, output, error = _invoke(["--list", "--missing", "-C", d])
    assert result == 1
    assert not error
    assert "ALL COMMITS:" in output
    assert "MISSING TRAILER:" in output


def test_main_accepts_revision_range_and_returns_zero_when_complete(tmp):
    d, hashes = _repo(tmp)
    result, output, error = _invoke([f"{hashes[0]}..HEAD", "-C", d])
    assert result == 0
    assert not error
    assert "2 commits, 2 co-authored, 0 missing" in output


def test_main_reports_empty_revision_range_as_success(tmp):
    d, hashes = _repo(tmp)
    result, output, error = _invoke([f"{hashes[0]}..{hashes[0]}", "-C", d])
    assert result == 0
    assert not error
    assert output.strip() == "No commits in range."


def test_main_exits_two_for_an_invalid_revision_range(tmp):
    d, _hashes = _repo(tmp)
    old_argv = sys.argv
    stdout = io.StringIO()
    stderr = io.StringIO()
    try:
        sys.argv = ["author-stats", "not-a-revision", "-C", d]
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                _AS.main()
            except SystemExit as exc:
                assert exc.code == 2
            else:
                raise AssertionError("invalid revision did not exit 2")
    finally:
        sys.argv = old_argv
    assert "git log failed" in stderr.getvalue()


def main():
    return _util.runner(_util.collect(globals()), tmp_prefix="authorstats_")


if __name__ == "__main__":
    raise SystemExit(main())
