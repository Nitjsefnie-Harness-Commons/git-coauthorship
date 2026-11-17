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
    return tuple(_out(d, "log", "-1", "--format=%an%x00%ae%x00%cn%x00%ce").split("\x00"))


def _out(d, *args, env=None):
    """git's stripped stdout in `d`; raises when git fails."""
    return subprocess.check_output(["git", "-C", d, *args], env=env,
                                   timeout=60).decode().strip()


def _head(d):
    return _out(d, "rev-parse", "HEAD")


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
    assert _out(d, "rev-parse", "HEAD^") == boundary
    assert _idents(d)[:2] == ("Nitjsefnie", "z@example.com")


def test_identity_rewrite_restores_current_branch_upstream(tmp):
    _require_filter_repo()
    d = _repo(os.path.join(tmp, "r"), ("Peter Z", "p@example.com"),
              ("Human", "human@example.com"))
    remote = os.path.join(tmp, "remote.git")
    push_remote = os.path.join(tmp, "push.git")
    subprocess.run(["git", "init", "--bare", "-q", remote], check=True)
    subprocess.run(["git", "init", "--bare", "-q", push_remote], check=True)
    _out(d, "remote", "add", "origin", remote)
    _out(d, "push", "-q", "-u", "origin", "main")
    _out(d, "remote", "set-url", "--push", "origin", push_remote)
    extra_fetch = "+refs/pull/*:refs/remotes/origin/pull/*"
    _out(d, "config", "--add", "remote.origin.fetch", extra_fetch)
    fetch_before = _out(d, "config", "--get-all", "remote.origin.fetch").splitlines()

    _reauthor(d, "--reauthor", "Peter Z", "p@example.com",
              "Nitjsefnie", "z@example.com")

    remote_name = _out(d, "config", "--get", "branch.main.remote")
    merge_ref = _out(d, "config", "--get", "branch.main.merge")
    assert remote_name == "origin", remote_name
    assert merge_ref == "refs/heads/main", merge_ref
    assert _out(d, "remote", "get-url", "origin") == remote
    assert _out(d, "remote", "get-url", "--push", "origin") == push_remote
    fetch_after = _out(d, "config", "--get-all", "remote.origin.fetch").splitlines()
    assert fetch_after == fetch_before
    push = subprocess.run(
        ["git", "-C", d, "push", "--force"], capture_output=True, text=True,
        check=False, timeout=60)
    assert push.returncode == 0, push.stderr
    assert _out(push_remote, "rev-parse", "refs/heads/main") == _head(d)


def test_restore_does_not_copy_global_remote_values_into_local_config(tmp):
    _require_filter_repo()
    d = _repo(os.path.join(tmp, "r"), ("Peter Z", "p@example.com"),
              ("Human", "human@example.com"))
    local_url = os.path.join(tmp, "local.git")
    global_url = os.path.join(tmp, "global.git")
    _out(d, "remote", "add", "origin", local_url)
    global_config = os.path.join(tmp, "global.gitconfig")
    subprocess.run(
        ["git", "config", "--file", global_config, "--add",
         "remote.origin.url", global_url], check=True)
    env = {**os.environ, "GIT_CONFIG_GLOBAL": global_config,
           "GIT_CONFIG_NOSYSTEM": "1"}
    urls = ("config", "--get-all", "remote.origin.url")
    before = _out(d, *urls, env=env).splitlines()
    local_before = _out(d, "config", "--local", *urls[1:], env=env).splitlines()

    _reauthor(d, "--reauthor", "Peter Z", "p@example.com",
              "Nitjsefnie", "z@example.com", env=env)

    after = _out(d, *urls, env=env).splitlines()
    local_after = _out(d, "config", "--local", *urls[1:], env=env).splitlines()
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
    _out(d, "remote", "add", "origin", remote)
    _out(d, "push", "-q", "-u", "origin", "main")
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
    assert _out(d, "remote", "get-url", "origin") == remote
    assert _out(d, "config", "--get", "branch.main.remote") == "origin"


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
OLD, NEW = ("Peter Z", "p@example.com"), ("Nitjsefnie", "z@example.com")
# mode -> (argv, text its rewrite leaves in `git log --format=_LOG`, the
# count line it prints, that count when scoped, widened and --stop-at bound).
_LOG = "%B%an <%ae>|%cn <%ce>"
_MODES = {
    "--reauthor": (["--reauthor", *OLD, *NEW], f"{NEW[0]} <{NEW[1]}>|{OLD[0]}",
                   "--reauthor: {} matching", (3, 5, 2)),
    "--recommitter": (["--recommitter", *OLD, *NEW], f"{OLD[1]}>|{NEW[0]}",
                      "--recommitter: {} matching", (3, 5, 2)),
    "--older": (["--older", *NEW], f"By: {NEW[0]}", "Filling {} commits", (2, 2, 1)),
    "--all": (["--all", *NEW], f"By: {NEW[0]}", "", ()),
    "--rename": (["--rename", "Opus 5", "noreply@anthropic.com", "Claude Opus 5",
                  "noreply@anthropic.com"], "By: Claude Opus 5",
                 "[rename] {} commit(s) carry", (1, 2, 1)),
}
_REFS = ("feature", "main", "other", "refs/remotes/origin/main")


