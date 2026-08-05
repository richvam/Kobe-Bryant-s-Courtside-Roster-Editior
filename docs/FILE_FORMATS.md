# File formats in *Kobe Bryant's NBA Courtside*

Everything here was recovered by disassembling the retail US cartridge
(`NBA COURTSIDE`, cart id `NNBE`, 12 MiB, CIC-NUS-6103). Addresses in the form
`0x800xxxxx` are RAM addresses in the main code image; ROM offset =
RAM address − `0x7FFFF400`.

The engine is Left Field Productions' N64 engine, so most of this applies to
their other N64 titles too.

---

## 1. ROM layout

| Region | Contents |
| --- | --- |
| `0x000000`–`0x000040` | standard N64 header (CRCs at `0x10`/`0x14`) |
| `0x001000`–`0x0DF9C0` | code + rodata, loaded at RAM `0x80000400` |
| `0x0DF9C0` | packed-file table |
| `0x0DFC84`–`0xB85408` | packed file data |
| `0xB8540C`–`0xC00000` | filler ("Hey! What are you doing looking at this binary…") |

### 1.1 Packed-file table

```
u32               file count (22 on the retail cart)
per file, 0x20 bytes:
  char[16]        name, NUL padded ("TEAMINFO.IFF")
  u32             reserved (always 0)
  u32             size in bytes
  u32             offset, relative to the data base
```

The data base is the table's end, with **no rounding at all** — `0xDFC84` here.
The boot scan computes it at RAM `0x8007D378` as `table + 4 + count * 0x20` and
every offset in the table is relative to that. Getting this wrong by even four
bytes still lets every file parse (each one begins with a header word, so a
shifted view merely looks like the header is missing), and still lets a file be
rewritten in place — but the moment one has to be moved, every file lands four
bytes past where the engine looks for it and the cartridge stops booting.

Files are stored in offset order, each on a 16-byte boundary, with the gaps
filled with `0x55`.

Right after computing the base the scan walks the table and DMAs sixteen bytes
from the head of each file (`0x8007D41C`). A file whose first word is
`0x735764B0` gets the "compressed" bit set in its entry and takes its *logical*
size from the third word; anything else is read straight off the cartridge and
keeps the size in the table. That is the whole of the engine's notion of what a
packed file is.

Table of contents on the retail cart: `CONFIG.GID`, `RES.BIG`, `CONFIG.BIG`,
`SHELL.BIG`, `CONFIG.DIR`, `WAVE.GID`, `WAVE.BIG`, `TEAMINFO.IFF`,
`TEAMTALK.IFF`, `WAVE.DIR`, `SFX.DIR`, `SFX.GID`, `SFX.BIG`, `TEAMDATA.IFF`,
`RES.GID`, `RES.DIR`, `ANIM.BIG`, `ANIM.DIR`, `ANIM.GID`, `CROWDSFX.ALL`,
`SHELL.DIR`, `SHELL.GID`.

### 1.2 CRC

The header CRC pair is the ordinary bootcode checksum over `0x1000`–`0x101000`
with the CIC-6103 seed `0xA3886759`. Note the roster files live well past that
window, but the file table at `0x0DF9C0` does not, so any edit that changes a
file's size lands inside the checksum after all. The editor recomputes it.

### 1.3 Growing a file

Nothing in the ROM refers to a packed file by absolute address: the boot scan
patches each table entry's offset into a real cartridge address and everything
downstream goes through that. So a file that outgrows the gap behind it can be
moved, as long as the table is updated and the files behind it are pushed down
in step. The editor does exactly that, keeping the files in *front* of it — and
therefore most of the checksummed window — byte for byte untouched.

It much prefers not to have to. Both offset-table chunks (`PIMG`, `PNAM`) hold
blobs that declare their own length, so a replacement that fits its existing
slot is dropped in and padded, leaving every other entry exactly where it was;
and a recompressed block is padded back out to the length it had before, so the
blocks behind it keep their offsets too. A one-photo change therefore rewrites
about 8 KB of a 12 MB cartridge instead of a megabyte and a half.

---

## 2. The `IFF` block container

`TEAMINFO.IFF` and `TEAMDATA.IFF` are block-addressed containers so the engine
can seek inside them without decompressing the whole file (`fread` in
`file.c`, RAM `0x8007D804`, works in `0x2000`-byte blocks).

