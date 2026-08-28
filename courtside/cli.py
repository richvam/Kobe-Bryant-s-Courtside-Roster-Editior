"""Command line front end: ``python -m courtside ...``"""

from __future__ import annotations

import argparse
import os
import re
import sys

from . import __version__, roster
from .editor import EditorError, RosterEditor
from .rom import RomError

HEIGHT_RE = re.compile(r"^\s*(\d+)\s*(?:'|ft|feet)?\s*(?:(\d+)\s*(?:\"|in|inches)?)?\s*$")


def parse_height(text: str) -> int:
    """Accept ``6'6"``, ``6-6``, ``78`` or ``78in`` and return inches."""
    text = text.strip().replace("-", "'")
    if text.isdigit() and int(text) > 12:
        return int(text)
    m = HEIGHT_RE.match(text)
    if not m:
        raise argparse.ArgumentTypeError("cannot read height %r (try 6'6\" or 78)" % text)
    feet = int(m.group(1))
    inches = int(m.group(2) or 0)
    return feet * 12 + inches


# --------------------------------------------------------------------------
# output helpers
# --------------------------------------------------------------------------

def player_line(ed: RosterEditor, p: roster.Player) -> str:
    star = "*" if ed.db.starter_slot(p) else " "
    return "%s%4d  %-24s %-3s #%-3s %-5s %3dlb  %-14s ovr %3d" % (
        star, p.player_id, p.full_name, p.position, p.jersey_text,
        p.height_text, p.weight_lbs, ed.db.team_name(p.team)[:14], p.overall)


def print_card(ed: RosterEditor, p: roster.Player) -> None:
    slot = ed.db.starter_slot(p)
    print("%s  (#%s, %s)" % (p.full_name, p.jersey_text, p.position))
    print("  id %-6d team %-18s %s" % (
        p.player_id, ed.db.team_name(p.team),
        "starter %d" % slot if slot else "bench"))
    print("  experience: %s" % p.years_pro_text)
    print("  %s, %d lb   shooting range %d ft" % (p.height_text, p.weight_lbs, p.range_feet))
    print("  ratings:")
    for a in roster.ATTRIBUTES:
        value = p.attribute(a.key)
        bar = "#" * round(value / 5)
        print("    %-14s %3d  %s" % (a.label, value, bar))
    misc = "  ".join("%s=%d" % (key, p.misc(key)) for key, *_ in roster.MISC_FIELDS)
    print("  other bytes: %s" % misc)


def save_and_report(ed: RosterEditor, out: str) -> int:
    report = ed.save(out)
    print("wrote %s" % report.path)
    print("  TEAMINFO.IFF %s" % report.teaminfo)
    if report.teamdata:
        print("  TEAMDATA.IFF %s" % report.teamdata)
    print("  CRC fixed: 0x%08X 0x%08X" % report.crc)
    for w in report.warnings:
        print("  warning: %s" % w, file=sys.stderr)
    return 0


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def cmd_info(ed: RosterEditor, args) -> int:
    rom = ed.rom
    print("%s  [%s]  CIC %d  %.1f MiB" % (
        rom.title, rom.cart_id, rom.cic, len(rom.data) / (1024 * 1024)))
    print("packed files: %d (table at 0x%X, data base 0x%X)"
          % (rom.fs.count, rom.fs.table_offset, rom.fs.base))
    print("teams: %d   players: %d used of %d slots"
          % (len(ed.db.teams), ed.db.player_used, ed.db.player_capacity))
    problems = ed.db.problems()
    print("roster consistency: %s" % ("OK" if not problems else "%d issue(s)" % len(problems)))
    for p in problems:
        print("  - %s" % p)
    return 0


def cmd_teams(ed: RosterEditor, args) -> int:
    for t in ed.db.teams:
        group = ed.db.roster(t.index)
        if args.nba_only and not t.is_nba:
            continue
        print("%2d  %-14s %-5s %2d players" % (t.index, t.display, t.abbreviation, len(group)))
        if args.verbose:
            for s in sorted(group, key=lambda s: (-s.starter or 9, s.player_id)):
                p = ed.db.by_id(s.player_id)
                if p is None:
                    print("      (id %d - no such player)" % s.player_id)
                    continue
                print("     %s" % player_line(ed, p))
    return 0


