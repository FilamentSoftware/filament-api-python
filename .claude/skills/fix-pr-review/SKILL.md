---
name: fix-pr-review
description: Fix PR review comments from GitHub, including CodeRabbit and Codex Review conversations. This handles inline review threads, outside-diff-range comments, and nitpicks.
---

# Fix CodeRabbit GitHub Reviews

## Summary

Fix CodeRabbit review comments from a GitHub PR. This handles inline review threads, outside-diff-range comments, and nitpicks.

## Instructions

### Preamble: API Rate Limit Routing

Before making `gh` CLI calls, determine the preferred API mode (the `rate_limit` endpoint is free):

```bash
REST_USED=$(gh api rate_limit --jq '(.resources.core.limit - .resources.core.remaining) * 100 / .resources.core.limit' 2>/dev/null || echo "0")
GQL_USED=$(gh api rate_limit --jq '(.resources.graphql.limit - .resources.graphql.remaining) * 100 / .resources.graphql.limit' 2>/dev/null || echo "0")
# Tie-break: when usage is equal, prefer REST (simpler, cacheable)
PREFER_REST=$([ "${REST_USED%.*}" -le "${GQL_USED%.*}" ] && echo true || echo false)
```

Use `$PREFER_REST` to choose between REST and GraphQL variants for dual-mode commands below. Commands that are already REST (`gh api repos/...`) or GraphQL-only (`gh api graphql`, review thread queries, mutations) are unaffected.

### Phase 1: Gather Review Items

1. Get the current PR number:

   ```bash
   if [ "$PREFER_REST" = "true" ]; then
     BRANCH=$(git rev-parse --abbrev-ref HEAD)
     PRS_JSON=$(gh api "repos/{owner}/{repo}/pulls?head={owner}:$BRANCH&state=open")
     PR_COUNT=$(printf '%s' "$PRS_JSON" | jq 'length')
     if [ "$PR_COUNT" -ne 1 ]; then
       echo "Expected exactly one open PR for branch '$BRANCH', found $PR_COUNT" >&2
       exit 1
     fi
     printf '%s' "$PRS_JSON" | jq -r '.[0].number'
   else
     gh pr view --json number -q '.number'
   fi
   ```

2. Fetch CodeRabbit review data:

   ```bash
   # Get UNRESOLVED review threads with ALL comments (inline comments)
   # Filter: isResolved == false AND first comment author is coderabbitai
   gh api graphql -f query='
   query {
     repository(owner: "{owner}", name: "{repo}") {
       pullRequest(number: PR_NUMBER) {
         reviewThreads(first: 50) {
           nodes {
             id
             isResolved
             path
             line
             comments(first: 10) {
               nodes {
                 id
                 body
                 author { login }
               }
               pageInfo {
                 hasNextPage
                 endCursor
               }
             }
           }
         }
       }
     }
   }'
   # Then filter in your processing: only threads where isResolved=false
   # AND comments[0].author.login == "coderabbitai"
   ```

   **Paginate thread comments**: If any thread's `comments.pageInfo.hasNextPage`
   is true, fetch additional pages until all comments are retrieved:

   ```bash
   # Fetch next page of comments for a specific thread
   gh api graphql -f query='
   query {
     node(id: "THREAD_ID") {
       ... on PullRequestReviewThread {
         comments(first: 10, after: "END_CURSOR") {
           nodes {
             id
             body
             author { login }
           }
           pageInfo {
             hasNextPage
             endCursor
           }
         }
       }
     }
   }'
   # Repeat until hasNextPage is false, appending nodes to the thread's comment list
   ```

   **Read the full conversation**: All comments in a thread matter, not just the
   initial CodeRabbit comment. Human replies may clarify, override, or reject the
   suggestion (e.g., "this finding is not needed", "use approach X instead").
   When deciding how to fix each item, consider the entire thread conversation —
   if a maintainer has replied with context or a rejection, respect that over
   the original suggestion.

   ```bash
   # Get LATEST review body only (contains Outside Diff Range + Nitpicks)
   # Sort by submitted_at and take the last one
   gh api repos/{owner}/{repo}/pulls/PR_NUMBER/reviews \
     --jq '[.[] | select(.user.login == "coderabbitai[bot]")] | sort_by(.submitted_at) | last | .body'
   ```

   **Important**: Only process UNRESOLVED threads. Resolved threads are from previous
   reviews and should be ignored.

3. Create `.claude/temp/coderabbit-gh-review.md` with this structure:

```markdown
# CodeRabbit GitHub Review - PR #XXX

## Review Threads (Inline Comments)

Items that have GitHub thread IDs - will reply individually.

- [ ] **{file}:{line}** | Thread: `{thread_id}`
  - **Issue**: {description}
  - **Suggested fix**: {suggestion}
  - **Thread context**: {summary of any replies — human overrides, rejections, or clarifications}
  - **Status**: PENDING
  - **Fix notes**:
  - **Commit**:

## Outside Diff Range Comments

Items embedded in review body - cannot be replied to as threads.

- [ ] **{file}:{lines}** | Severity: {Major/Minor}
  - **Issue**: {description}
  - **Suggested fix**: {suggestion}
  - **Status**: PENDING
  - **Fix notes**:
  - **Commit**:

## Nitpick Comments

Lower priority suggestions.

- [ ] **{file}:{lines}**
  - **Issue**: {description}
  - **Suggested fix**: {suggestion}
  - **Status**: PENDING
  - **Fix notes**:
  - **Commit**:

## Cross-Reference Check

Verify against "🤖 Fix all issues with AI agents" section:

- [ ] All items accounted for
```

