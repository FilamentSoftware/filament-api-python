---
name: codex-review-plan
description: Send the current plan file to codex for review. Tracks the codex session ID per plan so that follow-up reviews resume the prior codex conversation instead of starting cold. Runs codex in the background with a default 20 minute timeout, then presents findings and asks whether to fold them into the plan.
allowed-tools: Bash, Read, Glob, Edit, Write
---

# Codex Plan Review

Asks codex to review the current plan and returns its findings. On a second run for the same plan, resumes codex's prior session so it can compare against the updated plan instead of re-reading from scratch.

**Do not modify the plan during this skill.** Only present findings and ask the user.

## Inputs

The skill may be invoked with:

- A plan path (`plans/...md`)
- A timeout override (e.g. `timeout=30m`, `timeout=600s`)
- `fresh` to force a new codex session even if one exists for this plan

If no plan path is given, auto-detect (see Step 1).

## Session tracking

- Session IDs live at `.claude/codex-sessions/plan-<PLAN_BASE>.session` (one line, the UUID). `<PLAN_BASE>` is the plan filename minus `.md`, sanitised via `sed 's/[^A-Za-z0-9_.-]/_/g'` (see Step 2).
- The log from the most recent run lives next to it as `plan-<PLAN_BASE>.log`.
- The `plan-` prefix keeps these paths distinct from the branch skill's `branch-<BRANCH_SLUG>.session/.log` so they cannot collide.
- Both paths are gitignored via `.claude/.gitignore`.

## Timeout

- Default **1200 s** (20 min). Override via `timeout=` arg. Convert `30m` → `1800`, `45m` → `2700`.

---

## Step 1 — Locate the plan

1. If the user supplied a path, verify it exists. If not, say so and stop.
2. Otherwise `Glob plans/*.md`. Prefer a plan whose filename contains the current git branch slug (e.g. branch `fix-schedule-crash` → `2026-04-16-fix-schedule-crash-zero-duration-task.md`).
3. If exactly one match, use it. If several, list candidates and ask the user which.
4. If `plans/` is empty or nothing matches, tell the user:

   > No plan file found under `plans/`. Create one (see CLAUDE.md → "Planning Rules") and re-run this skill.

   Then stop. Do not invent or create a plan.

## Step 2 — Prepare session state

```bash
mkdir -p .claude/codex-sessions
```

Verify `.claude/.gitignore` contains `codex-sessions/`. If it doesn't, tell the user to add it themselves and stop this skill — do not write to the file. This keeps the review run strictly read-only; a skill-driven `.gitignore` edit would otherwise taint the repo state during a review.

Compute:

- `PLAN_FILE` = plan filename (e.g., `2026-04-16-fix-schedule-crash.md`)
- `PLAN_BASE` = `$(basename "$PLAN_FILE" .md | sed 's/[^A-Za-z0-9_.-]/_/g')`
- `SESSION_FILE` = `.claude/codex-sessions/plan-$PLAN_BASE.session`
- `LOG_FILE` = `.claude/codex-sessions/plan-$PLAN_BASE.log`

If `SESSION_FILE` exists and `fresh` was not passed, read its contents as `SESSION_ID` — this is a re-review.

## Step 3 — Compose the prompt

**Fresh review prompt:**

```text
Please review the plan at `<plan-path>` in this repository.

Read the plan and any code it references. Evaluate:
- Correctness of the proposed approach
- Missing edge cases, failure modes, or race conditions
- Unstated assumptions that could bite during implementation
- Scope concerns (too broad, too narrow, unrelated work mixed in)
- Alternatives worth considering

End with a numbered findings list. Tag each item [blocker], [suggestion], or [nit].
Do not modify any files.
```

**Re-review prompt (when resuming):**

```text
The plan at `<plan-path>` has been updated since your last review. Re-read it
and focus on what changed: were your prior findings addressed, and do the
changes introduce new concerns? Use the same [blocker]/[suggestion]/[nit]
tagging in a numbered list. Do not modify any files.
```

## Step 4 — Run codex in the background

Use `Bash` with `run_in_background: true`. Always redirect both streams to the log file.

**Fresh session:**

```bash
timeout <SECONDS> codex exec \
  --dangerously-bypass-approvals-and-sandbox \
  --json \
  "<PROMPT>" < /dev/null > "$LOG_FILE" 2>&1
```

**Resuming a session:**

```bash
timeout <SECONDS> codex exec resume \
  --dangerously-bypass-approvals-and-sandbox \
  --json \
  "$SESSION_ID" \
  "<PROMPT>" < /dev/null > "$LOG_FILE" 2>&1
```

Notes:

- The `< /dev/null` is required. Without it, `codex exec` blocks on `Reading additional input from stdin...` under the background-task harness and the run eventually hits the `timeout` with no output. If you ever see the log stall at that line, missing stdin close is the cause.
- `--json` is required so the thread ID is emitted to stdout as a JSONL event. In `--json` mode codex emits *only* JSONL — no human-readable streaming output — so don't expect anything else in the log.
- Do not poll the background job — Claude Code notifies on completion.
- Tell the user the run has started, the timeout in use, and whether it's a fresh or resumed session.

## Step 5 — Parse the log once codex reports back

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

   If non-empty, write it to `$SESSION_FILE` (overwrite — stable across resumes but re-saving is safe).

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

## Step 6 — Present findings and ask the user

Show the findings to the user, preserving all `[blocker]` items verbatim. Summarise `[nit]`s if very long. Then ask **exactly one** question:

> Codex finished its review. Would you like me to incorporate these findings into `<plan-path>`?
> - `yes` — fold in all of them
> - `no` — leave the plan alone
> - `items 1,3,5` — only those

Wait for the user's answer before touching the plan. Editing the plan is out of scope for this skill — hand off to normal planning flow after the user decides.

---

## Rules

- Never commit during this skill.
- Never run codex in the foreground — always `run_in_background: true`.
- Never modify the plan file inside this skill.
- Do not retry on timeout without asking the user (a 20-minute timeout usually means codex is stuck, not slow).
- Honour `fresh` even if a session file exists; in that case delete `$SESSION_FILE` before the new run.
