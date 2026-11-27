"""Game enhancements: what PCSX2 needs on disk (and in its settings) for a
patch, cheat or texture pack to actually take effect, plus the matching and
recommendation logic shared by Classic and Big Picture. Qt-free and network-free.

How PCSX2 2.x decides what's active (from its source, pcsx2/Patch.cpp and
VMManager.cpp):

* Patch/cheat files are `<SERIAL>_<CRC>*.pnach` or `<CRC>*.pnach` in the
  `patches/` and `cheats/` folders.
* Every named `[group]` in a .pnach is OFF unless its exact (case-sensitive)
  name is listed in the game's settings file,
  `gamesettings/<SERIAL>_<CRC>.ini`, as repeated `Enable = <name>` lines under
  `[Patches]` or `[Cheats]`. Unnamed codes are always on.
* "Widescreen 16:9" and "No-Interlacing" groups are also switched on by the
  global EnableWideScreenPatches / EnableNoInterlacingPatches options;
  `Disable = <name>` turns one off for a single game.
* Cheats only run at all when [EmuCore] EnableCheats is on.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

WS_PATCH_NAME = "Widescreen 16:9"
NI_PATCH_NAME = "No-Interlacing"
PATCHES_SECTION = "Patches"
CHEATS_SECTION = "Cheats"


# ---------------------------------------------------------------- per-game settings file
def sanitize_serial(serial: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', "_", serial or "")


def game_settings_path(data_dir: str, serial: str, crc: str) -> str:
    """PCSX2's per-game settings file (VMManager::GetGameSettingsPath)."""
    name = f"{sanitize_serial(serial)}_{crc.upper():0>8}.ini" if serial else f"{crc.upper():0>8}.ini"
    return os.path.join(data_dir, "gamesettings", name)


def _read_lines(path: str) -> List[str]:
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return f.read().splitlines()
    except OSError:
        return []


def read_list(path: str, section: str, key: str = "Enable") -> List[str]:
    """All values of a repeated key (e.g. every `Enable = x`) in one section."""
    out, cur = [], None
    for line in _read_lines(path):
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            cur = s[1:-1].strip()
        elif cur == section and "=" in s and not s.startswith((";", "#")):
            k, v = s.split("=", 1)
            if k.strip() == key and v.strip():
                out.append(v.strip())
    return out


def write_lists(path: str, section: str, lists: Dict[str, Sequence[str]]) -> None:
    """Replace the given repeated keys (e.g. {'Enable': [...], 'Disable': [...]})
    in `section`, keeping every other line of the file as it was. Atomic."""
    lines = _read_lines(path)
    out: List[str] = []
    cur, found = None, False

    def emit_lists():
        for key, values in lists.items():
            for v in dict.fromkeys(values):            # de-duplicate, keep order
                out.append(f"{key} = {v}")

    for line in lines:
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            cur = s[1:-1].strip()
            out.append(line)
            if cur == section:
                found = True
                emit_lists()
            continue
        if cur == section and "=" in s and s.split("=", 1)[0].strip() in lists:
            continue                                     # replaced above
        out.append(line)
    if not found:
        if out and out[-1].strip():
            out.append("")
        out.append(f"[{section}]")
        emit_lists()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".pctx-tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(out).rstrip("\n") + "\n")
    os.replace(tmp, path)


def set_enabled(path: str, section: str, enable: Iterable[str], manage: Iterable[str],
                disable: Iterable[str] = ()) -> List[str]:
    """Make exactly `enable` (out of the names in `manage`) enabled for this game,
    leaving entries for names we don't manage untouched. `disable` names get a
    `Disable =` line (used for the globally auto-enabled WS/NI patches)."""
    manage, enable, disable = set(manage), list(dict.fromkeys(enable)), set(disable)
    keep = [n for n in read_list(path, section, "Enable") if n not in manage]
    keep_dis = [n for n in read_list(path, section, "Disable") if n not in manage]
    new_enable = keep + [n for n in enable if n not in keep]
    write_lists(path, section, {"Enable": new_enable, "Disable": keep_dis + sorted(disable - set(enable))})
    return new_enable


# ---------------------------------------------------------------- .pnach groups
@dataclass
class PatchGroup:
    name: str
    description: str = ""
    author: str = ""
    kind: str = "other"          # widescreen | nointerlace | fps | fix | quality | other
    recommended: bool = False

    def to_json(self) -> dict:
        return {"name": self.name, "description": self.description, "author": self.author,
                "kind": self.kind, "recommended": self.recommended}


