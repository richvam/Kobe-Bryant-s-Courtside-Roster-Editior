"""N64 ROM access: header, CRC and the game's flat file system."""

from __future__ import annotations

import struct
from dataclasses import dataclass

MASK = 0xFFFFFFFF

Z64_MAGIC = b"\x80\x37\x12\x40"
N64_MAGIC = b"\x40\x12\x37\x80"  # little-endian dump
V64_MAGIC = b"\x37\x80\x40\x12"  # byte-swapped dump

CRC_START = 0x1000
CRC_LENGTH = 0x100000

CIC_SEEDS = {
    6101: 0xF8CA4DDC,
    6102: 0xF8CA4DDC,
    6103: 0xA3886759,
    6105: 0xDF26F436,
    6106: 0x1FEA617A,
}

#: Entry stride in the game's file table.
_ENTRY_SIZE = 0x20
_NAME_LEN = 0x10


class RomError(ValueError):
    pass


# --------------------------------------------------------------------------
# byte order
# --------------------------------------------------------------------------

def to_big_endian(data: bytes) -> bytes:
    """Normalise a .z64/.n64/.v64 dump to big-endian (.z64) order."""
    head = data[:4]
    if head == Z64_MAGIC:
        return data
    if head == V64_MAGIC:
        out = bytearray(data)
        out[0::2], out[1::2] = data[1::2], data[0::2]
        return bytes(out)
    if head == N64_MAGIC:
        out = bytearray(len(data))
        out[0::4] = data[3::4]
        out[1::4] = data[2::4]
        out[2::4] = data[1::4]
        out[3::4] = data[0::4]
        return bytes(out)
    raise RomError("unrecognised N64 ROM magic %r" % (head,))


# --------------------------------------------------------------------------
# CRC
# --------------------------------------------------------------------------

def _rol(value: int, bits: int) -> int:
    bits &= 31
    if not bits:
        return value
    return ((value << bits) | (value >> (32 - bits))) & MASK


def calc_crc(data: bytes, cic: int = 6103) -> tuple[int, int]:
    """The standard bootcode CRC pair over 1 MiB starting at 0x1000."""
    try:
        seed = CIC_SEEDS[cic]
    except KeyError:
        raise RomError("unsupported CIC %r" % (cic,)) from None
    t1 = t2 = t3 = t4 = t5 = t6 = seed
    unpack = struct.unpack_from
    for i in range(CRC_START, CRC_START + CRC_LENGTH, 4):
        d = unpack(">I", data, i)[0]
        if ((t6 + d) & MASK) < t6:
            t4 = (t4 + 1) & MASK
        t6 = (t6 + d) & MASK
        t3 ^= d
        r = _rol(d, d & 0x1F)
        t5 = (t5 + r) & MASK
        if t2 > d:
            t2 ^= r
        else:
            t2 ^= t6 ^ d
        if cic == 6105:
            t1 = (t1 + (unpack(">I", data, 0x40 + 0x0710 + (i & 0xFF))[0] ^ d)) & MASK
        else:
            t1 = (t1 + (t5 ^ d)) & MASK
    if cic == 6103:
        return ((t6 ^ t4) + t3) & MASK, ((t5 ^ t2) + t1) & MASK
    if cic == 6106:
        return ((t6 * t4) + t3) & MASK, ((t5 * t2) + t1) & MASK
    return t6 ^ t4 ^ t3, t5 ^ t2 ^ t1


def detect_cic(data: bytes) -> int:
    """Work out which CIC seed reproduces the CRCs already in the header."""
    have = struct.unpack_from(">II", data, 0x10)
    for cic in (6102, 6103, 6105, 6106, 6101):
        if calc_crc(data, cic) == have:
            return cic
    return 6102


# --------------------------------------------------------------------------
# file system
# --------------------------------------------------------------------------

@dataclass
class FileEntry:
    name: str
    size: int
    offset: int  # relative to the data base
    index: int

    def rom_offset(self, base: int) -> int:
        return base + self.offset


def _align(value: int, to: int = 8) -> int:
    return (value + to - 1) & ~(to - 1)


def _plausible_name(raw: bytes) -> str | None:
    if raw[0] in (0, 0x20):
        return None
    end = raw.find(b"\0")
    if end <= 0:
        return None
    name = raw[:end]
    if any(not (0x20 < c < 0x7F) for c in name):
        return None
    if any(c != 0 for c in raw[end:]):
        return None
    return name.decode("ascii")


