from __future__ import annotations

import os
import platform
import queue
import subprocess
import sys
import threading
import traceback
import tkinter as tk
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .config import DownloaderConfig
from .run import run_downloader, publish_to_azure_from_canonical_incremental
from .recommend import recommend_lookback_days_v2
from .run_logging import normalize_log_line
from .azure_remediation import (
    audit_duplicates,
    backup_user_rows,
    check_raw_userid_variants,
    create_unique_index,
    dedupe_user_marketid,
    delete_user_rows,
    detect_row_identifier,
    normalize_userid,
    preview_normalize_userid,
)
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
                "rows_after_filter",
                "markets_aggregated",
                "rows_to_write_count",
                "existing_rows_in_db",
                "matching_rows_unchanged",
                "rows_to_update",
                "rows_to_insert",
                "rows_db_only_not_in_new",
                "inserted_rows",
                "updated_rows",
                "deleted_rows",
                "message",
                "user_id",
            ]
        elif title.lower().startswith("azure tools"):
            preferred = [
                "user_id",
                "table",
                "total_rows",
                "duplicated_marketids",
                "rows_involved_in_duplication",
                "duplicates_csv",
                "rows_exported",
                "backup_csv",
                "rows_with_padding",
                "rows_updated",
                "pre_delete_rows",
                "rows_deleted",
                "post_delete_rows",
                "index_name",
                "created",
                "message",
            ]
        elif title.lower().startswith("publish-only"):
            preferred = [
                "attempted",
                "canonical_path",
                "rows_loaded",
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
        self.var_results_dir = tk.StringVar(
            value=str(get_nested(initial, "paths.results_csv_dir", ""))
        )

        self.var_bf_user = tk.StringVar(
            value=str(get_nested(initial, "betfair.username", ""))
        )
        self.var_bf_pass = tk.StringVar(
            value=str(get_nested(initial, "betfair.password", ""))
        )
        self.var_bf_appkey = tk.StringVar(
            value=str(get_nested(initial, "betfair.app_key", ""))
        )

        self.var_user_id = tk.StringVar(
            value=str(get_nested(initial, "user.user_id", DEFAULT_USER_ID))
        )
        self.var_days = tk.StringVar(value=str(get_nested(initial, "user.days", 7)))
        self.var_horses = tk.BooleanVar(
            value=bool(get_nested(initial, "user.include_horses", True))
        )
        self.var_greyhounds = tk.BooleanVar(
            value=bool(get_nested(initial, "user.include_greyhounds", True))
        )
        self.var_dry_run = tk.BooleanVar(
            value=bool(get_nested(initial, "user.dry_run", True))
        )

        self.var_enable_azure = tk.BooleanVar(
            value=bool(get_nested(initial, "user.enable_azure_sql", False))
        )
        self.var_az_server = tk.StringVar(
            value=str(get_nested(initial, "azure_sql.server", ""))
        )
        self.var_az_db = tk.StringVar(
            value=str(get_nested(initial, "azure_sql.database", ""))
        )
        self.var_az_user = tk.StringVar(
            value=str(get_nested(initial, "azure_sql.username", ""))
        )
        self.var_az_pass = tk.StringVar(
            value=str(get_nested(initial, "azure_sql.password", ""))
        )
        self.var_az_driver = tk.StringVar(
            value=str(
                get_nested(initial, "azure_sql.driver", "ODBC Driver 18 for SQL Server")
            )
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
            self.geometry(f"+{px + (pw - w) // 2}+{py + (ph - h) // 2}")
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
                "Welcome! Let's set up Betfair Results Downloader.\n"
                "Choose where to save your credentials and where to store CSV outputs."
            ),
            justify="left",
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 10))

        # --- Paths ---
        pf = ttk.LabelFrame(frm, text="Paths", padding=10)
        pf.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(0, 10))
        pf.columnconfigure(1, weight=1)

        ttk.Label(pf, text="Credentials file").grid(row=0, column=0, sticky="w")
        ttk.Entry(pf, textvariable=self.var_creds_path).grid(
            row=0, column=1, sticky="ew", padx=(10, 10)
        )
        ttk.Button(pf, text="Browse...", command=self._choose_creds_file).grid(
            row=0, column=2, sticky="e"
        )

        ttk.Label(pf, text="CSV results folder").grid(
            row=1, column=0, sticky="w", pady=(6, 0)
        )
        ttk.Entry(pf, textvariable=self.var_results_dir).grid(
            row=1, column=1, sticky="ew", padx=(10, 10), pady=(6, 0)
        )
        ttk.Button(pf, text="Browse...", command=self._choose_results_dir).grid(
            row=1, column=2, sticky="e", pady=(6, 0)
        )

        # --- Betfair ---
        bf = ttk.LabelFrame(frm, text="Betfair", padding=10)
        bf.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(0, 10))
        bf.columnconfigure(1, weight=1)

        ttk.Label(bf, text="Username").grid(row=0, column=0, sticky="w")
        ttk.Entry(bf, textvariable=self.var_bf_user).grid(
            row=0, column=1, columnspan=2, sticky="ew", padx=(10, 0)
        )

        ttk.Label(bf, text="Password").grid(row=1, column=0, sticky="w")
        ttk.Entry(bf, textvariable=self.var_bf_pass, show="*").grid(
            row=1, column=1, columnspan=2, sticky="ew", padx=(10, 0)
        )

        ttk.Label(bf, text="App Key").grid(row=2, column=0, sticky="w")
        ttk.Entry(bf, textvariable=self.var_bf_appkey, show="*").grid(
            row=2, column=1, columnspan=2, sticky="ew", padx=(10, 0)
        )

        # --- Run defaults ---
        rc = ttk.LabelFrame(frm, text="Run defaults", padding=10)
        rc.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(0, 10))
        rc.columnconfigure(1, weight=1)

        ttk.Label(rc, text="User ID").grid(row=0, column=0, sticky="w")
        ttk.Entry(rc, textvariable=self.var_user_id).grid(
            row=0, column=1, columnspan=2, sticky="ew", padx=(10, 0)
        )

        ttk.Label(rc, text="Days to download").grid(row=1, column=0, sticky="w")
        ttk.Entry(rc, textvariable=self.var_days, width=8).grid(
            row=1, column=1, sticky="w", padx=(10, 0)
        )

        ttk.Checkbutton(
            rc, text="Include Horses (eventTypeId 7)", variable=self.var_horses
        ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(6, 0))
        ttk.Checkbutton(
            rc,
            text="Include Greyhounds (eventTypeId 4339)",
            variable=self.var_greyhounds,
        ).grid(row=3, column=0, columnspan=3, sticky="w")

        ttk.Checkbutton(
            rc, text="Dry run (recommended)", variable=self.var_dry_run
        ).grid(row=4, column=0, columnspan=3, sticky="w", pady=(6, 0))

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
        self.ent_az_server.grid(
            row=1, column=1, columnspan=2, sticky="ew", padx=(10, 0)
        )

        ttk.Label(az, text="Database").grid(row=2, column=0, sticky="w")
        self.ent_az_db = ttk.Entry(az, textvariable=self.var_az_db)
        self.ent_az_db.grid(row=2, column=1, columnspan=2, sticky="ew", padx=(10, 0))

        ttk.Label(az, text="Username").grid(row=3, column=0, sticky="w")
        self.ent_az_user = ttk.Entry(az, textvariable=self.var_az_user)
        self.ent_az_user.grid(row=3, column=1, columnspan=2, sticky="ew", padx=(10, 0))

        ttk.Label(az, text="Password").grid(row=4, column=0, sticky="w")
        self.ent_az_pass = ttk.Entry(az, textvariable=self.var_az_pass, show="*")
        self.ent_az_pass.grid(row=4, column=1, columnspan=2, sticky="ew", padx=(10, 0))

        ttk.Label(az, text="ODBC Driver").grid(row=5, column=0, sticky="w")
        self.ent_az_driver = ttk.Entry(az, textvariable=self.var_az_driver)
        self.ent_az_driver.grid(
            row=5, column=1, columnspan=2, sticky="ew", padx=(10, 0)
        )

        # --- Buttons ---
        btns = ttk.Frame(frm)
        btns.grid(row=5, column=0, columnspan=3, sticky="ew")
        btns.columnconfigure(0, weight=1)

        ttk.Button(btns, text="Cancel", command=self._cancel).grid(
            row=0, column=1, sticky="e", padx=(0, 8)
        )
        ttk.Button(btns, text="Save & Continue", command=self._save).grid(
            row=0, column=2, sticky="e"
        )

    def _refresh_azure_state(self) -> None:
        enabled = bool(self.var_enable_azure.get())
        state = "normal" if enabled else "disabled"
        for w in (
            self.ent_az_server,
            self.ent_az_db,
            self.ent_az_user,
            self.ent_az_pass,
            self.ent_az_driver,
        ):
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
            messagebox.showerror(
                "Missing field", "Credentials file path is required.", parent=self
            )
            return

        if not self.var_results_dir.get().strip():
            messagebox.showerror(
                "Missing field", "CSV results folder is required.", parent=self
            )
            return

        if not self.var_bf_user.get().strip():
            messagebox.showerror(
                "Missing field", "Betfair username is required.", parent=self
            )
            return
        if not self.var_bf_pass.get():
            messagebox.showerror(
                "Missing field", "Betfair password is required.", parent=self
            )
            return
        if not self.var_bf_appkey.get():
            messagebox.showerror(
                "Missing field", "Betfair app key is required.", parent=self
            )
            return

        try:
            days = int(self.var_days.get().strip())
            if days <= 0:
                raise ValueError
        except Exception:
            messagebox.showerror(
                "Invalid field",
                "Days to download must be a positive integer.",
                parent=self,
            )
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
        self.var_results_dir = tk.StringVar(
            value=str(get_nested(self.creds, "paths.results_csv_dir", ""))
        )

        # --- Vars (Betfair) ---
        self.var_bf_user = tk.StringVar(
            value=str(get_nested(self.creds, "betfair.username", ""))
        )
        self.var_bf_pass = tk.StringVar(
            value=str(get_nested(self.creds, "betfair.password", ""))
        )
        self.var_bf_appkey = tk.StringVar(
            value=str(get_nested(self.creds, "betfair.app_key", ""))
        )

        # --- Vars (Run config) ---
        self.var_user_id = tk.StringVar(
            value=str(get_nested(self.creds, "user.user_id", DEFAULT_USER_ID))
        )
        self.var_days = tk.StringVar(value=str(get_nested(self.creds, "user.days", 7)))
        self.var_manual_override = tk.BooleanVar(value=False)
        self.var_override_warning = tk.StringVar(
            value="Overrides auto lookback for this run."
        )
        self.var_effective_days = tk.StringVar(value="")
        self.var_lookback_source = tk.StringVar(value="")
        self.var_missing_range = tk.StringVar(value="")
        self.var_recommendation_note = tk.StringVar(value="")

        self.var_horses = tk.BooleanVar(
            value=bool(get_nested(self.creds, "user.include_horses", True))
        )
        self.var_greyhounds = tk.BooleanVar(
            value=bool(get_nested(self.creds, "user.include_greyhounds", True))
        )

        self.var_enable_azure = tk.BooleanVar(
            value=bool(get_nested(self.creds, "user.enable_azure_sql", False))
        )
        self.var_dry_run = tk.BooleanVar(
            value=bool(get_nested(self.creds, "user.dry_run", True))
        )

        # --- Vars (Azure unlock) ---
        self.var_allow_publish = tk.BooleanVar(value=False)
        self.var_publish_text = tk.StringVar(value="")

        # --- Vars (Azure) ---
        self.var_az_server = tk.StringVar(
            value=str(get_nested(self.creds, "azure_sql.server", ""))
        )
        self.var_az_db = tk.StringVar(
            value=str(get_nested(self.creds, "azure_sql.database", ""))
        )
        self.var_az_user = tk.StringVar(
            value=str(get_nested(self.creds, "azure_sql.username", ""))
        )
        self.var_az_pass = tk.StringVar(
            value=str(get_nested(self.creds, "azure_sql.password", ""))
        )
        self.var_az_driver = tk.StringVar(
            value=str(
                get_nested(
                    self.creds, "azure_sql.driver", "ODBC Driver 18 for SQL Server"
                )
            )
        )

        # --- Vars (Azure tools) ---
        self.var_azure_tools_enabled = tk.BooleanVar(value=False)
        self.var_azure_tools_phrase = tk.StringVar(value="")
        self.var_azure_tools_user_id = tk.StringVar(value="")

        # --- Runtime / status ---
        self._status_q: "queue.Queue[tuple[str, object]]" = queue.Queue()
        self._worker_thread: threading.Thread | None = None
        self._last_results_dir: Path | None = None
        self._last_artifacts_dir: Path | None = None
        self._run_log_buffer: list[str] | None = None
        self._run_log_path: Path | None = None
        self._last_lookback_rec: dict[str, object] | None = None

        self._build()
        self.master.after(100, self._poll_status_queue)
        self._refresh_publish_unlock_state()
        self._refresh_azure_tools_user_id()
        self._refresh_azure_tools_state()
        self._refresh_action_buttons()
        self._refresh_manual_override_state()
        self.var_publish_text.trace_add(
            "write", lambda *_: self._refresh_publish_button_state()
        )
        self.var_azure_tools_phrase.trace_add(
            "write", lambda *_: self._refresh_azure_tools_state()
        )
        self.var_results_dir.trace_add(
            "write", lambda *_: self._refresh_action_buttons()
        )
        self.var_bf_user.trace_add("write", lambda *_: self._refresh_action_buttons())
        self.var_bf_pass.trace_add("write", lambda *_: self._refresh_action_buttons())
        self.var_bf_appkey.trace_add("write", lambda *_: self._refresh_action_buttons())
        self.var_results_dir.trace_add(
            "write", lambda *_: self._refresh_publish_button_state()
        )
        self.var_manual_override.trace_add(
            "write", lambda *_: self._refresh_manual_override_state()
        )

        # First-run setup wizard
        if first_run:
            self._run_first_time_setup()
        else:
            # Ensure the resolved file exists going forward
            ensure_credentials_file_exists()

    # ---------------- First-run onboarding ----------------

    def _run_first_time_setup(self) -> None:
        self._log("First run detected: launching setup wizard...")

        wiz = FirstRunWizard(
            self.master, self.creds, default_creds_path=self._creds_path
        )
        self.master.wait_window(wiz)

        if wiz.result is None:
            self._log(
                "First run setup cancelled. You can edit settings in the GUI and click Save Settings."
            )
            return

        # Apply wizard values into creds
        chosen_creds_path_raw = str(wiz.result.get("__credentials_path__", "")).strip()
        chosen_creds_path = (
            Path(chosen_creds_path_raw) if chosen_creds_path_raw else self._creds_path
        )

        # Persist chosen credentials path
        set_credentials_path(chosen_creds_path)
        self._creds_path = credentials_path()
        self.var_creds_file.set(str(self._creds_path))

        for k, v in wiz.result.items():
            if k == "__credentials_path__":
                continue
            set_nested(self.creds, k, v)

        # Update bound Vars
        self.var_results_dir.set(
            str(get_nested(self.creds, "paths.results_csv_dir", ""))
        )

        self.var_bf_user.set(str(get_nested(self.creds, "betfair.username", "")))
        self.var_bf_pass.set(str(get_nested(self.creds, "betfair.password", "")))
        self.var_bf_appkey.set(str(get_nested(self.creds, "betfair.app_key", "")))

        self.var_user_id.set(
            str(get_nested(self.creds, "user.user_id", DEFAULT_USER_ID))
        )
        self.var_days.set(str(get_nested(self.creds, "user.days", 7)))
        self.var_horses.set(bool(get_nested(self.creds, "user.include_horses", True)))
        self.var_greyhounds.set(
            bool(get_nested(self.creds, "user.include_greyhounds", True))
        )
        self.var_enable_azure.set(
            bool(get_nested(self.creds, "user.enable_azure_sql", False))
        )
        self.var_dry_run.set(bool(get_nested(self.creds, "user.dry_run", True)))

        self.var_az_server.set(str(get_nested(self.creds, "azure_sql.server", "")))
        self.var_az_db.set(str(get_nested(self.creds, "azure_sql.database", "")))
        self.var_az_user.set(str(get_nested(self.creds, "azure_sql.username", "")))
        self.var_az_pass.set(str(get_nested(self.creds, "azure_sql.password", "")))
        self.var_az_driver.set(
            str(
                get_nested(
                    self.creds, "azure_sql.driver", "ODBC Driver 18 for SQL Server"
                )
            )
        )

        try:
            save_credentials(
                self.creds
            )  # saves to resolved credentials_path() via pointer
        except Exception as e:
            messagebox.showerror("Save failed", str(e))
            self._log(f"ERROR saving credentials: {e}")
            return

        # Friendly checklist
        self._log(f"OK: credentials file created: {self._creds_path}")

        results_dir_raw = get_nested(self.creds, "paths.results_csv_dir", None)
        if results_dir_raw:
            self._log(f"OK: results directory configured: {results_dir_raw}")
        else:
            self._log(
                "WARNING: results directory not configured yet (paths.results_csv_dir)."
            )

        if bool(get_nested(self.creds, "user.dry_run", True)):
            self._log("OK: dry-run enabled (safe-by-default)")
        else:
            self._log(
                "WARNING: dry-run disabled (publishing still requires GUI unlock + confirmation)"
            )

        if bool(get_nested(self.creds, "user.enable_azure_sql", False)):
            self._log("INFO: Azure enabled (still safe-by-default)")
        else:
            self._log("INFO: Azure disabled")

        messagebox.showinfo(
            "Setup complete", "First run setup saved. You can now run the downloader."
        )
        self._refresh_publish_unlock_state()

    # ---------------- UI ----------------

    def _build(self) -> None:
        self.master.title("Betfair Results Downloader (GUI)")
        self.master.minsize(820, 600)

        self.grid(row=0, column=0, sticky="nsew")
        self.master.columnconfigure(0, weight=1)
        self.master.rowconfigure(0, weight=1)

        self.columnconfigure(0, weight=1)
        self.rowconfigure(8, weight=1)

        ttk.Label(
            self,
            text="1) Choose Paths -> 2) Validate -> 3) Compute Lookback -> 4) Run Downloader -> (Optional) 5) Publish to Azure",
            wraplength=760,
            justify="left",
        ).grid(row=0, column=0, sticky="w", padx=10, pady=(8, 4))

        # --- Paths ---
        pf = ttk.LabelFrame(self, text="Paths", padding=10)
        pf.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        pf.columnconfigure(1, weight=1)

        ttk.Label(pf, text="Credentials file").grid(row=0, column=0, sticky="w")
        ttk.Entry(pf, textvariable=self.var_creds_file, state="readonly").grid(
            row=0, column=1, sticky="ew", padx=(10, 10)
        )
        ttk.Button(pf, text="Change...", command=self.on_change_credentials_file).grid(
            row=0, column=2, sticky="e"
        )

        ttk.Label(pf, text="CSV results folder").grid(
            row=1, column=0, sticky="w", pady=(6, 0)
        )
        ttk.Entry(pf, textvariable=self.var_results_dir).grid(
            row=1, column=1, sticky="ew", padx=(10, 10), pady=(6, 0)
        )
        ttk.Button(pf, text="Browse...", command=self.on_choose_results_folder).grid(
            row=1, column=2, sticky="e", pady=(6, 0)
        )

        self.btn_save = ttk.Button(pf, text="Save Settings", command=self.on_save)
        self.btn_save.grid(row=2, column=1, sticky="w", padx=(10, 0), pady=(6, 0))

        # --- Betfair ---
        bf = ttk.LabelFrame(self, text="Betfair Credentials", padding=10)
        bf.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        bf.columnconfigure(1, weight=1)

        ttk.Label(bf, text="Username").grid(row=0, column=0, sticky="w")
        ttk.Entry(bf, textvariable=self.var_bf_user).grid(
            row=0, column=1, sticky="ew", padx=(10, 0)
        )

        ttk.Label(bf, text="Password").grid(row=1, column=0, sticky="w")
        ttk.Entry(bf, textvariable=self.var_bf_pass, show="*").grid(
            row=1, column=1, sticky="ew", padx=(10, 0)
        )

        ttk.Label(bf, text="App Key").grid(row=2, column=0, sticky="w")
        ttk.Entry(bf, textvariable=self.var_bf_appkey, show="*").grid(
            row=2, column=1, sticky="ew", padx=(10, 0)
        )

        self.btn_validate = ttk.Button(bf, text="Validate", command=self.on_validate)
        self.btn_validate.grid(row=3, column=1, sticky="w", padx=(10, 0), pady=(6, 0))

        # --- Run config ---
        rc = ttk.LabelFrame(self, text="Run Configuration", padding=10)
        rc.grid(row=3, column=0, sticky="ew", pady=(0, 10))
        for i in range(4):
            rc.columnconfigure(i, weight=1)

        ttk.Label(rc, text="User ID").grid(row=0, column=0, sticky="w")
        ttk.Entry(rc, textvariable=self.var_user_id).grid(
            row=0, column=1, sticky="ew", padx=(10, 20)
        )

        ttk.Label(rc, text="Days to download").grid(row=0, column=2, sticky="w")
        self.ent_days = ttk.Entry(rc, textvariable=self.var_days, width=8)
        self.ent_days.grid(row=0, column=3, sticky="w", padx=(10, 0))

        self.chk_manual_override = ttk.Checkbutton(
            rc,
            text="Manual override",
            variable=self.var_manual_override,
            command=self._refresh_manual_override_state,
        )
        self.chk_manual_override.grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(6, 0)
        )
        self.lbl_override_warning = ttk.Label(
            rc, textvariable=self.var_override_warning, foreground="#8B0000"
        )
        self.lbl_override_warning.grid(
            row=1, column=2, columnspan=2, sticky="w", pady=(6, 0)
        )

        ttk.Checkbutton(
            rc, text="Horses (eventTypeId 7)", variable=self.var_horses
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Checkbutton(
            rc, text="Greyhounds (eventTypeId 4339)", variable=self.var_greyhounds
        ).grid(row=2, column=2, columnspan=2, sticky="w", pady=(6, 0))

        lb = ttk.LabelFrame(self, text="Effective Lookback (Auto)", padding=10)
        lb.grid(row=4, column=0, sticky="ew", pady=(0, 10))
        lb.columnconfigure(1, weight=1)

        ttk.Label(lb, text="Effective lookback").grid(row=0, column=0, sticky="w")
        ttk.Label(lb, textvariable=self.var_effective_days).grid(
            row=0, column=1, sticky="w"
        )

        ttk.Label(lb, text="Source").grid(row=1, column=0, sticky="w", pady=(4, 0))
        ttk.Label(lb, textvariable=self.var_lookback_source).grid(
            row=1, column=1, sticky="w", pady=(4, 0)
        )

        ttk.Label(lb, text="Missing range").grid(
            row=2, column=0, sticky="w", pady=(4, 0)
        )
        ttk.Label(lb, textvariable=self.var_missing_range).grid(
            row=2, column=1, sticky="w", pady=(4, 0)
        )

        ttk.Label(lb, text="Note").grid(row=3, column=0, sticky="w", pady=(4, 0))
        ttk.Label(
            lb,
            textvariable=self.var_recommendation_note,
            wraplength=540,
            justify="left",
        ).grid(row=3, column=1, sticky="w", pady=(4, 0))

        run_actions = ttk.Frame(self, padding=(0, 0, 0, 6))
        run_actions.grid(row=5, column=0, sticky="ew")
        run_actions.columnconfigure(0, weight=1)
        run_actions.columnconfigure(1, weight=1)

        self.btn_compute = ttk.Button(
            run_actions, text="Compute Lookback", command=self.on_compute_lookback
        )
        self.btn_compute.grid(row=0, column=0, sticky="w", pady=(6, 0))

        self.btn_run = ttk.Button(
            run_actions, text="Run Downloader", command=self.on_run
        )
        self.btn_run.grid(row=0, column=1, sticky="e", pady=(6, 0))

        # --- Azure ---
        az = ttk.LabelFrame(self, text="Azure SQL (Optional)", padding=10)
        az.grid(row=6, column=0, sticky="ew", pady=(0, 10))
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

        ttk.Label(az, text="Type PUBLISH to enable").grid(
            row=1, column=1, sticky="w", padx=(10, 0), pady=(6, 0)
        )
        self.ent_publish_text = ttk.Entry(
            az, textvariable=self.var_publish_text, width=16
        )
        self.ent_publish_text.grid(
            row=1, column=1, sticky="e", padx=(10, 0), pady=(6, 0)
        )

        ttk.Label(az, text="Server").grid(row=2, column=0, sticky="w")
        ttk.Entry(az, textvariable=self.var_az_server).grid(
            row=2, column=1, sticky="ew", padx=(10, 0)
        )

        ttk.Label(az, text="Database").grid(row=3, column=0, sticky="w")
        ttk.Entry(az, textvariable=self.var_az_db).grid(
            row=3, column=1, sticky="ew", padx=(10, 0)
        )

        ttk.Label(az, text="Username").grid(row=4, column=0, sticky="w")
        ttk.Entry(az, textvariable=self.var_az_user).grid(
            row=4, column=1, sticky="ew", padx=(10, 0)
        )

        ttk.Label(az, text="Password").grid(row=5, column=0, sticky="w")
        ttk.Entry(az, textvariable=self.var_az_pass, show="*").grid(
            row=5, column=1, sticky="ew", padx=(10, 0)
        )

        ttk.Label(az, text="ODBC Driver").grid(row=6, column=0, sticky="w")
        ttk.Entry(az, textvariable=self.var_az_driver).grid(
            row=6, column=1, sticky="ew", padx=(10, 0)
        )

        self.btn_publish = ttk.Button(
            az, text="Publish to Azure", command=self.on_publish_only, state="disabled"
        )
        self.btn_publish.grid(row=7, column=1, sticky="e", pady=(8, 0))

        # --- Azure Tools ---
        tools = ttk.LabelFrame(self, text="Azure Tools", padding=10)
        tools.grid(row=7, column=0, sticky="ew", pady=(0, 10))
        tools.columnconfigure(1, weight=1)

        ttk.Label(tools, text="Active UserID").grid(row=0, column=0, sticky="w")
        ttk.Label(tools, textvariable=self.var_azure_tools_user_id).grid(
            row=0, column=1, sticky="w"
        )

        self.btn_az_health = ttk.Button(
            tools,
            text="Azure Health Check (Read-only)",
            command=self.on_azure_health_check,
        )
        self.btn_az_health.grid(row=1, column=0, sticky="w", pady=(6, 0))

        self.btn_az_backup = ttk.Button(
            tools, text="Export Azure Backup (My Rows)", command=self.on_azure_backup
        )
        self.btn_az_backup.grid(row=1, column=1, sticky="w", pady=(6, 0), padx=(10, 0))

        adv = ttk.LabelFrame(
            tools, text="Advanced Tools (Write Operations)", padding=10
        )
        adv.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        adv.columnconfigure(1, weight=1)

        self.chk_azure_tools = ttk.Checkbutton(
            adv,
            text="Enable Advanced Azure Tools",
            variable=self.var_azure_tools_enabled,
            command=self._refresh_azure_tools_state,
        )
        self.chk_azure_tools.grid(row=0, column=0, sticky="w")

        ttk.Label(adv, text="Type to confirm").grid(
            row=0, column=1, sticky="e", padx=(10, 0)
        )
        self.ent_azure_tools_phrase = ttk.Entry(
            adv, textvariable=self.var_azure_tools_phrase, width=20
        )
        self.ent_azure_tools_phrase.grid(row=0, column=2, sticky="e", padx=(10, 0))

        self.btn_az_normalize = ttk.Button(
            adv,
            text="Normalize My UserID (Padding Fix)",
            command=self.on_azure_normalize,
        )
        self.btn_az_normalize.grid(row=1, column=0, sticky="w", pady=(8, 0))

        self.btn_az_index = ttk.Button(
            adv,
            text="Create/Verify My Unique Index",
            command=self.on_azure_create_index,
        )
        self.btn_az_index.grid(row=1, column=1, sticky="w", padx=(10, 0), pady=(8, 0))

        self.btn_az_cleanup = ttk.Button(
            adv,
            text="Emergency Cleanup Wizard...",
            command=self.on_azure_cleanup_wizard,
        )
        self.btn_az_cleanup.grid(row=1, column=2, sticky="e", padx=(10, 0), pady=(8, 0))

        # --- Output + buttons ---
        out = ttk.LabelFrame(self, text="Output", padding=10)
        out.grid(row=8, column=0, sticky="nsew")
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
        self._log(
            "Tip: Dry run is ON by default. Non-dry-run publishing requires explicit unlock + confirmation."
        )

        btns = ttk.Frame(self)
        btns.grid(row=9, column=0, sticky="ew", pady=(10, 0))
        btns.columnconfigure(0, weight=1)

        self.btn_clear = ttk.Button(btns, text="Clear Output", command=self.on_clear)
        self.btn_clear.grid(row=0, column=0, sticky="w")

        self.btn_copy = ttk.Button(
            btns, text="Copy Summary", command=self.on_copy_summary
        )
        self.btn_copy.grid(row=0, column=1, sticky="w", padx=(10, 0))

        self.btn_open_artifacts = ttk.Button(
            btns,
            text="Open Artifacts Folder",
            command=self.on_open_artifacts_folder,
            state="disabled",
        )
        self.btn_open_artifacts.grid(row=0, column=2, sticky="w", padx=(10, 0))

        self.btn_open = ttk.Button(
            btns,
            text="Open Results Folder",
            command=self.on_open_results_folder,
            state="disabled",
        )
        self.btn_open.grid(row=0, column=3, sticky="w", padx=(10, 0))

    # ---------------- Helpers ----------------

    def _log(self, msg: str) -> None:
        cleaned = msg.rstrip()
        if self._run_log_buffer is not None:
            self._run_log_buffer.append(cleaned)
        self.txt.insert("end", cleaned + "\n")
        self.txt.see("end")

    def _log_block(self, title: str, data: object) -> None:
        self._log(format_block(title, data).rstrip())

    def _sync_to_creds(self) -> None:
        # Paths
        set_nested(
            self.creds, "paths.results_csv_dir", self.var_results_dir.get().strip()
        )

        # Betfair
        set_nested(self.creds, "betfair.username", self.var_bf_user.get().strip())
        set_nested(self.creds, "betfair.password", self.var_bf_pass.get())
        set_nested(self.creds, "betfair.app_key", self.var_bf_appkey.get())

        # User/run config
        set_nested(
            self.creds,
            "user.user_id",
            (self.var_user_id.get().strip() or DEFAULT_USER_ID),
        )
        set_nested(self.creds, "user.days", int(self.var_days.get().strip()))
        set_nested(self.creds, "user.include_horses", bool(self.var_horses.get()))
        set_nested(
            self.creds, "user.include_greyhounds", bool(self.var_greyhounds.get())
        )
        set_nested(
            self.creds, "user.enable_azure_sql", bool(self.var_enable_azure.get())
        )
        set_nested(self.creds, "user.dry_run", bool(self.var_dry_run.get()))

        # Azure SQL
        set_nested(self.creds, "azure_sql.server", self.var_az_server.get().strip())
        set_nested(self.creds, "azure_sql.database", self.var_az_db.get().strip())
        set_nested(self.creds, "azure_sql.username", self.var_az_user.get().strip())
        set_nested(self.creds, "azure_sql.password", self.var_az_pass.get())
        set_nested(self.creds, "azure_sql.driver", self.var_az_driver.get().strip())

        self._refresh_azure_tools_user_id()
        self._refresh_azure_tools_state()

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

    def _set_running_state(self, running: bool) -> None:
        state = "disabled" if running else "normal"
        self.btn_run.configure(state=state)
        self.btn_compute.configure(state=state)
        if running:
            self.btn_publish.configure(state="disabled")
        else:
            self._refresh_publish_button_state()
        try:
            if running:
                self.btn_az_health.configure(state="disabled")
                self.btn_az_backup.configure(state="disabled")
                self.btn_az_normalize.configure(state="disabled")
                self.btn_az_index.configure(state="disabled")
                self.btn_az_cleanup.configure(state="disabled")
            else:
                self._refresh_azure_tools_state()
        except Exception:
            pass
        self.btn_save.configure(state=state)
        self.btn_validate.configure(state=state)
        self.btn_clear.configure(state="normal")
        self.btn_copy.configure(state="normal")
        self.btn_open.configure(
            state=(
                "normal"
                if (not running and self._last_results_dir is not None)
                else "disabled"
            )
        )
        self.btn_open_artifacts.configure(
            state=(
                "normal"
                if (not running and self._last_artifacts_dir is not None)
                else "disabled"
            )
        )

        if running:
            self.progress.start(10)
        else:
            self.progress.stop()

    def _has_betfair_creds(self) -> bool:
        return bool(
            self.var_bf_user.get().strip()
            and self.var_bf_pass.get()
            and self.var_bf_appkey.get().strip()
        )

    def _results_dir_path(self) -> Path | None:
        raw = self.var_results_dir.get().strip()
        return Path(raw) if raw else None

    def _refresh_action_buttons(self) -> None:
        results_dir = self._results_dir_path()
        has_results = bool(results_dir)
        has_creds = self._has_betfair_creds()
        can_run = has_results and has_creds
        try:
            self.btn_compute.configure(state=("normal" if can_run else "disabled"))
            self.btn_run.configure(state=("normal" if can_run else "disabled"))
        except Exception:
            pass

    def _refresh_manual_override_state(self) -> None:
        enabled = bool(self.var_manual_override.get())
        state = "normal" if enabled else "disabled"
        try:
            self.ent_days.configure(state=state)
            self.lbl_override_warning.configure(
                state=("normal" if enabled else "disabled")
            )
        except Exception:
            pass

    def _start_run_log(self) -> None:
        self._run_log_buffer = []
        self._run_log_path = None
        results_dir = self._results_dir_path()
        if not results_dir:
            return
        log_dir = results_dir / "run_logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        self._run_log_path = log_dir / f"run_{ts}.txt"

    def _persist_run_log(self) -> None:
        if not self._run_log_buffer or self._run_log_path is None:
            return
        try:
            with self._run_log_path.open("w", encoding="utf-8", newline="\n") as handle:
                for line in self._run_log_buffer:
                    handle.write(normalize_log_line(line) + "\n")
            self._log(f"Run log saved: {self._run_log_path}")
        except Exception as e:
            self._log(f"WARNING: failed to write run log: {e}")
        finally:
            self._run_log_buffer = None

    def _compute_lookback_recommendation(
        self,
        *,
        manual_days: int,
        results_dir_raw: str | None = None,
    ) -> tuple[dict[str, object], dict[str, object], int]:
        results_dir = (
            Path(results_dir_raw) if results_dir_raw else self._results_dir_path()
        )
        if not results_dir:
            rec = {
                "recommended_days": manual_days,
                "lookback_source": "ui_default",
                "recommendation_note": "Missing results directory; using manual days.",
                "today_utc": datetime.now(timezone.utc).date().isoformat(),
                "missing_range": None,
            }
            summary = {
                "manual_days": manual_days,
                "effective_days": manual_days,
                "lookback_source": "ui_default",
                "missing_range": None,
                "note": "Missing results directory; using manual days.",
            }
            return rec, summary, manual_days

        rec = recommend_lookback_days_v2(
            canonical_csv_path=results_dir / "cleared_orders_cleaned.csv",
            run_state_path=results_dir / "run_state.json",
            window_days=90,
        )
        lookback_source = str(rec.get("lookback_source") or "ui_default")
        effective_days = int(rec.get("recommended_days", manual_days) or manual_days)
        if lookback_source == "ui_default":
            effective_days = manual_days
            rec["recommended_days"] = effective_days
        missing_range = rec.get("missing_range")
        summary: dict[str, object] = {
            "manual_days": manual_days,
            "effective_days": effective_days,
            "lookback_source": lookback_source,
            "missing_range": missing_range,
        }
        if missing_range:
            summary["window_start"] = rec.get("window_start")
            summary["window_end"] = rec.get("window_end")
        return rec, summary, effective_days

    def _apply_lookback_panel(self, rec: dict[str, object]) -> None:
        effective_days = int(rec.get("recommended_days", 0) or 0)
        lookback_source = str(rec.get("lookback_source") or "")
        missing_range = rec.get("missing_range")
        note = str(rec.get("recommendation_note") or "")

        self.var_effective_days.set(f"{effective_days} days" if effective_days else "")
        self.var_lookback_source.set(lookback_source)
        if isinstance(missing_range, dict):
            mr = f"{missing_range.get('start')}..{missing_range.get('end')} ({missing_range.get('days')} days)"
            self.var_missing_range.set(mr)
        else:
            self.var_missing_range.set("")
        self.var_recommendation_note.set(note)
        self._last_lookback_rec = rec

    def on_compute_lookback(self) -> None:
        try:
            self._sync_to_creds()
            manual_days = int(self.var_days.get().strip())
            rec, summary, effective_days = self._compute_lookback_recommendation(
                manual_days=manual_days
            )
            self._apply_lookback_panel(rec)
            self._log_block("Lookback Summary", summary)
            if rec.get("recommendation_note"):
                self._log(str(rec.get("recommendation_note")))
            self._log(f"Effective lookback: {effective_days} days")
        except Exception as e:
            messagebox.showerror("Compute Lookback", str(e))

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

    def _get_scoped_user_id_from_creds(self) -> str:
        user = self.creds.get("user", {}) or {}
        db_user_id = str(user.get("db_user_id") or "").strip()
        if db_user_id:
            return db_user_id
        return str(user.get("user_id") or "").strip() or DEFAULT_USER_ID

    def _refresh_azure_tools_user_id(self) -> None:
        try:
            self.var_azure_tools_user_id.set(self._get_scoped_user_id_from_creds())
        except Exception:
            self.var_azure_tools_user_id.set(DEFAULT_USER_ID)

    def _azure_tools_ready(self) -> bool:
        az = self.creds.get("azure_sql", {}) or {}
        user = self.creds.get("user", {}) or {}
        if not str(user.get("db_user_id") or user.get("user_id") or "").strip():
            return False
        if not str(az.get("server") or "").strip():
            return False
        if not str(az.get("database") or "").strip():
            return False
        if not str(az.get("username") or "").strip():
            return False
        if not str(az.get("password") or "").strip():
            return False
        return True

    def _refresh_azure_tools_state(self) -> None:
        ready = self._azure_tools_ready()
        adv_enabled = bool(self.var_azure_tools_enabled.get())
        enable_az = bool(self.var_enable_azure.get())
        dry_run = bool(self.var_dry_run.get())
        can_write = ready and enable_az and (not dry_run) and adv_enabled

        try:
            self.btn_az_health.configure(state=("normal" if ready else "disabled"))
            self.btn_az_backup.configure(state=("normal" if ready else "disabled"))
            self.btn_az_normalize.configure(
                state=("normal" if can_write else "disabled")
            )
            self.btn_az_index.configure(state=("normal" if can_write else "disabled"))
            self.btn_az_cleanup.configure(state=("normal" if can_write else "disabled"))
            self.chk_azure_tools.configure(state=("normal" if ready else "disabled"))
            self.ent_azure_tools_phrase.configure(
                state=("normal" if adv_enabled and ready else "disabled")
            )
        except Exception:
            pass

    def _require_advanced_tools(self, *, phrase: str, action: str) -> None:
        if not bool(self.var_enable_azure.get()):
            raise ValueError(
                "Azure upload is disabled. Enable Azure upload to use Azure Tools."
            )
        if bool(self.var_dry_run.get()):
            raise ValueError("Dry run is enabled. Turn it off to use write operations.")
        if not bool(self.var_azure_tools_enabled.get()):
            raise ValueError("Enable Advanced Azure Tools to proceed.")
        if self.var_azure_tools_phrase.get().strip() != phrase:
            raise ValueError(f"Type {phrase!r} to confirm {action}.")

    def _confirm_action_threadsafe(self, title: str, message: str) -> bool:
        ev = threading.Event()
        result_holder: dict[str, bool] = {"ok": False}

        def _do():
            try:
                result_holder["ok"] = messagebox.askokcancel(
                    title, message, icon="warning"
                )
            finally:
                ev.set()

        self.master.after(0, _do)
        ev.wait()
        return bool(result_holder["ok"])

    def _refresh_publish_button_state(self) -> None:
        enable_az = bool(self.var_enable_azure.get())
        dry_run = bool(self.var_dry_run.get())
        unlocked = bool(self.var_allow_publish.get()) and (
            self.var_publish_text.get().strip() == "PUBLISH"
        )
        results_dir = self._results_dir_path()
        canonical_ok = bool(
            results_dir and (results_dir / "cleared_orders_cleaned.csv").exists()
        )
        can_publish = enable_az and (not dry_run) and unlocked and canonical_ok
        try:
            self.btn_publish.configure(state=("normal" if can_publish else "disabled"))
        except Exception:
            pass

    def _status(self, msg: str) -> None:
        self._status_q.put(("log", msg))

    def _get_azure_tools_table(self) -> str:
        return os.getenv("AZURE_SQL_TABLE") or "dbo.MarketResults"

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
                elif kind == "azure_tools_done":
                    self._handle_azure_tools_done(payload)
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
            self._log_block("Missing settled dates", result.get("audit"))
            self._log_block("Run state", result.get("run_state"))
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

        self._persist_run_log()
        messagebox.showinfo("Run complete", "Run finished. See Output for details.")

    def _handle_publish_done(self, result: object) -> None:
        self._set_running_state(False)

        if isinstance(result, dict) and result.get("message"):
            self._log(str(result["message"]))
        else:
            self._log("Publish-only completed.")

        if isinstance(result, dict):
            summary = result.get("publish_only")
            if summary is not None:
                self._log_block("Publish-only summary", summary)

            azure = result.get("azure")
            if azure is not None:
                self._log_block("Azure summary", azure)

            if result.get("ok") is False:
                messagebox.showerror(
                    "Publish failed", str(result.get("message", "Publish failed."))
                )
                return

        results_dir_raw = get_nested(self.creds, "paths.results_csv_dir", None)
        if results_dir_raw:
            self._last_results_dir = Path(str(results_dir_raw))
            if self._last_results_dir.exists():
                self.btn_open.configure(state="normal")

        self._persist_run_log()
        messagebox.showinfo(
            "Publish complete", "Publish-only run finished. See Output for details."
        )

    def _handle_azure_tools_done(self, payload: object) -> None:
        if isinstance(payload, dict):
            title = payload.get("title") or "Azure Tools"
            summary = payload.get("summary")
            message = payload.get("message")
            suppress = bool(payload.get("suppress_dialog"))
            defer_reset = bool(payload.get("defer_running_reset"))

            if message:
                self._log(str(message))
            if summary is not None:
                self._log_block(title, summary)

            if payload.get("ok") is False:
                self._set_running_state(False)
                messagebox.showerror(
                    "Azure Tools", str(message or "Azure Tools action failed.")
                )
                return
            if not defer_reset:
                self._set_running_state(False)

        if isinstance(payload, dict) and payload.get("ok") is not False:
            if suppress:
                return
            messagebox.showinfo(
                "Azure Tools", "Azure tools action completed. See Output for details."
            )

    def _handle_run_error(self, err_text: object) -> None:
        self._set_running_state(False)
        self._log("ERROR:")
        self._log(str(err_text))
        self._persist_run_log()
        messagebox.showerror("Run failed", str(err_text))

    def _ask_confirm_publish_from_mainthread(
        self, *, user_id: str, markets: int, rows: int
    ) -> bool:
        msg = (
            "You are about to WRITE to Azure SQL (non-dry-run).\n\n"
            f"UserID: {user_id}\n"
            f"Markets to write: {markets:,}\n"
            f"Rows to write: {rows:,}\n\n"
            "This will INSERT new rows and UPDATE changed rows for this UserID.\n"
            "Existing rows not present in the new dataset will be left unchanged.\n\n"
            "Proceed?"
        )
        return messagebox.askokcancel("Confirm Azure Publish", msg, icon="warning")

    def _confirm_publish_cb_threadsafe(
        self, *, user_id: str, markets: int, rows: int
    ) -> bool:
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
            messagebox.showinfo(
                "Credentials moved",
                f"Credentials will now be read from:\n{self._creds_path}",
            )
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
            messagebox.showerror(
                "Open folder failed", f"Folder not found:\n{self._last_artifacts_dir}"
            )
            return
        self._open_folder(self._last_artifacts_dir)

    def on_open_results_folder(self) -> None:
        if self._last_results_dir is None:
            return
        if not self._last_results_dir.exists():
            messagebox.showerror(
                "Open folder failed", f"Folder not found:\n{self._last_results_dir}"
            )
            return
        self._open_folder(self._last_results_dir)

    def on_save(self) -> None:
        try:
            int(self.var_days.get().strip())
            if not self.var_results_dir.get().strip():
                raise ValueError(
                    "CSV results folder is required (paths.results_csv_dir)."
                )

            self._sync_to_creds()
            save_credentials(self.creds)

            self._log(f"Saved credentials: {self._creds_path}")
            messagebox.showinfo("Saved", f"Settings saved to:\n{self._creds_path}")
        except Exception as e:
            messagebox.showerror("Save failed", str(e))

    def on_validate(self) -> None:
        try:
            self._sync_to_creds()
            v = validate_credentials(self.creds)

            if v.ok:
                self._log("VALIDATION OK")
                messagebox.showinfo("Validation", "Credentials look valid.")
                return

            self._log("VALIDATION FAILED")
            for err in getattr(v, "errors", []) or []:
                self._log(f"- {err}")
            messagebox.showerror(
                "Validation", "Credentials validation failed. See Output for details."
            )
        except Exception as e:
            self._log("ERROR during validation:")
            self._log(str(e))
            self._log(traceback.format_exc())
            messagebox.showerror("Validation error", str(e))

    def _start_azure_tool_worker(self, *, title: str, worker) -> None:
        if self._worker_thread is not None and self._worker_thread.is_alive():
            messagebox.showinfo("Busy", "A run is already in progress.")
            return

        self._set_running_state(True)
        self._worker_thread = threading.Thread(
            target=worker,
            daemon=True,
        )
        self._worker_thread.start()

    def on_azure_health_check(self) -> None:
        try:
            self._sync_to_creds()
            user_id = self._get_scoped_user_id_from_creds()
            table = self._get_azure_tools_table()

            def _worker() -> None:
                try:
                    self._status("Azure Tools: running health check...")
                    summary = audit_duplicates(user_id, table)
                    self._status_q.put(
                        (
                            "azure_tools_done",
                            {
                                "ok": True,
                                "title": "Azure Tools - Health Check",
                                "summary": summary,
                                "message": "Health check completed.",
                            },
                        )
                    )
                except Exception as e:
                    self._status_q.put(
                        (
                            "azure_tools_done",
                            {
                                "ok": False,
                                "title": "Azure Tools - Health Check",
                                "message": str(e),
                            },
                        )
                    )

            self._start_azure_tool_worker(
                title="Azure Tools - Health Check", worker=_worker
            )
        except Exception as e:
            messagebox.showerror("Azure Tools", str(e))

    def on_azure_backup(self) -> None:
        try:
            self._sync_to_creds()
            user_id = self._get_scoped_user_id_from_creds()
            table = self._get_azure_tools_table()

            def _worker() -> None:
                try:
                    self._status("Azure Tools: exporting backup...")
                    summary = backup_user_rows(user_id, table)
                    self._status_q.put(
                        (
                            "azure_tools_done",
                            {
                                "ok": True,
                                "title": "Azure Tools - Backup",
                                "summary": summary,
                                "message": "Backup completed.",
                            },
                        )
                    )
                except Exception as e:
                    self._status_q.put(
                        (
                            "azure_tools_done",
                            {
                                "ok": False,
                                "title": "Azure Tools - Backup",
                                "message": str(e),
                            },
                        )
                    )

            self._start_azure_tool_worker(title="Azure Tools - Backup", worker=_worker)
        except Exception as e:
            messagebox.showerror("Azure Tools", str(e))

    def on_azure_normalize(self) -> None:
        try:
            self._sync_to_creds()
            self._require_advanced_tools(
                phrase="NORMALIZE", action="Normalize My UserID"
            )
            user_id = self._get_scoped_user_id_from_creds()
            table = self._get_azure_tools_table()

            def _worker() -> None:
                try:
                    preview = preview_normalize_userid(user_id, table)
                    msg = (
                        "You are about to normalize UserID padding for this user.\n\n"
                        f"UserID: {user_id}\n"
                        f"Rows affected: {preview['rows_with_padding']}\n\n"
                        "Proceed?"
                    )
                    if not self._confirm_action_threadsafe("Confirm Normalize", msg):
                        self._status_q.put(
                            (
                                "azure_tools_done",
                                {
                                    "ok": True,
                                    "title": "Azure Tools - Normalize",
                                    "message": "Cancelled.",
                                },
                            )
                        )
                        return

                    self._status("Azure Tools: normalizing UserID...")
                    summary = normalize_userid(user_id, table)
                    self._status_q.put(
                        (
                            "azure_tools_done",
                            {
                                "ok": True,
                                "title": "Azure Tools - Normalize",
                                "summary": summary,
                                "message": summary.get(
                                    "message", "Normalization completed."
                                ),
                            },
                        )
                    )
                except Exception as e:
                    self._status_q.put(
                        (
                            "azure_tools_done",
                            {
                                "ok": False,
                                "title": "Azure Tools - Normalize",
                                "message": str(e),
                            },
                        )
                    )

            self._start_azure_tool_worker(
                title="Azure Tools - Normalize", worker=_worker
            )
        except Exception as e:
            messagebox.showerror("Azure Tools", str(e))

    def on_azure_create_index(self) -> None:
        try:
            self._sync_to_creds()
            self._require_advanced_tools(
                phrase="INDEX", action="Create/Verify My Unique Index"
            )
            user_id = self._get_scoped_user_id_from_creds()
            table = self._get_azure_tools_table()
            index_name = os.getenv("AZURE_SQL_UNIQUE_INDEX_NAME") or None

            def _worker() -> None:
                try:
                    variants = check_raw_userid_variants(user_id, table)
                    msg = (
                        "You are about to create/verify a scoped unique index for this user.\n\n"
                        f"UserID: {user_id}\n"
                        f"Raw variants: {variants['variant_count']}\n\n"
                        "Proceed?"
                    )
                    if not self._confirm_action_threadsafe("Confirm Index", msg):
                        self._status_q.put(
                            (
                                "azure_tools_done",
                                {
                                    "ok": True,
                                    "title": "Azure Tools - Index",
                                    "message": "Cancelled.",
                                },
                            )
                        )
                        return

                    self._status("Azure Tools: creating/verifying scoped index...")
                    summary = create_unique_index(
                        scope="scoped",
                        user_id=user_id,
                        table=table,
                        index_name=index_name,
                    )
                    self._status_q.put(
                        (
                            "azure_tools_done",
                            {
                                "ok": True,
                                "title": "Azure Tools - Index",
                                "summary": summary,
                                "message": summary.get(
                                    "message", "Index operation completed."
                                ),
                            },
                        )
                    )
                except Exception as e:
                    self._status_q.put(
                        (
                            "azure_tools_done",
                            {
                                "ok": False,
                                "title": "Azure Tools - Index",
                                "message": str(e),
                            },
                        )
                    )

            self._start_azure_tool_worker(title="Azure Tools - Index", worker=_worker)
        except Exception as e:
            messagebox.showerror("Azure Tools", str(e))

    def on_azure_cleanup_wizard(self) -> None:
        try:
            self._sync_to_creds()
            if not bool(self.var_enable_azure.get()):
                raise ValueError(
                    "Azure upload is disabled. Enable Azure upload to use Azure Tools."
                )
            if bool(self.var_dry_run.get()):
                raise ValueError(
                    "Dry run is enabled. Turn it off to use write operations."
                )
            if not bool(self.var_azure_tools_enabled.get()):
                raise ValueError("Enable Advanced Azure Tools to proceed.")
            user_id = self._get_scoped_user_id_from_creds()
            table = self._get_azure_tools_table()

            wiz = tk.Toplevel(self.master)
            wiz.title("Emergency Cleanup Wizard")
            wiz.resizable(False, False)

            var_choice = tk.StringVar(value="reset")
            var_status = tk.StringVar(value="Ready.")
            var_backup_done = tk.BooleanVar(value=False)

            ttk.Label(wiz, text="Choose remediation option:", justify="left").grid(
                row=0, column=0, columnspan=2, sticky="w"
            )
            rb_reset = ttk.Radiobutton(
                wiz,
                text="Reset my Azure rows and republish from canonical CSV (recommended)",
                variable=var_choice,
                value="reset",
            )
            rb_reset.grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))

            identifier = detect_row_identifier(table)
            rb_dedupe = ttk.Radiobutton(
                wiz,
                text="Attempt surgical dedupe (only if safe row identifier exists)",
                variable=var_choice,
                value="dedupe",
                state=("normal" if identifier else "disabled"),
            )
            rb_dedupe.grid(row=2, column=0, columnspan=2, sticky="w")

            ttk.Label(wiz, textvariable=var_status).grid(
                row=3, column=0, columnspan=2, sticky="w", pady=(8, 0)
            )

            def _run_backup() -> None:
                def _worker() -> None:
                    try:
                        self._status("Azure Tools: wizard backup...")
                        summary = backup_user_rows(user_id, table)
                        self.master.after(
                            0,
                            lambda: (
                                var_backup_done.set(True),
                                var_status.set("Backup complete."),
                            ),
                        )
                        self._status_q.put(
                            (
                                "azure_tools_done",
                                {
                                    "ok": True,
                                    "title": "Azure Tools - Backup",
                                    "summary": summary,
                                },
                            )
                        )
                    except Exception as e:
                        self._status_q.put(
                            (
                                "azure_tools_done",
                                {
                                    "ok": False,
                                    "title": "Azure Tools - Backup",
                                    "message": str(e),
                                },
                            )
                        )

                self._start_azure_tool_worker(
                    title="Azure Tools - Backup", worker=_worker
                )

            def _run_cleanup() -> None:
                if not var_backup_done.get():
                    messagebox.showerror("Wizard", "Run backup first.")
                    return

                choice = var_choice.get()
                phrase = "WIPE MY ROWS" if choice == "reset" else "DEDUPE"
                if self.var_azure_tools_phrase.get().strip() != phrase:
                    messagebox.showerror(
                        "Wizard", f"Type {phrase!r} to confirm this action."
                    )
                    return

                msg = (
                    "You are about to run emergency cleanup.\n\n"
                    f"UserID: {user_id}\n"
                    f"Mode: {choice}\n\n"
                    "Proceed?"
                )
                if not messagebox.askokcancel(
                    "Confirm Emergency Cleanup", msg, icon="warning"
                ):
                    return

                def _worker() -> None:
                    try:
                        self._status("Azure Tools: wizard audit...")
                        audit_summary = audit_duplicates(user_id, table)
                        self._status_q.put(
                            (
                                "azure_tools_done",
                                {
                                    "ok": True,
                                    "title": "Azure Tools - Audit",
                                    "summary": audit_summary,
                                    "suppress_dialog": True,
                                    "defer_running_reset": True,
                                },
                            )
                        )

                        if choice == "dedupe":
                            self._status("Azure Tools: wizard dedupe...")
                            dedupe_summary = dedupe_user_marketid(user_id, table)
                            self._status_q.put(
                                (
                                    "azure_tools_done",
                                    {
                                        "ok": True,
                                        "title": "Azure Tools - Dedupe",
                                        "summary": dedupe_summary,
                                        "suppress_dialog": True,
                                        "defer_running_reset": True,
                                    },
                                )
                            )
                        else:
                            self._status("Azure Tools: wizard delete...")
                            delete_summary = delete_user_rows(user_id, table)
                            self._status_q.put(
                                (
                                    "azure_tools_done",
                                    {
                                        "ok": True,
                                        "title": "Azure Tools - Delete",
                                        "summary": delete_summary,
                                        "suppress_dialog": True,
                                        "defer_running_reset": True,
                                    },
                                )
                            )

                        self._status("Azure Tools: wizard normalize...")
                        normalize_summary = normalize_userid(user_id, table)
                        self._status_q.put(
                            (
                                "azure_tools_done",
                                {
                                    "ok": True,
                                    "title": "Azure Tools - Normalize",
                                    "summary": normalize_summary,
                                    "suppress_dialog": True,
                                    "defer_running_reset": True,
                                },
                            )
                        )

                        self._status("Azure Tools: wizard index...")
                        index_summary = create_unique_index(
                            scope="scoped",
                            user_id=user_id,
                            table=table,
                            index_name=None,
                        )
                        self._status_q.put(
                            (
                                "azure_tools_done",
                                {
                                    "ok": True,
                                    "title": "Azure Tools - Index",
                                    "summary": index_summary,
                                    "suppress_dialog": True,
                                    "defer_running_reset": True,
                                },
                            )
                        )

                        self._status("Azure Tools: wizard final audit...")
                        final_audit = audit_duplicates(user_id, table)
                        self._status_q.put(
                            (
                                "azure_tools_done",
                                {
                                    "ok": True,
                                    "title": "Azure Tools - Final Audit",
                                    "summary": final_audit,
                                },
                            )
                        )

                        self.master.after(
                            0,
                            lambda: var_status.set(
                                "Cleanup complete. Now click 'Publish to Azure' to repopulate."
                            ),
                        )
                    except Exception as e:
                        self._status_q.put(
                            (
                                "azure_tools_done",
                                {
                                    "ok": False,
                                    "title": "Azure Tools - Wizard",
                                    "message": str(e),
                                },
                            )
                        )

                self._start_azure_tool_worker(
                    title="Azure Tools - Wizard", worker=_worker
                )

            btn_backup = ttk.Button(wiz, text="Run Backup", command=_run_backup)
            btn_backup.grid(row=4, column=0, sticky="w", pady=(8, 0))

            btn_cleanup = ttk.Button(wiz, text="Run Cleanup", command=_run_cleanup)
            btn_cleanup.grid(row=4, column=1, sticky="e", pady=(8, 0))

        except Exception as e:
            messagebox.showerror("Azure Tools", str(e))

    def on_run(self) -> None:
        if self._worker_thread is not None and self._worker_thread.is_alive():
            messagebox.showinfo("Busy", "A run is already in progress.")
            return

        try:
            self.txt.delete("1.0", "end")
            self._start_run_log()
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
                    "results_csv_dir": results_dir_raw
                    or "(missing: paths.results_csv_dir)",
                    "enable_azure_sql": bool(self.var_enable_azure.get()),
                    "dry_run": bool(self.var_dry_run.get()),
                },
            )

            cfg = self._build_config_from_ui()

            if cfg.enable_azure_sql and (not cfg.dry_run):
                if (not bool(self.var_allow_publish.get())) or (
                    self.var_publish_text.get().strip() != "PUBLISH"
                ):
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
            self._persist_run_log()
            messagebox.showerror("Run failed", str(e))

    def on_publish_only(self) -> None:
        if self._worker_thread is not None and self._worker_thread.is_alive():
            messagebox.showinfo("Busy", "A run is already in progress.")
            return

        try:
            self.txt.delete("1.0", "end")
            self._start_run_log()
            self._log("-" * 64)
            self._log("Starting publish-only...")

            self._sync_to_creds()

            cfg = self._build_config_from_ui()
            if not cfg.enable_azure_sql:
                raise ValueError(
                    "Azure upload is disabled. Enable Azure upload to publish."
                )
            if cfg.dry_run:
                raise ValueError("Dry run is enabled. Turn it off to publish to Azure.")

            if (not bool(self.var_allow_publish.get())) or (
                self.var_publish_text.get().strip() != "PUBLISH"
            ):
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
            self._persist_run_log()
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
                return self._confirm_publish_cb_threadsafe(
                    user_id=user_id, markets=markets, rows=rows
                )

            manual_days = int(cfg.days)
            recommended_days = None
            recommendation_note = None
            last_settled_date_utc = None
            lookback_source = "ui_default"

            try:
                results_dir_raw = get_nested(creds_copy, "paths.results_csv_dir", None)
                rec, summary, effective_days = self._compute_lookback_recommendation(
                    manual_days=manual_days,
                    results_dir_raw=str(results_dir_raw) if results_dir_raw else None,
                )
                manual_override = bool(self.var_manual_override.get())
                recommended_days = int(rec.get("recommended_days", 0) or 0)
                recommendation_note = str(rec.get("recommendation_note") or "")
                lookback_source = str(rec.get("lookback_source") or "ui_default")

                if manual_override:
                    effective_days = manual_days

                summary.update(
                    {
                        "manual_override": manual_override,
                        "manual_days": manual_days if manual_override else "(disabled)",
                        "effective_days": effective_days,
                        "lookback_source": lookback_source,
                        "missing_range": rec.get("missing_range"),
                    }
                )
                status_cb(format_block("Lookback Summary", summary).rstrip())
                status_cb(f"Using lookback_days={effective_days}")

                recommended_days = effective_days
                cfg = replace(cfg, days=int(effective_days))
                self._apply_lookback_panel(rec)
            except Exception as e:
                status_cb("Lookback: lookback_source=ui_default")
                status_cb(
                    f"Lookback: failed to compute (using configured days={cfg.days}): {e}"
                )

            result = run_downloader(
                cfg,
                creds_copy,
                status_cb=status_cb,
                confirm_publish_cb=confirm_publish_cb,
                last_settled_date_utc=last_settled_date_utc,
                recommended_days=recommended_days,
                recommendation_note=recommendation_note,
                lookback_source=lookback_source,
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
                return self._confirm_publish_cb_threadsafe(
                    user_id=user_id, markets=markets, rows=rows
                )

            result = publish_to_azure_from_canonical_incremental(
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
