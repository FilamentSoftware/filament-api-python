#!/usr/bin/env bash
# Generate Python code from the Filament backend.
# Usage: ./scripts/generate_code.sh <backend_repo> [-o <output_path>]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
python3 "$SCRIPT_DIR/generate_code.py" "$@"
