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

#: The engine DMAs every block straight out of the ROM and reads the decoded
#: payload with 32-bit loads, so block starts and chunk sizes have to stay on
#: four-byte boundaries.  Every one of the 211 blocks and every chunk in the
#: shipped files obeys this; a misaligned one stops the game loading.
ALIGNMENT = 4


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
        # Pad the compressed stream so the block after it stays 32-bit aligned.
        # The decoder stops at the end-of-stream token, so the filler is never
        # looked at.  A verbatim block must NOT be padded: the engine uses its
        # extent as the DMA length into a BLOCK_SIZE buffer, and anything
        # longer would run off the end of it.
        encoded += b"\0" * (-len(encoded) % ALIGNMENT)
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


def check(data: bytes) -> list[str]:
    """Report anything about a packed container the engine would choke on.

    Written against the invariants every shipped file obeys, so it catches a
    rewrite that decodes perfectly here but would not load on the console.
    """
    problems: list[str] = []
    if data[:4] not in (b"IFF\0", b"iff\0"):
        return ["bad container magic %r" % (data[:4],)]
    size = struct.unpack_from(">I", data, 4)[0]
    nblocks = (size + BLOCK_SIZE - 1) // BLOCK_SIZE
    table = struct.unpack_from(">%dI" % (nblocks + 1), data, 8)
    starts = [(t >> 4) - 4 for t in table]

    for i, start in enumerate(starts):
        if start % ALIGNMENT:
            problems.append("block %d starts at 0x%X, not %d-byte aligned"
                            % (i, start, ALIGNMENT))
    for i in range(nblocks):
        length = starts[i + 1] - starts[i]
        if length <= 0:
            problems.append("block %d has length %d" % (i, length))
        elif (table[i] & 0xF) == 0 and length > BLOCK_SIZE:
            problems.append(
                "block %d is stored verbatim but %d bytes long; the engine "
                "would DMA it into a %d-byte buffer" % (i, length, BLOCK_SIZE))
    if starts[0] != 8 + 4 * (nblocks + 1):
        problems.append("first block starts at 0x%X, expected 0x%X"
                        % (starts[0], 8 + 4 * (nblocks + 1)))
    if starts[-1] + 4 > len(data):
        problems.append("the block table runs past the end of the file")

    try:
        payload = unpack(data).payload
    except (ContainerError, lzss.CorruptStream) as exc:
        problems.append("payload does not decode: %s" % exc)
        return problems
    if len(payload) != size:
        problems.append("payload decoded to %d bytes, header says %d"
                        % (len(payload), size))
    off = 12
    while off + 8 <= len(payload):
        ident = payload[off:off + 4]
        chunk_size = struct.unpack_from(">I", payload, off + 4)[0]
        if off % ALIGNMENT:
            problems.append("chunk %r starts at 0x%X, not aligned" % (ident, off))
        if chunk_size % ALIGNMENT:
            problems.append("chunk %r is %d bytes, not a multiple of %d"
                            % (ident, chunk_size, ALIGNMENT))
        if off + 8 + chunk_size > len(payload):
            break
        off += 8 + chunk_size + (chunk_size & 1)
    return problems


def build_chunks(cf: ChunkFile) -> bytes:
    body = bytearray(cf.form)
    for c in cf.chunks:
        # Pad the payload to a four-byte multiple and count the padding in the
        # size, exactly as the shipped files do, so every following chunk stays
        # aligned for the 32-bit reads the engine does inside them.
        data = c.data + b"\0" * (-len(c.data) % ALIGNMENT)
        body += c.ident
        body += struct.pack(">I", len(data))
        body += data
    return cf.magic + struct.pack(">I", len(body)) + bytes(body)
