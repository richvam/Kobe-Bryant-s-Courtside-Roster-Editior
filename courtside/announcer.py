"""The public address announcer's recordings, in ``TEAMTALK.IFF``.

Unlike the other packed files this one is not block-compressed - it is a plain
``LFPT`` container::

    u32       size of everything after this field
    char[4]   'LFPT'
    repeat:   char[4] chunk id, u32 size, data padded to an even length

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

from .offsets import OffsetTable

FORM = b"LFPT"


class AnnouncerError(ValueError):
    pass


class AnnouncerDatabase:
    """Parsed ``TEAMTALK.IFF``."""

    def __init__(self, data: bytes) -> None:
        if data[4:8] != FORM:
            raise AnnouncerError("not an %s container (found %r)" % (FORM.decode(), data[4:8]))
        self.chunks: list[tuple[bytes, bytes]] = []
        off = 8
        while off + 8 <= len(data):
            tag = data[off:off + 4]
            size = struct.unpack_from(">I", data, off + 4)[0]
            if size == 0 or off + 8 + size > len(data):
                break
            self.chunks.append((tag, data[off + 8:off + 8 + size]))
            off += 8 + size + (size & 1)
        #: alignment filler the ROM leaves past the last chunk
        self.tail = bytes(data[off:])
        body = dict(self.chunks)
        if b"PNAM" not in body:
            raise AnnouncerError("this TEAMTALK.IFF has no PNAM chunk")
        self.names = OffsetTable(body[b"PNAM"])

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
        blobs = self.names.blobs()
        if given_name:
            blobs[dst_first] = blobs[src_first]
        if surname:
            blobs[dst_last] = blobs[src_last]
        self.names.rebuild(blobs)

    def swap_call(self, a_player_id: int, b_player_id: int) -> None:
        a_first, a_last = self.clip_indices(a_player_id)
        b_first, b_last = self.clip_indices(b_player_id)
        blobs = self.names.blobs()
        blobs[a_first], blobs[b_first] = blobs[b_first], blobs[a_first]
        blobs[a_last], blobs[b_last] = blobs[b_last], blobs[a_last]
        self.names.rebuild(blobs)

    def silence(self, player_id: int) -> None:
        """Leave the announcer with nothing to say for this player."""
        first, last = self.clip_indices(player_id)
        blobs = self.names.blobs()
        blobs[first] = blobs[last] = b""
        self.names.rebuild(blobs)

    # -- serialisation --------------------------------------------------
    def to_file(self) -> bytes:
        body = bytearray(FORM)
        for tag, data in self.chunks:
            payload = self.names.data if tag == b"PNAM" else data
            body += tag + struct.pack(">I", len(payload)) + payload
            if len(payload) & 1:
                body += b"\0"
        return struct.pack(">I", len(body)) + bytes(body) + self.tail


def load(blob: bytes) -> AnnouncerDatabase:
    return AnnouncerDatabase(blob)
