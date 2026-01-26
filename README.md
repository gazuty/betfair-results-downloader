# Betfair Results Downloader (GUI-first)

A **professional, GUI-first Python application** for downloading settled Betfair orders, enriching them with market metadata (with caching), writing reliable CSV outputs, and optionally publishing aggregated market results to Azure SQL — **safe by default**.

The GUI is the **official and recommended way** to run this project.

---

## What this does

- Downloads settled (cleared) orders from Betfair using `betfairlightweight`
- Cleans and normalises the data
- Enriches orders with market & event metadata (cached to avoid repeat API calls)
- Writes:
  - a **canonical CSV** (stable filename, always latest state)
  - **dated snapshot CSVs** (append-only history)
- Aggregates results to market-level profit
- *(Optional)* Publishes market-level results to Azure SQL

> Core functionality is **CSV generation**. Azure SQL publishing is optional and heavily gated for safety.

---

## Official runner (GUI)

```powershell
python -m betfair_results_downloader.gui_app
```

The GUI:

- Runs the pipeline in a background thread (no UI freezing)
- Streams live status updates during each phase
- Prints clean, structured summary blocks at the end of each run
- Provides strong safety controls around Azure publishing

---

## Recommended GUI Workflow

Follow the same order shown at the top of the GUI:

1) Choose Paths (Results folder) -> 2) Validate -> 3) Compute Lookback -> 4) Run Downloader -> (Optional) 5) Publish to Azure

---

## Lookback v2 (auto)

The downloader computes an **effective lookback** before a run. Decision order:

1) Missing settled-date gaps within the audit window (<= 90 days) -> recommend based on the earliest missing date in the most recent missing range.
2) Otherwise use `run_state.json` (`last_success_utc`).
3) Otherwise fall back to the canonical CSV latest settledDate heuristic.
4) If no CSV and no run_state exist, default to **90 days** (Betfair maximum backfill).

The audit only considers gaps **between observed settled dates** inside the backfillable window.

---

## Manual Override

By default, the run uses the **computed effective lookback**.

To force a manual value for a single run:

- Tick **Manual override**
- Enter the Days value
- Run Downloader will use that manual Days value for this run only

---

## Run logs

Each run persists a full log transcript for debugging:

`<results_csv_dir>/run_logs/run_YYYYMMDD_HHMMSS.txt`

These logs match the GUI output and are written in UTF-8 with ASCII-safe status lines.

---

## First Run Wizard (onboarding)

On first launch, if no credentials file exists:

- The GUI starts from a template credentials file
- A First Run Wizard opens and guides you through:
  - Choosing where to save `credentials.json`
  - Selecting your results output folder
  - Entering Betfair credentials
  - Setting run defaults (lookback days, sports)
  - *(Optional)* Entering Azure SQL credentials
- The completed credentials file is saved and remembered for future runs

> No manual file copying is required.

---

## Azure publishing safety model (important)

Azure SQL publishing is **safe by default** and requires multiple explicit actions.

### Mandatory conditions

- `enable_azure_sql = true` in credentials
- `dry_run = false` in credentials
- In the GUI:
  - Tick the unlock checkbox
  - Type **PUBLISH** exactly
  - Confirm a final modal dialog after seeing the Azure prep summary

If any step is missing, **no database writes occur**.

---

## Publish-only (Azure)

The GUI includes a **Publish to Azure** button that publishes only from the
canonical CSV. It does **not** download from Betfair.

This flow:

- Reads `cleared_orders_cleaned.csv`
- Builds the Azure dataset (same filter + aggregation as a normal run)
- Applies **incremental sync** (insert + update only)

All Azure publish safety gates still apply.

---

## Azure publishing scope (current restriction)

To reduce risk and keep the database focused, Azure publishing is currently restricted to:

- **Horse Racing** (`eventTypeId = 7`)
- **Greyhound Racing** (`eventTypeId = 4339`)

Other sports:

- are downloaded
- are written to CSV
- are **excluded from Azure uploads by design**

This restriction is enforced in code and can be expanded later if required.

---

## Azure Data Safety & Remediation

Azure publishing is **incremental and non-destructive** by design:

- Sync key is `(UserID, MarketID)`
- Inserts new rows and updates changed rows only
- Leaves DB-only rows unchanged

A **filtered unique index** is enforced per user to prevent duplicates.
Delete-then-insert is intentionally avoided.

The GUI provides **Azure Tools** for safe recovery:

