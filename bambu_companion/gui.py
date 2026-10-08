"""Desktop GUI for Bambu Companion.

Honesty note: this file imports tkinter, which is part of the Python
standard library on Windows but is NOT installed in the sandbox this
project was built in (no display server there), so it can only be
syntax-checked here, never run — it has since been run and fixed once
for real on a Windows machine (a label wraplength/width bug), but every
change to this file still needs a real run on Windows to confirm, the
same as before. All of the logic it calls into (`gui_logic.py`,
`gui_config.py`) has real unit tests that don't depend on tkinter, so a
bug is far more likely to be a layout/widget mistake in this file than a
wrong recommendation or a config that fails to save.

Run it with:

    python -m bambu_companion.gui

or double-click launch_gui.pyw at the repo root.
"""

from __future__ import annotations

import tkinter as tk
import traceback
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .gui_config import add_recent_file, load_config, save_config
from .gui_logic import (
    DEFAULT_CURRENT_SETTINGS,
    GOAL_LABELS,
    MATERIALS,
    AnalysisError,
    AnalysisRun,
    change_rows,
    current_settings_from_full_profile,
    export_files,
    load_full_profile,
    run_analysis,
    summary_lines,
)
from .schemas import PrintGoal

APP_TITLE = "Bambu Companion"
DEFAULT_NOZZLE_MM = 0.4
ASSETS_DIR = Path(__file__).resolve().parent / "assets"


class SettingsEditor(ttk.Frame):
    """A small key/value grid for editing the 'current settings' the user
    tells us about (we can't read Bambu Studio's live profile from here).
    """

    def __init__(self, master, initial_settings: dict | None = None):
        super().__init__(master)
        self._vars: dict[str, tk.StringVar] = {}
        self.set_settings(initial_settings or DEFAULT_CURRENT_SETTINGS)

    def set_settings(self, settings: dict) -> None:
        """(Re)builds the grid from `settings`. Public so gui.py can push
        in a saved config's values after the widget already exists."""
        for widget in self.winfo_children():
            widget.destroy()
        self._vars.clear()

        ttk.Label(self, text="Setting", font=("", 9, "bold")).grid(row=0, column=0, sticky="w", padx=4, pady=2)
        ttk.Label(self, text="Current value", font=("", 9, "bold")).grid(row=0, column=1, sticky="w", padx=4, pady=2)

        for i, (key, value) in enumerate(sorted(settings.items()), start=1):
            ttk.Label(self, text=key).grid(row=i, column=0, sticky="w", padx=4, pady=1)
            var = tk.StringVar(value=str(value))
            ttk.Entry(self, textvariable=var, width=18).grid(row=i, column=1, sticky="w", padx=4, pady=1)
            self._vars[key] = var

    def values(self) -> dict:
        """Parses each field back to int/float/bool where possible,
        falling back to the raw string (mirrors how the CLI parses
        --current key=value pairs)."""
        out = {}
        for key, var in self._vars.items():
            raw = var.get().strip()
            out[key] = _coerce(raw)
        return out


def _coerce(raw: str):
    if raw.lower() in ("true", "false"):
        return raw.lower() == "true"
    try:
        if "." in raw:
            return float(raw)
        return int(raw)
    except ValueError:
        return raw


