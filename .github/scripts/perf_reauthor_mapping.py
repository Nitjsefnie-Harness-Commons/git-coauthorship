#!/usr/bin/env python3
"""Build `reauthor --older`'s gap mapping in the current repository as its
main() does, stopping before the git-filter-repo rewrite; perf_budget.py runs
it beside a copy of the package."""
from git_coauthorship import reauthor

mapping = reauthor.build_mapping(reauthor.get_all_commits(), 0, None,
                                 reauthor.build_trailer("Claude Opus 4.6", "noreply@anthropic.com"))
print(f"mapping {len(mapping)}")
