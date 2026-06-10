---
name: fix-issue
description: Fix a GitHub issue using a plan-driven workflow with autonomous review loops
---

# Fix Issue

Fix a GitHub issue using a plan-driven workflow with autonomous review loops.

## Arguments

- `$1` (optional) - Branch name (e.g., `449-scope-cost-fix`), issue number/URL (e.g., `449` or `https://github.com/.../issues/449`), or issue context/description

## Workflow

### Preamble: API Rate Limit Routing

Before making `gh` CLI calls, determine the preferred API mode (the `rate_limit` endpoint is free):

```bash
REST_USED=$(gh api rate_limit --jq '(.resources.core.limit - .resources.core.remaining) * 100 / .resources.core.limit' 2>/dev/null || echo "0")
GQL_USED=$(gh api rate_limit --jq '(.resources.graphql.limit - .resources.graphql.remaining) * 100 / .resources.graphql.limit' 2>/dev/null || echo "0")
# Tie-break: when usage is equal, prefer REST (simpler, cacheable)
PREFER_REST=$([ "${REST_USED%.*}" -le "${GQL_USED%.*}" ] && echo true || echo false)
```

Use `$PREFER_REST` to choose between REST and GraphQL variants for dual-mode commands below. Commands that are already REST (`gh api repos/...`) or GraphQL-only (`gh api graphql`, `gh project *`, linked branches) are unaffected.

### 1. Determine Branch

Check the current branch and arguments to determine the working branch:

**Case A: Existing branch name given as argument**

The current branch must be `develop` or `main`. Checkout the given branch (it must already exist):

```bash
git checkout <branch-name>
```

**Case B: Issue number or URL given as argument**

The current branch must be `develop` or `main`. Fetch the issue title, derive a concise branch name (kebab-case, prefixed with the issue number, e.g., `449-scope-cost-fix`), and create it:

```bash
git checkout -b <issue-number>-<derived-name>
```

Then link the branch to the existing issue (see step 2) and set the issue status to "In progress".

**Case C: No argument, already on a feature branch (not `develop` or `main`)**

Already checked out and ready to go. Verify the branch is associated with a GitHub issue by extracting an issue number from the branch name (e.g., `449-scope-cost-fix` → `449`) and checking:

```bash
if [ "$PREFER_REST" = "true" ]; then
  gh api repos/{owner}/{repo}/issues/<number> --jq '.number'
else
  gh issue view <number> --json number -q '.number'
fi
```

If no issue is found, proceed to step 2 (Create Issue).

**Case D: No argument, on `develop` or `main`**

If extra context was provided (e.g., a description of what to fix), invent a concise branch name (2-10 words, kebab-case) and create it:

```bash
git checkout -b <branch-name>
```

If no context was provided, ask the user what they'd like to work on.

Then proceed to step 2 (Create Issue) since there's no associated issue yet.

### 2. Link or Create Issue

**If an issue already exists** (Cases A, B, C where issue was found):

Link the current branch to the issue if not already linked, and set the issue status to "In progress":

```bash
# Get issue node ID, repo node ID, owner, and name (adaptive routing)
if [ "$PREFER_REST" = "true" ]; then
  ISSUE_ID=$(gh api repos/{owner}/{repo}/issues/<number> --jq '.node_id')
  read -r REPO_ID REPO_OWNER REPO_NAME <<< "$(gh api repos/{owner}/{repo} --jq '[.node_id, .owner.login, .name] | @tsv')"
else
  ISSUE_ID=$(gh issue view <number> --json id -q '.id')
  read -r REPO_ID REPO_OWNER REPO_NAME <<< "$(gh repo view --json id,owner,name -q '[.id, .owner.login, .name] | @tsv')"
fi

# Get HEAD commit SHA
HEAD_SHA=$(git rev-parse HEAD)

# Check if the branch ref already exists in the repository
BRANCH_NAME=$(git branch --show-current)
REF_EXISTS=$(gh api graphql -f query="
query {
  repository(owner: \"$REPO_OWNER\", name: \"$REPO_NAME\") {
    ref(qualifiedName: \"refs/heads/$BRANCH_NAME\") { id }
  }
}" -q '.data.repository.ref.id // empty')

# Link branch to issue only if the ref does not already exist
if [ -z "$REF_EXISTS" ]; then
  gh api graphql -f query="
mutation {
  createLinkedBranch(input: {
    issueId: \"$ISSUE_ID\",
    name: \"$BRANCH_NAME\",
    oid: \"$HEAD_SHA\",
    repositoryId: \"$REPO_ID\"
  }) {
    linkedBranch { id }
  }
}"
fi
```

**If no issue exists** (Cases C where no issue found, Case D):

Use the `/create-issue` skill to create one. Override the default Status to "In progress". After creation, link the branch using the same method above.

---

### 3. Get Issue Details

