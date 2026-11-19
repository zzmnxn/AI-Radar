#!/usr/bin/env bash
set -euo pipefail

branch="${DEFAULT_BRANCH:?Missing default branch}"
git check-ref-format --branch "$branch" >/dev/null
git add -A -- data/

if git diff --cached --quiet; then
  printf 'head_sha=%s\n' "$(git rev-parse HEAD)" >> "$GITHUB_OUTPUT"
  exit 0
fi

export GIT_AUTHOR_NAME=github-actions
export GIT_AUTHOR_EMAIL=41898282+github-actions[bot]@users.noreply.github.com
export GIT_COMMITTER_NAME="$GIT_AUTHOR_NAME"
export GIT_COMMITTER_EMAIL="$GIT_AUTHOR_EMAIL"
git commit -m "Publish AI Radar snapshot"
pushed_commit=1
if ! git push origin "HEAD:refs/heads/$branch"; then
  git fetch origin "refs/heads/$branch"
  if ! git rebase FETCH_HEAD; then
    git rebase --abort
    exit 1
  fi
  if [[ "$(git rev-parse HEAD)" == "$(git rev-parse FETCH_HEAD)" ]]; then
    pushed_commit=0
  fi
  python -m radar validate-data
  git push origin "HEAD:refs/heads/$branch"
fi

sha="$(git rev-parse HEAD)"
printf 'head_sha=%s\n' "$sha" >> "$GITHUB_OUTPUT"
if [[ "$pushed_commit" == 1 ]]; then
  printf 'commit_sha=%s\n' "$sha" >> "$GITHUB_OUTPUT"
fi
