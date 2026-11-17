"""Suite for reauthor.py's identity rewrites — `--reauthor` (AUTHOR field)
and `--recommitter` (COMMITTER field, added 2026-08-03).

The two fields need separate passes because they diverge in exactly the case
that motivates the cleanup: a web-merged PR is authored by a person and
committed by `GitHub <noreply@github.com>`. Matching on the author identity
would never see that committer, so `--recommitter` matches on the committer's
own identity and leaves the author alone (and vice versa). These tests pin
that independence — a callback that wrote both fields would pass a
"committer changed" assertion while silently clobbering authorship.

git-filter-repo is optional here: absent, the end-to-end tests return early
as not-applicable rather than failing.
"""
import contextlib
import io
import os
import shutil
import subprocess
import sys

import _util

_RA = _util.load(os.path.join(_util.SCRIPTS, "reauthor.py"))


def _require_git():
    if not shutil.which("git"):
        _util.skip("git is not available on PATH")


def _require_filter_repo():
    _require_git()
    if not shutil.which("git-filter-repo"):
        _util.skip("git-filter-repo is not available on PATH")


def _repo(d, author, committer):
    """One-commit repo with the given ("Name", "mail") author/committer."""
    _require_git()
    os.makedirs(d, exist_ok=True)
    env = {**os.environ,
           "GIT_AUTHOR_NAME": author[0], "GIT_AUTHOR_EMAIL": author[1],
           "GIT_COMMITTER_NAME": committer[0], "GIT_COMMITTER_EMAIL": committer[1]}

    def run(*args):
        return subprocess.run(["git", "-C", d, *args], env=env,
                               capture_output=True, check=True)
    run("init", "-q")
    run("checkout", "-q", "-b", "main")
    run("config", "user.name", committer[0])
    run("config", "user.email", committer[1])
    run("config", "commit.gpgsign", "false")
    with open(os.path.join(d, "f.txt"), "w", encoding="utf-8") as f:
        f.write("x")
    run("add", "f.txt")
    run("commit", "-q", "-m", "subject")
    return d


def _commit(d, author, committer, text):
    env = {**os.environ,
           "GIT_AUTHOR_NAME": author[0], "GIT_AUTHOR_EMAIL": author[1],
           "GIT_COMMITTER_NAME": committer[0],
           "GIT_COMMITTER_EMAIL": committer[1]}
    with open(os.path.join(d, "f.txt"), "a", encoding="utf-8") as f:
        f.write(text)
    subprocess.run(["git", "-C", d, "add", "f.txt"], env=env, check=True)
    subprocess.run(["git", "-C", d, "commit", "-q", "-m", text],
                   env=env, check=True)


def _idents(d):
    """-> (author_name, author_email, committer_name, committer_email)."""
    out = subprocess.check_output(
        ["git", "-C", d, "log", "-1", "--format=%an%x00%ae%x00%cn%x00%ce"],
        timeout=60).decode().strip()
    return tuple(out.split("\x00"))


def _head(d):
    return subprocess.check_output(
        ["git", "-C", d, "rev-parse", "HEAD"], timeout=60
    ).decode().strip()


def _add_fake_signature(d):
    """Replace HEAD with an equivalent commit carrying a gpgsig header."""
    raw = subprocess.check_output(
        ["git", "-C", d, "cat-file", "commit", "HEAD"], timeout=60)
    marker = b"\n\n"
    header, separator, body = raw.partition(marker)
    assert separator, raw
    signed = (
        header + b"\ngpgsig -----BEGIN PGP SIGNATURE-----\n"
        b" fake-signature\n -----END PGP SIGNATURE-----" + marker + body
    )
    commit = subprocess.check_output(
        ["git", "-C", d, "hash-object", "-t", "commit", "-w", "--stdin"],
        input=signed, timeout=60).decode().strip()
    subprocess.run(
        ["git", "-C", d, "update-ref", "refs/heads/main", commit],
        check=True, timeout=60)
    return commit


def _reauthor(d, *args, env=None):
    r = subprocess.run(
        [sys.executable, os.path.join(_util.SCRIPTS, "reauthor.py"), *args],
        cwd=d, env=env, capture_output=True, text=True, check=False,
        timeout=300)
    assert r.returncode == 0, f"reauthor.py failed: {r.stdout}\n{r.stderr}"
    return r


def test_rewrite_identity_rejects_an_unknown_field(_tmp):
    """The field name is interpolated straight into generated callback source,
    so an unvetted value would be an injection point, not just a typo."""
    try:
        _RA.rewrite_identity("commiter", "A", "a@x", "B", "b@x")
    except ValueError as e:
        assert "author" in str(e) and "committer" in str(e), e
        return
    raise AssertionError("rewrite_identity accepted a bogus field name")


def test_zero_match_identity_rewrite_preserves_signed_history(tmp):
    d = _repo(os.path.join(tmp, "r"), ("Peter Z", "p@example.com"),
              ("Human", "human@example.com"))
    before = _add_fake_signature(d)

    result = _reauthor(d, "--reauthor", "Nobody", "nobody@example.invalid",
                       "New Name", "new@example.invalid")

    assert _head(d) == before, "a zero-match rewrite changed signed history"
    assert "0 matching commits; history left untouched" in result.stdout


