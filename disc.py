"""Read a PS2 disc image's serial and the CRC PCSX2 uses for it.

PCSX2 names cheat files, patch files and per-game settings after the game's
CRC, so knowing the exact CRC is what makes cheats/patches line up with the
user's actual disc (different revisions/regions of a game have different
CRCs). PCSX2 computes it in ElfObject::GetCRC(): the XOR of every 32-bit
little-endian word of the boot ELF named by BOOT2 in SYSTEM.CNF.

Supported containers: plain .iso (2048-byte sectors), raw .bin/.img
(2352-byte sectors, Mode 1 or Mode 2 Form 1) and .cso (CISO v1, deflate).
.chd is not supported (callers fall back to database lookups).

No third-party dependencies; everything is read lazily and only the sectors
needed are touched, so this is quick even for multi-GB images.
"""
from __future__ import annotations

import os
import re
import struct
import zlib
from array import array
from dataclasses import dataclass
from functools import reduce
from operator import xor
from typing import Optional

SECTOR = 2048
_SYNC = b"\x00\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\x00"
MAX_ELF = 64 * 1024 * 1024


@dataclass
class DiscInfo:
    serial: Optional[str]
    crc: Optional[str]          # 8 upper-case hex digits, as PCSX2 shows it
    elf: Optional[str]          # e.g. "SLUS_209.46"


class _Reader:
    """Uniform "read logical 2048-byte sectors" over the supported formats."""

    def __init__(self, path: str):
        self.path = path
        self.f = open(path, "rb")
        self.kind = "iso"
        self.raw_offset = 0
        head = self.f.read(64)
        if head[:4] == b"CISO":
            self._init_cso(head)
        elif path.lower().endswith(".chd") or head[:8] == b"MComprHD":
            raise ValueError("CHD images aren't supported")
        else:
            self.f.seek(0)
            first = self.f.read(2352 * 17)
            if first[:12] == _SYNC or (len(first) >= 2352 * 16 + 12 and first[2352 * 16:2352 * 16 + 12] == _SYNC):
                self.kind = "raw"
                mode = first[15] if first[:12] == _SYNC else first[2352 * 16 + 15]
                self.raw_offset = 16 if mode == 1 else 24

    def _init_cso(self, head: bytes):
        self.kind = "cso"
        _magic, header_size, total, block_size, _ver, align = struct.unpack("<4sIQIBB", head[:22])
        self.cso_block = block_size or SECTOR
        self.cso_align = align
        n = (total + self.cso_block - 1) // self.cso_block
        self.f.seek(max(24, header_size))
        self.cso_index = array("I")
        self.cso_index.frombytes(self.f.read(4 * (n + 1)))
        self._cso_cache = (-1, b"")

    def _cso_block_data(self, b: int) -> bytes:
        if self._cso_cache[0] == b:
            return self._cso_cache[1]
        raw0, raw1 = self.cso_index[b], self.cso_index[b + 1]
        plain = bool(raw0 & 0x80000000)
        start = (raw0 & 0x7FFFFFFF) << self.cso_align
        end = (raw1 & 0x7FFFFFFF) << self.cso_align
        self.f.seek(start)
        data = self.f.read(max(end - start, self.cso_block if plain else 0))
        if plain:
            out = data[:self.cso_block]
        else:
            out = zlib.decompressobj(-15).decompress(data)
        self._cso_cache = (b, out)
        return out

    def read(self, lba: int, count: int = 1) -> bytes:
        if self.kind == "iso":
            self.f.seek(lba * SECTOR)
            return self.f.read(count * SECTOR)
        if self.kind == "raw":
            out = bytearray()
            for i in range(count):
                self.f.seek((lba + i) * 2352 + self.raw_offset)
                out += self.f.read(SECTOR)
            return bytes(out)
        # CSO
        out = bytearray()
        per = self.cso_block // SECTOR
        for i in range(count):
            s = lba + i
            blk = self._cso_block_data(s // per)
            off = (s % per) * SECTOR
            out += blk[off:off + SECTOR]
        return bytes(out)

    def close(self):
        self.f.close()


def _dir_entries(r: _Reader, lba: int, size: int):
    data = r.read(lba, (size + SECTOR - 1) // SECTOR)[:size]
    i = 0
    while i < len(data):
        n = data[i]
        if n == 0:
            i = (i // SECTOR + 1) * SECTOR
            continue
        if n < 34 or i + n > len(data):
            break
        rec = data[i:i + n]
        name = rec[33:33 + rec[32]].decode("ascii", "ignore")
        yield name, int.from_bytes(rec[2:6], "little"), int.from_bytes(rec[10:14], "little"), bool(rec[25] & 2)
        i += n


def _find(r: _Reader, path: str):
    """(lba, size) of a file by ISO9660 path like 'SLUS_209.46' or 'DATA/X.ELF'."""
    pvd = r.read(16)
    if pvd[1:6] != b"CD001":
        return None
    root = pvd[156:190]
    lba, size = int.from_bytes(root[2:6], "little"), int.from_bytes(root[10:14], "little")
    parts = [p for p in re.split(r"[\\/]+", path) if p]
    for depth, part in enumerate(parts):
        want = part.upper().split(";")[0]
        for name, elba, esize, is_dir in _dir_entries(r, lba, size):
            if name.upper().split(";")[0] == want:
                if depth == len(parts) - 1:
                    return elba, esize
                if is_dir:
                    lba, size = elba, esize
                    break
        else:
            return None
    return None


def elf_crc(data: bytes) -> str:
    """PCSX2's game CRC for a boot ELF: XOR of all 32-bit little-endian words."""
    words = array("I")
    words.frombytes(data[:len(data) - len(data) % 4])
    if words.itemsize != 4:  # pragma: no cover - every supported platform has 4-byte unsigned int
        words = array("L", words)
    return f"{reduce(xor, words, 0):08X}"


def read_disc_info(path: str) -> Optional[DiscInfo]:
    """Serial + CRC of a PS2 disc image, or None if it can't be read."""
    try:
        r = _Reader(path)
    except (OSError, ValueError, struct.error, zlib.error):
        return None
    try:
        cnf = _find(r, "SYSTEM.CNF")
        if not cnf or not (0 < cnf[1] <= 65536):
            return None
        text = r.read(cnf[0], (cnf[1] + SECTOR - 1) // SECTOR)[:cnf[1]].decode("ascii", "ignore")
        m = re.search(r"BOOT2\s*=\s*cdrom0?:\\?\\?([^\r\n;]+)", text, re.I)
        if not m:
            return None
        elf_path = m.group(1).strip()
        sm = re.search(r"([A-Z]{4})[_-](\d{3})\.(\d{2})", elf_path.upper())
        serial = f"{sm.group(1)}-{sm.group(2)}{sm.group(3)}" if sm else None
        crc = None
        loc = _find(r, elf_path)
        if loc and 0 < loc[1] <= MAX_ELF:
            data = r.read(loc[0], (loc[1] + SECTOR - 1) // SECTOR)[:loc[1]]
            if data[:4] == b"\x7fELF":
                crc = elf_crc(data)
        return DiscInfo(serial=serial, crc=crc, elf=os.path.basename(elf_path.replace("\\", "/")))
    except (OSError, ValueError, struct.error, zlib.error, IndexError):
        return None
    finally:
        r.close()
