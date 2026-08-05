"""Reading ordinary image files and turning them into game photographs.

The game stores a roster photo as a 64x56 block of 8-bit indices into one of
the ten shared ``SPAL`` palettes, LZSS-packed inside a Left Field asset blob.
To import a picture we have to decode it, crop and scale it to 64x56, choose
the palette bank that suits it best and map every pixel onto that bank.

PNG and BMP are decoded here with nothing but ``zlib``, so the editor keeps
its "standard library only" promise.  If Pillow happens to be installed it is
used for anything else (JPEG, WEBP, ...); without it those formats produce a
clear message asking for a PNG.
"""

from __future__ import annotations

import struct
import zlib

PHOTO_WIDTH = 64
PHOTO_HEIGHT = 56

#: Formats decoded without any third-party help.
NATIVE_SUFFIXES = (".png", ".bmp")


class ImageError(ValueError):
    pass


class Image:
    """A decoded picture: 8-bit RGB, row major, no padding."""

    __slots__ = ("width", "height", "rgb")

    def __init__(self, width: int, height: int, rgb: bytes) -> None:
        if len(rgb) != width * height * 3:
            raise ImageError("pixel buffer is %d bytes, expected %d"
                             % (len(rgb), width * height * 3))
        self.width = width
        self.height = height
        self.rgb = rgb

    def pixel(self, x: int, y: int) -> tuple[int, int, int]:
        i = (y * self.width + x) * 3
        return self.rgb[i], self.rgb[i + 1], self.rgb[i + 2]


# ---------------------------------------------------------------------------
# PNG
# ---------------------------------------------------------------------------

_PNG_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
# Adam7: (x offset, y offset, x step, y step) for each of the seven passes.
_ADAM7 = ((0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4),
          (0, 2, 2, 2), (1, 0, 2, 2), (0, 1, 1, 2))


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def _unfilter(raw: bytes, width: int, height: int, bpp: int, stride: int) -> bytearray:
    """Undo the per-scanline PNG filters."""
    out = bytearray(height * stride)
    pos = 0
    prev = bytearray(stride)
    for y in range(height):
        ftype = raw[pos]
        pos += 1
        line = bytearray(raw[pos:pos + stride])
        pos += stride
        if ftype == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif ftype == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif ftype == 3:
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif ftype == 4:
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                upleft = prev[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + _paeth(left, prev[i], upleft)) & 0xFF
        elif ftype != 0:
            raise ImageError("unknown PNG row filter %d" % ftype)
        out[y * stride:(y + 1) * stride] = line
        prev = line
    return out


def _expand_scanlines(data: bytearray, width: int, height: int, depth: int,
                      channels: int, stride: int) -> list[list[int]]:
    """One list of channel samples per row, normalised to 8 bits per sample."""
    rows: list[list[int]] = []
    for y in range(height):
        line = data[y * stride:(y + 1) * stride]
        if depth == 8:
            samples = list(line)
        elif depth == 16:
            samples = [line[i] for i in range(0, len(line), 2)]  # keep the high byte
        else:
            samples = []
            per_byte = 8 // depth
            mask = (1 << depth) - 1
            need = width * channels
            for byte in line:
                for k in range(per_byte):
                    if len(samples) >= need:
                        break
                    samples.append((byte >> (8 - depth * (k + 1))) & mask)
        rows.append(samples)
    return rows


