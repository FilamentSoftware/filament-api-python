---
name: create-pr
description: Create a draft PR with changelog validation, issue linking, and project board updates
allowed-tools: Bash, Skill
---

# Create Draft Pull Request

Create a draft pull request in `FilamentSoftware/filament-api-python` with changelog validation, automatic issue linking, and project board status update.

**Creates DRAFT PRs by default** — this allows code reviews to run before wasting CI minutes on action workflows. Exception: when `skip ci` is specified, creates a regular (non-draft) PR since CI won't run anyway.

## Arguments

- `$ARGUMENTS` (optional) - Issue number to link (e.g., `621`). If omitted, detected from branch name or commit history. If the user says to skip CI (e.g. `/create-pr skip ci`), append ` [skip ci]` to the PR title.

## Execution Guidance

Minimize tool calls by batching independent operations:

- **Phase 1**: when finding the issue number, fetch the issue title in the same step (one `gh issue view` call returns both)
- **Phase 4**: run `git log` and `git diff --name-only` in parallel for body generation
- **CHANGELOG**: run the CHANGELOG `git log` once and reuse the output for both the check (Phase 3) and the body section (Phase 4) — do not query twice
- **Write body to temp file** (`/tmp/pr-body.md`) to avoid shell escaping issues with backticks in markdown

## Instructions

### Preamble: API Rate Limit Routing

Before making `gh` CLI calls, determine the preferred API mode (the `rate_limit` endpoint is free):

```bash
REST_USED=$(gh api rate_limit --jq '(.resources.core.limit - .resources.core.remaining) * 100 / .resources.core.limit' 2>/dev/null || echo "0")
GQL_USED=$(gh api rate_limit --jq '(.resources.graphql.limit - .resources.graphql.remaining) * 100 / .resources.graphql.limit' 2>/dev/null || echo "0")
# Tie-break: when usage is equal, prefer REST (simpler, cacheable)
PREFER_REST=$([ "${REST_USED%.*}" -le "${GQL_USED%.*}" ] && echo true || echo false)
```

Use `$PREFER_REST` to choose between REST and GraphQL variants for dual-mode commands below. Commands that are already REST (`gh api repos/...`) or GraphQL-only (`gh api graphql`, `gh project *`) are unaffected.

### Phase 1: Find Linked Issue

Determine the issue number for this PR. Check in order:

1. If provided as `$ARGUMENTS`, use that
2. Check branch name for number prefix (e.g., `621-some-branch` → `#621`)
3. Search commit messages for `closes #XXX` pattern:

   ```bash
   git log origin/develop..HEAD --oneline | grep -ioE '(close[sd]?|fix(e[sd])?|resolve[sd]?) #[0-9]+' | head -1 | sed -E 's/.*#([0-9]+).*/\1/'
   ```

4. If no issue found, ask the user: "Which issue does this PR close? (number or skip)"

Once the issue number is known, fetch the title immediately (reuse later for body):

```bash
# Note: replace <number> with the actual issue number variable
if [ "$PREFER_REST" = "true" ]; then
  gh api repos/FilamentSoftware/filament-api-python/issues/<number> --jq '.title'
else
  gh issue view <number> --repo FilamentSoftware/filament-api-python --json title --jq '.title'
fi
```

### Phase 2: Rebase

Invoke the `/rebase` skill to fetch develop and rebase onto it.

### Phase 3: CHANGELOG Check

Check for CHANGELOG commits and extract lines in a single call (reuse output in Phase 4):

```bash
git log origin/develop..HEAD --grep="^CHANGELOG:" --format="%B"
```

If this returns output, extract `CHANGELOG:` lines from it. If empty, no CHANGELOG commits exist.

**Include CHANGELOG for:**

- Features that affect what users see or interact with (UI changes, new screens, behavior changes)
- Bug fixes that users would notice
- Significant app changes (deployment config, infrastructure, CI/CD) that affect how the app runs or is delivered

**Repository policy:** Every PR branch must include at least one commit with a `CHANGELOG:` line (except dependabot branches).

If no `CHANGELOG:` line is found in any commit on the branch:

