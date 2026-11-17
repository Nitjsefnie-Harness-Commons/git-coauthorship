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


def test_a_coauthor_line_git_does_not_parse_is_missing(tmp):
    """#378 (agent-harness-bundle): with no blank line before it, the line is
    body text to git, so the commit carries no trailer and the audit must say
    so rather than disagree with git and the push gate."""
    d, _hashes = _repo(tmp)
    with open(os.path.join(d, "f.txt"), "a", encoding="utf-8") as fh:
        fh.write("glued")
    _git(d, "add", "f.txt")
    _git(d, "commit", "-q", "-m",
         "fourth glued\n\nbody text\nCo-Authored-By: Bot <bot@example.com>")
    result, output, _error = _invoke(["-C", d])
    assert "4 commits, 2 co-authored, 2 missing" in output, output
    assert result == 1


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


_ACTIONS = ("github-actions[bot] <41898282+github-actions[bot]"
            "@users.noreply.github.com>")
_DEPENDABOT = ("dependabot[bot] <49699333+dependabot[bot]"
               "@users.noreply.github.com>")


def _commit_as(d, author, message):
    with open(os.path.join(d, "f.txt"), "a", encoding="utf-8") as fh:
        fh.write(message)
    _git(d, "add", "f.txt")
    _git(d, "commit", "-q", f"--author={author}", "-m", message)
    return _git(d, "rev-parse", "HEAD").stdout.strip()


def _only_gap_by(tmp, author):
    """Exit status and output for a range whose one gap is `author`'s."""
    d, hashes = _repo(tmp)
    _commit_as(d, author, "automated bump")
    return _invoke([f"{hashes[0]}..HEAD", "-C", d])


def test_bot_only_gaps_exit_zero_and_stay_in_every_tally(tmp):
    d, hashes = _repo(tmp)
    bot_hashes = [_commit_as(d, _ACTIONS, "actions bump"),
                  _commit_as(d, _DEPENDABOT, "dependabot bump")]
    result, output, error = _invoke(
        [f"{hashes[0]}..HEAD", "--list", "--missing", "-C", d])
    assert result == 0, output
    assert not error
    assert output.splitlines()[0] == (
        "4 commits, 2 co-authored, 2 missing (2 bot-authored, exempt)"), output
    assert f"      1  {_ACTIONS}" in output, output
    assert f"      1  {_DEPENDABOT}" in output, output
    assert "      2  (none)" in output, output
    listed = output.split("ALL COMMITS:", 1)[1].split("MISSING TRAILER:")[0]
    missing = output.split("MISSING TRAILER:", 1)[1]
    for h in bot_hashes:
        assert "MISSING" in next(line for line in listed.splitlines()
                                 if h[:9] in line), listed
        assert next(line for line in missing.splitlines()
                    if h[:9] in line).endswith("(bot-authored, exempt)"), missing


def test_a_human_gap_beside_bot_gaps_exits_one(tmp):
    d, hashes = _repo(tmp)
    _commit_as(d, _ACTIONS, "actions bump")
    _commit_as(d, _DEPENDABOT, "dependabot bump")
    result, output, _error = _invoke(["--missing", "-C", d])
    assert result == 1, output
    assert output.splitlines()[0] == (
        "5 commits, 2 co-authored, 3 missing (2 bot-authored, exempt)"), output
    human = next(line for line in output.split("MISSING TRAILER:", 1)[1]
                 .splitlines() if hashes[0][:9] in line)
    assert human.endswith("first missing"), human


def test_a_human_gap_beside_a_co_authored_bot_commit_exits_one(tmp):
    """Only bot GAPS are exempt: a bot commit with a trailer excuses nothing."""
    d, _hashes = _repo(tmp)
    _commit_as(d, _ACTIONS,
               "actions bump\n\nCo-Authored-By: Bot <bot@example.com>")
    result, output, _error = _invoke(["-C", d])
    assert result == 1, output
    assert output.splitlines()[0] == "4 commits, 3 co-authored, 1 missing"


