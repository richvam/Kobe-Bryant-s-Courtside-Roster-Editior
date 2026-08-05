"""The roster database held in ``TEAMINFO.IFF``.

Chunks
------
``CSTR``  u32 count, then a NUL-terminated string pool.  Names elsewhere are
          byte offsets from the start of the chunk, so the pool is only ever
          appended to - never rebuilt - and existing offsets stay valid.
``TEAM``  u32 count, then 56-byte team records.
``ROST``  u16 group count, then per-team ``u16 count`` followed by that many
          ``(starter_slot << 9) | player_id`` entries.
``PLYR``  u32 capacity, u32 used, then 72-byte player records.
``DATE``  season schedule; carried through untouched.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from . import iff

PLAYER_RECORD_SIZE = 72
TEAM_RECORD_SIZE = 56

FREE_AGENT_TEAM = 0

#: The engine stores the "00" shirt as 100 so it sorts apart from plain 0.
JERSEY_DOUBLE_ZERO = 100

# ---------------------------------------------------------------------------
# attributes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AttributeDef:
    key: str
    label: str
    offset: int
    #: some attributes are stored twice in a row and the game reads both
    mirror: int | None = None
    minimum: int = 1
    maximum: int = 100


#: In the order the game's own stat screen lists them.  Offsets come from the
#: formatter table at RAM 0x800D5010, whose entries load exactly these bytes.
ATTRIBUTES: tuple[AttributeDef, ...] = (
    AttributeDef("shooting", "Shooting", 0x17),
    AttributeDef("three_pointers", "3 Pointers", 0x18),
    AttributeDef("free_throws", "Free Throws", 0x19),
    AttributeDef("dribbling", "Dribbling", 0x20),
    AttributeDef("passing", "Passing", 0x1D),
    AttributeDef("speed", "Speed", 0x21),
    AttributeDef("jumping", "Jumping", 0x22),
    AttributeDef("rebounding", "Rebounding", 0x1A, mirror=0x1B),
    AttributeDef("strength", "Strength", 0x25),
    AttributeDef("dunking", "Dunking", 0x23),
    AttributeDef("stealing", "Stealing", 0x1C),
    AttributeDef("blocking", "Blocking", 0x1E),
    AttributeDef("stamina", "Stamina", 0x26),
)

ATTRIBUTES_BY_KEY = {a.key: a for a in ATTRIBUTES}

#: Shooting range is an index, not a 0-100 rating; the game prints "N feet".
RANGE_OFFSET = 0x16
RANGE_FEET = (8, 12, 16, 20, 25)

#: Seasons completed before this one; 0 is a rookie.  Verified against the
#: draft year of 68 players, from the 1997 rookie class up to the four
#: 1980-81 draftees who sit at 16.
YEARS_PRO_OFFSET = 0x10
YEARS_PRO_MAX = 30

#: Bytes that drive how a player looks and behaves but whose exact meaning is
#: not pinned down.  They are copied wholesale by "reassign appearance" and are
#: individually editable for anyone who wants to experiment.
MISC_FIELDS: tuple[tuple[str, str, int, int, int], ...] = (
    ("model_b", "Model B", 0x11, 0, 255),
    ("flags_a", "Flags A", 0x12, 0, 255),
    ("flags_b", "Flags B", 0x13, 0, 255),
    ("misc_14", "Misc 14", 0x14, 0, 255),
    ("misc_15", "Misc 15", 0x15, 0, 255),
    ("misc_1f", "Misc 1F", 0x1F, 0, 255),
    ("misc_24", "Misc 24", 0x24, 0, 255),
    ("durability", "Durability?", 0x27, 0, 255),
    ("misc_28", "Misc 28", 0x28, 0, 255),
)

MISC_BY_KEY = {m[0]: m for m in MISC_FIELDS}

#: The bytes that describe a player's on-court model and skin.  0x10 sits in
#: the middle of this run but is his years pro, so it is deliberately excluded:
#: handing someone another player's face must not rewrite his career length.
APPEARANCE_OFFSETS = (0x11, 0x12, 0x13, 0x14, 0x15)

# ---------------------------------------------------------------------------
# positions
# ---------------------------------------------------------------------------
# The byte at 0x0C packs three fields:
#   bits 0-1  primary class   1=C  2=F  3=G
#   bits 2-3  qualifier       0=none  1=Power/Point  2=Small/Shooting
#   bits 4-5  secondary class 0=none  1=C  2=F  3=G
# which generates exactly the eleven strings the game ships: C PF SF F G PG SG
# and the hybrids FC CF GF FG.

PRIMARY_CLASSES = {1: "C", 2: "F", 3: "G"}
QUALIFIERS = {0: "", 1: "P", 2: "S"}
SECONDARY_CLASSES = {0: "", 1: "C", 2: "F", 3: "G"}

POSITION_NAMES = {
    "C": "Center",
    "PF": "Power Forward",
    "SF": "Small Forward",
    "F": "Forward",
    "PG": "Point Guard",
    "SG": "Shooting Guard",
    "G": "Guard",
    "FC": "Forward / Center",
    "CF": "Center / Forward",
    "GF": "Guard / Forward",
    "FG": "Forward / Guard",
}


def decode_position(raw: int) -> str:
    primary = PRIMARY_CLASSES.get(raw & 3)
    if primary is None:
        return "?"
    secondary = SECONDARY_CLASSES.get((raw >> 4) & 3, "")
    if secondary:
        return primary + secondary
    return QUALIFIERS.get((raw >> 2) & 3, "") + primary


def encode_position(text: str) -> int:
    text = text.strip().upper()
    inverse = {}
    for raw in range(64):
        name = decode_position(raw)
        inverse.setdefault(name, raw)
    if text not in inverse or text == "?":
        raise ValueError("unknown position %r (expected one of %s)"
                         % (text, ", ".join(sorted(POSITION_NAMES))))
    return inverse[text]


POSITION_CODES = sorted(POSITION_NAMES, key=lambda p: encode_position(p))


# ---------------------------------------------------------------------------
# string pool
# ---------------------------------------------------------------------------

class StringPool:
    """Append-only view of the ``CSTR`` chunk.

    Offsets already baked into player and team records must keep pointing at
    the same text, so new names are appended rather than the pool rebuilt.
    """

    def __init__(self, data: bytes) -> None:
        self.declared_count = struct.unpack_from(">I", data, 0)[0]
        self._buf = bytearray(data)
        self._index: dict[str, int] = {}
        off = 4
        while off < len(self._buf):
            end = self._buf.find(b"\0", off)
            if end < 0:
                break
            text = self._buf[off:end].decode("latin-1")
            self._index.setdefault(text, off)
            off = end + 1
        self._added = 0

    def get(self, offset: int) -> str:
        if offset <= 0 or offset >= len(self._buf):
            return ""
        end = self._buf.find(b"\0", offset)
        if end < 0:
            end = len(self._buf)
        return self._buf[offset:end].decode("latin-1")

    def intern(self, text: str) -> int:
        text = text.replace("\0", "")
        if text == "":
            return 4  # the pool always opens with an empty string
        hit = self._index.get(text)
        if hit is not None:
            return hit
        # trailing padding zeros are part of the pool; append past them
        while self._buf and self._buf[-1] == 0 and (len(self._buf) < 2 or self._buf[-2] == 0):
            self._buf.pop()
        offset = len(self._buf)
        self._buf += text.encode("latin-1", "replace") + b"\0"
        self._index[text] = offset
        self._added += 1
        return offset

    def to_bytes(self) -> bytes:
        out = bytearray(self._buf)
        struct.pack_into(">I", out, 0, self.declared_count + self._added)
        out += b"\0" * (-len(out) % 4)   # keep the chunks behind us aligned
        return bytes(out)


# ---------------------------------------------------------------------------
# records
# ---------------------------------------------------------------------------

@dataclass
class Team:
    index: int
    city: str
    abbreviation: str
    name: str
    raw: bytearray

    @property
    def display(self) -> str:
        if self.name and self.city and self.name != self.city:
            return self.name
        return self.city or self.name or "Team %d" % self.index

    @property
    def is_nba(self) -> bool:
        return 1 <= self.index <= 29


class Player:
    """A 72-byte ``PLYR`` record with named accessors."""

    def __init__(self, index: int, raw: bytearray, pool: StringPool) -> None:
        self.index = index
        self.raw = raw
        self._pool = pool

    # -- identity -------------------------------------------------------
    @property
    def player_id(self) -> int:
        return struct.unpack_from(">H", self.raw, 0x08)[0]

    @property
    def last_name(self) -> str:
        return self._pool.get(struct.unpack_from(">I", self.raw, 0x00)[0])

    @last_name.setter
    def last_name(self, value: str) -> None:
        struct.pack_into(">I", self.raw, 0x00, self._pool.intern(value))

    @property
    def first_name(self) -> str:
        return self._pool.get(struct.unpack_from(">I", self.raw, 0x04)[0])

    @first_name.setter
    def first_name(self, value: str) -> None:
        struct.pack_into(">I", self.raw, 0x04, self._pool.intern(value))

    @property
    def full_name(self) -> str:
        return (self.first_name + " " + self.last_name).strip()

    # -- roster ---------------------------------------------------------
    @property
    def team(self) -> int:
        return self.raw[0x0A]

    @team.setter
    def team(self, value: int) -> None:
        self.raw[0x0A] = value & 0xFF

    @property
    def jersey(self) -> int:
        """Raw number; 100 is the game's encoding of the double-zero shirt."""
        return self.raw[0x0B]

    @jersey.setter
    def jersey(self, value: int) -> None:
        self.raw[0x0B] = max(0, min(JERSEY_DOUBLE_ZERO, int(value)))

    @property
    def jersey_text(self) -> str:
        return "00" if self.jersey == JERSEY_DOUBLE_ZERO else str(self.jersey)

    @property
    def years_pro(self) -> int:
        """Seasons completed before this one; 0 is a rookie."""
        return self.raw[YEARS_PRO_OFFSET]

    @years_pro.setter
    def years_pro(self, value: int) -> None:
        self.raw[YEARS_PRO_OFFSET] = max(0, min(YEARS_PRO_MAX, int(value)))

    @property
    def years_pro_text(self) -> str:
        years = self.years_pro
        if years == 0:
            return "rookie"
        return "%d year%s" % (years, "" if years == 1 else "s")

    @property
    def position(self) -> str:
        return decode_position(self.raw[0x0C])

    @position.setter
    def position(self, value: str) -> None:
        self.raw[0x0C] = encode_position(value)

    @property
    def position_raw(self) -> int:
        return self.raw[0x0C]

    # -- physique -------------------------------------------------------
    # Height is packed as two nibbles: feet in the high nibble, inches in the
    # low one, so 6'1" is stored as 0x61.  Every shipped record obeys this.
    @property
    def height_feet(self) -> int:
        return self.raw[0x0D] >> 4

    @property
    def height_remainder(self) -> int:
        return self.raw[0x0D] & 0xF

    @property
    def height_inches(self) -> int:
        return self.height_feet * 12 + self.height_remainder

    @height_inches.setter
    def height_inches(self, value: int) -> None:
        value = max(0, min(15 * 12 + 11, int(value)))
        feet, inches = divmod(value, 12)
        self.set_height(feet, inches)

    def set_height(self, feet: int, inches: int) -> None:
        feet = max(0, min(15, int(feet)))
        inches = max(0, min(11, int(inches)))
        self.raw[0x0D] = (feet << 4) | inches

    @property
    def height_text(self) -> str:
        return "%d'%d\"" % (self.height_feet, self.height_remainder)

    @property
    def weight_lbs(self) -> int:
        return self.raw[0x0E] + 100

    @weight_lbs.setter
    def weight_lbs(self, value: int) -> None:
        self.raw[0x0E] = max(0, min(255, int(value) - 100))

    # -- ratings --------------------------------------------------------
    def attribute(self, key: str) -> int:
        return self.raw[ATTRIBUTES_BY_KEY[key].offset]

    def set_attribute(self, key: str, value: int) -> None:
        a = ATTRIBUTES_BY_KEY[key]
        value = max(a.minimum, min(a.maximum, int(value)))
        self.raw[a.offset] = value
        if a.mirror is not None:
            self.raw[a.mirror] = value

    @property
    def attributes(self) -> dict[str, int]:
        return {a.key: self.raw[a.offset] for a in ATTRIBUTES}

    @property
    def shooting_range(self) -> int:
        return self.raw[RANGE_OFFSET]

    @shooting_range.setter
    def shooting_range(self, value: int) -> None:
        self.raw[RANGE_OFFSET] = max(0, min(len(RANGE_FEET) - 1, int(value)))

    @property
    def range_feet(self) -> int:
        return RANGE_FEET[min(self.shooting_range, len(RANGE_FEET) - 1)]

    @property
    def overall(self) -> int:
        """A convenience summary; the game does not store one."""
        vals = [self.raw[a.offset] for a in ATTRIBUTES]
        return round(sum(vals) / len(vals))

    # -- misc / appearance ----------------------------------------------
    def misc(self, key: str) -> int:
        return self.raw[MISC_BY_KEY[key][2]]

    def set_misc(self, key: str, value: int) -> None:
        _, _, off, lo, hi = MISC_BY_KEY[key]
        self.raw[off] = max(lo, min(hi, int(value)))

    @property
    def appearance_bytes(self) -> bytes:
        return bytes(self.raw[o] for o in APPEARANCE_OFFSETS)

    @appearance_bytes.setter
    def appearance_bytes(self, values: bytes) -> None:
        for off, v in zip(APPEARANCE_OFFSETS, values):
            self.raw[off] = v & 0xFF

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<Player %d %s (%s #%d)>" % (
            self.player_id, self.full_name, self.position, self.jersey)