_FIX_WORDS = re.compile(r"\b(fix|fixes|fixed|bug|crash|freez|hang|softlock|black screen|stutter|flicker|glitch|"
                        r"corrupt|missing|broken|sound fix|audio fix|boot|speed ?fix)", re.I)
_FPS_WORDS = re.compile(r"\b(\d{2,3}\s?fps|frame ?rate|framerate|unlock(ed)? fps)", re.I)
_QUALITY_WORDS = re.compile(r"\b(blur|bloom|depth of field|dof|shadow|fog|hd hud|native|sharpen|filter|lod|draw distance)", re.I)
KIND_LABEL = {"widescreen": "Widescreen", "nointerlace": "No interlacing", "fps": "Frame rate",
              "fix": "Game fix", "quality": "Visual tweak", "other": "Other"}


def classify(name: str, body: str = "", description: str = "") -> Tuple[str, bool]:
    """(kind, recommended) for a pnach group. Recommended = safe, broadly wanted
    improvements: widescreen, no-interlacing and bug fixes. Frame-rate unlocks
    and visual tweaks are offered but left off (they can change game speed or look)."""
    text = f"{name} {description}"
    low = body.lower()
    if name == WS_PATCH_NAME or "gsaspectratio=16:9" in low.replace(" ", "") or re.search(r"widescreen|16:9", text, re.I):
        return "widescreen", True
    if name == NI_PATCH_NAME or "gsinterlacemode" in low or re.search(r"interlac", text, re.I):
        return "nointerlace", True
    if _FPS_WORDS.search(text):
        return "fps", False
    if _FIX_WORDS.search(text):
        return "fix", True
    if _QUALITY_WORDS.search(text):
        return "quality", False
    return "other", False


def parse_groups(text: str) -> List[PatchGroup]:
    """Named groups in a .pnach, with their description/author and a kind."""
    groups: List[PatchGroup] = []
    cur: Optional[PatchGroup] = None
    body: List[str] = []

    def close():
        if cur is not None:
            cur.kind, cur.recommended = classify(cur.name, "\n".join(body), cur.description)
            groups.append(cur)

    for raw in (text or "").splitlines():
        line = raw.strip()
        m = re.match(r"^\[(.+)\]$", line)
        if m:
            close()
            cur, body = PatchGroup(name=m.group(1).strip()), []
            continue
        if cur is None:
            continue
        body.append(line)
        if line.lower().startswith("description="):
            cur.description = line.split("=", 1)[1].strip()
        elif line.lower().startswith("author="):
            cur.author = line.split("=", 1)[1].strip()
    close()
    return groups


def pnach_group_name(name: str) -> str:
    """The [group] name PCTX writes for a cheat (and so the name to enable)."""
    return (name or "Cheat").replace("[", "(").replace("]", ")").strip() or "Cheat"


# ---------------------------------------------------------------- texture-pack matching
_REGION_TAGS = re.compile(r"\((?:usa|europe|japan|en|fr|de|es|it|ntsc|pal|.*?,.*?)\)|\[.*?\]", re.I)
_ROMAN = {"ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6", "vii": "7", "viii": "8", "ix": "9", "x": "10",
          "xi": "11", "xii": "12", "xiii": "13"}
_STOP = {"the", "of", "a", "an", "and", "edition", "collector", "collectors", "greatest", "hits", "platinum", "ps2"}


def normalize_title(title: str) -> str:
    t = _REGION_TAGS.sub(" ", title or "")
    t = re.sub(r"[^\w\s]", " ", t.lower())
    words = [(_ROMAN.get(w, w)) for w in t.split()]
    return " ".join(words)


def title_tokens(title: str) -> List[str]:
    return [w for w in normalize_title(title).split() if w not in _STOP]


def title_variants(title: str) -> List[str]:
    """Search strings from most to least specific: the full title, the title
    without its subtitle, and with numerals converted, e.g.
    'DRAGON BALL Z - BUDOKAI TENKAICHI 3' -> ['dragon ball z budokai tenkaichi 3', 'dragon ball z']."""
    base = _REGION_TAGS.sub(" ", title or "").strip()
    base = re.sub(r"\b(collector'?s edition|greatest hits|platinum|special edition)\b", " ", base, flags=re.I)

    def clean(t: str) -> str:
        # As people write it (Roman numerals kept: "Final Fantasy XII" is how
        # packs are named); matching later normalises both sides anyway.
        t = re.sub(r"'", "", t.lower())
        return " ".join(re.sub(r"[^\w\s]", " ", t).split())

    out = [clean(base)]
    for sep in (" - ", ": ", " – "):
        if sep in base:
            head = clean(base.split(sep)[0])
            if len(head) >= 4:
                out.append(head)
    return [v for v in dict.fromkeys(out) if v]


