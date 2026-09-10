"""Generate a human-readable Markdown reference for the generated types.

Reads the auto-generated dataclasses under
``src/filament/types/generated/{entities,connections}`` and the enums in
``enums.py`` and emits ``docs/type-reference.md``.

The generator is **dependency-free**: it parses the source with :mod:`ast`
rather than importing the package, so it runs anywhere (including without the
runtime dependencies installed) and shows type annotations exactly as written.
Because every generated file uses ``from __future__ import annotations``, the
annotations are already plain, human-readable strings.

Run it standalone::

    python3 scripts/generate_type_reference.py

It is also invoked automatically at the end of ``scripts/generate_code.py`` so
the reference is regenerated whenever the types are.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
GENERATED = REPO_ROOT / "src" / "filament" / "types" / "generated"
OUTPUT = REPO_ROOT / "docs" / "type-reference.md"

# Root classes whose fields are documented once (in finding-types.md) and so are
# NOT repeated for every entity/connection. Resolution of inherited fields stops
# here.
STOP_CLASSES = frozenset({
    "SpecifiedPropertiesBase",
    "EntityBase",
    "Entity",
    "EntityConnectionBase",
    "EntityConnection",
    "EntityConnection_2",
    "StructBase",
    "object",
})


@dataclass
class FieldInfo:
    name: str
    type: str
    default: str  # "" means required (no default)
    doc: str = ""  # per-field description from the class docstring's :param line


@dataclass
class ClassInfo:
    name: str
    module: str          # e.g. "user_service"
    bases: list[str]     # base class names, subscripts stripped
    doc: str             # first line of the docstring (may be "")
    fields: list[FieldInfo]
    is_enum: bool
    enum_kind: str       # "Enum" / "Flag" / "" (non-enum)
    members: list[tuple[str, str]]  # enum members: (name, description)


def _strip_subscript(base: str) -> str:
    """``GenericUserSkillConnection_1[UserEntity]`` -> ``GenericUserSkillConnection_1``."""
    return base.split("[", 1)[0].strip()


def _clean_doc(text: str) -> str:
    """Strip C# XML-doc artifacts (``<see cref="T:..."/>`` etc.) inherited from
    the backend docstrings, leaving readable prose."""
    def _see_repl(m: re.Match[str]) -> str:
        val = m.group(1)
        name = val.split(":", 1)[-1]   # drop the T:/P:/M: prefix
        name = name.split("(", 1)[0]   # drop method parameter lists
        name = name.rstrip("`0123456789")  # drop generic-arity suffix (`1)
        name = name.split(".")[-1]     # last dotted segment
        return f"`{name}`"

    text = re.sub(r'<see\s+cref="([^"]*)"\s*/>', _see_repl, text)
    text = re.sub(r'<see\s+cref="[^"]*"\s*>(.*?)</see>', r"\1", text)
    text = re.sub(r"<[^>]+>", "", text)        # strip any remaining tags
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _first_doc_line(doc: str) -> str:
    """Return the docstring's summary paragraph, joined onto a single line.

    The backend wraps summaries across several source lines, so stopping at the
    first line truncates them mid-sentence. Collect every line up to the first
    blank line or ``:directive:`` line instead.
    """
    if not doc:
        return ""
    summary: list[str] = []
    for line in doc.splitlines():
        line = line.strip()
        if not line:
            if summary:  # blank line ends the summary paragraph
                break
            continue
        if line.startswith(":"):  # directives begin; no summary left to read
            break
        summary.append(line)
    if not summary:
        return ""
    return _clean_doc(" ".join(summary))


def _parse_doc_directives(doc: str, directive: str) -> dict[str, str]:
    """Parse ``:param name: text`` (or ``:cvar Name: text``) lines into a map.

    Continuation lines (the backend indents wrapped text) are joined onto the
    preceding directive. Descriptions are cleaned of XML-doc artifacts.
    """
    if not doc:
        return {}
    result: dict[str, str] = {}
    current: Optional[str] = None
    buf: list[str] = []
    pattern = re.compile(rf":{directive}\s+(\w+)\s*:\s*(.*)")
    for raw in doc.splitlines():
        line = raw.strip()
        m = pattern.match(line)
        if m:
            if current is not None:
                result[current] = _clean_doc(" ".join(buf))
            current = m.group(1)
            buf = [m.group(2)]
        elif current is not None:
            if line.startswith(":"):  # a different directive ends the current one
                result[current] = _clean_doc(" ".join(buf))
                current = None
                buf = []
            else:
                buf.append(line)
    if current is not None:
        result[current] = _clean_doc(" ".join(buf))
    return result


def _format_default(value: Optional[ast.expr]) -> str:
    """Return a readable default for an annotated assignment, or "" if required.

    Literal and enum-member defaults are shown verbatim because they are
    informative. ``default_factory`` defaults (empty collections, zero-UUIDs,
    ``now()``) carry no useful literal, so they render as "" (shown as "—").
    """
    if value is None:
        return ""
    # `field(default=..., default_factory=...)`
    if isinstance(value, ast.Call) and _name_of(value.func) == "field":
        for kw in value.keywords:
            if kw.arg == "default":
                return ast.unparse(kw.value)
        return ""  # default_factory or metadata only — no informative literal
    return ast.unparse(value)


def _name_of(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _is_enum_base(bases: list[str]) -> tuple[bool, str]:
    for b in bases:
        if b in ("Enum", "IntEnum"):
            return True, "Enum"
        if b == "Flag":
            return True, "Flag"
    return False, ""


def _get_all(tree: ast.Module) -> list[str]:
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == "__all__":
                    if isinstance(node.value, (ast.List, ast.Tuple)):
                        return [
                            el.value
                            for el in node.value.elts
                            if isinstance(el, ast.Constant) and isinstance(el.value, str)
                        ]
    return []


def _parse_module(path: Path) -> tuple[list[str], dict[str, ClassInfo]]:
    """Parse one module; return its ``__all__`` order and a name->ClassInfo map."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    module = path.stem
    order = _get_all(tree)
    classes: dict[str, ClassInfo] = {}

    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        bases = [_strip_subscript(ast.unparse(b)) for b in node.bases]
        is_enum, enum_kind = _is_enum_base(bases)
        docstring = ast.get_docstring(node) or ""
        param_docs = _parse_doc_directives(docstring, "param")
        cvar_docs = _parse_doc_directives(docstring, "cvar")

        fields: list[FieldInfo] = []
        members: list[tuple[str, str]] = []
        for stmt in node.body:
            if is_enum:
                # Enum members: `Name = value`
                if isinstance(stmt, ast.Assign):
                    for tgt in stmt.targets:
                        if isinstance(tgt, ast.Name):
                            members.append((tgt.id, cvar_docs.get(tgt.id, "")))
            else:
                # Dataclass fields: `name: Type` or `name: Type = ...`
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                    name = stmt.target.id
                    fields.append(FieldInfo(
                        name=name,
                        type=ast.unparse(stmt.annotation),
                        default=_format_default(stmt.value),
                        doc=param_docs.get(name, ""),
                    ))

        classes[node.name] = ClassInfo(
            name=node.name,
            module=module,
            bases=bases,
            doc=_first_doc_line(docstring),
            fields=fields,
            is_enum=is_enum,
            enum_kind=enum_kind,
            members=members,
        )
    return order, classes


