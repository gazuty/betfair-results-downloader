# Contributing

## Development Setup

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
```

## Quality Checks

```powershell
ruff check .
ruff format .
pytest -q
```

## Running the App

```powershell
python -m betfair_results_downloader run
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
- Add a section for the version to `CHANGELOG.md`
- Run `ruff check .`, `ruff format .`, `pytest -q`
- `git tag -a vX.Y.Z -m "..."` and `git push --follow-tags`