TEXTURE_WORDS = ("texture", "textures", "retexture", "retextured", "upscale", "upscaled", "upscaling", "replacement",
                 "replacements", "texturepack")
# Things search engines return for a game's title that are never texture packs.
NOT_A_PACK = re.compile(r"\b(gameplay|video|longplay|walkthrough|playthrough|let'?s play|review|reviews|trailer|speedrun|"
                        r"soundtrack|ost|music|manual|scans?|press disc|demo disc|undub|translation|iso|rom|save ?data|"
                        r"memory card|cheats?|mod menu|stream|vs com|comvs|parking|completionist)\b", re.I)


def score_texture_candidate(cand: dict, title: str, serial: str = "", crc: str = "") -> Tuple[int, str]:
    """(score 0-100, confidence) for an unverified texture-pack search hit.
    confidence: 'verified' | 'high' | 'medium' | 'low'."""
    if cand.get("verified") is not False:
        return 100, "verified"
    hay = " ".join(str(cand.get(k) or "") for k in ("name", "github_repo", "archive_org_id", "description", "title"))
    hay_n = normalize_title(hay.replace("-", " ").replace("_", " "))
    words = set(hay_n.split())
    toks = title_tokens(title)
    if not toks:
        return 0, "low"
    # It has to say it's a texture pack, and not be a video/review/ROM/etc.
    if not (any(w in words for w in TEXTURE_WORDS) or re.search(r"texture|upscal", hay, re.I)):
        return 0, "low"
    if NOT_A_PACK.search(hay):
        return 0, "low"
    # Naming a different disc's serial means it's for another game/region.
    serials = {re.sub(r"[^A-Z0-9]", "", m.group(0).upper())
               for m in re.finditer(r"\b(S[CL][UEPK][SMA]|SCAJ|SLKA|PBPX)[-_ ]?\d{3}[._]?\d{2}\b", hay, re.I)}
    if serials and serial and re.sub(r"[^A-Z0-9]", "", serial.upper()) not in serials:
        return 0, "low"
    hit = sum(1 for t in toks if t in words)
    # Nearly every word of the title must be there: "Dragon Ball Z" alone
    # matches a dozen different games.
    if hit / len(toks) < 0.8:
        return 0, "low"
    score = int(60 * hit / len(toks))
    # the numbers in a title (sequel numbers) must match, or it's the wrong game
    nums = [t for t in toks if t.isdigit()]
    if nums and not all(n in words for n in nums):
        # A different sequel number ("Tenkaichi 2" for "Tenkaichi 3") is the
        # wrong game outright; no number at all is merely less certain.
        other = {w for w in words if w.isdigit() and len(w) <= 2} - set(nums)
        if other:
            return 0, "low"
        score -= 30
    if any(w in words for w in TEXTURE_WORDS):
        score += 20
    if re.search(r"pcsx2|ps2|playstation 2", hay, re.I):
        score += 10
    if serial and serial.replace("-", "").lower() in hay.replace("-", "").replace("_", "").lower():
        score += 25
    if crc and crc.lower() in hay.lower():
        score += 25
    if cand.get("downloadable"):
        score += 10
    score = max(0, min(99, score))
    conf = "high" if score >= 75 else "medium" if score >= 50 else "low"
    return score, conf


def rank_texture_candidates(cands: List[dict], title: str, serial: str = "", crc: str = "",
                            min_score: int = 35) -> List[dict]:
    """Score, drop obvious non-matches, de-duplicate and sort best-first."""
    seen, out = set(), []
    for c in cands:
        key = (c.get("github_repo") or c.get("archive_org_id") or c.get("name") or "").lower()
        if key in seen:
            continue
        seen.add(key)
        score, conf = score_texture_candidate(c, title, serial, crc)
        if c.get("ai_upscale"):
            score, conf = 1, "fallback"
        elif score < min_score:
            continue
        out.append(dict(c, score=score, confidence=conf))
    out.sort(key=lambda c: -c["score"])
    return out


