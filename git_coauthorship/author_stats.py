#!/usr/bin/env python3
"""Authorship stats for git history — authors AND co-author trailers.

This standalone package provides the read-only reporting half of the
git-coauthorship wheel. It complements `reauthor.py`, which rewrites history,
without changing any commits itself.

Read-only counterpart to `reauthor.py` (which rewrites history to fill
gaps and correct identities). This script only reports — it never touches
commits.

Default output is a summary: total commits, how many carry a
`Co-Authored-By:` trailer, how many don't, a per-AUTHOR breakdown (the git
author field, `Name <email>`, shortlog-style), and a per-trailer breakdown
(commit counts grouped by co-author, with a `(none)` bucket for the gaps).

The two breakdowns answer different questions. The author tally is who git
records as writing each commit; the trailer tally is which agents assisted.
An agent appearing in the AUTHOR list is a misattribution — that is what
`reauthor.py --reauthor` corrects.

  --list      list every commit with its trailer (or MISSING)
  --missing   list only the commits with no Co-Authored-By trailer

Both helpers are independent — pass either, both, or neither.

Trailer detection is case-insensitive on the key (`co-authored-by:`), so
GitHub-style `Co-authored-by:` counts as co-authored — unlike
`reauthor.py`'s strict `Co-Authored-By:` check, whose job is
"would --recent touch this", not "is this commit compliant".

Exit status: 0 if every commit in range is co-authored, 1 if any are
missing, 2 on error (no git, not a repo, bad range). The 0/1 split lets
this gate a pre-push hook or CI step.

Examples
--------
python3 author_stats.py                    # summary, repo at cwd
python3 author_stats.py --missing          # ...and list the offenders
python3 author_stats.py --list             # ...and list every commit
python3 author_stats.py -C /path/to/repo   # another repo
python3 author_stats.py abc1234..HEAD      # only commits after abc1234
                                             # (skip pre-AI history)
"""

import argparse
import subprocess
import sys

__version__ = "1.0.0"

# git trailer keys are conventionally case-insensitive; match accordingly.
_TRAILER_PREFIX = "co-authored-by:"

# NUL as field separator. Git refuses to commit text containing NUL, so this
# is guaranteed not to appear inside %H / %an / %ad / %s / %B. Earlier
# versions used 0x1f / 0x1e (ASCII unit / record separators), which broke on
# the rare commit whose own message documented those bytes verbatim — e.g.
# a commit titled "strip stray control bytes" that quotes raw 0x1f / 0x1e in
# its body. `git log -z` puts a NUL between commits, %x00 puts NUL between
# fields; one `split(b'\x00')` recovers every field cleanly.
_SEP = b"\x00"


def git_log_records(repo, revrange):
    """Return [(hash, author, email, date, subject, body), ...] newest-first.

    One `git log` call (no per-commit subprocess). Cross-platform: relies
    only on git + stdlib, no POSIX-only calls."""
    # %x00 emits a literal NUL between fields; -z emits a literal NUL between
    # commits. Six fields per commit → six NULs per commit in the stream.
    fmt = "%H%x00%an%x00%ae%x00%ad%x00%s%x00%B"
    cmd = ["git"]
    if repo:
        cmd += ["-C", repo]
    cmd += ["log", "-z", f"--format={fmt}", "--date=short"]
    if revrange:
        cmd.append(revrange)
    try:
        proc = subprocess.run(cmd, capture_output=True, check=True, timeout=120)
    except FileNotFoundError:
        print("ERROR: git not found on PATH.", file=sys.stderr)
        sys.exit(2)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or b"").decode("utf-8", "replace").strip()
        print(f"ERROR: git log failed: {detail}", file=sys.stderr)
        sys.exit(2)

    # Split on NUL bytes BEFORE decoding so any UTF-8 oddities in field
    # content can't corrupt the separator search.
    tokens = proc.stdout.split(_SEP)
    # The stream ends with a trailing NUL after the last commit's body, so
    # the final token is empty. Drop it.
    if tokens and not tokens[-1]:
        tokens.pop()

    records = []
    # Every six tokens form one commit. If git ever emits a malformed
    # stream, the tail is silently dropped (defensive).
    for i in range(0, len(tokens) - len(tokens) % 6, 6):
        h, an, ae, ad, s, body = (
            tokens[i].decode("utf-8", "replace"),
            tokens[i + 1].decode("utf-8", "replace"),
            tokens[i + 2].decode("utf-8", "replace"),
            tokens[i + 3].decode("utf-8", "replace"),
            tokens[i + 4].decode("utf-8", "replace"),
            tokens[i + 5].decode("utf-8", "replace"),
        )
        records.append((h, an, ae, ad, s, body))
    return records


