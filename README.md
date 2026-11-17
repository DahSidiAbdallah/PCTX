<p align="center">
  <img src="logo.png" alt="PCSX2 Patch & Texture Manager logo" width="240" />
</p>

# PCSX2 Patch & Texture Manager

This project is a desktop utility for organizing and installing the parts of a PS2 game setup that usually take the most time to manage: cheat patches and texture packs.

Instead of hunting through folders, serials, and patch files manually, the app lets you scan a game library, match each title to the correct region and serial, and install the relevant content in one pass.

It is built with PySide6 and is designed to fit into a normal PCSX2 workflow without making the process feel like a chore.

## What it does

- Scans a folder of PS2 disc images such as `.iso`, `.bin`, `.chd`, and `.cso`
- Detects game serials and titles from filenames where possible
- Lets you add games manually when matching is incomplete
- Installs matching cheat patches from a bundled local database with online fallback support
- Installs texture packs from a curated source list and matches them to the current title/region
- Includes extra tools for PNACH editing and texture-install workflows for advanced users
- Keeps settings such as PCSX2 paths and launch preferences in the app itself

## Core features

### Library-based management

Point the app at your PS2 games folder and it will build a library view of the titles it can recognize. From there, you can select a game and sync its patch and texture data instead of managing individual files by hand.

### Cheat handling

The app uses a local cheat database as its main source and falls back to online sources when needed. The idea is to keep common setups working offline while still allowing online lookups when the database is missing a title.

### Texture pack support

Texture installs are handled through a curated manifest and the app tries to resolve the correct pack for the selected game using region/serial information. This keeps the install flow focused and avoids the usual mismatch problems that come with manual pack management.

### Advanced tools

There are also extra tools for users who want more control:

- raw and PNACH conversion helpers
- cheat parsing and preview support
- direct texture pack staging and installation tools
- PCSX2-related settings and quick-launch options

## Project layout

A few of the main files and folders are worth knowing about:

- `main.py` — main Qt application entry point and most of the app logic
- `cheat_online.py` — online cheat sources and fallback logic
- `textures_install.py` — texture installation flow
- `texture_sources.json` — curated texture-pack source mapping
- `ps2_cheats_database_merged.json` — local cheat database
- `tests/` — project test suite
- `CONTRIBUTING.md` — contributor guidance and local workflow

## Requirements

- Python 3.10 or newer
- PySide6
- beautifulsoup4
- requests (recommended for online cheat/metadata lookups)

The repository also includes a minimal [requirements.txt](requirements.txt) to make setup easier.

## Getting started

### 1. Create a virtual environment

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

### 2. Install dependencies

```powershell
pip install -r requirements.txt
```

### 3. Run the application

```powershell
python main.py
```

The app expects to be launched from the project root. If you have a custom logo asset in the repository, it will be used as the app icon.

## Typical workflow

1. Open the app and point it at your PS2 game folder.
2. Let the library scan detect the games it can recognize.
3. Pick a title from the library.
4. Use the sync flow to install the matching cheat patch and texture pack.
5. If a title is not found automatically, add it manually by serial and continue.

This keeps the app focused on the common use case: getting a game ready with the right patching and textures without a lot of manual setup.

## Troubleshooting

- If the app fails to start, confirm that PySide6 is installed in the active environment.
- If online lookups fail, the app should still run for local/offline use.
- If PCSX2 is installed in a non-standard location, set the path manually in the app settings.
- If a title is not found automatically, try adding it manually using its serial.

## Contributing

Contributions are welcome, especially for bug fixes, UI polish, compatibility issues, and better matching logic.

Please read [CONTRIBUTING.md](CONTRIBUTING.md) for local setup, testing, and pull request guidance.

## License

This project is licensed under the [MIT License](LICENSE).
