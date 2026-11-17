# Contributing

Thanks for taking the time to improve the project.

This app is small enough that focused contributions are usually the easiest way to make a meaningful impact. Whether you are fixing a bug, improving the library matching logic, polishing the UI, or adding coverage for a tricky edge case, a clear and well-tested change is always appreciated.

## Local setup

From the repository root:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Then run the app with:

```powershell
python main.py
```

## Main project areas

A few places are especially relevant when contributing:

- `main.py` — app logic, scan flow, sync flow, UI, and core helpers
- `cheat_online.py` — online cheat lookup logic and fallback behavior
- `textures_install.py` — texture install pipeline
- `texture_sources.json` — curated pack source mappings
- `theme.py` and `icons.py` — styling and UI polish
- `tests/` — regression tests and fixtures

## Running checks and tests

Before submitting a change, it is a good idea to run the project checks locally.

```powershell
python check_deps.py
python check_pyside_import.py
pytest -q
```

If you are working on a feature or a bug fix, add or update a focused test in the `tests/` folder when possible.

## What makes a good PR

- Keep changes focused and easy to review.
- Prefer small, well-described updates over broader refactors.
- Include tests when the behavior is user-facing or bug-related.
- Update the docs when the workflow, settings, or app behavior changes.
- If you change the UI or user experience, add a short explanation or screenshots if helpful.

## Working with data files

This project relies on a few data sources that should be handled carefully:

- `ps2_cheats_database_merged.json` is the bundled local cheat database.
- `texture_sources.json` contains the curated texture-pack source list.
- New entries should point to valid, working repositories or release assets.

If you are editing these files, double-check that they still match the actual project behavior and avoid adding broken or stale references.

## Style and code conventions

- Match the style already used in the repository.
- Keep UI styling centralized where appropriate instead of scattering inline theme logic across the app.
- Prefer understandable, maintainable code over clever but opaque shortcuts.
- Be careful with platform-specific behavior, especially around Windows and PCSX2 path detection.

## Questions or help

If you are unsure where to start, a good first contribution is usually a small bug fix, test, or documentation improvement. The project is easier to improve when changes stay narrow and easy to verify.

Thanks for helping make the app better.
