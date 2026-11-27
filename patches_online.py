"""Per-game patches and fixes from PCSX2's own patch collection.

PCSX2 ships a collection of community patches (widescreen, no-interlacing and
per-game fixes) at https://github.com/PCSX2/pcsx2_patches, one file per game
revision named `<SERIAL>_<CRC>.pnach`. PCSX2 loads user-supplied copies from
`<data folder>/patches/` and prefers them over its bundled ones, so installing
a file there is how a newer or missing patch reaches the emulator. PCSX2 only
applies a file whose CRC matches the disc being played, so installing every
revision of a serial is harmless.

This module is Qt-free so it can be tested headlessly. Network access goes
through an injectable `getter` for the same reason.
"""
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

try:
    import requests
except Exception:  # requests is optional; callers get a readable error instead
    requests = None

REPO = "PCSX2/pcsx2_patches"
BRANCH = "main"
TREE_URL = f"https://api.github.com/repos/{REPO}/git/trees/{BRANCH}?recursive=1"
RAW_BASE = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/patches/"
USER_AGENT = "PCTX/2.0"
INDEX_MAX_AGE_S = 7 * 24 * 3600          # the repo changes slowly; a weekly refresh is plenty
_FILE_RE = re.compile(r"^patches/([A-Z]{4}-\d{5})_([0-9A-F]{8})\.pnach$")
_SERIAL_RE = re.compile(r"([A-Za-z]{4})[-_ ]?(\d{5})")


@dataclass(frozen=True)
class PatchFile:
    serial: str
    crc: str

    @property
    def filename(self) -> str:
        return f"{self.serial}_{self.crc}.pnach"

    @property
    def url(self) -> str:
        return RAW_BASE + self.filename


def normalize_serial(serial: str) -> str:
    """'slus_20554' / 'SLUS 20554' / 'SLUS20554' -> 'SLUS-20554' ('' if not a serial)."""
    m = _SERIAL_RE.search(serial or "")
    return f"{m.group(1).upper()}-{m.group(2)}" if m else ""


def normalize_crc(crc: Optional[str]) -> str:
    c = (crc or "").strip().upper().removeprefix("0X")
    return c if re.fullmatch(r"[0-9A-F]{8}", c) else ""


def _http_get(url: str, timeout: int = 15):
    if requests is None:
        raise RuntimeError("the 'requests' package is not installed")
    return requests.get(url, timeout=timeout, headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"})


# ---------------------------------------------------------------- index
def parse_tree(tree_json: dict) -> List[PatchFile]:
    """Turn GitHub's recursive tree listing into PatchFiles (ignores anything
    that isn't a SERIAL_CRC.pnach under patches/)."""
    out = []
    for node in tree_json.get("tree", []):
        m = _FILE_RE.match(node.get("path", ""))
        if m:
            out.append(PatchFile(m.group(1), m.group(2)))
    return out


def _cache_file(cache_dir: str) -> str:
    return os.path.join(cache_dir, "patch_index.json")


def _read_cache(cache_dir: str) -> Optional[Tuple[float, List[PatchFile]]]:
    try:
        with open(_cache_file(cache_dir), "r", encoding="utf-8") as f:
            d = json.load(f)
        files = [PatchFile(*name.split("_", 1)) for name in d["files"]]
        return float(d["fetched_at"]), files
    except Exception:
        return None


def load_index(cache_dir: str, force: bool = False,
               getter: Callable = _http_get, now: Callable[[], float] = time.time
               ) -> Tuple[List[PatchFile], Optional[str]]:
    """The list of every patch file PCSX2 publishes. Uses the on-disk cache while
    it is fresh, refreshes it from GitHub otherwise, and falls back to a stale
    cache if the network is down. Returns (files, error); error is None on
    success and a short human-readable reason when nothing usable was found."""
    cached = _read_cache(cache_dir)
    if cached and not force and now() - cached[0] < INDEX_MAX_AGE_S:
        return cached[1], None
    try:
        resp = getter(TREE_URL)
        if resp.status_code == 403:
            raise RuntimeError("GitHub rate limit reached -- try again later")
        if resp.status_code != 200:
            raise RuntimeError(f"GitHub returned HTTP {resp.status_code}")
        tree = resp.json()
        files = parse_tree(tree)
        if not files:
            raise RuntimeError("the patch list came back empty")
        os.makedirs(cache_dir, exist_ok=True)
        with open(_cache_file(cache_dir), "w", encoding="utf-8") as f:
            json.dump({"fetched_at": now(), "files": [p.filename[:-6] for p in files]}, f)
        return files, None
    except Exception as e:
        if cached:
            return cached[1], f"couldn't refresh the patch list ({e}); using a saved copy"
        return [], f"couldn't reach PCSX2's patch collection ({e})"


def find_for_game(index: List[PatchFile], serial: str, crc: Optional[str] = None) -> Tuple[List[PatchFile], bool]:
    """Patch files for a game. With a known CRC and an exact match, returns just
    that file and True. Otherwise returns every revision of the serial and
    False (PCSX2 ignores the ones that don't match the disc)."""
    s, c = normalize_serial(serial), normalize_crc(crc)
    if not s:
        return [], False
    same_serial = sorted((p for p in index if p.serial == s), key=lambda p: p.crc)
    exact = [p for p in same_serial if c and p.crc == c]
    if exact:
        return exact, True
    return same_serial, False


# ---------------------------------------------------------------- files
def fetch_text(pf: PatchFile, getter: Callable = _http_get) -> str:
    resp = getter(pf.url)
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code} for {pf.filename}")
    text = resp.text
    if "patch=" not in text and "gsaspectratio" not in text and "[" not in text:
        raise RuntimeError(f"{pf.filename} doesn't look like a patch file")
    return text


