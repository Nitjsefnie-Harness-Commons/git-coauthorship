"""Suite for reauthor.py. `--reauthor` and `--recommitter` are separate
passes: a web-merged PR is authored by a person and committed by `GitHub
<noreply@github.com>`, so each matches and writes only its own field. Without
git-filter-repo the end-to-end tests skip.
"""
import contextlib
import os
import shutil
import subprocess
import sys
from unittest import mock

import _util

_RA = _util.load(os.path.join(_util.SCRIPTS, "reauthor.py"))
OLD, NEW, HUMAN = ("Peter Z", "p@example.com"), ("Nitjsefnie", "z@example.com"), ("Human", "human@example.com")


def _require_filter_repo():
    _util.require_git()
    if not shutil.which("git-filter-repo"):
        _util.skip("git-filter-repo is not available on PATH")


def _stub(directory, script):
    """A POSIX-shell git-filter-repo in `directory` -> directory."""
    if os.name == "nt":
        _util.skip("the git-filter-repo stub is a POSIX shell script")
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, "git-filter-repo"), "w", encoding="utf-8") as f:
        f.write("#!/bin/sh\n" + script)
    os.chmod(f.name, 0o700)
    return directory


def _out(d, *args, env=None):
    return _util.git(d, *args, env=env).stdout.strip()


def _idents(d):
    """-> (author_name, author_email, committer_name, committer_email)."""
    return tuple(_out(d, "log", "-1", "--format=%an%x00%ae%x00%cn%x00%ce").split("\x00"))


def _tracked(tmp):
    """-> (repository whose `main` tracks `origin`, the bare remote)."""
    d, remote = os.path.join(tmp, "r"), os.path.join(tmp, "remote.git")
    _util.repo(d, ["subject"], OLD, HUMAN)
    _out(tmp, "init", "--bare", "-q", remote)
    _out(d, "remote", "add", "origin", remote)
    _out(d, "push", "-q", "-u", "origin", "main")
    return d, remote


def _settings(d):
    """Remote and branch settings by key, each key's values in order."""
    return sorted(_out(d, "config", "--get-regexp", r"^(remote|branch)\.").splitlines(), key=lambda x: x.split()[0])


def _sign(d):
    """Replace HEAD with the same commit carrying a gpgsig header."""
    raw = subprocess.check_output(["git", "-C", d, "cat-file", "commit", "HEAD"], timeout=60)
    header, _, body = raw.partition(b"\n\n")
    signed = (header + b"\ngpgsig -----BEGIN PGP SIGNATURE-----\n fake-signature\n"
              b" -----END PGP SIGNATURE-----\n\n" + body)
    commit = subprocess.check_output(["git", "-C", d, "hash-object", "-t", "commit", "-w", "--stdin"],
                                     input=signed, timeout=60).decode().strip()
    _out(d, "update-ref", "refs/heads/main", commit)
    return commit


def _reauthor(d, *args, env=None, code=0):
    r = subprocess.run(
        [sys.executable, os.path.join(_util.SCRIPTS, "reauthor.py"), *args],
        cwd=d, env=env, capture_output=True, text=True, check=False,
        timeout=300)
    assert r.returncode == code, f"reauthor.py exited {r.returncode}: {r.stdout}\n{r.stderr}"
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
    d = os.path.join(tmp, "r")
    _util.repo(d, ["subject"], OLD, HUMAN)
    head = _sign(d)

    out = _reauthor(d, "--reauthor", "Nobody", "nobody@example.invalid", *NEW).stdout

    assert _out(d, "rev-parse", "HEAD") == head, "a zero-match rewrite changed signed history"
    assert "[filter-repo] --reauthor: 0 matching commits; history left untouched." in out.splitlines(), out


