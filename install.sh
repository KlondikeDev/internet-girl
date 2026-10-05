#!/usr/bin/env bash
# Internet Girl installer: venv + `igirl` on your PATH + choose where girls live.
set -euo pipefail
cd "$(dirname "$0")"
PY="${PYTHON:-python3}"
"$PY" -c 'import sys; assert sys.version_info >= (3, 10), "Python 3.10+ needed"'
[ -d .venv ] || "$PY" -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -e '.[images]' || .venv/bin/pip install -q -e .
mkdir -p "$HOME/.local/bin"
ln -sf "$PWD/.venv/bin/igirl" "$HOME/.local/bin/igirl"
echo "✨ installed igirl → ~/.local/bin/igirl"
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) echo "   (add ~/.local/bin to your PATH)";; esac
.venv/bin/igirl install
