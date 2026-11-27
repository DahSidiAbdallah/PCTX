"""Disc CRCs, PCSX2's per-game enable lists, patch classification, texture-pack
ranking and recommendations. Pure Python: no Qt, no network."""
import struct
import zlib

import disc
import enhancements as E


# ---------------------------------------------------------------- disc images
def _record(name: bytes, lba: int, size: int, is_dir=False) -> bytes:
    body = bytearray(33 + len(name) + (1 - len(name) % 2))
    body[0] = len(body)
    body[2:6] = struct.pack("<I", lba); body[6:10] = struct.pack(">I", lba)
    body[10:14] = struct.pack("<I", size); body[14:18] = struct.pack(">I", size)
    body[25] = 2 if is_dir else 0
    body[32] = len(name)
    body[33:33 + len(name)] = name
    return bytes(body)


def make_iso(elf: bytes, boot="cdrom0:\\SLUS_209.46;1", elf_name=b"SLUS_209.46;1") -> bytes:
    cnf = f"BOOT2 = {boot}\r\nVER = 1.00\r\nVMODE = NTSC\r\n".encode()
    sectors = {20: _record(b"\x00", 20, 2048, True) + _record(b"\x01", 20, 2048, True) +
               _record(b"SYSTEM.CNF;1", 21, len(cnf)) + _record(elf_name, 22, len(elf)),
               21: cnf, 22: elf}
    pvd = bytearray(2048)
    pvd[0] = 1
    pvd[1:6] = b"CD001"
    pvd[156:190] = _record(b"\x00", 20, 2048, True)
    sectors[16] = bytes(pvd)
    n = 22 + (len(elf) + 2047) // 2048 + 1
    img = bytearray(n * 2048)
    for lba, data in sectors.items():
        img[lba * 2048:lba * 2048 + len(data)] = data
    return bytes(img)


def make_bin(iso: bytes) -> bytes:
    out = bytearray()
    for i in range(0, len(iso), 2048):
        sector = bytearray(2352)
        sector[:12] = b"\x00" + b"\xff" * 10 + b"\x00"
        sector[15] = 2                                  # Mode 2 Form 1: user data at 24
        sector[24:24 + 2048] = iso[i:i + 2048]
        out += sector
    return bytes(out)


def make_cso(iso: bytes, block=2048) -> bytes:
    blocks = [iso[i:i + block] for i in range(0, len(iso), block)]
    header = struct.pack("<4sIQIBB2x", b"CISO", 24, len(iso), block, 1, 0)
    data_start = 24 + 4 * (len(blocks) + 1)
    index, payload, pos = [], bytearray(), data_start
    for j, b in enumerate(blocks):
        if j % 2:                                       # mix plain and deflated blocks
            index.append(pos | 0x80000000); payload += b
        else:
            c = zlib.compressobj(9, zlib.DEFLATED, -15)
            comp = c.compress(b) + c.flush()
            index.append(pos); payload += comp
        pos = data_start + len(payload)
    index.append(pos)
    return header + struct.pack(f"<{len(index)}I", *index) + bytes(payload)


ELF = b"\x7fELF" + bytes(range(256)) * 40 + b"\x01\x02"          # odd length: trailing bytes are ignored


