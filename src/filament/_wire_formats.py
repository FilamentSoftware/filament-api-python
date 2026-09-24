"""Wire-format helpers for the Filament SignalR contract.

Kept dependency-free so both ``_serialization.py`` (entity dataclass conversion)
and ``types/options.py`` (DataOption conversion) can use them without
creating an import cycle through ``filament.types``.

Wire formats match the C# backend converters in
``Filament.FSCore.Json.Converters.NodaTime``:

- Instant  : ``uuuu-MM-ddTHH:mm:ss.ffffffffZ`` (8 fractional digits + Z)
- Duration : ``[-]D:HH:MM:SS[.FFFFFFFFF]`` (NodaTime ``Duration.ToString()``)
"""
from __future__ import annotations

import datetime

# ``%f`` produces 6 microsecond digits; the trailing literal ``00`` pads to 8.
DATETIME_FORMAT = "%Y-%m-%dT%H:%M:%S.%f00Z"


def instant_to_wire(value: datetime.datetime) -> str:
    """Format a datetime as a C# ``Instant`` wire string.

    Timezone-aware inputs are converted to UTC first so a non-UTC offset
    does not silently shift the emitted instant.
    """
    if value.tzinfo is not None:
        value = value.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return value.strftime(DATETIME_FORMAT)


def instant_from_wire(value: str) -> datetime.datetime:
    """Parse a C# ``Instant`` wire string into a UTC-aware datetime.

    The canonical wire format is ``uuuu-MM-ddTHH:mm:ss.ffffffffZ`` (see
    :data:`DATETIME_FORMAT`), but the backend emits only as many significant
    fractional digits as the instant needs — so the fraction can be anything
    from absent to 9 digits (e.g. ``...:57.23421050Z``, which the fixed-width
    ``%f00`` format cannot parse).  The fractional component is therefore parsed
    at whatever length it arrives and truncated to microseconds, the most a
    Python ``datetime`` can represent.

    The trailing ``Z`` denotes UTC; the returned datetime carries
    ``tzinfo=datetime.timezone.utc`` so downstream code never has to guess.
    """
    if not value.endswith("Z"):
        raise ValueError(
            f"Invalid Instant wire format (expected trailing 'Z'): {value!r}"
        )
    body = value[:-1]
    if "." in body:
        main, frac = body.split(".", 1)
    else:
        main, frac = body, ""
    parsed = datetime.datetime.strptime(main, "%Y-%m-%dT%H:%M:%S")
    # Right-pad then truncate to 6 digits — datetime has microsecond precision,
    # so any 7th–9th fractional digits are dropped.
    microseconds = int((frac + "000000")[:6]) if frac else 0
    return parsed.replace(microsecond=microseconds, tzinfo=datetime.timezone.utc)


def timedelta_to_wire(value: datetime.timedelta) -> str:
    """Format a timedelta as a NodaTime ``Duration`` wire string.

    Format: ``[-]D:HH:MM:SS[.FFFFFFFFF]`` (sign + days with no padding +
    zero-padded hours/minutes/seconds + optional 9-digit fractional). Python
    ``timedelta`` has microsecond precision, so the fractional component is
    right-padded with three zero nanoseconds when non-zero.
    """
    total_us = value // datetime.timedelta(microseconds=1)
    negative = total_us < 0
    if negative:
        total_us = -total_us
    days, rem = divmod(total_us, 86_400_000_000)
    hours, rem = divmod(rem, 3_600_000_000)
    minutes, rem = divmod(rem, 60_000_000)
    seconds, microseconds = divmod(rem, 1_000_000)
    sign = "-" if negative else ""
    if microseconds == 0:
        return f"{sign}{days}:{hours:02d}:{minutes:02d}:{seconds:02d}"
    nanoseconds = microseconds * 1000
    return f"{sign}{days}:{hours:02d}:{minutes:02d}:{seconds:02d}.{nanoseconds:09d}"


def timedelta_from_wire(value: str) -> datetime.timedelta:
    """Parse a NodaTime ``Duration`` wire string into a timedelta.

    Accepts ``[-]D:HH:MM:SS[.FFFFFFFFF]``. Fractional digits beyond
    microsecond precision are truncated, since Python ``timedelta`` cannot
    represent nanoseconds.
    """
    if value.startswith("-"):
        negative = True
        value = value[1:]
    else:
        negative = False
    if "." in value:
        main, frac = value.split(".", 1)
    else:
        main, frac = value, ""
    parts = main.split(":")
    if len(parts) != 4:
        raise ValueError(f"Invalid Duration wire format: {value!r}")
    days, hours, minutes, seconds = (int(p) for p in parts)
    microseconds = int((frac + "000000")[:6]) if frac else 0
    result = datetime.timedelta(
        days=days,
        hours=hours,
        minutes=minutes,
        seconds=seconds,
        microseconds=microseconds,
    )
    return -result if negative else result
