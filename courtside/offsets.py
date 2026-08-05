"""The engine's ``count + offset table + payload`` chunk shape.

``PTEX``, ``PIMG`` and ``PNAM`` all use it::

    u32       entry count
    u32[n+1]  offsets from the start of the chunk
    ...       entry data

Entry *i* runs from ``offsets[i]`` to ``offsets[i + 1]``, so an entry's extent
is implied by its neighbour.  Entries therefore cannot share storage - two
players wanting the same blob need two copies of it.

Every offset in the shipped tables is a multiple of four, and so is every
entry size - across all 406 face, 386 photo and 769 name entries, without one
exception.  That is not luck: the engine reads these blobs with 32-bit loads,
and a misaligned one faults the CPU.  :meth:`OffsetTable.rebuild` keeps the
invariant by padding each entry out to a four-byte boundary.

Rebuilding is the expensive way to change one entry, because every entry
behind it slides.  A photo blob's LZSS stream ends at its own end-of-stream
token and an announcer clip declares its own length in its header, so a
replacement that fits the slot it is going into can simply be dropped in and
padded instead - which is what :meth:`OffsetTable.replace` tries first.
"""

from __future__ import annotations

import struct

ALIGNMENT = 4


class TableError(ValueError):
    pass


class OffsetTable:
    def __init__(self, data: bytes) -> None:
        self.count = struct.unpack_from(">I", data, 0)[0]
        self.offsets = list(struct.unpack_from(">%dI" % (self.count + 1), data, 4))
        self.data = data

    def entry(self, index: int) -> bytes:
        if not 0 <= index < self.count:
            raise TableError("entry %d out of range (0-%d)" % (index, self.count - 1))
        return self.data[self.offsets[index]:self.offsets[index + 1]]

    def blobs(self) -> list[bytes]:
        return [self.entry(i) for i in range(self.count)]

    def slot(self, index: int) -> int:
        """How many bytes entry ``index`` currently occupies."""
        if not 0 <= index < self.count:
            raise TableError("entry %d out of range (0-%d)" % (index, self.count - 1))
        return self.offsets[index + 1] - self.offsets[index]

    def replace(self, index: int, blob: bytes) -> None:
        if self._replace_in_place(index, blob):
            return
        blobs = self.blobs()
        blobs[index] = blob
        self.rebuild(blobs)

    def repoint(self, dst: int, src: int) -> None:
        """Give ``dst`` a copy of ``src``'s blob."""
        if not (0 <= dst < self.count and 0 <= src < self.count):
            raise TableError("entry index out of range")
        self.replace(dst, self.entry(src))

    def replace_many(self, changes: dict[int, bytes]) -> None:
        """Apply several replacements, moving nothing unless something must."""
        if all(len(b) <= self.slot(i) for i, b in changes.items()):
            for i, blob in changes.items():
                self._replace_in_place(i, blob)
            return
        blobs = self.blobs()
        for i, blob in changes.items():
            blobs[i] = blob
        self.rebuild(blobs)

    def _replace_in_place(self, index: int, blob: bytes) -> bool:
        """Drop a blob into its existing slot without moving anything else.

        Both kinds of blob stored this way carry their own length - a photo's
        LZSS stream ends at its end-of-stream token, an announcer clip declares
        its own size in its header - so filler past the end is never looked at.
        Keeping the slot means every other entry stays byte for byte where it
        was, which is what stops a small edit from pushing the whole file
        downhill and forcing the packed files behind it to move.
        """
        room = self.slot(index)
        if len(blob) > room:
            return False
        start = self.offsets[index]
        data = bytearray(self.data)
        data[start:start + room] = blob + b"\0" * (room - len(blob))
        self.data = bytes(data)
        return True

    def rebuild(self, blobs: list[bytes]) -> None:
        header = 4 + 4 * (len(blobs) + 1)   # already a multiple of four
        body = bytearray()
        offsets = []
        for blob in blobs:
            offsets.append(header + len(body))
            body += blob
            pad = -len(blob) % ALIGNMENT
            if pad:
                body += b"\0" * pad         # keeps the next entry 32-bit aligned
        offsets.append(header + len(body))
        out = bytearray(struct.pack(">I", len(blobs)))
        out += struct.pack(">%dI" % (len(blobs) + 1), *offsets)
        out += body
        self.count = len(blobs)
        self.offsets = offsets
        self.data = bytes(out)