def test_elf_crc_is_the_xor_of_little_endian_words():
    words = struct.unpack(f"<{len(ELF) // 4}I", ELF[:len(ELF) // 4 * 4])
    x = 0
    for w in words:
        x ^= w
    assert disc.elf_crc(ELF) == f"{x:08X}"


def test_reads_serial_and_crc_from_iso_bin_and_cso(tmp_path):
    iso = make_iso(ELF)
    want = disc.elf_crc(ELF)
    for name, data in (("g.iso", iso), ("g.bin", make_bin(iso)), ("g.cso", make_cso(iso))):
        p = tmp_path / name
        p.write_bytes(data)
        info = disc.read_disc_info(str(p))
        assert info and info.serial == "SLUS-20946" and info.crc == want and info.elf == "SLUS_209.46", name


def test_unreadable_images_return_none(tmp_path):
    (tmp_path / "junk.iso").write_bytes(b"nope" * 5000)
    (tmp_path / "x.chd").write_bytes(b"MComprHD" + b"\0" * 100)
    assert disc.read_disc_info(str(tmp_path / "junk.iso")) is None
    assert disc.read_disc_info(str(tmp_path / "x.chd")) is None
    assert disc.read_disc_info(str(tmp_path / "missing.iso")) is None


# ---------------------------------------------------------------- PCSX2 per-game settings
def test_game_settings_path_matches_pcsx2(tmp_path):
    assert E.game_settings_path("D:/PCSX2", "SLUS-20946", "c0498d24").replace("\\", "/") == \
        "D:/PCSX2/gamesettings/SLUS-20946_C0498D24.ini"


def test_enable_lists_round_trip_and_keep_other_settings(tmp_path):
    ini = tmp_path / "gamesettings" / "SLUS-1_ABCDEF01.ini"
    ini.parent.mkdir()
    ini.write_text("[EmuCore/GS]\nupscale_multiplier = 3\n\n[Patches]\nEnable = Mine\nEnable = Widescreen 16:9\n", encoding="utf-8")
    E.set_enabled(str(ini), "Patches", ["No-Interlacing", "Widescreen 16:9"],
                  manage=["Widescreen 16:9", "No-Interlacing", "60 FPS"], disable=["60 FPS"])
    text = ini.read_text(encoding="utf-8")
    assert "upscale_multiplier = 3" in text                       # untouched
    assert E.read_list(str(ini), "Patches") == ["Mine", "No-Interlacing", "Widescreen 16:9"]
    assert E.read_list(str(ini), "Patches", "Disable") == ["60 FPS"]
    E.set_enabled(str(ini), "Cheats", ["Infinite Health"], manage=["Infinite Health", "Max Money"])
    assert E.read_list(str(ini), "Cheats") == ["Infinite Health"]
    E.set_enabled(str(ini), "Cheats", [], manage=["Infinite Health", "Max Money"])
    assert E.read_list(str(ini), "Cheats") == [] and E.read_list(str(ini), "Patches")    # other section kept


def test_set_enabled_creates_the_file(tmp_path):
    ini = tmp_path / "gamesettings" / "X_00000001.ini"
    E.set_enabled(str(ini), "Patches", ["A"], manage=["A"])
    assert ini.read_text(encoding="utf-8") == "[Patches]\nEnable = A\n"
    assert E.read_list(str(ini), "Patches") == ["A"]


# ---------------------------------------------------------------- patch groups and recommendations
PNACH = """gametitle=Grand Theft Auto - San Andreas
[Widescreen 16:9]
gsaspectratio=16:9
author=Arapapa
patch=1,EE,00000000,extended,00000000
[No-Interlacing]
gsinterlacemode=1
patch=1,EE,00000001,extended,00000000
[60 FPS]
description=Unlocks the frame rate (may speed up physics)
patch=1,EE,00000002,extended,00000000
[Silentpatch Lite Fixes]
description=Fixes several bugs
patch=1,EE,00000003,extended,00000000
[Remove Color Filter]
patch=1,EE,00000004,extended,00000000
"""


def test_groups_are_classified_and_only_safe_ones_recommended():
    groups = {g.name: g for g in E.parse_groups(PNACH)}
    assert [(groups[n].kind, groups[n].recommended) for n in
            ("Widescreen 16:9", "No-Interlacing", "60 FPS", "Silentpatch Lite Fixes", "Remove Color Filter")] == \
        [("widescreen", True), ("nointerlace", True), ("fps", False), ("fix", True), ("quality", False)]
    assert groups["Widescreen 16:9"].author == "Arapapa"
    assert E.parse_groups("patch=1,EE,0,extended,0\n") == []         # unnamed: always on, no groups


def test_recommend_uses_the_exact_disc_file_and_good_texture_packs():
    groups = E.parse_groups(PNACH)
    files = [{"exact": False, "groups": groups}, {"exact": True, "groups": groups}]
    tex = [{"name": "maybe", "confidence": "medium"}, {"name": "HD", "confidence": "high"}]
    rec = E.recommend(files, tex)
    assert list(rec.patches) == [1]
    assert rec.patches[1] == ["Widescreen 16:9", "No-Interlacing", "Silentpatch Lite Fixes"]
    assert rec.texture == 1 and any("HD" in s for s in rec.summary)
    assert E.recommend([], [{"name": "x", "confidence": "low"}]).texture == -1


def test_cheat_group_names_match_what_is_written():
    assert E.pnach_group_name("Max [Money]") == "Max (Money)"
    assert E.pnach_group_name("") == "Cheat"


# ---------------------------------------------------------------- texture-pack matching
def test_title_variants_drop_region_tags_and_subtitles():
    assert E.title_variants("Okami (USA)") == ["okami"]
    assert E.title_variants("Final Fantasy XII") == ["final fantasy xii"]
    assert E.title_variants("Dragon Ball Z - Budokai 3 (Collector's Edition)") == ["dragon ball z budokai 3", "dragon ball z"]
    assert E.normalize_title("Final Fantasy XII") == E.normalize_title("final fantasy 12")
    assert E.title_variants("DRAGON BALL Z - BUDOKAI TENKAICHI 3") == ["dragon ball z budokai tenkaichi 3", "dragon ball z"]


def test_ranking_drops_other_games_and_wrong_sequels():
    title = "DRAGON BALL Z - BUDOKAI TENKAICHI 3"
    hits = [
        {"verified": False, "name": "DBZ-BT3-HD", "description": "Dragon Ball Z Budokai Tenkaichi 3 HD texture pack for PCSX2"},
        {"verified": False, "name": "bt2-textures", "description": "Dragon Ball Z Budokai Tenkaichi 2 HD textures"},
        {"verified": False, "name": "Jak-HD", "description": "Jak and Daxter HD textures"},
        {"verified": False, "name": "DBZ-BT3-HD", "description": "duplicate"},
    ]
    ranked = E.rank_texture_candidates(hits, title)
    assert [h["description"][:40] for h in ranked] == ["Dragon Ball Z Budokai Tenkaichi 3 HD tex"]
    assert ranked[0]["confidence"] == "high"
    assert E.score_texture_candidate({"name": "x"}, title) == (100, "verified")


def test_health_checks_offer_fixes_for_switched_off_options():
    checks = E.health_checks(crc="ABCD1234", crc_source="disc", has_ini=True,
                             ini_flags={"EnableCheats": False, "LoadTextureReplacements": False},
                             cheats_installed=True, cheats_enabled=2, textures_installed=True,
                             patches_installed=False, patches_enabled=0, ws_installed=False)
    fixes = {c.fix for c in checks if c.fix}
    assert fixes == {"EnableCheats", "LoadTextureReplacements"}
    ok = E.health_checks(crc=None, crc_source="", has_ini=False, ini_flags={}, cheats_installed=False, cheats_enabled=0,
                         textures_installed=False, patches_installed=False, patches_enabled=0, ws_installed=False)
    assert ok[0].id == "crc" and ok[0].level == "warn"
