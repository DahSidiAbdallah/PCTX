"""Python side of Big Picture mode.

The screen itself is web/bigpicture.html (a flat, controller-first game shelf
and grid in the spirit of Steam's Big Picture), embedded with QWebEngineView
and driven over QWebChannel. This module deliberately holds no business logic:
LibraryView hands it plain data and reacts to the one signal it emits, so the
scan / install / cover-cache code stays in a single place and is shared with
Classic mode.
"""
import json
import os
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

import threading

from PySide6.QtCore import QObject, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QColor, QImage
from PySide6.QtWebEngineCore import QWebEngineSettings
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QVBoxLayout, QWidget

_WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

# Actions the page can request. Anything else is ignored.
ACTIONS = ("launch", "exit")


def cover_url(path: str) -> str:
    """file:// URL the page can use as an <img> source, or '' when there is no
    cover on disk yet (the page draws its own placeholder)."""
    return QUrl.fromLocalFile(path).toString() if path and os.path.isfile(path) else ""


@dataclass
class BigPictureItem:
    """Plain data shape sent to the page -- not GameEntry, so this module has
    no dependency on LibraryView internals."""
    serial: str
    title: str
    region: str = ""
    crc: str = ""
    cover_path: str = ""
    cheats_installed: bool = False
    textures_installed: bool = False
    patches_installed: bool = False
    launchable: bool = False
    favorite: bool = False
    last_played: float = 0.0          # epoch seconds, 0 = never launched from PCTX

    def to_json_dict(self) -> dict:
        return {
            "serial": self.serial,
            "title": self.title,
            "region": self.region,
            "crc": self.crc,
            "cover": cover_url(self.cover_path),
            "backdrop": cover_url(backdrop_path(self.cover_path)),
            "cheats": self.cheats_installed,
            "textures": self.textures_installed,
            "patches": self.patches_installed,
            "launchable": self.launchable,
            "favorite": self.favorite,
            "played": self.last_played,
        }


# ---- backdrops -------------------------------------------------------------
# The page shows a soft, blurred version of the focused game's cover behind
# everything. Blurring a full-window layer with a CSS filter on every focus
# change is the most expensive thing a page can do, so the blur is baked once
# into a tiny image here; the browser just stretches it (bilinear scaling of a
# 40 px image is itself a blur) and cross-fades opacity, which is nearly free.
BACKDROP_SMALL = (12, 16)
BACKDROP_OUT = (120, 160)


def backdrop_path(cover_path: str) -> str:
    if not cover_path:
        return ""
    folder, name = os.path.split(cover_path)
    return os.path.join(folder, "bp", os.path.splitext(name)[0] + ".bg.jpg")