def _git(d, *args):
    return subprocess.run(["git", "-C", d, *args], capture_output=True,
                          text=True, check=False, timeout=60)


def _tip(d, ref):
    return _git(d, "rev-parse", "-q", "--verify", ref).stdout.strip()


@contextlib.contextmanager
def _in(directory):
    """rename_scope reads the repository it is standing in."""
    previous = os.getcwd()
    os.chdir(directory)
    try:
        yield
    finally:
        os.chdir(previous)


def _fixture(d):
    """The reported shape: `feature` checked out, `main` and `origin/main`
    left behind at the base commit, which every mode but --rename rewrites,
    and `other` checked out in a second worktree. `feature` and `other` each
    add a TRAILER commit and then a gap, so every mode has work on both;
    every commit is OLD's as author and committer."""
    repo, worktree = _repo(os.path.join(d, "repo"), OLD, OLD), os.path.join(d, "wt")
    for args in (["worktree", "add", "-q", "-b", "other", worktree],
                 ["checkout", "-q", "-b", "feature"],
                 ["update-ref", "refs/remotes/origin/main", "main"]):
        assert _git(repo, *args).returncode == 0, args
    for where, branch in ((repo, "feature"), (worktree, "other")):
        _commit(where, OLD, OLD, f"{branch} work\n\n{TRAILER}")
        _commit(where, OLD, OLD, f"{branch} gap")
    return repo