```
u32       0x735764B0, the asset magic the boot scan looks for
char[4]   'IFF\0' or 'iff\0'
u32       decoded payload size
u32[n+1]  block table, n = ceil(size / 0x2000)
...       block data
```

Each table entry is `file_offset << 4 | flag`, counted from the first byte of
the file — which is why the first entry reads `0xC + 4 * (n + 1)` and why the
last one, marking the end of the final block, is exactly the file's length.
`flag == 0` means the block is stored verbatim, otherwise it is
LZSS-compressed. Every block decodes to exactly `0x2000` bytes except the last.

The block loader (`0x8007DBFC`) reads the table out of the file's first
`0x2000` bytes at `+0xC`, DMAs `end - start` bytes into an `0x2000`-byte buffer
and hands a compressed block to the decompressor. So the table has to live
inside the first `0x2000` bytes and no stored block may be longer than
`0x2000`.

### 2.1 Alignment — the rule that bites

**Every block start is a multiple of four.** So is every chunk size, every
entry offset and every entry size, across all 211 blocks, 406 face textures,
386 photographs and 769 name clips in the shipped cartridge — without a single
exception. That is not a coincidence of the data:

* the engine DMAs each block straight out of the ROM (`0x8007DBC0` onwards),
  and the PI wants an aligned cartridge address;
* it then reads the decoded payload with 32-bit loads — the offset tables, the
  chunk headers, the player records — and a misaligned load faults the CPU.

A rewrite that decodes perfectly on a PC will still refuse to load on the
console if it drops this. `iff.check()` enforces it, and the editor runs that
over every container before it will write a ROM.

One exception matters when padding: a **verbatim** block must be left at its
natural length. The engine uses the block's extent as the DMA length into a
`0x2000` buffer, so padding one would run off the end of that buffer. Only
compressed blocks get padded — harmless, because the decoder stops at its
end-of-stream token long before it reaches the filler.

---

## 3. LZSS codec

Decompressor at RAM `0x800DE914`. The stream interleaves whole bytes with a
bit buffer: the decoder primes a byte with a sentinel (`byte | 0x100`), takes
bits from the least significant end, and pulls a fresh byte the moment the
buffer empties. Literals and "extra" bytes come straight from the stream.

```
0                       literal, copy the next stream byte
1 0                     match, length 2
1 1 <2 bits n>          match, length n + 2          (n != 0)
1 1 00 <4 bits n>       match, length n + 5          (n != 0)
1 1 00 0000 <byte n>    match, length n + 0x14       (n != 0)
1 1 00 0000 0x00        end of stream
```

Each match is followed by a 2-bit distance class (table at RAM `0x800DE8F4`):

| Class | Bits | Base | Distance range |
| --- | --- | --- | --- |
| 0 | 5 | 0x001 | 1 – 32 |
| 1 | 7 | 0x021 | 33 – 160 |
| 2 | 9 | 0x0A1 | 161 – 672 |
| 3 | 10 | 0x2A1 | 673 – 1696 |

Multi-bit values are read most significant bit first; a class wider than 8 bits
takes its top 8 bits from a whole stream byte first.

`courtside/lzss.py` implements both directions. The encoder does a shortest
path parse over the format's exact bit costs and comes out about 2–3 % smaller
than the data the game shipped with.

---

## 4. `AIFF` chunk payload

Decoded container payloads are EA-style chunk files:

```
char[4]  'AIFF'
u32      size of everything after this field
char[4]  form id — 'LFPI' for TEAMINFO, 'LFPD' for TEAMDATA
repeat:
  char[4]  chunk id
  u32      chunk size
  bytes    chunk data, padded to an even length
```

---

## 5. `TEAMINFO.IFF` — the roster database

Form `LFPI`, chunks `IVER`, `CSTR`, `TEAM`, `ROST`, `PLYR`, `DATE`.

### 5.1 `CSTR` — string pool

```
u32     string count (640 on the retail cart)
bytes   NUL-terminated strings, starting with an empty one at offset 4
```

Names elsewhere are **byte offsets from the start of the chunk**, so the pool
must only ever be appended to.

### 5.2 `TEAM` — 56 bytes per team

```
0x00  u32   city string offset            ("Los Angeles")
0x04  u32   unknown
0x08  u32   abbreviation string offset    ("LAL")
0x0C  u32   display name string offset    ("L.A. Lakers")
0x10  u8    conference group
0x11  u8    division group
0x12  u8    team id
...         colours, arena and schedule data
```