def test_matching_identity_rewrite_reports_scope_and_signature_risk(tmp):
    _require_filter_repo()
    d = _repo(os.path.join(tmp, "r"), ("Peter Z", "p@example.com"),
              ("Human", "human@example.com"))
    _add_fake_signature(d)

    result = _reauthor(d, "--reauthor", "Peter Z", "p@example.com",
                       "Nitjsefnie", "z@example.com")

    assert "1 matching commit will be rewritten" in result.stdout
    assert "GPG signatures in the processed history will be stripped" in \
        result.stdout
    assert "signed commits and their descendants will receive new SHAs" in \
        result.stdout


def test_stop_at_preserves_a_signed_boundary_object(tmp):
    _require_filter_repo()
    old = ("Peter Z", "p@example.com")
    committer = ("Human", "human@example.com")
    d = _repo(os.path.join(tmp, "r"), old, committer)
    boundary = _add_fake_signature(d)
    boundary_bytes = subprocess.check_output(
        ["git", "-C", d, "cat-file", "commit", boundary], timeout=60)
    _commit(d, old, committer, "child")

    _reauthor(d, "--reauthor", *old, "Nitjsefnie", "z@example.com",
              "--stop-at", boundary)

    after = subprocess.check_output(
        ["git", "-C", d, "cat-file", "commit", boundary], timeout=60)
    assert after == boundary_bytes
    assert b"gpgsig " in after
    assert subprocess.check_output(
        ["git", "-C", d, "rev-parse", "HEAD^"], timeout=60
    ).decode().strip() == boundary
    assert _idents(d)[:2] == ("Nitjsefnie", "z@example.com")


def test_identity_rewrite_restores_current_branch_upstream(tmp):
    _require_filter_repo()
    d = _repo(os.path.join(tmp, "r"), ("Peter Z", "p@example.com"),
              ("Human", "human@example.com"))
    remote = os.path.join(tmp, "remote.git")
    push_remote = os.path.join(tmp, "push.git")
    subprocess.run(["git", "init", "--bare", "-q", remote], check=True)
    subprocess.run(["git", "init", "--bare", "-q", push_remote], check=True)
    subprocess.run(["git", "-C", d, "remote", "add", "origin", remote],
                   check=True)
    subprocess.run(["git", "-C", d, "push", "-q", "-u", "origin", "main"],
                   check=True)
    subprocess.run(
        ["git", "-C", d, "remote", "set-url", "--push", "origin",
         push_remote], check=True)
    extra_fetch = "+refs/pull/*:refs/remotes/origin/pull/*"
    subprocess.run(
        ["git", "-C", d, "config", "--add", "remote.origin.fetch",
         extra_fetch], check=True)
    fetch_before = subprocess.check_output(
        ["git", "-C", d, "config", "--get-all", "remote.origin.fetch"],
        timeout=60).decode().splitlines()

    _reauthor(d, "--reauthor", "Peter Z", "p@example.com",
              "Nitjsefnie", "z@example.com")

    remote_name = subprocess.check_output(
        ["git", "-C", d, "config", "--get", "branch.main.remote"],
        timeout=60).decode().strip()
    merge_ref = subprocess.check_output(
        ["git", "-C", d, "config", "--get", "branch.main.merge"],
        timeout=60).decode().strip()
    assert remote_name == "origin", remote_name
    assert merge_ref == "refs/heads/main", merge_ref
    assert subprocess.check_output(
        ["git", "-C", d, "remote", "get-url", "origin"], timeout=60
    ).decode().strip() == remote
    assert subprocess.check_output(
        ["git", "-C", d, "remote", "get-url", "--push", "origin"],
        timeout=60).decode().strip() == push_remote
    fetch_after = subprocess.check_output(
        ["git", "-C", d, "config", "--get-all", "remote.origin.fetch"],
        timeout=60).decode().splitlines()
    assert fetch_after == fetch_before
    push = subprocess.run(
        ["git", "-C", d, "push", "--force"], capture_output=True, text=True,
        check=False, timeout=60)
    assert push.returncode == 0, push.stderr
    assert subprocess.check_output(
        ["git", "-C", push_remote, "rev-parse", "refs/heads/main"],
        timeout=60).decode().strip() == _head(d)


def test_restore_does_not_copy_global_remote_values_into_local_config(tmp):
    _require_filter_repo()
    d = _repo(os.path.join(tmp, "r"), ("Peter Z", "p@example.com"),
              ("Human", "human@example.com"))
    local_url = os.path.join(tmp, "local.git")
    global_url = os.path.join(tmp, "global.git")
    subprocess.run(["git", "-C", d, "remote", "add", "origin", local_url],
                   check=True)
    global_config = os.path.join(tmp, "global.gitconfig")
    subprocess.run(
        ["git", "config", "--file", global_config, "--add",
         "remote.origin.url", global_url], check=True)
    env = {**os.environ, "GIT_CONFIG_GLOBAL": global_config,
           "GIT_CONFIG_NOSYSTEM": "1"}
    before = subprocess.check_output(
        ["git", "-C", d, "config", "--get-all", "remote.origin.url"],
        env=env, timeout=60).decode().splitlines()
    local_before = subprocess.check_output(
        ["git", "-C", d, "config", "--local", "--get-all",
         "remote.origin.url"], env=env, timeout=60).decode().splitlines()

    _reauthor(d, "--reauthor", "Peter Z", "p@example.com",
              "Nitjsefnie", "z@example.com", env=env)

    after = subprocess.check_output(
        ["git", "-C", d, "config", "--get-all", "remote.origin.url"],
        env=env, timeout=60).decode().splitlines()
    local_after = subprocess.check_output(
        ["git", "-C", d, "config", "--local", "--get-all",
         "remote.origin.url"], env=env, timeout=60).decode().splitlines()
    assert after == before
    assert local_after == local_before == [local_url]


