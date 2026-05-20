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
