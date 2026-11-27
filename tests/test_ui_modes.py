"""Classic / Big Picture shell, library table, settings and patch install.

Everything runs headlessly (offscreen Qt). Settings go to the throwaway INI set
up in conftest.py, and the library/cover paths are redirected to a temp folder
so these tests never touch a real library or hit the network.
"""
import os

import pytest
from PySide6.QtCore import QEvent, QSettings, Qt
from PySide6.QtWidgets import QApplication

import bigpicture
import main
import patches_online
import theme


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def _shared_window(qapp, tmp_path_factory):
    """One main window for the whole module: building many windows (each with a
    QtWebEngine view) in one process makes Qt's teardown flaky, and nothing here
    needs a fresh one -- the `window` fixture resets the state tests touch."""
    tmp = tmp_path_factory.mktemp("pctx")
    mp = pytest.MonkeyPatch()
    mp.setattr(main.LibraryView, "LIBRARY_STORE", str(tmp / "library.json"))
    mp.setattr(main.LibraryView, "COVER_CACHE_DIR", str(tmp / "covers"))
    mp.setattr(main.LibraryView, "PATCH_CACHE_DIR", str(tmp / "cache"))
    mp.setattr(main.LibraryView, "_load_cover_if_missing", lambda self, game: None)
    mp.setattr(main.LibraryView, "_prefetch_missing_covers", lambda self: None)
    w = main.MainWindow()
    yield w
    w.library_view._shutting_down = True
    bp = w.library_view.bp
    bp.setParent(None)
    bp.deleteLater()
    w.deleteLater()
    qapp.sendPostedEvents(None, QEvent.DeferredDelete)
    qapp.processEvents()
    mp.undo()


@pytest.fixture
def window(_shared_window):
    w = _shared_window
    lv = w.library_view
    lv.games.clear()
    lv._selected_serial = None
    lv.search_box.setText("")
    lv.act_view_list.setChecked(True)
    w.set_ui_mode(theme.MODE_CLASSIC, initial=True)
    lv._refresh_library()
    return w


def add_games(w, n=3):
    for i in range(n):
        s = f"SLUS-2{i:04d}"
        w.library_view.games[s] = main.GameEntry(serial=s, title=f"Game {chr(67 - i)}", crc=f"AABBCC0{i}",
                                                 source_path=None)
    w.library_view._refresh_library()


# ---------------------------------------------------------------- modes
def test_starts_in_classic_and_switches_to_big_picture_and_back(window):
    assert window.ui_mode == theme.MODE_CLASSIC
    window.set_ui_mode(theme.MODE_BIG_PICTURE, initial=True)   # initial=True: no real fullscreen in tests
    assert not window.title_bar.isVisibleTo(window) and not window.status_bar.isVisibleTo(window)
    assert window.library_view.view_stack.currentWidget() is window.library_view.bp
    assert main.app_settings().value("ui/mode") == theme.MODE_BIG_PICTURE
    window.set_ui_mode(theme.MODE_CLASSIC, initial=True)
    assert window.title_bar.isVisibleTo(window) and window.status_bar.isVisibleTo(window)
    assert window.library_view.view_stack.currentWidget() is window.library_view.classic


def test_unknown_mode_falls_back_to_classic(window):
    window.set_ui_mode("nonsense", initial=True)
    assert window.ui_mode == theme.MODE_CLASSIC


def test_theme_applies_per_mode(qapp):
    theme.apply_theme(qapp, theme.MODE_BIG_PICTURE)
    assert "#1a9fff" in qapp.styleSheet()
    theme.apply_theme(qapp, theme.MODE_CLASSIC)
    assert "#1a9fff" not in qapp.styleSheet()