def test_failed_filter_repo_still_restores_remote_and_upstream(tmp):
    _require_git()
    if os.name == "nt":
        _util.skip("the fake filter-repo wrapper uses a POSIX shell")
    d = _repo(os.path.join(tmp, "r"), ("Peter Z", "p@example.com"),
              ("Human", "human@example.com"))
    remote = os.path.join(tmp, "remote.git")
    subprocess.run(["git", "init", "--bare", "-q", remote], check=True)
    subprocess.run(["git", "-C", d, "remote", "add", "origin", remote],
                   check=True)
    subprocess.run(["git", "-C", d, "push", "-q", "-u", "origin", "main"],
                   check=True)
    wrappers = os.path.join(tmp, "bin")
    os.makedirs(wrappers)
    fake = os.path.join(wrappers, "git-filter-repo")
    with open(fake, "w", encoding="utf-8") as f:
        f.write(
            "#!/bin/sh\n"
            "if [ \"$1\" = \"--help\" ]; then exit 0; fi\n"
            "git config --remove-section remote.origin >/dev/null 2>&1 || true\n"
            "git config --remove-section branch.main >/dev/null 2>&1 || true\n"
            "exit 9\n")
    os.chmod(fake, 0o700)
    env = {**os.environ, "PATH": wrappers + os.pathsep + os.environ["PATH"]}

    result = subprocess.run(
        [sys.executable, os.path.join(_util.SCRIPTS, "reauthor.py"),
         "--reauthor", "Peter Z", "p@example.com", "Nitjsefnie",
         "z@example.com"], cwd=d, env=env, capture_output=True, text=True,
        check=False, timeout=60)

    assert result.returncode != 0
    assert "git-filter-repo failed" in result.stdout
    assert subprocess.check_output(
        ["git", "-C", d, "remote", "get-url", "origin"], timeout=60
    ).decode().strip() == remote
    assert subprocess.check_output(
        ["git", "-C", d, "config", "--get", "branch.main.remote"],
        timeout=60).decode().strip() == "origin"


def test_restore_configuration_failure_is_not_silenced(_tmp):
    old_origin = _RA.__dict__["_ORIGIN_CONFIG"]
    old_tracking = _RA.__dict__["_BRANCH_TRACKING"]
    real_run = _RA.subprocess.run
    try:
        _RA.__dict__["_ORIGIN_CONFIG"] = {}
        _RA.__dict__["_BRANCH_TRACKING"] = {
            "branch": "main", "remote": ["origin"], "merge": [],
            "pushRemote": []}

        def fail_unset(args, **kwargs):
            if args[:2] == ["git", "config"] and "--unset-all" in args:
                return subprocess.CompletedProcess(args, 9, b"", b"denied")
            return real_run(args, **kwargs)

        _RA.subprocess.run = fail_unset
        try:
            _RA.__dict__["_restore_origin"]()
        except subprocess.CalledProcessError:
            pass
        else:
            raise AssertionError("configuration restoration failure was ignored")
    finally:
        _RA.subprocess.run = real_run
        _RA.__dict__["_ORIGIN_CONFIG"] = old_origin
        _RA.__dict__["_BRANCH_TRACKING"] = old_tracking


def test_recommitter_rewrites_committer_and_leaves_author(tmp):
    _require_filter_repo()
    d = _repo(os.path.join(tmp, "r"), ("Peter Z", "p@example.com"),
              ("GitHub", "noreply@github.com"))
    _reauthor(d, "--recommitter", "GitHub", "noreply@github.com",
              "Nitjsefnie", "z@example.com")
    an, ae, cn, ce = _idents(d)
    assert (cn, ce) == ("Nitjsefnie", "z@example.com"), (cn, ce)
    assert (an, ae) == ("Peter Z", "p@example.com"), (an, ae)


def test_reauthor_rewrites_author_and_leaves_committer(tmp):
    _require_filter_repo()
    d = _repo(os.path.join(tmp, "r"), ("Peter Z", "p@example.com"),
              ("GitHub", "noreply@github.com"))
    _reauthor(d, "--reauthor", "Peter Z", "p@example.com",
              "Nitjsefnie", "z@example.com")
    an, ae, cn, ce = _idents(d)
    assert (an, ae) == ("Nitjsefnie", "z@example.com"), (an, ae)
    assert (cn, ce) == ("GitHub", "noreply@github.com"), (cn, ce)