def test_stop_at_preserves_a_signed_boundary_object(tmp):
    """The pass prints its count and signature risk; the boundary keeps its bytes."""
    _require_filter_repo()
    d = os.path.join(tmp, "r")
    _util.repo(d, ["subject"], OLD, HUMAN)
    boundary = _sign(d)
    boundary_bytes = _out(d, "cat-file", "commit", boundary)
    _util.commit(d, "child", OLD, HUMAN)

    out = _reauthor(d, "--reauthor", *OLD, *NEW, "--stop-at", boundary).stdout.splitlines()

    for line in ("[filter-repo] --reauthor: 1 matching commit will be rewritten.",
                 "[warn] GPG signatures in the processed history will be stripped; "
                 "signed commits and their descendants will receive new SHAs."):
        assert line in out, out
    after = _out(d, "cat-file", "commit", boundary)
    assert after == boundary_bytes and "gpgsig " in after
    assert _out(d, "rev-parse", "HEAD^") == boundary and _idents(d)[:2] == NEW


def test_identity_rewrite_restores_current_branch_upstream(tmp):
    _require_filter_repo()
    d, _remote = _tracked(tmp)
    push_remote = os.path.join(tmp, "push.git")
    _out(tmp, "init", "--bare", "-q", push_remote)
    _out(d, "remote", "set-url", "--push", "origin", push_remote)
    _out(d, "config", "--add", "remote.origin.fetch", "+refs/pull/*:refs/remotes/origin/pull/*")
    before = _settings(d)

    _reauthor(d, "--reauthor", *OLD, *NEW)

    assert _settings(d) == before
    _out(d, "push", "--force")
    assert _out(push_remote, "rev-parse", "refs/heads/main") == _out(d, "rev-parse", "HEAD")


def test_push_overwrites_only_the_remote_tip_this_clone_last_saw(tmp):
    """The lease is what refs/remotes/origin/<branch> held before the rewrite
    (an --all-refs pass strips it): a remote moved since, or a branch the clone
    never fetched, is left as it was; a separate push URL is pushed unchecked."""
    recent, new, ok = ["--recent", "1", *NEW], ["checkout", "-q", "-b", "new"], "[push] Force-pushed."
    moved = "[push] ERROR: the remote branch moved since this clone last saw it; the rewritten local history was not pushed."
    rows = [(recent, [], 0, ok, "remote.git", "main"),
            (recent, [["push", "-q", "origin", "HEAD:main"], ["update-ref", "refs/remotes/origin/main", "HEAD~1"]],
             1, moved, "remote.git", "main"),
            (recent, [new], 0, ok, "remote.git", "new"),
            (recent, [new, ["push", "-q", "origin", "HEAD~1:refs/heads/new"], ["update-ref", "-d", "refs/remotes/origin/new"]],
             1, moved, "remote.git", "new"),
            (recent, [["init", "--bare", "-q", "../push.git"], ["remote", "set-url", "--push", "origin", "../push.git"]], 0,
             "[push] Force-pushed to the separate push URL; the push was not checked against the remote.", "push.git", "main"),
            (recent, [["remote", "set-url", "origin", "../missing.git"]], 1,
             "[push] ERROR: git push failed; the rewritten local history was not pushed.", "remote.git", "main")]
    rows += [(["--all", *NEW, "--all-refs"], [], 0, ok, "remote.git", "main")] * bool(shutil.which("git-filter-repo"))
    for i, (args, prep, code, line, remote, branch) in enumerate(rows):
        d, _remote = _tracked(os.path.join(tmp, str(i)))
        _util.commit(d, "two", OLD)
        for step in prep:
            _out(d, *step)
        where = os.path.join(d, os.pardir, remote)
        before = _tip(where, f"refs/heads/{branch}")
        out = _reauthor(d, *args, "--push", code=code).stdout
        assert line in out.splitlines() and f"By: {NEW[0]}" in _out(d, "log", "-1", "--format=%B"), (prep, out)
        assert _tip(where, f"refs/heads/{branch}") == (_tip(d, "HEAD") if code == 0 else before), (prep, out)


