# PyInstaller spec for PCTX: a one-folder Windows build (PCTX.exe + _internal/).
# Built by packaging/build_release.py; see that file for the full release flow.
import os
import sys

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))

datas = [
    (os.path.join(ROOT, "web"), "web"),
    (os.path.join(ROOT, "app_icon.ico"), "."),
    (os.path.join(ROOT, "app_icon.png"), "."),
    (os.path.join(ROOT, "logo.png"), "."),
    (os.path.join(ROOT, "texture_sources.json"), "."),
    (os.path.join(ROOT, "ps2_cheats_database.json"), "."),
    (os.path.join(ROOT, "ps2_cheats_database_merged.json"), "."),
    (os.path.join(ROOT, "ulist2.html"), "."),
    (os.path.join(ROOT, "plist2.html"), "."),
    (os.path.join(ROOT, "jlist2.html"), "."),
    (os.path.join(ROOT, "cheat_cache", ".gitkeep"), "cheat_cache"),
    (os.path.join(ROOT, "LICENSE"), "."),
]

# Qt modules PCTX never uses; leaving them out keeps the download smaller.
excludes = [
    "tkinter", "pytest", "PIL",
    "PySide6.Qt3DAnimation", "PySide6.Qt3DCore", "PySide6.Qt3DExtras", "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic", "PySide6.Qt3DRender", "PySide6.QtCharts", "PySide6.QtDataVisualization",
    "PySide6.QtGraphs", "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets", "PySide6.QtBluetooth",
    "PySide6.QtNfc", "PySide6.QtSensors", "PySide6.QtSerialPort", "PySide6.QtSerialBus",
    "PySide6.QtLocation", "PySide6.QtPositioning", "PySide6.QtRemoteObjects", "PySide6.QtScxml",
    "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtTextToSpeech", "PySide6.QtHttpServer",
    "PySide6.QtDesigner", "PySide6.QtHelp", "PySide6.QtUiTools", "PySide6.QtPdf", "PySide6.QtPdfWidgets",
    "PySide6.QtSpatialAudio", "PySide6.QtStateMachine", "PySide6.QtVirtualKeyboard",
]

version_file = os.path.join(SPECPATH, "build", "version_info.txt")

a = Analysis(
    [os.path.join(ROOT, "main.py")],
    pathex=[ROOT],
    datas=datas,
    hiddenimports=["yaml", "py7zr", "bs4", "qtawesome"],
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PCTX",
    icon=os.path.join(ROOT, "app_icon.ico"),
    console=False,
    version=version_file if os.path.isfile(version_file) else None,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="PCTX", upx=False)