# ---------------------------------------------------------------- library
def test_library_table_sorts_filters_and_tracks_selection(window):
    add_games(window)
    lv = window.library_view
    assert lv.table.rowCount() == 3
    titles = [lv.table.item(r, lv.COL_TITLE).text() for r in range(3)]
    assert titles == sorted(titles, key=str.upper)

    lv.search_box.setText("game b")
    assert lv._search_timer.isActive()        # typing is debounced...
    lv._search_timer.stop()
    lv._refresh_library(bp=False)             # ...and the timer's job is this call
    assert lv.table.rowCount() == 1
    lv.search_box.setText("")
    lv._search_timer.stop()
    lv._refresh_library(bp=False)
    assert lv.table.rowCount() == 3

    lv.table.selectRow(1)
    chosen = lv.table.item(1, lv.COL_TITLE).data(Qt.UserRole)
    assert window.state.current_game.serial == chosen
    assert lv.act_install.isEnabled()


def test_empty_library_shows_empty_state_and_disables_actions(window):
    lv = window.library_view
    lv._refresh_library()
    assert lv.list_stack.currentWidget() is lv.empty_page
    assert not lv.act_install.isEnabled() and not lv.act_remove.isEnabled()


def test_big_picture_selection_and_actions_route_to_the_library(window, monkeypatch):
    add_games(window)
    lv = window.library_view
    launched = []
    monkeypatch.setattr(lv, "_launch_in_pcsx2", lambda: launched.append(lv._selected_serial))
    lv._on_bp_selection("SLUS-20001")
    assert window.state.current_game.serial == "SLUS-20001"
    lv._on_bp_action("launch", "SLUS-20002")
    assert launched == ["SLUS-20002"]
    lv.set_ui_mode(theme.MODE_BIG_PICTURE)
    lv._on_bp_action("exit", "")
    assert window.ui_mode == theme.MODE_CLASSIC


def test_big_picture_item_json_shape(tmp_path):
    cover = tmp_path / "c.jpg"
    cover.write_bytes(b"x")
    item = bigpicture.BigPictureItem(serial="SLUS-1", title="T", region="NTSC-U", crc="AB", cover_path=str(cover),
                                     cheats_installed=True, launchable=True).to_json_dict()
    assert item["cover"].startswith("file:///") and item["cheats"] and not item["textures"] and item["launchable"]
    assert bigpicture.BigPictureItem(serial="S", title="t").to_json_dict()["cover"] == ""


def test_bridge_only_forwards_whitelisted_actions(qapp):
    bridge = bigpicture.BigPictureBridge()
    got = []
    bridge.actionRequested.connect(lambda a, s: got.append(a))
    for action in ("install", "settings", "rm -rf", "launch", "exit"):
        bridge.requestAction(action, "X")
    assert got == ["launch", "exit"]


def test_bridge_call_dispatches_registered_methods_only(qapp):
    import json
    bridge = bigpicture.BigPictureBridge()
    bridge.methods = {"echo": lambda a: {"got": a}, "boom": lambda a: 1 / 0}
    assert json.loads(bridge.call("echo", '{"x": 1}')) == {"got": {"x": 1}}
    assert "error" in json.loads(bridge.call("nope", "{}"))
    assert "error" in json.loads(bridge.call("boom", "{}"))       # exceptions become replies


# ---------------------------------------------------------------- in-page screens (RPC)
def rpc(window, method, **arg):
    import json
    return json.loads(window.library_view.bp.bridge.call(method, json.dumps(arg)))


def test_rpc_registers_every_method_the_page_calls(window):
    page = open(os.path.join(os.path.dirname(main.__file__), "web", "bigpicture.html"), encoding="utf-8").read()
    import re
    used = set(re.findall(r"rpc\('([a-z_.]+)'", page))
    bridge = window.library_view.bp.bridge
    assert used and used <= set(bridge.methods) | set(bridge.async_methods)


def test_rpc_add_and_remove_game(window):
    assert "error" in rpc(window, "game.add", serial="nonsense")
    assert rpc(window, "game.add", serial="slus-20946", title="My Game") == {"ok": True}
    assert window.library_view.games["SLUS-20946"].title == "My Game"
    assert rpc(window, "game.remove", serial="SLUS-20946") == {"ok": True}
    assert "SLUS-20946" not in window.library_view.games


