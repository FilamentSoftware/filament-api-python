"""Mypy plugin for typed condition builder.

Narrows the return type of ``Condition(Entity).prop("field_name")`` based on
the entity's actual field type, enabling mypy to validate property names and
value types at type-check time.
"""
from __future__ import annotations

from typing import Callable, Optional, Type

from mypy.nodes import TypeInfo, Var
from mypy.plugin import MethodContext, Plugin
from mypy.types import (
    Instance,
    LiteralType,
    NoneType,
    ProperType,
    Type as MypyType,
    UnionType,
    get_proper_type,
)

_PROP_FULLNAME = "filament._condition_builder.Condition.prop"

_COMPARABLE_FULLNAMES: frozenset[str] = frozenset({
    "builtins.int",
    "builtins.float",
    "builtins.str",
    "builtins.bool",
    "uuid_utils.UUID",
    "uuid_utils._uuid.UUID",
    "datetime.datetime",
    "datetime.timedelta",
})

_PROPERTY_CLASS = "filament._condition_builder.DbConditionProperty"
_COMPARABLE_CLASS = "filament._condition_builder.DbConditionComparableProperty"
_LIST_CLASS = "filament._condition_builder.DbConditionListProperty"
_DICT_CLASS = "filament._condition_builder.DbConditionDictProperty"


# ---------------------------------------------------------------------------
# Type helpers
# ---------------------------------------------------------------------------

def _extract_str_literal(tp: ProperType) -> Optional[str]:
    """Extract a string literal value from a mypy type.

    Handles both ``LiteralType("value")`` and ``Instance(str)`` with
    ``last_known_value`` (which is how mypy represents inferred literals).
    """
    if isinstance(tp, LiteralType) and isinstance(tp.value, str):
        return tp.value
    if isinstance(tp, Instance) and tp.last_known_value is not None:
        lkv = tp.last_known_value
        if isinstance(lkv, LiteralType) and isinstance(lkv.value, str):
            return lkv.value
    return None


def _unwrap_optional(tp: ProperType) -> ProperType:
    """If *tp* is ``Optional[X]`` (i.e. ``Union[X, None]``), return ``X``."""
    if isinstance(tp, UnionType):
        non_none = [t for t in tp.items if not isinstance(get_proper_type(t), NoneType)]
        if len(non_none) == 1:
            return get_proper_type(non_none[0])
    return tp


def _is_comparable(tp: ProperType) -> bool:
    """Return ``True`` if *tp* is one of the known comparable scalar types."""
    return isinstance(tp, Instance) and tp.type.fullname in _COMPARABLE_FULLNAMES


def _get_list_element(tp: ProperType) -> Optional[MypyType]:
    """If *tp* is ``list[X]``, return ``X``.  Otherwise ``None``."""
    if isinstance(tp, Instance) and tp.type.fullname == "builtins.list" and tp.args:
        return tp.args[0]
    return None


def _get_dict_args(tp: ProperType) -> Optional[tuple[MypyType, MypyType]]:
    """If *tp* is ``dict[K, V]``, return ``(K, V)``.  Otherwise ``None``."""
    if isinstance(tp, Instance) and tp.type.fullname == "builtins.dict" and len(tp.args) >= 2:
        return (tp.args[0], tp.args[1])
    return None


# ---------------------------------------------------------------------------
# Plugin
# ---------------------------------------------------------------------------

class FilamentConditionPlugin(Plugin):
    """Mypy plugin for the Filament typed condition builder."""

    def get_method_hook(
        self, fullname: str
    ) -> Optional[Callable[[MethodContext], MypyType]]:
        """Return the hook for ``Condition.prop()`` if *fullname* matches."""
        if fullname == _PROP_FULLNAME:
            return self._prop_hook
        return None

    def _lookup_type(self, fullname: str) -> Optional[TypeInfo]:
        """Look up a TypeInfo by fully qualified name."""
        sym = self.lookup_fully_qualified(fullname)
        if sym is not None and isinstance(sym.node, TypeInfo):
            return sym.node
        return None

    def _make_generic(
        self, ctx: MethodContext, fullname: str, args: list[MypyType]
    ) -> MypyType:
        """Construct a parameterised ``Instance`` type."""
        info = self._lookup_type(fullname)
        if info is None:
            return ctx.default_return_type
        return Instance(info, args)

    def _prop_hook(self, ctx: MethodContext) -> MypyType:
        """Narrow ``Condition.prop("field")`` return type based on the entity."""
        # 1. Extract TEntity from Condition[TEntity]
        callee = get_proper_type(ctx.type)
        if not isinstance(callee, Instance) or not callee.args:
            return ctx.default_return_type
        entity_type = get_proper_type(callee.args[0])
        if not isinstance(entity_type, Instance):
            return ctx.default_return_type

        # 2. Extract string literal field name
        if not ctx.arg_types or not ctx.arg_types[0]:
            return ctx.default_return_type
        arg_type = get_proper_type(ctx.arg_types[0][0])
        field_name = _extract_str_literal(arg_type)
        if field_name is None:
            return ctx.default_return_type

        # 3. Look up field on the entity. TypeInfo.get() walks the MRO, so
        #    fields inherited from base entities are resolved too (TypeInfo.names
        #    only holds members declared directly on the class).
        sym = entity_type.type.get(field_name)
        if sym is None or not isinstance(sym.node, Var) or sym.node.type is None:
            ctx.api.fail(
                f'Entity "{entity_type.type.name}" has no field "{field_name}"',
                ctx.context,
            )
            return ctx.default_return_type
        field_type = get_proper_type(sym.node.type)

        # 4. Unwrap Optional[X] -> X
        field_type = _unwrap_optional(field_type)

        # 5. Classify and construct return type
        elem_type = _get_list_element(field_type)
        if elem_type is not None:
            return self._make_generic(ctx, _LIST_CLASS, [elem_type])

        dict_args = _get_dict_args(field_type)
        if dict_args is not None:
            return self._make_generic(ctx, _DICT_CLASS, list(dict_args))

        if _is_comparable(field_type):
            return self._make_generic(ctx, _COMPARABLE_CLASS, [field_type])

        return self._make_generic(ctx, _PROPERTY_CLASS, [field_type])


def plugin(version: str) -> Type[Plugin]:
    """Entry point for mypy plugin discovery."""
    return FilamentConditionPlugin
