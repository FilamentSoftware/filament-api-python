## 0.8.1-dev.0.0.4 (2026-11-19 11:59)

### Types

#### OTHER
- Adds user settings and scope item versioning types, and the Timeout error code

## 0.8.1-dev.0.0.3 (2026-11-05 17:45)

### Types

#### OTHER
- MediaEntity gains frame_rate, and UploadGrant/FileReference gain issued_at so clients can measure grant lifetime against their own clock

## 0.8.1-dev.0.0.2 (2026-10-23 17:58)

### Types

#### OTHER
- Dependent-task routing is now per category (add_to_dependent_category_ids, dependent_task_source_item_ids) and ProcessingSettingsEntity gains anonymise_snapshots

## 0.8.1-dev.0.0.1 (2026-10-20 14:36)

### Types

#### OTHER
- Adds a review flag to MediaEntity marking whether media is included in its task's review

## 0.8.0-dev.0.0.2 (2026-10-15 22:10)

### Types

#### OTHER
- Adds an optional description field to MediaEntity and MediaElementEntity

## 0.8.0-dev.0.0.1 (2026-10-14 11:30)

### Serialization

#### FEATURE
- Fields carrying compression metadata (e.g. ScheduleSegmentEntity.reasons_data) are decoded from br+base64:/gzip+base64: to plain text; older clients will see encoded values once the backend starts compressing

### Types

#### OTHER
- Adds CompressionMethod / PropertyEncoding enums and compression metadata on ScheduleSegmentEntity.reasons_data

## 0.7.4-dev.0.0.3 (2026-10-14 08:25)

### Serialization

#### FIX
- Snake_case names in include_properties / exclude_properties are converted to wire camelCase

### Types

#### OTHER
- Adds media service and permission delta types; PermissionEntity.properties is replaced by all_properties / include_properties / exclude_properties

## 0.7.4-dev.0.0.2 (2026-09-29 14:42)

### Types

#### OTHER
- Split entity `priority` fields into `priority_weight` (higher wins) and `priority_order` (lower wins), and added milestone priority ordering and a priority-milestone weight multiplier

## 0.7.4-dev.0.0.1 (2026-09-29 10:22)

### Client

#### FIX
- Refreshing the private UDP definition-change subscription no longer cancels a user subscription the backend deduplicated onto the same ID

## 0.7.3-dev.0.0.1 (2026-09-26 11:32)

### Client

#### FIX
- get_entity_property_permissions returns the fetched permissions and keeps the UDP cache when the definition-change subscription is rejected, matching read paths
- Reads no longer re-send a rejected UDP definition-change subscription on every call; retries back off from 5s up to 5 minutes
- UDP definitions are still returned with a warning when subscribing to their changes fails, and the subscribe is retried on the next call

### Mypy Plugin

#### FIX
- Other mypy plugins can now type attributes that aren't on Filament entities

## 0.7.2-dev.0.0.1 (2026-09-24 16:15)

### Client

#### FIX
- On Python 3.10, cancelling a call (e.g. via asyncio.wait_for) while the client is closing now raises CancelledError instead of FilamentConnectionError
- A connection drop during UDP loading raises FilamentConnectionError instead of CancelledError, and UDP loading inside a DataUpdated no longer stalls the connection

### Serialization

#### FIX
- A malformed <name>_data projection that raises AttributeError is logged and skipped instead of failing the whole entity

### Udp

#### FIX
- UDP names with a digit followed by a letter now match the backend's wire key (e.g. udpFoo2Bar) and read back correctly; their Python attribute is now udp_foo2_bar
- Definitions whose names map to the same Python attribute now fail the UDP load instead of one silently overwriting the other

## 0.7.1-dev.0.0.1 (2026-09-24 10:36)

### Client

#### FIX
- Dropped connections now raise FilamentConnectionError with the websocket close code instead of hanging or leaking raw pysignalr errors

## 0.7.0-dev.0.1.3 (2026-09-24 08:17)

### Types

#### OTHER
- Task entities and connections now expose the module that auto-generated them

## 0.7.0-dev.0.1.2 (2026-09-16 12:17)

