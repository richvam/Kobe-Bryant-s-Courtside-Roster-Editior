"""Player likenesses held in ``TEAMDATA.IFF``.

Chunks
------
``PTEX``  u32 count, u32 offset table of ``count + 1`` entries, then fixed
          2112-byte face textures used on the 3D player model.
``PIMG``  same shape, but variable-length compressed roster photographs.
``SPAL``  ten shared 256-colour RGBA5551 palettes; each photo names the bank
          it was quantised against in its first decoded byte.

Both tables are indexed by player id, so pointing one player's entry at
another player's blob is all it takes to give him that face.
"""

from __future__ import annotations

import struct

from . import iff, lzss
from .offsets import OffsetTable, TableError

FACE_SIZE = 0x840          # bytes per PTEX entry
PHOTO_WIDTH = 64
PHOTO_HEIGHT = 56
PHOTO_PIXELS = PHOTO_WIDTH * PHOTO_HEIGHT

#: 0x735764xx marks a Left Field asset blob; the tag says how it is stored.
ASSET_MAGIC = 0x735764


class AppearanceError(TableError):
    """Raised for anything wrong with a face, photo or palette."""


def decode_asset(blob: bytes) -> bytes:
    """Unwrap a ``0x735764xx`` asset blob (``RAW`` payloads are LZSS-packed)."""
    if len(blob) < 12:
        raise AppearanceError("asset blob too small (%d bytes)" % len(blob))
    if (struct.unpack_from(">I", blob, 0)[0] >> 8) != ASSET_MAGIC:
        raise AppearanceError("not a Left Field asset blob")
    size = struct.unpack_from(">I", blob, 8)[0]
    return lzss.decompress(blob[12:], size)[:size]


