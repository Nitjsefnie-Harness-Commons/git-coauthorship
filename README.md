# git-coauthorship

Audit and rewrite Co-Authored-By and author trailers in agent-authored git
history.

## Install

Not on PyPI. Every release publishes the wheel with a `SHA256SUMS` file beside
it, and checking against it is the point: fetching "the newest release" is
otherwise a promise about a URL, not about the artifact CI built.

```sh
gh release download v1.0.0 --repo Nitjsefnie-Harness-Commons/git-coauthorship
sha256sum -c SHA256SUMS
pip install ./git_coauthorship-1.0.0-py3-none-any.whl
```

| Tool | Purpose |
|---|---|
| `reauthor` | Fill or rewrite co-author trailers and author/committer identities |
| `author-stats` | Report author tallies, co-author trailers, and missing trailers |

Each takes `--help`, and each reports its own version with `--version`.
