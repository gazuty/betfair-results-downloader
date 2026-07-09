# Release Notes — v0.6.0

**Date:** 2026-07-09

## Summary

Data lifecycle management, prompted by a real outage: 94 unbounded
full-copy snapshots (~25 GB) pushed the OneDrive-hosted results folder
into dataless placeholder eviction on macOS, which broke both scheduled
downloads (cert file reads) and DM reports (CSV reads) with
`OSError: [Errno 11] Resource deadlock avoided`.

This release keeps the results folder permanently small: snapshots are
compressed and pruned automatically, and old canonical rows move into
yearly archives. It also fixes the `ScheduleState` schema upgrade script,
which could never have succeeded as shipped.

## Added

- **Snapshot retention** — only the newest `user.snapshot_retention_days`
  (default 14) dated snapshots are kept; older ones are deleted after each
  run. Set to `0` to disable.
- **Snapshot compression** — snapshots are written as
  `cleared_orders_cleaned_YYYY-MM-DD.csv.gz` (~18× smaller). Opt out with
  `user.compress_snapshots: false`. The canonical CSV stays uncompressed.
- **Yearly canonical archival** — rows settled more than
  `user.canonical_archive_months` ago (default 12) move from the canonical
  into `cleared_orders_archive_YYYY.csv.gz`, deduplicated on `betId` and
  safe to re-run. Set to `0` to disable.

## Changed

- **`itemDescription` is no longer downloaded.** The verbose JSON blob was
  ~45% of canonical file size on disk and nothing downstream reads it —
  event and market names come from the enrichment columns. Existing
  canonical files keep the column until stripped; new rows won't have it.
- README now recommends a local, non-cloud-synced certs directory
  (e.g. `~/.betfair/certs`). Cloud sync clients can evict cert files to
  online-only placeholders, silently breaking non-interactive login.

## Fixed

- `scripts/azure_upgrade_schedulestate.py` sent its `ALTER TABLE`s and
  backfill `UPDATE`s in one T-SQL batch, so SQL Server rejected the whole
  script at compile time (`Invalid column name`). DDL and backfill now run
  as separate batches.

## Upgrade notes

- **Run `python scripts/azure_upgrade_schedulestate.py` once** if you use
  the intraday scheduler. Until it succeeds, every scheduled run logs
  `Failed to upsert ScheduleState ... Invalid column name
  'LastCoveredDateLocal'` and the Azure checkpoint never advances — the
  scheduler silently depends on the canonical CSV alone.
- Existing uncompressed snapshots are pruned by the retention policy as
  new runs happen; no manual cleanup is required (but deleting old ones
  by hand reclaims space immediately).
- If your machine syncs the results folder with OneDrive/iCloud: keep the
  sync client running at login, and consider pinning
  `cleared_orders_cleaned.csv` and `run_state.json`
  ("Always Keep on This Device") plus moving certs to a local directory.