### Types

#### OTHER
- New entity fields from the backend — CurrentInstantSettingsEntity.enable_update to disable automatic instant advancement, and UserLicenseEntity.user_count for hourly license usage without the id list

## 0.7.0-dev.0.1.1 (2026-09-10 15:57)

### Docs

#### FIX
- The generated type reference no longer cuts entity and enum descriptions off mid-sentence

### Mypy Plugin

#### FIX
- Condition.prop() on a reference property now types as every entity it can point at, rather than only the first

### Serialization

#### FIX
- Deserializing a polymorphic reference whose $type names an unexpected type now fails cleanly instead of silently returning a different entity
- Polymorphic entity references now deserialize as the type the payload names, instead of always the first candidate type

### Types

#### OTHER
- Task review state is now tracked per stage, review routing moves from tags to review groups, and entity reference properties expose their concrete target types

## 0.7.0-dev.0.1.0 (2026-09-03 18:07)

### Client

#### FEATURE
- Adds file upload and download, and auto-populates file read projections (e.g. UserEntity.image) into FileReference objects

### Serialization

#### FIX
- Fixes parsing of backend timestamps that carry more than six fractional-second digits

### Types

#### OTHER
- FileMetadata and FileReference now live in filament.types.generated.shared rather than .misc

## 0.7.0-dev.0.0.1 (2026-08-26 14:30)

### Client

#### FEATURE
- Tenant-defined fields are now readable and writable as typed udp_* attributes on entities and connections, across reads, connected reads, writes, and subscription updates
- get_user_defined_properties() reports each field's declared type, effective permission, and a condition factory for filtering on it
- Field definitions refresh automatically, so definition changes are picked up without reconnecting

## 0.6.1-dev.0.0.2 (2026-08-14 00:00)

### Client

#### FEATURE
- Add user-defined property discovery, typed entity attributes, construction, conditions, cache maintenance, connected reads, and definition administration

## 0.6.1-dev.0.0.1 (2026-08-12 16:06)

### Codex

#### OTHER
- Added Codex workflow skill wrappers for repository development.

## 0.6.0-dev.0.2.0 (2026-08-04 15:44)

### Types

#### FEATURE
- Add task deliverables and distinguish null vs empty milestone step overrides

## 0.6.0-dev.0.1.0 (2026-07-30 16:45)

### Types

#### FEATURE
- Exposes structured schedule-segment reasons, principal-type fields on entities, and renames NoteUserConnection.type to recipient_type

## 0.6.0-dev.0.0.1 (2026-07-23 15:46)

### Types

#### FEATURE
- ProcessingRunEntity and ServiceProcessingInfo now expose progress fraction and start/end instants

## 0.5.0-dev.0.0.1 (2026-07-22 13:06)

### Types

#### FEATURE
- Subscriptions can narrow pushed entity properties; recurring-review gating, scope-step list, and task warnings type support

## 0.4.0-dev.0.0.1 (2026-06-25 13:21)

### Serialization

#### FIX
- Property names with leading or repeated underscores now convert to well-formed camelCase

#### FEATURE
- Property name lists in read/modify requests now accept snake_case and are de-duplicated

## 0.3.0-dev.0.1.0 (2026-06-19 12:07)

### Client

#### FEATURE
- read_data now transparently pages around the server's response-size limit, raising ResponseTooLargeError only when a single entity cannot fit

## 0.3.0-dev.0.0.4 (2026-06-17 10:09)

### Mypy Plugin

#### FIX
- Condition.prop() now type-narrows fields inherited from base entities instead of flagging them as unknown

## 0.3.0-dev.0.0.3 (2026-06-16 13:30)

### Serialization

#### FIX
- from_dict no longer overrides a dataclass field's own default or default_factory when the wire payload omits an optional field

## 0.3.0-dev.0.0.2 (2026-06-16 11:03)

### Other

#### OTHER
- Internal package now uses relative imports throughout, so it can be vendored or renamed without rewriting import paths

## 0.3.0-dev.0.0.1 (2026-06-15 17:58)

### Types

