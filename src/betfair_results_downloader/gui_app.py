from __future__ import annotations

import os
import platform
import queue
import subprocess
import sys
import threading
import traceback
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .config import DownloaderConfig
from .recommend import recommend_lookback_days
from .run import run_downloader, publish_to_azure_from_canonical
from .secrets import (
    credentials_path,
    ensure_credentials_file_exists,
    get_nested,
    load_credentials,
    load_credentials_template,
    save_credentials,
    set_credentials_path,
    set_nested,
    validate_credentials,
)

DEFAULT_USER_ID = "YourUserName"


def kv(key: str, value: object, *, width: int = 22) -> str:
    """
    Key/value formatter for report-style output.
    """
    k = str(key).strip()
    v = "" if value is None else str(value)
    return f"{k:<{width}} : {v}"


def _ordered_items(data: dict, preferred_order: list[str]) -> list[tuple[str, object]]:
    """
    Return items in preferred_order first (if present), then any remaining keys
    in original dict insertion order.
    """
    items: list[tuple[str, object]] = []
    seen: set[str] = set()
    for k in preferred_order:
        if k in data:
            items.append((k, data[k]))
            seen.add(k)
    for k, v in data.items():
        if k not in seen:
            items.append((k, v))
    return items


def format_block(title: str, data: object) -> str:
    """
    Consistent block formatting for Output pane.
    """
    header = f"\n=== {title} ==="
    if not data:
        return f"{header}\n(none)\n"

    if isinstance(data, dict):
        # Provide stable, human-friendly ordering for common blocks
        preferred: list[str] = []
        if title.lower().startswith("plan"):
            preferred = [
                "user_id",
                "days",
                "include_horses",
                "include_greyhounds",
                "enable_azure_sql",
                "dry_run",
                "last_settled_date_utc",
                "recommended_days",
                "recommendation_note",
            ]
        elif title.lower().startswith("download"):
            preferred = [
                "rows_downloaded",
                "rows_written",
                "from_date",
                "to_date",
                "event_types",
            ]
        elif title.lower().startswith("enrichment"):
            preferred = [
                "rows_before_enrich",
                "rows_after_enrich",
                "unique_market_ids",
                "unique_markets_seen",
                "unique_markets_enriched",
                "enrichment_mode",
                "cache_hits",
                "cache_misses",
                "cache_only",
                "api_only",
                "cache_and_api",
                "none",
                "cache_path",
                "snapshot_path",
                "requested_total",
                "returned_catalogues_total",
            ]
        elif title.lower().startswith("csv"):
            preferred = [
                "canonical_csv_path",
                "snapshot_csv_path",
                "rows_written",
                "deduped_rows_dropped",
            ]
        elif title.lower().startswith("azure"):
            preferred = [
                "prep_attempted",
                "publish_attempted",
                "dry_run",
                "enable_azure_sql",
                "rows_filtered",
                "markets_aggregated",
                "rows_to_write_count",
                "published",
                "message",
                "user_id",
            ]
        elif title.lower().startswith("publish-only"):
            preferred = [
                "canonical_path",
                "canonical_rows_read",
                "markets_aggregated",
                "existing_markets_in_azure_count",
                "new_markets_to_publish",
                "publish_attempted",
                "inserted_rows",
                "message",
            ]

        lines = [header]
        for k, v in _ordered_items(data, preferred):
            lines.append(kv(str(k), v))
        return "\n".join(lines) + "\n"

    # Fallback: treat as plain text / object
    return f"{header}\n{data}\n"


