#!/usr/bin/env python3
"""Launcher for the offline Courtside roster editor.

Double-click this file, or run it from a terminal:

    python3 courtside_gui.py                  # pick a ROM from the file dialog
    python3 courtside_gui.py courtside.z64    # open one straight away

Needs Python 3.10+ with Tkinter. Nothing else - no internet, no pip install.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from courtside.gui import main
except ImportError as exc:  # pragma: no cover - first-run diagnostics
    if "tkinter" in str(exc).lower() or "_tkinter" in str(exc).lower():
        sys.exit(
            "This editor needs Tkinter, which is missing from your Python install.\n\n"
            "  Debian/Ubuntu : sudo apt install python3-tk\n"
            "  Fedora        : sudo dnf install python3-tkinter\n"
            "  Arch          : sudo pacman -S tk\n"
            "  macOS/Windows : use the installer from python.org, which bundles it\n\n"
            "The command line editor works without Tkinter:\n"
            "  python3 -m courtside YOUR-ROM.z64 info"
        )
    raise

if __name__ == "__main__":
    raise SystemExit(main())
