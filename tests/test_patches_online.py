import json
import os

import patches_online as po


class FakeResp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload


TREE = {"tree": [
    {"path": "patches/SLUS-20554_93B9A720.pnach"},
    {"path": "patches/SLUS-20554_C539049D.pnach"},
    {"path": "patches/SLUS-21002_88A808FA.pnach"},
    {"path": "patches/6D980D22.pnach"},          # CRC-only legacy name: ignored
    {"path": "README.md"},
]}

PNACH = "gametitle=Test\n\n[Widescreen 16:9]\ngsaspectratio=16:9\ndescription=Widescreen Hack\npatch=1,EE,2011fee0,extended,dfbf0030\n"


def test_parse_tree_keeps_only_serial_crc_files():
    files = po.parse_tree(TREE)
    assert [f.filename for f in files] == [
        "SLUS-20554_93B9A720.pnach", "SLUS-20554_C539049D.pnach", "SLUS-21002_88A808FA.pnach"]


def test_normalize_serial_and_crc():
    assert po.normalize_serial("slus_20554") == "SLUS-20554"
    assert po.normalize_serial("SLUS 20554") == "SLUS-20554"
    assert po.normalize_serial("nonsense") == ""
    assert po.normalize_crc("0x93b9a720") == "93B9A720"
    assert po.normalize_crc("xyz") == ""


def test_find_for_game_exact_crc_beats_other_revisions():
    index = po.parse_tree(TREE)
    files, exact = po.find_for_game(index, "SLUS-20554", "c539049d")
    assert exact and [f.crc for f in files] == ["C539049D"]


def test_find_for_game_without_crc_returns_every_revision():
    index = po.parse_tree(TREE)
    files, exact = po.find_for_game(index, "SLUS-20554", None)
    assert not exact and [f.crc for f in files] == ["93B9A720", "C539049D"]
    assert po.find_for_game(index, "SLUS-99999", None) == ([], False)


def test_describe_groups():
    assert po.describe_groups(PNACH) == [("Widescreen 16:9", "Widescreen Hack")]


def test_load_index_caches_and_falls_back(tmp_path):
    calls = []

    def ok(url, timeout=15):
        calls.append(url)
        return FakeResp(200, TREE)

    files, err = po.load_index(str(tmp_path), getter=ok, now=lambda: 1000.0)
    assert err is None and len(files) == 3 and len(calls) == 1

    # fresh cache -> no second request
    files, err = po.load_index(str(tmp_path), getter=ok, now=lambda: 1000.0 + 60)
    assert err is None and len(files) == 3 and len(calls) == 1

    # stale cache + network failure -> stale copy with a warning
    def down(url, timeout=15):
        raise OSError("offline")

    files, err = po.load_index(str(tmp_path), getter=down, now=lambda: 1000.0 + po.INDEX_MAX_AGE_S + 1)
    assert len(files) == 3 and "saved copy" in err

    # no cache at all + network failure -> empty list and a readable reason
    files, err = po.load_index(str(tmp_path / "empty"), getter=down)
    assert files == [] and "offline" in err


def test_load_index_reports_rate_limit(tmp_path):
    files, err = po.load_index(str(tmp_path), getter=lambda url, timeout=15: FakeResp(403))
    assert files == [] and "rate limit" in err


def test_install_writes_file_and_keeps_backup_of_a_different_one(tmp_path):
    pf = po.PatchFile("SLUS-20554", "93B9A720")
    dest = po.install(pf, PNACH, str(tmp_path))
    assert os.path.basename(dest) == "SLUS-20554_93B9A720.pnach" and open(dest, encoding="utf-8").read() == PNACH
    assert po.install(pf, PNACH, str(tmp_path)) == dest            # identical -> untouched
    assert not os.path.exists(dest + ".bak")
    po.install(pf, PNACH + "// edited\n", str(tmp_path))            # different -> old one kept
    assert open(dest + ".bak", encoding="utf-8").read() == PNACH
    assert not os.path.exists(dest + ".part")


def test_install_for_game_reports_installed_skipped_failed(tmp_path):
    index = po.parse_tree(TREE)
    pdir = str(tmp_path)

    def getter(url, timeout=15):
        return FakeResp(200, text=PNACH) if "93B9A720" in url else FakeResp(404)

    r = po.install_for_game(index, "SLUS-20554", None, pdir, getter=getter)
    assert r == {"installed": ["SLUS-20554_93B9A720.pnach"], "skipped": [], "failed": ["SLUS-20554_C539049D.pnach"]}
    r2 = po.install_for_game(index, "SLUS-20554", None, pdir, getter=getter)
    assert r2["skipped"] == ["SLUS-20554_93B9A720.pnach"]


def test_fetch_text_rejects_non_patch_content():
    pf = po.PatchFile("SLUS-20554", "93B9A720")
    try:
        po.fetch_text(pf, getter=lambda url, timeout=15: FakeResp(200, text="<html>rate limited</html>"))
    except RuntimeError as e:
        assert "doesn't look like" in str(e)
    else:
        raise AssertionError("expected RuntimeError")