1. Stop and propose a CHANGELOG commit message to the user:
   - Ask the user for ideas and provide possible options
   - Every CHANGELOG must be descriptive — never use `CHANGELOG: no user-facing changes`
2. Format: `{type}({scope}): add CHANGELOG line for {branch-description}`
3. Body must include one or more `CHANGELOG:` lines
4. Wait for user approval before creating the commit
5. Create the commit (empty/no-files): `git commit --allow-empty -m "..."`
6. Push: `git push`
7. Then proceed with PR creation

CHANGELOG line format: `CHANGELOG: {factual description of user-facing change}` or `CHANGELOG: no user-facing changes`

Multiple CHANGELOG lines are allowed for multiple distinct changes.

### Phase 4: Draft PR Proposal

#### Generate Title

Convert branch name to title:

- If branch has number prefix: `[#{number}] {Title Case Rest}` (e.g., `621-add-schedule-sorting` → `[#621] Add Schedule Sorting`)
- If no number prefix: `{Title Case Rest}` (e.g., `add-schedule-sorting` → `Add Schedule Sorting`)

Dashes become spaces, each word title-cased.

#### Generate Body

Run `git log origin/develop..HEAD --oneline` and `git diff origin/develop..HEAD --name-only` in parallel, then build:

1. **tldr;** — One bold sentence summarizing the PR's purpose
2. **Summary** — Bullet points summarizing the changes from commits
3. **CHANGELOG** — If any CHANGELOG lines were found in Phase 3, list them as bullets (strip the `CHANGELOG:` prefix). Omit this section entirely if none exist.
4. **Issue link** — `_Closes #{number} {issue title}_` at the bottom (italicized, using the title fetched in Phase 1)

#### Present Proposal

Show in chat:

1. **CHANGELOG entries** as a bulleted list (or "none — developer-facing changes only")
2. A horizontal rule (`---`)
3. **PR title**
4. **Full PR body** preview

Ask: "Create this draft PR?"

### Phase 5: Wait for Approval

Only proceed after receiving **explicit approval** such as "yes", "approved", "go ahead", or "do it".

- Corrections or feedback are **not approval** — update and present again
- If the user's response is ambiguous, ask: "Should I create this draft PR?"

### Phase 6: Create PR

Write the body to `/tmp/pr-body.md` first, then create:

- If `skip ci` was specified: create a **regular** (non-draft) PR (omit `--draft`)
- Otherwise: create a **draft** PR

```bash
gh pr create \
  [--draft]  # omit when skip ci
  --base develop \
  --title "{title}" \
  --body-file /tmp/pr-body.md
```

### Phase 7: Post-Creation — Update Issue Status

If an issue was linked (Phase 1), set its project board status to "In Review":

1. Get the project item ID:

   ```bash
   ITEM_ID=$(gh api graphql \
     -f query='{ repository(owner: "FilamentSoftware", name: "filament-api-python") {
       issue(number: {ISSUE_NUMBER}) {
         projectItems(first: 20) {
           nodes { id project { id } }
         }
       }
     } }' \
     --jq '.data.repository.issue.projectItems.nodes[] | select(.project.id == "PVT_kwDOBykWts4AkSPD") | .id')
   ```

2. Guard against empty ITEM_ID, then set status to "In Review" (option ID: `df73e18b`):

   ```bash
   if [ -z "$ITEM_ID" ]; then
     echo "No project item found for this issue — skipping project board update."
   else
     gh project item-edit \
       --id "$ITEM_ID" \
       --project-id PVT_kwDOBykWts4AkSPD \
       --field-id PVTSSF_lADOBykWts4AkSPDzgchC84 \
       --single-select-option-id df73e18b
   fi
   ```

### Phase 8: Report + Wait for Reviews

Report:

- Draft PR URL (clickable)
- CHANGELOG entries (bulleted, if any)
- Issue status update confirmation (if applicable)

Example:

```text
Draft PR: https://github.com/FilamentSoftware/filament-api-python/pull/XXX
Issue #621 status: In Review
```

Then invoke `/wait-then-fix-pr-review {PR number}` to poll for reviews and auto-fix them. **Skip** this step only if all changed files are in non-reviewed paths: `.claude/` or `README.md` files.
