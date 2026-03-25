---
name: create-issue
description: Create a GitHub issue and add it to the Filament MVP project board with fields
allowed-tools: Bash
---

# Create Ticket

Create a GitHub issue in `FilamentSoftware/filament-python-api` and add it to the "Filament MVP" project board with Type, Status, Priority, Size, and Section fields.

## Arguments

- `$ARGUMENTS` (optional) - Brief description of the ticket. If omitted, derive from conversation context.

## Defaults

| Field    | Default                                 |
| -------- | --------------------------------------- |
| Type     | Task                                    |
| Status   | Backlog                                 |
| Priority | P2                                      |
| Size     | XS                                      |
| Section  | _(no default - determine from context)_ |
| Labels   | _(determine from context)_              |
| Assignee | @me                                     |

## Available Options

**Type**: Bug, Feature, Task — Set via the `issueType` field on the issue. Use Bug for defects, Feature for new functionality, Task for everything else.

**Labels**: `bug`, `enhancement`, `documentation`, `duplicate`, `good first issue`, `help wanted`, `invalid`, `question`, `wontfix`, `dependencies`, `github_actions`, `python`

**Status**: Triage, Requires Design, Backlog, In progress, In review, Done, Blocked, Planned

**Priority**: P0, P1, P2, P3, P4

**Size**: XS, S, M, L, XL — Size reflects real effort including AI agent assistance. Default to XS as most issues are very small. Go up in size as more application components are touched. M and above are for larger features. XL is very rare.

**Section**: API _(add more sections as the project grows)_

## Execution Guidance

Minimize tool calls by batching independent operations:

- **Preflight**: combine both git commands into a single Bash call
- **After issue creation**: run "get node_id + set type" and "add to project" in parallel
- **Project field edits**: run all `gh project item-edit` calls in parallel (they are independent)
- **Link commit**: chain `git commit` and `git push` with `&&` in a single Bash call
- **Write body to temp file** (`/tmp/issue-body.md`) to avoid shell escaping issues with backticks in markdown

## Instructions

### Phase 1: Preflight — Branch Check

Before anything else, verify the current branch is pushed to origin (single Bash call):

```bash
BRANCH=$(git rev-parse --abbrev-ref HEAD) && git ls-remote --heads origin "$BRANCH"
```

- If the branch **does not exist at origin**, ask the user: "Branch `{branch}` hasn't been pushed to origin. Push it now, or skip issue creation?"
  - If user says push: run `git push -u origin {branch}`, then continue
  - If user says skip/quit: stop execution
- If the branch **exists at origin**, continue silently

### Phase 2: Draft Proposal

Analyze the user's context (conversation, branch work, arguments) and draft the ticket. Determine appropriate values for all fields:

- **Title**: Concise imperative description (e.g., "Add priority-based task sorting to ScheduleService")
- **Body**: Markdown description with context, acceptance criteria, or reproduction steps as appropriate
- **Labels**: Select from the available set based on context
- **Section**: Match to the service or area of the backend being discussed
- **Type**: Bug for defects, Feature for new functionality, Task for everything else
- **Priority/Size**: Use defaults unless context suggests otherwise

### Phase 3: Present Proposal

Present the proposal as a table followed by the body preview. The table is the complete proposal -- do NOT add explanations outside it.

**Ticket:**

| Field    | Value                  |
| -------- | ---------------------- |
| Title    | `{title}`              |
| Assignee | @me                    |
| Labels   | `{label1}`, `{label2}` |
| Type     | Task                   |
| Status   | Backlog                |
| Priority | P2                     |
| Size     | XS                     |
| Section  | {section}              |

**Body:**

> {formatted body preview}

Ask: "Create this ticket?"

### Phase 4: Wait for Approval

Only proceed after receiving **explicit approval** such as "yes", "approved", "go ahead", or "do it".

- Corrections or feedback on the proposal (e.g., "change priority to P1") are **not approval** -- update the proposal and present again
- If the user's response is ambiguous, ask again: "Should I create this ticket?"
- It is better to ask for clarification than to create prematurely

### Phase 5: Execute

