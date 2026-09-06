# Contributing

## Development Setup

macOS / Linux:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
pip install ruff pytest pytest-cov
```

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
pip install ruff pytest pytest-cov
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

CI runs the same three checks on Python 3.10, 3.12, 3.13 and 3.14 (Ubuntu).
Coverage is reported in CI; locally, `pytest --cov=betfair_results_downloader`
needs `pytest-cov` installed.

## Pull Request Workflow

Every change, however small, goes through the same loop:

1. **Branch** off `main` (`feat/...`, `fix/...`, `docs/...`). One PR per change.
2. **Plan before code** for anything non-trivial: read the affected modules and
   their tests, decide the design, and settle questions that change it up front.
3. **Tests at every layer** — pure logic, pipeline wiring and failure isolation,
   and the CLI surface. A test must be able to fail for the reason it exists.
   Tests whose expectations change by design are updated, not deleted.
4. **Local gates** before every push: `ruff check .`, `ruff format --check .`,
   `pytest -q`.
5. **Open the PR** with an imperative title and a body that says why, what, and
   how it was verified (include any live-data check that was run).
6. **CI green** on every matrix job.
7. **Codex review.** Codex reviews on PR open; after each fix push, comment
   `@codex review` to re-run it. Treat every P1/P2 as real until disproved,
   fix it with a test, reply on the thread with what changed, and resolve the
   thread. Repeat until Codex reports no major issues on the final commit.
8. **Merge** with a merge commit once CI and Codex are both clean on the same
   commit, delete the branch, and add a `CHANGELOG.md` entry under
   `[Unreleased]` if the PR did not already.

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
