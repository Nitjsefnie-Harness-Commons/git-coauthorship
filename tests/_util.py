"""Shared helpers for the git-coauthorship suites.

Not a suite itself -- run_tests.py only loads `test_*.py`.

Stdlib only and OS-agnostic: these suites run on Linux, macOS and Windows in
CI, and a POSIX-only assumption here would be a platform failure reported as a
test failure.
"""
import contextlib
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path
from unittest import mock

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = str(_PROJECT_ROOT / "git_coauthorship")
"""The package directory. The suites load a tool by path rather than by
import so that a broken `__init__` cannot mask a tool defect as an
ImportError."""


def script(name):
    return str(_PROJECT_ROOT / "git_coauthorship" / name)


def load(path, name=None):
    """Import a tool by path without running its __main__ block."""
    name = name or ("mod_" + os.path.splitext(os.path.basename(path))[0]
                    .replace("-", "_"))
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Skipped(Exception):
    """Raised by a test that cannot hold on this platform."""


def skip(reason):
    """End the running test as skipped, with a reason the log will show."""
    raise Skipped(reason)


def require_git():
    if not shutil.which("git"):
        skip("git is not available on PATH")


def git(d, *args, check=True, env=None):
    """git in `d` -> its CompletedProcess, output as text."""
    return subprocess.run(["git", "-C", d, *args], capture_output=True,
                          text=True, check=check, env=env, timeout=60)


def commit(d, message, author, committer=None):
    """Append `message` to f.txt and commit it as ("Name", "mail") `author`
    and `committer` (default: the author) -> the new commit's hash."""
    name, email = committer or author
    env = {**os.environ, "GIT_AUTHOR_NAME": author[0],
           "GIT_AUTHOR_EMAIL": author[1], "GIT_COMMITTER_NAME": name,
           "GIT_COMMITTER_EMAIL": email}
    with open(os.path.join(d, "f.txt"), "a", encoding="utf-8") as f:
        f.write(message)
    git(d, "add", "f.txt")
    git(d, "commit", "-q", "-m", message, env=env)
    return git(d, "rev-parse", "HEAD").stdout.strip()


def repo(d, messages, author, committer=None):
    """A new repository at `d` on `main`, configured as the committer, with
    one commit per message -> their hashes."""
    require_git()
    os.makedirs(d, exist_ok=True)
    name, email = committer or author
    for args in (["init", "-q"], ["checkout", "-q", "-b", "main"],
                 ["config", "user.name", name], ["config", "user.email", email],
                 ["config", "commit.gpgsign", "false"]):
        git(d, *args)
    return [commit(d, message, author, committer) for message in messages]


def call_main(main, argv):
    """Run a tool's main() as `argv` -> (exit status, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(sys, "argv", argv), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = main() or 0
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


def _assertion_site(exc):
    """`file:line: source` of the assert that failed, for bare asserts."""
    frames = traceback.extract_tb(exc.__traceback__)
    if not frames:
        return ""
    last = frames[-1]
    return f"{os.path.basename(last.filename)}:{last.lineno}: {last.line}"


def runner(tests, tmp_prefix="coauthorshiptests_"):
    """Shared main(): run every callable, print PASS/FAIL, return exit code.

    Each test takes one argument: an isolated temp dir, handed over fully
    resolved -- macOS resolves /var to /private/var, and a suite comparing a
    path it was given against a path the code produced would otherwise see two
    spellings of the same directory and call them different.
    """
    failed = []
    skipped = []
    with tempfile.TemporaryDirectory(prefix=tmp_prefix) as tmp:
        tmp = os.path.realpath(tmp)
        for t in tests:
            d = os.path.join(tmp, t.__name__)
            os.makedirs(d, exist_ok=True)
            try:
                t(d)
                print(f"  PASS  {t.__name__}")
            except Skipped as e:
                skipped.append(t.__name__)
                print(f"  SKIP  {t.__name__}: {e}")
            except AssertionError as e:
                failed.append(t.__name__)
                # A bare `assert x == y` carries no message, which on a
                # platform you cannot run locally leaves nothing to go on.
                detail = str(e) or _assertion_site(e)
                print(f"  FAIL  {t.__name__}: {detail}")
            except Exception as e:  # noqa: BLE001
                failed.append(t.__name__)
                print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
    passed = len(tests) - len(failed) - len(skipped)
    summary = f"\n{passed}/{len(tests)} passed"
    if skipped:
        summary += f", {len(skipped)} skipped"
    print(summary)
    return 1 if failed else 0


def collect(namespace):
    return [v for k, v in sorted(namespace.items())
            if k.startswith("test_") and callable(v)]