def _main_in(d, *args):
    """Run reauthor's main() in `d` -> (exit status, stdout, [(how much
    stdout was printed when it started, argv)] per filter-repo pass). With
    no git-filter-repo a stub answers its --help probe and passes are only
    recorded."""
    found, stub = shutil.which("git-filter-repo"), os.path.join(d, os.pardir, "bin")
    if not found:
        if os.name == "nt":
            _util.skip("the git-filter-repo stub is a POSIX shell script")
        os.makedirs(stub, exist_ok=True)
        with open(os.path.join(stub, "git-filter-repo"), "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(f.name, 0o700)
    stdout, runs, code = io.StringIO(), [], 0
    real, saved = _RA.__dict__["_run_filter_repo"], (sys.argv, os.environ["PATH"])

    def recording(command):
        runs.append((len(stdout.getvalue()), command))
        return real(command) if found else None

    _RA.__dict__["_run_filter_repo"] = recording
    sys.argv, os.environ["PATH"] = ["reauthor", *args], stub + os.pathsep + saved[1]
    try:
        with _in(d), contextlib.redirect_stdout(stdout):
            _RA.main()
    except SystemExit as exc:
        code = exc.code
    finally:
        sys.argv, os.environ["PATH"] = saved
        _RA.__dict__["_run_filter_repo"] = real
    return code, stdout.getvalue(), runs


def test_each_mode_rewrites_only_the_scope_it_prints(tmp):
    """Each mode alone, with --all-refs, and with --stop-at beside --all-refs
    (the bound wins), plus --all-refs beside --recent, the near miss of its
    refusal. Every case checks the scope, note and count printed and
    filter-repo's --refs; with git-filter-repo also the refs that moved, the
    other refs' reflogs and old objects, and what the rewrite wrote."""
    cases = [(mode, extra) for mode in _MODES for extra in
             ([], ["--all-refs"], ["--all-refs", "--stop-at", "main"])]
    problems = []
    for i, (mode, extra) in enumerate(cases + [("--all", ["--all-refs", "--recent", "1", *NEW])]):
        d = _fixture(os.path.join(tmp, str(i)))
        before = {ref: _tip(d, ref) for ref in _REFS}
        reflogs = [_git(d, "reflog", "--format=%H", ref).stdout for ref in ("main", "other")]
        code, out, runs = _main_in(d, *extra, *_MODES[mode][0])
        kind, tag, stop = 2 if "--stop-at" in extra else int("--all-refs" in extra), f"[{mode[2:]}]", before["main"]
        here = f"{tag} scope: the current branch — refs/heads/feature"
        scope, note, bound = [
            (here, f"{tag} Other refs are untouched. Pass --all-refs to rewrite the whole repository.", "HEAD"),
            (f"{tag} scope: every ref in the repository — refs/heads/feature, refs/heads/main, "
             "refs/heads/other, refs/remotes/origin/main", None, None),
            (f"{here}, bounded to {stop[:7]}..HEAD (--all-refs has no effect with --stop-at)",
             f"{tag} Other refs are untouched.", f"{stop}..HEAD")][kind]
        line = next((x for x in out.splitlines() if x.startswith(f"{tag} scope: ")), "")
        argv, (wrote, counted, counts) = runs[0][1] if runs else [], _MODES[mode][1:]
        checks = {
            "exit status": code == 0, "scope line": line == scope,
            "note": note in out.splitlines() if note else "Other refs are untouched" not in out,
            "scope before the pass": bool(runs) and 0 <= out.find(line) < runs[0][0],
            "--refs": (argv[argv.index("--refs") + 1] if "--refs" in argv else None) == bound,
            "count": not counts or counted.format(counts[kind]) in out}
        if shutil.which("git-filter-repo"):
            moved = {ref for ref in _REFS if _tip(d, ref) != before[ref]}
            other = _git(d, "log", "-1", "--format=%B", "other").stdout
            checks.update({
                "refs moved": {"feature", "other"} <= moved if kind == 1 else moved == {"feature"},
                "reflogs": kind == 1 or all(reflogs) and reflogs == [
                    _git(d, "reflog", "--format=%H", ref).stdout for ref in ("main", "other")],
                "old objects": kind == 1 or all(
                    _git(d, "cat-file", "-e", before[ref]).returncode == 0 for ref in ("feature", "main")),
                "rewrite": kind == 2 or wrote in _git(d, "log", f"--format={_LOG}").stdout,
                "other's own commit": kind != 1 or mode not in ("--all", "--older")
                or ("By: " in other) == (mode == "--all")})
        failed = [name for name, ok in checks.items() if not ok]
        if failed:
            problems.append(f"{mode} {extra}: {failed}\n{out}")
    assert not problems, "\n".join(problems)


def test_runs_that_rewrite_nothing(tmp):
    """--all-refs beside --recent alone widens nothing and --dry-run scopes
    only --rename, so both are refused; a --rename dry run, one matching
    nothing, and one whose --stop-at range holds no match stop after their
    preflight. Outside a repository rename_scope reports nothing."""
    d = _fixture(tmp)
    head, rename, none = _tip(d, "HEAD"), _MODES["--rename"][0], "[rename] Nothing to rename — not running filter-repo."
    for args, code, line in (
            (["--all-refs", "--recent", "1", *NEW], 1, "ERROR: --all-refs widens --older, --all, --rename, "
             "--reauthor and --recommitter; none of them was given."),
            (["--dry-run", *_MODES["--reauthor"][0]], 1, "ERROR: --dry-run currently scopes --rename only."),
            (["--dry-run", *rename], 0, "[rename] --dry-run: stopping before any rewrite."),
            (["--rename", "Nobody", "n@example.com", *NEW], 0, none),
            (["--stop-at", "HEAD~1", *rename], 0, none)):
        status, out, runs = _main_in(d, *args)
        assert (status, runs, _tip(d, "HEAD")) == (code, [], head) and line in out.splitlines(), (args, out)
    with _in(tmp):
        assert _RA.rename_scope("A", "a@x", False) == (None, [])


def main():
    return _util.runner(_util.collect(globals()), tmp_prefix="reauthor_")


if __name__ == "__main__":
    raise SystemExit(main())
