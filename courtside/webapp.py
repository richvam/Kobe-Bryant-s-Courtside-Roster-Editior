"""A local web front end for the roster editor.

Runs on the standard library alone: ``python -m courtside ROM serve`` starts a
loopback HTTP server and opens the single-page UI in a browser.
"""

from __future__ import annotations

import json
import os
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from . import roster
from .editor import EditorError, RosterEditor

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


class EditorService:
    """Thread-safe wrapper around a :class:`RosterEditor`."""

    def __init__(self, editor: RosterEditor) -> None:
        self.editor = editor
        self.lock = threading.Lock()
        self.source_path = getattr(editor.rom, "path", None)

    # -- reads ----------------------------------------------------------
    def state(self) -> dict:
        ed = self.editor
        db = ed.db
        return {
            "rom": {
                "title": ed.rom.title,
                "cart": ed.rom.cart_id,
                "cic": ed.rom.cic,
                "size": len(ed.rom.data),
                "source": self.source_path or "",
            },
            "dirty": ed.has_changes,
            "teams": [
                {
                    "index": t.index,
                    "city": t.city,
                    "abbreviation": t.abbreviation,
                    "name": t.name,
                    "display": t.display,
                    "is_nba": t.is_nba,
                    "count": len(db.roster(t.index)),
                }
                for t in db.teams
            ],
            "players": [ed.player_dict(p) for p in db.players],
            "problems": db.problems(),
            "meta": {
                "attributes": [
                    {"key": a.key, "label": a.label, "min": a.minimum, "max": a.maximum}
                    for a in roster.ATTRIBUTES
                ],
                "misc": [
                    {"key": k, "label": label, "min": lo, "max": hi}
                    for k, label, _off, lo, hi in roster.MISC_FIELDS
                ],
                "positions": [
                    {"code": code, "label": roster.POSITION_NAMES[code]}
                    for code in roster.POSITION_CODES
                ],
                "range_feet": list(roster.RANGE_FEET),
                "player_used": db.player_used,
            },
        }

    # -- writes ---------------------------------------------------------
    def _player(self, player_id: int) -> roster.Player:
        p = self.editor.db.by_id(int(player_id))
        if p is None:
            raise ApiError("no player with id %s" % player_id, 404)
        return p

    def patch_player(self, player_id: int, body: dict) -> dict:
        ed = self.editor
        p = self._player(player_id)
        if "starter" in body:
            starter = int(body.pop("starter"))
            try:
                ed.db.set_starter_slot(p, starter)
            except ValueError as exc:
                raise ApiError(str(exc)) from None
        if "team" in body:
            team = int(body.pop("team"))
            if team != p.team:
                ed.db.assign_team(p, team, 0)
        if "height_feet" in body or "height_inches_part" in body:
            feet = int(body.pop("height_feet", p.height_feet))
            inches = int(body.pop("height_inches_part", p.height_remainder))
            p.set_height(feet, inches)
        ed.apply_player_dict(p, body)
        return ed.player_dict(p)

    def action(self, body: dict) -> dict:
        ed = self.editor
        kind = body.get("action")
        if kind == "trade":
            a, b = ed.trade(str(body["a"]), str(body["b"]))
            return {"message": "%s and %s swapped teams" % (a.full_name, b.full_name)}
        if kind == "move":
            p = ed.move(str(body["player"]), str(body["team"]), int(body.get("starter", 0)))
            return {"message": "%s signed with %s" % (p.full_name, ed.db.team_name(p.team))}
        if kind == "release":
            p = ed.release(str(body["player"]))
            return {"message": "%s released to free agency" % p.full_name}
        if kind == "appearance":
            d, s = ed.reassign_appearance(
                str(body["target"]), str(body["source"]),
                model_bytes=bool(body.get("model", True)),
                face=bool(body.get("face", True)),
                photo=bool(body.get("photo", True)))
            return {"message": "%s now looks like %s" % (d.full_name, s.full_name)}
        if kind == "swap_appearance":
            a, b = ed.swap_appearance(str(body["target"]), str(body["source"]))
            return {"message": "%s and %s swapped looks" % (a.full_name, b.full_name)}
        raise ApiError("unknown action %r" % kind)

    def save(self, path: str) -> dict:
        if not path:
            raise ApiError("a destination path is required")
        report = self.editor.save(path)
        return {
            "path": report.path,
            "teaminfo": report.teaminfo,
            "teamdata": report.teamdata,
            "crc": ["0x%08X" % c for c in report.crc],
            "warnings": report.warnings,
        }

    def photo_png(self, player_id: int) -> bytes | None:
        return self.editor.appearance.photo_png(int(player_id), scale=3)


