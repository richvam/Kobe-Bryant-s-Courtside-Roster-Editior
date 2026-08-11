# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A roster editor for the Nintendo 64 game *Kobe Bryant's NBA Courtside* (1998). It reads a
retail ROM, decodes the game's own compressed data files, edits players/teams/photos/announcer
calls, and writes a playable ROM back out with a corrected header CRC.

None of the game's formats were documented anywhere — they were recovered by disassembling the
cartridge. `docs/FILE_FORMATS.md` is the authoritative write-up (container format, LZSS codec,
72-byte player record, position packing). **Read it before touching any parsing or packing
code.** Module docstrings cite the RAM addresses of the engine routines they mirror
(e.g. the boot scan at `0x8007D378`, the block loader at `0x8007DBFC`, the chunk walker at
`0x80088ABC`); keep that convention when you learn something new about the format.

## Hard constraints

- **Standard library only.** `pyproject.toml` declares `dependencies = []` and it must stay that
  way. PNG/BMP decoding, PNG encoding, the LZSS codec, the HTTP server and the GUI are all
  hand-rolled on the stdlib. Pillow is an *optional* fallback for exotic image formats only
  (`images._decode_with_pillow`) and its absence must degrade to a helpful message, never a crash.
- **Python 3.10+** (`X | None` syntax is used throughout).
- **No game data in the repo.** `.gitignore` blocks `*.z64/*.n64/*.v64`. Never commit a ROM,
  a ROM fragment, or extracted game assets.
- `.bat`/`.cmd` files must keep CRLF endings (enforced by `.gitattributes`); everything else is LF.

## Commands

No build step, no linter config, no `requirements.txt` — run everything from a clean checkout.

```sh
# tests (unittest; pytest works too but is not a dependency)
python3 tests/test_courtside.py
COURTSIDE_ROM=/path/to/rom.z64 python3 tests/test_courtside.py   # 51 of 78 tests need a ROM

# a single class or test
python3 -m unittest tests.test_courtside.TestLzss -v
python3 -m unittest tests.test_courtside.TestAlignment.test_shipped_files_pass -v

# the three front ends
python3 -m courtside ROM.z64 info
python3 courtside_gui.py ROM.z64            # Tkinter desktop app
python3 -m courtside ROM.z64 serve          # local web UI on 127.0.0.1:8756

# the sanity check that matters most after any codec/packing change
python3 -m courtside ROM.z64 verify
```

`verify` re-packs all three packed files and asserts they round-trip byte for byte, re-reads the
cartridge the way the console's boot scan does, and checks the header CRC. Without a ROM the
suite still covers the codec, the position packing, the string pool, image decoding and the
launcher's failure paths — but **any change to `iff.py`, `lzss.py`, `rom.py`, `offsets.py` or the
serialisers is effectively untested until it is run against a real ROM.** Say so explicitly when
you cannot run the ROM-backed tests.

## Architecture

The package is a strict stack; each layer is usable on its own and knows nothing about the ones
above it.

```
rom.py        byte order (.z64/.n64/.v64), bootcode CRC, the packed-file table
  └ iff.py    the 0x735764B0 block container + the AIFF chunk list inside it
      └ lzss.py     the engine's LZSS codec (decoder + optimal-parse encoder)
      └ offsets.py  the count/offset-table shape used by PTEX, PIMG and PNAM
          └ roster.py      TEAMINFO.IFF — teams, players, string pool, depth charts
          └ appearance.py  TEAMDATA.IFF — face textures, roster photos, palettes
          └ announcer.py   TEAMTALK.IFF — the PA announcer's recorded names
              └ editor.py  RosterEditor: the facade all three front ends drive
                  └ cli.py / gui.py / webapp.py
```

`images.py` sits off to the side: PNG/BMP decode, crop/scale, palette selection and quantisation
for photo import. It has no ROM knowledge.

**`RosterEditor` is the only public API.** `cli.py`, `gui.py` and `webapp.py` must not reach past
it into the containers — add a method to the facade instead. `TEAMDATA.IFF` (1.3 MB of packed art)
and `TEAMTALK.IFF` are loaded lazily on first access and only re-packed when their dirty flag is
set, which is why an ordinary roster edit saves in a couple of seconds.

### Save pipeline (`RosterEditor.save`)

1. `db.problems()` collects roster warnings (non-fatal, reported to the user).
2. Each dirty container is serialised, then run through `iff.check()` / `check_chunks()`.
   Any problem **raises `EditorError` — the ROM is not written.**
3. `rom.write()` places each file, in place if it fits, otherwise reflowing the files behind it.
4. `rom.verify()` re-reads the whole cartridge the way the boot scan does. Again, a problem
   refuses the save.
5. Only then is the CRC recomputed and the file written.

Never weaken steps 2 and 4 to get a save through. A ROM that decodes perfectly in Python can
still refuse to boot on hardware, and these checks are the only thing standing between an edit
and a dead cartridge.

## Format invariants you must not break

These are the rules the engine enforces implicitly; violating one produces a ROM that fails
silently or won't boot, and no Python-side test of decoded content will notice.

- **Four-byte alignment, everywhere.** Block starts, chunk sizes, offset-table entries. The engine
  DMAs blocks straight out of the ROM and reads payloads with 32-bit loads, and the chunk walker
  rounds every size up to four before stepping. Compressed blocks are zero-padded to alignment
  (the decoder stops at its end-of-stream token, so filler is never read); **verbatim blocks must
  not be padded** — their extent is the DMA length into a 0x2000-byte buffer.
- **No stored block may exceed `BLOCK_SIZE` (0x2000)**, and the block table must fit inside the
  file's first 0x2000 bytes — that is all the loader reads to find it.
- **The data base is `table_offset + 4 + count * 0x20`, with no rounding.** Aligning it puts every
  file four bytes off. Harmless while files are only rewritten in place; fatal the first time one
  has to move.
- **The `CSTR` string pool is append-only.** Player and team records hold byte offsets into it, so
  it is interned and extended, never rebuilt.
- **Prefer in-place writes over relayout.** `OffsetTable._replace_in_place` and `iff.pack`'s
  block-reuse both exist so that a one-photo edit rewrites ~8 KB of a 12 MB cartridge instead of
  shunting a megabyte downhill. `iff.pack(payload, original=…)` copies unchanged blocks verbatim,
  which is also what makes an untouched file re-pack byte-identically (tested).
- **`0x10` is years pro, not appearance.** It sits in the middle of the model-bytes run, so
  `APPEARANCE_OFFSETS` deliberately skips it — copying a likeness must not rewrite a career length.
- Roster photos and announcer clips both declare their own length, which is why they can be
  dropped into an oversized slot and padded.

## Conventions

- Comments explain *why the format demands this*, citing the engine routine where possible —
  not what the Python does. Match that density and tone; the existing comments are load-bearing
  documentation of a reverse-engineered format.
- `%`-style string formatting throughout, not f-strings.
- Errors are typed per layer (`RomError`, `ContainerError`, `CorruptStream`, `TableError`,
  `AppearanceError`, `AnnouncerError`, `EditorError`, `ImageError`, `ApiError`) and funnelled to
  a single message in `cli.main`.
- Long-running work (saving) runs on a background thread in the GUI and marshals results back to
  the Tk thread through `self._work` — never touch widgets off-thread.
- The web UI is three static files under `courtside/web/` served by `webapp.py`. It is fully
  offline by design: no CDNs, no external requests, no fonts fetched. Keep it that way.
- Commit messages are imperative and describe the user-visible effect, e.g.
  "Keep repacked files 32-bit aligned so edited ROMs load".
