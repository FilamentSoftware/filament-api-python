# Audit Branch — Considerations

This is a **considerations guide**, not a mechanical checklist. Each focus captures the architecture lessons a contributor would naturally absorb after months of working in this codebase — the patterns that matter, why they matter, and the characteristic shapes that betray a wrong call. The audit's job is to recognize the lesson, not pattern-match the literal symptom.

**Two things to keep in mind as you work this document**:

1. **Rules describe shapes, not exhaustive enumerations.** A symptom listed under a rule is one example of how a lesson surfaces in code. Look for the lesson, not just the literal symptom. If the architecture is wrong but the wrongness shows up in a way the rule doesn't list verbatim, surface it anyway — that's what `[Q]` is for.
2. **Read the reference files first, audit second.** Each focus points at concrete source files (and `.claude/CLAUDE.md` sections). This repo has **no separate prose architecture docs** — the source files and CLAUDE.md *are* the docs. Read them before scanning the branch; internalize *why* the convention exists and what "doing it right" looks like. Then audit.

This is the **`filament-api-python` client library** — an async Python SignalR client (Python 3.10–3.13, strict mypy, Poetry) that talks to the C# Filament backend. There is no rendered UI: "drift" means deviation from code conventions, async/SignalR patterns, the wire-format contract with the backend, and the public Python API — never visual change. When in doubt about whether something deserves a finding, prefer surfacing a thoughtful `[Q]` over silence — the orchestrator's anti-rule (`do not suppress findings for brevity`) is here for that reason.

Source of truth for what `/audit-branch` audits. Each section maps to one focus area / one parallel auditor.

---

## public-api

**References**: `src/filament/__init__.py` (the package-level export list), `src/filament/_client.py` (`FilamentClient` — the headline class), `src/filament/_condition_builder.py` (`Condition`), `src/filament/_exceptions.py`, `src/filament/_specified_properties.py` (`SpecifiedPropertiesBase`); `.claude/CLAUDE.md` "Architecture" section.

**Scope**: Changes that alter the package's importable surface — anything a downstream user could `from filament import X` or rely on as a stable signature.

### What this focus is really about

The package exports a small, curated set of names from `filament/__init__.py`'s `__all__` — currently `FilamentClient`, `Condition`, the three `FilamentError*` exceptions, `SpecifiedPropertiesBase`, and the `as_entity` helper. **Anything in `__all__` is part of the public contract** — renaming, removing, or changing a signature is a SemVer-meaningful break. New public symbols on the other hand must be deliberately added to `__all__` and `__init__.py`; otherwise they're not really exported even if technically importable from the underscored module.

The companion contract is `FilamentClient`'s **method signatures**: organization/api-key/server/port/path/connection_timeout/max_size ctor params, the seven hub-callable methods (`get_entity_permissions`, `get_entity_property_permissions`, `read_data`, `modify_data`, `subscribe`, `unsubscribe`, `unsubscribe_all`), and the three `on_*` event registrations.

### Rules

