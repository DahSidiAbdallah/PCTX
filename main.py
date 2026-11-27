#!/usr/bin/env python3

import os
import shutil
import zipfile
import subprocess
import tempfile

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QFileDialog, QVBoxLayout,
    QHBoxLayout, QFormLayout, QLineEdit, QTextEdit, QPushButton, QLabel, QHeaderView,
    QMessageBox, QListWidget, QProgressBar, QGroupBox, QComboBox, QCheckBox,
    QDialog, QListWidgetItem, QAbstractItemView, QRadioButton,
    QTreeWidget, QTreeWidgetItem, QMenu, QInputDialog, QScrollArea,
)
from PySide6.QtWidgets import *
import html
import re
import threading
import json
from dataclasses import dataclass
from typing import Optional, List, Tuple, Dict, Any
from collections import deque
import time
import logging

from PySide6.QtCore import Qt, QThread, Signal, QSize, QSettings, QTimer, QCoreApplication, QPoint, QPointF, QRectF, QEventLoop, QUrl
import sys
from PySide6.QtGui import (QIcon, QPixmap, QDragEnterEvent, QDropEvent, QAction, QActionGroup, QPainter,
                           QColor, QPen, QDesktopServices, QKeySequence, QShortcut)
import concurrent.futures

# Application icon: multi-resolution .ico with hand-tuned small sizes (see
# tools/build_icons.py), used for the window, taskbar and title bar.
_ASSETS_DIR = os.path.dirname(os.path.abspath(__file__))
APP_ICON_ICO_PATH = os.path.join(_ASSETS_DIR, "app_icon.ico")

# ---------------------------- Windows native window-chrome support ----------------------------
# Qt's Qt.FramelessWindowHint strips the window styles Windows' own Snap feature
# (drag-to-top-to-maximize, drag-to-edge-to-half-screen) checks for, so a purely
# Qt-frameless window never gets Snap regardless of window-flag combinations. The
# documented fix (used by e.g. Windows Terminal/VS Code) is the opposite approach:
# keep the window fully native (no FramelessWindowHint at all, so WS_CAPTION/
# WS_THICKFRAME/Snap/shadow/rounded-corners all keep working normally), then use
# WM_NCCALCSIZE to claim the whole window rect as client area (so nothing is
# visually drawn in the space a native title bar would occupy) and WM_NCHITTEST to
# redirect clicks on our custom TitleBar to behave like the native caption. Only
# engaged on win32; other platforms keep the simpler FramelessWindowHint approach.
IS_WINDOWS = sys.platform == 'win32'
if IS_WINDOWS:
    import ctypes

    class _RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    class _NCCALCSIZE_PARAMS(ctypes.Structure):
        _fields_ = [("rgrc", _RECT * 3), ("lppos", ctypes.c_void_p)]

    class _MSG(ctypes.Structure):
        _fields_ = [("hwnd", ctypes.c_void_p), ("message", ctypes.c_uint),
                    ("wParam", ctypes.c_size_t), ("lParam", ctypes.c_ssize_t),
                    ("time", ctypes.c_ulong), ("pt_x", ctypes.c_long), ("pt_y", ctypes.c_long)]

    WM_NCCALCSIZE = 0x0083
    WM_NCHITTEST = 0x0084
    SM_CXFRAME = 32
    SM_CXPADDEDBORDER = 92
    HT_CLIENT = 1
    HT_CAPTION = 2
    HT_LEFT = 10
    HT_RIGHT = 11
    HT_TOP = 12
    HT_TOPLEFT = 13
    HT_TOPRIGHT = 14
    HT_BOTTOM = 15
    HT_BOTTOMLEFT = 16
    HT_BOTTOMRIGHT = 17


def set_windows_app_user_model_id():
    """Windows groups taskbar buttons/icons by "Application User Model ID";
    a plain `python main.py` process has none set, so Explorer falls back to
    showing python.exe's own icon in the taskbar instead of setWindowIcon()'s
    icon. Giving the process its own explicit AppUserModelID (must be called
    before the QApplication/first window is created) fixes that -- same
    well-known workaround used by most Python/Electron apps launched as a
    script rather than a compiled executable."""
    if not IS_WINDOWS:
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "PCTX.PatchTextureManager.2"
        )
    except Exception:
        pass

# Registry to keep running QThread-based workers alive if callers don't hold a reference
_ACTIVE_WORKERS: list = []


def release_worker_when_done(worker: QThread, on_done) -> None:
    """Call `on_done` on the main thread once `worker` has really finished.

    Several worker classes shadow QThread.finished with their own result signal,
    emitted from inside run() while the thread is still alive. Cleaning up on that
    one (dropping the last reference / deleteLater) destroys the QThread under a
    running thread, which aborts the whole process. This hooks QThread's own
    end-of-thread signal instead and queues the callback onto the main thread.
    """
    def _done():
        worker.wait(2000)
        on_done()
    super(type(worker), worker).finished.connect(_done, Qt.QueuedConnection)

# Ensure any remaining QThread workers are asked to quit and waited on at process exit to
# reduce "QThread: Destroyed while thread '' is still running" warnings and possible
# platform-specific crashes during interpreter shutdown.
import atexit

def _wait_active_workers(timeout_ms: int = 2000):
    for w in list(_ACTIVE_WORKERS):
        try:
            if isinstance(w, QThread):
                try:
                    w.quit()
                except Exception:
                    pass
                try:
                    w.wait(timeout_ms)
                except Exception:
                    pass
        except Exception:
            pass

atexit.register(_wait_active_workers)

# Optional for online features elsewhere; not required for offline resolver
try:
    import requests  # optional for certain lookups
except Exception:
    requests = None


# Online cheat database integration
import cheat_online
from cheat_online import fetch_and_cache_cheats
import theme
import icons
import texture_upscale
import bigpicture
import disc
import enhancements
import texture_catalog
import patches_online
# Imported here, on the main thread, on purpose: textures_install pulls in py7zr, whose
# `import multiprocessing` can deadlock when it is first executed on a worker thread
# (PackInstallWorker used to do exactly that, hanging the install forever).
import textures_install  # noqa: F401
from bs4 import BeautifulSoup

# ---------------------------- Helpers / Model ----------------------------

# module logger for debug messages
logger = logging.getLogger("pcsx2_manager")
if not logger.handlers:
    # default to WARNING to reduce noisy logs; callers can enable INFO/DEBUG when needed
    logging.basicConfig(level=logging.WARNING)

# ---------------------------- App identity / settings ----------------------------
APP_NAME = "PCTX"
APP_VERSION = "2.0.0"
USER_AGENT = "PCTX/2.0"
# The app was called "PCSX2 Manager" before the PCTX rename, and its settings
# (PCSX2 paths, profiles, welcome flag, ...) lived under that name. Those are
# copied across once so renaming doesn't silently reset anyone's setup. The
# on-disk data files in the home folder (.pcsx2_manager_*) keep their names for
# the same reason -- renaming them would orphan existing libraries and caches.
_LEGACY_SETTINGS = ("PCSX2-Manager", "PatchTextureManager")


def app_settings() -> QSettings:
    """The app's persistent settings. PCTX_SETTINGS_FILE redirects them to an INI
    file instead of the Windows registry -- used by the tests and screenshot
    scripts so they can never touch a real user's settings."""
    override = os.environ.get("PCTX_SETTINGS_FILE")
    if override:
        return QSettings(override, QSettings.IniFormat)
    return QSettings(APP_NAME, APP_NAME)


def migrate_legacy_settings(old: QSettings, new: QSettings) -> int:
    """One-time copy of every key from the pre-rename settings store into the
    PCTX one. Never overwrites a key PCTX already has. Returns how many keys
    were copied (0 if it already ran, or there was nothing to copy)."""
    if new.value("migrated_from_legacy", False, bool):
        return 0
    copied = 0
    for key in old.allKeys():
        if not new.contains(key):
            new.setValue(key, old.value(key))
            copied += 1
    new.setValue("migrated_from_legacy", True)
    new.sync()
    return copied


def fmt_speed(bps: Optional[float]) -> str:
    if bps is None:
        return '--'
    try:
        if bps <= 0:
            return '0 B/s'
        if bps < 1024:
            return f"{bps:.0f} B/s"
        if bps < 1024 * 1024:
            return f"{bps/1024:.2f} KiB/s"
        if bps < 1024 * 1024 * 1024:
            return f"{bps/(1024*1024):.2f} MiB/s"
        return f"{bps/(1024*1024*1024):.2f} GiB/s"
    except Exception:
        return '--'


def fmt_eta(sec: Optional[float]) -> str:
    if sec is None or sec == float('inf'):
        return '--:--'
    try:
        s = int(max(0, int(sec)))
        if s >= 3600:
            hh = s // 3600
            mm = (s % 3600) // 60
            ss = s % 60
            return f"{hh:02d}:{mm:02d}:{ss:02d}"
        mm = s // 60
        ss = s % 60
        return f"{mm:02d}:{ss:02d}"
    except Exception:
        return '--:--'

HEX8 = re.compile(r"^[0-9A-Fa-f]{8}$")
SERIAL_RE = re.compile(
    r"\b(SCUS|SLUS|SLES|SCES|SLPS|SLPM|SCPS|SCAJ|SLKA|ULUS|UCUS|PBPX|PAPX|TCUS|TCES)[-_ ]?\d{3,6}\b",
    re.IGNORECASE
)
PNACH_PATCH_LINE = re.compile(
    r"^\s*patch\s*=\s*\d+\s*,\s*EE\s*,\s*([0-9A-Fa-f]{8})\s*,\s*(extended|word|short|byte)\s*,\s*([0-9A-Fa-f]{1,8})",
    re.IGNORECASE
)
TITLE_LINE = re.compile(r"^\s*gametitle\s*=\s*(.+)$", re.IGNORECASE)
CRC_IN_TEXT = re.compile(r"\bCRC\s*[:=]\s*(?:0x)?([0-9A-Fa-f]{8})\b")
INI_BOOL = re.compile(r"^(true|false|enabled|disabled|1|0)$", re.I)


@dataclass
class PnachData:
    crc: Optional[str] = None            # 8-hex uppercase
    serials: List[str] = None            # list of serial strings
    title: Optional[str] = None
    raw_pairs: List[Tuple[str, str]] = None  # [(addr, value)] both 8-hex uppercase
    comments: List[str] = None
    # items preserves original file ordering: a list of either comment strings or (addr,value) tuples
    items: List[Any] = None

    def __post_init__(self):
        if self.serials is None:
            self.serials = []
        if self.raw_pairs is None:
            self.raw_pairs = []
        if self.comments is None:
            self.comments = []
        if self.items is None:
            self.items = []


@dataclass
class GameEntry:
    """A single detected/selected game, shared across tabs via MainWindow.current_game_changed."""
    serial: str = ""
    title: str = ""
    crc: Optional[str] = None
    source_path: Optional[str] = None
    # Where `crc` came from: 'disc' (read from the image -- exact), or a
    # database name. Anything but 'disc' may belong to another revision.
    crc_source: Optional[str] = None


@dataclass
class AppState:
    """Shared state published by MainWindow so tabs don't have to reach into each
    other directly for the current PCSX2 paths / selected game."""
    pcsx2_paths: Dict[str, str] = None
    current_game: Optional[GameEntry] = None

    def __post_init__(self):
        if self.pcsx2_paths is None:
            self.pcsx2_paths = {}


# Guess common PCSX2 user dir locations

def default_pcsx2_user_dirs(pcsx2_exe: str = "") -> List[str]:
    """Guess PCSX2's user data folder. If `pcsx2_exe` is given and PCSX2's
    portable-mode marker (a "portable.ini" file next to the executable) is
    present, that folder is the *actual* answer and takes priority -- a
    portable install keeps all its data (including the cheats folder) next to
    the exe, not in Documents, so guessing Documents\\PCSX2 for a portable
    setup would silently point at a folder PCSX2 never reads."""
    candidates: List[str] = []
    if pcsx2_exe:
        exe_dir = os.path.dirname(os.path.abspath(pcsx2_exe))
        if os.path.isfile(os.path.join(exe_dir, "portable.ini")):
            candidates.append(exe_dir)
    home = os.path.expanduser("~")
    candidates += [
        os.path.join(home, "Documents", "PCSX2"),                 # Windows
        os.path.join(home, ".config", "PCSX2"),                   # Linux
        os.path.join(home, "Library", "Application Support", "PCSX2"),  # macOS
        os.path.abspath(os.path.join(os.getcwd(), "PCSX2")),       # portable (this app's own cwd)
    ]
    return [p for p in candidates if os.path.isdir(p)]


def ensure_subdirs(base: str) -> dict:
    """PCSX2's folders under its data folder. Only the ones this app installs
    into are created; logs/ and inis/ belong to PCSX2 and are just reported."""
    paths = {
        "cheats": os.path.join(base, "cheats"),
        "patches": os.path.join(base, "patches"),
        "textures": os.path.join(base, "textures"),
        "logs": os.path.join(base, "logs"),
        "inis": os.path.join(base, "inis"),
    }
    if os.path.isdir(base):
        for key in ("cheats", "patches", "textures"):
            try:
                os.makedirs(paths[key], exist_ok=True)
            except OSError:
                pass
    return paths


def normalize_crc(crc: str) -> Optional[str]:
    if not crc:
        return None
    crc = crc.strip().upper()
    return crc if HEX8.match(crc) else None


# Worker to download cover images without blocking UI
class CoverFetchWorker(QThread):
    fetched = Signal(str)  # path to cached image
    fetch_failed = Signal()

    def __init__(self, url_or_urls, cache_path: str, parent=None):
        """Accept either a single URL string or an iterable/list of candidate URLs to try in order.
        cache_path is the path where the successful content will be written.
        """
        super().__init__(parent)
        # Normalize to list of urls
        if isinstance(url_or_urls, (list, tuple)):
            self.urls = list(url_or_urls)
        else:
            self.urls = [url_or_urls]
        self.cache_path = cache_path

    def run(self):
        # If the optional requests dependency is missing, fall back to urllib so
        # cover fetching still works. This mirrors the basic GET logic below but
        # without the HEAD optimization.
        if requests is None:
            try:
                from urllib.request import urlopen
            except Exception:
                urlopen = None
            if urlopen is None:
                logger.warning("[CoverFetchWorker] Neither requests nor urllib available; cannot fetch covers")
                self.fetch_failed.emit()
                return
            
            attempted = []
            for candidate in self.urls:
                if not candidate:
                    continue
                attempted.append(candidate)
                try:
                    logger.debug(f"[CoverFetchWorker] Trying: {candidate}")
                    with urlopen(candidate, timeout=12) as resp:
                        status = getattr(resp, 'status', 200)
                        content = resp.read() or b''
                    if status == 200 and content:
                        try:
                            os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
                            with open(self.cache_path, 'wb') as fh:
                                fh.write(content)
                            # Record successful URL in index
                            try:
                                idx_dir = os.path.dirname(self.cache_path)
                                idx_file = os.path.join(idx_dir, 'index.json')
                                key = os.path.splitext(os.path.basename(candidate))[0]
                                data = {}
                                if os.path.isfile(idx_file):
                                    with open(idx_file, 'r', encoding='utf-8') as inf:
                                        data = json.load(inf)
                                data[key] = candidate
                                with open(idx_file, 'w', encoding='utf-8') as outf:
                                    json.dump(data, outf)
                            except Exception as e:
                                logger.debug(f"[CoverFetchWorker] Could not update index: {e}")
                            
                            logger.info(f"[CoverFetchWorker] ✓ Cover found: {os.path.basename(candidate)}")
                            self.fetched.emit(self.cache_path)
                            return
                        except (IOError, OSError) as e:
                            logger.error(f"[CoverFetchWorker] Failed to save cover: {e}")
                            self.fetch_failed.emit()
                            return
                except Exception as e:
                    logger.debug(f"[CoverFetchWorker] Failed: {candidate} - {e}")
                    continue
            
            logger.info(f"[CoverFetchWorker] ✗ No cover found (tried {len(attempted)} URLs)")
            self.fetch_failed.emit()
            return

        # Try each candidate URL in order. Prefer GET directly because
        # GitHub raw endpoints often do not honour HEAD requests. Any
        # successful response will be cached.
        attempted = []
        for candidate in self.urls:
            if not candidate:
                continue
            attempted.append(candidate)
            try:
                logger.debug(f"[CoverFetchWorker] Trying: {candidate}")
                resp = requests.get(candidate, timeout=12)
                status = getattr(resp, 'status_code', None)
                content = getattr(resp, 'content', None) or b''
                clen = len(content)
                
                if status == 200 and content:
                    try:
                        os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
                        with open(self.cache_path, 'wb') as fh:
                            fh.write(content)
                        # Update index.json for caching
                        idx_dir = os.path.dirname(self.cache_path)
                        idx_file = os.path.join(idx_dir, 'index.json')
                        key = os.path.splitext(os.path.basename(candidate))[0]
                        data = {}
                        if os.path.isfile(idx_file):
                            with open(idx_file, 'r', encoding='utf-8') as inf:
                                data = json.load(inf)
                        data[key] = candidate
                        with open(idx_file, 'w', encoding='utf-8') as outf:
                            json.dump(data, outf)
                        
                        logger.info(f"[CoverFetchWorker] ✓ Cover found: {os.path.basename(candidate)} ({clen} bytes)")
                        self.fetched.emit(self.cache_path)
                        return
                    except (IOError, OSError) as e:
                        logger.error(f"[CoverFetchWorker] Failed to save cover: {e}")
                        self.fetch_failed.emit()
                        return
                else:
                    logger.debug(f"[CoverFetchWorker] Not found (status={status}): {candidate}")
            except requests.exceptions.RequestException as e:
                logger.debug(f"[CoverFetchWorker] Request error: {e}")
            except Exception as e:
                logger.debug(f"[CoverFetchWorker] Unexpected error: {e}")
                continue
        
        # All candidates failed
        logger.info(f"[CoverFetchWorker] ✗ No cover found (tried {len(attempted)} URLs)")
        self.fetch_failed.emit()


def norm_serial_key(s: str) -> str:
    return (s or "").upper().replace("-", "").replace("_", "").replace(" ", "")


_REGION_BY_PREFIX = {
    "SLUS": "NTSC-U", "SCUS": "NTSC-U", "SLUX": "NTSC-U",
    "SLES": "PAL", "SCES": "PAL", "SLED": "PAL",
    "SLPS": "NTSC-J", "SLPM": "NTSC-J", "SCPS": "NTSC-J", "SCAJ": "NTSC-J",
    "SLKA": "NTSC-K", "SCKA": "NTSC-K",
}


def region_for_serial(serial: str) -> str:
    """Human-readable region label (e.g. "NTSC-U") from a serial's prefix."""
    prefix = (serial or "").split("-")[0].strip().upper()
    return _REGION_BY_PREFIX.get(prefix, "")


_COVERS_REPO_BASE = "https://raw.githubusercontent.com/xlenore/ps2-covers/main/covers/default"


def build_cover_candidates(serial: str) -> List[str]:
    """Build candidate cover-art URLs to probe against the public xlenore/ps2-covers
    index (the same collection PCSX2's own built-in Cover Downloader points people
    at). Confirmed from the repo's own docs: filenames are the canonical dashed
    serial verbatim (e.g. "SLUS-20123.jpg"), so that exact form is tried first;
    a couple of normalized fallbacks follow in case of inconsistent entries."""
    raw = (serial or '').strip()
    sk = norm_serial_key(raw)
    variants = []
    if raw:
        variants.append(raw.upper())
    if sk:
        variants.append(sk.upper())
    variants.append(raw.replace('-', '').replace('_', '').replace(' ', '').lower())
    seen = set()
    uniq = []
    for v in variants:
        if not v or v in seen:
            continue
        seen.add(v)
        uniq.append(v)
    return [f"{_COVERS_REPO_BASE}/{v}.jpg" for v in uniq]


def scale_and_crop_pixmap(pm: QPixmap, w: int, h: int) -> QPixmap:
    """Scale a pixmap to fill a w x h box (may overshoot one dimension) and
    center-crop it down to exactly w x h -- used for both the detail-panel cover
    and the small list-row thumbnails so cover art never looks stretched."""
    scaled = pm.scaled(w, h, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
    if scaled.width() > w or scaled.height() > h:
        x = max(0, (scaled.width() - w) // 2)
        y = max(0, (scaled.height() - h) // 2)
        scaled = scaled.copy(x, y, w, h)
    return scaled


def cover_cache_path(serial: str, cache_dir: str) -> str:
    return os.path.join(cache_dir, f"cover_{norm_serial_key(serial)}.jpg")


def try_download_cover(serial: str, cache_dir: str) -> bool:
    """Best-effort synchronous cover download for one serial (used by the bulk
    prefetch worker, run inside a thread pool). Returns True if a file is cached
    afterwards, whether it was already there or freshly downloaded."""
    if requests is None:
        return False
    cache_path = cover_cache_path(serial, cache_dir)
    if os.path.isfile(cache_path):
        return True
    for url in build_cover_candidates(serial):
        try:
            resp = requests.get(url, timeout=10)
            if resp.status_code == 200 and resp.content:
                os.makedirs(cache_dir, exist_ok=True)
                with open(cache_path, 'wb') as f:
                    f.write(resp.content)
                return True
        except Exception:
            continue
    return False


class CoverPrefetchWorker(QThread):
    """Downloads covers for a whole batch of serials in the background (bounded
    parallelism via ThreadPoolExecutor) so the library list can show real cover
    thumbnails shortly after a scan, instead of only fetching one cover at a
    time as the user happens to click through games."""

    progressed = Signal(int, int)
    cover_ready = Signal(str)  # serial
    finished = Signal()

    def __init__(self, serials: List[str], cache_dir: str, parent=None):
        super().__init__(parent)
        self.serials = serials
        self.cache_dir = cache_dir

    def run(self):
        total = max(1, len(self.serials))
        completed = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
            futures = {ex.submit(try_download_cover, s, self.cache_dir): s for s in self.serials}
            for fut in concurrent.futures.as_completed(futures):
                serial = futures[fut]
                completed += 1
                try:
                    if fut.result():
                        self.cover_ready.emit(serial)
                except Exception:
                    pass
                self.progressed.emit(completed, total)
        self.finished.emit()


def create_cover_placeholder(serial: str = "", size: int = 420) -> QPixmap:
    """Create a placeholder pixmap for when no cover is available."""
    placeholder = QPixmap(size, size)
    placeholder.fill(Qt.lightGray)
    
    try:
        painter = QPainter(placeholder)
        if not painter.isActive():
            logger.warning("[Placeholder] QPainter not active")
            return placeholder
        
        painter.setPen(QColor(80, 80, 80))
        font = painter.font()
        font.setPointSize(11)
        font.setBold(True)
        painter.setFont(font)
        
        # Calculate text positioning
        y_offset = size // 3
        
        # Draw main message
        painter.drawText(20, y_offset, size - 40, 40, 
                        Qt.AlignCenter | Qt.TextWordWrap, 
                        "No cover available")
        
        # Draw serial if provided
        if serial:
            font.setPointSize(10)
            font.setBold(False)
            painter.setFont(font)
            painter.drawText(20, y_offset + 50, size - 40, 40,
                           Qt.AlignCenter | Qt.TextWordWrap,
                           f"for {serial}")
        
        # Draw hint
        font.setPointSize(9)
        font.setItalic(True)
        painter.setFont(font)
        painter.setPen(QColor(120, 120, 120))
        painter.drawText(20, size - 80, size - 40, 60,
                        Qt.AlignCenter | Qt.TextWordWrap,
                        "(Right-click to retry)")
        
        painter.end()
        logger.debug(f"[Placeholder] Created placeholder for {serial or 'unknown'}")
    except Exception as e:
        logger.error(f"[Placeholder] Failed to create placeholder: {e}")
    
    return placeholder


def create_library_cover_placeholder(serial: str = "", found: Optional[bool] = None) -> QPixmap:
    """Portrait, dark-themed placeholder cover for LibraryView's detail panel --
    distinct from create_cover_placeholder()'s square light-gray one (used by the
    older Cheats/Textures tab previews), which would look out of place against
    the dark bordered cover frame here.

    `found` distinguishes "haven't tried fetching yet" (None, generic) from
    "tried and there's genuinely no cover for this serial" (False, explicit) --
    the caller shows a separate loading overlay while a fetch is in flight, so
    this placeholder is never shown mid-fetch.
    """
    w, h = theme.COVER_WIDTH, theme.COVER_HEIGHT
    pm = QPixmap(w, h)
    pm.fill(QColor(theme.PLACEHOLDER['bg']))
    try:
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        pen = QPen(QColor(theme.PLACEHOLDER['line']))
        pen.setWidthF(1.5)
        p.setPen(pen)
        cx, cy = w / 2, h / 2 - 16
        p.drawEllipse(QPointF(cx, cy), 30, 30)
        p.drawEllipse(QPointF(cx, cy), 9, 9)
        p.setPen(QColor(theme.PLACEHOLDER['text']))
        font = p.font()
        font.setPointSize(9)
        p.setFont(font)
        label = "No cover art found" if found is False else (serial or "No cover art")
        p.drawText(QRectF(10, cy + 42, w - 20, 50), Qt.AlignHCenter | Qt.TextWordWrap, label)
        p.end()
    except Exception as e:
        logger.debug(f"[create_library_cover_placeholder] {e}")
    return pm


_TITLE_INDEX: Optional[Dict[str, str]] = None
_TITLE_INDEX_LOCK = threading.Lock()
_ROW_RE = re.compile(r"<tr\b(.*?)</tr>", re.S | re.I)
_TD_RE = re.compile(r"<td\b([^>]*)>(.*?)</td>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")


def _cell_text(raw: str) -> str:
    return " ".join(html.unescape(_TAG_RE.sub(" ", raw)).replace("\xa0", " ").split())


def _bundled_title_index() -> Dict[str, str]:
    """serial -> title from the bundled PSXDataCenter lists, built once.

    The lists are ~3 MB of HTML; parsing them with BeautifulSoup on every lookup
    used to cost several seconds per call (and ran on the UI thread at startup).
    A row scan with regexes builds the whole index in a fraction of a second."""
    global _TITLE_INDEX
    with _TITLE_INDEX_LOCK:
        if _TITLE_INDEX is not None:
            return _TITLE_INDEX
        index: Dict[str, str] = {}
        for name in ('ulist2.html', 'plist2.html', 'jlist2.html'):
            path = os.path.join(_ASSETS_DIR, name)
            try:
                with open(path, 'r', encoding='utf-8', errors='replace') as fh:
                    doc = fh.read()
            except OSError:
                continue
            for row in _ROW_RE.finditer(doc):
                cells = [(attrs, _cell_text(body)) for attrs, body in _TD_RE.findall(row.group(1))]
                serials = [tok for _a, t in cells for tok in t.upper().split() if SERIAL_RE.fullmatch(tok)]
                if not serials:
                    continue
                title = next((t for a, t in cells if re.search(r"col7|col3", a, re.I) and t), "")
                if not title:
                    rest = [(t, "") for _a, t in cells if t and not any(x in t.upper() for x in serials)]
                    if rest:
                        title = max(rest, key=lambda c: _score_title_candidate(*c))[0]
                if not title or title.upper() in ('INFO', 'TITLE', 'N/A', 'UNKNOWN'):
                    continue
                for ser in serials:
                    index.setdefault(ser, title)
        _TITLE_INDEX = index
        return index


def bundled_lookup_title(serial: str) -> Optional[str]:
    """Title for a serial from the bundled PSXDataCenter lists, or None."""
    if not serial:
        return None
    return _bundled_title_index().get(serial.upper().strip())


# ---- Shared local cheats database (loaded once, used by CheatsTab and LibraryView) ----
_CHEATS_DB_CACHE: Optional[dict] = None
_CHEATS_DB_INDEX: Optional[Dict[str, Tuple[dict, str]]] = None


def get_cheats_database() -> dict:
    """Load (and cache for the process lifetime) the built-in PS2 cheats database.
    Shared so the multi-MB JSON is parsed once, not once per widget that needs it."""
    global _CHEATS_DB_CACHE
    if _CHEATS_DB_CACHE is not None:
        return _CHEATS_DB_CACHE
    db_paths = [
        os.path.join(os.path.dirname(__file__), 'ps2_cheats_database_merged.json'),
        os.path.join(os.path.dirname(__file__), 'ps2_cheats_database.json'),
    ]
    for db_path in db_paths:
        try:
            if os.path.isfile(db_path):
                with open(db_path, 'r', encoding='utf-8') as f:
                    db = json.load(f)
                    games_count = len(db.get('games', []))
                    logger.info(f"[cheats_db] Loaded {games_count} games from {os.path.basename(db_path)}")
                    _CHEATS_DB_CACHE = db
                    return db
        except json.JSONDecodeError as e:
            logger.error(f"[cheats_db] Invalid JSON in {db_path}: {e}")
        except Exception as e:
            logger.error(f"[cheats_db] Failed to load {db_path}: {e}")
    logger.warning("[cheats_db] No cheats database found - online features only")
    _CHEATS_DB_CACHE = {"games": []}
    return _CHEATS_DB_CACHE


def get_cheats_index() -> Dict[str, Tuple[dict, str]]:
    """Return {normalized_serial: (game_dict, region_key)}, built once from get_cheats_database()."""
    global _CHEATS_DB_INDEX
    if _CHEATS_DB_INDEX is not None:
        return _CHEATS_DB_INDEX
    index: Dict[str, Tuple[dict, str]] = {}
    for game in get_cheats_database().get('games', []):
        for region_key, region in (game.get('regions') or {}).items():
            serial = region.get('serial')
            if serial:
                index[norm_serial_key(serial)] = (game, region_key)
    _CHEATS_DB_INDEX = index
    return index


def find_local_cheats(serial: str) -> Optional[Tuple[str, str, List[dict]]]:
    """Look up a serial in the local cheats database.
    Returns (title, crc, cheats) or None if the serial isn't in the local DB."""
    if not serial:
        return None
    entry = get_cheats_index().get(norm_serial_key(serial))
    if not entry:
        return None
    game, region_key = entry
    region = game['regions'][region_key]
    return game.get('title', 'Unknown Game'), region.get('crc', ''), region.get('cheats', [])


def write_cheats_pnach(title: str, serial: str, crc: str, cheats: List[dict], cheats_dir: str) -> str:
    """Build and write a .pnach file for the given cheats. Returns the written filepath.
    Raises on failure (missing crc/cheats_dir, I/O errors) -- callers handle user-facing messaging.
    Shared by CheatsTab's manual install button and LibraryView's one-click sync so there's a
    single pnach-writing implementation instead of two drifting copies.

    Each cheat gets its own "[Group Name]" bracket header (PCSX2's actual per-cheat
    labeling syntax -- see https://pcsx2.net/docs/advanced/writing-patches/) so it
    shows up as an individually named, toggleable entry in PCSX2's Cheats panel.
    A "// comment" line does NOT do this -- PCSX2 only reads it as documentation,
    so cheats written that way fall back to being lumped together as one block of
    unlabeled codes that just auto-activate with no per-cheat control.
    """
    if not crc:
        raise ValueError("Cannot write a .pnach file without a CRC value.")
    if not cheats_dir or not os.path.isdir(cheats_dir):
        raise ValueError("Invalid PCSX2 cheats folder path.")

    pnach_lines = [
        f"// {title}",
        f"// Serial: {serial}",
        f"// CRC: {crc}",
        f"// Generated by PCTX",
        f"// {len(cheats)} cheat(s)",
        "",
        "gametitle=" + title,
        "",
    ]
    for cheat in cheats:
        name = (cheat.get('name') or 'Cheat').replace('[', '(').replace(']', ')')
        pnach_lines.append(f"[{name}]")
        if cheat.get('description'):
            pnach_lines.append(f"description={cheat['description']}")
        for code in cheat.get('codes', []):
            pnach_lines.append(code)
        pnach_lines.append("")

    filepath = os.path.join(cheats_dir, f"{crc.upper()}.pnach")
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write("\n".join(pnach_lines))
    return filepath


# ---- Community GitHub-hosted .pnach collections (additional cheat source) ----
# CRC-keyed collections of real .pnach files (PCSX2's own format), fetched
# directly via GitHub's raw content CDN -- no scraping, no ToS concerns,
# just reading public files. Verified against the repo directly (see the
# commit introducing this) rather than assumed.
_GITHUB_PNACH_COLLECTIONS = [
    "xs1l3n7x/pcsx2_cheats_collection/main/cheats",
    # EU-region companion collection, same author/format/CRC-keyed layout as
    # the above -- verified live (200 on a raw .pnach fetch) before adding.
    "complicatiion/pcsx2_europe_cheats_collection/main/cheats",
]


def parse_pnach_cheats(content: str) -> List[dict]:
    """Parse a raw .pnach file's content into [{'name','description','codes'}, ...]
    using PCSX2's real [Group Name] bracket-header syntax (see write_cheats_pnach()
    for the write side of this same format). Patch lines appearing before any
    bracket header (old-format files) are grouped under one generic entry."""
    cheats: List[dict] = []
    current: Optional[dict] = None
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith('//'):
            continue
        m = re.match(r'^\[(.+)\]$', line)
        if m:
            if current and current['codes']:
                cheats.append(current)
            current = {'name': m.group(1).strip(), 'description': '', 'codes': []}
            continue
        low = line.lower()
        if low.startswith('gametitle=') or low.startswith('author='):
            continue
        if low.startswith('description='):
            if current is not None:
                current['description'] = line.split('=', 1)[1].strip()
            continue
        if low.startswith('patch='):
            if current is None:
                current = {'name': 'Unlabeled Codes', 'description': '', 'codes': []}
            current['codes'].append(line)
    if current and current['codes']:
        cheats.append(current)
    return cheats


def fetch_github_pnach_cheats(crc: str) -> List[dict]:
    """Try fetching a community-maintained .pnach for `crc` from known
    GitHub-hosted cheat collections and parse it into cheat dicts. Best-effort:
    returns [] on any failure, missing `requests`, or no match anywhere."""
    if requests is None or not crc:
        return []
    crc_upper = crc.upper()
    for path in _GITHUB_PNACH_COLLECTIONS:
        url = f"https://raw.githubusercontent.com/{path}/{crc_upper}.pnach"
        try:
            resp = requests.get(url, timeout=10, headers={'User-Agent': USER_AGENT})
            if resp.status_code == 200 and resp.text.strip():
                cheats = parse_pnach_cheats(resp.text)
                if cheats:
                    return cheats
        except Exception as e:
            logger.debug(f"[cheats] GitHub pnach fetch failed for {crc}: {e}")
    return []


# ---- Curated texture-pack manifest ----
# texture_sources.json maps a game serial to one or more known-good texture
# pack sources, hand-verified (real item, real file) rather than guessed.
# Each entry uses exactly one of these source-type fields, resolved by
# resolve_texture_source():
#   github_repo    -- a GitHub repo publishing the pack as a release asset
#                      (optionally narrowed by asset_pattern, the exact
#                      release-asset filename)
#   archive_org_id -- an archive.org item identifier (optionally narrowed by
#                      asset_pattern, the exact filename within that item)
#   direct_url     -- any host that serves the archive file straight off a
#                      plain HTTPS GET (no HTML/JS in the way)
#   manual_url     -- a page the app can't auto-download from (JS-gated
#                      hosts like mega.nz); opened in the browser instead of
#                      fetched, and the user installs it themselves
# There is no automatic/scraped discovery beyond the GitHub and Internet
# Archive searches in this module -- most games will honestly report "not
# found" until the manifest is extended. Users can edit the JSON file directly.
_TEXTURE_MANIFEST_CACHE: Optional[dict] = None


def _load_texture_manifest() -> dict:
    global _TEXTURE_MANIFEST_CACHE
    if _TEXTURE_MANIFEST_CACHE is not None:
        return _TEXTURE_MANIFEST_CACHE
    path = os.path.join(os.path.dirname(__file__), 'texture_sources.json')
    try:
        with open(path, 'r', encoding='utf-8') as f:
            _TEXTURE_MANIFEST_CACHE = json.load(f)
    except Exception as e:
        logger.warning(f"[textures] Failed to load texture_sources.json: {e}")
        _TEXTURE_MANIFEST_CACHE = {}
    return _TEXTURE_MANIFEST_CACHE


def get_texture_pack_options(serial: str) -> List[dict]:
    """Return the curated manifest entries for `serial` (may be empty, may have
    more than one -- some games have multiple community texture packs)."""
    if not serial:
        return []
    entries = _load_texture_manifest().get(serial.upper())
    if not entries:
        return []
    if isinstance(entries, dict):
        entries = [entries]
    return entries


def resolve_texture_release_asset(entry: dict) -> Optional[Tuple[str, str, str]]:
    """Resolve one manifest entry's current download URL for its GitHub release
    asset via GitHub's public releases API (no auth needed for public repos
    within rate limits). Returns (display_name, repo, download_url), or None if
    there's no matching release/asset or `requests` isn't available."""
    if requests is None or not entry:
        return None
    repo = entry.get('github_repo')
    asset_pattern = (entry.get('asset_pattern') or '').lower()
    if not repo:
        return None
    try:
        resp = requests.get(
            f'https://api.github.com/repos/{repo}/releases/latest',
            headers={'Accept': 'application/vnd.github+json', 'User-Agent': USER_AGENT},
            timeout=10,
        )
        if resp.status_code != 200:
            return None
        assets = resp.json().get('assets', [])
        for asset in assets:
            name = asset.get('name', '')
            if asset_pattern and name.lower() == asset_pattern:
                return entry.get('name', repo), repo, asset.get('browser_download_url')
        if not asset_pattern:
            # Unverified search candidate with no known filename -- best-effort:
            # take the first .zip release asset (perform_pack_installs only
            # handles .zip), not just whatever happens to be listed first.
            for asset in assets:
                name = asset.get('name', '')
                if name.lower().endswith('.zip'):
                    return entry.get('name', repo), repo, asset.get('browser_download_url')
    except Exception as e:
        logger.warning(f"[textures] Failed to resolve GitHub release for {repo}: {e}")
    return None


def resolve_texture_pack_url(serial: str) -> Optional[Tuple[str, str, str]]:
    """Resolve the first/default curated texture-pack option for `serial`.
    Convenience wrapper around get_texture_pack_options()/resolve_texture_release_asset()
    for callers that don't need to offer a choice between multiple packs."""
    options = get_texture_pack_options(serial)
    if not options:
        return None
    return resolve_texture_release_asset(options[0])


def search_github_texture_packs(serial: str, title: str) -> Tuple[List[dict], Optional[str]]:
    """Best-effort GitHub repository search for community PCSX2 texture packs
    that aren't in the curated manifest yet. Returns (candidates, error) --
    candidates are shaped like manifest entries (name/github_repo, no
    asset_pattern) plus 'verified': False and 'stars'/'url' for display; these
    are unverified hits and must always be shown to the user for manual
    confirmation, never auto-installed, unlike the hand-checked curated
    manifest. `error` is a human-readable note (e.g. rate-limited) when the
    search couldn't run, so the caller can tell "nothing found" apart from
    "couldn't check".

    GitHub's search endpoint has a much tighter unauthenticated rate limit
    (10 requests/minute) than the releases API used elsewhere in this module,
    so this only fires one query -- by title, since repos essentially never
    contain a game's literal serial in their name/description/README, so a
    serial-based query reliably finds nothing and would just burn budget.
    """
    if requests is None or not title:
        return [], None
    headers = {'Accept': 'application/vnd.github+json', 'User-Agent': USER_AGENT}
    try:
        resp = requests.get(
            'https://api.github.com/search/repositories',
            params={'q': f'{title} texture in:name,description,readme', 'per_page': 10},
            headers=headers, timeout=10,
        )
        if resp.status_code == 403:
            return [], "GitHub search is rate-limited right now (unauthenticated searches are capped at 10/minute) -- try again in a bit."
        if resp.status_code != 200:
            return [], f"GitHub search returned HTTP {resp.status_code}."
        candidates = []
        for item in resp.json().get('items', []):
            repo = item.get('full_name')
            if not repo:
                continue
            candidates.append({
                'name': item.get('name', repo),
                'github_repo': repo,
                'description': item.get('description') or '',
                'stars': item.get('stargazers_count', 0),
                'url': item.get('html_url'),
                'verified': False,
            })
        candidates.sort(key=lambda c: -c.get('stars', 0))
        return candidates[:10], None
    except Exception as e:
        logger.debug(f"[textures] GitHub search failed: {e}")
        return [], f"GitHub search failed: {e}"


# ---- Internet Archive (archive.org) texture-pack search ----
# archive.org's advancedsearch/metadata/download endpoints are fully public
# and its robots.txt only disallows /control/ and /report/ -- this is a
# straightforward, invited API call, the same trust level as the GitHub API
# calls above, not scraping. Several other real hosts were checked and
# deliberately NOT wired in the same way: GBAtemp and forums.pcsx2.net sit
# behind an active Cloudflare bot challenge (bypassing that is off-limits
# regardless of what's being fetched), and mega.nz's own robots.txt disallows
# general bot access to exactly the /file and /folder paths a downloader would
# need, so this app does not attempt to resolve or fetch mega.nz links itself.
_ARCHIVE_ORG_ASSET_EXTS = ('.zip', '.7z')


def search_archive_org_texture_packs(serial: str, title: str) -> Tuple[List[dict], Optional[str]]:
    """Best-effort Internet Archive item search for community PCSX2 texture
    packs. Same contract as search_github_texture_packs: candidates are
    unverified and must be confirmed by the user, never auto-installed."""
    if requests is None or not title:
        return [], None
    try:
        resp = requests.get(
            'https://archive.org/advancedsearch.php',
            params={
                # Title words anywhere, a PS2 mention, and an explicit texture
                # word; videos and music are never packs.
                'q': (f'("{title}") AND (pcsx2 OR ps2 OR "playstation 2") AND '
                      f'(texture OR textures OR retexture OR upscale OR upscaled) AND NOT mediatype:(movies OR audio)'),
                'fl[]': ['identifier', 'title', 'description'],
                'rows': 12, 'page': 1, 'output': 'json',
            },
            headers={'User-Agent': USER_AGENT}, timeout=10,
        )
        if resp.status_code != 200:
            return [], f"Internet Archive search returned HTTP {resp.status_code}."
        docs = resp.json().get('response', {}).get('docs', [])
        candidates = []
        for doc in docs:
            identifier = doc.get('identifier')
            if not identifier:
                continue
            desc = doc.get('description') or ''
            if isinstance(desc, list):
                desc = ' '.join(desc)
            candidates.append({
                'name': doc.get('title') or identifier,
                'archive_org_id': identifier,
                'description': desc[:200],
                'url': f'https://archive.org/details/{identifier}',
                'verified': False,
            })
        return candidates[:10], None
    except Exception as e:
        logger.debug(f"[textures] Internet Archive search failed: {e}")
        return [], f"Internet Archive search failed: {e}"


def resolve_archive_org_asset(identifier: str, asset_pattern: str = '') -> Optional[Tuple[str, str, str]]:
    """Resolve an Internet Archive item to a direct download URL for its
    packaged texture pack (.zip/.7z -- the two formats perform_pack_installs()
    can actually extract; a .rar-only item resolves to None rather than
    handing back a file nothing downstream can open). Same return shape as
    resolve_texture_release_asset(): (display_name, source_label, download_url)."""
    if requests is None or not identifier:
        return None
    try:
        resp = requests.get(
            f'https://archive.org/metadata/{identifier}',
            headers={'User-Agent': USER_AGENT}, timeout=10,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        files = data.get('files', [])
        pat = asset_pattern.lower()
        chosen = None
        for f in files:
            name = f.get('name', '')
            if pat and name.lower() == pat:
                chosen = name
                break
        if not chosen:
            for f in files:
                name = f.get('name', '')
                if name.lower().endswith(_ARCHIVE_ORG_ASSET_EXTS):
                    chosen = name
                    break
        if not chosen:
            return None
        display = (data.get('metadata') or {}).get('title') or identifier
        from urllib.parse import quote
        url = f'https://archive.org/download/{identifier}/{quote(chosen)}'
        return display, identifier, url
    except Exception as e:
        logger.warning(f"[textures] Failed to resolve Internet Archive item {identifier}: {e}")
        return None


def resolve_texture_source(entry: dict) -> Optional[Tuple[str, str, str]]:
    """Dispatch a texture-pack candidate (curated, GitHub search, or Internet
    Archive search) to the resolver matching its source type. Returns
    (display_name, source_label, download_url), or None if it can't be
    resolved to something installable."""
    if not entry:
        return None
    if entry.get('archive_org_id'):
        return resolve_archive_org_asset(entry['archive_org_id'], entry.get('asset_pattern', ''))
    if entry.get('direct_url'):
        return entry.get('name') or 'Direct Download', 'direct', entry['direct_url']
    if entry.get('github_repo'):
        return resolve_texture_release_asset(entry)
    return None


def save_texture_source(serial: str, entry: dict) -> bool:
    """Persist a confirmed-working texture-pack entry into texture_sources.json
    so it's a verified curated option next time instead of needing to be
    rediscovered via search. `entry` should already be shaped for exactly one
    source type (github_repo, archive_org_id, or direct_url -- see
    _offer_save_texture_source()). Returns True on success."""
    global _TEXTURE_MANIFEST_CACHE
    path = os.path.join(os.path.dirname(__file__), 'texture_sources.json')
    try:
        manifest = dict(_load_texture_manifest())
        key = serial.upper()
        existing = list(manifest.get(key, []))
        source_id = entry.get('github_repo') or entry.get('archive_org_id') or entry.get('direct_url')

        def _source_id(e):
            return e.get('github_repo') or e.get('archive_org_id') or e.get('direct_url')

        if any(_source_id(e) == source_id for e in existing):
            return True
        existing.append(entry)
        manifest[key] = existing
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
        _TEXTURE_MANIFEST_CACHE = manifest
        return True
    except Exception as e:
        logger.warning(f"[textures] Failed to save texture source for {serial}: {e}")
        return False


def _score_title_candidate(text: str, html: Optional[str] = None) -> int:
    """Return a heuristic score for a title candidate. Higher is better.
    Boosts multi-word, alphabetic content, presence of lowercase (likely proper titles),
    and punctuation like parentheses or colon. Penalizes short tokens, hex-like tokens,
    and common placeholders.
    """
    if not text:
        return -9999
    # sanitize leading punctuation/nbsp
    t = re.sub(r'^[\s\u00A0\._:\-\|]+', '', text).strip()
    tu = t.upper()
    # obvious placeholders should be rejected
    if tu in ('INFO', 'TITLE', 'N/A', 'UNKNOWN'):
        return -9999
    # base score from length (favor substantial titles)
    score = max(0, len(t))
    # must contain letters to be useful
    if not re.search(r'[A-Za-z]', t):
        score -= 120
    else:
        score += 40
    # multi-word bonus
    words = [w for w in re.split(r'\s+', t) if w]
    if len(words) > 1:
        score += 14 * min(6, len(words))
    # lowercase presence is a good sign (titles often mixed case)
    if re.search(r'[a-z]', t):
        score += 24
    # punctuation often in titles
    if re.search(r'[\(\)\-:–—\.]', t):
        score += 10
    # penalize short ALL-CAPS tokens (likely abbreviations or placeholders)
    if t.isupper():
        # heavily penalize if many short tokens (e.g., 'INFO', 'DVD MENU', 'PS1')
        short_tokens = [w for w in words if len(w) < 5]
        if len(words) == 1 and len(t) < 6:
            score -= 60
        elif len(short_tokens) >= len(words) and len(words) <= 3:
            score -= 36
    # penalize hex-like tokens
    if re.fullmatch(r'[0-9A-Fa-f]{1,8}', t):
        score -= 100
    # prefer candidates with many alphabetic chars relative to length
    alpha = len(re.findall(r'[A-Za-z]', t))
    if alpha > 0:
        score += int((alpha / max(1, len(t))) * 40)
    # boost when HTML suggests a title cell (col3 or col7) or anchor/link text
    if html:
        hu = html.lower()
        if 'class="col3"' in hu or "class='col3'" in hu or 'class="col7"' in hu or "class='col7'" in hu:
            score += 60
        # prefer anchor text only if it's not the generic 'INFO' placeholder
        if '<a' in hu:
            # try to extract anchor text
            try:
                soup = BeautifulSoup(html, 'html.parser')
                a = soup.find('a')
                if a:
                    at = (a.get_text(' ', strip=True) or '').strip()
                    if at and at.upper() not in ('INFO', '詳細', 'DETAILS') and re.search(r'[A-Za-z]', at):
                        score += 36
                    else:
                        # generic anchors are less useful
                        score -= 18
            except Exception:
                score += 8
    return score


# Helpers extracted from build_pnach to reduce function complexity
def ai_label_for_group(label_hint, codes, inline_hints=None, used_labels=None):
    import re
    from collections import Counter
    used_labels = used_labels or set()
    # Use comment if it's meaningful
    if label_hint and not re.match(r"^(patch|cheat|code|modifier|fix|enable|disable|on|off|1|2|3|4|5|6|7|8|9|0| )+$", label_hint, re.I):
        label = label_hint.strip()
        if label not in used_labels:
            used_labels.add(label)
            return label
    # Use inline comment if present and meaningful
    if inline_hints:
        meaningful = [h.strip() for h in inline_hints if h and not re.match(r"^(patch|cheat|code|modifier|fix|enable|disable|on|off|1|2|3|4|5|6|7|8|9|0| )+$", h, re.I)]
        if meaningful:
            cnt = Counter(meaningful)
            label = cnt.most_common(1)[0][0]
            if label not in used_labels:
                used_labels.add(label)
                return label
    # Try to infer from code patterns and keywords.
    code_text = " ".join(f"{a} {v}" for a, v in codes)
    patterns = [
        (r"infinite.?ammo|unlimited.?ammo|max.?ammo|all.?ammo|endless.?ammo|never.?reload|no.?reload", "Infinite Ammo"),
        (r"one.?hit.?kill|1.?hit.?kill|kill.?in.?1|insta.?kill|instant.?kill|kill.?with.?one", "One-Hit Kill"),
        (r"invincib|invulnerab|god.?mode|no.?damage|no.?hit|no.?death|immortal|never.?die|undying|unharmed|invincible", "Invincibility"),
        (r"unlock.?char|all.?char|all.?fighters|all.?heroes|all.?players|all.?characters|every.?character|character.?select", "Unlock Characters"),
        (r"unlock.?level|all.?level|all.?stages|all.?maps|every.?level|stage.?select|open.?all.?levels", "Unlock Levels/Stages"),
        (r"unlock.?weapon|all.?weapon|all.?guns|all.?arms|every.?weapon|weapon.?select|all.?swords|all.?items.?unlocked", "Unlock All Weapons"),
        (r"unlock.?item|all.?item|all.?cards|all.?gear|all.?equipment|every.?item|item.?select|all.?collectibles|all.?costumes|all.?outfits", "Unlock All Items"),
        (r"exp|experience|level.?up|max.?level|lvl.?up|gain.?level|max.?exp|infinite.?exp|infinite.?experience|level.?999|level.?max", "EXP/Level Modifier"),
        (r"stat.?max|max.?stat|all.?stat|full.?stat|999.?stat|255.?stat|max.?strength|max.?defense|max.?attack|max.?magic|max.?skill|max.?ability|all.?abilities|all.?skills|all.?stats", "Max Stats"),
        (r"money|gil|zenny|cash|gold|coins|credits|points|score|infinite.?money|max.?money|max.?gold|max.?cash|max.?score|all.?points", "Money/Score Modifier"),
        (r"health|hp|life|max.?hp|full.?hp|restore.?hp|heal|infinite.?hp|infinite.?health|never.?hurt|max.?life|auto.?heal|auto.?recovery", "Health Modifier"),
        (r"mp|sp|ap|ep|energy|mana|magic.?points|infinite.?mp|max.?mp|infinite.?energy|max.?energy|full.?mp|full.?energy", "MP/Energy Modifier"),
        (r"timer|time.?stop|freeze.?time|infinite.?time|no.?timer|time.?modifier|slow.?time|fast.?time|pause.?timer|no.?countdown", "Timer Modifier"),
        (r"speed.?up|fast.?move|run.?fast|move.?speed|walk.?speed|move.?faster|faster.?movement|quick.?move|speed.?modifier|slow.?motion|slowmo|slow.?move", "Speed Modifier"),
        (r"gravity|low.?gravity|zero.?gravity|float|fly|anti.?gravity|moon.?jump|super.?jump|high.?jump", "Gravity Modifier"),
        (r"npc|enemy|ai|boss|monster|foe|all.?enemies|enemy.?modifier|enemy.?ai|boss.?rush|enemy.?stats|enemy.?hp|enemy.?damage", "NPC/Enemy Modifier"),
        (r"distance|range|reach|attack.?range|long.?range|melee.?range|shoot.?range", "Distance/Range Modifier"),
        (r"menu|pause|debug.?menu|test.?menu|secret.?menu|hidden.?menu|cheat.?menu|extra.?menu|bonus.?menu", "Menu/Debug Modifier"),
        (r"latency|input.?lag|input.?latency|controller.?lag|controller.?delay|input.?delay", "Input Latency Modifier"),
        (r"camera|fov|field.?of.?view|zoom|angle|perspective|camera.?control|free.?camera|camera.?hack|camera.?mod", "Camera Modifier"),
        (r"music|sound|audio|bgm|sfx|mute|volume|no.?music|no.?sound|disable.?music|disable.?sound|soundtrack|background.?music", "Music/Sound Modifier"),
        (r"language|region|pal|ntsc|japan|usa|europe|eng|fre|ger|ita|spa|por|rus|chi|kor|region.?free|region.?unlock|language.?select|multi.?language|all.?languages", "Language/Region Patch"),
        (r"save.?anywhere|save.?menu|quick.?save|auto.?save|save.?state|save.?anytime|save.?hack|save.?modifier|save.?location", "Save Anywhere/Save Modifier"),
        (r"walk.?through.?walls|no.?clip|noclip|clip.?off|ghost.?mode|walk.?anywhere|pass.?through.?walls|phase.?through.?walls|wall.?hack|collision.?off|collision.?hack", "No Clip/Walk Through Walls"),
        (r"debug|test|dev.?mode|developer|debug.?mode|test.?mode|beta.?mode|prototype.?mode|dev.?tools|dev.?menu|debug.?tools", "Debug/Test Mode"),
        (r"framerate|60.?fps|30.?fps|120.?fps|fps.?unlock|frame.?rate|unlocked.?fps|frame.?skip|frame.?rate.?modifier", "Framerate Modifier"),
        (r"fix|patch|workaround|bypass|skip|crash|freeze|hang|softlock|hardlock|anti.?crash|anti.?freeze|skip.?scene|skip.?cutscene|skip.?intro|skip.?logo|skip.?movie|skip.?video", "Fix/Bypass Patch"),
        (r"cheat|enable|disable|toggle|on.?off|activate|deactivate|switch|turn.?on|turn.?off", "Cheat Toggle"),
        (r"^20[0-9A-F]{6}", "Simple 8-bit Patch"),
        (r"^10[0-9A-F]{6}", "16-bit Patch"),
        (r"^00[0-9A-F]{6}", "8-bit Patch"),
        (r"^E0[0-9A-F]{6}", "Conditional Patch"),
        (r"^D0[0-9A-F]{6}", "Conditional Patch"),
        (r"^2[0-9A-F]{7}", "Write Patch"),
        (r"^1[0-9A-F]{7}", "Write Patch"),
        (r"^0[0-9A-F]{7}", "Write Patch"),
    ]
    if (not label_hint) and (not inline_hints or not any(h and not re.match(r"^(patch|cheat|code|modifier|fix|enable|disable|on|off|1|2|3|4|5|6|7|8|9|0| )+$", h, re.I) for h in inline_hints)):
        for pat, name in patterns:
            if re.search(pat, code_text, re.I):
                if name not in used_labels:
                    used_labels.add(name)
                    return name
    if len(codes) == 1:
        a, v = codes[0]
        label = f"Patch {a[-6:]}={v[-6:]}"
        if label not in used_labels:
            used_labels.add(label)
            return label
    n = len(used_labels) + 1
    label = f"Cheat Group {n} ({len(codes)} codes)"
    used_labels.add(label)
    return label


def split_group_if_mixed(label_hint, codes, hints):
    import re
    if not codes or len(codes) <= 1:
        return [(label_hint, codes, hints)]

    def _is_trivial_hint(h):
        return not h or re.match(r"^(patch|cheat|code|modifier|fix|enable|disable|on|off|1|2|3|4|5|6|7|8|9|0| )+$", h, re.I)

    meaningful_keys = [ (h.strip() if h and not _is_trivial_hint(h) else "__NO_HINT__") for h in hints ]
    if any(k != "__NO_HINT__" for k in meaningful_keys):
        res = []
        cur_codes = []
        cur_hints = []
        cur_key = meaningful_keys[0]
        for (a,v), k, h in zip(codes, meaningful_keys, hints):
            if k != cur_key and cur_codes:
                res.append((None if cur_key=="__NO_HINT__" else cur_key, list(cur_codes), list(cur_hints)))
                cur_codes = []
                cur_hints = []
                cur_key = k
            cur_codes.append((a,v))
            cur_hints.append(h)
        if cur_codes:
            res.append((None if cur_key=="__NO_HINT__" else cur_key, list(cur_codes), list(cur_hints)))
        if len(res) > 1:
            min_run = 1
            i = 0
            merged = []
            while i < len(res):
                key, rcodes, rhints = res[i]
                if len(rcodes) < min_run:
                    if merged:
                        pk, pcodes, phints = merged[-1]
                        merged[-1] = (pk, pcodes + rcodes, phints + rhints)
                    else:
                        if i+1 < len(res):
                            nk, ncodes, nhints = res[i+1]
                            res[i+1] = (nk, rcodes + ncodes, rhints + nhints)
                        else:
                            merged.append((key, rcodes, rhints))
                    i += 1
                    continue
                else:
                    merged.append((key, rcodes, rhints))
                    i += 1
            if len(merged) > 1:
                return merged
    prefixes = [a[:2] for a, _ in codes]
    uniq_pref = sorted(set(prefixes), key=lambda x: prefixes.count(x), reverse=True)
    if len(uniq_pref) > 1 and len(codes) >= 4:
        split_map = {}
        for p in uniq_pref:
            split_map[p] = []
        for a, v in codes:
            split_map[a[:2]].append((a, v))
        res = []
        for p in uniq_pref:
            grp = split_map.get(p, [])
            if grp:
                res.append((None, grp, [None]*len(grp)))
        return res
    return [(label_hint, codes, hints)]


def _set_label_pixmap_exact(label: 'QLabel', pixmap: 'QPixmap', max_dim: int = 420):
    """Scale pixmap down to fit within max_dim x max_dim, keep aspect ratio.
    Do not scale up small images; set the label fixed size to the resulting pixmap size
    so the image is displayed without cropping or stretching.
    """
    try:
        if not pixmap or pixmap.isNull():
            try:
                label.clear()
            except Exception:
                pass
            return
        w = pixmap.width()
        h = pixmap.height()
        if w <= 0 or h <= 0:
            label.clear()
            return
        # don't upscale; only scale down to max_dim
        maxd = int(max_dim or 512)
        scale = 1.0
        if max(w, h) > maxd:
            scale = float(maxd) / float(max(w, h))
        tw = max(1, int(w * scale))
        th = max(1, int(h * scale))
        scaled = pixmap.scaled(tw, th, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        label.setPixmap(scaled)
        # make the label exactly the pixmap size so there's no cropping
        try:
            label.setFixedSize(scaled.size())
        except Exception:
            pass
    except Exception:
        try:
            label.clear()
        except Exception:
            pass


def parse_serials(text: str) -> List[str]:
    # finditer to capture full match
    return sorted({m.group(0).upper().replace("_", "-") for m in SERIAL_RE.finditer(text)})


def parse_pnach_text(text: str) -> PnachData:
    pd = PnachData()
    lines = text.splitlines()
    for line in lines:
        m = TITLE_LINE.match(line)
        if m:
            pd.title = m.group(1).strip()
            break
    pd.serials = parse_serials(text)
    mcrc = CRC_IN_TEXT.search(text)
    if mcrc:
        pd.crc = normalize_crc(mcrc.group(1))
    for line in lines:
        # Remove leading comment markers for patch scan, but keep original for comments
        scan_line = line.lstrip()
        # If the line starts with comment markers, strip them for patch recognition
        if scan_line.startswith("//") or scan_line.startswith("#") or scan_line.startswith(";"):
            scan_line = scan_line.lstrip("/#; ")
        # If it's a patch line, record as raw_pair
        m = PNACH_PATCH_LINE.match(scan_line)
        if m:
            # Accept all types, but only store address and value, pad value to 8
            addr = m.group(1).upper()
            val = m.group(3).upper().rjust(8, "0")
            pair = (addr, val)
            pd.raw_pairs.append(pair)
            # preserve inline hint from the original line if present (// ...)
            hint_m = re.search(r"//(.+)$", line)
            hint = hint_m.group(1).strip() if hint_m else None
            if hint:
                pd.items.append((addr, val, hint))
            else:
                pd.items.append(pair)
        else:
            # Skip empty lines
            if not line.strip():
                continue
            # Preserve bracket headers that may include trailing text on the same line.
            # Example: "[50/60 FPS] author-asasega ..." should become:
            #   "[50/60 FPS]"
            #   "author-asasega ..."
            mbr_full = re.match(r"^\s*\[([^\]]+)\]\s*(.+)$", line)
            if mbr_full:
                header = mbr_full.group(1).strip()
                remainder = mbr_full.group(2).rstrip()
                pd.comments.append(f"[{header}]")
                pd.items.append(f"[{header}]")
                if remainder:
                    pd.comments.append(remainder)
                    pd.items.append(remainder)
                continue
            # Skip title lines (already captured earlier)
            if TITLE_LINE.match(line):
                continue
            # Keep original leading comment markers (//, #, ;) and formatting; strip only trailing newlines/spaces
            pd.comments.append(line.rstrip())
            pd.items.append(line.rstrip())
    return pd


def parse_raw_8x8(text: str) -> List[Tuple[str, str]]:
    pairs: List[Tuple[str, str]] = []
    for i, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if not s or s.startswith(("#", "//", ";")):
            continue
        s = s.replace(",", " ").replace("=", " ").replace("\t", " ")
        parts = [p for p in s.split() if p]
        # Accept patterns like: XXXXXXXX Y, XXXXXXXX YYYY, XXXXXXXX YYYYYYYY
        if len(parts) >= 2 and HEX8.match(parts[0]) and re.fullmatch(r"[0-9A-Fa-f]{1,8}", parts[1]):
            addr = parts[0].upper()
            val = parts[1].upper().rjust(8, "0")  # pad to 8
            pairs.append((addr, val))
    return pairs


def build_pnach(pd: PnachData) -> str:
    out = []
    if pd.title:
        out.append(f"gametitle={pd.title}")
    if pd.serials:
        out.append(f"// serials: {'; '.join(pd.serials)}")
    if pd.crc:
        out.append(f"// CRC: 0x{pd.crc}")
    if pd.comments:
        out.extend(pd.comments)
    out.append("")

    # Use module-level helpers for label inference and group splitting to keep this function concise
    from collections import Counter

    # Robust grouping: split by blank lines, comment headers, bracket headers, or contiguous patch lines
    lines = list(pd.items) if getattr(pd, 'items', None) else []

    groups = []
    current_label = None
    current_group = []
    inline_hints = []

    def flush_group():
        nonlocal current_label, current_group, inline_hints
        if current_group:
            groups.append((current_label, list(current_group), list(inline_hints)))
            current_group = []
            current_label = None
            inline_hints = []

    BRACKET_HDR = re.compile(r"^\s*\[(.*?)\]\s*$")
    for item in lines:
        if isinstance(item, str):
            mbr = BRACKET_HDR.match(item)
            if mbr:
                flush_group()
                current_label = "__RAW_LITERAL__:" + mbr.group(1).strip()
                continue
            m = re.match(r"\s*(//|#|;)\s*([^:]+):?", item)
            if m:
                flush_group()
                current_label = m.group(2).strip()
            elif item.strip() == "":
                flush_group()
        elif isinstance(item, tuple):
            if len(item) == 3:
                addr, val, hint = item
            else:
                addr, val = item
                hint = None
                m = re.search(r"//(.+)$", val)
                if m:
                    hint = m.group(1).strip()
                    val = val.split("//")[0].strip()
            current_group.append((addr, val))
            inline_hints.append(hint)
    flush_group()

    # Apply splitting pass using module helper
    post_groups = []
    for label, codes, hints in groups:
        splits = split_group_if_mixed(label, codes, hints)
        for s_label, s_codes, s_hints in splits:
            post_groups.append((s_label, s_codes, s_hints))
    groups = post_groups

    used_labels = set()
    if not groups:
        label = ai_label_for_group(pd.title, pd.raw_pairs, used_labels=used_labels)
        out.append(f"[Cheats/{label}]")
        for addr, val in pd.raw_pairs:
            out.append(f"patch=1,EE,{addr},extended,{val}")
    else:
        for idx, (label, codes, hints) in enumerate(groups, 1):
            if label and isinstance(label, str) and label.startswith("__RAW_LITERAL__:"):
                literal = label.split(':', 1)[1]
                out.append(f"[{literal}]")
            else:
                group_label = ai_label_for_group(label, codes, inline_hints=hints, used_labels=used_labels) or f"Cheat {idx}"
                out.append(f"[Cheats/{group_label}]")
            for addr, val in codes:
                out.append(f"patch=1,EE,{addr},extended,{val}")
    out.append("")
    out.append("// Generated by PCTX")
    return "\n".join(out).strip() + "\n"


# ---------------------------- Title Resolver (offline lists) ----------------------------

class ResolveWorker(QThread):
    progressed = Signal(int, int)  # current, total
    resolved = Signal(dict)        # mapping dict {key -> title}
    failed = Signal(str)

    def __init__(self, keys: List[str], local_map: Dict[str, str], use_bundled_lists: bool = False, try_online: bool = False):
        super().__init__()
        self.keys = keys
        self.local_map = {k.upper(): v for k, v in (local_map or {}).items()}
        self.use_bundled_lists = use_bundled_lists
        # only true if caller wants it AND requests is available
        self.try_online = bool(try_online and (requests is not None))
        self.out: Dict[str, str] = {}

    def run(self):
        total = len(self.keys)

        def norm_serial(s):
            return (s or "").upper().replace('-', '').replace('_', '').replace(' ', '')

        psx_map = {}
        psx_crc_map = {}
        # Use local HTML files placed next to the script: ulist2.html, plist2.html, jlist2.html
        if self.use_bundled_lists:
            urls = [os.path.join(_ASSETS_DIR, n) for n in ('ulist2.html', 'plist2.html', 'jlist2.html')]
            for url in urls:
                for enc in ('utf-8', 'windows-1252', 'shift_jis'):
                    try:
                        with open(url, 'r', encoding=enc) as f:
                            soup = BeautifulSoup(f, 'html.parser')
                    except Exception:
                        continue
                    for row in soup.find_all('tr'):
                        tds = row.find_all('td')
                        serial = title = crc = None
                        for td in tds:
                            txt = td.get_text(strip=True)
                            m = SERIAL_RE.search(txt)
                            if m and not serial:
                                serial = m.group(0)
                            if not title and len(txt) > 2 and not SERIAL_RE.search(txt) and not CRC_IN_TEXT.search(txt):
                                title = txt
                            mcrc = CRC_IN_TEXT.search(txt)
                            if mcrc and not crc:
                                crc = mcrc.group(1).upper()
                        if serial and title:
                            psx_map[norm_serial(serial)] = title
                        if serial and crc:
                            psx_crc_map[norm_serial(serial)] = crc
                        if crc and title:
                            psx_map[crc.upper()] = title

        # Parallel resolver per-key
        from concurrent.futures import ThreadPoolExecutor, as_completed

        def resolve_one(key):
            k = (key or '').upper().strip()
            k_norm = norm_serial(k)
            title = self.local_map.get(k) or self.local_map.get(k_norm)
            crc = None
            found_title_html = None
            # Try local HTML bundles
            if not title and self.use_bundled_lists and k:
                title = psx_map.get(k_norm) or psx_map.get(k)
            if not crc and self.use_bundled_lists and k:
                crc = psx_crc_map.get(k_norm) or psx_crc_map.get(k)

            # Online lookup if requested and something missing
            if self.try_online and k and (not title or not crc):
                found_title, found_crc, found_html = cheat_online.resolve_serial_online([k, k_norm])
                if found_title and not title:
                    title = found_title
                    found_title_html = found_html
                if found_crc and not crc:
                    crc = found_crc

            # include optional matched html if present
            return (k, title, crc, found_title_html)

        max_workers = min(6, (os.cpu_count() or 4))
        completed = 0
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = {ex.submit(resolve_one, key): key for key in self.keys}
            for fut in as_completed(futures):
                try:
                    res = fut.result()
                    if isinstance(res, tuple) and len(res) >= 3:
                        k, title, crc = res[0], res[1], res[2]
                        html_snip = res[3] if len(res) > 3 else None
                        if title:
                            self.out[k] = title
                        if crc:
                            self.out[k + '_CRC'] = crc
                        if html_snip:
                            # store a small snippet for later debugging
                            try:
                                self.out[k + '_HTML'] = html_snip
                            except Exception:
                                pass
                except Exception:
                    pass
                completed += 1
                try:
                    self.progressed.emit(completed, total)
                except Exception:
                    pass

        self.resolved.emit(self.out)


class SingleOnlineLookup(QThread):
    """One-off focused lookup for a single serial using PSXDataCenter pages."""
    found = Signal(str)   # title
    failed = Signal()

    def __init__(self, serial: str, parent=None):
        super().__init__(parent)
        self.serial = serial

    def run(self):
        if requests is None:
            self.failed.emit()
            return
        serial = (self.serial or '').strip()
        if not serial:
            self.failed.emit()
            return
        try:
            title, _crc, _html = cheat_online.resolve_serial_online([serial, norm_serial_key(serial)])
        except Exception:
            logger.debug(f"[SingleOnlineLookup] lookup failed for {self.serial}", exc_info=True)
            self.failed.emit()
            return
        if title:
            self.found.emit(title)
        else:
            self.failed.emit()


# ---------------------------- GUI ----------------------------

class CheatsTab(QWidget):
    def __init__(self, parent: 'MainWindow'):
        super().__init__()
        self.parent = parent
        self.parent.paths_changed.connect(self.load_paths)
        self._shutting_down = False  # Flag to prevent new workers during shutdown
        self.mapping: Dict[str, str] = {}  # CRC/Serial -> Title
        # persistent mapping store path (auto-load/save)
        self.map_store_path = os.path.join(os.path.expanduser("~"), ".pcsx2_manager_mapping.json")
        # create minimal widgets early so helper methods can use them safely
        self.list = QListWidget()
        self.progress = QProgressBar()
        
        # Debounce timer for auto-title to prevent freezing on every keystroke
        self._autotitle_timer = QTimer()
        self._autotitle_timer.setSingleShot(True)
        self._autotitle_timer.setInterval(500)  # 500ms delay
        self._autotitle_timer.timeout.connect(self._do_autotitle)
        
        # Load built-in cheats database
        self.cheats_database = self._load_cheats_database()
        
        self._build_ui()
        # load persisted mapping if present
        try:
            if os.path.isfile(self.map_store_path):
                with open(self.map_store_path, 'r', encoding='utf-8') as fh:
                    obj = json.load(fh)
                    if isinstance(obj, dict):
                        for k, v in obj.items():
                            if k and v:
                                self.mapping[str(k).upper()] = str(v)
        except Exception as e:
            logger.warning(f"Failed to load mapping: {e}")
        self.refresh_list()

    def save_mapping(self, path: Optional[str] = None):
        """Persist the current mapping to a JSON file. If path is provided, use that, else use default store."""
        try:
            outp = path or self.map_store_path
            # write a stable sorted mapping
            with open(outp, 'w', encoding='utf-8') as fh:
                json.dump({k: self.mapping[k] for k in sorted(self.mapping.keys())}, fh, ensure_ascii=False, indent=2)
            # reflect chosen path in UI if user-specified via load
            if path and hasattr(self, 'map_path'):
                self.map_path.setText(outp)
            return True
        except (IOError, OSError) as e:
            logger.error(f"Failed to save mapping to {outp}: {e}")
            return False
    
    def _load_cheats_database(self) -> dict:
        """Load the built-in PS2 cheats database (shared module-level cache -- see get_cheats_database())."""
        return get_cheats_database()

    # ---- Worker management to prevent GC / crashes ----
    def _start_worker(self, worker: QThread):
        # Don't start new workers if shutting down
        if getattr(self, '_shutting_down', False):
            return
        # Keep a strong reference so the worker isn't GC'd mid-run
        if not hasattr(self, "_workers"):
            self._workers = []
        worker.setParent(self)
        self._workers.append(worker)
        def _cleanup():
            try:
                self._workers.remove(worker)
            except ValueError:
                pass
            worker.deleteLater()
        release_worker_when_done(worker, _cleanup)
        worker.start()

    def _preview_context_menu(self, pos):
        # Show context menu with Refresh Cover action
        menu = QMenu(self)
        act_refresh = menu.addAction("Refresh Cover")
        act = menu.exec_(self.preview_cover.mapToGlobal(pos))
        if act == act_refresh:
            # Force re-download by removing cached file and spawning worker.
            serial = self.preview_serial.text().strip()
            if not serial:
                try:
                    sel = self.packs_list.selectedItems()
                    if sel:
                        serial = (sel[0].data(0, Qt.UserRole + 2) or '').strip()
                except Exception:
                    serial = ''
            serial_key = norm_serial_key(serial)
            if not serial_key:
                return
            cache_name = os.path.join(self._thumb_cache, f"cover_{serial_key}.jpg")
            try:
                if os.path.isfile(cache_name):
                    os.remove(cache_name)
            except Exception:
                pass
            remote_url = f"https://raw.githubusercontent.com/xlenore/ps2-covers/main/covers/default/{serial_key}.jpg"
            # Build candidate variants to probe (normalized, original, no-separators, lower)
            raw = (serial or '').strip()
            sk = norm_serial_key(raw)
            variants = []
            if sk:
                variants.append(sk.upper())
            if raw:
                variants.append(raw.upper())
            variants.append(raw.replace('-', '').replace('_', '').replace(' ', '').upper())
            variants.append((raw.replace('-', '').replace('_', '').replace(' ', '')).lower())
            variants.append(sk.lower())
            seen = set()
            uniq = []
            for v in variants:
                if not v: continue
                if v in seen: continue
                seen.add(v)
                uniq.append(v)
            candidates = [f"https://raw.githubusercontent.com/xlenore/ps2-covers/main/covers/default/{v}.jpg" for v in uniq]
            # Prefer previously successful candidate if recorded in index.json
            try:
                idx_file = os.path.join(self._thumb_cache, 'index.json')
                if os.path.isfile(idx_file):
                    with open(idx_file, 'r', encoding='utf-8') as inf:
                        idx = json.load(inf)
                    # find any recorded key matching our variants
                    for v in uniq:
                        if v in idx:
                            known = idx[v]
                            if known in candidates:
                                candidates.remove(known)
                                candidates.insert(0, known)
                                break
            except Exception:
                pass
            self.preview_loading.setVisible(True)

            def _on_fetched(path: str):
                try:
                    pm = QPixmap(path)
                    if pm and not pm.isNull():
                        _set_label_pixmap_exact(self.preview_cover, pm, max_dim=420)
                except Exception:
                    self.preview_cover.clear()
                finally:
                    self.preview_loading.setVisible(False)

            def _on_failed():
                try:
                    # try local cache first
                    if os.path.isfile(cache_name):
                        pm = QPixmap(cache_name)
                        if pm and not pm.isNull():
                            _set_label_pixmap_exact(self.preview_cover, pm, max_dim=420)
                            return
                    # fallback to bundled logo.png if available
                    bundled = os.path.join(os.path.dirname(__file__), 'logo.png')
                    if os.path.isfile(bundled):
                        pm = QPixmap(bundled)
                        if pm and not pm.isNull():
                            _set_label_pixmap_exact(self.preview_cover, pm, max_dim=420)
                            return
                    # Show placeholder
                    placeholder = create_cover_placeholder(serial)
                    if placeholder and not placeholder.isNull():
                        _set_label_pixmap_exact(self.preview_cover, placeholder, max_dim=420)
                except Exception:
                    try:
                        self.preview_cover.clear()
                    except Exception:
                        pass
                finally:
                    self.preview_loading.setVisible(False)

            worker = CoverFetchWorker(candidates, cache_name, parent=self)
            worker.fetched.connect(_on_fetched)
            worker.fetch_failed.connect(_on_failed)
            self._start_worker(worker)

    def _build_ui(self):
        # Create a scroll area for the entire tab content
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        
        # Create a container widget for all content
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(theme.SPACING_LG, theme.SPACING_LG, theme.SPACING_LG, theme.SPACING_LG)
        layout.setSpacing(theme.SPACING_MD)
        
        # Quick Start Guide (collapsible)
        self.quick_start_group = QGroupBox("Quick Start Guide")
        self.quick_start_group.setCheckable(True)
        self.quick_start_group.setChecked(True)  # Expand by default for beginners
        qs_layout = QVBoxLayout()
        quick_start_text = QLabel(
            "<b>How to add cheats:</b><br>"
            "1. Enter your game's <b>Serial</b> (e.g., SLUS-12345) or <b>CRC</b><br>"
            "2. Click <b>Fetch Online Cheats</b> or paste codes below<br>"
            "3. Click <b>Generate Preview</b> to see the result<br>"
            "4. Click <b>Save to Cheats</b> to install<br><br>"
            "<i>Tip: You can drag & drop .pnach files directly!</i>"
        )
        quick_start_text.setWordWrap(True)
        qs_layout.addWidget(quick_start_text)
        self.quick_start_group.setLayout(qs_layout)
        layout.addWidget(self.quick_start_group)
        
        # Built-in Cheats Browser (NEW FEATURE)
        self.browser_group = QGroupBox("Browse Built-in Cheats Database")
        self.browser_group.setCheckable(True)
        self.browser_group.setChecked(True)  # Open by default
        browser_layout = QVBoxLayout()
        
        # Info label
        browser_info = QLabel(
            f"<b>{len(self.cheats_database.get('games', []))} popular PS2 games</b> with cheats for different regions (PAL, NTSC-U, NTSC-J)<br>"
            "Select a game and region, choose cheats, then click 'Install Selected Cheats'"
        )
        browser_info.setWordWrap(True)
        browser_layout.addWidget(browser_info)
        
        # Game selector with search and letter filter
        search_label = QLabel("<b>Search Games:</b>")
        browser_layout.addWidget(search_label)
        
        # Search box
        self.game_search = QLineEdit()
        self.game_search.setPlaceholderText("Type game title to search... (e.g., 'Final Fantasy', 'Metal Gear')")
        self.game_search.textChanged.connect(self._on_game_search_changed)
        browser_layout.addWidget(self.game_search)
        
        # Letter filter buttons
        letter_row = QWidget()
        letter_layout = QHBoxLayout(letter_row)
        letter_layout.setContentsMargins(0, 5, 0, 5)
        letter_layout.addWidget(QLabel("Filter by first letter:"))
        
        self.letter_buttons = {}
        letters = "0-9 A B C D E F G H I J K L M N O P Q R S T U V W X Y Z ALL".split()
        for letter in letters:
            btn = QPushButton(letter)
            btn.setMaximumWidth(40)
            btn.setCheckable(True)
            btn.clicked.connect(lambda checked, l=letter: self._on_letter_filter_clicked(l))
            self.letter_buttons[letter] = btn
            letter_layout.addWidget(btn)
        
        letter_layout.addStretch()
        browser_layout.addWidget(letter_row)
        
        # Game list with search results
        game_list_label = QLabel("<b>Available Games:</b>")
        browser_layout.addWidget(game_list_label)
        
        # Store current filter state (initialize BEFORE populate_game_list)
        self._current_letter_filter = None
        self._current_search = ""
        
        self.game_list_widget = QListWidget()
        self.game_list_widget.itemSelectionChanged.connect(self._on_game_list_selected)
        self.game_list_widget.setMinimumHeight(150)
        self._populate_game_list()
        browser_layout.addWidget(self.game_list_widget)
        
        # Region selector
        region_row = QWidget()
        region_layout = QHBoxLayout(region_row)
        region_layout.setContentsMargins(0, 5, 0, 5)
        region_layout.addWidget(QLabel("Region:"))
        self.region_selector = QComboBox()
        self.region_selector.addItem("-- Select region --", None)
        self.region_selector.currentIndexChanged.connect(self._on_region_selected)
        region_layout.addWidget(self.region_selector, 1)
        
        # Serial/CRC info display
        self.region_info_label = QLabel("")
        self.region_info_label.setObjectName(theme.OBJ_MUTED_LABEL)
        region_layout.addWidget(self.region_info_label)
        browser_layout.addWidget(region_row)
        
        # Cheats list with checkboxes
        cheats_label = QLabel("<b>Available Cheats:</b>")
        browser_layout.addWidget(cheats_label)
        
        self.cheats_tree = QTreeWidget()
        self.cheats_tree.setHeaderLabels(["Cheat Name", "Description"])
        self.cheats_tree.setColumnWidth(0, 250)
        self.cheats_tree.setAlternatingRowColors(True)
        self.cheats_tree.setMinimumHeight(200)
        browser_layout.addWidget(self.cheats_tree)
        
        # Action buttons
        browser_actions = QWidget()
        browser_actions_layout = QHBoxLayout(browser_actions)
        browser_actions_layout.setContentsMargins(0, 5, 0, 5)
        
        self.btn_select_all = QPushButton("Select All")
        self.btn_select_all.clicked.connect(self._select_all_cheats)
        self.btn_select_all.setEnabled(False)
        
        self.btn_deselect_all = QPushButton("Deselect All")
        self.btn_deselect_all.clicked.connect(self._deselect_all_cheats)
        self.btn_deselect_all.setEnabled(False)
        
        self.btn_install_selected = QPushButton("Install Selected Cheats")
        self.btn_install_selected.setMinimumHeight(40)
        self.btn_install_selected.setObjectName(theme.OBJ_SUCCESS_BUTTON)
        self.btn_install_selected.clicked.connect(self._install_selected_cheats)
        self.btn_install_selected.setEnabled(False)
        
        browser_actions_layout.addWidget(self.btn_select_all)
        browser_actions_layout.addWidget(self.btn_deselect_all)
        browser_actions_layout.addStretch()
        browser_actions_layout.addWidget(self.btn_install_selected)
        browser_layout.addWidget(browser_actions)
        
        self.browser_group.setLayout(browser_layout)
        layout.addWidget(self.browser_group)
        
        # Target directories panel (simplified, collapsible)
        self.paths_group = QGroupBox("PCSX2 Folders (Auto-detected)")
        self.paths_group.setCheckable(True)
        self.paths_group.setChecked(False)
        fl = QFormLayout(self.paths_group)
        self.cheats_dir = QLineEdit()
        self.cheats_dir.setReadOnly(True)
        self.textures_dir = QLineEdit()
        self.textures_dir.setReadOnly(True)
        self.btn_browse_cheats = QPushButton("Change…")
        self.btn_browse_cheats.clicked.connect(lambda: self._pick_dir(self.cheats_dir))
        self.btn_browse_textures = QPushButton("Change…")
        self.btn_browse_textures.clicked.connect(lambda: self._pick_dir(self.textures_dir))

        fl.addRow("Cheats folder:", self._row(self.cheats_dir, self.btn_browse_cheats))
        fl.addRow("Textures folder:", self._row(self.textures_dir, self.btn_browse_textures))
        layout.addWidget(self.paths_group)

        # Main cheat builder panel (simplified)
        build_group = QGroupBox("Add Cheats to Your Game")
        bl = QVBoxLayout(build_group)
        
        # Basic info section
        basic_info = QWidget()
        basic_layout = QFormLayout(basic_info)
        basic_layout.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)
        
        self.title_edit = QLineEdit()
        self.title_edit.setPlaceholderText("Game title (auto-filled)")
        self.serial_edit = QLineEdit()
        self.serial_edit.setPlaceholderText("e.g., SLUS-12345")
        self.serial_edit.setToolTip("The game's serial number, usually found on the disc or in PCSX2")
        self.crc_edit = QLineEdit()
        self.crc_edit.setPlaceholderText("e.g., F4715852 (optional)")
        self.crc_edit.setToolTip("8-digit hex code identifying your game version. Leave empty if unsure.")
        
        basic_layout.addRow("Game Title:", self.title_edit)
        basic_layout.addRow("Serial Number:", self.serial_edit)
        basic_layout.addRow("CRC (optional):", self.crc_edit)
        bl.addWidget(basic_info)
        
        # Action buttons (prominent)
        action_row = QWidget()
        action_layout = QHBoxLayout(action_row)
        action_layout.setContentsMargins(0, 10, 0, 10)
        
        self.btn_fetch_online = QPushButton("Fetch Online Cheats")
        self.btn_fetch_online.setMinimumHeight(40)
        self.btn_fetch_online.setToolTip("Download cheats from online databases")
        self.btn_fetch_online.clicked.connect(self._fetch_online_cheats)
        
        self.btn_open_pnach = QPushButton("Open Cheat File")
        self.btn_open_pnach.setMinimumHeight(40)
        self.btn_open_pnach.setToolTip("Load an existing .pnach cheat file")
        self.btn_open_pnach.clicked.connect(self._open_pnach_file)
        
        action_layout.addWidget(self.btn_fetch_online)
        action_layout.addWidget(self.btn_open_pnach)
        bl.addWidget(action_row)
        
        # Codes editor
        codes_label = QLabel("Cheat Codes:")
        bl.addWidget(codes_label)
        self.codes_text = QTextEdit()
        self.codes_text.setPlaceholderText(
            "Paste or edit cheat codes here...\n\n"
            "Supported formats:\n"
            "• Raw codes (XXXXXXXX YYYYYYYY)\n"
            "• PNACH format (patch=1,EE,XXXXXXXX,extended,YYYYYYYY)\n"
            "• Format is auto-detected"
        )
        self.codes_text.setMinimumHeight(150)
        bl.addWidget(self.codes_text)
        
        # Generate and Save buttons
        action_row2 = QWidget()
        action_layout2 = QHBoxLayout(action_row2)
        action_layout2.setContentsMargins(0, 5, 0, 5)
        
        self.btn_make = QPushButton("Preview")
        self.btn_make.setMinimumHeight(35)
        self.btn_make.setToolTip("Preview how the cheat file will look")
        self.btn_make.clicked.connect(self._generate_preview)

        self.btn_save = QPushButton("Save to PCSX2")
        self.btn_save.setMinimumHeight(35)
        self.btn_save.setToolTip("Install the cheats to your PCSX2 folder")
        self.btn_save.clicked.connect(self._save_to_cheats)
        
        action_layout2.addWidget(self.btn_make)
        action_layout2.addWidget(self.btn_save)
        bl.addWidget(action_row2)
        
        # Preview (collapsible)
        self.preview_group = QGroupBox("Preview")
        self.preview_group.setCheckable(True)
        self.preview_group.setChecked(False)
        preview_layout = QVBoxLayout()
        self.preview = QTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setMaximumHeight(200)
        preview_layout.addWidget(self.preview)
        self.preview_group.setLayout(preview_layout)
        bl.addWidget(self.preview_group)
        
        layout.addWidget(build_group)
        
        # Advanced options (collapsible)
        self.advanced_group = QGroupBox("Advanced Options")
        self.advanced_group.setCheckable(True)
        self.advanced_group.setChecked(False)
        adv_layout = QVBoxLayout(self.advanced_group)
        
        # Open any file button
        mode_row = QWidget()
        mode_layout = QHBoxLayout(mode_row)
        mode_layout.setContentsMargins(0, 0, 0, 0)
        self.btn_open_codes = QPushButton("Open Any File…")
        self.btn_open_codes.clicked.connect(self._open_codes_file)
        mode_layout.addWidget(self.btn_open_codes)
        mode_layout.addStretch()
        adv_layout.addWidget(mode_row)
        
        # Title resolver
        resolver_subgroup = QGroupBox("Title Auto-Detection")
        resolver_layout = QVBoxLayout()
        
        map_row = QWidget()
        map_layout = QHBoxLayout(map_row)
        map_layout.setContentsMargins(0, 0, 0, 0)
        self.map_path = QLineEdit()
        self.map_path.setPlaceholderText("Custom title mapping file (optional)")
        self.btn_load_map = QPushButton("Load…")
        self.btn_load_map.clicked.connect(self._load_mapping)
        map_layout.addWidget(self.map_path)
        map_layout.addWidget(self.btn_load_map)
        resolver_layout.addWidget(map_row)
        
        self.chk_offline_lists = QCheckBox("Use offline game database")
        self.chk_offline_lists.setToolTip("Use bundled PSXDataCenter lists for offline title lookup")
        self.chk_offline_lists.setChecked(True)
        
        self.chk_online = QCheckBox("Enable online lookup")
        self.chk_online.setToolTip("Search PSXDataCenter.com for game titles (requires internet)")
        
        self.btn_resolve = QPushButton("Resolve Title Now")
        self.btn_resolve.setToolTip("Look up the game title using the Serial/CRC you entered")
        self.btn_resolve.clicked.connect(self._resolve_title_clicked)
        
        resolver_layout.addWidget(self.chk_offline_lists)
        resolver_layout.addWidget(self.chk_online)
        resolver_layout.addWidget(self.btn_resolve)
        
        self.progress.setMinimum(0)
        self.progress.setMaximum(1)
        self.progress.setValue(0)
        self.progress.setMaximumHeight(15)
        resolver_layout.addWidget(self.progress)
        
        self.source_label = QLabel("")
        self.source_label.setObjectName(theme.OBJ_MUTED_LABEL)
        resolver_layout.addWidget(self.source_label)
        
        resolver_subgroup.setLayout(resolver_layout)
        adv_layout.addWidget(resolver_subgroup)
        
        layout.addWidget(self.advanced_group)

        # Existing cheats list
        list_group = QGroupBox("Installed Cheats")
        v2 = QVBoxLayout(list_group)
        
        # Search/filter bar
        search_row = QWidget()
        search_layout = QHBoxLayout(search_row)
        search_layout.setContentsMargins(0, 0, 0, 5)
        search_layout.addWidget(QLabel("Search:"))
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Filter by game name or CRC...")
        self.search_box.textChanged.connect(self._filter_existing_cheats)
        search_layout.addWidget(self.search_box)
        v2.addWidget(search_row)
        
        v2.addWidget(self.list)
        self.btn_refresh = QPushButton("Refresh List")
        self.btn_refresh.clicked.connect(self.refresh_list)
        v2.addWidget(self.btn_refresh)
        layout.addWidget(list_group)

        # Set the container as the scroll area's widget
        scroll.setWidget(container)
        
        # Set the scroll area as the main layout for this tab
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.addWidget(scroll)

        # QoL: auto-title when user edits CRC/Serial (debounced to prevent freezing)
        self.serial_edit.textChanged.connect(lambda: self._autotitle_timer.start())
        self.crc_edit.textChanged.connect(lambda: self._autotitle_timer.start())

        # Drag & Drop
        self.setAcceptDrops(True)

    

    

    def _fetch_online_cheats(self):
        # Automatically collect serial/CRC keys from several places:
        #  - explicit CRC field
        #  - explicit serial field (semicolon separated)
        #  - any serials found in the main codes/editor text area
        keys = []
        crc = self.crc_edit.text().strip()
        if crc:
            keys.append(crc)
        serials = [s.strip() for s in self.serial_edit.text().split(';') if s.strip()]
        keys.extend(serials)
        # Scan the main codes editor for embedded serials/CRCs (auto-detect)
        try:
            editor_text = self.codes_text.toPlainText()
        except Exception:
            editor_text = ''
        try:
            from __main__ import parse_serials
        except Exception:
            # parse_serials is defined in this module; fall back to local reference
            parse_serials = globals().get('parse_serials')
        if parse_serials and editor_text:
            found = parse_serials(editor_text)
            for f in found:
                if f not in keys:
                    keys.append(f)
        if not keys:
            QMessageBox.information(self, "No Serial/CRC", "No serials or CRCs detected to fetch. Enter a Serial/CRC or paste content containing them.")
            return
        all_results = []
        for key in keys:
            try:
                results = fetch_and_cache_cheats(key)
            except Exception as e:
                results = [{'source': 'error', 'error': str(e)}]
            if results:
                all_results.append((key, results))
        if not all_results:
            QMessageBox.information(self, "No Cheats Found", "No cheats found online for the given Serial/CRC.")
            return
        # Show results in a dialog
        msg = ""
        for key, results in all_results:
            msg += f"<b>{key}</b><br>"
            for entry in results:
                if 'error' in entry:
                    msg += f"<i>Error: {entry['error']}</i><br>"
                elif entry.get('source') == 'gamehacking.org':
                    data = entry.get('data')
                    if data and isinstance(data, dict):
                        cheats = data.get('codes') or data.get('results') or []
                        if cheats:
                            for cheat in cheats:
                                desc = cheat.get('name') or cheat.get('desc') or ''
                                code = cheat.get('code') or ''
                                msg += f"<b>{desc}</b><br><pre>{code}</pre>"
                        else:
                            msg += f"<i>No codes found on GameHacking.org.</i><br>"
                    else:
                        msg += f"<i>No data from GameHacking.org.</i><br>"
                elif entry.get('source') == 'psxdatacenter':
                    html = entry.get('html', '')
                    # Try to extract cheat table or relevant chunk
                    snippet = html[:2000] + ('...' if len(html) > 2000 else '')
                    msg += f"<b>PSXDatacenter:</b><br><pre>{snippet}</pre>"
                else:
                    msg += f"<pre>{entry}</pre>"
            msg += "<hr>"
        dlg = QMessageBox(self)
        dlg.setWindowTitle("Online Cheats")
        dlg.setTextFormat(Qt.RichText)
        dlg.setText(msg)
        dlg.setStandardButtons(QMessageBox.Ok)
        dlg.exec()

    def _show_fetch_results(self, results):
        """Present a selectable list of fetched cheats and allow importing codes into the editor.
        `results` should be a list of dicts: {source,title,codes,raw_html,link}
        """
        if not results:
            QMessageBox.information(self, "No Results", "No fetched cheat entries to display.")
            return

        dlg = QDialog(self)
        dlg.setWindowTitle("Fetched Cheats")
        vbox = QVBoxLayout(dlg)

        listw = QListWidget()
        listw.setSelectionMode(QAbstractItemView.ExtendedSelection)

        # Populate list
        for idx, e in enumerate(results):
            title = e.get('title') or e.get('source') or f"Entry {idx+1}"
            codes = e.get('codes') or []
            if not codes and isinstance(e.get('data'), dict):
                codes = e['data'].get('codes') or []
            count = len(codes) if isinstance(codes, (list, tuple)) else (1 if codes else 0)
            item = QListWidgetItem(f"[{e.get('source')}] {title} ({count} codes)")
            item.setData(Qt.UserRole, e)
            listw.addItem(item)

        vbox.addWidget(listw)

        # Layout: left = per-entry editable codes (when single selected), right = aggregated preview
        hmid = QWidget()
        hmid_h = QHBoxLayout(hmid)
        hmid_h.setContentsMargins(0, 0, 0, 0)

        codes_col = QVBoxLayout()
        codes_lbl = QLabel('Codes (editable):')
        codes_list = QListWidget()
        codes_list.setSelectionMode(QAbstractItemView.SingleSelection)
        codes_list.setEditTriggers(QListWidget.DoubleClicked | QListWidget.EditKeyPressed)
        codes_col.addWidget(codes_lbl)
        codes_col.addWidget(codes_list)

        preview_col = QVBoxLayout()
        preview_lbl = QLabel('Preview (editable):')
        preview = QTextEdit()
        preview.setAcceptRichText(False)
        preview.setPlaceholderText(
            'Select one or more entries to aggregate their codes here. You can edit before importing.'
        )
        preview_col.addWidget(preview_lbl)
        preview_col.addWidget(preview)

        left_w = QWidget()
        left_w.setLayout(codes_col)
        right_w = QWidget()
        right_w.setLayout(preview_col)
        hmid_h.addWidget(left_w, 1)
        hmid_h.addWidget(right_w, 2)
        vbox.addWidget(hmid)

        # Options: Replace vs Append
        opt_row = QWidget()
        opt_h = QHBoxLayout(opt_row)
        opt_h.setContentsMargins(0, 0, 0, 0)
        rb_append = QRadioButton('Append to editor')
        rb_replace = QRadioButton('Replace editor')

        # Load stored preference for append/replace
        settings = app_settings()
        pref = settings.value('fetch_dialog/mode', 'append')
        if pref == 'replace':
            rb_replace.setChecked(True)
        else:
            rb_append.setChecked(True)

        opt_h.addWidget(rb_append)
        opt_h.addWidget(rb_replace)
        opt_h.addStretch(1)
        vbox.addWidget(opt_row)

        # Buttons
        btn_row = QWidget()
        btn_h = QHBoxLayout(btn_row)
        btn_h.setContentsMargins(0, 0, 0, 0)
        btn_aggregate = QPushButton("Aggregate Selected")
        btn_import = QPushButton("Import Preview")
        btn_close = QPushButton("Close")
        btn_h.addWidget(btn_aggregate)
        btn_h.addWidget(btn_import)
        btn_h.addWidget(btn_close)
        btn_h.addStretch(1)
        vbox.addWidget(btn_row)

        # Helper to extract code lines from an entry
        def _extract_codes_from_entry(e) -> List[str]:
            codes = e.get('codes')
            if codes and isinstance(codes, (list, tuple)):
                return [str(x).strip() for x in codes if str(x).strip()]
            for key in ('code', 'raw', 'text'):
                v = e.get(key)
                if v and isinstance(v, str):
                    return [ln for ln in v.splitlines() if ln.strip()]
            data = e.get('data') or {}
            if isinstance(data, dict):
                c = data.get('codes') or data.get('results')
                if c and isinstance(c, (list, tuple)):
                    return [str(x).strip() for x in c if str(x).strip()]
                if c and isinstance(c, str):
                    return [ln for ln in c.splitlines() if ln.strip()]
            return []

        def do_aggregate():
            items = listw.selectedItems()
            if not items:
                QMessageBox.information(dlg, 'No selection', 'Select one or more entries to aggregate.')
                return
            agg_lines = []
            for it in items:
                e = it.data(Qt.UserRole)
                for ln in _extract_codes_from_entry(e):
                    agg_lines.append(ln)
            preview.setPlainText('\n'.join(agg_lines))

        def _populate_codes_list_for_item(it: QListWidgetItem):
            codes_list.clear()
            if not it:
                return
            e = it.data(Qt.UserRole)
            codes = _extract_codes_from_entry(e)
            for ln in codes:
                item = QListWidgetItem(ln)
                item.setFlags(item.flags() | Qt.ItemIsEditable | Qt.ItemIsSelectable | Qt.ItemIsEnabled)
                codes_list.addItem(item)

        def _codes_item_changed(changed_item: QListWidgetItem):
            lines = [codes_list.item(i).text() for i in range(codes_list.count())]
            preview.setPlainText('\n'.join(lines))

        def do_import_preview():
            text = preview.toPlainText().strip()
            if not text:
                QMessageBox.information(dlg, 'Empty preview', 'Nothing to import.')
                return
            try:
                mode_val = 'replace' if rb_replace.isChecked() else 'append'
                settings.setValue('fetch_dialog/mode', mode_val)
            except Exception:
                pass

            if rb_replace.isChecked():
                self.codes_text.setPlainText(text)
            else:
                cur = self.codes_text.toPlainText().rstrip()
                if cur:
                    cur = cur + '\n\n' + text
                else:
                    cur = text
                self.codes_text.setPlainText(cur)
            dlg.accept()

        # Connections
        btn_aggregate.clicked.connect(do_aggregate)
        btn_import.clicked.connect(do_import_preview)
        btn_close.clicked.connect(dlg.reject)
        listw.itemSelectionChanged.connect(
            lambda: (
                _populate_codes_list_for_item(listw.currentItem())
                if len(listw.selectedItems()) == 1
                else do_aggregate()
            )
        )
        codes_list.itemChanged.connect(_codes_item_changed)

        dlg.setLayout(vbox)
        dlg.resize(720, 520)
        dlg.exec()
    # Drag & drop events

    # Drag & drop events
    def dragEnterEvent(self, e: QDragEnterEvent):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e: QDropEvent):
        self.progress.setMaximum(1)
        self.progress.setValue(0)
        for url in e.mimeData().urls():
            path = url.toLocalFile()
            low = path.lower()
            if low.endswith('.pnach'):
                try:
                    with open(path, 'r', encoding='utf-8', errors='replace') as f:
                        text = f.read()
                    self.codes_text.setPlainText(text)
                    # Prefer CRC from filename if present
                    m = re.search(r"([0-9A-Fa-f]{8})", os.path.basename(path))
                    prefer_crc = m.group(1) if m else None
                    self._autofill_from_text(text, prefer_filename_crc=prefer_crc)
                except Exception as ex:
                    QMessageBox.warning(self, 'Drop error', str(ex))
            elif low.endswith(('.zip',)):
                # forward to textures tab
                self.parent.textures_tab.import_zip_path(path)
            elif os.path.isdir(path):
                self.parent.textures_tab.import_folder_path(path)
            elif low.endswith(('.txt', '.ini', '.cb', '.cbc', '.rtxt', '.bin')):
                # RAW-ish or converter source
                try:
                    with open(path, 'r', encoding='utf-8', errors='replace') as f:
                        text = f.read()
                    self.codes_text.setPlainText(text)
                    m = re.search(r"([0-9A-Fa-f]{8})", os.path.basename(path))
                    prefer_crc = m.group(1) if m else None
                    self._autofill_from_text(text, prefer_filename_crc=prefer_crc)
                except Exception as ex:
                    QMessageBox.warning(self, 'Drop error', str(ex))

    def _row(self, *widgets):
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        for wd in widgets:
            h.addWidget(wd)
        h.addStretch(1)
        return w

    def load_paths(self, paths: dict):
        self.cheats_dir.setText(paths.get("cheats", ""))
        self.textures_dir.setText(paths.get("textures", ""))

    def _pick_dir(self, line: QLineEdit):
        d = QFileDialog.getExistingDirectory(self, "Select folder", line.text() or os.path.expanduser("~"))
        if d: line.setText(d)

    def _open_pnach_file(self):
        self.progress.setMaximum(1)
        self.progress.setValue(0)
        path, _ = QFileDialog.getOpenFileName(self, "Open PNACH", os.path.expanduser("~"), "PNACH files (*.pnach);;All files (*.*)")
        if not path: return
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
            self.codes_text.setPlainText(text)
            # prefer CRC from filename if present
            fname = os.path.basename(path)
            m = re.search(r"([0-9A-Fa-f]{8})", fname)
            prefer_crc = m.group(1) if m else None
            self._autofill_from_text(text, prefer_filename_crc=prefer_crc)
        except Exception as e:
            QMessageBox.warning(self, "Open error", f"Failed to open: {e}")

    def _open_codes_file(self):
        self.progress.setMaximum(1)
        self.progress.setValue(0)
        path, _ = QFileDialog.getOpenFileName(self, "Open codes file", os.path.expanduser("~"), "All files (*.*)")
        if not path: return
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
            self.codes_text.setPlainText(text)
            # Try to infer CRC from filename if it looks like XXXXXXXX.pnach or contains 8-hex
            fname = os.path.basename(path)
            m = re.search(r"([0-9A-Fa-f]{8})", fname)
            prefer_crc = m.group(1) if m else None
            self._autofill_from_text(text, prefer_filename_crc=prefer_crc)
        except Exception as e:
            QMessageBox.warning(self, "Open error", f"Failed to open: {e}")

    # --- Code Conversion ---
    # Omniconvert integration removed - placeholder was non-functional
    # Users can paste RAW codes directly or use the built-in parser

    def _basic_nonraw_to_raw(self, text: str) -> List[Tuple[str,str]]:
        """Very light parser: if lines look like XXXXXXXX YYYYYYYY or XXXXXXXX=YYYYYYYY, treat as RAW."""
        return parse_raw_8x8(text)

    # --- Title Resolver (load mapping) ---
    def _load_mapping(self):
        path, _ = QFileDialog.getOpenFileName(self, "Load mapping (CSV/JSON)", os.path.expanduser("~"), "CSV/JSON (*.csv *.json);")
        if not path: return
        try:
            mapping: Dict[str,str] = {}
            if path.lower().endswith('.json'):
                with open(path, 'r', encoding='utf-8') as f:
                    obj = json.load(f)
                for k, v in obj.items(): mapping[str(k).upper()] = str(v)
            else:
                # CSV: key,title
                import csv
                with open(path, 'r', encoding='utf-8') as f:
                    for row in csv.DictReader(f):
                        k = (row.get('key') or row.get('id') or row.get('crc') or row.get('serial') or '').upper()
                        t = row.get('title') or row.get('name') or ''
                        if k and t: mapping[k] = t
            # Expand mapping with normalized serials
            expanded = {}
            for k, v in mapping.items():
                expanded[k.upper()] = v
                if SERIAL_RE.search(k):
                    expanded[norm_serial_key(k)] = v
            self.mapping = expanded
            self.map_path.setText(path)
            QMessageBox.information(self, "Loaded", f"Loaded {len(mapping)} mappings.")
        except Exception as e:
            QMessageBox.critical(self, "Mapping error", str(e))

    # --- Unified auto-fill logic ---
    def _autofill_from_text(self, text: str, prefer_filename_crc: Optional[str] = None):
        """
        Best-effort extraction:
        - PNACH format: try parse_pnach_text()
        - RAW-ish text: parse_serials() + CRC_IN_TEXT
        - Fallbacks: filename CRC, emuLog scan
        - Auto-fill Title from local mapping and/or bundled lists if checkbox is on
        """
        # Always clear previous values. When loading a new codes or PNACH file,
        # any previously displayed CRC, serials, title or source labels should be reset.
        self.serial_edit.clear()
        self.crc_edit.clear()
        self.title_edit.clear()
        # Note: the source_label may contain multiple sources separated by " | ". Clear it fully.
        self.source_label.setText("")
        # Try PNACH first
        pd = parse_pnach_text(text)
        found_crc = pd.crc
        found_serials = pd.serials[:]
        found_title = pd.title

        # If nothing, try RAW-ish
        if not (found_crc or found_serials or found_title):
            found_serials = parse_serials(text)
            m = CRC_IN_TEXT.search(text)
            if m:
                found_crc = normalize_crc(m.group(1))

        # Filename CRC preferred if provided
        if prefer_filename_crc and not found_crc:
            fn_crc = normalize_crc(prefer_filename_crc)
            if fn_crc:
                found_crc = fn_crc

        # If still no CRC, ask logs
        if not found_crc:
            suggested = self.parent.textures_tab._suggest_crc_from_logs()
            if suggested:
                found_crc = suggested

        # Push to UI: fill serials and CRC if found
        if found_serials:
            self.serial_edit.setText('; '.join(sorted(set(found_serials))))
        if found_crc:
            self.crc_edit.setText(found_crc)

        # Determine the title source. Prefer the title embedded in the PNACH file (gametitle=)
        # over mapping or offline lists. Keep track of which source(s) provided the title.
        title = None
        source_tags = []
        if found_title:
            title = found_title.strip()
            source_tags.append("Title: PNACH")
        # If no title from PNACH, try the local mapping (CSV/JSON)
        if not title:
            mapping = getattr(self, 'mapping', {}) or {}
            keys_to_try = []
            if found_crc:
                keys_to_try.append(found_crc)
            keys_to_try.extend(found_serials or [])
            for k in keys_to_try:
                kU = k.upper().strip()
                if kU in mapping:
                    title = mapping[kU]
                    source_tags.append("Title: mapping")
                    break
                kN = norm_serial_key(kU)
                if kN in mapping:
                    title = mapping[kN]
                    source_tags.append("Title: mapping")
                    break
        # If still no title and user wants bundled lists, spawn a worker to resolve offline
        if not title and (self.chk_offline_lists.isChecked()) and (found_crc or found_serials):
            keys_to_try = []
            if found_crc:
                keys_to_try.append(found_crc)
            keys_to_try.extend(found_serials or [])
            def on_done(out):
                picked = None
                for kk in keys_to_try:
                    if kk.upper() in out:
                        picked = out[kk.upper()]
                        break
                if picked:
                    # Only set the title if it hasn't been set by PNACH or mapping
                    if not self.title_edit.text().strip():
                        self.title_edit.setText(picked)
                        self.source_label.setText("Title: offline lists")
            worker = ResolveWorker(keys_to_try, getattr(self, 'mapping', {}) or {}, use_bundled_lists=True, try_online=False)
            worker.resolved.connect(on_done)
            self._start_worker(worker)
        else:
            # If a title was found from PNACH or mapping, update the UI accordingly
            if title:
                self.title_edit.setText(title)
                if source_tags:
                    self.source_label.setText(" | ".join(source_tags))

    def _resolve_title_clicked(self):
        keys: List[str] = []
        crc = normalize_crc(self.crc_edit.text())
        if crc: keys.append(crc)
        for s in [s.strip() for s in self.serial_edit.text().split(';') if s.strip()]:
            keys.append(s.upper())
        if not keys:
            QMessageBox.information(self, "No keys", "Enter a CRC or Serial to resolve.")
            return
        self.progress.setMaximum(len(keys))
        self.progress.setValue(0)
        # Always try online as fallback
        worker = ResolveWorker(
            keys,
            self.mapping,
            use_bundled_lists=self.chk_offline_lists.isChecked(),
            try_online=self.chk_online.isChecked()
        )
        worker.progressed.connect(lambda i,t: self.progress.setValue(i))
        def done(out: Dict[str,str]):
            # Prefer CRC title, then serial
            title = None
            found_crc = None
            if crc and crc in out:
                title = out[crc]
            if not title:
                for s in keys:
                    if s in out:
                        title = out[s]
                        break
            # Try to get CRC from results
            if crc and (crc+'_CRC') in out:
                found_crc = out[crc+'_CRC']
            if not found_crc:
                for s in keys:
                    if (s+'_CRC') in out:
                        found_crc = out[s+'_CRC']
                        break
            if title:
                self.title_edit.setText(title)
                self.source_label.setText("Title: online" if self.chk_online.isChecked() else "Title: mapping/offline")
            if found_crc:
                self.crc_edit.setText(found_crc)
            if not title and not found_crc:
                self.source_label.setText("")
                QMessageBox.information(self, "No match", "No title or CRC found in mapping or online.\n\nPossible reasons:\n- Serial/CRC not present in PSXDataCenter or mapping.\n- Serial format/region mismatch.\n- Demo, prototype, or rare disc.\n- Try another region or check for typos.")
        worker.resolved.connect(done)
        worker.failed.connect(lambda msg: QMessageBox.critical(self, "Resolve error", msg))
        self._start_worker(worker)

    def _generate_preview(self):
        title = self.title_edit.text().strip() or None
        serials = [s.strip() for s in self.serial_edit.text().split(";") if s.strip()]
        crc = normalize_crc(self.crc_edit.text())
        content = self.codes_text.toPlainText()

        # Auto-detect format based on content
        if 'patch=' in content.lower() or content.strip().startswith('//'):
            # Looks like PNACH format
            pd = parse_pnach_text(content)
            if title: pd.title = title
            if serials: pd.serials = serials
            if crc: pd.crc = crc
            if not pd.raw_pairs:
                QMessageBox.information(self, "No patch lines", "The .pnach contains no patch lines in 'patch=1,EE,XXXXXXXX,extended,YYYYYYYY' format.")
                return
        else:
            # Try to parse as RAW pairs
            pairs = parse_raw_8x8(content)
            if not pairs:
                bad = self._collect_invalid_raw_lines(content)
                hint = "\n".join(bad) if bad else "No candidate lines detected."
                QMessageBox.information(
                    self, "No codes",
                    "No valid RAW pairs found.\nExpected lines like: XXXXXXXX YYYYYYYY (hex)\n\nSome problematic lines:\n" + hint
                )
                return
            pd = PnachData(crc=crc, serials=serials, title=title, raw_pairs=pairs)
        self.preview.setPlainText(build_pnach(pd))

    def _save_to_cheats(self):
        cheats_dir = self.cheats_dir.text().strip()
        if not cheats_dir or not os.path.isdir(cheats_dir):
            QMessageBox.warning(self, "Missing cheats folder", "Please set a valid PCSX2 cheats folder path.")
            return
        preview = self.preview.toPlainText().strip()
        if not preview:
            QMessageBox.information(self, "Nothing to save", "Generate a preview first.")
            return
        crc = normalize_crc(self.crc_edit.text())
        serials = [s.strip() for s in self.serial_edit.text().split(";") if s.strip()]

        # If CRC is missing but serial is present, try to resolve CRC (local + optional lightweight online best-effort)
        if not crc and serials:
            serial_upper = serials[0].upper()
            mapping = self.mapping if hasattr(self, 'mapping') else {}
            crc_from_map = None
            for k in mapping:
                if k == serial_upper and normalize_crc(mapping[k]):
                    crc_from_map = normalize_crc(mapping[k])
                    break
                if mapping[k].upper() == serial_upper and normalize_crc(k):
                    crc_from_map = normalize_crc(k)
                    break
            # Optional: minimal online scrape if available (kept as fallback)
            if not crc_from_map and requests is not None:
                try:
                    serial_variants = [serial_upper, serial_upper.replace('-', ''), serial_upper.replace('_', ''), serial_upper.replace(' ', '')]
                    found_crc = None
                    url_templates = [
                        'https://psxdatacenter.com/ps2/ntscu2.html',
                        'https://psxdatacenter.com/ps2/pal2.html',
                        'https://psxdatacenter.com/ps2/ntscj2.html',
                    ]
                    for url in url_templates:
                        try:
                            resp = requests.get(url, timeout=10)
                            if resp.status_code == 200:
                                html = resp.text.upper()
                                for variant in serial_variants:
                                    idx = html.find(variant.upper())
                                    if idx != -1:
                                        window = html[max(0, idx-250):idx+250]
                                        mcrc = CRC_IN_TEXT.search(window)
                                        if mcrc:
                                            found_crc = normalize_crc(mcrc.group(1))
                                            break
                                if found_crc:
                                    break
                        except Exception:
                            continue
                    crc_from_map = found_crc
                except Exception:
                    crc_from_map = None

            if crc_from_map:
                crc = crc_from_map
                self.crc_edit.setText(crc)
            else:
                QMessageBox.warning(self, "CRC not found", "Could not resolve CRC for the given serial. Please provide a valid CRC.")
                return

        if not crc:
            QMessageBox.warning(self, "Need CRC", "A valid CRC is required to save the patch. Please provide or resolve the CRC.")
            return

        fname = f"{crc}.pnach"
        outpath = os.path.join(cheats_dir, fname)
        try:
            with open(outpath, "w", encoding="utf-8") as f: f.write(preview)
            QMessageBox.information(self, "Saved", f"Wrote:\n{outpath}")
            self.refresh_list()
        except Exception as e:
            QMessageBox.critical(self, "Save Failed", f"Could not save the cheat file.\n\nError: {str(e)}\n\nPlease check:\n- The cheats folder path is correct\n- You have write permissions\n- PCSX2 is not running")

    def refresh_list(self):
        self.list.clear()
        cheats_dir = self.cheats_dir.text().strip()
        if cheats_dir and os.path.isdir(cheats_dir):
            for name in sorted(os.listdir(cheats_dir)):
                if name.lower().endswith(".pnach"):
                    self.list.addItem(QListWidgetItem(name))
        # Apply current filter if any
        self._filter_existing_cheats(self.search_box.text())

    def _filter_existing_cheats(self, text: str):
        """Filter the installed cheats list by search text."""
        search_text = text.lower().strip()
        for i in range(self.list.count()):
            item = self.list.item(i)
            if not search_text:
                item.setHidden(False)
            else:
                item.setHidden(search_text not in item.text().lower())

    # QoL: auto-title when user types CRC/Serial
    def _maybe_autotitle(self):
        """Debounced trigger for auto-title lookup. Restarts timer on each keystroke."""
        self._autotitle_timer.start()
    
    def _do_autotitle(self):
        """Actual auto-title lookup logic, called after debounce delay."""
        mapping = getattr(self, 'mapping', {}) or {}
        keys = []
        crc = normalize_crc(self.crc_edit.text())
        if crc: keys.append(crc)
        for s in [s.strip() for s in self.serial_edit.text().split(';') if s.strip()]:
            keys.append(s.upper())
        for k in keys:
            if k in mapping:
                self.title_edit.setText(mapping[k])
                self.source_label.setText("Title: mapping")
                return
            kN = norm_serial_key(k)
            if kN in mapping:
                self.title_edit.setText(mapping[kN])
                self.source_label.setText("Title: mapping")
                return
        # Optionally try bundled lists if box checked
        if keys and self.chk_offline_lists.isChecked():
            def on_done(out):
                for k in keys:
                    if k.upper() in out:
                        self.title_edit.setText(out[k.upper()])
                        self.source_label.setText("Title: offline lists")
                        return
            worker = ResolveWorker(keys, mapping, use_bundled_lists=True, try_online=False)
            worker.resolved.connect(on_done)
            self._start_worker(worker)
    
    # ---- Cheat Browser Methods ----
    
    def _populate_game_list(self):
        """Populate the game list widget with all games (or filtered)."""
        self.game_list_widget.clear()
        
        search_text = self._current_search.lower().strip()
        letter_filter = self._current_letter_filter
        
        for game in self.cheats_database.get('games', []):
            title = game.get('title', '').strip()
            
            # Apply letter filter
            if letter_filter and letter_filter != "ALL":
                first_char = title[0].upper() if title else ''
                if letter_filter.startswith('0'):  # "0-9"
                    if not first_char.isdigit():
                        continue
                elif first_char != letter_filter:
                    continue
            
            # Apply search filter
            if search_text and search_text not in title.lower():
                continue
            
            # Add to list
            item = QListWidgetItem(title)
            item.setData(Qt.UserRole, game)
            self.game_list_widget.addItem(item)
        
        # Show result count
        count = self.game_list_widget.count()
        if search_text or letter_filter:
            search_info = f"Found {count} game(s)"
            if search_text:
                search_info += f" matching '{self._current_search}'"
            if letter_filter and letter_filter != "ALL":
                if search_text:
                    search_info += f" starting with '{letter_filter}'"
                else:
                    search_info += f" starting with '{letter_filter}'"
            self.game_list_widget.setToolTip(search_info)
    
    def _on_game_search_changed(self, text):
        """Handle search text changes."""
        self._current_search = text
        self._populate_game_list()
    
    def _on_letter_filter_clicked(self, letter):
        """Handle letter filter button clicks."""
        # Toggle current filter or set new one
        if self._current_letter_filter == letter:
            self._current_letter_filter = None
            self.letter_buttons[letter].setChecked(False)
        else:
            # Uncheck all other buttons
            for btn in self.letter_buttons.values():
                btn.setChecked(False)
            # Check the clicked button
            self.letter_buttons[letter].setChecked(True)
            self._current_letter_filter = letter
        
        self._populate_game_list()
    
    def _on_game_list_selected(self):
        """Handle game selection from the list."""
        self.region_selector.clear()
        self.region_selector.addItem("-- Select region --", None)
        self.cheats_tree.clear()
        self.region_info_label.setText("")
        self.btn_select_all.setEnabled(False)
        self.btn_deselect_all.setEnabled(False)
        self.btn_install_selected.setEnabled(False)
        
        selected_items = self.game_list_widget.selectedItems()
        if not selected_items:
            return
        
        game = selected_items[0].data(Qt.UserRole)
        if game and 'regions' in game:
            for region_name in sorted(game['regions'].keys()):
                self.region_selector.addItem(region_name, game['regions'][region_name])
    
    def _on_region_selected(self, index):
        """Handle region selection in the browser."""
        self.cheats_tree.clear()
        self.region_info_label.setText("")
        self.btn_select_all.setEnabled(False)
        self.btn_deselect_all.setEnabled(False)
        self.btn_install_selected.setEnabled(False)
        
        region_data = self.region_selector.currentData()
        if not region_data:
            return
        
        # Display serial and CRC info
        serial = region_data.get('serial', 'N/A')
        crc = region_data.get('crc', 'N/A')
        self.region_info_label.setText(f"Serial: {serial} | CRC: {crc}")
        
        # Populate cheats tree
        cheats = region_data.get('cheats', [])
        for cheat in cheats:
            item = QTreeWidgetItem([cheat['name'], cheat['description']])
            item.setCheckState(0, Qt.Unchecked)
            item.setData(0, Qt.UserRole, cheat)  # Store cheat data
            self.cheats_tree.addTopLevelItem(item)
        
        if cheats:
            self.btn_select_all.setEnabled(True)
            self.btn_deselect_all.setEnabled(True)
            self.btn_install_selected.setEnabled(True)
    
    def _select_all_cheats(self):
        """Select all cheats in the tree."""
        for i in range(self.cheats_tree.topLevelItemCount()):
            item = self.cheats_tree.topLevelItem(i)
            item.setCheckState(0, Qt.Checked)
    
    def _deselect_all_cheats(self):
        """Deselect all cheats in the tree."""
        for i in range(self.cheats_tree.topLevelItemCount()):
            item = self.cheats_tree.topLevelItem(i)
            item.setCheckState(0, Qt.Unchecked)
    
    def _install_selected_cheats(self):
        """Install selected cheats to PCSX2."""
        # Get selected cheats
        selected_cheats = []
        for i in range(self.cheats_tree.topLevelItemCount()):
            item = self.cheats_tree.topLevelItem(i)
            if item.checkState(0) == Qt.Checked:
                cheat_data = item.data(0, Qt.UserRole)
                if cheat_data:
                    selected_cheats.append(cheat_data)
        
        if not selected_cheats:
            QMessageBox.information(self, "No Cheats Selected", "Please select at least one cheat to install.")
            return
        
        # Get region data for serial and CRC
        region_data = self.region_selector.currentData()
        if not region_data:
            QMessageBox.warning(self, "No Region Selected", "Please select a region first.")
            return
        
        serial = region_data.get('serial', '')
        crc = region_data.get('crc', '')
        # Get game from the selected game in game_list_widget
        selected_game_items = self.game_list_widget.selectedItems()
        game = selected_game_items[0].data(Qt.UserRole) if selected_game_items else None
        title = game.get('title', 'Unknown Game') if game else 'Unknown Game'
        
        # Check if cheats folder is set
        cheats_dir = self.cheats_dir.text().strip()
        if not cheats_dir or not os.path.isdir(cheats_dir):
            QMessageBox.warning(self, "Missing Cheats Folder", 
                              "Please set a valid PCSX2 cheats folder path in the settings.")
            return
        
        if not crc:
            QMessageBox.warning(self, "Missing CRC", "Cannot install cheats without a CRC value.")
            return

        filename = f"{crc.upper()}.pnach"
        existing_filepath = os.path.join(cheats_dir, filename)
        if os.path.isfile(existing_filepath):
            reply = QMessageBox.question(
                self, "File Exists",
                f"A cheat file for this game already exists:\n{filename}\n\nDo you want to overwrite it?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No
            )
            if reply != QMessageBox.Yes:
                return

        try:
            filepath = write_cheats_pnach(title, serial, crc, selected_cheats, cheats_dir)

            # Update mapping
            if serial and title:
                self.mapping[serial.upper()] = title
                self.mapping[crc.upper()] = title
                self.save_mapping()

            # Refresh list
            self.refresh_list()

            # Show success message
            QMessageBox.information(
                self, "Cheats Installed Successfully",
                f"Installed {len(selected_cheats)} cheat(s) to:\n{filepath}\n\n"
                f"The cheats are now available in PCSX2!"
            )

        except Exception as e:
            QMessageBox.critical(self, "Installation Failed", f"Failed to install cheats:\n{str(e)}")

    def _collect_invalid_raw_lines(self, text: str, limit: int = 6) -> List[str]:
        bad = []
        for i, line in enumerate(text.splitlines(), 1):
            s = line.strip()
            if not s or s.startswith(("#", "//", ";")):
                continue
            parts = [p for p in s.replace(",", " ").replace("=", " ").split() if p]
            if len(parts) < 2 or not HEX8.match(parts[0]) or not re.fullmatch(r"[0-9A-Fa-f]{1,8}", parts[1]):
                bad.append(f"L{i}: {line[:120]}")
                if len(bad) >= limit:
                    break
        return bad


class TexturesTab(QWidget):
    class PackInstallWorker(QThread):
        """Background worker that wraps the headless installer so tests and the UI
        can run installs in a QThread and receive progress signals. Exposed as
        TexturesTab.PackInstallWorker for tests.
        """
        progressed = Signal(int, int, str)
        file_progressed = Signal(int, int, str, int, int)
        finished = Signal(int, list)
        failed = Signal(str)

        def __init__(self, items, base, target_hint: str = '', installer=None, parent=None):
            super().__init__(parent)
            self.items = items
            self.base = base
            self.target_hint = target_hint
            # installer is an optional callable to perform installs; if None, run will import the module
            self.installer = installer
            self._cancel = False

        def cancel(self):
            self._cancel = True

        def run(self):
            # Use provided installer callable when available to avoid importing inside worker thread
            perform_pack_installs = self.installer
            if perform_pack_installs is None:
                try:
                    from textures_install import perform_pack_installs
                except Exception as e:
                    try:
                        self.failed.emit(str(e))
                    except Exception:
                        pass
                    return

            def progress_cb(c, t, d):
                try:
                    self.progressed.emit(c, t, d)
                except Exception:
                    pass

            def file_progress_cb(idx, total_items, display, written, total_bytes):
                try:
                    self.file_progressed.emit(idx, total_items, display, written, total_bytes)
                except Exception:
                    pass

            try:
                installed, failures = perform_pack_installs(
                    self.items,
                    self.base,
                    target_hint=self.target_hint,
                    progress_cb=progress_cb,
                    cancel_cb=lambda: self._cancel,
                    file_progress_cb=file_progress_cb,
                )
                try:
                    self.finished.emit(installed, failures)
                except Exception:
                    pass
            except Exception as e:
                try:
                    self.failed.emit(str(e))
                except Exception:
                    pass

        def start(self):
            # If no QApplication (tests that use this worker without GUI), run synchronously
            try:
                from PySide6.QtWidgets import QApplication
                app = QApplication.instance()
            except Exception:
                app = None
            if app is None:
                try:
                    self.run()
                except Exception:
                    pass
                return
            # Ensure a strong reference so the QThread object isn't GC'd while running
            try:
                _ACTIVE_WORKERS.append(self)
                def _on_finish():
                    # Runs on the main thread (queued below). Make sure the thread has fully
                    # unwound before the last reference to it goes away.
                    self.wait(2000)
                    try:
                        _ACTIVE_WORKERS.remove(self)
                    except Exception:
                        pass
                    try:
                        self.deleteLater()
                    except Exception:
                        pass
                # QThread's own end-of-thread signal, NOT self.finished: that name is shadowed by this
                # class's (installed, failures) result signal, which is emitted from inside run() while the
                # thread is still alive. Cleaning up on it deleted the QThread object under a running thread
                # ("QThread: Destroyed while thread is still running" -> process abort).
                super().finished.connect(_on_finish, Qt.QueuedConnection)
            except Exception:
                pass
            super().start()

    def _import_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Folder", os.path.expanduser("~"))
        if folder:
            self.import_folder_path(folder)

    def _import_zip(self):
        file, _ = QFileDialog.getOpenFileName(self, "Select ZIP file", os.path.expanduser("~"), "ZIP Files (*.zip)")
        if file:
            self.import_zip_path(file)
    def __init__(self, parent: 'MainWindow'):
        super().__init__()
        self.parent = parent
        self.parent.paths_changed.connect(self.load_paths)
        self._shutting_down = False  # Flag to prevent new workers during shutdown
        # Thumbnail cache used for installed pack icons
        self._thumb_cache = os.path.join(os.path.expanduser("~"), ".pcsx2_manager_thumbs")
        try:
            os.makedirs(self._thumb_cache, exist_ok=True)
        except Exception:
            pass
        
        # Debounce timer for search filter to improve performance
        self._filter_timer = QTimer()
        self._filter_timer.setSingleShot(True)
        self._filter_timer.setInterval(150)  # 150ms delay
        self._filter_timer.timeout.connect(self._do_filter)
        self._pending_filter_text = ""
        
        self._build_ui()
        
        # The installed-packs list walks every pack folder on disk. It's only
        # visible in the Texture Manager window, so scan the first time that
        # window is shown instead of on every app start.
        self._initial_scan_done = False

    def showEvent(self, event):
        super().showEvent(event)
        if not self._initial_scan_done:
            self._initial_scan_done = True
            QTimer.singleShot(0, self._deferred_scan)

    def _preview_context_menu(self, pos):
        # Context menu for preview cover in TexturesTab
        menu = QMenu(self)
        act_refresh = menu.addAction("Refresh Cover")
        act = menu.exec_(self.preview_cover.mapToGlobal(pos))
        if act == act_refresh:
            # Prefer the preview serial, fall back to the selected item's cached serial
            serial = self.preview_serial.text().strip()
            if not serial:
                try:
                    sel = self.packs_list.selectedItems()
                    if sel:
                        serial = (sel[0].data(0, Qt.UserRole + 2) or '').strip()
                except Exception:
                    serial = ''
            serial_key = norm_serial_key(serial)
            if not serial_key:
                return
            cache_name = os.path.join(self._thumb_cache, f"cover_{serial_key}.jpg")
            try:
                if os.path.isfile(cache_name):
                    os.remove(cache_name)
            except Exception:
                pass
            # Build candidate variants to probe (normalized, original, no-separators, lower)
            raw = (serial or '').strip()
            sk = norm_serial_key(raw)
            variants = []
            if sk:
                variants.append(sk.upper())
            if raw:
                variants.append(raw.upper())
            variants.append(raw.replace('-', '').replace('_', '').replace(' ', '').upper())
            variants.append((raw.replace('-', '').replace('_', '').replace(' ', '')).lower())
            variants.append(sk.lower())
            seen = set()
            uniq = []
            for v in variants:
                if not v: continue
                if v in seen: continue
                seen.add(v)
                uniq.append(v)
            candidates = [f"https://raw.githubusercontent.com/xlenore/ps2-covers/main/covers/default/{v}.jpg" for v in uniq]
            # Prefer previously successful candidate if recorded in index.json
            try:
                idx_file = os.path.join(self._thumb_cache, 'index.json')
                if os.path.isfile(idx_file):
                    with open(idx_file, 'r', encoding='utf-8') as inf:
                        idx = json.load(inf)
                    for v in uniq:
                        if v in idx:
                            known = idx[v]
                            if known in candidates:
                                candidates.remove(known)
                                candidates.insert(0, known)
                                break
            except Exception:
                pass
            self.preview_loading.setVisible(True)

            def _on_fetched(path: str):
                try:
                    pm = QPixmap(path)
                    if pm and not pm.isNull():
                        _set_label_pixmap_exact(self.preview_cover, pm, max_dim=420)
                except Exception:
                    self.preview_cover.clear()
                finally:
                    self.preview_loading.setVisible(False)

            def _on_failed():
                try:
                    # try local cache first
                    if os.path.isfile(cache_name):
                        pm = QPixmap(cache_name)
                        if pm and not pm.isNull():
                            _set_label_pixmap_exact(self.preview_cover, pm, max_dim=420)
                            return
                    # fallback to bundled logo.png if available
                    bundled = os.path.join(os.path.dirname(__file__), 'logo.png')
                    if os.path.isfile(bundled):
                        pm = QPixmap(bundled)
                        if pm and not pm.isNull():
                            _set_label_pixmap_exact(self.preview_cover, pm, max_dim=420)
                            return
                    # Show placeholder
                    placeholder = create_cover_placeholder(serial)
                    if placeholder and not placeholder.isNull():
                        _set_label_pixmap_exact(self.preview_cover, placeholder, max_dim=420)
                except Exception:
                    try:
                        self.preview_cover.clear()
                    except Exception:
                        pass
                finally:
                    self.preview_loading.setVisible(False)

            worker = CoverFetchWorker(candidates, cache_name, parent=self)
            worker.fetched.connect(_on_fetched)
            worker.fetch_failed.connect(_on_failed)
            self._start_worker(worker)

    def _build_ui(self):
        # Create a scroll area for the entire tab content
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        
        # Create a container widget for all content
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(theme.SPACING_LG, theme.SPACING_LG, theme.SPACING_LG, theme.SPACING_LG)
        layout.setSpacing(theme.SPACING_MD)

        group = QGroupBox("Texture Pack Import")
        fl = QFormLayout(group)
        self.textures_dir = QLineEdit()
        self.btn_browse_textures = QPushButton("Browse…")
        self.btn_browse_textures.clicked.connect(lambda: self._pick_dir(self.textures_dir))

        self.target_folder_name = QLineEdit()
        self.target_folder_name.setPlaceholderText("Game ID (e.g., SLUS-12345) or custom folder name")
        self.btn_zip = QPushButton("Import ZIP…")
        self.btn_zip.clicked.connect(self._import_zip)
        self.btn_folder = QPushButton("Import Folder…")
        self.btn_folder.clicked.connect(self._import_folder)

        self.preload_chk = QCheckBox("Suggest enabling texture replacement in PCSX2 settings (manual)")

        fl.addRow("textures:", self._row(self.textures_dir, self.btn_browse_textures))
        fl.addRow("Target folder name:", self.target_folder_name)
        fl.addRow(self._row(self.btn_zip, self.btn_folder))
        fl.addRow(self.preload_chk)
        layout.addWidget(group)

        info = QLabel(
            "Hint: Newer PCSX2 nightlies store texture packs under a 'textures' folder in the user directory.\n"
            "Packs are often organized in subfolders named after the game's CRC. You can paste the CRC here."
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        # Installed packs list + actions
        packs_group = QGroupBox("Installed Packs")
        pv = QVBoxLayout(packs_group)
        # Use a QTreeWidget with three columns: Folder/Serial, Title, Staging
        self.packs_list = QTreeWidget()
        self.packs_list.setColumnCount(3)
        self.packs_list.setHeaderLabels(["Folder/Serial", "Title", "Staged"])
        # Add a search box above the list to filter by serial or title
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search by serial or title...")
        self.search_edit.textChanged.connect(self._filter_packs)
        # Allow multi-selection for mass-install operations
        self.packs_list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.packs_list.itemSelectionChanged.connect(self._on_pack_selected)
        # Context menu for revealing pack paths and staging folder configuration
        self.packs_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.packs_list.customContextMenuRequested.connect(self._packs_context_menu)

        # Layout: left = search + list, right = preview/metadata
        split_row = QHBoxLayout()
        left_w = QWidget()
        left_l = QVBoxLayout(left_w)
        left_l.setContentsMargins(0,0,0,0)
        left_l.addWidget(self.search_edit)
        left_l.addWidget(self.packs_list)

        # Preview panel on the right
        self.preview_cover = QLabel()
        # Do not force a fixed size; we will size the label to the pixmap exact fit
        self.preview_cover.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.preview_cover.setFrameStyle(QLabel.Box | QLabel.Plain)
        self.preview_cover.setAlignment(Qt.AlignCenter)
        # allow right-click to refresh remote cover
        self.preview_cover.setContextMenuPolicy(Qt.CustomContextMenu)
        self.preview_cover.customContextMenuRequested.connect(self._preview_context_menu)
        # Loading indicator shown while fetching remote cover
        self.preview_loading = QLabel("Loading...")
        self.preview_loading.setVisible(False)
        self.preview_loading.setAlignment(Qt.AlignCenter)
        self.preview_loading.setObjectName(theme.OBJ_OVERLAY_LABEL)
        self.preview_title = QLabel("")
        self.preview_serial = QLabel("")
        self.preview_path = QLabel("")
        self.preview_title.setWordWrap(True)
        meta_v = QVBoxLayout()
        # stack cover and loading label vertically (loading under cover so it can be shown/hidden)
        meta_v.addWidget(self.preview_cover)
        meta_v.addWidget(self.preview_loading)
        meta_v.addWidget(self.preview_title)
        meta_v.addWidget(self.preview_serial)
        meta_v.addWidget(self.preview_path)
        meta_v.addStretch(1)
        right_w = QWidget()
        right_w.setLayout(meta_v)

        split_row.addWidget(left_w, 3)
        split_row.addWidget(right_w, 2)
        pv.addLayout(split_row)

        btn_row = QWidget()
        br = QHBoxLayout(btn_row)
        br.setContentsMargins(0, 0, 0, 0)
        self.btn_open_pack = QPushButton("Open")
        self.btn_open_pack.clicked.connect(self._open_selected_pack)
        self.btn_install_pack = QPushButton("Install")
        self.btn_install_pack.clicked.connect(self._install_selected_pack)
        # Mass-install selected staged packs
        self.btn_install_selected = QPushButton("Install Selected")
        self.btn_install_selected.clicked.connect(self._install_selected_multiple)
        self.btn_remove_pack = QPushButton("Remove")
        self.btn_remove_pack.clicked.connect(self._remove_selected_pack)
        self.btn_refresh_packs = QPushButton("Refresh")
        self.btn_refresh_packs.clicked.connect(self.scan_installed_textures)
        for b in (self.btn_open_pack, self.btn_install_pack, self.btn_install_selected, self.btn_remove_pack, self.btn_refresh_packs):
            br.addWidget(b)
        br.addStretch(1)
        # Debug buttons hidden for cleaner UI (uncomment if needed for troubleshooting)
        # self.btn_resolve_all = QPushButton("Resolve All")
        # self.btn_resolve_all.clicked.connect(self._resolve_all_packs)
        # self.btn_show_matched = QPushButton("Show matched HTML")
        # self.btn_show_matched.clicked.connect(self._show_matched_for_selected)
        # br.addWidget(self.btn_resolve_all)
        # br.addWidget(self.btn_show_matched)
        pv.addWidget(btn_row)
        layout.addWidget(packs_group)

        # disable action buttons until a pack is selected
        self.btn_open_pack.setEnabled(False)
        self.btn_install_pack.setEnabled(False)
        self.btn_remove_pack.setEnabled(False)

        # Configure packs_list for better UX
        header = self.packs_list.header()
        header.setStretchLastSection(True)
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        self.packs_list.setAlternatingRowColors(True)
        self.packs_list.setSortingEnabled(True)
        self.packs_list.sortByColumn(1, Qt.AscendingOrder)
        
        # Set the container as the scroll area's widget
        scroll.setWidget(container)
        
        # Set the scroll area as the main layout for this tab
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.addWidget(scroll)

    def _filter_packs(self, text: str):
        """Debounced trigger for filtering. Stores text and restarts timer."""
        self._pending_filter_text = text
        self._filter_timer.start()
    
    def _do_filter(self):
        """Actual filtering logic, called after debounce delay."""
        text = self._pending_filter_text.strip().lower()
        for i in range(self.packs_list.topLevelItemCount()):
            it = self.packs_list.topLevelItem(i)
            serial = (it.text(0) or '').lower()
            title = (it.text(1) or '').lower()
            match = (not text) or (text in serial) or (text in title)
            it.setHidden(not match)

    # ---- Worker management to prevent GC / crashes ----
    def _start_worker(self, worker: QThread):
        # Don't start new workers if shutting down
        if getattr(self, '_shutting_down', False):
            return
        # Keep a strong reference so the worker isn't GC'd mid-run
        if not hasattr(self, "_workers"):
            self._workers = []
        worker.setParent(self)
        self._workers.append(worker)
        def _cleanup():
            try:
                self._workers.remove(worker)
            except ValueError:
                pass
            worker.deleteLater()
        # Try to attach common signals if present
        try:
            worker.fetched.connect(_cleanup)
            worker.fetch_failed.connect(_cleanup)
        except Exception:
            try:
                release_worker_when_done(worker, _cleanup)
            except Exception:
                pass
        worker.start()
    
    def _deferred_scan(self):
        """Perform initial texture scan after UI is fully initialized."""
        try:
            self.scan_installed_textures()
        except Exception as e:
            try:
                logger.debug(f"[TexturesTab] deferred scan failed: {e}")
            except Exception:
                pass
    
    def cleanup_workers(self):
        """Stop and cleanup all running workers."""
        if hasattr(self, "_workers"):
            for worker in list(self._workers):
                try:
                    if worker.isRunning():
                        worker.quit()
                        worker.wait(1000)  # Wait up to 1 second
                    worker.deleteLater()
                except Exception:
                    pass
            self._workers.clear()

    # Public helpers for DnD from Cheats tab
    def import_zip_path(self, path: str):
        base = self.textures_dir.text().strip()
        if not base or not os.path.isdir(base):
            QMessageBox.warning(self, "Missing textures folder", "Please set a valid PCSX2 textures folder path.")
            return

        # Create a staging imports area inside the textures folder so imports are non-destructive
        imports_root = self._imports_root(base)
        os.makedirs(imports_root, exist_ok=True)
        zipname = os.path.splitext(os.path.basename(path))[0]
        staging = os.path.join(imports_root, zipname)
        try:
            # ensure clean staging
            if os.path.exists(staging):
                shutil.rmtree(staging)
            os.makedirs(staging, exist_ok=True)
            with zipfile.ZipFile(path, 'r') as z:
                z.extractall(staging)

            # Detect top-level folders inside the staging area that represent separate packs
            tops = [os.path.join(staging, p) for p in os.listdir(staging)]
            pack_dirs = []
            for p in tops:
                if os.path.isdir(p):
                    # If a folder contains images or a 'replacements' subtree, treat it as a pack
                    rep = os.path.join(p, 'replacements')
                    if self._find_replacements_in_tree(p) or (os.path.isdir(rep) and self._find_replacements_in_tree(rep)):
                        pack_dirs.append(p)
            # If no obvious child packs, treat the whole staging folder as one pack
            if not pack_dirs:
                pack_dirs = [staging]

            # Register each detected pack in the UI without copying into the textures base
            for pd in pack_dirs:
                display_name = os.path.basename(pd)
                # try to prefer a serial-like display name
                m = SERIAL_RE.search(display_name)
                if m:
                    display_name = m.group(0).upper()
                # avoid duplicates
                exists = False
                for i in range(self.packs_list.topLevelItemCount()):
                    it = self.packs_list.topLevelItem(i)
                    if it and it.data(0, Qt.UserRole) == pd:
                        exists = True
                        break
                if exists:
                    continue
                safe_key = os.path.basename(display_name).replace(os.sep, '_')
                thumb = self._make_thumbnail(pd, safe_key)
                # resolve title similarly to import_folder_path
                title_col = ""
                try:
                    if SERIAL_RE.search(display_name) and not HEX8.match(display_name):
                        serial = display_name
                        mapping = getattr(self.parent.cheats_tab, 'mapping', {}) or {}
                        kU = serial.upper().strip()
                        title_col = mapping.get(kU) or mapping.get(norm_serial_key(kU)) or ""
                        try:
                            bl = bundled_lookup_title(kU)
                            if bl:
                                title_col = bl
                                try:
                                    self.parent.cheats_tab.mapping[kU] = bl
                                    self.parent.cheats_tab.save_mapping()
                                except Exception:
                                    pass
                        except Exception:
                            pass
                except Exception:
                    title_col = ""
                it = QTreeWidgetItem([display_name, title_col, "staged"])
                it.setData(0, Qt.UserRole, pd)
                tt = pd
                try:
                    if title_col:
                        tt = f"{pd}\n{title_col}"
                except Exception:
                    pass
                it.setToolTip(0, tt)
                # Use the thumbnail if it was successfully created
                if thumb and os.path.isfile(thumb):
                    it.setIcon(0, QIcon(thumb))
                else:
                    it.setIcon(0, QIcon())
                self.packs_list.addTopLevelItem(it)

            QMessageBox.information(self, "Imported (staged)", f"Imported ZIP into staging folder:\n{staging}\n\nPacks are available in the list and will be installed only when you click Install.")
        except Exception as e:
            QMessageBox.critical(self, "ZIP Import Failed", f"Could not import the ZIP file.\n\nError: {str(e)}\n\nPlease check:\n- The file is a valid ZIP archive\n- It's not corrupted\n- You have enough disk space")

    def _imports_root(self, base_textures_dir: str) -> str:
        """Return the configured imports/staging root. Reads a settings key if present, otherwise defaults to <base>/_imports."""
        try:
            # allow a global override via QSettings under key 'staging/imports_root'
            from PySide6.QtCore import QSettings
            qs = app_settings()
            cfg = qs.value('staging/imports_root', '')
            if cfg:
                return os.path.expanduser(cfg)
        except Exception:
            pass
        return os.path.join(base_textures_dir, '_imports')

    def _packs_context_menu(self, pos):
        sel = self.packs_list.itemAt(pos)
        menu = QMenu(self)
        act_reveal = menu.addAction('Reveal in Explorer')
        act_open_staging = menu.addAction('Open staging folder')
        act_set_staging = menu.addAction('Set staging folder...')
        act = menu.exec_(self.packs_list.mapToGlobal(pos))
        if act == act_reveal and sel:
            p = sel.data(0, Qt.UserRole)
            if p and os.path.exists(p):
                if os.name == 'nt':
                    subprocess.Popen(['explorer', os.path.normpath(p)])
                else:
                    subprocess.Popen(['xdg-open', p])
        elif act == act_open_staging:
            base = self.textures_dir.text().strip()
            if not base: return
            st = self._imports_root(base)
            if not os.path.isdir(st):
                QMessageBox.information(self, 'Staging folder', f'Staging folder does not exist:\n{st}')
                return
            if os.name == 'nt':
                subprocess.Popen(['explorer', os.path.normpath(st)])
            else:
                subprocess.Popen(['xdg-open', st])
        elif act == act_set_staging:
            # allow user to pick a folder to use as staging root
            d = QFileDialog.getExistingDirectory(self, 'Select staging folder', os.path.expanduser('~'))
            if d:
                try:
                    from PySide6.QtCore import QSettings
                    qs = app_settings()
                    qs.setValue('staging/imports_root', os.path.expanduser(d))
                    QMessageBox.information(self, 'Staging folder', f'Staging folder set to:\n{d}')
                except Exception:
                    QMessageBox.information(self, 'Staging folder', f'Staging folder selection saved to:\n{d}')

    def _install_selected_multiple(self):
        items = self.packs_list.selectedItems()
        if not items:
            return
        base = self.textures_dir.text().strip()
        if not base or not os.path.isdir(base):
            QMessageBox.warning(self, "Missing textures folder", "Please set a valid PCSX2 textures folder path.")
            return

        install_items = []
        for it in items:
            try:
                src = it.data(0, Qt.UserRole) or ''
                install_items.append((it.text(0) or '', src))
            except Exception:
                continue

        dlg = QDialog(self)
        dlg.setWindowTitle('Installing Packs')
        dlg.setModal(False)
        v = QVBoxLayout(dlg)
        overall_label = QLabel('Overall:')
        overall_bar = QProgressBar()
        overall_bar.setRange(0, 100)
        per_label = QLabel('Current:')
        per_bar = QProgressBar()
        per_bar.setRange(0, 100)
        eta_label = QLabel('ETA: --:--')
        failures_text = QTextEdit()
        failures_text.setReadOnly(True)
        failures_text.setMaximumHeight(120)
        btn_row = QWidget()
        bh = QHBoxLayout(btn_row)
        retry_btn = QPushButton('Retry Failures')
        retry_btn.setEnabled(False)
        close_btn = QPushButton('Close')
        bh.addWidget(retry_btn)
        bh.addWidget(close_btn)

        v.addWidget(overall_label)
        v.addWidget(overall_bar)
        v.addWidget(per_label)
        v.addWidget(per_bar)
        v.addWidget(eta_label)
        v.addWidget(failures_text)
        v.addWidget(btn_row)
        dlg.show()

        state = {
            'samples': deque(maxlen=6),
            'ema': None,
            'pack_totals': {},
            'current_idx': 0,
            'last_ts': None,
            'failures': [],
        }

        target_hint = self.target_folder_name.text().strip() or ''
        # Use a plain threading.Thread for the dialog installer to avoid Qt QThread / COM issues on Windows.
        import threading

        try:
            from textures_install import perform_pack_installs as _perform_pack_installs
        except Exception:
            _perform_pack_installs = None

        class _DialogWorker:
            def __init__(self, items, base, target_hint, installer):
                self.items = items
                self.base = base
                self.target_hint = target_hint
                self.installer = installer
                self._cancel = False
                self._thread = None

            def cancel(self):
                self._cancel = True

            def is_running(self):
                return self._thread is not None and self._thread.is_alive()

            def start(self, progress_cb, file_progress_cb, finished_cb, failed_cb):
                def _run():
                    try:
                        if not self.installer:
                            # import on main thread failed earlier; try import here as fallback
                            from textures_install import perform_pack_installs as _inst
                        else:
                            _inst = self.installer
                        def _cancel_cb():
                            return self._cancel

                        def _progress_cb(c, t, d):
                            # marshal UI update into Qt main thread
                            QTimer.singleShot(0, lambda: progress_cb(c, t, d))

                        def _file_progress_cb(idx, total_items, display, written, total_bytes):
                            QTimer.singleShot(0, lambda: file_progress_cb(idx, total_items, display, written, total_bytes))

                        installed, failures = _inst(self.items, self.base, target_hint=self.target_hint, progress_cb=_progress_cb, cancel_cb=_cancel_cb, file_progress_cb=_file_progress_cb)
                        QTimer.singleShot(0, lambda: finished_cb(installed, failures))
                    except Exception as e:
                        QTimer.singleShot(0, lambda: failed_cb(str(e)))
                # If running under pytest, run inline to avoid Windows COM / thread issues in test environment
                if os.environ.get('PYTEST_CURRENT_TEST'):
                    _run()
                    return
                th = threading.Thread(target=_run, daemon=True)
                self._thread = th
                th.start()

        worker = _DialogWorker(install_items, base, target_hint, _perform_pack_installs)
        dlg._worker = worker

        def on_progress(c, t, d):
            try:
                overall_bar.setValue(int((c / max(1, t)) * 100))
                per_label.setText(f"Current: {d}")
                now = time.time()
                if state['last_ts'] is None:
                    state['last_ts'] = now
                remaining_bytes = 0
                for idx_key, tot in state['pack_totals'].items():
                    if idx_key >= c:
                        remaining_bytes += tot
                ema = state['ema']
                sec = remaining_bytes / ema if ema and ema > 0 else None
                eta_label.setText(f"ETA: {fmt_eta(sec)}")
            except Exception:
                pass

        def on_file_progress(idx, total_items, display, written, total_bytes):
            try:
                if total_bytes:
                    per_bar.setValue(int((written / max(1, total_bytes)) * 100))
                now = time.time()
                last = state.get('last_ts') or now
                dt = max(1e-6, now - last)
                state['last_ts'] = now
                try:
                    state['pack_totals'][idx] = total_bytes
                except Exception:
                    pass
                try:
                    sample_bps = written / dt if dt > 0 else 0
                    ema = state.get('ema')
                    if ema:
                        lo = max(1.0, ema * 0.1)
                        hi = max(lo, ema * 10.0)
                        sample_bps = max(lo, min(hi, sample_bps))
                    state['samples'].append(sample_bps)
                    alpha = 0.4
                    s = state['ema']
                    if s is None:
                        s = sample_bps
                    else:
                        s = alpha * sample_bps + (1 - alpha) * s
                    state['ema'] = s
                except Exception:
                    pass
                ema = state.get('ema')
                remaining = 0
                for k, v in state.get('pack_totals', {}).items():
                    if k >= idx:
                        remaining += v
                sec = remaining / ema if ema and ema > 0 else None
                eta_label.setText(f"ETA: {fmt_eta(sec)} ({fmt_speed(ema)})")
            except Exception:
                pass

        def on_finished(installed, failures):
            try:
                if failures:
                    state['failures'] = failures
                    failures_text.clear()
                    for f in failures:
                        try:
                            failures_text.append(str(f))
                        except Exception:
                            pass
                    retry_btn.setEnabled(True)
                else:
                    retry_btn.setEnabled(False)
                try:
                    dlg.hide()
                except Exception:
                    pass
                try:
                    QTimer.singleShot(50, dlg.close)
                except Exception:
                    try:
                        dlg.close()
                    except Exception:
                        pass
            except Exception:
                pass

        def on_failed(msg):
            try:
                QMessageBox.critical(self, 'Install failed', msg)
                dlg.close()
            except Exception:
                pass

        def do_retry():
            retry_items = [(d, s) for (d, s, msg) in state.get('failures', [])]
            if not retry_items:
                return
            failures_text.clear()
            retry_btn.setEnabled(False)
            new_worker = _DialogWorker(retry_items, base, target_hint, _perform_pack_installs)
            dlg._worker = new_worker
            new_worker.start(on_progress, on_file_progress, on_finished, on_failed)

        retry_btn.clicked.connect(do_retry)

        def _close_and_cancel():
            try:
                worker.cancel()
            except Exception:
                pass
            try:
                dlg.hide()
            except Exception:
                pass
            try:
                dlg.close()
            except Exception:
                pass

        close_btn.clicked.connect(_close_and_cancel)

        # Start the dialog worker
        worker.start(on_progress, on_file_progress, on_finished, on_failed)

        # Watchdog: ensure the dialog closes when the worker stops even if signals are missed
        def _watch_worker():
            try:
                w = getattr(dlg, '_worker', None)
                if not w:
                    try:
                        dlg.hide()
                        QTimer.singleShot(50, dlg.close)
                    except Exception:
                        pass
                    return
                running = False
                try:
                    running = bool(w.isRunning())
                except Exception:
                    running = False
                if not running:
                    try:
                        dlg.hide()
                    except Exception:
                        pass
                    try:
                        QTimer.singleShot(50, dlg.close)
                    except Exception:
                        try:
                            dlg.close()
                        except Exception:
                            pass
                    return
                # re-schedule
                QTimer.singleShot(100, _watch_worker)
            except Exception:
                pass

        QTimer.singleShot(200, _watch_worker)

    def import_folder_path(self, src: str):
        base = self.textures_dir.text().strip()
        if not base or not os.path.isdir(base):
            QMessageBox.warning(self, "Missing textures folder", "Please set a valid PCSX2 textures folder path.")
            return

        # If src is already inside base (user selected the intended pack folder), use it directly
        try:
            common = os.path.commonpath([os.path.abspath(base), os.path.abspath(src)])
        except Exception:
            common = None
        if common and os.path.abspath(common) == os.path.abspath(base):
            # If user selected the textures base itself, don't register it as a pack.
            # Instead trigger a scan to list detected serial/CRC subfolders and return.
            try:
                if os.path.abspath(src) == os.path.abspath(base):
                    try:
                        # refresh listing and focus user on Installed Packs
                        self.scan_installed_textures()
                    except Exception:
                        pass
                    return
            except Exception:
                pass
            # Otherwise fall through for cases like selecting a 'replacements' subfolder inside base
            bn = os.path.basename(src)
            if bn.lower() == 'replacements':
                pack_dir = src
                display_name = os.path.basename(os.path.dirname(src))
            else:
                pack_dir = src
                display_name = os.path.basename(src)
            # Instead of copying into the textures base immediately, stage the selected folder under _imports
            imports_root = self._imports_root(base)
            os.makedirs(imports_root, exist_ok=True)
            bn = os.path.basename(src)
            staging = os.path.join(imports_root, bn)
            try:
                if os.path.exists(staging):
                    shutil.rmtree(staging)
                shutil.copytree(src, staging)
            except Exception:
                # fallback: try to move or continue using src as staging
                staging = src

            # Detect child pack folders inside staging and register them separately
            pack_dirs = []
            for child in os.listdir(staging):
                childp = os.path.join(staging, child)
                if os.path.isdir(childp):
                    if self._find_replacements_in_tree(childp) or os.path.isdir(os.path.join(childp, 'replacements')):
                        pack_dirs.append(childp)
            if not pack_dirs:
                pack_dirs = [staging]

            for pd in pack_dirs:
                display_name = os.path.basename(pd)
                m = SERIAL_RE.search(display_name)
                if m:
                    display_name = m.group(0).upper()
                exists = False
                for i in range(self.packs_list.topLevelItemCount()):
                    it = self.packs_list.topLevelItem(i)
                    if it and it.data(0, Qt.UserRole) == pd:
                        exists = True
                        break
                if exists:
                    continue
                safe_key = os.path.basename(display_name).replace(os.sep, '_')
                thumb = self._make_thumbnail(pd, safe_key)
                title_col = ""
                try:
                    if SERIAL_RE.search(display_name) and not HEX8.match(display_name):
                        kU = display_name.upper().strip()
                        mapping = getattr(self.parent.cheats_tab, 'mapping', {}) or {}
                        title_col = mapping.get(kU) or mapping.get(norm_serial_key(kU)) or ""
                        try:
                            bl = bundled_lookup_title(kU)
                            if bl:
                                title_col = bl
                                try:
                                    self.parent.cheats_tab.mapping[kU] = bl
                                    self.parent.cheats_tab.save_mapping()
                                except Exception:
                                    pass
                        except Exception:
                            pass
                except Exception:
                    title_col = ""
                it = QTreeWidgetItem([display_name, title_col, "staged"])
                it.setData(0, Qt.UserRole, pd)
                tt = pd
                try:
                    if title_col:
                        tt = f"{pd}\n{title_col}"
                except Exception:
                    pass
                it.setToolTip(0, tt)
                # Use the thumbnail if it was successfully created
                if thumb and os.path.isfile(thumb):
                    it.setIcon(0, QIcon(thumb))
                else:
                    it.setIcon(0, QIcon())
                self.packs_list.addTopLevelItem(it)
            # Refresh UI list but do not install any pack yet
            try:
                # keep focus on Installed Packs
                self.scan_installed_textures()
            except Exception:
                pass
            return

        # Otherwise infer target folder name from user input or source folder name
        sub = self.target_folder_name.text().strip()
        if not sub:
            # If source ends with 'replacements', use parent folder name
            bn = os.path.basename(src)
            if bn.lower() == 'replacements':
                candidate = os.path.basename(os.path.dirname(src))
            else:
                candidate = os.path.basename(src)
            # Prefer serial if detected in the candidate or inside folder tree
            m = SERIAL_RE.search(candidate)
            if not m:
                # shallow search inside src for serial-like folder or file names
                for root, dirs, files in os.walk(src):
                    for nm in dirs + files:
                        mm = SERIAL_RE.search(nm)
                        if mm:
                            m = mm
                            break
                    if m:
                        break
            sub = m.group(0).upper() if m else candidate

        target = os.path.join(base, sub)
        try:
            if os.path.exists(target) and os.path.isdir(target):
                # copy contents of src into existing target
                for item in os.listdir(src):
                    s = os.path.join(src, item)
                    d = os.path.join(target, item)
                    if os.path.isdir(s):
                        if os.path.exists(d): shutil.rmtree(d)
                        shutil.copytree(s, d)
                    else:
                        shutil.copy2(s, d)
            else:
                # copy entire src into target
                shutil.copytree(src, target)
            QMessageBox.information(self, "Imported", f"Copied folder contents to:\n{target}")
            try:
                self.scan_installed_textures()
            except Exception:
                pass
        except Exception as e:
            QMessageBox.critical(self, "Copy error", str(e))

    # Drag & drop
    def dragEnterEvent(self, e: QDragEnterEvent):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e: QDropEvent):
        for url in e.mimeData().urls():
            p = url.toLocalFile()
            if p.lower().endswith('.zip'):
                self.import_zip_path(p)
            elif os.path.isdir(p):
                self.import_folder_path(p)

    def _row(self, *widgets):
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        for wd in widgets:
            h.addWidget(wd)
        h.addStretch(1)
        return w

    def load_paths(self, paths: dict):
        self.textures_dir.setText(paths.get("textures", ""))

    def _pick_dir(self, line: QLineEdit):
        d = QFileDialog.getExistingDirectory(self, "Select folder", line.text() or os.path.expanduser("~"))
        if d: line.setText(d)

    def _target_dir(self) -> Optional[str]:
        base = self.textures_dir.text().strip()
        if not base or not os.path.isdir(base):
            QMessageBox.warning(self, "Missing textures folder", "Please set a valid PCSX2 textures folder path.")
            return None
        sub = self.target_folder_name.text().strip()
        if not sub:
            # Try auto-CRC from logs
            suggestion = self._suggest_crc_from_logs()
            if suggestion:
                self.target_folder_name.setText(suggestion)
                sub = suggestion
            else:
                QMessageBox.information(self, "Folder name needed", "Enter a target subfolder name (CRC recommended).")
                return None
        out = os.path.join(base, sub)
        os.makedirs(out, exist_ok=True)
        return out

    def _on_pack_selected(self):
        sel = self.packs_list.selectedItems()
        if not sel:
            # clear preview
            try:
                self.preview_cover.clear()
                self.preview_title.setText("")
                self.preview_serial.setText("")
                self.preview_path.setText("")
            except Exception:
                pass
            self.btn_open_pack.setEnabled(False)
            self.btn_install_pack.setEnabled(False)
            self.btn_remove_pack.setEnabled(False)
            return

        it = sel[0]
        pack_dir = it.data(0, Qt.UserRole)
        # Prefer an explicit serial stored on the item (UserRole+2). If missing, try to compute and cache one.
        serial_stored = ''
        try:
            serial_stored = (it.data(0, Qt.UserRole + 2) or '').strip()
        except Exception:
            serial_stored = ''
        if not serial_stored:
            # Try to extract from display text then from path
            try:
                disp = it.text(0) or ''
                m = SERIAL_RE.search(disp)
                if m:
                    serial_stored = m.group(0).upper()
                else:
                    pth = it.data(0, Qt.UserRole) or ''
                    try:
                        m2 = SERIAL_RE.search(os.path.basename(pth) or '')
                        if m2:
                            serial_stored = m2.group(0).upper()
                    except Exception:
                        serial_stored = ''
            except Exception:
                serial_stored = ''
            # cache back onto the item for future use
            try:
                if serial_stored:
                    it.setData(0, Qt.UserRole + 2, serial_stored)
            except Exception:
                pass
        # populate preview path
        self.preview_path.setText(pack_dir or "")

    # Prefer Title column (column 1) for the preview title when available
        try:
            title_col = it.text(1) or ""
        except Exception:
            # fallback if API differs
            title_col = ""

        # The display column (0) may include 'Serial — Title' for backward compat
        try:
            display_col = it.text(0) or ""
        except Exception:
            display_col = str(it)

        serial_val = ""
        title_val = ""

        if title_col and title_col.strip():
            # Use explicit title column; try to extract serial from display_col or path
            title_val = title_col.strip()
            # If display_col contains an em-dash, left side is serial
            if '—' in display_col:
                parts = [p.strip() for p in display_col.split('—', 1)]
                serial_val = parts[0]
            else:
                # Prefer stored serial if present, otherwise try to find a serial-like token in display_col
                if serial_stored:
                    serial_val = serial_stored
                else:
                    m = SERIAL_RE.search(display_col or '')
                    if m:
                        serial_val = m.group(0).upper()
                    else:
                        # fallback: try pack_dir basename
                        try:
                            bn = os.path.basename(pack_dir or '')
                            m2 = SERIAL_RE.search(bn)
                            if m2:
                                serial_val = m2.group(0).upper()
                        except Exception:
                            serial_val = ""
        else:
            # No explicit title column: parse display_col for both serial and title
            if '—' in display_col:
                parts = [p.strip() for p in display_col.split('—', 1)]
                serial_val = parts[0]
                title_val = parts[1]
            else:
                # try to extract serial first, then use rest as title
                if serial_stored:
                    serial_val = serial_stored
                    # try to produce a title by removing serial token from display_col if present
                    m = SERIAL_RE.search(display_col or '')
                    if m:
                        title_val = display_col.replace(m.group(0), '').strip(' -_/') or ""
                    else:
                        title_val = display_col
                else:
                    m = SERIAL_RE.search(display_col or '')
                    if m:
                        serial_val = m.group(0).upper()
                        title_val = display_col.replace(m.group(0), '').strip(' -_/') or ""
                    else:
                        # nothing serial-like; use display_col as title
                        title_val = display_col

        # Populate preview widgets
        try:
            self.preview_title.setText(title_val or "")
            self.preview_serial.setText(serial_val or "")
        except Exception:
            pass

    # Try to resolve title from local mapping if missing
        try:
            if not title_val and serial_val:
                mapping = getattr(self.parent.cheats_tab, 'mapping', {}) or {}
                kU = serial_val.upper().strip()
                title_val = mapping.get(kU) or mapping.get(norm_serial_key(kU)) or title_val
        except Exception:
            pass

        # Try to get game cover - prioritize actual covers over random texture files
        pix = None
        
        # First check if we have a serial to fetch proper cover
        if serial_stored:
            serial_val = serial_stored

        if serial_val and requests is not None:
            try:
                # Normalize the serial for cache filename and remote URL lookups
                serial_key = norm_serial_key(serial_val)
                cache_name = os.path.join(self._thumb_cache, f"cover_{serial_key}.jpg")
                # If cache exists, load it synchronously
                if os.path.isfile(cache_name):
                    pm = QPixmap(cache_name)
                    if pm and not pm.isNull():
                        pix = pm
                else:
                    # Build a prioritized list of candidate serial keys to probe for remote covers
                    raw = (serial_val or '').strip()
                    sk = norm_serial_key(raw)
                    variants = []
                    # preferred: normalized uppercase no separators
                    if sk:
                        variants.append(sk.upper())
                    # original presentation (uppercase)
                    if raw:
                        variants.append(raw.upper())
                    # no-hyphen/no-underscore/lower variants
                    variants.append(raw.replace('-', '').replace('_', '').replace(' ', '').upper())
                    variants.append((raw.replace('-', '').replace('_', '').replace(' ', '')).lower())
                    variants.append(sk.lower())
                    # dedupe while preserving order
                    seen = set()
                    uniq = []
                    for v in variants:
                        if not v: continue
                        if v in seen: continue
                        seen.add(v)
                        uniq.append(v)
                    candidates = [f"https://raw.githubusercontent.com/xlenore/ps2-covers/main/covers/default/{v}.jpg" for v in uniq]
                    # Prefer previously successful candidate if recorded in index.json
                    try:
                        idx_file = os.path.join(self._thumb_cache, 'index.json')
                        if os.path.isfile(idx_file):
                            with open(idx_file, 'r', encoding='utf-8') as inf:
                                idx = json.load(inf)
                            for v in uniq:
                                if v in idx:
                                    known = idx[v]
                                    if known in candidates:
                                        candidates.remove(known)
                                        candidates.insert(0, known)
                                        break
                    except Exception:
                        pass
                    self.preview_loading.setVisible(True)

                    # local handlers for worker signals
                    def _on_fetched(path: str):
                        try:
                            pm = QPixmap(path)
                            if pm and not pm.isNull():
                                # set pixmap but size label to exact scaled dimensions (no cropping)
                                _set_label_pixmap_exact(self.preview_cover, pm, max_dim=420)
                        except Exception:
                            self.preview_cover.clear()
                        finally:
                            self.preview_loading.setVisible(False)

                    def _on_failed():
                        try:
                            # Try cached file first
                            if os.path.isfile(cache_name):
                                pm = QPixmap(cache_name)
                                if pm and not pm.isNull():
                                    _set_label_pixmap_exact(self.preview_cover, pm, max_dim=420)
                                    return
                            
                            # Try bundled logo
                            bundled = os.path.join(os.path.dirname(__file__), 'logo.png')
                            if os.path.isfile(bundled):
                                pm = QPixmap(bundled)
                                if pm and not pm.isNull():
                                    _set_label_pixmap_exact(self.preview_cover, pm, max_dim=420)
                                    return
                            
                            # Show placeholder with hint - use exact fitting like other pixmaps
                            placeholder = create_cover_placeholder(serial_val)
                            if placeholder and not placeholder.isNull():
                                _set_label_pixmap_exact(self.preview_cover, placeholder, max_dim=420)
                            else:
                                self.preview_cover.clear()
                        except Exception as e:
                            logger.error(f"Error in _on_failed: {e}")
                            try:
                                self.preview_cover.clear()
                            except Exception:
                                pass
                        finally:
                            self.preview_loading.setVisible(False)

                    worker = CoverFetchWorker(candidates, cache_name, parent=self)
                    worker.fetched.connect(_on_fetched)
                    worker.fetch_failed.connect(_on_failed)
                    self._start_worker(worker)
            except Exception:
                pix = None

        if pix:
            # Display pixmap with exact fit up to a sensible maximum
            _set_label_pixmap_exact(self.preview_cover, pix, max_dim=420)
        else:
            self.preview_cover.clear()

        # enable actions
        self.btn_open_pack.setEnabled(bool(pack_dir))
        # allow install for local zips or external packs; assume install enabled
        self.btn_install_pack.setEnabled(True)
        self.btn_remove_pack.setEnabled(bool(pack_dir))

    def scan_installed_textures(self):
        """Scan the textures base folder for installed packs (subfolders). Build thumbnails for each pack."""
        base = self.textures_dir.text().strip()
        self.packs_list.clear()
        if not base or not os.path.isdir(base):
            return
        
        # Process events periodically to keep UI responsive
        from PySide6.QtWidgets import QApplication
        process_counter = 0

        def contains_images(d: str, depth=2) -> bool:
            exts = ('.png', '.jpg', '.jpeg', '.bmp', '.tga')
            try:
                for root, _, files in os.walk(d):
                    for f in files:
                        if f.lower().endswith(exts):
                            return True
                    if os.path.relpath(root, d).count(os.sep) >= depth:
                        continue
            except Exception:
                return False
            return False

        # If user pointed to a 'replacements' folder inside a CRC folder, show that CRC as a pack
        bn = os.path.basename(base)
        parent = os.path.dirname(base)
        if bn.lower() == 'replacements' and os.path.isdir(parent):
            parent_bn = os.path.basename(parent)
            if HEX8.match(parent_bn) or SERIAL_RE.search(parent_bn) or contains_images(base):
                # mark serial folders explicitly
                if SERIAL_RE.search(parent_bn) and not HEX8.match(parent_bn):
                    # Try to resolve full title from mapping using serial
                    serial = parent_bn
                    mapping = getattr(self.parent.cheats_tab, 'mapping', {}) or {}
                    title = None
                    kU = serial.upper().strip()
                    if kU in mapping:
                        title = mapping[kU]
                    else:
                        kN = norm_serial_key(kU)
                        if kN in mapping:
                            title = mapping[kN]
                    display = f"{serial} — {title}" if title else serial
                else:
                    display = parent_bn
                pack_dir = base
                thumb = self._make_thumbnail(pack_dir, display)
                title_col = ""
                try:
                    if SERIAL_RE.search(parent_bn) and not HEX8.match(parent_bn):
                        kU = parent_bn.upper().strip()
                        mapping = getattr(self.parent.cheats_tab, 'mapping', {}) or {}
                        title_col = mapping.get(kU) or mapping.get(norm_serial_key(kU)) or ""
                except Exception:
                    title_col = ""
                it = QTreeWidgetItem([display, title_col, ""]) 
                it.setData(0, Qt.UserRole, pack_dir)
                tt = pack_dir
                try:
                    if title_col:
                        tt = f"{pack_dir}\n{title_col}"
                except Exception:
                    pass
                it.setToolTip(0, tt)
                # Use the thumbnail if it was successfully created
                if thumb and os.path.isfile(thumb):
                    it.setIcon(0, QIcon(thumb))
                else:
                    it.setIcon(0, QIcon())
                try:
                    if logger.isEnabledFor(logging.DEBUG):
                        logger.debug(f"[TexturesTab] adding item: display='{display}' title_col='{title_col}' path='{pack_dir}'")
                except Exception:
                    pass
                self.packs_list.addTopLevelItem(it)

        # helper: check shallow images (direct files in base or a direct 'replacements' child)
        def _contains_images_shallow(d: str) -> bool:
            exts = ('.png', '.jpg', '.jpeg', '.bmp', '.tga')
            try:
                # check direct files in d
                for f in os.listdir(d):
                    pth = os.path.join(d, f)
                    if os.path.isfile(pth) and f.lower().endswith(exts):
                        return True
                # check direct 'replacements' child
                repl = os.path.join(d, 'replacements')
                if os.path.isdir(repl):
                    for f in os.listdir(repl):
                        if f.lower().endswith(exts):
                            return True
            except Exception:
                return False
            return False

        

        # If the base itself looks like a CRC folder or contains images, treat it as a single pack
        # but if the base contains serial/CRC subfolders, prefer listing those instead.
        # Look for serial/CRC child folders not only immediately under base but also one level deeper.
        def _find_serial_children(root: str, max_depth: int = 2):
            found = []
            try:
                # breadth-first-ish: check immediate children first, then one-level nested
                for name in os.listdir(root):
                    p = os.path.join(root, name)
                    if os.path.isdir(p):
                        if SERIAL_RE.search(name) or HEX8.match(name):
                            found.append(name)
                # if none found at level 1, check level 2 (child of each immediate child)
                if not found and max_depth >= 2:
                    for name in os.listdir(root):
                        p = os.path.join(root, name)
                        if os.path.isdir(p):
                            try:
                                for sub in os.listdir(p):
                                    ps = os.path.join(p, sub)
                                    if os.path.isdir(ps) and (SERIAL_RE.search(sub) or HEX8.match(sub)):
                                        found.append(os.path.join(name, sub))
                            except Exception:
                                continue
            except Exception:
                return []
            return found

        child_dirs = _find_serial_children(base, max_depth=2)
        has_serial_children = bool(child_dirs)
        try:
            if child_dirs:
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug(f"[TexturesTab] detected serial children under '{base}': {child_dirs}")
        except Exception:
            pass

        if (HEX8.match(bn) or SERIAL_RE.search(bn) or contains_images(base)) and (not has_serial_children):
            # mark serial folders explicitly
            if SERIAL_RE.search(bn) and not HEX8.match(bn):
                # Resolve serial -> full title when possible
                serial = bn
                mapping = getattr(self.parent.cheats_tab, 'mapping', {}) or {}
                title = None
                kU = serial.upper().strip()
                if kU in mapping:
                    title = mapping[kU]
                else:
                    kN = norm_serial_key(kU)
                    if kN in mapping:
                        title = mapping[kN]
                display = f"{serial} — {title}" if title else serial
            else:
                display = bn
            pack_dir = base
            # prefer 'replacements' subfolder for actual images if present
            repl = os.path.join(base, 'replacements')
            if os.path.isdir(repl) and contains_images(repl):
                pack_dir = repl
            safe_key = display.replace(os.sep, '_')
            thumb = self._make_thumbnail(pack_dir, safe_key)
            title_col = ""
            try:
                if SERIAL_RE.search(bn) and not HEX8.match(bn):
                    kU = bn.upper().strip()
                    mapping = getattr(self.parent.cheats_tab, 'mapping', {}) or {}
                    title_col = mapping.get(kU) or mapping.get(norm_serial_key(kU)) or ""
            except Exception:
                title_col = ""
            it = QTreeWidgetItem([display, title_col, ""]) 
            it.setData(0, Qt.UserRole, pack_dir)
            tt = pack_dir
            try:
                if title_col:
                    tt = f"{pack_dir}\n{title_col}"
            except Exception:
                pass
            it.setToolTip(0, tt)
            # Use the thumbnail if it was successfully created
            if thumb and os.path.isfile(thumb):
                it.setIcon(0, QIcon(thumb))
            else:
                it.setIcon(0, QIcon())
            try:
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug(f"[TexturesTab] adding item: display='{display}' title_col='{title_col}' path='{pack_dir}'")
            except Exception:
                pass
            self.packs_list.addTopLevelItem(it)
            return
        def contains_images(d: str, depth=2) -> bool:
            exts = ('.png', '.jpg', '.jpeg', '.bmp', '.tga')
            try:
                for root, _, files in os.walk(d):
                    for f in files:
                        if f.lower().endswith(exts):
                            return True
                    # limit depth to avoid long walks
                    if os.path.relpath(root, d).count(os.sep) >= depth:
                        continue
            except Exception:
                return False
            return False

        for name in sorted(os.listdir(base)):
            p = os.path.join(base, name)
            try:
                logger.debug(f"[TexturesTab] scanning child: '{name}' is_dir={os.path.isdir(p)}")
            except Exception:
                pass
            # show ZIP files as importable packs
            if os.path.isfile(p) and p.lower().endswith('.zip'):
                title_col = ""
                try:
                    if SERIAL_RE.search(name) and not HEX8.match(name):
                        kU = name.upper().strip()
                        mapping = getattr(self.parent.cheats_tab, 'mapping', {}) or {}
                        title_col = mapping.get(kU) or mapping.get(norm_serial_key(kU)) or ""
                except Exception:
                    title_col = ""
                it = QTreeWidgetItem([name, title_col, ""]) 
                it.setData(0, Qt.UserRole, p)
                tt = p
                try:
                    if title_col:
                        tt = f"{p}\n{title_col}"
                except Exception:
                    pass
                it.setToolTip(0, tt)
                it.setIcon(0, QIcon())
                try:
                    logger.debug(f"[TexturesTab] adding zip item: display='{name}' title_col='{title_col}' path='{p}'")
                except Exception:
                    pass
                self.packs_list.addTopLevelItem(it)
        
                # Process events every 5 items to keep UI responsive
                process_counter += 1
                if process_counter % 5 == 0:
                    QApplication.processEvents()
        
                continue

            if not os.path.isdir(p):
                continue

            # If this child folder itself is a serial/CRC folder, prefer to add it as a pack
            try:
                if SERIAL_RE.search(name) or HEX8.match(name):
                    # Diagnostic: list immediate entries to understand folder layout
                    try:
                        entries = os.listdir(p)
                        logger.debug(f"[TexturesTab] child entries for '{name}': {entries}")
                    except Exception:
                        entries = []
                    try:
                        has_repl = os.path.isdir(os.path.join(p, 'replacements'))
                        logger.debug(f"[TexturesTab] '{name}' has 'replacements' child: {has_repl}")
                    except Exception:
                        pass
                    # Find the best image root (prefer replacements)
                    target = self._find_replacements_in_tree(p) or p
                    logger.debug(f"[TexturesTab] _find_replacements_in_tree('{p}') -> {target}")
                    # If still no images, check direct 'replacements' child explicitly
                    try:
                        repl = os.path.join(p, 'replacements')
                        if os.path.isdir(repl) and contains_images(repl):
                            target = repl
                    except Exception:
                        pass
                    # Always add the serial/CRC child as a pack entry (user can inspect it)
                    display = name
                    if SERIAL_RE.search(name) and not HEX8.match(name):
                        serial = name
                        mapping = getattr(self.parent.cheats_tab, 'mapping', {}) or {}
                        title = None
                        kU = serial.upper().strip()
                        if kU in mapping:
                            title = mapping[kU]
                        else:
                            kN = norm_serial_key(kU)
                            if kN in mapping:
                                title = mapping[kN]
                        display = f"{serial} — {title}" if title else serial
                    safe_key = display.replace(os.sep, '_')
                    thumb = self._make_thumbnail(target, safe_key)
                    title_col = ""
                    try:
                        if SERIAL_RE.search(name) and not HEX8.match(name):
                            kU = name.upper().strip()
                            mapping = getattr(self.parent.cheats_tab, 'mapping', {}) or {}
                            title_col = mapping.get(kU) or mapping.get(norm_serial_key(kU)) or ""
                            # Try bundled local lookup as a deterministic source before async resolver
                            try:
                                bl = bundled_lookup_title(kU)
                                if bl:
                                    title_col = bl
                                    try:
                                        # persist into mapping
                                        self.parent.cheats_tab.mapping[kU] = bl
                                        self.parent.cheats_tab.save_mapping()
                                    except Exception:
                                        pass
                            except Exception:
                                pass
                    except Exception:
                        title_col = ""
                    it = QTreeWidgetItem([display, title_col, ""]) 
                    it.setData(0, Qt.UserRole, target)
                    tt = target
                    try:
                        if title_col:
                            tt = f"{target}\n{title_col}"
                    except Exception:
                        pass
                    it.setToolTip(0, tt)
                    # Use the thumbnail if it was successfully created
                    if thumb and os.path.isfile(thumb):
                        it.setIcon(0, QIcon(thumb))
                    else:
                        it.setIcon(0, QIcon())
                    try:
                        logger.debug(f"[TexturesTab] adding serial child item: display='{display}' title_col='{title_col}' path='{target}'")
                    except Exception:
                        pass
                    self.packs_list.addTopLevelItem(it)
                    
                    # Process events to keep UI responsive
                    process_counter += 1
                    if process_counter % 5 == 0:
                        QApplication.processEvents()
                    
                    continue
            except Exception:
                pass

            # If folder contains CRC-named child folders, list those separately
            try:
                children = [d for d in os.listdir(p) if os.path.isdir(os.path.join(p, d))]
            except Exception:
                children = []
            crc_children = [d for d in children if HEX8.match(d)]
            if crc_children:
                for c in sorted(crc_children):
                    pack_dir = os.path.join(p, c)
                    # If CRC child is actually a serial, try to resolve title
                    if SERIAL_RE.search(c) and not HEX8.match(c):
                        serial = c
                        mapping = getattr(self.parent.cheats_tab, 'mapping', {}) or {}
                        title = None
                        kU = serial.upper().strip()
                        if kU in mapping:
                            title = mapping[kU]
                        else:
                            kN = norm_serial_key(kU)
                            if kN in mapping:
                                title = mapping[kN]
                        display = f"{serial} — {title}" if title else f"{name}/{c}"
                    else:
                        display = f"{name}/{c}"
                    thumb = self._make_thumbnail(pack_dir, display.replace(os.sep, '_'))
                    title_col = ""
                    try:
                        if SERIAL_RE.search(c) and not HEX8.match(c):
                            kU = c.upper().strip()
                            mapping = getattr(self.parent.cheats_tab, 'mapping', {}) or {}
                            title_col = mapping.get(kU) or mapping.get(norm_serial_key(kU)) or ""
                    except Exception:
                        title_col = ""
                    it = QTreeWidgetItem([display, title_col, ""]) 
                    it.setData(0, Qt.UserRole, pack_dir)
                    tt = pack_dir
                    try:
                        if title_col:
                            tt = f"{pack_dir}\n{title_col}"
                    except Exception:
                        pass
                    it.setToolTip(0, tt)
                    # Use the thumbnail if it was successfully created
                    if thumb and os.path.isfile(thumb):
                        it.setIcon(0, QIcon(thumb))
                    else:
                        it.setIcon(0, QIcon())
                    try:
                        logger.debug(f"[TexturesTab] adding crc-child item: display='{display}' title_col='{title_col}' path='{pack_dir}'")
                    except Exception:
                        pass
                    self.packs_list.addTopLevelItem(it)
                    
                    # Process events to keep UI responsive
                    process_counter += 1
                    if process_counter % 5 == 0:
                        QApplication.processEvents()
                    
                continue

            # If the folder (or a single nested child) contains images, treat it as a pack
            target = p
            # collapse single-child chains up to depth 3
            for _ in range(3):
                try:
                    subs = [d for d in os.listdir(target) if os.path.isdir(os.path.join(target, d))]
                except Exception:
                    subs = []
                if len(subs) == 1 and not contains_images(target):
                    target = os.path.join(target, subs[0])
                else:
                    break

            if contains_images(target):
                # display name should be relative to base to help identify nested packs
                display = os.path.relpath(target, base)
                # cache key needs safe name (no path separators)
                safe_key = display.replace(os.sep, '_')
                thumb = self._make_thumbnail(target, safe_key)
                # If display looks like a serial, try to resolve title and prefer showing the full title
                if SERIAL_RE.search(os.path.basename(display)) and not HEX8.match(os.path.basename(display)):
                    serial = os.path.basename(display)
                    mapping = getattr(self.parent.cheats_tab, 'mapping', {}) or {}
                    title = None
                    kU = serial.upper().strip()
                    if kU in mapping:
                        title = mapping[kU]
                    else:
                        kN = norm_serial_key(kU)
                        if kN in mapping:
                            title = mapping[kN]
                    item_text = f"{serial} — {title}" if title else serial
                else:
                    item_text = display
                title_col = ""
                try:
                    if SERIAL_RE.search(os.path.basename(display)) and not HEX8.match(os.path.basename(display)):
                        kU = os.path.basename(display).upper().strip()
                        mapping = getattr(self.parent.cheats_tab, 'mapping', {}) or {}
                        title_col = mapping.get(kU) or mapping.get(norm_serial_key(kU)) or ""
                        try:
                            bl = bundled_lookup_title(kU)
                            if bl:
                                title_col = bl
                                try:
                                    self.parent.cheats_tab.mapping[kU] = bl
                                    self.parent.cheats_tab.save_mapping()
                                except Exception:
                                    pass
                        except Exception:
                            pass
                except Exception:
                    title_col = ""
                it = QTreeWidgetItem([item_text, title_col, ""]) 
                it.setData(0, Qt.UserRole, target)
                tt = target
                try:
                    if title_col:
                        tt = f"{target}\n{title_col}"
                except Exception:
                    pass
                it.setToolTip(0, tt)
                # Use the thumbnail if it was successfully created
                if thumb and os.path.isfile(thumb):
                    it.setIcon(0, QIcon(thumb))
                else:
                    it.setIcon(0, QIcon())
                try:
                    logger.debug(f"[TexturesTab] adding image-pack item: display='{item_text}' title_col='{title_col}' path='{target}'")
                except Exception:
                    pass
                self.packs_list.addTopLevelItem(it)
                
                # Process events to keep UI responsive
                process_counter += 1
                if process_counter % 5 == 0:
                    QApplication.processEvents()

        # After full listing, print diagnostics and resolve titles for items missing Title column
        try:
            # If we detected serial/CRC child folders, remove any top-level item that points to the base folder
            try:
                if has_serial_children:
                    # remove matching items (iterate backwards)
                    for idx in range(self.packs_list.topLevelItemCount()-1, -1, -1):
                        itm = self.packs_list.topLevelItem(idx)
                        try:
                            pth = itm.data(0, Qt.UserRole)
                            if isinstance(pth, str) and os.path.abspath(pth) == os.path.abspath(base):
                                try:
                                    logger.debug(f"[TexturesTab] removing base-as-pack item at index {idx} (path='{pth}')")
                                except Exception:
                                    pass
                                self.packs_list.takeTopLevelItem(idx)
                        except Exception:
                            continue
            except Exception:
                pass
            # Diagnostic: enumerate all items we added so we can see what display/title/path they have
            try:
                for i in range(self.packs_list.topLevelItemCount()):
                    itm = self.packs_list.topLevelItem(i)
                    if not itm:
                        continue
                    disp = itm.text(0) or ''
                    title_col = itm.text(1) or ''
                    data_path = itm.data(0, Qt.UserRole) or ''
                    try:
                        logger.debug(f"[TexturesTab] item #{i}: display='{disp}' title_col='{title_col}' path='{data_path}'")
                    except Exception:
                        pass
            except Exception:
                pass

            keys = []
            item_map = {}
            for i in range(self.packs_list.topLevelItemCount()):
                itm = self.packs_list.topLevelItem(i)
                if not itm:
                    continue
                title_col = (itm.text(1) or '').strip()
                if title_col:
                    continue
                # try display name then stored path
                display = itm.text(0) or ''
                cand = None
                m = SERIAL_RE.search(display)
                if m:
                    cand = m.group(0).upper().strip()
                else:
                    try:
                        p = itm.data(0, Qt.UserRole)
                        if isinstance(p, str):
                            m2 = SERIAL_RE.search(os.path.basename(p))
                            if m2:
                                cand = m2.group(0).upper().strip()
                    except Exception:
                        cand = None
                if cand:
                    if cand not in keys:
                        keys.append(cand)
                    item_map.setdefault(cand, []).append(itm)

            if keys:
                try:
                    logger.debug(f"[TexturesTab] resolving serial keys: {keys}")
                except Exception:
                    pass
                # Build variants for more robust lookup (with/without hyphen, normalized)
                expanded_keys = []
                for k in keys:
                    if not k: continue
                    k = k.strip()
                    variants = [k, k.upper(), norm_serial_key(k), k.replace('-', ''), k.replace('-', '').upper()]
                    # keep unique preserving order
                    for v in variants:
                        if v and v not in expanded_keys:
                            expanded_keys.append(v)

                try:
                    logger.debug(f"[TexturesTab] expanded resolver keys: {expanded_keys}")
                except Exception:
                    pass

                local_map = getattr(self.parent.cheats_tab, 'mapping', {}) or {}
                # Use bundled local PSXDataCenter lists by default for reliability
                use_bundled = True
                # Respect the user's 'Also try web lookup' checkbox for online fallback
                try_online = False
                try:
                    try_online = bool(getattr(self.parent.cheats_tab, 'chk_online', None) and self.parent.cheats_tab.chk_online.isChecked()) and (requests is not None)
                except Exception:
                    try_online = False
                if try_online:
                    try:
                        logger.debug("[TexturesTab] online lookup enabled for resolver")
                    except Exception:
                        pass

                worker = ResolveWorker(keys=list(expanded_keys), local_map=local_map, use_bundled_lists=use_bundled, try_online=try_online)

                def _on_resolved(out: Dict[str, str]):
                    try:
                        logger.debug(f"[TexturesTab] resolver returned: {out}")
                    except Exception:
                        pass
                    # Collect serials that still need a better lookup (resolver returned placeholders)
                    need_online = []
                    for cand, items in item_map.items():
                        picked = None
                        # Check multiple variant forms in returned map
                        candidates_to_check = [cand, cand.upper(), norm_serial_key(cand), cand.replace('-', ''), cand.replace('-', '').upper()]
                        for ck in candidates_to_check:
                            if not ck: continue
                            if ck in out:
                                picked = out[ck]
                                break
                        # Try CRC-linked entries if present
                        if not picked:
                            for ck in candidates_to_check:
                                if (ck + '_CRC') in out:
                                    crc = out[ck + '_CRC']
                                    if crc in out:
                                        picked = out[crc]
                                        break
                        # If picked is an unhelpful placeholder like 'INFO' or 'TITLE', treat as unresolved
                        if isinstance(picked, str) and picked.strip().upper() in ('INFO', 'TITLE', 'N/A', 'UNKNOWN'):
                            picked = None
                        if picked:
                            for it in items:
                                try:
                                    it.setText(1, picked)
                                    tt = it.toolTip(0) or it.data(0, Qt.UserRole) or ''
                                    it.setToolTip(0, f"{tt}\n{picked}")
                                except Exception:
                                    pass
                            try:
                                self.parent.cheats_tab.mapping[cand.upper()] = picked
                                # persist mapping
                                try:
                                    self.parent.cheats_tab.save_mapping()
                                except Exception:
                                    pass
                            except Exception:
                                pass
                        else:
                            # schedule focused online lookup for this serial if possible
                            try:
                                if SERIAL_RE.search(cand):
                                    need_online.append(cand)
                            except Exception:
                                pass

                    # Kick focused online lookups for unresolved serials (best-effort)
                    if need_online and requests is not None:
                        try:
                            for s in need_online:
                                def make_on_found(serial):
                                    def _on_found(title: str):
                                        try:
                                            # update all items for this serial
                                            for it in item_map.get(serial, []):
                                                try:
                                                    it.setText(1, title)
                                                    tt = it.toolTip(0) or it.data(0, Qt.UserRole) or ''
                                                    it.setToolTip(0, f"{tt}\n{title}")
                                                except Exception:
                                                    pass
                                                # cache in mapping and persist
                                                try:
                                                    self.parent.cheats_tab.mapping[serial.upper()] = title
                                                    try:
                                                        self.parent.cheats_tab.save_mapping()
                                                    except Exception:
                                                        pass
                                                except Exception:
                                                    pass
                                        except Exception:
                                            pass
                                    return _on_found

                                def make_on_failed(serial):
                                    def _on_failed():
                                        try:
                                            logger.debug(f"[TexturesTab] SingleOnlineLookup failed for {serial}")
                                            # Automatic-only mode: do not prompt the user. Leave unresolved.
                                            try:
                                                pass
                                            except Exception:
                                                pass
                                        except Exception:
                                            pass
                                    return _on_failed

                                worker2 = SingleOnlineLookup(s, parent=self)
                                worker2.found.connect(make_on_found(s))
                                worker2.failed.connect(make_on_failed(s))
                                self._start_worker(worker2)
                        except Exception:
                            pass

                worker.resolved.connect(_on_resolved)
                try:
                    self.parent.cheats_tab._start_worker(worker)
                except Exception:
                    self._start_worker(worker)
        except Exception:
            pass

    def _make_thumbnail(self, pack_dir: str, key: str) -> Optional[str]:
        """Create or reuse a thumbnail image path for a pack. Returns path to thumbnail file usable by QIcon."""
        try:
            # Sanitize the key to create a valid filename
            # Remove or replace characters that are invalid in filenames
            import re
            # Replace invalid filename characters with underscore
            safe_key = re.sub(r'[<>:"/\\|?*◆♦●○■□▲△▼▽◇◆★☆]', '_', key)
            # Also replace em-dash and other special dashes with regular dash
            safe_key = safe_key.replace('—', '-').replace('–', '-')
            # Remove any remaining non-ASCII characters that might cause issues
            safe_key = safe_key.encode('ascii', 'ignore').decode('ascii')
            # Remove multiple consecutive underscores
            safe_key = re.sub(r'_+', '_', safe_key)
            # Remove leading/trailing underscores and spaces
            safe_key = safe_key.strip('_ ')
            
            cache_file = os.path.join(self._thumb_cache, f"{safe_key}.png")
            # Check if cached thumbnail exists - if so, use it (performance optimization)
            # Only regenerate if cache is missing or very old (>30 days)
            if os.path.isfile(cache_file):
                try:
                    cache_age = time.time() - os.path.getmtime(cache_file)
                    if cache_age < (30 * 24 * 3600):  # Less than 30 days old
                        return cache_file
                except Exception:
                    # If we can't check age, just use the cache
                    return cache_file
            
            # find first suitable image
            exts = ('.png', '.jpg', '.jpeg', '.bmp', '.tga')
            for root, _, files in os.walk(pack_dir):
                for f in files:
                    if f.lower().endswith(exts):
                        src = os.path.join(root, f)
                        try:
                            pix = QPixmap(src)
                            if pix and not pix.isNull():
                                try:
                                    # don't upscale small images; only scale down to max 64
                                    maxd = 64
                                    w = pix.width()
                                    h = pix.height()
                                    scale = 1.0
                                    if max(w, h) > maxd:
                                        scale = float(maxd) / float(max(w, h))
                                    tw = max(1, int(w * scale))
                                    th = max(1, int(h * scale))
                                    scaled = pix.scaled(tw, th, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                                    scaled.save(cache_file, 'PNG')
                                    return cache_file
                                except Exception:
                                    pass
                        except Exception:
                            continue
                break
            
            # If no images found, create a placeholder thumbnail
            try:
                # Create a 64x64 placeholder with a folder icon or neutral color
                placeholder = QPixmap(64, 64)
                placeholder.fill(Qt.transparent)
                
                # Draw a simple folder/texture icon
                painter = QPainter(placeholder)
                try:
                    painter.setRenderHint(QPainter.Antialiasing)
                    
                    # Draw a folder-like shape
                    painter.setPen(QPen(QColor(150, 150, 150), 2))
                    painter.setBrush(QColor(200, 200, 200, 180))
                    painter.drawRoundedRect(8, 16, 48, 40, 4, 4)
                    
                    # Draw a tab
                    painter.setBrush(QColor(180, 180, 180, 180))
                    painter.drawRoundedRect(8, 12, 24, 8, 2, 2)
                    
                    # Draw texture pattern (small squares)
                    painter.setPen(Qt.NoPen)
                    painter.setBrush(QColor(120, 120, 120, 100))
                    for i in range(3):
                        for j in range(3):
                            painter.drawRect(16 + i*12, 24 + j*12, 8, 8)
                finally:
                    # Always end the painter, even if drawing fails
                    painter.end()
                
                # Save placeholder
                placeholder.save(cache_file, 'PNG')
                return cache_file
            except Exception as e:
                try:
                    logger.debug(f"[TexturesTab] placeholder generation failed: {e}")
                except Exception:
                    pass
            
            return None
        except Exception:
            return None

    def _find_replacements_in_tree(self, start: str) -> Optional[str]:
        """Recursively search for a 'replacements' folder or the first folder that contains images.
        Returns the path to the folder that should be used as the pack root (replacements) or None."""
        exts = ('.png', '.jpg', '.jpeg', '.bmp', '.tga')
        # If start itself contains images, prefer it
        for root, dirs, files in os.walk(start):
            for f in files:
                if f.lower().endswith(exts):
                    return root
            # prefer a direct 'replacements' folder if found among children
            for d in list(dirs):
                if d.lower() == 'replacements':
                    cand = os.path.join(root, d)
                    # ensure it contains images
                    for _, __, files2 in os.walk(cand):
                        for f2 in files2:
                            if f2.lower().endswith(exts):
                                return cand
        return None

    def _install_selected_pack(self):
        sel = self.packs_list.selectedItems()
        if not sel:
            return
        # resolve source path from selected item
        src = sel[0].data(0, Qt.UserRole)
        base = self.textures_dir.text().strip()
        if not base or not os.path.isdir(base):
            QMessageBox.warning(self, "Missing textures folder", "Please set a valid PCSX2 textures folder path.")
            return
        # Determine source folder: if selected item is a zip file, extract to a temp dir
        temp_dir = None
        try:
            if os.path.isfile(src) and src.lower().endswith('.zip'):
                temp_dir = os.path.join(self._thumb_cache, "_zip_extract")
                if os.path.exists(temp_dir): shutil.rmtree(temp_dir)
                os.makedirs(temp_dir, exist_ok=True)
                with zipfile.ZipFile(src, 'r') as z:
                    z.extractall(temp_dir)
                src_folder = temp_dir
            else:
                src_folder = src

            # Find replacements or image root inside src_folder
            chosen = None
            # direct replacements subfolder
            repl = os.path.join(src_folder, 'replacements')
            if os.path.isdir(repl):
                chosen = self._find_replacements_in_tree(repl) or repl
            else:
                chosen = self._find_replacements_in_tree(src_folder) or src_folder

            if not chosen:
                QMessageBox.information(self, "No images", "Could not find replacement images inside the selected pack.")
                return

            # Determine target folder name: prefer detected Serial code where possible
            display = sel[0].text(0)
            # Try to extract a serial from the display text first
            m = SERIAL_RE.search(display or '')
            serial_candidate = m.group(0).upper() if m else None
            # Also scan chosen folder for serial-like names
            if not serial_candidate:
                for root, dirs, files in os.walk(chosen):
                    for nm in dirs + files:
                        mm = SERIAL_RE.search(nm)
                        if mm:
                            serial_candidate = mm.group(0).upper()
                            break
                    if serial_candidate:
                        break

            if self.target_folder_name.text().strip():
                target_name = self.target_folder_name.text().strip()
            elif serial_candidate:
                target_name = serial_candidate
            else:
                # fallback to basename of display
                target_name = os.path.basename(display)
            if '/' in target_name or '\\' in target_name:
                target_name = os.path.basename(target_name)

            dst = os.path.join(base, target_name)
            # copy chosen content into dst (replace if exists)
            if os.path.exists(dst):
                if QMessageBox.question(self, 'Overwrite', f'Target exists: {dst}\nReplace it?') != QMessageBox.StandardButton.Yes:
                    return
                shutil.rmtree(dst)
            shutil.copytree(chosen, dst)
            QMessageBox.information(self, 'Installed', f'Installed pack into:\n{dst}')
            self.scan_installed_textures()
        except Exception as e:
            QMessageBox.critical(self, 'Install failed', str(e))
        finally:
            if temp_dir and os.path.exists(temp_dir):
                try: shutil.rmtree(temp_dir)
                except Exception: pass

    def _open_selected_pack(self):
        sel = self.packs_list.selectedItems()
        if not sel:
            return
        p = sel[0].data(0, Qt.UserRole)
        if os.name == 'nt':
            subprocess.Popen(['explorer', os.path.normpath(p)])
        else:
            subprocess.Popen(['xdg-open', p])

    def _show_matched_for_selected(self):
        sel = self.packs_list.selectedItems()
        if not sel:
            return
        it = sel[0]
        # matched HTML stored in UserRole+1
        try:
            snippet = it.data(0, Qt.UserRole + 1) or ''
        except Exception:
            snippet = ''
        dlg = QDialog(self)
        dlg.setWindowTitle('Matched HTML for pack')
        v = QVBoxLayout(dlg)
        txt = QTextEdit()
        txt.setReadOnly(True)
        txt.setPlainText(snippet or '(no matched HTML stored)')
        v.addWidget(txt)
        b = QPushButton('Close')
        b.clicked.connect(dlg.accept)
        v.addWidget(b)
        dlg.exec()

    def _resolve_all_packs(self):
        # Collect unresolved serials from the list and run ResolveWorker in bulk
        keys = []
        item_map = {}
        for i in range(self.packs_list.topLevelItemCount()):
            itm = self.packs_list.topLevelItem(i)
            if not itm: continue
            title_col = (itm.text(1) or '').strip()
            if title_col:
                continue
            display = itm.text(0) or ''
            cand = None
            m = SERIAL_RE.search(display)
            if m:
                cand = m.group(0).upper().strip()
            else:
                try:
                    p = itm.data(0, Qt.UserRole)
                    if isinstance(p, str):
                        m2 = SERIAL_RE.search(os.path.basename(p))
                        if m2:
                            cand = m2.group(0).upper().strip()
                except Exception:
                    cand = None
            if cand:
                if cand not in keys:
                    keys.append(cand)
                item_map.setdefault(cand, []).append(itm)

        if not keys:
            QMessageBox.information(self, 'Resolve All', 'No unresolved serial-named packs found.')
            return

        # Prepare expanded variants
        expanded = []
        for k in keys:
            if not k: continue
            variants = [k, k.upper(), norm_serial_key(k), k.replace('-', ''), k.replace('-', '').upper()]
            for v in variants:
                if v and v not in expanded:
                    expanded.append(v)

        local_map = getattr(self.parent.cheats_tab, 'mapping', {}) or {}
        use_bundled = True
        try_online = False
        try:
            try_online = bool(getattr(self.parent.cheats_tab, 'chk_online', None) and self.parent.cheats_tab.chk_online.isChecked()) and (requests is not None)
        except Exception:
            try_online = False

        worker = ResolveWorker(keys=list(expanded), local_map=local_map, use_bundled_lists=use_bundled, try_online=try_online)

        def _on_resolved(out: Dict[str, str]):
            try:
                logger.debug(f"[TexturesTab.resolve_all] resolver returned: {out}")
            except Exception:
                pass
            for cand, items in item_map.items():
                picked = None
                html_snip = None
                candidates_to_check = [cand, cand.upper(), norm_serial_key(cand), cand.replace('-', ''), cand.replace('-', '').upper()]
                for ck in candidates_to_check:
                    if not ck: continue
                    if ck in out:
                        picked = out[ck]
                        break
                if not picked:
                    for ck in candidates_to_check:
                        if (ck + '_CRC') in out:
                            crc = out[ck + '_CRC']
                            if crc in out:
                                picked = out[crc]
                                break
                # Grab html snippet if provided
                for ck in candidates_to_check:
                    key_html = ck + '_HTML'
                    if key_html in out:
                        html_snip = out[key_html]
                        break

                if isinstance(picked, str) and picked.strip().upper() in ('INFO', 'TITLE', 'N/A', 'UNKNOWN'):
                    picked = None

                if picked:
                    for it in items:
                        try:
                            it.setText(1, picked)
                            tt = it.toolTip(0) or it.data(0, Qt.UserRole) or ''
                            it.setToolTip(0, f"{tt}\n{picked}")
                            if html_snip:
                                it.setData(0, Qt.UserRole + 1, html_snip)
                        except Exception:
                            pass
                    try:
                        self.parent.cheats_tab.mapping[cand.upper()] = picked
                        try:
                            self.parent.cheats_tab.save_mapping()
                        except Exception:
                            pass
                    except Exception:
                        pass
                else:
                    # leave unresolved
                    pass

        worker.resolved.connect(_on_resolved)
        try:
            self.parent.cheats_tab._start_worker(worker)
        except Exception:
            self._start_worker(worker)

    def _remove_selected_pack(self):
        sel = self.packs_list.selectedItems()
        if not sel:
            return
        p = sel[0].data(0, Qt.UserRole)
        ok = QMessageBox.question(self, 'Remove Pack', f'Remove installed pack folder?\n{p}')
        if ok == QMessageBox.StandardButton.Yes:
            try:
                shutil.rmtree(p)
                self.scan_installed_textures()
            except Exception as e:
                QMessageBox.critical(self, 'Remove failed', str(e))

    def _suggest_crc_from_logs(self) -> Optional[str]:
        # Read emuLog.txt for last CRC = 0xXXXXXXXX
        logs_dir = self.parent.settings_tab.paths.get("logs", "")
        if not logs_dir or not os.path.isdir(logs_dir):
            return None
        log_path = os.path.join(logs_dir, 'emuLog.txt')
        if not os.path.isfile(log_path):
            # try recent logs
            candidates = [os.path.join(logs_dir, f) for f in os.listdir(logs_dir) if f.lower().endswith('.txt')]
            candidates.sort(key=lambda p: os.path.getmtime(p), reverse=True)
            for p in candidates:
                m = self._scan_crc_in_file(p)
                if m: return m
            return None
        return self._scan_crc_in_file(log_path)

    @staticmethod
    def _scan_crc_in_file(p: str) -> Optional[str]:
        try:
            with open(p, 'r', encoding='utf-8', errors='replace') as f:
                for line in f:
                    m = re.search(r"CRC\s*=\s*0x([0-9A-Fa-f]{8})", line)
                    if m: return m.group(1).upper()
        except Exception:
            return None
        return None


def ini_get_bool(ini_path: str, key: str, section: str) -> Optional[bool]:
    """Read a true/false value from a PCSX2 .ini (None if the file, section or
    key isn't there). PCSX2 writes bools as the literal strings true/false."""
    try:
        in_section = False
        with open(ini_path, 'r', encoding='utf-8', errors='replace') as f:
            for raw in f:
                line = raw.strip()
                if line.startswith('[') and line.endswith(']'):
                    in_section = line[1:-1].strip().lower() == section.lower()
                elif in_section and '=' in line:
                    k, v = line.split('=', 1)
                    if k.strip().lower() == key.lower():
                        return v.strip().lower() == 'true'
    except OSError:
        return None
    return None


def ini_set_bool(ini_path: str, key: str, val: bool, section: str) -> bool:
    """Write a bool the way PCSX2 itself does -- confirmed against its actual
    write path (INISettingsInterface::SetBoolValue -> SimpleIni), which always
    serializes bools as the literal strings "true"/"false", never 1/0 or
    enabled/disabled, under a proper [Section] header. Anything else produces a
    line current PCSX2 simply doesn't recognize. Returns False if the file
    can't be read or written."""
    try:
        if not os.path.isfile(ini_path):
            return False
        with open(ini_path, 'r', encoding='utf-8', errors='replace') as f:
            lines = f.readlines()
        target = f"{key} = {'true' if val else 'false'}\n"
        header = f"[{section}]"
        start, end = None, len(lines)
        for i, line in enumerate(lines):
            if line.strip().lower() == header.lower():
                start = i
                for j in range(i + 1, len(lines)):
                    s = lines[j].strip()
                    if s.startswith('[') and s.endswith(']'):
                        end = j
                        break
                break
        if start is None:
            if lines and not lines[-1].endswith('\n'):
                lines[-1] += '\n'
            lines += [f"\n{header}\n", target]
        else:
            for i in range(start + 1, end):
                k = lines[i].split('=', 1)[0].strip().lower()
                if '=' in lines[i] and k == key.lower():
                    lines[i] = target
                    break
            else:
                lines.insert(end, target)
        with open(ini_path, 'w', encoding='utf-8') as f:
            f.writelines(lines)
        return True
    except OSError:
        return False


# (label, tooltip, ini section, ini key) -- what the Settings "PCSX2 options" checkboxes control
PCSX2_OPTIONS = [
    ("Enable cheats", "Lets PCSX2 load the cheat files this app installs.", "EmuCore", "EnableCheats"),
    ("Enable widescreen patches", "Applies each game's 'Widescreen 16:9' patch (installed with Patches & Fixes).",
     "EmuCore", "EnableWideScreenPatches"),
    ("Enable no-interlacing patches", "Applies each game's 'No-Interlacing' patch where one exists.",
     "EmuCore", "EnableNoInterlacingPatches"),
    ("Enable texture replacement", "Lets PCSX2 load the texture packs this app installs.",
     "EmuCore/GS", "LoadTextureReplacements"),
    ("Dump textures while playing",
     "PCSX2 saves a game's original textures to disk as you play it, so the Install dialog can AI-upscale "
     "them (Real-ESRGAN) when no ready-made pack exists. Turn off again afterwards -- it slows PCSX2 down.",
     "EmuCore/GS", "DumpReplaceableTextures"),
]


class SettingsTab(QWidget):
    """Where PCSX2 is and which of its options this app relies on.

    The checkboxes are not stored by this app: they read and write PCSX2's own
    PCSX2.ini, so what you see is what PCSX2 will actually do.
    """

    def __init__(self, parent: 'MainWindow'):
        super().__init__()
        self.parent = parent
        self.paths: Dict[str, str] = {}
        self._build_ui()
        self._load_settings()

    # ---- UI ----
    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(theme.SPACING_LG, theme.SPACING_LG, theme.SPACING_LG, theme.SPACING_LG)
        layout.setSpacing(theme.SPACING_MD)

        grp = QGroupBox("PCSX2")
        form = QFormLayout(grp)
        form.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)

        self.pcsx2_exe = QLineEdit()
        self.pcsx2_exe.setPlaceholderText("Optional -- only needed to launch games from here")
        self.pcsx2_exe.editingFinished.connect(self._save_pcsx2_exe)
        btn_exe = QPushButton("Browse…")
        btn_exe.clicked.connect(self._browse_exe)
        form.addRow("Program:", self._row(self.pcsx2_exe, btn_exe))

        self.user_dir = QLineEdit()
        self.user_dir.setPlaceholderText("Folder that contains PCSX2's cheats, patches, textures and inis")
        self.user_dir.editingFinished.connect(self._update_subs)
        btn_dir = QPushButton("Browse…")
        btn_dir.clicked.connect(self._browse)
        btn_detect = QPushButton("Detect")
        btn_detect.setToolTip("Find PCSX2's data folder automatically")
        btn_detect.clicked.connect(self._detect)
        btn_open = QPushButton("Open")
        btn_open.setToolTip("Show the data folder in Explorer")
        btn_open.clicked.connect(self._open_user_dir)
        form.addRow("Data folder:", self._row(self.user_dir, btn_dir, btn_detect, btn_open))

        self.folder_label = QLabel("")
        self.folder_label.setWordWrap(True)
        form.addRow("", self.folder_label)
        layout.addWidget(grp)

        opts = QGroupBox("PCSX2 options")
        ol = QVBoxLayout(opts)
        self.option_boxes: Dict[Tuple[str, str], QCheckBox] = {}
        for label, tip, section, key in PCSX2_OPTIONS:
            cb = QCheckBox(label)
            cb.setToolTip(tip)
            cb.toggled.connect(lambda on, s=section, k=key: self._set_option(s, k, on))
            self.option_boxes[(section, key)] = cb
            ol.addWidget(cb)
        self.options_note = QLabel("")
        self.options_note.setObjectName(theme.OBJ_MUTED_LABEL)
        self.options_note.setWordWrap(True)
        ol.addWidget(self.options_note)
        layout.addWidget(opts)
        layout.addStretch(1)

    def _row(self, *widgets):
        """A line edit followed by its buttons: the edit takes the spare width."""
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        for wd in widgets:
            h.addWidget(wd, 1 if isinstance(wd, QLineEdit) else 0)
        return w

    # ---- PCSX2 program ----
    def _browse_exe(self):
        flt = "Programs (*.exe);;All files (*)" if IS_WINDOWS else "All files (*)"
        p, _ = QFileDialog.getOpenFileName(self, "Select the PCSX2 program", self.pcsx2_exe.text() or os.path.expanduser("~"), flt)
        if p:
            self.pcsx2_exe.setText(p)
            self._save_pcsx2_exe()
            self._offer_portable_switch(p)

    def _save_pcsx2_exe(self):
        app_settings().setValue('paths/pcsx2_exe', self.pcsx2_exe.text().strip())

    def _offer_portable_switch(self, pcsx2_exe: str):
        """If the PCSX2 exe just picked has a portable.ini next to it, that's the
        *real* data folder (portable installs never touch Documents\\PCSX2) --
        offer to switch to it rather than silently leaving a possibly-wrong
        folder configured, which would make installs write files PCSX2 never sees."""
        exe_dir = os.path.dirname(os.path.abspath(pcsx2_exe))
        if not os.path.isfile(os.path.join(exe_dir, "portable.ini")):
            return
        current = os.path.abspath(self.user_dir.text().strip()) if self.user_dir.text().strip() else ""
        if os.path.normcase(current) == os.path.normcase(exe_dir):
            return
        reply = QMessageBox.question(
            self, "Portable PCSX2 detected",
            "This looks like a portable PCSX2 install (portable.ini is next to the program).\n\n"
            "Portable installs keep their cheats, patches and textures next to the program, not in "
            f"Documents\\PCSX2. Use this folder instead?\n\n{exe_dir}",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
        )
        if reply == QMessageBox.Yes:
            self.user_dir.setText(exe_dir)
            self._update_subs()

    # ---- data folder ----
    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, "Select PCSX2's data folder", self.user_dir.text() or os.path.expanduser("~"))
        if d:
            self.user_dir.setText(d)
            self._update_subs()

    def _detect(self):
        guesses = default_pcsx2_user_dirs(self.pcsx2_exe.text().strip())
        if not guesses:
            QMessageBox.information(self, "Data folder", "Couldn't find PCSX2's data folder. Start PCSX2 once so it "
                                    "creates it, or choose the folder yourself.")
            return
        self.user_dir.setText(guesses[0])
        self._update_subs()

    def _open_user_dir(self):
        d = self.user_dir.text().strip()
        if d and os.path.isdir(d):
            QDesktopServices.openUrl(QUrl.fromLocalFile(d))

    def _ini_path(self) -> str:
        return os.path.join(self.paths.get('inis', ''), 'PCSX2.ini')

    def _update_subs(self):
        base = self.user_dir.text().strip()
        self.paths = ensure_subdirs(base) if base else {}
        self.parent.state.pcsx2_paths = self.paths
        self._refresh_folder_status()
        self._refresh_options()
        app_settings().setValue('paths/user_dir', base)
        self.parent.paths_changed.emit(self.paths)

    def folder_status(self) -> Tuple[str, str]:
        """(message, object-name) describing the configured data folder."""
        base = self.user_dir.text().strip()
        if not base:
            text, kind = "Choose PCSX2's data folder to install cheats, patches and textures.", theme.OBJ_STATUS_WARNING
        elif not os.path.isdir(base):
            text, kind = "That folder doesn't exist.", theme.OBJ_STATUS_ERROR
        elif os.path.isfile(self._ini_path()):
            text, kind = "PCSX2 found. Installed files go into its cheats, patches and textures folders.", theme.OBJ_STATUS_SUCCESS
        else:
            text, kind = ("No inis/PCSX2.ini in this folder yet. Installing still works, but start PCSX2 once "
                          "so the options below can be changed.", theme.OBJ_STATUS_WARNING)
        return text, kind

    def _refresh_folder_status(self):
        text, kind = self.folder_status()
        self.folder_label.setText(text)
        self.folder_label.setObjectName(kind)
        self.folder_label.style().unpolish(self.folder_label)
        self.folder_label.style().polish(self.folder_label)

    # ---- PCSX2.ini options ----
    def _refresh_options(self):
        ini = self._ini_path()
        have_ini = os.path.isfile(ini)
        for (section, key), cb in self.option_boxes.items():
            cb.blockSignals(True)
            cb.setChecked(bool(have_ini and ini_get_bool(ini, key, section)))
            cb.setEnabled(have_ini)
            cb.blockSignals(False)
        self.options_note.setText(
            "Close PCSX2 before changing these -- it rewrites its settings file when it exits." if have_ini
            else "Available once PCSX2.ini exists (start PCSX2 once).")

    def _set_option(self, section: str, key: str, on: bool):
        if not ini_set_bool(self._ini_path(), key, on, section):
            QMessageBox.warning(self, "PCSX2 options", "Couldn't write PCSX2.ini. Is the data folder correct?")
            self._refresh_options()

    # ---- persistence ----
    def _load_settings(self):
        s = app_settings()
        exe = s.value('paths/pcsx2_exe', '', str)
        if exe:
            self.pcsx2_exe.setText(exe)
        saved_dir = s.value('paths/user_dir', '', str)
        if saved_dir:
            self.user_dir.setText(saved_dir)
            self._update_subs()
        else:
            guesses = default_pcsx2_user_dirs(exe)
            if guesses:
                self.user_dir.setText(guesses[0])
            self._update_subs()


class ResizableFrame(QWidget):
    """Outer frame for the frameless MainWindow. On Windows the real window stays
    fully native (see MainWindow.nativeEvent/_win_hit_test) -- WM_NCHITTEST already
    handles resize-edge detection and the OS sets its own resize cursors, so this
    widget's manual mouseMoveEvent/setCursor logic below must stay OFF there: with
    both systems active at once, this one's cursor overrides would occasionally
    "win" the race over stray hover events (e.g. the thin exposed-background strips
    the layout margins leave between the title bar/library view and the real window
    edge), showing a resize cursor over ordinary content with no way to clear it.
    Only used as the fallback resize mechanism on non-Windows platforms, where the
    window really is FramelessWindowHint and nothing else handles this.
    """

    MARGIN = theme.RESIZE_MARGIN

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName(theme.OBJ_APP_FRAME)
        if not IS_WINDOWS:
            self.setMouseTracking(True)

    def _edge_at(self, pos):
        r = self.rect()
        m = self.MARGIN
        edge = Qt.Edge(0)
        if pos.y() <= m:
            edge |= Qt.TopEdge
        if pos.y() >= r.height() - m:
            edge |= Qt.BottomEdge
        if pos.x() <= m:
            edge |= Qt.LeftEdge
        if pos.x() >= r.width() - m:
            edge |= Qt.RightEdge
        return edge if edge else None

    _CURSORS = {
        Qt.TopEdge: Qt.SizeVerCursor,
        Qt.BottomEdge: Qt.SizeVerCursor,
        Qt.LeftEdge: Qt.SizeHorCursor,
        Qt.RightEdge: Qt.SizeHorCursor,
        Qt.TopEdge | Qt.LeftEdge: Qt.SizeFDiagCursor,
        Qt.BottomEdge | Qt.RightEdge: Qt.SizeFDiagCursor,
        Qt.TopEdge | Qt.RightEdge: Qt.SizeBDiagCursor,
        Qt.BottomEdge | Qt.LeftEdge: Qt.SizeBDiagCursor,
    }

    def mousePressEvent(self, e):
        if IS_WINDOWS:
            super().mousePressEvent(e)
            return
        if e.button() == Qt.LeftButton:
            edge = self._edge_at(e.position().toPoint())
            if edge:
                wh = self.window().windowHandle()
                if wh:
                    wh.startSystemResize(edge)
                    return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if IS_WINDOWS:
            super().mouseMoveEvent(e)
            return
        edge = self._edge_at(e.position().toPoint())
        self.setCursor(self._CURSORS.get(edge, Qt.ArrowCursor))
        super().mouseMoveEvent(e)


class TitleBar(QWidget):
    """Custom-drawn, draggable title bar replacing the native OS chrome. Holds
    the app icon and name, the menu bar (so Classic mode doesn't spend a second
    row on it), and the window controls."""

    def __init__(self, window: 'MainWindow'):
        super().__init__(window)
        self._window = window
        self.setObjectName(theme.OBJ_TITLE_BAR)
        self.setFixedHeight(theme.TITLE_BAR_HEIGHT)
        self.setMouseTracking(True)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 0, 6, 1)
        layout.setSpacing(8)

        # Everything in the bar is centred on the same line: QMenuBar draws its
        # items from its own top edge, so it must not be stretched to the bar's
        # full height (that is what left the menus sitting higher than the name).
        icon_label = QLabel()
        icon_label.setPixmap(QIcon(APP_ICON_ICO_PATH).pixmap(18, 18))
        layout.addWidget(icon_label, 0, Qt.AlignVCenter)

        title_label = QLabel(APP_NAME)
        title_label.setObjectName(theme.OBJ_TITLE_BAR_LABEL)
        layout.addWidget(title_label, 0, Qt.AlignVCenter)
        layout.addSpacing(6)

        self.menu_bar = QMenuBar()
        self.menu_bar.setNativeMenuBar(False)
        self.menu_bar.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        layout.addWidget(self.menu_bar, 0, Qt.AlignVCenter)

        layout.addStretch(1)

        self.btn_min = self._make_button("minimize", "Minimize")
        self.btn_min.clicked.connect(self._window.showMinimized)
        layout.addWidget(self.btn_min)

        self.btn_max = self._make_button("maximize", "Maximize")
        self.btn_max.clicked.connect(self._toggle_maximize)
        layout.addWidget(self.btn_max)

        self.btn_close = self._make_button("close", "Close")
        self.btn_close.setObjectName(theme.OBJ_TITLE_BAR_CLOSE_BUTTON)
        self.btn_close.clicked.connect(self._window.close)
        layout.addWidget(self.btn_close)

    def _make_button(self, icon_name, tooltip):
        b = QToolButton()
        b.setIcon(icons.tab_icon(icon_name))
        b.setToolTip(tooltip)
        b.setObjectName(theme.OBJ_TITLE_BAR_BUTTON)
        b.setFixedSize(32, 24)
        b.setCursor(Qt.ArrowCursor)
        return b

    def _toggle_maximize(self):
        if self._window.isMaximized():
            self._window.showNormal()
            self.btn_max.setIcon(icons.tab_icon("maximize"))
        else:
            self._window.showMaximized()
            self.btn_max.setIcon(icons.tab_icon("restore"))

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            wh = self._window.windowHandle()
            if wh:
                if e.position().y() <= theme.RESIZE_MARGIN:
                    wh.startSystemResize(Qt.TopEdge)
                else:
                    wh.startSystemMove()
                return
        super().mousePressEvent(e)

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._toggle_maximize()
        super().mouseDoubleClickEvent(e)


def read_iso_serial(path: str) -> Optional[str]:
    """Read the PS2 serial directly out of an ISO9660 disc image's SYSTEM.CNF
    (the boot file every PS2 disc has, containing e.g. "BOOT2 = cdrom0:\\SLUS_205.46;1").

    This is the fallback for the very common case where the filename doesn't
    embed the serial at all (many "clean" ROM sets are named by title only, e.g.
    "Grand Theft Auto - San Andreas (USA).iso"). Implemented as a minimal,
    read-only ISO9660 primary-volume-descriptor + root-directory walk -- no new
    dependency. Only handles plain 2048-byte-sector .iso images (not raw
    2352-byte .bin or compressed .chd/.cso); returns None on any failure.
    """
    try:
        with open(path, 'rb') as f:
            f.seek(16 * 2048)
            pvd = f.read(2048)
            if len(pvd) < 2048 or pvd[1:6] != b'CD001':
                return None
            root_record = pvd[156:156 + 34]
            root_lba = int.from_bytes(root_record[2:6], 'little')
            root_size = int.from_bytes(root_record[10:14], 'little')
            if root_size <= 0 or root_size > 10 * 1024 * 1024:
                return None

            f.seek(root_lba * 2048)
            root_data = f.read(root_size)
            system_cnf = None
            i = 0
            while i < len(root_data):
                rec_len = root_data[i]
                if rec_len == 0:
                    i = ((i // 2048) + 1) * 2048
                    continue
                if rec_len < 33 or i + rec_len > len(root_data):
                    break
                rec = root_data[i:i + rec_len]
                id_len = rec[32]
                name = rec[33:33 + id_len].decode('ascii', errors='ignore').upper()
                if name.startswith('SYSTEM.CNF'):
                    system_cnf = {
                        'lba': int.from_bytes(rec[2:6], 'little'),
                        'size': int.from_bytes(rec[10:14], 'little'),
                    }
                    break
                i += rec_len

            if not system_cnf or not (0 < system_cnf['size'] <= 65536):
                return None
            f.seek(system_cnf['lba'] * 2048)
            content = f.read(system_cnf['size']).decode('ascii', errors='ignore')
            m = re.search(r'([A-Za-z]{4})_(\d{3})\.(\d{2})', content)
            if m:
                return f"{m.group(1).upper()}-{m.group(2)}{m.group(3)}"
    except Exception as e:
        logger.debug(f"[read_iso_serial] Failed to read {path}: {e}")
    return None


class GameScanWorker(QThread):
    """Walks a folder for PS2 disc images and extracts each one's serial.

    Tries the filename first (fast, e.g. "SLUS-20946 - Title.iso"); if that
    doesn't contain a serial, falls back to reading SYSTEM.CNF directly out of
    plain .iso images via read_iso_serial(). .bin/.chd/.cso files without a
    filename serial are skipped -- reading their content would need raw-sector
    or decompression handling this v1 doesn't have.
    """

    progressed = Signal(int, int)
    finished = Signal(list, int)  # entries, total disc images found (before serial resolution)

    GAME_EXTS = ('.iso', '.bin', '.chd', '.cso')

    def __init__(self, folder: str, parent=None):
        super().__init__(parent)
        self.folder = folder

    def run(self):
        paths = []
        try:
            for root, _dirs, files in os.walk(self.folder):
                for fn in files:
                    if fn.lower().endswith(self.GAME_EXTS):
                        paths.append(os.path.join(root, fn))
        except Exception as e:
            logger.error(f"[GameScanWorker] Failed to walk {self.folder}: {e}")

        total = max(1, len(paths))
        entries: List[GameEntry] = []
        seen = set()

        for i, path in enumerate(paths, 1):
            if self.isInterruptionRequested():
                break
            fname = os.path.basename(path)
            serial = None
            m = SERIAL_RE.search(fname)
            if m:
                serial = m.group(0).upper().replace('_', '-').replace(' ', '-')
            # The disc itself is the authority: its SYSTEM.CNF names the real
            # serial and its boot ELF gives the exact CRC PCSX2 will use.
            info = None if path.lower().endswith('.chd') else disc.read_disc_info(path)
            if info and info.serial:
                serial = info.serial
            if serial and serial not in seen:
                seen.add(serial)
                title = bundled_lookup_title(serial) or os.path.splitext(fname)[0]
                crc = info.crc if info else None
                entries.append(GameEntry(serial=serial, title=title, crc=crc, source_path=path,
                                         crc_source='disc' if crc else None))
            self.progressed.emit(i, total)

        self.finished.emit(entries, len(paths))


def _entry_to_dict(g: GameEntry) -> dict:
    return {"serial": g.serial, "title": g.title, "crc": g.crc, "source_path": g.source_path,
            "crc_source": g.crc_source}


def _dict_to_entry(d: dict) -> GameEntry:
    return GameEntry(
        serial=d.get('serial', ''),
        title=d.get('title', ''),
        crc=d.get('crc'),
        source_path=d.get('source_path'),
        crc_source=d.get('crc_source') or (None if not d.get('crc') else 'database'),
    )


class EnhancementsDialog(QDialog):
    """Classic: everything that makes a game better in PCSX2, in one place.

    Patches & fixes, cheats and texture packs each get a tab showing what's
    available, what's installed and what's switched on; a Recommended button
    picks the safe improvements; a Checks tab lists anything in PCSX2's own
    settings that would stop them from working. Nothing changes until Apply.
    The choices come back as one `selection()` dict, the same shape Big
    Picture sends, so both modes apply through the same code."""

    KIND_ORDER = ("widescreen", "nointerlace", "fix", "fps", "quality", "other")

    def __init__(self, game: 'GameEntry', o: dict, parent=None):
        super().__init__(parent)
        self.o = o
        self.game = game
        self.setWindowTitle(f"Enhancements · {game.title or game.serial}")
        self.resize(760, 640)
        lay = QVBoxLayout(self)
        lay.setSpacing(10)

        # ---- header ----
        head = QLabel(f"<span style='font-size:15pt; font-weight:600'>{html.escape(game.title or game.serial)}</span>")
        lay.addWidget(head)
        crc_note = ("read from your disc image" if game.crc_source == 'disc'
                    else f"from {game.crc_source}" if game.crc else "unknown")
        sub = QLabel(f"{game.serial}  ·  CRC {game.crc or '—'} ({crc_note})")
        sub.setObjectName(theme.OBJ_MUTED_LABEL)
        lay.addWidget(sub)

        # ---- recommended ----
        rec = o["recommendation"]
        rec_box = QFrame()
        rec_box.setObjectName("recommendBox")
        rec_box.setStyleSheet("QFrame#recommendBox { background: #2b3a4a; border: 1px solid #3d5a78; border-radius: 6px; }")
        rl = QHBoxLayout(rec_box)
        rl.setContentsMargins(12, 8, 8, 8)
        rec_text = QLabel("<b>Recommended:</b> " + (html.escape("; ".join(rec.summary)) if rec.summary
                                                    else "nothing extra is needed for this game."))
        rec_text.setWordWrap(True)
        rl.addWidget(rec_text, 1)
        self.btn_rec = QPushButton("Use recommended")
        self.btn_rec.setEnabled(bool(rec.summary))
        self.btn_rec.clicked.connect(self.apply_recommended)
        rl.addWidget(self.btn_rec)
        lay.addWidget(rec_box)

        self.tabs = QTabWidget()
        lay.addWidget(self.tabs, 1)
        self._build_patches_tab()
        self._build_cheats_tab()
        self._build_textures_tab()
        self._build_checks_tab()

        log_btn = QPushButton("What was searched…")
        log_btn.setFlat(True)
        log_btn.clicked.connect(self._show_log)
        bottom = QHBoxLayout()
        bottom.addWidget(log_btn)
        bottom.addStretch(1)
        self.btns = QDialogButtonBox(QDialogButtonBox.Apply | QDialogButtonBox.Cancel)
        self.ok_btn = self.btns.button(QDialogButtonBox.Apply)
        self.ok_btn.setObjectName(theme.OBJ_SUCCESS_BUTTON)
        self.ok_btn.setDefault(True)
        self.ok_btn.clicked.connect(self.accept)
        self.btns.rejected.connect(self.reject)
        bottom.addWidget(self.btns)
        lay.addLayout(bottom)

        # First visit: start from the recommendation. Otherwise show what's on now.
        nothing_installed = not (o["cheats_installed"] or o["textures_installed"] or any(p["installed"] for p in o["patches"]))
        if nothing_installed and rec.summary:
            self.apply_recommended()

    # ---- patches ----
    def _build_patches_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        if not self.o["patches"]:
            v.addWidget(self._note("PCSX2's patch collection has nothing for this game. It may simply not need any."))
            v.addStretch(1)
        else:
            v.addWidget(self._note("Widescreen, no-interlacing and bug-fix patches from PCSX2's own collection. "
                                   "Ticked patches are installed and switched on for this game only."))
            self.patch_tree = QTreeWidget()
            self.patch_tree.setHeaderLabels(["Patch", "Type", "Notes"])
            self.patch_tree.setRootIsDecorated(True)
            self.patch_tree.header().setStretchLastSection(True)
            self.patch_tree.setColumnWidth(0, 300)
            self.patch_tree.setColumnWidth(1, 120)
            for i, p in enumerate(self.o["patches"]):
                pf = p["file"]
                top = QTreeWidgetItem([f"{pf.serial} · CRC {pf.crc}" + ("   (your disc)" if p["exact"] else ""),
                                       "Update available" if p.get("update") else "Installed" if p["installed"] else "",
                                       "Apply to update it" if p.get("update") else ""])
                top.setFirstColumnSpanned(False)
                f = top.font(0)
                f.setBold(True)
                top.setFont(0, f)
                self.patch_tree.addTopLevelItem(top)
                if p["always"]:
                    it = QTreeWidgetItem(["Always-on fixes", "Game fix", "Applied whenever this file is installed"])
                    it.setData(0, Qt.UserRole, (i, "*"))
                    it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
                    it.setCheckState(0, Qt.Checked if p["installed"] else Qt.Unchecked)
                    top.addChild(it)
                for g in sorted(p["groups"], key=lambda g: self.KIND_ORDER.index(g.kind)):
                    it = QTreeWidgetItem([g.name, enhancements.KIND_LABEL[g.kind] + ("  ★" if g.recommended else ""),
                                          g.description or (f"by {g.author}" if g.author else "")])
                    it.setToolTip(2, g.description)
                    it.setData(0, Qt.UserRole, (i, g.name))
                    it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
                    it.setCheckState(0, Qt.Checked if g.enabled else Qt.Unchecked)
                    top.addChild(it)
                top.setExpanded(p["exact"] or len(self.o["patches"]) == 1)
            v.addWidget(self.patch_tree, 1)
            legend = self._note("★ recommended.  Frame-rate unlocks and visual tweaks are offered but not recommended: "
                                "they can change how fast the game runs or how it looks.")
            v.addWidget(legend)
            if any(p["installed"] for p in self.o["patches"]):
                self.remove_patches = QCheckBox("Remove the installed patch files for this game")
                v.addWidget(self.remove_patches)
        self.tabs.addTab(w, icons.tab_icon("download"), "Patches && Fixes")

    # ---- cheats ----
    def _build_cheats_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        cheats = self.o["cheats"]
        self.cheats_list = QListWidget()
        self.cheats_overwrite_cb = None
        if not cheats:
            v.addWidget(self._note("No cheats were found for this game in any source."))
            v.addStretch(1)
        else:
            v.addWidget(self._note(f"{len(cheats)} cheat(s) {self.o['cheat_note']}. All of them are written to the "
                                   "cheat file; only the ticked ones are switched on, and you can change that later "
                                   "here or in PCSX2."))
            row = QHBoxLayout()
            self.cheat_filter = QLineEdit()
            self.cheat_filter.setPlaceholderText("Filter cheats…")
            self.cheat_filter.textChanged.connect(self._filter_cheats)
            row.addWidget(self.cheat_filter, 1)
            for label, state in (("All", True), ("None", False)):
                b = QPushButton(label)
                b.clicked.connect(lambda _c=False, s=state: self._set_all_cheats(s))
                row.addWidget(b)
            v.addLayout(row)
            for c in cheats:
                it = QListWidgetItem(c.get("name") or "Cheat")
                it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
                it.setCheckState(Qt.Checked if c.get("enabled") else Qt.Unchecked)
                it.setToolTip("\n".join(x for x in (c.get("description"), f"Source: {c.get('source', '')}") if x))
                self.cheats_list.addItem(it)
            v.addWidget(self.cheats_list, 1)
            if self.o.get("cheats_foreign"):
                self.cheats_overwrite_cb = QCheckBox("Replace my existing cheat file (it wasn't made by PCTX)")
                v.addWidget(self.cheats_overwrite_cb)
        if self.o["cheats_installed"]:
            self.remove_cheats = QCheckBox("Remove the cheat file for this game")
            v.addWidget(self.remove_cheats)
        self.tabs.addTab(w, icons.tab_icon("cheats") if hasattr(icons, "tab_icon") else QIcon(), "Cheats")

    def _filter_cheats(self, text: str):
        t = text.strip().lower()
        for i in range(self.cheats_list.count()):
            it = self.cheats_list.item(i)
            it.setHidden(bool(t) and t not in it.text().lower())

    def _set_all_cheats(self, on: bool):
        for i in range(self.cheats_list.count()):
            it = self.cheats_list.item(i)
            if not it.isHidden():
                it.setCheckState(Qt.Checked if on else Qt.Unchecked)

    # ---- textures ----
    def _build_textures_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        tex = self.o["textures"]
        self.texture_list = QListWidget()
        self.textures_overwrite_cb = None
        self.texture_list.addItem(QListWidgetItem("Don't install a texture pack"))
        for t in tex:
            conf = enhancements.CONFIDENCE_LABEL.get(t.get("confidence"), "")
            it = QListWidgetItem(f"{texture_candidate_label(t)}      [{conf}]" if conf else texture_candidate_label(t))
            if t.get("confidence") in ("verified", "high"):
                it.setForeground(QColor(theme.COLOR_SUCCESS))
            elif t.get("confidence") == "low":
                it.setForeground(QColor(theme.CLASSIC["muted"]))
            it.setToolTip(t.get("description") or t.get("url") or "")
            self.texture_list.addItem(it)
        self.texture_list.setCurrentRow(0)
        if tex:
            v.addWidget(self._note("Best matches first. Verified packs are hand-checked; search results are ranked by how "
                                   "closely they match this game, and unrelated hits are hidden."))
        else:
            v.addWidget(self._note("No texture pack was found for this game. If you turn on \"Dump textures while "
                                   "playing\" in Settings and play for a while, PCTX can AI-upscale the game's own textures."))
        v.addWidget(self.texture_list, 1)
        if self.o["textures_installed"]:
            self.textures_overwrite_cb = QCheckBox("Replace the installed pack with the one selected above")
            v.addWidget(self.textures_overwrite_cb)
            self.remove_textures = QCheckBox("Remove the installed texture pack")
            v.addWidget(self.remove_textures)
        self.tabs.addTab(w, icons.tab_icon("textures") if hasattr(icons, "tab_icon") else QIcon(), "Texture Packs")

    # ---- checks ----
    def _build_checks_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        self.fix_boxes: Dict[str, QCheckBox] = {}
        colors = {"ok": theme.COLOR_SUCCESS, "warn": theme.COLOR_WARNING, "error": theme.COLOR_DANGER}
        warn = 0
        for c in self.o["checks"]:
            row = QHBoxLayout()
            dot = QLabel("●")
            dot.setStyleSheet(f"color: {colors.get(c.level, theme.CLASSIC['muted'])};")
            dot.setAlignment(Qt.AlignTop)
            row.addWidget(dot)
            lbl = QLabel(c.text)
            lbl.setWordWrap(True)
            row.addWidget(lbl, 1)
            if c.fix:
                cb = QCheckBox(c.fix_label)
                cb.setChecked(True)
                self.fix_boxes[c.fix] = cb
                row.addWidget(cb)
            warn += c.level in ("warn", "error")
            v.addLayout(row)
        v.addStretch(1)
        self.tabs.addTab(w, "Checks" + (f" ({warn})" if warn else ""))

    # ---- helpers ----
    @staticmethod
    def _note(text: str) -> QLabel:
        l = QLabel(text)
        l.setWordWrap(True)
        l.setObjectName(theme.OBJ_MUTED_LABEL)
        return l

    def _show_log(self):
        box = QMessageBox(self)
        box.setWindowTitle("What was searched")
        box.setText(f"Sources checked for {self.game.title or self.game.serial}:")
        box.setDetailedText("\n".join(self.o["log"]))
        box.exec()

    def apply_recommended(self):
        rec = self.o["recommendation"]
        if hasattr(self, "patch_tree"):
            for i in range(self.patch_tree.topLevelItemCount()):
                top = self.patch_tree.topLevelItem(i)
                for j in range(top.childCount()):
                    it = top.child(j)
                    fi, name = it.data(0, Qt.UserRole)
                    on = name in rec.patches.get(fi, []) or (name == "*" and fi in rec.patches)
                    it.setCheckState(0, Qt.Checked if on else Qt.Unchecked)
        self.texture_list.setCurrentRow(rec.texture + 1 if rec.texture >= 0 else 0)

    def selection(self) -> dict:
        patches: Dict[int, List[str]] = {}
        if hasattr(self, "patch_tree"):
            for i in range(self.patch_tree.topLevelItemCount()):
                top = self.patch_tree.topLevelItem(i)
                for j in range(top.childCount()):
                    it = top.child(j)
                    if it.checkState(0) == Qt.Checked:
                        fi, name = it.data(0, Qt.UserRole)
                        patches.setdefault(fi, []).append(name)
        cheats = [i for i in range(self.cheats_list.count()) if self.cheats_list.item(i).checkState() == Qt.Checked]
        remove = [k for k, attr in (("patches", "remove_patches"), ("cheats", "remove_cheats"), ("textures", "remove_textures"))
                  if getattr(self, attr, None) is not None and getattr(self, attr).isChecked()]
        return {"patches": patches, "cheats": cheats,
                "cheatsOverwrite": bool(self.cheats_overwrite_cb and self.cheats_overwrite_cb.isChecked()),
                "texture": self.texture_list.currentRow() - 1,
                "texturesOverwrite": bool(self.textures_overwrite_cb and self.textures_overwrite_cb.isChecked()),
                "remove": remove, "fixes": [k for k, cb in self.fix_boxes.items() if cb.isChecked()]}


def texture_candidate_label(t: dict) -> str:
    """Human label for a texture-pack candidate (curated, GitHub or Internet Archive)."""
    source_ref = t.get('github_repo') or t.get('archive_org_id') or t.get('manual_url') or ''
    label = t.get('name') or source_ref or 'Pack'
    if t.get('paid'):
        label += "  \u00b7  paid: opens the creator's page"
    elif t.get('manual_url'):
        label += "  \u00b7  opens in your browser to download by hand"
    elif t.get('archive_org_id'):
        label += f"  \u00b7  Internet Archive ({source_ref})"
    elif t.get('github_repo') and not t.get('verified', True):
        label += f"  \u00b7  GitHub {source_ref}" + (f", {t['stars']}\u2605" if t.get('stars') else "")
    return label


class TaskWorker(QThread):
    """Run one plain function off the UI thread.

    `fn(progress)` gets a callable that posts a progress line; its return value
    (or the exception text) comes back on the main thread through `done`. The
    result signal is not called `finished`, so QThread's own end-of-thread
    signal stays free for release_worker_when_done()."""

    progressed = Signal(str)
    done = Signal(object, str)          # (result, error message or "")

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self._fn = fn

    def run(self):
        try:
            result = self._fn(self.progressed.emit)
        except Exception as e:  # reported to the caller, never raised in the thread
            logger.error(f"[TaskWorker] {e}")
            self.done.emit(None, str(e) or e.__class__.__name__)
            return
        self.done.emit(result, "")


class PatchAllWorker(QThread):
    """Installs every available PCSX2 patch/fix for a list of games, off the UI
    thread. Emits progress as (done, total) and one summary when finished."""

    progressed = Signal(int, int)
    summary = Signal(dict)

    def __init__(self, games: List[Tuple[str, Optional[str]]], patches_dir: str, cache_dir: str, parent=None,
                 data_dir: str = ""):
        super().__init__(parent)
        self._games = games
        self._patches_dir = patches_dir
        self._cache_dir = cache_dir
        self._data_dir = data_dir

    def run(self):
        total = {"games": len(self._games), "with_patches": 0, "installed": 0, "skipped": 0, "failed": 0,
                 "switched_on": 0, "error": None}
        index, err = patches_online.load_index(self._cache_dir)
        if not index:
            total["error"] = err
            self.summary.emit(total)
            return
        for i, (serial, crc) in enumerate(self._games, start=1):
            if self.isInterruptionRequested():
                break
            r = patches_online.install_for_game(index, serial, crc, self._patches_dir)
            if any(r.values()):
                total["with_patches"] += 1
            for k in ("installed", "skipped", "failed"):
                total[k] += len(r[k])
            # Switch the recommended groups on for the exact disc (needs its CRC).
            files, exact = patches_online.find_for_game(index, serial, crc)
            if exact and self._data_dir and crc:
                try:
                    names, managed = [], []
                    for pf in files:
                        groups = enhancements.parse_groups(patches_online.fetch_text(pf))
                        managed += [g.name for g in groups]
                        names += [g.name for g in groups if g.recommended]
                    if names:
                        gs = enhancements.game_settings_path(self._data_dir, serial, crc)
                        enhancements.set_enabled(gs, enhancements.PATCHES_SECTION,
                                                 list(dict.fromkeys(names + enhancements.read_list(gs, enhancements.PATCHES_SECTION))),
                                                 [])
                        total["switched_on"] += 1
                except Exception as e:
                    logger.warning(f"[patches] enabling for {serial}: {e}")
            self.progressed.emit(i, len(self._games))
        self.summary.emit(total)


class _SortItem(QTableWidgetItem):
    """Table cell that sorts on a separate, case-insensitive key (UserRole+1)
    instead of its display text."""

    def __lt__(self, other):
        a, b = self.data(Qt.UserRole + 1), other.data(Qt.UserRole + 1)
        if a is not None and b is not None:
            return a < b
        return super().__lt__(other)


class LibraryView(QWidget):
    """The game library and everything done with it (scan, add, install, covers),
    shown in one of two ways:

    Classic      -- a native toolbar + sortable game table (or cover grid), the
                    way PCSX2's own front-end presents its game list.
    Big Picture  -- web/bigpicture.html, a fullscreen controller-first shelf.

    Both are views over the same `games` dict and the same selection, so the
    install/scan/cover code below exists once.
    """

    LIBRARY_STORE = os.path.join(os.path.expanduser("~"), ".pcsx2_manager_library.json")
    COVER_CACHE_DIR = os.path.join(os.path.expanduser("~"), ".pcsx2_manager_thumbs")

    COL_COVER, COL_TITLE, COL_SERIAL, COL_REGION, COL_CRC, COL_PATCHES, COL_CHEATS, COL_TEXTURES, COL_FILE = range(9)
    HEADERS = ["", "Title", "Serial", "Region", "CRC", "Patches", "Cheats", "Textures", "Disc Image"]
    LIST_ICON = (30, 40)

    def __init__(self, parent: 'MainWindow'):
        super().__init__()
        self.parent = parent
        self.ui_mode = theme.MODE_CLASSIC
        self.view_mode = "list"
        self._shutting_down = False
        self._workers: List[QThread] = []
        self.games: Dict[str, GameEntry] = {}
        self._selected_serial: Optional[str] = None
        self._grid_cover_w = theme.GRID_COVER_DEFAULT
        self._icon_cache: Dict[Tuple[str, int], QIcon] = {}
        self._grid_icons: Dict[str, Tuple[tuple, QIcon]] = {}   # serial -> ((cheats, textures), icon)
        self._status_cache: Dict[str, Tuple[bool, bool]] = {}   # serial -> installed flags (disk lookups)
        self._busy = False
        self._ui_ready = False   # actions fire before the widgets exist otherwise
        self._cover_inflight: Optional[str] = None
        # Cover downloads arrive one at a time; coalesce the resulting redraws.
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(350)
        self._refresh_timer.timeout.connect(self._refresh_library)
        # Typing in the search box and dragging the zoom slider fire constantly;
        # coalesce them so the UI never rebuilds more than it needs to.
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(120)
        self._search_timer.timeout.connect(lambda: self._refresh_library(bp=False))
        self._zoom_save_timer = QTimer(self)
        self._zoom_save_timer.setSingleShot(True)
        self._zoom_save_timer.setInterval(400)
        self._zoom_save_timer.timeout.connect(self.save_view_prefs)

        self._create_actions()
        self._build_ui()
        self._ui_ready = True
        self._load_library()
        self._restore_view_prefs()
        self._refresh_library()
        self._prefetch_missing_covers()
        self.parent.paths_changed.connect(lambda _p: self._refresh_library(clear_status=True))
        # Exact CRCs from the disc images, once the window is up (reads a few MB per game).
        QTimer.singleShot(2500, self._resolve_disc_crcs)

    # ---- actions (shared by the toolbar, the menus and context menus) ----
    def _act(self, text: str, icon: str, slot, shortcut: Optional[str] = None,
             tip: str = "", checkable: bool = False) -> QAction:
        a = QAction(icons.tab_icon(icon), text, self)
        if shortcut:
            a.setShortcut(QKeySequence(shortcut))
        if tip:
            a.setToolTip(tip)
            a.setStatusTip(tip)
        a.setCheckable(checkable)
        if checkable:
            a.toggled.connect(slot)
        else:
            a.triggered.connect(lambda _checked=False, f=slot: f())
        return a

    def _create_actions(self):
        self.act_scan = self._act("Scan Folder…", "scan_folder", self._scan_folder, "Ctrl+O",
                                  "Add every PS2 disc image found in a folder")
        self.act_add = self._act("Add Game…", "add", self._add_game_manually, "Ctrl+N",
                                 "Add a single game by its serial number")
        self.act_install = self._act("Enhancements…", "enhance", self._sync_selected, "Ctrl+E",
                                     "Patches & fixes, cheats and texture packs for the selected game")
        self.act_patch_all = self._act("Apply Recommended Patches to All Games…", "enhance", self._patch_all_games, None,
                                       "Install and switch on the widescreen, no-interlacing and fix patches PCSX2 "
                                       "publishes for every game in the library")
        self.act_cheats = self._act("Cheat Editor", "cheats", self._open_in_cheats, None,
                                    "Open the selected game in the cheat editor")
        self.act_textures = self._act("Texture Manager", "textures", self._open_in_textures, None,
                                      "Open the texture pack manager")
        self.act_launch = self._act("Launch in PCSX2", "launch", self._launch_in_pcsx2, "Ctrl+L",
                                    "Start the selected game in PCSX2")
        self.act_folder = self._act("Open Disc Folder", "folder", self._open_game_folder, None,
                                    "Show the disc image in Explorer")
        self.act_copy_serial = self._act("Copy Serial", "copy", self._copy_serial, None)
        self.act_remove = self._act("Remove from Library", "remove", self._remove_selected, "Del",
                                    "Remove the selected game from this list (installed files are kept)")
        self.act_view_list = self._act("List", "view_list", lambda on: on and self._set_view_mode("list"),
                                       "Ctrl+1", "Show games as a list", checkable=True)
        self.act_view_grid = self._act("Grid", "view_grid", lambda on: on and self._set_view_mode("grid"),
                                       "Ctrl+2", "Show games as cover art", checkable=True)
        grp = QActionGroup(self)
        grp.setExclusive(True)
        grp.addAction(self.act_view_list)
        grp.addAction(self.act_view_grid)
        self.act_view_list.setChecked(True)
        self.act_zoom_in = self._act("Zoom In", "zoom_in", lambda: self._bump_zoom(16), "Ctrl+=")
        self.act_zoom_out = self._act("Zoom Out", "zoom_out", lambda: self._bump_zoom(-16), "Ctrl+-")
        self.act_find = self._act("Find…", "scan", lambda: self.search_box.setFocus(), "Ctrl+F")
        self.act_big_picture = self._act("Big Picture", "big_picture", self.parent.toggle_ui_mode, "F11",
                                         "Fullscreen, controller-friendly interface")
        self.act_settings = self._act("Settings…", "settings", self.parent.open_settings_dialog, "Ctrl+,",
                                      "PCSX2 folders and options")
        self._selection_actions = (self.act_install, self.act_cheats, self.act_textures,
                                   self.act_folder, self.act_copy_serial, self.act_remove)

    # ---- UI ----
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.view_stack = QStackedWidget(self)

        # ---------- Classic ----------
        self.classic = QWidget()
        cl = QVBoxLayout(self.classic)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(0)

        self.toolbar = QToolBar()
        self.toolbar.setMovable(False)
        self.toolbar.setFloatable(False)
        self.toolbar.setIconSize(QSize(24, 24))
        self.toolbar.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
        for a in (self.act_scan, self.act_add):
            self.toolbar.addAction(a)
        self.toolbar.addSeparator()
        for a in (self.act_install, self.act_cheats, self.act_textures, self.act_launch):
            self.toolbar.addAction(a)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.toolbar.addWidget(spacer)

        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Search games…")
        self.search_box.setClearButtonEnabled(True)
        self.search_box.setFixedWidth(200)
        self.search_box.textChanged.connect(lambda _t: self._search_timer.start())
        self.toolbar.addWidget(self.search_box)

        self.toolbar.addAction(self.act_view_list)
        self.toolbar.addAction(self.act_view_grid)
        self.zoom_slider = QSlider(Qt.Horizontal)
        self.zoom_slider.setRange(theme.GRID_COVER_MIN, theme.GRID_COVER_MAX)
        self.zoom_slider.setValue(self._grid_cover_w)
        self.zoom_slider.setFixedWidth(90)
        self.zoom_slider.setToolTip("Cover size")
        self.zoom_slider.valueChanged.connect(self._on_zoom_changed)
        self.zoom_action = self.toolbar.addWidget(self.zoom_slider)
        self.zoom_action.setVisible(False)
        self.toolbar.addSeparator()
        self.toolbar.addAction(self.act_settings)
        self.toolbar.addAction(self.act_big_picture)
        cl.addWidget(self.toolbar)

        self.list_stack = QStackedWidget()
        self.table = self._build_table()
        self.grid = self._build_grid()
        self.empty_page = self._build_empty_page()
        for w in (self.table, self.grid, self.empty_page):
            self.list_stack.addWidget(w)
        cl.addWidget(self.list_stack, 1)

        # ---------- Big Picture ----------
        self.bp = bigpicture.BigPictureView()
        self.bp.selectionChanged.connect(self._on_bp_selection)
        self.bp.actionRequested.connect(self._on_bp_action)
        self.bp.set_methods(self.bp_rpc_methods(), self.bp_rpc_async_methods())

        self.view_stack.addWidget(self.classic)
        self.view_stack.addWidget(self.bp)
        root.addWidget(self.view_stack)

    def _build_table(self) -> QTableWidget:
        t = QTableWidget(0, len(self.HEADERS))
        t.setHorizontalHeaderLabels(self.HEADERS)
        t.verticalHeader().setVisible(False)
        t.verticalHeader().setDefaultSectionSize(self.LIST_ICON[1] + 6)
        t.setSelectionBehavior(QAbstractItemView.SelectRows)
        t.setSelectionMode(QAbstractItemView.SingleSelection)
        t.setEditTriggers(QAbstractItemView.NoEditTriggers)
        t.setShowGrid(False)
        t.setAlternatingRowColors(True)
        t.setIconSize(QSize(*self.LIST_ICON))
        t.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        t.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        t.setFrameShape(QFrame.NoFrame)
        t.setWordWrap(False)
        hh = t.horizontalHeader()
        hh.setHighlightSections(False)
        hh.setSortIndicatorShown(True)
        hh.setSectionResizeMode(QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(self.COL_TITLE, QHeaderView.Stretch)
        hh.setSectionResizeMode(self.COL_FILE, QHeaderView.Interactive)
        t.setColumnWidth(self.COL_FILE, 220)
        t.setSortingEnabled(True)
        hh.setSortIndicator(self.COL_TITLE, Qt.AscendingOrder)
        t.itemSelectionChanged.connect(self._on_classic_selection)
        t.itemActivated.connect(lambda _i: self._sync_selected())
        t.setContextMenuPolicy(Qt.CustomContextMenu)
        t.customContextMenuRequested.connect(lambda pos: self._show_context_menu(t, pos))
        return t

    def _build_grid(self) -> QListWidget:
        g = QListWidget()
        g.setViewMode(QListWidget.IconMode)
        g.setMovement(QListWidget.Static)
        g.setResizeMode(QListWidget.Adjust)
        g.setWordWrap(True)
        g.setUniformItemSizes(True)
        g.setSpacing(6)
        g.setFrameShape(QFrame.NoFrame)
        g.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        g.itemSelectionChanged.connect(self._on_classic_selection)
        g.itemActivated.connect(lambda _i: self._sync_selected())
        g.setContextMenuPolicy(Qt.CustomContextMenu)
        g.customContextMenuRequested.connect(lambda pos: self._show_context_menu(g, pos))
        return g

    def _build_empty_page(self) -> QWidget:
        """Shown while the library has no games -- same idea as PCSX2's empty
        game list, with the two ways to fill it right there."""
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.addStretch(1)
        title = QLabel("No games in your library yet")
        f = title.font()
        f.setPointSize(f.pointSize() + 4)
        f.setBold(True)
        title.setFont(f)
        title.setAlignment(Qt.AlignCenter)
        lay.addWidget(title)
        hint = QLabel("Scan the folder where you keep your PS2 disc images, or add a game by its serial number.")
        hint.setObjectName(theme.OBJ_MUTED_LABEL)
        hint.setAlignment(Qt.AlignCenter)
        hint.setWordWrap(True)
        lay.addWidget(hint)
        row = QHBoxLayout()
        row.addStretch(1)
        b1 = QPushButton("Scan Folder…")
        b1.clicked.connect(self._scan_folder)
        b2 = QPushButton("Add Game…")
        b2.clicked.connect(self._add_game_manually)
        row.addWidget(b1)
        row.addWidget(b2)
        row.addStretch(1)
        lay.addSpacing(theme.SPACING_MD)
        lay.addLayout(row)
        lay.addStretch(2)
        return page

    # ---- persistence ----
    def _load_library(self):
        try:
            if os.path.isfile(self.LIBRARY_STORE):
                with open(self.LIBRARY_STORE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                for d in data:
                    ge = _dict_to_entry(d)
                    if ge.serial:
                        self.games[ge.serial] = ge
        except Exception as e:
            logger.warning(f"[LibraryView] Failed to load library: {e}")

    def _save_library(self):
        try:
            data = [_entry_to_dict(g) for g in self.games.values()]
            with open(self.LIBRARY_STORE, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"[LibraryView] Failed to save library: {e}")

    def _restore_view_prefs(self):
        s = app_settings()
        try:
            self._grid_cover_w = max(theme.GRID_COVER_MIN, min(theme.GRID_COVER_MAX,
                                     int(s.value('ui/grid_zoom', theme.GRID_COVER_DEFAULT))))
        except (TypeError, ValueError):
            pass
        self.zoom_slider.blockSignals(True)
        self.zoom_slider.setValue(self._grid_cover_w)
        self.zoom_slider.blockSignals(False)
        try:
            col = int(s.value('ui/sort_col', self.COL_TITLE))
            order = Qt.DescendingOrder if int(s.value('ui/sort_desc', 0)) else Qt.AscendingOrder
            self.table.horizontalHeader().setSortIndicator(col, order)
        except (TypeError, ValueError):
            pass
        if s.value('ui/view_mode', 'list') == 'grid':
            self.act_view_grid.setChecked(True)

    def save_view_prefs(self):
        s = app_settings()
        hh = self.table.horizontalHeader()
        s.setValue('ui/view_mode', self.view_mode)
        s.setValue('ui/grid_zoom', self._grid_cover_w)
        s.setValue('ui/sort_col', hh.sortIndicatorSection())
        s.setValue('ui/sort_desc', 1 if hh.sortIndicatorOrder() == Qt.DescendingOrder else 0)

    # ---- display mode / view mode ----
    def set_ui_mode(self, mode: str):
        self.ui_mode = mode if mode in (theme.MODE_CLASSIC, theme.MODE_BIG_PICTURE) else theme.MODE_CLASSIC
        if self.ui_mode == theme.MODE_BIG_PICTURE:
            self.view_stack.setCurrentWidget(self.bp)
            if self._selected_serial:
                self.bp.select_serial(self._selected_serial)
            self.bp.enter()
            self.bp.setFocus()
        else:
            self.view_stack.setCurrentWidget(self.classic)
            self._select_in_classic(self._selected_serial)
            self._active_view().setFocus()

    def _active_view(self) -> QWidget:
        if not self.games:
            return self.empty_page
        return self.grid if self.view_mode == "grid" else self.table

    def _set_view_mode(self, mode: str):
        self.view_mode = mode
        if not self._ui_ready:
            return
        self.zoom_action.setVisible(mode == "grid")
        self.act_zoom_in.setEnabled(mode == "grid")
        self.act_zoom_out.setEnabled(mode == "grid")
        self._show_list_page()
        self._select_in_classic(self._selected_serial)
        self.save_view_prefs()

    def _show_list_page(self):
        self.list_stack.setCurrentWidget(self._active_view())

    def _bump_zoom(self, delta: int):
        self.zoom_slider.setValue(self.zoom_slider.value() + delta)

    def _on_zoom_changed(self, value: int):
        """Resizing the cover grid only changes the view's icon/grid size: the covers
        are kept at a fixed high resolution and scaled by Qt as they're drawn, so
        dragging the slider never rebuilds anything."""
        self._grid_cover_w = value
        self._apply_grid_size()
        self._zoom_save_timer.start()

    def _apply_grid_size(self):
        cw = self._grid_cover_w
        ch = int(cw * 4 / 3)
        self.grid.setIconSize(QSize(cw, ch))
        self.grid.setGridSize(QSize(cw + 24, ch + 52))

    # ---- building the library views ----
    def _cover_pixmap(self, game: GameEntry, w: int, h: int) -> QPixmap:
        path = cover_cache_path(game.serial, self.COVER_CACHE_DIR)
        if not os.path.isfile(path):
            return scale_and_crop_pixmap(create_library_cover_placeholder(game.serial), w, h)
        # Decoding a full 512x736 cover just to draw a 40 px list icon is most of
        # the library's build time, so each size is cached on disk next to the
        # cover the first time it's needed (and rebuilt if the cover changes).
        folder, name = os.path.split(path)
        small = os.path.join(folder, "sized", f"{os.path.splitext(name)[0]}.{w}x{h}.png")
        try:
            if os.path.isfile(small) and os.path.getmtime(small) >= os.path.getmtime(path):
                pm = QPixmap(small)
                if not pm.isNull():
                    return pm
        except OSError:
            pass
        pm = QPixmap(path)
        if pm.isNull():
            return scale_and_crop_pixmap(create_library_cover_placeholder(game.serial), w, h)
        pm = scale_and_crop_pixmap(pm, w, h)
        try:
            os.makedirs(os.path.dirname(small), exist_ok=True)
            pm.save(small, "PNG")
        except OSError:
            pass
        return pm

    def _list_icon(self, game: GameEntry) -> QIcon:
        key = (game.serial, self.LIST_ICON[0])
        icon = self._icon_cache.get(key)
        if icon is None:
            icon = QIcon(self._cover_pixmap(game, *self.LIST_ICON))
            self._icon_cache[key] = icon
        return icon

    def _matches_filter(self, g: GameEntry, needle: str) -> bool:
        if not needle:
            return True
        hay = f"{g.title} {g.serial} {region_for_serial(g.serial)} {g.crc or ''}".upper()
        return needle in hay

    def _can_launch(self, game: GameEntry) -> bool:
        exe = self.parent.settings_tab.pcsx2_exe.text().strip()
        return bool(exe and os.path.isfile(exe) and game.source_path and os.path.isfile(game.source_path))

    GRID_SRC = (240, 320)   # grid covers are stored at this size and scaled down by Qt

    def _status_for(self, g: GameEntry) -> Tuple[bool, bool]:
        s = self._status_cache.get(g.serial)
        if s is None:
            s = self._status_cache[g.serial] = self._game_install_status(g)
        return s

    def _grid_icon(self, g: GameEntry, cheats: bool, textures: bool) -> QIcon:
        key = (cheats, textures)
        hit = self._grid_icons.get(g.serial)
        if hit and hit[0] == key:
            return hit[1]
        pm = self._cover_pixmap(g, *self.GRID_SRC)
        if cheats or textures:
            pm = self._badge_cover(pm, cheats, textures)
        icon = QIcon(pm)
        self._grid_icons[g.serial] = (key, icon)
        return icon

    def _refresh_library(self, bp: bool = True, clear_status: bool = False):
        """Rebuild the views. `clear_status` re-reads what's installed from disk
        (after installs / path changes); `bp=False` skips the Big Picture push
        (it ignores the search filter, so typing needn't touch it)."""
        if clear_status:
            self._status_cache.clear()
        needle = (self.search_box.text() or "").strip().upper() if hasattr(self, 'search_box') else ""
        ordered = sorted(self.games.values(), key=lambda g: (g.title or g.serial).upper())
        shown = [g for g in ordered if self._matches_filter(g, needle)]

        hh = self.table.horizontalHeader()
        col, order = hh.sortIndicatorSection(), hh.sortIndicatorOrder()
        self.table.setUpdatesEnabled(False)
        self.grid.setUpdatesEnabled(False)
        try:
            self.table.setSortingEnabled(False)
            self.table.setRowCount(0)
            self.grid.clear()
            self._apply_grid_size()
            patched = self._patches_installed_index()
            for g in shown:
                cheats, textures = self._status_for(g)
                self._add_table_row(g, cheats, textures, g.serial.upper() in patched)
                it = QListWidgetItem(self._grid_icon(g, cheats, textures), g.title or g.serial)
                it.setData(Qt.UserRole, g.serial)
                it.setTextAlignment(Qt.AlignHCenter | Qt.AlignTop)
                it.setToolTip(f"{g.title or g.serial}\n{g.serial}")
                self.grid.addItem(it)
            self.table.setSortingEnabled(True)
            self.table.sortItems(col, order)
        finally:
            self.table.setUpdatesEnabled(True)
            self.grid.setUpdatesEnabled(True)

        self._show_list_page()
        self._select_in_classic(self._selected_serial)
        self.parent.set_library_count(len(self.games), len(shown))

        if bp:
            favs, played, patched = self._favorites(), self._last_played(), self._patches_installed_index()
            self.bp.set_items([
                bigpicture.BigPictureItem(
                    serial=g.serial, title=g.title or g.serial, region=region_for_serial(g.serial),
                    crc=g.crc or "", cover_path=cover_cache_path(g.serial, self.COVER_CACHE_DIR),
                    cheats_installed=self._status_for(g)[0], textures_installed=self._status_for(g)[1],
                    patches_installed=g.serial.upper() in patched,
                    launchable=self._can_launch(g), favorite=g.serial in favs,
                    last_played=played.get(g.serial, 0.0),
                ) for g in ordered
            ])
            if self._selected_serial:
                self.bp.select_serial(self._selected_serial)
        self._update_action_states()

    def _add_table_row(self, g: GameEntry, cheats: bool, textures: bool, patches: bool = False):
        row = self.table.rowCount()
        self.table.insertRow(row)
        title = g.title or g.serial

        cover = QTableWidgetItem()
        cover.setIcon(self._list_icon(g))
        cover.setData(Qt.UserRole + 1, "")
        self.table.setItem(row, self.COL_COVER, cover)

        def cell(col, text, key=None, color=None):
            it = _SortItem(text)
            it.setData(Qt.UserRole + 1, (key if key is not None else text).upper())
            if col == self.COL_TITLE:
                it.setData(Qt.UserRole, g.serial)
            if color:
                it.setForeground(QColor(color))
            self.table.setItem(row, col, it)

        cell(self.COL_TITLE, title)
        cell(self.COL_SERIAL, g.serial)
        cell(self.COL_REGION, region_for_serial(g.serial))
        cell(self.COL_CRC, g.crc or "")
        self.table.item(row, self.COL_CRC).setToolTip(
            "Read from the disc image (exact)" if g.crc_source == 'disc' else
            f"From {g.crc_source}; may differ from your copy" if g.crc else "Unknown -- add the disc image")
        cell(self.COL_PATCHES, "Installed" if patches else "—", "1" if patches else "0",
             theme.COLOR_SUCCESS if patches else None)
        cell(self.COL_CHEATS, "Installed" if cheats else "—", "1" if cheats else "0",
             theme.COLOR_SUCCESS if cheats else None)
        cell(self.COL_TEXTURES, "Installed" if textures else "—", "1" if textures else "0",
             theme.COLOR_SUCCESS if textures else None)
        cell(self.COL_FILE, os.path.basename(g.source_path) if g.source_path else "")
        self.table.item(row, self.COL_FILE).setToolTip(g.source_path or "")

    @staticmethod
    def _badge_cover(pm: QPixmap, cheats_installed: bool, textures_installed: bool) -> QPixmap:
        """Small corner dot on a grid cover: green when both are installed,
        amber when only one is."""
        out = QPixmap(pm)
        d = max(10, out.width() // 9)
        color = QColor(theme.COLOR_SUCCESS) if (cheats_installed and textures_installed) else QColor(theme.COLOR_WARNING)
        p = QPainter(out)
        p.setRenderHint(QPainter.Antialiasing)
        p.setBrush(color)
        p.setPen(QColor("#1c1c1c"))
        p.drawEllipse(out.width() - d - 5, out.height() - d - 5, d, d)
        p.end()
        return out

    # ---- selection (single source of truth: self._selected_serial) ----
    def _classic_serial(self) -> Optional[str]:
        if self.view_mode == "grid":
            it = self.grid.currentItem()
            return it.data(Qt.UserRole) if it and it.isSelected() else None
        rows = self.table.selectionModel().selectedRows()
        if rows:
            it = self.table.item(rows[0].row(), self.COL_TITLE)
            return it.data(Qt.UserRole) if it else None
        return None

    def _select_in_classic(self, serial: Optional[str]):
        """Reflect `serial` in whichever classic view is showing, without
        re-triggering the selection handlers."""
        self.table.blockSignals(True)
        self.grid.blockSignals(True)
        try:
            self.table.clearSelection()
            self.grid.clearSelection()
            if serial:
                for r in range(self.table.rowCount()):
                    if self.table.item(r, self.COL_TITLE).data(Qt.UserRole) == serial:
                        self.table.selectRow(r)
                        self.table.scrollToItem(self.table.item(r, self.COL_TITLE))
                        break
                for i in range(self.grid.count()):
                    if self.grid.item(i).data(Qt.UserRole) == serial:
                        self.grid.setCurrentRow(i)
                        self.grid.scrollToItem(self.grid.item(i))
                        break
        finally:
            self.table.blockSignals(False)
            self.grid.blockSignals(False)

    def _on_classic_selection(self):
        self._select(self._classic_serial(), source="classic")

    def _on_bp_selection(self, serial: str):
        self._select(serial or None, source="bp")

    def _select(self, serial: Optional[str], source: str):
        self._selected_serial = serial
        game = self.games.get(serial) if serial else None
        self.parent.state.current_game = game
        self.parent.current_game_changed.emit(game)
        if source != "bp" and self.ui_mode == theme.MODE_BIG_PICTURE and serial:
            self.bp.select_serial(serial)
        self._update_action_states()
        self.parent.set_status_hint(self._describe(game))
        if game:
            self._load_cover_if_missing(game)

    @staticmethod
    def _describe(game: Optional[GameEntry]) -> str:
        if not game:
            return ""
        bits = [game.title or game.serial, game.serial, region_for_serial(game.serial)]
        if game.crc:
            bits.append(f"CRC {game.crc}")
        return "  ·  ".join(b for b in bits if b)

    def _selected_game(self) -> Optional[GameEntry]:
        return self.games.get(self._selected_serial) if self._selected_serial else None

    def _update_action_states(self):
        game = self._selected_game()
        for a in self._selection_actions:
            a.setEnabled(game is not None and not self._busy)
        self.act_folder.setEnabled(bool(game and game.source_path and os.path.exists(game.source_path)))
        self.act_launch.setEnabled(bool(game and self._can_launch(game)))
        self.act_scan.setEnabled(not self._busy)

    # ---- context menu ----
    def _show_context_menu(self, view, pos):
        if view is self.table:
            idx = self.table.indexAt(pos)
            if idx.isValid():
                self.table.selectRow(idx.row())
        else:
            it = self.grid.itemAt(pos)
            if it:
                self.grid.setCurrentItem(it)
        if not self._selected_game():
            return
        menu = QMenu(self)
        menu.addAction(self.act_install)
        menu.addSeparator()
        for a in (self.act_cheats, self.act_textures, self.act_launch, self.act_folder):
            menu.addAction(a)
        menu.addSeparator()
        menu.addAction(self.act_copy_serial)
        menu.addAction(self.act_remove)
        menu.exec(view.viewport().mapToGlobal(pos))

    # ---- actions on the selected game ----
    def _copy_serial(self):
        game = self._selected_game()
        if game:
            QApplication.clipboard().setText(game.serial)
            self.parent.flash_status(f"Copied {game.serial}")

    def _remove_selected(self):
        game = self._selected_game()
        if game:
            self._remove_game(game)

    def _notify(self, text: str, title: str = "", warn: bool = False):
        """Tell the user something. Classic uses a message box; Big Picture shows
        an in-page toast instead, because it must never open a native window."""
        if self.ui_mode == theme.MODE_BIG_PICTURE:
            self.bp.show_toast(text)
        elif warn:
            QMessageBox.warning(self, title, text)
        else:
            QMessageBox.information(self, title, text)

    def _remove_game(self, game: GameEntry):
        reply = QMessageBox.question(
            self, "Remove Game",
            f"Remove \"{game.title or game.serial}\" from your library?\n\n"
            f"This only removes it from this app's list -- any cheats/textures "
            f"already installed in PCSX2 are left untouched.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            self._forget_game(game.serial)

    def _forget_game(self, serial: str):
        """Drop a game from the library list (no confirmation; callers ask first)."""
        if serial not in self.games:
            return
        del self.games[serial]
        if self._selected_serial == serial:
            self._selected_serial = None
            self._select(None, source="classic")
        self._save_library()
        self._refresh_library()

    def _open_in_cheats(self):
        if self._selected_game():
            self.parent.open_advanced_cheats()

    def _open_in_textures(self):
        self.parent.open_advanced_textures()

    def _open_game_folder(self):
        game = self._selected_game()
        if game and game.source_path and os.path.exists(game.source_path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(os.path.abspath(game.source_path))))

    def _launch_in_pcsx2(self):
        game = self._selected_game()
        exe = self.parent.settings_tab.pcsx2_exe.text().strip()
        if not exe or not os.path.isfile(exe):
            self._notify("Set the path to the PCSX2 program in Settings first.", "PCSX2 not set")
            return
        if not game or not game.source_path or not os.path.isfile(game.source_path):
            self._notify("This game has no disc image on disk to launch (it was added by serial).",
                         "Disc image not found")
            return
        try:
            subprocess.Popen([exe, game.source_path], cwd=os.path.dirname(exe))
        except Exception as e:
            self._notify(f"Couldn't start PCSX2: {e}", "Launch failed", warn=True)
            return
        played = self._last_played()
        played[game.serial] = time.time()
        app_settings().setValue("library/last_played", json.dumps(played))
        if self.ui_mode == theme.MODE_BIG_PICTURE:
            self.bp.show_toast(f"Starting {game.title or game.serial}…")
        self._refresh_timer.start()

    # ---- Big Picture requests ----
    def _on_bp_action(self, action: str, serial: str):
        """The few actions Big Picture hands to Python. Everything that needs a
        screen (install, settings, add, scan, confirm) lives in the page and talks
        to us through bp_rpc_methods() instead, so no native window ever opens."""
        if serial and serial in self.games:
            self._selected_serial = serial
        if action == "launch":
            self._launch_in_pcsx2()
        elif action == "exit":
            self.parent.set_ui_mode(theme.MODE_CLASSIC)

    # ---- Big Picture RPC (called from web/bigpicture.html) ----
    def bp_rpc_async_methods(self) -> Dict[str, Any]:
        """Requests that take a while: fn(arg, reply) answers later via reply(dict)
        and may post progress lines with reply.progress(text)."""
        return {
            "install.prepare": self._rpc_install_prepare,
            "install.apply": self._rpc_install_apply,
        }

    def bp_rpc_methods(self) -> Dict[str, Any]:
        return {
            "settings.get": lambda a: self._rpc_settings(),
            "settings.set_option": self._rpc_set_option,
            "settings.set_path": self._rpc_set_path,
            "settings.detect": lambda a: self._rpc_detect(),
            "fs.list": self._rpc_fs_list,
            "texture.remember": self._rpc_remember_source,
            "game.add": self._rpc_add_game,
            "game.remove": self._rpc_remove_game,
            "library.scan": self._rpc_scan,
            "library.patch_all": lambda a: ({"error": e} if (e := self._patch_all_games(confirm=False)) else {"ok": True}),
            "prefs.get": lambda a: self._rpc_prefs(),
            "prefs.set": self._rpc_set_pref,
            "game.favorite": self._rpc_favorite,
            "app.info": lambda a: {"name": APP_NAME, "version": APP_VERSION,
                                   "games": len(self.games), "pcsx2": bool(self.parent.settings_tab.pcsx2_exe.text().strip())},
        }

    # Big Picture's own preferences. Kept in the app settings (not the page's
    # localStorage, which QtWebEngine's default profile forgets on exit).
    BP_PREFS = {"sounds": True, "volume": 0.5, "motion": "full", "mode": "shelf",
                "zoom": 1.0, "filter": "all", "clock": True}

    def _rpc_prefs(self) -> dict:
        s = app_settings()
        out = {}
        for key, default in self.BP_PREFS.items():
            v = s.value(f"bp/{key}", default)
            try:
                if isinstance(default, bool):
                    v = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes")
                elif isinstance(default, float):
                    v = float(v)
                else:
                    v = str(v)
            except (TypeError, ValueError):
                v = default
            out[key] = v
        return out

    def _rpc_set_pref(self, a: dict) -> dict:
        key = a.get("key")
        if key not in self.BP_PREFS:
            return {"error": f"unknown preference {key!r}"}
        app_settings().setValue(f"bp/{key}", a.get("value"))
        return self._rpc_prefs()

    def _favorites(self) -> set:
        v = app_settings().value("library/favorites", [])
        if isinstance(v, str):
            v = [v] if v else []
        return set(v or [])

    def _last_played(self) -> Dict[str, float]:
        try:
            return {k: float(t) for k, t in json.loads(app_settings().value("library/last_played", "{}", str) or "{}").items()}
        except (ValueError, TypeError, AttributeError):
            return {}

    def _rpc_favorite(self, a: dict) -> dict:
        serial = a.get("serial", "")
        if serial not in self.games:
            return {"error": "That game isn't in the library."}
        favs = self._favorites()
        on = bool(a.get("on", serial not in favs))
        (favs.add if on else favs.discard)(serial)
        app_settings().setValue("library/favorites", sorted(favs))
        return {"serial": serial, "favorite": on}

    def _patches_installed_index(self) -> set:
        """Serials that have a PCSX2 patch file installed (<SERIAL>_<CRC>.pnach)."""
        d = (self.parent.state.pcsx2_paths or {}).get('patches', '')
        try:
            return {n.split('_', 1)[0].upper() for n in os.listdir(d) if n.lower().endswith('.pnach')} if d else set()
        except OSError:
            return set()

    def _rpc_settings(self) -> dict:
        t = self.parent.settings_tab
        text, kind = t.folder_status()
        have = os.path.isfile(t._ini_path())
        ini = t._ini_path()
        return {
            "userDir": t.user_dir.text().strip(), "exe": t.pcsx2_exe.text().strip(),
            "status": {"text": text, "kind": kind},
            "options": [{"section": s, "key": k, "label": label, "tip": tip, "enabled": have,
                         "on": bool(have and ini_get_bool(ini, k, s))} for label, tip, s, k in PCSX2_OPTIONS],
            "note": ("Close PCSX2 before changing these -- it rewrites its settings file when it exits." if have
                     else "Available once PCSX2.ini exists (start PCSX2 once)."),
        }

    def _rpc_set_option(self, a: dict) -> dict:
        t = self.parent.settings_tab
        ok = ini_set_bool(t._ini_path(), a["key"], bool(a["on"]), a["section"])
        out = self._rpc_settings()
        if not ok:
            out["error"] = "Couldn't write PCSX2.ini. Is the data folder correct?"
        return out

    def _rpc_set_path(self, a: dict) -> dict:
        t = self.parent.settings_tab
        path = (a.get("path") or "").strip()
        if a.get("kind") == "exe":
            t.pcsx2_exe.setText(path)
            t._save_pcsx2_exe()
            out = self._rpc_settings()
            exe_dir = os.path.dirname(os.path.abspath(path)) if path else ""
            if exe_dir and os.path.isfile(os.path.join(exe_dir, "portable.ini")) \
                    and os.path.normcase(os.path.abspath(t.user_dir.text().strip() or ".")) != os.path.normcase(exe_dir):
                out["portableSuggestion"] = exe_dir
        else:
            t.user_dir.setText(path)
            t._update_subs()
            out = self._rpc_settings()
        self._refresh_library(clear_status=True)
        return out

    def _rpc_detect(self) -> dict:
        t = self.parent.settings_tab
        guesses = default_pcsx2_user_dirs(t.pcsx2_exe.text().strip())
        if not guesses:
            out = self._rpc_settings()
            out["error"] = "Couldn't find PCSX2's data folder. Start PCSX2 once, or choose the folder yourself."
            return out
        return self._rpc_set_path({"kind": "userDir", "path": guesses[0]})

    @staticmethod
    def _rpc_fs_list(a: dict) -> dict:
        """List a folder for the in-page file browser. Empty path = the drives/home."""
        path = (a.get("path") or "").strip()
        exts = tuple(e.lower() for e in (a.get("exts") or []))
        if not path:
            roots = []
            if IS_WINDOWS:
                import string
                roots = [f"{c}:\\" for c in string.ascii_uppercase if os.path.exists(f"{c}:\\")]
            roots.append(os.path.expanduser("~"))
            return {"path": "", "parent": None, "dirs": [{"name": r, "path": r} for r in roots], "files": []}
        p = os.path.abspath(path)
        if not os.path.isdir(p):
            p = os.path.dirname(p) if os.path.isdir(os.path.dirname(p)) else os.path.expanduser("~")
        dirs, files = [], []
        try:
            for e in os.scandir(p):
                if e.name.startswith(('.', '$')):
                    continue
                try:
                    if e.is_dir():
                        dirs.append({"name": e.name, "path": e.path})
                    elif exts and e.name.lower().endswith(exts):
                        files.append({"name": e.name, "path": e.path})
                except OSError:
                    continue
        except OSError as err:
            return {"path": p, "parent": os.path.dirname(p), "dirs": [], "files": [], "error": str(err)}
        key = lambda x: x["name"].lower()
        parent = os.path.dirname(p)
        return {"path": p, "parent": "" if parent == p else parent,
                "dirs": sorted(dirs, key=key)[:500], "files": sorted(files, key=key)[:500]}

    def _rpc_install_prepare(self, a: dict, reply) -> None:
        game = self.games.get(a.get("serial", ""))
        if not game:
            reply({"error": "That game isn't in the library."})
            return
        self._selected_serial = game.serial

        def _found(opts, err):
            if err:
                reply({"error": err})
                return
            self._bp_install_ctx = (game, opts)
            reply(self._enhancements_json(game, opts))
        self._gather_async(game, _found)

    @staticmethod
    def _enhancements_json(game: GameEntry, o: dict) -> dict:
        rec = o["recommendation"]
        return {
            "serial": game.serial, "title": game.title or game.serial, "crc": game.crc or "",
            "crcSource": game.crc_source or "",
            "recommended": {"summary": rec.summary, "patches": {str(k): v for k, v in rec.patches.items()},
                            "texture": rec.texture},
            "patches": [{"label": f"{p['file'].serial} · CRC {p['file'].crc}" + ("  (your disc)" if p["exact"] else ""),
                         "exact": p["exact"], "installed": p["installed"], "update": p.get("update", False), "always": p["always"],
                         "groups": [dict(g.to_json(), enabled=g.enabled, kindLabel=enhancements.KIND_LABEL[g.kind])
                                    for g in p["groups"]]} for p in o["patches"]],
            "cheats": [{"name": c.get("name") or "Cheat", "description": c.get("description", ""),
                        "source": c.get("source", ""), "enabled": c.get("enabled", False)} for c in o["cheats"]],
            "cheatNote": o["cheat_note"], "cheatsInstalled": o["cheats_installed"], "cheatsForeign": o["cheats_foreign"],
            "textures": [{"label": texture_candidate_label(t), "confidence": t.get("confidence", ""),
                          "confidenceLabel": enhancements.CONFIDENCE_LABEL.get(t.get("confidence"), ""),
                          "score": t.get("score", 0)} for t in o["textures"]],
            "texturesInstalled": o["textures_installed"],
            "checks": [c.to_json() for c in o["checks"]],
            "log": o["log"],
        }

    def _rpc_install_apply(self, a: dict, reply) -> None:
        ctx = getattr(self, "_bp_install_ctx", None)
        if not ctx:
            reply({"error": "Nothing to apply."})
            return
        self._bp_install_ctx = None
        game, o = ctx

        def _done(results, followups):
            offers = self._run_followups(followups, ask=False)
            self._bp_offers = offers
            reply({"results": results,
                   "remember": [{"i": i, "label": entry.get('github_repo') or entry.get('archive_org_id') or "this source"}
                                for i, (_s, entry, _u) in enumerate(offers)]})
        self._apply_async(game, o, a, on_done=_done, on_progress=reply.progress)

    def _rpc_remember_source(self, a: dict) -> dict:
        offers = getattr(self, "_bp_offers", []) or []
        i = a.get("i", -1)
        if not isinstance(i, int) or not 0 <= i < len(offers):
            return {"error": "Nothing to remember."}
        serial, entry, url = offers[i]
        from urllib.parse import unquote
        clean = {'name': entry.get('name') or entry.get('github_repo') or entry.get('archive_org_id'),
                 'asset_pattern': unquote(os.path.basename(url.split('?')[0]))}
        if entry.get('archive_org_id'):
            clean['archive_org_id'] = entry['archive_org_id']
        else:
            clean['github_repo'] = entry.get('github_repo')
        return {"ok": True} if save_texture_source(serial, clean) else {"error": "Couldn't update texture_sources.json."}

    def _rpc_add_game(self, a: dict) -> dict:
        err = self._add_game(a.get("serial", ""), a.get("title", ""))
        return {"error": err} if err else {"ok": True}

    def _rpc_remove_game(self, a: dict) -> dict:
        self._forget_game(a.get("serial", ""))
        return {"ok": True}

    def _rpc_scan(self, a: dict) -> dict:
        folder = (a.get("path") or "").strip()
        if not os.path.isdir(folder):
            return {"error": "That folder doesn't exist."}
        self._scan_folder(folder)
        return {"ok": True}

    # ---- cover art ----
    def _load_cover_if_missing(self, game: GameEntry):
        """The bulk CoverPrefetchWorker (see _prefetch_missing_covers) covers
        most games; this is the fallback for one that slipped through (e.g.
        added manually after the last prefetch pass)."""
        cache_path = cover_cache_path(game.serial, self.COVER_CACHE_DIR)
        if os.path.isfile(cache_path) or self._cover_inflight == game.serial:
            return
        candidates = build_cover_candidates(game.serial)
        if not candidates:
            return
        try:
            os.makedirs(self.COVER_CACHE_DIR, exist_ok=True)
        except Exception:
            pass
        self._cover_inflight = game.serial
        worker = CoverFetchWorker(candidates, cache_path, parent=self)
        worker.fetched.connect(lambda path, s=game.serial: self._on_cover_ready(s, path))
        worker.fetch_failed.connect(self._clear_cover_inflight)
        self._start_worker(worker)

    def _clear_cover_inflight(self):
        self._cover_inflight = None

    def _on_cover_ready(self, serial: str, path: str):
        if self._cover_inflight == serial:
            self._cover_inflight = None
        self._icon_cache.pop((serial, self.LIST_ICON[0]), None)
        self._grid_icons.pop(serial, None)
        self.bp.update_cover(serial, path)
        self._refresh_timer.start()

    # ---- scanning ----
    def _resolve_disc_crcs(self):
        """Read the exact CRC from the disc image of every game that has one but
        whose CRC came from a database (or is missing). Databases are often wrong
        or for another revision, and PCSX2 only loads files named after the real
        CRC -- so this is what makes cheats and patches line up for certain."""
        todo = [(g.serial, g.source_path) for g in self.games.values()
                if g.source_path and g.crc_source != 'disc' and not g.source_path.lower().endswith('.chd')
                and os.path.isfile(g.source_path)]
        if not todo or self._busy:
            return

        def _work(_progress):
            return [(serial, disc.read_disc_info(path)) for serial, path in todo]

        def _done(results, _err):
            changed = 0
            for serial, info in results or []:
                g = self.games.get(serial)
                if g and info and info.crc:
                    if g.crc != info.crc:
                        changed += 1
                    g.crc, g.crc_source = info.crc, 'disc'
            self._save_library()
            if changed:
                self._refresh_library(clear_status=True)
                self.parent.flash_status(f"Read the exact CRC from {changed} disc image(s).")
        self._run_task(_work, _done, busy=False)

    def _scan_folder(self, folder: Optional[str] = None):
        if not folder:
            folder = QFileDialog.getExistingDirectory(self, "Select your games folder", os.path.expanduser("~"))
        if not folder:
            return
        self._set_busy(True)
        self.parent.show_progress(0, 0, "Scanning…")
        worker = GameScanWorker(folder, parent=self)
        worker.progressed.connect(self._on_scan_progress)
        worker.finished.connect(self._on_scan_finished)
        self._start_worker(worker)

    def _on_scan_progress(self, current, total):
        self.parent.show_progress(current, total, "Scanning…")

    def _on_scan_finished(self, entries, total_files):
        self.parent.hide_progress()
        self._set_busy(False)
        added = 0
        for g in entries:
            old = self.games.get(g.serial)
            if old is None:
                self.games[g.serial] = g
                added += 1
            else:
                # Already known: keep the user's title, but take the disc's path and exact CRC.
                old.source_path = old.source_path or g.source_path
                if g.crc_source == 'disc':
                    old.crc, old.crc_source = g.crc, 'disc'
        self._status_cache.clear()
        self._save_library()
        self._refresh_library()

        if total_files == 0:
            msg = "No disc images (.iso/.bin/.chd/.cso) were found in that folder."
        elif not entries:
            msg = (f"Found {total_files} disc image(s) in that folder, but couldn't determine "
                   f"a serial for any of them (filename has no serial, and reading SYSTEM.CNF "
                   f"didn't work for .bin/.chd/.cso -- only plain .iso is supported for that).")
        else:
            msg = f"Found {total_files} disc image(s) in that folder, added {added} new to your library."
        self._notify(msg, "Scan Complete")
        self._prefetch_missing_covers()

    # ---- bulk cover prefetch (matches PCSX2's own "fetch all covers" behavior) ----
    def _prefetch_missing_covers(self):
        if requests is None or not self.games:
            return
        try:
            os.makedirs(self.COVER_CACHE_DIR, exist_ok=True)
        except Exception:
            pass
        missing = [s for s in self.games if not os.path.isfile(cover_cache_path(s, self.COVER_CACHE_DIR))]
        if not missing:
            return
        worker = CoverPrefetchWorker(missing, self.COVER_CACHE_DIR, parent=self)
        worker.cover_ready.connect(self._on_cover_prefetched)
        self._start_worker(worker)

    def _on_cover_prefetched(self, serial: str):
        self._on_cover_ready(serial, cover_cache_path(serial, self.COVER_CACHE_DIR))

    def _set_busy(self, busy: bool):
        self._busy = busy
        self._update_action_states()

    def refresh(self):
        """Rebuild both views from the current library and PCSX2 paths."""
        self._refresh_library(clear_status=True)

    def focus_active(self):
        target = self.bp if self.ui_mode == theme.MODE_BIG_PICTURE else self._active_view()
        target.setFocus()

    # ---- manual add ----
    def _add_game_manually(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("Add Game")
        lay = QFormLayout(dlg)
        serial_edit = QLineEdit()
        serial_edit.setPlaceholderText("e.g. SLUS-20946")
        title_edit = QLineEdit()
        title_edit.setPlaceholderText("Optional -- looked up automatically if left blank")
        lay.addRow("Serial:", serial_edit)
        lay.addRow("Title:", title_edit)
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        lay.addRow(btns)
        if dlg.exec() != QDialog.Accepted:
            return
        err = self._add_game(serial_edit.text(), title_edit.text())
        if err:
            QMessageBox.warning(self, "Invalid Serial", err)

    def _add_game(self, serial_text: str, title_text: str = "") -> Optional[str]:
        """Add a game by serial. Returns an error message, or None on success."""
        m = SERIAL_RE.search((serial_text or "").strip())
        if not m:
            return "Enter a valid PS2 serial, e.g. SLUS-20946."
        serial = m.group(0).upper().replace('_', '-').replace(' ', '-')
        title = (title_text or "").strip() or bundled_lookup_title(serial) or serial
        self.games[serial] = GameEntry(serial=serial, title=title, crc=None, source_path=None)
        self._selected_serial = serial
        self._save_library()
        self._refresh_library()
        return None

    # ---- sync ----
    def _resolve_crc(self, game: GameEntry, log: list, index=None):
        """Best CRC for `game`, most reliable source first: the disc image, then
        PCSX2's own patch list (when it knows exactly one CRC for the serial),
        then the cheat database, then an online lookup."""
        if game.source_path and game.crc_source != 'disc' and os.path.isfile(game.source_path) \
                and not game.source_path.lower().endswith('.chd'):
            info = disc.read_disc_info(game.source_path)
            if info and info.crc:
                if game.crc and game.crc != info.crc:
                    log.append(f"CRC: corrected {game.crc} -> {info.crc} (read from your disc image).")
                game.crc, game.crc_source = info.crc, 'disc'
        if game.crc_source == 'disc' and game.crc:
            log.append(f"CRC: {game.crc}, read from your disc image.")
            return
        if not game.crc and index:
            crcs = sorted({pf.crc for pf in index if pf.serial == patches_online.normalize_serial(game.serial)})
            if len(crcs) == 1:
                game.crc, game.crc_source = crcs[0], "PCSX2's patch list"
                log.append(f"CRC: {game.crc}, the only revision in PCSX2's patch list.")
                return
        if not game.crc:
            local = find_local_cheats(game.serial)
            if local and local[1]:
                game.crc, game.crc_source = local[1].upper(), 'cheat database'
                log.append(f"CRC: {game.crc}, from the cheat database.")
                return
        if not game.crc:
            try:
                _t, resolved, _h = cheat_online.resolve_serial_online([game.serial, norm_serial_key(game.serial)])
                if resolved:
                    game.crc, game.crc_source = resolved.upper(), 'online lookup'
                    log.append(f"CRC: {game.crc}, from an online lookup.")
                    return
            except Exception as e:
                log.append(f"CRC: online lookup failed ({e}).")
        if game.crc:
            log.append(f"CRC: {game.crc} ({game.crc_source or 'saved earlier'}).")
        else:
            log.append("CRC: unknown. Add the game's disc image so PCTX can read it.")

    @staticmethod
    def _cheat_key(name: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", (name or "").lower())

    def _gather_cheat_candidates(self, game: GameEntry, cheats_dir: str):
        """Every cheat available for the game, from every source, merged and
        de-duplicated (the local database, the CRC-keyed GitHub collection, and
        GameHacking.org / PSXDataCenter). Returns
        (candidates, source_note, log_lines, already_installed)."""
        log, out, seen, sources = [], [], set(), []

        def add(items, source):
            n = 0
            for c in items:
                key = self._cheat_key(c.get('name'))
                if not c.get('codes') or not key or key in seen:
                    continue
                seen.add(key)
                out.append(dict(c, source=source))
                n += 1
            if n:
                sources.append(source)
            return n

        local = find_local_cheats(game.serial)
        if local:
            title, crc, cheats = local
            same = not crc or not game.crc or crc.upper() == game.crc.upper()
            if same:
                log.append(f"Local database: {add(cheats, 'local database')} cheat(s) for \"{title}\".")
            else:
                log.append(f"Local database: has cheats for CRC {crc.upper()}, but your disc is {game.crc} "
                           "(another revision), so they were skipped.")
        else:
            log.append("Local database: no entry for this serial.")

        if game.crc:
            try:
                n = add(fetch_github_pnach_cheats(game.crc), 'GitHub collection')
                log.append(f"GitHub cheat collection: {n} cheat(s) for CRC {game.crc}." if n
                           else "GitHub cheat collection: nothing for this CRC.")
            except Exception as e:
                log.append(f"GitHub cheat collection: failed ({e}).")
        else:
            log.append("GitHub cheat collection: skipped (no CRC).")

        try:
            results = fetch_and_cache_cheats(game.serial) or []
        except Exception as e:
            results = []
            log.append(f"GameHacking.org / PSXDataCenter: failed ({e}).")
        codes = [{'name': e.get('title') or e.get('source', 'Cheat'), 'description': e.get('source', ''), 'codes': e['codes']}
                 for e in results if e.get('codes')]
        n = add(codes, 'GameHacking.org')
        log.append(f"GameHacking.org / PSXDataCenter: {n} more cheat(s)." if n else "GameHacking.org / PSXDataCenter: nothing new.")

        already = self._cheats_already_installed(cheats_dir, game.crc) if game.crc else False
        note = "from " + (", ".join(sources) if sources else "no source")
        return out, note, log, already

    TEXTURE_SEARCH_LIMIT = 5     # best unverified hits checked for a real downloadable .zip/.7z

    def _gather_texture_candidates(self, game: GameEntry, textures_dir: str):
        """Texture packs for the game: the curated manifest, then GitHub and the
        Internet Archive searched with several spellings of the title, every
        hit scored for how well it matches this game, obvious mismatches
        dropped, and the best GitHub hits checked for an actual download.
        Returns (candidates best-first, log_lines, already_installed)."""
        existing_dir = texture_upscale.replacement_textures_dir(textures_dir, game.serial) if textures_dir else ''
        already_installed = bool(existing_dir and os.path.isdir(existing_dir) and os.listdir(existing_dir))
        log = ["A pack is already installed; searched anyway so you can replace it."] if already_installed else []

        curated = [dict(c, verified=c.get('verified', True)) for c in get_texture_pack_options(game.serial)]
        log.append(f"Curated list: {len(curated)} verified pack(s)." if curated else "Curated list: no entry for this game.")
        found = list(curated)

        # The PS2 texture-pack catalog, keyed by disc serial: the most reliable
        # source after our own list. Automatic downloads are checked for a real
        # .zip/.7z; anything that can't be fetched becomes a browser link.
        catalog, cat_err = texture_catalog.load(self.PATCH_CACHE_DIR)
        if cat_err:
            log.append(f"Texture-pack catalog: {cat_err}.")
        known = {(c.get('github_repo') or c.get('archive_org_id') or '').lower() for c in curated}
        cat = [c for c in texture_catalog.candidates_for(catalog, game.serial, game.title or "")
               if (c.get('github_repo') or c.get('archive_org_id') or c.get('manual_url') or '').lower() not in known]
        auto = 0
        for c in cat:
            if not c.get('manual_url'):
                try:
                    ok = bool(resolve_texture_source(c))
                except Exception:
                    ok = False
                if ok:
                    auto += 1
                else:
                    page = (f"https://archive.org/details/{c['archive_org_id']}" if c.get('archive_org_id')
                            else f"https://github.com/{c.get('github_repo')}")
                    c.pop('archive_org_id', None)
                    c.pop('github_repo', None)
                    c['manual_url'] = page
            c['score'] = enhancements.CONFIDENCE_SCORE.get(c['confidence'], 80) - (10 if c.get('manual_url') else 0) \
                - (20 if c.get('paid') else 0)
            found.append(c)
        if catalog:
            paid = sum(1 for c in cat if c.get('paid'))
            log.append(f"Texture-pack catalog: {len(cat)} pack(s) listed for this game"
                       + (f" ({auto} downloadable here, {len(cat) - auto - paid} through your browser, {paid} paid)."
                          if cat else "."))

        if any(not c.get('manual_url') for c in found):
            log.append("Online search: skipped, a listed pack is already available.")
        elif requests is None:
            log.append("Online search: skipped (requests isn't installed).")
        else:
            variants = enhancements.title_variants(game.title or "")[:2]
            gh_all, ia_all = [], []
            for v in variants:
                gh, err = search_github_texture_packs(game.serial, v)
                if err:
                    log.append(f"GitHub search ({v}): {err}")
                    break
                gh_all += gh
            for v in variants:
                ia, err = search_archive_org_texture_packs(game.serial, v)
                if err:
                    log.append(f"Internet Archive search ({v}): {err}")
                    break
                ia_all += ia
            ranked = enhancements.rank_texture_candidates(gh_all + ia_all, game.title or "", game.serial, game.crc or "")
            dropped = len({(c.get('github_repo') or c.get('archive_org_id')) for c in gh_all + ia_all}) - len(ranked)
            checked = 0
            no_download = 0
            for c in ranked:
                if checked >= self.TEXTURE_SEARCH_LIMIT:
                    break
                # Only offer what can really be downloaded and unpacked (.zip/.7z).
                checked += 1
                try:
                    c['downloadable'] = bool(resolve_texture_source(c))
                except Exception:
                    c['downloadable'] = False
                if not c['downloadable']:
                    no_download += 1
                    continue
                c['score'], c['confidence'] = enhancements.score_texture_candidate(c, game.title or "", game.serial, game.crc or "")
                found.append(c)
            if no_download:
                log.append(f"Online search: {no_download} match(es) skipped because they have no .zip/.7z to download.")
            log.append(f"Online search: {len(gh_all) + len(ia_all)} hit(s) for {', '.join(repr(v) for v in variants) or 'the title'}; "
                       f"{len(found) - len(curated)} look like this game" + (f", {dropped} unrelated hit(s) hidden." if dropped > 0 else "."))
        if textures_dir and texture_upscale.has_dumped_textures(textures_dir, game.serial):
            found.append({'name': 'AI upscale of your own dumped textures (Real-ESRGAN)', 'ai_upscale': True,
                          'verified': True, 'confidence': 'fallback', 'score': 1})
            log.append("PCSX2 has dumped this game's textures, so an AI upscale is offered too.")
        for c in found:
            c.setdefault('confidence', 'verified' if c.get('verified', True) else 'medium')
            c.setdefault('score', 100 if c['confidence'] == 'verified' else 50)
        found.sort(key=lambda c: -c.get('score', 0))
        return found, log, already_installed

    def _install_dirs(self) -> Tuple[Optional[dict], Optional[str]]:
        paths = self.parent.state.pcsx2_paths or {}
        cheats_dir = paths.get('cheats', '')
        if not cheats_dir or not os.path.isdir(cheats_dir):
            return None, "Set your PCSX2 folder in Settings first."
        t = self.parent.settings_tab
        return {"cheats_dir": cheats_dir, "textures_dir": paths.get('textures', ''),
                "patches_dir": paths.get('patches', ''), "data_dir": t.user_dir.text().strip(),
                "ini": t._ini_path()}, None

    def _gather_install_options(self, game: GameEntry, dirs: Optional[dict] = None):
        """Everything about this game's enhancements -- what's available, what's
        installed, what's switched on in PCSX2, what we recommend and what's in
        the way -- without changing anything. Returns (options, None) or
        (None, error). Touches no widgets: safe on a worker thread."""
        if dirs is None:
            dirs, err = self._install_dirs()
            if err:
                return None, err
        log: List[str] = []
        index, idx_err = patches_online.load_index(self.PATCH_CACHE_DIR)
        self._resolve_crc(game, log, index)
        cheats, cheat_note, cheat_log, cheats_installed = self._gather_cheat_candidates(game, dirs["cheats_dir"])
        textures, texture_log, textures_installed = self._gather_texture_candidates(game, dirs["textures_dir"])
        patches, patch_log = self._gather_patch_candidates(game, dirs["patches_dir"], index, idx_err)

        ini = dirs.get("ini") or ""
        has_ini = bool(ini) and os.path.isfile(ini)
        flags = {k: (ini_get_bool(ini, k, s) if has_ini else None) for _l, _t, s, k in PCSX2_OPTIONS}
        gs = enhancements.game_settings_path(dirs["data_dir"], game.serial, game.crc) if (game.crc and dirs.get("data_dir")) else ""
        en_patch = set(enhancements.read_list(gs, enhancements.PATCHES_SECTION)) if gs else set()
        dis_patch = set(enhancements.read_list(gs, enhancements.PATCHES_SECTION, "Disable")) if gs else set()
        en_cheat = set(enhancements.read_list(gs, enhancements.CHEATS_SECTION)) if gs else set()
        auto = {enhancements.WS_PATCH_NAME: flags.get("EnableWideScreenPatches"),
                enhancements.NI_PATCH_NAME: flags.get("EnableNoInterlacingPatches")}
        for p in patches:
            for g in p["groups"]:
                g.enabled = p["installed"] and (g.name in en_patch or (bool(auto.get(g.name)) and g.name not in dis_patch))
        for c in cheats:
            c["enabled"] = cheats_installed and enhancements.pnach_group_name(c.get("name")) in en_cheat
        cheat_file = os.path.join(dirs["cheats_dir"], f"{game.crc.upper()}.pnach") if game.crc else ""
        foreign = bool(cheat_file and os.path.isfile(cheat_file) and "Generated by PCTX" not in
                       open(cheat_file, encoding="utf-8", errors="ignore").read(4096))

        rec = enhancements.recommend(patches, textures)
        checks = enhancements.health_checks(
            crc=game.crc, crc_source=game.crc_source or "", has_ini=has_ini, ini_flags=flags,
            cheats_installed=cheats_installed, cheats_enabled=sum(1 for c in cheats if c["enabled"]),
            textures_installed=textures_installed, patches_installed=any(p["installed"] for p in patches),
            patches_enabled=sum(1 for p in patches for g in p["groups"] if g.enabled),
            ws_installed=any(p["installed"] and any(g.kind == "widescreen" for g in p["groups"]) for p in patches))
        log = (log + ["", "Patches:"] + [f"  {l}" for l in patch_log] + ["", "Cheats:"] + [f"  {l}" for l in cheat_log]
               + ["", "Textures:"] + [f"  {l}" for l in texture_log])
        return dict(dirs, cheats=cheats, cheat_note=cheat_note, cheats_installed=cheats_installed, cheats_foreign=foreign,
                    textures=textures, textures_installed=textures_installed, patches=patches, log=log,
                    game_settings=gs, flags=flags, recommendation=rec, checks=checks), None

    def _run_task(self, fn, on_done, on_progress=None, busy=True):
        """Run `fn(progress)` on a worker thread; `on_done(result, error)` runs on
        the UI thread afterwards. The UI stays fully interactive meanwhile."""
        if busy:
            self._set_busy(True)
        worker = TaskWorker(fn, parent=self)
        if on_progress:
            worker.progressed.connect(on_progress)

        def _finish(result, error):
            if busy:
                self._set_busy(False)
            on_done(result, error)
        worker.done.connect(_finish)
        self._start_worker(worker)
        return worker

    def _gather_async(self, game: GameEntry, on_done):
        """on_done(options, error) on the UI thread."""
        dirs, err = self._install_dirs()
        if err:
            on_done(None, err)
            return
        def _work(_progress):
            return self._gather_install_options(game, dirs)
        self._run_task(_work, lambda res, error: on_done(*(res if res else (None, error or "Search failed."))))

    def _apply_async(self, game: GameEntry, o: dict, sel: dict, on_done, on_progress=None):
        """Install on a worker thread. on_done(results, followups) on the UI thread;
        `followups` are things only the UI thread may do (open a URL, ask to
        remember a texture source)."""
        followups: list = []

        def _work(progress):
            return self._apply_install(game, o, sel, progress=progress, followups=followups)

        def _done(results, error):
            self._save_library()
            self._refresh_library(clear_status=True)
            on_done(results or [f"Install failed: {error}"], followups)
        self._run_task(_work, _done, on_progress)

    def _apply_install(self, game: GameEntry, o: dict, sel: dict, progress=None,
                       followups: Optional[list] = None) -> List[str]:
        """Make the game's enhancements match `sel`, and switch on in PCSX2 what
        is needed for them to actually run. Runs on a worker thread: no widgets.

        sel = {"patches": {file_index: [group names to enable]}, "cheats": [indices to enable],
               "cheatsOverwrite": bool, "texture": index | -1, "texturesOverwrite": bool,
               "remove": {"cheats", "patches", "textures"} subset, "fixes": [PCSX2.ini keys to turn on]}"""
        progress = progress or (lambda _t: None)
        followups = followups if followups is not None else []
        results: List[str] = []
        remove = set(sel.get("remove") or [])
        gs = o.get("game_settings") or ""
        ini = o.get("ini") or ""
        flags = o.get("flags") or {}
        sections = {k: s for _l, _t, s, k in PCSX2_OPTIONS}

        def turn_on(key: str, why: str):
            if ini and os.path.isfile(ini) and flags.get(key) is False and ini_set_bool(ini, key, True, sections[key]):
                flags[key] = True
                results.append(f"PCSX2: turned on \"{why}\".")

        # ---- patches ----
        chosen: Dict[int, List[str]] = {int(k): list(v) for k, v in (sel.get("patches") or {}).items()}
        all_names = [g.name for p in o["patches"] for g in p["groups"]]
        if "patches" in remove:
            n = 0
            for p in o["patches"]:
                path = patches_online.installed_path(o["patches_dir"], p["file"])
                if os.path.isfile(path):
                    os.remove(path)
                    n += 1
            if gs:
                enhancements.set_enabled(gs, enhancements.PATCHES_SECTION, [], all_names)
            results.append(f"Patches: removed {n} file(s).")
        elif o["patches"]:
            installed, enable = 0, []
            for i, p in enumerate(o["patches"]):
                names = chosen.get(i, [])
                if names or (p["always"] and i in chosen):
                    progress(f"Installing {p['file'].filename}…")
                    patches_online.install(p["file"], p["text"], o["patches_dir"])
                    installed += 1
                    enable += [n for n in names if n != "*"]
            if gs:
                auto = {enhancements.WS_PATCH_NAME: flags.get("EnableWideScreenPatches"),
                        enhancements.NI_PATCH_NAME: flags.get("EnableNoInterlacingPatches")}
                disable = [n for n in all_names if auto.get(n) and n not in enable]
                enhancements.set_enabled(gs, enhancements.PATCHES_SECTION, enable, all_names, disable)
            if installed or enable:
                results.append(f"Patches: {installed} file(s) installed, {len(enable)} patch(es) switched on"
                               + (f" ({', '.join(enable[:4])}{'…' if len(enable) > 4 else ''})." if enable else "."))
                if enable and not gs:
                    results.append("Patches: couldn't switch them on automatically (CRC unknown) -- enable them in "
                                   "PCSX2's game properties > Patches.")
            elif any(g.enabled for p in o["patches"] for g in p["groups"]):
                results.append("Patches: all switched off.")

        # ---- cheats ----
        cheats = o["cheats"]
        picked = [cheats[i] for i in (sel.get("cheats") or []) if isinstance(i, int) and 0 <= i < len(cheats)]
        cheat_names = [enhancements.pnach_group_name(c.get("name")) for c in cheats]
        if "cheats" in remove:
            path = os.path.join(o["cheats_dir"], f"{game.crc.upper()}.pnach") if game.crc else ""
            if path and os.path.isfile(path):
                os.remove(path)
            if gs:
                enhancements.set_enabled(gs, enhancements.CHEATS_SECTION, [], cheat_names)
            results.append("Cheats: removed.")
        elif cheats and (picked or o["cheats_installed"]):
            if not game.crc:
                results.append("Cheats: can't be installed without the game's CRC -- add its disc image first.")
            else:
                if o.get("cheats_foreign") and not sel.get("cheatsOverwrite"):
                    results.append("Cheats: kept your existing cheat file (it wasn't made by PCTX).")
                else:
                    progress("Writing cheats…")
                    # Every cheat goes in the file (so it can be toggled in PCSX2
                    # later); only the chosen ones are switched on.
                    write_cheats_pnach(game.title or game.serial, game.serial, game.crc, cheats, o["cheats_dir"])
                if gs:
                    enhancements.set_enabled(gs, enhancements.CHEATS_SECTION,
                                             [enhancements.pnach_group_name(c.get("name")) for c in picked], cheat_names)
                results.append(f"Cheats: {len(picked)} of {len(cheats)} switched on." if picked
                               else f"Cheats: {len(cheats)} available, none switched on.")
                if picked:
                    turn_on("EnableCheats", "Enable cheats")

        # ---- textures ----
        textures = o["textures"]
        tex_i = sel.get("texture", -1)
        entry = textures[tex_i] if isinstance(tex_i, int) and 0 <= tex_i < len(textures) else None
        if "textures" in remove:
            d = texture_upscale.replacement_textures_dir(o["textures_dir"], game.serial)
            if os.path.isdir(d):
                shutil.rmtree(d, ignore_errors=True)
            results.append("Texture pack: removed.")
        elif entry:
            if o["textures_installed"] and not sel.get("texturesOverwrite"):
                results.append("Texture pack: one is already installed; left unchanged.")
            else:
                if o["textures_installed"]:
                    shutil.rmtree(texture_upscale.replacement_textures_dir(o["textures_dir"], game.serial), ignore_errors=True)
                line = self._install_texture_entry(game, o["textures_dir"], entry, progress, followups)
                results.append(line.replace("Textures:", "Texture pack:", 1))
                if "installed" in line.lower() or "upscaled" in line.lower():
                    turn_on("LoadTextureReplacements", "Load texture replacements")

        for key in sel.get("fixes") or []:
            if key in sections:
                turn_on(key, next(l for l, _t, s, k in PCSX2_OPTIONS if k == key))
        if not results:
            results.append("Nothing to change.")
        return results

    def _sync_selected(self):
        """Classic: search in the background, then the Install dialog, then
        install in the background with progress in the status bar."""
        game = self._selected_game()
        if not game or self._busy:
            return
        self.parent.show_progress(0, 0, "Looking up patches, cheats and texture packs…")

        def _found(o, err):
            self.parent.hide_progress()
            if err:
                self._notify(err, "Can't search", warn=True)
                return
            dlg = EnhancementsDialog(game, o, parent=self)
            if dlg.exec() != QDialog.Accepted:
                return
            self.parent.show_progress(0, 0, "Applying…")
            self._apply_async(game, o, dlg.selection(), on_done=_installed,
                              on_progress=lambda t: self.parent.show_progress(0, 0, t))

        def _installed(results, followups):
            self.parent.hide_progress()
            self._run_followups(followups, ask=True)
            self._report(results)

        self._gather_async(game, _found)

    def _run_followups(self, followups: list, ask: bool) -> list:
        """Do the UI-thread parts of an install. Returns the texture sources that
        could be remembered when `ask` is False (Big Picture shows its own prompt)."""
        offers = []
        for f in followups:
            if f[0] == "open_url":
                QDesktopServices.openUrl(QUrl(f[1]))
            elif f[0] == "offer_source":
                if ask:
                    self._offer_save_texture_source(*f[1:])
                else:
                    offers.append(f[1:])
        return offers

    PATCH_CACHE_DIR = os.path.join(os.path.expanduser("~"), ".pcsx2_manager_cache")

    PATCH_TEXT_MAX_AGE = 7 * 24 * 3600

    def _patch_text(self, pf) -> str:
        """A patch file's text, cached for a week (they're tiny and rarely change)."""
        path = os.path.join(self.PATCH_CACHE_DIR, "pnach", pf.filename)
        try:
            if os.path.isfile(path) and time.time() - os.path.getmtime(path) < self.PATCH_TEXT_MAX_AGE:
                with open(path, encoding="utf-8") as f:
                    return f.read()
        except OSError:
            pass
        text = patches_online.fetch_text(pf)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
        except OSError:
            pass
        return text

    def _gather_patch_candidates(self, game: GameEntry, patches_dir: str, index=None, idx_err=None):
        """PCSX2's published patch files for this game, each with its named
        groups classified (widescreen / no-interlacing / fix / frame rate / ...).
        Returns (candidates, log_lines)."""
        if index is None:
            index, idx_err = patches_online.load_index(self.PATCH_CACHE_DIR)
        log = []
        if idx_err:
            log.append(f"PCSX2 patch collection: {idx_err}.")
        files, exact = patches_online.find_for_game(index or [], game.serial, game.crc)
        if not files:
            if index:
                log.append("PCSX2 patch collection: nothing published for this game.")
            return [], log
        log.append(f"PCSX2 patch collection: {len(files)} file(s) for {game.serial}"
                   + (f", one matches your CRC {game.crc}." if exact else
                      " (your CRC is unknown or different, so every revision is offered)."))
        out = []
        for pf in files:
            try:
                text = self._patch_text(pf)
            except Exception as e:
                log.append(f"  {pf.filename}: download failed ({e}).")
                continue
            groups = enhancements.parse_groups(text)
            installed = patches_online.is_installed(patches_dir, pf)
            update = False
            if installed:
                try:
                    with open(patches_online.installed_path(patches_dir, pf), encoding="utf-8", errors="ignore") as f:
                        update = f.read().strip() != text.strip()
                except OSError:
                    pass
            if update:
                log.append(f"  {pf.filename}: PCSX2 has published a newer version than the one installed.")
            out.append({"file": pf, "exact": exact and (not game.crc or pf.crc == game.crc.upper()), "text": text,
                        "installed": installed, "update": update, "groups": groups, "always": not groups})
            if groups:
                log.append(f"  {pf.filename}: " + ", ".join(g.name for g in groups))
            else:
                log.append(f"  {pf.filename}: fixes that are always on once installed.")
        return out, log

    def _install_patches(self, game: GameEntry, patches_dir: str, selected: list) -> str:
        if not selected:
            return "Patches: none selected."
        if not patches_dir:
            return "Patches: PCSX2's data folder isn't set."
        names, failed = [], 0
        for pf in selected:
            try:
                text = patches_online.fetch_text(pf)
                patches_online.install(pf, text, patches_dir)
                for name, _desc in patches_online.describe_groups(text):
                    if name not in names:
                        names.append(name)
            except Exception as e:
                failed += 1
                logger.warning(f"[patches] {pf.filename}: {e}")
        done = len(selected) - failed
        msg = f"Patches: installed {done} file(s)" + (f" ({', '.join(names[:4])})" if names else "")
        return msg + (f"; {failed} failed to download." if failed else ".")

    def _patch_all_games(self, confirm: bool = True) -> Optional[str]:
        """Returns an error message (Big Picture shows it), or None once started."""
        paths = self.parent.state.pcsx2_paths or {}
        patches_dir = paths.get('patches', '')
        if not patches_dir or not os.path.isdir(os.path.dirname(patches_dir)):
            if confirm:
                self._notify("Set PCSX2's data folder in Settings first.", "PCSX2 folder not set", warn=True)
            return "Set PCSX2's data folder in Settings first."
        if not self.games:
            if confirm:
                self._notify("Your library is empty.", "No games")
            return "Your library is empty."
        if self._busy:
            return "PCTX is busy with something else; try again in a moment."
        reply = QMessageBox.Yes if not confirm else QMessageBox.question(
            self, "Recommended patches for all games",
            f"Install the patches PCSX2 publishes for the {len(self.games)} game(s) in your library, and switch on "
            f"the recommended ones (widescreen, no-interlacing and bug fixes) for each exact disc?\n\n"
            "They are small text files from PCSX2's own patch collection. Frame-rate unlocks and visual tweaks are "
            "installed but left off; turn them on per game in Enhancements.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        if reply != QMessageBox.Yes:
            return
        self._set_busy(True)
        self.parent.show_progress(0, len(self.games), "Patches")
        worker = PatchAllWorker([(g.serial, g.crc) for g in self.games.values()], patches_dir,
                                self.PATCH_CACHE_DIR, parent=self,
                                data_dir=self.parent.settings_tab.user_dir.text().strip())
        worker.progressed.connect(lambda done, total: self.parent.show_progress(done, total, "Patches"))
        worker.summary.connect(self._on_patch_all_done)
        self._start_worker(worker)

    def _on_patch_all_done(self, s: dict):
        self.parent.hide_progress()
        self._set_busy(False)
        if s.get("error"):
            self._report([f"Patches: {s['error']}."])
            return
        self._report([f"Patches: {s['installed']} new file(s) for {s['with_patches']} of {s['games']} games"
                      f" ({s['skipped']} already installed" + (f", {s['failed']} failed" if s['failed'] else "") + ")"
                      f"; recommended patches switched on for {s['switched_on']} game(s)."])
        self._refresh_library()

    def _install_texture_entry(self, game: GameEntry, textures_dir: str, entry: dict,
                               progress=None, followups: Optional[list] = None) -> str:
        progress = progress or (lambda _t: None)
        followups = followups if followups is not None else []
        if entry.get('ai_upscale'):
            return self._install_ai_upscale(game, textures_dir, progress)
        if entry.get('manual_url'):
            # A source this app won't fetch automatically (e.g. a mega.nz link
            # found by hand) -- open it for the user instead of pretending it's
            # a normal download.
            followups.append(("open_url", entry['manual_url']))
            dest = os.path.join(textures_dir, game.serial, texture_upscale.TEXTURE_REPLACEMENT_SUBDIR)
            return (f"Textures: '{entry.get('name', 'this pack')}' opened in your browser -- "
                    f"this app can't auto-download from that host, so extract it into {dest} yourself.")
        try:
            resolved = resolve_texture_source(entry)
            if not resolved or not resolved[2]:
                return "Textures: couldn't resolve a download for the selected pack."
            display_name, source_label, download_url = resolved

            ext = '.7z' if download_url.split('?')[0].lower().endswith('.7z') else '.zip'
            tmp_dir = os.path.join(os.path.expanduser('~'), '.pcsx2_manager_tmp')
            os.makedirs(tmp_dir, exist_ok=True)
            local_archive = os.path.join(tmp_dir, f"{game.serial}_texture_pack{ext}")
            progress(f"Downloading {display_name}…")
            resp = requests.get(download_url, timeout=60, stream=True)
            resp.raise_for_status()
            total = int(resp.headers.get('content-length') or 0)
            got = 0
            with open(local_archive, 'wb') as f:
                for chunk in resp.iter_content(chunk_size=1024 * 256):
                    if chunk:
                        f.write(chunk)
                        got += len(chunk)
                        if total:
                            progress(f"Downloading {display_name}… {got * 100 // total}%")
            progress("Extracting texture pack…")

            # PCSX2 only loads texture replacements from <textures_dir>/<serial>/replacements/
            # (confirmed against its GSTextureReplacements.cpp source) -- installing straight
            # into <textures_dir>/<serial>/ instead, one level too shallow, is what this used
            # to do, which meant PCSX2 likely never actually picked the pack up.
            game_dir = os.path.join(textures_dir, game.serial)
            from textures_install import perform_pack_installs
            installed, failures = perform_pack_installs(
                [(display_name, local_archive)], game_dir,
                target_hint=texture_upscale.TEXTURE_REPLACEMENT_SUBDIR)
            try:
                os.remove(local_archive)
            except Exception:
                pass

            if installed:
                if not entry.get('verified', True):
                    followups.append(("offer_source", game.serial, entry, download_url))
                return f"Textures: installed '{display_name}' from {source_label}."
            msg = failures[0][2] if failures else "install failed"
            return f"Textures: found a pack but install failed -- {msg}"
        except Exception as e:
            logger.error(f"[LibraryView] Textures install failed for {game.serial}: {e}")
            return f"Textures: error -- {e}"

    def _install_ai_upscale(self, game: GameEntry, textures_dir: str, progress=None) -> str:
        """Batch-upscale whatever PCSX2 has already dumped for this game (its
        own "Dump Textures" graphics option) with Real-ESRGAN, writing the
        result straight into the replacements/ folder PCSX2 loads from.
        Runs on the install worker thread; progress goes to `progress`."""
        progress = progress or (lambda _t: None)
        src_dir = texture_upscale.dumped_textures_dir(textures_dir, game.serial)
        dst_dir = texture_upscale.replacement_textures_dir(textures_dir, game.serial)
        tools_dir = os.path.join(os.path.expanduser('~'), '.pcsx2_manager_tools')
        os.makedirs(tools_dir, exist_ok=True)
        progress("Preparing Real-ESRGAN…")
        exe = texture_upscale.ensure_realesrgan_binary(tools_dir, progress_cb=progress)
        if not exe:
            return "Textures: couldn't get Real-ESRGAN (check your internet connection)."
        progress("Upscaling textures… this can take a few minutes for a large dump.")
        ok, msg = texture_upscale.upscale_textures(exe, src_dir, dst_dir, scale=4)
        if ok:
            return f"Textures: {msg} (AI-upscaled from your dumped textures with Real-ESRGAN)."
        return f"Textures: {msg}"

    def _offer_save_texture_source(self, serial: str, entry: dict, download_url: str):
        """After a successful install from an unverified search result (GitHub
        or Internet Archive), offer to promote it into the persistent curated
        manifest -- closes the loop on "control over sources": your own
        confirmed finds stick around instead of needing to be rediscovered by
        search every time."""
        from urllib.parse import unquote
        asset_name = unquote(os.path.basename(download_url.split('?')[0]))
        source_label = entry.get('github_repo') or entry.get('archive_org_id') or 'this source'
        reply = QMessageBox.question(
            self, "Remember This Source?",
            f"\"{source_label}\" worked for this game.\n\n"
            f"Add it to texture_sources.json so future syncs find it directly "
            f"instead of searching again?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
        )
        if reply != QMessageBox.Yes:
            return
        clean_entry = {'name': entry.get('name') or source_label, 'asset_pattern': asset_name}
        if entry.get('archive_org_id'):
            clean_entry['archive_org_id'] = entry['archive_org_id']
        else:
            clean_entry['github_repo'] = entry.get('github_repo')
        if not save_texture_source(serial, clean_entry):
            QMessageBox.warning(self, "Couldn't Save", "Failed to update texture_sources.json.")

    def _game_install_status(self, game: GameEntry) -> Tuple[bool, bool]:
        """(cheats_installed, textures_installed) from what's actually on disk,
        so the library shows the real state even after restarting the app."""
        paths = self.parent.state.pcsx2_paths or {}
        cheats_dir = paths.get('cheats', '')
        textures_dir = paths.get('textures', '')

        crc = game.crc
        if not crc:
            local = find_local_cheats(game.serial)
            if local:
                crc = local[1]

        cheats_installed = bool(cheats_dir) and self._cheats_already_installed(cheats_dir, crc)

        existing_tex_dir = texture_upscale.replacement_textures_dir(textures_dir, game.serial) if textures_dir else ''
        textures_installed = bool(existing_tex_dir) and os.path.isdir(existing_tex_dir) and bool(os.listdir(existing_tex_dir))

        return cheats_installed, textures_installed

    def _report(self, results: List[str]):
        """Show the outcome of an install -- status bar in Classic, a toast over
        the page in Big Picture (where there is no status bar) -- and refresh
        the Installed columns/badges from what is now on disk."""
        self._refresh_library(clear_status=True)
        if self.ui_mode == theme.MODE_BIG_PICTURE:
            self.bp.show_toast("\n".join(results))
        else:
            self.parent.flash_status("  ·  ".join(results), 20000)

    @staticmethod
    def _cheats_already_installed(cheats_dir: str, crc: str) -> bool:
        return bool(crc) and os.path.isfile(os.path.join(cheats_dir, f"{crc.upper()}.pnach"))

    # ---- worker bookkeeping (matches CheatsTab/TexturesTab pattern) ----
    def _start_worker(self, worker: QThread):
        if getattr(self, '_shutting_down', False):
            return
        worker.setParent(self)
        self._workers.append(worker)

        def _cleanup():
            try:
                self._workers.remove(worker)
            except ValueError:
                pass
            worker.deleteLater()
        release_worker_when_done(worker, _cleanup)
        worker.start()


class MainWindow(QMainWindow):
    # Shared state signals: tabs subscribe instead of reaching into each other directly.
    paths_changed = Signal(dict)          # emitted with the latest PCSX2 paths dict
    current_game_changed = Signal(object)  # emitted with a GameEntry (or None)

    def __init__(self):
        super().__init__()
        self.state = AppState()
        self.ui_mode = theme.MODE_CLASSIC
        self._was_maximized = False
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(QIcon(APP_ICON_ICO_PATH))
        self.resize(1180, 760)
        self.setMinimumSize(820, 520)

        # Custom-drawn title bar (TitleBar/ResizableFrame above). On Windows we keep
        # the window fully native (see nativeEvent()) so Snap/shadow/rounded-corners
        # keep working; elsewhere FramelessWindowHint + manual drag/resize is used.
        if not IS_WINDOWS:
            self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint)

        self.frame = ResizableFrame(self)
        self.outer = QVBoxLayout(self.frame)
        self.outer.setSpacing(0)
        self._set_frame_margins(True)

        self.title_bar = TitleBar(self)
        self.outer.addWidget(self.title_bar)

        # The Advanced Cheat Editor / Texture Manager are big legacy widgets that
        # most sessions never open; they're built on first use (see the
        # cheats_tab / textures_tab properties) instead of at every startup.
        self._cheats_tab: Optional['CheatsTab'] = None
        self._textures_tab: Optional['TexturesTab'] = None
        self.settings_tab = SettingsTab(self)

        self._build_status_bar()
        self.library_view = LibraryView(self)
        self.outer.addWidget(self.library_view, 1)
        self.outer.addWidget(self.status_bar)
        self._build_menus(self.title_bar.menu_bar)

        self.setCentralWidget(self.frame)
        self.setAcceptDrops(True)

        # Theme + visibility for the remembered mode. The window itself is
        # shown (fullscreen or not) by show_initial().
        self.set_ui_mode(app_settings().value('ui/mode', theme.MODE_CLASSIC, str), initial=True)
        self._show_welcome_if_needed()

    @property
    def cheats_tab(self) -> 'CheatsTab':
        if self._cheats_tab is None:
            self._cheats_tab = CheatsTab(self)
            self._cheats_tab.load_paths(self.state.pcsx2_paths or {})
        return self._cheats_tab

    @property
    def textures_tab(self) -> 'TexturesTab':
        if self._textures_tab is None:
            self._textures_tab = TexturesTab(self)
            self._textures_tab.load_paths(self.state.pcsx2_paths or {})
        return self._textures_tab

    def _set_frame_margins(self, classic: bool):
        m = theme.RESIZE_MARGIN if classic else 0
        self.outer.setContentsMargins(m, 0, m, m)

    def _build_status_bar(self):
        self.status_bar = QStatusBar()
        self.status_bar.setSizeGripEnabled(False)
        self.status_hint = QLabel("")
        self.status_hint.setObjectName(theme.OBJ_MUTED_LABEL)
        self.status_count = QLabel("")
        self.status_count.setObjectName(theme.OBJ_MUTED_LABEL)
        self.status_progress = QProgressBar()
        self.status_progress.setFixedWidth(180)
        self.status_progress.setFixedHeight(16)
        self.status_progress.setVisible(False)
        self.status_bar.addWidget(self.status_hint, 1)
        self.status_bar.addPermanentWidget(self.status_progress)
        self.status_bar.addPermanentWidget(self.status_count)

    # ---- status bar API (used by LibraryView) ----
    def set_status_hint(self, text: str):
        self.status_hint.setText(text)

    def flash_status(self, text: str, ms: int = 6000):
        if text:
            self.status_bar.showMessage(text, ms)
        else:
            self.status_bar.clearMessage()

    def set_library_count(self, total: int, shown: int):
        if not total:
            self.status_count.setText("")
        elif shown != total:
            self.status_count.setText(f"{shown} of {total} games")
        else:
            self.status_count.setText(f"{total} game{'s' if total != 1 else ''}")

    def show_progress(self, current: int, total: int, label: str = ""):
        self.status_progress.setRange(0, total)
        if total:
            self.status_progress.setValue(current)
        self.status_progress.setFormat(f"{label} %p%" if total else label)
        self.status_progress.setTextVisible(True)
        self.status_progress.setVisible(True)

    def hide_progress(self):
        self.status_progress.setVisible(False)

    # ---- Windows native chrome: keep Snap/shadow while hiding the native title bar ----
    def nativeEvent(self, eventType, message):
        if IS_WINDOWS and eventType == b'windows_generic_MSG':
            try:
                msg = _MSG.from_address(int(message))
            except Exception:
                return super().nativeEvent(eventType, message)

            if msg.message == WM_NCCALCSIZE:
                if msg.wParam:
                    try:
                        if self.isMaximized():
                            # Replicate the inset Windows normally applies so a maximized
                            # window doesn't hang off-screen / over the taskbar -- we're
                            # claiming the whole rect as client area, so nothing else does
                            # this for us automatically.
                            params = _NCCALCSIZE_PARAMS.from_address(msg.lParam)
                            border = (ctypes.windll.user32.GetSystemMetrics(SM_CXFRAME)
                                      + ctypes.windll.user32.GetSystemMetrics(SM_CXPADDEDBORDER))
                            params.rgrc[0].left += border
                            params.rgrc[0].top += border
                            params.rgrc[0].right -= border
                            params.rgrc[0].bottom -= border
                    except Exception:
                        pass
                    return True, 0
            elif msg.message == WM_NCHITTEST:
                try:
                    handled, result = self._win_hit_test(msg.lParam)
                    if handled:
                        return True, result
                except Exception:
                    pass
        return super().nativeEvent(eventType, message)

    def _win_hit_test(self, lparam):
        """Translate a WM_NCHITTEST screen point into resize-edge/caption regions
        so Windows handles drag-move (with Snap) and edge-resize natively, even
        though there's no visible native title bar."""
        x = ctypes.c_short(lparam & 0xFFFF).value
        y = ctypes.c_short((lparam >> 16) & 0xFFFF).value
        # WM_NCHITTEST always reports physical screen pixels, but mapFromGlobal
        # (and every Qt geometry getter used below) works in logical/DIP pixels.
        # On a scaled display (125%/150%/etc, the Windows default on most laptops)
        # skipping this conversion made the resize-edge hit-test region several
        # times wider than the visible 6px border -- wide enough to swallow clicks
        # on real content near the window edges.
        if self.isFullScreen() or not self.title_bar.isVisible():
            # Big Picture is fullscreen: no resize edges, no caption to drag.
            return False, HT_CLIENT
        dpr = self.devicePixelRatioF() or 1.0
        pos = self.mapFromGlobal(QPoint(int(x / dpr), int(y / dpr)))
        w, h = self.width(), self.height()
        m = theme.RESIZE_MARGIN

        if not self.isMaximized():
            left = pos.x() < m
            right = pos.x() > w - m
            top = pos.y() < m
            bottom = pos.y() > h - m
            if top and left:
                return True, HT_TOPLEFT
            if top and right:
                return True, HT_TOPRIGHT
            if bottom and left:
                return True, HT_BOTTOMLEFT
            if bottom and right:
                return True, HT_BOTTOMRIGHT
            if left:
                return True, HT_LEFT
            if right:
                return True, HT_RIGHT
            if top:
                return True, HT_TOP
            if bottom:
                return True, HT_BOTTOM

        tb = self.title_bar
        tb_origin = tb.mapTo(self, QPoint(0, 0))
        local = QPoint(pos.x() - tb_origin.x(), pos.y() - tb_origin.y())
        if 0 <= local.x() <= tb.width() and 0 <= local.y() <= tb.height():
            child = tb.childAt(local)
            # Anything interactive stays a normal client-area widget; only the
            # empty strip and the icon/name labels act as the draggable caption.
            if child is not None and not isinstance(child, QLabel):
                return False, HT_CLIENT
            return True, HT_CAPTION
        return False, HT_CLIENT

    # ---- display modes ----
    def set_ui_mode(self, mode: str, initial: bool = False):
        """Switch between Classic and Big Picture. Big Picture hides the title
        bar/status bar and goes fullscreen; Classic restores the window as it was."""
        if mode not in (theme.MODE_CLASSIC, theme.MODE_BIG_PICTURE):
            mode = theme.MODE_CLASSIC
        if mode == self.ui_mode and not initial:
            return
        big = mode == theme.MODE_BIG_PICTURE
        if big and not initial:
            self._was_maximized = self.isMaximized()
        self.ui_mode = mode

        app = QApplication.instance()
        if app is not None:
            theme.apply_theme(app, mode)
        self.title_bar.setVisible(not big)
        self.status_bar.setVisible(not big)
        self._set_frame_margins(not big)
        self.library_view.set_ui_mode(mode)
        app_settings().setValue('ui/mode', mode)

        if not initial:
            if big:
                self.showFullScreen()
            elif self._was_maximized:
                self.showMaximized()
            else:
                self.showNormal()

    def toggle_ui_mode(self):
        self.set_ui_mode(theme.MODE_CLASSIC if self.ui_mode == theme.MODE_BIG_PICTURE else theme.MODE_BIG_PICTURE)

    def show_initial(self):
        """Show the window the way it was left: fullscreen if Big Picture."""
        if self.ui_mode == theme.MODE_BIG_PICTURE:
            self.showFullScreen()
        else:
            self.show()

    def open_settings_dialog(self):
        self._open_widget_dialog(self.settings_tab, "Settings", size=(680, 480))
        self.library_view.refresh()  # PCSX2 paths/exe may have changed

    def showEvent(self, event):
        super().showEvent(event)
        # Re-request focus for the library every time the window is (re)shown --
        # e.g. after being minimized and restored.
        # Skip this while a modal dialog (Install, Add Game, Settings, ...) is
        # open: Windows can send the owner window a show/activate notification
        # while an owned dialog appears, which re-enters this handler and yanks
        # keyboard focus away from the dialog's own widgets -- making them look
        # unclickable even though the dialog is genuinely on top and visible.
        if QApplication.activeModalWidget() is not None:
            return
        self.library_view.focus_active()

    def dragEnterEvent(self, e: QDragEnterEvent):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e: QDropEvent):
        # Pass-through: if it's pnach, hand to Cheats; else textures or codes
        for url in e.mimeData().urls():
            path = url.toLocalFile()
            if path.lower().endswith('.pnach'):
                self.cheats_tab.dropEvent(e)
                break
            else:
                self.textures_tab.dropEvent(e)
                break

    def _build_menus(self, mb: QMenuBar):
        """PCSX2-style menu bar, living inside the custom title bar."""
        lv = self.library_view
        m = mb.addMenu("&File")
        m.addAction(lv.act_scan)
        m.addAction(lv.act_add)
        m.addSeparator()
        m.addAction(lv.act_settings)
        m.addSeparator()
        quit_act = QAction("E&xit", self)
        quit_act.setShortcut(QKeySequence("Ctrl+Q"))
        quit_act.triggered.connect(self.close)
        m.addAction(quit_act)

        m = mb.addMenu("&View")
        m.addAction(lv.act_view_list)
        m.addAction(lv.act_view_grid)
        m.addSeparator()
        m.addAction(lv.act_zoom_in)
        m.addAction(lv.act_zoom_out)
        m.addSeparator()
        m.addAction(lv.act_find)
        m.addSeparator()
        m.addAction(lv.act_big_picture)

        m = mb.addMenu("&Tools")
        for a in (lv.act_install, lv.act_patch_all, lv.act_launch, lv.act_folder, lv.act_remove):
            m.addAction(a)
        m.addSeparator()
        cheats_act = QAction("Advanced Cheat Editor…", self)
        cheats_act.triggered.connect(self.open_advanced_cheats)
        m.addAction(cheats_act)
        textures_act = QAction("Advanced Texture Manager…", self)
        textures_act.triggered.connect(self.open_advanced_textures)
        m.addAction(textures_act)

        m = mb.addMenu("&Help")
        keys_act = QAction("Keyboard Shortcuts…", self)
        keys_act.triggered.connect(self._show_shortcuts)
        m.addAction(keys_act)
        about_act = QAction(f"About {APP_NAME}", self)
        about_act.triggered.connect(self._about)
        m.addAction(about_act)

    def _show_shortcuts(self):
        QMessageBox.information(
            self, "Keyboard Shortcuts",
            "<table cellpadding='3'>"
            "<tr><td><b>Ctrl+O</b></td><td>Scan folder</td></tr>"
            "<tr><td><b>Ctrl+N</b></td><td>Add game</td></tr>"
            "<tr><td><b>Enter</b></td><td>Install for the selected game</td></tr>"
            "<tr><td><b>Ctrl+L</b></td><td>Launch in PCSX2</td></tr>"
            "<tr><td><b>Del</b></td><td>Remove from library</td></tr>"
            "<tr><td><b>Ctrl+F</b></td><td>Search</td></tr>"
            "<tr><td><b>Ctrl+1 / Ctrl+2</b></td><td>List / grid view</td></tr>"
            "<tr><td><b>Ctrl+, </b></td><td>Settings</td></tr>"
            "<tr><td><b>F11</b></td><td>Switch Classic &harr; Big Picture</td></tr>"
            "</table>"
            "<p>In Big Picture: arrow keys or controller D-pad to move, Enter/A to select, "
            "Esc/B to go back, G/Y for grid, +/- or LB/RB to zoom.</p>"
        )

    def _open_widget_dialog(self, widget: QWidget, title: str, size=(900, 700)):
        """Host an existing tab widget (Cheats/Textures/Settings) in a one-off
        modal dialog, then detach it back so it survives to be reopened."""
        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        dlg.resize(*size)
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(widget)
        dlg.exec()
        lay.removeWidget(widget)
        widget.setParent(self)

    def open_advanced_cheats(self):
        game = self.state.current_game
        if game:
            self.cheats_tab.title_edit.setText(game.title or "")
            self.cheats_tab.serial_edit.setText(game.serial or "")
            self.cheats_tab.crc_edit.setText(game.crc or "")
        self._open_widget_dialog(self.cheats_tab, "Advanced Cheat Editor")

    def open_advanced_textures(self):
        self._open_widget_dialog(self.textures_tab, "Advanced Texture Manager")

    def _about(self):
        QMessageBox.information(
            self,
            f"About {APP_NAME}",
            f"<h3>{APP_NAME} 2.0</h3>"
            "<p>Cheats and texture packs for your PCSX2 game library.</p>"
            "<ul>"
            "<li>Scan a folder to build your library</li>"
            "<li>Install a game's cheats and texture pack in one step</li>"
            "<li>Region-correct cheats from a bundled database, with online fallbacks</li>"
            "<li>Classic desktop view and a fullscreen Big Picture mode (F11)</li>"
            "</ul>"
        )

    def _show_welcome_if_needed(self):
        """Show welcome dialog on first run"""
        settings = app_settings()
        if not settings.value('welcome_shown', False, bool):
            msg = QMessageBox(self)
            msg.setWindowTitle(f"Welcome to {APP_NAME}")
            msg.setTextFormat(Qt.RichText)
            msg.setText(
                f"<h3>Welcome to {APP_NAME}</h3>"
                "<ol>"
                "<li>Open <b>File \u25b8 Settings</b> and check your PCSX2 folder.</li>"
                "<li>Use <b>File \u25b8 Scan Folder</b> to add the games you own.</li>"
                "<li>Select a game and press <b>Enter</b> to install its cheats and textures.</li>"
                "</ol>"
                "<p>Press <b>F11</b> any time for Big Picture mode.</p>"
            )
            msg.setStandardButtons(QMessageBox.Ok)
            msg.exec()
            settings.setValue('welcome_shown', True)

    def _cleanup_workers(self, tab, timeout_ms=1000):
        """Helper to clean up workers for a given tab."""
        if not hasattr(tab, '_workers'):
            return

        for worker in list(tab._workers):
            try:
                if not worker.isRunning():
                    continue

                # Request graceful quit
                worker.quit()
                if worker.wait(timeout_ms):
                    continue  # Successfully quit

                # Force terminate if still running
                logger.warning(f"Force terminating worker: {worker.__class__.__name__}")
                worker.terminate()
                worker.wait(500)  # Give terminate a moment
            except RuntimeError as e:
                # Worker already deleted
                logger.debug(f"Worker cleanup: {e}")
            except Exception as e:
                logger.error(f"Error cleaning up worker: {e}")

    def closeEvent(self, event):
        """Clean up all running threads before closing."""
        logger.info("Application closing - cleaning up workers...")
        try:
            self.library_view.save_view_prefs()
        except Exception:
            pass
        self.library_view.bp.shutdown()

        # Set shutdown flags to prevent new workers
        # (the private attributes: the lazy properties would build the
        # editors just to shut them down)
        built = [t for t in (self._cheats_tab, self._textures_tab, getattr(self, 'library_view', None)) if t is not None]
        for tab in built:
            tab._shutting_down = True

        # Clean up workers in each widget
        for tab in built:
            self._cleanup_workers(tab)

        logger.info("Worker cleanup complete")
        event.accept()



def main():
    set_windows_app_user_model_id()
    QCoreApplication.setOrganizationName(APP_NAME)
    QCoreApplication.setApplicationName(APP_NAME)
    app = QApplication(sys.argv)
    app.setWindowIcon(QIcon(APP_ICON_ICO_PATH))
    migrate_legacy_settings(QSettings(*_LEGACY_SETTINGS), app_settings())
    theme.apply_theme(app, app_settings().value('ui/mode', theme.MODE_CLASSIC, str))
    w = MainWindow()
    w.show_initial()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