- Read-only health check (duplicate audit)
- Scoped backup export
- UserID normalization (padding fix)
- Scoped unique index creation/verification
- Emergency cleanup wizard (backup -> wipe user rows -> index -> re-audit)

Azure cleanup tools are **user-scoped and guarded**. They exist for recovery,
not routine use.

## Outputs

### CSV outputs

Written to the configured `results_csv_dir`:

- **Canonical CSV**  
  Stable filename representing the latest full dataset

- **Snapshot CSVs**  
  Dated files (e.g. `*_2026-01-11.csv`) for historical tracking

### Enrichment cache artifacts

Written to the project `outputs/` directory:

- Market catalogue cache (CSV)
- Latest enrichment snapshot

These paths are reported in the GUI and accessible via **Open Artifacts Folder**.

---

## Understanding enrichment behaviour

It is normal for Betfair to return **zero market catalogues for settled markets**.

In this case the app will report:

> “API returned 0 catalogues (common for settled markets). Enriched from cache only.”

This is **expected behaviour** and not an error.

---

## Repository structure (simplified)

```
src/
  betfair_results_downloader/
    gui_app.py
    run.py
    pipeline.py
    downloader_core.py
    azure_publish.py
    secrets.py

secrets/
  credentials.template.json   # committed
  credentials.json            # git-ignored (created by GUI)

outputs/                      # git-ignored (cache + artifacts)
README.md
.gitignore
```

---

## Setup (once)

Create and activate a virtual environment, then install in editable mode:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
```

Notes:

- `pyodbc` is only required if Azure SQL publishing is enabled
- The GUI runs fully without Azure enabled

---

## Typical workflow

1. Launch the GUI
2. Complete First Run Wizard (once)
3. Choose Paths (Results folder) and Validate credentials
4. Compute Lookback (recommended)
5. Run Downloader (auto lookback by default, or manual override if enabled)
6. Review structured summary blocks
7. *(Optional)* Publish to Azure if explicitly unlocked
8. *(Optional)* Use Azure Tools for health checks or recovery

---

## Reporting Dashboard (Streamlit)

The Reporting Dashboard is a **local, professional analytics UI** for analysing
Betfair settled (cleared) orders using CSV outputs produced by this project.

It is built with **Streamlit** and is designed for:

- fast iteration
- local-only usage
- code-first, reproducible analysis
- clean separation of data, transforms, and UI

> **Status:** Active development  
> **Branch:** `feature/reporting-dashboard`

### Key Features

- Reads **local canonical CSVs only** (no Azure dependency)
- Timezone-aware reporting (UTC → Australia/Sydney)
- **Sunday–Saturday weekly aggregation**
- Sport filtering (Horses, Greyhounds)
- Daily and weekly P&L views
- KPI summaries (profit, strike rate, averages)
- CSV export of all tables
- Cached loading for large datasets
- Clean, professional Streamlit UI

### Data Requirements

The dashboard expects CSVs produced by the downloader pipeline, typically:

```
cleared_orders_cleaned.csv
cleared_orders_cleaned_YYYYMMDD.csv
```

Minimum required columns:

- `betId`
- `profit`
- `placedDate`
- `settledDate`
- `eventTypeId`
- `evt_countryCode`
- `mkt_marketName`

### Running the Dashboard

From the repository root:

```powershell
streamlit run src/betfair_results_downloader/reporting_app.py
```

In the sidebar:

1. Select the folder containing your cleaned CSVs
2. Choose the canonical file
3. Navigate using the left-hand menu

### Architecture Overview

```
reporting/
  io.py          # CSV discovery, loading, caching
  schema.py      # Normalisation and derived fields
  filters.py     # Sidebar filters
  transforms.py  # Aggregations (daily, weekly, monthly)
  ui.py          # Shared UI components
  pages/         # Individual report pages
```

This design keeps data logic separate from presentation and allows
incremental extension without refactoring.

### Reporting Roadmap

- Track filtering using `evt_venue`
- Monthly and rolling 2 / 4 / 8 week views
- Sport / Country / Track breakdown pages
- Drill-down from aggregates to raw bets
- Additional UI polish and visualisations

---

## Safety notes

- Real credentials and outputs are **never committed**
- `.gitignore` intentionally ignores:
  - `secrets/credentials.json`
  - `outputs/`
  - generated CSVs

Keep this behaviour intact.

---

## Disclaimer

This project is for **personal analytics and learning**.
You are responsible for compliance with Betfair’s terms and any applicable laws or regulations.
