"""The block-addressed ``IFF`` container and the EA-style chunk format inside.

Container layout (big-endian)::

    +0x00  'IFF\\0' or 'iff\\0'    magic (case is preserved on rewrite)
    +0x04  u32                    size of the decoded payload
    +0x08  u32 * (nblocks + 1)    block table, nblocks = ceil(size / 0x2000)
    ...                           block data

Each block table entry packs an offset and a flag: ``(offset + 4) << 4 | flag``.
The ``+ 4`` bias is what the engine's loader applies, and the final entry marks
the end of the last block, so the whole file is ``last_end + 4`` bytes long.
A flag of 0 means the block is stored verbatim; anything else means it is
LZSS-compressed.  Every block decodes to exactly 0x2000 bytes except the last.

The decoded payload is an ``AIFF`` container: a four byte magic, a u32 size, a
four byte form id (``LFPI`` for TEAMINFO, ``LFPD`` for TEAMDATA) and then a run
of ``id/size/data`` chunks padded to even lengths.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

from . import lzss

BLOCK_SIZE = 0x2000
COMPRESSED_FLAG = 1


class ContainerError(ValueError):
    pass


# --------------------------------------------------------------------------
# block container
# --------------------------------------------------------------------------

@dataclass
class Container:
    """A parsed ``IFF`` block container."""

    magic: bytes
    payload: bytes
    #: raw bytes of each block as stored in the file, kept so untouched blocks
    #: can be written back byte for byte instead of being recompressed.
    blocks: list[bytes] = field(default_factory=list)
    flags: list[int] = field(default_factory=list)
    #: the four filler bytes past the last block implied by the offset bias
    tail: bytes = b"\0\0\0\0"

    @property
    def block_count(self) -> int:
        return (len(self.payload) + BLOCK_SIZE - 1) // BLOCK_SIZE


def unpack(data: bytes) -> Container:
    """Decode a container into its payload, keeping the original blocks."""
    magic = data[:4]
    if magic not in (b"IFF\0", b"iff\0"):
        raise ContainerError("not an IFF container (magic %r)" % (magic,))
    size = struct.unpack_from(">I", data, 4)[0]
    nblocks = (size + BLOCK_SIZE - 1) // BLOCK_SIZE
    need = 8 + 4 * (nblocks + 1)
    if len(data) < need:
        raise ContainerError("container truncated: want %d bytes, have %d" % (need, len(data)))
    table = struct.unpack_from(">%dI" % (nblocks + 1), data, 8)

    payload = bytearray()
    blocks: list[bytes] = []
    flags: list[int] = []
    for i in range(nblocks):
        start = (table[i] >> 4) - 4
        end = (table[i + 1] >> 4) - 4
        flag = table[i] & 0xF
        want = min(BLOCK_SIZE, size - i * BLOCK_SIZE)
        raw = data[start:end]
        if flag == 0:
            decoded = raw[:want]
        else:
            decoded = lzss.decompress(raw, want)[:want]
        if len(decoded) != want:
            raise ContainerError(
                "block %d decoded to %d bytes, expected %d" % (i, len(decoded), want)
            )
        payload += decoded
        blocks.append(bytes(raw))
        flags.append(flag)
    last_end = (table[nblocks] >> 4) - 4
    tail = data[last_end:last_end + 4]
    return Container(magic=magic, payload=bytes(payload), blocks=blocks, flags=flags,
                     tail=bytes(tail).ljust(4, b"\0"))


def pack(payload: bytes, magic: bytes = b"IFF\0", original: Container | None = None) -> bytes:
    """Build a container around ``payload``.

    When ``original`` is supplied, blocks whose decoded contents are unchanged
    are copied across verbatim.  That keeps rewrites cheap (only the handful of
    blocks a roster edit touches get recompressed) and keeps the output
    byte-identical to the input when nothing changed.
    """
    size = len(payload)
    nblocks = (size + BLOCK_SIZE - 1) // BLOCK_SIZE
    header_len = 8 + 4 * (nblocks + 1)

    old_payload = original.payload if original is not None else b""
    stored: list[tuple[bytes, int]] = []
    for i in range(nblocks):
        chunk = payload[i * BLOCK_SIZE:(i + 1) * BLOCK_SIZE]
        old_chunk = old_payload[i * BLOCK_SIZE:(i + 1) * BLOCK_SIZE]
        if original is not None and i < len(original.blocks) and chunk == old_chunk:
            stored.append((original.blocks[i], original.flags[i]))
            continue
        encoded = lzss.compress(chunk)
        if len(encoded) >= len(chunk):
            stored.append((chunk, 0))  # verbatim is smaller, the format allows it
        else:
            stored.append((encoded, COMPRESSED_FLAG))

    out = bytearray(header_len)
    table: list[int] = []
    for blob, flag in stored:
        table.append(((len(out) + 4) << 4) | flag)
        out += blob
    table.append((len(out) + 4) << 4)

    out[0:4] = magic
    struct.pack_into(">I", out, 4, size)
    struct.pack_into(">%dI" % (nblocks + 1), out, 8, *table)
    out += original.tail if original is not None else b"\0\0\0\0"
    return bytes(out)


# --------------------------------------------------------------------------
# AIFF chunk list
# --------------------------------------------------------------------------

@dataclass
class Chunk:
    ident: bytes
    data: bytes


@dataclass
class ChunkFile:
    magic: bytes
    form: bytes
    chunks: list[Chunk]

    def get(self, ident: bytes) -> bytes:
        for c in self.chunks:
            if c.ident == ident:
                return c.data
        raise KeyError(ident)

    def set(self, ident: bytes, data: bytes) -> None:
        for c in self.chunks:
            if c.ident == ident:
                c.data = data
                return
        raise KeyError(ident)


def parse_chunks(payload: bytes) -> ChunkFile:
    magic = payload[:4]
    if magic not in (b"AIFF", b"aiff"):
        raise ContainerError("unexpected payload magic %r" % (magic,))
    form = payload[8:12]
    chunks: list[Chunk] = []
    off = 12
    while off + 8 <= len(payload):
        ident = payload[off:off + 4]
        size = struct.unpack_from(">I", payload, off + 4)[0]
        if off + 8 + size > len(payload):
            break
        chunks.append(Chunk(ident, payload[off + 8:off + 8 + size]))
        off += 8 + size + (size & 1)
    return ChunkFile(magic=magic, form=form, chunks=chunks)


def build_chunks(cf: ChunkFile) -> bytes:
    body = bytearray(cf.form)
    for c in cf.chunks:
        body += c.ident
        body += struct.pack(">I", len(c.data))
        body += c.data
        if len(c.data) & 1:
            body += b"\0"
    return cf.magic + struct.pack(">I", len(body)) + bytes(body)
