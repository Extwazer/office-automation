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

# Once running, the app lives in the menu bar (see office.py's
# OfficeAutomationApp) -- this Terminal window is just how it got launched,
# so minimize it almost immediately instead of leaving it on screen. (Not
# closed: macOS's own "a process is still running in this window" warning
# fires for that -- even for a script closing its own window -- and can't
# be answered non-interactively, so a scripted close can't be made silent
# and reliable. Minimizing has no such prompt.)
(
    sleep 0.2
    osascript -e 'tell application "Terminal" to set miniaturized of front window to true' \
        >/dev/null 2>&1
) &

"$DIR/.venv/bin/python3" "$DIR/office.py"