def make_backdrop(cover_path: str) -> bool:
    """Write the blurred backdrop for a cover if it's missing or stale. Safe to
    call from any thread (QImage, not QPixmap)."""
    out = backdrop_path(cover_path)
    if not out or not os.path.isfile(cover_path):
        return False
    try:
        if os.path.isfile(out) and os.path.getmtime(out) >= os.path.getmtime(cover_path):
            return True
        img = QImage(cover_path)
        if img.isNull():
            return False
        # Down to a few pixels and back up = a wide, smooth gaussian-ish blur.
        small = img.scaled(*BACKDROP_SMALL, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        big = small.scaled(*BACKDROP_OUT, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        tmp = out + ".tmp.jpg"
        if big.save(tmp, "JPG", 88):
            os.replace(tmp, out)
            return True
    except OSError:
        pass
    return False


class BigPictureBridge(QObject):
    """The QWebChannel object the page talks to. Only slots are callable from
    JS; signals are just how the Python side listens."""

    actionRequested = Signal(str, str)   # (action, serial)
    selectionChanged = Signal(str)

    replied = Signal(int, str)           # (request id, JSON result) for callAsync
    progressed = Signal(int, str)        # (request id, progress line)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.methods: Dict[str, Callable[[dict], Any]] = {}
        self.async_methods: Dict[str, Callable[[dict, Any], None]] = {}

    @Slot(str, str)
    def requestAction(self, action: str, serial: str):
        if action in ACTIONS:
            self.actionRequested.emit(action, serial)

    @Slot(str)
    def notifySelectionChanged(self, serial: str):
        self.selectionChanged.emit(serial)

    @Slot(str, str, result=str)
    def call(self, method: str, arg_json: str) -> str:
        """Generic request from the page: `method` must be one the owner registered
        (see BigPictureView.set_methods), `arg_json` is a JSON object, and the reply
        is JSON. This is how the in-page screens (settings, install, add, folder
        picker, ...) get data and trigger actions without any native dialog."""
        fn = self.methods.get(method)
        if fn is None:
            return json.dumps({"error": f"unknown method {method!r}"})
        try:
            return json.dumps(fn(json.loads(arg_json) if arg_json else {}))
        except Exception as e:  # the page must always get an answer
            return json.dumps({"error": str(e)})

    @Slot(int, str, str)
    def callAsync(self, request_id: int, method: str, arg_json: str):
        """Like call(), but for slow requests: returns at once, and the answer is
        delivered later through `replied` (progress lines through `progressed`),
        so the page -- and the whole window -- stays responsive meanwhile."""
        fn = self.async_methods.get(method)
        if fn is None:
            sync = self.methods.get(method)
            if sync is None:
                self.replied.emit(request_id, json.dumps({"error": f"unknown method {method!r}"}))
            else:
                self.replied.emit(request_id, self.call(method, arg_json))
            return
        done = {"sent": False}

        def reply(result: dict):
            if not done["sent"]:
                done["sent"] = True
                self.replied.emit(request_id, json.dumps(result))
        reply.progress = lambda text: self.progressed.emit(request_id, str(text))
        try:
            fn(json.loads(arg_json) if arg_json else {}, reply)
        except Exception as e:
            reply({"error": str(e)})


class BigPictureView(QWidget):
    """Big Picture screen as an ordinary widget with a plain Qt signal API.

    The Chromium engine behind it costs most of a second to start, so the web
    view inside is only created the first time Big Picture is actually shown
    (or something asks for it via ensure()). Until then the latest library
    snapshot is simply remembered and handed over on creation."""

    actionRequested = Signal(str, str)
    selectionChanged = Signal(str)
    _backdropReady = Signal(str, str)     # (serial, backdrop url), from the backdrop thread

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items: List[BigPictureItem] = []
        self._backdrop_job: Optional[threading.Thread] = None
        self._backdropReady.connect(self._on_backdrop_ready, Qt.QueuedConnection)
        self._current_serial: Optional[str] = None
        self._loaded = False
        self._pending_js: List[str] = []
        self._web: Optional[QWebEngineView] = None
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setStyleSheet("background-color: #0a0e13;")

        # The bridge is plain Python/Qt and cheap, so it exists from the start:
        # the RPC methods are registered on it before the page ever loads.
        self.bridge = BigPictureBridge(self)
        self.bridge.selectionChanged.connect(self._on_selection_changed)
        self.bridge.actionRequested.connect(self.actionRequested.emit)
        self.bridge.replied.connect(lambda rid, res: self._run(f"window.__rpcReply({rid}, {res});"))
        self.bridge.progressed.connect(lambda rid, t: self._run(f"window.__rpcProgress({rid}, {json.dumps(t)});"))

    # ---- lazy web view ----
    @property
    def created(self) -> bool:
        return self._web is not None

    def ensure(self) -> QWebEngineView:
        if self._web is not None:
            return self._web
        web = QWebEngineView(self)
        web.setContextMenuPolicy(Qt.NoContextMenu)
        # UI sounds are Web Audio; let them play without a click first.
        st = web.settings()
        st.setAttribute(QWebEngineSettings.PlaybackRequiresUserGesture, False)
        st.setAttribute(QWebEngineSettings.ShowScrollBars, False)
        web.page().setBackgroundColor(QColor("#0a0e13"))
        self.channel = QWebChannel(web.page())
        self.channel.registerObject("bridge", self.bridge)
        web.page().setWebChannel(self.channel)
        web.loadFinished.connect(self._on_load_finished)
        self._web = web
        self._layout.addWidget(web)
        # Hand over everything the page missed while it didn't exist.
        self._pending_js = [f"window.setGames({json.dumps([i.to_json_dict() for i in self._items])});"]
        if self._current_serial:
            self._pending_js.append(f"window.selectGameSerial({json.dumps(self._current_serial)});")
        web.setUrl(QUrl.fromLocalFile(os.path.join(_WEB_DIR, "bigpicture.html")))
        self._make_missing_backdrops()
        if self.hasFocus():
            web.setFocus()
        return web

    def page(self):
        return self.ensure().page()

    def showEvent(self, event):
        super().showEvent(event)
        self.ensure()

    def focusInEvent(self, event):
        super().focusInEvent(event)
        if self._web is not None:
            self._web.setFocus()

    # ---- plumbing ----
    def _on_load_finished(self, ok: bool):
        self._loaded = bool(ok)
        if self._loaded:
            pending, self._pending_js = self._pending_js, []
            for js in pending:
                self._web.page().runJavaScript(js)

    def _on_selection_changed(self, serial: str):
        self._current_serial = serial or None
        self.selectionChanged.emit(serial)

    def _run(self, js: str, keep_if_unborn: bool = False):
        """Run JS now, or once the page has finished loading. runJavaScript()
        interpolates a literal expression, so data goes in as real JS values
        (json.dumps) and the page never has to JSON.parse it. Before the view
        exists, only replies to the page matter (there is no page yet), so
        everything else is dropped: setGames on creation carries the state."""
        if self._web is None:
            return
        if self._loaded:
            self._web.page().runJavaScript(js)
        else:
            self._pending_js.append(js)

    # ---- API used by LibraryView ----
    def set_items(self, items: List[BigPictureItem]):
        self._items = list(items)
        data = json.dumps([i.to_json_dict() for i in self._items])
        # Replace, don't queue: only the latest library snapshot matters.
        self._pending_js = [j for j in self._pending_js if not j.startswith("window.setGames(")]
        self._run(f"window.setGames({data});")
        if self._web is not None:
            self._make_missing_backdrops()

    def _make_missing_backdrops(self):
        """Bake blurred backdrops for covers that don't have one yet, off the UI
        thread; each one is handed to the page as it's done."""
        todo = [(i.serial, i.cover_path) for i in self._items
                if i.cover_path and os.path.isfile(i.cover_path) and not os.path.isfile(backdrop_path(i.cover_path))]
        if not todo or (self._backdrop_job and self._backdrop_job.is_alive()):
            return

        def work():
            for serial, cover in todo:
                if make_backdrop(cover):
                    self._backdropReady.emit(serial, cover_url(backdrop_path(cover)))
        self._backdrop_job = threading.Thread(target=work, name="pctx-backdrops", daemon=True)
        self._backdrop_job.start()

    def _on_backdrop_ready(self, serial: str, url: str):
        self._run(f"window.updateGameBackdrop({json.dumps(serial)}, {json.dumps(url)});")

    def update_status(self, serial: str, cheats: bool, textures: bool):
        for item in self._items:
            if item.serial == serial:
                item.cheats_installed, item.textures_installed = cheats, textures
        self._run(f"window.updateGameStatus({json.dumps(serial)}, {json.dumps(bool(cheats))}, {json.dumps(bool(textures))});")

    def update_cover(self, serial: str, cover_path: str):
        for item in self._items:
            if item.serial == serial:
                item.cover_path = cover_path
        self._run(f"window.updateGameCover({json.dumps(serial)}, {json.dumps(cover_url(cover_path))});")
        if self._web is not None and make_backdrop(cover_path):
            self._on_backdrop_ready(serial, cover_url(backdrop_path(cover_path)))

    def select_serial(self, serial: str):
        self._current_serial = serial
        self._run(f"window.selectGameSerial({json.dumps(serial)});")

    def set_methods(self, methods: Dict[str, Callable[[dict], Any]],
                    async_methods: Optional[Dict[str, Callable[[dict, Any], None]]] = None):
        """Register the requests the page is allowed to make (name -> callable).
        `async_methods` take (arg, reply) and answer later with reply(dict)."""
        self.bridge.methods = dict(methods)
        self.bridge.async_methods = dict(async_methods or {})

    def enter(self):
        """Big Picture was just opened: replay the page's short intro."""
        self._run("window.bpEnter && window.bpEnter();")

    def show_toast(self, message: str):
        self._run(f"window.showToast({json.dumps(message)});")

    def current_serial(self) -> Optional[str]:
        return self._current_serial

    def shutdown(self):
        """Dispose of the web view now. Chromium's teardown is order-sensitive,
        and leaving it to interpreter shutdown crashes intermittently."""
        self._pending_js.clear()
        if self._web is not None:
            self._web.setParent(None)
            self._web.deleteLater()
            self._web = None
        self.setParent(None)
        self.deleteLater()