- **A new public symbol must be added to `__all__` in `src/filament/__init__.py` and imported there**, not left to be reached only via `from filament._client import …`. The leading underscore on `_client.py` / `_condition_builder.py` / etc. signals "private module; reach through the package re-export instead." Forgetting to re-export is `[FIX]` (the convention is documented by the file naming itself and by `__init__.py`'s shape).
  - Grep hint: any new public class/function in `src/filament/*.py` whose name isn't listed in `__all__` of `src/filament/__init__.py`.

- **Public method signatures on `FilamentClient` follow the established async-result shape.** Each hub-invoking method is `async def`, takes the request dataclass (camelCase serialised via `to_dict`), awaits `self._invoke("MethodName", request_dict)`, deserialises through `from_dict(result_dict, ValueOrErrorPublic_1[T])`, and returns `_deserialization_error()` on a `TypeError`/`ValueError`/`KeyError` or `None` result (canonical: `read_data`, `modify_data`, `get_entity_permissions`, `get_entity_property_permissions`). A new public method that returns a bare `dict` / raw `Any` / `T` directly without the `ValueOrErrorPublic_1` wrap is `[Q]` (does it want the error channel, or does it genuinely never fail?).

- **Removing or renaming an exported symbol — or changing an existing public signature — is mandatory `[Q]`.** Even if "obviously safer" (e.g. tightening a parameter type), the consumer surface is what matters. The auditor cannot decide alone whether the break is acceptable.
  - Grep hint: any deletion or rename of a name listed in `__all__`; any signature change to a `FilamentClient` public method.

- **New ctor parameters on `FilamentClient` must be keyword-default-compatible.** Existing call sites pass `organization_id`, `api_key_name`, `api_key` positionally and may pass `server`/`port`/`path`/`connection_timeout`/`max_size` as kwargs. A new ctor param inserted before existing ones, or one without a default, is `[FIX]` (breaks backward-compatible construction) — unless `[Q]` is warranted because the absence of a default is intentional (a required new credential, say).

- **Public types must come from the generated module or be exported by name.** Public methods that take/return undocumented private types (e.g. a class defined inside `_client.py` and not re-exported) push a hidden contract on the caller — `[Q]`. The pattern is: take/return generated dataclasses from `filament.types.generated.misc` (or `types.generated.entities.*`), or the hand-rolled types in `types/conditions.py` / `types/options.py`, or the public exceptions.

### Common false positives

- Adding an internal helper to `_client.py` that is not exported — fine, that's why the module is underscored.
- Tightening type hints on private methods (`_invoke`, `_handle_*`) — not part of the public surface.

---

## client-lifecycle

**References**: `src/filament/_client.py` — read the full `FilamentClient` class, especially `__init__`, `__aenter__`, `__aexit__`, `_invoke`, `_send_client_hello`, `_handle_close`, and the registration of `_signalr_client.on(...)` event handlers; `.claude/CLAUDE.md` "Architecture".

**Eager-load**: `src/filament/_client.py` in full before reading any branch change to it.

**Scope**: The `FilamentClient` async-context-manager lifecycle and the request/response plumbing it owns.

### What this focus is really about

`FilamentClient` is an async context manager wrapping a `pysignalr.SignalRClient`. The pattern is non-trivial because pysignalr is **fire-and-forget by default** — you `send()` a method, you don't `await` the response. To give callers an awaitable API, `FilamentClient` wraps each invocation in an `asyncio.Future`, registers it in `_pending_invocations`, passes the on-completion callback into `send(..., on_invocation=…)`, and waits on the future. The lifecycle has to keep that machinery honest:

- The connection has to be established (`__aenter__` waits on `_connected` Event vs the run task vs a timeout) before any invocation can land.
- An auth failure that arrives during the handshake must abort `__aenter__` with `FilamentAuthenticationError`, not let the caller proceed to a broken connection.
- A websocket close (e.g. close-code 1009 — message too big) must fail all `_pending_invocations` so awaiting callers don't hang.
- `__aexit__` must cancel the background `_run_task` even if the body raised, suppressing the `CancelledError`.

### Rules

- **A new public hub method follows the established `_invoke → from_dict → return-or-deserialization-error` shape.** Canonical:

  ```python
  async def read_data(self, request: ReadDataRequest) -> ValueOrErrorPublic_1[ServiceDataRead]:
      request_dict = to_dict(request)
      result_dict: Any = await self._invoke("ReadData", request_dict)
      try:
          result = from_dict(result_dict, ValueOrErrorPublic_1[ServiceDataRead])
      except (TypeError, ValueError, KeyError):
          return _deserialization_error()
      if result is not None:
          return result
      return _deserialization_error()
  ```

  Deviations to flag: forgetting the `try/except (TypeError, ValueError, KeyError)` around `from_dict` (`[FIX]` — the helper exists precisely so deserialisation glitches never raise to the caller); forgetting the `if result is not None` fallback (`[FIX]`); calling `self._signalr_client.send(...)` or `.invoke(...)` directly instead of `self._invoke(...)` (`[Q]` — bypasses pending-invocation tracking, so a disconnect would leave the caller hanging).
  - Grep hint: new `async def` method on `FilamentClient` that calls `_signalr_client.send` / `.invoke` directly, or omits the `from_dict` try-block.

- **New incoming server events register a handler in `__init__` via `self._signalr_client.on("EventName", self._wrap_handler(self._handle_event_name))`** alongside the existing six (`AuthenticationFailed`, `DataUpdated`, `DataUpdateRejected`, `ProcessingStarted`, `ProcessingCompleted`, `ProcessingFailed`, `KeepAlive`). The handler should: deserialise via `from_dict(...)` inside `try/except (TypeError, ValueError, KeyError)` with `logger.exception` on failure, no-op on `None`, then dispatch to user callbacks via `_invoke_callback` inside a per-callback `try/except Exception` that calls `logger.exception`. A new handler missing the deserialisation guard, the `None` check, or the per-callback exception isolation is `[FIX]` (one bad callback would otherwise take down the whole event dispatch).
  - Grep hint: a new `self._signalr_client.on("…", …)` registration in `__init__`; a new `async def _handle_…(self, data: Any)` method.

- **`__aenter__` / `__aexit__` must keep the background `_run_task` accounted for.** `__aenter__` creates `_run_task = asyncio.create_task(self._signalr_client.run())` and waits with `asyncio.wait({wait_connected, self._run_task}, timeout=…, return_when=FIRST_COMPLETED)`. If the connect future isn't done, the task is cancelled and awaited under `contextlib.suppress(asyncio.CancelledError)`. `__aexit__` does the same cancel-and-suppress. A new code path in `__aenter__` that bails (raises) without cancelling `_run_task`, or `__aexit__` that doesn't suppress `CancelledError`, leaks a task and is `[FIX]`.
  - Grep hint: a `raise` inside `__aenter__` that isn't preceded by `self._run_task.cancel()` + an `await` of the run task under `contextlib.suppress`.

- **`_send_client_hello` is sent once per connection, before returning from `__aenter__`.** It advertises `max_size` to the backend and tolerates an "older server" by warning-and-continuing on any non-connection exception (only `FilamentConnectionError` re-raises). A new branch that swallows `FilamentConnectionError` here, or fails noisily on a backwards-compat scenario the comment already authorises, is `[Q]`.

- **The constructor wires `_signalr_client.on_close(self._handle_close)`** so a disconnect fails every `_pending_invocations` future with `FilamentConnectionError` and discards any in-flight `_part_buffers` / `_poisoned_part_buffer_keys`. A new feature that adds a similar "lives across the connection" piece of state (a future, a buffer, a registry) without a parallel cleanup in `_handle_close` is `[Q]` — a reconnect or close would leave it stranded.

### Common false positives

- A private helper method added to `_client.py` for a single call site — doesn't need the full `_invoke` shape.
- A change that only touches event handler internals (not registration) — the registration list in `__init__` is the canonical entry point.

---

## serialization

**References**: `src/filament/_serialization.py` (the `to_dict` / `from_dict` engine), `src/filament/_wire_formats.py` (Instant + Duration string formats); `.claude/CLAUDE.md` "Architecture → Serialization".

**Eager-load**: `src/filament/_serialization.py` **and** `src/filament/_wire_formats.py` in full before reading any branch change to either. The wire-format contract with the C# backend is encoded in both files; misreading one tends to break round-trip.

**Scope**: Changes to the serialization / wire-format conversion path. This is the contract layer with the backend — drift here causes silent wire-format mismatches that ship and only surface in production.

### What this focus is really about

`_serialization.py` converts Python dataclasses ↔ camelCase dicts for the SignalR wire format. It handles:

- **Case conversion** via `camel_case_from_snake_case` / `snake_case_from_camel_case` — Python uses snake_case, the wire uses camelCase.
- **`from __future__ import annotations`** by resolving string annotations through `typing.get_type_hints` (without `globalns` so each base class's annotations are resolved in its own module).
- **`uuid_utils.UUID`** (not the stdlib `uuid.UUID` — the codebase deliberately chose `uuid_utils` for uuid7 support).
- **NodaTime alignment**: `datetime.datetime` maps to Instant (`"uuuu-MM-ddTHH:mm:ss.ffffffffZ"`, 8 fractional digits + Z); `datetime.timedelta` maps to Duration (`"[-]D:HH:MM:SS[.FFFFFFFFF]"`). Both formats are defined as `DATETIME_FORMAT` and the `instant_*` / `timedelta_*` helpers in `_wire_formats.py`.
- **Enum naming**: the backend's `None` enum member is generated as `None_` in Python (since `None` is a keyword); `_enum_from_wire` maps the wire string `"None"` back to `_NONE_MEMBER = "None_"`, and `_enum_to_wire` emits the int `.value` for the on-the-wire form.
- **Polymorphic dispatch** for `DbConditionInstance` (via `condition_to_dict` / `condition_from_dict` in `types/conditions.py`) and `DataOption` (via `option_to_dict` / `option_from_dict` in `types/options.py`).
- **`$type` field for `EntityBase` subclasses** — `to_dict` emits `result["$type"] = data.__class__.__name__` and `from_dict` resolves the concrete subclass via `_resolve_entity_type`.
- **`specified_properties` partial-update support** — if `getattr(data, "specified_properties", None)` is a list, `to_dict` only emits those fields (see `_specified_properties.py`).

### Rules

- **Wire-format strings live in `_wire_formats.py` and only there.** A new module that hand-formats a datetime or timedelta with `strftime` / arithmetic instead of calling `instant_to_wire` / `timedelta_to_wire` is `[REFACTOR]` (duplicates the format string), and changing the format string in either direction without changing the C# backend's converter is `[Q]` — observable contract break with the server.
  - Grep hint: a new `strftime("%Y-%m-%dT…")` or `f"{...}:{...}:{...}.{nanoseconds:09d}"` outside `_wire_formats.py`.

- **Use `uuid_utils.UUID`, not the stdlib `uuid.UUID`.** The codebase imports `from uuid_utils import UUID, uuid7` everywhere. A new `import uuid` for the `UUID` type is `[FIX]` (`_is_uuid_type` only checks against `uuid_utils.UUID` — a stdlib `uuid.UUID` field would silently bypass conversion and reach the wire as `repr(uuid)`).
  - Grep hint: `^import uuid\b` or `from uuid import UUID` in branch-added/modified `*.py`.

- **`Optional[X]` over `X | None`.** The codebase consistently uses `Optional[X]` (imported from `typing`) and the serialization engine matches on both `Union[X, None]` and the PEP-604 `X | None` syntax for safety, but the project convention is `Optional[X]` (see every module). A new file using `X | None` where `Optional[X]` is the established sibling style is `[REFACTOR]`. (Not a `[FIX]` — both forms are valid Python 3.10; this is a style/consistency call.)

- **Round-trip discipline.** A change that alters how a Python type is serialised must update both `_convert_to_dict_value` *and* `_convert_from_dict_value` symmetrically; a one-sided change is `[Q]` — `to_dict` + `from_dict` are the round-trip the tests pin, and one-sided changes silently corrupt data the engine *thinks* it can re-read.

- **`$type` dispatch follows the established `EntityBase` walk.** `_resolve_entity_type` walks `EntityBase.__subclasses__()` recursively. A change that hard-codes a subclass map, or matches on a string other than `__name__`, is `[Q]` (the wire `$type` is the C# `TypeNameHandling.Auto` short name = `__class__.__name__`).

- **`specified_properties` is a contract with `SpecifiedPropertiesBase`.** `to_dict` reads `getattr(data, "specified_properties", None)`; if it's a list, only those fields serialise. `from_dict` clears the attribute on the result so a deserialised entity round-trips with every field included. A new path that mutates `specified_properties` from outside these two helpers is `[Q]`.

- **Generated-code drift via serialization changes.** A serialization change that papers over a missing case in the generator (e.g. adding an `if isinstance(data, NewType)` branch in `_convert_to_dict_value` to handle a type the generator should have emitted differently) is `[Q]` — the generated code is the source of truth (see `generated-code` and the `.claude/CLAUDE.md` "Generated Code" section). Fixing serialization to hide a codegen gap papers over a real upstream bug.

### Common false positives

- Internal helper renames inside `_serialization.py` — convention is whatever the file says.
- A new `_is_<type>_type` helper that follows the existing pattern.

---

## conditions

**References**: `src/filament/types/conditions.py` (the `DbConditionInstance` dataclass hierarchy + `condition_to_dict` / `condition_from_dict` polymorphic serialisation + `and_` / `or_` / `not_` / `equal_to` / ... convenience constructors), `src/filament/_condition_builder.py` (the typed `Condition(EntityClass).prop("field")` API and the four `DbConditionProperty` / `…Comparable…` / `…List…` / `…Dict…` property wrappers), `src/filament_mypy_plugin.py` (the static narrowing — see also the `mypy-plugin` focus).

**Eager-load**: `src/filament/types/conditions.py` + `src/filament/_condition_builder.py` in full before auditing.

**Scope**: Anything under `src/filament/types/conditions.py` or `src/filament/_condition_builder.py`, and the C# backend's `DbConditionInstance*` hierarchy whenever the branch changes the wire-shape.

### What this focus is really about

`DbConditionInstance` and its subclasses (`DbConditionNot`, `DbConditionCombine`, `DbConditionComparison`, `DbConditionBetween`, `DbConditionComparisonArray`, `DbConditionArrayComparison`, `DbConditionContains`, `DbConditionLink`) mirror the C# backend's `DbConditionInstance` hierarchy on the wire. The `condition_*` functions handle polymorphism manually (no dataclass `$type` field; the wire uses a string discriminator like `EqualTo` / `In` / `And`). `_condition_builder.py` sits on top with the typed `Condition(Entity).prop("field").equal_to(value)` API that drops back to one of those concrete dataclasses on wire-out. The mypy plugin narrows `Condition.prop()`'s return type at type-check time based on the field's annotated type — comparable scalar → `DbConditionComparableProperty[T]`, list → `DbConditionListProperty[T]`, dict → `DbConditionDictProperty[K, V]`, anything else → plain `DbConditionProperty[T]`.

The three pieces — runtime hierarchy, builder, mypy plugin — must stay in sync.

### Rules

- **Wire format is a stable contract with the C# backend.** Changing a `DbConditionType` enum value's string (e.g. renaming `"EqualTo"` to `"Equals"`), reordering polymorphic dispatch in `condition_to_dict`/`condition_from_dict`, or adding a new `DbCondition*` subclass without wiring up both directions is `[Q]` — the C# side has a matching converter that has to be updated in lockstep. The auditor cannot verify the backend change exists.
  - Grep hint: edits to `DbConditionType`/`DbConditionMode` enum members; new `DbCondition*` dataclass; edits to the `condition_to_dict` / `condition_from_dict` switch.

- **A new builder method needs both a runtime wrapper and a mypy-plugin narrowing.** If a new `DbConditionProperty.*` method is added that should only apply to (say) list fields, it belongs on `DbConditionListProperty` — and the mypy plugin must already route list-typed fields to `DbConditionListProperty`. Adding a method to the wrong wrapper, or adding a new property-wrapper subclass without a corresponding branch in `filament_mypy_plugin.py`, is `[FIX]` (the mypy plugin would silently widen back to base `DbConditionProperty[Any]`).
  - Grep hint: a new property-wrapper subclass; a new method on an existing wrapper that takes args the base contract doesn't.

- **Value coercion goes through `_value_to_wire` / `_values_to_wire`.** A new builder method that calls `str(value)` or `value.strftime(...)` directly is `[REFACTOR]` (the helpers handle bool-before-int, Enum, UUID, datetime+Duration in one place and stay aligned with `_wire_formats.py`'s `DATETIME_FORMAT`). The `_values_to_wire` "scalar string" guard (`isinstance(values, (str, bytes))`) is deliberate — silently passing `"abc"` instead of `["abc"]` would be a confusing footgun; a new wrapper method that takes a sequence and skips this guard is `[FIX]`.

- **Negation goes through `DbConditionNot` and `not_`.** Property wrappers carry an `_invert` flag and apply it via `self._wrap(...)` — they don't construct `DbConditionNot` ad-hoc except where wire-shape requires it (e.g. `not_null`). A new builder method that adds its own inversion path bypassing `self._wrap` is `[Q]` (the `not_` cascade is the canonical pattern, and the double-negation case in `not_null` is the established model).

- **`Condition.prop("name")` only validates that the field exists at runtime.** Static narrowing comes from the mypy plugin (see `mypy-plugin`). A change that adds extra runtime validation to `_classify_field` — type-checking the value at builder-call time — is `[Q]`: it may break consumer code the plugin already accepts (and the value is in any case re-checked at the C# side).

- **Condition wire-shape changes are codegen-surface adjacent.** A new entity field in `types/generated/entities/*.py` whose type doesn't fit one of `_COMPARABLE_TYPES` / `list[X]` / `dict[K,V]` may need a new property-wrapper category — see `codegen-surface`.

### Common false positives

- A new convenience constructor in `types/conditions.py` (`like`, `between`, etc.) that wraps an existing `DbCondition*` dataclass without changing its shape — fine.
- Re-export tweaks in `__all__` for `types/conditions.py` — fine if they match the export list shape.

---

## mypy-plugin

**References**: `src/filament_mypy_plugin.py` (the single-file plugin — note it lives at the top of `src/`, not inside the `filament/` package, to avoid a runtime import cycle), `pyproject.toml` (`plugins = ["src/filament_mypy_plugin.py"]` registration), and `src/filament/_condition_builder.py` (the symbols it narrows).

**Eager-load**: `src/filament_mypy_plugin.py` in full.

**Scope**: Anything under `src/filament_mypy_plugin.py`, and changes to `_condition_builder.py` that affect the symbols the plugin matches.

### What this focus is really about

The plugin narrows `Condition.prop()`'s return type at type-check time based on the entity's field annotation. It uses `MethodContext` to look at the literal string argument, find the field on the entity TypeInfo, then build a synthetic `Instance` of one of `DbConditionProperty` / `DbConditionComparableProperty` / `DbConditionListProperty` / `DbConditionDictProperty`. All of this hinges on stable **fullname strings** matching the runtime class paths (`filament._condition_builder.DbConditionProperty`, etc.) and the runtime `_PROP_FULLNAME = "filament._condition_builder.Condition.prop"` exactly.

The plugin is **registered via `pyproject.toml`** (`[tool.mypy] plugins = ["src/filament_mypy_plugin.py"]`); without registration the entire static-narrowing layer silently disappears.

### Rules

- **Fullname constants must match the runtime fully-qualified names.** Renaming `Condition` / `prop` / a `DbConditionProperty*` class, or moving any of them to a new module, requires the matching fullname constant in the plugin to be updated in lockstep. A rename in `_condition_builder.py` without a plugin update is `[FIX]` — the plugin would still load (no error), but `Condition.prop("…")` would silently widen to `Any`. A plugin update without the rename is the inverse `[FIX]`.
  - Grep hint: any rename / move of `Condition`, `prop`, `DbConditionProperty`, `DbConditionComparableProperty`, `DbConditionListProperty`, `DbConditionDictProperty`; conversely, edits to `_PROP_FULLNAME` / `_PROPERTY_CLASS` / `_COMPARABLE_CLASS` / `_LIST_CLASS` / `_DICT_CLASS`.

- **`_COMPARABLE_FULLNAMES` must mirror the runtime `_COMPARABLE_TYPES` set in `_condition_builder.py`.** The runtime set is `{int, float, str, bool, uuid_utils.UUID, datetime.datetime, datetime.timedelta}`; the plugin's fullnames set has the corresponding string fullnames (including `uuid_utils._uuid.UUID` as the internal location). A new comparable type added in one without the other is `[FIX]` — the runtime would say "comparable" while the plugin widens it to plain `Property`, or vice versa.

- **The plugin registration must stay in `pyproject.toml`.** Removing `plugins = ["src/filament_mypy_plugin.py"]` from `[tool.mypy]`, or moving the file without updating the path, silently disables the entire static layer. Branch edits to `pyproject.toml` that touch this line are `[Q]` regardless of direction.

- **The plugin file must live outside the `filament/` package** (see the comment in `.claude/CLAUDE.md` "Architecture → Mypy plugin"). Moving it to `src/filament/` reintroduces the circular-import risk that drove the original location. Such a move is `[FIX]` to revert, or `[Q]` if the author specifically reasoned through the import order.

- **Imports from `mypy.*` must come from the documented API surface.** The current plugin uses `mypy.nodes.TypeInfo`/`Var`, `mypy.plugin.MethodContext`/`Plugin`, and `mypy.types.*`. Reaching into mypy internals (anything not in those modules) is `[Q]` — mypy versions break on internal API.

### Common false positives

- Adding a docstring / comment.
- A new helper in the plugin that follows the existing `_extract_str_literal` / `_unwrap_optional` shape.

---

## generated-code

**References**: `.claude/CLAUDE.md` "Generated Code" (the DO NOT EDIT rule), the marker `# Generated by Filament GenerateCode` at the top of every generated file, `scripts/generate_code.py` / `.sh` / `.cmd` (the codegen runner), the generated tree `src/filament/types/generated/**`.

**Scope**: Any change to a file under `src/filament/types/generated/**`.

### What this focus is really about

Files under `src/filament/types/generated/**` are auto-generated from the C# backend by the `GenerateCode` tool. The first line of every generated file is the marker `# Generated by Filament GenerateCode`. **The DO NOT EDIT rule is in `.claude/CLAUDE.md`:**

> Files in `src/filament/types/generated/` are **auto-generated** and must **never** be edited by hand. … 1. Do not modify the generated files. Any manual changes will be overwritten by the next codegen run. 2. Identify what needs to change and describe the fix in terms of the code-generation logic.

So the auditor's job here is **not** to apply the regular style rules to these files (they may contain whatever the generator emits). It is to catch a hand-edit masquerading as a generated change — the change *will* be wiped out, and worse, will mask a real codegen bug.

### Rules

- **Any branch-introduced edit under `src/filament/types/generated/**` is `[Q]`, not a `[FIX]`.** The auditor cannot tell from the file alone whether the edit came from a real codegen run (legitimate — a regen happened) or a hand-edit (drift — will be wiped). Two telltales help the reviewer answer:
  - **Was the change accompanied by other generated-file edits with a consistent shape?** A real regen touches many files at once, with a consistent diff structure (renames propagate, enum members reorder together, imports update in lockstep). A single-file targeted edit is almost certainly a hand-edit.
  - **Did the branch also change `scripts/generate_code.py` or the backend GenerateCode generators?** A regen usually follows a generator change. A regen with no upstream change is suspicious.
  - **Does the change still carry the `# Generated by Filament GenerateCode` marker on line 1?** If the marker was deleted, that's a strong tell that the file is being treated as hand-authored — `[Q]` to confirm.
  - Frame the question with consumer context: "this file is auto-generated; any hand-edit will be overwritten by the next codegen run. Was this change made by re-running `scripts/generate_code.py`, or hand-edited?"

- **Don't apply the `code-quality` rules to generated files.** The generator emits what it emits; mismatched style there is the generator's problem, not the branch's.

- **A change to non-generated code that exists to "fix" a wart in generated code is `[Q]`.** Per `.claude/CLAUDE.md`, the fix path is to identify the codegen bug and surface it to the user with a suggested generator change — not to wrap the generated symbol in a hand-coded shim. (E.g. a new `from filament.types.generated.entities.foo import Foo as _RawFoo; class Foo(_RawFoo): …` should be `[Q]` — why isn't the fix in the generator?)

### Common false positives

- A change to `src/filament/types/conditions.py` or `src/filament/types/options.py` — these live alongside `generated/` but are **hand-authored** (see the `conditions` focus). They are NOT generated.
- The `src/filament/types/__init__.py` re-exports — also hand-authored.

---

## codegen-surface

**References**: `scripts/generate_code.py`, `scripts/generate_code.sh`, `scripts/generate_code.cmd`, and the read-only mount of the backend repo at `D:\dev\FilamentSoftware\MVP\Filament` (host) → `/mnt/d/dev/FilamentSoftware/MVP/Filament` (container). The generator itself lives in the backend at `utils/GenerateCode/`.

### What this focus is really about

The Python side of codegen has two surfaces:

1. **The runner scripts** (`scripts/generate_code.{py,sh,cmd}`) — they shell out to the backend's `GenerateCode` project with `--output-path` pointing at `src/filament/types/generated/`. A change to the runner can break regen for downstream users.
2. **The Python-side shape the generator targets.** The generator emits dataclasses and enums into `types/generated/**` that depend on the runtime helpers in `_serialization.py` (`from_dict` / `to_dict`), `_wire_formats.py`, `types/conditions.py` (`DbConditionInstance`), `types/options.py` (`DataOption`), and `types/generated/base/base.py` (which imports `SpecifiedPropertiesBase` from `_specified_properties.py`). A non-generated change that breaks one of those assumptions (renames an import, changes a signature the generated code calls) **breaks the entire generated package** the next time someone regenerates and pulls.

This is informational/visibility — the auditor cannot verify the backend generator was updated.

### Rules

- **Flag, as `[Q]` / note, any change that the generator would need to know about** for the next regen to keep working:
  - Renaming/removing a symbol generated code imports — e.g. `DbConditionInstance` in `types/conditions.py`, `DataOption` in `types/options.py`, `EntityBase` / `Entity` / `EntityConnection` in `types/generated/base/base.py` (this file is generated but the parent class names are a contract with the generator), `SpecifiedPropertiesBase` in `_specified_properties.py`.
  - Changing a wire-format primitive in `_wire_formats.py` (the generated code emits `datetime.datetime` / `datetime.timedelta` fields that round-trip through these helpers via `_serialization.py`).
  - Changing the case-conversion or `None_` enum-name rules in `_serialization.py` — generated code currently emits `None_` as the enum member name and expects the wire to be `"None"` (see `_NONE_MEMBER` / `_NONE_WIRE` in `_serialization.py`).
  - Changing the `scripts/generate_code.{py,sh,cmd}` argument shape — downstream regen calls may break.

- **A change to a runner script (`scripts/generate_code.*`) is `[Q]`** unless the description is unambiguous. The runner is consumed by humans (and CI), not by the code itself; signature drift only surfaces at next run.

- **A new entity / enum / hub method in the C# backend will need a regen here** — but the auditor cannot see the backend in this audit pass (it's a separate repo, only mounted read-only). Note this as `[Q]` only when the branch *itself* changes a generated file (caught by `generated-code` instead) or changes a runner script.

### Common false positives

- A new entity that arrives via a generated-file edit — owned by `generated-code`, not here.
- A `from filament.types.generated.misc import X` in hand-coded code where `X` already existed — fine, no codegen impact.

---

## error-handling

**References**: `src/filament/_exceptions.py` (the three-class exception hierarchy), `src/filament/_client.py` (the `_deserialization_error()` and `_error_result_public()` helpers; the `ValueOrErrorPublic_1[T]` return shape on public methods), `src/filament/types/generated/misc.py` (`SuccessOrErrorPublic` / `ValueOrErrorPublic_1` / `SuccessOrErrorBase` definitions), `src/filament/types/generated/enums.py` (`ErrorCodes`).

**Eager-load**: `src/filament/_exceptions.py` and the `_deserialization_error` / `_error_result_public` helpers at the bottom of `src/filament/_client.py` before auditing.

**Scope**: Error-handling discipline across the public API.

### What this focus is really about

The client has **two error channels** that are deliberately not unified:

- **Raised exceptions** for connection / authentication / lifecycle failures the caller cannot pre-check: `FilamentError` (base), `FilamentAuthenticationError`, `FilamentConnectionError`. These are raised from `__aenter__` / `_invoke` and propagate.
- **Returned `SuccessOrErrorPublic` / `ValueOrErrorPublic_1[T]`** values for operation-level failures the server reports as data: bad request, not-found, permission-denied. These carry an `ErrorCodes` enum value and a message, never raise.

The split mirrors the C# `SuccessOrErrorPublic` / `ValueOrErrorPublic` contract. **A `from_dict` failure** on a server response falls back to the in-channel `_deserialization_error()` helper (which returns a `ValueOrErrorPublic_1` with `success=False, error_code=ErrorCodes.InternalError`) — it does **not** raise. Likewise `_error_result_public(message)` returns a `SuccessOrErrorPublic` with `success=False`.

### Rules

- **Public hub methods catch deserialization failures and return `_deserialization_error()`**. Canonical (see `_client.py`):

  ```python
  try:
      result = from_dict(result_dict, ValueOrErrorPublic_1[T])
  except (TypeError, ValueError, KeyError):
      return _deserialization_error()
  if result is not None:
      return result
  return _deserialization_error()
  ```

  A new public method that lets `from_dict` raise to the caller, or returns `None` instead of the deserialization-error sentinel, is `[FIX]` (callers expect `.success` / `.error_code` on every return).

- **Raise `FilamentError` subclasses only at the connection / lifecycle boundary.** `__aenter__` raises `FilamentAuthenticationError` / `FilamentConnectionError`; `_handle_close` populates `_pending_invocations` with `FilamentConnectionError`. A new `raise FilamentError` (or any new exception subclass) from a non-lifecycle path is `[Q]` — the operation channel is the return-value channel.

- **No bare `raise Exception(...)` for expected failure modes.** The hub-call path inside `_invoke` does `future.set_exception(Exception(message.error))` — that's the SignalR completion error (the server told us the invocation itself failed at the framework layer, not the operation layer), and it predates the `FilamentError` hierarchy. A new bare `Exception` outside that single site is `[Q]` (should it be a `FilamentError`, or a returned error?). Genuine programmer-error guards (`ValueError("Dictionary key must not be None")` in the condition builder, `TypeError` in `_value_to_wire` for an unsupported type) are fine — those guard misconfiguration, not runtime conditions.

- **`logger.exception` over `logger.error`** when an exception path has been caught. The codebase uses `logger.exception(...)` inside `except` blocks (e.g. `_handle_data_updated`, `_handle_processing_started`) so the traceback lands in the log. A new `except` block that uses `logger.error("...", exc)` is `[REFACTOR]`.

- **Don't widen `(TypeError, ValueError, KeyError)` to bare `except Exception`** in the deserialization fallback. The narrow tuple is deliberate — a bug elsewhere should still surface, only well-known wire-format glitches fall back to `_deserialization_error()`. A new `except Exception:` around `from_dict` is `[Q]`.

- **`ErrorCodes` values come from the generated enum** (`filament.types.generated.enums.ErrorCodes`). A new error-return path that hard-codes a string instead of using `ErrorCodes.InternalError` / `ErrorCodes.None_` / etc. is `[FIX]`.

### Common false positives

- Test code that raises arbitrary exceptions to simulate failure — out of scope.
- The `__post_init__` guard in `DbConditionInstance` raising `TypeError("DbConditionInstance cannot be instantiated directly…")` — that's a configuration / programmer-error guard, not a runtime failure mode.

---

## async-correctness

**References**: `src/filament/_client.py` — read the full class, focusing on `__aenter__` / `__aexit__`, `_invoke`, `_handle_close`, `_run_task`, `_pending_invocations`, and the async/sync callback dispatch via `_invoke_callback`.

**Scope**: asyncio patterns inside hand-written code — task lifecycle, cancellation discipline, awaitable safety, sync-vs-async callback handling, race conditions.

### What this focus is really about

The client is async-first and uses several non-obvious asyncio patterns:

- A background `_run_task = asyncio.create_task(self._signalr_client.run())` carries the SignalR loop; `__aenter__` and `__aexit__` cancel-and-await-with-`contextlib.suppress(asyncio.CancelledError)` to shut it down cleanly.
- `_invoke` wraps pysignalr's fire-and-forget `send()` with an `asyncio.Future` registered in `_pending_invocations`; the future is auto-removed via `future.add_done_callback(self._pending_invocations.discard)`.
- `_handle_close` fails every pending future with `FilamentConnectionError` so a websocket close doesn't strand awaiting callers.
- User callbacks may be **sync or async**, dispatched via `_invoke_callback` which uses `inspect.iscoroutinefunction(callback)` to choose `await callback(*args)` vs `callback(*args)`.
- Each event handler wraps its dispatch loop in a per-callback `try/except Exception` so a bad callback doesn't take down the whole fanout.

### Rules

- **A new background `asyncio.Task` (created with `asyncio.create_task(...)`) must have a lifecycle owner.** `_run_task` is owned by `FilamentClient` and cancelled-and-awaited in `__aexit__`. A new task created somewhere else (e.g. inside an event handler) without a stored reference and a matching cancel path is `[Q]` — Python will warn about "Task was destroyed but it is pending" and silently drop work.
  - Grep hint: a new `asyncio.create_task(` outside `__aenter__` whose return value isn't stored on `self`.

- **Cancellation paths suppress `asyncio.CancelledError`.** The canonical pattern is:

  ```python
  task.cancel()
  with contextlib.suppress(asyncio.CancelledError):
      await task
  ```

  A new `task.cancel()` followed by `await task` without the suppression, or by no await at all, is `[FIX]` (the first leaks an exception; the second leaks the cancel itself).

- **Pending-invocation futures are tracked in `_pending_invocations`.** A new awaitable that lives across the SignalR connection boundary (e.g. a custom request/response pair) must register itself in `_pending_invocations` so `_handle_close` can fail it. A path that creates a future and awaits it without that registration is `[Q]` — a disconnect leaves the caller hanging.

- **Callbacks may be sync or async — dispatch via `_invoke_callback`.** A new code path that dispatches a user-registered callback (the `Callback = Union[Callable[..., Any], Callable[..., Awaitable[Any]]]` alias) by `await callback(...)` directly is `[FIX]` — `_invoke_callback` exists exactly so callers can register either form.
  - Grep hint: a new `await callback(` where `callback` is one of `_client_callbacks`, `_client_error_callbacks`, `_on_processing_*_callbacks`.

- **Per-callback dispatch is isolated by `try/except Exception` + `logger.exception`.** A new fanout loop that doesn't isolate per-callback exceptions is `[FIX]` (canonical: every `_handle_processing_*` and `_dispatch_data_update`).

- **No blocking I/O on the async path.** A new `time.sleep`, blocking `requests.get`, sync `urllib`, or sync file I/O inside an `async def` (without `asyncio.to_thread`) is `[FIX]`. (`asyncio.sleep` and aiohttp/pysignalr awaitables are fine.)

- **`asyncio.Event` / `asyncio.Future` are bound to the current loop.** `__aenter__` creates `self._connected = asyncio.Event()` lazily because the loop only exists once `__aenter__` runs. A new `asyncio.Event()` / `asyncio.Future()` in `__init__` (no loop yet, or wrong loop on re-entry) is `[Q]`.

### Common false positives

- `await asyncio.sleep(0.1)` in `_on_open` for the auth-failure race — documented in the source comments.
- Helper functions that are `async def` purely for caller convenience without awaiting anything — fine.

---

## subscription-routing

**References**: `src/filament/_client.py` — `subscribe()`, `unsubscribe()`, `unsubscribe_all()`, `_handle_data_updated()`, `_buffer_part()`, `_merge_parts()`, `_dispatch_data_update()`, `_handle_data_update_rejected()`, `_handle_close()`. The invariants live in the four maps (`_backend_to_client`, `_client_to_backend`, `_client_callbacks`, `_client_error_callbacks`) and the two reassembly buffers (`_part_buffers`, `_poisoned_part_buffer_keys`).

**Eager-load**: the whole subscription/dispatch section of `_client.py` (roughly `_handle_data_updated` through `unsubscribe_all`).

**Scope**: Anything that touches the subscription tables or the chunk-reassembly buffers.

### What this focus is really about

The backend may return the **same subscription ID for duplicate type/permissions** subscriptions, but `subscribe()` returns a **per-call client UUID** so callers can `unsubscribe` independently. The mapping is:

- `_backend_to_client: dict[UUID, set[UUID]]` — one backend sub → many client subs.
- `_client_to_backend: dict[UUID, UUID]` — reverse lookup.
- `_client_callbacks: dict[UUID, Callback]` — per client sub.
- `_client_error_callbacks: dict[UUID, Callback]` — per client sub, optional.

Backend `Unsubscribe` is only called when the **last** client sub for a backend ID is removed.

**Multi-part `ServiceDataUpdate` reassembly**: the backend may split a logical update into N parts (`part_idx`, `part_count`, all sharing a `sequence_idx`). The client buffers parts by `(backend_sub_id, sequence_idx)` until all `part_count` arrive, then merges them via `_merge_parts` (envelope from `buffer[0]`, `modified_entities` per-type-extended on collision, output rewritten to `part_idx=0`, `part_count=1`). Malformed metadata **poisons** the `(sub, seq)` key — every subsequent frame for that key is dropped with a warning, even single-part ones.

### Rules

- **The four subscription maps move together.** A `subscribe` add must insert into `_backend_to_client`, `_client_callbacks`, optionally `_client_error_callbacks`, and `_client_to_backend`. An `unsubscribe` remove must pop from all four (and only call the backend `Unsubscribe` when the backend's client-set goes empty). A new code path that updates one map and forgets another is `[FIX]` — silent leak (callbacks never fire) or silent hang (backend never told to unsubscribe).

- **Reassembly buffers must be cleaned up alongside the subscription tables.** `unsubscribe` deletes any `_part_buffers` keys whose first element is the dying backend ID, and filters them out of `_poisoned_part_buffer_keys`. `unsubscribe_all` clears both buffers. `_handle_close` clears both buffers (a reconnect cannot resume the server's view of partial state). A new subscription-lifecycle path that adds a buffer but doesn't extend these cleanups is `[FIX]`.

- **Drop frames for unknown subscriptions BEFORE inserting into a buffer.** `_handle_data_updated` checks `backend_id not in self._backend_to_client` first, then validates `part_count >= 1`, then checks the poison set, then either fast-paths single-part or calls `_buffer_part`. The ordering matters — a post-unsubscribe part otherwise leaks into `_part_buffers` and can never be cleaned (no callback to dispatch to). A new branch that reorders these checks is `[Q]`.

- **`part_count`-disagreement poisons the `(sub, seq)` key.** Once a key is poisoned, every subsequent frame for it — including legitimate-looking single-part `part_count == 1` frames — is dropped with a `WARNING`. The check is in `_handle_data_updated` (ahead of both the fast path and `_buffer_part`). A new branch that starts a fresh buffer for a poisoned key, or skips the poison check on the fast path, is `[FIX]`.

- **`_dispatch_data_update` iterates `list(client_ids)` to allow callback-driven mutation.** The wrapping `list(...)` snapshots the set so a callback can `unsubscribe()` without invalidating the iterator. A new dispatch loop that iterates the raw set is `[FIX]` (a callback `unsubscribe` would raise `RuntimeError: Set changed size during iteration`).
  - Same pattern in `_handle_data_update_rejected` (`for client_id in list(client_ids)`).

- **`_handle_data_update_rejected` warns when no `on_error` is registered.** This is the only signal the user gets that a server-rejected update was silently dropped (the data is gone — the server won't retry). A new branch that drops the rejection silently (no callback, no log) is `[FIX]`.

- **A new "lives across a connection / subscription" piece of state needs cleanup in `_handle_close` *and* `unsubscribe` *and* `unsubscribe_all`.** The pattern in this focus area (and `client-lifecycle`) is that there are exactly three sites that tear down per-subscription state — adding a fourth without updating all three is `[Q]`.

### Common false positives

- A new `_handle_<event>` that doesn't touch subscriptions at all — out of scope.
- Logging adjustments that don't change the control flow.

---

## code-quality

**References**: `.claude/CLAUDE.md` "Code Conventions", `pyproject.toml` (`[tool.mypy]` strict=true, python_version="3.10"), and any existing file in `src/filament/` as a style reference.

**Scope**: Python language-level conventions across all `*.py`.

### Rules

These are codified in `.claude/CLAUDE.md` and `pyproject.toml`:

- **Strict mypy.** *"all code must pass `mypy --strict`"* (`.claude/CLAUDE.md` Code Conventions). A new file or signature that the existing strict mypy run would reject is `[FIX]`. The CI workflow `.github/workflows/mypy-check.yml` enforces this.
  - Common strict-mypy violations to flag: missing return type annotation on `def`/`async def`; missing parameter annotation; `Any` used where a more specific type fits; `# type: ignore` without a code (the strict bar is `# type: ignore[code]`); silently-untyped third-party imports without a `[[tool.mypy.overrides]]` entry.
  - Grep hint: `def \w+\([^)]*\)\s*:` (no `->`) for missing return annotations in branch-added code; `# type: ignore$` for code-less ignores.

- **Type annotations on every function signature.** *"Type annotations on all function signatures"* (`.claude/CLAUDE.md`). Subset of strict mypy but worth calling out — any new `def`/`async def` lacking a fully-annotated signature is `[FIX]`.

- **`from __future__ import annotations` at the top of every module.** *"Use `from __future__ import annotations` for forward references"* (`.claude/CLAUDE.md`). Every existing file has it; a new module without is `[FIX]`. Beware: with `from __future__ import annotations` all annotations are strings, so `typing.get_type_hints` is required to resolve them — see how `_serialization.py` and `_condition_builder.py` do it.
  - Grep hint: new `.py` file under `src/` where `head -10` doesn't contain `from __future__ import annotations`.

- **Python 3.10 baseline; avoid 3.11+ syntax.** *"Python 3.10, 3.11, 3.12, 3.13 — avoid syntax only available in 3.11+ (e.g., `type` statement, `ExceptionGroup`)"* (`.claude/CLAUDE.md`). The CI matrix in `.github/workflows/run-unit-tests.yml` runs all four versions; 3.10-only syntax breaks the matrix. Concrete bans:
  - `type X = …` statement (PEP 695) — use `X: TypeAlias = …` or a `TypeVar` instead.
  - `except*` / `ExceptionGroup` — out of scope until 3.10 is dropped.
  - `tomllib` from stdlib — 3.11+; use `tomli` or avoid.
  - `Self` from `typing` — 3.11+; use `typing_extensions.Self` or a `TypeVar(..., bound=…)`.
  Any such usage in branch code is `[FIX]`.

- **Prefer `Optional[X]` over `X | None`.** The codebase consistently uses `Optional[X]` (imported from `typing`). Both forms are valid in 3.10, but the established style is `Optional`. A new file using `X | None` instead is `[REFACTOR]` (style consistency).

- **Private modules use a leading underscore: `_client.py`, `_serialization.py`, `_exceptions.py`, `_condition_builder.py`, `_specified_properties.py`, `_wire_formats.py`.** Public symbols are re-exported through `filament/__init__.py`. A new hand-coded module under `src/filament/` without the leading underscore is `[Q]` — is it meant to be a top-level public module (rare), or did the author forget the convention?

- **Logging via `logger = logging.getLogger(__name__)` at module scope; no `print`.** The hand-coded modules all do this (see `_client.py:38`). A new module that uses `print` or `logging.getLogger("filament")` (hard-coded name) is `[REFACTOR]`.
  - Use `logger.exception` inside `except` blocks (see `error-handling`).

- **No PEP 695 `type` aliases, no `match`-statement abuse.** `match`-statement (PEP 634) is available from 3.10 and acceptable, but the codebase doesn't use it; introducing one in a new branch is `[Q]` (style choice that should be deliberate).

### Common false positives

- Style inside `src/filament/types/generated/**` — owned by `generated-code`, not here.
- A `# type: ignore[code]` with a specific error code — that's the strict-mypy form.
- A new test file using lower-prefix names (`test_foo`) — that's the pytest convention, not the leading-underscore module rule.

---

## tests

**References**: `tests/test_client.py`, `tests/test_serialization.py`, `tests/test_condition_builder.py`, `tests/test_conditions.py`, `tests/test_options.py` — canonical patterns for the corresponding source modules. `pyproject.toml` `[tool.poetry.group.test.dependencies]` (pytest + pytest-asyncio + pytest-cov). `.github/workflows/run-unit-tests.yml` (the CI matrix across Python 3.10–3.13).

**Scope**: Test coverage and pytest discipline for branch changes.

### What this focus is really about

The tests use `pytest` + `pytest-asyncio`. `FilamentClient` tests **mock `pysignalr.SignalRClient` via `patch("filament._client.SignalRClient")`** to avoid live-server dependence (see the `_make_client(...)` helper in `tests/test_client.py`). Serialization, condition-builder, and option tests are pure-Python round-trip tests with no mocking. The CI runs every test against four Python versions in parallel — anything that passes 3.10 may still break 3.13 (e.g. removed deprecations) or vice versa.

### Rules

- **A new public method on `FilamentClient` ships with at least one test** under `tests/test_client.py`. Establish the matching `Test<Behaviour>` class shape (see `TestClientConstruction`, etc.). A new public method without a test is `[FIX]` — this is what the existing tests show as the bar.

- **Tests that touch SignalR mock the client.** `with patch("filament._client.SignalRClient") as mock_cls:` is the established pattern (see `_make_client` and `TestClientConstruction.test_default_url`). A new test that touches `FilamentClient.__init__` or its hub methods without mocking the SignalR client is `[FIX]` — it would try to open a real websocket.

- **`async def` tests are marked `@pytest.mark.asyncio` (or rely on `asyncio_mode = "auto"`).** Check `pyproject.toml` `[tool.pytest.ini_options]` for the mode; the existing `[tool.pytest.ini_options]` block does not set `asyncio_mode`, so async tests need the explicit marker. A new `async def test_…` without the marker is `[FIX]` (pytest silently skips it).

- **Wire-format / serialization changes ship with a round-trip test.** `tests/test_serialization.py` covers `to_dict(from_dict(d)) == d` shapes; a branch that changes the wire format without adding a round-trip case is `[Q]` (silent breakage of the C# contract is exactly the failure mode the existing tests guard).

- **Condition-builder changes ship with both a runtime test and a static type test.** The runtime tests in `tests/test_condition_builder.py` exercise the builder; the mypy plugin's narrowing is covered separately (a comment / `reveal_type` block, or by absence — when types break, the mypy CI workflow fails). A new builder method without a runtime test is `[FIX]`; if the plugin needs to narrow it specially, the absence of a static-narrowing fixture is `[Q]` (the auditor can't tell whether the existing `mypy-check.yml` workflow catches it).

- **Don't import `pytest_asyncio` directly** if avoidable — the existing tests rely on `pytest.mark.asyncio`. A new import is `[REFACTOR]`.

- **Coverage gates aren't enforced in CI, but the existing pattern is one or more focused tests per public-API method.** A branch that adds public-API surface and bumps test count by zero is `[FIX]`.

### Common false positives

- A refactor that doesn't change behaviour and is covered by existing tests — no new test needed.
- Tests live exclusively in `tests/`; modules under `src/filament/` shouldn't be tested by editing them in place.

---

## changelog

**References**: `.claude/CLAUDE.md` "Changelog" section, `CHANGELOG_DEV.md` / `CHANGELOG.md` (the generated changelog files — **never** hand-edited), `.github/workflows/validate-pull-request.yml` and `.github/workflows/increment-version-develop.yml` / `increment-version-reusable.yml` (the validation + auto-version steps).

### What this focus is really about

Changelog content is carried **in commit messages**, not written into the changelog files. The release tooling harvests `CHANGELOG:` lines from the branch's commits and writes them into `CHANGELOG_DEV.md` / `CHANGELOG.md` at version-bump time.

- **`CHANGELOG_DEV.md` and `CHANGELOG.md` are generated/maintained by the release tooling.** Hand-editing either one causes version drift and merge conflicts. This is stated in `.claude/CLAUDE.md` "Changelog" — treat it with the same weight as the "Generated Code" DO NOT EDIT rule.
- The unit of changelog intent is a commit with a `CHANGELOG: <summary>` line in its message. The branch's user-visible change is described there, and the tooling does the rest on merge.

So the auditor's job here is two-fold: confirm the branch carries its changelog intent in a commit (not in the changelog files), and flag any direct edit to the changelog files.

### Rules

- **Each branch should have at least one commit with a `CHANGELOG:` line in it.** This is where a user-visible change is recorded for the release tooling to harvest. "User-visible" means any change a downstream consumer could notice: a new / changed / removed `__all__` export, a new ctor parameter, a behaviour change in serialization, a new event handler, a regenerated type. A branch with such a change but no `CHANGELOG:` commit line is `[FIX]`.
  - Grep hint: `git log <merge-base>..HEAD --format=%B | grep -i '^CHANGELOG:'` — zero matches on a branch with user-visible changes is the tell.

- **Changelogs should never be written to `CHANGELOG_DEV.md` or `CHANGELOG.md` as part of the branch.** Any branch diff that touches either file is `[FIX]` — these are owned by the release tooling, and a manual edit causes version drift and merge conflicts (see `.claude/CLAUDE.md` "Changelog").
  - Grep hint: any add/modify of `CHANGELOG_DEV.md` or `CHANGELOG.md` in the branch diff.

- **Pure-internal refactors (no observable user effect) don't need a `CHANGELOG:` line.** A branch that refactors a private helper, renames an internal variable, tightens a type hint on a private method, etc. doesn't require changelog intent — and one that adds a noisy `CHANGELOG:` line for such a change is `[Q]` (clutters the harvested release notes).

- **Generated-code regenerations** carry their changelog intent in the `CHANGELOG:` commit line like any other user-visible change (new types → feature-flavoured wording; corrected serialization mismatch → fix-flavoured). A branch that regenerates `types/generated/**` with no `CHANGELOG:` commit line is `[Q]`.

### Common false positives

- A branch that *only* edits `.claude/` (skills, agents, CLAUDE.md) or `.github/` workflows — no end-user impact, no `CHANGELOG:` line required.
- A test-only branch where the production code is unchanged.

---

## reinvention

**References**: open-ended. Survey `src/filament/_*.py`, `src/filament/types/conditions.py`, `src/filament/types/options.py`, `src/filament/_wire_formats.py`, and the `__init__.py` re-exports before reading the branch.

### What this focus is really about

A high-level intelligent sweep for **unknowns** — things the new code invents inline that already have a home in the codebase. Other focuses catch *known* reinvention (a `uuid.UUID` instead of `uuid_utils.UUID`, a hand-rolled `strftime` instead of `instant_to_wire`). This focus is the open-ended counterpart: scan the new code with curiosity and flag anything — named or unnamed — that duplicates an existing concept, utility, helper, type, or pattern.

The audit's core principle: codebase-wide consistency, deviation only by design. Unknown reinventions are where the principle quietly breaks. They're caught by curiosity, not by checklist.

### Workflow

1. **Survey first, read second.** Before reading the branch, build a map of what already exists:
   - Glob `src/filament/_*.py` — note the hand-coded modules and their public helpers.
   - Glob `src/filament/types/*.py` — note the hand-coded `conditions.py` / `options.py` building blocks (`DbConditionInstance` hierarchy, `DataOption` constructors).
   - Read `src/filament/_serialization.py` and `_wire_formats.py` for the conversion + wire-format primitives.
   - Read `src/filament/__init__.py` for the public re-exports.
2. **Read every new/modified hand-coded `*.py` in full.** (Skip `src/filament/types/generated/**` — that's owned by `generated-code`.) Inventory **discrete** capabilities: things that, if extracted, would be named (a function, a class, a helper, a type).
3. **For each item, search broadly** — by behavioural keyword, synonym (`serialize`, `to_dict`, `wire`, `format`, `condition`, `narrow`, `dispatch`, `subscription`, `callback`), and sibling that solved a similar problem.
4. **Decide and report**:
   - Match found, high confidence → `[REFACTOR]`. Cite both sites with `path(line)`.
   - Semantics overlap but not 1:1 → `[Q]`. Describe the overlap.
   - Searched, found nothing → log in the transparency section so the reviewer sees what was considered.
5. **Be thorough but not noisy.** Don't flag trivial inline derivations. Flag things that are 3+ lines, *named*, *re-used*, or *would naturally be shared*.

### Examples to spark the search (NOT a closed list)

If the findings map 1:1 onto these bullets, you anchored too narrowly — search broader.

- A new wire-format `strftime` / parsing helper where `instant_to_wire` / `instant_from_wire` / `timedelta_to_wire` / `timedelta_from_wire` in `_wire_formats.py` fits.
- A new case-conversion helper where `camel_case_from_snake_case` / `snake_case_from_camel_case` exists.
- A new `uuid.UUID` import where `uuid_utils.UUID` is the codebase choice.
- A hand-rolled dataclass ↔ dict converter where `to_dict` / `from_dict` fit.
- A new condition wrapper where one of `DbConditionProperty` / `…Comparable…` / `…List…` / `…Dict…` covers the case.
- A new `DataOption`-style key-value wrapper where the existing `option_to_dict` / `option_from_dict` cover the shape.
- A custom exception hierarchy where `FilamentError` already covers the boundary.
- A custom future-tracking dictionary or task lifecycle where `_pending_invocations` + `_handle_close` already define the pattern.
- A new sync-vs-async dispatch where `_invoke_callback` exists.
- A new module-level logger named differently than `logging.getLogger(__name__)`.

### Common false positives

- Trivial inline derivations (`list(...)`, `dict.get(...)`, `if x is None: return None`).
- Service- or feature-specific helpers that genuinely have no equivalent.
- An equivalent that exists but is intentionally shaped differently (e.g. a partial-application helper that doesn't fit `to_dict`'s generic shape).

### Output

Two parts, both required: the `[REFACTOR]`/`[Q]` findings, **and** a short transparency list of the discrete capabilities you searched for and found no prior art for (so the reviewer can sanity-check coverage).
