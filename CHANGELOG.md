## 0.5.0 (2026-07-22 13:12)

### Types

#### FEATURE
- Subscriptions can narrow pushed entity properties; recurring-review gating, scope-step list, and task warnings type support

## 0.4.0 (2026-06-25 13:25)

### Serialization

#### FIX
- Property names with leading or repeated underscores now convert to well-formed camelCase

#### FEATURE
- Property name lists in read/modify requests now accept snake_case and are de-duplicated

## 0.3.0 (2026-06-22 09:13)

### Client

#### FEATURE
- read_data now transparently pages around the server's response-size limit, raising ResponseTooLargeError only when a single entity cannot fit

### Mypy Plugin

#### FIX
- Condition.prop() now type-narrows fields inherited from base entities instead of flagging them as unknown

### Serialization

#### FIX
- from_dict no longer overrides a dataclass field's own default or default_factory when the wire payload omits an optional field

### Types

#### FEATURE
- Generated types now support recurring events (event repeat schedules, shared RepeatMode/RepeatDays enums, per-occurrence schedule keys and alignment settings)

### Other

#### OTHER
- Internal package now uses relative imports throughout, so it can be vendored or renamed without rewriting import paths

## 0.2.0 (2026-06-15 13:15)

### Client

#### FIX
- Surface SignalR disconnects mid-call as FilamentConnectionError instead of hanging indefinitely

#### FEATURE
- Reassemble multi-part DataUpdated events into one callback per logical update
- Add FilamentClient(max_size=...) to opt in to larger SignalR responses

### Scripts

#### OTHER
- Switch codegen runner to the backend's --api hub generator flag

### Other

#### OTHER
- Plugin manifest version is now stamped only on release builds, so develop to main merges no longer conflict on .claude-plugin/plugin.json
- PRs now get an auto-updating comment previewing the changelog and version on merge

## 0.1.0 (2026-06-10 20:00)

### Build

#### OTHER
- Renamed agent Docker sandbox directory from .claude-docker to .agent-docker

### Client

#### OTHER
- Migrated SignalR transport from signalr-async to pysignalr for better compatibility and connection error handling

#### FIX
- Default connection port changed from 7076 to 443

#### FEATURE
- Subscriptions now support multiple callbacks for the same entity type and permissions
- FilamentClient now requires an organization_id parameter, sent as X-Organization-Id header
- Add async FilamentClient for SignalR communication with the Filament backend

### Codegen

#### OTHER
- Generated types now include docstrings from backend definitions

#### FEATURE
- Add code generation scripts to invoke backend type generator

### Conditions

#### FEATURE
- Conditions can be combined using &, |, ~ operators with standard NOT > AND > OR precedence
- Add typed condition system matching C# backend for database query filtering

### Examples

#### FEATURE
- Added examples demonstrating typed conditions with &, |, ~ operators for reads and subscriptions

### Generated

#### FEATURE
- Regenerate Python types: rename MeetingService to EventService and refresh entity/enum definitions

### Release

#### OTHER
- Fix GitHub releases not triggering after version increment

#### FIX
- Fix release build failing on dev versions by converting to PEP 440 format
- Fix release workflow failing due to double v prefix in git tag reference

### Serialization

#### FIX
- Deserialized Instant values now carry UTC tzinfo instead of being naive
- Fixed Duration, Instant, and Decimal serialization to match the C# backend wire format

#### FEATURE
- Entities automatically track which properties were explicitly set, allowing to_dict to omit server-managed audit fields on create operations

### Other

#### OTHER
- Added scheduler diagnostic fields (unscheduled events and required-user exclusions) and new backend enum values
- Add guides for connections, reading/modifying data, subscriptions, data options, and finding types
- Renamed Python module from filament_api to filament

#### FEATURE
- Initial Python API project structure with Poetry, pytest, and mypy
