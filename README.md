# Betfair Results Downloader (Settled Orders → CSV)

A production-ready Jupyter notebook that downloads **settled Betfair orders** using `betfairlightweight`, cleans them, writes a canonical CSV plus dated snapshots, and aggregates results to **market-level profit**.

An **optional** Azure SQL publish step is included, but it is **disabled by default** and gated behind configuration + safety switches.

---

## What this does

1. Downloads settled (cleared) orders via Betfair API (`betfairlightweight`)
2. Cleans/normalises data and writes:
   - a **canonical CSV** (stable filename / latest state)
   - **dated snapshot CSVs** (append-only history)
3. Aggregates to **market-level** results (profit)
4. *(Optional)* Publishes market-level results to **Azure SQL**

> Core functionality is the CSV outputs. Azure SQL is strictly optional.

---

## Azure publishing scope (current restriction)

To reduce risk and keep the database focused, the Azure publish path is currently restricted to:

- **Horse Racing** (`eventTypeId = 7`)
- **Greyhound Racing** (`eventTypeId = 4339`)

This is enforced in the notebook (Cell 9) by building a filtered dataset (`df_azure_upload`) from an **allow-list** of eventTypeIds. Other sports may still be downloaded and written to CSV, but they are **excluded from Azure uploads by design**.

To expand scope later, update the allow-list in Cell 9.

---

## Repository structure

```
src/
  betfair_results_downloader/
    __init__.py
    csv_utils.py

notebooks/
  betfair_results_downloader.ipynb

secrets/
  credentials.template.json   # committed
  credentials.json            # git-ignored

outputs/                      # git-ignored
requirements.txt
README.md
.gitignore
.gitattributes
```

---

## Setup

### 1) Create your local credentials file

Copy the template:

- `secrets/credentials.template.json` → `secrets/credentials.json`

Then fill in your Betfair credentials.

> `secrets/credentials.json` is intentionally **git-ignored**.

### 2) Install dependencies

Create/activate a virtual environment, then:

```bash
pip install -r requirements.txt
```

Notes:
- `pyodbc` is only required if you enable Azure SQL publishing.
- The notebook runs end-to-end for CSV generation without Azure enabled.

---

## Configuration

The notebook reads secrets from:

- `secrets/credentials.json`

### Key fields

- `betfair.*` — Betfair credentials
- `paths.results_csv_dir` — CSV output directory
- `user.enable_azure_sql` — **false by default** (must be true to even attempt DB work)
- `user.db_user_id` — your database user identity (not hard-coded in the notebook)
- `user.dry_run` — **true by default** (must be false to allow DB writes)
- `azure_sql.*` — connection settings (only used if Azure SQL is enabled)

---

## Safety switches (Azure SQL)

Azure SQL publishing is controlled by **two explicit switches** in `secrets/credentials.json`:

1. `user.enable_azure_sql` must be **true** (otherwise no DB work is attempted)
2. `user.dry_run` must be **false** (otherwise the notebook will refuse to write)

This is intentional “two-step safety” to prevent accidental writes.

Example configuration for a real publish:

```json
"user": {
  "enable_azure_sql": true,
  "db_user_id": "Gazuty",
  "dry_run": false
}
```

If either condition is not met, the notebook will not write to Azure SQL.

---

## Running the notebook (repeatable workflow)

After a kernel restart, run cells in order:

1. **Cell 0** — environment setup (repo root, secrets, safety switches)
2. **Cell 1** — project imports (from `src/`)
3. Continue top-to-bottom

---

## Sharing / examples

This repo does not commit real outputs or secrets.

If you want to share structure safely, place small anonymised samples in:

- `examples/`

The `.gitignore` is designed to ignore real outputs (e.g. `outputs/`, `*.csv`) while allowing curated examples under `examples/`.

---

## Notes on `.gitignore`

This repo is configured so that:
- `secrets/credentials.json` is **ignored**
- `secrets/credentials.template.json` is **committed**
- outputs and raw CSVs are **ignored**

If you edit `.gitignore`, keep that behaviour intact.

---

## Disclaimer

This project is for personal analytics and learning. You are responsible for compliance with Betfair’s terms and any local laws/regulations.
