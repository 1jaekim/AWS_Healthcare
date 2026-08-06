#!/usr/bin/env bash
# Build the shared Lambda asset used by the two independent A2A functions.
set -euo pipefail

BACKEND="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd "$BACKEND/.." && pwd)"
BUILD_DIR="$BACKEND/build/a2a_lambda"
PYTHON_VERSION="3.12"
PLATFORM="manylinux2014_x86_64"
PYTHON_BIN="${PYTHON_BIN:-python3}"

rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR"

"$PYTHON_BIN" -m pip install \
  --quiet \
  --requirement "$BACKEND/a2a_lambda/requirements.txt" \
  --target "$BUILD_DIR" \
  --platform "$PLATFORM" \
  --python-version "$PYTHON_VERSION" \
  --implementation cp \
  --only-binary=:all: \
  --upgrade

cp -R "$BACKEND/a2a_agents" "$BUILD_DIR/a2a_agents"
cp -R "$REPO_ROOT/agent" "$BUILD_DIR/agent"

find "$BUILD_DIR" -type d -name "__pycache__" -prune -exec rm -rf {} + 2>/dev/null || true
find "$BUILD_DIR" -type d -name "tests" -prune -exec rm -rf {} + 2>/dev/null || true
find "$BUILD_DIR" -type f -name "*.pyc" -delete 2>/dev/null || true

SIZE="$(du -sh "$BUILD_DIR" | cut -f1)"
echo "A2A Lambda asset ready: $BUILD_DIR ($SIZE)"
