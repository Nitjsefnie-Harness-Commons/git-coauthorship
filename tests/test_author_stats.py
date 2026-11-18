"""Behavior and command-line coverage for :mod:`author_stats`."""
import os

import _util

_AS = _util.load(os.path.join(_util.SCRIPTS, "author_stats.py"))
TESTER = ("Tester", "tester@example.com")
_ACTIONS = ("github-actions[bot]", "41898282+github-actions[bot]@users.noreply.github.com")
_DEPENDABOT = ("dependabot[bot]", "dependabot[bot]@users.noreply.github.com")


def _repo(tmp):
    """Commits with no, one lower-case and two trailers -> (repo, hashes)."""
    d = os.path.join(tmp, "repo")
    return d, _util.repo(d, ["first missing", "second lower-case trailer\n\nco-authored-by: Bot <bot@example.com>",
                             "third multi trailer\n\nCo-Authored-By: Bot <bot@example.com>\n"
                             "Co-Authored-By: Helper <helper@example.com>"], TESTER)


def _invoke(*args):
    return _util.call_main(_AS.main, ["author-stats", *args])


def test_tally_authors_counts_per_name_and_email(_tmp):
    """Keying on name alone would merge two distinct identities."""
    for pairs, want in (([], {}), ([("Alex", "a@work.com"), ("Alex", "a@home.com"), ("Alex", "a@work.com")],
                                   {"Alex <a@work.com>": 2, "Alex <a@home.com>": 1})):
        assert _AS.tally_authors([("h" * 40, *pair, "2026-08-03", "subj", "body") for pair in pairs]) == want, pairs


def test_records_carry_six_fields_from_real_git(tmp):
    """The author email must not shift date, subject, or trailer columns."""
    recs = _AS.git_log_records(_repo(tmp)[0], None)
    h, name, email, date, subject, trailers = recs[-1]
    assert (len(recs), len(h), name, email, date[:2], subject, trailers) == (
        3, 40, *TESTER, "20", "first missing", ""), recs


def test_main_report_and_exit_status_per_range(tmp):
    """Whole stdout and stderr: the one gap fails the summary, an empty range
    succeeds and a bad range is git's error, exit 2."""
    d, hashes = _repo(tmp)
    summary = ["3 commits, 2 co-authored, 1 missing", "", "By author (1 distinct; the git author field):",
               "      3  Tester <tester@example.com>", "", "By trailer (commits; a commit may carry more than one):",
               "      2  Bot <bot@example.com>", "      1  (none)", "      1  Helper <helper@example.com>"]
    for args, code, lines, error in (([], 1, summary, ""),
                                     ([f"{hashes[0]}..{hashes[0]}"], 0, ["No commits in range."], ""),
                                     (["not-a-revision"], 2, [], "ERROR: git log failed: ")):
        result, output, err = _invoke(*args, "-C", d)
        assert (result, output.splitlines(), err[:len(error)], bool(err)) == (code, lines, error, bool(error)), err


def test_bot_gaps_are_counted_and_marked_but_only_a_human_gap_fails(tmp):
    """A complete range exits 0. The id-prefixed and bare noreply forms exempt
    their gap and stay in every tally; a human gap still exits 1 beside bot
    gaps, a co-authored bot commit, a bot address under a name lacking `[bot]`
    and a trailer glued to body text, and `--missing` lists no co-authored
    commit."""
    d, hashes = _repo(tmp)
    result, output, error = _invoke(f"{hashes[0]}..HEAD", "-C", d)
    assert (result, error) == (0, "") and "2 commits, 2 co-authored, 0 missing" in output, output
    bots = [_util.commit(d, "bump", who) for who in (_ACTIONS, _DEPENDABOT)]
    result, output, _error = _invoke(f"{hashes[0]}..{bots[0]}", "-C", d)
    assert (result, output.splitlines()[0]) == (
        0, "3 commits, 2 co-authored, 1 missing (1 bot-authored, exempt)"), output
    result, output, error = _invoke(f"{hashes[0]}..HEAD", "--list", "--missing", "-C", d)
    assert (result, error, output.splitlines()[0]) == (
        0, "", "4 commits, 2 co-authored, 2 missing (2 bot-authored, exempt)"), output
    for line in [f"      1  {name} <{email}>" for name, email in (_ACTIONS, _DEPENDABOT)] + ["      2  (none)"]:
        assert line in output, output
    listed, missing = output.split("ALL COMMITS:", 1)[1].split("MISSING TRAILER:")
    assert "Bot <bot@example.com> (+1)" in listed, listed
    for h in bots:
        assert "MISSING" in next(x for x in listed.splitlines() if h[:9] in x), listed
        assert next(x for x in missing.splitlines() if h[:9] in x).endswith(
            "(bot-authored, exempt)"), missing
    _util.commit(d, "bump\n\nCo-Authored-By: Bot <bot@example.com>", _ACTIONS)
    _util.commit(d, "spoof", ("github-actions", _ACTIONS[1]))
    _util.commit(d, "glued\n\nbody text\nCo-Authored-By: Bot <bot@example.com>", TESTER)
    result, output, error = _invoke("--missing", "-C", d)
    assert (result, error, output.splitlines()[0]) == (
        1, "", "8 commits, 3 co-authored, 5 missing (2 bot-authored, exempt)"), output
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
            ("dependabot[bot]", "٤٢+dependabot[bot]@users.noreply.github.com", False),
            ("dependabot[bot]", "42+dependabot[bot]@users.noreply.gıthub.com", False),
            ("dependabot[bot]", "42+dependabot[bot]@users.noreply.github.com.example", False)):
        assert _AS.is_bot_author(name, email) is bot, (name, email)


def main():
    return _util.runner(_util.collect(globals()), tmp_prefix="authorstats_")


if __name__ == "__main__":
    raise SystemExit(main())
