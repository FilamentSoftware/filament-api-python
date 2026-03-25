---
name: rebase
description: Stash changes, fetch and rebase onto develop, then pop stash
allowed-tools: Bash
---

# Rebase onto Develop

Safely rebase the current branch onto the latest develop.

## Steps

1. **Stash uncommitted changes** (if any):
   ```bash
   git stash -u
   ```
   If there are no changes to stash, note this and skip the stash/pop steps.

2. **Fetch and rebase onto develop**:
   ```bash
   git fetch origin develop && git rebase origin/develop
   ```
   If the rebase fails with conflicts:
   - Show the conflicting files
   - Attempt to resolve simple conflicts (e.g., both sides added to the same area)
   - For complex conflicts, show the conflict markers and ask the user how to proceed
   - After resolving, run `git rebase --continue`

3. **Pop stash** (only if step 1 stashed changes):
   ```bash
   git stash pop
   ```
   If the pop fails with conflicts:
   - Show the conflicting files and the conflict markers
   - Resolve the conflicts, preferring the working tree (stashed) changes when intent is clear
   - If ambiguous, show both sides and ask the user
   - After resolving, stage the fixed files

4. **Verify**:
   - Run `git status` to show the final state
   - **Only if the branch contains code changes** (not just docs, skills, or config): run `poetry run pytest tests/ && poetry run mypy src/` to verify the code still passes. For docs-only branches, skip this step.
   - If build fails, fix any issues introduced by the rebase
