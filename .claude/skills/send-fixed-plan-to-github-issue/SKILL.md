---
name: send-fixed-plan-to-github-issue
description: Post completed plan doc to its GitHub issue and clean up
allowed-tools: Bash
---

# Send Fixed Plan to GitHub Issue

Post a summarized version of the completed plan doc from `/fix-issue` as a comment on the corresponding GitHub issue, then delete the plan file.

## Workflow

### Preamble: API Rate Limit Routing

Before making `gh` CLI calls, determine the preferred API mode (the `rate_limit` endpoint is free):

```bash
REST_USED=$(gh api rate_limit --jq '(.resources.core.limit - .resources.core.remaining) * 100 / .resources.core.limit' 2>/dev/null || echo "0")
GQL_USED=$(gh api rate_limit --jq '(.resources.graphql.limit - .resources.graphql.remaining) * 100 / .resources.graphql.limit' 2>/dev/null || echo "0")
# Tie-break: when usage is equal, prefer REST (simpler, cacheable)
PREFER_REST=$([ "${REST_USED%.*}" -le "${GQL_USED%.*}" ] && echo true || echo false)
```

Use `$PREFER_REST` to choose between REST and GraphQL variants for dual-mode commands below. Commands that are already REST (`gh api --method POST ...`) are unaffected.

### 1. Find the Plan Doc

1. Extract the issue number from the current branch name (e.g., `457-scope-cost-fix` → `457`)
2. Look for the plan doc at `plans/<issue-number-or-branch-name>.plan.md`
   - Try `plans/<number>.plan.md` first
   - If not found, try `plans/<full-branch-name>.plan.md`
   - If still not found, list `plans/*.plan.md` and ask the user which one to use
3. Read the plan doc contents

### 2. Summarize the Plan Doc

The plan doc is a working document with investigation detail that's too verbose for a GitHub issue. Condense it to two sections: what was wrong, and what was done about it. The PR diff is the authoritative record of file changes.

**Drop entirely** — these sections add no value on the issue:
- `## Original Prompt` — duplicates the `[PROMPT]` comment
- `### Key Files and Their Roles` — investigation breadcrumbs
- `## Files to Modify` — pre-implementation planning
- `## Results` / `### Changes Made` — the PR diff shows this
- `## CodeRabbit Review` — internal QA
- `### Notes` — implementation rationale belongs in commits

**Condense into two sections using note form** — terse bullets that preserve the technical reasoning but drop narrative scaffolding, code blocks, and numbered step-by-step traces:

0. **TL;DR** — Add a single bold line near the top using this exact prefix: `**tl;dr:**`. Write it as a very short plain-language summary focused on readability, not rigid structure.
   - Prefer one sentence (two max only if clarity requires it)
   - Explain what broke for users and what is now fixed
   - Avoid forced wording patterns, long clause chains, and unnecessary jargon
   Example shape: `**tl;dr:** Selecting Custom complexity failed because the required custom record was not created; now it is created and linked correctly.`

1. **Root Cause** — Merge all investigation subsections (Context, Root Cause, Why This Causes the Issue, Consequence, etc.) into bullets. Each bullet should capture one key insight — the specific function/behavior that's wrong, the mechanism that causes the failure, knock-on effects. Keep technical specifics (function names, prop names, event sequences) but drop prose connectors and code snippets.

2. **Fix** — Merge Approach + Changes Made into bullets describing what was done and why. One bullet per distinct change. Include the reasoning (not just "added X" but "added X so that Y").

**Output structure:**

```markdown
## Done! 🎉 Plan Summary

**tl;dr:** <one-sentence summary of failure mechanism and resolution>

### Root Cause

- <key insight as a terse note>
- <mechanism / why it fails>
- <knock-on effects if relevant>

### Fix

- <what was done and why>
- <what was done and why>
```

If the plan doc contains multiple issues (separated by `---`), summarize each one independently and keep them separated by `---`.

### 3. Find the GitHub Issue

1. Fetch the issue:
   ```bash
   if [ "$PREFER_REST" = "true" ]; then
     gh api repos/{owner}/{repo}/issues/<number> --jq '{number, title, url: .html_url}'
   else
     gh issue view <number> --json number,title,url
   fi
   ```
2. If the issue is not found, report the error and stop (early exit)
3. No need to confirm the match with the user unless the situation is ambiguous - issue number/branch name lookups are unambiguous

### 4. Post Comment to GitHub Issue

Post a comment using the template:

```
_[Posted by Claude]_

---

<summarized plan doc contents>
```

Where `<summarized plan doc contents>` must use:
- top header `## Done! 🎉 Plan Summary`
- `**tl;dr:**` directly below that header
- `### Root Cause` and `### Fix` section headers

Write the comment body to a temp file first, then post via API to capture the comment ID:

```bash
if [ "$PREFER_REST" = "true" ]; then
  REPO=$(gh api repos/{owner}/{repo} --jq '.full_name')
else
  REPO=$(gh repo view --json nameWithOwner -q '.nameWithOwner')
fi

COMMENT_FILE="$(mktemp -t plan-comment.XXXXXX.md)"
trap 'rm -f "$COMMENT_FILE"' EXIT

cat <<'COMMENT_EOF' > "$COMMENT_FILE"
_[Posted by Claude]_

---

<summarized plan doc contents>
COMMENT_EOF

COMMENT_ID=$(gh api --method POST "repos/$REPO/issues/<number>/comments" \
  -F body=@"$COMMENT_FILE" --jq '.id')
```

### 5. Verify the Comment

1. Fetch the specific comment by ID: `gh api "repos/$REPO/issues/comments/$COMMENT_ID" --jq '.body' | head -5`
2. Confirm the comment body starts with `_[Posted by Claude]_`
3. Report success or failure to the user

### 6. Delete the Plan Doc (only if step 5 verified successfully)

1. Only proceed if the comment was verified in step 5. If verification failed, stop and report the failure — do not delete the plan doc.
2. Remove the plan file: `rm -- "$PLAN_FILE"`
3. Confirm deletion