def test_a_history_without_bots_prints_no_exemption(tmp):
    d, hashes = _repo(tmp)
    result, output, _error = _invoke(["--missing", "-C", d])
    assert result == 1
    assert output.splitlines()[0] == "3 commits, 2 co-authored, 1 missing"
    assert "bot-authored" not in output, output
    assert next(line for line in output.splitlines()
                if hashes[0][:9] in line).endswith("first missing"), output


def test_the_id_prefixed_bot_noreply_form_is_exempt(tmp):
    result, output, _error = _only_gap_by(tmp, _ACTIONS)
    assert result == 0, output
    assert output.splitlines()[0] == (
        "3 commits, 2 co-authored, 1 missing (1 bot-authored, exempt)"), output


def test_the_bare_bot_noreply_form_is_exempt(tmp):
    result, output, _error = _only_gap_by(
        tmp, "dependabot[bot] <dependabot[bot]@users.noreply.github.com>")
    assert result == 0, output


def test_the_bot_noreply_domain_is_compared_case_blind(tmp):
    result, output, _error = _only_gap_by(
        tmp, "dependabot[bot] <49699333+dependabot[bot]"
        "@Users.NoReply.GitHub.com>")
    assert result == 0, output


def test_a_bot_name_with_a_non_noreply_email_is_gated(tmp):
    result, output, _error = _only_gap_by(
        tmp, "github-actions[bot] <ci@example.com>")
    assert result == 1, output


def test_a_bot_noreply_email_under_a_name_lacking_bot_is_gated(tmp):
    result, output, _error = _only_gap_by(
        tmp, "github-actions <41898282+github-actions[bot]"
        "@users.noreply.github.com>")
    assert result == 1, output


def test_a_person_noreply_email_under_a_bot_name_is_gated(tmp):
    """A user's own noreply address has no `[bot]` in its local part."""
    result, output, _error = _only_gap_by(
        tmp, "github-actions[bot] <41898282+github-actions"
        "@users.noreply.github.com>")
    assert result == 1, output


def test_a_non_numeric_noreply_prefix_is_gated(tmp):
    result, output, _error = _only_gap_by(
        tmp, "github-actions[bot] <ci+github-actions[bot]"
        "@users.noreply.github.com>")
    assert result == 1, output


def test_an_empty_noreply_id_prefix_is_gated(tmp):
    result, output, _error = _only_gap_by(
        tmp, "github-actions[bot] <+github-actions[bot]"
        "@users.noreply.github.com>")
    assert result == 1, output


def test_an_empty_noreply_bot_name_is_gated(tmp):
    result, output, _error = _only_gap_by(
        tmp, "github-actions[bot] <[bot]@users.noreply.github.com>")
    assert result == 1, output


def test_a_name_with_bot_mid_name_is_gated(tmp):
    result, output, _error = _only_gap_by(
        tmp, "github-actions[bot] ci <41898282+github-actions[bot]"
        "@users.noreply.github.com>")
    assert result == 1, output


# The two non-ASCII near misses call the predicate directly: a non-ASCII
# `--author` would test the host's argv encoding, not this predicate.
def test_a_non_ascii_digit_noreply_id_is_not_a_bot(_tmp):
    assert _AS.is_bot_author(
        "dependabot[bot]", "42+dependabot[bot]@users.noreply.github.com")
    assert not _AS.is_bot_author(
        "dependabot[bot]",
        "٤٢+dependabot[bot]@users.noreply.github.com")


def test_a_unicode_case_folded_noreply_domain_is_not_a_bot(_tmp):
    assert _AS.is_bot_author(
        "dependabot[bot]", "dependabot[bot]@users.noreply.GITHUB.com")
    assert not _AS.is_bot_author(
        "dependabot[bot]", "dependabot[bot]@users.noreply.gıthub.com")


def test_a_bot_noreply_domain_near_miss_is_gated(tmp):
    result, output, _error = _only_gap_by(
        tmp, "github-actions[bot] <41898282+github-actions[bot]"
        "@users.noreply.github.com.example>")
    assert result == 1, output


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
