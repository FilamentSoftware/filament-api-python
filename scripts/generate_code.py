"""Run the Filament backend code generator for Python.

Deletes the contents of the output path, then invokes the .NET GenerateCode
project to regenerate them.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
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
        / "filament_api"
        / "types"
        / "generated",
        help="Output directory for generated code (default: src/filament_api/_generated).",
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

    # Clear output directory
    if output_path.exists():
        shutil.rmtree(output_path)
    output_path.mkdir(parents=True, exist_ok=True)

    result = subprocess.run(cmd, check=False)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
