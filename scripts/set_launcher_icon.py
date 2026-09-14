"""
Apply assets/launcher_icon.icns as the custom Finder icon on
"Start Office Automation.command".

A custom Finder icon lives in the filesystem's resource-fork metadata,
which git does not track -- so it would not survive cloning this repo
onto another Mac. Re-applying it here, every launch, means the icon just
shows up correctly anywhere instead of depending on that metadata making
the trip, or on a manual one-off setup step.
"""

import os

from Cocoa import NSImage, NSWorkspace

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ICON_PATH = os.path.join(PROJECT_DIR, "assets", "launcher_icon.icns")
TARGET_PATH = os.path.join(PROJECT_DIR, "Start Office Automation.command")


def main() -> None:
    if not os.path.isfile(ICON_PATH) or not os.path.isfile(TARGET_PATH):
        return

    image = NSImage.alloc().initWithContentsOfFile_(ICON_PATH)
    if image is None:
        return

    NSWorkspace.sharedWorkspace().setIcon_forFile_options_(image, TARGET_PATH, 0)


if __name__ == "__main__":
    main()
