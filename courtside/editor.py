"""High level roster editor: load a ROM, change things, write a new ROM."""

from __future__ import annotations

import json
from dataclasses import dataclass

from . import announcer, appearance, roster
from .rom import Rom

TEAMINFO = "TEAMINFO.IFF"
TEAMDATA = "TEAMDATA.IFF"
TEAMTALK = "TEAMTALK.IFF"

SUPPORTED_CARTS = {"NNBE", "NNBP", "NNBJ"}


class EditorError(ValueError):
    pass


@dataclass
class SaveReport:
    path: str
    teaminfo: str
    teamdata: str | None
    teamtalk: str | None
    crc: tuple[int, int]
    warnings: list[str]


class RosterEditor:
    """Everything the UI and CLI drive."""

    def __init__(self, rom: Rom) -> None:
        self.rom = rom
        if TEAMINFO not in rom.fs:
            raise EditorError(
                "this ROM has no %s - it does not look like NBA Courtside" % TEAMINFO)
        self.db = roster.load(rom.read(TEAMINFO))
        self._appearance: appearance.AppearanceDatabase | None = None
        self._appearance_dirty = False
        self._announcer: announcer.AnnouncerDatabase | None = None
        self._announcer_dirty = False
        self.dirty = False

    @classmethod
    def open(cls, path: str) -> "RosterEditor":
        return cls(Rom.load(path))

    @property
    def has_changes(self) -> bool:
        return self.dirty or self._appearance_dirty or self._announcer_dirty

    # -- lazy appearance database ---------------------------------------
    @property
    def appearance(self) -> appearance.AppearanceDatabase:
        if self._appearance is None:
            if TEAMDATA not in self.rom.fs:
                raise EditorError("this ROM has no %s" % TEAMDATA)
            self._appearance = appearance.load(self.rom.read(TEAMDATA))
        return self._appearance

    @property
    def announcer(self) -> announcer.AnnouncerDatabase:
        if self._announcer is None:
            if TEAMTALK not in self.rom.fs:
                raise EditorError("this ROM has no %s" % TEAMTALK)
            self._announcer = announcer.load(self.rom.read(TEAMTALK))
        return self._announcer

    # -- lookups --------------------------------------------------------
    def find(self, needle: str) -> list[roster.Player]:
        needle = needle.strip().lower()
        if not needle:
            return []
        if needle.isdigit():
            p = self.db.by_id(int(needle))
            if p is not None:
                return [p]
        return [p for p in self.db.players if needle in p.full_name.lower()]

    def one(self, needle: str) -> roster.Player:
        hits = self.find(needle)
        if not hits:
            raise EditorError("no player matches %r" % needle)
        if len(hits) > 1:
            names = ", ".join(p.full_name for p in hits[:8])
            raise EditorError("%r matches %d players: %s" % (needle, len(hits), names))
        return hits[0]

    def team_index(self, needle: str) -> int:
        needle = needle.strip().lower()
        if needle.isdigit():
            return int(needle)
        for t in self.db.teams:
            if needle in (t.display.lower(), t.city.lower(), t.abbreviation.lower()):
                return t.index
        for t in self.db.teams:
            if needle and needle in t.display.lower():
                return t.index
        raise EditorError("no team matches %r" % needle)

    # -- edits ----------------------------------------------------------
    def trade(self, a: str, b: str) -> tuple[roster.Player, roster.Player]:
        pa, pb = self.one(a), self.one(b)
        if pa.player_id == pb.player_id:
            raise EditorError("cannot trade a player for himself")
        self.db.trade(pa, pb)
        self.dirty = True
        return pa, pb

    def move(self, player: str, team: str, starter: int = 0) -> roster.Player:
        p = self.one(player)
        self.db.assign_team(p, self.team_index(team), starter)
        self.dirty = True
        return p

    def release(self, player: str) -> roster.Player:
        p = self.one(player)
        self.db.release(p)
        self.dirty = True
        return p

    def reassign_appearance(self, dst: str, src: str, model_bytes: bool = True,
                            face: bool = True, photo: bool = True) -> tuple[roster.Player, roster.Player]:
        """Give ``dst`` the likeness of ``src``."""
        pd, ps = self.one(dst), self.one(src)
        if model_bytes:
            pd.appearance_bytes = ps.appearance_bytes
            self.dirty = True
        if face or photo:
            app = self.appearance
            if face:
                app.copy_face(pd.player_id, ps.player_id)
            if photo:
                app.copy_photo(pd.player_id, ps.player_id)
            self._appearance_dirty = True
        return pd, ps

    def swap_appearance(self, a: str, b: str) -> tuple[roster.Player, roster.Player]:
        pa, pb = self.one(a), self.one(b)
        ba, bb = pa.appearance_bytes, pb.appearance_bytes
        pa.appearance_bytes, pb.appearance_bytes = bb, ba
        self.appearance.swap_likeness(pa.player_id, pb.player_id)
        self.dirty = True
        self._appearance_dirty = True
        return pa, pb

    # -- the PA announcer -------------------------------------------------
    def reassign_announcer(self, dst: str, src: str, given_name: bool = True,
                           surname: bool = True) -> tuple[roster.Player, roster.Player]:
        """Make the announcer call ``dst`` by ``src``'s name."""
        pd, ps = self.one(dst), self.one(src)
        self.announcer.copy_call(pd.player_id, ps.player_id,
                                 given_name=given_name, surname=surname)
        self._announcer_dirty = True
        return pd, ps

    def swap_announcer(self, a: str, b: str) -> tuple[roster.Player, roster.Player]:
        pa, pb = self.one(a), self.one(b)
        self.announcer.swap_call(pa.player_id, pb.player_id)
        self._announcer_dirty = True
        return pa, pb

    def silence_announcer(self, player: str) -> roster.Player:
        p = self.one(player)
        self.announcer.silence(p.player_id)
        self._announcer_dirty = True
        return p

    # -- photographs -----------------------------------------------------
    def import_photo(self, player: str, path: str, mode: str = "crop",
                     dither: bool = False) -> tuple[roster.Player, int]:
        """Replace a player's roster photo with an image from disk."""
        p = self.one(player)
        bank = self.appearance.import_photo(p.player_id, path, mode=mode, dither=dither)
        self._appearance_dirty = True
        return p, bank

    def export_photo(self, player: str, path: str, scale: int = 4) -> roster.Player:
        p = self.one(player)
        png = self.appearance.photo_png(p.player_id, scale=scale)
        if png is None:
            raise EditorError("%s has no roster photo" % p.full_name)
        with open(path, "wb") as fh:
            fh.write(png)
        return p

    def export_all_photos(self, directory: str, scale: int = 4,
                          players: list[roster.Player] | None = None) -> list[str]:
        """Write every roster photo into ``directory`` as a PNG."""
        import os
        import re

        os.makedirs(directory, exist_ok=True)
        written: list[str] = []
        for p in (players if players is not None else self.db.players):
            png = self.appearance.photo_png(p.player_id, scale=scale)
            if png is None:
                continue
            safe = re.sub(r"[^A-Za-z0-9._-]+", "-", p.full_name).strip("-") or "player"
            path = os.path.join(directory, "%03d-%s.png" % (p.player_id, safe))
            with open(path, "wb") as fh:
                fh.write(png)
            written.append(path)
        return written

    # -- import / export -------------------------------------------------
    def export_dict(self) -> dict:
        return {
            "rom": {"title": self.rom.title, "cart": self.rom.cart_id, "cic": self.rom.cic},
            "teams": [
                {
                    "index": t.index,
                    "city": t.city,
                    "abbreviation": t.abbreviation,
                    "name": t.name,
                    "roster": [
                        {"player_id": s.player_id, "starter": s.starter}
                        for s in self.db.roster(t.index)
                    ],
                }
                for t in self.db.teams
            ],
            "players": [self.player_dict(p) for p in self.db.players],
        }

    def player_dict(self, p: roster.Player) -> dict:
        return {
            "id": p.player_id,
            "index": p.index,
            "first_name": p.first_name,
            "last_name": p.last_name,
            "team": p.team,
            "team_name": self.db.team_name(p.team),
            "jersey": p.jersey,
            "jersey_text": p.jersey_text,
            "position": p.position,
            "years_pro": p.years_pro,
            "years_pro_text": p.years_pro_text,
            "height_inches": p.height_inches,
            "height_text": p.height_text,
            "weight_lbs": p.weight_lbs,
            "range": p.shooting_range,
            "range_feet": p.range_feet,
            "starter": self.db.starter_slot(p),
            "overall": p.overall,
            "attributes": p.attributes,
            "misc": {k: p.misc(k) for k, *_ in roster.MISC_FIELDS},
        }

    def apply_dict(self, blob: dict) -> list[str]:
        """Apply an exported roster back onto the loaded ROM."""
        notes: list[str] = []
        for entry in blob.get("players", []):
            p = self.db.by_id(int(entry["id"]))
            if p is None:
                notes.append("no player with id %s" % entry["id"])
                continue
            self.apply_player_dict(p, entry)
        for team in blob.get("teams", []):
            idx = int(team["index"])
            if not 0 <= idx < len(self.db.rosters):
                continue
            if "roster" in team:
                self.db.rosters[idx] = [
                    roster.RosterSlot(int(s["player_id"]), int(s.get("starter", 0)))
                    for s in team["roster"]
                ]
            t = self.db.teams[idx] if idx < len(self.db.teams) else None
            if t is not None:
                t.city = team.get("city", t.city)
                t.abbreviation = team.get("abbreviation", t.abbreviation)
                t.name = team.get("name", t.name)
        self.dirty = True
        return notes

    def apply_player_dict(self, p: roster.Player, entry: dict) -> None:
        if "first_name" in entry:
            p.first_name = entry["first_name"]
        if "last_name" in entry:
            p.last_name = entry["last_name"]
        if "team" in entry:
            p.team = int(entry["team"])
        if "jersey" in entry:
            p.jersey = int(entry["jersey"])
        if entry.get("position"):
            p.position = entry["position"]
        if "years_pro" in entry:
            p.years_pro = int(entry["years_pro"])
        if "height_inches" in entry:
            p.height_inches = int(entry["height_inches"])
        if "weight_lbs" in entry:
            p.weight_lbs = int(entry["weight_lbs"])
        if "range" in entry:
            p.shooting_range = int(entry["range"])
        for key, value in (entry.get("attributes") or {}).items():
            if key in roster.ATTRIBUTES_BY_KEY:
                p.set_attribute(key, int(value))
        for key, value in (entry.get("misc") or {}).items():
            if key in roster.MISC_BY_KEY:
                p.set_misc(key, int(value))
        self.dirty = True

    def export_json(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.export_dict(), fh, indent=2)

    def import_json(self, path: str) -> list[str]:
        with open(path, encoding="utf-8") as fh:
            return self.apply_dict(json.load(fh))

    # -- output ---------------------------------------------------------
    def save(self, path: str) -> SaveReport:
        warnings = self.db.problems()
        teaminfo = self.rom.write(TEAMINFO, self.db.to_file())
        teamdata = None
        if self._appearance is not None and self._appearance_dirty:
            teamdata = self.rom.write(TEAMDATA, self._appearance.to_file())
        teamtalk = None
        if self._announcer is not None and self._announcer_dirty:
            teamtalk = self.rom.write(TEAMTALK, self._announcer.to_file())
        crc = self.rom.save(path)
        return SaveReport(path=path, teaminfo=teaminfo, teamdata=teamdata,
                          teamtalk=teamtalk, crc=crc, warnings=warnings)
