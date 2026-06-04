---
name: audit-branch
description: Audit a branch for alignment with this Python SignalR client's conventions, patterns, and architecture using parallel read-only sub-agents, then walk the resulting questions with the user to make findings actionable. Surfaces public-API drift, FilamentClient lifecycle / async correctness issues, SignalR wire-format serialization drift, typed Condition-builder + mypy-plugin coupling, subscription-routing invariants, hand-edits to generated code, error-handling discipline, strict-mypy / Python 3.10 baseline violations, test-coverage gaps, and changelog omissions. Distinct from `/review` (bugs/security) — this is alignment, not correctness.
---

# Audit Branch

A two-phase skill that audits a branch against this codebase's conventions, patterns, and architecture, then walks the resulting questions with the user to refine the findings into actionable work.

The companion `KNOWLEDGE.md` is **consideration guidance**, not a mechanical pass list — it captures the architecture lessons a senior contributor would absorb after months in this codebase, so an auditor can apply those lessons creatively to new code rather than pattern-matching grep hints.

This is the **`filament-api-python` client library** — an async Python (3.10–3.13, strict mypy, Poetry) SignalR client that talks to the C# Filament backend. There is no rendered UI — "drift" means deviation from code conventions, async/SignalR patterns, the wire-format contract with the backend, and the public Python API, never visual change.

## Lifecycle

```text
Step 0   Audit file already exists for this branch?
            ├── Yes  → skip Phase 1 (idempotent), enter Phase 2
            └── No   → Phase 1, then Phase 2

Phase 1 — Produce the punch list (parallel, read-only)
    Resolve scope → spawn one codebase-auditor per focus → aggregate → write audit file.

Phase 2 — Q&A with user (refine actionability)
    Walk unanswered [Q]s → record [A]/[FIXED]/[SKIP] in the audit file.
    Apply fixes inline when the user confirms; commit each via /commit.
    When all questions are answered, the skill is done.
```

After Phase 2, the main agent has full context from both phases for any remaining fix work in normal chat.

## When to use

- Reviewing a feature branch (especially heavily AI-assisted ones) before merge or PR.
- Onboarding a contributor's first branch into the codebase's conventions.
- Periodic sweep after a flurry of vibe-coded work.
- Re-running on a branch that already has an audit file (skips straight to Phase 2 to resume Q&A).

Not for: pure correctness/security review (use `/review`).

## Inputs

- **Base branch** — defaults to `develop` (project convention; never override unless the user explicitly says to). The skill takes an optional positional override; if absent, use `develop`.
- **Focus filter** — optional comma-separated subset of focus areas (e.g. `public-api,serialization`). Default: all. Phase 1 only — no effect on Phase 2.

---

## Severity philosophy

Three severities, three jobs. The auditor decides which by asking *can I justify this call without the maintainer's input?*

