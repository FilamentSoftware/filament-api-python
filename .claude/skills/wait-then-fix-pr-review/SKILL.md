---
name: wait-then-fix-pr-review
description: Poll for PR review comments, wait for all batches, then auto-fix
allowed-tools: Bash, Skill
---

# Wait for Reviews Then Fix

Poll a PR for review comments (e.g., CodeRabbit). Once detected, wait for all batches to arrive, then invoke `/fix-pr-review` to address them.

## Arguments

- `$ARGUMENTS` (required) - PR number (e.g., `622`)

## Instructions

### Phase 1: Poll for Reviews

Run a blocking poll that checks for inline review comments every 15 seconds, for up to 8 minutes. Once the first comment is detected, wait 2 minutes for remaining batches (CodeRabbit often posts in multiple rounds), then exit.

Use a single Bash call with **timeout 600000** (10 minutes):

```bash
PR_NUM="$ARGUMENTS"
REPO="FilamentSoftware/filament-python-api"

if [ -z "$PR_NUM" ] || ! [[ "$PR_NUM" =~ ^[0-9]+$ ]]; then
  echo "Error: PR number is required and must be numeric. Got: '$PR_NUM'"
  exit 1
fi

echo "Polling PR #$PR_NUM for review comments (every 15s, up to 8 min)..."

for i in $(seq 1 32); do
  if ! COUNT=$(gh api "repos/$REPO/pulls/$PR_NUM/comments" --jq 'length' 2>/dev/null); then
    echo "Error: failed to query PR comments (gh api). Check GitHub auth and repo access."
    exit 1
  fi
  if [ "$COUNT" -gt 0 ]; then
    echo "Found $COUNT review comment(s) at $(date +%H:%M:%S)."
    echo "Waiting 2 minutes for remaining batches..."
    sleep 120
    if ! FINAL=$(gh api "repos/$REPO/pulls/$PR_NUM/comments" --jq 'length' 2>/dev/null); then
      echo "Error: failed to re-query PR comments after wait window."
      exit 1
    fi
    echo "Final count: $FINAL review comment(s). Proceeding to fix."
    exit 0
  fi
  sleep 15
done

echo "No review comments found after 8 minutes."
exit 1
```

### Phase 2: Fix Reviews

- If Phase 1 **succeeded** (reviews found): invoke `/fix-pr-review` to address the comments
- If Phase 1 **timed out** (exit 1): report "No reviews arrived within 8 minutes" and stop
