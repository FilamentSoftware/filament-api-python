---
name: codex-review-branch
description: Send the current branch's changes to codex for review. Reviews the full diff against the merge-base with origin/develop by default; if there are uncommitted changes, asks whether to review those only or the whole branch. Tracks the codex session ID per branch so re-runs resume the prior conversation. Runs codex in the background with a default 20 minute timeout, then presents findings and asks whether to act on them.
allowed-tools: Bash, Read, Glob, Edit, Write
---

# Codex Branch Review

Asks codex to review the code changes on the current branch and returns its findings. On a second run for the same branch, resumes codex's prior session so it can compare against the updated diff instead of starting cold.

**Do not modify code during this skill.** Only present findings and ask the user what to do.

## Inputs

The skill may be invoked with:

- A timeout override (e.g. `timeout=30m`, `timeout=600s`)
- `fresh` to force a new codex session even if one exists for this branch
- `scope=branch` or `scope=uncommitted` to skip the "which scope?" prompt

## Session tracking

- Session IDs live at `.claude/codex-sessions/branch-<branch-slug>.session` (one line, the UUID).
- The log from the most recent run is next to it as `branch-<branch-slug>.log`.
- `<branch-slug>` is the branch name with `/` replaced by `_` and any other non-`[A-Za-z0-9_.-]` chars replaced by `_`.
- Both paths are gitignored via `.claude/.gitignore` (`codex-sessions/`).

## Timeout

- Default **1200 s** (20 min). Override via `timeout=` arg. Convert `30m` → `1800`, `45m` → `2700`.

---

## Step 1 — Sanity checks

```bash
git rev-parse --abbrev-ref HEAD
```

If the branch is `develop` or `main`, stop:

> You're on `<branch>`. This skill reviews a feature branch's diff against `origin/develop`; it has nothing to do on the base branch itself.

Refresh the remote ref so the merge-base is current:

```bash
git fetch origin develop
```

Compute:

- `BRANCH` = current branch
- `MERGE_BASE` = `git merge-base origin/develop HEAD`
- `BRANCH_SLUG` = `$(echo "$BRANCH" | sed 's/[^A-Za-z0-9_.-]/_/g')`
- `SESSION_FILE` = `.claude/codex-sessions/branch-$BRANCH_SLUG.session`
- `LOG_FILE` = `.claude/codex-sessions/branch-$BRANCH_SLUG.log`

If `MERGE_BASE == HEAD` (branch has no commits ahead of develop), check `git status --porcelain` before stopping:

- If there are uncommitted changes, continue to Step 2 (the user may pick `scope=uncommitted`).
- If there are no uncommitted changes, tell the user there is nothing to review and stop.

## Step 2 — Pick scope

Check for uncommitted work:

```bash
git status --porcelain
```

Decision tree:

- **No uncommitted changes** → scope is `branch` (the committed diff vs merge-base). Proceed.
- **Uncommitted changes exist** and `scope=` was passed → honour it.
- **Uncommitted changes exist** and no `scope=` arg → ask the user:

  > You have uncommitted changes. Should codex review:
  > - `branch` — all committed changes on this branch vs `origin/develop`
  > - `uncommitted` — only the working-tree changes (staged + unstaged + untracked)
  > - `both` — the full branch diff *and* the uncommitted changes as a combined review

  Wait for an answer. Default to `branch` only if the user explicitly says so.

## Step 3 — Prepare session state

```bash
mkdir -p .claude/codex-sessions
```

Verify `.claude/.gitignore` contains `codex-sessions/`. If it doesn't, tell the user to add it themselves and stop this skill — do not write to the file. This keeps the review run strictly read-only; a skill-driven `.gitignore` edit would otherwise taint `scope=uncommitted` reviews.

If `SESSION_FILE` exists and `fresh` was not passed, read its contents as `SESSION_ID` — this is a re-review. If `fresh` was passed and the file exists, delete it.

## Step 4 — Compose the prompt

Codex runs in this repo with shell access, so the prompt tells it which refs / working-tree state to diff rather than pasting the diff inline.

**Fresh review, `scope=branch`:**

```text
Please review the changes on branch `<BRANCH>` in this repository.

The change set is: `git diff <MERGE_BASE>...HEAD` (committed changes only —
ignore anything in the working tree or index).

Start by running `git log --oneline <MERGE_BASE>..HEAD` and
`git diff --stat <MERGE_BASE>...HEAD` to orient yourself, then read the full
diff and the surrounding code as needed.

Evaluate:
- Correctness and potential bugs
- Edge cases, failure modes, concurrency / race issues
- Security concerns (injection, authz, unsafe I/O)
- Tests — coverage of the new behaviour, missed regression scenarios
- Adherence to the repo's conventions (see `CLAUDE.md`)
- Scope — anything unrelated, half-finished, or missing

End with a numbered findings list. Tag each item [blocker], [suggestion], or [nit].
Do not modify any files.
```

**Fresh review, `scope=uncommitted`:** swap the change set line for:

