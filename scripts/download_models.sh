#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)

echo "[INFO] Placeholder model download workflow"
echo "[INFO] Intended usage:"
echo "  1) Download exported runtime model artifacts from your private registry/storage."
echo "  2) Place vision artifacts into: $REPO_ROOT/models/vision/"
echo "  3) Place LLM artifacts into:    $REPO_ROOT/models/llm/"
echo "[INFO] No model files are downloaded by this placeholder script."
