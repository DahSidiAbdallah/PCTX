"""Build the Windows release downloads for PCTX.

    python packaging/build_release.py

Produces, in packaging/build/out/:
  * PCTX-<version>-Windows-Portable.zip  -- unzip anywhere and run PCTX.exe.
    Contains portable.txt, so settings stay in that folder (PCTX.ini).
  * PCTX-<version>-Setup.exe             -- per-user installer (no admin prompt),
    Start-menu/desktop shortcuts and an uninstaller. Needs Inno Setup 6.

Requirements: `pip install -r requirements.txt pyinstaller`, and Inno Setup 6
(https://jrsoftware.org/isinfo.php) for the installer; without it only the
portable zip is built.
"""
import os
import shutil
import subprocess
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BUILD = os.path.join(HERE, "build")
DIST = os.path.join(BUILD, "dist")
OUT = os.path.join(BUILD, "out")
sys.path.insert(0, ROOT)


def version() -> str:
    import re
    with open(os.path.join(ROOT, "main.py"), encoding="utf-8") as f:
        return re.search(r'^APP_VERSION = "([^"]+)"', f.read(), re.M).group(1)


def write_version_info(v: str):
    nums = (v.split(".") + ["0", "0", "0"])[:3] + ["0"]
    t = ", ".join(nums)
    text = f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers=({t}), prodvers=({t}), mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'Dah Sidi Abdallah'),
      StringStruct('FileDescription', 'PCTX - cheats, patches and texture packs for PCSX2'),
      StringStruct('FileVersion', '{v}'),
      StringStruct('InternalName', 'PCTX'),
      StringStruct('LegalCopyright', 'MIT License'),
      StringStruct('OriginalFilename', 'PCTX.exe'),
      StringStruct('ProductName', 'PCTX'),
      StringStruct('ProductVersion', '{v}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""
    os.makedirs(BUILD, exist_ok=True)
    with open(os.path.join(BUILD, "version_info.txt"), "w", encoding="utf-8") as f:
        f.write(text)


def find_iscc():
    for p in (shutil.which("iscc"), r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
              r"C:\Program Files\Inno Setup 6\ISCC.exe"):
        if p and os.path.isfile(p):
            return p
    return None


def main():
    v = version()
    print(f"Building PCTX {v}")
    shutil.rmtree(DIST, ignore_errors=True)
    os.makedirs(OUT, exist_ok=True)
    write_version_info(v)
    subprocess.check_call([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
                           "--distpath", DIST, "--workpath", os.path.join(BUILD, "work"),
                           os.path.join(HERE, "pctx.spec")])
    app = os.path.join(DIST, "PCTX")
    # Build-time leftovers that must not ship (e.g. a developer's cheat cache).
    cache = os.path.join(app, "_internal", "cheat_cache")
    for name in os.listdir(cache) if os.path.isdir(cache) else []:
        if name != ".gitkeep":
            os.remove(os.path.join(cache, name))

    # ---- installer (installed copies keep settings in the registry, like a normal app)
    iscc = find_iscc()
    if iscc:
        subprocess.check_call([iscc, f"/DAppVersion={v}", f"/DSourceDir={app}", "/Q",
                               os.path.join(HERE, "installer.iss")])
    else:
        print("Inno Setup not found: skipping the installer.")

    # ---- portable zip (portable.txt => settings stay next to PCTX.exe)
    with open(os.path.join(app, "portable.txt"), "w", encoding="utf-8") as f:
        f.write("PCTX keeps its settings in PCTX.ini in this folder while this file exists.\n"
                "Delete it to use the normal per-user settings instead.\n")
    zpath = os.path.join(OUT, f"PCTX-{v}-Windows-Portable.zip")
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for root, _dirs, files in os.walk(app):
            for name in files:
                full = os.path.join(root, name)
                z.write(full, os.path.join("PCTX", os.path.relpath(full, app)))
    os.remove(os.path.join(app, "portable.txt"))

    for name in sorted(os.listdir(OUT)):
        p = os.path.join(OUT, name)
        print(f"  {name}  {os.path.getsize(p) / 1e6:.0f} MB")


if __name__ == "__main__":
    main()
