---
name: setup-issue
description: Set up an existing GitHub issue with project board fields and project linking
allowed-tools: Bash
---

# Setup Issue

Set up an existing GitHub issue in `FilamentSoftware/filament-api-python` by adding it to the "Filament MVP" project board and configuring Type, Status, Priority, Size, and Section fields.

## Arguments

- `$ARGUMENTS` (required) - Issue number or GitHub issue URL (e.g., `12` or `https://github.com/FilamentSoftware/filament-api-python/issues/12`)

## Defaults

| Field    | Default                                 |
| -------- | --------------------------------------- |
| Type     | Task                                    |
| Status   | Backlog                                 |
| Priority | P2                                      |
| Size     | XS                                      |
| Section  | _(no default - determine from context)_ |
| Labels   | _(keep existing, suggest additions)_    |
| Assignee | @me                                     |

## Available Options

**Type**: Bug, Feature, Task -- Set via the `issueType` field on the issue. Use Bug for defects, Feature for new functionality, Task for everything else.

**Labels**: `bug`, `enhancement`, `documentation`, `duplicate`, `good first issue`, `help wanted`, `invalid`, `question`, `wontfix`, `dependencies`, `github_actions`, `python`

**Status**: Triage, Requires Design, Backlog, In progress, In review, Done, Blocked, Planned

**Priority**: P0, P1, P2, P3, P4

**Size**: XS, S, M, L, XL -- Size reflects real effort including AI agent assistance. Default to XS as most issues are very small. Go up in size as more application components are touched. M and above are for larger features. XL is very rare.

**Section**: API _(add more sections as the project grows)_

## Execution Guidance

Minimize tool calls by batching independent operations:

- **Fetch phase**: combine issue details + project field values into a single GraphQL query
- **After approval**: run "set type" and "add to project" in parallel (if both needed)
- **Project field edits**: run all `gh project item-edit` calls in parallel (they are independent)

## Instructions

### Phase 1: Fetch Existing State

Extract the issue number from `$ARGUMENTS` (strip URL prefix if needed).

Fetch all existing data in a **single GraphQL call**:

```bash
gh api graphql -f query='{
  repository(owner: "FilamentSoftware", name: "filament-api-python") {
    issue(number: {NUMBER}) {
      title
      body
      issueType { id name }
      labels(first: 20) { nodes { name } }
      assignees(first: 10) { nodes { login } }
      projectItems(first: 10) {
        nodes {
          id
          project { title number }
          fieldValues(first: 10) {
            nodes {
              ... on ProjectV2ItemFieldSingleSelectValue {
                name
                field { ... on ProjectV2SingleSelectField { name } }
              }
            }
          }
        }
      }
    }
  }
}'
```

From the response, determine:

- Whether the issue is already on "Filament MVP" (project number 3)
- If on the project: the project item ID and existing field values (Status, Priority, Size, Section)
- Whether the issue is on "Filament Support" (project number 9) and its current Status
- Existing issue type, labels, and assignees

### Phase 2: Draft Proposal

Analyze the issue's existing state and conversation context. For each field:

- If the field **already has a value** on the issue/project and it looks correct: keep that value and mark it `(exists)`
- If the field **is not set**: propose a value based on the issue content, labels, and conversation context
- **Special case - Title**: if the existing title is vague or could be improved, propose a better one (no `(exists)` marker). If the title is already good, show it with `(exists)`.
- **Special case - Labels**: show existing labels with `(exists)`. If additional labels seem appropriate, propose them without the marker. Never propose removing existing labels.
- **Special case - Project**: if "Filament MVP" is not linked, note it will be added
- **Special case - Filament Support**: if the issue is on "Filament Support", propose setting its Status to "Accepted" by default (since the issue is being picked up). Use conversation context to override if another status is more appropriate (e.g., "In Review" if already submitting a fix). Show the current Support status with `(exists)` only if it already matches the proposed value.

### Phase 3: Present Proposal

Present the proposal as a table. Fields marked `(exists)` will be skipped during execution. The table is the complete proposal -- do NOT add explanations outside it.

**Change Proposal for #{number}:**

