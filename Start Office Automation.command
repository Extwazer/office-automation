#!/bin/bash
# Double-click launcher: runs office.py in Terminal, using this project's
# own venv. Launching via Terminal (instead of a standalone .app bundle)
# means it reuses Terminal.app's own Accessibility permission instead of
# needing a fresh, separate macOS permission grant of its own.
#
# Resolves its own directory instead of a hardcoded path, so this keeps
# working after cloning the repo anywhere, on any machine/username.
DIR="$(cd "$(dirname "$0")" && pwd)"

cd "$DIR"
"$DIR/.venv/bin/python3" "$DIR/office.py"
