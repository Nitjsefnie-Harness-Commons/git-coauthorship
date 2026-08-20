#!/usr/bin/env python3
"""Batch-add or rewrite Co-Authored-By trailers in git history.

This standalone package provides the history-rewrite half of the
git-coauthorship wheel. It is intended for agent-authored repositories and
keeps the original rewrite behavior intact.

--recent   Rebase last N commits; add trailer if missing.
--older    Fill ALL gaps. Each gap looks backward to the nearest
           coauthored commit for its author:
             - If that trailer matches --older-inherit REGEX → inherit it
             - Otherwise → use the default --older value
           The default regex is ``Opus`` solely for compatibility with this
           historical Claude workflow; it is not a generic git rule.
           Existing coauthored commits are never touched.
--older-inherit
           Regex selecting which existing boundary trailer --older inherits.
           Default: ``Opus`` (legacy behavior). Use a provider/model regex for
           another history, ``.`` to inherit any non-empty trailer, or ``(?!)``
           to disable inheritance and always use the --older default.
--all      Overwrite every commit with a single trailer.
--rename   Rewrite one co-author identity across history. Every
           `Co-Authored-By: <OLD_NAME> <<OLD_EMAIL>>` trailer becomes
           `Co-Authored-By: <NEW_NAME> <<NEW_EMAIL>>`. Case-insensitive
           on the trailer key. Commits without the old trailer are left
           untouched, so it is safe to run across all history.
--reauthor Overwrite the git AUTHOR of every commit whose author matches
           <OLD_NAME> <<OLD_EMAIL>> to <NEW_NAME> <<NEW_EMAIL>>. Author
           field only — committer and co-author trailers are untouched.
           For cleaning up commits where an agent was recorded as the
           author instead of a co-author. It does NOT add a co-author
           trailer; pair it with --older or a manual pass if the agent
           also needs to appear as Co-Authored-By.
--recommitter
           Same as --reauthor but for the COMMITTER field. Matches on the
           committer identity, not the author's — a web-merged PR is
           authored by a person and committed by `GitHub
           <noreply@github.com>`, so the two fields need separate passes.
           Author field and co-author trailers are untouched.
           Both identity modes preflight exact matches. Zero matches skip
           git-filter-repo entirely; a nonzero pass prints its match count and
           warns that filter-repo strips GPG signatures before rewriting.
--between  Modifier for --older: fill only INTERIOR gaps — runs of
           un-trailered commits bracketed by a co-authored commit on
           BOTH sides. The oldest pre-AI run (nothing co-authored older
           than it) and any newest-end gap are left untouched. Stops
           filling once it runs out of co-authored commits to anchor
           against — auto-protects pre-AI history without --stop-at.
--stop-at  Boundary commit (exclusive). No commit at-or-older than this
           hash is touched by --recent / --older / --all / --rename.
           Also applies to --reauthor / --recommitter.
           Useful for repos that have pre-AI history to leave alone.
--before   Modifier for --rename: restrict the rename to commits whose
           AUTHOR date is strictly before WHEN (unix epoch or ISO-8601;
           naive ISO is read as UTC). Author date is used because it
           records when the work happened and survives rebases.
--after    Modifier for --rename: restrict to commits whose AUTHOR date is
           at-or-after WHEN. Combine with --before for a half-open window
           [after, before). Intersects with --stop-at when both are given.

Examples
--------
# Last 6 by Kimi; legacy default inherits the nearest backward Opus trailer:
python3 reauthor.py \\
    --recent 6 "Kimi K2.6" "noreply@kimi.com" \\
    --older "Claude Opus 4.7" "noreply@anthropic.com" \\
    --push

# Fill gaps but leave anything at-or-before commit `abc1234` untouched:
python3 reauthor.py \\
    --older "Claude Opus 4.7" "noreply@anthropic.com" \\
    --stop-at abc1234

# Fill only interior gaps; leave the pre-AI history at the oldest end alone:
python3 reauthor.py \\
    --older "Claude Opus 4.7" "noreply@anthropic.com" \\
    --between

# In a Kimi-native history, inherit the nearest Kimi boundary instead:
python3 reauthor.py \\
    --older "Kimi K3" "noreply@kimi.com" \\
    --older-inherit "Kimi" \\
    --between

# Fix a mistyped Kimi co-author everywhere it appears:
python3 reauthor.py \\
    --rename "Kimi CLI" "kimi-cli@moonshot.cn" \\
             "Kimi K2.6" "noreply@kimi.com" \\
    --push

# Collapse the committer GitHub stamps a web-merged PR leaves behind:
python3 reauthor.py \\
    --recommitter "GitHub" "noreply@github.com" \\
             "Nitjsefnie" "you@example.com" \\
    --push

# Re-split a blanket rename: revert "Kimi K2.7 Code" back to "Kimi K2.6" only
# on commits authored before the model cutoff (commits at/after keep K2.7 Code):
python3 reauthor.py \\
    --rename "Kimi K2.7 Code" "noreply@kimi.com" \\
             "Kimi K2.6" "noreply@kimi.com" \\
    --before 1781217035 \\
    --push
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

__version__ = "1.0.0"


def run(cmd, *, timeout=120, **kwargs):
    # 120s covers normal git plumbing on huge repos; long-running rewrites
    # (filter-repo) take their own timeout explicitly.
    return subprocess.run(cmd, check=True, timeout=timeout, **kwargs)


def _tmp_path(name):
    """Per-run sidecar path under the platform temp dir (POSIX /tmp, Windows
    %TEMP%). PID-suffixed so concurrent runs can't collide. Generated
    filter-repo / rebase-exec code receives this path as a JSON-encoded
    literal — never a hardcoded '/tmp/...' string, which broke on Windows
    and collided across runs."""
    return Path(tempfile.gettempdir()) / f'coauthor_{os.getpid()}_{name}'


def git(*args, **kwargs):
    return run(["git", *args], **kwargs)


def get_all_commits():
    out = subprocess.check_output(
        ["git", "log", "--format=%H"], timeout=120
    ).decode().strip()
    if not out:
        return []
    return out.split("\n")


def resolve_stop_at(stop_at):
    """Resolve user-supplied hash (possibly short) to full 40-char hash.
    Returns None if `stop_at` is None. Exits with error if hash doesn't
    resolve to a real commit."""
    if stop_at is None:
        return None
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--verify", f"{stop_at}^{{commit}}"],
            stderr=subprocess.DEVNULL, timeout=120,
        ).decode().strip()
    except subprocess.CalledProcessError:
        print(f"ERROR: --stop-at hash '{stop_at}' does not resolve to a commit.")
        sys.exit(1)


def commits_after_stop(stop_full):
    """Return commits strictly newer than stop_full (oldest-to-newest order),
    or None if stop_full is None (no filter). Stop commit itself is excluded."""
    if stop_full is None:
        return None
    out = subprocess.check_output(
        ["git", "rev-list", "--reverse", f"{stop_full}..HEAD"], timeout=120
    ).decode().strip()
    if not out:
        return []
    return out.split("\n")


def parse_when(s):
    """Parse a --before/--after value into an integer UTC epoch, or None.

    Accepts a raw unix epoch (all-digits, optional fraction) or an ISO-8601
    timestamp. A naive ISO value (no offset) is read as UTC, matching the
    convention of the kimi model-cutoff source (kimi-dash parse.py)."""
    if s is None:
        return None
    s = s.strip()
    try:
        return int(float(s))
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        print(f"ERROR: --before/--after value '{s}' is neither a unix epoch nor ISO-8601.")
        sys.exit(1)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def commits_in_window(before=None, after=None):
    """Return the set of commit hashes whose AUTHOR date falls in the window:
    author_epoch < before (if given) AND author_epoch >= after (if given).

    Returns None when neither bound is given (i.e. no restriction). Author
    date (%at) is used rather than committer date because it records when the
    work happened and is preserved across rebases / filter-repo rewrites,
    whereas committer date is reset by a rewrite."""
    if before is None and after is None:
        return None
    out = subprocess.check_output(
        ["git", "log", "--format=%H %at"], timeout=120
    ).decode().strip()
    scope = set()
    if not out:
        return scope
    for line in out.split("\n"):
        h, _, at = line.partition(" ")
        try:
            ts = int(at)
        except ValueError:
            continue
        if before is not None and ts >= before:
            continue
        if after is not None and ts < after:
            continue
        scope.add(h)
    return scope


def get_all_trailers():
    """hash -> first `Co-Authored-By:` trailer line (or None) for EVERY commit,
    from one `git log -z` pass. Replaces the old per-commit `git log -1`
    subprocess (O(n) process spawns on --older over full history). NUL field
    separator is safe: git refuses to commit text containing NUL (same format
    trick as author_stats.py)."""
    out = subprocess.check_output(
        ["git", "log", "-z", "--format=%H%x00%B"], timeout=300
    )
    tokens = out.split(b"\x00")
    if tokens and not tokens[-1]:
        tokens.pop()
    trailers = {}
    for i in range(0, len(tokens) - len(tokens) % 2, 2):
        h = tokens[i].decode("utf-8", "replace")
        body = tokens[i + 1].decode("utf-8", "replace")
        trailer = None
        for line in body.split("\n"):
            if line.startswith("Co-Authored-By:"):
                trailer = line.strip()
                break
        trailers[h] = trailer
    return trailers


def build_trailer(name, email):
    return f"Co-Authored-By: {name} <{email}>"


def matches_inherited_trailer(trailer, pattern):
    """Whether an existing boundary trailer should propagate across a gap.

    ``Opus`` is the CLI default only to preserve the script's pre-option
    behavior. Keeping the matcher explicit here prevents provider policy from
    masquerading as a general history-rewrite invariant.
    """
    return bool(trailer and pattern and re.search(pattern, trailer))


def stash_if_needed():
    dirty = (
        subprocess.run(["git", "diff", "--quiet"], check=False,
                       timeout=60).returncode != 0
        or subprocess.run(["git", "diff", "--cached", "--quiet"], check=False,
                          timeout=60).returncode != 0
        or bool(
            subprocess.check_output(
                ["git", "ls-files", "--others", "--exclude-standard"],
                timeout=60,
            ).decode().strip()
        )
    )
    if dirty:
        git("stash", "push", "-u", "-m", "reauthor auto-stash")
    return dirty


def pop_stash():
    result = subprocess.run(["git", "stash", "pop"], capture_output=True,
                            check=False, timeout=60)
    if result.returncode != 0:
        print("[warn] Could not auto-pop stash. Run `git stash pop` manually.")
    else:
        print("[stash] Restored working directory.")


def rebase_recent(n, trailer, stop_full=None):
    # If --stop-at would land inside the last N range, shrink N so the rebase
    # never reaches stop_full (stop_full itself stays untouched).
    if stop_full is not None:
        newer = commits_after_stop(stop_full) or []
        if n > len(newer):
            print(
                f"[stop-at] Requested last {n} but only {len(newer)} commits are "
                f"newer than {stop_full[:7]}; trimming N to {len(newer)}."
            )
            n = len(newer)
        if n == 0:
            print("[stop-at] Nothing to rebase — HEAD is at or before stop boundary.")
            return
    commits = subprocess.check_output(
        ["git", "log", "--reverse", "--format=%H", f"HEAD~{n}..HEAD"],
        timeout=120,
    ).decode().strip().split("\n")

    trailer_by_hash = get_all_trailers()
    lines = []
    script_path = _amend_script(trailer)
    for c in commits:
        subject = subprocess.check_output(
            ["git", "log", "-1", "--format=%s", c], timeout=120
        ).decode().strip()
        lines.append(f"pick {c} {subject}")
        if trailer_by_hash.get(c) is None:
            # git runs exec lines through sh (Git for Windows ships its own);
            # quote both paths so spaces in TEMP can't split the command.
            lines.append(f'exec "{sys.executable}" "{script_path}"')

    todo = _tmp_path("rebase_todo")
    todo.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # GIT_SEQUENCE_EDITOR also runs through git's sh; as_posix() keeps the
    # path unmangled on Windows (backslashes are escapes inside sh).
    env = {**os.environ, "GIT_SEQUENCE_EDITOR": f'cp "{todo.as_posix()}"'}
    result = subprocess.run(["git", "rebase", "-i", f"HEAD~{n}"],
                            check=False, env=env)
    if result.returncode != 0:
        print("ERROR: rebase failed. Resolve manually and run `git rebase --continue`.")
        sys.exit(1)


def _amend_script(trailer):
    """Write the rebase exec-step amend script. The trailer travels via a
    JSON sidecar — NOT interpolated into the generated source — so a name
    containing quotes/backslashes can't break or inject the generated code
    (same hardening as filter_all / rename_trailer / reauthor)."""
    cfg_path = _tmp_path("amend.json")
    cfg_path.write_text(json.dumps({"trailer": trailer}), encoding="utf-8")
    msg_path = _tmp_path("amend_msg")
    path = _tmp_path("amend.py")
    code = (
        "import json, subprocess\n"
        f"cfg = json.load(open({json.dumps(str(cfg_path))}, encoding='utf-8'))\n"
        "msg = subprocess.check_output(['git','log','-1','--format=%B']).decode()\n"
        'if "Co-Authored-By" not in msg:\n'
        "    new = msg.rstrip() + '\\n\\n' + cfg['trailer'] + '\\n'\n"
        f"    open({json.dumps(str(msg_path))}, 'w', encoding='utf-8').write(new)\n"
        f"    subprocess.run(['git','commit','--amend','-F',{json.dumps(str(msg_path))}])\n"
    )
    path.write_text(code, encoding="utf-8")
    path.chmod(0o755)
    return str(path)


def build_mapping(commits, recent_n, recent_trailer, older_default_trailer,
                  stop_full=None, between=False, inherit_pattern="Opus"):
    """Return dict: commit_hash -> full trailer line to append.

    If `stop_full` is set, commits at-or-before that hash are excluded from
    the mapping (never touched).

    `between=True` fills only INTERIOR gaps — runs of un-trailered commits
    that have a co-authored commit on BOTH sides. The two end gaps (the
    oldest pre-AI run, and any newest-end run) are left untouched, since
    each is anchored on only one side. This auto-protects pre-AI history
    without needing an explicit --stop-at hash."""
    # Filter out commits at-or-before stop_full.
    if stop_full is not None:
        newer = set(commits_after_stop(stop_full) or [])
        commits = [c for c in commits if c in newer]

    mapping = {}
    trailer_by_hash = get_all_trailers()

    # Recent commits (newest N)
    if recent_n > 0:
        recent_hashes = commits[-recent_n:]
        rest = commits[:-recent_n]
        for h in recent_hashes:
            if trailer_by_hash.get(h) is None:
                mapping[h] = recent_trailer
    else:
        rest = commits

    # Older commits: walk 'rest', filling each gap with its boundary trailer.
    boundary = older_default_trailer
    gap = []
    seen_trailer = False  # have we passed a co-authored commit yet?

    for h in reversed(rest):
        existing = trailer_by_hash.get(h)
        if existing:
            # This commit anchors the open gap on one side. The gap is
            # INTERIOR iff a co-authored commit was already seen on the
            # other side. --between fills interior gaps only; a gap with
            # no prior co-authored commit is an end gap and is skipped.
            if gap:
                if not between or seen_trailer:
                    for g in gap:
                        mapping[g] = boundary
                gap = []
            seen_trailer = True
            # Only caller-selected boundary identities propagate across gaps.
            if matches_inherited_trailer(existing, inherit_pattern):
                boundary = existing
        else:
            gap.append(h)

    # The trailing gap is anchored on only one side, so it is never an
    # interior gap — --between leaves it alone; otherwise close it.
    if gap and not between:
        for g in gap:
            mapping[g] = boundary

    return mapping


_ORIGIN_CONFIG = None
_BRANCH_TRACKING = None


def _config_values(key):
    try:
        raw = subprocess.check_output(
            ["git", "config", "--local", "--no-includes", "--null",
             "--get-all", key],
            stderr=subprocess.DEVNULL, timeout=60,
        )
    except subprocess.CalledProcessError:
        return []
    values = raw.split(b"\x00")
    if values and not values[-1]:
        values.pop()
    return [value.decode("utf-8", "replace") for value in values]


def _unset_config(key):
    result = subprocess.run(
        ["git", "config", "--local", "--no-includes", "--unset-all", key],
        capture_output=True,
        check=False, timeout=60)
    if result.returncode not in (0, 5):
        raise subprocess.CalledProcessError(
            result.returncode, result.args, result.stdout, result.stderr)


def _config_keys(pattern):
    try:
        raw = subprocess.check_output(
            ["git", "config", "--local", "--no-includes", "--null",
             "--name-only", "--get-regexp", pattern],
            stderr=subprocess.DEVNULL, timeout=60)
    except subprocess.CalledProcessError:
        return []
    return [key.decode("utf-8", "replace")
            for key in raw.split(b"\x00") if key]


def _save_origin():
    global _ORIGIN_CONFIG, _BRANCH_TRACKING
    keys = _config_keys(r"^remote\.origin\.")
    _ORIGIN_CONFIG = {key: _config_values(key) for key in dict.fromkeys(keys)}
    try:
        branch = subprocess.check_output(
            ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
            stderr=subprocess.DEVNULL, timeout=60,
        ).decode().strip()
    except subprocess.CalledProcessError:
        branch = None
    _BRANCH_TRACKING = {
        "branch": branch,
        "remote": _config_values(f"branch.{branch}.remote") if branch else [],
        "merge": _config_values(f"branch.{branch}.merge") if branch else [],
        "pushRemote": _config_values(f"branch.{branch}.pushRemote")
        if branch else [],
    }


def _restore_origin():
    origin = _ORIGIN_CONFIG or {}
    if origin:
        current = _config_keys(r"^remote\.origin\.")
        for key in dict.fromkeys([*current, *origin]):
            _unset_config(key)
        for key, values in origin.items():
            for value in values:
                subprocess.run(
                    ["git", "config", "--local", "--no-includes", "--add",
                     key, value], check=True, timeout=60)
    tracking = _BRANCH_TRACKING or {}
    branch = tracking.get("branch")
    if not branch:
        return
    for key in ("remote", "merge", "pushRemote"):
        config_key = f"branch.{branch}.{key}"
        _unset_config(config_key)
        for value in tracking.get(key, []):
            subprocess.run(
                ["git", "config", "--local", "--no-includes", "--add",
                 config_key, value], check=True, timeout=60)


def _run_filter_repo(command):
    """Run one filter-repo transaction and always restore Git configuration."""
    _save_origin()
    result = None
    run_error = None
    restore_error = None
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, input="n\n", check=False,
            timeout=1800)
    except (OSError, subprocess.TimeoutExpired) as exc:
        run_error = exc
    try:
        _restore_origin()
    except (OSError, subprocess.SubprocessError) as exc:
        restore_error = exc

    if run_error is not None:
        print(f"ERROR: git-filter-repo could not complete: {run_error}")
    elif result.returncode != 0:  # pyright: ignore[reportOptionalMemberAccess]
        print("ERROR: git-filter-repo failed:")
        print(result.stderr)  # pyright: ignore[reportOptionalMemberAccess]
    if restore_error is not None:
        print(f"ERROR: could not restore Git remote/upstream configuration: "
              f"{restore_error}")
    if run_error is not None or result.returncode != 0 or restore_error is not None:  # pyright: ignore[reportOptionalMemberAccess]
        sys.exit(1)
    return result


def filter_repo_with_mapping(mapping, stop_full=None):
    if not mapping:
        print("[filter-repo] No matching commits; history left untouched.")
        return
    if subprocess.run(["git-filter-repo", "--help"], capture_output=True,
                      check=False).returncode != 0:
        print("ERROR: git-filter-repo is required for --older / --all but is not installed.")
        sys.exit(1)

    # Write mapping to JSON for the callback to read
    map_path = _tmp_path("map.json")
    map_path.write_text(json.dumps(mapping), encoding="utf-8")

    # Write callback to file (git-filter-repo expects the FUNCTION BODY, not a
    # def). The sidecar path is embedded as a JSON-encoded literal.
    callback_path = _tmp_path("callback.py")
    callback = (
        'import json\n'
        f'mapping = json.load(open({json.dumps(str(map_path))}, encoding="utf-8"))\n'
        'commit_id = commit.original_id.decode()\n'
        'if commit_id in mapping:\n'
        '    trailer = mapping[commit_id].encode()\n'
        '    commit.message = commit.message.rstrip(b"\\n") + b"\\n\\n" + trailer + b"\\n"\n'
    )
    callback_path.write_text(callback, encoding="utf-8")

    command = ["git-filter-repo", "--force"]
    if stop_full is not None:
        command += ["--refs", f"{stop_full}..HEAD"]
    command += ["--commit-callback", str(callback_path)]
    _run_filter_repo(command)


def filter_all(name, email, stop_full=None):
    trailer_line = build_trailer(name, email)
    if stop_full is None:
        # Fast path: rewrite every commit unconditionally via message-callback.
        # The trailer travels as a JSON-encoded literal so a name/email
        # containing `"""`, backslashes, or newlines can't escape the source
        # string and inject arbitrary Python into filter-repo's eval context.
        trailer_repr = json.dumps(trailer_line)
        callback = (
            "import re\n"
            f"_trailer = {trailer_repr}.encode()\n"
            'message = re.sub(rb"Co-Authored-By:.*\\n?", b"", message)\n'
            'message = message.rstrip(b"\\n") + b"\\n\\n" + _trailer + b"\\n"\n'
            "return message\n"
        )
        _run_filter_repo(
            ["git-filter-repo", "--force", "--message-callback", callback])
    else:
        # Bounded path: build a mapping of {commit_id: trailer} for commits
        # newer than stop_full, then route through the existing commit-callback
        # path which is already stop-aware via mapping membership.
        newer = commits_after_stop(stop_full) or []
        mapping = {h: trailer_line for h in newer}
        if not mapping:
            print("[stop-at] No commits newer than stop boundary — nothing to do.")
            return
        print(f"[filter-repo] --all bounded by stop-at: {len(mapping)} commits will be rewritten.")
        filter_repo_with_mapping(mapping, stop_full=stop_full)
        return


def rename_trailer(old_name, old_email, new_name, new_email, stop_full=None,
                   restrict=None):
    """Rewrite every `Co-Authored-By: <old_name> <<old_email>>` trailer to the
    new name/email. Case-insensitive on the trailer key. Commits without the
    old trailer are untouched, so this is safe across all history.

    `restrict`, when given, is an explicit set of commit hashes (e.g. an
    author-date window from --before/--after); only those commits are
    considered. It intersects with the --stop-at boundary when both are
    supplied — the effective scope is the intersection of every restriction.

    Values are passed to the git-filter-repo callback via a JSON sidecar
    (not string-interpolated into the callback source) so names containing
    quotes or regex metacharacters can't break the generated code."""
    if subprocess.run(["git-filter-repo", "--help"], capture_output=True,
                      check=False).returncode != 0:
        print("ERROR: git-filter-repo is required for --rename but is not installed.")
        sys.exit(1)

    cfg = {
        "old_name": old_name,
        "old_email": old_email,
        "new_name": new_name,
        "new_email": new_email,
    }

    # Combine the optional restrictions into one scope set. scope is None when
    # neither --stop-at nor a date window applies (rename across all history,
    # via the fast message-callback path); otherwise it is the intersection.
    scope = None
    if stop_full is not None:
        scope = set(commits_after_stop(stop_full) or [])
    if restrict is not None:
        scope = restrict if scope is None else (scope & restrict)
    if scope is not None:
        if not scope:
            print("[scope] No commits in the requested range — nothing to do.")
            return
        cfg["newer"] = list(scope)

    cfg_path = _tmp_path("rename.json")
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")

    # Shared callback prologue: build the old-trailer regex + new-trailer bytes.
    # `[ \t]*` (not `\s*`) keeps the match on one line — `\s` would let it
    # bleed across the trailer block.
    build = (
        'import json, re\n'
        f'cfg = json.load(open({json.dumps(str(cfg_path))}, encoding="utf-8"))\n'
        '_pat = re.compile(rb"(?i)co-authored-by:[ \\t]*"'
        ' + re.escape(cfg["old_name"].encode())'
        ' + rb"[ \\t]*<" + re.escape(cfg["old_email"].encode()) + rb">")\n'
        '_new = ("Co-Authored-By: " + cfg["new_name"]'
        ' + " <" + cfg["new_email"] + ">").encode()\n'
    )

    if scope is None:
        callback = build + 'return _pat.sub(_new, message)\n'
        flag = "--message-callback"
    else:
        callback = (
            build
            + '_newer = set(cfg["newer"])\n'
            'if commit.original_id.decode() in _newer:\n'
            '    commit.message = _pat.sub(_new, commit.message)\n'
        )
        flag = "--commit-callback"

    command = ["git-filter-repo", "--force"]
    if stop_full is not None:
        command += ["--refs", f"{stop_full}..HEAD"]
    command += [flag, callback]
    _run_filter_repo(command)


def matching_identity_commits(field, old_name, old_email, stop_full=None):
    """Commit hashes whose selected identity exactly matches the old value.

    Scan every reachable local ref because an unbounded git-filter-repo pass
    does the same. A stop boundary retains the existing HEAD-relative scope
    used by the callback.
    """
    if field not in ("author", "committer"):
        raise ValueError(f"field must be 'author' or 'committer', got {field!r}")
    name_field, email_field = (
        ("%an", "%ae") if field == "author" else ("%cn", "%ce")
    )
    fmt = f"%H%x00{name_field}%x00{email_field}"
    out = subprocess.check_output(
        ["git", "log", "--all", "-z", f"--format={fmt}"], timeout=120)
    tokens = out.split(b"\x00")
    if tokens and not tokens[-1]:
        tokens.pop()
    allowed = None if stop_full is None else set(
        commits_after_stop(stop_full) or [])
    matches = []
    for i in range(0, len(tokens) - len(tokens) % 3, 3):
        commit = tokens[i].decode("ascii", "replace")
        name = tokens[i + 1].decode("utf-8", "replace")
        email = tokens[i + 2].decode("utf-8", "replace")
        if allowed is not None and commit not in allowed:
            continue
        if name == old_name and email == old_email:
            matches.append(commit)
    return matches


def rewrite_identity(field, old_name, old_email, new_name, new_email,
                     stop_full=None):
    """Overwrite the git identity in `field` ('author' or 'committer') on
    every commit whose identity in THAT SAME field matches
    (old_name, old_email). The other identity field and the co-author
    trailers are left untouched.

    `author` is the 'an agent was recorded as the author instead of a
    co-author' cleanup; it does not add a Co-Authored-By trailer.
    `committer` is the separate pass a web-merged PR needs — GitHub records
    the person as author and `GitHub <noreply@github.com>` as committer, so
    matching on the author identity would never see it.

    Values are passed to the git-filter-repo callback via a JSON sidecar
    so identities containing quotes can't break the generated code."""
    if field not in ("author", "committer"):
        raise ValueError(f"field must be 'author' or 'committer', got {field!r}")
    flag = "--reauthor" if field == "author" else "--recommitter"
    matches = matching_identity_commits(
        field, old_name, old_email, stop_full=stop_full)
    count = len(matches)
    noun = "commit" if count == 1 else "commits"
    if not matches:
        print(f"[filter-repo] {flag}: 0 matching commits; history left untouched.")
        return
    print(f"[filter-repo] {flag}: {count} matching {noun} will be rewritten.")
    print(
        "[warn] GPG signatures in the processed history will be stripped; "
        "signed commits and their descendants will receive new SHAs."
    )
    if subprocess.run(["git-filter-repo", "--help"], capture_output=True,
                      check=False).returncode != 0:
        print(f"ERROR: git-filter-repo is required for {flag} but is not installed.")
        sys.exit(1)

    cfg = {
        "old_name": old_name,
        "old_email": old_email,
        "new_name": new_name,
        "new_email": new_email,
    }
    if stop_full is not None:
        newer = commits_after_stop(stop_full) or []
        if not newer:
            print("[stop-at] No commits newer than stop boundary — nothing to do.")
            return
        cfg["newer"] = newer

    # Field-specific sidecar name: --reauthor and --recommitter can run in
    # the SAME invocation, and _tmp_path is only PID-unique.
    cfg_path = _tmp_path(f"{field}.json")
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")

    gate = (
        'commit.original_id.decode() in set(cfg["newer"]) and '
        if stop_full is not None
        else ''
    )
    callback = (
        'import json\n'
        f'cfg = json.load(open({json.dumps(str(cfg_path))}, encoding="utf-8"))\n'
        f'if {gate}commit.{field}_name == cfg["old_name"].encode()'
        f' and commit.{field}_email == cfg["old_email"].encode():\n'
        f'    commit.{field}_name = cfg["new_name"].encode()\n'
        f'    commit.{field}_email = cfg["new_email"].encode()\n'
    )

    command = ["git-filter-repo", "--force"]
    if stop_full is not None:
        command += ["--refs", f"{stop_full}..HEAD"]
    command += ["--commit-callback", callback]
    _run_filter_repo(command)


def reauthor(old_name, old_email, new_name, new_email, stop_full=None):
    """Rewrite the AUTHOR identity. See rewrite_identity()."""
    return rewrite_identity("author", old_name, old_email, new_name, new_email,
                            stop_full=stop_full)


def recommitter(old_name, old_email, new_name, new_email, stop_full=None):
    """Rewrite the COMMITTER identity. See rewrite_identity()."""
    return rewrite_identity("committer", old_name, old_email, new_name,
                            new_email, stop_full=stop_full)


def main():
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")  # pyright: ignore[reportAttributeAccessIssue]
        except (AttributeError, ValueError, OSError):
            pass  # not a reconfigurable stream (redirected/piped)
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"reauthor {__version__}")
    ap.add_argument("--recent", nargs=3, metavar=("N", "NAME", "EMAIL"))
    ap.add_argument("--older", nargs=2, metavar=("NAME", "EMAIL"))
    ap.add_argument(
        "--older-inherit",
        default="Opus",
        metavar="REGEX",
        help="existing boundary trailers --older may inherit (default: Opus, "
        "the historical heuristic; use '(?!)' to disable)",
    )
    ap.add_argument("--all", nargs=2, metavar=("NAME", "EMAIL"))
    ap.add_argument(
        "--rename",
        nargs=4,
        metavar=("OLD_NAME", "OLD_EMAIL", "NEW_NAME", "NEW_EMAIL"),
    )
    ap.add_argument(
        "--reauthor",
        nargs=4,
        metavar=("OLD_NAME", "OLD_EMAIL", "NEW_NAME", "NEW_EMAIL"),
    )
    ap.add_argument(
        "--recommitter",
        nargs=4,
        metavar=("OLD_NAME", "OLD_EMAIL", "NEW_NAME", "NEW_EMAIL"),
    )
    ap.add_argument(
        "--between",
        action="store_true",
        help="modifier for --older: fill only interior gaps (bracketed by a "
        "co-authored commit on both sides); leave the oldest pre-AI run and "
        "any newest-end gap untouched",
    )
    ap.add_argument(
        "--stop-at",
        metavar="HASH",
        help="Boundary commit (exclusive). Anything at-or-older than HASH "
        "is never modified by any rewrite mode.",
    )
    ap.add_argument(
        "--before",
        metavar="WHEN",
        help="Modifier for --rename: restrict to commits whose AUTHOR date is "
        "strictly before WHEN (unix epoch or ISO-8601; naive ISO = UTC).",
    )
    ap.add_argument(
        "--after",
        metavar="WHEN",
        help="Modifier for --rename: restrict to commits whose AUTHOR date is "
        "at-or-after WHEN (unix epoch or ISO-8601; naive ISO = UTC).",
    )
    ap.add_argument("--push", action="store_true")
    args = ap.parse_args()

    if not (args.recent or args.older or args.all or args.rename or args.reauthor
            or args.recommitter):
        ap.print_help()
        sys.exit(1)

    try:
        re.compile(args.older_inherit)
    except re.error as exc:
        print(f"ERROR: invalid --older-inherit regex: {exc}")
        sys.exit(1)

    stop_full = resolve_stop_at(args.stop_at)
    if stop_full is not None:
        short = stop_full[:7]
        print(f"[stop-at] Honoring boundary: nothing at-or-before {short} will be touched.")

    before_epoch = parse_when(args.before)
    after_epoch = parse_when(args.after)
    window = commits_in_window(before_epoch, after_epoch)
    if window is not None and not args.rename:
        print("ERROR: --before/--after currently scope --rename only.")
        sys.exit(1)

    dirty = stash_if_needed()

    try:
        recent_n = int(args.recent[0]) if args.recent else 0
        recent_trailer = build_trailer(args.recent[1], args.recent[2]) if args.recent else None
        older_default = build_trailer(args.older[0], args.older[1]) if args.older else None

        if args.recent:
            print(f"[rebase] Tagging last {recent_n} commits with: {recent_trailer}")
            rebase_recent(recent_n, recent_trailer, stop_full=stop_full)

        if args.older:
            commits = get_all_commits()
            mapping = build_mapping(
                commits, recent_n, recent_trailer, older_default,
                stop_full=stop_full, between=args.between,
                inherit_pattern=args.older_inherit,
            )
            scope = "interior gaps only" if args.between else "all gaps"
            print(
                f"[filter-repo] Filling {len(mapping)} commits ({scope}); "
                f"default older = {older_default}; inherit regex = "
                f"{args.older_inherit!r}"
            )
            filter_repo_with_mapping(mapping, stop_full=stop_full)

        if args.all:
            print(f"[filter-repo] Tagging ALL commits with: {build_trailer(args.all[0], args.all[1])}")
            filter_all(args.all[0], args.all[1], stop_full=stop_full)

        if args.rename:
            on, oe, nn, ne = args.rename
            print(
                f"[filter-repo] Renaming co-author: {on} <{oe}>  ->  {nn} <{ne}>"
            )
            if window is not None:
                print(f"[scope] author-date window restricts to {len(window)} commit(s).")
            rename_trailer(on, oe, nn, ne, stop_full=stop_full, restrict=window)

        if args.reauthor:
            on, oe, nn, ne = args.reauthor
            print(
                f"[filter-repo] Reauthoring: author {on} <{oe}>  ->  {nn} <{ne}>"
            )
            reauthor(on, oe, nn, ne, stop_full=stop_full)

        if args.recommitter:
            on, oe, nn, ne = args.recommitter
            print(
                f"[filter-repo] Recommitting: committer {on} <{oe}>  ->  {nn} <{ne}>"
            )
            recommitter(on, oe, nn, ne, stop_full=stop_full)

        if args.push:
            # Plain --force, not --force-with-lease: git-filter-repo strips
            # remote-tracking refs, so --force-with-lease bails with "stale
            # info" every run. Since we just deliberately rewrote history
            # locally, plain --force is the intended semantic.
            git("push", "origin", "HEAD", "--force")
            print("[push] Force-pushed.")

    finally:
        if dirty:
            pop_stash()

    print("Done.")


if __name__ == "__main__":
    main()