class FirstRunWizard(tk.Toplevel):
    """
    Minimal modal wizard to gather credentials/settings on first run.
    Allows choosing where to save the credentials file and where to store CSV outputs.
    """

    def __init__(self, master: tk.Misc, initial: dict, *, default_creds_path: Path):
        super().__init__(master)
        self.title("First run setup")
        self.resizable(False, False)

        self._result: dict | None = None

        # --- Vars ---
        self.var_creds_path = tk.StringVar(value=str(default_creds_path))
        self.var_results_dir = tk.StringVar(value=str(get_nested(initial, "paths.results_csv_dir", "")))

        self.var_bf_user = tk.StringVar(value=str(get_nested(initial, "betfair.username", "")))
        self.var_bf_pass = tk.StringVar(value=str(get_nested(initial, "betfair.password", "")))
        self.var_bf_appkey = tk.StringVar(value=str(get_nested(initial, "betfair.app_key", "")))

        self.var_user_id = tk.StringVar(value=str(get_nested(initial, "user.user_id", DEFAULT_USER_ID)))
        self.var_days = tk.StringVar(value=str(get_nested(initial, "user.days", 7)))
        self.var_horses = tk.BooleanVar(value=bool(get_nested(initial, "user.include_horses", True)))
        self.var_greyhounds = tk.BooleanVar(value=bool(get_nested(initial, "user.include_greyhounds", True)))
        self.var_dry_run = tk.BooleanVar(value=bool(get_nested(initial, "user.dry_run", True)))

        self.var_enable_azure = tk.BooleanVar(value=bool(get_nested(initial, "user.enable_azure_sql", False)))
        self.var_az_server = tk.StringVar(value=str(get_nested(initial, "azure_sql.server", "")))
        self.var_az_db = tk.StringVar(value=str(get_nested(initial, "azure_sql.database", "")))
        self.var_az_user = tk.StringVar(value=str(get_nested(initial, "azure_sql.username", "")))
        self.var_az_pass = tk.StringVar(value=str(get_nested(initial, "azure_sql.password", "")))
        self.var_az_driver = tk.StringVar(
            value=str(get_nested(initial, "azure_sql.driver", "ODBC Driver 18 for SQL Server"))
        )

        self._build()
        self._refresh_azure_state()

        # Modal behavior
        self.transient(master)
        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self._cancel)

        # Center on parent
        self.update_idletasks()
        try:
            px = master.winfo_rootx()
            py = master.winfo_rooty()
            pw = master.winfo_width()
            ph = master.winfo_height()
            w = self.winfo_width()
            h = self.winfo_height()
            self.geometry(f"+{px + (pw - w)//2}+{py + (ph - h)//2}")
        except Exception:
            pass

    @property
    def result(self) -> dict | None:
        return self._result

    def _choose_creds_file(self) -> None:
        initial = self.var_creds_path.get().strip()
        initial_dir = str(Path(initial).parent) if initial else str(Path.cwd())

        filename = filedialog.asksaveasfilename(
            parent=self,
            title="Choose where to save credentials.json",
            initialdir=initial_dir,
            initialfile="credentials.json",
            defaultextension=".json",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
        )
        if filename:
            self.var_creds_path.set(str(Path(filename)))

    def _choose_results_dir(self) -> None:
        initial = self.var_results_dir.get().strip()
        initial_dir = initial if initial else str(Path.home())

        folder = filedialog.askdirectory(
            parent=self,
            title="Choose where to store CSV results",
            initialdir=initial_dir,
            mustexist=False,
        )
        if folder:
            self.var_results_dir.set(str(Path(folder)))

    def _build(self) -> None:
        frm = ttk.Frame(self, padding=12)
        frm.grid(row=0, column=0, sticky="nsew")
        frm.columnconfigure(1, weight=1)

        ttk.Label(
            frm,
            text=(
                "Welcome! Let’s set up Betfair Results Downloader.\n"
                "Choose where to save your credentials and where to store CSV outputs."
            ),
            justify="left",
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 10))

        # --- Paths ---
        pf = ttk.LabelFrame(frm, text="Paths", padding=10)
        pf.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(0, 10))
        pf.columnconfigure(1, weight=1)

        ttk.Label(pf, text="Credentials file").grid(row=0, column=0, sticky="w")
        ttk.Entry(pf, textvariable=self.var_creds_path).grid(row=0, column=1, sticky="ew", padx=(10, 10))
        ttk.Button(pf, text="Browse…", command=self._choose_creds_file).grid(row=0, column=2, sticky="e")

        ttk.Label(pf, text="CSV results folder").grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(pf, textvariable=self.var_results_dir).grid(
            row=1, column=1, sticky="ew", padx=(10, 10), pady=(6, 0)
        )
        ttk.Button(pf, text="Browse…", command=self._choose_results_dir).grid(row=1, column=2, sticky="e", pady=(6, 0))

        # --- Betfair ---
        bf = ttk.LabelFrame(frm, text="Betfair", padding=10)
        bf.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(0, 10))
        bf.columnconfigure(1, weight=1)

        ttk.Label(bf, text="Username").grid(row=0, column=0, sticky="w")
        ttk.Entry(bf, textvariable=self.var_bf_user).grid(row=0, column=1, columnspan=2, sticky="ew", padx=(10, 0))

        ttk.Label(bf, text="Password").grid(row=1, column=0, sticky="w")
        ttk.Entry(bf, textvariable=self.var_bf_pass, show="•").grid(
            row=1, column=1, columnspan=2, sticky="ew", padx=(10, 0)
        )

        ttk.Label(bf, text="App Key").grid(row=2, column=0, sticky="w")
        ttk.Entry(bf, textvariable=self.var_bf_appkey, show="•").grid(
            row=2, column=1, columnspan=2, sticky="ew", padx=(10, 0)
        )

        # --- Run defaults ---
        rc = ttk.LabelFrame(frm, text="Run defaults", padding=10)
        rc.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(0, 10))
        rc.columnconfigure(1, weight=1)

        ttk.Label(rc, text="User ID").grid(row=0, column=0, sticky="w")
        ttk.Entry(rc, textvariable=self.var_user_id).grid(row=0, column=1, columnspan=2, sticky="ew", padx=(10, 0))

        ttk.Label(rc, text="Days to download").grid(row=1, column=0, sticky="w")
        ttk.Entry(rc, textvariable=self.var_days, width=8).grid(row=1, column=1, sticky="w", padx=(10, 0))

        ttk.Checkbutton(rc, text="Include Horses (eventTypeId 7)", variable=self.var_horses).grid(
            row=2, column=0, columnspan=3, sticky="w", pady=(6, 0)
        )
        ttk.Checkbutton(rc, text="Include Greyhounds (eventTypeId 4339)", variable=self.var_greyhounds).grid(
            row=3, column=0, columnspan=3, sticky="w"
        )

        ttk.Checkbutton(rc, text="Dry run (recommended)", variable=self.var_dry_run).grid(
            row=4, column=0, columnspan=3, sticky="w", pady=(6, 0)
        )

        # --- Azure (optional) ---
        az = ttk.LabelFrame(frm, text="Azure SQL (optional)", padding=10)
        az.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(0, 10))
        az.columnconfigure(1, weight=1)

        ttk.Checkbutton(
            az,
            text="Enable Azure upload",
            variable=self.var_enable_azure,
            command=self._refresh_azure_state,
        ).grid(row=0, column=0, columnspan=3, sticky="w")

        ttk.Label(az, text="Server").grid(row=1, column=0, sticky="w")
        self.ent_az_server = ttk.Entry(az, textvariable=self.var_az_server)
        self.ent_az_server.grid(row=1, column=1, columnspan=2, sticky="ew", padx=(10, 0))

        ttk.Label(az, text="Database").grid(row=2, column=0, sticky="w")
        self.ent_az_db = ttk.Entry(az, textvariable=self.var_az_db)
        self.ent_az_db.grid(row=2, column=1, columnspan=2, sticky="ew", padx=(10, 0))

        ttk.Label(az, text="Username").grid(row=3, column=0, sticky="w")
        self.ent_az_user = ttk.Entry(az, textvariable=self.var_az_user)
        self.ent_az_user.grid(row=3, column=1, columnspan=2, sticky="ew", padx=(10, 0))

        ttk.Label(az, text="Password").grid(row=4, column=0, sticky="w")
        self.ent_az_pass = ttk.Entry(az, textvariable=self.var_az_pass, show="•")
        self.ent_az_pass.grid(row=4, column=1, columnspan=2, sticky="ew", padx=(10, 0))

        ttk.Label(az, text="ODBC Driver").grid(row=5, column=0, sticky="w")
        self.ent_az_driver = ttk.Entry(az, textvariable=self.var_az_driver)
        self.ent_az_driver.grid(row=5, column=1, columnspan=2, sticky="ew", padx=(10, 0))

        # --- Buttons ---
        btns = ttk.Frame(frm)
        btns.grid(row=5, column=0, columnspan=3, sticky="ew")
        btns.columnconfigure(0, weight=1)

        ttk.Button(btns, text="Cancel", command=self._cancel).grid(row=0, column=1, sticky="e", padx=(0, 8))
        ttk.Button(btns, text="Save & Continue", command=self._save).grid(row=0, column=2, sticky="e")

    def _refresh_azure_state(self) -> None:
        enabled = bool(self.var_enable_azure.get())
        state = "normal" if enabled else "disabled"
        for w in (self.ent_az_server, self.ent_az_db, self.ent_az_user, self.ent_az_pass, self.ent_az_driver):
            try:
                w.configure(state=state)
            except Exception:
                pass

    def _cancel(self) -> None:
        self._result = None
        self.grab_release()
        self.destroy()

    def _save(self) -> None:
        # Lightweight validation
        if not self.var_creds_path.get().strip():
            messagebox.showerror("Missing field", "Credentials file path is required.", parent=self)
            return

        if not self.var_results_dir.get().strip():
            messagebox.showerror("Missing field", "CSV results folder is required.", parent=self)
            return

        if not self.var_bf_user.get().strip():
            messagebox.showerror("Missing field", "Betfair username is required.", parent=self)
            return
        if not self.var_bf_pass.get():
            messagebox.showerror("Missing field", "Betfair password is required.", parent=self)
            return
        if not self.var_bf_appkey.get():
            messagebox.showerror("Missing field", "Betfair app key is required.", parent=self)
            return

        try:
            days = int(self.var_days.get().strip())
            if days <= 0:
                raise ValueError
        except Exception:
            messagebox.showerror("Invalid field", "Days to download must be a positive integer.", parent=self)
            return

        self._result = {
            "__credentials_path__": self.var_creds_path.get().strip(),
            "paths.results_csv_dir": self.var_results_dir.get().strip(),
            "betfair.username": self.var_bf_user.get().strip(),
            "betfair.password": self.var_bf_pass.get(),
            "betfair.app_key": self.var_bf_appkey.get(),
            "user.user_id": (self.var_user_id.get().strip() or DEFAULT_USER_ID),
            "user.days": int(self.var_days.get().strip()),
            "user.include_horses": bool(self.var_horses.get()),
            "user.include_greyhounds": bool(self.var_greyhounds.get()),
            "user.enable_azure_sql": bool(self.var_enable_azure.get()),
            "user.dry_run": bool(self.var_dry_run.get()),
            "azure_sql.server": self.var_az_server.get().strip(),
            "azure_sql.database": self.var_az_db.get().strip(),
            "azure_sql.username": self.var_az_user.get().strip(),
            "azure_sql.password": self.var_az_pass.get(),
            "azure_sql.driver": self.var_az_driver.get().strip(),
        }

        self.grab_release()
        self.destroy()


