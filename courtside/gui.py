"""Offline desktop editor built on Tkinter.

Standard library only, no network access, no browser. Launch it with::

    python3 courtside_gui.py                  # pick a ROM from the file dialog
    python3 courtside_gui.py courtside.z64    # or open one straight away
    python3 -m courtside courtside.z64 gui

Tkinter ships with the python.org installers on Windows and macOS. On Linux
distributions it is usually a separate package (``python3-tk`` on Debian and
Ubuntu, ``python3-tkinter`` on Fedora).
"""

from __future__ import annotations

import base64
import os
import queue
import sys
import threading
import tkinter as tk
import traceback
from tkinter import filedialog, messagebox, ttk

from . import __version__, roster
from .editor import EditorError, RosterEditor
from .rom import RomError

PORTRAIT_SCALE = 3
PORTRAIT_W = 64 * PORTRAIT_SCALE
PORTRAIT_H = 56 * PORTRAIT_SCALE

ROM_FILETYPES = [
    ("Nintendo 64 ROMs", "*.z64 *.n64 *.v64 *.Z64 *.N64 *.V64"),
    ("All files", "*.*"),
]
JSON_FILETYPES = [("JSON files", "*.json"), ("All files", "*.*")]

SORTS = ("Depth chart", "Name", "Overall", "Jersey")


