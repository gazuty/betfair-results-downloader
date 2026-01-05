# Betfair Results Downloader (Settled Orders → CSV)

A production-ready Jupyter notebook that downloads **settled Betfair orders** using `betfairlightweight`, cleans them, writes a canonical CSV plus dated snapshots, and aggregates results to **market-level profit**.

An **optional** Azure SQL publish step is included, but **disabled by default** and fully gated behind configuration + safety switches.

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

## Repository structure

notebooks/ # main notebook lives here
secrets/
credentials.template.json # safe to commit (template only)
credentials.json # local secrets (git-ignored)
outputs/ # generated CSVs (git-ignored)
examples/ # optional: small anonymised samples (committable)
requirements.txt
README.md
.gitignore


---

## Setup

### 1) Create your local credentials file

Copy the template:

- `secrets/credentials.template.json` → `secrets/credentials.json`

Then fill in your Betfair credentials.

> `secrets/credentials.json` is intentionally **git-ignored**.

### 2) Install dependencies

Create/activate a virtual environment, then:
pip install -r requirements.txt


Notes:

pyodbc and SQLAlchemy are only required if you enable Azure SQL publishing.

The notebook still runs end-to-end for CSV generation without Azure enabled.

Configuration

The notebook reads secrets from:

secrets/credentials.json

Template fields

betfair.* — Betfair credentials

paths.results_csv_dir — output directory (recommended: outputs)

user.enable_azure_sql — false by default (must be true to even attempt DB work)

user.db_user_id — your database user identity (not hard-coded in the notebook)

azure_sql.* — connection settings (only used if Azure SQL is enabled)

Safety switches
Azure SQL is optional and disabled by default

Azure SQL publishing is controlled by:

user.enable_azure_sql in secrets/credentials.json (default: false)

When disabled:

No DB connection is required

No DB code is executed

CSV outputs still work fully

DRY_RUN prevents writes by default

All DB writes are guarded by a notebook variable:

DRY_RUN = True (default)

To allow writes, you must explicitly set:

DRY_RUN = False

This is intentional “two-step safety”:

user.enable_azure_sql must be true

DRY_RUN must be False

If either condition is not met, the notebook will not write to Azure SQL.

Sharing with friends / comparing results

This repo does not commit real outputs or secrets.

If you want others to compare structures safely, put small anonymised samples in:

examples/

The .gitignore is designed to ignore outputs/ (real data) while allowing examples/.

Disclaimer

This project is for personal analytics and learning. You are responsible for compliance with Betfair’s terms and any local laws/regulations.


---

## 2) Critical `.gitignore` fix (template won’t commit otherwise)

Your current `.gitignore` ignores the whole `secrets/` directory, then tries to unignore the template. In Git, if a directory is ignored, you must unignore the directory itself as well, otherwise the exception often won’t work as expected.

Update the **secrets** section to this:

```gitignore
# ------------------------------
# Secrets / credentials (DO NOT COMMIT)
# ------------------------------
secrets/*
!secrets/
!secrets/credentials.template.json

*.env
.env