def test_recommitter_matches_on_committer_not_author(tmp):
    """A committer identity that is NOT the author's must still be found —
    the case an author-keyed modifier would have missed entirely."""
    _require_filter_repo()
    d = _repo(os.path.join(tmp, "r"), ("Alex", "alex@example.com"),
              ("Bot", "bot@example.com"))
    # Old identity = the AUTHOR's: matches nothing in the committer field.
    _reauthor(d, "--recommitter", "Alex", "alex@example.com",
              "Nitjsefnie", "z@example.com")
    assert _idents(d)[2:] == ("Bot", "bot@example.com"), _idents(d)
    # Old identity = the COMMITTER's: hits.
    _reauthor(d, "--recommitter", "Bot", "bot@example.com",
              "Nitjsefnie", "z@example.com")
    assert _idents(d)[2:] == ("Nitjsefnie", "z@example.com"), _idents(d)


def test_both_passes_in_one_invocation_do_not_share_a_sidecar(tmp):
    """_tmp_path is only PID-unique, so author and committer configs must not
    collide on one filename — they run in the same process."""
    _require_filter_repo()
    d = _repo(os.path.join(tmp, "r"), ("Peter Z", "p@example.com"),
              ("GitHub", "noreply@github.com"))
    _reauthor(d,
              "--reauthor", "Peter Z", "p@example.com", "Nitjsefnie", "z@example.com",
              "--recommitter", "GitHub", "noreply@github.com", "Nitjsefnie", "z@example.com")
    assert _idents(d) == ("Nitjsefnie", "z@example.com",
                          "Nitjsefnie", "z@example.com"), _idents(d)


def test_older_inherit_pattern_is_configurable(_tmp):
    """The historical Opus boundary heuristic is policy, not a universal git
    fact. Callers must be able to choose which existing trailer propagates."""
    old = _RA.get_all_trailers
    kimi = "Co-Authored-By: Kimi K3 <noreply@kimi.com>"
    try:
        _RA.get_all_trailers = lambda: {
            "old-anchor": "Co-Authored-By: Human <h@example.com>",
            "gap": None,
            "new-anchor": kimi,
        }
        default = "Co-Authored-By: Default Agent <agent@example.com>"
        mapping = _RA.build_mapping(
            ["old-anchor", "gap", "new-anchor"], 0, None, default,
            inherit_pattern="Kimi",
        )
        assert mapping["gap"] == kimi

        disabled = _RA.build_mapping(
            ["old-anchor", "gap", "new-anchor"], 0, None, default,
            inherit_pattern=r"(?!)",
        )
        assert disabled["gap"] == default
    finally:
        _RA.get_all_trailers = old


def test_parse_when_accepts_epoch_and_iso8601_forms(_tmp):
    assert _RA.parse_when("1704067200.9") == 1704067200
    assert _RA.parse_when("2024-01-01T00:00:00Z") == 1704067200
    assert _RA.parse_when("2024-01-01T00:00:00") == 1704067200


TRAILER = "Co-Authored-By: Opus 5 <noreply@anthropic.com>"


def _branch_tip(d, ref):
    return subprocess.check_output(
        ["git", "-C", d, "rev-parse", ref], timeout=60).decode().strip()


def _rename_fixture(d):
    """A repo with `main` checked out and `other`, which is not its ancestor.

    Both branches carry a commit with the old trailer, so a rename has work to
    do on each and the difference between the two scopes is visible.
    """
    ident = ("Dev", "dev@example.com")
    _repo(d, ident, ident)
    subprocess.run(["git", "-C", d, "checkout", "-q", "-b", "other"],
                   check=True, timeout=60)
    _commit(d, ident, ident, f"other work\n\n{TRAILER}")
    subprocess.run(["git", "-C", d, "checkout", "-q", "main"],
                   check=True, timeout=60)
    _commit(d, ident, ident, f"main work\n\n{TRAILER}")
    return d


def test_rename_leaves_branches_it_was_not_pointed_at_alone(tmp):
    """The reported defect: `--rename` rewrote every ref in the repository.

    A branch that is not an ancestor of HEAD and was never named came back
    with a new SHA and no reflog, which in a checkout shared through
    `git worktree` moves every other session's HEAD out from under it.
    """
    _require_filter_repo()
    d = _rename_fixture(os.path.join(tmp, "scoped"))
    before_other = _branch_tip(d, "other")
    before_main = _branch_tip(d, "main")
    _reauthor(d, "--rename", "Opus 5", "noreply@anthropic.com",
              "Claude Opus 5", "noreply@anthropic.com")
    assert _branch_tip(d, "main") != before_main, "the current branch was not rewritten"
    assert _branch_tip(d, "other") == before_other, "an unnamed branch was rewritten"
    body = subprocess.check_output(
        ["git", "-C", d, "log", "-1", "--format=%B"], timeout=60).decode()
    assert "Claude Opus 5" in body, body


def test_rename_all_refs_is_available_explicitly(tmp):
    """The old behaviour stays reachable, by asking for it."""
    _require_filter_repo()
    d = _rename_fixture(os.path.join(tmp, "allrefs"))
    before_other = _branch_tip(d, "other")
    _reauthor(d, "--all-refs", "--rename", "Opus 5", "noreply@anthropic.com",
              "Claude Opus 5", "noreply@anthropic.com")
    assert _branch_tip(d, "other") != before_other, "--all-refs left a ref alone"


