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
3. Click **Run Downloader**
4. Watch live phase progress
5. Review structured summary blocks
6. *(Optional)* Publish to Azure if explicitly unlocked

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
