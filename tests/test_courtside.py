"""Tests for the Courtside roster editor.

The codec and model tests run without a ROM.  The rest are skipped unless a
ROM is available - set ``COURTSIDE_ROM`` or drop one at ``tests/courtside.z64``.

    python -m pytest tests            # or
    python tests/test_courtside.py    # plain unittest runner
"""

from __future__ import annotations

import os
import random
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from courtside import iff, lzss, roster  # noqa: E402
from courtside.editor import RosterEditor  # noqa: E402
from courtside.rom import calc_crc  # noqa: E402

ROM_PATH = os.environ.get("COURTSIDE_ROM") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "courtside.z64")
HAVE_ROM = os.path.exists(ROM_PATH)
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
        try:
            import tkinter  # noqa: F401
        except ImportError:
            self.skipTest("Tkinter is not installed in this interpreter")
        from courtside import gui
        self.assertTrue(callable(gui.main))
        self.assertTrue(issubclass(gui.CourtsideGUI, __import__("tkinter").Tk))

    def test_launcher_script_is_runnable(self):
        launcher = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "courtside_gui.py")
        self.assertTrue(os.path.exists(launcher))
        with open(launcher, encoding="utf-8") as fh:
            compile(fh.read(), launcher, "exec")


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