def test_rename_states_its_scope_and_dry_run_changes_nothing(tmp):
    """No commit count, no ref list and no dry run was the other half of it."""
    _require_filter_repo()
    d = _rename_fixture(os.path.join(tmp, "dry"))
    before = _branch_tip(d, "main")
    out = _reauthor(d, "--dry-run", "--rename", "Opus 5",
                    "noreply@anthropic.com", "Claude Opus 5",
                    "noreply@anthropic.com").stdout
    assert "refs/heads/main" in out, out
    assert "1 commit(s) carry the old trailer" in out, out
    assert "--all-refs" in out, out
    assert _branch_tip(d, "main") == before, "--dry-run rewrote history"


def test_rename_with_no_matches_does_not_rewrite(tmp):
    """A rename that matches nothing still renamed every SHA under it."""
    _require_filter_repo()
    d = _rename_fixture(os.path.join(tmp, "nomatch"))
    before = _branch_tip(d, "main")
    out = _reauthor(d, "--rename", "Nobody", "nobody@example.com",
                    "Somebody", "somebody@example.com").stdout
    assert "Nothing to rename" in out, out
    assert _branch_tip(d, "main") == before, "a no-op rename rewrote history"


@contextlib.contextmanager
def _in(directory):
    """rename_scope reads the repository it is standing in."""
    previous = os.getcwd()
    os.chdir(directory)
    try:
        yield
    finally:
        os.chdir(previous)


def test_rename_scope_counts_and_names_only_the_current_branch(tmp):
    _require_git()
    d = _rename_fixture(os.path.join(tmp, "scope"))
    with _in(d):
        matches, refs = _RA.rename_scope("Opus 5", "noreply@anthropic.com", False)
    assert matches == 1, matches
    assert refs == ["refs/heads/main"], refs


def test_rename_scope_over_all_refs_sees_every_branch(tmp):
    _require_git()
    d = _rename_fixture(os.path.join(tmp, "scopeall"))
    with _in(d):
        matches, refs = _RA.rename_scope("Opus 5", "noreply@anthropic.com", True)
    assert matches == 2, matches
    assert sorted(refs) == ["refs/heads/main", "refs/heads/other"], refs


def test_rename_scope_honours_an_explicit_commit_set(tmp):
    """--stop-at and the date window both arrive as a commit-hash scope."""
    _require_git()
    d = _rename_fixture(os.path.join(tmp, "scopeset"))
    with _in(d):
        assert _RA.rename_scope(
            "Opus 5", "noreply@anthropic.com", False, scope=set())[0] == 0
        head = _head(d)
        assert _RA.rename_scope(
            "Opus 5", "noreply@anthropic.com", False, scope={head})[0] == 1


def test_rename_scope_outside_a_repository_reports_nothing(tmp):
    plain = os.path.join(tmp, "plain")
    os.makedirs(plain, exist_ok=True)
    with _in(plain):
        assert _RA.rename_scope("A", "a@x", False) == (None, [])


OLD = ("Peter Z", "p@example.com")
NEW = ("Nitjsefnie", "z@example.com")
_MODES = {
    "--reauthor": ["--reauthor", *OLD, *NEW],
    "--recommitter": ["--recommitter", *OLD, *NEW],
    "--older": ["--older", *NEW],
    "--all": ["--all", *NEW],
    "--rename": ["--rename", "Opus 5", "noreply@anthropic.com",
                 "Claude Opus 5", "noreply@anthropic.com"],
}


def _git(d, *args):
    return subprocess.run(["git", "-C", d, *args], capture_output=True,
                          text=True, check=False, timeout=60)


def _shared_checkout(d):
    """The reported repro: `main` checked out, `other` sharing its first
    commit and checked out in a second `git worktree`, and an `origin/main`
    remote-tracking ref. Every commit is OLD's as author and committer.
    The shared base and `other`'s own commit carry no trailer and `main`'s
    own commit carries TRAILER, so every mode has work on `main` and the
    identity modes have work on `other` too.
    """
    repo = _repo(os.path.join(d, "repo"), OLD, OLD)
    base = _head(repo)
    assert _git(repo, "branch", "other").returncode == 0
    worktree = os.path.join(d, "wt")
    assert _git(repo, "worktree", "add", "-q", worktree,
                "other").returncode == 0
    _commit(worktree, OLD, OLD, "other work")
    _commit(repo, OLD, OLD, f"main work\n\n{TRAILER}")
    assert _git(repo, "update-ref", "refs/remotes/origin/main",
                base).returncode == 0
    return repo


def _reflog(d, ref):
    return _git(d, "reflog", "show", "--format=%H", ref).stdout.split()


def _scope_line(out, mode):
    """The `[<mode>] scope: ...` line, or '' when none was printed."""
    tag = f"[{mode[2:]}] scope: "
    return next((line for line in out.splitlines()
                 if line.startswith(tag)), "")