def describe_groups(text: str) -> List[Tuple[str, str]]:
    """[(group name, description)] for the labelled groups in a pnach file,
    e.g. ('Widescreen 16:9', 'Widescreen Hack'). Unlabelled patches are skipped."""
    groups: List[Tuple[str, str]] = []
    current: Optional[List[str]] = None
    for raw in text.splitlines():
        line = raw.strip()
        m = re.match(r"^\[(.+)\]$", line)
        if m:
            current = [m.group(1).strip(), ""]
            groups.append(current)  # type: ignore[arg-type]
        elif current is not None and line.lower().startswith("description="):
            current[1] = line.split("=", 1)[1].strip()
    return [(g[0], g[1]) for g in groups]


def installed_path(patches_dir: str, pf: PatchFile) -> str:
    return os.path.join(patches_dir, pf.filename)


def is_installed(patches_dir: str, pf: PatchFile) -> bool:
    return bool(patches_dir) and os.path.isfile(installed_path(patches_dir, pf))


def install(pf: PatchFile, text: str, patches_dir: str) -> str:
    """Write `text` to <patches_dir>/<SERIAL>_<CRC>.pnach (atomically) and return
    the path. An existing different file is kept next to it as .bak first."""
    os.makedirs(patches_dir, exist_ok=True)
    dest = installed_path(patches_dir, pf)
    if os.path.isfile(dest):
        with open(dest, "r", encoding="utf-8", errors="replace") as f:
            if f.read() == text:
                return dest
        bak = dest + ".bak"
        if not os.path.exists(bak):
            os.replace(dest, bak)
    tmp = dest + ".part"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, dest)
    return dest


def install_for_game(index: List[PatchFile], serial: str, crc: Optional[str], patches_dir: str,
                     getter: Callable = _http_get, overwrite: bool = False) -> Dict[str, List[str]]:
    """Download and install every applicable patch file for one game. Returns
    {'installed': [...], 'skipped': [...], 'failed': [...]} of file names."""
    files, _exact = find_for_game(index, serial, crc)
    result: Dict[str, List[str]] = {"installed": [], "skipped": [], "failed": []}
    for pf in files:
        if is_installed(patches_dir, pf) and not overwrite:
            result["skipped"].append(pf.filename)
            continue
        try:
            install(pf, fetch_text(pf, getter), patches_dir)
            result["installed"].append(pf.filename)
        except Exception:
            result["failed"].append(pf.filename)
    return result