CONFIDENCE_LABEL = {"verified": "Verified", "catalog": "Listed for your disc in the PS2 texture-pack catalog",
                    "catalog_title": "Listed for this game in the PS2 texture-pack catalog",
                    "high": "Likely match", "medium": "Possible match",
                    "low": "Unlikely match", "fallback": "Built from your own textures"}
CONFIDENCE_SCORE = {"verified": 100, "catalog": 95, "catalog_title": 85}


# ---------------------------------------------------------------- recommendations / checks
@dataclass
class Check:
    id: str
    level: str               # 'ok' | 'warn' | 'error' | 'info'
    text: str
    fix: str = ""            # id of an automatic fix, '' when there's nothing to do
    fix_label: str = ""

    def to_json(self) -> dict:
        return {"id": self.id, "level": self.level, "text": self.text, "fix": self.fix, "fixLabel": self.fix_label}


def health_checks(*, crc: Optional[str], crc_source: str, has_ini: bool, ini_flags: Dict[str, Optional[bool]],
                  cheats_installed: bool, cheats_enabled: int, textures_installed: bool,
                  patches_installed: bool, patches_enabled: int, ws_installed: bool) -> List[Check]:
    """Things that would stop an installed enhancement from working, each with
    an automatic fix where one exists."""
    out: List[Check] = []
    if not crc:
        out.append(Check("crc", "warn", "PCTX couldn't work out this game's CRC, so cheats can't be installed and "
                         "patches can't be matched to your exact disc. Scanning the disc image fixes this."))
    elif crc_source == "disc":
        out.append(Check("crc", "ok", f"CRC {crc} read from your disc image, so everything matches your exact copy."))
    else:
        out.append(Check("crc", "info", f"CRC {crc} comes from a database ({crc_source}); it may differ from your copy "
                         "if you have another revision. Add the disc image to be certain."))
    if not has_ini:
        out.append(Check("ini", "warn", "PCSX2.ini wasn't found, so PCSX2's own switches can't be checked. Start PCSX2 once."))
        return out
    if cheats_installed and cheats_enabled and ini_flags.get("EnableCheats") is False:
        out.append(Check("cheats_off", "warn", "Cheats are turned off in PCSX2, so the cheats you enabled won't run.",
                         "EnableCheats", "Turn on cheats"))
    if textures_installed and ini_flags.get("LoadTextureReplacements") is False:
        out.append(Check("tex_off", "warn", "Texture replacement is turned off in PCSX2, so the installed pack won't load.",
                         "LoadTextureReplacements", "Turn on texture replacement"))
    if ws_installed and ini_flags.get("EnableWideScreenPatches") is False and not patches_enabled:
        out.append(Check("ws_off", "info", "A widescreen patch is installed but not enabled for this game.",
                         "", ""))
    if patches_installed and not patches_enabled:
        out.append(Check("patch_none", "info", "Patches are installed for this game but none of them are switched on."))
    if not any(c.level in ("warn", "error") for c in out):
        out.append(Check("all_ok", "ok", "Nothing is blocking the enhancements you've chosen."))
    return out


@dataclass
class Recommendation:
    patches: Dict[int, List[str]] = field(default_factory=dict)  # patch-file index -> group names
    texture: int = -1
    summary: List[str] = field(default_factory=list)


def recommend(patch_files: List[dict], textures: List[dict]) -> Recommendation:
    """The "just make it better" preset: every recommended patch group (widescreen,
    no-interlacing, fixes) from the file that matches the disc, and the best
    texture pack if it's verified or a likely match. Cheats are never recommended."""
    rec = Recommendation()
    exact = [i for i, p in enumerate(patch_files) if p.get("exact")]
    use = exact or ([0] if len(patch_files) == 1 else [])
    for i in use:
        names = [g.name for g in patch_files[i].get("groups", []) if g.recommended]
        if names:
            rec.patches[i] = names
            kinds = sorted({KIND_LABEL[g.kind] for g in patch_files[i]["groups"] if g.recommended})
            rec.summary.append(", ".join(kinds) + " patch" + ("es" if len(names) > 1 else ""))
    for i, t in enumerate(textures):
        # only something PCTX can download and install by itself
        if t.get("manual_url") or t.get("ai_upscale"):
            continue
        if t.get("confidence") in ("verified", "catalog", "high"):
            rec.texture = i
            rec.summary.append(f"Texture pack: {t.get('name') or 'best match'}")
            break
    return rec
