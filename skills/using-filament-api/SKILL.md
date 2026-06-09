---
name: using-filament-api
description: >-
  Use when writing Python code that uses the `filament` package / FilamentClient
  — connecting to the Filament backend, building typed query Conditions, reading
  entity data, creating/updating/deleting entities, finding entity/connection/enum
  types, or setting up real-time subscriptions over SignalR.
---

# Using the Filament Python API

This skill helps write correct code against the `filament` Python package — an
async client for the Filament platform over SignalR.

Detailed, authoritative reference documentation is bundled with this plugin under
`${CLAUDE_PLUGIN_ROOT}/docs/`. **Read the relevant page(s) before answering** —
they contain exact type names, field tables, and worked examples. Do not guess
API shapes from memory.

## How to use this skill

1. Identify which task the user is doing (connect, query, read, write, subscribe,
   or find a type).
2. Read the matching reference file(s) below with the Read tool, resolving
   `${CLAUDE_PLUGIN_ROOT}` to this plugin's installed location.
3. Write code that follows the patterns and conventions in those docs.

## Reference map

| Task | Read this |
| ---- | --------- |
| Install, connect, authenticate, async lifecycle, error handling | `${CLAUDE_PLUGIN_ROOT}/docs/getting-started.md` |
| Find an entity type, connection type, or enum; naming conventions; base classes | `${CLAUDE_PLUGIN_ROOT}/docs/finding-types.md` |
| Look up a specific entity/connection/enum and its exact fields and types | `${CLAUDE_PLUGIN_ROOT}/docs/type-reference.md` (auto-generated, exhaustive) |
| Read data: queries, ordering, paging, projections, counting, parsing results | `${CLAUDE_PLUGIN_ROOT}/docs/reading-data.md` |
| Build query filters with the typed `Condition` builder (and the untyped API) | `${CLAUDE_PLUGIN_ROOT}/docs/conditions.md` |
| Create, update, or delete entities; partial updates / `specified_properties` | `${CLAUDE_PLUGIN_ROOT}/docs/modifying-data.md` |
| Real-time subscriptions and processing-lifecycle events | `${CLAUDE_PLUGIN_ROOT}/docs/subscriptions.md` |
| Typed `DataOption` key-value options | `${CLAUDE_PLUGIN_ROOT}/docs/data-options.md` |
| `FilamentClient` method reference, the response envelope, exceptions | `${CLAUDE_PLUGIN_ROOT}/docs/api-reference.md` |

Start from `${CLAUDE_PLUGIN_ROOT}/docs/README.md` for an index and a 60-second
tour if you are unsure where to look.

## Key conventions to honour

These trip people up most often — confirm against the docs as you write:

- **Every API method is a coroutine** on `FilamentClient` and must be `await`ed,
  inside an `async with filament.FilamentClient(...) as f:` block.
- **Calls return a success/error envelope, they do not raise** for request
  failures. Check `result.success` (and `result.data is not None`) before using
  the payload. Only connection/authentication failures raise exceptions.
- **snake_case in Python, camelCase on the wire.** The typed `Condition` builder
  and entity attributes use snake_case; the `properties` projection list,
  `update_properties`, and the untyped condition constructors take camelCase wire
  names. See `docs/finding-types.md`.
- **Prefer the typed `Condition(EntityClass).prop("field")` builder** over the
  untyped constructors so field names and value types are validated.
- **Use `filament.as_entity(wrapped.entity, SomeEntity)`** to downcast results to
  a concrete type; it returns `None` on a mismatch.
- **Updates and deletes are partial**: construct the entity with just `id` plus
  the fields you are changing — only fields you pass to the constructor are sent.

## Runnable examples

Complete scripts live at `${CLAUDE_PLUGIN_ROOT}/examples/` (e.g. `create_user.py`,
`read_with_conditions.py`, `subscribe_users.py`). Point users at the one that
matches their task.
