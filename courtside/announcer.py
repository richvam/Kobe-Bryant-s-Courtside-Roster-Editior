"""The public address announcer's recordings, in ``TEAMTALK.IFF``.

Unlike the other packed files this one carries no ``0x735764B0`` compression
header, so the boot-time scan leaves it alone and the engine reads it straight
off the cartridge.  What is left is a bare ``AIFF`` chunk list - the same thing
the other two files hold *inside* their compression wrapper::

    char[4]   'AIFF'
    u32       size of everything after this field
    char[4]   'LFPT'
    repeat:   char[4] chunk id, u32 size, data padded to a multiple of four

``PNAM`` holds the spoken player names and ``TALK`` the general commentary,
both in the usual count/offset-table shape.

The mapping is not guesswork.  The routine at RAM ``0x8008AB08`` reads the
player id straight out of the record (``lhu $a2, 8($a2)``) and indexes the
``PNAM`` table at ``id * 2 + n``, where ``n`` picks one of the player's two
clips.  Allowing for the count word that sits in front of the offsets, player
*N* owns entries ``2N - 1`` and ``2N`` - which is why the chunk holds
``1 + 2 * 384`` of them.

Which of the pair is the given name and which the surname is inferred rather
than proven: clip lengths track the two halves of each name (Rick Fox has the
shortest pair, Shareef Abdur-Rahim one of the longest).  Copying a call moves
both by default, so the distinction rarely matters.
"""

from __future__ import annotations

import struct

from . import iff
from .offsets import OffsetTable

FORM = b"LFPT"


class AnnouncerError(ValueError):
    pass


class AnnouncerDatabase:
    """Parsed ``TEAMTALK.IFF``."""

    def __init__(self, data: bytes) -> None:
        try:
            self.chunkfile = iff.parse_chunks(data)
        except iff.ContainerError as exc:
            raise AnnouncerError(str(exc)) from None
        if self.chunkfile.form != FORM:
            raise AnnouncerError(
                "not an %s container (found %r)" % (FORM.decode(), self.chunkfile.form))
        try:
            self.names = OffsetTable(self.chunkfile.get(b"PNAM"))
        except KeyError:
            raise AnnouncerError("this TEAMTALK.IFF has no PNAM chunk") from None

    @property
    def chunks(self) -> list[tuple[bytes, bytes]]:
        return [(c.ident, c.data) for c in self.chunkfile.chunks]

    # -- clip lookup ----------------------------------------------------
    def clip_indices(self, player_id: int) -> tuple[int, int]:
        """The two ``PNAM`` entries the announcer uses for this player."""
        first, last = player_id * 2 - 1, player_id * 2
        if not (0 < first and last < self.names.count):
            raise AnnouncerError(
                "player id %d has no announcer clips (the chunk holds %d)"
                % (player_id, self.names.count))
        return first, last

    def clip_lengths(self, player_id: int) -> tuple[int, int]:
        first, last = self.clip_indices(player_id)
        return len(self.names.entry(first)), len(self.names.entry(last))

    def has_call(self, player_id: int) -> bool:
        try:
            return all(self.clip_lengths(player_id))
        except AnnouncerError:
            return False

    # -- editing --------------------------------------------------------
    def copy_call(self, dst_player_id: int, src_player_id: int,
                  given_name: bool = True, surname: bool = True) -> None:
        """Make the announcer say ``src``'s name for ``dst``."""
        dst_first, dst_last = self.clip_indices(dst_player_id)
        src_first, src_last = self.clip_indices(src_player_id)
        changes: dict[int, bytes] = {}
        if given_name:
            changes[dst_first] = self.names.entry(src_first)
        if surname:
            changes[dst_last] = self.names.entry(src_last)
        self.names.replace_many(changes)

    def swap_call(self, a_player_id: int, b_player_id: int) -> None:
        a_first, a_last = self.clip_indices(a_player_id)
        b_first, b_last = self.clip_indices(b_player_id)
        entry = self.names.entry
        self.names.replace_many({
            a_first: entry(b_first), b_first: entry(a_first),
            a_last: entry(b_last), b_last: entry(a_last),
        })

    def silence(self, player_id: int) -> None:
        """Leave the announcer with nothing to say for this player."""
        first, last = self.clip_indices(player_id)
        blobs = self.names.blobs()
        blobs[first] = blobs[last] = b""
        self.names.rebuild(blobs)

    # -- serialisation --------------------------------------------------
    def to_file(self) -> bytes:
        self.chunkfile.set(b"PNAM", self.names.data)
        return iff.build_chunks(self.chunkfile)

    def problems(self) -> list[str]:
        return iff.check_chunks(self.to_file())


def load(blob: bytes) -> AnnouncerDatabase:
    return AnnouncerDatabase(blob)
