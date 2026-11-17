#!/usr/bin/env bash
# Release dist/ for GITHUB_REF's tag via a draft; never touch a published one.
set -euo pipefail

case "${GITHUB_REF:-}" in
  refs/tags/*) tag="${GITHUB_REF#refs/tags/}" ;;
  *)
    echo "Not a tag ref (${GITHUB_REF:-}); nothing to publish." >&2
    echo "Push a v* tag — .github/workflows/tag.yml does that" >&2
    echo "automatically when __version__ changes." >&2
    exit 1
    ;;
esac
repo="${GITHUB_REPOSITORY:?}"

# Not `gh release view`: its "not found" can stand for a failed lookup.
releases=$(gh api --paginate "repos/$repo/releases?per_page=100" \
  --jq '.[] | [.tag_name, .draft] | @tsv')

drafts=0
published=0
while IFS=$'\t' read -r name draft; do
  [ "$name" = "$tag" ] || continue
  case "$draft" in
    true) drafts=$((drafts + 1)) ;;
    false) published=$((published + 1)) ;;
    *) echo "Release $tag has an unexpected draft flag '$draft'." >&2; exit 1 ;;
  esac
done <<< "$releases"

if [ "$published" -gt 0 ]; then
  echo "$tag was already published; its assets never change." >&2
  echo "Bump __version__ to release again." >&2
  exit 1
fi
if [ "$drafts" -gt 1 ]; then
  echo "$drafts draft releases for $tag; delete all but one, then rerun." >&2
  exit 1
fi

if [ "$drafts" -eq 0 ]; then
  gh release create "$tag" --repo "$repo" --draft --title "$tag" --generate-notes
else
  echo "Completing the draft release for $tag."
fi
gh release upload "$tag" --repo "$repo" dist/*.whl dist/SHA256SUMS --clobber
gh release edit "$tag" --repo "$repo" --draft=false
echo "Published $tag."