class RomFileSystem:
    """The table of packed data files the engine loads by name."""

    def __init__(self, data: bytearray, table_offset: int, count: int) -> None:
        self.data = data
        self.table_offset = table_offset
        self.count = count
        self.base = _align(table_offset + 4 + count * _ENTRY_SIZE)
        self.entries: list[FileEntry] = []
        for i in range(count):
            off = table_offset + 4 + i * _ENTRY_SIZE
            name = _plausible_name(bytes(data[off:off + _NAME_LEN])) or ""
            size, rel = struct.unpack_from(">II", data, off + 0x14)
            self.entries.append(FileEntry(name, size, rel, i))

    # -- lookup ---------------------------------------------------------
    def __contains__(self, name: str) -> bool:
        return any(e.name.upper() == name.upper() for e in self.entries)

    def entry(self, name: str) -> FileEntry:
        for e in self.entries:
            if e.name.upper() == name.upper():
                return e
        raise KeyError(name)

    def read(self, name: str) -> bytes:
        e = self.entry(name)
        start = e.rom_offset(self.base)
        return bytes(self.data[start:start + e.size])

    # -- mutation -------------------------------------------------------
    def _slack(self, entry: FileEntry) -> int:
        """Bytes available at this file's current location."""
        starts = sorted(e.offset for e in self.entries if e.offset > entry.offset)
        end = starts[0] if starts else (len(self.data) - self.base)
        return end - entry.offset

    def _end_of_data(self) -> int:
        return max(_align(e.offset + e.size) for e in self.entries)

    def write(self, name: str, blob: bytes) -> str:
        """Replace a file, relocating it if it no longer fits.

        Returns a short description of what happened, for reporting.
        """
        e = self.entry(name)
        slack = self._slack(e)
        if len(blob) <= slack:
            start = e.rom_offset(self.base)
            self.data[start:start + len(blob)] = blob
            # scrub the tail of the old file so no stale bytes linger
            if e.size > len(blob):
                self.data[start + len(blob):start + e.size] = b"\0" * (e.size - len(blob))
            e.size = len(blob)
            self._flush(e)
            return "in place at 0x%X (%d bytes, %d spare)" % (
                start, len(blob), slack - len(blob))

        new_off = self._end_of_data()
        start = self.base + new_off
        if start + len(blob) <= len(self.data):
            self.data[start:start + len(blob)] = blob
            e.offset = new_off
            e.size = len(blob)
            self._flush(e)
            return "relocated to 0x%X (%d bytes)" % (start, len(blob))
        # Nothing large enough is free at the end, so close the gaps instead:
        # every file keeps its order but is laid down back to back, and the
        # table is rewritten to match.
        return self._compact(name, blob)

    def _compact(self, name: str, blob: bytes) -> str:
        order = sorted(self.entries, key=lambda e: e.offset)
        payload = {e.name: (blob if e.name == name else self.read(e.name))
                   for e in order}          # read everything before overwriting

        cursor = 0
        for e in order:
            cursor = _align(cursor + len(payload[e.name]))
        if self.base + cursor > len(self.data):
            raise RomError(
                "no room in the ROM: the packed files would need %d bytes past 0x%X"
                % (self.base + cursor - len(self.data), len(self.data))
            )

        cursor = 0
        for e in order:
            data = payload[e.name]
            start = self.base + cursor
            self.data[start:start + len(data)] = data
            e.offset = cursor
            e.size = len(data)
            self._flush(e)
            cursor += len(data)
            padded = _align(cursor)
            if padded > cursor:              # keep the cartridge's own filler
                self.data[self.base + cursor:self.base + padded] = b"\x55" * (padded - cursor)
            cursor = padded
        spare = len(self.data) - (self.base + cursor)
        return ("relaid the packed files to make room (%d bytes, %d spare in the ROM)"
                % (len(blob), spare))

    def _flush(self, e: FileEntry) -> None:
        off = self.table_offset + 4 + e.index * _ENTRY_SIZE
        struct.pack_into(">II", self.data, off + 0x14, e.size, e.offset)


def find_filesystem(data: bytes | bytearray) -> RomFileSystem:
    """Locate the file table by validating the offset/size chain."""
    view = bytes(data)
    limit = min(len(view), 0x200000)
    for off in range(0x1000, limit - 0x40, 4):
        count = struct.unpack_from(">I", view, off)[0]
        if not (4 <= count <= 128):
            continue
        end = off + 4 + count * _ENTRY_SIZE
        if end > len(view):
            continue
        names = 0
        chain: list[tuple[int, int]] = []
        ok = True
        for i in range(count):
            e = off + 4 + i * _ENTRY_SIZE
            if _plausible_name(view[e:e + _NAME_LEN]) is None:
                ok = False
                break
            names += 1
            size, rel = struct.unpack_from(">II", view, e + 0x14)
            chain.append((rel, size))
        if not ok or names != count:
            continue
        base = _align(end)
        chain.sort()
        good = all(
            chain[i][0] + chain[i][1] <= chain[i + 1][0] + 8 for i in range(len(chain) - 1)
        ) and base + chain[-1][0] + chain[-1][1] <= len(view)
        if good and chain[0][0] == 0:
            return RomFileSystem(bytearray(data), off, count)
    raise RomError("could not locate the game's file table in this ROM")


class Rom:
    """A loaded ROM image plus its file system."""

    def __init__(self, data: bytes, path: str | None = None) -> None:
        self.path = path
        self.data = bytearray(to_big_endian(data))
        self.fs = find_filesystem(self.data)
        self.fs.data = self.data
        self.cic = detect_cic(self.data)

    @classmethod
    def load(cls, path: str) -> "Rom":
        with open(path, "rb") as fh:
            return cls(fh.read(), path=path)

    @property
    def title(self) -> str:
        return bytes(self.data[0x20:0x34]).rstrip(b" \0").decode("ascii", "replace")

    @property
    def cart_id(self) -> str:
        return bytes(self.data[0x3B:0x3F]).decode("ascii", "replace")

    def read(self, name: str) -> bytes:
        return self.fs.read(name)

    def write(self, name: str, blob: bytes) -> str:
        return self.fs.write(name, blob)

    def fix_crc(self) -> tuple[int, int]:
        crc = calc_crc(bytes(self.data), self.cic)
        struct.pack_into(">II", self.data, 0x10, *crc)
        return crc

    def save(self, path: str) -> tuple[int, int]:
        crc = self.fix_crc()
        with open(path, "wb") as fh:
            fh.write(bytes(self.data))
        return crc
