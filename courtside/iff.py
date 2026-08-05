"""The block-addressed ``IFF`` container and the EA-style chunk format inside.

Every entry in the game's packed-file table starts with an eight byte asset
header, and the boot-time scan at RAM ``0x8007D41C`` reads sixteen bytes from
each file to decide what it is: a magic of ``73 57 64 B0`` marks a
block-compressed file, sets the "compressed" bit in the file entry and takes
the file's *logical* size from the third word.  Anything else is read straight
off the cartridge.

Container layout (big-endian)::

    +0x00  u32                    0x735764B0, the asset magic
    +0x04  'IFF\\0' or 'iff\\0'     tag (case is preserved on rewrite)
    +0x08  u32                    size of the decoded payload
    +0x0C  u32 * (nblocks + 1)    block table, nblocks = ceil(size / 0x2000)
    ...                           block data

Each block table entry packs an offset and a flag: ``offset << 4 | flag``.  The
offset is counted from the start of the file - which is why the first entry
reads ``0xC + 4 * (nblocks + 1)``, and why the final entry, marking the end of
the last block, is exactly the file's length.  A flag of 0 means the block is
stored verbatim; anything else means it is LZSS-compressed.  Every block
decodes to exactly 0x2000 bytes except the last.

The block loader at ``0x8007DBFC`` reads the table out of the file's first
0x2000 bytes at ``+0xC``, DMAs ``end - start`` bytes into an 0x2000 byte
buffer and, for a compressed block, hands that to the decompressor.  So the
table must live inside the first 0x2000 bytes and no block may be longer than
0x2000 bytes stored.

The decoded payload is an ``AIFF`` container: a four byte magic, a u32 size, a
four byte form id (``LFPI`` for TEAMINFO, ``LFPD`` for TEAMDATA) and then a run
of ``id/size/data`` chunks.  ``TEAMTALK.IFF`` is an ``AIFF`` container on its
own, with no compression wrapper around it.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

from . import lzss

BLOCK_SIZE = 0x2000
COMPRESSED_FLAG = 1

#: The word the boot-time scan looks for to mark a block-compressed file.
ASSET_MAGIC = 0x735764B0

TAGS = (b"IFF\0", b"iff\0")

#: The engine DMAs every block straight out of the ROM and reads the decoded
#: payload with 32-bit loads, so block starts and chunk sizes have to stay on
#: four-byte boundaries.  The chunk walker at ``0x80088ABC`` rounds every chunk
#: size up to a multiple of four before stepping over it, so a size that is not
#: already a multiple of four silently desynchronises the walk.
ALIGNMENT = 4

#: Where the block table starts inside the file.
TABLE_OFFSET = 0xC


class ContainerError(ValueError):
    pass


def _round_up(value: int, to: int = ALIGNMENT) -> int:
    return (value + to - 1) & ~(to - 1)


# --------------------------------------------------------------------------
# block container
# --------------------------------------------------------------------------

@dataclass
class Container:
    """A parsed ``IFF`` block container."""

    payload: bytes
    #: the asset magic, kept verbatim so a rewrite is byte-identical
    magic: int = ASSET_MAGIC
    #: ``IFF\\0`` or ``iff\\0``
    tag: bytes = b"IFF\0"
    #: raw bytes of each block as stored in the file, kept so untouched blocks
    #: can be written back byte for byte instead of being recompressed.
    blocks: list[bytes] = field(default_factory=list)
    flags: list[int] = field(default_factory=list)

    @property
    def block_count(self) -> int:
        return (len(self.payload) + BLOCK_SIZE - 1) // BLOCK_SIZE


def is_container(data: bytes) -> bool:
    """True when the packed file carries the compressed-asset magic."""
    return (len(data) >= 8
            and struct.unpack_from(">I", data, 0)[0] == ASSET_MAGIC
            and data[4:8] in TAGS)


def unpack(data: bytes) -> Container:
    """Decode a container into its payload, keeping the original blocks."""
    if len(data) < TABLE_OFFSET:
        raise ContainerError("container truncated: %d bytes" % len(data))
    magic = struct.unpack_from(">I", data, 0)[0]
    if magic != ASSET_MAGIC:
        raise ContainerError("not a packed asset (magic 0x%08X)" % magic)
    tag = data[4:8]
    if tag not in TAGS:
        raise ContainerError("not an IFF container (tag %r)" % (tag,))
    size = struct.unpack_from(">I", data, 8)[0]
    nblocks = (size + BLOCK_SIZE - 1) // BLOCK_SIZE
    need = TABLE_OFFSET + 4 * (nblocks + 1)
    if len(data) < need:
        raise ContainerError("container truncated: want %d bytes, have %d" % (need, len(data)))
    table = struct.unpack_from(">%dI" % (nblocks + 1), data, TABLE_OFFSET)

    payload = bytearray()
    blocks: list[bytes] = []
    flags: list[int] = []
    for i in range(nblocks):
        start = table[i] >> 4
        end = table[i + 1] >> 4
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
    return Container(payload=bytes(payload), magic=magic, tag=tag,
                     blocks=blocks, flags=flags)


def pack(payload: bytes, original: Container | None = None,
         magic: int = ASSET_MAGIC, tag: bytes = b"IFF\0") -> bytes:
    """Build a container around ``payload``.

    When ``original`` is supplied, blocks whose decoded contents are unchanged
    are copied across verbatim.  That keeps rewrites cheap (only the handful of
    blocks a roster edit touches get recompressed) and keeps the output
    byte-identical to the input when nothing changed.
    """
    if original is not None:
        magic, tag = original.magic, original.tag
    size = len(payload)
    nblocks = (size + BLOCK_SIZE - 1) // BLOCK_SIZE
    header_len = TABLE_OFFSET + 4 * (nblocks + 1)

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
        # The decompressor stops at the end-of-stream token, so the filler is
        # never looked at.  A verbatim block must NOT be padded: the engine uses
        # its extent as the DMA length into a BLOCK_SIZE buffer, and anything
        # longer would run off the end of it.
        encoded += b"\0" * (-len(encoded) % ALIGNMENT)
        if len(encoded) >= len(chunk):
            stored.append((chunk, 0))  # verbatim is smaller, the format allows it
            continue
        if original is not None and i < len(original.blocks):
            # Pad back out to the length this block had before, so every block
            # behind it keeps its old offset and the file keeps its old size.
            # That turns a one-photo edit into a few kilobytes of changed ROM
            # instead of a rewrite of everything downstream.
            was = len(original.blocks[i])
            if len(encoded) < was <= BLOCK_SIZE:
                encoded += b"\0" * (was - len(encoded))
        stored.append((encoded, COMPRESSED_FLAG))

    out = bytearray(header_len)
    table: list[int] = []
    for blob, flag in stored:
        table.append((len(out) << 4) | flag)
        out += blob
    table.append(len(out) << 4)

    struct.pack_into(">I", out, 0, magic)
    out[4:8] = tag
    struct.pack_into(">I", out, 8, size)
    struct.pack_into(">%dI" % (nblocks + 1), out, TABLE_OFFSET, *table)
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
    # The walker at 0x80088A44 rounds the declared size up to four and stops at
    # ``size + 8``; mirror that instead of trusting the buffer length.
    declared = struct.unpack_from(">I", payload, 4)[0]
    end = min(len(payload), _round_up(declared) + 8)
    form = payload[8:12]
    chunks: list[Chunk] = []
    off = 12
    while off + 8 <= end:
        ident = payload[off:off + 4]
        size = struct.unpack_from(">I", payload, off + 4)[0]
        if off + 8 + size > end:
            break
        chunks.append(Chunk(ident, payload[off + 8:off + 8 + size]))
        off += 8 + _round_up(size)
    return ChunkFile(magic=magic, form=form, chunks=chunks)


def build_chunks(cf: ChunkFile) -> bytes:
    body = bytearray(cf.form)
    for c in cf.chunks:
        # Pad the payload to a four-byte multiple and count the padding in the
        # size, exactly as the shipped files do, so every following chunk stays
        # where the walker's ``(size + 3) & ~3`` step expects it.
        data = c.data + b"\0" * (-len(c.data) % ALIGNMENT)
        body += c.ident
        body += struct.pack(">I", len(data))
        body += data
    return cf.magic + struct.pack(">I", len(body)) + bytes(body)


def check(data: bytes) -> list[str]:
    """Report anything about a packed container the engine would choke on.

    Written against the invariants every shipped file obeys, so it catches a
    rewrite that decodes perfectly here but would not load on the console.
    """
    problems: list[str] = []
    if len(data) < TABLE_OFFSET:
        return ["container is only %d bytes long" % len(data)]
    magic = struct.unpack_from(">I", data, 0)[0]
    if magic != ASSET_MAGIC:
        return ["bad asset magic 0x%08X, expected 0x%08X" % (magic, ASSET_MAGIC)]
    if data[4:8] not in TAGS:
        return ["bad container tag %r" % (data[4:8],)]
    size = struct.unpack_from(">I", data, 8)[0]
    nblocks = (size + BLOCK_SIZE - 1) // BLOCK_SIZE
    header_len = TABLE_OFFSET + 4 * (nblocks + 1)
    if header_len > BLOCK_SIZE:
        problems.append(
            "the block table needs %d bytes; the engine only reads the first "
            "%d bytes of the file to find it" % (header_len, BLOCK_SIZE))
    if len(data) < header_len:
        return problems + ["the block table runs past the end of the file"]
    table = struct.unpack_from(">%dI" % (nblocks + 1), data, TABLE_OFFSET)
    starts = [t >> 4 for t in table]

    for i, start in enumerate(starts):
        if start % ALIGNMENT:
            problems.append("block %d starts at 0x%X, not %d-byte aligned"
                            % (i, start, ALIGNMENT))
    for i in range(nblocks):
        length = starts[i + 1] - starts[i]
        if length <= 0:
            problems.append("block %d has length %d" % (i, length))
        elif length > BLOCK_SIZE:
            problems.append(
                "block %d is %d bytes long; the engine would DMA it into a "
                "%d-byte buffer" % (i, length, BLOCK_SIZE))
    if starts[0] != header_len:
        problems.append("first block starts at 0x%X, expected 0x%X"
                        % (starts[0], header_len))
    if starts[-1] != len(data):
        problems.append("the last block ends at 0x%X but the file is 0x%X bytes"
                        % (starts[-1], len(data)))

    try:
        payload = unpack(data).payload
    except (ContainerError, lzss.CorruptStream) as exc:
        problems.append("payload does not decode: %s" % exc)
        return problems
    if len(payload) != size:
        problems.append("payload decoded to %d bytes, header says %d"
                        % (len(payload), size))
    problems.extend(check_chunks(payload))
    return problems


def check_chunks(payload: bytes) -> list[str]:
    """Validate an ``AIFF`` chunk list the way the engine's walker reads it."""
    problems: list[str] = []
    if payload[:4] not in (b"AIFF", b"aiff"):
        return ["payload magic is %r, the walker only accepts 'AIFF'" % (payload[:4],)]
    declared = struct.unpack_from(">I", payload, 4)[0]
    end = _round_up(declared) + 8
    if end > len(payload):
        problems.append("the chunk list claims %d bytes but only %d are present"
                        % (end, len(payload)))
        end = len(payload)
    off = 12
    while off + 8 <= end:
        ident = payload[off:off + 4]
        size = struct.unpack_from(">I", payload, off + 4)[0]
        if size % ALIGNMENT:
            problems.append("chunk %r is %d bytes, not a multiple of %d"
                            % (ident, size, ALIGNMENT))
        if off + 8 + size > end:
            problems.append("chunk %r runs past the end of the payload" % (ident,))
            break
        off += 8 + _round_up(size)
    if off != end:
        problems.append("the chunk list ends at 0x%X, the header says 0x%X"
                        % (off, end))
    return problems