def _read_web(name: str) -> bytes:
    path = os.path.join(WEB_DIR, os.path.basename(name))
    with open(path, "rb") as fh:
        return fh.read()


def make_handler(service: EditorService):
    class Handler(BaseHTTPRequestHandler):
        server_version = "courtside"

        def log_message(self, fmt, *args):  # keep the console quiet
            pass

        # -- helpers ----------------------------------------------------
        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, payload, status: int = 200) -> None:
            self._send(status, json.dumps(payload).encode("utf-8"), "application/json")

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            return json.loads(self.rfile.read(length).decode("utf-8"))

        # -- routes -----------------------------------------------------
        def do_GET(self):  # noqa: N802
            path = urlparse(self.path).path
            try:
                if path in ("/", "/index.html"):
                    return self._send(200, _read_web("index.html"), "text/html; charset=utf-8")
                if path == "/app.js":
                    return self._send(200, _read_web("app.js"),
                                      "text/javascript; charset=utf-8")
                if path == "/style.css":
                    return self._send(200, _read_web("style.css"), "text/css; charset=utf-8")
                if path == "/api/state":
                    with service.lock:
                        return self._json(service.state())
                if path == "/api/export":
                    with service.lock:
                        return self._json(service.editor.export_dict())
                if path.startswith("/api/photo/"):
                    pid = path.rsplit("/", 1)[-1].split(".")[0]
                    with service.lock:
                        png = service.photo_png(int(pid))
                    if png is None:
                        return self._json({"error": "no photo"}, 404)
                    return self._send(200, png, "image/png")
                return self._json({"error": "not found"}, 404)
            except ApiError as exc:
                return self._json({"error": str(exc)}, exc.status)
            except FileNotFoundError:
                return self._json({"error": "not found"}, 404)
            except Exception as exc:  # pragma: no cover - surfaced to the UI
                traceback.print_exc()
                return self._json({"error": str(exc)}, 500)

        def do_POST(self):  # noqa: N802
            path = urlparse(self.path).path
            try:
                body = self._body()
                with service.lock:
                    if path.startswith("/api/player/"):
                        pid = int(path.rsplit("/", 1)[-1])
                        return self._json({"player": service.patch_player(pid, body),
                                           "state": service.state()})
                    if path == "/api/action":
                        result = service.action(body)
                        result["state"] = service.state()
                        return self._json(result)
                    if path == "/api/import":
                        notes = service.editor.apply_dict(body.get("roster") or {})
                        return self._json({"notes": notes, "state": service.state()})
                    if path == "/api/save":
                        return self._json(service.save(str(body.get("path") or "")))
                return self._json({"error": "not found"}, 404)
            except ApiError as exc:
                return self._json({"error": str(exc)}, exc.status)
            except (EditorError, ValueError, KeyError) as exc:
                return self._json({"error": str(exc)}, 400)
            except Exception as exc:  # pragma: no cover
                traceback.print_exc()
                return self._json({"error": str(exc)}, 500)

    return Handler


def serve(editor: RosterEditor, host: str = "127.0.0.1", port: int = 8756,
          open_browser: bool = True) -> None:
    service = EditorService(editor)
    httpd = ThreadingHTTPServer((host, port), make_handler(service))
    url = "http://%s:%d/" % (host, httpd.server_address[1])
    print("Courtside roster editor running at %s" % url)
    print("Press Ctrl+C to stop.")
    if open_browser:
        try:
            import webbrowser
            threading.Timer(0.5, webbrowser.open, args=(url,)).start()
        except Exception:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
