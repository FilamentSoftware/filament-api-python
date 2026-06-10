---
name: commit
description: Create a git commit with proper conventions
allowed-tools: Bash
---

# Git Commit

Create commits following project conventions.

## Current State

```bash
git status && echo "=== STAGED DIFF ===" && git diff --cached
```

## Commit Message Format

`<type>(<scope>): <subject>`

- **Types:** feat, fix, docs, style, refactor, perf, test, build, ci, chore
- **Scope:** use `claude` for `CLAUDE.md` and `.claude/skills` changes
- **Subject:** imperative present tense, max 100 chars. If the user says to skip CI (e.g. `/commit skip ci`), append ` [skip ci]` to the end of the first line of the message. When proposing multiple commits, only add `[skip ci]` to the last commit.
- **Body:** bullet points for larger commits (optional)
- **CHANGELOG:** single-line user-facing summary (when applicable, see below)

**Do NOT include Co-Authored-By lines** - this overrides default commit behavior.

### Formatting-Only Commits

When changes are pure auto-formatting from linters or formatters (whitespace, line breaks):

- Use `style` type (e.g., `style: auto-formatting changes only`)
- No CHANGELOG
- Verify the diff contains only formatting (no semantic changes)

### CHANGELOG Lines

**Repository policy:** Every PR branch must include at least one commit with a `CHANGELOG:` line (except dependabot branches). This is enforced during PR creation.

For **user-facing or significant app changes**, add a descriptive CHANGELOG line at the end of the commit message:

```text
CHANGELOG: <single-line summary>
```

**Every CHANGELOG must be descriptive.** Never use `CHANGELOG: no user-facing changes`. Every branch will eventually
have a meaningful, descriptive CHANGELOG line — even for internal changes, describe what actually changed
(e.g., `CHANGELOG: Refactored service initialization to use admin transactions`).

**Don't repeat context from the commit message.** The scope and subject already establish what area changed, so CHANGELOG lines should add detail, not restate it. For example, if the commit is `feat(site-search): replace searchbar with site-search`, write `CHANGELOG: Supports non-consecutive matching` not `CHANGELOG: Search supports non-consecutive matching`.

**When to include a CHANGELOG line:**

If the branch already has a commit with a `CHANGELOG:` line (`git log $(git merge-base develop HEAD)..HEAD --grep="^CHANGELOG:"`), additional
commits can omit it. If the branch has no CHANGELOG yet and it's not obvious which commit in the batch should carry it,
propose a descriptive CHANGELOG line for each commit and ask the user which one(s) to use. The user may also answer
"no" or "none" to skip the CHANGELOG entirely for this batch. Wait for the user's answer before proceeding.

## Instructions

### Branch Guard

**Before doing anything else, check the current branch.** If on `develop` or `main`, refuse to commit. Instead:

1. Propose a branch name based on the changes (e.g., `update-dev-scripts`)
2. Ask: "You're on `{branch}`. Want me to create `{proposed-name}` and continue, or use a different name?"
3. Wait for the user to confirm or provide an alternative name
4. Create and switch to the branch, then proceed with the commit flow

### Approval Requirements

Only execute commits after receiving **explicit approval** such as "yes", "approved", "go ahead", or "do it".

- Corrections or feedback on the proposal (e.g., "change scope to X") are **not approval**
- If the user's response is ambiguous, ask again: "Should I proceed with these commits?"
- It is better to ask for clarification than to commit prematurely

---

### Mode Selection (DECIDE FIRST)

**Before analyzing anything, check `git diff --cached`:**

- **Has output?** → Use **Mode A** (commit only what's staged)
- **Empty?** → Use **Mode B** (analyze and group all changes)

This decision is binary. If ANY changes are staged, use Mode A and ignore unstaged files.

---

### Mode A: Staged Changes Exist

1. Analyze the staged diff
2. Propose the commit using the same list format as Mode B below
3. Wait for approval, then execute

### Mode B: Nothing Staged (Multi-Commit Analysis)

When no changes are staged, analyze all uncommitted changes and propose intelligent groupings.

#### Grouping Rules

1. **Primary: Context** - Group by the area being changed (component, section, library)
2. **Secondary: Type** - Split within a context if types differ (feat vs fix need separate CHANGELOGs)

#### Proposal Format

Present a list for user review, putting the proposed message in a triple-back-tick code-
block. The list is the complete proposal — do NOT add descriptions, bullet points, or
explanations outside the list. The Commit Message code-block must contain the full commit
message (subject + optional body including optional CHANGELOG) so the user can review
exactly what will be committed.

````text
1. **- - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -**

```bash
feat(ScheduleService): add priority-based task sorting

CHANGELOG: Tasks now sort by priority in schedule view
```

Files (2):

  - services/CoreServices/CoreServices/Processors/ScheduleServiceProcessor.cs
  - services/CoreServices/CoreServices/Entities/ScheduleItemEntity.cs

2. **- - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -**

```bash
chore(claude): update commit skill proposal format
```

File (1):

  - .claude/skills/commit/SKILL.md

````

#### Handling Inseparable Changes

When changes in the same file can't be split (single hunk) but need separate CHANGELOGs:

1. Commit the changes with one type
2. Create an empty commit for the additional CHANGELOG entry:

   ```bash
   git commit --allow-empty -m "fix(scope): description

   CHANGELOG: ..."
   ```

#### Execution

After user approves:

1. Stage files for each commit group by explicit file path
2. Commit with the **exact approved message** — reproduce it verbatim including subject, body, and CHANGELOG lines. Do not rewrite, summarize, or omit any part.
3. Repeat for all groups

### Notes

- Test files should always be kept separate from main source files.
   - If there are both main source files and test files staged at the same time, suggest separating them into different commits.
   - Never suggest combining test changes with other source changes.
   - When creating multiple commits and another commit in the PR already has a CHANGELOG entry, test-only commits should omit the CHANGELOG line entirely. If the test commit is the only commit in the PR, propose a descriptive CHANGELOG (e.g., `CHANGELOG: Add unit tests for ScheduleService processor`) and confirm with the user.
