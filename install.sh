#!/usr/bin/env sh
# mutexcc one-line installer — drops the single-file CLI onto your PATH.
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/august/mutexcc/main/install.sh | sh
# Override the install dir with PREFIX=/somewhere ... | sh
set -e

REPO_RAW="${MUTEXCC_RAW:-https://raw.githubusercontent.com/august/mutexcc/main/mutexcc.py}"
PREFIX="${PREFIX:-$HOME/.local/bin}"
DEST="$PREFIX/mutexcc"

if ! command -v python3 >/dev/null 2>&1; then
    echo "mutexcc needs python3 (3.8+); please install it first." >&2
    exit 1
fi

mkdir -p "$PREFIX"
echo "Downloading mutexcc -> $DEST"
curl -fsSL "$REPO_RAW" -o "$DEST"
chmod +x "$DEST"

echo "Installed: $DEST"
case ":$PATH:" in
    *":$PREFIX:"*) ;;
    *) echo "NOTE: $PREFIX is not on your PATH. Add this to your shell rc:"
       echo "      export PATH=\"$PREFIX:\$PATH\"" ;;
esac
echo
echo "Next:"
echo "  cd your-repo && mutexcc install-hooks   # enforced auto-locking"