def cmd_list(ed: RosterEditor, args) -> int:
    players = ed.db.players if args.all else ed.db.real_players()
    if args.team:
        idx = ed.team_index(args.team)
        players = [p for p in players if p.team == idx]
    if args.search:
        needle = args.search.lower()
        players = [p for p in players if needle in p.full_name.lower()]
    if args.position:
        want = args.position.upper()
        players = [p for p in players if p.position == want]
    key = {"name": lambda p: p.last_name.lower(),
           "overall": lambda p: -p.overall,
           "team": lambda p: (p.team, p.last_name.lower()),
           "id": lambda p: p.player_id,
           "years": lambda p: -p.years_pro}[args.sort]
    for p in sorted(players, key=key):
        print(player_line(ed, p))
    print("%d player(s)" % len(players))
    return 0


def cmd_show(ed: RosterEditor, args) -> int:
    for needle in args.player:
        for p in ed.find(needle) or []:
            print_card(ed, p)
            print()
    return 0


def cmd_set(ed: RosterEditor, args) -> int:
    p = ed.one(args.player)
    if args.first_name:
        p.first_name = args.first_name
    if args.last_name:
        p.last_name = args.last_name
    if args.name:
        first, _, last = args.name.partition(" ")
        p.first_name, p.last_name = first, last or first
    if args.jersey is not None:
        p.jersey = args.jersey
    if args.position:
        p.position = args.position
    if args.years_pro is not None:
        p.years_pro = args.years_pro
    if args.height:
        p.height_inches = parse_height(args.height)
    if args.weight is not None:
        p.weight_lbs = args.weight
    if args.range is not None:
        p.shooting_range = args.range
    if args.starter is not None:
        ed.db.set_starter_slot(p, args.starter)
    for pair in args.attr or []:
        key, _, value = pair.partition("=")
        key = key.strip().lower().replace(" ", "_").replace("-", "_")
        if key in roster.ATTRIBUTES_BY_KEY:
            p.set_attribute(key, int(value))
        elif key in roster.MISC_BY_KEY:
            p.set_misc(key, int(value))
        else:
            raise EditorError("unknown attribute %r; known: %s" % (
                key, ", ".join(list(roster.ATTRIBUTES_BY_KEY) + list(roster.MISC_BY_KEY))))
    ed.dirty = True
    print_card(ed, p)
    return save_and_report(ed, args.output)


def cmd_trade(ed: RosterEditor, args) -> int:
    a, b = ed.trade(args.player_a, args.player_b)
    print("%s -> %s" % (a.full_name, ed.db.team_name(a.team)))
    print("%s -> %s" % (b.full_name, ed.db.team_name(b.team)))
    return save_and_report(ed, args.output)


def cmd_move(ed: RosterEditor, args) -> int:
    p = ed.move(args.player, args.team, args.starter or 0)
    print("%s -> %s" % (p.full_name, ed.db.team_name(p.team)))
    return save_and_report(ed, args.output)


def cmd_release(ed: RosterEditor, args) -> int:
    p = ed.release(args.player)
    print("%s released to %s" % (p.full_name, ed.db.team_name(p.team)))
    return save_and_report(ed, args.output)


def cmd_appearance(ed: RosterEditor, args) -> int:
    if args.swap:
        a, b = ed.swap_appearance(args.target, args.source)
        print("swapped the likenesses of %s and %s" % (a.full_name, b.full_name))
    else:
        d, s = ed.reassign_appearance(
            args.target, args.source,
            model_bytes=not args.no_model, face=not args.no_face, photo=not args.no_photo)
        print("%s now looks like %s" % (d.full_name, s.full_name))
    return save_and_report(ed, args.output)


def cmd_export(ed: RosterEditor, args) -> int:
    ed.export_json(args.output)
    print("exported %d players and %d teams to %s"
          % (len(ed.db.players), len(ed.db.teams), args.output))
    return 0