class AppearanceDatabase:
    """Parsed ``TEAMDATA.IFF``."""

    def __init__(self, container: iff.Container) -> None:
        self.container = container
        self.chunkfile = iff.parse_chunks(container.payload)
        self.faces = OffsetTable(self.chunkfile.get(b"PTEX"))
        self.photos = OffsetTable(self.chunkfile.get(b"PIMG"))
        self.palettes = self.chunkfile.get(b"SPAL")
        self._photo_cache: dict[int, bytes | None] = {}

    # -- reassignment ---------------------------------------------------
    def copy_face(self, dst_player_id: int, src_player_id: int) -> None:
        self.faces.repoint(dst_player_id, src_player_id)

    def copy_photo(self, dst_player_id: int, src_player_id: int) -> None:
        self.photos.repoint(dst_player_id, src_player_id)

    def copy_likeness(self, dst_player_id: int, src_player_id: int) -> None:
        """Give ``dst`` both the face texture and the roster photo of ``src``."""
        self.copy_face(dst_player_id, src_player_id)
        self.copy_photo(dst_player_id, src_player_id)

    def swap_likeness(self, a: int, b: int) -> None:
        faces = self.faces.blobs()
        photos = self.photos.blobs()
        faces[a], faces[b] = faces[b], faces[a]
        photos[a], photos[b] = photos[b], photos[a]
        self.faces.rebuild(faces)
        self.photos.rebuild(photos)

    # -- rendering ------------------------------------------------------
    def photo(self, player_id: int) -> tuple[int, bytes] | None:
        """``(palette bank, 64x56 palette indices)`` for a roster photo.

        The decoded blob leads with the index of the 256-colour ``SPAL`` bank
        the picture was quantised against, followed by the pixels.
        """
        if player_id in self._photo_cache:
            return self._photo_cache[player_id]
        result: tuple[int, bytes] | None = None
        try:
            blob = self.photos.entry(player_id)
            if len(blob) >= 12:
                pixels = decode_asset(blob)
                if len(pixels) >= PHOTO_PIXELS + 1:
                    result = (pixels[0], pixels[1:1 + PHOTO_PIXELS])
        except (AppearanceError, lzss.CorruptStream, IndexError):
            result = None
        self._photo_cache[player_id] = result
        return result

    def photo_indices(self, player_id: int) -> bytes | None:
        got = self.photo(player_id)
        return None if got is None else got[1]

    def palette_rgb(self, bank: int = 0) -> list[tuple[int, int, int]]:
        """One 256-colour bank expanded to 8-bit RGB (source is RGBA5551)."""
        out = []
        base = bank * 512
        for i in range(256):
            off = base + i * 2
            if off + 2 > len(self.palettes):
                out.append((0, 0, 0))
                continue
            v = struct.unpack_from(">H", self.palettes, off)[0]
            out.append(((((v >> 11) & 31) * 255) // 31,
                        (((v >> 6) & 31) * 255) // 31,
                        (((v >> 1) & 31) * 255) // 31))
        return out

    def photo_rgb(self, player_id: int, bank: int | None = None) -> bytes | None:
        got = self.photo(player_id)
        if got is None:
            return None
        own_bank, idx = got
        pal = self.palette_rgb(own_bank if bank is None else bank)
        out = bytearray(PHOTO_PIXELS * 3)
        for i, v in enumerate(idx):
            r, g, b = pal[v]
            out[i * 3] = r
            out[i * 3 + 1] = g
            out[i * 3 + 2] = b
        return bytes(out)

    def photo_png(self, player_id: int, bank: int | None = None, scale: int = 1) -> bytes | None:
        """A roster photo as a PNG, ready to drop into an ``<img>``."""
        rgb = self.photo_rgb(player_id, bank)
        if rgb is None:
            return None
        return _png(PHOTO_WIDTH, PHOTO_HEIGHT, rgb, scale)

    # -- importing ------------------------------------------------------
    def set_photo(self, player_id: int, bank: int, indices: bytes) -> None:
        """Replace a roster photo with 64x56 indices into ``SPAL`` bank."""
        from . import lzss

        if len(indices) != PHOTO_PIXELS:
            raise AppearanceError("expected %d pixels, got %d"
                                  % (PHOTO_PIXELS, len(indices)))
        if not 0 <= bank < len(self.palettes) // 512:
            raise AppearanceError("palette bank %d does not exist" % bank)
        payload = bytes([bank]) + indices
        # Keep whatever flavour byte the original blob carried.
        magic = (ASSET_MAGIC << 8) | 0x80
        try:
            existing = self.photos.entry(player_id)
            if len(existing) >= 4:
                magic = struct.unpack_from(">I", existing, 0)[0]
        except AppearanceError:
            pass
        blob = (struct.pack(">I", magic) + b"RAW\0"
                + struct.pack(">I", len(payload)) + lzss.compress(payload))
        blobs = self.photos.blobs()
        blobs[player_id] = blob
        self.photos.rebuild(blobs)
        self._photo_cache.pop(player_id, None)

    def import_photo(self, player_id: int, path: str, mode: str = "crop",
                     dither: bool = False) -> int:
        """Put an image file on a player's roster card. Returns the bank used."""
        from . import images

        picture = images.load_image(path)
        rgb = images.fit(picture, PHOTO_WIDTH, PHOTO_HEIGHT, mode=mode)
        banks = [self.palette_rgb(i) for i in range(len(self.palettes) // 512)]
        bank = images.choose_palette(rgb, banks)
        self.set_photo(player_id, bank, images.quantise(rgb, banks[bank], dither))
        return bank

    # -- serialisation --------------------------------------------------
    def to_file(self) -> bytes:
        self.chunkfile.set(b"PTEX", self.faces.data)
        self.chunkfile.set(b"PIMG", self.photos.data)
        payload = iff.build_chunks(self.chunkfile)
        return iff.pack(payload, magic=self.container.magic, original=self.container)


def load(blob: bytes) -> AppearanceDatabase:
    return AppearanceDatabase(iff.unpack(blob))


def _png(width: int, height: int, rgb: bytes, scale: int = 1) -> bytes:
    """Minimal truecolour PNG encoder (stdlib only)."""
    import zlib

    scale = max(1, int(scale))
    rows = bytearray()
    for y in range(height):
        row = bytearray()
        base = y * width * 3
        for x in range(width):
            px = rgb[base + x * 3:base + x * 3 + 3]
            row += px * scale
        for _ in range(scale):
            rows += b"\0" + row

    def chunk(tag: bytes, payload: bytes) -> bytes:
        body = tag + payload
        return (struct.pack(">I", len(payload)) + body
                + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", width * scale, height * scale, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(bytes(rows), 6))
            + chunk(b"IEND", b""))
