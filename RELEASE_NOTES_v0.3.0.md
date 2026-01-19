Azure Safety, Incremental Sync Hardening, and GUI Azure Tools

Highlights
- Added Azure Tools GUI: health check, backup, normalize, scoped index creation, cleanup wizard
- Enforced per-user uniqueness via filtered unique index
- Publish-only flow uses incremental sync (insert + update)

Breaking changes
- None. Publishing now blocks if duplicate keys exist, by design.

Upgrade notes
- If your Azure table has duplicate (UserID, MarketID) rows, use Azure Tools -> Cleanup Wizard.
- The tools are user-scoped and guarded; they are for recovery, not routine use.

Safety guarantees
- No destructive behavior by default
- All deletes are user-scoped, confirmed, and optional
- Incremental sync remains non-destructive and safe-by-default