def tally_authors(records):
    """{'Name <email>': commit count} — the AUTHOR field, not trailers.

    Distinct from the co-author tally: this is who git records as having
    written each commit. An agent showing up here rather than in a
    Co-Authored-By trailer is the misattribution `reauthor.py --reauthor`
    exists to correct."""
    out = {}
    for rec in records:
        key = f"{rec[1]} <{rec[2]}>"
        out[key] = out.get(key, 0) + 1
    return out


def coauthor_trailers(body):
    """Return the list of Co-Authored-By trailer values in a commit body.

    Empty list means the commit carries no trailer (not co-authored). A
    commit may legitimately have more than one."""
    out = []
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith(_TRAILER_PREFIX):
            out.append(stripped[len(_TRAILER_PREFIX):].strip())
    return out


def main():
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")  # pyright: ignore[reportAttributeAccessIssue]
        except (AttributeError, ValueError, OSError):
            pass  # not a reconfigurable stream (redirected/piped)
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--version", action="version", version=f"author_stats {__version__}")
    ap.add_argument(
        "revrange",
        nargs="?",
        default=None,
        help="optional git revision range (e.g. abc1234..HEAD); default is "
        "all history",
    )
    ap.add_argument(
        "--list",
        action="store_true",
        dest="do_list",
        help="list every commit with its trailer (or MISSING)",
    )
    ap.add_argument(
        "--missing",
        action="store_true",
        dest="do_missing",
        help="list only commits with no Co-Authored-By trailer",
    )
    ap.add_argument(
        "-C",
        "--repo",
        metavar="PATH",
        default=None,
        help="run against the repo at PATH instead of the current directory",
    )
    args = ap.parse_args()

    records = git_log_records(args.repo, args.revrange)
    total = len(records)
    if total == 0:
        print("No commits in range.")
        return 0

    missing = []          # (hash, author, date, subject)
    by_trailer = {}        # trailer value -> commit count ("(none)" for gaps)
    for h, author, _email, date, subject, body in records:
        trailers = coauthor_trailers(body)
        if not trailers:
            missing.append((h, author, date, subject))
            by_trailer["(none)"] = by_trailer.get("(none)", 0) + 1
        else:
            # A multi-trailer commit counts once per distinct trailer.
            for trailer in set(trailers):
                by_trailer[trailer] = by_trailer.get(trailer, 0) + 1

    coauthored = total - len(missing)
    authors = tally_authors(records)
    print(f"{total} commits, {coauthored} co-authored, {len(missing)} missing")
    print()
    print(f"By author ({len(authors)} distinct; the git author field):")
    for who, count in sorted(authors.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"  {count:>5}  {who}")
    print()
    print("By trailer (commits; a commit may carry more than one):")
    for trailer, count in sorted(by_trailer.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"  {count:>5}  {trailer}")

    if args.do_list:
        print()
        print("ALL COMMITS:")
        for h, author, _email, date, subject, body in records:
            trailers = coauthor_trailers(body)
            tag = trailers[0] if trailers else "MISSING"
            if len(trailers) > 1:
                tag += f" (+{len(trailers) - 1})"
            print(f"  {h[:9]}  {date}  {tag:<40.40}  {subject}")

    if args.do_missing:
        print()
        print("MISSING TRAILER:")
        if not missing:
            print("  (none)")
        for h, author, date, subject in missing:
            print(f"  {h[:9]}  {date}  {author:<18.18}  {subject}")

    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
