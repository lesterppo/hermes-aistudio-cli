#!/usr/bin/env bash
# Install the `ais` CLI into ~/.local/bin and create its runtime state dirs.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="${AIS_BIN_DIR:-$HOME/.local/bin}"
STATE="${AIS_HOME:-$HOME/.aistudio-cli}"

mkdir -p "$BIN" "$STATE" "$STATE/out" "$STATE/sessions"
chmod 700 "$STATE"

chmod +x "$SRC/ais.py"
ln -sf "$SRC/ais.py" "$BIN/ais"

echo "installed: $BIN/ais"
echo "state dir: $STATE"

case ":$PATH:" in
  *":$BIN:"*) ;;
  *) echo "NOTE: $BIN is not on PATH — add it to your shell profile." ;;
esac

echo
echo "verifying (needs a browser signed in to https://aistudio.google.com)..."
if command -v ais >/dev/null 2>&1; then
  ais auth || {
    echo
    echo "auth failed. Sign in to https://aistudio.google.com in Firefox, then run:"
    echo "  ais auth --capture"
    exit 1
  }
else
  "$BIN/ais" auth || echo "auth failed — run '$BIN/ais auth --capture' after signing in"
fi
