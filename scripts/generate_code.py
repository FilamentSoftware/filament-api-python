"""Run the Filament backend code generator for Python.

Invokes the .NET GenerateCode project into a staging directory, then replaces
the output path on success.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> int:
    default_output_path = (
        Path(__file__).resolve().parents[1] / "src" / "filament" / "types" / "generated"
    )
    parser = argparse.ArgumentParser(
        description="Generate Python code from the Filament backend.",
    )
    parser.add_argument(
        "backend_repo",
        type=Path,
        help="Path to the Filament backend repository.",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=default_output_path,
        help="Output directory for generated code (default: src/filament/types/generated).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the command that would be run without executing it.",
    )
    args = parser.parse_args()

    backend_repo: Path = args.backend_repo.resolve()
    output_path: Path = args.output.resolve()

    csproj = (
        backend_repo
        / "utils"
        / "GenerateCode"
        / "GenerateCode"
        / "GenerateCode.csproj"
    )
    if not csproj.exists():
        print(f"Error: GenerateCode project not found at {csproj}", file=sys.stderr)
        return 1

    cmd = [
        "dotnet",
        "run",
        "--project",
        str(csproj),
        "--",
        "--language",
        "python",
        "--exclude-system-types",
        "--api",
        "--entity-base",
        # Relative import: EntityBase is generated at
        # src/filament/types/generated/base/base.py, so reaching
        # filament._specified_properties is four levels up
        # (base -> generated -> types -> filament).
        "...._specified_properties.SpecifiedPropertiesBase",
        "-o",
        str(output_path),
    ]

    if args.dry_run:
        print(f"Would delete: {output_path}")
        print(" ".join(cmd))
        return 0

    # Clear output directory — guard against deleting outside the repo
    repo_root = Path(__file__).resolve().parents[1]
    if (
        output_path == repo_root
        or output_path == output_path.parent
        or not output_path.is_relative_to(repo_root)
    ):
        print(f"Refusing to delete unsafe output path: {output_path}", file=sys.stderr)
        return 1

    # Generate into a staging directory, then swap on success
    output_path.parent.mkdir(parents=True, exist_ok=True)
    staging_path = Path(
        tempfile.mkdtemp(prefix="filament_codegen_", dir=output_path.parent)
    )
    cmd[-1] = str(staging_path)

    try:
        result = subprocess.run(cmd, check=False)
    except FileNotFoundError:
        print("Error: `dotnet` executable not found on PATH.", file=sys.stderr)
        shutil.rmtree(staging_path, ignore_errors=True)
        return 1

    if result.returncode != 0:
        shutil.rmtree(staging_path, ignore_errors=True)
        return result.returncode

    if output_path.exists():
        if not output_path.is_dir():
            print(f"Error: output path exists and is not a directory: {output_path}", file=sys.stderr)
            shutil.rmtree(staging_path, ignore_errors=True)
            return 1
        shutil.rmtree(output_path)
    staging_path.replace(output_path)

    # Regenerate the human-readable type reference from the new output so it
    # never drifts from the generated dataclasses. A failure here fails the
    # run: a silently stale docs/type-reference.md is worse than a red exit.
    # The reference generator only reads the default tree, so it has nothing
    # to say about output written elsewhere.
    if output_path != default_output_path:
        print(
            f"Note: docs/type-reference.md not regenerated, because {output_path} "
            "is not the default output directory."
        )
        return 0
    if not _regenerate_type_reference():
        print(
            "Error: generated types were updated, but docs/type-reference.md "
            "was NOT regenerated and is now stale. Fix the error above and "
            "re-run scripts/generate_type_reference.py.",
            file=sys.stderr,
        )
        return 1
    return 0


def _regenerate_type_reference() -> bool:
    """Run the type-reference doc generator. Returns False on failure."""
    script = Path(__file__).resolve().parent / "generate_type_reference.py"
    if not script.exists():
        print(f"Error: type reference generator not found at {script}", file=sys.stderr)
        return False
    try:
        subprocess.run([sys.executable, str(script)], check=True)
    except (subprocess.CalledProcessError, OSError) as exc:
        print(f"Error: failed to regenerate type reference: {exc}", file=sys.stderr)
        return False
    return True


if __name__ == "__main__":
    raise SystemExit(main())