1. Extract the issue number from the branch name (e.g., `449-scope-cost-fix` → `449`)
2. Fetch the issue details:
   ```bash
   if [ "$PREFER_REST" = "true" ]; then
     gh api repos/{owner}/{repo}/issues/<number> --jq '{title, body, url: .html_url}'
   else
     gh issue view <number> --json title,body,url
   fi
   ```
   - If the issue is not found, report the error and stop (early exit)
   - No need to confirm the match with the user unless the situation is ambiguous
   - **Field mapping**: The REST `--jq` remaps `html_url` to `url` so both paths return a consistent `url` field
3. Look for a comment starting with `[PROMPT]`: `gh api repos/{owner}/{repo}/issues/<number>/comments --jq '.[] | select(.body | startswith("[PROMPT]")) | .body'`
4. If a `[PROMPT]` comment exists, use its content (after stripping the `[PROMPT]` prefix) as the task description
5. If no `[PROMPT]` comment exists, ask the user what they want done
6. **Create the plan doc** with the original prompt as the first section. Use the Write tool to create the file at `plans/<issue-number-or-branch-name>.plan.md`:

   ```markdown
   # Issue <number>: <title>

   ## Prompt

   <paste the full task description from step 4/5 here verbatim>
   ```

   This ensures the plan doc is self-contained and the investigation agent (and all future readers) can reference the original requirements.

### 4. Spawn Investigation Agent

Use the Task tool with `subagent_type: "general-purpose"`:

````
## Investigate Issue

**Issue**: <issue title and URL>

**Task**: <content from [PROMPT] comment or user description>

**Plan Doc**: plans/<issue-number-or-branch-name>.plan.md

### Instructions

1. Read the plan doc — the original prompt is in the `## Prompt` section.

2. Explore the codebase to understand the issue
   - Search for relevant files
   - Read existing implementations
   - Understand the context and patterns

3. Append to the plan document (after the Prompt section):
   ```markdown
   ## Investigation Findings

   <What you discovered about the codebase, relevant files, patterns>

   ## Approach

   <Your implementation strategy>

   ## Files to Modify

   <List of files you'll change>
   ```
````

DO NOT implement anything - investigation and planning only.

### 5. Review Plan

Read the plan doc and assess:
- Is the approach sound?
- Are there better alternatives?
- Any missing considerations?

If improvements needed, spawn a revision agent:

```

## Revise Plan

**Plan Doc**: <path to plan doc>

**Feedback**:
<specific issues and suggestions>

### Instructions

1. Read the current plan
2. Address the feedback
3. Update the plan doc with improved approach

DO NOT implement anything.

```

Repeat until plan is satisfactory.

### 6. Spawn Implementation Agent

Use the Task tool with `subagent_type: "general-purpose"` (fresh agent with clean context):

````

## Implement Plan

**Plan Doc**: <path to plan doc>

### Instructions

1. Read the plan doc
2. Implement the approach described
3. Update the plan doc with Results section:

   ```markdown
   ---

   ## Results

   ### Changes Made

   <List each file and what was changed>

   ### Notes

   <Important context, decisions made, anything unusual>
   ```

4. Run `poetry run pytest tests/ && poetry run mypy src/` to verify no errors

Changes must not be committed at this stage.

````

### 7. Review Implementation

Review the code changes (`git diff`) and assess:
- Does implementation match the plan?
- Any obvious issues or improvements?
- Does it follow project conventions?

If issues found, spawn a fix agent:

```

## Fix Implementation Issues

**Plan Doc**: <path to plan doc>

**Issues**:
<list of issues found>

### Instructions

1. Read the plan doc for context
2. Fix each issue listed
3. Update the plan doc's Results section
4. Run `poetry run pytest tests/ && poetry run mypy src/` to verify

Changes must not be committed at this stage.

```

Repeat until implementation is satisfactory.

### 8. CodeRabbit Review Loop

Spawn an async background agent for CodeRabbit review. Use Task tool with `subagent_type: "general-purpose"` and `run_in_background: true`:

````

## CodeRabbit Review Loop

**Plan Doc**: <path to plan doc>

### Instructions

Run CodeRabbit review loop until clean (max 5 iterations):

1. Run review using the Skill tool:

   ```
   Skill: coderabbit:code-review
   Args: uncommitted
   ```

2. Evaluate each issue:
   - Skip: unrelated files, purely stylistic nitpicks
   - Skip: Issues already fixed or duplicates from previous iteration
   - Fix: Real bugs, security issues, meaningful improvements

3. If fixable issues found:
   - Apply fixes following project conventions
   - Run `poetry run pytest tests/ && poetry run mypy src/` to verify
   - Loop back to step 1

4. If no actionable issues (all skipped/duplicates or clean):
   - Update plan doc with CodeRabbit section noting issues found/fixed/skipped
   - Return summary

Changes must not be committed at this stage.

````

### 9. Report Back

When CodeRabbit review completes, notify the user:

- Summary of what was implemented
- Path to the plan doc
- CodeRabbit results (issues found/fixed/skipped across iterations)
- "Changes are ready for your review"