def test_restore_does_not_copy_global_remote_values_into_local_config(tmp):
    _require_filter_repo()
    d, remote = _tracked(tmp)
    global_config = os.path.join(tmp, "global.gitconfig")
    _out(tmp, "config", "--file", global_config, "remote.origin.url", os.path.join(tmp, "global.git"))
    env = {**os.environ, "GIT_CONFIG_GLOBAL": global_config, "GIT_CONFIG_NOSYSTEM": "1"}

    _reauthor(d, "--reauthor", *OLD, *NEW, env=env)

    assert _out(d, "config", "--local", "--get-all", "remote.origin.url", env=env).splitlines() == [remote]


def test_failed_filter_repo_still_restores_remote_and_upstream(tmp):
    _util.require_git()
    fake = _stub(os.path.join(tmp, "bin"), 'if [ "$1" = "--help" ]; then exit 0; fi\n'
                 "git config --remove-section remote.origin >/dev/null 2>&1 || true\n"
                 "git config --remove-section branch.main >/dev/null 2>&1 || true\n"
                 "exit 9\n")
    d, remote = _tracked(tmp)
    env = {**os.environ, "PATH": fake + os.pathsep + os.environ["PATH"]}

    result = _reauthor(d, "--reauthor", *OLD, *NEW, env=env, code=1)

    assert "git-filter-repo failed" in result.stdout
    assert _out(d, "remote", "get-url", "origin") == remote
    assert _out(d, "config", "--get", "branch.main.remote") == "origin"


def test_restore_configuration_failure_is_not_silenced(_tmp):
    def refuse_unset(args, **_kwargs):
        """No git runs, so nothing reaches a real repository."""
        return subprocess.CompletedProcess(args, 0 if "--add" in args else 9, b"", b"denied")
    tracking = {"branch": "main", "remote": ["origin"], "merge": [], "pushRemote": []}
    with mock.patch.multiple(_RA, _ORIGIN_CONFIG={}, _BRANCH_TRACKING=tracking), \
            mock.patch.object(_RA.subprocess, "run", refuse_unset):
        try:
            _RA.__dict__["_restore_origin"]()
        except subprocess.CalledProcessError:
            return
    raise AssertionError("configuration restoration failure was ignored")


def test_each_identity_pass_matches_and_writes_only_its_own_field(tmp):
    """Each pass matches on its own field; both in one run need per-field
    sidecars, as _tmp_path is only PID-unique."""
    _require_filter_repo()
    bot, github = ("Bot", "bot@example.com"), ("GitHub", "noreply@github.com")
    for i, (committer, args, want) in enumerate((
            (bot, ["--recommitter", *OLD, *NEW], (*OLD, *bot)),
            (bot, ["--recommitter", *bot, *NEW], (*OLD, *NEW)),
            (github, ["--reauthor", *OLD, *NEW, "--recommitter", *github, *NEW], (*NEW, *NEW)))):
        d = os.path.join(tmp, str(i))
        _util.repo(d, ["subject"], OLD, committer)
        _reauthor(d, *args)
        assert _idents(d) == want, (args, _idents(d))


def test_older_inherit_pattern_is_configurable(_tmp):
    """The historical Opus boundary heuristic is policy, not a universal git
    fact. Callers must be able to choose which existing trailer propagates."""
    kimi, default = "Co-Authored-By: Kimi K3 <noreply@kimi.com>", "Co-Authored-By: Default Agent <agent@example.com>"
    trailers = {"old-anchor": "Co-Authored-By: Human <h@example.com>", "gap": None, "new-anchor": kimi}
    with mock.patch.object(_RA, "get_all_trailers", lambda: trailers):
        for pattern, want in (("Kimi", kimi), (r"(?!)", default)):
            assert _RA.build_mapping(list(trailers), 0, None, default, inherit_pattern=pattern)["gap"] == want, pattern


def test_parse_when_accepts_epoch_and_iso8601_forms(_tmp):
    assert [_RA.parse_when(s) for s in ("1704067200.9", "2024-01-01T00:00:00Z", "2024-01-01T00:00:00")] == [
        1704067200] * 3


