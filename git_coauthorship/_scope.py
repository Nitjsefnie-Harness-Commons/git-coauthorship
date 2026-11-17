import subprocess


def refs_args(stop_full, all_refs):
    """-> filter-repo's --refs. With none it rewrites every ref, expires the
    reflogs and prunes the old objects; --refs implies --partial, which keeps
    the reflogs, so the default is HEAD."""
    if stop_full is not None:
        return ["--refs", f"{stop_full}..HEAD"]
    if all_refs:
        return []
    return ["--refs", "HEAD"]


def scope_refs(all_refs):
    if all_refs:
        try:
            refs = subprocess.check_output(
                ["git", "show-ref"], timeout=60,
                stderr=subprocess.DEVNULL).decode("utf-8", "replace")
        except (OSError, subprocess.SubprocessError):
            return []
        return [line.split(" ", 1)[1]
                for line in refs.splitlines() if " " in line]
    try:
        head = subprocess.check_output(
            ["git", "rev-parse", "--symbolic-full-name", "HEAD"],
            timeout=60, stderr=subprocess.DEVNULL).decode().strip()
    except (OSError, subprocess.SubprocessError):
        head = ""
    return [head or "HEAD (detached)"]


def scope_lines(mode, all_refs, refs, stop_full=None):
    """-> (the scope line, the untouched-refs note or None) for `mode`."""
    listed = ', '.join(refs) or 'no refs'
    if stop_full is not None:
        return (f"[{mode}] scope: the current branch — {listed}, bounded to "
                f"{stop_full[:7]}..HEAD (--all-refs has no effect with "
                "--stop-at)"), f"[{mode}] Other refs are untouched."
    where = "every ref in the repository" if all_refs else "the current branch"
    scope = f"[{mode}] scope: {where} — {listed}"
    if all_refs:
        return scope, None
    return scope, (f"[{mode}] Other refs are untouched. Pass --all-refs to "
                   "rewrite the whole repository.")


def announce_scope(mode, all_refs, stop_full=None):
    refs = scope_refs(all_refs and stop_full is None)
    for line in scope_lines(mode, all_refs, refs, stop_full):
        if line:
            print(line)