def _collect(subdir: str) -> tuple[list[tuple[str, list[str]]], dict[str, ClassInfo]]:
    """Parse every module in a subdirectory.

    Returns ``([(module, __all__order)], registry)``.
    """
    per_module: list[tuple[str, list[str]]] = []
    registry: dict[str, ClassInfo] = {}
    base_dir = GENERATED / subdir
    for path in sorted(base_dir.glob("*.py")):
        if path.stem == "__init__":
            continue
        order, classes = _parse_module(path)
        per_module.append((path.stem, order))
        registry.update(classes)
    return per_module, registry


def _resolve_fields(
    name: str, registry: dict[str, ClassInfo], _seen: Optional[set[str]] = None
) -> list[FieldInfo]:
    """Merge a class's own fields with those of its non-root ancestors.

    Base fields come first; a subclass field with the same name overrides the
    inherited one. Resolution stops at :data:`STOP_CLASSES` so the universal
    ``EntityBase`` fields are not repeated for every type.
    """
    if _seen is None:
        _seen = set()
    if name in STOP_CLASSES or name in _seen:
        return []
    _seen.add(name)
    info = registry.get(name)
    if info is None:
        return []

    merged: dict[str, FieldInfo] = {}
    for base in info.bases:
        for f in _resolve_fields(base, registry, _seen):
            merged[f.name] = f
    for f in info.fields:
        # A subclass may redeclare a field without re-documenting it; keep the
        # inherited description in that case.
        if not f.doc and f.name in merged and merged[f.name].doc:
            f = FieldInfo(f.name, f.type, f.default, merged[f.name].doc)
        merged[f.name] = f
    return list(merged.values())


def _classify(info: ClassInfo) -> str:
    """A short tag distinguishing abstract/generic bases from concrete types."""
    if info.name.endswith("Base"):
        return " *(abstract base)*"
    if info.name[-1].isdigit() and "_" in info.name:
        return " *(generic base)*"
    return ""


def _cell(text: str) -> str:
    """Escape a value for use inside a Markdown table cell."""
    return text.replace("\\", "\\\\").replace("|", "\\|")


def _render_fields_table(fields: list[FieldInfo]) -> list[str]:
    if not fields:
        return ["_No type-specific fields beyond the common base fields._", ""]
    lines = [
        "| Field | Type | Default | Description |",
        "| ----- | ---- | ------- | ----------- |",
    ]
    for f in fields:
        default = f"`{_cell(f.default)}`" if f.default else "—"
        desc = _cell(f.doc) if f.doc else ""
        lines.append(f"| `{_cell(f.name)}` | `{_cell(f.type)}` | {default} | {desc} |")
    lines.append("")
    return lines