class CourtsideGUI(tk.Tk):
    def __init__(self, rom_path: str | None = None) -> None:
        super().__init__()
        self.title("Courtside Roster Editor")
        self.geometry("1180x760")
        self.minsize(900, 600)

        self.editor: RosterEditor | None = None
        self.current: roster.Player | None = None
        self._loading = False          # suppress change handlers while repopulating
        self._photo_cache: dict[str, tk.PhotoImage] = {}
        self._work: queue.Queue = queue.Queue()

        self._build_style()
        self._build_menu()
        self._build_body()
        self._set_enabled(False)
        self.status("Open a ROM to begin.")
        self.after(100, self._drain_work)

        if rom_path:
            self.after(50, lambda: self.open_rom(rom_path))

    # ------------------------------------------------------------------ chrome
    def _build_style(self) -> None:
        style = ttk.Style(self)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("Heading.TLabel", font=("TkDefaultFont", 13, "bold"))
        style.configure("Sub.TLabel", foreground="#555")
        style.configure("Ovr.TLabel", font=("TkDefaultFont", 22, "bold"))
        style.configure("Warn.TLabel", foreground="#a33")
        style.configure("Ok.TLabel", foreground="#2a7")

    def _build_menu(self) -> None:
        bar = tk.Menu(self)

        filemenu = tk.Menu(bar, tearoff=0)
        filemenu.add_command(label="Open ROM…", accelerator="Ctrl+O",
                             command=self.choose_rom)
        filemenu.add_command(label="Save ROM As…", accelerator="Ctrl+S",
                             command=self.save_rom_as)
        filemenu.add_separator()
        filemenu.add_command(label="Export roster to JSON…", command=self.export_json)
        filemenu.add_command(label="Import roster from JSON…", command=self.import_json)
        filemenu.add_separator()
        filemenu.add_command(label="Quit", accelerator="Ctrl+Q", command=self.on_quit)
        bar.add_cascade(label="File", menu=filemenu)
        self.filemenu = filemenu

        toolsmenu = tk.Menu(bar, tearoff=0)
        toolsmenu.add_command(label="Check roster consistency", command=self.show_problems)
        toolsmenu.add_command(label="Save this player's photo as PNG…", command=self.save_photo)
        bar.add_cascade(label="Tools", menu=toolsmenu)

        helpmenu = tk.Menu(bar, tearoff=0)
        helpmenu.add_command(label="About", command=self.show_about)
        bar.add_cascade(label="Help", menu=helpmenu)

        self.config(menu=bar)
        self.bind_all("<Control-o>", lambda e: self.choose_rom())
        self.bind_all("<Control-s>", lambda e: self.save_rom_as())
        self.bind_all("<Control-q>", lambda e: self.on_quit())
        self.protocol("WM_DELETE_WINDOW", self.on_quit)

    def _build_body(self) -> None:
        outer = ttk.Frame(self, padding=6)
        outer.pack(fill="both", expand=True)

        pane = ttk.PanedWindow(outer, orient="horizontal")
        pane.pack(fill="both", expand=True)

        pane.add(self._build_sidebar(pane), weight=0)
        pane.add(self._build_detail(pane), weight=1)

        self.statusbar = ttk.Label(self, relief="sunken", anchor="w", padding=(8, 3))
        self.statusbar.pack(fill="x", side="bottom")

    def _build_sidebar(self, parent) -> ttk.Frame:
        frame = ttk.Frame(parent, padding=(0, 0, 6, 0))

        filters = ttk.Frame(frame)
        filters.pack(fill="x", pady=(0, 6))

        self.var_search = tk.StringVar()
        self.var_search.trace_add("write", lambda *_: self.refresh_list())
        search = ttk.Entry(filters, textvariable=self.var_search)
        search.pack(fill="x")
        _placeholder(search, "Search players")

        row = ttk.Frame(filters)
        row.pack(fill="x", pady=(6, 0))
        self.var_team_filter = tk.StringVar(value="All teams")
        self.cmb_team_filter = ttk.Combobox(row, textvariable=self.var_team_filter,
                                            state="readonly", width=18)
        self.cmb_team_filter.pack(side="left", fill="x", expand=True)
        self.cmb_team_filter.bind("<<ComboboxSelected>>", lambda e: self.refresh_list())

        self.var_sort = tk.StringVar(value=SORTS[0])
        cmb_sort = ttk.Combobox(row, textvariable=self.var_sort, state="readonly",
                                values=SORTS, width=12)
        cmb_sort.pack(side="left", padx=(6, 0))
        cmb_sort.bind("<<ComboboxSelected>>", lambda e: self.refresh_list())

        columns = ("num", "name", "pos", "ovr")
        self.tree = ttk.Treeview(frame, columns=columns, show="headings",
                                 selectmode="browse", height=25)
        for col, text, width, anchor in (
                ("num", "#", 44, "e"), ("name", "Player", 190, "w"),
                ("pos", "Pos", 46, "center"), ("ovr", "OVR", 46, "e")):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor=anchor,
                             stretch=(col == "name"))
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="left", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self.on_select_player)
        self.tree.tag_configure("starter", foreground="#b45309")
        return frame

    def _build_detail(self, parent) -> ttk.Frame:
        frame = ttk.Frame(parent)

        head = ttk.Frame(frame, padding=(0, 0, 0, 8))
        head.pack(fill="x")

        self.lbl_portrait = ttk.Label(head, relief="solid", borderwidth=1)
        self.lbl_portrait.pack(side="left", padx=(0, 12))

        titles = ttk.Frame(head)
        titles.pack(side="left", fill="x", expand=True)

        names = ttk.Frame(titles)
        names.pack(anchor="w")
        self.var_first = tk.StringVar()
        self.var_last = tk.StringVar()
        self.ent_first = ttk.Entry(names, textvariable=self.var_first, width=16,
                                   font=("TkDefaultFont", 12, "bold"))
        self.ent_first.pack(side="left")
        self.ent_last = ttk.Entry(names, textvariable=self.var_last, width=20,
                                  font=("TkDefaultFont", 12, "bold"))
        self.ent_last.pack(side="left", padx=(6, 0))
        for entry, field in ((self.ent_first, "first_name"), (self.ent_last, "last_name")):
            entry.bind("<Return>", lambda e, f=field: self.commit_name(f))
            entry.bind("<FocusOut>", lambda e, f=field: self.commit_name(f))

        self.lbl_sub = ttk.Label(titles, style="Sub.TLabel", text="")
        self.lbl_sub.pack(anchor="w", pady=(6, 0))

        ovrbox = ttk.Frame(head)
        ovrbox.pack(side="right")
        self.lbl_ovr = ttk.Label(ovrbox, style="Ovr.TLabel", text="—")
        self.lbl_ovr.pack()
        ttk.Label(ovrbox, text="OVR", style="Sub.TLabel").pack()

        self.notebook = ttk.Notebook(frame)
        self.notebook.pack(fill="both", expand=True)
        self.notebook.add(self._tab_profile(), text="Profile")
        self.notebook.add(self._tab_ratings(), text="Ratings")
        self.notebook.add(self._tab_appearance(), text="Appearance")
        self.notebook.add(self._tab_moves(), text="Roster moves")
        self.notebook.add(self._tab_advanced(), text="Advanced")
        self.notebook.bind("<<NotebookTabChanged>>", self.on_tab_changed)
        return frame

    # ------------------------------------------------------------------- tabs
    def _tab_profile(self) -> ttk.Frame:
        tab = ttk.Frame(self.notebook, padding=12)
        grid = ttk.Frame(tab)
        grid.pack(fill="x")
        for col in (1, 3):
            grid.columnconfigure(col, weight=1, minsize=170)

        def cell(row, col, label):
            ttk.Label(grid, text=label).grid(row=row, column=col * 2, sticky="w",
                                             padx=(0, 8), pady=5)
            holder = ttk.Frame(grid)
            holder.grid(row=row, column=col * 2 + 1, sticky="ew", padx=(0, 24), pady=5)
            return holder

        self.var_team = tk.StringVar()
        self.cmb_team = ttk.Combobox(cell(0, 0, "Team"), textvariable=self.var_team,
                                     state="readonly")
        self.cmb_team.pack(fill="x")
        self.cmb_team.bind("<<ComboboxSelected>>", lambda e: self.commit_team())

        self.var_starter = tk.StringVar()
        self.cmb_starter = ttk.Combobox(
            cell(0, 1, "Lineup role"), textvariable=self.var_starter, state="readonly",
            values=["Bench"] + ["Starter — slot %d" % i for i in range(1, 6)])
        self.cmb_starter.pack(fill="x")
        self.cmb_starter.bind("<<ComboboxSelected>>", lambda e: self.commit_starter())

        self.var_jersey = tk.StringVar()
        self.spn_jersey = ttk.Spinbox(cell(1, 0, 'Jersey (100 = "00")'), from_=0, to=100,
                                      textvariable=self.var_jersey,
                                      command=lambda: self.commit_simple("jersey"))
        self.spn_jersey.pack(fill="x")
        self.spn_jersey.bind("<FocusOut>", lambda e: self.commit_simple("jersey"))
        self.spn_jersey.bind("<Return>", lambda e: self.commit_simple("jersey"))

        self.var_position = tk.StringVar()
        self.cmb_position = ttk.Combobox(
            cell(1, 1, "Position"), textvariable=self.var_position, state="readonly",
            values=["%s — %s" % (c, roster.POSITION_NAMES[c]) for c in roster.POSITION_CODES])
        self.cmb_position.pack(fill="x")
        self.cmb_position.bind("<<ComboboxSelected>>", lambda e: self.commit_position())

        height = cell(2, 0, "Height")
        self.var_feet = tk.StringVar()
        self.var_inches = tk.StringVar()
        cmb_ft = ttk.Combobox(height, textvariable=self.var_feet, state="readonly",
                              width=6, values=[str(f) + " ft" for f in range(4, 9)])
        cmb_ft.pack(side="left")
        cmb_in = ttk.Combobox(height, textvariable=self.var_inches, state="readonly",
                              width=6, values=[str(i) + " in" for i in range(12)])
        cmb_in.pack(side="left", padx=(6, 0))
        for c in (cmb_ft, cmb_in):
            c.bind("<<ComboboxSelected>>", lambda e: self.commit_height())

        self.var_weight = tk.StringVar()
        self.spn_weight = ttk.Spinbox(cell(2, 1, "Weight (lb)"), from_=100, to=355,
                                      textvariable=self.var_weight,
                                      command=lambda: self.commit_simple("weight_lbs"))
        self.spn_weight.pack(fill="x")
        self.spn_weight.bind("<FocusOut>", lambda e: self.commit_simple("weight_lbs"))
        self.spn_weight.bind("<Return>", lambda e: self.commit_simple("weight_lbs"))

        self.var_range = tk.StringVar()
        self.cmb_range = ttk.Combobox(
            cell(3, 0, "Shooting range"), textvariable=self.var_range, state="readonly",
            values=["%d feet" % ft for ft in roster.RANGE_FEET])
        self.cmb_range.pack(fill="x")
        self.cmb_range.bind("<<ComboboxSelected>>", lambda e: self.commit_range())
        return tab

    def _tab_ratings(self) -> ttk.Frame:
        tab = ttk.Frame(self.notebook, padding=12)
        grid = ttk.Frame(tab)
        grid.pack(fill="x")
        grid.columnconfigure(1, weight=1)

        self.rating_vars: dict[str, tk.IntVar] = {}
        for row, attr in enumerate(roster.ATTRIBUTES):
            ttk.Label(grid, text=attr.label).grid(row=row, column=0, sticky="w",
                                                  padx=(0, 10), pady=2)
            var = tk.IntVar(value=attr.minimum)
            self.rating_vars[attr.key] = var
            scale = ttk.Scale(grid, from_=attr.minimum, to=attr.maximum,
                              variable=var, orient="horizontal")
            scale.grid(row=row, column=1, sticky="ew", pady=2)
            spin = ttk.Spinbox(grid, from_=attr.minimum, to=attr.maximum, width=5,
                               textvariable=var)
            spin.grid(row=row, column=2, sticky="e", padx=(10, 0), pady=2)
            for widget in (scale, spin):
                widget.bind("<ButtonRelease-1>",
                            lambda e, k=attr.key: self.commit_rating(k))
                widget.bind("<KeyRelease>", lambda e, k=attr.key: self.commit_rating(k))
            spin.bind("<FocusOut>", lambda e, k=attr.key: self.commit_rating(k))

        bulk = ttk.Frame(tab)
        bulk.pack(fill="x", pady=(14, 0))
        ttk.Button(bulk, text="All −5", command=lambda: self.bulk_ratings(-5)).pack(side="left")
        ttk.Button(bulk, text="All +5",
                   command=lambda: self.bulk_ratings(5)).pack(side="left", padx=6)
        ttk.Button(bulk, text="Max out", command=lambda: self.bulk_ratings("max")).pack(side="left")
        return tab

    def _tab_appearance(self) -> ttk.Frame:
        tab = ttk.Frame(self.notebook, padding=12)
        ttk.Label(tab, wraplength=640, style="Sub.TLabel", text=(
            "Faces and roster photos live in TEAMDATA.IFF and are indexed by player id, "
            "so a likeness can be handed to anyone. The model bytes below drive the "
            "on-court player model.")).pack(anchor="w", pady=(0, 12))

        swap = ttk.Frame(tab)
        swap.pack(anchor="w")

        left = ttk.Frame(swap)
        left.pack(side="left")
        self.lbl_app_current = ttk.Label(left, relief="solid", borderwidth=1)
        self.lbl_app_current.pack()
        self.lbl_app_who = ttk.Label(left, style="Sub.TLabel", text="")
        self.lbl_app_who.pack(pady=(6, 0))

        ttk.Label(swap, text="◀", font=("TkDefaultFont", 18)).pack(side="left", padx=18)

        right = ttk.Frame(swap)
        right.pack(side="left")
        self.lbl_app_source = ttk.Label(right, relief="solid", borderwidth=1)
        self.lbl_app_source.pack()
        self.var_source = tk.StringVar()
        self.cmb_source = ttk.Combobox(right, textvariable=self.var_source,
                                       state="readonly", width=34)
        self.cmb_source.pack(pady=(6, 0))
        self.cmb_source.bind("<<ComboboxSelected>>", lambda e: self.refresh_source_photo())

        checks = ttk.Frame(tab)
        checks.pack(anchor="w", pady=(14, 8))
        self.var_c_photo = tk.BooleanVar(value=True)
        self.var_c_face = tk.BooleanVar(value=True)
        self.var_c_model = tk.BooleanVar(value=True)
        ttk.Checkbutton(checks, text="Roster photo", variable=self.var_c_photo).pack(side="left")
        ttk.Checkbutton(checks, text="3D face texture",
                        variable=self.var_c_face).pack(side="left", padx=14)
        ttk.Checkbutton(checks, text="Model bytes", variable=self.var_c_model).pack(side="left")

        buttons = ttk.Frame(tab)
        buttons.pack(anchor="w")
        ttk.Button(buttons, text="Copy likeness to this player",
                   command=self.copy_likeness).pack(side="left")
        ttk.Button(buttons, text="Swap likenesses",
                   command=self.swap_likeness).pack(side="left", padx=8)

        ttk.Separator(tab, orient="horizontal").pack(fill="x", pady=14)
        self.model_vars = self._byte_grid(
            tab, ["model_a", "model_b", "flags_a", "flags_b", "misc_14", "misc_15"])
        return tab

    def _tab_moves(self) -> ttk.Frame:
        tab = ttk.Frame(self.notebook, padding=12)

        def block(title, hint):
            ttk.Label(tab, text=title, style="Heading.TLabel").pack(anchor="w", pady=(0, 2))
            ttk.Label(tab, text=hint, style="Sub.TLabel",
                      wraplength=640).pack(anchor="w", pady=(0, 6))
            row = ttk.Frame(tab)
            row.pack(anchor="w", pady=(0, 14))
            return row

        row = block("Trade", "Swaps two players between their teams. Both rosters keep "
                             "their size and the players inherit each other's lineup slot.")
        self.var_trade = tk.StringVar()
        self.cmb_trade = ttk.Combobox(row, textvariable=self.var_trade,
                                      state="readonly", width=40)
        self.cmb_trade.pack(side="left")
        ttk.Button(row, text="Trade", command=self.do_trade).pack(side="left", padx=8)

        row = block("Sign to a team", "Moves this player without anything coming back, "
                                      "so the roster sizes change.")
        self.var_signto = tk.StringVar()
        self.cmb_signto = ttk.Combobox(row, textvariable=self.var_signto,
                                       state="readonly", width=40)
        self.cmb_signto.pack(side="left")
        ttk.Button(row, text="Sign", command=self.do_sign).pack(side="left", padx=8)

        row = block("Release", "Drops the player into the Free Agents pool.")
        ttk.Button(row, text="Release player", command=self.do_release).pack(side="left")

        ttk.Separator(tab, orient="horizontal").pack(fill="x", pady=(0, 10))
        self.lbl_problems = ttk.Label(tab, style="Ok.TLabel", wraplength=640,
                                      justify="left", text="")
        self.lbl_problems.pack(anchor="w")
        return tab

    def _tab_advanced(self) -> ttk.Frame:
        tab = ttk.Frame(self.notebook, padding=12)
        ttk.Label(tab, wraplength=640, style="Sub.TLabel", text=(
            "Bytes in the player record whose meaning is not fully pinned down. They are "
            "editable for experimentation; leave them alone if you are unsure.")
        ).pack(anchor="w", pady=(0, 12))
        keys = [m[0] for m in roster.MISC_FIELDS
                if m[0] not in ("model_a", "model_b", "flags_a", "flags_b",
                                "misc_14", "misc_15")]
        self.misc_vars = self._byte_grid(tab, keys)
        return tab

    def _byte_grid(self, parent, keys: list[str]) -> dict[str, tk.StringVar]:
        grid = ttk.Frame(parent)
        grid.pack(anchor="w", fill="x")
        out: dict[str, tk.StringVar] = {}
        for i, key in enumerate(keys):
            meta = roster.MISC_BY_KEY[key]
            col, row = i % 3, i // 3
            ttk.Label(grid, text=meta[1]).grid(row=row, column=col * 2, sticky="w",
                                               padx=(0, 8), pady=4)
            var = tk.StringVar()
            spin = ttk.Spinbox(grid, from_=meta[3], to=meta[4], width=8, textvariable=var,
                               command=lambda k=key: self.commit_misc(k))
            spin.grid(row=row, column=col * 2 + 1, sticky="w", padx=(0, 24), pady=4)
            spin.bind("<FocusOut>", lambda e, k=key: self.commit_misc(k))
            spin.bind("<Return>", lambda e, k=key: self.commit_misc(k))
            out[key] = var
        return out

    # ---------------------------------------------------------------- plumbing
    def status(self, text: str) -> None:
        dirty = " · unsaved changes" if self.editor and self.editor.has_changes else ""
        self.statusbar.config(text=text + dirty)

    def _set_enabled(self, on: bool) -> None:
        state = "normal" if on else "disabled"
        for index in ("Save ROM As…", "Export roster to JSON…",
                      "Import roster from JSON…"):
            try:
                self.filemenu.entryconfig(index, state=state)
            except tk.TclError:
                pass

    def _drain_work(self) -> None:
        """Run callbacks handed over by background threads on the Tk thread."""
        try:
            while True:
                self._work.get_nowait()()
        except queue.Empty:
            pass
        self.after(100, self._drain_work)

    def report(self, exc: Exception) -> None:
        messagebox.showerror("Courtside", str(exc), parent=self)
        self.status("Error: %s" % exc)

    # -------------------------------------------------------------- ROM loading
    def choose_rom(self) -> None:
        path = filedialog.askopenfilename(title="Open a Courtside ROM",
                                          filetypes=ROM_FILETYPES, parent=self)
        if path:
            self.open_rom(path)

    def open_rom(self, path: str) -> None:
        if self.editor and self.editor.has_changes and not messagebox.askokcancel(
                "Courtside", "The current ROM has unsaved changes. Open another anyway?",
                parent=self):
            return
        self.config(cursor="watch")
        self.update_idletasks()
        try:
            self.editor = RosterEditor.open(path)
        except (EditorError, RomError, OSError) as exc:
            self.config(cursor="")
            self.report(exc)
            return
        finally:
            self.config(cursor="")
        self._photo_cache.clear()
        self.current = None
        rom = self.editor.rom
        self.title("Courtside Roster Editor — %s" % os.path.basename(path))
        self.cmb_team_filter.config(
            values=["All teams"] + [t.display for t in self.editor.db.teams])
        self.var_team_filter.set("All teams")
        self.cmb_team.config(values=[t.display for t in self.editor.db.teams])
        self.cmb_signto.config(values=[t.display for t in self.editor.db.teams])
        self._set_enabled(True)
        self.refresh_list(select_first=True)
        self.status("%s [%s] · CIC %d · %d teams · %d players"
                    % (rom.title, rom.cart_id, rom.cic,
                       len(self.editor.db.teams), self.editor.db.player_used))

    # ------------------------------------------------------------------- list
    def visible_players(self) -> list[roster.Player]:
        db = self.editor.db
        players = list(db.players)
        team = self.var_team_filter.get()
        if team and team != "All teams":
            players = [p for p in players if db.team_name(p.team) == team]
        needle = self.var_search.get().strip().lower()
        if needle and needle != "search players":
            players = [p for p in players
                       if needle in p.full_name.lower()
                       or needle == p.jersey_text
                       or needle == str(p.player_id)]
        sort = self.var_sort.get()
        if sort == "Name":
            players.sort(key=lambda p: (p.last_name.lower(), p.first_name.lower()))
        elif sort == "Overall":
            players.sort(key=lambda p: -p.overall)
        elif sort == "Jersey":
            players.sort(key=lambda p: p.jersey)
        else:
            players.sort(key=lambda p: (p.team, db.starter_slot(p) or 9,
                                        p.last_name.lower()))
        return players

    def refresh_list(self, select_first: bool = False) -> None:
        if not self.editor:
            return
        keep = self.current.player_id if self.current else None
        self.tree.delete(*self.tree.get_children())
        db = self.editor.db
        for p in self.visible_players():
            slot = db.starter_slot(p)
            self.tree.insert(
                "", "end", iid=str(p.player_id),
                values=(slot if slot else p.jersey_text, p.full_name, p.position, p.overall),
                tags=("starter",) if slot else ())
        target = None
        if keep is not None and self.tree.exists(str(keep)):
            target = str(keep)
        elif select_first and self.tree.get_children():
            target = self.tree.get_children()[0]
        if target:
            self.tree.selection_set(target)
            self.tree.see(target)

    def on_select_player(self, _event=None) -> None:
        sel = self.tree.selection()
        if not sel or not self.editor:
            return
        player = self.editor.db.by_id(int(sel[0]))
        if player is None or player is self.current:
            return
        self.current = player
        self.load_player()

    # ------------------------------------------------------------ player panel
    def load_player(self) -> None:
        p, db = self.current, self.editor.db
        if p is None:
            return
        self._loading = True
        try:
            self.var_first.set(p.first_name)
            self.var_last.set(p.last_name)
            self.lbl_sub.config(text="#%s · %s · %s · %d lb · %s%s · id %d" % (
                p.jersey_text, p.position, p.height_text, p.weight_lbs,
                db.team_name(p.team),
                " · starter %d" % db.starter_slot(p) if db.starter_slot(p) else "",
                p.player_id))
            self.lbl_ovr.config(text=str(p.overall))

            self.var_team.set(db.team_name(p.team))
            slot = db.starter_slot(p)
            self.var_starter.set("Bench" if not slot else "Starter — slot %d" % slot)
            self.var_jersey.set(str(p.jersey))
            self.var_position.set("%s — %s" % (p.position, roster.POSITION_NAMES[p.position]))
            self.var_feet.set("%d ft" % p.height_feet)
            self.var_inches.set("%d in" % p.height_remainder)
            self.var_weight.set(str(p.weight_lbs))
            self.var_range.set("%d feet" % p.range_feet)

            for key, var in self.rating_vars.items():
                var.set(p.attribute(key))
            for key, var in list(self.model_vars.items()) + list(self.misc_vars.items()):
                var.set(str(p.misc(key)))

            self.lbl_app_who.config(text="%s (current)" % p.full_name)
            self._fill_other_players()
            self.show_problems(quiet=True)
        finally:
            self._loading = False
        self.set_portrait(self.lbl_portrait, p.player_id)
        if self.notebook.index(self.notebook.select()) == 2:
            self.on_tab_changed()

    def _fill_other_players(self) -> None:
        db = self.editor.db
        others = sorted((o for o in db.players if o is not self.current),
                        key=lambda o: (o.last_name.lower(), o.first_name.lower()))
        self._others = others
        labels = ["%s, %s — %s" % (o.last_name, o.first_name, db.team_name(o.team))
                  for o in others]
        self.cmb_source.config(values=labels)
        self.cmb_trade.config(values=labels)
        if labels:
            if self.var_source.get() not in labels:
                self.var_source.set(labels[0])
            if self.var_trade.get() not in labels:
                self.var_trade.set(labels[0])
        self.var_signto.set(db.team_name(self.current.team))

    def _selected_other(self, var: tk.StringVar) -> roster.Player | None:
        try:
            return self._others[self.cmb_source.cget("values").index(var.get())]
        except (ValueError, AttributeError, IndexError):
            return None

    # --------------------------------------------------------------- portraits
    def set_portrait(self, label: ttk.Label, player_id: int) -> None:
        image = self.portrait(player_id)
        label.config(image=image or "", text="" if image else "no photo",
                     width=0 if image else 14)
        label.image = image

    def portrait(self, player_id: int) -> tk.PhotoImage | None:
        key = str(player_id)
        if key in self._photo_cache:
            return self._photo_cache[key]
        try:
            png = self.editor.appearance.photo_png(player_id, scale=PORTRAIT_SCALE)
        except (EditorError, ValueError):
            png = None
        image = None
        if png:
            image = tk.PhotoImage(data=base64.b64encode(png).decode("ascii"), master=self)
        self._photo_cache[key] = image
        return image

    def on_tab_changed(self, _event=None) -> None:
        if not self.editor or self.current is None:
            return
        if self.notebook.index(self.notebook.select()) != 2:
            return
        self.config(cursor="watch")
        self.update_idletasks()
        try:
            self.set_portrait(self.lbl_app_current, self.current.player_id)
            self.refresh_source_photo()
        finally:
            self.config(cursor="")

    def refresh_source_photo(self) -> None:
        other = self._selected_other(self.var_source)
        if other is not None:
            self.set_portrait(self.lbl_app_source, other.player_id)

    # ----------------------------------------------------------------- commits
    def _after_edit(self, message: str) -> None:
        self.editor.dirty = True
        self.refresh_list()
        self.load_player()
        self.status(message)

    def commit_name(self, field: str) -> None:
        if self._loading or self.current is None:
            return
        value = (self.var_first if field == "first_name" else self.var_last).get().strip()
        if value == getattr(self.current, field):
            return
        setattr(self.current, field, value)
        self._after_edit("Renamed to %s" % self.current.full_name)

    def commit_simple(self, field: str) -> None:
        if self._loading or self.current is None:
            return
        var = self.var_jersey if field == "jersey" else self.var_weight
        try:
            value = int(float(var.get()))
        except ValueError:
            return
        if getattr(self.current, field) == value:
            return
        setattr(self.current, field, value)
        self._after_edit("%s set to %s" % (field.replace("_", " "), value))

    def commit_position(self) -> None:
        if self._loading or self.current is None:
            return
        code = self.var_position.get().split(" — ")[0]
        if code and code != self.current.position:
            self.current.position = code
            self._after_edit("Position set to %s" % code)

    def commit_height(self) -> None:
        if self._loading or self.current is None:
            return
        try:
            feet = int(self.var_feet.get().split()[0])
            inches = int(self.var_inches.get().split()[0])
        except (ValueError, IndexError):
            return
        self.current.set_height(feet, inches)
        self._after_edit("Height set to %s" % self.current.height_text)

    def commit_range(self) -> None:
        if self._loading or self.current is None:
            return
        try:
            feet = int(self.var_range.get().split()[0])
        except (ValueError, IndexError):
            return
        self.current.shooting_range = roster.RANGE_FEET.index(feet)
        self._after_edit("Shooting range set to %d feet" % feet)

    def commit_team(self) -> None:
        if self._loading or self.current is None:
            return
        db = self.editor.db
        name = self.var_team.get()
        index = next((t.index for t in db.teams if t.display == name), None)
        if index is None or index == self.current.team:
            return
        db.assign_team(self.current, index)
        self._after_edit("%s moved to %s" % (self.current.full_name, name))

    def commit_starter(self) -> None:
        if self._loading or self.current is None:
            return
        text = self.var_starter.get()
        slot = 0 if text == "Bench" else int(text.rsplit(" ", 1)[-1])
        try:
            self.editor.db.set_starter_slot(self.current, slot)
        except ValueError as exc:
            self.report(exc)
            return
        self._after_edit("Lineup role updated")

    def commit_rating(self, key: str) -> None:
        if self._loading or self.current is None:
            return
        try:
            value = int(round(self.rating_vars[key].get()))
        except (ValueError, tk.TclError):
            return
        if self.current.attribute(key) == value:
            return
        self.current.set_attribute(key, value)
        self.editor.dirty = True
        self.lbl_ovr.config(text=str(self.current.overall))
        if self.tree.exists(str(self.current.player_id)):
            self.tree.set(str(self.current.player_id), "ovr", self.current.overall)
        self.status("%s set to %d" % (roster.ATTRIBUTES_BY_KEY[key].label, value))

    def bulk_ratings(self, mode) -> None:
        if self.current is None:
            return
        for attr in roster.ATTRIBUTES:
            current = self.current.attribute(attr.key)
            value = attr.maximum if mode == "max" else current + mode
            self.current.set_attribute(attr.key, value)
        self._after_edit("Ratings updated")

    def commit_misc(self, key: str) -> None:
        if self._loading or self.current is None:
            return
        var = self.model_vars.get(key) or self.misc_vars.get(key)
        try:
            value = int(float(var.get()))
        except (ValueError, AttributeError):
            return
        if self.current.misc(key) == value:
            return
        self.current.set_misc(key, value)
        self.editor.dirty = True
        self.status("%s set to %d" % (roster.MISC_BY_KEY[key][1], value))

    # ------------------------------------------------------------------ actions
    def copy_likeness(self) -> None:
        other = self._selected_other(self.var_source)
        if other is None or self.current is None:
            return
        self.config(cursor="watch")
        self.update_idletasks()
        try:
            self.editor.reassign_appearance(
                str(self.current.player_id), str(other.player_id),
                model_bytes=self.var_c_model.get(), face=self.var_c_face.get(),
                photo=self.var_c_photo.get())
        except (EditorError, ValueError) as exc:
            self.report(exc)
            return
        finally:
            self.config(cursor="")
        self._photo_cache.clear()
        self.load_player()
        self.on_tab_changed()
        self.status("%s now looks like %s" % (self.current.full_name, other.full_name))

    def swap_likeness(self) -> None:
        other = self._selected_other(self.var_source)
        if other is None or self.current is None:
            return
        self.config(cursor="watch")
        self.update_idletasks()
        try:
            self.editor.swap_appearance(str(self.current.player_id), str(other.player_id))
        except (EditorError, ValueError) as exc:
            self.report(exc)
            return
        finally:
            self.config(cursor="")
        self._photo_cache.clear()
        self.load_player()
        self.on_tab_changed()
        self.status("Swapped the looks of %s and %s"
                    % (self.current.full_name, other.full_name))

    def do_trade(self) -> None:
        other = self._selected_other(self.var_trade)
        if other is None or self.current is None:
            return
        try:
            a, b = self.editor.trade(str(self.current.player_id), str(other.player_id))
        except (EditorError, ValueError) as exc:
            self.report(exc)
            return
        db = self.editor.db
        self._after_edit("%s → %s, %s → %s" % (a.full_name, db.team_name(a.team),
                                               b.full_name, db.team_name(b.team)))

    def do_sign(self) -> None:
        if self.current is None:
            return
        try:
            p = self.editor.move(str(self.current.player_id), self.var_signto.get())
        except (EditorError, ValueError) as exc:
            self.report(exc)
            return
        self._after_edit("%s signed with %s"
                         % (p.full_name, self.editor.db.team_name(p.team)))

    def do_release(self) -> None:
        if self.current is None:
            return
        if not messagebox.askokcancel(
                "Courtside",
                "Release %s to free agency?\n\nHis team will be left with one fewer "
                "player than the game ships with." % self.current.full_name, parent=self):
            return
        p = self.editor.release(str(self.current.player_id))
        self._after_edit("%s released to free agency" % p.full_name)

    def show_problems(self, quiet: bool = False) -> None:
        if not self.editor:
            return
        problems = self.editor.db.problems()
        self.lbl_problems.config(
            style="Warn.TLabel" if problems else "Ok.TLabel",
            text="\n".join("• " + w for w in problems) if problems
            else "Every team has twelve players and a full starting five.")
        if not quiet:
            self.notebook.select(3)
            self.status("%d roster issue(s)" % len(problems) if problems
                        else "Roster is consistent")

    def save_photo(self) -> None:
        if self.current is None:
            return
        path = filedialog.asksaveasfilename(
            title="Save roster photo", defaultextension=".png",
            initialfile="%s.png" % self.current.full_name.replace(" ", "-").lower(),
            filetypes=[("PNG images", "*.png")], parent=self)
        if not path:
            return
        png = self.editor.appearance.photo_png(self.current.player_id, scale=6)
        if png is None:
            messagebox.showinfo("Courtside", "This player has no roster photo.", parent=self)
            return
        with open(path, "wb") as fh:
            fh.write(png)
        self.status("Wrote %s" % path)

    # ------------------------------------------------------------------ saving
    def save_rom_as(self) -> None:
        if not self.editor:
            return
        source = self.editor.rom.path or "courtside.z64"
        suggested = os.path.splitext(os.path.basename(source))[0] + "-edited.z64"
        path = filedialog.asksaveasfilename(
            title="Save edited ROM", defaultextension=".z64", initialfile=suggested,
            filetypes=ROM_FILETYPES, parent=self)
        if not path:
            return
        if os.path.abspath(path) == os.path.abspath(source):
            messagebox.showerror(
                "Courtside", "Choose a different file - the original ROM is never "
                             "overwritten.", parent=self)
            return
        self._save_in_background(path)

    def _save_in_background(self, path: str) -> None:
        self.config(cursor="watch")
        self.status("Saving %s… (repacking compressed data)" % os.path.basename(path))
        self.update_idletasks()

        def work():
            try:
                report = self.editor.save(path)
            except Exception as exc:  # surfaced on the Tk thread below
                self._work.put(lambda e=exc: self._save_failed(e))
            else:
                self._work.put(lambda r=report: self._save_done(r))

        threading.Thread(target=work, daemon=True).start()

    def _save_done(self, report) -> None:
        self.config(cursor="")
        self.status("Saved %s · CRC 0x%08X 0x%08X" % (report.path, *report.crc))
        detail = ["Wrote %s" % report.path,
                  "",
                  "TEAMINFO.IFF %s" % report.teaminfo]
        if report.teamdata:
            detail.append("TEAMDATA.IFF %s" % report.teamdata)
        detail.append("CRC fixed: 0x%08X 0x%08X" % report.crc)
        if report.warnings:
            detail += ["", "Warnings:"] + ["• " + w for w in report.warnings]
        messagebox.showinfo("Courtside", "\n".join(detail), parent=self)

    def _save_failed(self, exc: Exception) -> None:
        self.config(cursor="")
        traceback.print_exception(type(exc), exc, exc.__traceback__)
        self.report(exc)

    # ------------------------------------------------------------------- JSON
    def export_json(self) -> None:
        if not self.editor:
            return
        path = filedialog.asksaveasfilename(
            title="Export roster", defaultextension=".json",
            initialfile="courtside-roster.json", filetypes=JSON_FILETYPES, parent=self)
        if not path:
            return
        self.editor.export_json(path)
        self.status("Exported roster to %s" % path)

    def import_json(self) -> None:
        if not self.editor:
            return
        path = filedialog.askopenfilename(title="Import roster",
                                          filetypes=JSON_FILETYPES, parent=self)
        if not path:
            return
        try:
            notes = self.editor.import_json(path)
        except (EditorError, ValueError, OSError) as exc:
            self.report(exc)
            return
        self.current = None
        self.refresh_list(select_first=True)
        self.status("Imported %s%s" % (path, " (%d note(s))" % len(notes) if notes else ""))

    # ------------------------------------------------------------------- misc
    def show_about(self) -> None:
        messagebox.showinfo("About", (
            "Courtside Roster Editor %s\n\n"
            "Offline roster editor for Kobe Bryant's NBA Courtside (Nintendo 64).\n"
            "Standard library only - no network access, no third-party packages.\n\n"
            "Your original ROM is never modified; edits are always written to a new "
            "file." % __version__), parent=self)

    def on_quit(self) -> None:
        if self.editor and self.editor.has_changes and not messagebox.askokcancel(
                "Courtside", "You have unsaved changes. Quit anyway?", parent=self):
            return
        self.destroy()


def _placeholder(entry: ttk.Entry, text: str) -> None:
    """Grey prompt text that clears on focus."""
    entry.insert(0, text)
    entry.config(foreground="grey")

    def on_focus_in(_e):
        if entry.get() == text:
            entry.delete(0, "end")
            entry.config(foreground="")

    def on_focus_out(_e):
        if not entry.get():
            entry.insert(0, text)
            entry.config(foreground="grey")

    entry.bind("<FocusIn>", on_focus_in)
    entry.bind("<FocusOut>", on_focus_out)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    rom = argv[0] if argv else None
    try:
        app = CourtsideGUI(rom)
    except tk.TclError as exc:
        print("Could not start the graphical editor: %s" % exc, file=sys.stderr)
        print("If you are on a headless machine, use the command line instead:\n"
              "  python3 -m courtside ROM info", file=sys.stderr)
        return 1
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