def test_rpc_fs_list_lists_folders_and_filters_files(window, tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "a.exe").write_text("x")
    (tmp_path / "b.txt").write_text("x")
    r = rpc(window, "fs.list", path=str(tmp_path), exts=[".exe"])
    assert [d["name"] for d in r["dirs"]] == ["sub"] and [f["name"] for f in r["files"]] == ["a.exe"]
    assert rpc(window, "fs.list", path="")["parent"] is None        # the roots view
    assert "error" in rpc(window, "library.scan", path=str(tmp_path / "missing"))


def test_rpc_settings_paths_and_options(window, tmp_path):
    ini_dir = tmp_path / "pcsx2"
    (ini_dir / "inis").mkdir(parents=True)
    (ini_dir / "inis" / "PCSX2.ini").write_text("[EmuCore]\nEnableCheats = false\n", encoding="utf-8")
    s = rpc(window, "settings.set_path", kind="userDir", path=str(ini_dir))
    assert s["userDir"] == str(ini_dir) and all(o["enabled"] for o in s["options"])
    opt = next(o for o in s["options"] if o["key"] == "EnableCheats")
    assert opt["on"] is False
    s = rpc(window, "settings.set_option", section=opt["section"], key="EnableCheats", on=True)
    assert next(o for o in s["options"] if o["key"] == "EnableCheats")["on"] is True


# ---------------------------------------------------------------- settings
def test_legacy_settings_are_copied_once_and_never_overwrite(tmp_path):
    old = QSettings(str(tmp_path / "old.ini"), QSettings.IniFormat)
    new = QSettings(str(tmp_path / "new.ini"), QSettings.IniFormat)
    old.setValue("paths/user_dir", "D:/old")
    old.setValue("paths/pcsx2_exe", "D:/pcsx2.exe")
    new.setValue("paths/user_dir", "D:/new")
    assert main.migrate_legacy_settings(old, new) == 1          # only the key PCTX lacked
    assert new.value("paths/user_dir") == "D:/new" and new.value("paths/pcsx2_exe") == "D:/pcsx2.exe"
    old.setValue("later", "x")
    assert main.migrate_legacy_settings(old, new) == 0          # already migrated: runs once
    assert not new.contains("later")


def test_ini_helpers_round_trip_and_create_sections(tmp_path):
    ini = tmp_path / "PCSX2.ini"
    ini.write_text("[UI]\nTheme = dark\n\n[EmuCore]\nEnableCheats = false\n", encoding="utf-8")
    assert main.ini_get_bool(str(ini), "EnableCheats", "EmuCore") is False
    assert main.ini_get_bool(str(ini), "EnableWideScreenPatches", "EmuCore") is None
    assert main.ini_set_bool(str(ini), "EnableCheats", True, "EmuCore")
    assert main.ini_set_bool(str(ini), "LoadTextureReplacements", True, "EmuCore/GS")   # new section
    text = ini.read_text(encoding="utf-8")
    assert "EnableCheats = true" in text and "[EmuCore/GS]" in text and "Theme = dark" in text
    assert main.ini_get_bool(str(ini), "LoadTextureReplacements", "EmuCore/GS") is True
    assert not main.ini_set_bool(str(tmp_path / "missing.ini"), "a", True, "S")


def test_settings_options_mirror_pcsx2_ini(window, tmp_path):
    data = tmp_path / "PCSX2"
    (data / "inis").mkdir(parents=True)
    (data / "inis" / "PCSX2.ini").write_text("[EmuCore]\nEnableCheats = true\n", encoding="utf-8")
    tab = window.settings_tab
    tab.user_dir.setText(str(data))
    tab._update_subs()
    box = tab.option_boxes[("EmuCore", "EnableCheats")]
    assert box.isChecked() and box.isEnabled()
    assert (data / "patches").is_dir() and (data / "cheats").is_dir() and (data / "textures").is_dir()
    assert not tab.option_boxes[("EmuCore", "EnableWideScreenPatches")].isChecked()
    tab.option_boxes[("EmuCore", "EnableWideScreenPatches")].setChecked(True)
    assert main.ini_get_bool(str(data / "inis" / "PCSX2.ini"), "EnableWideScreenPatches", "EmuCore") is True


