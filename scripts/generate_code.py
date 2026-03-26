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
        default=Path(__file__).resolve().parents[1]
        / "src"
        / "filament"
        / "types"
        / "generated",
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
