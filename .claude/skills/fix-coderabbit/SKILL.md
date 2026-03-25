---
name: fix-coderabbit
description: Fix CodeRabbit review issues using subagents
---

# Fix CodeRabbit Issues

> **Subagent-based workflow**: Main agent orchestrates, specialist subagents fix.
> **NEVER COMMIT.** Only make code fixes. User commits manually.

## Overview

This workflow uses focused subagents to fix each CodeRabbit issue. Subagents get minimal
context (just the issue details and project conventions), investigate independently, apply
fixes with marker comments (for easy identification and cleanup), and report back structured results.

---

## Step 1: Run Review

Use the CodeRabbit code-reviewer subagent to review changes:

```text
Task tool with subagent_type: "coderabbit:code-reviewer"
Prompt: "Review the changes on this branch. Focus on code quality, potential bugs, and style issues. Return a structured list of issues found."
```

Alternatively, save the output to a file for parsing:

```bash
mkdir -p .claude/coderabbit-fixes
```

## Step 2: Parse Issues

From the CodeRabbit review output, extract each issue:

- File path
- Line range
- Issue type (nitpick, suggestion, etc.)
- The issue description or "Prompt for AI Agent" text

## Step 3: Spawn Subagents

For each issue (or batch of related issues in the same file), spawn a **general-purpose**
subagent using the Task tool with this prompt template:

---

### Subagent Prompt Template

````text
## Fix CodeRabbit Issue

**File**: {file_path}
**Lines**: {line_range}
**Type**: {issue_type}

### Issue Description
{issue_prompt_text}

---

### Instructions

1. **Read the affected file** at `{file_path}`

2. **Decide: Fix or Skip**
   - **Fix**: Nits, formatting, deduplication, typing improvements, dead code removal, docstring fixes
   - **Skip**: Logic changes, design decisions, architectural suggestions, over-engineering

3. **If fixing**:
   - Apply the minimal fix following project conventions (see CLAUDE.md)
   - Run `poetry run pytest tests/ && poetry run mypy src/` to verify no errors introduced
   - If errors, fix them or revert and skip

4. **Report back** with this exact format:

   ```text
   STATUS: fixed | skipped
   FILE: {file_path}
   DESCRIPTION: {what was done or why skipped}
```

### Skip if

- Issue suggests logic/behavior changes
- Issue recommends architectural refactoring
- Fix would require changes beyond the identified lines
- You're uncertain whether the fix is safe

````

---

## Step 4: Collect Results

As subagents complete, collect their reports:

- Track fixed issues (file, description, marker count)
- Track skipped issues (file, reason)
- Track errors (file, what went wrong)

## Step 5: Verify Build

After all subagents complete:

```bash
poetry run pytest tests/ && poetry run mypy src/
```

If errors, identify which fix caused them and either:

- Spawn a subagent to fix the error
- Revert that fix (undo the change and remove the marker comment)

## Step 6: Iterate

Run CodeRabbit review again using the coderabbit:code-reviewer subagent.

Repeat Steps 2-5 until:

- No new actionable issues, OR
- Only skipped issues remain

Typically stabilizes in 2-4 rounds.

## Step 7: Present Summary

Report to user:

```markdown
## CodeRabbit Fix Summary

### Fixed ({count})

- `module.py`: {description}
- `service.py`: {description}

### Skipped ({count})

- `model.py`: {reason}

### Verification

- Build: {pass/fail}

### Rounds: {n}
```

---

## Execution Rules

- **Subagent type**: Use `coderabbit:code-reviewer` for reviews, `general-purpose` for fixes
- **Review scope**: Committed changes only (not staged or working directory changes)
- **Parallelization**: Can run subagents for different files in parallel; sequential for same file
- **Verification**: Each subagent verifies its own fix; main agent does final verification
- **Never commit**: Leave all changes for user review