class App(ttk.Frame):
    def __init__(self, master: tk.Tk):
        super().__init__(master, padding=12)
        self.master = master

        # Resolve credentials path (may be user-selected)
        self._creds_path = credentials_path()
        first_run = not self._creds_path.exists()

        # If first run, start from template (do not auto-create file yet)
        if first_run:
            self.creds = load_credentials_template()
        else:
            self.creds = load_credentials()

        # --- Vars (Paths) ---
        self.var_creds_file = tk.StringVar(value=str(self._creds_path))
        self.var_results_dir = tk.StringVar(value=str(get_nested(self.creds, "paths.results_csv_dir", "")))

        # --- Vars (Betfair) ---
        self.var_bf_user = tk.StringVar(value=str(get_nested(self.creds, "betfair.username", "")))
        self.var_bf_pass = tk.StringVar(value=str(get_nested(self.creds, "betfair.password", "")))
        self.var_bf_appkey = tk.StringVar(value=str(get_nested(self.creds, "betfair.app_key", "")))

        # --- Vars (Run config) ---
        self.var_user_id = tk.StringVar(value=str(get_nested(self.creds, "user.user_id", DEFAULT_USER_ID)))
        self.var_days = tk.StringVar(value=str(get_nested(self.creds, "user.days", 7)))
        self.var_reco_note = tk.StringVar(value="")
        self._reco_last_settled_date_utc: object | None = None
        self._reco_days: int | None = None

        self.var_horses = tk.BooleanVar(value=bool(get_nested(self.creds, "user.include_horses", True)))
        self.var_greyhounds = tk.BooleanVar(value=bool(get_nested(self.creds, "user.include_greyhounds", True)))

        self.var_enable_azure = tk.BooleanVar(value=bool(get_nested(self.creds, "user.enable_azure_sql", False)))
        self.var_dry_run = tk.BooleanVar(value=bool(get_nested(self.creds, "user.dry_run", True)))

        # --- Vars (Azure unlock) ---
        self.var_allow_publish = tk.BooleanVar(value=False)
        self.var_publish_text = tk.StringVar(value="")

        # --- Vars (Azure) ---
        self.var_az_server = tk.StringVar(value=str(get_nested(self.creds, "azure_sql.server", "")))
        self.var_az_db = tk.StringVar(value=str(get_nested(self.creds, "azure_sql.database", "")))
        self.var_az_user = tk.StringVar(value=str(get_nested(self.creds, "azure_sql.username", "")))
        self.var_az_pass = tk.StringVar(value=str(get_nested(self.creds, "azure_sql.password", "")))
        self.var_az_driver = tk.StringVar(value=str(get_nested(self.creds, "azure_sql.driver", "ODBC Driver 18 for SQL Server")))

        # --- Runtime / status ---
        self._status_q: "queue.Queue[tuple[str, object]]" = queue.Queue()
        self._worker_thread: threading.Thread | None = None
        self._last_results_dir: Path | None = None
        self._last_artifacts_dir: Path | None = None

        self._build()
        self._refresh_lookback_recommendation()
        self.master.after(100, self._poll_status_queue)
        self._refresh_publish_unlock_state()
        self.var_publish_text.trace_add("write", lambda *_: self._refresh_publish_button_state())

        # First-run setup wizard
        if first_run:
            self._run_first_time_setup()
        else:
            # Ensure the resolved file exists going forward
            ensure_credentials_file_exists()

    # ---------------- First-run onboarding ----------------

    def _run_first_time_setup(self) -> None:
        self._log("First run detected: launching setup wizard…")

        wiz = FirstRunWizard(self.master, self.creds, default_creds_path=self._creds_path)
        self.master.wait_window(wiz)

        if wiz.result is None:
            self._log("First run setup cancelled. You can edit settings in the GUI and click Save Settings.")
            return

        # Apply wizard values into creds
        chosen_creds_path_raw = str(wiz.result.get("__credentials_path__", "")).strip()
        chosen_creds_path = Path(chosen_creds_path_raw) if chosen_creds_path_raw else self._creds_path

        # Persist chosen credentials path
        set_credentials_path(chosen_creds_path)
        self._creds_path = credentials_path()
        self.var_creds_file.set(str(self._creds_path))

        for k, v in wiz.result.items():
            if k == "__credentials_path__":
                continue
            set_nested(self.creds, k, v)

        # Update bound Vars
        self.var_results_dir.set(str(get_nested(self.creds, "paths.results_csv_dir", "")))
        self._refresh_lookback_recommendation()

        self.var_bf_user.set(str(get_nested(self.creds, "betfair.username", "")))
        self.var_bf_pass.set(str(get_nested(self.creds, "betfair.password", "")))
        self.var_bf_appkey.set(str(get_nested(self.creds, "betfair.app_key", "")))

        self.var_user_id.set(str(get_nested(self.creds, "user.user_id", DEFAULT_USER_ID)))
        self.var_days.set(str(get_nested(self.creds, "user.days", 7)))
        self.var_horses.set(bool(get_nested(self.creds, "user.include_horses", True)))
        self.var_greyhounds.set(bool(get_nested(self.creds, "user.include_greyhounds", True)))
        self.var_enable_azure.set(bool(get_nested(self.creds, "user.enable_azure_sql", False)))
        self.var_dry_run.set(bool(get_nested(self.creds, "user.dry_run", True)))

        self.var_az_server.set(str(get_nested(self.creds, "azure_sql.server", "")))
        self.var_az_db.set(str(get_nested(self.creds, "azure_sql.database", "")))
        self.var_az_user.set(str(get_nested(self.creds, "azure_sql.username", "")))
        self.var_az_pass.set(str(get_nested(self.creds, "azure_sql.password", "")))
        self.var_az_driver.set(str(get_nested(self.creds, "azure_sql.driver", "ODBC Driver 18 for SQL Server")))

        try:
            save_credentials(self.creds)  # saves to resolved credentials_path() via pointer
        except Exception as e:
            messagebox.showerror("Save failed", str(e))
            self._log(f"ERROR saving credentials: {e}")
            return

        # Friendly checklist
        self._log(f"✅ credentials file created: {self._creds_path}")

        results_dir_raw = get_nested(self.creds, "paths.results_csv_dir", None)
        if results_dir_raw:
            self._log(f"✅ results directory configured: {results_dir_raw}")
        else:
            self._log("⚠️ results directory not configured yet (paths.results_csv_dir).")

        if bool(get_nested(self.creds, "user.dry_run", True)):
            self._log("✅ dry-run enabled (safe-by-default)")
        else:
            self._log("⚠️ dry-run disabled (publishing still requires GUI unlock + confirmation)")

        if bool(get_nested(self.creds, "user.enable_azure_sql", False)):
            self._log("ℹ️ Azure enabled (still safe-by-default)")
        else:
            self._log("ℹ️ Azure disabled")

        messagebox.showinfo("Setup complete", "First run setup saved. You can now run the downloader.")
        self._refresh_publish_unlock_state()

    # ---------------- UI ----------------

    def _build(self) -> None:
        self.master.title("Betfair Results Downloader (GUI)")
        self.master.minsize(820, 600)

        self.grid(row=0, column=0, sticky="nsew")
        self.master.columnconfigure(0, weight=1)
        self.master.rowconfigure(0, weight=1)

        self.columnconfigure(0, weight=1)
        self.rowconfigure(4, weight=1)

        # --- Paths ---
        pf = ttk.LabelFrame(self, text="Paths", padding=10)
        pf.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        pf.columnconfigure(1, weight=1)

        ttk.Label(pf, text="Credentials file").grid(row=0, column=0, sticky="w")
        ttk.Entry(pf, textvariable=self.var_creds_file, state="readonly").grid(
            row=0, column=1, sticky="ew", padx=(10, 10)
        )
        ttk.Button(pf, text="Change…", command=self.on_change_credentials_file).grid(row=0, column=2, sticky="e")

        ttk.Label(pf, text="CSV results folder").grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(pf, textvariable=self.var_results_dir).grid(
            row=1, column=1, sticky="ew", padx=(10, 10), pady=(6, 0)
        )
        ttk.Button(pf, text="Browse…", command=self.on_choose_results_folder).grid(
            row=1, column=2, sticky="e", pady=(6, 0)
        )

        # --- Betfair ---
        bf = ttk.LabelFrame(self, text="Betfair Credentials", padding=10)
        bf.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        bf.columnconfigure(1, weight=1)

        ttk.Label(bf, text="Username").grid(row=0, column=0, sticky="w")
        ttk.Entry(bf, textvariable=self.var_bf_user).grid(row=0, column=1, sticky="ew", padx=(10, 0))

        ttk.Label(bf, text="Password").grid(row=1, column=0, sticky="w")
        ttk.Entry(bf, textvariable=self.var_bf_pass, show="•").grid(row=1, column=1, sticky="ew", padx=(10, 0))

        ttk.Label(bf, text="App Key").grid(row=2, column=0, sticky="w")
        ttk.Entry(bf, textvariable=self.var_bf_appkey, show="•").grid(row=2, column=1, sticky="ew", padx=(10, 0))

        # --- Run config ---
        rc = ttk.LabelFrame(self, text="Run Configuration", padding=10)
        rc.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        for i in range(4):
            rc.columnconfigure(i, weight=1)

        ttk.Label(rc, text="User ID").grid(row=0, column=0, sticky="w")
        ttk.Entry(rc, textvariable=self.var_user_id).grid(row=0, column=1, sticky="ew", padx=(10, 20))

        ttk.Label(rc, text="Days to download").grid(row=0, column=2, sticky="w")
        ttk.Entry(rc, textvariable=self.var_days, width=8).grid(row=0, column=3, sticky="w", padx=(10, 0))

        ttk.Label(rc, textvariable=self.var_reco_note, wraplength=680).grid(
            row=1, column=0, columnspan=4, sticky="w", pady=(4, 0)
        )

        ttk.Checkbutton(rc, text="Horses (eventTypeId 7)", variable=self.var_horses).grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(6, 0)
        )
        ttk.Checkbutton(rc, text="Greyhounds (eventTypeId 4339)", variable=self.var_greyhounds).grid(
            row=2, column=2, columnspan=2, sticky="w", pady=(6, 0)
        )

        # --- Azure ---
        az = ttk.LabelFrame(self, text="Azure SQL (Optional)", padding=10)
        az.grid(row=3, column=0, sticky="ew", pady=(0, 10))
        az.columnconfigure(1, weight=1)

        ttk.Checkbutton(
            az,
            text="Enable Azure upload",
            variable=self.var_enable_azure,
            command=self._refresh_publish_unlock_state,
        ).grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(
            az,
            text="Dry run (recommended)",
            variable=self.var_dry_run,
            command=self._refresh_publish_unlock_state,
        ).grid(row=0, column=1, sticky="w", padx=(10, 0))

        self.chk_allow_publish = ttk.Checkbutton(
            az,
            text="Allow non-dry-run publish (writes to Azure)",
            variable=self.var_allow_publish,
            command=self._refresh_publish_unlock_state,
        )
        self.chk_allow_publish.grid(row=1, column=0, sticky="w", pady=(6, 0))

        ttk.Label(az, text="Type PUBLISH to enable").grid(row=1, column=1, sticky="w", padx=(10, 0), pady=(6, 0))
        self.ent_publish_text = ttk.Entry(az, textvariable=self.var_publish_text, width=16)
        self.ent_publish_text.grid(row=1, column=1, sticky="e", padx=(10, 0), pady=(6, 0))

        ttk.Label(az, text="Server").grid(row=2, column=0, sticky="w")
        ttk.Entry(az, textvariable=self.var_az_server).grid(row=2, column=1, sticky="ew", padx=(10, 0))

        ttk.Label(az, text="Database").grid(row=3, column=0, sticky="w")
        ttk.Entry(az, textvariable=self.var_az_db).grid(row=3, column=1, sticky="ew", padx=(10, 0))

        ttk.Label(az, text="Username").grid(row=4, column=0, sticky="w")
        ttk.Entry(az, textvariable=self.var_az_user).grid(row=4, column=1, sticky="ew", padx=(10, 0))

        ttk.Label(az, text="Password").grid(row=5, column=0, sticky="w")
        ttk.Entry(az, textvariable=self.var_az_pass, show="•").grid(row=5, column=1, sticky="ew", padx=(10, 0))

        ttk.Label(az, text="ODBC Driver").grid(row=6, column=0, sticky="w")
        ttk.Entry(az, textvariable=self.var_az_driver).grid(row=6, column=1, sticky="ew", padx=(10, 0))

        # --- Output + buttons ---
        out = ttk.LabelFrame(self, text="Output", padding=10)
        out.grid(row=4, column=0, sticky="nsew")
        out.columnconfigure(0, weight=1)
        out.rowconfigure(1, weight=1)

        self.progress = ttk.Progressbar(out, mode="indeterminate")
        self.progress.grid(row=0, column=0, sticky="ew", pady=(0, 8))

        self.txt = tk.Text(out, height=12, wrap="word")
        self.txt.grid(row=1, column=0, sticky="nsew")

        # Output polish: monospaced font (improves kv() alignment) + light padding
        try:
            sys_name = platform.system().lower()
            if "windows" in sys_name:
                font = ("Consolas", 10)
            elif "darwin" in sys_name or "mac" in sys_name:
                font = ("Menlo", 11)
            else:
                font = ("Courier New", 10)
            self.txt.configure(font=font, padx=6, pady=6)
        except Exception:
            pass

        self._log("Loaded credentials. Sensitive fields are masked in UI display only.")
        self._log("Tip: Dry run is ON by default. Non-dry-run publishing requires explicit unlock + confirmation.")

        btns = ttk.Frame(self)
        btns.grid(row=5, column=0, sticky="ew", pady=(10, 0))
        btns.columnconfigure(0, weight=1)

        self.btn_clear = ttk.Button(btns, text="Clear Output", command=self.on_clear)
        self.btn_clear.grid(row=0, column=0, sticky="w")

        self.btn_copy = ttk.Button(btns, text="Copy Summary", command=self.on_copy_summary)
        self.btn_copy.grid(row=0, column=1, sticky="w", padx=(10, 0))

        self.btn_open_artifacts = ttk.Button(
            btns, text="Open Artifacts Folder", command=self.on_open_artifacts_folder, state="disabled"
        )
        self.btn_open_artifacts.grid(row=0, column=2, sticky="w", padx=(10, 0))

        self.btn_open = ttk.Button(btns, text="Open Results Folder", command=self.on_open_results_folder, state="disabled")
        self.btn_open.grid(row=0, column=3, sticky="w", padx=(10, 0))

        self.btn_save = ttk.Button(btns, text="Save Settings", command=self.on_save)
        self.btn_save.grid(row=0, column=4, sticky="e", padx=(0, 8))

        self.btn_validate = ttk.Button(btns, text="Validate", command=self.on_validate)
        self.btn_validate.grid(row=0, column=5, sticky="e", padx=(0, 8))

        self.btn_run = ttk.Button(btns, text="Run Downloader", command=self.on_run)
        self.btn_run.grid(row=0, column=6, sticky="e")

        self.btn_publish = ttk.Button(btns, text="Publish to Azure", command=self.on_publish_only)
        self.btn_publish.grid(row=0, column=7, sticky="e", padx=(8, 0))

    # ---------------- Helpers ----------------

    def _log(self, msg: str) -> None:
        self.txt.insert("end", msg.rstrip() + "\n")
        self.txt.see("end")

    def _log_block(self, title: str, data: object) -> None:
        self._log(format_block(title, data).rstrip())

    def _sync_to_creds(self) -> None:
        # Paths
        set_nested(self.creds, "paths.results_csv_dir", self.var_results_dir.get().strip())

        # Betfair
        set_nested(self.creds, "betfair.username", self.var_bf_user.get().strip())
        set_nested(self.creds, "betfair.password", self.var_bf_pass.get())
        set_nested(self.creds, "betfair.app_key", self.var_bf_appkey.get())

        # User/run config
        set_nested(self.creds, "user.user_id", (self.var_user_id.get().strip() or DEFAULT_USER_ID))
        set_nested(self.creds, "user.days", int(self.var_days.get().strip()))
        set_nested(self.creds, "user.include_horses", bool(self.var_horses.get()))
        set_nested(self.creds, "user.include_greyhounds", bool(self.var_greyhounds.get()))
        set_nested(self.creds, "user.enable_azure_sql", bool(self.var_enable_azure.get()))
        set_nested(self.creds, "user.dry_run", bool(self.var_dry_run.get()))

        # Azure SQL
        set_nested(self.creds, "azure_sql.server", self.var_az_server.get().strip())
        set_nested(self.creds, "azure_sql.database", self.var_az_db.get().strip())
        set_nested(self.creds, "azure_sql.username", self.var_az_user.get().strip())
        set_nested(self.creds, "azure_sql.password", self.var_az_pass.get())
        set_nested(self.creds, "azure_sql.driver", self.var_az_driver.get().strip())

    def _build_config_from_ui(self) -> DownloaderConfig:
        days = int(self.var_days.get().strip())
        if days <= 0:
            raise ValueError("Days to download must be a positive integer.")

        return DownloaderConfig(
            days=days,
            include_horses=bool(self.var_horses.get()),
            include_greyhounds=bool(self.var_greyhounds.get()),
            enable_azure_sql=bool(self.var_enable_azure.get()),
            dry_run=bool(self.var_dry_run.get()),
            user_id=self.var_user_id.get().strip() or DEFAULT_USER_ID,
        )

    def _refresh_lookback_recommendation(self) -> None:
        results_dir_raw = self.var_results_dir.get().strip()
        if not results_dir_raw:
            self._reco_days = 90
            self._reco_last_settled_date_utc = None
            self.var_reco_note.set(
                "No existing data - Recommend 90 days capture, however this may take some time."
            )
            self.var_days.set(str(self._reco_days))
            return

        try:
            recommended_days, note, last_settled_utc = recommend_lookback_days(Path(results_dir_raw))
        except Exception:
            recommended_days, note, last_settled_utc = 90, (
                "No existing data - Recommend 90 days capture, however this may take some time."
            ), None

        self._reco_days = int(recommended_days)
        self._reco_last_settled_date_utc = last_settled_utc
        self.var_reco_note.set(note)
        self.var_days.set(str(self._reco_days))

    def _set_running_state(self, running: bool) -> None:
        state = "disabled" if running else "normal"
        self.btn_run.configure(state=state)
        if running:
            self.btn_publish.configure(state="disabled")
        else:
            self._refresh_publish_button_state()
        self.btn_save.configure(state=state)
        self.btn_validate.configure(state=state)
        self.btn_clear.configure(state="normal")
        self.btn_copy.configure(state="normal")
        self.btn_open.configure(state=("normal" if (not running and self._last_results_dir is not None) else "disabled"))
        self.btn_open_artifacts.configure(
            state=("normal" if (not running and self._last_artifacts_dir is not None) else "disabled")
        )

        if running:
            self.progress.start(10)
        else:
            self.progress.stop()

    def _refresh_publish_unlock_state(self) -> None:
        enable_az = bool(self.var_enable_azure.get())
        dry_run = bool(self.var_dry_run.get())

        if (not enable_az) or dry_run:
            self.var_allow_publish.set(False)
            self.var_publish_text.set("")
            try:
                self.chk_allow_publish.configure(state="disabled")
                self.ent_publish_text.configure(state="disabled")
            except Exception:
                pass
            self._refresh_publish_button_state()
            return

        try:
            self.chk_allow_publish.configure(state="normal")
            self.ent_publish_text.configure(state="normal")
        except Exception:
            pass

        self._refresh_publish_button_state()

    def _azure_credentials_ok(self) -> bool:
        try:
            self._sync_to_creds()
        except Exception:
            return False

        v = validate_credentials(self.creds)
        if not v.ok:
            return False

        user = self.creds.get("user", {}) or {}
        db_user_id = (user.get("db_user_id") or "").strip()
        if not db_user_id:
            return False

        return True

    def _refresh_publish_button_state(self) -> None:
        enable_az = bool(self.var_enable_azure.get())
        dry_run = bool(self.var_dry_run.get())
        unlocked = bool(self.var_allow_publish.get()) and (self.var_publish_text.get().strip() == "PUBLISH")

        can_publish = enable_az and self._azure_credentials_ok() and (dry_run or unlocked)
        try:
            self.btn_publish.configure(state=("normal" if can_publish else "disabled"))
        except Exception:
            pass

    def _status(self, msg: str) -> None:
        self._status_q.put(("log", msg))

    def _poll_status_queue(self) -> None:
        try:
            while True:
                kind, payload = self._status_q.get_nowait()
                if kind == "log":
                    self._log(str(payload))
                elif kind == "done":
                    self._handle_run_done(payload)
                elif kind == "publish_done":
                    self._handle_publish_done(payload)
                elif kind == "error":
                    self._handle_run_error(payload)
        except queue.Empty:
            pass
        self.master.after(100, self._poll_status_queue)

    def _handle_run_done(self, result: object) -> None:
        self._set_running_state(False)

        if isinstance(result, dict) and result.get("message"):
            self._log(str(result["message"]))
        else:
            self._log("Run completed (GUI branch).")

        enrich_block: dict | None = None
        if isinstance(result, dict):
            self._log_block("Plan", result.get("plan"))
            self._log_block("Download summary", result.get("download"))
            self._log_block("Enrichment summary", result.get("enrich"))
            self._log_block("CSV outputs", result.get("csv"))
            self._log_block("Azure summary", result.get("azure"))
            if isinstance(result.get("enrich"), dict):
                enrich_block = result.get("enrich")

        # Enable Results folder if configured + exists
        results_dir_raw = get_nested(self.creds, "paths.results_csv_dir", None)
        if results_dir_raw:
            self._last_results_dir = Path(str(results_dir_raw))
            if self._last_results_dir.exists():
                self.btn_open.configure(state="normal")

        # Enable Artifacts folder if enrichment reported cache/snapshot paths
        self._last_artifacts_dir = None
        if enrich_block:
            cache_path = enrich_block.get("cache_path")
            snapshot_path = enrich_block.get("snapshot_path")

            candidate_paths: list[Path] = []
            if cache_path:
                candidate_paths.append(Path(str(cache_path)))
            if snapshot_path:
                candidate_paths.append(Path(str(snapshot_path)))

            for p in candidate_paths:
                try:
                    if p.exists():
                        self._last_artifacts_dir = p.parent
                        break
                except Exception:
                    continue

        if self._last_artifacts_dir is not None and self._last_artifacts_dir.exists():
            self.btn_open_artifacts.configure(state="normal")

        messagebox.showinfo("Run complete", "Run finished. See Output for details.")

    def _handle_publish_done(self, result: object) -> None:
        self._set_running_state(False)

        if isinstance(result, dict) and result.get("message"):
            self._log(str(result["message"]))
        else:
            self._log("Publish-only completed.")

        if isinstance(result, dict):
            summary = result.get("summary")
            if summary is not None:
                self._log_block("Publish-only summary", summary)

            if result.get("ok") is False:
                messagebox.showerror("Publish failed", str(result.get("message", "Publish failed.")))
                return

        messagebox.showinfo("Publish complete", "Publish-only run finished. See Output for details.")

    def _handle_run_error(self, err_text: object) -> None:
        self._set_running_state(False)
        self._log("ERROR:")
        self._log(str(err_text))
        messagebox.showerror("Run failed", str(err_text))

    def _ask_confirm_publish_from_mainthread(self, *, user_id: str, markets: int, rows: int) -> bool:
        msg = (
            "You are about to WRITE to Azure SQL (non-dry-run).\n\n"
            f"UserID: {user_id}\n"
            f"Markets to write: {markets:,}\n"
            f"Rows to write: {rows:,}\n\n"
            "This will DELETE existing rows for this UserID and then INSERT the new rows.\n\n"
            "Proceed?"
        )
        return messagebox.askokcancel("Confirm Azure Publish", msg, icon="warning")

    def _confirm_publish_cb_threadsafe(self, *, user_id: str, markets: int, rows: int) -> bool:
        ev = threading.Event()
        result_holder: dict[str, bool] = {"ok": False}

        def _do():
            try:
                result_holder["ok"] = self._ask_confirm_publish_from_mainthread(
                    user_id=user_id, markets=markets, rows=rows
                )
            finally:
                ev.set()

        self.master.after(0, _do)
        ev.wait()
        return bool(result_holder["ok"])

    def _open_folder(self, path: Path) -> None:
        try:
            if os.name == "nt":
                os.startfile(str(path))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.run(["open", str(path)], check=False)
            else:
                subprocess.run(["xdg-open", str(path)], check=False)
        except Exception as e:
            messagebox.showerror("Open folder failed", str(e))

    # ---------------- Actions ----------------

    def on_choose_results_folder(self) -> None:
        initial = self.var_results_dir.get().strip()
        initial_dir = initial if initial else str(Path.home())

        folder = filedialog.askdirectory(
            parent=self.master,
            title="Choose where to store CSV results",
            initialdir=initial_dir,
            mustexist=False,
        )
        if folder:
            self.var_results_dir.set(str(Path(folder)))
            self._log(f"Selected results folder: {folder}")
            self._refresh_lookback_recommendation()

    def on_change_credentials_file(self) -> None:
        initial = self.var_creds_file.get().strip()
        initial_dir = str(Path(initial).parent) if initial else str(Path.cwd())

        filename = filedialog.asksaveasfilename(
            parent=self.master,
            title="Choose where to save credentials.json",
            initialdir=initial_dir,
            initialfile="credentials.json",
            defaultextension=".json",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
        )
        if not filename:
            return

        new_path = Path(filename)

        try:
            # Save current state to new location, then point future runs to it
            self._sync_to_creds()
            set_credentials_path(new_path)
            self._creds_path = credentials_path()
            save_credentials(self.creds)  # uses pointer target
            self.var_creds_file.set(str(self._creds_path))
            self._log(f"Credentials file updated: {self._creds_path}")
            messagebox.showinfo("Credentials moved", f"Credentials will now be read from:\n{self._creds_path}")
        except Exception as e:
            messagebox.showerror("Change failed", str(e))

    def on_clear(self) -> None:
        self.txt.delete("1.0", "end")
        self._log("Output cleared.")

    def on_copy_summary(self) -> None:
        text = self.txt.get("1.0", "end-1c")
        if not text.strip():
            messagebox.showinfo("Copy Summary", "Nothing to copy yet.")
            return

        try:
            self.master.clipboard_clear()
            self.master.clipboard_append(text)
            self.master.update_idletasks()
            self._log("Copied summary to clipboard.")
        except Exception as e:
            messagebox.showerror("Copy failed", str(e))

    def on_open_artifacts_folder(self) -> None:
        if self._last_artifacts_dir is None:
            return
        if not self._last_artifacts_dir.exists():
            messagebox.showerror("Open folder failed", f"Folder not found:\n{self._last_artifacts_dir}")
            return
        self._open_folder(self._last_artifacts_dir)

    def on_open_results_folder(self) -> None:
        if self._last_results_dir is None:
            return
        if not self._last_results_dir.exists():
            messagebox.showerror("Open folder failed", f"Folder not found:\n{self._last_results_dir}")
            return
        self._open_folder(self._last_results_dir)

    def on_save(self) -> None:
        try:
            int(self.var_days.get().strip())
            if not self.var_results_dir.get().strip():
                raise ValueError("CSV results folder is required (paths.results_csv_dir).")

            self._sync_to_creds()
            save_credentials(self.creds)

            self._log(f"Saved credentials: {self._creds_path}")
            messagebox.showinfo("Saved", f"Settings saved to:\n{self._creds_path}")
            self._refresh_publish_button_state()
        except Exception as e:
            messagebox.showerror("Save failed", str(e))

    def on_validate(self) -> None:
        try:
            self._sync_to_creds()
            v = validate_credentials(self.creds)

            if v.ok:
                self._log("VALIDATION OK ✅")
                messagebox.showinfo("Validation", "Credentials look valid.")
                self._refresh_publish_button_state()
                return

            self._log("VALIDATION FAILED ❌")
            for err in getattr(v, "errors", []) or []:
                self._log(f"- {err}")
            messagebox.showerror("Validation", "Credentials validation failed. See Output for details.")
        except Exception as e:
            self._log("ERROR during validation:")
            self._log(str(e))
            self._log(traceback.format_exc())
            messagebox.showerror("Validation error", str(e))

    def on_run(self) -> None:
        if self._worker_thread is not None and self._worker_thread.is_alive():
            messagebox.showinfo("Busy", "A run is already in progress.")
            return

        try:
            self.txt.delete("1.0", "end")
            self._log("-" * 64)
            self._log("Starting run...")

            self._sync_to_creds()

            # Reset last folders on new run
            self._last_results_dir = None
            self._last_artifacts_dir = None
            self.btn_open.configure(state="disabled")
            self.btn_open_artifacts.configure(state="disabled")

            results_dir_raw = get_nested(self.creds, "paths.results_csv_dir", None)
            self._log_block(
                "Preflight",
                {
                    "credentials_file": str(self._creds_path),
                    "results_csv_dir": results_dir_raw or "(missing: paths.results_csv_dir)",
                    "enable_azure_sql": bool(self.var_enable_azure.get()),
                    "dry_run": bool(self.var_dry_run.get()),
                    "last_settled_date_utc": (
                        str(self._reco_last_settled_date_utc) if self._reco_last_settled_date_utc else None
                    ),
                    "recommended_days": self._reco_days,
                    "recommendation_note": self.var_reco_note.get(),
                },
            )

            cfg = self._build_config_from_ui()

            if cfg.enable_azure_sql and (not cfg.dry_run):
                if (not bool(self.var_allow_publish.get())) or (self.var_publish_text.get().strip() != "PUBLISH"):
                    raise ValueError(
                        "Azure upload is enabled and Dry run is OFF.\n\n"
                        "To proceed with non-dry-run publishing:\n"
                        "1) Tick: 'Allow non-dry-run publish (writes to Azure)'\n"
                        "2) Type: PUBLISH\n"
                        "3) You will then be asked to confirm after Azure prep summary is computed.\n\n"
                        "Otherwise, turn Dry run back ON."
                    )

            self._set_running_state(True)
            self._worker_thread = threading.Thread(
                target=self._run_worker,
                args=(cfg, dict(self.creds)),
                daemon=True,
            )
            self._worker_thread.start()

        except Exception as e:
            self._set_running_state(False)
            self._log("ERROR:")
            self._log(str(e))
            self._log(traceback.format_exc())
            messagebox.showerror("Run failed", str(e))

    def on_publish_only(self) -> None:
        if self._worker_thread is not None and self._worker_thread.is_alive():
            messagebox.showinfo("Busy", "A run is already in progress.")
            return

        try:
            self.txt.delete("1.0", "end")
            self._log("-" * 64)
            self._log("Starting Azure publish-only...")

            self._sync_to_creds()

            cfg = self._build_config_from_ui()

            if cfg.enable_azure_sql and (not cfg.dry_run):
                if (not bool(self.var_allow_publish.get())) or (self.var_publish_text.get().strip() != "PUBLISH"):
                    raise ValueError(
                        "Azure upload is enabled and Dry run is OFF.\n\n"
                        "To proceed with non-dry-run publishing:\n"
                        "1) Tick: 'Allow non-dry-run publish (writes to Azure)'\n"
                        "2) Type: PUBLISH\n"
                        "3) You will then be asked to confirm before publishing.\n\n"
                        "Otherwise, turn Dry run back ON."
                    )

            self._set_running_state(True)
            self._worker_thread = threading.Thread(
                target=self._publish_only_worker,
                args=(cfg, dict(self.creds)),
                daemon=True,
            )
            self._worker_thread.start()

        except Exception as e:
            self._set_running_state(False)
            self._log("ERROR:")
            self._log(str(e))
            self._log(traceback.format_exc())
            messagebox.showerror("Publish failed", str(e))

    def _run_worker(self, cfg: DownloaderConfig, creds_copy: dict) -> None:
        """
        Background worker thread. Never call Tk directly here.
        """
        try:

            def status_cb(msg: str) -> None:
                self._status(msg)

            def confirm_publish_cb(prep_summary: dict) -> bool:
                user_id = str(prep_summary.get("user_id", cfg.user_id))
                markets = int(prep_summary.get("markets_aggregated", 0) or 0)
                rows = int(prep_summary.get("rows_to_write_count", 0) or 0)
                return self._confirm_publish_cb_threadsafe(user_id=user_id, markets=markets, rows=rows)

            result = run_downloader(
                cfg,
                creds_copy,
                status_cb=status_cb,
                confirm_publish_cb=confirm_publish_cb,
                last_settled_date_utc=self._reco_last_settled_date_utc,
                recommended_days=self._reco_days,
                recommendation_note=self.var_reco_note.get(),
            )
            self._status_q.put(("done", result))
        except Exception as e:
            err_text = f"{e}\n\n{traceback.format_exc()}"
            self._status_q.put(("error", err_text))

    def _publish_only_worker(self, cfg: DownloaderConfig, creds_copy: dict) -> None:
        """
        Background worker thread for publish-only. Never call Tk directly here.
        """
        try:

            def status_cb(msg: str) -> None:
                self._status(msg)

            def confirm_publish_cb(prep_summary: dict) -> bool:
                user_id = str(prep_summary.get("user_id", cfg.user_id))
                markets = int(prep_summary.get("markets_aggregated", 0) or 0)
                rows = int(prep_summary.get("rows_to_write_count", 0) or 0)
                return self._confirm_publish_cb_threadsafe(user_id=user_id, markets=markets, rows=rows)

            result = publish_to_azure_from_canonical(
                cfg,
                creds_copy,
                status_cb=status_cb,
                confirm_publish_cb=confirm_publish_cb,
            )
            self._status_q.put(("publish_done", result))
        except Exception as e:
            err_text = f"{e}\n\n{traceback.format_exc()}"
            self._status_q.put(("error", err_text))


def main() -> None:
    root = tk.Tk()

    try:
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
        elif "clam" in style.theme_names():
            style.theme_use("clam")
    except Exception:
        pass

    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
