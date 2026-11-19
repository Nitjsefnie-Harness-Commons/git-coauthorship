"""--push, leased on the remote tip this clone last saw."""
import subprocess
import sys


def _git(*args):
    return subprocess.run(["git", *args], capture_output=True, text=True,
                          check=False, timeout=60).stdout.strip()


def read_lease():
    """-> (branch, its origin tracking ref or ""), read before any rewrite
    strips that ref; `ls-remote` would see another clone's push."""
    branch = _git("symbolic-ref", "--quiet", "--short", "HEAD")
    return branch, branch and _git(
        "rev-parse", "-q", "--verify", f"refs/remotes/origin/{branch}")


def push(lease, dry_run):
    if dry_run:
        print("[push] --dry-run: push skipped.")
        return
    pushurl = _git("config", "--get", "remote.origin.pushurl")
    if pushurl and pushurl != _git("config", "--get", "remote.origin.url"):
        # The tracking ref records the fetch URL only.
        subprocess.run(["git", "push", "origin", "HEAD", "--force"],
                       check=True, timeout=120)
        print("[push] Force-pushed to the separate push URL; the push was "
              "not checked against the remote.")
        return
    branch, expected = lease
    result = subprocess.run(
        ["git", "push", "--porcelain",
         f"--force-with-lease=refs/heads/{branch}:{expected}",
         "origin", f"HEAD:refs/heads/{branch}"],
        capture_output=True, text=True, check=False, timeout=120)
    print(result.stdout, end="")
    print(result.stderr, end="", file=sys.stderr)
    if result.returncode == 0:
        print("[push] Force-pushed.")
        return
    if "(stale info)" in result.stdout:
        print("[push] ERROR: the remote branch moved since this clone last "
              "saw it; the rewritten local history was not pushed.")
    else:
        print("[push] ERROR: git push failed; the rewritten local history "
              "was not pushed.")
    sys.exit(1)