def test_settings_options_disabled_without_an_ini(window, tmp_path):
    tab = window.settings_tab
    tab.user_dir.setText(str(tmp_path))
    tab._update_subs()
    assert not any(cb.isEnabled() for cb in tab.option_boxes.values())


def test_missing_data_folder_is_not_created(window, tmp_path):
    ghost = tmp_path / "does" / "not" / "exist"
    window.settings_tab.user_dir.setText(str(ghost))
    window.settings_tab._update_subs()
    assert not ghost.exists()


# ---------------------------------------------------------------- the Enhancements dialog (Classic)
def _options(patches=(), cheats=(), textures=(), **kw):
    import enhancements
    o = {"patches": list(patches), "cheats": list(cheats), "textures": list(textures), "cheat_note": "from test",
         "cheats_installed": False, "cheats_foreign": False, "textures_installed": False, "log": ["x"],
         "checks": [], "recommendation": enhancements.recommend(list(patches), list(textures))}
    o.update(kw)
    return o


def test_enhancements_dialog_starts_from_the_recommendation_and_returns_a_selection(window):
    import enhancements
    pf = patches_online.PatchFile("SLUS-20554", "93B9A720")
    groups = enhancements.parse_groups("[Widescreen 16:9]\ngsaspectratio=16:9\n[60 FPS]\npatch=1\n")
    for g in groups:
        g.enabled = False
    o = _options(patches=[{"file": pf, "exact": True, "installed": False, "always": False, "groups": groups, "text": ""}],
                 cheats=[{"name": "Infinite Health", "codes": ["x"], "enabled": False}],
                 textures=[{"name": "HD Pack", "verified": True, "confidence": "verified", "score": 100}])
    dlg = main.EnhancementsDialog(main.GameEntry(serial="SLUS-20554", title="MGS2", crc="93B9A720", crc_source="disc"), o,
                                  parent=window)
    sel = dlg.selection()
    assert sel["patches"] == {0: ["Widescreen 16:9"]}           # 60 FPS is offered but not recommended
    assert sel["texture"] == 0 and sel["cheats"] == [] and sel["remove"] == []
    dlg.cheats_list.item(0).setCheckState(Qt.Checked)
    assert dlg.selection()["cheats"] == [0]


def test_enhancements_dialog_without_anything_found_still_works(window):
    dlg = main.EnhancementsDialog(main.GameEntry(serial="SLUS-1", title="X"), _options(), parent=window)
    assert dlg.selection() == {"patches": {}, "cheats": [], "cheatsOverwrite": False, "texture": -1,
                               "texturesOverwrite": False, "remove": [], "fixes": []}
    assert not dlg.btn_rec.isEnabled()


def test_install_patches_writes_files_and_reports(window, tmp_path, monkeypatch):
    text = "gametitle=T\n\n[Widescreen 16:9]\ngsaspectratio=16:9\npatch=1,EE,00000000,extended,00000000\n"
    monkeypatch.setattr(patches_online, "fetch_text", lambda pf, getter=None: text)
    pf = patches_online.PatchFile("SLUS-20554", "93B9A720")
    game = main.GameEntry(serial="SLUS-20554", title="MGS2", crc="93B9A720")
    msg = window.library_view._install_patches(game, str(tmp_path / "patches"), [pf])
    assert "installed 1 file" in msg and "Widescreen 16:9" in msg
    assert (tmp_path / "patches" / "SLUS-20554_93B9A720.pnach").read_text(encoding="utf-8") == text
    assert window.library_view._install_patches(game, str(tmp_path), []) == "Patches: none selected."


def test_install_patches_reports_download_failures(window, tmp_path, monkeypatch):
    def boom(pf, getter=None):
        raise RuntimeError("HTTP 404")
    monkeypatch.setattr(patches_online, "fetch_text", boom)
    pf = patches_online.PatchFile("SLUS-20554", "93B9A720")
    game = main.GameEntry(serial="SLUS-20554", title="MGS2")
    assert "1 failed" in window.library_view._install_patches(game, str(tmp_path), [pf])


