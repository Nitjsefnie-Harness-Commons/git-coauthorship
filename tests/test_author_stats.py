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
    except SystemExit as exc:
        result = exc.code
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
    assert output.splitlines()[0] == "3 commits, 2 co-authored, 1 missing"
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


def test_main_reports_empty_revision_range_as_success(tmp):
    d, hashes = _repo(tmp)
    result, output, error = _invoke([f"{hashes[0]}..{hashes[0]}", "-C", d])
    assert result == 0
    assert not error
    assert output.strip() == "No commits in range."


_ACTIONS = "github-actions[bot] <41898282+github-actions[bot]@users.noreply.github.com>"
_DEPENDABOT = "dependabot[bot] <dependabot[bot]@users.noreply.github.com>"


def _commit_as(d, author, message):
    with open(os.path.join(d, "f.txt"), "a", encoding="utf-8") as fh:
        fh.write(message)
    _git(d, "add", "f.txt")
    _git(d, "commit", "-q", f"--author={author}", "-m", message)
    return _git(d, "rev-parse", "HEAD").stdout.strip()


def test_bot_gaps_are_counted_and_marked_but_only_a_human_gap_fails(tmp):
    """A complete range exits 0. The id-prefixed and bare noreply forms exempt
    their gap and stay in every tally; a human gap still exits 1 beside bot
    gaps, a co-authored bot commit and a bot address under a name lacking
    `[bot]`, and `--missing` lists no co-authored commit."""
    d, hashes = _repo(tmp)
    result, output, error = _invoke([f"{hashes[0]}..HEAD", "-C", d])
    assert (result, error) == (0, "") and "2 commits, 2 co-authored, 0 missing" in output, output
    bots = [_commit_as(d, who, "bump") for who in (_ACTIONS, _DEPENDABOT)]
    result, output, _error = _invoke([f"{hashes[0]}..{bots[0]}", "-C", d])
    assert (result, output.splitlines()[0]) == (
        0, "3 commits, 2 co-authored, 1 missing (1 bot-authored, exempt)"), output
    result, output, error = _invoke([f"{hashes[0]}..HEAD", "--list", "--missing", "-C", d])
    assert (result, error, output.splitlines()[0]) == (
        0, "", "4 commits, 2 co-authored, 2 missing (2 bot-authored, exempt)"), output
    for line in (f"      1  {_ACTIONS}", f"      1  {_DEPENDABOT}", "      2  (none)"):
        assert line in output, output
    listed, missing = output.split("ALL COMMITS:", 1)[1].split("MISSING TRAILER:")
    for h in bots:
        assert "MISSING" in next(x for x in listed.splitlines() if h[:9] in x), listed
        assert next(x for x in missing.splitlines() if h[:9] in x).endswith(
            "(bot-authored, exempt)"), missing
    _commit_as(d, _ACTIONS, "bump\n\nCo-Authored-By: Bot <bot@example.com>")
    _commit_as(d, _ACTIONS.replace("[bot] <", " <"), "spoof")
    result, output, error = _invoke(["--missing", "-C", d])
    assert (result, error, output.splitlines()[0]) == (
        1, "", "7 commits, 3 co-authored, 4 missing (2 bot-authored, exempt)"), output
    section = output.split("MISSING TRAILER:", 1)[1]
    assert next(x for x in section.splitlines() if hashes[0][:9] in x).endswith(
        "first missing"), output
    assert hashes[1][:9] not in section and "second lower-case trailer" not in section, output


def test_a_bot_is_a_bot_name_with_a_bot_noreply_address(_tmp):
    """A member, then one near miss per part of the rule. The predicate is
    called directly: a non-ASCII `--author` would test the host's argv
    encoding, not the rule."""
    for name, email, bot in (
            ("dependabot[bot]", "42+dependabot[bot]@users.noreply.GitHub.com", True),
            ("dependabot[bot] ci", "42+dependabot[bot]@users.noreply.github.com", False),
            ("dependabot[bot]", "42+dependabot[BOT]@users.noreply.github.com", False),
            ("dependabot[bot]", "42+[bot]@users.noreply.github.com", False),
            ("dependabot[bot]", "+dependabot[bot]@users.noreply.github.com", False),
            ("dependabot[bot]", "ci+dependabot[bot]@users.noreply.github.com", False),
            ("dependabot[bot]", "\u0664\u0662+dependabot[bot]@users.noreply.github.com", False),
            ("dependabot[bot]", "42+dependabot[bot]@users.noreply.g\u0131thub.com", False),
            ("dependabot[bot]", "42+dependabot[bot]@users.noreply.github.com.example", False)):
        assert _AS.is_bot_author(name, email) is bot, (name, email)


def test_main_exits_two_for_an_invalid_revision_range(tmp):
    d, _hashes = _repo(tmp)
    result, _output, error = _invoke(["not-a-revision", "-C", d])
    assert result == 2, result
    assert "git log failed" in error


def main():
    return _util.runner(_util.collect(globals()), tmp_prefix="authorstats_")


if __name__ == "__main__":
    raise SystemExit(main())
