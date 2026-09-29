"""Tests for the Filament mypy plugin (``filament_mypy_plugin``).

These tests type-check small code snippets with mypy in-process (via
``mypy.api``), using the project's real ``pyproject.toml`` config so the plugin
is loaded exactly as it is in CI and locally. Each snippet calls
``reveal_type(...)`` on a ``Condition(Entity).prop("field")`` expression; we
assert on the narrowed type mypy reports, and on the diagnostics emitted for
unknown fields.

The plugin lives in ``src/filament_mypy_plugin.py`` and narrows
``Condition.prop()`` to the appropriate ``DbCondition*Property`` subtype based
on the entity's field type. A key behaviour is that fields *inherited* from
base entities (e.g. ``EntityBase.id``, ``ReferenceEntityBase.reference_id``)
must resolve, not just fields declared directly on the queried class.
"""
from __future__ import annotations

import contextlib
import os
import re
from pathlib import Path
from typing import Iterator

import pytest

mypy_api = pytest.importorskip(
    "mypy.api",
    reason="mypy is required to exercise the plugin (install the 'types' group)",
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CONFIG = _REPO_ROOT / "pyproject.toml"

_CB = "filament._condition_builder"


@contextlib.contextmanager
def _in_repo_root() -> Iterator[None]:
    """Run with the repo root as CWD so the plugin path resolves consistently."""
    previous = os.getcwd()
    os.chdir(_REPO_ROOT)
    try:
        yield
    finally:
        os.chdir(previous)


def _run_mypy(source: str, tmp_path: Path) -> str:
    """Type-check *source* with the project config and return mypy's stdout."""
    snippet = tmp_path / "snippet.py"
    snippet.write_text(source)
    with _in_repo_root():
        stdout, stderr, _status = mypy_api.run(
            [
                "--config-file",
                str(_CONFIG),
                "--no-incremental",
                "--no-error-summary",
                str(snippet),
            ]
        )
    # A non-empty stderr means mypy itself failed (bad config / crashed plugin),
    # which should fail the test loudly rather than masquerade as "no findings".
    assert not stderr, f"mypy reported an internal error:\n{stderr}"
    return stdout


def _revealed_types(output: str) -> list[str]:
    return re.findall(r'Revealed type is "(.*)"', output)


def _errors(output: str) -> list[str]:
    return [line for line in output.splitlines() if ": error:" in line]


# ---------------------------------------------------------------------------
# Narrowing of prop() return types
# ---------------------------------------------------------------------------

# Each case: (field_name, expected_revealed_type). All fields below live on
# ``AvailabilityEntity`` either directly or via its base classes:
#   AvailabilityEntity -> ReferenceEntityBase -> Entity -> EntityBase
_AVAILABILITY_CASES = [
    # Declared directly on AvailabilityEntity.
    pytest.param("name", f"{_CB}.DbConditionComparableProperty[str]", id="own-str"),
    pytest.param("priority_weight", f"{_CB}.DbConditionComparableProperty[int]", id="own-int"),
    # Non-comparable scalar (an enum) -> plain property, not comparable.
    pytest.param(
        "availability_mode",
        f"{_CB}.DbConditionProperty[filament.types.generated.enums.AvailabilityModeEnum]",
        id="own-enum-plain",
    ),
    # Inherited from the immediate base, ReferenceEntityBase.
    pytest.param(
        "reference_id",
        f"{_CB}.DbConditionComparableProperty[uuid_utils.UUID]",
        id="inherited-parent-uuid",
    ),
    pytest.param(
        "reference_type",
        f"{_CB}.DbConditionComparableProperty[str]",
        id="inherited-parent-str",
    ),
    # Optional[Union[...]] override of ReferenceEntityBase.reference -> None
    # stripped, union preserved, not a known scalar.
    pytest.param(
        "reference",
        f"{_CB}.DbConditionProperty["
        "filament.types.generated.entities.organization_service.OfficeLocationEntity"
        " | filament.types.generated.entities.user_service.UserEntity]",
        id="own-optional-entity-union",
    ),
    # Inherited from a grandparent base, EntityBase (two levels up).
    pytest.param(
        "id",
        f"{_CB}.DbConditionComparableProperty[uuid_utils.UUID]",
        id="inherited-grandparent-uuid",
    ),
    pytest.param(
        "created_at",
        f"{_CB}.DbConditionComparableProperty[datetime.datetime]",
        id="inherited-grandparent-datetime",
    ),
    # Inherited Optional[UUID] -> Optional unwrapped to a comparable scalar.
    pytest.param(
        "created_by_id",
        f"{_CB}.DbConditionComparableProperty[uuid_utils.UUID]",
        id="inherited-optional-unwrapped",
    ),
]


@pytest.mark.parametrize("field_name, expected", _AVAILABILITY_CASES)
def test_prop_narrows_field_type(
    field_name: str, expected: str, tmp_path: Path
) -> None:
    source = (
        "from filament._condition_builder import Condition\n"
        "from filament.types.generated.entities.availability_service import "
        "AvailabilityEntity\n"
        f'reveal_type(Condition(AvailabilityEntity).prop("{field_name}"))\n'
    )
    output = _run_mypy(source, tmp_path)
    assert _errors(output) == [], output
    assert _revealed_types(output) == [expected], output


def test_prop_narrows_multi_arm_entity_union(tmp_path: Path) -> None:
    """A 3+-arm ``Optional[Union[...]]`` keeps every arm, not just the first.

    ``AvailabilityEntity.reference`` covers the 2-arm case; this exercises
    ``_unwrap_optional``'s ``make_union`` path with more arms than that.
    """
    source = (
        "from filament._condition_builder import Condition\n"
        "from filament.types.generated.entities.schedule_service import "
        "ScheduleSegmentEntity\n"
        'reveal_type(Condition(ScheduleSegmentEntity).prop("reference"))\n'
    )
    output = _run_mypy(source, tmp_path)
    assert _errors(output) == [], output
    assert _revealed_types(output) == [
        f"{_CB}.DbConditionProperty["
        "filament.types.generated.entities.event_service.EventEntity"
        " | filament.types.generated.entities.scope_service.ScopeEntity"
        " | filament.types.generated.entities.task_service.TaskEntity"
        " | filament.types.generated.entities.task_service.TaskGroupEntity]"
    ], output


def test_prop_narrows_list_field(tmp_path: Path) -> None:
    # address_parts: list[str] (inherited from AddressEntityBase -> Entity).
    source = (
        "from filament._condition_builder import Condition\n"
        "from filament.types.generated.entities.generic import AddressEntityBase\n"
        'reveal_type(Condition(AddressEntityBase).prop("address_parts"))\n'
    )
    output = _run_mypy(source, tmp_path)
    assert _errors(output) == [], output
    assert _revealed_types(output) == [f"{_CB}.DbConditionListProperty[str]"], output


def test_prop_narrows_dict_field(tmp_path: Path) -> None:
    # costs_by_currency_rate_id: dict[UUID, float] on ActualEntity.
    source = (
        "from filament._condition_builder import Condition\n"
        "from filament.types.generated.entities.actual_service import ActualEntity\n"
        'reveal_type(Condition(ActualEntity).prop("costs_by_currency_rate_id"))\n'
    )
    output = _run_mypy(source, tmp_path)
    assert _errors(output) == [], output
    assert _revealed_types(output) == [
        f"{_CB}.DbConditionDictProperty[uuid_utils.UUID, float]"
    ], output


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------

def test_unknown_field_is_reported(tmp_path: Path) -> None:
    source = (
        "from filament._condition_builder import Condition\n"
        "from filament.types.generated.entities.availability_service import "
        "AvailabilityEntity\n"
        'Condition(AvailabilityEntity).prop("does_not_exist")\n'
    )
    output = _run_mypy(source, tmp_path)
    errors = _errors(output)
    assert len(errors) == 1, output
    assert 'Entity "AvailabilityEntity" has no field "does_not_exist"' in errors[0]


def test_inherited_fields_do_not_error(tmp_path: Path) -> None:
    """Regression guard: inherited fields must resolve, not be flagged unknown.

    Before the MRO-aware lookup fix, every field inherited from a base entity
    was rejected with 'has no field', because ``TypeInfo.names`` only holds
    members declared directly on the class.
    """
    inherited = ["id", "created_at", "scope_id", "reference_id", "reference_type"]
    lines = [
        "from filament._condition_builder import Condition",
        "from filament.types.generated.entities.availability_service import "
        "AvailabilityEntity",
        "c = Condition(AvailabilityEntity)",
    ]
    lines += [f'c.prop("{name}")' for name in inherited]
    output = _run_mypy("\n".join(lines) + "\n", tmp_path)
    assert _errors(output) == [], output


# ---------------------------------------------------------------------------
# Dynamic user-defined properties
# ---------------------------------------------------------------------------

def test_udp_attribute_is_any_but_other_unknown_attribute_errors(
    tmp_path: Path,
) -> None:
    source = (
        "from filament.types.generated.entities.task_service import TaskEntity\n"
        "task = TaskEntity()\n"
        "task.udp_budget = object()\n"
        "reveal_type(task.udp_budget)\n"
        "task.not_a_real_property\n"
    )
    output = _run_mypy(source, tmp_path)
    assert _revealed_types(output) == ["Any"], output
    errors = _errors(output)
    assert len(errors) == 1, output
    assert 'has no attribute "not_a_real_property"' in errors[0]


@pytest.mark.parametrize(
    "fullname, claimed",
    [
        pytest.param(
            "filament._specified_properties.SpecifiedPropertiesBase.udp_budget",
            True,
            id="specified-properties-base",
        ),
        pytest.param(
            "filament.types.generated.entities.task_service.TaskEntity.name",
            False,
            id="declared-entity-field",
        ),
        pytest.param("builtins.str.upper", False, id="non-filament-class"),
    ],
)
def test_attribute_hook_is_claimed_only_for_specified_properties_base(
    fullname: str, claimed: bool,
) -> None:
    # A claimed hook blocks any other plugin from typing the same attribute.
    from mypy.options import Options

    from filament_mypy_plugin import FilamentConditionPlugin

    plugin = FilamentConditionPlugin(Options())
    assert (plugin.get_attribute_hook(fullname) is not None) is claimed


def test_udp_constructor_literal_keys_are_validated(tmp_path: Path) -> None:
    valid = (
        "from filament.types.generated.entities.task_service import TaskEntity\n"
        "TaskEntity(_user_defined_properties={'udp_budget': 12})\n"
    )
    assert _errors(_run_mypy(valid, tmp_path)) == []

    invalid = (
        "from filament.types.generated.entities.task_service import TaskEntity\n"
        "TaskEntity(_user_defined_properties={'budget': 12})\n"
    )
    errors = _errors(_run_mypy(invalid, tmp_path))
    assert len(errors) == 1
    assert "keys must start with 'udp_'" in errors[0]


def test_constructor_input_is_not_a_readable_mapping(tmp_path: Path) -> None:
    source = (
        "from filament.types.generated.entities.task_service import TaskEntity\n"
        "task = TaskEntity(_user_defined_properties={'udp_budget': 12})\n"
        "reveal_type(task._user_defined_properties)\n"
    )
    output = _run_mypy(source, tmp_path)
    assert _errors(output), output