# ---------------------------------------------------------------- responsiveness / Big Picture extras
def _wait_until(qapp, cond, timeout=5.0):
    import time
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        qapp.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_bridge_call_async_replies_later_with_progress(qapp):
    import json
    bridge = bigpicture.BigPictureBridge()
    later = []
    bridge.async_methods = {"slow": lambda a, reply: later.append((a, reply))}
    bridge.methods = {"fast": lambda a: {"x": a.get("x")}}
    replies, progress = [], []
    bridge.replied.connect(lambda rid, res: replies.append((rid, json.loads(res))))
    bridge.progressed.connect(lambda rid, t: progress.append((rid, t)))
    bridge.callAsync(1, "fast", '{"x": 2}')               # sync methods answer through the same channel
    bridge.callAsync(2, "slow", '{"y": 1}')
    assert replies == [(1, {"x": 2})] and later[0][0] == {"y": 1}
    later[0][1].progress("half way")
    later[0][1]({"done": True})
    later[0][1]({"done": "twice"})                         # a second reply is ignored
    assert progress == [(2, "half way")] and replies[-1] == (2, {"done": True}) and len(replies) == 2
    bridge.callAsync(3, "nope", "{}")
    assert "error" in replies[-1][1]


def test_install_search_and_apply_run_off_the_ui_thread(window, qapp, monkeypatch, tmp_path):
    import threading
    add_games(window, 1)
    lv = window.library_view
    threads = []
    window.state.pcsx2_paths = {"cheats": str(tmp_path), "textures": str(tmp_path), "patches": str(tmp_path)}
    monkeypatch.setattr(lv, "_gather_cheat_candidates", lambda g, d: (threads.append(threading.current_thread()), ([], "n/a", [], False))[1])
    monkeypatch.setattr(lv, "_gather_texture_candidates", lambda g, d: ([], [], False))
    monkeypatch.setattr(lv, "_gather_patch_candidates", lambda g, d, *a: ([], []))
    monkeypatch.setattr(lv, "_resolve_crc", lambda g, log, index=None: None)
    monkeypatch.setattr(patches_online, "load_index", lambda *a, **k: ([], None))
    got = []
    lv._gather_async(lv.games["SLUS-20000"], lambda o, err: got.append((o, err)))
    assert _wait_until(qapp, lambda: got)
    assert got[0][1] is None and threads[0] is not threading.main_thread()

    done = []
    lv._apply_async(lv.games["SLUS-20000"], got[0][0], {}, on_done=lambda results, followups: done.append(results))
    assert _wait_until(qapp, lambda: done)
    assert done[0] == ["Nothing to change."] and not lv._busy


def test_bp_prefs_round_trip_and_reject_unknown_keys(window):
    prefs = rpc(window, "prefs.get")
    assert prefs["sounds"] is True and prefs["mode"] in ("shelf", "grid")
    assert rpc(window, "prefs.set", key="sounds", value=False)["sounds"] is False
    assert rpc(window, "prefs.set", key="volume", value=0.3)["volume"] == pytest.approx(0.3)
    assert "error" in rpc(window, "prefs.set", key="evil", value=1)
    rpc(window, "prefs.set", key="sounds", value=True)


def test_favorites_toggle_and_show_up_in_big_picture_items(window):
    add_games(window)
    assert rpc(window, "game.favorite", serial="SLUS-20001")["favorite"] is True
    window.library_view._refresh_library()
    fav = {i.serial: i.favorite for i in window.library_view.bp._items}
    assert fav["SLUS-20001"] and not fav["SLUS-20000"]
    assert rpc(window, "game.favorite", serial="SLUS-20001")["favorite"] is False
    assert "error" in rpc(window, "game.favorite", serial="NOPE-1")