class ChangesTable(ttk.Frame):
    """Results table: one checkbutton row per recommended change."""

    # Pixel width the "Reason" column wraps at. Deliberately NOT combined
    # with a character `width=` on that label — ttk.Label clips wrapped
    # text to whichever of the two (width-in-characters vs
    # wraplength-in-pixels) is narrower, and the two don't line up
    # reliably across fonts, which is what was cutting reason text off
    # mid-word. wraplength alone is enough to control wrapping.
    REASON_WRAP_PX = 460

    def __init__(self, master):
        super().__init__(master)
        self._row_vars: list[tuple[tk.BooleanVar, dict]] = []
        self._rows_frame = ttk.Frame(self)
        self._rows_frame.pack(fill="both", expand=True)
        self._rows_frame.grid_columnconfigure(4, weight=1)

    def set_rows(self, rows: list[dict]) -> None:
        for widget in self._rows_frame.winfo_children():
            widget.destroy()
        self._row_vars.clear()

        headers = ["Apply?", "Setting", "Current", "Recommended", "Reason"]
        header_widths = [6, 20, 10, 12, None]  # None = no fixed width (Reason wraps instead)
        for c, (h, w) in enumerate(zip(headers, header_widths)):
            kwargs = {"width": w} if w is not None else {}
            ttk.Label(self._rows_frame, text=h, font=("", 9, "bold"), anchor="w", **kwargs).grid(
                row=0, column=c, sticky="w", padx=4, pady=2
            )

        if not rows:
            ttk.Label(self._rows_frame, text="No changes recommended — current settings already fit this goal.").grid(
                row=1, column=0, columnspan=5, sticky="w", padx=4, pady=8
            )
            return

        for i, row in enumerate(rows, start=1):
            var = tk.BooleanVar(value=row["approved"])
            ttk.Checkbutton(self._rows_frame, variable=var).grid(row=i, column=0, padx=4, pady=1, sticky="n")
            ttk.Label(self._rows_frame, text=row["label"], width=20, anchor="w").grid(
                row=i, column=1, sticky="nw", padx=4
            )
            ttk.Label(self._rows_frame, text=str(row["current"]), width=10, anchor="w").grid(
                row=i, column=2, sticky="nw", padx=4
            )
            ttk.Label(self._rows_frame, text=str(row["recommended"]), width=12, anchor="w").grid(
                row=i, column=3, sticky="nw", padx=4
            )
            # No `width=` here on purpose — see REASON_WRAP_PX note above.
            ttk.Label(
                self._rows_frame,
                text=row["reason"],
                anchor="w",
                wraplength=self.REASON_WRAP_PX,
                justify="left",
            ).grid(row=i, column=4, sticky="new", padx=4, pady=2)
            self._row_vars.append((var, row))

    def approved_keys(self) -> set[str]:
        return {row["key"] for var, row in self._row_vars if var.get()}


