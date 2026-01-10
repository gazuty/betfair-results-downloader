from __future__ import annotations

import traceback
import tkinter as tk
from tkinter import ttk, messagebox

from .config import DownloaderConfig
from .run import run_downloader
from .secrets import (
    ensure_credentials_file_exists,
    load_credentials,
    save_credentials,
    get_nested,
    set_nested,
    validate_credentials,
)


class App(ttk.Frame):
    def __init__(self, master: tk.Tk):
        super().__init__(master, padding=12)
        self.master = master

        ensure_credentials_file_exists()
        self.creds = load_credentials()

        # --- Vars (Betfair) ---
        self.var_bf_user = tk.StringVar(value=str(get_nested(self.creds, "betfair.username", "")))
        self.var_bf_pass = tk.StringVar(value=str(get_nested(self.creds, "betfair.password", "")))
        self.var_bf_appkey = tk.StringVar(value=str(get_nested(self.creds, "betfair.app_key", "")))

        # --- Vars (Run config) ---
        self.var_user_id = tk.StringVar(value=str(get_nested(self.creds, "user.user_id", "Gazuty")))
        self.var_days = tk.StringVar(value=str(get_nested(self.creds, "user.days", 7)))

        self.var_horses = tk.BooleanVar(value=bool(get_nested(self.creds, "user.include_horses", True)))
        self.var_greyhounds = tk.BooleanVar(value=bool(get_nested(self.creds, "user.include_greyhounds", True)))

        self.var_enable_azure = tk.BooleanVar(value=bool(get_nested(self.creds, "user.enable_azure_sql", False)))
        self.var_dry_run = tk.BooleanVar(value=bool(get_nested(self.creds, "user.dry_run", True)))

        # --- Vars (Azure) ---
        self.var_az_server = tk.StringVar(value=str(get_nested(self.creds, "azure_sql.server", "")))
        self.var_az_db = tk.StringVar(value=str(get_nested(self.creds, "azure_sql.database", "")))
        self.var_az_user = tk.StringVar(value=str(get_nested(self.creds, "azure_sql.username", "")))
        self.var_az_pass = tk.StringVar(value=str(get_nested(self.creds, "azure_sql.password", "")))
        self.var_az_driver = tk.StringVar(
            value=str(get_nested(self.creds, "azure_sql.driver", "ODBC Driver 18 for SQL Server"))
        )

        self._build()

    # ---------------- UI ----------------

    def _build(self) -> None:
        self.master.title("Betfair Results Downloader (GUI)")
        self.master.minsize(760, 520)

        self.grid(row=0, column=0, sticky="nsew")
        self.master.columnconfigure(0, weight=1)
        self.master.rowconfigure(0, weight=1)

        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)

        # --- Betfair ---
        bf = ttk.LabelFrame(self, text="Betfair Credentials", padding=10)
        bf.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        bf.columnconfigure(1, weight=1)

        ttk.Label(bf, text="Username").grid(row=0, column=0, sticky="w")
        ttk.Entry(bf, textvariable=self.var_bf_user).grid(row=0, column=1, sticky="ew", padx=(10, 0))

        ttk.Label(bf, text="Password").grid(row=1, column=0, sticky="w")
        ttk.Entry(bf, textvariable=self.var_bf_pass, show="•").grid(row=1, column=1, sticky="ew", padx=(10, 0))

        ttk.Label(bf, text="App Key").grid(row=2, column=0, sticky="w")
        ttk.Entry(bf, textvariable=self.var_bf_appkey, show="•").grid(row=2, column=1, sticky="ew", padx=(10, 0))

        # --- Run config ---
        rc = ttk.LabelFrame(self, text="Run Configuration", padding=10)
        rc.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        for i in range(4):
            rc.columnconfigure(i, weight=1)

        ttk.Label(rc, text="User ID").grid(row=0, column=0, sticky="w")
        ttk.Entry(rc, textvariable=self.var_user_id).grid(row=0, column=1, sticky="ew", padx=(10, 20))

        ttk.Label(rc, text="Days to download").grid(row=0, column=2, sticky="w")
        ttk.Entry(rc, textvariable=self.var_days, width=8).grid(row=0, column=3, sticky="w", padx=(10, 0))

        ttk.Checkbutton(rc, text="Horses (eventTypeId 7)", variable=self.var_horses).grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(6, 0)
        )
        ttk.Checkbutton(rc, text="Greyhounds (eventTypeId 4339)", variable=self.var_greyhounds).grid(
            row=1, column=2, columnspan=2, sticky="w", pady=(6, 0)
        )

        # --- Azure ---
        az = ttk.LabelFrame(self, text="Azure SQL (Optional)", padding=10)
        az.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        az.columnconfigure(1, weight=1)

        ttk.Checkbutton(az, text="Enable Azure upload", variable=self.var_enable_azure).grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(az, text="Dry run (recommended)", variable=self.var_dry_run).grid(
            row=0, column=1, sticky="w", padx=(10, 0)
        )

        ttk.Label(az, text="Server").grid(row=1, column=0, sticky="w")
        ttk.Entry(az, textvariable=self.var_az_server).grid(row=1, column=1, sticky="ew", padx=(10, 0))

        ttk.Label(az, text="Database").grid(row=2, column=0, sticky="w")
        ttk.Entry(az, textvariable=self.var_az_db).grid(row=2, column=1, sticky="ew", padx=(10, 0))

        ttk.Label(az, text="Username").grid(row=3, column=0, sticky="w")
        ttk.Entry(az, textvariable=self.var_az_user).grid(row=3, column=1, sticky="ew", padx=(10, 0))

        ttk.Label(az, text="Password").grid(row=4, column=0, sticky="w")
        ttk.Entry(az, textvariable=self.var_az_pass, show="•").grid(row=4, column=1, sticky="ew", padx=(10, 0))

        ttk.Label(az, text="ODBC Driver").grid(row=5, column=0, sticky="w")
        ttk.Entry(az, textvariable=self.var_az_driver).grid(row=5, column=1, sticky="ew", padx=(10, 0))

        # --- Output + buttons ---
        out = ttk.LabelFrame(self, text="Output", padding=10)
        out.grid(row=3, column=0, sticky="nsew")
        out.columnconfigure(0, weight=1)
        out.rowconfigure(0, weight=1)

        self.txt = tk.Text(out, height=10, wrap="word")
        self.txt.grid(row=0, column=0, sticky="nsew")

        self._log("Loaded credentials file. Sensitive fields are masked in UI display only.")

        btns = ttk.Frame(self)
        btns.grid(row=4, column=0, sticky="ew", pady=(10, 0))
        btns.columnconfigure(0, weight=1)

        ttk.Button(btns, text="Clear Output", command=self.on_clear).grid(row=0, column=0, sticky="w")
        ttk.Button(btns, text="Save Settings", command=self.on_save).grid(row=0, column=1, sticky="e", padx=(0, 8))
        ttk.Button(btns, text="Validate", command=self.on_validate).grid(row=0, column=2, sticky="e", padx=(0, 8))
        ttk.Button(btns, text="Run Downloader", command=self.on_run).grid(row=0, column=3, sticky="e")

    # ---------------- Helpers ----------------

    def _log(self, msg: str) -> None:
        self.txt.insert("end", msg.rstrip() + "\n")
        self.txt.see("end")

    def _sync_to_creds(self) -> None:
        # Betfair
        set_nested(self.creds, "betfair.username", self.var_bf_user.get().strip())
        set_nested(self.creds, "betfair.password", self.var_bf_pass.get())
        set_nested(self.creds, "betfair.app_key", self.var_bf_appkey.get())

        # User/run config
        set_nested(self.creds, "user.user_id", self.var_user_id.get().strip())
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
            user_id=self.var_user_id.get().strip(),
        )

    # ---------------- Actions ----------------

    def on_clear(self) -> None:
        self.txt.delete("1.0", "end")
        self._log("Output cleared.")

    def on_save(self) -> None:
        try:
            # Validate days parse early
            int(self.var_days.get().strip())

            self._sync_to_creds()
            save_credentials(self.creds)

            self._log("Saved secrets/credentials.json")
            messagebox.showinfo("Saved", "Settings saved to secrets/credentials.json")
        except Exception as e:
            messagebox.showerror("Save failed", str(e))

    def on_validate(self) -> None:
        try:
            # sync so we validate current UI state (even if not saved yet)
            self._sync_to_creds()
            v = validate_credentials(self.creds)

            if v.ok:
                self._log("VALIDATION OK ✅")
                messagebox.showinfo("Validation", "Credentials look valid.")
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
        try:
            # Clear output each run so it’s obvious what belongs to this run
            self.txt.delete("1.0", "end")
            self._log("Starting run...")

            # Keep creds in sync with UI (in-memory)
            self._sync_to_creds()

            # Safety: Azure non-dry-run is not implemented on feature/gui
            if bool(self.var_enable_azure.get()) and (not bool(self.var_dry_run.get())):
                raise ValueError(
                    "Azure upload is enabled but Dry run is unchecked.\n\n"
                    "Azure publishing is not implemented yet on feature/gui.\n"
                    "Please re-check Dry run, or disable Azure upload."
                )

            cfg = self._build_config_from_ui()
            result = run_downloader(cfg, self.creds)

            if isinstance(result, dict) and result.get("message"):
                self._log(str(result["message"]))
            else:
                self._log("Run completed (GUI branch).")

            if isinstance(result, dict) and "plan" in result:
                self._log(f"Plan: {result.get('plan')}")

            if isinstance(result, dict) and "download" in result:
                self._log(f"Download: {result['download']}")
            if isinstance(result, dict) and "azure" in result:
                self._log(f"Azure: {result['azure']}")

            messagebox.showinfo("Run complete", "Run finished. See Output for details.")
        except Exception as e:
            self._log("ERROR:")
            self._log(str(e))
            self._log(traceback.format_exc())
            messagebox.showerror("Run failed", str(e))


def main() -> None:
    root = tk.Tk()

    # Use themed widgets
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
