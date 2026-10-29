#!/usr/bin/env bash
# Commit and push the files under data/ (used by the workflows). Usage: scripts/commit_data.sh "message"
# Several workflows push to main, so a rejected push is rebased and retried instead of failing the run.
set -e
git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
git add -A data/
if git diff --cached --quiet; then
  echo "Nothing new to commit."
  exit 0
fi
git commit -m "$1 $(date -u +'%Y-%m-%d %H:%M') UTC"
for i in 1 2 3 4; do
  git pull --rebase -q origin main && git push && exit 0
  sleep $((i * 5))
done
echo "push failed after retries"
exit 1
