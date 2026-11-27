# Contributing to PCTX

Bug reports, fixes, new texture sources and UI improvements are all welcome. Small, focused changes are the easiest to review.

## Setup

PCTX targets Windows and Python 3.10+.

```powershell
git clone https://github.com/DahSidiAbdallah/PCTX.git
cd PCTX
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt -r requirements-dev.txt
python main.py
```

Run it from the project folder; some data files are found relative to it.

## Tests

```powershell
python -m pytest -q
```

- Tests run headless (`QT_QPA_PLATFORM=offscreen`). `tests/conftest.py` also points the app's settings at a throwaway INI file through `PCTX_SETTINGS_FILE`, so the suite never reads or writes your real settings. If you write a script that constructs `MainWindow`, set that variable too.
- `conftest.py` turns off Chromium's sandbox and GPU for the test process only. Do not copy those flags into the app itself.
- Qt windows containing a web view are expensive and make teardown flaky when many are created in one process. Share one window per test module (see `tests/test_ui_modes.py`).
- QtWebEngine occasionally crashes at interpreter exit in sandboxed environments. If a green run exits non-zero with no test failure, re-run it.
- Network code takes an injectable `getter` (see `patches_online.py`) so tests never touch the network. Keep it that way.

## Where to change what

| To change | Look at |
|---|---|
| Colors, fonts, QSS | `theme.py`. Classic is Fusion plus a neutral dark palette and very little QSS; Big Picture has no native dialogs at all |
| The Big Picture screen | `web/bigpicture.html`, `web/input.js` (gamepad, key repeat and which-device-was-used-last), `web/sound.js` (UI sounds, synthesized with Web Audio, no audio files). `bigpicture.py` is the Qt bridge: the page calls Python with `callAsync()`, limited to the methods registered in `LibraryView.bp_rpc_methods()` / `bp_rpc_async_methods()`; slow ones run on a worker thread and answer later |
| Library, toolbar, tables | `LibraryView` in `main.py`. Both interfaces are views over the same `games` dict and selection; keep install logic in one place |
| Settings | `SettingsTab` in `main.py`. The option checkboxes map to `PCSX2.ini` keys in `PCSX2_OPTIONS` |
| Patches & fixes | `patches_online.py` (finding/downloading), `enhancements.py` (what PCSX2 needs to switch them on, recommendations, checks) |
| Disc serial / CRC | `disc.py`. PCSX2 names cheat, patch and per-game settings files after the CRC of the disc's boot ELF; always prefer the disc over a database |
| Cheat sources | `cheat_online.py`, `_GITHUB_PNACH_COLLECTIONS` in `main.py` |
| Texture sources | `texture_sources.json` (see below), `textures_install.py` |
| App icon | Edit `app_icon.svg` / `logo.svg`, then run `python tools/build_icons.py` (needs `pillow`) |

## Data files

- `ps2_cheats_database_merged.json` is the bundled cheat database.
- `texture_sources.json` maps a serial to hand-verified texture packs. Each entry uses exactly one of `github_repo` (a release asset), `archive_org_id`, `direct_url` or `manual_url` (opened in the browser, never fetched). Before adding an entry, check that the link works and the pack really is for that game. Do not add sources that need bot-detection bypasses or violate a site's robots.txt or terms.
- `ulist2.html`, `plist2.html`, `jlist2.html` are saved PSXDataCenter pages used for offline title and CRC lookup.

## Conventions

- Match the code around you. `main.py` uses CRLF line endings; keep them (check your editor's setting before saving).
- No emoji in the UI. Classic stays native-looking (Fusion, light QSS). In Big Picture, motion and sound must give feedback (focus moved, screen changed, something finished): keep animations short (about 140-420 ms), animate only `transform` and `opacity` so they stay at 60 fps, and make sure they respect Settings > Interface > Reduce motion and the sound toggle.
- Keyboard prompts show keyboard keys only and controller prompts show controller buttons only; never mix them.
- The `.pcsx2_manager_*` folders in the home directory and the `PCSX2-Manager` legacy settings name are deliberately unchanged so existing users keep their data. Do not rename them without a migration.
- Be careful with anything that writes into a user's PCSX2 folders. Never overwrite without keeping a backup, and test with a throwaway data folder.

## Pull requests

- One logical change per PR, with a description of what changed and why.
- Add or update a test for behavior changes and bug fixes.
- For UI changes, attach a screenshot (use sample data, not your own library or cover art).
- Update `README.md` when behavior, settings or the workflow change.

## Reporting bugs

Include your Windows and Python versions, whether you are in Classic or Big Picture, what you expected, and what happened. For enhancement problems, the "What was searched" box in a game's Enhancements is the most useful thing to paste.