4. Parse the review body to extract:
   - `⚠️ Outside diff range comments (N)` section
   - `🧹 Nitpick comments (N)` section
   - `🤖 Fix all issues with AI agents` section (for cross-reference)

### Phase 2: Fix Issues

1. For each item, spawn a subagent:

   ```
   Task(subagent_type="general-purpose", prompt="Fix: {item details}")
   ```

2. As fixes complete, update the document:
   - Mark checkbox `[x]`
   - Set **Status**: `FIXED` or `SKIPPED`
   - Add **Fix notes**: What was done (or why skipped)
   - Leave **Commit**: blank for now

3. Continue until all items are processed.

### Phase 3: User Review

**STOP HERE** and present a summary to the user with two tables:

**GitHub Posts** — what will be posted to the PR after committing:

| #   | Thread                                          | Reply                              |
| --- | ----------------------------------------------- | ---------------------------------- |
| 1   | service.py stale IDs (PRRT_kw...)  | [From Claude][FIXED] Refactored... |
| 2   | handlers.py marks (PRRT_kw...)     | [From Claude][SKIPPED] Aligned...  |

If there are outside-diff-range or nitpick items (not tied to a thread), also show
a preview of the aggregated PR comment that will be posted for those items.

**Commits:**

| #   | File                            | Issue             | Status  | Notes                   |
| --- | ------------------------------- | ----------------- | ------- | ----------------------- |
| 1   | service.py:137    | None check order  | FIXED   | Reordered checks        |
| 2   | service.py:588    | Description limit | SKIPPED | Intentional for testing |

Ask: "Ready to commit and post to GitHub?"

### Phase 4: Commit Each Fix

#### Commit Granularity

When a file has multiple fixes:

1. Check if hunks are separable: `git diff <file> | grep "^@@" | wc -l`
2. If multiple hunks: stage individually with `git add -p` (pipe responses)
3. If single hunk (fixes overlap): combine into one commit, note in fix notes

#### Commit Message Rules

See `.claude/skills/commit/SKILL.md` for format and rules.

#### Commit Process

For each FIXED item (not SKIPPED):

1. Stage only the files/hunks changed for that fix
2. Commit with format:

   ```
   fix({scope}): coderabbit review fix

   {Paste the full item from the document including issue and fix notes}
   ```

3. Record the commit hash in the document under **Commit**: `{hash}`

Example:

```bash
git add src/services/service.py
git commit -m "$(cat <<'EOF'
fix(service): coderabbit review fix

**service.py:137** | Thread: PRRT_kwDOM2fKEc5sBTEK
- **Issue**: None check order misses edge case
- **Fix notes**: Reordered None checks to handle empty collections first

(Authored by Claude during CodeRabbit Review)
EOF
)"
```

### Phase 5: Post to GitHub

#### 5a. Reply to Review Threads

For each item in "Review Threads" section, reply using GraphQL:

```bash
gh api graphql -f query='
mutation {
  addPullRequestReviewThreadReply(input: {
    pullRequestReviewThreadId: "THREAD_ID",
    body: "[From Claude][FIXED] Reordered null checks to handle empty collections first.\n\n_commit: abc123f_"
  }) {
    comment { id }
  }
}'
```

Reply format:

```
[From Claude][{FIXED | SKIPPED}] {Details - one line or formatted markdown}

commit: {commit_hash}
```

- For SKIPPED items, omit the commit line
- Keep commit hash bare (no surrounding punctuation) for GitHub auto-linking

#### 5b. Post PR Comment for Other Items

For Outside Diff Range and Nitpick items, post a single PR comment using tables:

```bash
gh pr comment PR_NUMBER --body "$(cat <<'EOF'
@coderabbitai [From Claude]

## Outside Diff Range Fixes

| File | Action | Notes |
|------|--------|-------|
| `service.py:88-112` | FIXED<br>def456g | Removed unnecessary type: ignore. Refactored to use proper typing. |
| `handlers.py:100` | SKIPPED | Intentional for testing purposes |

## Nitpick Fixes

| File | Action | Notes |
|------|--------|-------|
| `model.py:1-6` | FIXED<br>ghi789h | Added docstrings |

EOF
)"
```

#### 5c. Resolve Review Threads

After replying, resolve each thread using GraphQL:

```bash
gh api graphql -f query='
mutation {
  resolveReviewThread(input: {
    threadId: "THREAD_ID"
  }) {
    thread { isResolved }
  }
}'
```

### Phase 6: Cleanup

Delete the working document:

```bash
rm .claude/temp/coderabbit-gh-review.md
```

## Notes

- **Resolve threads after replying** - Keeps the PR clean
- **Always post details** - Whether FIXED or SKIPPED, explain what/why
- **One commit per fix** - Even for small changes like typo fixes
- **Cross-check completeness** - Use the "🤖 Fix all issues" section to verify nothing was missed
- **Note verbosity** - Simple fixes can have brief notes, but complex changes (refactors, architectural decisions, moving code between files) should have fuller explanations. Don't over-compress notes that involve design decisions or multi-step changes.

## Persistence

- Follow phases 1-3 without stopping
- **Stop at Phase 3** for user review before commits
- Proceed to phases 4-6 after user approval (no separate review for GitHub posts)
