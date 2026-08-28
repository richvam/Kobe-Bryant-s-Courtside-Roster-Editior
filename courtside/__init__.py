"""Roster editor for *Kobe Bryant's NBA Courtside* (Nintendo 64).

The package is layered so each piece is usable on its own:

``lzss``        the engine's LZSS codec (decoder and an optimal-parse encoder)
``iff``         the block container and the chunk format inside it
``rom``         ROM byte order, CRC and the game's packed file table
``roster``      the roster database in ``TEAMINFO.IFF``
``appearance``  faces and roster photos in ``TEAMDATA.IFF``
``editor``      the facade the CLI and desktop editor drive
"""

from .editor import EditorError, RosterEditor, SaveReport  # noqa: F401
from .rom import Rom, RomError  # noqa: F401

__all__ = ["RosterEditor", "EditorError", "SaveReport", "Rom", "RomError", "__version__"]

__version__ = "1.0.0"