#### FEATURE
- Generated types now support recurring events (event repeat schedules, shared RepeatMode/RepeatDays enums, per-occurrence schedule keys and alignment settings)

## 0.2.0-dev.0.0.4 (2026-06-15 10:48)

## 0.2.0-dev.0.0.3 (2026-06-15 10:46)

### Other

#### OTHER
- Plugin manifest version is now stamped only on release builds, so develop to main merges no longer conflict on .claude-plugin/plugin.json

## 0.2.0-dev.0.0.2 (2026-06-15 10:01)

### Other

#### OTHER
- PRs now get an auto-updating comment previewing the changelog and version on merge

## 0.2.0-dev.0.0.1 (2026-06-11 10:24)

### Client

#### FIX
- Surface SignalR disconnects mid-call as FilamentConnectionError instead of hanging indefinitely

#### FEATURE
- Reassemble multi-part DataUpdated events into one callback per logical update
- Add FilamentClient(max_size=...) to opt in to larger SignalR responses

### Scripts

#### OTHER
- Switch codegen runner to the backend's --api hub generator flag

## 0.1.0-dev.0.4.1 (2026-06-10 19:54)

### Other

#### OTHER
- Added scheduler diagnostic fields (unscheduled events and required-user exclusions) and new backend enum values

## 0.1.0-dev.0.4.0 (2026-06-09 11:58)

### Other

#### OTHER
- Add guides for connections, reading/modifying data, subscriptions, data options, and finding types

## 0.1.0-dev.0.3.2 (2026-05-26 10:16)

### Serialization

#### FIX
- Deserialized Instant values now carry UTC tzinfo instead of being naive

## 0.1.0-dev.0.3.1 (2026-05-20 16:23)

### Serialization

#### FIX
- Fixed Duration, Instant, and Decimal serialization to match the C# backend wire format

## 0.1.0-dev.0.3.0 (2026-05-19 11:09)

### Generated

#### FEATURE
- Regenerate Python types: rename MeetingService to EventService and refresh entity/enum definitions

## 0.1.0-dev.0.2.1 (2026-04-01 18:09)

### Client

#### FIX
- Default connection port changed from 7076 to 443

## 0.1.0-dev.0.2.0 (2026-04-01 09:24)

### Build

#### OTHER
- Renamed agent Docker sandbox directory from .claude-docker to .agent-docker

### Client

#### OTHER
- Migrated SignalR transport from signalr-async to pysignalr for better compatibility and connection error handling

#### FEATURE
- Subscriptions now support multiple callbacks for the same entity type and permissions
- FilamentClient now requires an organization_id parameter, sent as X-Organization-Id header
- Add async FilamentClient for SignalR communication with the Filament backend

### Codegen

#### OTHER
- Generated types now include docstrings from backend definitions

### Conditions

#### FEATURE
- Conditions can be combined using &, |, ~ operators with standard NOT > AND > OR precedence
- Add typed condition system matching C# backend for database query filtering

### Examples

#### FEATURE
- Added examples demonstrating typed conditions with &, |, ~ operators for reads and subscriptions

### Serialization

#### FEATURE
- Entities automatically track which properties were explicitly set, allowing to_dict to omit server-managed audit fields on create operations

## 0.1.0-dev.0.1.1 (2026-03-26 22:37)

### Other

#### OTHER
- Renamed Python module from filament_api to filament

## 0.1.0-dev.0.1.0 (2026-03-26 22:16)

### Codegen

#### FEATURE
- Add code generation scripts to invoke backend type generator

### Release

#### FIX
- Fix release build failing on dev versions by converting to PEP 440 format

## 0.1.0-dev.0.0.3 (2026-03-25 22:29)

### Release

#### FIX
- Fix release workflow failing due to double v prefix in git tag reference

## 0.1.0-dev.0.0.2 (2026-03-25 16:49)

### Release

#### OTHER
- Fix GitHub releases not triggering after version increment

## 0.1.0-dev.0.0.1 (2026-03-25 14:41)

### Other

#### FEATURE
- Initial Python API project structure with Poetry, pytest, and mypy
