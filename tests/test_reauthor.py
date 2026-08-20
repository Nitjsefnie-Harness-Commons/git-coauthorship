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
    os.chmod(fake, 0o755)
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


def main():
    return _util.runner(_util.collect(globals()), tmp_prefix="reauthor_")


if __name__ == "__main__":
    raise SystemExit(main())