```text
The change set is the working tree: staged + unstaged + untracked. Use
`git status`, `git diff --staged`, `git diff`, and direct reads of untracked
files to see the changes. Ignore committed history on this branch.
```

**Fresh review, `scope=both`:** change set is:

```text
The change set is `git diff <MERGE_BASE>` (committed changes vs merge-base,
plus staged and unstaged working-tree changes — i.e. everything not on
origin/develop). Also check `git status` for untracked files and read them.
```

**Re-review (resuming):**

```text
The branch has changed since your last review. Re-check using <same scope
description as above>. Focus on what changed: were your prior findings
addressed, and do the new changes introduce new concerns? Use the same
[blocker]/[suggestion]/[nit] tagging in a numbered list. Do not modify any files.
```

Substitute `<BRANCH>` and `<MERGE_BASE>` (the actual SHA) into the prompt.

## Step 5 — Run codex in the background

Use `Bash` with `run_in_background: true`. Always redirect both streams to the log file.

**Fresh session:**

```bash
timeout <SECONDS> codex exec \
  --dangerously-bypass-approvals-and-sandbox \
  --json \
  "<PROMPT>" < /dev/null > "$LOG_FILE" 2>&1
```

**Resuming:**

```bash
timeout <SECONDS> codex exec resume \
  --dangerously-bypass-approvals-and-sandbox \
  --json \
  "$SESSION_ID" \
  "<PROMPT>" < /dev/null > "$LOG_FILE" 2>&1
```

Notes:

- The `< /dev/null` is required. Without it, `codex exec` blocks on `Reading additional input from stdin...` under the background-task harness and the run eventually hits the `timeout` with no output. If you ever see the log stall at that line, missing stdin close is the cause.
- `--json` is required so the thread ID is emitted on stdout as a JSONL event. In `--json` mode codex emits *only* JSONL — no human-readable streaming output — so don't expect anything else in the log.
- Do not poll — Claude Code notifies when the background job completes.
- Tell the user the run has started, the branch, the chosen scope, the timeout, and whether it's a fresh or resumed session.

## Step 6 — Parse the log once codex reports back

Codex `--json` emits JSONL events in this shape (verified):

```json
{"type":"thread.started","thread_id":"<uuid>"}
{"type":"turn.started"}
{"type":"item.completed","item":{"id":"...","type":"agent_message","text":"..."}}
{"type":"turn.completed","usage":{...}}
```

After the background run completes:

1. **Extract the thread ID** — this is the session ID; `codex exec resume` accepts it. Scan the entire JSONL log (not just the first line) so a warning/preamble line before `thread.started` or a reordered event doesn't silently break session resume:

   ```bash
   THREAD_ID=$(python3 - "$LOG_FILE" <<'PY'
   import json, sys
   thread_id = ""
   with open(sys.argv[1]) as f:
       for line in f:
           try:
               ev = json.loads(line)
           except json.JSONDecodeError:
               continue
           if ev.get("type") == "thread.started" and ev.get("thread_id"):
               thread_id = ev["thread_id"]
               break
   print(thread_id, end="")
   PY
   )
   ```

   If non-empty, write it to `$SESSION_FILE` (overwrite).

   If empty **and** codex exited non-zero:
   - If the exit code is 124 (GNU `timeout` killed the process), tell the user the run timed out and suggest re-running with a longer timeout (include the last ~40 log lines for context).
   - Otherwise, surface the error (exit code + last ~40 log lines) as a generic failure.

   In either case, do not overwrite `$SESSION_FILE`.

2. **Extract the final assistant message** — the last `item.completed` event with `item.type == "agent_message"`. Text may contain escaped quotes and newlines, so parse with Python:

   ```bash
   python3 - "$LOG_FILE" <<'PY'
   import json, sys
   last = None
   with open(sys.argv[1]) as f:
       for line in f:
           try:
               ev = json.loads(line)
           except json.JSONDecodeError:
               continue
           if ev.get("type") == "item.completed" and ev.get("item", {}).get("type") == "agent_message":
               last = ev["item"].get("text")
   print(last or "", end="")
   PY
   ```

   If the extracted text is large, write it to a temp file and `Read` in chunks rather than pulling it all into context at once.

## Step 7 — Present findings and ask the user

Show the findings, preserving all `[blocker]` items verbatim. Summarise `[nit]`s if very long. Then ask **exactly one** question:

> Codex finished its review of `<BRANCH>` (scope: `<scope>`). What next?
> - `fix all` — apply fixes for every finding
> - `fix N,M` — only those items
> - `nothing` — leave the code alone
> - `plan` — write a plan file under `plans/` capturing the findings for later

Wait for the user's answer before touching code. Fixing is out of scope for this skill — hand off to normal flow after the user decides.

---

## Rules

- Never commit during this skill.
- Never run codex in the foreground — always `run_in_background: true`.
- Never modify code or create plan files inside this skill.
- Do not retry on timeout without asking the user.
- Honour `fresh` even if a session file exists; delete `$SESSION_FILE` before the new run.
