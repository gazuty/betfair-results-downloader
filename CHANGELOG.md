# Changelog

All notable changes to this project will be documented in this file.
This project follows Semantic Versioning (SemVer), even while pre-1.0.

## [v0.2.0-gui] - 2026-01-18

### Added
- GUI-first workflow is the recommended runner for Windows.
- Cleared orders fetch includes Betfair itemDescription metadata, preserved in canonical and snapshot CSVs.
- Non-blocking smoke logs validate itemDescription presence (in-memory and post-canonical write).
- Smart "Days to download" default based on latest settledDate (UTC) + 1-day overlap, capped at 90; fallback to 90 when no canonical.
- "Publish to Azure" button publishes from the canonical CSV without downloading; respects Dry Run and safety gating; publishes missing markets only.

### Changed
- Publish-only summaries now distinguish publish_requested (intent) vs publish_attempted (execution).

### Fixed
- Precision-safe marketId comparison (Decimal-based) to avoid Azure mismatch issues.

### Notes
- Repo hygiene improvements: .gitignore hardened for secrets, outputs, and build artifacts.

[v0.2.0-gui]: <placeholder>
