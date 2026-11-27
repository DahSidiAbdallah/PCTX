<p align="center">
  <img src="logo.png" alt="PCTX" width="440">
</p>

PCTX manages cheats, patches and HD texture packs for your PCSX2 game library. Pick a game, choose what you want, and PCTX installs it and switches it on in PCSX2 for you.

It's a Windows app. It doesn't include games, BIOS files or emulator code, and it isn't affiliated with the PCSX2 project.

## What it does

- **Builds your library** from a folder of PS2 disc images (`.iso`, `.bin`, `.cso`, `.chd`), with cover art.
- **Enhancements for each game:** widescreen, no-interlacing and bug-fix patches, cheats, and texture packs, all on one screen. **Use recommended** picks the safe ones for you.
- **Matches your exact disc** by reading its CRC, so patches and cheats are for your version of the game.
- **Turns things on in PCSX2** (per-game patches and cheats, texture loading), so what you install actually works.
- **Two interfaces:** *Classic*, a desktop window like PCSX2's game list, and *Big Picture*, a fullscreen mode for controllers. Switch with **F11**.
- **Launches games** in PCSX2 straight from the library.

## Download

Get it from the [Releases](https://github.com/DahSidiAbdallah/PCSX2-Patch_Texture-Manager/releases) page. You need Windows 10 or 11 (64-bit) and PCSX2.

- **Installer** (`PCTX-<version>-Setup.exe`): installs for your user, no admin rights needed.
- **Portable** (`PCTX-<version>-Windows-Portable.zip`): unzip anywhere and run `PCTX.exe`. Settings stay in that folder.

If Windows SmartScreen warns you the first time, choose **More info > Run anyway**.

## Getting started

1. Open **File > Settings** and set PCSX2's data folder (**Detect** usually finds it).
2. Use **File > Scan Folder** to add your games.
3. Select a game, open **Enhancements**, press **Use recommended** (or pick what you want), then **Apply**.

## Run from source

Needs Python 3.10 or newer.

```powershell
git clone https://github.com/DahSidiAbdallah/PCSX2-Patch_Texture-Manager.git
cd PCSX2-Patch_Texture-Manager
pip install -r requirements.txt
python main.py
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE)