def test_title_index_is_built_once_and_matches_known_serials():
    main._TITLE_INDEX = None
    assert main.bundled_lookup_title("SLUS-20946") == "Grand Theft Auto - San Andreas"
    first = main._TITLE_INDEX
    assert main.bundled_lookup_title("slus-20946 ") == "Grand Theft Auto - San Andreas"
    assert main._TITLE_INDEX is first and main.bundled_lookup_title("XXXX-00000") is None


def test_backdrop_is_baked_small_and_reused(tmp_path, qapp):
    from PySide6.QtGui import QColor, QImage
    cover = tmp_path / "cover_SLUS-1.jpg"
    img = QImage(512, 736, QImage.Format_RGB32)
    img.fill(QColor("#c03030"))
    assert img.save(str(cover))
    assert bigpicture.make_backdrop(str(cover))
    out = bigpicture.backdrop_path(str(cover))
    assert QImage(out).size().width() == bigpicture.BACKDROP_OUT[0]
    mtime = os.path.getmtime(out)
    assert bigpicture.make_backdrop(str(cover)) and os.path.getmtime(out) == mtime     # cached
    assert bigpicture.BigPictureItem(serial="S", title="t", cover_path=str(cover)).to_json_dict()["backdrop"].startswith("file:///")


def test_apply_installs_patches_and_switches_on_groups_and_cheats(window, tmp_path, monkeypatch):
    import enhancements
    lv = window.library_view
    data = tmp_path / "PCSX2"
    for d in ("cheats", "patches", "textures", "inis"):
        (data / d).mkdir(parents=True)
    ini = data / "inis" / "PCSX2.ini"
    ini.write_text("[EmuCore]\nEnableCheats = false\n", encoding="utf-8")
    game = main.GameEntry(serial="SLUS-20946", title="GTA SA", crc="C0498D24", crc_source="disc")
    text = "[Widescreen 16:9]\ngsaspectratio=16:9\npatch=1,EE,0,extended,0\n[60 FPS]\npatch=1,EE,1,extended,0\n"
    pf = patches_online.PatchFile("SLUS-20946", "C0498D24")
    gs = enhancements.game_settings_path(str(data), game.serial, game.crc)
    o = {"cheats_dir": str(data / "cheats"), "patches_dir": str(data / "patches"), "textures_dir": str(data / "textures"),
         "data_dir": str(data), "ini": str(ini), "game_settings": gs, "flags": {"EnableCheats": False},
         "patches": [{"file": pf, "exact": True, "installed": False, "always": False, "text": text,
                      "groups": enhancements.parse_groups(text)}],
         "cheats": [{"name": "Infinite Health", "codes": ["patch=1,EE,2,extended,0"]},
                    {"name": "Max [Money]", "codes": ["patch=1,EE,3,extended,0"]}],
         "cheats_installed": False, "cheats_foreign": False, "textures": [], "textures_installed": False}
    results = lv._apply_install(game, o, {"patches": {0: ["Widescreen 16:9"]}, "cheats": [1]})
    assert (data / "patches" / "SLUS-20946_C0498D24.pnach").read_text(encoding="utf-8") == text
    assert enhancements.read_list(gs, "Patches") == ["Widescreen 16:9"]
    assert enhancements.read_list(gs, "Cheats") == ["Max (Money)"]
    cheat_file = (data / "cheats" / "C0498D24.pnach").read_text(encoding="utf-8")
    assert "[Infinite Health]" in cheat_file and "[Max (Money)]" in cheat_file      # all written, one switched on
    assert main.ini_get_bool(str(ini), "EnableCheats", "EmuCore") is True             # turned on for them to run
    assert any("turned on" in r for r in results)

    o["patches"][0]["installed"] = True
    o["cheats_installed"] = True
    lv._apply_install(game, o, {"remove": ["patches", "cheats"]})
    assert not (data / "patches" / "SLUS-20946_C0498D24.pnach").exists()
    assert not (data / "cheats" / "C0498D24.pnach").exists()
    assert enhancements.read_list(gs, "Patches") == [] and enhancements.read_list(gs, "Cheats") == []