| Field            | Value                            |
| ---------------- | -------------------------------- |
| Title            | `{title}` (exists)               |
| Assignee         | @me                              |
| Labels           | `bug` (exists), `python`         |
| Type             | Bug                              |
| Project          | Filament MVP                     |
| Status           | Backlog                          |
| Support Status   | Accepted                         |
| Priority         | P2                               |
| Size             | XS                               |
| Section          | _(none)_                         |

Ask: "Apply these settings?"

### Phase 4: Wait for Approval

Only proceed after receiving **explicit approval** such as "yes", "approved", "go ahead", or "do it".

- Corrections or feedback on the proposal (e.g., "change priority to P1") are **not approval** -- update the proposal and present again
- If the user's response is ambiguous, ask again: "Should I apply these settings?"
- It is better to ask for clarification than to apply prematurely

### Phase 5: Execute

Stop and report on any error. Skip any field marked `(exists)` in the proposal.

#### 5a. Set Type + Add to Project (parallel, if needed)

Run whichever of these are needed in parallel:

**Update title** (only if proposing a new title, not marked `(exists)`):

```bash
gh issue edit {number} \
  --repo FilamentSoftware/filament-api-python \
  --title "{new title}"
```

**Set type** (only if type is not already set or is changing):

```bash
ISSUE_NODE_ID=$(gh api "repos/FilamentSoftware/filament-api-python/issues/{number}" --jq '.node_id') && \
gh api graphql -f query="mutation {
  updateIssue(input: {
    id: \"$ISSUE_NODE_ID\",
    issueTypeId: \"{TYPE_ID}\"
  }) { issue { id } }
}"
```

**Add to project** (only if not already on "Filament MVP"):

```bash
ITEM_ID=$(gh project item-add 3 \
  --owner FilamentSoftware \
  --url "https://github.com/FilamentSoftware/filament-api-python/issues/{number}" \
  --format json --jq '.id')
echo "$ITEM_ID"
```

**Set labels** (only if new labels are being added):

```bash
gh issue edit {number} \
  --repo FilamentSoftware/filament-api-python \
  --add-label "{new_label1},{new_label2}"
```

**Set assignee** (only if not already assigned):

```bash
gh issue edit {number} \
  --repo FilamentSoftware/filament-api-python \
  --add-assignee "@me"
```

**Issue Type IDs:**

| Value   | Type ID              |
| ------- | -------------------- |
| Bug     | IT_kwDOBykWts4BF1G9  |
| Feature | IT_kwDOBykWts4BF1G-  |
| Task    | IT_kwDOBykWts4BF1G7  |

#### 5b. Set Project Fields (parallel)

Run all field edits in parallel -- they are independent. Only set fields that are not marked `(exists)`.

If the issue was just added to the project in 5a, use the `$ITEM_ID` from that step. If already on the project, use the item ID from the Phase 1 GraphQL response.

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

#### 5c. Set Filament Support Status (if on Support project)

Only if the issue is on "Filament Support" (project number 9) and the Support Status is not marked `(exists)`. Use the item ID from the Phase 1 GraphQL response for the Filament Support project item.

**Project ID**: `PVT_kwDOBykWts4BL8fD`

**Status** (field: `PVTSSF_lADOBykWts4BL8fDzg7Xk60`):

| Value       | Option ID |
| ----------- | --------- |
| Triage      | f971fb55  |
| Planning    | f75ad846  |
| Accepted    | 856cdede  |
| In Review   | f1fe568b  |
| Done        | 98236657  |
| Rejected    | 96e1fa96  |

```bash
gh project item-edit \
  --id "$SUPPORT_ITEM_ID" \
  --project-id PVT_kwDOBykWts4BL8fD \
  --field-id PVTSSF_lADOBykWts4BL8fDzg7Xk60 \
  --single-select-option-id {OPTION_ID}
```

### Phase 6: Report

After all steps complete, report:

- Issue URL (clickable)
- What was changed vs what was already set
- Confirm: Type, project board fields (Status, Priority, Size, Section) applied

Example:

```text
Updated: https://github.com/FilamentSoftware/filament-api-python/issues/12
Added to: Filament MVP
Set: Type=Bug, Status=Backlog, Priority=P2, Size=XS, Assignee=@me
Already set: Labels=bug
```