35 teams: index 0 is Free Agents, 1–29 the NBA clubs, 30/31 the All-Star
squads, 32–34 the Left Field / Nintendo bonus teams.

### 5.3 `ROST` — rosters and depth charts

```
u16       group count (37)
per group:
  u16     entry count
  u16[]   (starter_slot << 9) | player_id
```

Group index matches the team index. `starter_slot` is 0 for a bench player and
1–5 for the starting five; each NBA team ships exactly 12 players with one
player in each starting slot. Groups 35 and 36 hold the ids reserved for
created players.

### 5.4 `PLYR` — 72 bytes per player

```
u32  slot capacity (384)
u32  slots used    (348 real NBA players; the rest are the bonus teams)
```

| Offset | Type | Field |
| --- | --- | --- |
| `0x00` | u32 | last name — CSTR byte offset |
| `0x04` | u32 | first name — CSTR byte offset |
| `0x08` | u16 | player id (1-based; also the index into `PTEX`/`PIMG`) |
| `0x0A` | u8 | team index |
| `0x0B` | u8 | jersey number (100 encodes the "00" shirt) |
| `0x0C` | u8 | position, packed — see below |
| `0x0D` | u8 | height: feet in the high nibble, inches in the low one (`0x61` = 6'1") |
| `0x0E` | u8 | weight in pounds − 100 |
| `0x0F` | u8 | always 0 |
| `0x10` | u8 | years pro — seasons completed before this one, 0 for a rookie |
| `0x11`–`0x15` | u8 × 5 | on-court model / skin bytes |
| `0x16` | u8 | shooting range, index into 8/12/16/20/25 feet |
| `0x17` | u8 | Shooting |
| `0x18` | u8 | 3 Pointers |
| `0x19` | u8 | Free Throws |
| `0x1A`,`0x1B` | u8 × 2 | Rebounding (stored twice; the game reads both) |
| `0x1C` | u8 | Stealing |
| `0x1D` | u8 | Passing |
| `0x1E` | u8 | Blocking |
| `0x1F` | u8 | unknown, 1–3 |
| `0x20` | u8 | Dribbling |
| `0x21` | u8 | Speed |
| `0x22` | u8 | Jumping |
| `0x23` | u8 | Dunking |
| `0x24` | u8 | unknown, 1–5 |
| `0x25` | u8 | Strength |
| `0x26` | u8 | Stamina |
| `0x27` | u8 | unknown, 30–100 (correlates well with games played — probably durability) |
| `0x28` | u8 | unknown, 1–83 |
| `0x29`–`0x47` | | flags and career statistics |

The attribute offsets are not guesswork: the roster screen's formatter table at
RAM `0x800D5010` holds fourteen function pointers, and the label pointer table
right behind it at `0x800D50D8` holds `Range, Shooting, 3 Pointers, Free
Throws, Dribbling, Passing, Speed, Jumping, Rebounding, Strength, Dunking,
Stealing, Blocking, Stamina` in the same order. Each formatter loads exactly
one byte of the record, which pins the mapping. Spot checks agree with what you
would expect: Shaquille O'Neal has Free Throws 49 and Dunking 100, Reggie
Miller has 3 Pointers 100, Gary Payton has Stealing 100, Dikembe Mutombo has
Blocking 100 and Muggsy Bogues has Blocking 5.

`0x10` was pinned down by comparing it against the draft year of 68 players:
it agrees with every one, from the 1997 rookie class at 0 up to the four
1980-81 draftees (Rick Mahorn, Danny Schayes, Buck Williams, Herb Williams)
at 16. The whole roster forms a clean career pyramid — 33 rookies tapering to
those four. It sits in the middle of the model/skin run, so copying a likeness
must skip it.

#### Position byte (`0x0C`)

```
bits 0-1   primary class     1 = C, 2 = F, 3 = G
bits 2-3   qualifier         0 = none, 1 = Power/Point, 2 = Small/Shooting
bits 4-5   secondary class   0 = none, 1 = C, 2 = F, 3 = G
```

If a secondary class is set the game prints `primary + secondary`, otherwise
`qualifier + primary`. That generates exactly the eleven strings in the ROM's
position table at RAM `0x800CE00C`: `C PF SF F G PG SG` plus `FC CF GF FG`.
Observed values are `1 = C`, `2 = F`, `3 = G`, `6 = PF`, `7 = PG`, `10 = SF`,
`11 = SG`, `18 = FC`, `33 = CF`, `35 = GF`, `50 = FG`.

### 5.5 `DATE`

The season schedule. The editor carries it through untouched.

---

## 6. `TEAMDATA.IFF` — likenesses

Form `LFPD`, chunks `DVER`, `PTEX`, `PIMG`, `SPAL`.

`PTEX` and `PIMG` share a shape:

```
u32       entry count
u32[n+1]  offsets from the start of the chunk
...       entry data
```

Entry *i* runs from `offsets[i]` to `offsets[i+1]`, so an entry's extent is
implied by its neighbour — entries cannot be made to share storage, they have
to be duplicated.

* **`PTEX`** — 405 entries of exactly `0x840` bytes: the face texture pasted
  onto the 3D player model.
* **`PIMG`** — 385 entries, each a Left Field asset blob: `0x735764xx` magic,
  a four character tag (`RAW\0`), a u32 decoded size (3585) and then an LZSS
  stream. The decoded bytes are one palette-bank index followed by a 64 × 56
  8-bit indexed image — the roster photograph.
* **`SPAL`** — ten 256-colour palettes in RGBA5551 (`rrrrrggg gggbbbbb a`),
  512 bytes each. Each photo names its bank in that first decoded byte.

Writing a photo back means reversing that: quantise a picture to one of the ten
banks, prefix the bank index, LZSS-pack it and rebuild the blob with the same
`0x735764xx` header the original carried. `courtside/images.py` does the
decoding and quantising; `AppearanceDatabase.set_photo` does the packing.

Both tables are indexed by player id, which is what makes "give this player
that player's face" a table edit rather than an art job.

---

## 7. Other files

| File | Notes |
| --- | --- |
| `*.BIG` / `*.DIR` / `*.GID` | the engine's resource archives (`resman.c`); `BIG` holds the data, `DIR` the index, `GID` a group id |
| `CROWDSFX.ALL` | crowd samples, courtesy of the Seattle Supersonics |

---

## 8. `TEAMTALK.IFF` — the PA announcer

The only packed file that carries no `0x735764B0` compression header, so the
boot scan leaves it alone and the engine reads it straight off the cartridge.
What is left is a bare `AIFF` chunk list — the same thing the other two files
hold *inside* their compression wrapper.

```
char[4]   'AIFF'
u32       size of everything after this field
char[4]   'LFPT'
repeat:   char[4] chunk id, u32 size, data padded to a multiple of four
```

The chunk walker (`0x80088ABC`) rounds every size up to four before stepping
over it, so a chunk whose size is not already a multiple of four silently
desynchronises everything behind it.

| Chunk | Contents |
| --- | --- |
| `TVER` | version, `00 01 00 01` |
| `PNAM` | 769 spoken player names, in the count/offset-table shape |
| `TALK` | 390 general commentary clips, same shape |

`PNAM` holds `1 + 2 × 384` entries because each player owns **two** clips. The
routine at RAM `0x8008AB08` proves the mapping — it reads the player id
straight out of the record and indexes the table:

```
8008ab14  lw   $v1, 0x2dec($v1)   ; the PNAM offset table
8008ab20  lhu  $a2, 8($a2)        ; player id, from record + 0x08
8008ab34  sll  $v0, $a2, 1        ; id * 2
8008ab38  addu $v0, $v0, $a3      ;   + which of the pair
8008ab3c  sll  $v0, $v0, 2
8008ab40  addu $v0, $v0, $v1
8008ab44  lw   $a2, ($v0)         ; offsets[i]
8008ab48  lw   $a3, 4($v0)        ; offsets[i + 1]
```

The count word sits in front of the offsets, so player *N* owns entries
`2N - 1` and `2N`. Which of the pair is the given name and which the surname
is inferred from clip lengths rather than proven — Rick Fox has the shortest
pair, Shareef Abdur-Rahim one of the longest.

The clip data never reaches the file layer: `0x8008AB08` hands its caller a raw
cartridge address (`offsets[i]` + the file's ROM address + the chunk offset)
and the length `offsets[i + 1] - offsets[i]`, and the caller DMAs it. The
samples themselves are still opaque: each entry opens with a u32 `4` and a u32
decoded length, then compressed audio. Nothing here decodes them, so the editor
moves whole clips around rather than trying to make new ones. Because a clip
declares its own length, a shorter clip dropped into a longer slot can simply
be padded — which is how the editor avoids resizing a 3.6 MB file for a
one-name change.