Stop and report on any error.

#### 5a. Create the Issue

Write body to `/tmp/issue-body.md` first, then create:

```bash
gh issue create \
  --repo FilamentSoftware/filament-python-api \
  --title "{title}" \
  --body-file /tmp/issue-body.md \
  --label "{label1},{label2}" \
  --assignee "@me"
```

Extract the issue number from the returned URL.

#### 5b. Set Type + Add to Project (parallel)

Run these two in parallel — they are independent:

**Set type** (get node_id then mutate):

```bash
ISSUE_NODE_ID=$(gh api "repos/FilamentSoftware/filament-python-api/issues/{number}" --jq '.node_id') && \
gh api graphql -f query="mutation {
  updateIssue(input: {
    id: \"$ISSUE_NODE_ID\",
    issueTypeId: \"{TYPE_ID}\"
  }) { issue { id } }
}"
```

**Add to project:**

```bash
ITEM_ID=$(gh project item-add 3 \
  --owner FilamentSoftware \
  --url "$ISSUE_URL" \
  --format json --jq '.id')
echo "$ITEM_ID"
```

**Issue Type IDs:**

| Value   | Type ID              |
| ------- | -------------------- |
| Bug     | IT_kwDOBykWts4BF1G9  |
| Feature | IT_kwDOBykWts4BF1G-  |
| Task    | IT_kwDOBykWts4BF1G7  |

#### 5c. Set Project Fields (parallel)

Run all field edits in parallel — they are independent. Set each field with a separate `gh project item-edit` call.

**Project ID**: `PVT_kwDOBykWts4AkSPD`

**Status** (field: `PVTSSF_lADOBykWts4AkSPDzgchC84`):

| Value           | Option ID |
| --------------- | --------- |
| Triage          | 9290945b  |
| Requires Design | 97193aa4  |
| Backlog         | f75ad846  |
| In progress     | 47fc9ee4  |
| In review       | df73e18b  |
| Done            | 98236657  |
| Blocked         | ed66274e  |
| Planned         | b85456c2  |

**Priority** (field: `PVTSSF_lADOBykWts4AkSPDzgchC9k`):

| Value | Option ID |
| ----- | --------- |
| P0    | 79628723  |
| P1    | 0a877460  |
| P2    | da944a9c  |
| P3    | 908936f0  |
| P4    | 5c969545  |

**Size** (field: `PVTSSF_lADOBykWts4AkSPDzgchC9o`):

| Value | Option ID |
| ----- | --------- |
| XS    | 6c6483d2  |
| S     | f784b110  |
| M     | 7515a9f1  |
| L     | 817d0097  |
| XL    | db339eb2  |

**Section** (field: `PVTSSF_lADOBykWts4AkSPDzg2bkTk`):

Look up available Section options dynamically:

```bash
gh api graphql -f query='{ node(id: "PVT_kwDOBykWts4AkSPD") { ... on ProjectV2 { field(name: "Section") { ... on ProjectV2SingleSelectField { options { id name } } } } } }' --jq '.data.node.field.options[] | "\(.name)\t\(.id)"'
```

Use the option ID matching the relevant section for this project.

Each field command:

```bash
gh project item-edit \
  --id "$ITEM_ID" \
  --project-id PVT_kwDOBykWts4AkSPD \
  --field-id {FIELD_ID} \
  --single-select-option-id {OPTION_ID}
```

#### 5d. Link Branch to Issue

Create an empty commit linking the branch to the issue and push (single Bash call):

```bash
git commit --allow-empty -m "chore: closes #XXX" && git push
```

### Phase 6: Report

After all steps complete, report:

- Issue URL (clickable)
- Confirm: Type, project board fields (Status, Priority, Size, Section), and branch link commit were set

Example:

```text
Created: https://github.com/FilamentSoftware/filament-python-api/issues/XXX
Type: Task | Project fields: Status=Backlog, Priority=P2, Size=XS, Section=API
Branch linked via commit: chore: closes #XXX
```

After reporting, ask: "Would you like to create a Draft PR? (`/create-pr {number}`)"
