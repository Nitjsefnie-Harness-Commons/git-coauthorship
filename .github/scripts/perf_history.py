#!/usr/bin/env python3
"""Build the performance gate's history with git fast-import: the same names,
emails, dates and messages on every run, so every run makes the same commits."""
import argparse
import subprocess
import sys
from pathlib import Path

COMMITS = 20000
START = 1700000000
AUTHORS = (("Ada Lovelace", "ada@example.com"), ("Grace Hopper", "grace@example.com"),
           ("Alan Turing", "alan@example.com"),
           ("dependabot[bot]", "49699333+dependabot[bot]@users.noreply.github.com"))
TRAILERS = ("Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>",
            "Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>",
            "Co-authored-by: GPT-5.2 <noreply@openai.com>")


def message(index: int) -> str:
    """Of ten: three without a trailer, one with two, one with the lowercase key, five with one."""
    body = f"change {index}: adjust module {index % 37}\n\nWhy: case {index % 11} needed it.\n"
    kind = index % 10
    lines = {0: [], 1: [], 2: [], 3: TRAILERS[:2], 4: TRAILERS[2:]}.get(kind, [TRAILERS[kind % 2]])
    return body + ("\n" + "\n".join(lines) + "\n" if lines else "")


def stream(commits: int) -> bytes:
    chunks = []
    for index in range(commits):
        name, email = AUTHORS[index % len(AUTHORS)]
        who = f"{name} <{email}> {START + index * 3600} +0000"
        text = message(index).encode("utf-8")
        chunks.append(f"commit refs/heads/main\nmark :{index + 1}\nauthor {who}\ncommitter {who}\n"
                      f"data {len(text)}\n".encode("utf-8") + text
                      + (f"from :{index}\n" if index else "").encode("utf-8") + b"\n")
    return b"".join(chunks)


def build(target: Path, commits: int = COMMITS) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main", str(target)], check=True)
    subprocess.run(["git", "-C", str(target), "fast-import", "--quiet", "--date-format=raw"],
                   input=stream(commits), check=True)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", type=Path)
    parser.add_argument("--commits", type=int, default=COMMITS)
    args = parser.parse_args(argv)
    build(args.target, args.commits)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
