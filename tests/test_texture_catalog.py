"""The PS2 texture-pack catalog reader: parsing, caching, matching and how each
entry becomes something PCTX can offer. No network (a fake getter)."""
import io
import json
import zipfile

import enhancements
import texture_catalog as T

PACKS = {
    "free-archive.yaml": """key: free-archive
name: 'Racer HD'
game: {title: 'Sample Racer', serials: [SLUS-20946]}
credits: [{creator: alice}]
type: ai_upscale
completeness: complete
access: {cost: free}
sources: [{kind: forum_thread, url: 'https://example.org/thread', primary: true}]
download: {status: resolved, url: 'https://archive.org/details/racer-hd', host: archive_org, kind: file}
""",
    "free-mega.yaml": """key: free-mega
name: 'Racer 4K'
game: {title: 'Sample Racer', serials: [SLUS-20946, SLES-50001]}
credits: [{creator: bob}]
access: {cost: free}
sources: [{kind: video, url: 'https://example.org/video', primary: true}]
download: {status: resolved, url: 'https://mega.nz/file/abc', host: mega, kind: file}
""",
    "paid.yaml": """key: paid
name: 'Racer Deluxe'
game: {title: 'Sample Racer'}
credits: [{creator: carol}]
access: {cost: paid}
sources: [{kind: creator_page, url: 'https://patreon.example/carol', primary: true}]
""",
    "dead.yaml": """key: dead
name: 'Racer Old'
game: {title: 'Sample Racer', serials: [SLUS-20946]}
access: {cost: free}
download: {status: dead, url: 'https://example.org/gone', host: other, kind: file}
""",
    "other-game.yaml": """key: other
name: 'Other'
game: {title: 'Another Game', serials: [SLUS-99999]}
access: {cost: free}
download: {status: resolved, url: 'https://github.com/x/y', host: github, kind: release}
""",
}


def fake_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, text in PACKS.items():
            z.writestr(f"catalog-main/data/packs/{name}", text)
        z.writestr("catalog-main/README.md", "x")
    return buf.getvalue()


def test_parse_and_cache_weekly(tmp_path):
    calls = []

    def getter(url):
        calls.append(url)
        return fake_zip()
    packs, err = T.load(str(tmp_path), getter=getter, now=1000)
    assert err is None and len(packs) == 5 and calls == [T.CATALOG_ZIP]
    again, _ = T.load(str(tmp_path), getter=getter, now=1000 + 3600)
    assert again == packs and len(calls) == 1                      # served from the cache

    def broken(url):
        raise OSError("offline")
    stale, err = T.load(str(tmp_path), getter=broken, now=1000 + T.MAX_AGE + 1)
    assert stale == packs and "using the copy from before" in err  # never lose what we knew
    empty, err = T.load(str(tmp_path / "none"), getter=broken)
    assert empty == [] and "couldn't download" in err


def test_candidates_for_a_disc(tmp_path):
    packs, _ = T.load(str(tmp_path), getter=lambda url: fake_zip(), now=0)
    cands = T.candidates_for(packs, "SLUS-20946", "Sample Racer")
    by_key = {c["catalog_key"]: c for c in cands}
    assert set(by_key) == {"free-archive", "free-mega", "paid"}       # dead link and other game left out
    assert by_key["free-archive"]["archive_org_id"] == "racer-hd" and by_key["free-archive"]["confidence"] == "catalog"
    assert by_key["free-mega"]["manual_url"] == "https://mega.nz/file/abc"
    assert by_key["paid"]["paid"] and by_key["paid"]["manual_url"] == "https://patreon.example/carol"
    assert by_key["paid"]["confidence"] == "catalog_title"            # no serials: matched on the title
    assert [c["catalog_key"] for c in cands] == ["free-archive", "free-mega", "paid"]   # auto, browser, paid
    assert T.candidates_for(packs, "SLES-50001")[0]["catalog_key"] == "free-mega"


def test_paid_and_browser_packs_are_never_recommended():
    files = []
    tex = [{"name": "p", "confidence": "catalog", "manual_url": "https://x", "paid": True},
           {"name": "m", "confidence": "catalog", "manual_url": "https://mega"},
           {"name": "a", "confidence": "catalog", "archive_org_id": "id"}]
    assert enhancements.recommend(files, tex).texture == 2