def _rewrites_only_the_current_branch(tmp, mode):
    _require_filter_repo()
    d = _shared_checkout(tmp)
    before = {ref: _branch_tip(d, ref)
              for ref in ("main", "other", "refs/remotes/origin/main")}
    reflog = _reflog(d, "other")
    out = _reauthor(d, *_MODES[mode]).stdout
    # Collected, not asserted one by one: a reverted --refs breaks every
    # limb at once, and the report should show each of them.
    problems = []
    if _branch_tip(d, "main") == before["main"]:
        problems.append("the current branch was not rewritten")
    if _branch_tip(d, "other") != before["other"]:
        problems.append("the other worktree's branch moved")
    origin = _git(d, "rev-parse", "--verify", "-q",
                  "refs/remotes/origin/main").stdout.strip()
    if origin != before["refs/remotes/origin/main"]:
        problems.append(f"origin/main resolves to {origin!r}")
    if _git(d, "cat-file", "-e", before["main"]).returncode != 0:
        problems.append("the current branch's old tip was pruned")
    if not reflog or _reflog(d, "other") != reflog:
        problems.append("the other branch's reflog changed")
    scope = _scope_line(out, mode)
    if "the current branch" not in scope or \
            not scope.endswith("refs/heads/main"):
        problems.append(f"scope line {scope!r}")
    if f"[{mode[2:]}] Other refs are untouched. Pass --all-refs to " \
            "rewrite the whole repository." not in out.splitlines():
        problems.append("no note that other refs are untouched")
    assert not problems, f"{mode}: {problems}\n{out}"


def test_reauthor_rewrites_only_the_current_branch(tmp):
    _rewrites_only_the_current_branch(tmp, "--reauthor")


def test_recommitter_rewrites_only_the_current_branch(tmp):
    _rewrites_only_the_current_branch(tmp, "--recommitter")


def test_older_rewrites_only_the_current_branch(tmp):
    _rewrites_only_the_current_branch(tmp, "--older")


def test_all_rewrites_only_the_current_branch(tmp):
    _rewrites_only_the_current_branch(tmp, "--all")


def _branches_left_behind(d):
    """The other reported repro: `main` with two commits and `other` made
    there, checked out nowhere, then `feature` checked out with a third. The
    branches left behind point at an ancestor of the current branch, so the
    pass rewrites their tip commit too. No commit carries a trailer."""
    repo = _repo(d, OLD, OLD)
    _commit(repo, OLD, OLD, "one")
    assert _git(repo, "branch", "other").returncode == 0
    assert _git(repo, "checkout", "-q", "-b", "feature").returncode == 0
    _commit(repo, OLD, OLD, "feat")
    return repo


def _main_in(d, *args):
    """Run reauthor's main() in `d` -> (its stdout, how much of it was
    printed when the first filter-repo pass started, or -1 if none did)."""
    stdout = io.StringIO()
    started = []
    real = _RA.__dict__["_run_filter_repo"]

    def recording(command):
        started.append(len(stdout.getvalue()))
        return real(command)

    old_argv = sys.argv
    _RA.__dict__["_run_filter_repo"] = recording
    try:
        sys.argv = ["reauthor", *args]
        with _in(d), contextlib.redirect_stdout(stdout):
            _RA.main()
    except SystemExit as exc:
        raise AssertionError(
            f"reauthor exited {exc.code}\n{stdout.getvalue()}") from exc
    finally:
        sys.argv = old_argv
        _RA.__dict__["_run_filter_repo"] = real
    return stdout.getvalue(), (started or [-1])[0]


def _leaves_ancestor_branches_alone(tmp, mode):
    _require_filter_repo()
    d = _branches_left_behind(os.path.join(tmp, "r"))
    before = {ref: _branch_tip(d, ref) for ref in ("main", "other", "feature")}
    reflogs = {ref: _reflog(d, ref) for ref in ("main", "other")}
    out, started = _main_in(d, *_MODES[mode])
    problems = []
    if _branch_tip(d, "feature") == before["feature"]:
        problems.append("the current branch was not rewritten")
    for ref in ("main", "other"):
        if _branch_tip(d, ref) != before[ref]:
            problems.append(f"{ref} moved")
        if not reflogs[ref] or _reflog(d, ref) != reflogs[ref]:
            problems.append(f"{ref}'s reflog changed")
    for ref in ("other", "feature"):
        if _git(d, "cat-file", "-e", before[ref]).returncode != 0:
            problems.append(f"{ref}'s old tip was pruned")
    scope = _scope_line(out, mode)
    if scope != f"[{mode[2:]}] scope: the current branch — refs/heads/feature":
        problems.append(f"scope line {scope!r}")
    if not scope or not 0 <= out.find(scope) < started:
        problems.append(f"scope at {out.find(scope)}, filter-repo at {started}")
    assert not problems, f"{mode}: {problems}\n{out}"


def test_older_leaves_ancestor_branches_alone(tmp):
    _leaves_ancestor_branches_alone(tmp, "--older")


def test_all_leaves_ancestor_branches_alone(tmp):
    _leaves_ancestor_branches_alone(tmp, "--all")


def _all_refs_rewrites_the_other_branch(tmp, mode):
    _require_filter_repo()
    d = _shared_checkout(tmp)
    before_main = _branch_tip(d, "main")
    before_other = _branch_tip(d, "other")
    out = _reauthor(d, "--all-refs", *_MODES[mode]).stdout
    assert _branch_tip(d, "main") != before_main, f"{mode}: main kept\n{out}"
    assert _branch_tip(d, "other") != before_other, \
        f"{mode}: --all-refs left the other branch alone\n{out}"
    assert "every ref in the repository" in _scope_line(out, mode), out
    assert "Other refs are untouched" not in out, out
    # What each mode does to a commit that is only on the other branch.
    other_tip = _git(d, "log", "-1", "--format=%B", "other").stdout
    if mode == "--all":
        assert f"Co-Authored-By: {NEW[0]} <{NEW[1]}>" in other_tip, other_tip
    if mode == "--older":
        assert "Co-Authored-By" not in other_tip, \
            f"--older filled a gap off the current branch\n{other_tip}"