def cmd_import(ed: RosterEditor, args) -> int:
    notes = ed.import_json(args.input)
    for n in notes:
        print("note: %s" % n, file=sys.stderr)
    return save_and_report(ed, args.output)


def cmd_photo(ed: RosterEditor, args) -> int:
    """Save a player's roster photo, or every player's."""
    if args.all:
        players = ed.db.players
        if args.team:
            idx = ed.team_index(args.team)
            players = [p for p in players if p.team == idx]
        written = ed.export_all_photos(args.output, scale=args.scale, players=players)
        print("wrote %d photo(s) to %s" % (len(written), args.output))
        return 0
    if not args.player:
        raise EditorError("name a player, or pass --all to export every photo")
    p = ed.export_photo(args.player, args.output, scale=args.scale)
    print("wrote %s (%s)" % (args.output, p.full_name))
    return 0


def cmd_setphoto(ed: RosterEditor, args) -> int:
    p, bank = ed.import_photo(args.player, args.image, mode=args.fit,
                              dither=args.dither)
    print("%s now uses %s (palette bank %d)"
          % (p.full_name, os.path.basename(args.image), bank))
    return save_and_report(ed, args.output)


def cmd_gui(ed: RosterEditor, args) -> int:
    from .gui import main as gui_main
    return gui_main([ed.rom.path or ""])


def cmd_verify(ed: RosterEditor, args) -> int:
    """Re-pack every packed file and confirm it round-trips byte for byte."""
    ok = True
    info = ed.rom.read("TEAMINFO.IFF")
    if ed.db.to_file() != info:
        ok = False
        print("TEAMINFO.IFF does not round-trip", file=sys.stderr)
    else:
        print("TEAMINFO.IFF round-trips exactly (%d bytes)" % len(info))
    data = ed.rom.read("TEAMDATA.IFF")
    if ed.appearance.to_file() != data:
        ok = False
        print("TEAMDATA.IFF does not round-trip", file=sys.stderr)
    else:
        print("TEAMDATA.IFF round-trips exactly (%d bytes)" % len(data))
    layout = ed.rom.verify()
    if layout:
        ok = False
        for problem in layout:
            print("layout: %s" % problem, file=sys.stderr)
    else:
        print("packed files sit where the boot scan looks for them "
              "(data base 0x%X)" % ed.rom.fs.base)
    from .rom import calc_crc
    have = tuple(int.from_bytes(ed.rom.data[0x10 + i * 4:0x14 + i * 4], "big")
                 for i in range(2))
    if calc_crc(bytes(ed.rom.data), ed.rom.cic) == have:
        print("header CRC matches (CIC %d)" % ed.rom.cic)
    else:
        ok = False
        print("header CRC mismatch", file=sys.stderr)
    return 0 if ok else 1


