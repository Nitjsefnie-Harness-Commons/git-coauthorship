# Contributing to git-coauthorship

Running the suite and the linters is covered by the Development section of
[README.md](README.md#development).

## Commit messages

Conventional Commits: `type(scope)!: subject`. The scope and the `!` are
optional. A breaking change carries the `!`. The type is one of exactly these:

| Type | For |
|---|---|
| `build` | packaging: the build configuration in `pyproject.toml` and what the wheel contains |
| `chore` | housekeeping that fits no other type |
| `ci` | workflows and their gate configuration |
| `docs` | documentation only: `README.md`, this guide, the issue and pull-request forms |
| `feat` | new behaviour of `reauthor` or `author-stats` |
| `fix` | a defect in the behaviour of `reauthor` or `author-stats` |
| `perf` | faster, with the measurement in the commit body |
| `refactor` | no observable change in behaviour |
| `revert` | undoing an earlier commit, which the body names |
| `style` | formatting only, no change in behaviour |
| `test` | the test suites |

The scope names the SUBJECT of the change, not the directory it landed in.
Only these scopes are used:

| Scope | Subject |
|---|---|
| `reauthor` | `git_coauthorship/reauthor.py` and the `reauthor` command |
| `author-stats` | `git_coauthorship/author_stats.py` and the `author-stats` command |
| `harness` | the test machinery: `run_tests.py` and `tests/_util.py` |
| `package` | `pyproject.toml`, the package's `__version__` and packaging |
| `deps` | a version bump of any other pinned dependency or action |
| `deps-dev` | a version bump in `requirements-dev.txt`, the lint and type toolchain |
| `readme` | `README.md` |
| `contributing` | `CONTRIBUTING.md` |
| `templates` | the issue and pull-request forms |
| `repo` | `LICENSE`, `.gitignore` and repository settings |
| a workflow's `name:` | the workflow with that name, with type `ci` ONLY |

A workflow's scope is the top-level `name:` in its YAML, used as written:
`ci(tag)`, `ci(codeql)`, `ci(pr gate)`. A workflow name is never a scope for
another type, `revert` included: revert a workflow change as `ci(<name>)` and
say so in the summary.

`tests` is the name of the tests workflow, so only a change to that workflow
is `ci(tests)`. A change to the test machinery takes `harness`, never `tests`:
`test(harness)`. A new case for a command is `test(reauthor)` or
`test(author-stats)`.

A change that spans two scopes is usually two commits. Where it is not, name
the scope the change is about.

## Issues

File with the issue form (`.github/ISSUE_TEMPLATE/issue.md`). Its section
order is fixed, and its Description is observed behaviour only: what is wrong,
not the cause and not the fix. A cause or a fix goes under Suggested Fix,
marked unverified.

Drop the Reproduction Steps section, header included, when the problem is not
reliably reproducible, and say so in the Description instead.

Claim an issue before you start by commenting `/claim` on an open, unassigned
issue: the `claim` workflow assigns you. The number of issues one contributor
can hold at once is set by repository role.

## Pull requests

Fill in `.github/PULL_REQUEST_TEMPLATE.md`. Name the issue you claimed in its
Related Issues and Pull Requests section. One logical change per commit, in
the form above.
