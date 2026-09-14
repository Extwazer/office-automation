#!/bin/bash
# Double-click launcher: runs office.py in Terminal, using this project's
# own venv. Launching via Terminal (instead of a standalone .app bundle)
# means it reuses Terminal.app's own Accessibility permission instead of
# needing a fresh, separate macOS permission grant of its own.

cd "/Users/stanislav/Local Sites/office"
.venv/bin/python3 office.py