def decode_png(data: bytes) -> Image:
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ImageError("not a PNG file")
    pos = 8
    width = height = depth = colour = interlace = 0
    palette = b""
    idat = bytearray()
    while pos + 8 <= len(data):
        size = struct.unpack_from(">I", data, pos)[0]
        tag = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + size]
        pos += 12 + size
        if tag == b"IHDR":
            width, height, depth, colour, _comp, _filt, interlace = struct.unpack(
                ">IIBBBBB", body)
        elif tag == b"PLTE":
            palette = body
        elif tag == b"IDAT":
            idat += body
        elif tag == b"IEND":
            break
    if not width or not height:
        raise ImageError("PNG has no image header")
    if colour not in _PNG_CHANNELS:
        raise ImageError("unsupported PNG colour type %d" % colour)
    channels = _PNG_CHANNELS[colour]
    raw = zlib.decompress(bytes(idat))

    def rows_for(w: int, h: int, blob: bytes) -> list[list[int]]:
        bits = w * channels * depth
        stride = (bits + 7) // 8
        bpp = max(1, (channels * depth) // 8)
        return _expand_scanlines(_unfilter(blob, w, h, bpp, stride), w, h,
                                 depth, channels, stride)

    grid = [[0] * (width * channels) for _ in range(height)]
    if interlace == 0:
        for y, row in enumerate(rows_for(width, height, raw)):
            grid[y][:len(row)] = row
    elif interlace == 1:
        offset = 0
        for xo, yo, xs, ys in _ADAM7:
            pw = (width - xo + xs - 1) // xs
            ph = (height - yo + ys - 1) // ys
            if pw <= 0 or ph <= 0:
                continue
            bits = pw * channels * depth
            stride = (bits + 7) // 8
            chunk = raw[offset:offset + ph * (stride + 1)]
            offset += ph * (stride + 1)
            for py, row in enumerate(rows_for(pw, ph, chunk)):
                y = yo + py * ys
                for px in range(pw):
                    x = xo + px * xs
                    for c in range(channels):
                        grid[y][x * channels + c] = row[px * channels + c]
    else:
        raise ImageError("unknown PNG interlace method %d" % interlace)

    scale = 255 // ((1 << depth) - 1) if depth < 8 else 1
    out = bytearray(width * height * 3)
    for y in range(height):
        row = grid[y]
        base = y * width * 3
        for x in range(width):
            s = x * channels
            if colour == 3:
                idx = row[s] * 3
                if idx + 3 > len(palette):
                    raise ImageError("PNG palette index out of range")
                r, g, b = palette[idx], palette[idx + 1], palette[idx + 2]
            elif colour in (0, 4):
                r = g = b = row[s] * scale
            else:
                r, g, b = row[s] * scale, row[s + 1] * scale, row[s + 2] * scale
            out[base + x * 3] = r
            out[base + x * 3 + 1] = g
            out[base + x * 3 + 2] = b
    return Image(width, height, bytes(out))


# ---------------------------------------------------------------------------
# BMP
# ---------------------------------------------------------------------------

def decode_bmp(data: bytes) -> Image:
    if data[:2] != b"BM":
        raise ImageError("not a BMP file")
    pixel_offset = struct.unpack_from("<I", data, 10)[0]
    header = struct.unpack_from("<I", data, 14)[0]
    if header < 12:
        raise ImageError("unsupported BMP header")
    width, height = struct.unpack_from("<ii", data, 18)
    planes, bits = struct.unpack_from("<HH", data, 26)
    compression = struct.unpack_from("<I", data, 30)[0] if header >= 40 else 0
    if planes != 1 or compression != 0:
        raise ImageError("only uncompressed BMP files are supported")
    if bits not in (24, 32):
        raise ImageError("only 24- and 32-bit BMP files are supported "
                         "(this one is %d-bit)" % bits)
    flip = height > 0
    height = abs(height)
    per_pixel = bits // 8
    stride = ((width * bits + 31) // 32) * 4
    out = bytearray(width * height * 3)
    for y in range(height):
        src_y = height - 1 - y if flip else y
        base = pixel_offset + src_y * stride
        dst = y * width * 3
        for x in range(width):
            p = base + x * per_pixel
            out[dst + x * 3] = data[p + 2]
            out[dst + x * 3 + 1] = data[p + 1]
            out[dst + x * 3 + 2] = data[p]
    return Image(width, height, bytes(out))


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------

def _decode_with_pillow(path: str) -> Image | None:
    try:
        from PIL import Image as PilImage
    except ImportError:
        return None
    with PilImage.open(path) as im:
        im = im.convert("RGB")
        return Image(im.width, im.height, im.tobytes())


def load_image(path: str) -> Image:
    """Decode an image file into RGB pixels."""
    with open(path, "rb") as fh:
        head = fh.read(8)
        fh.seek(0)
        data = fh.read()
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return decode_png(data)
    if head[:2] == b"BM":
        return decode_bmp(data)
    got = _decode_with_pillow(path)
    if got is not None:
        return got
    kind = "JPEG" if head[:2] == b"\xff\xd8" else "this"
    raise ImageError(
        "%s image format cannot be read without Pillow.\n"
        "Either save the picture as a PNG and try again, or install Pillow "
        "with:  pip install pillow" % kind)


# ---------------------------------------------------------------------------
# fitting
# ---------------------------------------------------------------------------

def fit(image: Image, width: int = PHOTO_WIDTH, height: int = PHOTO_HEIGHT,
        mode: str = "crop", samples: int = 6) -> bytes:
    """Scale to ``width`` x ``height``, returning RGB bytes.

    ``crop`` (the default) trims the long edge so faces keep their proportions;
    ``stretch`` squashes the whole picture into the frame.  Each output pixel
    averages a grid of source samples, which keeps big photographs from
    aliasing without walking every one of their pixels.
    """
    if mode not in ("crop", "stretch"):
        raise ImageError("unknown fit mode %r" % mode)
    src_w, src_h = image.width, image.height
    x0, y0, box_w, box_h = 0, 0, src_w, src_h
    if mode == "crop":
        want = width / height
        have = src_w / src_h
        if have > want:                       # too wide: trim the sides
            box_w = max(1, int(round(src_h * want)))
            x0 = (src_w - box_w) // 2
        elif have < want:                     # too tall: trim top and bottom
            box_h = max(1, int(round(src_w / want)))
            y0 = (src_h - box_h) // 3         # bias upward, faces sit high
    rgb = image.rgb
    out = bytearray(width * height * 3)
    for ty in range(height):
        sy0 = y0 + (ty * box_h) // height
        sy1 = max(sy0 + 1, y0 + ((ty + 1) * box_h) // height)
        ys = _sample_points(sy0, sy1, samples)
        for tx in range(width):
            sx0 = x0 + (tx * box_w) // width
            sx1 = max(sx0 + 1, x0 + ((tx + 1) * box_w) // width)
            xs = _sample_points(sx0, sx1, samples)
            r = g = b = 0
            for sy in ys:
                row = sy * src_w
                for sx in xs:
                    i = (row + sx) * 3
                    r += rgb[i]
                    g += rgb[i + 1]
                    b += rgb[i + 2]
            n = len(ys) * len(xs)
            i = (ty * width + tx) * 3
            out[i] = r // n
            out[i + 1] = g // n
            out[i + 2] = b // n
    return bytes(out)


def _sample_points(lo: int, hi: int, limit: int) -> list[int]:
    span = hi - lo
    if span <= limit:
        return list(range(lo, hi))
    return [lo + (k * span) // limit for k in range(limit)]


# ---------------------------------------------------------------------------
# quantising
# ---------------------------------------------------------------------------

def choose_palette(rgb: bytes, palettes: list[list[tuple[int, int, int]]],
                   stride: int = 1) -> int:
    """Pick the palette bank that reproduces these pixels most closely.

    Every pixel is scored against every bank; a cache keyed on the quantised
    colour keeps that to a few thousand nearest-colour searches in practice.
    """
    best_bank, best_score = 0, None
    for bank, palette in enumerate(palettes):
        score = 0
        cache: dict[int, int] = {}
        for i in range(0, len(rgb) - 2, 3 * stride):
            key = (rgb[i] >> 2 << 12) | (rgb[i + 1] >> 2 << 6) | (rgb[i + 2] >> 2)
            hit = cache.get(key)
            if hit is None:
                hit = _nearest_cost(rgb[i], rgb[i + 1], rgb[i + 2], palette)
                cache[key] = hit
            score += hit
        if best_score is None or score < best_score:
            best_bank, best_score = bank, score
    return best_bank


def _nearest_cost(r: int, g: int, b: int, palette) -> int:
    best = 1 << 30
    for pr, pg, pb in palette:
        d = (pr - r) ** 2 + (pg - g) ** 2 + (pb - b) ** 2
        if d < best:
            best = d
    return best


#: Dithering is off by default: these palettes were built from photographs, so
#: a portrait usually lands on them cleanly, and diffusing the error only adds
#: visible speckle across flat areas like a studio backdrop.  Full-strength
#: Floyd-Steinberg checkerboards badly, so even when asked for, only part of
#: the error is carried forward.
DITHER_STRENGTH = 0.6


def quantise(rgb: bytes, palette: list[tuple[int, int, int]],
             dither: bool = False, strength: float = DITHER_STRENGTH) -> bytes:
    """Map RGB pixels onto palette indices, optionally dithering the error."""
    width, height = PHOTO_WIDTH, PHOTO_HEIGHT
    work = [float(v) for v in rgb] if dither else None
    out = bytearray(width * height)
    cache: dict[int, int] = {}
    for y in range(height):
        for x in range(width):
            i = (y * width + x) * 3
            if dither:
                r = min(255, max(0, int(work[i])))
                g = min(255, max(0, int(work[i + 1])))
                b = min(255, max(0, int(work[i + 2])))
            else:
                r, g, b = rgb[i], rgb[i + 1], rgb[i + 2]
            key = (r >> 1 << 14) | (g >> 1 << 7) | (b >> 1)
            index = cache.get(key)
            if index is None:
                index = _nearest_index(r, g, b, palette)
                cache[key] = index
            out[y * width + x] = index
            if dither:
                pr, pg, pb = palette[index]
                _spread(work, i, width, height, x, y,
                        (r - pr) * strength, (g - pg) * strength,
                        (b - pb) * strength)
    return bytes(out)


def _nearest_index(r: int, g: int, b: int, palette) -> int:
    best_index, best = 0, 1 << 30
    for index, (pr, pg, pb) in enumerate(palette):
        d = (pr - r) ** 2 + (pg - g) ** 2 + (pb - b) ** 2
        if d < best:
            best_index, best = index, d
            if not d:
                break
    return best_index


def _spread(work, i: int, width: int, height: int, x: int, y: int,
            er: float, eg: float, eb: float) -> None:
    """Floyd-Steinberg error diffusion."""
    for dx, dy, weight in ((1, 0, 7 / 16), (-1, 1, 3 / 16),
                           (0, 1, 5 / 16), (1, 1, 1 / 16)):
        nx, ny = x + dx, y + dy
        if 0 <= nx < width and 0 <= ny < height:
            j = (ny * width + nx) * 3
            work[j] += er * weight
            work[j + 1] += eg * weight
            work[j + 2] += eb * weight
