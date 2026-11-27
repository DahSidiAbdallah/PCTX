"""Test-suite-only environment setup. Must run before anything imports
main.py, which is why it lives in conftest.py (loaded ahead of collection).

1. Settings isolation. The app stores its settings in the Windows registry;
   PCTX_SETTINGS_FILE redirects them to a throwaway INI file so no test can
   ever read or change a real user's settings.

2. QtWebEngine flags. Importing main.py imports bigpicture.py ->
   QtWebEngineWidgets for every test, even ones that never touch Big Picture.
   Chromium's sandbox/GPU subprocess crashes intermittently inside restricted
   containers / CI runners (a well-known QtWebEngine issue, not an app bug), so
   the sandbox and GPU are switched off here. These flags are deliberately NOT
   set in the shipped app: disabling Chromium's sandbox is a real security
   trade-off that only makes sense in a disposable test run.
"""
import atexit
import os
import shutil
import tempfile

_settings_dir = tempfile.mkdtemp(prefix="pctx-test-settings-")
atexit.register(shutil.rmtree, _settings_dir, ignore_errors=True)
os.environ["PCTX_SETTINGS_FILE"] = os.path.join(_settings_dir, "settings.ini")
# Pre-answer the first-run welcome dialog: it is modal, so a fresh settings
# file would otherwise block any test that constructs the main window.
with open(os.environ["PCTX_SETTINGS_FILE"], "w", encoding="utf-8") as _f:
    _f.write("[General]\nwelcome_shown=true\nmigrated_from_legacy=true\n")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")
os.environ.setdefault(
    "QTWEBENGINE_CHROMIUM_FLAGS",
    "--disable-gpu --no-sandbox --disable-software-rasterizer",
)


import pytest  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _qt_application():
    """Keep one QApplication alive for the whole session. Several tests create
    theirs as a local variable, so it could be garbage-collected while a later
    test still starts QThreads -- which crashes the interpreter."""
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app