TRAILER = "Co-Authored-By: Opus 5 <noreply@anthropic.com>"
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
    return _util.git(d, *args, check=False)


def _tip(d, ref):
    return _git(d, "rev-parse", "-q", "--verify", ref).stdout.strip()


def _fixture(d):
    """`feature` checked out, `main` and `origin/main` at the base commit,
    `other` in a second worktree; `feature` and `other` each add a TRAILER
    commit then a gap. Every commit is OLD's."""
    repo, worktree = os.path.join(d, "repo"), os.path.join(d, "wt")
    _util.repo(repo, ["subject"], OLD)
    for args in (["worktree", "add", "-q", "-b", "other", worktree],
                 ["checkout", "-q", "-b", "feature"],
                 ["update-ref", "refs/remotes/origin/main", "main"]):
        _util.git(repo, *args)
    for where, branch in ((repo, "feature"), (worktree, "other")):
        _util.commit(where, f"{branch} work\n\n{TRAILER}", OLD)
        _util.commit(where, f"{branch} gap", OLD)
    return repo


def _main_in(d, *args):
    """main() in `d` -> (exit status, stdout, [(stdout length, argv)] per
    filter-repo pass); without git-filter-repo passes are only recorded."""
    found, runs, real = shutil.which("git-filter-repo"), [], _RA.__dict__["_run_filter_repo"]
    path = os.environ["PATH"]
    path = path if found else _stub(os.path.join(d, os.pardir, "bin"), "exit 0\n") + os.pathsep + path

    def recording(command):
        runs.append((sys.stdout.tell(), command))
        return real(command) if found else None
    with mock.patch.multiple(_RA, _run_filter_repo=recording), mock.patch.dict(os.environ, PATH=path), \
            contextlib.chdir(d):
        code, out, _err = _util.call_main(_RA.main, ["reauthor", *args])
    return code, out, runs


def test_each_mode_rewrites_only_the_scope_it_prints(tmp):
    """Each mode alone, with --all-refs, and with --stop-at beside it (the
    bound wins), plus --all-refs beside --recent; with git-filter-repo also
    the refs moved, reflogs and old objects kept, and what was written."""
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
    """Refusals, and passes that stop after their preflight (so strip no
    signature); outside a repository rename_scope reports nothing."""
    d, remote = _fixture(tmp), os.path.join(tmp, "remote.git")
    head, rename, none = _tip(d, "HEAD"), _MODES["--rename"][0], "[rename] Nothing to rename — not running filter-repo."
    for args in (["init", "--bare", "-q", remote], ["remote", "add", "origin", remote],
                 ["push", "-q", "origin", "main:feature"], ["update-ref", "-d", "refs/remotes/origin/feature"]):
        _util.git(d, *args)  # the remote holds a `feature` this clone never fetched
    for args, code, line in (
            (["--all-refs", "--recent", "1", *NEW], 1, "ERROR: --all-refs widens --older, --all, --rename, "
             "--reauthor and --recommitter; none of them was given."),
            (["--dry-run", *_MODES["--reauthor"][0]], 1, "ERROR: --dry-run currently scopes --rename only."),
            (["--dry-run", *rename], 0, "[rename] --dry-run: stopping before any rewrite."),
            (["--dry-run", "--push", *rename], 0, "[push] --dry-run: push skipped."),
            (["--rename", "Nobody", "n@example.com", *NEW], 0, none),
            (["--stop-at", "HEAD~1", *rename], 0, none)):
        status, out, runs = _main_in(d, *args)
        assert (status, runs, _tip(d, "HEAD"), _tip(remote, "feature")) == (code, [], head, _tip(d, "main")) \
            and line in out.splitlines(), (args, out)
    with contextlib.chdir(tmp):
        assert _RA.rename_scope("A", "a@x", False) == (None, [])


def main():
    return _util.runner(_util.collect(globals()), tmp_prefix="reauthor_")


if __name__ == "__main__":
    raise SystemExit(main())
