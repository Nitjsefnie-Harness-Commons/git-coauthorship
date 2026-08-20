"""Git co-authorship audit and history-rewrite tools.

The wheel contains two complementary command-line tools: `reauthor` rewrites
co-author trailers and git identities, while `author_stats` reports author and
co-author coverage without changing history.  Each module keeps its own
version for its command's `--version` output; the value here is the
distribution version setuptools reads.
"""

__version__ = "1.0.0"
