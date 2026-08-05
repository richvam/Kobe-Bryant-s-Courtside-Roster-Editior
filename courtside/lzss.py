"""LZSS codec used by Left Field Productions' N64 engine.

Reverse engineered from the decompressor at RAM 0x800DE914 in
*Kobe Bryant's NBA Courtside* (NNBE, USA).

Stream layout
-------------
The stream interleaves whole bytes with a bit buffer.  The decoder keeps a
one-byte buffer primed with a sentinel (``byte | 0x100``); bits are consumed
from the least significant end, and a fresh byte is pulled from the stream the
moment the buffer runs dry.  Literals and "extra" bytes are read straight from
the stream, not from the bit buffer.

    0                       -> literal: copy the next stream byte
    1 0                     -> match, length 2
    1 1 <2 bits = n>        -> match, length n + 2          (n != 0)
    1 1 00 <4 bits = n>     -> match, length n + 5          (n != 0)
    1 1 00 0000 <byte = n>  -> match, length n + 0x14       (n != 0)
    1 1 00 0000 0x00        -> end of stream

Every match is followed by a two-bit distance class selecting one of
``DISTANCE_CLASSES``; the class supplies a bit width and a base, and the
decoded value is added to the base.  Multi-bit values are read most
significant bit first, and a class wider than 8 bits takes its top 8 bits from
a whole stream byte before reading the remainder from the bit buffer.
"""

from __future__ import annotations

# (bit width, base distance) selected by the two-bit distance class.
DISTANCE_CLASSES = ((5, 0x1), (7, 0x21), (9, 0xA1), (10, 0x2A1))

MAX_DISTANCE = 0x2A1 + (1 << 10) - 1  # 1696
MAX_LENGTH = 0xFF + 0x14  # 275
MIN_LENGTH = 2


class CorruptStream(ValueError):
    """Raised when a stream cannot be decoded."""


# --------------------------------------------------------------------------
# decoder
# --------------------------------------------------------------------------

class _BitReader:
    __slots__ = ("data", "pos", "buf")

    def __init__(self, data: bytes, pos: int = 0) -> None:
        self.data = data
        self.pos = pos
        self.buf = 1  # sentinel: buffer empty

    def bit(self) -> int:
        if self.buf == 1:
            self.buf = self.data[self.pos] | 0x100
            self.pos += 1
        b = self.buf & 1
        self.buf >>= 1
        return b

    def bits(self, n: int) -> int:
        v = 0
        for _ in range(n):
            v = (v << 1) | self.bit()
        return v

    def byte(self) -> int:
        b = self.data[self.pos]
        self.pos += 1
        return b


def decompress(src: bytes, limit: int | None = None) -> bytes:
    """Decode one LZSS stream.

    ``limit`` stops decoding once that many output bytes exist, which is what
    the game does for a fixed-size block; without it the stream runs to its
    end-of-stream token.
    """
    r = _BitReader(src)
    out = bytearray()
    try:
        while True:
            if r.bit() == 0:
                out.append(r.byte())
                if limit is not None and len(out) >= limit:
                    break
                continue
            if r.bit() == 0:
                length = 2
            else:
                v = r.bits(2)
                if v:
                    length = v + 2
                else:
                    v = r.bits(4)
                    if v:
                        length = v + 5
                    else:
                        v = r.byte()
                        if not v:
                            break
                        length = v + 0x14
            nbits, base = DISTANCE_CLASSES[r.bits(2)]
            v = 0
            if nbits >= 8:
                v = r.byte()
                nbits -= 8
            if nbits:
                v = (v << nbits) | r.bits(nbits)
            dist = base + v
            start = len(out) - dist
            if start < 0:
                raise CorruptStream(
                    "back-reference %d bytes before start of output" % -start
                )
            for i in range(length):
                out.append(out[start + i])
            if limit is not None and len(out) >= limit:
                break
    except IndexError as exc:  # ran off the end of the input
        raise CorruptStream("truncated LZSS stream") from exc
    return bytes(out)


# --------------------------------------------------------------------------
# encoder
# --------------------------------------------------------------------------

# distance -> (class index, bit width, value); built once.
_DIST_ENCODE: list[tuple[int, int, int] | None] = [None] * (MAX_DISTANCE + 1)
for _cls, (_nb, _base) in enumerate(DISTANCE_CLASSES):
    for _d in range(_base, min(MAX_DISTANCE, _base + (1 << _nb) - 1) + 1):
        if _DIST_ENCODE[_d] is None:
            _DIST_ENCODE[_d] = (_cls, _nb, _d - _base)

