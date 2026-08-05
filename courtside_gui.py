#!/usr/bin/env python3
"""Launcher for the offline Courtside roster editor.

Double-click this file, or run it from a terminal:

    python3 courtside_gui.py                  # pick a ROM from the file dialog
    python3 courtside_gui.py courtside.z64    # open one straight away

Needs Python 3.10+ with Tkinter. Nothing else - no internet, no pip install.

If anything goes wrong this script keeps the message on screen instead of
letting the window disappear, and also writes it to courtside-error.log.
"""

import os
import sys
import tempfile
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
MIN_PYTHON = (3, 10)

TK_HELP = """The editor needs Tkinter, which is missing from this Python install.

  Debian, Ubuntu, Mint : sudo apt install python3-tk
  Fedora, RHEL         : sudo dnf install python3-tkinter
  Arch                 : sudo pacman -S tk
  openSUSE             : sudo zypper install python3-tk
  macOS (Homebrew)     : brew install python-tk
  macOS, Windows       : reinstall Python from python.org, which bundles it

Everything except the window works without Tkinter - the command line editor
covers the same features:

  python3 -m courtside YOUR-ROM.z64 info
  python3 -m courtside YOUR-ROM.z64 serve      (the same editor, in a browser)"""


def _log_path():
    """Somewhere writable to leave a copy of the error."""
    for candidate in (os.path.join(HERE, "courtside-error.log"),
                      os.path.join(tempfile.gettempdir(), "courtside-error.log")):
        try:
            with open(candidate, "w", encoding="utf-8"):
                pass
            return candidate
        except OSError:
            continue
    return None


def _show_dialog(message):
    """Put the message in a window, for when there is no console to read."""
    try:
        import tkinter
        from tkinter import messagebox
        root = tkinter.Tk()
        root.withdraw()
        messagebox.showerror("Courtside Roster Editor", message)
        root.destroy()
        return True
    except Exception:
        return False


def fail(message, detail=None):
    """Report a startup failure without letting the window vanish."""
    full = message if not detail else "%s\n\n%s" % (message, detail)
    sys.stderr.write("\n" + full + "\n")

    path = _log_path()
    if path:
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(full + "\n")
            sys.stderr.write("\nA copy of this message is in:\n  %s\n" % path)
        except OSError:
            path = None

    shown = _show_dialog(full if len(full) < 2000 else message)
    if not shown:
        # No Tk, so this is probably a console that is about to close.
        try:
            input("\nPress Enter to close this window... ")
        except (EOFError, KeyboardInterrupt, OSError):
            pass
    sys.exit(1)


def run():
    if sys.version_info < MIN_PYTHON:
        fail("This editor needs Python %d.%d or newer.\n"
             "You are running Python %s from:\n  %s"
             % (MIN_PYTHON[0], MIN_PYTHON[1],
                ".".join(str(n) for n in sys.version_info[:3]), sys.executable))

    sys.path.insert(0, HERE)
    try:
        from courtside.gui import main
    except ImportError as exc:
        name = getattr(exc, "name", "") or ""
        if "tkinter" in (name + str(exc)).lower():
            fail(TK_HELP)
        fail("The editor could not be loaded.\n\n"
             "Make sure courtside_gui.py is still next to the 'courtside' folder "
             "it came with.", traceback.format_exc())
    except Exception:
        fail("The editor could not be loaded.", traceback.format_exc())

    try:
        return main(sys.argv[1:])
    except SystemExit:
        raise
    except Exception:
        fail("The editor stopped because of an unexpected error.\n"
             "Please report this along with the details below.",
             traceback.format_exc())


if __name__ == "__main__":
    raise SystemExit(run())
