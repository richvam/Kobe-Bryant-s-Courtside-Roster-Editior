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

    def replace(self, index: int, blob: bytes) -> None:
        blobs = self.blobs()
        blobs[index] = blob
        self.rebuild(blobs)

    def repoint(self, dst: int, src: int) -> None:
        """Give ``dst`` a copy of ``src``'s blob."""
        if not (0 <= dst < self.count and 0 <= src < self.count):
            raise TableError("entry index out of range")
        blobs = self.blobs()
        blobs[dst] = blobs[src]
        self.rebuild(blobs)

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