def test_reauthor_all_refs_rewrites_the_other_branch(tmp):
    _all_refs_rewrites_the_other_branch(tmp, "--reauthor")


def test_recommitter_all_refs_rewrites_the_other_branch(tmp):
    _all_refs_rewrites_the_other_branch(tmp, "--recommitter")


def test_older_all_refs_rewrites_the_other_branch(tmp):
    _all_refs_rewrites_the_other_branch(tmp, "--older")


def test_all_all_refs_rewrites_the_other_branch(tmp):
    _all_refs_rewrites_the_other_branch(tmp, "--all")


def _stop_at_problems(out, mode, stop):
    """What is wrong with `mode`'s scope report under --stop-at, if anything:
    it names the current branch alone, the <stop>..HEAD bound and that
    --all-refs does nothing there."""
    scope = _scope_line(out, mode)
    problems = [f"{mode}: scope line {scope!r}"
                for part in ("the current branch", "refs/heads/main",
                             f"bounded to {stop[:7]}..HEAD",
                             "(--all-refs has no effect with --stop-at)")
                if part not in scope]
    if "refs/heads/other" in scope or "every ref" in scope:
        problems.append(f"{mode}: scope line names other refs: {scope!r}")
    if f"[{mode[2:]}] Other refs are untouched." not in out.splitlines():
        problems.append(f"{mode}: no plain untouched-refs note")
    return problems


def _stop_at_keeps_all_refs_on_the_current_branch(tmp, mode):
    """--stop-at's <stop>..HEAD bound wins over --all-refs, so a branch
    holding the rewritten commit plus its own is left alone, and the
    printed scope says so."""
    _require_filter_repo()
    d = _repo(os.path.join(tmp, "r"), OLD, OLD)
    stop = _head(d)
    _commit(d, OLD, OLD, f"main work\n\n{TRAILER}")
    assert _git(d, "checkout", "-q", "-b", "other").returncode == 0
    _commit(d, OLD, OLD, "other work")
    assert _git(d, "checkout", "-q", "main").returncode == 0
    before_main, before_other = _branch_tip(d, "main"), _branch_tip(d, "other")
    out = _reauthor(d, "--all-refs", "--stop-at", stop, *_MODES[mode]).stdout
    assert _branch_tip(d, "main") != before_main, f"{mode}: main kept\n{out}"
    assert _branch_tip(d, "other") == before_other, \
        f"{mode}: --stop-at did not bound it\n{out}"
    problems = _stop_at_problems(out, mode, stop)
    assert not problems, f"{problems}\n{out}"


def test_stop_at_keeps_reauthor_all_refs_on_the_current_branch(tmp):
    _stop_at_keeps_all_refs_on_the_current_branch(tmp, "--reauthor")


def test_stop_at_keeps_rename_all_refs_on_the_current_branch(tmp):
    _stop_at_keeps_all_refs_on_the_current_branch(tmp, "--rename")


def _fake_filter_repo(tmp):
    """-> (env, argv file): a git-filter-repo that records its arguments,
    prints a marker to stderr and fails, so nothing is rewritten and the
    marker shows where in reauthor's output the rewrite was attempted."""
    if os.name == "nt":
        _util.skip("the fake filter-repo wrapper uses a POSIX shell")
    wrappers = os.path.join(tmp, "bin")
    os.makedirs(wrappers)
    argv = os.path.join(tmp, "argv")
    fake = os.path.join(wrappers, "git-filter-repo")
    with open(fake, "w", encoding="utf-8") as f:
        f.write("#!/bin/sh\n"
                "if [ \"$1\" = \"--help\" ]; then exit 0; fi\n"
                f"printf '%s\\n' \"$@\" > '{argv}'\n"
                "echo FILTER-REPO-RAN >&2\n"
                "exit 9\n")
    os.chmod(fake, 0o700)
    return {**os.environ, "PATH": wrappers + os.pathsep + os.environ["PATH"]}, argv


def _run_with(env, d, *args):
    return subprocess.run(
        [sys.executable, os.path.join(_util.SCRIPTS, "reauthor.py"), *args],
        cwd=d, env=env, capture_output=True, text=True, check=False,
        timeout=60).stdout


def _argv(path):
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines()
    os.remove(path)
    return lines


def test_each_mode_states_its_scope_before_filter_repo_runs(tmp):
    _require_git()
    env, argv = _fake_filter_repo(tmp)
    problems = []
    for mode, args in _MODES.items():
        d = _shared_checkout(os.path.join(tmp, mode[2:]))
        out = _run_with(env, d, *args)
        scope = out.find(_scope_line(out, mode) or "\0")
        ran = out.find("FILTER-REPO-RAN")
        if not 0 <= scope < ran:
            problems.append(f"{mode}: scope at {scope}, rewrite at {ran}")
        command = _argv(argv) or []
        if "--refs" not in command or \
                command[command.index("--refs") + 1] != "HEAD":
            problems.append(f"{mode}: filter-repo ran without --refs HEAD")
        if f"[{mode[2:]}] Other refs are untouched. Pass --all-refs to " \
                "rewrite the whole repository." not in out.splitlines():
            problems.append(f"{mode}: no note that other refs are untouched")
    assert not problems, problems


