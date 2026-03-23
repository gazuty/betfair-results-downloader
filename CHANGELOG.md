## [0.4.0] - 2026-02-15

- Lookback v2: recommends days based on missing settled-date gaps, run_state watermark, or CSV heuristics
- GUI workflow clarified with step-by-step guidance and buttons placed by task area
- Run logs saved per execution for reproducible debugging
- Manual override is explicit via checkbox, defaulting to auto lookback
- Reporting Dashboard added (Streamlit): overview, daily, and weekly pages

## [0.3.0] - 2026-01-19

- Added Azure Tools GUI (health check, backup, normalize, scoped index, cleanup wizard)
- Hardened Azure publishing invariants with scoped uniqueness enforcement
- Added remediation module and script wrappers for safe recovery
- No changes to core downloader behavior

## [0.2.0-gui] - 2026-01-05

- GUI-first downloader with smart lookback and publish-only Azure
- Smart lookback recommendation based on latest settledDate in canonical CSV
- Publish-only Azure button allows publishing from canonical CSV without downloading
- Cleared orders now capture Betfair itemDescription metadata
