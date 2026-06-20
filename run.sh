#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

python3 --version >/dev/null 2>&1 || { echo "Python 3 is required."; exit 1; }

echo ""
echo "======================================"
echo "  Tenable Asset EOL Portal"
echo "  http://localhost:${PORT:-5555}"
echo "======================================"
echo ""

exec python3 app.py