class BambuCompanionApp(ttk.Frame):
    def __init__(self, master: tk.Tk):
        super().__init__(master, padding=10)
        self.master = master
        master.title(APP_TITLE)
        master.geometry("1050x750")
        master.minsize(850, 500)
        self.pack(fill="both", expand=True)
        _set_window_icon(master)

        self._config = load_config()

        self._model_path = tk.StringVar(value=self._config["last_model_path"])
        self._goal = tk.StringVar(value=self._config["goal_label"] or GOAL_LABELS[PrintGoal.BALANCED])
        self._material = tk.StringVar(value=self._config["material"] or "PLA")
        self._nozzle = tk.StringVar(value=str(self._config["nozzle_diameter_mm"] or DEFAULT_NOZZLE_MM))
        self._current_run: AnalysisRun | None = None

        # A real, full Bambu Studio process-settings export (Process panel
        # -> right-click your preset -> Export), loaded so the "Export"
        # button can hand back a complete, directly-importable profile
        # instead of just the dozen keys this project's optimizer tracks.
        # Optional — everything works without one, same as before.
        self._full_profile: dict | None = None
        self._full_profile_path = tk.StringVar(value="")

        self._build_layout()

        saved_full_profile_path = self._config.get("full_profile_path") or ""
        if saved_full_profile_path:
            self._load_full_profile_from_path(saved_full_profile_path, show_errors=False)

    # -- layout -----------------------------------------------------
    def _build_layout(self) -> None:
        file_row = ttk.Frame(self)
        file_row.pack(fill="x", pady=(0, 4))
        ttk.Label(file_row, text="Model file:").pack(side="left")
        ttk.Entry(file_row, textvariable=self._model_path, width=60).pack(side="left", padx=6, fill="x", expand=True)
        ttk.Button(file_row, text="Browse...", command=self._browse_file).pack(side="left")

        recent_files = self._config.get("recent_files") or []
        if recent_files:
            recent_row = ttk.Frame(self)
            recent_row.pack(fill="x", pady=(0, 8))
            ttk.Label(recent_row, text="Recent:").pack(side="left")
            self._recent_var = tk.StringVar(value="")
            recent_menu = ttk.Combobox(
                recent_row, textvariable=self._recent_var, values=recent_files, state="readonly", width=70
            )
            recent_menu.pack(side="left", padx=6, fill="x", expand=True)
            recent_menu.bind("<<ComboboxSelected>>", self._on_recent_selected)

        options_row = ttk.Frame(self)
        options_row.pack(fill="x", pady=(0, 8))

        ttk.Label(options_row, text="Goal:").grid(row=0, column=0, sticky="w", padx=(0, 4))
        goal_menu = ttk.Combobox(
            options_row, textvariable=self._goal, values=list(GOAL_LABELS.values()), state="readonly", width=22
        )
        goal_menu.grid(row=0, column=1, padx=(0, 16))

        ttk.Label(options_row, text="Material:").grid(row=0, column=2, sticky="w", padx=(0, 4))
        material_menu = ttk.Combobox(
            options_row, textvariable=self._material, values=MATERIALS, state="readonly", width=10
        )
        material_menu.grid(row=0, column=3, padx=(0, 16))

        ttk.Label(options_row, text="Nozzle (mm):").grid(row=0, column=4, sticky="w", padx=(0, 4))
        ttk.Entry(options_row, textvariable=self._nozzle, width=6).grid(row=0, column=5)

        ttk.Label(
            self,
            text="Current Bambu Studio settings (edit to match your real Process panel — "
            "this tool can't read it directly):",
        ).pack(anchor="w")
        saved_settings = self._config.get("current_settings") or {}
        self._settings_editor = SettingsEditor(self, initial_settings=saved_settings or None)
        self._settings_editor.pack(fill="x", pady=(2, 8))

        profile_row = ttk.Frame(self)
        profile_row.pack(fill="x", pady=(0, 8))
        ttk.Button(
            profile_row, text="Load full Bambu Studio profile...", command=self._on_load_full_profile
        ).pack(side="left")
        self._full_profile_status_var = tk.StringVar(value="(optional — makes the export a complete, importable profile)")
        ttk.Label(profile_row, textvariable=self._full_profile_status_var, foreground="#555").pack(
            side="left", padx=8
        )

        action_row = ttk.Frame(self)
        action_row.pack(fill="x", pady=(0, 8))
        ttk.Button(action_row, text="Analyze", command=self._on_analyze).pack(side="left")
        self._export_btn = ttk.Button(action_row, text="Export recommendations...", command=self._on_export, state="disabled")
        self._export_btn.pack(side="left", padx=8)

        self._summary_var = tk.StringVar(value="")
        ttk.Label(self, textvariable=self._summary_var, foreground="#a04000", wraplength=860, justify="left").pack(
            anchor="w", pady=(0, 6)
        )

        ttk.Label(self, text="Recommended changes:").pack(anchor="w")
        table_container = ttk.Frame(self)
        table_container.pack(fill="both", expand=True)
        canvas = tk.Canvas(table_container, highlightthickness=0)
        scrollbar = ttk.Scrollbar(table_container, orient="vertical", command=canvas.yview)
        self._changes_table = ChangesTable(canvas)
        table_window = canvas.create_window((0, 0), window=self._changes_table, anchor="nw")

        def _sync_scrollregion(_event=None):
            canvas.configure(scrollregion=canvas.bbox("all"))

        def _sync_inner_width(event):
            # Keep the embedded frame exactly as wide as the visible canvas
            # (only a vertical scrollbar exists) so the Reason column's
            # wraplength has real room instead of being sliced off at the
            # canvas edge — this is the second half of the same "cut off
            # letters" bug, not just the width/wraplength clash on the
            # label itself.
            canvas.itemconfig(table_window, width=event.width)

        self._changes_table.bind("<Configure>", _sync_scrollregion)
        canvas.bind("<Configure>", _sync_inner_width)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self._status_var = tk.StringVar(value="Pick a .stl or .3mf file and click Analyze.")
        ttk.Label(self, textvariable=self._status_var, foreground="#555").pack(anchor="w", pady=(6, 0))

    # -- actions ------------------------------------------------------
    def _browse_file(self) -> None:
        path = filedialog.askopenfilename(
            title="Choose a model file",
            filetypes=[("3D model files", "*.stl *.3mf"), ("STL files", "*.stl"), ("3MF files", "*.3mf"), ("All files", "*.*")],
        )
        if path:
            self._model_path.set(path)

    def _on_recent_selected(self, _event=None) -> None:
        chosen = self._recent_var.get()
        if chosen:
            self._model_path.set(chosen)

    def _on_load_full_profile(self) -> None:
        path = filedialog.askopenfilename(
            title="Choose your exported Bambu Studio settings file",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return
        self._load_full_profile_from_path(path, show_errors=True)

    def _load_full_profile_from_path(self, path: str, *, show_errors: bool) -> None:
        """Loads `path` as a full Bambu Studio profile and, if it works,
        refreshes the settings grid from it so the fields you edit start
        from your real profile instead of this project's generic
        defaults. `show_errors=False` is used for the silent reload of a
        previously-saved path on launch — a since-deleted/moved file
        there shouldn't pop an error box every time the GUI opens."""
        try:
            profile = load_full_profile(path)
        except AnalysisError as exc:
            self._full_profile = None
            self._full_profile_path.set("")
            self._full_profile_status_var.set("(optional — makes the export a complete, importable profile)")
            if show_errors:
                messagebox.showerror(APP_TITLE, str(exc))
            return

        self._full_profile = profile
        self._full_profile_path.set(path)
        self._full_profile_status_var.set(f"Loaded: {Path(path).name} ({len(profile)} settings)")
        self._settings_editor.set_settings(current_settings_from_full_profile(profile))

    def _goal_from_label(self) -> PrintGoal:
        label = self._goal.get()
        for goal, text in GOAL_LABELS.items():
            if text == label:
                return goal
        return PrintGoal.BALANCED

    def _on_analyze(self) -> None:
        model_path = self._model_path.get().strip()
        if not model_path:
            messagebox.showwarning(APP_TITLE, "Choose a model file first.")
            return
        try:
            nozzle = float(self._nozzle.get())
        except ValueError:
            messagebox.showwarning(APP_TITLE, "Nozzle diameter must be a number, e.g. 0.4")
            return

        self._status_var.set("Analyzing...")
        self.update_idletasks()
        try:
            run = run_analysis(
                model_path,
                goal=self._goal_from_label(),
                material=self._material.get(),
                nozzle_diameter_mm=nozzle,
                current_settings=self._settings_editor.values(),
            )
        except AnalysisError as exc:
            self._status_var.set("Analysis failed.")
            messagebox.showerror(APP_TITLE, str(exc))
            return
        except Exception:  # noqa: BLE001 - show unexpected errors instead of crashing the GUI
            self._status_var.set("Analysis failed.")
            messagebox.showerror(APP_TITLE, "Unexpected error:\n\n" + traceback.format_exc())
            return

        self._current_run = run
        self._summary_var.set("\n".join(summary_lines(run)))
        self._changes_table.set_rows(change_rows(run))
        self._export_btn.configure(state="normal" if run.optimization.changes else "disabled")
        self._status_var.set(f"Done. {len(run.optimization.changes)} change(s) recommended.")
        self._remember_this_run(model_path, nozzle)

    def _remember_this_run(self, model_path: str, nozzle: float) -> None:
        """Persists the fields used for a successful analysis so the next
        launch starts from where you left off, instead of the generic
        defaults. Best-effort — a failed save here should never interrupt
        showing you the results you just got."""
        recent = add_recent_file(self._config.get("recent_files") or [], model_path)
        self._config = {
            "last_model_path": model_path,
            "goal_label": self._goal.get(),
            "material": self._material.get(),
            "nozzle_diameter_mm": str(nozzle),
            "current_settings": self._settings_editor.values(),
            "recent_files": recent,
            "full_profile_path": self._full_profile_path.get(),
        }
        save_config(self._config)

    def _on_export(self) -> None:
        if self._current_run is None:
            return
        approved = self._changes_table.approved_keys()
        if not approved:
            if not messagebox.askyesno(APP_TITLE, "No changes are checked. Export anyway (just a summary, no setting changes)?"):
                return

        out_dir = filedialog.askdirectory(title="Choose a folder to save the recommendations into")
        if not out_dir:
            return
        try:
            paths = export_files(self._current_run, approved, out_dir, full_profile=self._full_profile)
        except Exception:  # noqa: BLE001
            messagebox.showerror(APP_TITLE, "Couldn't save the export:\n\n" + traceback.format_exc())
            return

        names = "\n".join(p.name for p in paths)
        extra = (
            "\n\nThe *_bambu_studio_full_profile.json file is your complete profile with "
            "just the approved changes applied — import it in Bambu Studio's Process panel "
            "(right-click a preset -> Import) rather than retyping values."
            if len(paths) == 3
            else "\n\nOpen the .txt for a plain-language summary, or the .json for the raw "
            "key/value settings to enter into Bambu Studio's Process panel.\n\n"
            "Tip: use \"Load full Bambu Studio profile...\" above (load a profile you've "
            "exported from Bambu Studio's Process panel) to get a complete, directly-"
            "importable export instead."
        )
        messagebox.showinfo(APP_TITLE, f"Saved:\n{names}\n\nin {out_dir}{extra}")


def _set_window_icon(root: tk.Tk) -> None:
    """Best-effort app icon. Tries the .ico first (what Windows actually
    uses for the taskbar/title bar); falls back to the .png via
    iconphoto for platforms where iconbitmap doesn't take .ico. Never
    raises — a missing/unreadable icon file should never stop the GUI
    from opening."""
    ico_path = ASSETS_DIR / "app_icon.ico"
    png_path = ASSETS_DIR / "app_icon.png"
    try:
        if ico_path.exists():
            root.iconbitmap(default=str(ico_path))
            return
    except tk.TclError:
        pass
    try:
        if png_path.exists():
            icon_img = tk.PhotoImage(file=str(png_path))
            root.iconphoto(True, icon_img)
            root._bambu_icon_ref = icon_img  # keep a reference; Tk drops GC'd images
    except tk.TclError:
        pass


def main() -> None:
    root = tk.Tk()
    BambuCompanionApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
