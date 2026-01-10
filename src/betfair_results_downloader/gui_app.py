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
    mask_value,
    validate_credentials,
)


class App(ttk.Frame):
    def __init__(self, master: tk.Tk):
        super().__init__(master, padding=12)
        self.master = master

        ensure_credentials_file_exists()
        self.creds = load_credentials()

        # Vars
        self.var_bf_user = tk.StringVar(value=str(get_nested(self.creds, "betfair.username", "")))
        self.var_bf_pass = tk.StringVar(value=str(get_nested(self.creds, "betfair.password", "")))
        self.var_bf_appkey = tk.StringVar(value=str(get_nested(self.creds, "betfair.app_key", "")))

        self.var_user_id = tk.StringVar(value=str(get_nested(self.creds, "user.user_id", "Gazuty")))
        self.var_days = tk.StringVar(value=str(get_nested(self.creds, "user.days", 7)))

        self.var_horses = tk.BooleanVar(value=bool(get_nested(self.creds, "user.include_horses", True)))
        self.var_greyhounds = tk.BooleanVar(value=bool(get_nested(self.creds, "user.include_greyhounds", True)))

        self.var_enable_azure = tk.BooleanVar(value=bool(get_nested(self.creds, "user.enable_azure_sql", False)))
        self.var_dry_run = tk.BooleanVar(value=bool(get_nested(self.creds, "user.dry_run", True)))

        self.var_az_server = tk.StringVar(value=str(get_nested(self.creds, "azure_sql.server", "")))
        self.var_az_db = tk.StringVar(value=str(get_nested(self.creds, "azure_sql.database", "")))
        self.var_az_user = tk.StringVar(value=str(get_nested(self.creds, "azure_sql.username", "")))
        self.var_az_pass = tk.StringVar(value=str(get_nested(self.creds, "azure_sql.password", "")))
        self.var_az_driver = tk.StringVar(value=str(get_nested(self.creds, "azure_sql.driver", "ODBC Driver 18 for SQL Server")))

        # UI
        self._build()

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

        ttk.Checkbutton(rc, text="Horses (eventTypeId 7)", variable=self.var_horses).grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Checkbutton(rc, text="Greyhounds (eventTypeId 4339)", variable=self.var_greyhounds).grid(row=1, column=2, columnspan=2, sticky="w", pady=(6, 0))

        # --- Azure ---
        az = ttk.LabelFrame(self, text="Azure SQL (Optional)", padding=10)
        az.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        az.columnconfigure(1, weight=1)

        ttk.Checkbutton(az, text="Enable Azure upload", variable=self.var_enable_azure).grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(az, text="Dry run (recommended)", variable=self.var_dry_run).grid(row=0, column=1, sticky="w", padx=(10, 0))

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

        ttk.Button(btns, text="Save Settings", command=self.on_save).grid(row=0, column=1, sticky="e", padx=(0, 8))
        ttk.Button(btns, text="Validate", command=self.on_validate).grid(row=0, column=2, sticky="e", padx=(0, 8))
        ttk.Button(btns, text="Run Downloader", command=self.on_run).grid(row=0, column=3, sticky="e")

    def _log(self, msg: str) -> None:
        self.txt.insert("end", msg.strip() + "\n")
        self.txt.see("end")

    def _sync_to_creds(self) -> None:
        set_nested(self.creds, "betfair.username", self.var_bf_user.get().strip())
        set_nested(self.creds, "betfair.password", self.var_bf_pass.get())
        set_nested(self.creds, "betfair.app_key", self.var_bf_appkey.get())

        set_nested(self.creds, "user.user_id", self.var_user_id.get().strip())
        set_nested(self.creds, "user.days", int(self.var_days.get().strip()))
        set_nested(self.creds, "user.include_horses", bool(self.var_horses.get()))
        set_nested(self.creds, "user.include_greyhounds", bool(self.var_greyhounds.get()))
        set_nested(self.creds, "user.enable_azure_sql", bool(self.var_enable_azure.get()))
        set_nested(self.creds, "user.dry_run", bool(self.var_dry_run.get()))

        set_nested(self.creds, "azure_sql.server", self.var_az_server.get().strip())
        set_nested(self.creds, "azure_sql.database", self.var_az_db.get().strip())
        set_nested(self.creds, "azure_sql.username", self.var_az_user.get().strip())
        set_nested(self.creds, "azure_sql.password", self.var_az_pass.get())
        set_nested(self.creds, "azure_sql.driver", self.var_az_driver.get().strip())

    def on_save(self) -> None:
        try:
            # validate days parse early
            int(self.var_days.get().strip())
            self._sync_to_creds()
            save_credentials(self.creds)
            self._log("Saved secrets/credentials.json")
            messagebox.showinfo("Saved", "Settings saved to secrets/credentials.json")
        except Exception as e:
            messagebox.showerror("Save failed", str(e))

    def on_validate(self) -> None:
        try:
            self._sync_to_creds()
            v = validate_credentials(self.creds)
            if v.ok:
                self._log("VALIDATION OK ✅")
                messagebox.showinfo("Validation", "Credentials look valid.")
            else:
                self._log("VALIDATION FAILED ❌")
                for err in v.errors:
                    self._log(f"- {err}")
                messagebox.showwarning("Validation", "Some required fields are missing.\nSee Output.")
        except Exception as e:
            messagebox.showerror("Validation error", str(e))

    def on_run(self) -> None:
        try:
            self._sync_to_creds()

            cfg = DownloaderConfig(
                days=int(self.var_days.get().strip()),
                include_horses=bool(self.var_horses.get()),
                include_greyhounds=bool(self.var_greyhounds.get()),
                enable_azure_sql=bool(self.var_enable_azure.get()),
                dry_run=bool(self.var_dry_run.get()),
                user_id=self.var_user_id.get().strip() or None,
            )

            self._log("Running...")
            result = run_downloader(cfg, self.creds)
            self._log(result.get("message", "Done."))
            self._log(f"Plan: {result.get('plan')}")
            messagebox.showinfo("Run complete", result.get("message", "Done."))
        except Exception as e:
            self._log("ERROR:")
            self._log(str(e))
            self._log(traceback.format_exc())
            messagebox.showerror("Run failed", str(e))


def main() -> None:
    root = tk.Tk()
    # better-ish default theme on Windows
    try:
        style = ttk.Style(root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
    except Exception:
        pass

    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
