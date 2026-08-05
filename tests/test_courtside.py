"""Tests for the Courtside roster editor.

The codec and model tests run without a ROM.  The rest are skipped unless a
ROM is available - set ``COURTSIDE_ROM`` or drop one at ``tests/courtside.z64``.

    python -m pytest tests            # or
    python tests/test_courtside.py    # plain unittest runner
"""

from __future__ import annotations

import importlib.util
import os
import random
import sys
import tempfile
import unittest
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from courtside import iff, images, lzss, roster  # noqa: E402
from courtside.editor import EditorError, RosterEditor  # noqa: E402
from courtside.rom import calc_crc  # noqa: E402

ROM_PATH = os.environ.get("COURTSIDE_ROM") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "courtside.z64")
HAVE_ROM = os.path.exists(ROM_PATH)
LAUNCHER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "courtside_gui.py")
needs_rom = unittest.skipUnless(HAVE_ROM, "no ROM at %s" % ROM_PATH)


class TestLzss(unittest.TestCase):
    def round_trip(self, data: bytes) -> bytes:
        packed = lzss.compress(data)
        self.assertEqual(lzss.decompress(packed, len(data))[:len(data)], data)
        return packed

    def test_empty(self):
        self.assertEqual(lzss.decompress(lzss.compress(b"")), b"")

    def test_literals(self):
        self.round_trip(bytes(range(256)))

    def test_long_run(self):
        self.round_trip(b"A" * 5000)

    def test_repeating_text(self):
        text = (b"Kobe Bryant's NBA Courtside roster editor. " * 200)
        packed = self.round_trip(text)
        self.assertLess(len(packed), len(text) // 4)

    def test_distance_classes(self):
        # exercise every distance class, including the widest one
        for gap in (1, 30, 100, 400, 1600):
            block = bytes(random.Random(gap).randrange(256) for _ in range(64))
            self.round_trip(block + b"\x00" * gap + block)

    def test_random_data(self):
        rng = random.Random(1234)
        self.round_trip(bytes(rng.randrange(256) for _ in range(4096)))

    def test_decoder_rejects_bad_backreference(self):
        # 0x01 asks for a length-2 match one byte back, at output position 0
        with self.assertRaises(lzss.CorruptStream):
            lzss.decompress(bytes([0x01, 0x00]))

    def test_decoder_rejects_truncated_stream(self):
        with self.assertRaises(lzss.CorruptStream):
            lzss.decompress(b"\x00")  # literal flag with no literal byte behind it


class TestPositions(unittest.TestCase):
    def test_every_code_round_trips(self):
        for code in roster.POSITION_NAMES:
            self.assertEqual(roster.decode_position(roster.encode_position(code)), code)

    def test_known_encodings(self):
        self.assertEqual(roster.decode_position(1), "C")
        self.assertEqual(roster.decode_position(7), "PG")
        self.assertEqual(roster.decode_position(11), "SG")
        self.assertEqual(roster.decode_position(6), "PF")
        self.assertEqual(roster.decode_position(10), "SF")
        self.assertEqual(roster.decode_position(50), "FG")

    def test_unknown_position_raises(self):
        with self.assertRaises(ValueError):
            roster.encode_position("QB")


class TestRosterSlot(unittest.TestCase):
    def test_pack_unpack(self):
        for pid in (1, 41, 384, 404):
            for starter in range(6):
                slot = roster.RosterSlot(pid, starter)
                again = roster.RosterSlot.decode(slot.encode())
                self.assertEqual((again.player_id, again.starter), (pid, starter))


class TestStringPool(unittest.TestCase):
    def make(self) -> roster.StringPool:
        body = b"\x00Smith\x00Jones\x00"
        return roster.StringPool(b"\x00\x00\x00\x02" + body)

    def test_reads_existing(self):
        pool = self.make()
        self.assertEqual(pool.get(5), "Smith")
        self.assertEqual(pool.get(11), "Jones")

    def test_intern_is_stable(self):
        pool = self.make()
        self.assertEqual(pool.intern("Smith"), 5)  # existing text is reused
        first = pool.intern("Bryant")
        self.assertEqual(pool.intern("Bryant"), first)
        self.assertEqual(pool.get(first), "Bryant")

    def test_existing_offsets_survive_appends(self):
        pool = self.make()
        pool.intern("Bryant")
        self.assertEqual(pool.get(5), "Smith")


class TestGuiModule(unittest.TestCase):
    """The GUI is only importable where Tkinter is installed."""

    def test_imports_and_exposes_main(self):
        if importlib.util.find_spec("tkinter") is None:
            self.skipTest("Tkinter is not installed in this interpreter")
        import tkinter

        from courtside import gui
        self.assertTrue(callable(gui.main))
        self.assertTrue(issubclass(gui.CourtsideGUI, tkinter.Tk))

    def test_launcher_script_is_runnable(self):
        self.assertTrue(os.path.exists(LAUNCHER))
        with open(LAUNCHER, encoding="utf-8") as fh:
            compile(fh.read(), LAUNCHER, "exec")


class TestLauncherFailures(unittest.TestCase):
    """A failed launch must stay readable instead of flashing past."""

    def load(self):
        spec = importlib.util.spec_from_file_location("courtside_launcher", LAUNCHER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # importing must not start the GUI
        return module

    def test_importing_the_launcher_does_not_launch(self):
        module = self.load()
        self.assertTrue(callable(module.run))
        self.assertTrue(callable(module.fail))

    def test_fail_writes_a_log_and_exits_nonzero(self):
        module = self.load()
        module.HERE = tempfile.mkdtemp()
        prompted = []
        module._show_dialog = lambda msg: False          # pretend Tk is unavailable
        module.input = lambda *a: prompted.append(True)  # pretend a console is attached
        with self.assertRaises(SystemExit) as caught:
            module.fail("something broke", "traceback here")
        self.assertEqual(caught.exception.code, 1)
        self.assertTrue(prompted, "a console launch must wait before closing")
        log = os.path.join(module.HERE, "courtside-error.log")
        self.assertTrue(os.path.exists(log))
        with open(log, encoding="utf-8") as fh:
            body = fh.read()
        self.assertIn("something broke", body)
        self.assertIn("traceback here", body)

    def test_fail_does_not_prompt_when_a_dialog_was_shown(self):
        module = self.load()
        module.HERE = tempfile.mkdtemp()
        prompted = []
        module._show_dialog = lambda msg: True           # a window carried the message
        module.input = lambda *a: prompted.append(True)
        with self.assertRaises(SystemExit):
            module.fail("something broke")
        self.assertFalse(prompted)

    def test_missing_tkinter_help_names_the_packages(self):
        module = self.load()
        for hint in ("python3-tk", "python3-tkinter", "python.org", "serve"):
            self.assertIn(hint, module.TK_HELP)


class TestImages(unittest.TestCase):
    """Decoding pictures with nothing but the standard library."""

    def rgb_png(self, width, height, rgb, **kw):
        from courtside.appearance import _png
        return _png(width, height, rgb, **kw)

    def test_png_round_trip(self):
        rgb = bytearray()
        for y in range(20):
            for x in range(30):
                rgb += bytes((x * 8 % 256, y * 12 % 256, (x * y) % 256))
        png = self.rgb_png(30, 20, bytes(rgb))
        got = images.decode_png(png)
        self.assertEqual((got.width, got.height), (30, 20))
        self.assertEqual(got.rgb, bytes(rgb))

    def test_png_greyscale_and_palette(self):
        # 8-bit greyscale, written by hand so the decoder is exercised directly
        raw = b"".join(b"\0" + bytes(range(0, 16)) for _ in range(4))
        png = _make_png(16, 4, 8, 0, zlib.compress(raw))
        got = images.decode_png(png)
        self.assertEqual(got.pixel(3, 0), (3, 3, 3))

        plte = bytes((255, 0, 0, 0, 255, 0, 0, 0, 255))
        raw = b"".join(b"\0" + bytes([0, 1, 2]) for _ in range(2))
        png = _make_png(3, 2, 8, 3, zlib.compress(raw), plte=plte)
        got = images.decode_png(png)
        self.assertEqual([got.pixel(x, 0) for x in range(3)],
                         [(255, 0, 0), (0, 255, 0), (0, 0, 255)])

    def test_bmp_round_trip(self):
        rgb = bytes(range(24)) * 4          # 8x4 pixels
        bmp = _make_bmp(8, 4, rgb)
        got = images.decode_bmp(bmp)
        self.assertEqual((got.width, got.height), (8, 4))
        self.assertEqual(got.rgb, rgb)

    def test_fit_scales_and_crops(self):
        src = images.Image(120, 40, bytes([200, 100, 50]) * (120 * 40))
        for mode in ("crop", "stretch"):
            out = images.fit(src, 64, 56, mode=mode)
            self.assertEqual(len(out), 64 * 56 * 3)
            self.assertEqual(out[:3], bytes([200, 100, 50]))

    def test_quantise_hits_exact_palette_colours(self):
        palette = [(0, 0, 0)] * 256
        palette[7] = (10, 20, 30)
        palette[9] = (200, 210, 220)
        rgb = (bytes([10, 20, 30]) * (64 * 28)) + (bytes([200, 210, 220]) * (64 * 28))
        out = images.quantise(rgb, palette, dither=False)
        self.assertEqual(set(out[:64 * 28]), {7})
        self.assertEqual(set(out[64 * 28:]), {9})

    def test_choose_palette_prefers_the_closer_bank(self):
        reds = [(255, 0, 0)] * 256
        blues = [(0, 0, 255)] * 256
        rgb = bytes([250, 5, 5]) * (64 * 56)
        self.assertEqual(images.choose_palette(rgb, [blues, reds]), 1)

    def test_unreadable_format_explains_itself(self):
        path = os.path.join(tempfile.mkdtemp(), "photo.jpg")
        with open(path, "wb") as fh:
            fh.write(b"\xff\xd8\xff\xe0" + b"\0" * 64)   # a JPEG header
        try:
            images.load_image(path)
        except images.ImageError as exc:
            if importlib.util.find_spec("PIL") is None:
                self.assertIn("PNG", str(exc))
                self.assertIn("pillow", str(exc).lower())
        else:
            self.assertIsNotNone(importlib.util.find_spec("PIL"))


def _make_png(width, height, depth, colour, idat, plte=None):
    import struct as _s

    def chunk(tag, body):
        return (_s.pack(">I", len(body)) + tag + body
                + _s.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF))

    out = b"\x89PNG\r\n\x1a\n" + chunk(
        b"IHDR", _s.pack(">IIBBBBB", width, height, depth, colour, 0, 0, 0))
    if plte:
        out += chunk(b"PLTE", plte)
    return out + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def _make_bmp(width, height, rgb):
    import struct as _s

    stride = ((width * 3 + 3) // 4) * 4
    rows = b""
    for y in range(height - 1, -1, -1):
        row = b""
        for x in range(width):
            i = (y * width + x) * 3
            row += bytes((rgb[i + 2], rgb[i + 1], rgb[i]))
        rows += row.ljust(stride, b"\0")
    return (b"BM" + _s.pack("<IHHI", 54 + len(rows), 0, 0, 54)
            + _s.pack("<IiiHHIIiiII", 40, width, height, 1, 24, 0,
                      len(rows), 2835, 2835, 0, 0) + rows)


@needs_rom
class TestPhotoImport(unittest.TestCase):
    def setUp(self):
        self.editor = RosterEditor.open(ROM_PATH)
        self.tmp = tempfile.mkdtemp()

    def test_export_then_reimport_is_lossless(self):
        kobe = self.editor.one("Kobe Bryant")
        before = self.editor.appearance.photo_rgb(kobe.player_id)
        path = os.path.join(self.tmp, "kobe.png")
        self.editor.export_photo("Kobe Bryant", path, scale=1)

        target = self.editor.one("Sean Rooks")
        self.editor.import_photo("Sean Rooks", path)
        after = self.editor.appearance.photo_rgb(target.player_id)
        self.assertEqual(after, before)

    def test_imported_photo_survives_a_save(self):
        path = os.path.join(self.tmp, "kobe.png")
        self.editor.export_photo("Kobe Bryant", path, scale=1)
        self.editor.import_photo("Zan Tabak", path)
        expected = self.editor.appearance.photo_rgb(self.editor.one("Zan Tabak").player_id)

        rom = os.path.join(self.tmp, "out.z64")
        self.editor.save(rom)
        again = RosterEditor.open(rom)
        got = again.appearance.photo_rgb(again.one("Zan Tabak").player_id)
        self.assertEqual(got, expected)

    def test_import_accepts_an_odd_size_and_aspect(self):
        wide = images.Image(200, 50, bytes([180, 60, 30]) * (200 * 50))
        from courtside.appearance import _png
        path = os.path.join(self.tmp, "wide.png")
        with open(path, "wb") as fh:
            fh.write(_png(200, 50, wide.rgb))
        player = self.editor.one("Zan Tabak")
        for mode in ("crop", "stretch"):
            bank = self.editor.appearance.import_photo(player.player_id, path, mode=mode)
            self.assertIn(bank, range(10))
            self.assertEqual(len(self.editor.appearance.photo_indices(player.player_id)),
                             64 * 56)

    def test_export_all_writes_one_file_per_photo(self):
        out = os.path.join(self.tmp, "shots")
        lakers = [p for p in self.editor.db.players if p.team == 13]
        written = self.editor.export_all_photos(out, scale=1, players=lakers)
        self.assertEqual(len(written), len(lakers))
        for path in written:
            self.assertTrue(path.endswith(".png"))
            self.assertGreater(os.path.getsize(path), 100)


@needs_rom
class TestAlignment(unittest.TestCase):
    """Everything the engine reads has to stay on four-byte boundaries.

    It DMAs each block straight out of the ROM and reads the decoded payload
    with 32-bit loads, so a rewrite that decodes perfectly here can still stop
    the game loading.  Every shipped block, chunk and table entry obeys this.
    """

    def setUp(self):
        self.editor = RosterEditor.open(ROM_PATH)
        self.tmp = tempfile.mkdtemp()

    def block_starts(self, blob):
        import struct as _s
        size = _s.unpack_from(">I", blob, 4)[0]
        n = (size + 0x1FFF) // 0x2000
        return [(t >> 4) - 4 for t in _s.unpack_from(">%dI" % (n + 1), blob, 8)]

    def assert_sound(self, blob, what):
        self.assertEqual(iff.check(blob), [], what)
        for start in self.block_starts(blob):
            self.assertEqual(start % 4, 0, "%s: block at 0x%X" % (what, start))

    def test_shipped_files_pass(self):
        for name in ("TEAMINFO.IFF", "TEAMDATA.IFF"):
            self.assert_sound(self.editor.rom.read(name), name)

    def test_renaming_a_player_keeps_blocks_aligned(self):
        p = self.editor.one("Sean Rooks")
        p.first_name, p.last_name = "Michael", "Jordan"
        self.assert_sound(self.editor.db.to_file(), "after a rename")

    def test_copying_a_portrait_keeps_blocks_aligned(self):
        self.editor.reassign_appearance("Sean Rooks", "Kobe Bryant")
        self.assert_sound(self.editor.appearance.to_file(), "after copying a portrait")

    def test_importing_a_photo_keeps_everything_aligned(self):
        from courtside.appearance import _png
        path = os.path.join(self.tmp, "square.png")
        with open(path, "wb") as fh:
            fh.write(_png(90, 90, bytes([12, 200, 80]) * (90 * 90)))
        self.editor.import_photo("Sean Rooks", path)
        self.assert_sound(self.editor.appearance.to_file(), "after importing a photo")
        table = self.editor.appearance.photos
        for i in range(table.count):
            self.assertEqual(table.offsets[i] % 4, 0, "photo entry %d" % i)
            self.assertEqual(len(table.entry(i)) % 4, 0, "photo entry %d" % i)

    def test_announcer_edit_keeps_entries_aligned(self):
        self.editor.reassign_announcer("Sean Rooks", "Kobe Bryant")
        table = self.editor.announcer.names
        for i in range(table.count):
            self.assertEqual(table.offsets[i] % 4, 0, "name clip %d" % i)

    def test_chunk_sizes_stay_multiples_of_four(self):
        # a name one byte longer used to shift every later chunk off alignment
        self.editor.one("Sean Rooks").last_name = "Jordanx"
        payload = iff.unpack(self.editor.db.to_file()).payload
        for chunk in iff.parse_chunks(payload).chunks:
            self.assertEqual(len(chunk.data) % 4, 0, chunk.ident)

    def test_a_verbatim_block_never_outgrows_the_engine_buffer(self):
        # padding a stored block would overflow the 0x2000 buffer it is DMA'd into
        blob = self.editor.appearance.to_file()
        starts = self.block_starts(blob)
        import struct as _s
        size = _s.unpack_from(">I", blob, 4)[0]
        n = (size + 0x1FFF) // 0x2000
        table = _s.unpack_from(">%dI" % (n + 1), blob, 8)
        for i in range(n):
            if (table[i] & 0xF) == 0:
                self.assertLessEqual(starts[i + 1] - starts[i], 0x2000, "block %d" % i)

    def test_save_refuses_a_container_that_would_not_load(self):
        editor = RosterEditor.open(ROM_PATH)
        broken = bytearray(editor.db.to_file())
        broken[8:12] = (0xDEADBE0).to_bytes(4, "big")   # bend the block table
        editor.db.to_file = lambda: bytes(broken)
        with self.assertRaises(EditorError):
            editor.save(os.path.join(self.tmp, "nope.z64"))


@needs_rom
class TestAnnouncer(unittest.TestCase):
    def setUp(self):
        self.editor = RosterEditor.open(ROM_PATH)
        self.tmp = tempfile.mkdtemp()

    def test_container_round_trips_byte_for_byte(self):
        original = self.editor.rom.read("TEAMTALK.IFF")
        self.assertEqual(self.editor.announcer.to_file(), original)

    def test_every_player_has_a_pair_of_clips(self):
        db = self.editor.announcer
        self.assertEqual(db.names.count, 1 + 2 * self.editor.db.player_capacity)
        for p in self.editor.db.real_players():
            first, last = db.clip_indices(p.player_id)
            self.assertEqual((first, last), (p.player_id * 2 - 1, p.player_id * 2))
            self.assertTrue(db.has_call(p.player_id), p.full_name)

    def test_copying_a_call_moves_both_clips(self):
        db = self.editor.announcer
        target = self.editor.one("Sean Rooks").player_id
        source = self.editor.one("Kobe Bryant").player_id
        before = [db.names.entry(i) for i in db.clip_indices(target)]
        self.editor.reassign_announcer("Sean Rooks", "Kobe Bryant")
        after = [db.names.entry(i) for i in db.clip_indices(target)]
        wanted = [db.names.entry(i) for i in db.clip_indices(source)]
        self.assertEqual(after, wanted)
        self.assertNotEqual(after, before)

    def test_copying_one_half_leaves_the_other(self):
        db = self.editor.announcer
        target = self.editor.one("Sean Rooks").player_id
        first_before, _ = (db.names.entry(i) for i in db.clip_indices(target))
        self.editor.reassign_announcer("Sean Rooks", "Kobe Bryant",
                                       given_name=False, surname=True)
        first_after, last_after = (db.names.entry(i) for i in db.clip_indices(target))
        self.assertEqual(first_after, first_before)
        source = self.editor.one("Kobe Bryant").player_id
        self.assertEqual(last_after, db.names.entry(db.clip_indices(source)[1]))

    def test_silence_empties_both_clips(self):
        db = self.editor.announcer
        p = self.editor.one("Sean Rooks")
        self.editor.silence_announcer("Sean Rooks")
        self.assertEqual(db.clip_lengths(p.player_id), (0, 0))
        self.assertFalse(db.has_call(p.player_id))

    def test_a_changed_call_survives_a_save(self):
        self.editor.reassign_announcer("Sean Rooks", "Kobe Bryant")
        path = os.path.join(self.tmp, "out.z64")
        report = self.editor.save(path)
        self.assertIsNotNone(report.teamtalk)

        again = RosterEditor.open(path)
        db = again.announcer
        rooks = again.one("Sean Rooks").player_id
        kobe = again.one("Kobe Bryant").player_id
        self.assertEqual([db.names.entry(i) for i in db.clip_indices(rooks)],
                         [db.names.entry(i) for i in db.clip_indices(kobe)])

    def test_growing_a_file_leaves_every_other_file_readable(self):
        # TEAMTALK outgrows its slot, so the whole data area gets relaid
        self.editor.reassign_announcer("Sean Rooks", "Shareef Abdur-Rahim")
        path = os.path.join(self.tmp, "out.z64")
        self.editor.save(path)

        again = RosterEditor.open(path)
        for entry in again.rom.fs.entries:
            self.assertEqual(len(again.rom.read(entry.name)), entry.size, entry.name)
        self.assertEqual(again.db.problems(), [])
        self.assertIsNotNone(again.appearance.photo(again.one("Kobe Bryant").player_id))
        self.assertEqual(again.one("Kobe Bryant").jersey_text, "8")


@needs_rom
class TestRomRoundTrip(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.editor = RosterEditor.open(ROM_PATH)

    def test_rom_identified(self):
        self.assertEqual(self.editor.rom.cart_id, "NNBE")
        self.assertEqual(self.editor.rom.cic, 6103)

    def test_header_crc_matches(self):
        have = tuple(int.from_bytes(self.editor.rom.data[0x10 + i * 4:0x14 + i * 4], "big")
                     for i in range(2))
        self.assertEqual(calc_crc(bytes(self.editor.rom.data), 6103), have)

    def test_teaminfo_repacks_byte_identically(self):
        original = self.editor.rom.read("TEAMINFO.IFF")
        self.assertEqual(self.editor.db.to_file(), original)

    def test_teamdata_repacks_byte_identically(self):
        original = self.editor.rom.read("TEAMDATA.IFF")
        self.assertEqual(self.editor.appearance.to_file(), original)

    def test_every_block_decodes(self):
        container = iff.unpack(self.editor.rom.read("TEAMDATA.IFF"))
        self.assertEqual(len(container.payload), 0x1A3C10)

    def test_roster_is_self_consistent(self):
        self.assertEqual(self.editor.db.problems(), [])

    def test_known_players(self):
        kobe = self.editor.one("Kobe Bryant")
        self.assertEqual(kobe.jersey_text, "8")
        self.assertEqual(kobe.height_text, "6'6\"")
        self.assertEqual(kobe.weight_lbs, 200)
        shaq = self.editor.one("Shaquille")
        self.assertEqual(shaq.height_text, "7'1\"")
        self.assertEqual(shaq.position, "C")
        bogues = self.editor.one("Bogues")
        self.assertEqual(bogues.height_text, "5'3\"")

    def test_years_pro_matches_real_careers(self):
        # seasons completed before 1997-98, taken from each player's draft year
        expected = {
            "Tim Duncan": 0, "Keith Van Horn": 0, "Brevin Knight": 0,
            "Kobe Bryant": 1, "Allen Iverson": 1, "Ray Allen": 1,
            "Kevin Garnett": 2, "Grant Hill": 3, "Anfernee Hardaway": 4,
            "Shaquille O'Neal": 5, "Dikembe Mutombo": 6, "Gary Payton": 7,
            "Reggie Miller": 10, "Dennis Rodman": 11, "Karl Malone": 12,
            "John Stockton": 13, "Hakeem Olajuwon": 13, "Buck Williams": 16,
        }
        for name, want in expected.items():
            self.assertEqual(self.editor.one(name).years_pro, want, name)

    def test_rookies_and_veterans_look_sane(self):
        years = [p.years_pro for p in self.editor.db.real_players()]
        self.assertEqual(min(years), 0)
        self.assertLessEqual(max(years), roster.YEARS_PRO_MAX)
        self.assertGreater(years.count(0), 20)   # a full rookie class

    def test_heights_are_valid_feet_inches(self):
        for p in self.editor.db.players:
            self.assertLessEqual(p.height_remainder, 11, p.full_name)
            self.assertGreaterEqual(p.height_feet, 4, p.full_name)

    def test_photos_decode(self):
        app = self.editor.appearance
        decoded = sum(1 for pid in range(1, 60) if app.photo(pid) is not None)
        self.assertGreater(decoded, 50)


@needs_rom
class TestEditing(unittest.TestCase):
    def setUp(self):
        self.editor = RosterEditor.open(ROM_PATH)
        self.tmp = tempfile.mkdtemp()

    def out(self, name="out.z64") -> str:
        return os.path.join(self.tmp, name)

    def test_edit_survives_a_save_and_reload(self):
        p = self.editor.one("Kobe Bryant")
        p.jersey = 24
        p.set_attribute("three_pointers", 99)
        p.set_height(6, 7)
        p.weight_lbs = 212
        p.position = "SG"
        p.last_name = "Bean-Bryant"
        path = self.out()
        self.editor.save(path)

        again = RosterEditor.open(path)
        q = again.one("Bean-Bryant")
        self.assertEqual(q.jersey, 24)
        self.assertEqual(q.attribute("three_pointers"), 99)
        self.assertEqual(q.height_text, "6'7\"")
        self.assertEqual(q.weight_lbs, 212)
        self.assertEqual(q.position, "SG")
        self.assertEqual(q.first_name, "Kobe")

    def test_saved_rom_has_a_valid_crc(self):
        self.editor.one("Kobe Bryant").jersey = 24
        path = self.out()
        self.editor.save(path)
        again = RosterEditor.open(path)
        have = tuple(int.from_bytes(again.rom.data[0x10 + i * 4:0x14 + i * 4], "big")
                     for i in range(2))
        self.assertEqual(calc_crc(bytes(again.rom.data), 6103), have)

    def test_trade_keeps_rosters_valid(self):
        a, b = self.editor.trade("Kobe Bryant", "Karl Malone")
        self.assertEqual(self.editor.db.team_name(a.team), "Utah")
        self.assertEqual(self.editor.db.team_name(b.team), "L.A. Lakers")
        self.assertEqual(self.editor.db.problems(), [])
        path = self.out()
        self.editor.save(path)
        again = RosterEditor.open(path)
        self.assertEqual(again.db.team_name(again.one("Kobe Bryant").team), "Utah")
        self.assertEqual(again.db.problems(), [])

    def test_release_moves_player_to_free_agents(self):
        p = self.editor.release("Kobe Bryant")
        self.assertEqual(p.team, roster.FREE_AGENT_TEAM)
        ids = [s.player_id for s in self.editor.db.roster(13)]
        self.assertNotIn(p.player_id, ids)
        self.assertIn(p.player_id,
                      [s.player_id for s in self.editor.db.roster(roster.FREE_AGENT_TEAM)])

    def test_starter_slots_stay_unique(self):
        db = self.editor.db
        lakers = [db.by_id(s.player_id) for s in db.roster(13)]
        bench = next(p for p in lakers if db.starter_slot(p) == 0)
        db.set_starter_slot(bench, 1)
        slots = sorted(s.starter for s in db.roster(13) if s.starter)
        self.assertEqual(slots, [1, 2, 3, 4, 5])

    def test_years_pro_survives_a_save(self):
        p = self.editor.one("Tim Duncan")
        self.assertEqual(p.years_pro, 0)
        p.years_pro = 12
        path = self.out()
        self.editor.save(path)
        again = RosterEditor.open(path)
        self.assertEqual(again.one("Tim Duncan").years_pro, 12)
        self.assertEqual(again.one("Tim Duncan").years_pro_text, "12 years")

    def test_reassigning_a_likeness_leaves_experience_alone(self):
        # 0x10 sits among the model bytes but is a career figure, not a look
        rookie = self.editor.one("Tim Duncan")
        veteran = self.editor.one("Karl Malone")
        before = rookie.years_pro
        self.editor.reassign_appearance("Tim Duncan", "Karl Malone")
        self.assertEqual(rookie.years_pro, before)
        self.assertNotEqual(rookie.years_pro, veteran.years_pro)

    def test_appearance_reassignment_persists(self):
        dst, src = self.editor.reassign_appearance("Shaquille", "Kobe Bryant")
        path = self.out()
        self.editor.save(path)
        again = RosterEditor.open(path)
        app = again.appearance
        self.assertEqual(app.photos.entry(dst.player_id), app.photos.entry(src.player_id))
        self.assertEqual(app.faces.entry(dst.player_id), app.faces.entry(src.player_id))
        self.assertEqual(again.one("Shaquille").appearance_bytes,
                         again.one("Kobe Bryant").appearance_bytes)

    def test_json_export_import_round_trip(self):
        blob = self.editor.export_dict()
        blob["players"][40]["jersey"] = 24
        blob["players"][40]["attributes"]["dunking"] = 55
        self.editor.apply_dict(blob)
        p = self.editor.db.players[40]
        self.assertEqual(p.jersey, 24)
        self.assertEqual(p.attribute("dunking"), 55)

    def test_renaming_many_players_still_fits(self):
        for i, p in enumerate(self.editor.db.real_players()[:40]):
            p.last_name = "Testcase%02d" % i
        path = self.out()
        report = self.editor.save(path)
        again = RosterEditor.open(path)
        self.assertEqual(again.db.players[0].last_name, "Testcase00")
        self.assertEqual(again.db.problems(), [])
        self.assertFalse(report.warnings)


if __name__ == "__main__":
    unittest.main(verbosity=2)
