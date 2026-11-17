# git-coauthorship

Audit and rewrite Co-Authored-By and author trailers in agent-authored git
history.

## Install

Not on PyPI. Every release publishes the wheel with a `SHA256SUMS` file beside
it, and checking against it is the point: fetching "the newest release" is
otherwise a promise about a URL, not about the artifact CI built. Replace
`<version>` with the version number of the release to install, without the
leading `v`: `1.1.1` for the release tagged `v1.1.1` on the Releases page.

```sh
gh release download v<version> --repo Nitjsefnie-Harness-Commons/git-coauthorship
sha256sum -c SHA256SUMS
pip install ./git_coauthorship-<version>-py3-none-any.whl
```

| Tool | Purpose |
|---|---|
| `reauthor` | Fill or rewrite co-author trailers and author/committer identities |
| `author-stats` | Report author tallies, co-author trailers, and missing trailers |

Each takes `--help`, and each reports its own version with `--version`.

## Development

```sh
python3 run_tests.py                                             # the suite
git ls-files -co --exclude-standard '*.py' | xargs pylint        # lint
git ls-files -co --exclude-standard '*.py' | xargs pycodestyle   # lint
pyright                                                          # types
pip-audit -r requirements-dev.txt -r requirements-test.txt       # audit
actionlint .github/workflows/*.yml && zizmor .github/workflows/  # workflows
```

`pip install -r requirements-dev.txt -r requirements-test.txt` gets the
pinned toolchain. Use `-co --exclude-standard`, not a bare `git ls-files`:
a brand-new module is untracked until you stage it, and pylint would
otherwise report a clean run over every file except the one you just
wrote.

Coverage:

```sh
for s in tests/test_*.py; do
  python3 -m coverage run --parallel-mode --source=git_coauthorship "$s"
done
python3 -m coverage combine && python3 -m coverage report
```

Each suite in its own subprocess, because that is what `run_tests.py`
does — measuring a different execution shape than CI runs would report
coverage for a program nobody executes. Gated at **33%**, a ratchet
under the current 35.2%, not a target. Raise it as coverage climbs;
never lower it to turn a build green.

### CI

| Workflow | What it does |
| --- | --- |
| `tests` | `run_tests.py` across every OS × Python pair, plus a single-run coverage job — the matrix would otherwise report the same coverage number once per pair. |
| `lint` / `types` | pylint + pycodestyle, and pyright. |
| `codeql` | Security analysis (Python only — no JS here). Findings go to the Security tab, never the build. Weekly cron on top of push, because a query published today would otherwise only ever run against files touched after it shipped. |
| `audit` | `pip-audit` over both requirements files, resolving the full transitive tree. **Daily** cron: this answers "is a version we froze months ago still safe", and that answer changes with no commit here to hang it on. |
| `actionlint` | Every pull request, checked out at its head rather than GitHub's test merge, and every push to `main` (a push or manual dispatch checks out the event's own commit). `actionlint` + `zizmor` over the workflow YAML: a broken workflow does not go red, it silently stops running. Before them, the `.github/scripts` suites (`python3 -m unittest discover -s .github/scripts -p 'test_*.py'`) and three gate-integrity checks: no tracked file carries a merge-conflict marker; the head carries every commit `main` gained on a file a required check reads (`gate_base_freshness.py`); no outgoing commit (a pull request's `origin/main..HEAD`, a push's `before..HEAD`) pairs a workflow-name scope with a type other than `ci` (`commit_scopes.py`). Last, a digest-verified gitleaks scan of the checked-out head's full history. No aggregate check exists, so this job hosts the scan: requiring its `actionlint` check makes the scan gate merges. |
| `secrets` | Daily cron only: the same gitleaks scan over every branch's history, for commits no pull request or `main` push ran it on. |
| `tag` | Runs on every push to `main` the gates run on. When `__version__` has no `v<version>` tag yet, it waits for every other check on that commit and then pushes the tag, so a bump whose own commit was red is tagged by the later commit that turns `main` green. A push that leaves an already-tagged version unchanged ends green without tagging. |
| `release` | Fires on that tag: runs every suite, builds the wheel, and publishes `SHA256SUMS` beside it. |
| `claim` | `Nitjsefnie-Actions/claim`: `/claim` assigns the commenter to an open, unassigned issue; `/unclaim` (or `/release`) removes the commenter's own assignment. |
| `pr gate` | `Nitjsefnie-Actions/pr-gate`: checks a pull-request description against `.github/PULL_REQUEST_TEMPLATE.md` and its assigned issues, plus commit attribution and Conventional Commits subjects. |
| `comment share` | `Nitjsefnie-Actions/comment-ratchet`: gates each tracked source file's comment share (target 15%) against `.github/comment-share.json`; the default branch commits its own tightened records. |
| `size baseline` | `Nitjsefnie-Actions/size-ratchet`: gates file line counts against per-type ceilings and `.github/size-baseline.json`, and budgets the `tests/` tree's total; the default branch commits its own records. |
| `pin test screen` | `Nitjsefnie-Actions/pin-guard`: lists test lines that assert on a pinned dependency version as review comments; not a gate. |

**There is deliberately no speed gate here**, unlike the dashboard repos.
pytest cannot run these suites — the tests are plain functions taking
helper arguments, which pytest reads as fixture requests — and the
comparator needs `--junitxml`, which only pytest emits. Rather than
reshape the tests to suit a gate, the gate is omitted.

**Releasing = bumping `__version__`.** `tag` creates the tag, `release`
publishes from it. Nothing bumps the version automatically: deciding
patch-vs-minor is a judgement about what changed.

**Actions are hash-pinned**, with the version in a trailing comment. Do
not "tidy" one back to `@v4`: a tag is a moving pointer, and these jobs
run with a repository token. Dependabot keeps the hashes current.

**`.gitignore` is deny-by-default**: `*` first, then each shipped path
named back. `build/`, `dist/` and `*.egg-info/` need no rules at all now —
they are simply never named back. A new file of an unlisted type is
invisible to git and will NOT appear in `git status`;
`git check-ignore -v <path>` names the rule hiding it. Never "fix" it by
loosening the leading `*`.