- **`[FIX]`** — mechanical correction. Two **hard preconditions** must both hold: (a) **no change to observable behaviour or contract** (runtime semantics, public API signature, wire-format shape, generated-type shape), and (b) the suggestion is anchored in a documented convention the auditor can cite with **verbatim text** (from a source file or `.claude/CLAUDE.md`) or canonical sibling code. If either fails, downgrade. Examples: missing return-type annotation on a new `def`; missing `from __future__ import annotations`; `uuid.UUID` → `uuid_utils.UUID`; a new public method missing the `from_dict` try/except + `_deserialization_error()` fallback; a new `_handle_*` event handler that doesn't wrap callback dispatch in per-callback try/except; a new task created with `asyncio.create_task` not awaited in `__aexit__`.
- **`[REFACTOR]`** — structural improvement with **no behaviour/contract change**. Examples: hand-rolled `strftime` where `instant_to_wire` exists; a bespoke dataclass→dict helper duplicating `to_dict`; `X | None` where the codebase consistently uses `Optional[X]`; an unused `pytest_asyncio` import where `pytest.mark.asyncio` is the established form.
- **`[Q]`** — escalation to the user when the auditor cannot make the call alone. The primary trigger is *ambiguity*: deviation that might be by-design or accidental drift. Common shapes:
  - The pattern violates a rule but might be a legitimate exception (auditor needs the maintainer's intent).
  - Two valid prior-art patterns exist and the auditor can't tell which is canonical here.
  - **The change alters observable behaviour, the public API, the wire-format with the C# backend, or a generated type.** That's a design/runtime call — **mandatory `[Q]`** unless a documented convention authors the exact change. (Example: any branch-introduced edit under `src/filament/types/generated/**` is `[Q]` — hand-edit or legitimate regen?)
  - A runtime-semantic concern the auditor can't decide alone (async task lifecycle, race conditions in subscription dispatch, cache/buffer invariants, reconnect behaviour).
  - An anti-pattern shows up here and also in prior siblings — should this be a codebase-wide design call rather than a per-site fix?

When in doubt, downgrade to `[Q]`. Auditors do not make design calls.

### Doc-citation discipline

A finding's `Doc: <ref>` cite is a contract with the reviewer that the cited source actually supports the recommendation. Failing the contract — manufacturing authority the source doesn't grant — misleads the reviewer worse than no cite at all.

- **Quoted text must be a verbatim substring of the cited file.** Do not stitch text from non-adjacent lines into a single quoted phrase. Do not paraphrase inside quotation marks. Do not pull a phrase out of context and re-cite it as supporting a claim the source doesn't make in context.
- **Verify the recommendation is what the source says, in the context it applies to.** A rule about `_client.py` `_invoke` doesn't automatically apply to the lower-level pysignalr `send` call; a rule about hand-coded modules doesn't apply to `src/filament/types/generated/**`. Read enough surrounding code/doc to confirm scope.
- **If the source supports the point but doesn't say it in one quotable sentence**, paraphrase without quotes and cite the file + line/section. No fake quotes.

---

## Step 0 — Idempotent check

Run this before any other step. The audit file is the contract between phases — if it exists, Phase 1 is already done.

```bash
BRANCH=$(git rev-parse --abbrev-ref HEAD)
# Slug rule: leading number from a numbered branch (e.g. "759-..." → "759"); otherwise the full branch name.
if [[ "$BRANCH" =~ ^([0-9]+)- ]]; then SLUG="${BASH_REMATCH[1]}"; else SLUG="$BRANCH"; fi
AUDIT_FILE="plans/temp/${SLUG}-audit-branch.md"
```

- **If `$AUDIT_FILE` does not exist** → enter Phase 1 (produce the punch list). Create `plans/temp/` if needed.
- **If `$AUDIT_FILE` exists** → read its first heading line (`# Audit: <branch> vs <base>`). If `<branch>` matches the current `$BRANCH`, skip Phase 1 and enter Phase 2 to walk any unanswered `[Q]`s. If `<branch>` differs, tell the user the file is for a different branch and ask whether to overwrite (re-run Phase 1) or abort. Never silently overwrite.

The audit file is meant to stay local — add `plans/temp/` to the repo root `.gitignore` if it isn't already, so the file isn't accidentally committed.

---

## Phase 1 — Produce the punch list (read-only)

### Step 1 — Resolve scope

**Always diff against `origin/<base>`, never local `<base>`.** Local refs go stale (worktrees especially). Using `origin/<base>` after a fetch guarantees the comparison is against the actual state of the upstream branch.

```bash
BASE=develop  # default — only change if user explicitly specified another base
git fetch origin "$BASE" 2>/dev/null || true

# Sanity check: confirm local <base> isn't dramatically stale (informational).
LOCAL_BEHIND=$(git rev-list --count "$BASE..origin/$BASE" 2>/dev/null || echo 0)
echo "Local $BASE is $LOCAL_BEHIND commits behind origin/$BASE."

# Use origin/<base> as the actual base — this is what the rebase landed on.
git diff --name-status "origin/$BASE...HEAD"
git diff --shortstat "origin/$BASE...HEAD"
```

> All `git` commands run directly in the container shell — no `hostcmd` wrapping needed. Phase 1 never runs `mypy` / `pytest` / `poetry`; if a fix in Phase 2 needs one, use `hostcmd poetry run mypy ...` / `hostcmd poetry run pytest ...` per `.claude/CLAUDE.md`.

Capture: the full list of changed files (`A` and `M`; `D` is informational), the shortstat for the report header, the current branch name, and the "local behind" count (mention in the report header if > 50, as a courtesy).

If the branch has no diff vs `origin/<base>`, stop with a one-line message.

**Failure mode this prevents**: local `develop` going stale (no recent `git pull`) silently includes upstream's recent commits as if they were branch-unique work. The audit then flags develop's own changes as branch drift. `origin/<base>` is the truth.

### Step 2 — Spawn auditors in parallel

For each focus area not excluded by the focus filter, spawn a `codebase-auditor` subagent. **Send all spawns in a single message** so they run concurrently.

The focus areas (kept in sync with `KNOWLEDGE.md`):

- `public-api` — the exported surface (`src/filament/__init__.py` `__all__`, `FilamentClient` method/ctor signatures, `Condition`, `FilamentError*`, `SpecifiedPropertiesBase`). Refs: `src/filament/__init__.py`, `src/filament/_client.py`, `src/filament/_condition_builder.py`, `src/filament/_exceptions.py`, `src/filament/_specified_properties.py`.
- `client-lifecycle` — `FilamentClient` ctor / `__aenter__` / `__aexit__` / `_invoke` / `_send_client_hello` / event-handler registration patterns, including the `_pending_invocations` future contract and `_handle_close` cleanup. Ref: `src/filament/_client.py`.
- `serialization` — `_serialization.py` + `_wire_formats.py` discipline (case conversion, NodaTime Instant/Duration wire formats, `uuid_utils.UUID`, `$type` dispatch, `None_` enum naming, `specified_properties`). Refs: `src/filament/_serialization.py`, `src/filament/_wire_formats.py`.
- `conditions` — the `DbConditionInstance` hierarchy + `condition_to_dict` / `condition_from_dict` polymorphic serialisation + the four typed builder wrappers, with cross-references to `mypy-plugin`. Refs: `src/filament/types/conditions.py`, `src/filament/_condition_builder.py`.
- `mypy-plugin` — `filament_mypy_plugin.py` narrowing logic, fullname-constant alignment with `_condition_builder.py`, registration in `pyproject.toml`. Refs: `src/filament_mypy_plugin.py`, `pyproject.toml`, `src/filament/_condition_builder.py`.
- `generated-code` — DO NOT EDIT rule for `src/filament/types/generated/**` (every file starts `# Generated by Filament GenerateCode`); detection of hand-edits vs legitimate regen. Refs: `.claude/CLAUDE.md` "Generated Code", `scripts/generate_code.py`, the generated tree.
- `codegen-surface` — hand-coded changes that downstream regen depends on (renames/removals in `_wire_formats.py`, `types/conditions.py`, `types/options.py`, `_specified_properties.py`), and runner-script (`scripts/generate_code.{py,sh,cmd}`) edits. Refs: those files.
- `error-handling` — two-channel discipline: raise `FilamentError*` at lifecycle boundary, return `ValueOrErrorPublic_1[T]` / `SuccessOrErrorPublic` from operations; `_deserialization_error()` / `_error_result_public()` fallbacks; `logger.exception` in `except` blocks. Refs: `src/filament/_exceptions.py`, `src/filament/_client.py` (the helpers + the `read_data` / `modify_data` / `subscribe` shape).
- `async-correctness` — asyncio patterns: task lifecycle, cancel-and-suppress, future tracking via `_pending_invocations`, sync-vs-async callback dispatch via `_invoke_callback`, no blocking I/O, no event-loop assumptions in `__init__`. Ref: `src/filament/_client.py`.
- `subscription-routing` — the four subscription maps (`_backend_to_client`, `_client_to_backend`, `_client_callbacks`, `_client_error_callbacks`), multi-part `_part_buffers` reassembly, `_poisoned_part_buffer_keys`, cleanup invariants on `unsubscribe` / `unsubscribe_all` / `_handle_close`. Ref: `src/filament/_client.py` (the `subscribe` … `_handle_data_update_rejected` region).
- `code-quality` — strict mypy compliance, full signature annotations, `from __future__ import annotations` on every module, Python 3.10 baseline (no PEP 695 `type` statement, no `ExceptionGroup`, no `tomllib`), `Optional[X]` over `X | None`, leading underscore on private modules, `logging.getLogger(__name__)`. Refs: `.claude/CLAUDE.md` "Code Conventions", `pyproject.toml` `[tool.mypy]`.
- `tests` — pytest + pytest-asyncio idioms; mock `pysignalr.SignalRClient` for `FilamentClient` tests; round-trip tests for wire-format changes; per-public-API test coverage. Refs: `tests/test_client.py`, `tests/test_serialization.py`, `tests/test_condition_builder.py`, `pyproject.toml` `[tool.pytest.ini_options]`, `.github/workflows/run-unit-tests.yml`.
- `changelog` — `CHANGELOG_DEV.md` updates for user-visible changes; section + category convention. Refs: `CHANGELOG_DEV.md`, `.github/workflows/validate-pull-request.yml`.
- `reinvention` — open-ended sweep for anything in new code that already has a home (hand-rolled wire-format helper vs `_wire_formats.py`, bespoke condition wrapper vs the four `DbCondition*Property` types, custom future-tracking vs `_pending_invocations`, sync-async dispatch vs `_invoke_callback`). Surveys the hand-coded modules under `src/filament/` and `src/filament/types/`.

#### Eager-load discipline

For high-discipline focuses where the cost of forgetting reference material is silently-wrong findings, the auditor must read the canonical reference *upfront* — before reading any branch file. The reference becomes ambient knowledge, not something the auditor remembers to grep for.

- `client-lifecycle` — eager-load `src/filament/_client.py` in full. The whole `FilamentClient` class is the contract; partial reads silently miss the `_pending_invocations` ↔ `_handle_close` coupling, the `_run_task` cancel-and-suppress dance, and the `_invoke` future shape.
- `serialization` — eager-load `src/filament/_serialization.py` **and** `src/filament/_wire_formats.py` in full. Wire-format alignment is a two-file contract; misreading one breaks the other half of the round-trip.
- `conditions` — eager-load `src/filament/types/conditions.py` + `src/filament/_condition_builder.py` in full. The polymorphic serialisation and the builder layer share a fixed wire format.
- `mypy-plugin` — eager-load `src/filament_mypy_plugin.py` in full. The fullname constants are the trip-wire.
- `error-handling` — eager-load `src/filament/_exceptions.py` and the `_deserialization_error` / `_error_result_public` helpers at the bottom of `src/filament/_client.py`.
- `subscription-routing` — eager-load the subscription/dispatch section of `src/filament/_client.py` (roughly `_handle_data_updated` through `unsubscribe_all`).

Other focuses use on-demand grep — they're either small enough to internalise quickly (code-quality, public-api) or so cross-cutting that an exhaustive load isn't tractable (reinvention surveys broadly per question).

#### Subagent prompt template

```text
You are the codebase-auditor for the `<focus>` focus area.

BASE_REF: origin/<base>           # the resolved ref — use this exact string in any git diff
BASE_BRANCH_NAME: <base>          # human-readable for the report header
BRANCH: <current-branch>

TOUCHED FILES (added + modified):
<paste git diff --name-status output>

YOUR CONSIDERATIONS (verbatim from .claude/skills/audit-branch/KNOWLEDGE.md, section: <focus>):
<paste the corresponding section>

EAGER-LOAD (read in full before reading any branch file):
<list from the eager-load section above; omit this block if the focus has no eager-load entries>

REFERENCE FILES:
<list the reference files from the focus entry above; full paths>

SEVERITY PHILOSOPHY (from SKILL.md):
- [FIX] = mechanical correction. HARD preconditions: (a) no change to observable behaviour/contract (runtime semantics, public API, wire-format shape, generated-type shape); (b) anchored in a documented convention with verbatim source support or canonical sibling code. If either fails, do not use [FIX].
- [REFACTOR] = structural improvement with no behaviour/contract change.
- [Q] = escalation. A change to behaviour/contract/wire-format/generated-type ⇒ MANDATORY [Q] unless a documented convention authors the exact change. Ambiguity, by-design vs drift, runtime semantics, codebase-wide design calls also ⇒ [Q]. When in doubt, downgrade to [Q].

DOC-CITATION DISCIPLINE: Quoted source text must be a verbatim substring of the cited file. No stitching across non-adjacent lines. No paraphrase inside quotes. Before citing a source as support for a fix, verify it says what the recommendation does, in the context it applies to (e.g. a generated-code rule is not a hand-coded-module rule; a serialization rule is not a condition-builder rule).

Reminder: KNOWLEDGE.md is consideration guidance, not a mechanical pass list. Read the reference files first to internalize the architecture, then audit. Look for the lesson each rule encodes — surface architectural drift even when its shape doesn't match the rule's literal grep hint.

Follow the workflow in your agent definition. Return findings only — no preamble.
```

The skill body should:

1. Read `KNOWLEDGE.md` once.
2. Slice out each focus's section.
3. Spawn the parallel agents with the slice + scope + eager-load list (where applicable).

### Step 3 — Aggregate findings

This report is the source document for Phase 2. Optimise for **complete surfaced scope**, not a "concise reviewer digest." The aggregator organises; it does not censor.

1. **Merge only exact duplicates.** Two findings collapse into one only when they share file, line, rule, AND suggested fix. Findings that share a rule but differ in file or line are distinct items and stay distinct. Two `[Q]`s asking similar questions about different sites are separate questions; preserve both.
2. **Pull up repeated patterns as a headline, not a replacement.** When the same rule fires across many sites (e.g. missing return annotation in 12 places), add a one-line count under `Repeated Patterns`. **Every individual occurrence still appears in `Findings`** — the headline is a summary, not a substitute.
3. **Sort findings: `[Q]` first, then `[FIX]`, then `[REFACTOR]`.** The user reads the report primarily through the lens of "what conversations do I need to have?" — Phase 2 walks `[Q]`s before any fixes, so they belong at the top. Within each section, group by file path so a reviewer fixing a file sees related items together.

**Anti-rule: do not suppress findings for brevity.** Don't drop `[Q]` items because the report is long. Don't collapse separate sites into a single "(12 occurrences)" line without listing each one. Don't decide what the user "doesn't need to see." When in doubt, prefer over-reporting with calibrated severity over under-reporting; the reviewer decides scope.

### Step 4 — Write the audit file

Write the report to `plans/temp/<slug>-audit-branch.md` (slug per Step 0), then respond inline with the file path and a one-paragraph summary.

If overwriting an existing file (because the user accepted re-run on a branch-mismatch in Step 0), the user has authorised this — proceed.

#### Report template

```markdown
# Audit: <branch> vs <base>

<shortstat line>
Focuses: <space-separated list>

## Summary

<3-7 prose bullets, fast to read, lead with what's good before what's drift. The summary is an executive overview — it names the headlines once. The Findings section below is exhaustive and is what the user works from for Phase 2.

If the branch edits files under `src/filament/types/generated/**`, ALWAYS include a tight one-line bullet here flagging the generated-code touchpoints with a question about whether the change came from a real codegen run.>

## Repeated Patterns

- **<rule>** — <count> occurrences across <count> files. Doc: <ref>.
- ...

(Repeated Patterns is a count headline; every individual occurrence still appears in Findings below.)

## Findings

### QUESTION

- [Q] full/path/to/File.py(line) — <question with reviewer-perspective context>.
- ...

### MUST FIX

- [FIX] full/path/to/File.py(line) — <description>. Suggested: <fix>. Doc: <ref>.
- ...

### SHOULD REFACTOR

- [REFACTOR] full/path/to/File.py(line) — <description>. Suggested: <fix>.
- ...

## Notable

- <only contributions that shift trust-level — e.g. "every new public method added the matching test class shape", or a verification trace the reviewer can spot-check at a glance>.

## Coverage

public-api=N · client-lifecycle=N · serialization=N · conditions=N · mypy-plugin=N · generated-code=N · codegen-surface=N · error-handling=N · async-correctness=N · subscription-routing=N · code-quality=N · tests=N · changelog=N · reinvention=N
```

#### Inline response after writing the file (Phase 1 → Phase 2 handoff)

After writing the report:

1. State the file path so the user can open it.
2. Give a one-paragraph summary (3-5 sentences) covering the headline counts (`X QUESTION, Y MUST FIX, Z SHOULD REFACTOR`), the most consequential items, and any generated-code / wire-format callouts.
3. **Offer the Q&A walkthrough** if any `[Q]`s exist: `Audit ready at <path>. Want to walk through the <N> question(s) together?` On accept, recommend `/compact` first — frame it as state-preserving: `Your answers will persist in the audit file — feel free to /compact to free context before we start.` Wait for the user to compact (or decline) before entering Phase 2.
4. If the audit produced zero `[Q]`s, skip the offer — Phase 2 has nothing to do — and end with `_Fixes are a separate, deliberate step — ask explicitly._`

Do not paste the full report into the chat — the file is the artifact.

#### Format strict rules

- **Severity prefix is `[FIX]` / `[REFACTOR]` / `[Q]`** as the first token after the markdown list marker. No other variations.
- **File reference uses compiler-style `name(line,col)`** — paren form, no space inside the parens. Default to the bare filename when unique in the repo (e.g. `_client.py(208,4)`). Use a partial path (parent dir + filename) only when the name is generic — `__init__.py`, `base.py`, etc. — where DevHub would otherwise show a disambiguation picker. Example: `types/generated/base/base.py(34,4)`.
- **Acceptable variants**: `name(line)` when col is irrelevant; `name:line` or `name:line:col` (colon form); `name(line-line)` for a range (informational — clicks to the first line).
- **Forms that DO NOT parse**: `name(line, col)` (space inside parens), `name(line, line, line)` (multi-line list), `L###`. Don't use them.
- **Multi-site findings**: pick a single representative line for the clickable reference, then list additional line numbers as plain text. Example: `[FIX] _client.py(77,3) — missing return annotation; also at 124, 137, 182, 245.`
- **State each headline once.** The Summary names a problem; Repeated Patterns counts it; Findings list each occurrence — don't restate the explanation three times.
- **Questions include reviewer-perspective context.** "[Q] _client.py(251) — New task created. Intentional?" is bad. "[Q] _client.py(251) — Adds a new background `asyncio.create_task(...)` whose return value isn't stored on `self`, so `__aexit__` won't cancel it and a clean shutdown will leak the task with a 'Task was destroyed but pending' warning. Is the orphaned-task lifetime intended?" is good. Without the runtime effect, the reviewer can't answer.

---

## Phase 2 — Q&A with user

Entered from Step 0 (re-invocation with an existing audit file) or from Step 4's handoff once the user accepts. Distinct from Phase 1: edits and commits happen here, but **only on per-item user confirmation**.

### Marker convention

Q&A annotates each `[Q]` line with sub-bullets (two-space indent under the question):

```markdown
- [Q] File.py(line) — original question text.
  - [A] answer rationale (1–3 sentences capturing the decision and reasoning).
  - [FIXED] what changed; commit hash if a commit was made.
  - [SKIP] reason this isn't needed (false positive, intentional deviation, out-of-scope).
```

- `[A]` is required on every answered question — captures the *why*, not just a marker.
- `[FIXED]` is added when an edit was applied during Q&A.
- `[SKIP]` is added when the question is dismissed without action.
- `[A]` and one of `[FIXED]`/`[SKIP]` can co-exist; they are not mutually exclusive.
- **Unanswered detection**: a `[Q]` whose sub-bullet block lacks any `[A]` line. Greppable.
- **Don't rewrite the original `[Q]` line.** The question text is the historical record. Append sub-bullets; don't replace.

### Workflow per question (or grouped questions)

1. **Pick the next question(s).** As the interviewer, decide on the fly whether one or several `[Q]` items belong together (same file/component/concept, tightly coupled answer). Group naturally; otherwise present one at a time.
2. **Present with full context.** Quote the question, link the file:line, and read the relevant code if it helps the user decide.
3. **Discuss.** Have a real conversation. Ask clarifying questions if the answer isn't obvious. Don't pressure to a fix — sometimes "leave as-is, document why" is the right answer.
4. **Record the answer.** Edit the audit file: append `- [A] <rationale>` under the `[Q]`.
5. **Offer to fix if applicable.** If the answer implies a clear, easy edit and the surrounding context is fresh, offer to apply it now: `Want me to make that change while we have the context?`
   - On confirmation, apply the edit. Append `- [FIXED] <what changed>` under the `[Q]`.
   - **After the edit, offer to commit via the `/commit` skill, staging only the files touched by this fix.** One commit per question keeps the diff focused and reviewable.
6. **Or mark as SKIP.** If the user dismisses the question, append `- [SKIP] <reason>` instead of (or alongside) `[FIXED]`. The `[A]` still gets the rationale.
7. **Move on.** Ask `Ready for the next question?` After they confirm, remind: `Your answers are persisted in the audit file — feel free to /compact to free context before the next question.`
8. **Repeat** until every `[Q]` has an `[A]`.

### When all questions are answered

After the last `[A]`, congratulate completion and offer the fix phase: `All questions answered. Want to start a specific [FIX]/[REFACTOR], or YOLO fix all of them?`

- **Specific**: user names which item to address; walk it like a Q&A item (apply, commit, recommend compact).
- **YOLO fix all**: walk every `[FIX]`/`[REFACTOR]` in order. After each fix, commit it via `/commit` (one fix per commit) before moving to the next. Apply the same compact recommendation between fixes.

After an item is fixed, annotate it the same way as a Q&A item: `- [FIXED] <what changed>` (and `- [SKIP] <reason>` if the user decides not to fix it).

When the user is done (all `[Q]`s answered, fixes applied to taste), the skill is done. The main agent now has full context for any remaining work in normal chat.

---

## Execution rules

### Phase 1 (read-only)

- **Report only.** No edits, no commits in Phase 1. The audit file is the only artifact.
- **Always parallel.** Spawning auditors sequentially defeats the purpose.
- **Default base is `develop`.** Project convention. Don't ask, don't default to anything else.
- **Don't expand `KNOWLEDGE.md` mid-run.** If an auditor wants to flag something not in the knowledge file, it surfaces it as `[Q]` and the user decides whether to add a rule.

### Phase 2 (user-directed)

- **Edits and commits ONLY on per-item user confirmation.** No batching without per-item go-ahead.
- **All commits via the `/commit` skill.** Never shell out to `git commit` directly. (Project rule: "Always use the `/commit` skill when creating commits.")
- **Recommend `/compact` between questions and between YOLO fixes** — frame it as state-preserving (answers/fixes are saved in the audit file and in commits).
- **Don't suppress questions for brevity.** Every `[Q]` gets at least a short conversation. The user can always say "skip with [SKIP]" mid-conversation.
- **Don't auto-skip a question** because it seems trivial. Every `[Q]` deserves at least a brief check-in.
- **If a question reveals a knowledge gap** (recurring false positive, rule that should be tightened), surface it to the user — they may want to update `KNOWLEDGE.md` directly. Don't auto-edit.
- **Verification commands respect the container environment.** Per `.claude/CLAUDE.md`, `mypy` / `pytest` / `poetry` run via `hostcmd` on the host (e.g. `hostcmd poetry run mypy src/`, `hostcmd poetry run pytest tests/ -v`). Git runs directly in the container.