# --------------------------------------------------------------------------
# argument parsing
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="courtside",
        description="Roster editor for Kobe Bryant's NBA Courtside (N64).")
    ap.add_argument("--version", action="version", version="courtside %s" % __version__)
    ap.add_argument("rom", help="path to the .z64/.n64/.v64 ROM")
    sub = ap.add_subparsers(dest="command", required=True)

    def with_output(p):
        p.add_argument("-o", "--output", required=True, help="path for the edited ROM")
        return p

    sub.add_parser("info", help="summarise the ROM and roster").set_defaults(func=cmd_info)

    p = sub.add_parser("teams", help="list teams and their rosters")
    p.add_argument("-v", "--verbose", action="store_true", help="include each roster")
    p.add_argument("--nba-only", action="store_true", help="skip All-Star and bonus teams")
    p.set_defaults(func=cmd_teams)

    p = sub.add_parser("list", help="list players")
    p.add_argument("--team", help="team name, abbreviation or index")
    p.add_argument("--search", help="substring of the player's name")
    p.add_argument("--position", help="exact position code, e.g. SG")
    p.add_argument("--all", action="store_true", help="include the bonus/dev roster slots")
    p.add_argument("--sort", choices=("name", "overall", "team", "id", "years"),
                   default="name")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("show", help="print a full player card")
    p.add_argument("player", nargs="+")
    p.set_defaults(func=cmd_show)

    p = with_output(sub.add_parser("set", help="edit one player"))
    p.add_argument("player")
    p.add_argument("--name", help='full name, e.g. "Kobe Bryant"')
    p.add_argument("--first-name")
    p.add_argument("--last-name")
    p.add_argument("--jersey", type=int)
    p.add_argument("--position", help="one of %s" % ", ".join(roster.POSITION_CODES))
    p.add_argument("--years-pro", type=int, metavar="N",
                   help="seasons completed before this one; 0 is a rookie")
    p.add_argument("--height", help="e.g. 6'6\" or 78")
    p.add_argument("--weight", type=int, help="pounds")
    p.add_argument("--range", type=int, choices=range(5),
                   help="shooting range index: %s"
                        % ", ".join("%d=%dft" % (i, f) for i, f in enumerate(roster.RANGE_FEET)))
    p.add_argument("--starter", type=int, choices=range(6),
                   help="0 for bench, 1-5 for a starting slot")
    p.add_argument("--attr", action="append", metavar="KEY=VALUE",
                   help="set a rating, e.g. --attr dunking=99 (repeatable)")
    p.set_defaults(func=cmd_set)

    p = with_output(sub.add_parser("trade", help="swap two players between their teams"))
    p.add_argument("player_a")
    p.add_argument("player_b")
    p.set_defaults(func=cmd_trade)

    p = with_output(sub.add_parser("move", help="sign a player to a team"))
    p.add_argument("player")
    p.add_argument("team")
    p.add_argument("--starter", type=int, choices=range(6))
    p.set_defaults(func=cmd_move)

    p = with_output(sub.add_parser("release", help="drop a player to free agency"))
    p.add_argument("player")
    p.set_defaults(func=cmd_release)

    p = with_output(sub.add_parser(
        "appearance", help="give one player another player's likeness"))
    p.add_argument("target", help="the player who changes")
    p.add_argument("source", help="the player whose look is copied")
    p.add_argument("--swap", action="store_true", help="exchange looks instead of copying")
    p.add_argument("--no-face", action="store_true", help="leave the 3D face texture alone")
    p.add_argument("--no-photo", action="store_true", help="leave the roster photo alone")
    p.add_argument("--no-model", action="store_true", help="leave the model bytes alone")
    p.set_defaults(func=cmd_appearance)

    p = sub.add_parser("export", help="dump the roster to JSON")
    p.add_argument("-o", "--output", required=True)
    p.set_defaults(func=cmd_export)

    p = with_output(sub.add_parser("import", help="apply a JSON roster to the ROM"))
    p.add_argument("input")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("photo", help="save roster photos as PNG files")
    p.add_argument("player", nargs="?", help="omit when using --all")
    p.add_argument("-o", "--output", required=True,
                   help="PNG file, or a directory when using --all")
    p.add_argument("--all", action="store_true",
                   help="export every photo into the output directory")
    p.add_argument("--team", help="with --all, limit to one team")
    p.add_argument("--scale", type=int, default=4, help="pixel zoom (default 4)")
    p.set_defaults(func=cmd_photo)

    p = with_output(sub.add_parser(
        "setphoto", help="put your own picture on a player's roster card"))
    p.add_argument("player")
    p.add_argument("image", help="PNG or BMP; other formats need Pillow installed")
    p.add_argument("--fit", choices=("crop", "stretch"), default="crop",
                   help="crop keeps proportions (default), stretch fills the frame")
    p.add_argument("--dither", action="store_true",
                   help="diffuse quantisation error; adds speckle on flat colours")
    p.set_defaults(func=cmd_setphoto)

    sub.add_parser("gui", help="open the offline desktop editor (Tkinter)")\
        .set_defaults(func=cmd_gui)

    sub.add_parser("verify", help="check the codec round-trips this ROM exactly")\
        .set_defaults(func=cmd_verify)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        ed = RosterEditor.open(args.rom)
        return args.func(ed, args)
    except (EditorError, RomError, ValueError, KeyError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        # someone closed the pipe (`| head`); point stdout at /dev/null so the
        # interpreter's shutdown flush cannot fail, then leave quietly
        os.dup2(os.open(os.devnull, os.O_WRONLY), 1)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
