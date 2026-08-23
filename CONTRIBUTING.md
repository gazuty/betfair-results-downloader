# Contributing

## Development Setup

macOS / Linux:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
pip install ruff pytest
```

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
pip install ruff pytest
```

## Quality Checks

Run all three before pushing — CI enforces them:

```bash
ruff check .
ruff format --check .   # or `ruff format .` to apply
pytest -q
```

The lint rule selection is pinned in `pyproject.toml` (`[tool.ruff.lint] select`)
so results don't drift when a newer ruff changes its defaults. Broaden the
selection deliberately, not by upgrading ruff.

## Running the App

```bash
python -m betfair_results_downloader run
python -m betfair_results_downloader audit
python -m betfair_results_downloader dm-report
```

## Line Endings

This repo enforces LF line endings via `.gitattributes`. Avoid manually converting
line endings or altering git autocrlf settings for this project.

## Notebook Lock (OneDrive/Windows)

Notebooks can be locked by OneDrive, VS Code, or Jupyter. If the notebook is
constantly showing as modified, you can temporarily mark it as unchanged:

```powershell
git update-index --skip-worktree notebooks/betfair_results_downloader.ipynb
```

To restore tracking after closing VS Code/Jupyter and pausing OneDrive:

```powershell
git update-index --no-skip-worktree notebooks/betfair_results_downloader.ipynb
```

## Release Process (Lightweight)

- Bump version in `pyproject.toml`
- Rename the `[Unreleased]` section in `CHANGELOG.md` to the version (add a fresh empty `[Unreleased]`)
- Run `ruff check .`, `ruff format --check .`, `pytest -q`
- `git tag -a vX.Y.Z -m "..."` and `git push --follow-tags`