@dataclass
class RosterSlot:
    player_id: int
    starter: int  # 0 = bench, 1-5 = starting five

    def encode(self) -> int:
        return ((self.starter & 0x7F) << 9) | (self.player_id & 0x1FF)

    @classmethod
    def decode(cls, value: int) -> "RosterSlot":
        return cls(player_id=value & 0x1FF, starter=value >> 9)


# ---------------------------------------------------------------------------
# database
# ---------------------------------------------------------------------------

class RosterDatabase:
    """Parsed ``TEAMINFO.IFF``: teams, players, string pool and rosters."""

    def __init__(self, container: iff.Container) -> None:
        self.container = container
        self.chunkfile = iff.parse_chunks(container.payload)

        self.pool = StringPool(self.chunkfile.get(b"CSTR"))

        team_raw = self.chunkfile.get(b"TEAM")
        team_count = struct.unpack_from(">I", team_raw, 0)[0]
        self.teams: list[Team] = []
        for i in range(team_count):
            off = 4 + i * TEAM_RECORD_SIZE
            rec = bytearray(team_raw[off:off + TEAM_RECORD_SIZE])
            city, _, abbrev, name = struct.unpack_from(">IIII", rec, 0)
            self.teams.append(Team(i, self.pool.get(city), self.pool.get(abbrev),
                                   self.pool.get(name), rec))

        plyr = self.chunkfile.get(b"PLYR")
        self.player_capacity, self.player_used = struct.unpack_from(">II", plyr, 0)
        self.players: list[Player] = []
        for i in range(self.player_capacity):
            off = 8 + i * PLAYER_RECORD_SIZE
            self.players.append(
                Player(i, bytearray(plyr[off:off + PLAYER_RECORD_SIZE]), self.pool))

        rost = self.chunkfile.get(b"ROST")
        group_count = struct.unpack_from(">H", rost, 0)[0]
        self.rosters: list[list[RosterSlot]] = []
        pos = 2
        for _ in range(group_count):
            n = struct.unpack_from(">H", rost, pos)[0]
            pos += 2
            group = [RosterSlot.decode(struct.unpack_from(">H", rost, pos + 2 * k)[0])
                     for k in range(n)]
            pos += 2 * n
            self.rosters.append(group)

    # -- lookups --------------------------------------------------------
    def by_id(self, player_id: int) -> Player | None:
        idx = player_id - 1
        if 0 <= idx < len(self.players) and self.players[idx].player_id == player_id:
            return self.players[idx]
        for p in self.players:
            if p.player_id == player_id:
                return p
        return None

    def team_name(self, index: int) -> str:
        if 0 <= index < len(self.teams):
            return self.teams[index].display
        return "Team %d" % index

    def roster(self, team_index: int) -> list[RosterSlot]:
        if 0 <= team_index < len(self.rosters):
            return self.rosters[team_index]
        return []

    def real_players(self) -> list[Player]:
        return self.players[:self.player_used]

    # -- roster edits ---------------------------------------------------
    def _remove_from_rosters(self, player_id: int, keep_allstar: bool = True) -> None:
        for idx, group in enumerate(self.rosters):
            if keep_allstar and idx in (30, 31):
                continue  # All-Star squads are selections, not employment
            self.rosters[idx] = [s for s in group if s.player_id != player_id]

    def assign_team(self, player: Player, team_index: int, starter: int = 0) -> None:
        """Move a player onto a team, updating both the record and ``ROST``."""
        if not 0 <= team_index < len(self.rosters):
            raise ValueError("no roster group for team %d" % team_index)
        self._remove_from_rosters(player.player_id)
        player.team = team_index
        if team_index != FREE_AGENT_TEAM:
            self.rosters[team_index].append(RosterSlot(player.player_id, starter))
            self.rosters[team_index].sort(key=lambda s: s.player_id)
        else:
            self.rosters[FREE_AGENT_TEAM].append(RosterSlot(player.player_id, 0))
            self.rosters[FREE_AGENT_TEAM].sort(key=lambda s: s.player_id)

    def release(self, player: Player) -> None:
        """Drop a player to the Free Agents pool."""
        self.assign_team(player, FREE_AGENT_TEAM)

    def trade(self, a: Player, b: Player) -> None:
        """Swap two players between their teams, keeping both rosters at size."""
        team_a, team_b = a.team, b.team
        slot_a = self.starter_slot(a)
        slot_b = self.starter_slot(b)
        self.assign_team(a, team_b, slot_b)
        self.assign_team(b, team_a, slot_a)

    def starter_slot(self, player: Player) -> int:
        for s in self.roster(player.team):
            if s.player_id == player.player_id:
                return s.starter
        return 0

    def set_starter_slot(self, player: Player, starter: int) -> None:
        """Put a player in (1-5) or out of (0) his team's starting five."""
        starter = max(0, min(5, int(starter)))
        group = self.roster(player.team)
        for s in group:
            if s.player_id == player.player_id:
                continue
            if starter and s.starter == starter:
                s.starter = 0  # only one player per starting slot
        for s in group:
            if s.player_id == player.player_id:
                s.starter = starter
                return
        raise ValueError("%s is not on %s" % (player.full_name, self.team_name(player.team)))

    # -- validation -----------------------------------------------------
    def problems(self) -> list[str]:
        """Sanity checks worth surfacing before writing a ROM."""
        out: list[str] = []
        for i, team in enumerate(self.teams):
            if not team.is_nba:
                continue
            group = self.roster(i)
            if len(group) != 12:
                out.append("%s has %d players (the game ships 12 per team)"
                           % (team.display, len(group)))
            starters = sorted(s.starter for s in group if s.starter)
            if starters != [1, 2, 3, 4, 5]:
                out.append("%s does not have exactly one player in each of the five "
                           "starting slots (found %s)" % (team.display, starters or "none"))
        for team_index, group in enumerate(self.rosters):
            for s in group:
                p = self.by_id(s.player_id)
                if p is None:
                    continue
                if team_index in (30, 31) or team_index >= len(self.teams):
                    continue
                if p.team != team_index:
                    out.append("%s is listed on %s but his record says %s"
                               % (p.full_name, self.team_name(team_index),
                                  self.team_name(p.team)))
        return out

    # -- serialisation --------------------------------------------------
    def _build_rost(self) -> bytes:
        out = bytearray(struct.pack(">H", len(self.rosters)))
        for group in self.rosters:
            out += struct.pack(">H", len(group))
            for s in group:
                out += struct.pack(">H", s.encode())
        return bytes(out)

    def _build_team(self) -> bytes:
        out = bytearray(struct.pack(">I", len(self.teams)))
        for t in self.teams:
            struct.pack_into(">I", t.raw, 0x00, self.pool.intern(t.city))
            struct.pack_into(">I", t.raw, 0x08, self.pool.intern(t.abbreviation))
            struct.pack_into(">I", t.raw, 0x0C, self.pool.intern(t.name))
            out += t.raw
        return bytes(out)

    def _build_plyr(self) -> bytes:
        out = bytearray(struct.pack(">II", self.player_capacity, self.player_used))
        for p in self.players:
            out += p.raw
        return bytes(out)

    def to_file(self) -> bytes:
        """Serialise back into a packed ``TEAMINFO.IFF``."""
        # TEAM and PLYR may intern new strings, so build them before CSTR.
        team_blob = self._build_team()
        plyr_blob = self._build_plyr()
        self.chunkfile.set(b"CSTR", self.pool.to_bytes())
        self.chunkfile.set(b"TEAM", team_blob)
        self.chunkfile.set(b"ROST", self._build_rost())
        self.chunkfile.set(b"PLYR", plyr_blob)
        payload = iff.build_chunks(self.chunkfile)
        return iff.pack(payload, magic=self.container.magic, original=self.container)


def load(blob: bytes) -> RosterDatabase:
    return RosterDatabase(iff.unpack(blob))