def _slug(text: str) -> str:
    """GitHub-style heading anchor slug."""
    s = text.strip().lower()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"\s+", "-", s)
    return s


def _service_title(stem: str) -> str:
    """Humanise a module stem into a service heading (``user_service`` -> ``User Service``)."""
    return " ".join(word.capitalize() for word in stem.split("_"))


def _render_class(name: str, registry: dict[str, ClassInfo], heading: str) -> list[str]:
    """Render one entity/connection class at the given heading level (e.g. ``#####``)."""
    info = registry.get(name)
    if info is None:
        return []
    out = [f"{heading} `{info.name}`{_classify(info)}", ""]
    if info.doc:
        out += [info.doc, ""]
    own_bases = [b for b in info.bases if b not in STOP_CLASSES]
    if own_bases:
        out += [f"_Extends:_ {', '.join(f'`{b}`' for b in own_bases)}", ""]
    out += _render_fields_table(_resolve_fields(name, registry))
    return out


def _render_enums() -> list[str]:
    _order, classes = _parse_module(GENERATED / "enums.py")
    order = _get_all(ast.parse((GENERATED / "enums.py").read_text(encoding="utf-8")))
    out = [
        "## Enums",
        "",
        "Defined in `enums.py`. `Flag` enums combine with the bitwise `|` operator; "
        "a member named `None_` is the Python-safe rename of a backend `None` member.",
        "",
    ]
    for name in order:
        info = classes.get(name)
        if info is None or not info.is_enum:
            continue
        out.append(f"### `{info.name}` (`{info.enum_kind}`)")
        out.append("")
        if info.doc:
            out.append(info.doc)
            out.append("")
        if any(desc for _m, desc in info.members):
            out.append("| Member | Description |")
            out.append("| ------ | ----------- |")
            for member, desc in info.members:
                out.append(f"| `{_cell(member)}` | {_cell(desc)} |")
        else:
            members = ", ".join(f"`{m}`" for m, _desc in info.members)
            out.append(f"**Members:** {members}")
        out.append("")
    return out


def build() -> str:
    ent_modules, ent_registry = _collect("entities")
    conn_modules, conn_registry = _collect("connections")

    # Connections may extend entity/generic bases — merge registries for resolution.
    combined: dict[str, ClassInfo] = {**ent_registry, **conn_registry}

    entities_by_service = dict(ent_modules)      # stem -> __all__ order
    connections_by_service = dict(conn_modules)
    all_services = set(entities_by_service) | set(connections_by_service)
    # "core" (foundational types) is surfaced first; the rest are alphabetical.
    services = (["core"] if "core" in all_services else []) + sorted(
        s for s in all_services if s != "core"
    )

    lines: list[str] = [
        "<!-- AUTO-GENERATED by scripts/generate_type_reference.py — DO NOT EDIT BY HAND. -->",
        "<!-- Regenerate with: python3 scripts/generate_type_reference.py -->",
        "",
        "# Type Reference",
        "",
        "An auto-generated catalogue of every entity, connection, and enum exported "
        "from `filament.types`. It is regenerated from the source whenever the types "
        "are, so it never drifts. For the concepts behind this catalogue — the "
        "base-class hierarchy, common fields, and naming conventions — see "
        "[Finding Entity & Connection Types](finding-types.md).",
        "",
        "> Every entity also carries the common `EntityBase` fields (`id`, "
        "`created_at`, `created_by_id`, `updated_at`, `updated_by_id`, `scope_id`), "
        "and every connection additionally carries `from_id`, `to_id`, `from_entity`, "
        "and `to_entity`. Those are documented once in "
        "[Finding Types](finding-types.md#the-base-class-hierarchy) and are not "
        "repeated below.",
        "",
    ]

    # --- Table of contents ---------------------------------------------------
    lines += ["## Contents", "", "- [Enums](#enums)", "- [Services](#services)"]
    for stem in services:
        title = _service_title(stem)
        lines.append(f"  - [{title}](#{_slug(title)})")
    lines.append("")

    # --- Enums (at the top) --------------------------------------------------
    lines += _render_enums()

    # --- Services: entities then connections per service ---------------------
    lines += [
        "## Services",
        "",
        "Types grouped by backend service. Within each service, entities come first, "
        "then connections (where the service defines any). Types tagged "
        "_abstract base_ / _generic base_ are shared parents you don't use directly.",
        "",
    ]
    for stem in services:
        lines += [f"### {_service_title(stem)}", ""]
        entity_names = entities_by_service.get(stem, [])
        connection_names = connections_by_service.get(stem, [])
        if entity_names:
            lines += ["#### Entities", ""]
            for name in entity_names:
                lines += _render_class(name, combined, "#####")
        if connection_names:
            lines += ["#### Connections", ""]
            for name in connection_names:
                lines += _render_class(name, combined, "#####")

    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(build(), encoding="utf-8")
    print(f"Wrote {OUTPUT.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