def test_each_mode_states_the_stop_at_bound_in_its_scope(tmp):
    _require_git()
    env, argv = _fake_filter_repo(tmp)
    problems = []
    for mode, args in _MODES.items():
        d = _shared_checkout(os.path.join(tmp, mode[2:]))
        stop = _git(d, "rev-parse", "HEAD~1").stdout.strip()
        # A trailerless commit after the stop, so --older has a gap to fill.
        _commit(d, OLD, OLD, "late work")
        problems += _stop_at_problems(
            _run_with(env, d, "--all-refs", "--stop-at", stop, *args),
            mode, stop)
        command = _argv(argv) or []
        if "--refs" not in command or \
                command[command.index("--refs") + 1] != f"{stop}..HEAD":
            problems.append(f"{mode}: filter-repo ran without --refs "
                            f"<stop>..HEAD: {command}")
    assert not problems, problems


def test_all_refs_with_recent_and_a_mode_it_widens_is_accepted(tmp):
    """The near miss of the --recent-only refusal: --recent beside a mode
    --all-refs widens is a valid request."""
    _require_git()
    env, argv = _fake_filter_repo(tmp)
    d = _shared_checkout(tmp)
    out = _run_with(env, d, "--all-refs", "--recent", "1", *NEW,
                    *_MODES["--older"])
    assert "ERROR: --all-refs" not in out, out
    command = _argv(argv)
    assert command is not None and "--refs" not in command, f"{command}\n{out}"


def test_all_refs_lifts_the_refs_limit_for_each_mode(tmp):
    _require_git()
    env, argv = _fake_filter_repo(tmp)
    problems = []
    for mode, args in _MODES.items():
        d = _shared_checkout(os.path.join(tmp, mode[2:]))
        out = _run_with(env, d, "--all-refs", *args)
        command = _argv(argv)
        if command is None:
            problems.append(f"{mode}: filter-repo never ran\n{out}")
        elif "--refs" in command:
            problems.append(f"{mode}: --all-refs still passed --refs")
        if "every ref in the repository" not in _scope_line(out, mode):
            problems.append(f"{mode}: scope line {_scope_line(out, mode)!r}")
    assert not problems, problems


def _old_identity_only_on_other(d):
    """`main` holds nothing of OLD's; `other` holds one commit by OLD."""
    ident = ("Dev", "dev@example.com")
    _repo(d, ident, ident)
    assert _git(d, "checkout", "-q", "-b", "other").returncode == 0
    _commit(d, OLD, OLD, "other work")
    assert _git(d, "checkout", "-q", "main").returncode == 0
    return d


def test_identity_preflight_counts_only_the_current_branch(tmp):
    """The preflight count and its zero-match skip describe the pass that
    runs, so they walk what that pass rewrites."""
    _require_git()
    d = _old_identity_only_on_other(tmp)
    other = _branch_tip(d, "other")
    with _in(d):
        for field in ("author", "committer"):
            assert _RA.matching_identity_commits(field, *OLD) == [], field
            assert _RA.matching_identity_commits(
                field, *OLD, all_refs=True) == [other], field


def test_identity_count_follows_all_refs_at_the_call_site(tmp):
    _require_git()
    env, argv = _fake_filter_repo(tmp)
    d = _old_identity_only_on_other(os.path.join(tmp, "r"))
    for mode in ("--reauthor", "--recommitter"):
        out = _run_with(env, d, *_MODES[mode])
        assert "0 matching commits; history left untouched" in out, out
        assert _argv(argv) is None, f"{mode}: filter-repo ran on no match"
        out = _run_with(env, d, "--all-refs", *_MODES[mode])
        assert "1 matching commit will be rewritten" in out, out
        assert _argv(argv) is not None, f"{mode}: --all-refs skipped"


def _refused(d, *args):
    before = _head(d)
    r = subprocess.run(
        [sys.executable, os.path.join(_util.SCRIPTS, "reauthor.py"), *args],
        cwd=d, capture_output=True, text=True, check=False, timeout=60)
    assert r.returncode == 1, f"{args}: exit {r.returncode}\n{r.stdout}"
    assert _head(d) == before, f"{args} rewrote HEAD"
    return r.stdout.splitlines()


def test_all_refs_with_only_recent_is_refused(tmp):
    """--recent rebases HEAD only, so --all-refs would widen nothing."""
    _require_git()
    d = _repo(os.path.join(tmp, "r"), OLD, OLD)
    _commit(d, OLD, OLD, "second")
    out = _refused(d, "--all-refs", "--recent", "1", *NEW)
    assert "ERROR: --all-refs widens --older, --all, --rename, --reauthor "\
        "and --recommitter; none of them was given." in out, out


def test_dry_run_still_scopes_rename_only(tmp):
    _require_git()
    d = _repo(os.path.join(tmp, "r"), OLD, OLD)
    out = _refused(d, "--dry-run", *_MODES["--reauthor"])
    assert "ERROR: --dry-run currently scopes --rename only." in out, out


def main():
    return _util.runner(_util.collect(globals()), tmp_prefix="reauthor_")


if __name__ == "__main__":
    raise SystemExit(main())