# Cost in bits of encoding a match at each distance (class selector included).
_DIST_COST = [
    None if e is None else 2 + e[1] for e in _DIST_ENCODE
]

LITERAL_COST = 1 + 8


def _length_cost(length: int) -> int:
    """Bits spent on the token that encodes ``length`` (flag bits included)."""
    if length == 2:
        return 2
    if length <= 5:
        return 4
    if length <= 20:
        return 8
    return 16


class _BitWriter:
    __slots__ = ("out", "ctrl_pos", "ctrl_left", "ctrl_val")

    def __init__(self) -> None:
        self.out = bytearray()
        self.ctrl_pos = -1
        self.ctrl_left = 0
        self.ctrl_val = 0

    def bit(self, b: int) -> None:
        if self.ctrl_left == 0:
            self.ctrl_pos = len(self.out)
            self.out.append(0)
            self.ctrl_val = 0
            self.ctrl_left = 8
        if b:
            self.ctrl_val |= 1 << (8 - self.ctrl_left)
            self.out[self.ctrl_pos] = self.ctrl_val
        self.ctrl_left -= 1

    def bits(self, value: int, n: int) -> None:
        for i in range(n - 1, -1, -1):
            self.bit((value >> i) & 1)

    def byte(self, b: int) -> None:
        self.out.append(b)


def _emit_end(w: _BitWriter) -> None:
    w.bit(1)
    w.bit(1)
    w.bits(0, 2)
    w.bits(0, 4)
    w.byte(0)


def compress(src: bytes, chain_limit: int = 96) -> bytes:
    """Encode ``src`` into a stream the game's decompressor accepts.

    Uses a shortest-path parse over the exact bit costs of the format, so
    output is typically a little smaller than the original game data.
    ``chain_limit`` bounds how many candidate positions are examined per
    offset - lower is faster, higher compresses slightly better.
    """
    n = len(src)
    w = _BitWriter()
    if n == 0:
        _emit_end(w)
        return bytes(w.out)

    # For every position, the cheapest (i.e. nearest) distance for each
    # achievable match length.
    matches: list[dict[int, int] | None] = [None] * n
    head: dict[int, int] = {}
    prev = [-1] * n
    for i in range(n - 1):
        key = (src[i] << 8) | src[i + 1]
        best: dict[int, int] = {}
        p = head.get(key, -1)
        low = i - MAX_DISTANCE
        seen = 0
        while p >= low and p >= 0 and seen < chain_limit:
            limit = min(MAX_LENGTH, n - i)
            length = 2
            while length < limit and src[p + length] == src[i + length]:
                length += 1
            dist = i - p
            for ll in range(2, length + 1):
                if ll not in best:
                    best[ll] = dist
            seen += 1
            p = prev[p]
        prev[i] = head.get(key, -1)
        head[key] = i
        if best:
            matches[i] = best

    # Backwards shortest path: cost[i] = cheapest bit cost to encode src[i:].
    inf = float("inf")
    cost = [inf] * (n + 1)
    cost[n] = 0
    choice: list[tuple[int, int] | None] = [None] * (n + 1)
    for i in range(n - 1, -1, -1):
        best_cost = LITERAL_COST + cost[i + 1]
        best_choice = None
        found = matches[i]
        if found:
            for length, dist in found.items():
                dcost = _DIST_COST[dist]
                if dcost is None:
                    continue
                c = _length_cost(length) + dcost + cost[i + length]
                if c < best_cost:
                    best_cost = c
                    best_choice = (length, dist)
        cost[i] = best_cost
        choice[i] = best_choice

    i = 0
    while i < n:
        pick = choice[i]
        if pick is None:
            w.bit(0)
            w.byte(src[i])
            i += 1
            continue
        length, dist = pick
        w.bit(1)
        if length == 2:
            w.bit(0)
        else:
            w.bit(1)
            if length <= 5:
                w.bits(length - 2, 2)
            elif length <= 20:
                w.bits(0, 2)
                w.bits(length - 5, 4)
            else:
                w.bits(0, 2)
                w.bits(0, 4)
                w.byte(length - 0x14)
        cls, nbits, value = _DIST_ENCODE[dist]
        w.bits(cls, 2)
        if nbits >= 8:
            w.byte(value >> (nbits - 8))
            if nbits > 8:
                w.bits(value & ((1 << (nbits - 8)) - 1), nbits - 8)
        else:
            w.bits(value, nbits)
        i += length

    _emit_end(w)
    return bytes(w.out)
